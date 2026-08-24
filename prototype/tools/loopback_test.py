"""Closed-loop hardware proof of the BT audio path, with no human ears required.

Play a pure tone out of the controller's own speaker via output report 0x36 while
simultaneously capturing the controller's own built-in microphone via input report
0x31 type-0x02, then FFT the capture. If the tone appears in the mic spectrum well
above the silent baseline, then the whole chain is proven on real hardware:

    WAV -> 45 kHz resample -> Opus CBR 160k/10ms lowdelay -> 0x36 + CRC32
        -> BT L2CAP -> controller DAC -> speaker -> air
        -> controller mic -> Opus 71B mono -> 0x31 type 0x02 -> decode -> FFT

Usage:
    python prototype/tools/loopback_test.py [--freq 1000] [--seconds 3] [--volume 120]
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ds5bridge import audio as A  # noqa: E402
from ds5bridge import device as D  # noqa: E402
from ds5bridge import protocol as P  # noqa: E402
from ds5bridge.pacing import Pacer, TimerResolution  # noqa: E402


def tone_frames(freq: float, seconds: float, amp: float = 0.6) -> list[bytes]:
    """Opus frames for a pure tone, generated at the 45 kHz trick rate."""
    n = int(seconds * A.AUDIO_SOURCE_RATE)
    t = np.arange(n) / A.AUDIO_SOURCE_RATE
    s = (np.sin(2 * np.pi * freq * t) * amp).astype(np.float32)
    return A.encode_opus_frames(np.stack([s, s], axis=1))


class Player(threading.Thread):
    """Pace 0x36 reports from a background thread while the main thread reads."""

    def __init__(self, dev: D.DualSense, frames: list[bytes], volume: int, target: str,
                 mic_active: bool = True, report: int = 36):
        super().__init__(daemon=True)
        self.dev, self.frames, self.volume, self.target = dev, frames, volume, target
        self.mic_active = mic_active
        self.report = report
        self.stop_flag = threading.Event()
        self.sent = 0
        self.errors = 0
        self.stats = ""

    def run(self) -> None:
        silent_h = A.silent_haptic_frame()
        per = 1 if self.report == 36 else 2
        pacer = Pacer(frame_ms=A.FRAME_MS * per, max_backlog=8, max_burst=4)
        fc = 0
        n = len(self.frames)

        def at(i: int) -> bytes:
            return self.frames[i] if i < n else A.silent_opus_frame()

        with TimerResolution(1):
            pacer.reset()
            while not self.stop_flag.is_set() and pacer.emitted * per < n:
                due = pacer.frames_due()
                if due == 0:
                    pacer.sleep_until_next()
                    continue
                for _ in range(due):
                    i = pacer.emitted * per
                    if i >= n:
                        break
                    try:
                        if self.report == 36:
                            self.dev.send_report_36(
                                at(i), silent_h, fc,
                                self.target, self.volume, mic_active=self.mic_active,
                            )
                        else:
                            self.dev.send_report_39(
                                (at(i), at(i + 1)), (silent_h, silent_h), fc,
                                self.target, mic_enabled=self.mic_active,
                            )
                        self.sent += 1
                    except Exception:  # noqa: BLE001
                        self.errors += 1
                    fc = (fc + per) & 0xFF
                    pacer.commit(1)
        self.stats = pacer.stats()


def capture(dev: D.DualSense, seconds: float) -> tuple[np.ndarray, int, int]:
    dec = A.MicDecoder()
    chunks: list[np.ndarray] = []
    n_audio = n_ctrl = 0
    end = time.perf_counter() + seconds
    while time.perf_counter() < end:
        raw = dev.read_raw(300)
        if not raw or raw[0] != P.BT_INPUT_31:
            continue
        pl = raw[1:]
        t = pl[0] & P.PAYLOAD_TYPE_MASK
        if t == P.PAYLOAD_TYPE_AUDIO:
            n_audio += 1
            pcm = dec.decode(P.get_mic_opus(pl))
            if pcm.size:
                chunks.append(pcm)
        elif t == P.PAYLOAD_TYPE_CONTROL:
            n_ctrl += 1
    pcm = np.concatenate(chunks) if chunks else np.zeros(0, np.float32)
    return pcm, n_audio, n_ctrl


def spectrum(pcm: np.ndarray, rate: int = A.MIC_RATE) -> tuple[np.ndarray, np.ndarray]:
    if pcm.size < 2048:
        return np.zeros(0), np.zeros(0)
    n = 1 << int(np.floor(np.log2(pcm.size)))
    x = pcm[:n] * np.hanning(n)
    mag = np.abs(np.fft.rfft(x)) / n
    freqs = np.fft.rfftfreq(n, 1.0 / rate)
    return freqs, mag


def band_energy(freqs: np.ndarray, mag: np.ndarray, f0: float, width: float = 60.0) -> float:
    if freqs.size == 0:
        return 0.0
    sel = (freqs >= f0 - width) & (freqs <= f0 + width)
    return float(np.sqrt(np.sum(mag[sel] ** 2)))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--freq", type=float, default=1000.0)
    ap.add_argument("--seconds", type=float, default=3.0)
    ap.add_argument("--volume", type=int, default=120)
    ap.add_argument("--target", choices=["speaker", "headphone"], default="speaker")
    ap.add_argument("--amp", type=float, default=0.6)
    ap.add_argument("--report", type=int, choices=[36, 39], default=36)
    ap.add_argument("--mic-off", action="store_true",
                    help="send 0x36 with p[68]=0xFE (the tester's value) to show it "
                         "tears down mic streaming")
    args = ap.parse_args()

    info = D.pick("BT")
    print(f"[dev] {D.describe(info)}")
    d = D.DualSense(info).open()

    print("[mic] arming (0x31 mic-state + 0x32 control)")
    d.send_mic_state(True)
    time.sleep(0.05)
    d.send_mic_control(True)
    time.sleep(0.4)  # let the stream spin up

    print(f"[base] capturing {args.seconds:.1f}s of SILENCE baseline")
    base, na, nc = capture(d, args.seconds)
    print(f"       {na} audio payloads, {nc} control payloads, "
          f"{base.size} samples ({base.size/A.MIC_RATE:.2f}s)")

    print(f"[tone] encoding {args.freq:.0f} Hz")
    frames = tone_frames(args.freq, args.seconds + 1.0, args.amp)
    d.send_setstate(
        P.SetState().speaker_volume(args.volume) if args.target == "speaker"
        else P.SetState().headphone_volume(args.volume)
    )
    time.sleep(0.05)

    player = Player(d, frames, args.volume, args.target, mic_active=not args.mic_off,
                    report=args.report)
    player.start()
    time.sleep(0.3)
    print(f"[tone] capturing {args.seconds:.1f}s WHILE playing")
    tone, na2, nc2 = capture(d, args.seconds)
    player.stop_flag.set()
    player.join(timeout=2.0)
    print(f"       {na2} audio payloads, {nc2} control payloads, "
          f"{tone.size} samples ({tone.size/A.MIC_RATE:.2f}s)")
    print(f"[tone] player: {player.sent} reports sent, {player.errors} errors; {player.stats}")

    # silence + disarm
    for _ in range(3):
        d.send_report_36(A.silent_opus_frame(), A.silent_haptic_frame(), 0,
                         args.target, args.volume)
        time.sleep(A.FRAME_MS / 1000)
    d.send_mic_control(False)
    time.sleep(0.02)
    d.send_mic_state(False)
    d.close()

    if base.size < 2048 or tone.size < 2048:
        print("\nRESULT: INCONCLUSIVE -- not enough mic audio captured")
        return 1

    fb, mb = spectrum(base)
    ft, mt = spectrum(tone)
    eb = band_energy(fb, mb, args.freq)
    et = band_energy(ft, mt, args.freq)
    ratio_db = 20 * np.log10(et / eb) if eb > 0 else float("inf")

    peak_t = float(ft[int(np.argmax(mt))]) if ft.size else 0.0
    peak_b = float(fb[int(np.argmax(mb))]) if fb.size else 0.0

    print("\n--- spectral comparison (mic capture) ---")
    print(f"  baseline: rms={np.sqrt(np.mean(base**2)):.5f} peak_bin={peak_b:7.1f} Hz "
          f"energy@{args.freq:.0f}Hz={eb:.6f}")
    print(f"  playing : rms={np.sqrt(np.mean(tone**2)):.5f} peak_bin={peak_t:7.1f} Hz "
          f"energy@{args.freq:.0f}Hz={et:.6f}")
    print(f"  gain at the played frequency: {ratio_db:+.1f} dB")

    near = abs(peak_t - args.freq) < 80
    if ratio_db > 12 and near:
        print("\nRESULT: PASS -- the played tone dominates the mic spectrum. "
              "Speaker output over BT 0x36 is confirmed on hardware.")
        return 0
    if ratio_db > 6:
        print("\nRESULT: WEAK PASS -- the tone band rose but does not dominate. "
              "Try --volume higher or a quieter room.")
        return 0
    print("\nRESULT: FAIL -- no tone detected in the mic capture.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
