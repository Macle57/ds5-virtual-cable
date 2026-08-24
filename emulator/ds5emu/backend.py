"""Backend interface — where the Phase-1 Bluetooth bridge plugs in.

The emulated USB device (`ds5emu.device`) never talks to hardware. Everything
that would reach a real DualSense goes through a `Backend`:

    USB interrupt IN  0x84  <-  read_input_report()
    USB interrupt OUT 0x03  ->  write_output_report()
    USB control GET_REPORT  <-  get_feature_report()
    USB control SET_REPORT  ->  set_feature_report()
    USB iso OUT 0x01        ->  write_audio_out()   4ch/48k/16 interleaved
    USB iso IN  0x82        <-  read_audio_in()     2ch/48k/16 interleaved

Two implementations live here:

* `SyntheticBackend` — no hardware at all. Used by the unit tests and by
  Phase-3 experiment E1 (see docs/virtualization-options.md §8), which measures
  isochronous timing without involving Bluetooth.
* `BridgeBackend` — adapts `prototype/ds5bridge`. **Skeleton only, never run.**
  Phase 3 owns finishing and verifying it; the TODOs mark exactly what is
  missing and STATUS.md records it as unverified.
"""

from __future__ import annotations

import math
import struct
import threading
import time
from abc import ABC, abstractmethod

from . import descriptors as D


class Backend(ABC):
    """Everything the emulated device needs from the world."""

    def start(self) -> None:  # pragma: no cover - trivial
        pass

    def stop(self) -> None:  # pragma: no cover - trivial
        pass

    # ---- HID ------------------------------------------------------------

    @abstractmethod
    def read_input_report(self, max_len: int) -> bytes | None:
        """One USB-shape input report (report id 0x01, 64 bytes) or None.

        None means "nothing queued" — the caller decides whether to wait or to
        complete the URB with a zero-length transfer.
        """

    @abstractmethod
    def write_output_report(self, data: bytes) -> None:
        """A USB-shape output report (report id 0x02) from the host."""

    def get_feature_report(self, report_id: int, length: int) -> bytes | None:
        """Feature report body INCLUDING the leading report id, or None to STALL."""
        return None

    def set_feature_report(self, report_id: int, data: bytes) -> None:
        pass

    # ---- audio ----------------------------------------------------------

    @abstractmethod
    def write_audio_out(self, pcm: bytes) -> None:
        """4-channel interleaved s16le at 48 kHz, from the host's speaker stream.

        ch0/ch1 -> speaker or headphone, ch2/ch3 -> haptics (see FINDINGS.md).
        """

    @abstractmethod
    def read_audio_in(self, nbytes: int) -> bytes:
        """Exactly `nbytes` of 2-channel interleaved s16le at 48 kHz.

        Must never block for long and must never return short: underruns are
        filled with silence, because an isochronous IN packet has to be
        answered on time or the audio stack glitches.
        """


class SyntheticBackend(Backend):
    """Hardware-free backend: a canned input report and a generated tone.

    Also records iso-OUT arrival timestamps, which is the measurement that
    experiment E1 needs.
    """

    #: Interrupt IN 0x84 declares bInterval = 6, i.e. 2^(6-1) = 32 microframes
    #: = 4 ms at high speed. Phase 0 measured the physical wired controller at
    #: 250.07 Hz (docs/STATUS.md §5.3), so that is the rate to emulate.
    INPUT_REPORT_HZ = 250.0

    def __init__(self, tone_hz: float = 1000.0, amplitude: float = 0.25,
                 record_timing: bool = True, input_report_hz: float | None = None):
        self.tone_hz = tone_hz
        self.amplitude = amplitude
        self.record_timing = record_timing
        #: 0 or None disables pacing (tests that want a report on every call).
        self.input_report_hz = (
            self.INPUT_REPORT_HZ if input_report_hz is None else input_report_hz
        )

        self._lock = threading.Lock()
        self._phase = 0.0
        self._counter = 0
        self._next_report_at = 0.0

        # observability, consumed by tools and tests
        self.audio_out_packets = 0
        self.audio_out_bytes = 0
        self.audio_in_bytes = 0
        self.output_reports: list[bytes] = []
        self.feature_writes: list[tuple[int, bytes]] = []
        self.iso_out_timestamps: list[float] = []

    # ---- HID ------------------------------------------------------------

    def read_input_report(self, max_len: int) -> bytes | None:
        """A plausible idle USB input report: id 0x01, sticks centred.

        Returns None until the next 4 ms service interval is due. Without this
        the interrupt IN URB completes the instant it arrives and the host
        resubmits immediately — measured 15 526 reports/s through hidapi on
        2026-08-24, 62x the real device's 250 Hz. A real endpoint NAKs instead.
        The clock-driven schedule (rather than sleeping) is the same trick
        `prototype/ds5bridge/pacing.py::Pacer` uses.
        """
        with self._lock:
            if self.input_report_hz:
                now = time.perf_counter()
                if now < self._next_report_at:
                    return None
                period = 1.0 / self.input_report_hz
                # Resynchronise rather than accumulate backlog after a stall.
                self._next_report_at = max(now, self._next_report_at + period)
            self._counter = (self._counter + 1) & 0xFF
            counter = self._counter
        body = bytearray(64)
        body[0] = 0x01              # report id
        body[1] = body[2] = 0x80    # left stick centred
        body[3] = body[4] = 0x80    # right stick centred
        body[7] = counter           # the device's own sequence byte
        return bytes(body[:max_len])

    def write_output_report(self, data: bytes) -> None:
        self.output_reports.append(bytes(data))

    def get_feature_report(self, report_id: int, length: int) -> bytes | None:
        # 0x05 calibration is 41 bytes and 0x20 firmware info is 64 on both
        # transports (docs/STATUS.md §5.1). Return correctly sized zero-filled
        # placeholders so the shape is right even though the content is not.
        sizes = {0x05: 41, 0x20: 64}
        if report_id not in sizes:
            return None
        buf = bytearray(sizes[report_id])
        buf[0] = report_id
        return bytes(buf[:length]) if length < len(buf) else bytes(buf)

    def set_feature_report(self, report_id: int, data: bytes) -> None:
        self.feature_writes.append((report_id, bytes(data)))

    # ---- audio ----------------------------------------------------------

    def write_audio_out(self, pcm: bytes) -> None:
        if self.record_timing:
            self.iso_out_timestamps.append(time.perf_counter())
        self.audio_out_packets += 1
        self.audio_out_bytes += len(pcm)

    def read_audio_in(self, nbytes: int) -> bytes:
        frame = D.AUDIO_IN_CHANNELS * D.AUDIO_BYTES_PER_SAMPLE
        nframes = nbytes // frame
        step = 2.0 * math.pi * self.tone_hz / D.AUDIO_SAMPLE_RATE
        peak = int(self.amplitude * 32767)
        out = bytearray()
        with self._lock:
            phase = self._phase
            for _ in range(nframes):
                v = int(peak * math.sin(phase))
                out += struct.pack("<hh", v, v)
                phase += step
            self._phase = math.fmod(phase, 2.0 * math.pi)
            self.audio_in_bytes += nbytes
        return bytes(out).ljust(nbytes, b"\0")[:nbytes]


class BridgeBackend(Backend):
    """Adapter onto `prototype/ds5bridge`. SKELETON — NOT YET EXERCISED.

    Phase 3 must finish and verify this. The hard parts, all of which are
    already solved on the Bluetooth side by Phase 1 and are only a matter of
    wiring here:

    * USB input report `0x01`/64 B vs BT input `0x31`/78 B — the payload bodies
      line up after the BT seq-nibble/CRC wrapper is stripped; reuse
      `ds5bridge.protocol` decode/encode rather than re-deriving offsets.
    * USB output report `0x02` body is the same SetState body the BT `0x31`
      wrapper carries, so this is unwrap-then-rewrap, not a translation.
    * Audio: the host hands us 4ch/48 kHz in 1 ms chunks; the BT side wants
      45 kHz resampled, Opus-encoded 10.667 ms frames (STATUS.md §5.6, §7).
      A jitter buffer between the two rates belongs here, NOT in the USB layer.
    * Haptics: ch2/ch3 -> int8 stereo at 3 kHz, 64-byte subpacket.
    * Mic: BT delivers 10 ms Opus frames at ~99/s; the USB side asks for 1 ms
      slices, so a decode + ring buffer is needed, with silence on underrun.
    * `write_audio_out` and `read_audio_in` are called from the USB/IP request
      path. They must not block on Bluetooth I/O. Hand off to the existing
      `ds5bridge.pacing.Pacer` thread.
    * STATUS.md gotcha #1: `mic_active=True` in report 0x36 is REQUIRED for
      simultaneous playback + capture, which a wired DualSense always does.
    """

    def __init__(self, transport: str = "BT"):
        self.transport = transport
        self._dev = None
        raise NotImplementedError(
            "BridgeBackend is a Phase-3 deliverable; use SyntheticBackend for now"
        )

    def read_input_report(self, max_len: int):  # pragma: no cover
        raise NotImplementedError

    def write_output_report(self, data: bytes) -> None:  # pragma: no cover
        raise NotImplementedError

    def write_audio_out(self, pcm: bytes) -> None:  # pragma: no cover
        raise NotImplementedError

    def read_audio_in(self, nbytes: int) -> bytes:  # pragma: no cover
        raise NotImplementedError
