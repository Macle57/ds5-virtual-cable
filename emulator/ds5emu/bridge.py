"""`BridgeBackend` — the live seam between the USB/IP emulator and a real
Bluetooth-connected DualSense.

This is the component that makes the emulated *wired* controller real. Four
pipes run at once, in both directions:

    USB interrupt IN  0x84   <-  BT input 0x31 type 0x01   (input state)
    USB interrupt OUT 0x03   ->  BT output 0x31            (SetState passthrough)
    USB iso OUT 0x01         ->  BT output 0x39            (speaker + haptics)
    USB iso IN  0x82         <-  BT input 0x31 type 0x02   (microphone)

Everything protocol-shaped is imported from Phase 1 (`prototype/ds5bridge`) or
from `translate.py`; this module owns only the plumbing: three threads, four
ring buffers, and the rate conversions between the USB timebase and the
Bluetooth one.

Threading model
---------------

    reader  thread  hidapi read loop, ~476 Hz. Translates control payloads to
                    USB 0x01 and decodes mic Opus frames. Never blocks on
                    anything but `hid.read()`, which releases the GIL.
    pump    thread  clock-driven `ds5bridge.pacing.Pacer` at 46.875 reports/s
                    (two 10.667 ms frames per 0x39). Resamples, Opus-encodes
                    and writes.
    writer  thread  coalesced SetState passthrough, so a host polling at 250 Hz
                    with unchanged state costs zero Bluetooth airtime.

    the asyncio server thread only ever touches lock-guarded byte buffers:
    `write_audio_out`, `read_audio_in` and `read_input_report` are O(n) memcpy
    and never wait on Bluetooth.

Rate arithmetic (all exact, no drift)
-------------------------------------

    one 0x39 report = 2 frames = 21.3333 ms
                    = 1024 samples of 48 kHz USB audio
                    = 960 samples at 45 kHz (the "45 kHz trick", STATUS.md §5.6)
                    =  64 samples at 3 kHz per haptic channel
    48000/45000 = 16/15 and 48000/3000 = 16, so 1024 -> 960 and 1024 -> 64 are
    integer ratios: a 1024-sample USB block maps onto exactly one 0x39 report.

THE THREE CLOCK DOMAINS (Phase 3c — read this before touching the buffering)
----------------------------------------------------------------------------
This backend sits between clocks that are NOT the same thing, and the merge of
Phase 3a and Phase 3b is precisely the seam between the first two:

  U — the USB domain.  `ds5emu.timing.FrameClock`, 1 ms service intervals off
      `time.perf_counter()`. It paces URB *completion*, and therefore sets the
      long-run rate at which `write_audio_out()` is fed and `read_audio_in()`
      is drained: 48 000 frames/s each, by construction. Note the data itself
      moves in ~10 ms bursts (usbaudio batches 10 packets per URB and submits
      one or two URBs every ~15 ms); only the *average* is smooth.

  B — the Bluetooth send domain.  `ds5bridge.pacing.Pacer` in `_pump_loop`,
      one tick per 0x39 report. Also off `time.perf_counter()`.

  C — the controller's own crystal.  Sets when microphone Opus payloads
      actually arrive (~100/s). Nothing on this PC can influence it.

**U and B are the same oscillator and an exact integer ratio, so they cannot
drift against each other:**

    46.875 reports/s x 1024 samples/report = 48 000 samples/s   (48000/46.875)

There is no rate conversion, no accumulator and no resampling error between the
USB timebase and the 0x39 timebase — the pump is not a second clock, it is a
1/1024 divider off the same one. What CAN go wrong between U and B is purely
*phase*: burst arrival against a 21.3 ms tick. That is a buffering problem, and
it is solved by an explicit target depth (`AUDIO_Q_*` below), not by rate
steering. Double-clocking is avoided by the pump never sleeping on behalf of
the USB side and the USB side never waiting on the pump: they meet only in
`_out_ring`, which is bounded.

**U and C are different oscillators, so the microphone path is the one place
real drift accumulates.** Two crystals at +/-100 ppm differ by up to 9.6
samples/s. Over an hour that is ~35 000 samples = 0.7 s, far more than any
sane buffer, so the mic ring is *steered* rather than merely sized: see
`_mic_depth_correction()`. The correction is bounded to one 48 kHz frame per
`read_audio_in()` call (<= 1000 frames/s, i.e. +/-2 %, ~200x the worst crystal
error) so it is always a slew and never a jump.

**No Bluetooth I/O ever happens on the USB request path.** `write_audio_out`,
`read_audio_in`, `read_input_report`, `write_output_report`, `set_alt_setting`
and `on_uac_control` are all called from the single asyncio server thread that
also has to hit 1 ms isochronous deadlines. Anything that talks to hidapi —
microphone arming (which sleeps 20 ms twice) and UAC volume mirroring — is
posted to `_control_q` and executed by the writer thread. Blocking that thread
for 40 ms in `SET_INTERFACE` would stall every endpoint at exactly the moment
the audio stream opens.

Hardware gotchas honoured here (docs/STATUS.md §8)
--------------------------------------------------

  * `mic_enabled=True` (report 0x39 `pkt[4] = 0x7F`) whenever the microphone is
    armed — with 0x7E the controller silently stops sending mic payloads the
    instant playback starts, and a wired DualSense does both at once.
  * `audio_buffer_length = 48`, which must stay inside [16, 128] or the
    controller discards every report with no error of any kind.
  * every spin-wait goes through `Pacer`, which spins on `time.sleep(0)`; a bare
    `pass` never releases the GIL and starves the other three threads.
  * `hid.write()`'s return value is meaningless on Windows (it returns the
    padded buffer size), so it is never used to validate a write.
"""

from __future__ import annotations

import fractions
import logging
import threading
import time
from collections import deque

from . import _bootstrap  # noqa: F401  (sys.path side effect)
from . import descriptors as D
from . import translate as T
from .backend import Backend

import numpy as np  # noqa: E402
import av  # noqa: E402
from av.audio.frame import AudioFrame  # noqa: E402

from ds5bridge import audio as A  # noqa: E402
from ds5bridge import device as DEV  # noqa: E402
from ds5bridge import protocol as P  # noqa: E402
from ds5bridge.crc import fill_feature_checksum  # noqa: E402
from ds5bridge.pacing import Pacer, TimerResolution  # noqa: E402

log = logging.getLogger("ds5emu.bridge")

# --- fixed rates ------------------------------------------------------------

USB_RATE = D.AUDIO_SAMPLE_RATE          # 48000
USB_OUT_CH = D.AUDIO_OUT_CHANNELS       # 4: [spkL, spkR, hapL, hapR]
USB_IN_CH = D.AUDIO_IN_CHANNELS         # 2
BYTES_PER_SAMPLE = D.AUDIO_BYTES_PER_SAMPLE

USB_OUT_FRAME_BYTES = USB_OUT_CH * BYTES_PER_SAMPLE   # 8 bytes per 48 kHz frame
USB_IN_FRAME_BYTES = USB_IN_CH * BYTES_PER_SAMPLE     # 4

FRAMES_PER_REPORT_39 = 2
REPORT_39_MS = A.FRAME_MS * FRAMES_PER_REPORT_39      # 21.3333...

#: Report 0x39's audio_buffer_length. MUST be in [16, 128] (FINDINGS banner).
AUDIO_BUFFER_LENGTH = 48
assert 16 <= AUDIO_BUFFER_LENGTH <= 128

# --- buffer sizing ----------------------------------------------------------

#: Cap the host->controller PCM ring. Anything older than this is stale by the
#: time Bluetooth could carry it, so the oldest bytes are dropped instead.
AUDIO_OUT_RING_MS = 120
AUDIO_OUT_RING_BYTES = int(AUDIO_OUT_RING_MS * USB_RATE / 1000) * USB_OUT_FRAME_BYTES

# --- the U -> B buffering policy (Phase 3c) ---------------------------------
# Encoded frames waiting for their 0x39 tick. Because U and B share an
# oscillator and an exact ratio, the steady-state depth is whatever the startup
# burst left in it and it never self-corrects -- so it has to be *chosen*, not
# inherited. Depth is the audio latency the host pays, one Opus frame = 10.667 ms.
#
#   TARGET  4 frames = 42.7 ms   covers the ~16 ms Windows audio-engine burst
#                                period plus scheduling jitter with room to spare
#   HIGH    8 frames = 85.3 ms   above this the oldest frames are dropped back to
#                                TARGET and counted; the alternative is latency
#                                that grows and never comes back
#   START   TARGET             transmission begins only once the queue is at target,
#                                so the first 0x39 is not followed by silence
AUDIO_Q_TARGET_FRAMES = 4
AUDIO_Q_HIGH_FRAMES = 8
assert AUDIO_Q_TARGET_FRAMES < AUDIO_Q_HIGH_FRAMES

#: Microphone jitter buffer. Bluetooth delivers 10 ms bursts; the host asks in
#: 1 ms slices, so a little slack removes almost all underruns.
MIC_RING_MS = 200
MIC_RING_BYTES = int(MIC_RING_MS * USB_RATE / 1000) * USB_IN_FRAME_BYTES
MIC_PRIME_MS = 25
MIC_PRIME_BYTES = int(MIC_PRIME_MS * USB_RATE / 1000) * USB_IN_FRAME_BYTES

# --- the C -> U drift governor (Phase 3c) -----------------------------------
# The controller's crystal and this PC's are independent, so the mic ring drifts
# for real. Steer it back towards MIC_PRIME_MS by adding or removing at most ONE
# 48 kHz frame per read_audio_in() call: at 1000 calls/s that is a +/-2 %
# correction authority against a crystal error of order 0.01 %, and one sample
# per millisecond is inaudible. Outside the band nothing is touched at all, so
# in normal operation the governor does nothing most of the time.
MIC_LOW_MS = 10
MIC_HIGH_MS = 90
MIC_LOW_BYTES = int(MIC_LOW_MS * USB_RATE / 1000) * USB_IN_FRAME_BYTES
MIC_HIGH_BYTES = int(MIC_HIGH_MS * USB_RATE / 1000) * USB_IN_FRAME_BYTES
assert MIC_LOW_BYTES < MIC_PRIME_BYTES < MIC_HIGH_BYTES < MIC_RING_BYTES

#: Stop transmitting 0x39 after this long with no host audio: it saves
#: Bluetooth airtime and, on a 10 %-battery controller, real power.
AUDIO_IDLE_TIMEOUT = 0.5

#: Minimum spacing between two SetState passthroughs, and the interval at which
#: an unchanged body is refreshed (0 disables the refresh).
SETSTATE_MIN_INTERVAL = 0.006
SETSTATE_REFRESH = 0.0

#: Send the lightbar-setup "light out" control at every connect, the way Linux
#: `hid-playstation`'s `dualsense_reset_leds` does, with the default blue
#: painted in the same report.
#:
#: ON, on evidence (2026-09-03, two pads, both over Bluetooth, photo-verified):
#: a freshly connected DualSense IGNORES every lightbar colour write until a
#: host has sent this control once -- the game's colour, remote mode's red and
#: the battery flashes all silently did nothing, while rumble and the player
#: LEDs in the very same reports were applied. One report carrying the setup
#: AND a colour is enough, so the prime also paints `P.DEFAULT_LIGHTBAR` and
#: the pad never goes dark. The earlier "off: Miles Morales set it fine"
#: reading was a libScePad title, which sends this control itself -- it hid
#: the gap rather than disproving it. The host's own replayed lightbar, if it
#: has one, follows straight after and wins. docs/FINDINGS.md "lightbar setup".
PRIME_LIGHTBAR_FADE_OUT = True

# --- factory-test feature reports 0x80 / 0x81 (Phase 4c) --------------------
# The DualSense's whole factory-diagnostics channel is one request/response
# pair: SET feature 0x80 carries [deviceId, actionId, params...], GET feature
# 0x81 returns [deviceId, actionId, status, 56-byte page]. daidr/dualsense-tester
# drives its "Factory Info" and "Diagnostics" panels entirely through it.
# docs/FINDINGS.md "Factory-test feature reports 0x80 / 0x81" has the full
# layout, the page protocol and the per-field command table.

FEATURE_TEST_CMD = 0x80
FEATURE_TEST_RESULT = 0x81

#: Feature report 0x08 -- DS_FEATURE_REPORT_BLUETOOTH_CONTROL (nondebug/
#: dualsense). Action 0x02 tells the pad to drop its Bluetooth link, and with
#: no console to fall back to it powers off. CAPTURED on this machine
#: 2026-08-31 (docs/wired-gap-findings.md symptom 3): TLOU writes `08 02 00...`
#: to a redundant pad and the link is dead 26 ms later. `power_off_pad()`
#: sends the same command deliberately, for PS+Triangle and the idle
#: off-timer. Host-issued 0x08 writes stay recorded-and-dropped.
FEATURE_BLUETOOTH_CONTROL = 0x08
BT_CONTROL_DISCONNECT = 0x02
#: Feature 0x08 is `count=47` in the pad's OWN Bluetooth report descriptor
#: (read back with hidapi 2026-09-03), not the 63 of the 0x80 test channel;
#: TLOU's captured kill was the same 47 + id = 48 bytes over USB. A 63-byte
#: payload is refused by the HID stack before it reaches the air
#: (`HidD_SetFeature` fails, hidapi returns -1) -- MEASURED: 64-byte write
#: rc=-1 and the pad stays on; 48-byte signed write rc=48 and the pad is off
#: within a second. The 0x53-seeded CRC still lives in the last 4 bytes.
FEATURE_BLUETOOTH_CONTROL_LEN = 47

#: Both are `95 3f` in the HID report descriptor: 63 data bytes after the id.
#: On Bluetooth the 4-byte CRC lives in the LAST 4 of those 63 -- it does not
#: make the report longer (STATUS.md section 5 gotcha 6, confirmed for feature 0x05).
#:
#: VERIFIED on hardware 2026-08-25: the *Bluetooth* report descriptor declares
#: `0x80` and `0x81` as `count=63, size=8`, byte for byte the same as the wired
#: one, and a signed 63-byte 0x80 is answered while an unsigned one is refused.
#: See docs/FINDINGS.md "Measured on hardware".
FEATURE_TEST_PAYLOAD_LEN = 63

#: What a real wired DualSense returns from GET_REPORT(Feature, 0x81) when no
#: factory-test command is outstanding: the report id and 63 zero bytes.
#: MEASURED on the physical wired unit 2026-08-27 -- it answers, it never
#: STALLs. See `_read_test_result`.
FEATURE_TEST_IDLE = bytes([FEATURE_TEST_RESULT]) + bytes(FEATURE_TEST_PAYLOAD_LEN)

#: Extra plain GET-only feature reports to prefetch at open, beyond 0x05
#: (calibration, read as part of the extended-mode flip) and 0x20 (firmware).
#: 0x22 is the BT patch version, which Factory Info reads directly.
#:
#: VERIFIED on hardware 2026-08-25: over Bluetooth 0x22 answers 64 bytes (id +
#: 63), no CRC needed for a GET, and its `u32` at offset 31 is the patch
#: version Factory Info shows. Prefetching it is correct -- it is static.
#:
#: 0x09 AND 0x0b ADDED 2026-08-27, and 0x09 is the important one.
#: `emulator/tools/hid_diff_probe.py` was run against a real *wired* DualSense
#: and against this emulator on the same machine, through the same HidUsb stack,
#: on the same day. A real wired unit answers feature 0x09 with 20 bytes and
#: 0x0b with 42; this emulator STALLed both. libScePad -- the Sony controller
#: library every Sony PC port links -- reads 0x09 as the FIRST thing it does to
#: a DualSense and uses the MAC in it as the device's identity. In the
#: open-source reimplementation (WujekFoliarz/duaLib, src/source/duaLib.cpp:145)
#: the whole registration is inside `if (getMacAddress(...))`, and
#: `getMacAddress` for a DualSense is one `hid_get_feature_report` of 0x09
#: (src/source/duaLibUtils.cpp:182-199). A STALL there means the pad is never
#: registered at all, which is exactly the "The Last of Us Part 1 sees no input"
#: report -- see docs/wired-gap-findings.md.
PREFETCH_FEATURES = (0x09, 0x0B, 0x20, 0x22)

#: Feature reports whose LAST FOUR BYTES are a Bluetooth CRC-32 that a wired
#: DualSense leaves as zero.
#:
#: MEASURED, both transports, 2026-08-27: for 0x05, 0x09, 0x0b, 0x20 and 0x22
#: the wired unit ends every one of them in `00 00 00 00` while the Bluetooth
#: unit ends them in a CRC. Forwarding the Bluetooth bytes verbatim therefore
#: put four bytes of Bluetooth-only checksum into a report that is supposed to
#: be a USB report. Nothing observed reads them, but "byte-identical to a real
#: wired DualSense" is the whole claim of this project, so they are zeroed.
FEATURE_CRC_TRAILER = frozenset({0x05, 0x09, 0x0B, 0x20, 0x22})

#: Bytes of feature 0x0b that a *wired* DualSense actually publishes.
#:
#: MEASURED 2026-08-27: over Bluetooth 0x0b carries, after the host MAC, a
#: pairing-slot count and a link-key blob. The wired unit publishes zeros there.
#: Serving the Bluetooth bytes verbatim would both differ from the ground truth
#: and export the controller's pairing material to anything that can open the
#: HID device.
#:
#: The wired layout, from the physical unit:
#:     [0]      report id 0x0b
#:     [1..6]   client (controller) MAC, little-endian
#:     [7..10]  08 25 00 00
#:     [11..16] host MAC, little-endian
#:     [17..]   zero
#: so keep 17 bytes and zero the rest.
FEATURE_0B_WIRED_PREFIX = 17

#: Feature reports that carry the CONTROLLER'S OWN Bluetooth MAC in bytes
#: [1..6] (little-endian, so the first textual octet of the address is at
#: byte [6]).
FEATURE_CLIENT_MAC = frozenset({0x09, 0x0B})

#: The locally-administered bit of the first MAC octet, i.e. of report
#: byte [6] of the reports above.
#:
#: WHY THE SERVED MAC IS ALTERED AT ALL (2026-08-31, the "TLOU powers the
#: pad off" bug). libScePad -- the Sony pad library every Sony PC port
#: links -- de-duplicates controllers by the MAC in feature 0x09. When it
#: sees a *wired* DualSense whose 0x09 MAC equals that of a *Bluetooth*
#: DualSense it also has open, it treats them as one pad on two transports
#: and does what a PS5 does when the cable is plugged in: it tells the
#: Bluetooth twin to drop its link -- a feature 0x08 write, action 0x02
#: (0x08 is DS_FEATURE_REPORT_BLUETOOTH_CONTROL in the public research,
#: nondebug/dualsense) -- and a DualSense told to drop its link with no
#: console to fall back to simply powers off.
#:
#: That is fatal here, because our "wired" pad IS the Bluetooth pad. The
#: captured sequence (emulator/ds5emu/capture.py dump, 2026-08-31, The Last
#: of Us Part I running with an open handle to the raw Bluetooth pad while
#: the virtual pad attached):
#:
#:     +15.7232  usb.get_feature  id=0x09 -> 20B     (TLOU learns our MAC)
#:     +15.7496  bt.read.error #1                    (the pad's link is dying,
#:                                                    26 ms later)
#:     +15.7503  usb.set_feature  id=0x08  08 02 00... (the same kill command,
#:                                                    aimed at the virtual pad;
#:                                                    recorded, never forwarded)
#:     +15.7532  link.death                          (pad off until PS press)
#:
#: We wrote nothing to the controller in that window -- the power-off went
#: down TLOU's own pre-existing handle to the Bluetooth device. HidHide does
#: not sever handles a game already holds, so hiding cannot prevent it; the
#: only robust fix is for the virtual pad to never claim the same identity.
#: Flipping the locally-administered bit keeps the MAC stable and unique per
#: controller (it is derived from the real address) while guaranteeing it
#: never collides with the real address on the air. Verified: with the bit
#: flipped the same TLOU scenario leaves the pad on; with it unflipped the
#: pad dies within seconds, 100% reproducible.
WIRED_MAC_LA_BIT = 0x02

#: How long a USB GET_REPORT(0x81) may wait for the writer thread to come back
#: from Bluetooth. This is the ONE place the USB request path waits on hidapi,
#: and it is a control transfer, never an isochronous deadline -- but it still
#: runs on the asyncio server thread, so the budget is deliberately small. The
#: tester allows itself 1000 ms per command and sleeps 10 ms between polls, so
#: a stall here costs it one poll, not the command.
FEATURE_BT_TIMEOUT = 0.06

#: Read-only factory-test subcommands, `(deviceId, actionId): why it is safe`.
#:
#: THE RULE: a subcommand gets in only if the tester issues it as a pure read
#: for Factory Info or Diagnostics AND its action id is a READ_*/GET_* in
#: `DualSenseTestActionId`, or (the AUDIO pair below, and only that pair) it
#: drives a transient output that touches no persistent state. Everything else
#: -- every WRITE_*, ERASE_*, AGING_*, SET_*, NVS_*, TEST_ACTION_BOOTLOADER_*,
#: the whole 0x84/0x85 individual-data channel and feature report 0x09 as a
#: *write* -- keeps the record-and-drop behaviour. Nothing here can re-pair the
#: controller, touch its flash or change a setting that outlives a power cycle.
TEST_COMMAND_ALLOWLIST: dict[tuple[int, int], str] = {
    # deviceId 1 = SYSTEM
    (0x01, 0x04): "READ_PCBAID (legacy PCBA id, 6 bytes)",
    (0x01, 0x09): "GET_MCU_UNIQUE_ID (9 bytes)",
    (0x01, 0x11): "READ_PCBAID_FULL (24 bytes)",
    (0x01, 0x13): "READ_SERIAL_NUMBER (32 bytes, Shift-JIS)",
    (0x01, 0x15): "READ_ASSEMBLE_PARTS_INFO (32 bytes)",
    (0x01, 0x18): "READ_BATTERY_BARCODE (32 bytes)",
    (0x01, 0x1A): "READ_VCM_LEFT_BARCODE (32 bytes)",
    (0x01, 0x1C): "READ_VCM_RIGHT_BARCODE (32 bytes)",
    # deviceId 4 = ANALOG_DATA
    (0x04, 0x03): "BATTERY (battery voltage in mV)",
    # deviceId 6 = AUDIO. THE ONE PAIR THAT IS NOT A READ, and it is here on
    # purpose -- see docs/wired-gap-findings.md symptom 2.
    #
    # These two are the entire "Speaker / Headphone 1 kHz sine wave" button in
    # dualsense-tester (`controlWaveOut`, src/utils/dualsense/ds.util.ts:673).
    # Action 2 (WAVEOUT_CTRL) starts and stops the controller's own built-in
    # tone generator; action 4 selects which output it comes out of. Blocking
    # them was measured, 2026-08-27, to be exactly why that button does nothing
    # through the emulator while it works on the same pad over plain Bluetooth.
    #
    # WHY THIS DOES NOT BREAK THE RULE THE REST OF THIS TABLE FOLLOWS.
    # The rule protects the controller's persistent state: pairing, flash,
    # calibration, settings. Neither of these touches any of it. WAVEOUT_CTRL
    # toggles a tone that stops the moment it is sent [0,1,0] and in any case
    # does not survive a power cycle; action 4 is the VERIFY of the
    # BUILTIN_MIC_CALIB_DATA family, not its _SEND (2) or _STORE (3), which are
    # the ones that would write, and which stay blocked.
    (0x06, 0x02): "WAVEOUT_CTRL (start/stop the built-in 1 kHz test tone)",
    (0x06, 0x04): "BUILTIN_MIC_CALIB_DATA_VERIFY (wave-out path select)",
    # deviceId 5 = TOUCH -- read-only, but the controller does not answer these
    # over Bluetooth, so expect a stall. Harmless either way.
    (0x05, 0x02): "SOLOMON_UID (touchpad unique id)",
    (0x05, 0x04): "SOLOMON_VERSION (touchpad firmware version)",
    # deviceId 7 = ADAPTIVE_TRIGGER. Action 37 is named both
    # CYPRESS_SIGNAL_STEP_2 and READ_TRACABILITY_INFO; 36 writes and 38 erases
    # the same data, so 37 is the read. The one ambiguous entry -- see FINDINGS.
    (0x07, 0x25): "READ_TRACABILITY_INFO (AT serial no + motor info)",
    # deviceId 9 = BLUETOOTH
    (0x09, 0x02): "READ_BDADR (BD MAC address, 6 bytes)",
    # deviceId 0x70 = TELEMETRY (the Diagnostics panel's only command)
    (0x70, 0x01): "GET_INFO (paged usage/connection/button counters)",
}

# --- link-loss handling (Phase 4a) ------------------------------------------
# Two independent things have to happen when the controller goes away, and only
# one of them was implemented before Phase 4a.
#
# 1. NOTICE. `hid.read()` on a vanished device usually raises, and five
#    consecutive raises trip the reconnect path — but a Bluetooth link can also
#    simply go *quiet*, in which case every read times out cleanly and returns
#    b"" forever and nothing ever trips. So there is a watchdog: no control
#    payload for LINK_DEAD_S while the device is nominally open means the link
#    is gone, full stop.
#
# 2. STOP PRESSING THINGS. `repeat_stale_input` is right in normal operation —
#    a wired DualSense emits a report every 4 ms whether or not anything moved —
#    but repeating forever hands the game whatever was held when the link died.
#    After INPUT_NEUTRAL_S of staleness the repeated report is neutralised
#    (sticks centred, buttons released, gyro zeroed) while the device stays
#    attached, so the reconnect is invisible to the game rather than fatal to it.
#
# Chosen over a clean detach on purpose: detaching tears the device out from
# under a running game, and every game tested treats that as "controller
# removed" and pauses to a "reconnect your controller" screen — from which it
# does NOT always recover when the device comes back on a different USB port.
LINK_DEAD_S = 4.0
INPUT_NEUTRAL_S = 1.0

# --- the control-channel input fallback (Phase 5) ---------------------------
# Windows' GameInput service gates HID *input report* delivery on gamepad
# focus: while the foreground window belongs to a GameInput-client application
# -- Google Chrome 15x is one, with any window on any page, even about:blank --
# every OTHER handle on every DualSense in the system reads nothing at all.
# MEASURED on this machine 2026-09-01 (docs/focus-gating-findings.md): the pad
# streams 0x31 at ~576/s, a Chromium window takes focus, and within a second
# both this bridge's handle AND an independent raw hidapi handle go to exactly
# zero -- every read times out empty, no error, no 0x01 minimal-mode reports,
# nothing -- then resume ~1 s after focus leaves. Stop GameInputSvc and the
# identical focus flip starves nothing, 27 s clean at full rate. HidHide does
# not help: the gate survives a pad power-cycle with the cloak active, so the
# session-0 service is not subject to the per-image whitelist.
#
# The way through is that the gate stops only the interrupt-IN queue. The
# control channel keeps working the whole time -- feature reads succeed, and,
# decisively, GET_REPORT(Input, 0x31) answers with the pad's CURRENT full
# extended state in 5-20 ms (measured mid-gate: live sticks, advancing seq).
# So when the stream has been silent for POLL_AFTER_S while the link is
# nominally up, the reader polls GET_REPORT(Input) at up to 1/POLL_INTERVAL_S
# and feeds the replies through the exact same translate/intercept path as
# streamed reports. The moment a streamed report arrives, the polls stop.
#
# Interactions, all deliberate:
#   * successful polls refresh `_latest_input_at`, so neither the link
#     watchdog (LINK_DEAD_S) nor neutralise-on-stale (INPUT_NEUTRAL_S) fires
#     while the fallback is carrying input -- the game keeps playing and the
#     dashboard stays live;
#   * a pad that is genuinely OFF also goes stream-silent, but its polls FAIL,
#     so after POLL_MAX_FAILURES the fallback stands down and the watchdog
#     declares the link dead exactly as before this fallback existed;
#   * microphone payloads (type 0x02) cannot be polled -- GET_REPORT answers
#     control state only -- so mic capture degrades to silence while a gate is
#     active. Input is the product; that trade is right.
#
#: How long the stream must be silent before the first poll. Well above any
#: normal Bluetooth burst gap (tens of ms), well below INPUT_NEUTRAL_S, so
#: the game never sees a neutralised report during a mere focus flip.
POLL_AFTER_S = 0.15
#: Minimum spacing between poll attempts. ~125 Hz ceiling; each poll is a
#: 5-20 ms control-channel round trip, so the realised rate self-paces below
#: that. The USB host polls at 250 Hz and repeats the freshest state between
#: arrivals, exactly as it does for normal Bluetooth burst gaps.
POLL_INTERVAL_S = 0.008
#: `hid.read()` timeout while the fallback is active. Short, so the loop both
#: notices the stream returning immediately and holds the poll cadence.
POLL_READ_MS = 4
#: Consecutive poll failures that stand the fallback down (until the stream
#: itself returns). Keeps a powered-off pad's death detection on the
#: watchdog's schedule instead of retrying a dead control channel forever.
POLL_MAX_FAILURES = 3


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


class _ByteRing:
    """Bounded FIFO of bytes, safe for one producer and one consumer.

    Overflow drops the *oldest* bytes: for live audio the newest data is always
    the useful data, and a growing buffer would turn into growing latency.
    """

    def __init__(self, cap: int):
        self.cap = cap
        self._buf = bytearray()
        self._lock = threading.Lock()
        self.dropped = 0
        self.underruns = 0
        #: Bytes of silence that had to be invented because the ring was short.
        self.underrun_bytes = 0
        #: Bytes discarded by `drop_oldest` (drift steering), kept apart from
        #: `dropped` so an overflow and a deliberate skew correction never look
        #: like the same event.
        self.skew_dropped = 0
        self.peak = 0

    def __len__(self) -> int:
        with self._lock:
            return len(self._buf)

    def write(self, data: bytes) -> None:
        with self._lock:
            self._buf += data
            over = len(self._buf) - self.cap
            if over > 0:
                del self._buf[:over]
                self.dropped += over
            if len(self._buf) > self.peak:
                self.peak = len(self._buf)

    def drop_oldest(self, n: int) -> int:
        """Discard up to `n` bytes from the front. Returns how many went.

        This is the drift governor's only tool on the overfull side; it is
        counted separately from overflow so the two are never confused.
        """
        with self._lock:
            n = min(n, len(self._buf))
            if n:
                del self._buf[:n]
                self.skew_dropped += n
            return n

    def take_all(self, limit: int | None = None) -> bytes:
        with self._lock:
            n = len(self._buf) if limit is None else min(limit, len(self._buf))
            out = bytes(self._buf[:n])
            del self._buf[:n]
            return out

    def read_exact(self, n: int, fill: bytes = b"\0") -> bytes:
        """Pop exactly `n` bytes, padding with `fill` on underrun."""
        with self._lock:
            have = min(n, len(self._buf))
            out = bytes(self._buf[:have])
            del self._buf[:have]
            if have < n:
                self.underruns += 1
                self.underrun_bytes += n - have
                out += fill * (n - have)
            return out

    def clear(self) -> None:
        with self._lock:
            self._buf.clear()


class _StreamResampler:
    """Stateful 48 kHz -> `out_rate` stereo resampler (swresample via PyAV).

    Stateful matters: the haptic path decimates 48 kHz to 3 kHz, a factor of 16,
    which needs a real anti-aliasing filter with memory across blocks. Naive
    per-block picking would fold everything above 1.5 kHz back into the audible
    haptic band.
    """

    def __init__(self, out_rate: int, in_rate: int = USB_RATE):
        self.in_rate = in_rate
        self.out_rate = out_rate
        self._r = av.AudioResampler(format="flt", layout="stereo", rate=out_rate)
        self._tb = fractions.Fraction(1, in_rate)
        self._pts = 0

    def reset(self) -> None:
        self._r = av.AudioResampler(format="flt", layout="stereo", rate=self.out_rate)
        self._pts = 0

    def push(self, pcm: np.ndarray) -> np.ndarray:
        """pcm: float32 (n, 2) at `in_rate`. Returns float32 (m, 2) at `out_rate`."""
        if pcm.shape[0] == 0:
            return np.zeros((0, 2), np.float32)
        f = AudioFrame.from_ndarray(
            np.ascontiguousarray(pcm.reshape(1, -1), dtype=np.float32),
            format="flt", layout="stereo",
        )
        f.sample_rate = self.in_rate
        f.time_base = self._tb
        f.pts = self._pts
        self._pts += pcm.shape[0]
        out = [x.to_ndarray().reshape(-1, 2) for x in self._r.resample(f)]
        if not out:
            return np.zeros((0, 2), np.float32)
        return np.concatenate(out, axis=0).astype(np.float32)


class _StreamOpusEncoder:
    """Streaming wrapper over `ds5bridge.audio.make_encoder()`.

    Same settings Phase 1 verified to emit exactly 200-byte packets: CBR
    160 kbps, 10 ms frames, `application=lowdelay` (pure CELT).
    """

    def __init__(self):
        self._cc = A.make_encoder(2)
        self._tb = fractions.Fraction(1, A.OPUS_NOMINAL_RATE)
        self._pts = 0
        self.short_packets = 0

    def reset(self) -> None:
        try:
            self._cc.close()
        except Exception:  # noqa: BLE001  - best effort
            pass
        self._cc = A.make_encoder(2)
        self._pts = 0

    def push(self, pcm45: np.ndarray) -> list[bytes]:
        """pcm45: float32 (480, 2) at 45 kHz. Returns 0..n 200-byte frames."""
        f = AudioFrame.from_ndarray(
            np.ascontiguousarray(pcm45.reshape(1, -1), dtype=np.float32),
            format="flt", layout="stereo",
        )
        f.sample_rate = A.OPUS_NOMINAL_RATE
        f.time_base = self._tb
        f.pts = self._pts
        self._pts += pcm45.shape[0]
        out = []
        for pkt in self._cc.encode(f):
            b = bytes(pkt)
            if len(b) != A.OPUS_FRAME_BYTES:
                self.short_packets += 1
            out.append(b[: A.OPUS_FRAME_BYTES].ljust(A.OPUS_FRAME_BYTES, b"\x00"))
        return out


def _uac_db_to_byte(millidb: int) -> int:
    """UAC1 volume (1/256 dB, signed) -> the DualSense's 0..255 linear knob.

    Heuristic, not captured from hardware: amplitude = 10^(dB/20) scaled to the
    Phase-1 working range. `uac.py`'s MIN/MAX are themselves assumed values
    (risk R7), so this cannot be better than they are.
    """
    db = millidb / 256.0
    amp = 10.0 ** (db / 20.0)
    return max(0, min(255, int(round(amp * 200.0))))


# ---------------------------------------------------------------------------
# the backend
# ---------------------------------------------------------------------------


class BridgeBackend(Backend):
    """Live backend: a Bluetooth DualSense behind the emulated wired one.

    Construction never touches hardware — `start()` opens the device, so the
    object can be built, inspected and unit-tested without a controller.
    """

    def __init__(
        self,
        transport: str = "BT",
        *,
        device_index: int = 0,
        serial: str | None = None,
        target: str = "speaker",
        speaker_volume: int = 0x60,
        haptic_volume: int = 0xFF,
        renumber_input_seq: bool = True,
        repeat_stale_input: bool = True,
        auto_arm: bool = True,
        mic_always_on: bool = False,
        report_id: int = 0x39,
        reconnect: bool = True,
        keep_raw: bool = False,
        distinct_wired_mac: bool = True,
    ):
        self.transport = transport
        self.device_index = device_index
        #: Pick the controller by BD address rather than by position. USE THIS
        #: whenever more than one controller has ever been paired: a unit that
        #: is charging over USB *still enumerates over Bluetooth* as a stale
        #: entry whose feature reads fail, and `enumerate_devices()` orders by
        #: path, so "first BT match" can hand you the wrong — or a dead —
        #: controller. Observed on 2026-08-25 with `0011223344aa` (charging,
        #: stale, feature read failed) sorting ahead of `0011223344bb` (live).
        self.serial = serial.lower() if serial else None
        self.target = target
        self.speaker_volume = speaker_volume
        self.haptic_volume = haptic_volume
        self.renumber_input_seq = renumber_input_seq
        #: Answer every poll, repeating the current state when no new Bluetooth
        #: report has arrived yet. A real wired DualSense emits a report every
        #: 4 ms whether or not anything moved, and Bluetooth delivery is bursty
        #: (measured: ~23 % of 250 Hz polls land inside a burst gap), so
        #: repeating is what reproduces the wired cadence. Set False to get
        #: strict "new data only" semantics instead.
        self.repeat_stale_input = repeat_stale_input
        self.auto_arm = auto_arm
        self.mic_always_on = mic_always_on
        self.report_id = report_id
        self.reconnect = reconnect
        #: Keep the source BT payload beside each translated report so a live
        #: soak run can re-decode both with the Phase-1 decoder and prove the
        #: translation on real traffic. Off by default: it doubles the copy.
        self.keep_raw = keep_raw
        #: Serve a MAC in features 0x09/0x0b that is derived from -- but not
        #: equal to -- the controller's real Bluetooth address. ON by default,
        #: and turning it off re-enables a proven kill: libScePad titles that
        #: can still reach the raw Bluetooth pad will power it off the moment
        #: they see a wired pad with the identical MAC. See WIRED_MAC_LA_BIT.
        self.distinct_wired_mac = distinct_wired_mac

        if report_id != 0x39:
            raise ValueError("only report 0x39 (two frames per report) is implemented")

        self._dev: DEV.DualSense | None = None
        self._dev_lock = threading.Lock()
        #: Set by `force_disconnect(hold_s=...)`; always 0 in production.
        self._reconnect_blocked_until = 0.0
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

        # --- HID input ---------------------------------------------------
        self._input_lock = threading.Lock()
        self._latest_input: bytes | None = None
        self._latest_bt_payload: bytes | None = None   # only when keep_raw
        #: perf_counter of the last control payload from the controller. Drives
        #: both the link watchdog and the neutralise-on-stale rule.
        self._latest_input_at = 0.0
        self._input_serial = 0        # bumped by the reader
        self._delivered_serial = 0    # last value handed to the USB side
        #: BT payload that produced the report `read_input_report` just
        #: returned (keep_raw only) — the live parity check reads this.
        self.last_bt_payload: bytes | None = None
        self._out_seq = 0             # renumbered device sequence byte

        # --- SetState passthrough ----------------------------------------
        self._setstate_lock = threading.Lock()
        self._setstate_pending: bytes | None = None
        self._setstate_last: bytes | None = None
        #: Everything the host has ever asked for, folded into one body: the
        #: newest value of every field whose valid-flag bit it has set. Replayed
        #: once after each (re)connect, because a controller that was away
        #: forgot its LEDs and trigger effects and the game will not resend
        #: them -- it set them once, at startup.
        self._setstate_host: bytes | None = None
        self._setstate_event = threading.Event()

        # --- deferred Bluetooth control work ------------------------------
        #: Jobs posted from the USB request path (mic arming, UAC mirroring)
        #: and executed on the writer thread. NOTHING that can block on hidapi
        #: may run on the asyncio server thread — it owes the isochronous
        #: endpoints a 1 ms deadline. See the module docstring.
        self._control_q: deque = deque(maxlen=64)

        # --- audio --------------------------------------------------------
        self._out_ring = _ByteRing(AUDIO_OUT_RING_BYTES)
        self._mic_ring = _ByteRing(MIC_RING_BYTES)
        self._mic_primed = False
        self._last_audio_out = 0.0
        #: Encoded-frame queue depth, published by the pump for the stats snapshot.
        self.audio_q_depth = 0

        self._audio_armed = threading.Event()
        self._mic_armed = threading.Event()

        # --- feature reports ----------------------------------------------
        self._features: dict[int, bytes] = {}
        self.feature_misses: dict[int, int] = {}
        self.feature_writes: list[tuple[int, bytes]] = []
        #: The (deviceId, actionId) of the last allowlisted 0x80 we forwarded.
        #: A GET of 0x81 with nothing armed is answered with a STALL rather than
        #: a Bluetooth round trip, so an idle host cannot make us poll the
        #: controller 250 times a second.
        self._test_armed: tuple[int, int] | None = None
        #: Per-instance so a test can shrink it; see FEATURE_BT_TIMEOUT.
        self.feature_timeout = FEATURE_BT_TIMEOUT

        # --- observability -------------------------------------------------
        self.stats = {
            "bt_reports": 0,
            "bt_control": 0,
            "bt_mic": 0,
            "bt_read_errors": 0,
            "input_delivered": 0,
            "input_repeated": 0,
            "input_none": 0,
            #: Input states served by the control-channel fallback while the
            #: interrupt stream was gated (see POLL_AFTER_S). Non-zero means a
            #: foreground GameInput client (a Chromium window, say) starved
            #: the stream and the bridge carried input through GET_REPORT.
            "input_polled": 0,
            "input_poll_errors": 0,
            #: Times the fallback engaged (episodes, not polls).
            "poll_fallbacks": 0,
            "setstate_in": 0,
            "setstate_sent": 0,
            "setstate_coalesced": 0,
            #: Output reports folded into an already-pending one instead of
            #: replacing it. Before Phase 4c these were silently LOST, which is
            #: what kept the player LEDs dark: a game sets them once and never
            #: repeats the valid flag.
            "setstate_merged": 0,
            #: Times the host's accumulated state was replayed after a connect.
            "setstate_replayed": 0,
            #: Factory-test channel (feature 0x80 -> 0x81).
            "feature_test_forwarded": 0,
            "feature_test_blocked": 0,
            "feature_test_errors": 0,
            "feature_test_reads": 0,
            "feature_bt_timeouts": 0,
            "audio_out_calls": 0,
            "audio_out_bytes": 0,
            "audio_in_bytes": 0,
            "reports_39": 0,
            "report_39_errors": 0,
            "opus_frames": 0,
            "audio_underrun_frames": 0,
            "audio_q_drop_frames": 0,
            "mic_decode_errors": 0,
            "mic_underrun_calls": 0,
            "mic_skew_pad_frames": 0,
            "mic_skew_drop_frames": 0,
            "mic_reprimes": 0,
            "control_jobs": 0,
            "control_jobs_dropped": 0,
            "reconnects": 0,
            #: Polls answered with a neutralised report because the link had
            #: been silent for INPUT_NEUTRAL_S. Non-zero means the controller
            #: went away; it is the counter to look at first after a dropout.
            "input_neutral": 0,
            #: Times the watchdog declared the link dead on silence alone
            #: (no read error at all) and forced a reopen.
            "link_watchdog_trips": 0,
            #: Disconnect events seen from any cause.
            "disconnects": 0,
            #: Calls into the attached interceptor that raised. Non-zero means
            #: the chord engine has a bug; the bridge carried on without it for
            #: that report.
            "interceptor_errors": 0,
            #: Deliberate pad power-offs sent (PS+Triangle, the idle timer).
            "pad_power_off": 0,
        }
        #: Optional flight recorder (`ds5emu.capture.BlackBox`). When set,
        #: every host->device request and every post-translation Bluetooth
        #: write is recorded into its bounded ring, and the ring is dumped to
        #: a file automatically when the Bluetooth link dies -- so one
        #: reproduction of a disconnect preserves the ~10 s that caused it.
        #: `python -m ds5emu serve --capture PATH` turns it on.
        self.blackbox = None
        #: Optional chord/shortcut engine (`ds5app.intercept.InputInterceptor`
        #: or anything with the same three methods). It sits on the decoded
        #: input stream BEFORE the emulated USB layer, so a chord the user
        #: presses is consumed here and the game never sees it:
        #:
        #:     on_input(usb01)  reader thread, once per BT control payload.
        #:                      Returns the (possibly masked) 64-byte report
        #:                      that becomes `_latest_input`.
        #:     rewrite_setstate(body)  writer thread, just before a host
        #:                      SetState goes on the air. Returns the body to
        #:                      transmit; the HOST's body stays the coalescing
        #:                      key, so an override that changes over time can
        #:                      never be mistaken for new host state.
        #:     tick()           writer thread, every wakeup (<= 50 ms apart).
        #:                      Drives the engine's timers (haptic pulse tails,
        #:                      idle off-timer, battery flashes) even when no
        #:                      input and no host traffic is flowing.
        #:
        #: Every call is wrapped: an engine bug costs one report, never the
        #: bridge. The emulator has no dependency on the engine's module --
        #: the app layer builds it and assigns it here (see
        #: `ds5app.intercept.attach_to_backend`).
        self.interceptor = None
        #: Sampled mic-ring depth in bytes, for the drift report. Cheap: three
        #: integers updated per `read_audio_in`.
        self._mic_depth_n = 0
        self._mic_depth_sum = 0
        self._mic_depth_min = 1 << 30
        self._mic_depth_max = 0
        self.connected = threading.Event()

    # =====================================================================
    # flight recorder
    # =====================================================================

    def _bb(self, kind: str, data: bytes | None = None, note: str = "") -> None:
        """Record one event into the black box, when one is attached.

        Kept to a None-check plus one method call so it is free to sprinkle
        on the hot paths when no capture is running.
        """
        bb = self.blackbox
        if bb is not None:
            bb.record(kind, data, note)

    def _bb_context(self) -> list:
        """Header lines for a dump: the stats and the last-known pad state."""
        lines = [f"stats: {self.stats}", f"feature_misses: {self.feature_misses}"]
        try:
            lines.append(f"device_status: {self.device_status()}")
        except Exception as e:  # noqa: BLE001
            lines.append(f"device_status failed: {e!r}")
        return lines

    # =====================================================================
    # lifecycle
    # =====================================================================

    def start(self) -> None:
        if self._threads:
            return
        if self.blackbox is not None:
            self.blackbox.context_fn = self._bb_context
            self._bb("lifecycle", note="start()")
        self._stop.clear()
        self._open_device()
        for name, fn in (
            ("ds5-bt-reader", self._reader_loop),
            ("ds5-audio-pump", self._pump_loop),
            ("ds5-setstate", self._writer_loop),
        ):
            t = threading.Thread(target=fn, name=name, daemon=True)
            t.start()
            self._threads.append(t)
        log.info("BridgeBackend started (%d threads)", len(self._threads))

    def stop(self) -> None:
        self._stop.set()
        self._setstate_event.set()
        # ONE shared deadline, not one per thread. `for t: t.join(timeout=3)`
        # is a 9 s worst case, and shutdown runs on the caller's thread -- which
        # for the packaged app is the one holding up the whole exit. All three
        # threads check `_stop` on the same tick, so they finish together and
        # the deadline is only ever reached when something is genuinely wedged.
        deadline = time.monotonic() + 3.0
        for t in self._threads:
            t.join(timeout=max(0.05, deadline - time.monotonic()))
        stuck = [t.name for t in self._threads if t.is_alive()]
        if stuck:
            log.warning("threads still running after stop(): %s", stuck)
        self._threads.clear()
        self._disarm_mic_best_effort()
        with self._dev_lock:
            if self._dev is not None:
                try:
                    self._dev.close()
                finally:
                    self._dev = None
        self.connected.clear()
        if self.blackbox is not None:
            self._bb("lifecycle", note="stop()")
            self.blackbox.dump("shutdown")
        log.info("BridgeBackend stopped: %s", self.stats)

    def __enter__(self) -> "BridgeBackend":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    # -- device open / reopen ------------------------------------------------

    def _pick_device(self):
        """Choose the controller, by BD address when one was given.

        Selecting by serial is not a nicety once two controllers have been
        paired. A unit charging over USB keeps a *stale Bluetooth entry* whose
        feature reads fail, and `enumerate_devices()` orders by path, so index 0
        is not stable across sessions and can be the dead one.
        """
        if not self.serial:
            return DEV.pick(self.transport, self.device_index)
        want = self.serial
        for d in DEV.enumerate_devices():
            if d.transport == self.transport and (d.serial or "").lower() == want:
                return d
        seen = [(d.transport, d.serial) for d in DEV.enumerate_devices()]
        raise RuntimeError(
            f"no {self.transport} DualSense with serial {self.serial!r}; saw {seen}")

    def _open_device(self) -> None:
        info = self._pick_device()
        dev = DEV.DualSense(info)
        dev.open(flip_extended=False)
        if dev.is_bt:
            # Reading feature 0x05 is what flips the controller out of the
            # minimal 0x01 report mode into extended 78-byte 0x31 reports.
            cal = dev.flip_to_extended()
            self._features[0x05] = self._feature_bytes(0x05, cal)
        # Plain GET-only feature reports the host may ask for. Prefetched, never
        # read lazily: a blocking feature read from the USB request path would
        # stall the isochronous endpoints. `feature_misses` is what reveals any
        # id that still belongs on this list.
        for rid in PREFETCH_FEATURES:
            try:
                self._features[rid] = self._feature_bytes(rid, dev.get_feature(rid, 64))
            except Exception as e:  # noqa: BLE001
                log.warning("feature 0x%02x read failed: %s", rid, e)
        with self._dev_lock:
            self._dev = dev
        self.connected.set()
        log.info("opened %s", DEV.describe(info))
        self._bb("link.open", note=DEV.describe(info))
        self._prime_setstate()

    def _feature_bytes(self, report_id: int, raw: bytes) -> bytes:
        """hidapi returns the feature body with the report id already at [0].

        Then it is made to look like it came off a cable rather than off a
        Bluetooth link: the trailing CRC-32 is zeroed on the reports where a
        wired unit publishes zeros, and 0x0b's link-key tail is dropped. See
        FEATURE_CRC_TRAILER and FEATURE_0B_WIRED_PREFIX.

        Finally, the controller MAC in 0x09/0x0b gets its locally-administered
        bit set, so this virtual *wired* pad never claims the exact identity of
        the Bluetooth pad behind it. Identical MACs are how libScePad decides
        "same pad, two transports" -- and its response is to power the
        Bluetooth one off. Full story and the capture proving it at
        WIRED_MAC_LA_BIT. Still stable and per-controller: derived from the
        real address by one deterministic bit.
        """
        b = bytes(raw)
        if not b or b[0] != report_id:
            b = bytes([report_id]) + b
        if report_id == 0x0B and len(b) > FEATURE_0B_WIRED_PREFIX:
            b = b[:FEATURE_0B_WIRED_PREFIX] + bytes(len(b) - FEATURE_0B_WIRED_PREFIX)
        elif report_id in FEATURE_CRC_TRAILER and len(b) >= 5:
            b = b[:-4] + b"\0\0\0\0"
        if (self.distinct_wired_mac and report_id in FEATURE_CLIENT_MAC
                and len(b) >= 7):
            # MAC is little-endian at [1..6]; the first textual octet -- the
            # one that carries the locally-administered bit -- is byte [6].
            mb = bytearray(b)
            mb[6] |= WIRED_MAC_LA_BIT
            b = bytes(mb)
        return b

    def _prime_setstate(self) -> None:
        """One SetState that routes audio and sets the volumes, then a replay of
        whatever the host has already commanded.

        Besides the audio fields the priming report carries the lightbar-setup
        control and the default blue (PRIME_LIGHTBAR_FADE_OUT): without the
        setup a Bluetooth pad ignores every lightbar colour for the whole
        connection. Nothing else the host later sends (rumble, triggers, LEDs)
        is touched — the controller applies a field only when its valid-flag
        bit is present.

        The replay afterwards exists because a controller that has just come
        back has forgotten its LEDs and trigger effects, and the game will not
        say them again: it set them once, at startup, and has been streaming
        rumble ever since. `_setstate_host` is the fold of everything it has
        asked for, so one report restores the lot. It goes out *after* the audio
        priming so the host's own audio fields, if it set any, win.
        """
        st = P.SetState()
        if self.target == "headphone":
            st.headphone_volume(self.speaker_volume)
        else:
            st.speaker_volume(self.speaker_volume)
        st.haptic_volume(self.haptic_volume)
        if PRIME_LIGHTBAR_FADE_OUT:
            st.lightbar_setup(P.LIGHTBAR_SETUP_LIGHT_OUT)
            st.lightbar(*P.DEFAULT_LIGHTBAR)
        self._write_raw(P.build_bt_setstate(bytes(st.body), self._next_bt_seq()))

        with self._setstate_lock:
            host = self._setstate_host
        if host is not None:
            self.stats["setstate_replayed"] += 1
            log.info("replaying the host's SetState state after connect (flags %s)",
                     T.setstate_flags(host))
            self._write_raw(T.usb02_to_bt31(host, self._next_bt_seq()))

    def _next_bt_seq(self) -> int:
        dev = self._dev
        if dev is None:
            return 0
        return dev._next_seq()  # noqa: SLF001 - the seq counter lives on the device

    def _write_raw(self, data: bytes) -> bool:
        dev = self._dev
        if dev is None:
            self._bb("bt.write.skip", data[:8], note="no device")
            return False
        if self.blackbox is not None:
            # seq nibble sits at payload[0] for every BT output report; the
            # CRC check proves at dump time that the killer was not a
            # malformed frame the controller would have discarded anyway.
            note = f"id=0x{data[0]:02x} seq={data[1] >> 4}" if len(data) > 1 else ""
            if len(data) >= 6:
                note += f" crc={'ok' if T.verify_bt_output_crc(data) else 'BAD'}"
            self._bb("bt.write", data, note=note)
        try:
            dev.write_raw(data)  # return value is meaningless on Windows
            return True
        except Exception as e:  # noqa: BLE001
            log.debug("hid write failed: %s", e)
            self._bb("bt.write.error", note=repr(e))
            self._on_io_error()
            return False

    def _on_io_error(self) -> None:
        if self.connected.is_set():
            self.stats["disconnects"] += 1
            log.warning("Bluetooth link lost; the virtual device stays attached "
                        "and will report a neutral controller until it returns")
            self._bb("link.death")
            if self.blackbox is not None:
                path = self.blackbox.dump("link-death")
                if path:
                    log.warning("black-box dump written: %s", path)
        self.connected.clear()
        # Whatever factory-test command was in flight died with the link; a new
        # 0x80 has to arm the 0x81 read again.
        self._test_armed = None

    def force_disconnect(self, hold_s: float = 0.0) -> None:
        """Simulate a link loss. TEST HOOK — closes the HID handle underneath
        the reader thread, which is the closest scriptable analogue of the
        controller being switched off (a long PS-button press is not
        scriptable). The next `hid.read()` raises, the reconnect path runs, and
        everything downstream sees exactly what a real dropout looks like.

        `hold_s` keeps the reconnect from succeeding for that long. Without it
        the handle reopens in well under a second — the device never actually
        left Windows' enumeration — which is a *better* outcome than a real
        power-off but exercises none of the down-state behaviour. A controller
        that is switched off cannot be reopened at all until it comes back, and
        `hold_s` is how that half gets tested.
        """
        log.warning("force_disconnect(hold_s=%.1f): closing the HID handle", hold_s)
        self._reconnect_blocked_until = time.monotonic() + hold_s
        with self._dev_lock:
            dev = self._dev
        if dev is not None:
            try:
                dev.close()
            except Exception:  # noqa: BLE001
                pass
        self._on_io_error()

    def _reconnect_loop(self) -> bool:
        """Try to reopen the controller. Returns True once connected."""
        with self._dev_lock:
            if self._dev is not None:
                try:
                    self._dev.close()
                except Exception:  # noqa: BLE001
                    pass
                self._dev = None
        while not self._stop.is_set():
            if time.monotonic() < self._reconnect_blocked_until:
                # Test hook only (force_disconnect(hold_s=...)); zero in
                # production, so this branch never runs for a real user.
                if self._stop.wait(0.25):
                    return False
                continue
            try:
                self._open_device()
                self.stats["reconnects"] += 1
                if self._mic_armed.is_set():
                    self._arm_mic_now(True)
                return True
            except Exception as e:  # noqa: BLE001
                log.info("reconnect failed (%s); retrying", e)
                if self._stop.wait(2.0):
                    return False
        return False

    # =====================================================================
    # thread 1: Bluetooth reader
    # =====================================================================

    def _reader_loop(self) -> None:
        dec = A.MicDecoder()
        consecutive_errors = 0
        #: perf_counter of the last non-empty STREAMED read. Deliberately not
        #: `_latest_input_at`, which successful polls refresh -- entering and
        #: leaving the fallback must key off the stream alone, or the first
        #: poll would make the stream look alive and flap the fallback off.
        last_stream_at = 0.0
        polling = False
        poll_failures = 0
        poll_next_at = 0.0
        while not self._stop.is_set():
            dev = self._dev
            if dev is None or not self.connected.is_set():
                if not self.reconnect or not self._reconnect_loop():
                    return
                consecutive_errors = 0
                last_stream_at = 0.0
                polling = False
                poll_failures = 0
                continue
            try:
                raw = dev.read_raw(POLL_READ_MS if polling else 200)
            except Exception as e:  # noqa: BLE001
                self.stats["bt_read_errors"] += 1
                consecutive_errors += 1
                log.debug("hid read failed: %s", e)
                self._bb("bt.read.error",
                         note=f"#{consecutive_errors} {e!r}")
                if consecutive_errors > 5:
                    self._on_io_error()
                continue
            now = time.perf_counter()
            if not raw:
                # A quiet link, not a broken one: every read timed out cleanly.
                # Nothing above will ever trip, so the watchdog has to.
                if (self._latest_input_at
                        and now - self._latest_input_at > LINK_DEAD_S):
                    self.stats["link_watchdog_trips"] += 1
                    log.warning("no Bluetooth report for %.1fs -- treating the link "
                                "as dead", LINK_DEAD_S)
                    self._bb("link.watchdog", note=f"silent for {LINK_DEAD_S}s")
                    self._on_io_error()
                    continue
                # The control-channel fallback (the Phase 5 block up top): the
                # stream is gated but the pad may still answer GET_REPORT.
                # Armed only once a streamed report has ever been seen, so a
                # connect that is still settling is never polled.
                if (last_stream_at and now - last_stream_at >= POLL_AFTER_S
                        and poll_failures < POLL_MAX_FAILURES):
                    if not polling:
                        polling = True
                        poll_next_at = now
                        self.stats["poll_fallbacks"] += 1
                        log.info(
                            "input stream silent for %.0f ms with the link up "
                            "-- polling GET_REPORT(0x31) over the control "
                            "channel (a foreground GameInput client is "
                            "probably gating the stream)", POLL_AFTER_S * 1000)
                        self._bb("bt.poll.start")
                    if now >= poll_next_at:
                        poll_next_at = now + POLL_INTERVAL_S
                        if self._poll_input_report(dev):
                            poll_failures = 0
                        else:
                            poll_failures += 1
                            if poll_failures >= POLL_MAX_FAILURES:
                                log.warning(
                                    "control-channel polling failed %d times; "
                                    "standing down until the stream returns "
                                    "(a dead pad belongs to the watchdog)",
                                    poll_failures)
                                self._bb("bt.poll.giveup")
                continue
            consecutive_errors = 0
            last_stream_at = now
            poll_failures = 0
            if polling:
                polling = False
                log.info("input stream is back -- control-channel polling off "
                         "(%d polled reports served so far)",
                         self.stats["input_polled"])
                self._bb("bt.poll.stop")
            self.stats["bt_reports"] += 1
            if raw[0] != P.BT_INPUT_31:
                continue
            payload = raw[1:]
            ptype = payload[0] & P.PAYLOAD_TYPE_MASK

            if ptype == P.PAYLOAD_TYPE_CONTROL:
                self.stats["bt_control"] += 1
                if self.blackbox is not None and self.stats["bt_control"] % 100 == 1:
                    # A ~5/s heartbeat with the raw payload: it timestamps the
                    # last input before a link death to ~0.2 s and preserves
                    # the status/battery bytes of the pad's dying seconds.
                    self._bb("bt.input.hb", payload,
                             note=f"#{self.stats['bt_control']}")
                usb = T.bt31_payload_to_usb01(payload)
                if usb is not None:
                    self._ingest_control_payload(usb, payload)
            elif ptype == P.PAYLOAD_TYPE_AUDIO:
                self.stats["bt_mic"] += 1
                self._on_mic_payload(dec, payload)

    def _ingest_control_payload(self, usb: bytes, payload: bytes) -> None:
        """One decoded input state into `_latest_input` -- both the streamed
        path and the control-channel fallback end here, so a polled report is
        intercepted, masked and serialised exactly like a streamed one."""
        usb = self._intercept_input(usb)
        with self._input_lock:
            self._latest_input = usb
            self._latest_input_at = time.perf_counter()
            if self.keep_raw:
                self._latest_bt_payload = payload
            self._input_serial += 1

    def _poll_input_report(self, dev) -> bool:
        """One GET_REPORT(Input, 0x31) over the control channel. Reader thread.

        Returns True when a control-type 0x31 answer was ingested. MEASURED on
        this hardware 2026-09-01, mid-gate: 78 bytes, live state, 5-20 ms.
        The answer is byte-compatible with a streamed report (same payload
        shape, seq nibble advancing), so it takes the normal translate path.
        """
        getter = getattr(dev.h, "get_input_report", None)
        if getter is None:
            # hidapi predates get_input_report: the fallback simply does not
            # exist on this install. Counted as a failure so the stand-down
            # message fires once instead of a silent busy loop.
            self.stats["input_poll_errors"] += 1
            return False
        try:
            raw = bytes(getter(P.BT_INPUT_31, P.BT_INPUT_31_LEN))
        except Exception as e:  # noqa: BLE001
            self.stats["input_poll_errors"] += 1
            log.debug("GET_REPORT(0x31) poll failed: %s", e)
            self._bb("bt.poll.error", note=repr(e))
            return False
        if len(raw) < 2 or raw[0] != P.BT_INPUT_31:
            self.stats["input_poll_errors"] += 1
            self._bb("bt.poll.error",
                     note=f"unexpected answer {raw[:2].hex() if raw else ''}")
            return False
        payload = raw[1:]
        usb = T.bt31_payload_to_usb01(payload)
        if usb is None:
            self.stats["input_poll_errors"] += 1
            return False
        self.stats["input_polled"] += 1
        if self.blackbox is not None and self.stats["input_polled"] % 100 == 1:
            self._bb("bt.poll.hb", payload, note=f"#{self.stats['input_polled']}")
        self._ingest_control_payload(usb, payload)
        return True

    # =====================================================================
    # the chord-engine seam (see the `interceptor` attribute)
    # =====================================================================

    def _intercept_input(self, usb: bytes) -> bytes:
        """Give the chord engine the decoded report; contain any failure.

        Runs on the reader thread at the Bluetooth rate (~476 Hz), which is the
        whole point of hooking HERE rather than in `read_input_report`: the
        engine sees every report the pad sent -- no 250 Hz decimation can eat
        the edge of a button press -- and what it returns is what the game can
        ever see, on both the interrupt and the control-transfer path.
        """
        icept = self.interceptor
        if icept is None:
            return usb
        try:
            out = icept.on_input(usb)
            return usb if out is None else out
        except Exception:  # noqa: BLE001
            self.stats["interceptor_errors"] += 1
            log.exception("interceptor.on_input failed; forwarding unmasked")
            return usb

    def _intercept_setstate(self, body: bytes) -> bytes:
        """Let the engine rewrite an outgoing host SetState body (writer thread).

        The caller keeps coalescing on the HOST body, not on what this returns:
        an override that varies over time (a battery flash, a dimming ramp)
        must not make an unchanged host body look new, nor a changed one look
        coalesced.
        """
        icept = self.interceptor
        if icept is None:
            return body
        try:
            out = icept.rewrite_setstate(body)
            return body if out is None else out
        except Exception:  # noqa: BLE001
            self.stats["interceptor_errors"] += 1
            log.exception("interceptor.rewrite_setstate failed; forwarding as-is")
            return body

    def _intercept_tick(self) -> None:
        icept = self.interceptor
        if icept is None:
            return
        try:
            icept.tick()
        except Exception:  # noqa: BLE001
            self.stats["interceptor_errors"] += 1
            log.exception("interceptor.tick failed")

    def push_setstate_body(self, body: bytes) -> None:
        """Send one engine-built SetState body to the pad. Any thread.

        The engine's own traffic (haptic ack pulses, lightbar flashes, the
        remote-mode colour) deliberately does NOT go through the host pending/
        coalesce path: it must not become the coalescing key the next host
        report is compared against, and it must not be merged into
        `_setstate_host` -- it is OURS, not state the host asked for, and a
        reconnect must not replay it. Valid-flag discipline makes this safe:
        an engine body only carries the fields whose flag bits it set.
        """
        self._defer(self._write_setstate_body, bytes(body))

    def power_off_pad(self) -> None:
        """Power the physical pad off, the way a PS5 does: feature 0x08.

        The mechanism is this project's own capture, not folklore
        (docs/wired-gap-findings.md, symptom 3): when libScePad decides a
        Bluetooth DualSense is redundant it writes SET feature 0x08 with
        action byte 0x02 -- DS_FEATURE_REPORT_BLUETOOTH_CONTROL in the public
        research (nondebug/dualsense) -- down the pad's own HID handle, and a
        DualSense told to drop its Bluetooth link with no console to fall back
        to powers off. Observed here 2026-08-31: link dead 26 ms after the
        write, pad off until the PS button, 2/2 runs. This method sends the
        same command on purpose, for the user's own PS+Triangle chord and the
        idle off-timer.

        SAFETY: this is the ONE deliberate non-0x80 feature write this project
        makes, and it is neither of the two the safety rail forbids
        (docs/ARCHITECTURE.md: never write pairing 0x09 or firmware reports).
        It changes nothing persistent -- the pad comes back with one PS press.
        Host-issued 0x08 writes are still recorded and dropped in
        `set_feature_report`; only the engine can reach this path.

        The Bluetooth write runs on the writer thread (`_defer`), so this is
        safe to call from the reader thread mid-`on_input`. The link death it
        causes then takes the normal disconnect path: input neutralises, the
        virtual device stays attached, and the reconnect loop waits for the
        pad to be switched back on.
        """
        self._defer(self._bt_power_off_now)

    def _bt_power_off_now(self) -> None:
        """Writer thread: the signed 0x08 [action=0x02] SET feature report.

        Framed at the length the pad's own descriptor gives feature 0x08
        (FEATURE_BLUETOOTH_CONTROL_LEN, 47 -- the 63 of the 0x80 test channel
        is refused by the HID stack and never reaches the pad), with the
        0x53-seeded CRC32 in the last 4 bytes: TLOU's own kill was captured as
        a 48-byte `08 02 00...` over USB, and over Bluetooth an unsigned
        feature write is refused by the pad, so it is signed here. VERIFIED on
        hardware 2026-09-03: pad off within a second of this exact write.
        """
        dev = self._dev
        if dev is None:
            return
        payload = bytearray(FEATURE_BLUETOOTH_CONTROL_LEN)
        payload[0] = BT_CONTROL_DISCONNECT
        if dev.is_bt:
            fill_feature_checksum(FEATURE_BLUETOOTH_CONTROL, payload)
        self._bb("bt.power_off",
                 bytes([FEATURE_BLUETOOTH_CONTROL]) + bytes(payload[:8]))
        log.info("powering the pad off (feature 0x08, action 0x02)")
        try:
            rc = dev.h.send_feature_report(
                bytes([FEATURE_BLUETOOTH_CONTROL]) + bytes(payload))
        except Exception as e:  # noqa: BLE001
            log.warning("pad power-off write failed: %s", e)
            return
        if rc is not None and rc < 0:
            # hidapi returns -1 rather than raising when the stack refuses the
            # report (same behaviour measured for an unsigned 0x80).
            log.warning("pad power-off rejected by the HID stack (rc=%d)", rc)
            return
        self.stats["pad_power_off"] += 1

    def _on_mic_payload(self, dec, payload: bytes) -> None:
        try:
            mono = dec.decode(P.get_mic_opus(payload))
        except Exception:  # noqa: BLE001
            self.stats["mic_decode_errors"] += 1
            return
        if mono.size == 0:
            return
        # mono -> the 2-channel USB IN stream: the real wired controller
        # duplicates the single capsule to L and R (FINDINGS: "USB mic IN: 2ch
        # descriptor, mono duplicated to L/R").
        i16 = np.clip(mono, -1.0, 1.0)
        i16 = (i16 * 32767.0).astype("<i2")
        stereo = np.repeat(i16, 2)
        self._mic_ring.write(stereo.tobytes())

    # =====================================================================
    # thread 2: audio pump  (USB iso OUT -> BT 0x39)
    # =====================================================================

    def _pump_loop(self) -> None:
        rs_spk = _StreamResampler(A.AUDIO_SOURCE_RATE)   # 48000 -> 45000
        rs_hap = _StreamResampler(A.HAPTIC_RATE)         # 48000 ->  3000
        enc = _StreamOpusEncoder()

        spk_acc = np.zeros((0, 2), np.float32)
        hap_acc = np.zeros((0, 2), np.float32)
        opus_q: list[bytes] = []
        hap_q: list[bytes] = []
        tail = b""

        pacer = Pacer(frame_ms=REPORT_39_MS, max_backlog=8, max_burst=4)
        running = False
        packet_counter = 0
        silent_op = A.silent_opus_frame()
        silent_hp = A.silent_haptic_frame()

        with TimerResolution(1):
            while not self._stop.is_set():
                streaming = (
                    self._audio_armed.is_set()
                    and (time.perf_counter() - self._last_audio_out) < AUDIO_IDLE_TIMEOUT
                )
                if not streaming:
                    if running:
                        # flush to silence so the controller does not repeat the
                        # tail of the last frame it received
                        for _ in range(2):
                            self._send_39((silent_op, silent_op),
                                          (silent_hp, silent_hp), packet_counter)
                            packet_counter = (packet_counter + 2) & 0xFF
                        running = False
                    rs_spk.reset()
                    rs_hap.reset()
                    enc.reset()
                    spk_acc = np.zeros((0, 2), np.float32)
                    hap_acc = np.zeros((0, 2), np.float32)
                    opus_q.clear()
                    hap_q.clear()
                    tail = b""
                    self._out_ring.clear()
                    self._stop.wait(0.005)
                    continue

                # ---- pull whatever the host handed us and convert it -------
                chunk = tail + self._out_ring.take_all()
                n_whole = len(chunk) // USB_OUT_FRAME_BYTES
                tail = chunk[n_whole * USB_OUT_FRAME_BYTES:]
                if n_whole:
                    block = np.frombuffer(
                        chunk[: n_whole * USB_OUT_FRAME_BYTES], dtype="<i2"
                    ).reshape(-1, USB_OUT_CH).astype(np.float32) / 32768.0
                    spk_acc = np.concatenate([spk_acc, rs_spk.push(block[:, 0:2])])
                    hap_acc = np.concatenate([hap_acc, rs_hap.push(block[:, 2:4])])

                while spk_acc.shape[0] >= A.OPUS_SAMPLES_PER_FRAME:
                    frame = spk_acc[: A.OPUS_SAMPLES_PER_FRAME]
                    spk_acc = spk_acc[A.OPUS_SAMPLES_PER_FRAME :]
                    opus_q.extend(enc.push(frame))
                while hap_acc.shape[0] >= A.HAPTIC_SAMPLES_PER_FRAME:
                    frame = hap_acc[: A.HAPTIC_SAMPLES_PER_FRAME]
                    hap_acc = hap_acc[A.HAPTIC_SAMPLES_PER_FRAME :]
                    hap_q.extend(A.pcm_to_haptic_frames(frame))

                # ---- start only once the queue is AT TARGET DEPTH ----------
                # Not "once there is something": starting early means the first
                # 0x39 reports are followed by invented silence, and because U
                # and B never drift apart the queue then sits at that unlucky
                # depth forever. Starting at target makes the steady-state
                # latency a chosen 42.7 ms instead of a startup accident.
                if not running:
                    if (len(opus_q) < AUDIO_Q_TARGET_FRAMES
                            and len(hap_q) < AUDIO_Q_TARGET_FRAMES):
                        self._stop.wait(0.002)
                        continue
                    pacer.reset()
                    packet_counter = 0
                    running = True

                due = pacer.frames_due()
                if due == 0:
                    pacer.sleep_until_next()
                    continue
                for _ in range(due):
                    ops, haps = [], []
                    for _i in range(FRAMES_PER_REPORT_39):
                        if opus_q:
                            ops.append(opus_q.pop(0))
                        else:
                            ops.append(silent_op)
                            self.stats["audio_underrun_frames"] += 1
                        haps.append(hap_q.pop(0) if hap_q else silent_hp)
                    self._send_39(tuple(ops), tuple(haps), packet_counter)
                    packet_counter = (packet_counter + FRAMES_PER_REPORT_39) & 0xFF
                    pacer.commit(1)

                # ---- depth governor on the U -> B seam ---------------------
                # U and B share an oscillator, so depth only moves when phase
                # does: a stall on either side, or the host restarting its
                # stream. Above HIGH, cut back to TARGET rather than to HIGH,
                # so one correction settles it instead of hovering at the
                # ceiling. Counted, because dropping encoded audio is audible
                # and must never be silent in the logs.
                if len(opus_q) > AUDIO_Q_HIGH_FRAMES:
                    n = len(opus_q) - AUDIO_Q_TARGET_FRAMES
                    del opus_q[:n]
                    self.stats["audio_q_drop_frames"] += n
                if len(hap_q) > AUDIO_Q_HIGH_FRAMES:
                    del hap_q[: len(hap_q) - AUDIO_Q_TARGET_FRAMES]
                self.audio_q_depth = len(opus_q)

    def _send_39(self, opus2, hap2, packet_counter: int) -> None:
        dev = self._dev
        if dev is None:
            return
        mic = self.mic_always_on or self._mic_armed.is_set()
        if self.blackbox is not None:
            self._bb("bt.39",
                     note=f"seq={dev.seq} pc={packet_counter} mic={int(mic)} "
                          f"target={self.target} "
                          f"op={len(opus2[0])}/{len(opus2[1])}B "
                          f"hap={len(hap2[0])}/{len(hap2[1])}B")
        try:
            dev.send_report_39(
                opus2, hap2, packet_counter,
                target=self.target,
                mic_enabled=mic,
                audio_buffer_length=AUDIO_BUFFER_LENGTH,
            )
            self.stats["reports_39"] += 1
            self.stats["opus_frames"] += FRAMES_PER_REPORT_39
        except Exception as e:  # noqa: BLE001
            self.stats["report_39_errors"] += 1
            log.debug("0x39 write failed: %s", e)
            self._bb("bt.39.error", note=repr(e))
            if self.stats["report_39_errors"] > 20:
                self._on_io_error()

    # =====================================================================
    # thread 3: SetState passthrough
    # =====================================================================

    def _post_control(self, fn, *args) -> None:
        """Queue Bluetooth control work for the writer thread.

        Called from the asyncio server thread (SET_INTERFACE, UAC SET_CUR),
        which must never block on hidapi. The deque is bounded, so a host that
        spams controls cannot grow memory; overflow is counted, not fatal.
        """
        if len(self._control_q) == self._control_q.maxlen:
            self.stats["control_jobs_dropped"] += 1
        self._control_q.append((fn, args))
        self._setstate_event.set()

    def _drain_control_q(self) -> None:
        while True:
            try:
                fn, args = self._control_q.popleft()
            except IndexError:
                return
            self.stats["control_jobs"] += 1
            try:
                fn(*args)
            except Exception as e:  # noqa: BLE001
                log.warning("control job %s failed: %s", getattr(fn, "__name__", fn), e)

    def _writer_loop(self) -> None:
        last_sent_at = 0.0
        while not self._stop.is_set():
            self._setstate_event.wait(0.05)
            self._setstate_event.clear()
            if self._stop.is_set():
                return
            self._drain_control_q()
            # The engine's clock, whether or not any traffic is flowing: this
            # loop wakes at least every 50 ms, which is plenty for pulse tails,
            # battery flashes and the idle off-timer.
            self._intercept_tick()
            if not self.connected.is_set():
                # Do NOT consume the pending body while the link is down: it
                # would be dropped on the floor, and the pending body is exactly
                # the once-per-session state (player LEDs, trigger effects) the
                # game will never send again. Leave it to keep merging; the
                # reconnect replays `_setstate_host` anyway.
                continue
            with self._setstate_lock:
                body = self._setstate_pending
                self._setstate_pending = None
            now = time.perf_counter()
            if body is None:
                if SETSTATE_REFRESH and self._setstate_last is not None \
                        and now - last_sent_at >= SETSTATE_REFRESH:
                    body = self._setstate_last
                else:
                    continue
            elif body == self._setstate_last and \
                    (not SETSTATE_REFRESH or now - last_sent_at < SETSTATE_REFRESH):
                # Windows re-sends an unchanged SetState at the full HID rate.
                # Forwarding it would burn Bluetooth airtime the audio stream
                # needs, and the controller would apply exactly the same bytes.
                self.stats["setstate_coalesced"] += 1
                continue
            delta = SETSTATE_MIN_INTERVAL - (now - last_sent_at)
            if delta > 0:
                if self._stop.wait(delta):
                    return
            # The engine may rewrite what actually goes on the air (lightbar
            # dim/flash, remote-mode colour). `body` -- the HOST's bytes --
            # stays the coalescing key and what `_setstate_last` remembers,
            # so a time-varying override never masquerades as host state.
            wire_body = self._intercept_setstate(body)
            if self._write_raw(T.usb02_to_bt31(wire_body, self._next_bt_seq())):
                self.stats["setstate_sent"] += 1
                self._setstate_last = body
                last_sent_at = time.perf_counter()
            else:
                # The link died between the check above and the write. Put the
                # body back rather than losing it, oldest-first so anything the
                # host queued meanwhile still wins field by field.
                with self._setstate_lock:
                    self._setstate_pending = (
                        body if self._setstate_pending is None
                        else T.merge_setstate(body, self._setstate_pending)
                    )

    # =====================================================================
    # Backend contract — HID
    # =====================================================================

    def read_input_report(self, max_len: int) -> bytes | None:
        """The freshest translated input state, one report per poll.

        Deterministic decimation: Bluetooth runs at ~476 Hz and the USB host
        polls at 250 Hz, so the newest state at poll time wins and everything
        between two polls is discarded. The drop pattern is a pure function of
        the two clocks — no queue, no buffering luck, and no stale state can
        ever overtake a newer one.

        A poll that lands inside a Bluetooth burst gap repeats the current
        state rather than returning None, because a real wired DualSense emits
        a report every 4 ms whether or not anything changed. Only "nothing has
        ever arrived" yields None.
        """
        with self._input_lock:
            if self._latest_input is None:
                self.stats["input_none"] += 1
                return None
            report = self._latest_input
            if self._input_serial == self._delivered_serial:
                if not self.repeat_stale_input:
                    self.stats["input_none"] += 1
                    return None
                self.stats["input_repeated"] += 1
                # Link gone: release everything rather than repeating whatever
                # was held when it died. See LINK_DEAD_S / INPUT_NEUTRAL_S.
                if time.perf_counter() - self._latest_input_at > INPUT_NEUTRAL_S:
                    self.stats["input_neutral"] += 1
                    report = T.neutralize_usb01(report)
            self._delivered_serial = self._input_serial
            self.last_bt_payload = self._latest_bt_payload
            if self.renumber_input_seq:
                b = bytearray(report)
                b[1 + T.USB_SEQ_OFFSET] = self._out_seq
                self._out_seq = (self._out_seq + 1) & 0xFF
                report = bytes(b)
        self.stats["input_delivered"] += 1
        return report[:max_len] if max_len < len(report) else report

    def latest_input_report(self, max_len: int) -> bytes | None:
        """The current state without consuming it — for control GET_REPORT.

        A real device answers a HID GET_REPORT(INPUT) with its current state
        every time; only the interrupt endpoint has "nothing new" semantics.
        """
        with self._input_lock:
            report = self._latest_input
        if report is None:
            return None
        return report[:max_len] if max_len < len(report) else report

    def write_output_report(self, data: bytes) -> None:
        """USB output report 0x02 -> BT 0x31. Never blocks on Bluetooth.

        Reports arrive faster than they are allowed onto the air
        (`SETSTATE_MIN_INTERVAL`), so an unsent one is still pending when the
        next arrives. It is **merged**, not replaced. Replacing it lost data for
        real: a game sets the player-LED bits in one early report and then
        streams rumble/trigger reports that never carry the player-indicator
        valid flag again, so the LED report only had to lose one race to be gone
        forever. The SetState body is valid-flag driven, which makes the merge
        exact rather than a guess -- see `translate.merge_setstate`.
        """
        self.stats["setstate_in"] += 1
        body = T.usb02_body(bytes(data))
        if self.blackbox is not None:
            f0, f1, f2 = T.setstate_flags(body)
            self._bb("usb.out02", bytes(data),
                     note=f"flags={f0:02x}/{f1:02x}/{f2:02x}")
        with self._setstate_lock:
            if self._setstate_pending is None:
                self._setstate_pending = body
            else:
                self._setstate_pending = T.merge_setstate(self._setstate_pending, body)
                self.stats["setstate_merged"] += 1
            self._setstate_host = (
                body if self._setstate_host is None
                else T.merge_setstate(self._setstate_host, body)
            )
        self._setstate_event.set()

    def get_feature_report(self, report_id: int, length: int) -> bytes | None:
        if report_id == FEATURE_TEST_RESULT:
            body = self._read_test_result()
        else:
            body = self._features.get(report_id)
        if body is None:
            self.feature_misses[report_id] = self.feature_misses.get(report_id, 0) + 1
            self._bb("usb.get_feature",
                     note=f"id=0x{report_id:02x} len={length} -> MISS (STALL)")
            return None
        self._bb("usb.get_feature",
                 note=f"id=0x{report_id:02x} len={length} -> {len(body)}B")
        return body[:length] if length < len(body) else body

    def set_feature_report(self, report_id: int, data: bytes) -> None:
        """Recorded, never forwarded -- except allowlisted factory-test reads.

        Feature *writes* are how a DualSense is re-paired (0x09) and how its
        firmware is touched. A stray host-issued write must not reach the
        physical controller, so by default they are logged and dropped.

        Report 0x80 is the one exception, and only for the subcommands in
        `TEST_COMMAND_ALLOWLIST` -- read-only but for the AUDIO wave-out pair,
        which starts and stops a test tone and nothing else. It is the query
        half of the
        factory-diagnostics channel that Factory Info and Diagnostics are built
        on, and there is no way to read those without writing the query. Every
        other (deviceId, actionId) -- and every other report id, 0x09 and the
        0x84/0x85 individual-data channel included -- keeps the old behaviour.
        """
        data = bytes(data)
        self._bb("usb.set_feature", data, note=f"id=0x{report_id:02x}")
        if report_id == FEATURE_TEST_CMD:
            self._on_test_command(data)
            return
        self.feature_writes.append((report_id, data))
        log.info("set_feature_report 0x%02x (%d bytes) recorded, NOT forwarded",
                 report_id, len(data))

    # -- the factory-test channel (feature 0x80 -> 0x81) ---------------------

    @staticmethod
    def _test_payload(data: bytes) -> bytes:
        """The 63-byte 0x80 payload, whichever framing the host used.

        A control SET_REPORT puts the report id in wValue and may or may not
        repeat it in the data stage; the length is what disambiguates, exactly
        as in `translate.usb02_body`.
        """
        if len(data) > FEATURE_TEST_PAYLOAD_LEN and data[0] == FEATURE_TEST_CMD:
            data = data[1:]
        buf = bytearray(FEATURE_TEST_PAYLOAD_LEN)
        n = min(len(data), FEATURE_TEST_PAYLOAD_LEN)
        buf[:n] = data[:n]
        return bytes(buf)

    def _on_test_command(self, data: bytes) -> None:
        payload = self._test_payload(data)
        key = (payload[0], payload[1])
        self.feature_writes.append((FEATURE_TEST_CMD, payload))
        why = TEST_COMMAND_ALLOWLIST.get(key)
        self._bb("usb.test_cmd",
                 note=f"dev=0x{key[0]:02x} act=0x{key[1]:02x} "
                      f"{'ALLOWED' if why else 'BLOCKED'}")
        if why is None:
            self.stats["feature_test_blocked"] += 1
            log.warning(
                "BLOCKED factory-test command device=0x%02x action=0x%02x: not on "
                "the read-only allowlist. Recorded, NOT forwarded to the controller.",
                key[0], key[1])
            return
        if not self.connected.is_set():
            log.info("factory-test command 0x%02x/0x%02x dropped: link is down",
                     key[0], key[1])
            return
        self.stats["feature_test_forwarded"] += 1
        self._test_armed = key
        log.info("forwarding factory-test command device=0x%02x action=0x%02x (%s)",
                 key[0], key[1], why)
        self._defer(self._bt_send_test_command, payload)

    def _bt_send_test_command(self, payload: bytes) -> None:
        """Writer thread: the 0x80 query, CRC-signed for Bluetooth.

        VERIFIED on hardware 2026-08-25 (`tools/factory_probe.py --framing`):
        the same 63-byte body, signed, is accepted and answered; unsigned, the
        identical report is rejected outright. The CRC is mandatory on
        Bluetooth, and it does not make the report longer.
        """
        dev = self._dev
        if dev is None:
            return
        buf = bytearray(payload)
        if dev.is_bt:
            # 0x53-seeded CRC32 in the last 4 of the SAME 63 payload bytes; a BT
            # feature report is not longer than its USB twin (FINDINGS).
            fill_feature_checksum(FEATURE_TEST_CMD, buf)
        self._bb("bt.feature_write", bytes([FEATURE_TEST_CMD]) + bytes(buf))
        try:
            rc = dev.h.send_feature_report(bytes([FEATURE_TEST_CMD]) + bytes(buf))
        except Exception as e:  # noqa: BLE001
            self.stats["feature_test_errors"] += 1
            log.warning("factory-test command write failed: %s", e)
            return
        if rc is not None and rc < 0:
            # hidapi does NOT raise when Windows refuses the report — it returns
            # -1. Measured: that is exactly what an unsigned 0x80 over Bluetooth
            # does. Without this check a malformed query looks sent, the host
            # polls 0x81 for a second and gets stale bytes, and nothing counts.
            self.stats["feature_test_errors"] += 1
            log.warning("factory-test command rejected by the HID stack (rc=%d); "
                        "the 0x80 body was not accepted", rc)

    def _bt_read_test_result(self) -> bytes | None:
        """Writer thread: one GET of feature 0x81.

        Deliberately NOT cached. One 0x80 can be answered by several 56-byte
        pages, each read with its own GET of 0x81, so replaying a stored answer
        would truncate every multi-page result -- including the whole Diagnostics
        block, which is four pages.
        """
        dev = self._dev
        if dev is None:
            return None
        raw = dev.get_feature(FEATURE_TEST_RESULT, FEATURE_TEST_PAYLOAD_LEN + 1)
        if not raw:
            return None
        return self._feature_bytes(FEATURE_TEST_RESULT, raw)

    def _read_test_result(self) -> bytes | None:
        if self._test_armed is None or not self.connected.is_set():
            # Nobody has asked a question, so there is no answer to fetch --
            # but a real device still ANSWERS. Measured on a physical wired
            # DualSense 2026-08-27 (emulator/tools/hid_diff_probe.py): a cold
            # GET_REPORT(Feature, 0x81) returns 64 bytes of `81 00 00 ...`, it
            # does not STALL. Stalling here was visible behaviour, not a detail:
            # dualsense-tester's getTestResult() is
            # `while (report = await receiveFeatureReport(item, 0x81))`, so a
            # STALL throws out of the loop and fails the command outright,
            # where a real pad just returns an idle header and lets the caller
            # poll out its own 1000 ms budget.
            return FEATURE_TEST_IDLE
        self.stats["feature_test_reads"] += 1
        # A timeout or a link hiccup must not turn into a STALL either: answer
        # idle, the way the physical device does when it has nothing to say.
        return self._bt_call(self._bt_read_test_result) or FEATURE_TEST_IDLE

    def _bt_call(self, fn, *args):
        """Run `fn` on the writer thread and wait a bounded time for its result.

        The only place the USB request path waits on Bluetooth, and it is a
        control transfer (GET_REPORT(0x81)), never an isochronous deadline. The
        work itself still runs on the writer thread, so hidapi is never touched
        from the asyncio server thread -- see the module docstring. On timeout
        the caller STALLs, which the host retries.
        """
        done = threading.Event()
        box: dict = {}

        def job():
            try:
                box["value"] = fn(*args)
            except Exception as e:  # noqa: BLE001
                box["error"] = e
            finally:
                done.set()

        self._defer(job)
        if not done.wait(self.feature_timeout):
            self.stats["feature_bt_timeouts"] += 1
            log.debug("Bluetooth feature call timed out after %.0f ms",
                      self.feature_timeout * 1000)
            return None
        if "error" in box:
            self.stats["feature_test_errors"] += 1
            log.debug("Bluetooth feature call failed: %s", box["error"])
            return None
        return box.get("value")

    # =====================================================================
    # Backend contract — audio
    # =====================================================================

    def write_audio_out(self, pcm: bytes) -> None:
        """4-channel interleaved s16le @48 kHz from the host's speaker stream."""
        self.stats["audio_out_calls"] += 1
        self.stats["audio_out_bytes"] += len(pcm)
        if self.blackbox is not None:
            # ~1000/s: a summary line, never the PCM itself. "silent" tells a
            # dump reader whether the host was actually driving the speaker.
            self._bb("usb.audio_out",
                     note=f"{len(pcm)}B"
                          f"{' silent' if pcm.count(0) == len(pcm) else ''}")
        self._last_audio_out = time.perf_counter()
        if self.auto_arm and not self._audio_armed.is_set():
            self._audio_armed.set()
        self._out_ring.write(pcm)

    def read_audio_in(self, nbytes: int) -> bytes:
        """Exactly `nbytes` of 2-channel interleaved s16le @48 kHz.

        Called once per isochronous IN packet from the asyncio server thread,
        which owes the endpoint a 1 ms deadline: this is a memcpy plus at most
        one 4-byte skew correction, and it never touches Bluetooth.
        """
        self.stats["audio_in_bytes"] += nbytes
        if self.auto_arm and not self._mic_armed.is_set() and self.mic_always_on:
            # Arming sleeps 20 ms twice inside hidapi — off the URB path it goes.
            self.arm_mic(True)

        depth = len(self._mic_ring)
        self._mic_depth_n += 1
        self._mic_depth_sum += depth
        self._mic_depth_min = min(self._mic_depth_min, depth)
        self._mic_depth_max = max(self._mic_depth_max, depth)

        if not self._mic_primed:
            if depth < MIC_PRIME_BYTES:
                return b"\0" * nbytes
            self._mic_primed = True

        pad = self._mic_depth_correction(depth)
        before = self._mic_ring.underruns
        data = self._mic_ring.read_exact(max(0, nbytes - pad))
        if pad:
            data = b"\0" * pad + data
        if self._mic_ring.underruns != before:
            # Genuinely dry: re-prime rather than dribble one packet at a time,
            # which would turn one gap into a run of them.
            self.stats["mic_underrun_calls"] += 1
            self.stats["mic_reprimes"] += 1
            self._mic_primed = False
        return data

    def _mic_depth_correction(self, depth: int) -> int:
        """Steer the mic ring back towards MIC_PRIME_BYTES. Returns bytes to pad.

        The controller's crystal and this PC's are independent (domain C vs U in
        the module docstring), so this buffer really does drift — the only one in
        the system that does. Authority is deliberately tiny: one 48 kHz frame
        per call, which at 1000 calls/s is +/-2 % against a crystal error of
        order 0.01 %. Inside [MIC_LOW, MIC_HIGH] nothing happens at all.
        """
        frame = USB_IN_FRAME_BYTES
        if depth > MIC_HIGH_BYTES:
            if self._mic_ring.drop_oldest(frame):
                self.stats["mic_skew_drop_frames"] += 1
            return 0
        if depth < MIC_LOW_BYTES:
            self.stats["mic_skew_pad_frames"] += 1
            return frame
        return 0

    # =====================================================================
    # additive hooks (see the Backend base class)
    # =====================================================================

    def set_alt_setting(self, interface: int, alt: int) -> None:
        """SET_INTERFACE on an AudioStreaming interface opens/closes a stream.

        alt 0 is the zero-bandwidth setting every UAC1 streaming interface
        boots into; alt 1 is the only other one this device declares. Windows
        switches to alt 1 exactly when an application opens the endpoint, which
        makes this the correct trigger for arming the Bluetooth microphone —
        arming it earlier would drain a 10 %-battery controller for nothing.

        Runs on the asyncio server thread. The Bluetooth half of arming (two
        writes with a 20 ms settle between them) is posted to the writer thread
        — blocking here would stall every endpoint, isochronous included, at
        exactly the moment the host opens the stream.
        """
        self._bb("usb.set_interface", note=f"iface={interface} alt={alt}")
        if interface == D.IFACE_AUDIO_OUT:
            if alt:
                self._audio_armed.set()
            else:
                self._audio_armed.clear()
                self._out_ring.clear()
        elif interface == D.IFACE_AUDIO_IN:
            self.arm_mic(bool(alt))

    def on_uac_control(self, unit: int, selector: int, value: int) -> None:
        """A UAC1 SET_CUR landed on a feature unit; mirror it onto the DualSense.

        The mapping is a heuristic (see `_uac_db_to_byte`): the emulator's UAC
        volume range is itself an assumed value, so this cannot be exact until
        the real wired controller is sniffed (risk R7).

        Runs on the asyncio server thread, so the Bluetooth write is posted to
        the writer thread rather than performed here (module docstring, last
        paragraph). The SetState body is built here — it is pure arithmetic —
        and only the `hid.write()` is deferred.
        """
        from .uac import FU_MUTE_CONTROL, FU_VOLUME_CONTROL

        self._bb("usb.uac_control",
                 note=f"unit={unit} selector={selector} value={value}")
        st = P.SetState()
        if unit == D.UNIT_FU_SPEAKER:
            vol = 0 if (selector == FU_MUTE_CONTROL and value) else (
                _uac_db_to_byte(value) if selector == FU_VOLUME_CONTROL
                else self.speaker_volume
            )
            self.speaker_volume = vol
            if self.target == "headphone":
                st.headphone_volume(vol)
            else:
                st.speaker_volume(vol)
        elif unit == D.UNIT_FU_MIC:
            if selector == FU_MUTE_CONTROL:
                self._defer(self._arm_mic_now, self._mic_armed.is_set(), bool(value))
                return
            st.mic_volume(_uac_db_to_byte(value) if selector == FU_VOLUME_CONTROL else 0x08)
        else:
            return
        self._defer(self._write_setstate_body, bytes(st.body))

    def _defer(self, fn, *args) -> None:
        """Post to the writer thread if it exists, else run inline (tests)."""
        if self._threads:
            self._post_control(fn, *args)
        else:
            fn(*args)

    def _write_setstate_body(self, body: bytes) -> None:
        self._write_raw(P.build_bt_setstate(body, self._next_bt_seq()))

    # =====================================================================
    # microphone arming
    # =====================================================================

    def arm_mic(self, on: bool, muted: bool = False) -> None:
        """Arm/disarm the Bluetooth microphone. Safe to call from any thread.

        The state flag flips immediately (so `_send_39` picks up `mic_enabled`
        on its very next report) and the two blocking hidapi writes are posted
        to the writer thread. When no threads are running — unit tests, or a
        caller driving the backend by hand — the work is done inline so the
        behaviour is unchanged.
        """
        if on == self._mic_armed.is_set():
            return
        if on:
            self._mic_armed.set()
        else:
            self._mic_armed.clear()
            self._mic_ring.clear()
            self._mic_primed = False
        self._defer(self._arm_mic_now, on, muted)

    def _arm_mic_now(self, on: bool, muted: bool = False) -> None:
        """The Phase-1 arming pair: 0x31 mic-state then 0x32 mic-control."""
        dev = self._dev
        if dev is None:
            return
        self._bb("bt.mic_arm", note=f"on={int(on)} muted={int(muted)} "
                                    f"(0x31 mic-state + 0x32 mic-control pair)")
        try:
            if on:
                dev.send_mic_state(True, muted=muted, headset_plugged=False)
                time.sleep(0.02)
                dev.send_mic_control(True)
            else:
                dev.send_mic_control(False)
                time.sleep(0.02)
                dev.send_mic_state(False)
            log.info("microphone %s", "armed" if on else "disarmed")
        except Exception as e:  # noqa: BLE001
            log.warning("mic arming failed: %s", e)
            self._bb("bt.mic_arm.error", note=repr(e))

    def _disarm_mic_best_effort(self) -> None:
        if self._mic_armed.is_set():
            self._mic_armed.clear()
            self._arm_mic_now(False)

    # =====================================================================
    # reporting
    # =====================================================================

    def device_status(self) -> dict:
        """Battery and link health, decoded on demand from the latest report.

        Deliberately NOT computed on the hot path: `read_input_report` runs 250
        times a second on the request path and must stay a memcpy. This decodes
        one 63-byte report when somebody asks (a tray refresh, a battery log
        line) — a few microseconds, off the event loop.
        """
        with self._input_lock:
            report = self._latest_input
            at = self._latest_input_at
        stale = (time.perf_counter() - at) if at else None
        out = {
            "connected": self.connected.is_set(),
            "serial": self.serial,
            "stale_s": round(stale, 3) if stale is not None else None,
            "battery_percent": None,
            "battery_state": "",
            "headphone": None,
            "mic_muted": None,
        }
        if report is None:
            return out
        st = P.decode_input(report[1:], usb=True)
        if st is None:
            return out
        out["battery_percent"] = min(100, st.battery_level * 10)
        out["battery_state"] = st.battery_state
        out["headphone"] = st.headphone
        out["mic_muted"] = st.mic_muted
        return out

    def summary(self) -> str:
        s = self.stats
        return (
            f"bt={s['bt_reports']} (ctrl {s['bt_control']}, mic {s['bt_mic']}, "
            f"err {s['bt_read_errors']})  input_out={s['input_delivered']}  "
            f"polled={s['input_polled']} (fallbacks {s['poll_fallbacks']}, "
            f"err {s['input_poll_errors']})  "
            f"setstate in/sent/coalesced/merged={s['setstate_in']}/{s['setstate_sent']}/"
            f"{s['setstate_coalesced']}/{s['setstate_merged']}  "
            f"feature0x80 fwd/blocked={s['feature_test_forwarded']}/"
            f"{s['feature_test_blocked']} 0x81 reads/timeouts="
            f"{s['feature_test_reads']}/{s['feature_bt_timeouts']}  "
            f"0x39={s['reports_39']} "
            f"(err {s['report_39_errors']}, underrun frames "
            f"{s['audio_underrun_frames']}, q-drop {s['audio_q_drop_frames']})  "
            f"audio_out={s['audio_out_bytes']}B "
            f"audio_in={s['audio_in_bytes']}B  mic_ring_drop={self._mic_ring.dropped}B "
            f"out_ring_drop={self._out_ring.dropped}B  "
            f"mic skew pad/drop={s['mic_skew_pad_frames']}/{s['mic_skew_drop_frames']} "
            f"underrun_calls={s['mic_underrun_calls']}"
        )

    def clock_seam(self) -> dict:
        """The U<->B and C->U clock-seam health, as JSON-able data.

        This is the Phase 3c measurement: everything that says whether the two
        clock domains stayed in step, and by how much they had to be corrected
        when they did not. See the module docstring for what each domain is.
        `ds5emu.__main__` renders this under `--stats-json` and at shutdown.
        """
        s = self.stats
        n = max(1, self._mic_depth_n)
        in_ms = 1000.0 / (USB_RATE * USB_IN_FRAME_BYTES)    # bytes -> ms, 2ch
        out_ms = 1000.0 / (USB_RATE * USB_OUT_FRAME_BYTES)  # bytes -> ms, 4ch
        seen = self._mic_depth_min <= self._mic_depth_max
        return {
            # -- U -> B: host speaker/haptics -> 0x39. One oscillator, so any
            # movement here is phase (burst arrival), never rate.
            "audio_q_depth_frames": self.audio_q_depth,
            "audio_q_target_frames": AUDIO_Q_TARGET_FRAMES,
            "audio_q_high_frames": AUDIO_Q_HIGH_FRAMES,
            "audio_q_drop_frames": s["audio_q_drop_frames"],
            "audio_underrun_frames": s["audio_underrun_frames"],
            "out_ring_ms": round(len(self._out_ring) * out_ms, 3),
            "out_ring_peak_ms": round(self._out_ring.peak * out_ms, 3),
            "out_ring_cap_ms": AUDIO_OUT_RING_MS,
            "out_ring_overflow_bytes": self._out_ring.dropped,
            "reports_39": s["reports_39"],
            "report_39_errors": s["report_39_errors"],
            # -- C -> U: controller mic -> host. Independent oscillator; this
            # is the one path where drift is real, so the governor is counted.
            "mic_payloads": s["bt_mic"],
            "mic_decode_errors": s["mic_decode_errors"],
            "mic_depth_ms_mean": round(self._mic_depth_sum / n * in_ms, 3),
            "mic_depth_ms_min": round((self._mic_depth_min if seen else 0) * in_ms, 3),
            "mic_depth_ms_max": round(self._mic_depth_max * in_ms, 3),
            "mic_depth_target_ms": MIC_PRIME_MS,
            "mic_depth_band_ms": [MIC_LOW_MS, MIC_HIGH_MS],
            "mic_skew_pad_frames": s["mic_skew_pad_frames"],
            "mic_skew_drop_frames": s["mic_skew_drop_frames"],
            "mic_underrun_calls": s["mic_underrun_calls"],
            "mic_reprimes": s["mic_reprimes"],
            "mic_ring_overflow_bytes": self._mic_ring.dropped,
            # -- the URB path never blocks on Bluetooth: every job counted here
            # is one that would otherwise have run on the asyncio thread.
            "control_jobs": s["control_jobs"],
            "control_jobs_dropped": s["control_jobs_dropped"],
        }
