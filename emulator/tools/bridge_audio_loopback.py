"""Phase 3b tests (c) + (d): audio out AND microphone in, at the same time,
through `BridgeBackend` only — closed-loop, no human ears, no room noise
sensitivity.

What this drives is exactly what the USB/IP layer drives: 1 ms slices of
4-channel/48 kHz/s16 into `write_audio_out()` and 1 ms slices out of
`read_audio_in()`, the same calls `ds5emu.device._iso_out` / `._iso_in` make
per isochronous packet. Everything between — the 45 kHz resample, the Opus
encoder, the 3 kHz haptic decimation, the 0x39 packing, the pacing, the mic
Opus decode and the jitter buffer — is the backend's.

    host-shaped 4ch frames
      ch0/ch1 = speaker tone -> 45 kHz resample -> Opus CBR 160k/10 ms
      ch2/ch3 = haptic tone  ->  3 kHz decimate -> int8 stereo
      -> report 0x39 (two frames, audio_buffer_length=48, mic flag 0x7F)
      -> controller speaker + voice coils -> AIR
      -> the same controller's microphone -> 71 B Opus -> 0x31 type 0x02
      -> BridgeBackend mic ring -> read_audio_in() -> FFT here

The test is **frequency-selective**: it only passes if energy rises at the
exact frequency commanded, so broadband room noise cannot fake it. That is the
same property that made the Phase-1 loopback the load-bearing evidence in this
repo, and the FFT helpers are imported from that tool rather than rewritten.

Because the capture runs *while* playback runs, a passing run is also the
proof of gotcha #1: the mic-active flag in report 0x39 (`pkt[4] = 0x7F`) is
being set correctly. With 0x7E the controller stops sending mic payloads the
moment playback starts and this test reports zero captured samples.

    cd emulator
    ..\\prototype\\.venv\\Scripts\\python.exe tools\\bridge_audio_loopback.py --mode both
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "emulator"))
sys.path.insert(0, str(ROOT / "prototype"))
sys.path.insert(0, str(ROOT / "prototype" / "tools"))

from ds5emu import descriptors as D          # noqa: E402
from ds5emu.bridge import BridgeBackend      # noqa: E402

from ds5bridge.pacing import Pacer, TimerResolution  # noqa: E402
import loopback_test as LB                   # noqa: E402  (spectrum/band_energy)

USB_RATE = D.AUDIO_SAMPLE_RATE               # 48000
OUT_FRAME = D.AUDIO_OUT_CHANNELS * D.AUDIO_BYTES_PER_SAMPLE   # 8
IN_FRAME = D.AUDIO_IN_CHANNELS * D.AUDIO_BYTES_PER_SAMPLE     # 4
SAMPLES_PER_MS = USB_RATE // 1000            # 48
OUT_BYTES_PER_MS = SAMPLES_PER_MS * OUT_FRAME                 # 384
IN_BYTES_PER_MS = SAMPLES_PER_MS * IN_FRAME                   # 192


def make_block(n0: int, count: int, spk_hz: float, hap_hz: float,
               spk_amp: float, hap_amp: float) -> bytes:
    """`count` frames of the 4-channel host stream starting at sample `n0`."""
    t = (np.arange(n0, n0 + count, dtype=np.float64)) / USB_RATE
    spk = np.sin(2 * np.pi * spk_hz * t) * spk_amp if spk_hz else np.zeros(count)
    hap = np.sin(2 * np.pi * hap_hz * t) * hap_amp if hap_hz else np.zeros(count)
    block = np.empty((count, 4), dtype=np.float64)
    block[:, 0] = spk
    block[:, 1] = spk
    block[:, 2] = hap
    block[:, 3] = hap
    return (np.clip(block, -1, 1) * 32767.0).astype("<i2").tobytes()


def run_capture(be: BridgeBackend, seconds: float, spk_hz: float, hap_hz: float,
                spk_amp: float, hap_amp: float, feed: bool) -> tuple[np.ndarray, dict]:
    """Drive one second-by-second pass at the real 1 ms isochronous cadence."""
    pacer = Pacer(frame_ms=1.0, max_backlog=16, max_burst=8)
    captured = bytearray()
    n = 0
    ticks = 0
    t_end = time.perf_counter() + seconds
    with TimerResolution(1):
        pacer.reset()
        while time.perf_counter() < t_end:
            due = pacer.frames_due()
            if due == 0:
                pacer.sleep_until_next()
                continue
            for _ in range(due):
                if feed:
                    be.write_audio_out(
                        make_block(n, SAMPLES_PER_MS, spk_hz, hap_hz, spk_amp, hap_amp)
                    )
                    n += SAMPLES_PER_MS
                captured += be.read_audio_in(IN_BYTES_PER_MS)
                ticks += 1
                pacer.commit(1)
    pcm = np.frombuffer(bytes(captured), dtype="<i2").reshape(-1, 2)[:, 0]
    return pcm.astype(np.float32) / 32768.0, {
        "ticks": ticks,
        "seconds": seconds,
        "pacer": pacer.stats(),
    }


def report_band(label: str, base_f, base_m, tone_f, tone_m, freq: float) -> float:
    eb = LB.band_energy(base_f, base_m, freq)
    et = LB.band_energy(tone_f, tone_m, freq)
    db = 20 * np.log10(et / eb) if eb > 0 else float("inf")
    print(f"  {label:<22} baseline {eb:.6f}  playing {et:.6f}  ->  {db:+.1f} dB")
    return db


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["both", "speaker", "haptics"], default="both")
    ap.add_argument("--speaker-freq", type=float, default=1500.0)
    ap.add_argument("--haptic-freq", type=float, default=250.0,
                    help="3 kHz is the actuators' native rate, so keep this < 1500")
    ap.add_argument("--speaker-amp", type=float, default=0.6)
    ap.add_argument("--haptic-amp", type=float, default=1.0)
    ap.add_argument("--seconds", type=float, default=3.0)
    ap.add_argument("--volume", type=int, default=140)
    args = ap.parse_args()

    spk_hz = args.speaker_freq if args.mode in ("both", "speaker") else 0.0
    hap_hz = args.haptic_freq if args.mode in ("both", "haptics") else 0.0

    be = BridgeBackend(speaker_volume=args.volume)
    be.start()

    # The host opening the capture endpoint is what arms the microphone.
    be.set_alt_setting(D.IFACE_AUDIO_IN, 1)
    time.sleep(0.5)  # let the BT mic stream spin up

    print(f"[base] {args.seconds:.1f}s of mic capture with NO playback")
    base, bi = run_capture(be, args.seconds, 0, 0, 0, 0, feed=False)
    print(f"       {base.size} samples ({base.size/USB_RATE:.2f}s), "
          f"{bi['ticks']} iso IN ticks, mic payloads {be.stats['bt_mic']}")

    mic_before = be.stats["bt_mic"]
    be.set_alt_setting(D.IFACE_AUDIO_OUT, 1)
    print(f"[tone] mode={args.mode} speaker={spk_hz:.0f}Hz haptics={hap_hz:.0f}Hz "
          f"volume={args.volume}; capturing WHILE playing")
    tone, ti = run_capture(be, args.seconds, spk_hz, hap_hz,
                           args.speaker_amp, args.haptic_amp, feed=True)
    mic_during = be.stats["bt_mic"] - mic_before
    print(f"       {tone.size} samples ({tone.size/USB_RATE:.2f}s), "
          f"{ti['ticks']} iso OUT+IN ticks, {mic_during} mic payloads DURING playback "
          f"({mic_during/args.seconds:.1f}/s)")
    print(f"       0x39 reports {be.stats['reports_39']} "
          f"({be.stats['reports_39']/args.seconds:.2f}/s, target 46.88), "
          f"{be.stats['report_39_errors']} write errors, "
          f"{be.stats['audio_underrun_frames']} underrun frames")
    print(f"       host fed {be.stats['audio_out_bytes']} B "
          f"({be.stats['audio_out_bytes']/OUT_BYTES_PER_MS/args.seconds:.1f} ms of "
          f"audio per second)")

    be.set_alt_setting(D.IFACE_AUDIO_OUT, 0)
    time.sleep(0.2)
    be.set_alt_setting(D.IFACE_AUDIO_IN, 0)
    print(f"\n  backend: {be.summary()}")
    be.stop()

    if base.size < 2048 or tone.size < 2048:
        print("\nRESULT: INCONCLUSIVE -- not enough microphone audio captured. "
              "If this happened only during playback, the 0x39 mic flag is wrong.")
        return 1

    bf, bm = LB.spectrum(base)
    tf, tm = LB.spectrum(tone)
    print("\n--- spectral comparison of the mic stream served on USB iso IN ---")
    print(f"  baseline rms {np.sqrt(np.mean(base**2)):.5f}   "
          f"playing rms {np.sqrt(np.mean(tone**2)):.5f}")
    # Always report BOTH bands, driven or not. The silent band is the channel-
    # mapping proof: in --mode haptics the Opus stream is literal digital
    # silence, so any energy at the speaker frequency would mean ch2/ch3 leaked
    # into the audio path, and vice versa.
    ok = True
    db_spk = report_band(
        f"ch0/1 band @{args.speaker_freq:.0f} Hz"
        + ("" if spk_hz else "  (NOT driven)"),
        bf, bm, tf, tm, args.speaker_freq)
    db_hap = report_band(
        f"ch2/3 band @{args.haptic_freq:.0f} Hz"
        + ("" if hap_hz else "  (NOT driven)"),
        bf, bm, tf, tm, args.haptic_freq)
    if spk_hz:
        ok &= db_spk > 12
    if hap_hz:
        ok &= db_hap > 10
    peak = float(tf[int(np.argmax(tm))]) if tf.size else 0.0
    print(f"  loudest bin while playing: {peak:.1f} Hz")

    full_duplex = mic_during > args.seconds * 50
    print(f"  full duplex: {mic_during} mic payloads arrived while playing "
          f"({'OK' if full_duplex else 'BROKEN -- check the 0x39 mic flag'})")

    print("\nRESULT:", "PASS" if (ok and full_duplex) else "FAIL")
    return 0 if (ok and full_duplex) else 1


if __name__ == "__main__":
    sys.exit(main())
