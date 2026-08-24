"""Audio pipeline: WAV I/O, resampling, Opus encode/decode, haptic PCM extraction.

Codec choice: **PyAV** (`pip install av`). Rationale --
  - its Windows wheels bundle libopus (av.libs/libopus-0-*.dll) plus swresample,
    so there is nothing to build and no separate opus.dll to hunt down;
  - ffmpeg's `libopus` encoder exposes exactly the knobs the controller needs
    (`application=lowdelay` = pure CELT, `frame_duration=10`, `vbr=off`, bit_rate),
    and it is verified below to emit exactly 200-byte packets;
  - the same library decodes the 71-byte mono mic frames.
`opuslib` was rejected: it is a ctypes binding with no bundled opus.dll on Windows.

The 45 kHz trick
----------------
The controller consumes the Opus stream at roughly 45 kHz while the frames are
*encoded* as nominal 48 kHz. So we resample the source to 45000 Hz and hand those
samples to a 48000 Hz encoder: each 480-sample frame then carries 480/45000 =
10.6667 ms of real audio and plays back at the right pitch.

That also makes every rate line up:
    audio   45000 Hz, 480 samples/frame -> 10.6667 ms
    haptics  3000 Hz,  32 samples/ch/frame (64 bytes stereo int8) -> 10.6667 ms
so one 0x36 report = one 10.6667 ms frame, and one 0x39 report = two of them.

(DS5Dongle does the equivalent by resampling 51200 -> 48000, ratio 1.0667:
 ../DS5Dongle/src/audio.cpp:483.)
"""

from __future__ import annotations

import fractions
import wave
from dataclasses import dataclass

import av
import numpy as np
from av.audio.frame import AudioFrame

OPUS_FRAME_BYTES = 200
HAPTIC_FRAME_BYTES = 64
HAPTIC_RATE = 3000
HAPTIC_SAMPLES_PER_FRAME = 32          # per channel
OPUS_NOMINAL_RATE = 48000
OPUS_SAMPLES_PER_FRAME = 480
AUDIO_SOURCE_RATE = 45000              # the "45 kHz trick" rate
FRAME_MS = 1000.0 * HAPTIC_SAMPLES_PER_FRAME / HAPTIC_RATE   # 10.666666... ms

MIC_OPUS_BYTES = 71
MIC_RATE = 48000


# ---------------------------------------------------------------------------
# WAV
# ---------------------------------------------------------------------------


@dataclass
class Wave:
    data: np.ndarray  # float32, shape (n, channels), range [-1, 1]
    rate: int

    @property
    def channels(self) -> int:
        return self.data.shape[1]

    @property
    def duration(self) -> float:
        return self.data.shape[0] / self.rate


def load_wav(path: str) -> Wave:
    with wave.open(path, "rb") as w:
        ch, sw, sr, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
        raw = w.readframes(n)
    if sw == 1:
        a = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    elif sw == 2:
        a = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif sw == 3:
        b = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3)
        i32 = (b[:, 0].astype(np.int32)
               | (b[:, 1].astype(np.int32) << 8)
               | (b[:, 2].astype(np.int8).astype(np.int32) << 16))
        a = i32.astype(np.float32) / 8388608.0
    elif sw == 4:
        a = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
    else:
        raise ValueError(f"unsupported sample width {sw}")
    return Wave(a.reshape(-1, ch), sr)


def save_wav(path: str, data: np.ndarray, rate: int) -> None:
    if data.ndim == 1:
        data = data.reshape(-1, 1)
    pcm = np.clip(data, -1.0, 1.0)
    pcm = (pcm * 32767.0).astype("<i2")
    with wave.open(path, "wb") as w:
        w.setnchannels(data.shape[1])
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())


# ---------------------------------------------------------------------------
# Resampling (swresample via PyAV -- good quality, already a dependency)
# ---------------------------------------------------------------------------


def resample(w: Wave, rate: int, channels: int = 2) -> np.ndarray:
    """Return float32 (n, channels) resampled to `rate`."""
    layout = "stereo" if channels == 2 else "mono"
    src_layout = "stereo" if w.channels == 2 else "mono"
    src = w.data
    if w.channels > 2:
        src = src[:, :2]
        src_layout = "stereo"
    elif w.channels == 1 and channels == 2:
        pass  # let swresample upmix

    if w.rate == rate and src.shape[1] == channels:
        return np.ascontiguousarray(src, dtype=np.float32)

    r = av.AudioResampler(format="flt", layout=layout, rate=rate)
    frame = AudioFrame.from_ndarray(
        np.ascontiguousarray(src.reshape(1, -1), dtype=np.float32),
        format="flt", layout=src_layout,
    )
    frame.sample_rate = w.rate
    frame.time_base = fractions.Fraction(1, w.rate)
    frame.pts = 0
    out = []
    for f in r.resample(frame):
        out.append(f.to_ndarray().reshape(-1, channels))
    for f in r.resample(None):
        out.append(f.to_ndarray().reshape(-1, channels))
    if not out:
        return np.zeros((0, channels), dtype=np.float32)
    return np.concatenate(out, axis=0).astype(np.float32)


# ---------------------------------------------------------------------------
# Opus
# ---------------------------------------------------------------------------


def make_encoder(channels: int = 2, bitrate: int = 160000) -> av.CodecContext:
    """CBR 160 kbps, 10 ms, low-delay (pure CELT) -- exactly 200 bytes/frame.

    Matches btAudioStream.ts:encodeOpusFrames (WebCodecs bitrateMode 'constant',
    application 'lowdelay') and DS5Dongle audio.cpp:473-481
    (OPUS_SET_BITRATE(200*8*100), OPUS_SET_VBR(false), 10 ms frames).
    """
    cc = av.codec.CodecContext.create("libopus", "w")
    cc.sample_rate = OPUS_NOMINAL_RATE
    cc.format = "flt"
    cc.layout = "stereo" if channels == 2 else "mono"
    cc.bit_rate = bitrate
    cc.options = {
        "application": "lowdelay",
        "frame_duration": "10",
        "vbr": "off",
        "compression_level": "1",
    }
    cc.open()
    return cc


def encode_opus_frames(pcm: np.ndarray, channels: int = 2,
                       bitrate: int = 160000) -> list[bytes]:
    """pcm: float32 (n, channels) already at AUDIO_SOURCE_RATE. Returns 200-byte frames."""
    cc = make_encoder(channels, bitrate)
    layout = "stereo" if channels == 2 else "mono"
    tb = fractions.Fraction(1, OPUS_NOMINAL_RATE)
    frames: list[bytes] = []
    n = pcm.shape[0]
    pad = (-n) % OPUS_SAMPLES_PER_FRAME
    if pad:
        pcm = np.vstack([pcm, np.zeros((pad, channels), dtype=np.float32)])
    for i in range(0, pcm.shape[0], OPUS_SAMPLES_PER_FRAME):
        chunk = np.ascontiguousarray(
            pcm[i : i + OPUS_SAMPLES_PER_FRAME].reshape(1, -1), dtype=np.float32
        )
        f = AudioFrame.from_ndarray(chunk, format="flt", layout=layout)
        f.sample_rate = OPUS_NOMINAL_RATE
        f.time_base = tb
        f.pts = i
        for p in cc.encode(f):
            frames.append(_fixed(bytes(p)))
    for p in cc.encode(None):
        frames.append(_fixed(bytes(p)))
    return frames


def _fixed(b: bytes) -> bytes:
    return b[:OPUS_FRAME_BYTES].ljust(OPUS_FRAME_BYTES, b"\x00")


_SILENT: bytes | None = None


def silent_opus_frame() -> bytes:
    """One 10 ms silent Opus frame -- used when we want haptics without sound."""
    global _SILENT
    if _SILENT is None:
        frames = encode_opus_frames(np.zeros((OPUS_SAMPLES_PER_FRAME * 3, 2), np.float32))
        _SILENT = frames[-1] if frames else b"\x00" * OPUS_FRAME_BYTES
    return _SILENT


class MicDecoder:
    """Decode the controller's 71-byte mono Opus mic frames."""

    def __init__(self, rate: int = MIC_RATE):
        cc = av.codec.CodecContext.create("libopus", "r")
        cc.sample_rate = rate
        cc.format = "flt"
        cc.layout = "mono"
        cc.open()
        self.cc = cc
        self.rate = rate

    @staticmethod
    def _norm(frame) -> np.ndarray:
        """ffmpeg's libopus decoder ignores a requested output format and emits
        whatever the build gives (s16 here). Normalise to float [-1, 1] by the
        frame's actual sample format rather than assuming."""
        a = frame.to_ndarray().reshape(-1)
        name = frame.format.name
        if name.startswith("s16"):
            return a.astype(np.float32) / 32768.0
        if name.startswith("s32"):
            return a.astype(np.float32) / 2147483648.0
        return a.astype(np.float32)

    def decode(self, opus: bytes) -> np.ndarray:
        pkt = av.Packet(bytes(opus))
        out = [self._norm(f) for f in self.cc.decode(pkt)]
        if not out:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(out)

    def flush(self) -> np.ndarray:
        out = []
        try:
            out = [self._norm(f) for f in self.cc.decode(None)]
        except Exception:  # noqa: BLE001
            pass
        return np.concatenate(out) if out else np.zeros(0, np.float32)


# ---------------------------------------------------------------------------
# Haptics
# ---------------------------------------------------------------------------


def pcm_to_haptic_frames(pcm3k: np.ndarray, gain: float = 1.0) -> list[bytes]:
    """pcm3k: float32 (n, 2) at 3000 Hz -> 64-byte int8 interleaved [L,R,...] frames."""
    x = np.clip(pcm3k * gain, -1.0, 1.0)
    i8 = np.round(x * 127.0).astype(np.int8)
    n = i8.shape[0]
    pad = (-n) % HAPTIC_SAMPLES_PER_FRAME
    if pad:
        i8 = np.vstack([i8, np.zeros((pad, 2), dtype=np.int8)])
    flat = i8.reshape(-1)  # already interleaved L,R,L,R...
    return [
        flat[i : i + HAPTIC_FRAME_BYTES].tobytes()
        for i in range(0, flat.size, HAPTIC_FRAME_BYTES)
    ]


def extract_haptic_frames(w: Wave, gain: float = 1.0) -> list[bytes]:
    return pcm_to_haptic_frames(resample(w, HAPTIC_RATE, 2), gain)


def sine_haptic_frames(freq_l: float, freq_r: float, seconds: float,
                       amp: float = 1.0) -> list[bytes]:
    """Pure tones straight into the voice coils at their native 3 kHz rate.

    3 kHz is the actuators' native rate, so anything above 1500 Hz aliases --
    that is real hardware behaviour, not a bug in this code.
    """
    n = int(round(seconds * HAPTIC_RATE))
    t = np.arange(n, dtype=np.float64) / HAPTIC_RATE
    left = np.sin(2 * np.pi * freq_l * t) * amp
    right = np.sin(2 * np.pi * freq_r * t) * amp
    return pcm_to_haptic_frames(np.stack([left, right], axis=1).astype(np.float32))


def silent_haptic_frame() -> bytes:
    return b"\x00" * HAPTIC_FRAME_BYTES


# ---------------------------------------------------------------------------
# Test signal generator
# ---------------------------------------------------------------------------


def generate_test_wav(path: str, seconds: float = 6.0, rate: int = 48000) -> str:
    """Sine sweep (200 Hz -> 4 kHz) over a 2 Hz beat pattern, hard-panned accents.

    Designed to exercise both paths at once: the sweep is audible on the speaker,
    the low beats are felt in the actuators, and the L/R accents make it obvious
    whether the stereo mapping survived the trip.
    """
    n = int(seconds * rate)
    t = np.arange(n, dtype=np.float64) / rate

    # log sweep 200 Hz -> 4000 Hz
    f0, f1 = 200.0, 4000.0
    k = (f1 / f0) ** (1.0 / seconds)
    phase = 2 * np.pi * f0 * (k**t - 1.0) / np.log(k)
    sweep = np.sin(phase) * 0.35

    # 2 Hz beat: 80 Hz bursts, alternating L/R every other beat
    beat_hz = 2.0
    env = np.clip(1.0 - 8.0 * ((t * beat_hz) % 1.0), 0.0, 1.0) ** 2
    thump = np.sin(2 * np.pi * 80.0 * t) * env * 0.55
    beat_index = np.floor(t * beat_hz).astype(int)
    left_beat = (beat_index % 2) == 0

    left = sweep + thump * left_beat
    right = sweep + thump * (~left_beat)

    # short fade in/out
    fade = int(0.01 * rate)
    ramp = np.linspace(0, 1, fade)
    for ch in (left, right):
        ch[:fade] *= ramp
        ch[-fade:] *= ramp[::-1]

    save_wav(path, np.stack([left, right], axis=1).astype(np.float32), rate)
    return path
