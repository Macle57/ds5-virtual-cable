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

        # --- observability -------------------------------------------------
        self.stats = {
            "bt_reports": 0,
            "bt_control": 0,
            "bt_mic": 0,
            "bt_read_errors": 0,
            "input_delivered": 0,
            "input_repeated": 0,
            "input_none": 0,
            "setstate_in": 0,
            "setstate_sent": 0,
            "setstate_coalesced": 0,
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
        }
        #: Sampled mic-ring depth in bytes, for the drift report. Cheap: three
        #: integers updated per `read_audio_in`.
        self._mic_depth_n = 0
        self._mic_depth_sum = 0
        self._mic_depth_min = 1 << 30
        self._mic_depth_max = 0
        self.connected = threading.Event()

    # =====================================================================
    # lifecycle
    # =====================================================================

    def start(self) -> None:
        if self._threads:
            return
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
        try:
            fw = dev.firmware_info()
            self._features[0x20] = self._feature_bytes(0x20, fw)
        except Exception as e:  # noqa: BLE001
            log.warning("feature 0x20 read failed: %s", e)
        with self._dev_lock:
            self._dev = dev
        self.connected.set()
        log.info("opened %s", DEV.describe(info))
        self._prime_setstate()

    @staticmethod
    def _feature_bytes(report_id: int, raw: bytes) -> bytes:
        """hidapi returns the feature body with the report id already at [0]."""
        b = bytes(raw)
        if not b or b[0] != report_id:
            b = bytes([report_id]) + b
        return b

    def _prime_setstate(self) -> None:
        """One SetState that routes audio and sets the volumes.

        Only the audio valid-flag bits are set, so nothing the host later sends
        (lightbar, rumble, triggers) is affected — the controller applies a
        field only when its valid-flag bit is present.
        """
        st = P.SetState()
        if self.target == "headphone":
            st.headphone_volume(self.speaker_volume)
        else:
            st.speaker_volume(self.speaker_volume)
        st.haptic_volume(self.haptic_volume)
        self._write_raw(P.build_bt_setstate(bytes(st.body), self._next_bt_seq()))

    def _next_bt_seq(self) -> int:
        dev = self._dev
        if dev is None:
            return 0
        return dev._next_seq()  # noqa: SLF001 - the seq counter lives on the device

    def _write_raw(self, data: bytes) -> bool:
        dev = self._dev
        if dev is None:
            return False
        try:
            dev.write_raw(data)  # return value is meaningless on Windows
            return True
        except Exception as e:  # noqa: BLE001
            log.debug("hid write failed: %s", e)
            self._on_io_error()
            return False

    def _on_io_error(self) -> None:
        if self.connected.is_set():
            self.stats["disconnects"] += 1
            log.warning("Bluetooth link lost; the virtual device stays attached "
                        "and will report a neutral controller until it returns")
        self.connected.clear()

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
        while not self._stop.is_set():
            dev = self._dev
            if dev is None or not self.connected.is_set():
                if not self.reconnect or not self._reconnect_loop():
                    return
                consecutive_errors = 0
                continue
            try:
                raw = dev.read_raw(200)
            except Exception as e:  # noqa: BLE001
                self.stats["bt_read_errors"] += 1
                consecutive_errors += 1
                log.debug("hid read failed: %s", e)
                if consecutive_errors > 5:
                    self._on_io_error()
                continue
            if not raw:
                # A quiet link, not a broken one: every read timed out cleanly.
                # Nothing above will ever trip, so the watchdog has to.
                if (self._latest_input_at
                        and time.perf_counter() - self._latest_input_at > LINK_DEAD_S):
                    self.stats["link_watchdog_trips"] += 1
                    log.warning("no Bluetooth report for %.1fs -- treating the link "
                                "as dead", LINK_DEAD_S)
                    self._on_io_error()
                continue
            consecutive_errors = 0
            self.stats["bt_reports"] += 1
            if raw[0] != P.BT_INPUT_31:
                continue
            payload = raw[1:]
            ptype = payload[0] & P.PAYLOAD_TYPE_MASK

            if ptype == P.PAYLOAD_TYPE_CONTROL:
                self.stats["bt_control"] += 1
                usb = T.bt31_payload_to_usb01(payload)
                if usb is not None:
                    with self._input_lock:
                        self._latest_input = usb
                        self._latest_input_at = time.perf_counter()
                        if self.keep_raw:
                            self._latest_bt_payload = payload
                        self._input_serial += 1
            elif ptype == P.PAYLOAD_TYPE_AUDIO:
                self.stats["bt_mic"] += 1
                self._on_mic_payload(dec, payload)

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
            if self._write_raw(T.usb02_to_bt31(body, self._next_bt_seq())):
                self.stats["setstate_sent"] += 1
                self._setstate_last = body
                last_sent_at = time.perf_counter()

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
        """USB output report 0x02 -> BT 0x31. Never blocks on Bluetooth."""
        self.stats["setstate_in"] += 1
        body = T.usb02_body(bytes(data))
        with self._setstate_lock:
            self._setstate_pending = body
        self._setstate_event.set()

    def get_feature_report(self, report_id: int, length: int) -> bytes | None:
        body = self._features.get(report_id)
        if body is None:
            self.feature_misses[report_id] = self.feature_misses.get(report_id, 0) + 1
            return None
        return body[:length] if length < len(body) else body

    def set_feature_report(self, report_id: int, data: bytes) -> None:
        """Recorded, never forwarded.

        Feature *writes* are how a DualSense is re-paired (0x09) and how its
        firmware is touched. Nothing in this project needs to write one, and a
        stray host-issued write must not reach the physical controller, so they
        are logged and dropped.
        """
        self.feature_writes.append((report_id, bytes(data)))
        log.info("set_feature_report 0x%02x (%d bytes) recorded, NOT forwarded",
                 report_id, len(data))

    # =====================================================================
    # Backend contract — audio
    # =====================================================================

    def write_audio_out(self, pcm: bytes) -> None:
        """4-channel interleaved s16le @48 kHz from the host's speaker stream."""
        self.stats["audio_out_calls"] += 1
        self.stats["audio_out_bytes"] += len(pcm)
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
            f"setstate in/sent/coalesced={s['setstate_in']}/{s['setstate_sent']}/"
            f"{s['setstate_coalesced']}  0x39={s['reports_39']} "
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
