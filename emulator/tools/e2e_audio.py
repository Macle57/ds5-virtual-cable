"""Phase 3c e2e stages (c) and (d): audio, both directions, through Windows.

This closes the entire loop with nothing but Windows audio APIs at each end:

    tone -> VIRTUAL render endpoint (WASAPI, 4 ch)      <- stage (c) starts here
      -> usbip2_ude -> TCP -> ds5emu iso OUT 0x01
      -> BridgeBackend: 48k->45k resample, Opus, 48k->3k haptic decimate
      -> BT report 0x39 -> controller
      -> ch0/1 come out of the SPEAKER, ch2/3 shake the HAPTIC voice coils
      -> ((( acoustically through the air / the controller's own body )))
      -> the controller's MICROPHONE picks it up                <- stage (d)
      -> BT 0x31 audio payload -> BridgeBackend Opus decode -> mic ring
      -> ds5emu iso IN 0x82 -> usbip2_ude
      -> VIRTUAL capture endpoint (WASAPI EXCLUSIVE) -> FFT here

Judged by FFT of what Windows captured, against a silent baseline recorded
moments earlier through the same path. The test is frequency-selective -- it
only passes if energy rises at the exact frequency commanded -- so room noise
cannot fake it.

CHANNEL MAPPING IS PROVEN, NOT ASSUMED. `--mode speaker` drives only ch0/1 and
`--mode haptics` only ch2/3; each must raise its own band and leave the other
alone. In haptics mode the Opus stream carries literal digital silence, so a
250 Hz peak can only be the voice coils.

WASAPI **EXCLUSIVE** mode on the capture side is not optional: Windows' shared
mode runs an enhancement chain that gates a steady tone to digital silence
within ~250 ms, and the *physical* controller shows the same behaviour -- it is
Windows, not us (docs/e1-results.md §7.2).

    cd emulator
    ..\\prototype\\.venv\\Scripts\\python.exe tools\\e2e_audio.py --mode both
"""

from __future__ import annotations

import argparse
import sys
import time

import numpy as np
import sounddevice as sd

SR = 48000
OUT_CH = 4          # [spkL, spkR, hapL, hapR]
IN_CH = 2


def find_device(name_substr: str, want_input: bool) -> int:
    """WASAPI device index whose name contains `name_substr`.

    Matched on the friendly name that `tools/e2e_endpoints.ps1` derived from the
    devnode tree. NEVER guess from the "2-"/"3-" prefix yourself: it moves
    between runs (STATUS.md §15.5 trap 2), which is exactly why the PowerShell
    walk exists.
    """
    hits = []
    for i, d in enumerate(sd.query_devices()):
        if sd.query_hostapis(d["hostapi"])["name"] != "Windows WASAPI":
            continue
        if name_substr.lower() not in d["name"].lower():
            continue
        if want_input and d["max_input_channels"] >= IN_CH:
            hits.append(i)
        if not want_input and d["max_output_channels"] >= OUT_CH:
            hits.append(i)
    if not hits:
        raise SystemExit(f"FAIL: no WASAPI {'input' if want_input else 'output'} "
                         f"device matching {name_substr!r}")
    return hits[0]


def band_power(x: np.ndarray, hz: float, half_width: float = 25.0) -> float:
    """Total power within +/-half_width Hz of `hz`."""
    if x.size < 4096:
        return 0.0
    w = np.hanning(x.size)
    spec = np.abs(np.fft.rfft(x * w)) ** 2
    freqs = np.fft.rfftfreq(x.size, 1.0 / SR)
    sel = (freqs >= hz - half_width) & (freqs <= hz + half_width)
    return float(spec[sel].sum())


def db(a: float, b: float) -> float:
    if b <= 0 or a <= 0:
        return float("nan")
    return 10.0 * np.log10(a / b)


def loudest_bin(x: np.ndarray, lo: float = 60.0, hi: float = 8000.0) -> float:
    w = np.hanning(x.size)
    spec = np.abs(np.fft.rfft(x * w))
    freqs = np.fft.rfftfreq(x.size, 1.0 / SR)
    sel = (freqs >= lo) & (freqs <= hi)
    return float(freqs[sel][int(np.argmax(spec[sel]))])


class Capture:
    """WASAPI exclusive-mode capture into a growing list of blocks."""

    def __init__(self, device: int, exclusive: bool = True):
        self.blocks: list[np.ndarray] = []
        self.overflows = 0
        extra = sd.WasapiSettings(exclusive=True) if exclusive else None
        self.stream = sd.InputStream(
            device=device, channels=IN_CH, samplerate=SR, dtype="int16",
            blocksize=0, latency="low", extra_settings=extra,
            callback=self._cb,
        )

    def _cb(self, indata, frames, t, status):
        if status and status.input_overflow:
            self.overflows += 1
        self.blocks.append(indata.copy())

    def __enter__(self):
        self.stream.start()
        return self

    def __exit__(self, *exc):
        try:
            self.stream.stop()
            self.stream.close()
        except Exception:  # noqa: BLE001
            pass

    def take(self) -> np.ndarray:
        """Everything captured so far, as float32 mono, and reset."""
        if not self.blocks:
            return np.zeros(0, np.float32)
        x = np.concatenate(self.blocks, axis=0).astype(np.float32) / 32768.0
        self.blocks = []
        return x.mean(axis=1)


def make_tone(seconds: float, spk_hz: float, hap_hz: float,
              mode: str, amp: float) -> np.ndarray:
    n = int(seconds * SR)
    t = np.arange(n, dtype=np.float32) / SR
    out = np.zeros((n, OUT_CH), np.float32)
    if mode in ("both", "speaker"):
        s = amp * np.sin(2 * np.pi * spk_hz * t)
        out[:, 0] = s
        out[:, 1] = s
    if mode in ("both", "haptics"):
        h = amp * np.sin(2 * np.pi * hap_hz * t)
        out[:, 2] = h
        out[:, 3] = h
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    # MACHINE-SPECIFIC defaults: the "2-"/"3-" prefix Windows puts in an audio
    # endpoint's friendly name is assigned at enumeration time and moves between
    # runs. Run tools/e2e_endpoints.ps1 to print RENDER=/CAPTURE= for your machine.
    ap.add_argument("--render-name", default="Speakers (3- DualSense",
                    help="substring of the virtual render endpoint's friendly name "
                         "(machine-specific; see tools/e2e_endpoints.ps1)")
    ap.add_argument("--capture-name", default="Headset Microphone (3- DualSense",
                    help="substring of the virtual capture endpoint's friendly name "
                         "(machine-specific; see tools/e2e_endpoints.ps1)")
    ap.add_argument("--mode", choices=("both", "speaker", "haptics"), default="both")
    ap.add_argument("--seconds", type=float, default=3.0)
    ap.add_argument("--spk-hz", type=float, default=1500.0)
    ap.add_argument("--hap-hz", type=float, default=250.0)
    ap.add_argument("--amp", type=float, default=0.5)
    ap.add_argument("--min-db", type=float, default=10.0,
                    help="minimum rise in the driven band to call it a PASS")
    ap.add_argument("--shared-capture", action="store_true",
                    help="DIAGNOSTIC ONLY: capture in shared mode, which gates a "
                         "steady tone to digital silence in ~250 ms")
    args = ap.parse_args(argv)

    rdev = find_device(args.render_name, want_input=False)
    cdev = find_device(args.capture_name, want_input=True)
    print(f"render  [{rdev}] {sd.query_devices(rdev)['name']}")
    print(f"capture [{cdev}] {sd.query_devices(cdev)['name']}"
          f"  ({'SHARED' if args.shared_capture else 'EXCLUSIVE'})")
    print(f"mode    {args.mode}   speaker {args.spk_hz:.0f} Hz on ch0/1, "
          f"haptics {args.hap_hz:.0f} Hz on ch2/3   amp {args.amp}")
    print()

    tone = make_tone(args.seconds, args.spk_hz, args.hap_hz, args.mode, args.amp)

    with Capture(cdev, exclusive=not args.shared_capture) as cap:
        # Opening the capture stream is what makes Windows SET_INTERFACE alt 1
        # on the AudioStreaming IN interface, which is what arms the Bluetooth
        # microphone. Give the arming (two writes, 20 ms apart, on the writer
        # thread) and the jitter buffer time to settle.
        time.sleep(1.2)
        cap.take()

        print(f"  baseline: {args.seconds:.1f} s, nothing playing ...", flush=True)
        time.sleep(args.seconds)
        base = cap.take()

        print(f"  playing:  {args.seconds:.1f} s ...", flush=True)
        sd.play(tone, samplerate=SR, device=rdev, blocking=False)
        t_end = time.perf_counter() + args.seconds + 0.5
        while time.perf_counter() < t_end:
            time.sleep(0.05)
        sd.stop()
        live = cap.take()

    print()
    print(f"  baseline {base.size} samples ({base.size/SR:.2f} s), "
          f"live {live.size} samples ({live.size/SR:.2f} s), "
          f"overflows {cap.overflows}")
    if base.size < SR // 2 or live.size < SR // 2:
        print("\nRESULT: FAIL — the virtual capture endpoint returned almost "
              "nothing. The microphone path is not delivering.")
        return 1

    spk_d = db(band_power(live, args.spk_hz), band_power(base, args.spk_hz))
    hap_d = db(band_power(live, args.hap_hz), band_power(base, args.hap_hz))
    print(f"  ch0/1 speaker band @{args.spk_hz:.0f} Hz : {spk_d:+.1f} dB")
    print(f"  ch2/3 haptic  band @{args.hap_hz:.0f} Hz : {hap_d:+.1f} dB")
    print(f"  loudest bin while playing        : {loudest_bin(live):.1f} Hz")
    print(f"  captured rms  baseline {np.sqrt((base**2).mean()):.6f}  "
          f"live {np.sqrt((live**2).mean()):.6f}")

    if args.mode == "both":
        ok = spk_d >= args.min_db and hap_d >= args.min_db
    elif args.mode == "speaker":
        ok = spk_d >= args.min_db
    else:
        ok = hap_d >= args.min_db
    print(f"\nRESULT: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
