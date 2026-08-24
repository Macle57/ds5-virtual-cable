# emulator/ — user-mode USB/IP device emulator for a wired DualSense

> **Just want to use it?** Go to `docs/USER-GUIDE.md` and `app/`. Since Phase 4a
> the whole bring-up is one command — `ds5bridge` — which finds the controller,
> serves this emulator in-process, runs `usbip attach`, supervises, and tears
> everything down again on Ctrl+C, a closed window or a crash. The two-terminal
> procedure below still works and is what you want when developing *this*
> directory.

Serves the **real** wired controller's descriptors (read off the physical device
in Phase 0, `docs/usb-ground-truth.md`) over the USB/IP wire protocol, so that
vadimgrn/usbip-win2's Microsoft-signed UDE driver can attach it as a local USB
device. Written in Phase 2; **validated against the live driver in Phase 3a**.

**Nothing here installs or loads a driver.** The full test suite runs on a bare
machine with no driver present. Attaching for real needs usbip-win2 installed
(`docs/install-record.md`).

## Quick start

```powershell
cd D:\Codes\dualSense\ds5-virtual-usb\emulator

# 185 protocol / timing / translation tests, no driver, no hardware
..\prototype\.venv\Scripts\python.exe -m unittest discover -s tests -t .

# dump the descriptors we would serve
..\prototype\.venv\Scripts\python.exe -m ds5emu descr

# run the server on loopback (binds 127.0.0.1:3240; nothing else happens
# until a USB/IP client attaches, which needs the driver from Phase 3)
..\prototype\.venv\Scripts\python.exe -m ds5emu serve
```

No third-party packages are needed — stdlib only. The existing
`prototype/.venv` is reused purely out of convenience.

## Layout

```
ds5emu/
  wire.py         USB/IP wire protocol. Pure bytes in / bytes out, no I/O.
  descriptors.py  Ground-truth descriptor tables + derived constants.
  uac.py          UAC1 mixer control state (mute/volume on the two feature units).
  timing.py       FrameClock (WE are the isochronous clock -- read it), UrbMeter,
                  TimerResolution.
  device.py       The emulated device: CMD_SUBMIT -> RET_SUBMIT. Pure, no threads.
  backend.py      Backend ABC; SyntheticBackend (hardware-free).
  bridge.py       BridgeBackend: a LIVE Bluetooth DualSense behind the virtual
                  wired one. 3 threads, 4 ring buffers, the rate conversions,
                  and the U/B/C clock-domain seam -- read its docstring.
  translate.py    Pure BT<->USB report translation, stdlib only.
  server.py       asyncio TCP shell. Thin on purpose.
  __main__.py     CLI: serve / descr / selftest
tests/
  test_wire.py         24 tests: byte-exact wire format
  test_descriptors.py  14 tests: descriptors vs docs/usb-ground-truth.md
  test_device.py       41 tests: control / interrupt / isochronous behaviour
  test_server.py       12 tests: full client-server round trips over loopback TCP
  test_timing.py       22 tests: frame clock, iso pacing, and the 4 ms
                       interrupt IN gate the endpoint owns
  test_translate.py    24 tests: BT<->USB report translation, incl. the
                       neutral report served while the link is down
  test_bridge.py       48 tests: BridgeBackend internals, the clock seam and
                       the link-loss policy (skipped without numpy/PyAV/hidapi)
```

The layering is deliberate: **everything protocol-shaped is a pure function**,
so the wire format and the device behaviour are testable without a socket, and
the socket layer is testable without a driver.

## Where the Phase-1 bridge plugs in

`backend.Backend` is the whole seam:

| USB traffic | Backend call |
|---|---|
| interrupt IN `0x84` | `read_input_report(max_len)` |
| interrupt OUT `0x03` | `write_output_report(data)` |
| control HID GET_REPORT (feature) | `get_feature_report(id, len)` |
| control HID SET_REPORT | `set_feature_report(id, data)` / `write_output_report` |
| isochronous OUT `0x01` | `write_audio_out(pcm)` — 4ch s16le @48 k |
| isochronous IN `0x82` | `read_audio_in(nbytes)` — 2ch s16le @48 k |

`SyntheticBackend` implements all of it with no hardware: a canned idle input
report and a generated sine on the mic. It also timestamps every isochronous
OUT packet, which is the measurement Phase-3 experiment E1 needs.

`BridgeBackend` (`bridge.py`) is **implemented and hardware-verified**. It
adapts `prototype/ds5bridge` onto a live Bluetooth DualSense: rate conversion
48 k↔45 k, Opus framing at 10.667 ms, the haptic channel split, the mic jitter
buffer, and the `mic_active` gotcha from STATUS.md §8.1. Run it with
`serve --backend bridge`.

Phase 3c attached it to Windows and verified **all five e2e stages**:
**249.90 Hz input with 100.00 % field parity** against a simultaneous direct
Bluetooth read; adaptive triggers driven by a hidapi write to the virtual
device; a tone played to the virtual render endpoint coming out of the real
speaker (+64.4 dB) and haptic voice coils (+27.8 dB) and heard back through the
controller's own microphone at the virtual capture endpoint; and a 120 s
concurrent soak at **1000.3 / 1001.1 isochronous packets/s with one frame-clock
resync per endpoint, both at stream start**. See `docs/e2e-results.md`.

## Language choice: Python, with an explicit escape hatch

Load the emulator will have to carry, worst case (both audio directions
streaming plus HID at full rate):

| stream | URBs/s | bytes/s |
|---|---|---|
| iso OUT `0x01` (usbaudio batches ~10 packets/URB) | ~100 | 392 000 |
| iso IN `0x82` | ~100 | 196 000 |
| interrupt IN `0x84` @ 4 ms | 250 | 16 000 |
| interrupt OUT `0x03` | ≤250 | ≤16 000 |
| **total** | **~700 req/resp pairs/s** | **~620 KB/s** |

CPython handles that comfortably; the concern is jitter, not throughput. Two
things make Python the right first choice here:

1. Phase 1 already demonstrated Python holding a hard real-time-ish cadence on
   this exact machine — `pacing.Pacer` sustained 93.75 fps with **0 dropped
   frames** over 6 s using `timeBeginPeriod(1)` (`docs/STATUS.md` §5.9).
2. The Bluetooth backend runs in the same process, so the audio hand-off is a
   queue append rather than an IPC hop. Rewriting the transport in C++ would
   force the bridge across a process boundary and *add* latency.

The escape hatch is real and cheap: `wire.py` and `device.py` are pure
functions over `bytes` with no asyncio in them, so the hot loop can move to
C++ (or the whole thing to a C++ server) while the descriptors, control
dispatch, HID translation and audio framing port unchanged.

Two Phase-1 gotchas apply verbatim to any timing loop added here
(`docs/STATUS.md` §8.3): never spin on a bare `pass` — it starves other Python
threads and looks exactly like a hardware failure — and never trust a write's
return value as a byte count.

## Protocol decisions that are load-bearing

All of these came from reading usbip-win2's sources, not from prose. Full
citations in `docs/virtualization-options.md` §2.8.

1. **`speed` must be `USB_SPEED_HIGH` (3)** in `usbip_usb_device`. usbip-win2's
   `patch_config()` rewrites isochronous `bInterval` to `min(bInterval + 3,
   16)` for anything below high speed, which would turn our 1 ms audio
   endpoints into 8 ms ones. Guarded by `if (dev.speed() < USB_SPEED_HIGH)` in
   `drivers/ude/wsk_receive.cpp`. Asserted by `test_device.IdentityTests`.
2. **Echo the busid** in the `OP_REP_IMPORT` reply. The driver compares it and
   aborts the attach on mismatch.
3. **`OP_REQ_DEVLIST` has no request body** — 8 bytes of `op_common` and
   nothing else.
4. **CMD_SUBMIT payload** = `[transfer buffer, OUT only][iso descriptors, iso
   only]`. **RET_SUBMIT payload** = `[transfer buffer, IN only][iso
   descriptors, iso only]`. An OUT reply carries no data no matter what
   `actual_length` says.
5. **Isochronous IN replies are compacted**: packets concatenated with no
   padding, `actual_length` = sum of per-packet actual lengths — but each
   returned descriptor's `offset` **must be the offset the client sent**,
   unchanged. usbip-win2's `validate()` hard-fails on a mismatch.
6. **`start_frame` matters.** usbip-win2 always sets
   `USBD_START_ISO_TRANSFER_ASAP` (it does not implement
   `URB_GET_CURRENT_FRAME_NUMBER`) and writes our reply's `start_frame`
   straight into `URB.StartFrame`, so we return a monotonic 1 kHz counter.
7. **`number_of_packets = -1`** means non-isochronous on the wire; replies use
   `0`, which the driver accepts either way.
8. Everything in the op phase and every header field is **big endian**;
   descriptor contents and USB setup packets stay **little endian**.

## The one thing to read before changing anything

**`timing.py`. The emulator is the audio clock.** Over USB/IP + UDE there is no
SOF and `usbip2_filter.sys` fakes `QueryBusTime` with a constant, so the audio
stream runs exactly as fast as this server completes isochronous URBs — nothing
else paces it. Measured on 2026-08-24 before pacing existed: a 48 kHz stream ran
at **174 429 frames/s, 3.63x real time, with Windows reporting zero underruns.**
`FrameClock` reserves 1 ms service intervals per endpoint and `server.py` sleeps
until the deadline. `docs/e1-results.md` §6 has the full story.

## Validated against the real driver (Phase 3a)

Experiments E1/E2/E3 ran against usbip-win2 0.9.7.7 on 2026-08-24 and **passed**:
Windows binds `usbccgp` + `usbaudio` + `HidUsb`, exposes a 4-channel render and a
2-channel capture endpoint, and the emulator sustains 384 kB/s out + 192 kB/s in
for 60 s with **zero underruns**. The descriptors Windows reads back are
byte-identical to the physical controller's. See `docs/e1-results.md` and
`docs/identity-comparison.md`.

Measurement is built in:

```powershell
..\prototype\.venv\Scripts\python.exe -m ds5emu serve --port 3241 `
    --stats-json C:\Temp\stats.json --stats-every 2 --record-out C:\Temp\out.raw
```

`--stats-json` is rewritten atomically while the server runs;
`endpoints.<ep>.resyncs` is the underrun counter (one at stream start is
expected). `--record-out` dumps the received speaker stream as raw 4ch s16le
48 kHz for offline FFT.

## Phase 4a additions

Two behaviours were added here for the product layer, both about a Bluetooth
link that goes away mid-session (STATUS.md §17.6(6) had this as "implemented,
never force-tested"):

* **`bridge.py` notices a link that goes quiet, not only one that errors.**
  `hid.read()` on a vanished device usually raises, and five raises trip the
  reconnect path — but a link can also simply stop delivering, every read
  timing out cleanly, in which case nothing ever trips. `LINK_DEAD_S` (4 s
  without a control payload) is the watchdog.
* **`translate.neutralize_usb01()` releases the controls while it is gone.**
  `repeat_stale_input` is right in normal operation, but repeating forever
  hands the game whatever was held when the link died. After `INPUT_NEUTRAL_S`
  (1 s) the repeated report has sticks centred, buttons released, the d-pad at
  8 (**not** 0 — a zero-filled report reads as UP held) and the touch points
  lifted, while battery/headphone/mic status survive untouched. The virtual
  device stays attached throughout, so a game sees somebody who stopped
  playing rather than a controller being unplugged.

`force_disconnect(hold_s=)` is the scriptable stand-in for a long PS-button
press; `app/tools/reconnect_test.py` drives it. Measured: controls released
1.00 s after the drop, link back 0.06 s after it becomes reopenable, 250
reports/s throughout, and the game's HID handle never dies.

`server.py`'s shutdown is also bounded now: `Server.wait_closed()` waits for
every connection handler, and when the usbip driver detaches it *resets* the
TCP connection, leaving the proactor transport's closed-future unresolved
forever. Teardown used to hang until the caller's timeout (15 s); both waits are
capped at 1 s and it now takes 2.1 s.

## What is NOT done

- **A game, or a Sony PC SDK title.** Now the only thing between "the pipe
  works" and "it is a controller". Biggest open gap.
- **Audio *fidelity*.** Every audio result is band energy at a commanded
  frequency against a silent baseline -- it proves the path carries the signal
  and the channel mapping is right, not that it is undistorted.
- **Anything longer than four minutes.** Longest continuous run: 120 s.
- UAC1 volume MIN/MAX/RES are **assumed** values, not captured from the
  physical controller (risk R7). Windows accepted them and built a working
  mixer, which is weaker evidence than sniffing the real answers.
- `SyntheticBackend`'s feature reports `0x05`/`0x20` are correctly *sized* but
  zero-filled, so anything parsing calibration or firmware strings sees garbage.
- No game or Sony PC SDK title has been run against the attached device.
