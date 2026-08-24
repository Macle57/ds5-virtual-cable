"""Independent acoustic proof: play out of the BT controller, listen on the WIRED one.

`loopback_test.py` captures the tone on the *same* controller that plays it, which
leaves one loophole open -- a self-capture could in principle be internal
electrical crosstalk rather than real sound in the room. This tool closes it: the
Bluetooth controller plays the tone through its speaker, and the capture comes from
the *other*, USB-connected controller's UAC1 microphone via WASAPI. Two physically
separate devices on two different transports, with only air in between.

Place the two controllers next to each other, then:

    python prototype/tools/cross_mic_test.py --freq 1000 --seconds 3
    python prototype/tools/cross_mic_test.py --list          # show input devices
    python prototype/tools/cross_mic_test.py --report 39 --target speaker
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np
import sounddevice as sd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from ds5bridge import audio as A  # noqa: E402
from ds5bridge import device as D  # noqa: E402
from ds5bridge import protocol as P  # noqa: E402

from loopback_test import Player, band_energy, spectrum, tone_frames  # noqa: E402


def find_dualsense_mic(prefer_hostapi: str = "Windows WASAPI") -> int:
    """Pick the wired DualSense mic endpoint, preferring the WASAPI host API."""
    cands = []
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] <= 0:
            continue
        if "dualsense" not in d["name"].lower():
            continue
        api = sd.query_hostapis(d["hostapi"])["name"]
        cands.append((api == prefer_hostapi, int(d["default_samplerate"]) == 48000, i, d, api))
    if not cands:
        raise RuntimeError(
            "No DualSense microphone input device found. Is the wired controller "
            "connected and its audio endpoint enabled?"
        )
    cands.sort(reverse=True)
    return cands[0][2]


def enable_wired_mic(vol: int = 0x40) -> str:
    """Unmute + power up the wired controller's mic over USB output report 0x02.

    A DualSense boots with its mic gated: without this the WASAPI endpoint is live
    but sits around -98 dBFS. Sending micVolume + audioControl + powerSaveMute=0x0F
    lifts it by roughly 36 dB, measured here.
    """
    try:
        info = D.pick("USB")
    except Exception as e:  # noqa: BLE001
        return f"no USB controller to unmute ({e})"
    d = D.DualSense(info).open()
    try:
        st = P.SetState()
        b = st.body
        b[P.VALID_FLAG0] = P.F0_MIC_VOLUME | P.F0_AUDIO_CONTROL
        b[P.VALID_FLAG1] = P.F1_MIC_MUTE_LED | P.F1_POWER_SAVE_MUTE | P.F1_AUDIO_CONTROL2
        b[P.MIC_VOLUME] = vol
        b[P.AUDIO_CONTROL] = 0x09
        b[P.MUTE_LED_CONTROL] = 0x00
        b[P.POWER_SAVE_MUTE_CONTROL] = 0x0F  # 0x1F would mute
        b[P.AUDIO_CONTROL2] = 0x01
        d.send_setstate(st)
        return f"wired mic enabled (micVolume=0x{vol:02X})"
    finally:
        d.close()


def record(dev_index: int, seconds: float, rate: int = 48000) -> np.ndarray:
    n = int(seconds * rate)
    buf = sd.rec(n, samplerate=rate, channels=1, dtype="float32", device=dev_index)
    sd.wait()
    return buf.reshape(-1)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--freq", type=float, default=1000.0)
    ap.add_argument("--seconds", type=float, default=3.0)
    ap.add_argument("--volume", type=int, default=140)
    ap.add_argument("--amp", type=float, default=0.6)
    ap.add_argument("--target", choices=["speaker", "headphone"], default="speaker")
    ap.add_argument("--report", type=int, choices=[36, 39], default=36)
    ap.add_argument("--device", type=int, default=None, help="input device index")
    ap.add_argument("--gate", type=float, default=1.0,
                    help="seconds per ON (and per OFF) window")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()

    if args.list:
        for i, d in enumerate(sd.query_devices()):
            if d["max_input_channels"] > 0:
                api = sd.query_hostapis(d["hostapi"])["name"]
                print(f"{i:3d}  {d['name']!r}  ch={d['max_input_channels']} "
                      f"sr={d['default_samplerate']:.0f} [{api}]")
        return 0

    mic_idx = args.device if args.device is not None else find_dualsense_mic()
    mic = sd.query_devices(mic_idx)
    api = sd.query_hostapis(mic["hostapi"])["name"]
    print(f"[listener] wired controller mic: [{mic_idx}] {mic['name']!r} "
          f"({api}, {mic['default_samplerate']:.0f} Hz)")

    print(f"[listener] {enable_wired_mic()}")
    time.sleep(0.4)

    info = D.pick("BT")
    print(f"[player]   bluetooth controller: {D.describe(info)}")
    d = D.DualSense(info).open()
    d.send_setstate(P.SetState().speaker_volume(args.volume) if args.target == "speaker"
                    else P.SetState().headphone_volume(args.volume))
    time.sleep(0.1)

    # A gated pattern inside ONE continuous recording. Recording "silence" and
    # "tone" as two separate takes is not good enough here: room noise drifts by
    # tens of dB between takes and swamps a speaker this small. Alternating
    # tone/silence every `gate` seconds and comparing the ON slices against the
    # OFF slices of the same capture cancels that drift out.
    gate = args.gate
    cycles = max(1, int(args.seconds / (2 * gate)))
    total = cycles * 2 * gate
    rate = 48000
    print(f"\n[gate] one continuous {total:.1f}s recording, {cycles} x "
          f"({gate:.1f}s ON / {gate:.1f}s OFF) at {args.freq:.0f} Hz "
          f"via report 0x{args.report}")

    frames = tone_frames(args.freq, gate + 0.4, args.amp)
    rec_buf = sd.rec(int(total * rate), samplerate=rate, channels=1,
                     dtype="float32", device=mic_idx)
    t0 = time.perf_counter()
    players = []
    for c in range(cycles):
        # ON window
        while time.perf_counter() - t0 < c * 2 * gate:
            time.sleep(0.001)
        p = Player(d, frames, args.volume, args.target, mic_active=False,
                   report=args.report)
        p.start()
        players.append(p)
        # OFF window
        while time.perf_counter() - t0 < c * 2 * gate + gate:
            time.sleep(0.001)
        p.stop_flag.set()
        silent_o, silent_h = A.silent_opus_frame(), A.silent_haptic_frame()
        for _ in range(3):
            if args.report == 36:
                d.send_report_36(silent_o, silent_h, 0, args.target, args.volume)
            else:
                d.send_report_39((silent_o, silent_o), (silent_h, silent_h), 0, args.target)
            time.sleep(A.FRAME_MS / 1000)
    sd.wait()
    for p in players:
        p.join(timeout=1.0)
    rec = rec_buf.reshape(-1)
    sent = sum(p.sent for p in players)
    errs = sum(p.errors for p in players)
    print(f"       recorded {rec.size} samples; {sent} reports sent, {errs} errors")

    # slice ON / OFF, trimming 150 ms at each edge for BT latency and decay
    trim = int(0.15 * rate)
    g = int(gate * rate)
    on_parts, off_parts = [], []
    for c in range(cycles):
        on_parts.append(rec[c * 2 * g + trim : c * 2 * g + g - trim])
        off_parts.append(rec[c * 2 * g + g + trim : (c + 1) * 2 * g - trim])
    tone = np.concatenate(on_parts)
    base = np.concatenate(off_parts)
    print(f"       ON  slices: {tone.size} samples rms={np.sqrt(np.mean(tone**2)):.6f}")
    print(f"       OFF slices: {base.size} samples rms={np.sqrt(np.mean(base**2)):.6f}")

    d.close()

    fb, mb = spectrum(base)
    ft, mt = spectrum(tone)
    if fb.size == 0 or ft.size == 0:
        print("\nRESULT: INCONCLUSIVE -- not enough audio recorded")
        return 1
    eb = band_energy(fb, mb, args.freq)
    et = band_energy(ft, mt, args.freq)
    ratio_db = 20 * np.log10(et / eb) if eb > 0 else float("inf")
    lo_t = ft > 150
    peak_t = float(ft[lo_t][int(np.argmax(mt[lo_t]))])

    print("\n--- spectral comparison (WIRED controller's microphone) ---")
    print(f"  baseline: energy@{args.freq:.0f}Hz={eb:.8f}")
    print(f"  playing : energy@{args.freq:.0f}Hz={et:.8f}  peak_bin={peak_t:.1f} Hz")
    print(f"  gain at the played frequency: {ratio_db:+.1f} dB")

    if ratio_db > 12 and abs(peak_t - args.freq) < 80:
        print("\nRESULT: PASS -- the second, physically separate controller hears the "
              "tone. Acoustic output from the BT controller's speaker is confirmed.")
        return 0
    if ratio_db > 6:
        print("\nRESULT: WEAK PASS -- tone band rose but does not dominate; "
              "move the controllers closer or raise --volume.")
        return 0
    print("\nRESULT: FAIL -- the wired controller did not hear the tone.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
