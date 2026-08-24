"""Phase 3c e2e stage (e): the concurrent soak.

Runs stage (a) and stage (c) AT THE SAME TIME for `--seconds`, which is the
thing neither Phase 3a nor Phase 3b ever did: HID input at 250 Hz, isochronous
OUT at 1000 packets/s and isochronous IN at 1000 packets/s, all through one
emulator, one TCP socket and one Bluetooth link, while a tone goes out to the
controller's speaker and haptics and comes back through its microphone.

What it records:

  * HID   delivered reports/s, inter-report gap median/p99/max, sequence breaks
  * audio the FFT band energy of what came back, per 10 s window, so a
          degradation partway through cannot hide inside a whole-run average
  * drift the mic ring depth over time -- the ONLY place in the system where
          two independent crystals meet (see ds5emu/bridge.py, domain C)
  * power the controller battery before and after

Everything is timeout-bounded and the whole run has an outer wall-clock cap.

    cd emulator
    ..\\prototype\\.venv\\Scripts\\python.exe tools\\e2e_soak.py --seconds 120
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ds5emu import _bootstrap  # noqa: F401,E402

import numpy as np                          # noqa: E402
import sounddevice as sd                    # noqa: E402
import hid                                  # noqa: E402
from ds5bridge import device as DEV         # noqa: E402
from ds5bridge import protocol as P         # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from e2e_audio import (SR, OUT_CH, band_power, db,            # noqa: E402
                       find_device, loudest_bin, make_tone)

O = P.OFFSETS_USB


class HidSoak(threading.Thread):
    """Stage (a) running concurrently: poll the virtual device at 250 Hz."""

    def __init__(self, path: bytes, stop: threading.Event, hz: float = 250.0):
        super().__init__(name="hid-soak", daemon=True)
        self.path, self.stop, self.period = path, stop, 1.0 / hz
        self.reports = self.empty = self.errors = self.seq_breaks = 0
        self.gaps: list[float] = []
        self.battery: str = "unknown"
        self.error_text = ""

    def run(self) -> None:
        try:
            h = hid.device()
            h.open_path(self.path)
        except Exception as e:  # noqa: BLE001
            self.error_text = f"open failed: {e}"
            return
        prev_seq = None
        last = 0.0
        nxt = time.perf_counter()
        try:
            while not self.stop.is_set():
                now = time.perf_counter()
                if now < nxt:
                    time.sleep(min(self.period, nxt - now))
                    continue
                nxt = max(now, nxt + self.period)
                try:
                    data = h.read(P.USB_INPUT_01_LEN, timeout_ms=50)
                except Exception as e:  # noqa: BLE001
                    self.errors += 1
                    self.error_text = str(e)
                    if self.errors > 500:
                        return
                    continue
                if not data:
                    self.empty += 1
                    continue
                b = bytes(data)
                if b[0] != P.USB_INPUT_01:
                    continue
                t = time.perf_counter()
                self.reports += 1
                if last:
                    self.gaps.append((t - last) * 1e3)
                last = t
                seq = b[1 + O.sequence_num]
                if prev_seq is not None and (prev_seq + 1) & 0xFF != seq:
                    self.seq_breaks += 1
                prev_seq = seq
                st = P.decode_input(b[1:], usb=True)
                if st:
                    self.battery = f"{st.battery_level * 10}%/{st.battery_state}"
        finally:
            try:
                h.close()
            except Exception:  # noqa: BLE001
                pass


def find_virtual_hid(substr: str) -> bytes:
    for d in DEV.enumerate_devices():
        if d.transport == "USB" and substr.lower() in d.path_str.lower():
            return d.path
    raise SystemExit(f"FAIL: no virtual HID matching {substr!r}")


def read_seam(path: str) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seconds", type=float, default=120.0)
    ap.add_argument("--render-name", default="Speakers (3- DualSense")
    ap.add_argument("--capture-name", default="Headset Microphone (3- DualSense")
    ap.add_argument("--virtual-path-contains", default="4&127b94db")
    ap.add_argument("--spk-hz", type=float, default=1500.0)
    ap.add_argument("--hap-hz", type=float, default=250.0)
    ap.add_argument("--amp", type=float, default=0.5)
    ap.add_argument("--window", type=float, default=10.0)
    ap.add_argument("--stats-json", default=r"C:\Temp\ds5c_stats.json")
    ap.add_argument("--min-hz", type=float, default=240.0)
    ap.add_argument("--min-db", type=float, default=10.0)
    args = ap.parse_args(argv)

    rdev = find_device(args.render_name, want_input=False)
    cdev = find_device(args.capture_name, want_input=True)
    hpath = find_virtual_hid(args.virtual_path_contains)
    print(f"render  [{rdev}] {sd.query_devices(rdev)['name']}")
    print(f"capture [{cdev}] {sd.query_devices(cdev)['name']}  (EXCLUSIVE)")
    print(f"hid     {hpath.decode(errors='replace')}")
    print(f"soak    {args.seconds:.0f} s, HID + audio out + mic in concurrently\n")

    seam0 = read_seam(args.stats_json)

    # A tone long enough for the whole soak, looped by sounddevice.
    tone = make_tone(min(args.seconds + 5.0, 130.0), args.spk_hz, args.hap_hz,
                     "both", args.amp)

    stop = threading.Event()
    hidsoak = HidSoak(hpath, stop)

    windows: list[tuple[float, float, float, float]] = []   # t, spk_dB, hap_dB, rms
    cap_blocks: list[np.ndarray] = []
    overflows = 0

    def cb(indata, frames, t, status):
        nonlocal overflows
        if status and status.input_overflow:
            overflows += 1
        cap_blocks.append(indata.copy())

    stream = sd.InputStream(device=cdev, channels=2, samplerate=SR,
                            dtype="int16", blocksize=0, latency="low",
                            extra_settings=sd.WasapiSettings(exclusive=True),
                            callback=cb)
    t0 = time.perf_counter()
    outer_deadline = t0 + args.seconds + 45.0
    base = np.zeros(0, np.float32)
    try:
        stream.start()
        time.sleep(1.2)                       # let the mic arm and prime
        cap_blocks.clear()
        time.sleep(2.0)                       # silent baseline
        base = (np.concatenate(cap_blocks, axis=0).astype(np.float32) / 32768.0
                ).mean(axis=1) if cap_blocks else np.zeros(0, np.float32)
        cap_blocks.clear()

        hidsoak.start()
        sd.play(tone, samplerate=SR, device=rdev, loop=True, blocking=False)
        t_start = time.perf_counter()
        t_end = t_start + args.seconds
        next_win = t_start + args.window
        while time.perf_counter() < t_end and time.perf_counter() < outer_deadline:
            time.sleep(0.2)
            if time.perf_counter() >= next_win:
                next_win += args.window
                chunk = (np.concatenate(cap_blocks, axis=0).astype(np.float32) / 32768.0
                         ).mean(axis=1) if cap_blocks else np.zeros(0, np.float32)
                cap_blocks.clear()
                if chunk.size >= 4096:
                    s = db(band_power(chunk, args.spk_hz), band_power(base, args.spk_hz))
                    h = db(band_power(chunk, args.hap_hz), band_power(base, args.hap_hz))
                    rms = float(np.sqrt((chunk ** 2).mean()))
                    el = time.perf_counter() - t_start
                    windows.append((el, s, h, rms))
                    print(f"  t={el:6.1f}s  spk {s:+6.1f} dB  hap {h:+6.1f} dB  "
                          f"rms {rms:.5f}  hid {hidsoak.reports} "
                          f"({hidsoak.reports/el:.1f}/s)", flush=True)
    finally:
        stop.set()
        try:
            sd.stop()
        except Exception:  # noqa: BLE001
            pass
        try:
            stream.stop(); stream.close()
        except Exception:  # noqa: BLE001
            pass
        hidsoak.join(timeout=5.0)

    span = time.perf_counter() - t0
    seam1 = read_seam(args.stats_json)

    print(f"\n=== stage (e) SOAK — {span:.1f} s wall clock ===")
    if hidsoak.error_text:
        print(f"  hid error: {hidsoak.error_text}")
    g = sorted(hidsoak.gaps)
    elapsed = max(1e-9, args.seconds)
    print(f"  HID reports        {hidsoak.reports} = {hidsoak.reports/elapsed:.2f}/s"
          f"   empty {hidsoak.empty}  errors {hidsoak.errors}")
    if len(g) > 10:
        print(f"  HID gap ms         median {statistics.median(g):.3f}  "
              f"p99 {g[int(len(g)*0.99)]:.3f}  max {g[-1]:.3f}")
    print(f"  HID seq breaks     {hidsoak.seq_breaks}")
    print(f"  battery (last)     {hidsoak.battery}")
    print(f"  capture overflows  {overflows}")
    if windows:
        spk = [w[1] for w in windows]
        hap = [w[2] for w in windows]
        print(f"  windows            {len(windows)} x {args.window:.0f} s")
        print(f"  speaker band dB    min {min(spk):+.1f}  median "
              f"{statistics.median(spk):+.1f}  max {max(spk):+.1f}")
        print(f"  haptic  band dB    min {min(hap):+.1f}  median "
              f"{statistics.median(hap):+.1f}  max {max(hap):+.1f}")

    def delta(k, sub="clock_seam"):
        a = (seam0.get(sub) or {}).get(k, 0)
        b = (seam1.get(sub) or {}).get(k, 0)
        return (b or 0) - (a or 0)

    if seam1.get("clock_seam"):
        cs = seam1["clock_seam"]
        print("\n  --- clock seam over the soak (delta) ---")
        print(f"  0x39 reports       {delta('reports_39')}  "
              f"(errors {delta('report_39_errors')})")
        print(f"  U->B underrun frm  {delta('audio_underrun_frames')}   "
              f"queue drops {delta('audio_q_drop_frames')}")
        print(f"  out-ring peak      {cs['out_ring_peak_ms']} ms of "
              f"{cs['out_ring_cap_ms']} ms cap   overflow "
              f"{delta('out_ring_overflow_bytes')} B")
        print(f"  mic payloads       {delta('mic_payloads')}  "
              f"(decode errors {delta('mic_decode_errors')})")
        print(f"  mic depth ms       mean {cs['mic_depth_ms_mean']}  "
              f"[{cs['mic_depth_ms_min']}..{cs['mic_depth_ms_max']}]  "
              f"target {cs['mic_depth_target_ms']}  band {cs['mic_depth_band_ms']}")
        print(f"  C->U skew          pad {delta('mic_skew_pad_frames')} frames  "
              f"drop {delta('mic_skew_drop_frames')} frames")
        print(f"  mic underruns      {delta('mic_underrun_calls')}  "
              f"reprimes {delta('mic_reprimes')}  "
              f"ring overflow {delta('mic_ring_overflow_bytes')} B")
        print(f"  control jobs       {delta('control_jobs')}  "
              f"(dropped {delta('control_jobs_dropped')})")
    # The meter is keyed by the RAW endpoint number off the wire, so the
    # isochronous IN endpoint (0x82 in the descriptor) appears as "0x02".
    for ep in ("0x01", "0x02"):
        a = ((seam0.get("endpoints") or {}).get(ep) or {}).get("resyncs", 0)
        b = ((seam1.get("endpoints") or {}).get(ep) or {}).get("resyncs", 0)
        e1 = ((seam1.get("endpoints") or {}).get(ep) or {})
        print(f"  iso {ep} resyncs   {b - a} during the soak "
              f"(packets/s {e1.get('packets_per_s', 0):.1f})")

    hz = hidsoak.reports / elapsed
    med_spk = statistics.median([w[1] for w in windows]) if windows else -99.0
    med_hap = statistics.median([w[2] for w in windows]) if windows else -99.0
    ok = (hz >= args.min_hz and hidsoak.seq_breaks == 0
          and med_spk >= args.min_db and med_hap >= args.min_db
          and delta("report_39_errors") == 0
          and delta("mic_decode_errors") == 0)
    print(f"\nRESULT: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
