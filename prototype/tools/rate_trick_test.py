"""Measure the controller's real audio consumption rate -- i.e. prove the 45 kHz trick.

FINDINGS.md claims the controller consumes the Opus stream at ~45 kHz even though
the frames are encoded as nominal 48 kHz, so the host must resample the source to
45 kHz to get the right pitch. That claim is testable without ears:

  * WITH the trick: build the tone at 45000 Hz, encode as 48000 -> should come back
    out of the speaker at the intended frequency f.
  * WITHOUT the trick: build the tone at 48000 Hz, encode as 48000 -> should come
    back SHIFTED DOWN by 45/48, i.e. at 0.9375 * f.

We capture both through the controller's own microphone and read off the peak bin.
The ratio between the two measured peaks is the controller's consumption rate
divided by 48000.

    python prototype/tools/rate_trick_test.py [--freq 1200] [--seconds 3]
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ds5bridge import audio as A  # noqa: E402
from ds5bridge import device as D  # noqa: E402
from ds5bridge import protocol as P  # noqa: E402

from loopback_test import Player, capture, spectrum  # noqa: E402


def tone_at(rate: int, freq: float, seconds: float, amp: float) -> list[bytes]:
    """Generate the tone sampled at `rate`, then hand it to the 48 kHz encoder."""
    n = int(seconds * rate)
    t = np.arange(n) / rate
    s = (np.sin(2 * np.pi * freq * t) * amp).astype(np.float32)
    return A.encode_opus_frames(np.stack([s, s], axis=1))


def measure(d, frames, volume, target, seconds) -> tuple[float, np.ndarray]:
    player = Player(d, frames, volume, target, mic_active=True)
    player.start()
    time.sleep(0.35)
    pcm, _, _ = capture(d, seconds)
    player.stop_flag.set()
    player.join(timeout=2.0)
    for _ in range(3):
        d.send_report_36(A.silent_opus_frame(), A.silent_haptic_frame(), 0,
                         target, volume, mic_active=True)
        time.sleep(A.FRAME_MS / 1000)
    f, m = spectrum(pcm)
    if f.size == 0:
        return 0.0, pcm
    # ignore DC / very low rumble
    lo = f > 150
    peak = float(f[lo][int(np.argmax(m[lo]))])
    return peak, pcm


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--freq", type=float, default=1200.0)
    ap.add_argument("--seconds", type=float, default=3.0)
    ap.add_argument("--volume", type=int, default=140)
    ap.add_argument("--amp", type=float, default=0.6)
    ap.add_argument("--target", choices=["speaker", "headphone"], default="speaker")
    args = ap.parse_args()

    info = D.pick("BT")
    print(f"[dev] {D.describe(info)}")
    d = D.DualSense(info).open()
    d.send_mic_state(True)
    time.sleep(0.05)
    d.send_mic_control(True)
    time.sleep(0.4)
    d.send_setstate(P.SetState().speaker_volume(args.volume) if args.target == "speaker"
                    else P.SetState().headphone_volume(args.volume))
    time.sleep(0.05)

    dur = args.seconds + 1.5
    print(f"\n[A] WITH the 45k trick: tone generated at {A.AUDIO_SOURCE_RATE} Hz, "
          f"encoded as {A.OPUS_NOMINAL_RATE} Hz")
    peak_a, _ = measure(d, tone_at(A.AUDIO_SOURCE_RATE, args.freq, dur, args.amp),
                        args.volume, args.target, args.seconds)
    print(f"    intended {args.freq:.1f} Hz -> mic peak {peak_a:.1f} Hz "
          f"(error {peak_a - args.freq:+.1f} Hz)")

    time.sleep(0.5)

    print(f"\n[B] WITHOUT the trick: tone generated at {A.OPUS_NOMINAL_RATE} Hz, "
          f"encoded as {A.OPUS_NOMINAL_RATE} Hz")
    peak_b, _ = measure(d, tone_at(A.OPUS_NOMINAL_RATE, args.freq, dur, args.amp),
                        args.volume, args.target, args.seconds)
    print(f"    intended {args.freq:.1f} Hz -> mic peak {peak_b:.1f} Hz "
          f"(error {peak_b - args.freq:+.1f} Hz)")

    d.send_mic_control(False)
    time.sleep(0.02)
    d.send_mic_state(False)
    d.close()

    print("\n--- result ---")
    if peak_a <= 0 or peak_b <= 0:
        print("could not measure both peaks")
        return 1
    implied = A.OPUS_NOMINAL_RATE * peak_b / args.freq
    print(f"  A (trick on)  peak {peak_a:8.1f} Hz  -> ratio to intended {peak_a/args.freq:.4f}")
    print(f"  B (trick off) peak {peak_b:8.1f} Hz  -> ratio to intended {peak_b/args.freq:.4f}")
    print(f"  implied controller consumption rate = 48000 * {peak_b/args.freq:.4f} "
          f"= {implied:.0f} Hz")
    print(f"  (FINDINGS.md predicts ~45000 Hz, i.e. ratio 0.9375 in case B and 1.0 in case A)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
