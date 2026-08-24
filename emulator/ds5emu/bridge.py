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

#: Microphone jitter buffer. Bluetooth delivers 10 ms bursts; the host asks in
#: 1 ms slices, so a little slack removes almost all underruns.
MIC_RING_MS = 200
MIC_RING_BYTES = int(MIC_RING_MS * USB_RATE / 1000) * USB_IN_FRAME_BYTES
MIC_PRIME_MS = 25
MIC_PRIME_BYTES = int(MIC_PRIME_MS * USB_RATE / 1000) * USB_IN_FRAME_BYTES

#: Stop transmitting 0x39 after this long with no host audio: it saves
#: Bluetooth airtime and, on a 10 %-battery controller, real power.
AUDIO_IDLE_TIMEOUT = 0.5

#: Minimum spacing between two SetState passthroughs, and the interval at which
#: an unchanged body is refreshed (0 disables the refresh).
SETSTATE_MIN_INTERVAL = 0.006
SETSTATE_REFRESH = 0.0


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
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

        # --- HID input ---------------------------------------------------
        self._input_lock = threading.Lock()
        self._latest_input: bytes | None = None
        self._latest_bt_payload: bytes | None = None   # only when keep_raw
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

        # --- audio --------------------------------------------------------
        self._out_ring = _ByteRing(AUDIO_OUT_RING_BYTES)
        self._mic_ring = _ByteRing(MIC_RING_BYTES)
        self._mic_primed = False
        self._last_audio_out = 0.0

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
            "mic_decode_errors": 0,
            "reconnects": 0,
        }
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
        for t in self._threads:
            t.join(timeout=3.0)
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

    def _open_device(self) -> None:
        info = DEV.pick(self.transport, self.device_index)
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
        self.connected.clear()

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

                # ---- start only once there is a little to work with --------
                if not running:
                    if len(opus_q) < 4 and len(hap_q) < 4:
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

                # Keep latency bounded: if the encoder ever ran ahead (a stall
                # elsewhere), drop the oldest frames rather than accumulate.
                if len(opus_q) > 12:
                    del opus_q[: len(opus_q) - 12]
                if len(hap_q) > 12:
                    del hap_q[: len(hap_q) - 12]

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

    def _writer_loop(self) -> None:
        last_sent_at = 0.0
        while not self._stop.is_set():
            self._setstate_event.wait(0.05)
            self._setstate_event.clear()
            if self._stop.is_set():
                return
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
            if self._input_serial == self._delivered_serial:
                if not self.repeat_stale_input:
                    self.stats["input_none"] += 1
                    return None
                self.stats["input_repeated"] += 1
            self._delivered_serial = self._input_serial
            report = self._latest_input
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
        """Exactly `nbytes` of 2-channel interleaved s16le @48 kHz."""
        self.stats["audio_in_bytes"] += nbytes
        if self.auto_arm and not self._mic_armed.is_set() and self.mic_always_on:
            self.arm_mic(True)
        if not self._mic_primed:
            if len(self._mic_ring) < MIC_PRIME_BYTES:
                return b"\0" * nbytes
            self._mic_primed = True
        data = self._mic_ring.read_exact(nbytes)
        if len(self._mic_ring) == 0:
            self._mic_primed = False
        return data

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
                self._arm_mic_now(self._mic_armed.is_set(), muted=bool(value))
                return
            st.mic_volume(_uac_db_to_byte(value) if selector == FU_VOLUME_CONTROL else 0x08)
        else:
            return
        self._write_raw(P.build_bt_setstate(bytes(st.body), self._next_bt_seq()))

    # =====================================================================
    # microphone arming
    # =====================================================================

    def arm_mic(self, on: bool, muted: bool = False) -> None:
        if on == self._mic_armed.is_set():
            return
        if on:
            self._mic_armed.set()
        else:
            self._mic_armed.clear()
            self._mic_ring.clear()
            self._mic_primed = False
        self._arm_mic_now(on, muted)

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

    def summary(self) -> str:
        s = self.stats
        return (
            f"bt={s['bt_reports']} (ctrl {s['bt_control']}, mic {s['bt_mic']}, "
            f"err {s['bt_read_errors']})  input_out={s['input_delivered']}  "
            f"setstate in/sent/coalesced={s['setstate_in']}/{s['setstate_sent']}/"
            f"{s['setstate_coalesced']}  0x39={s['reports_39']} "
            f"(err {s['report_39_errors']}, underrun frames "
            f"{s['audio_underrun_frames']})  audio_out={s['audio_out_bytes']}B "
            f"audio_in={s['audio_in_bytes']}B  mic_ring_drop={self._mic_ring.dropped}B "
            f"out_ring_drop={self._out_ring.dropped}B"
        )
