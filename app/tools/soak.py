"""A long soak of the packaged product, with battery and rate logged as it goes.

STATUS.md 17.6(3): "the longest continuous run was 120 s. Thermal behaviour,
long-run drift and battery sag are unmeasured." This runs the thing a person
actually launches -- `BridgeService`, the same object `ds5bridge` and the tray
use -- for as long as you ask, with a 250 Hz reader on the virtual device
standing in for a game, and prints a line a minute.

WHY NO AUDIO BY DEFAULT. The Phase 3c 120 s full-duplex soak cost 10 % of
battery in about four minutes (`docs/e2e-results.md` 2(e)) -- roughly 2.5 %/min
with both actuators driven continuously. A 30-minute audio soak is therefore
not something a battery can do at all; it would end as a dead-controller test.
HID-only is what a long session looks like between cutscenes, and it is the
regime where drift, thermal effects and battery sag are actually measurable.
`--audio` is there for a short full-duplex run on a charged unit.

    prototype\\.venv\\Scripts\\python.exe app\\tools\\soak.py ^
        --serial d42f4ba1485d --minutes 30 --jsonl C:\\Temp\\ds5-soak.jsonl

Every sample is appended to the JSONL as it is taken, so a crash 28 minutes in
still leaves 28 minutes of data.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import threading
import time
from collections import deque
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
REPO = APP_DIR.parent
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(REPO / "emulator"))

from ds5app import service as S            # noqa: E402
from ds5app.usbip import Usbip             # noqa: E402

import hid                                 # noqa: E402
from ds5bridge import protocol as P        # noqa: E402


def say(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def usb_hid_paths() -> set[str]:
    out = set()
    for d in hid.enumerate(0x054C, 0x0CE6):
        p = d["path"].decode("latin1") if isinstance(d["path"], bytes) else d["path"]
        if "00001124" not in p:
            out.add(p)
    return out


class GameReader(threading.Thread):
    """250 Hz reader on the virtual device, keeping only running statistics.

    Deliberately NOT keeping every sample: a 30-minute run is 450 000 reports,
    and a growing structure that gets summarised periodically is exactly the
    instrumentation trap STATUS 17.8 trap 10 describes. Gaps go into a bounded
    deque; everything else is a counter.
    """

    def __init__(self, path: str):
        super().__init__(name="soak-reader", daemon=True)
        self.path = path
        self.stop_flag = threading.Event()
        self.lock = threading.Lock()
        self.reports = 0
        self.empty = 0
        self.errors = 0
        self.seq_breaks = 0
        self.fatal: str | None = None
        self.gaps: deque = deque(maxlen=20_000)   # bounded on purpose
        self._last_t: float | None = None
        self._last_seq: int | None = None

    def run(self) -> None:
        d = hid.device()
        try:
            d.open_path(self.path.encode("latin1")
                        if isinstance(self.path, str) else self.path)
        except Exception as e:              # noqa: BLE001
            self.fatal = f"open failed: {e}"
            return
        try:
            while not self.stop_flag.is_set():
                try:
                    raw = bytes(d.read(64, timeout_ms=50))
                except Exception as e:      # noqa: BLE001
                    self.errors += 1
                    if self.errors > 500:
                        self.fatal = f"read failed {self.errors}x: {e}"
                        return
                    continue
                if not raw:
                    self.empty += 1
                    continue
                now = time.perf_counter()
                seq = raw[1 + P.OFFSETS_USB.sequence_num]
                with self.lock:
                    self.reports += 1
                    if self._last_t is not None:
                        self.gaps.append(now - self._last_t)
                    if self._last_seq is not None and \
                            seq != (self._last_seq + 1) & 0xFF:
                        self.seq_breaks += 1
                    self._last_t = now
                    self._last_seq = seq
        finally:
            try:
                d.close()
            except Exception:               # noqa: BLE001
                pass

    def take(self) -> dict:
        with self.lock:
            gaps = list(self.gaps)
            self.gaps.clear()
            out = {"reports": self.reports, "empty": self.empty,
                   "errors": self.errors, "seq_breaks": self.seq_breaks}
        if len(gaps) > 2:
            g = sorted(gaps)
            out.update(gap_ms_median=round(statistics.median(g) * 1e3, 3),
                       gap_ms_p99=round(g[int(len(g) * 0.99)] * 1e3, 3),
                       gap_ms_max=round(g[-1] * 1e3, 3))
        return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--serial", default=None)
    ap.add_argument("--port", type=int, default=3241)
    ap.add_argument("--minutes", type=float, default=30.0)
    ap.add_argument("--every", type=float, default=60.0, help="sample interval (s)")
    ap.add_argument("--jsonl", default=None, help="append every sample here")
    ap.add_argument("--audio", action="store_true",
                    help="also stream a tone to the virtual render endpoint. "
                         "Costs ~2.5 %%/min of battery; see the module docstring.")
    a = ap.parse_args()

    u = Usbip(port=a.port)
    S.cleanup(a.port, u.exe, log_fn=lambda t: print("    " + t, flush=True))

    before = usb_hid_paths()
    svc = S.BridgeService(serial=a.serial, port=a.port,
                          on_event=lambda k, t: say(f"  {k}: {t}"))
    reader = None
    fh = open(a.jsonl, "a", encoding="utf-8") if a.jsonl else None
    worst = {"gap_ms_max": 0.0}
    battery_first = battery_last = None
    ok = True
    try:
        svc.start()
        time.sleep(2.0)
        new = usb_hid_paths() - before
        if not new:
            print("FAIL: the virtual HID device did not appear")
            return 1
        reader = GameReader(sorted(new)[0])
        reader.start()

        backend = svc._backend
        t0 = time.perf_counter()
        end = t0 + a.minutes * 60
        prev_reports = 0
        prev_t = time.perf_counter()
        n = 0
        say(f"soaking for {a.minutes:g} minutes; a line every {a.every:g}s")
        print(f"{'min':>6} {'reports/s':>10} {'gap med':>8} {'gap p99':>8} "
              f"{'gap max':>8} {'bat':>5} {'bt/s':>7} {'rpt':>6} {'neut':>6} "
              f"{'disc':>5} {'recon':>6} {'errs':>5}", flush=True)
        while time.perf_counter() < end:
            time.sleep(min(a.every, max(0.0, end - time.perf_counter())))
            n += 1
            t = time.perf_counter() - t0
            r = reader.take()
            st = backend.device_status()
            bs = dict(backend.stats)
            # Against the ACTUAL elapsed time, not the nominal interval: the
            # final sample is a short one and would otherwise read as a 50 %
            # rate collapse in the last row of every run.
            now_t = time.perf_counter()
            rate = (r["reports"] - prev_reports) / max(1e-6, now_t - prev_t)
            prev_reports, prev_t = r["reports"], now_t
            if battery_first is None:
                battery_first = st["battery_percent"]
            battery_last = st["battery_percent"]
            worst["gap_ms_max"] = max(worst["gap_ms_max"], r.get("gap_ms_max", 0.0))
            print(f"{t/60:6.1f} {rate:10.1f} {r.get('gap_ms_median', 0):8.3f} "
                  f"{r.get('gap_ms_p99', 0):8.3f} {r.get('gap_ms_max', 0):8.3f} "
                  f"{str(st['battery_percent']) + '%':>5} "
                  f"{bs['bt_reports'] / t:7.1f} {bs['input_repeated']:6d} "
                  f"{bs['input_neutral']:6d} {bs['disconnects']:5d} "
                  f"{bs['reconnects']:6d} {bs['bt_read_errors']:5d}", flush=True)
            if fh:
                fh.write(json.dumps({"t_s": round(t, 2), "reports_per_s": round(rate, 2),
                                     **r, "battery": st, "backend": bs}) + "\n")
                fh.flush()
            if reader.fatal:
                print(f"FAIL: reader died: {reader.fatal}")
                ok = False
                break

        r = reader.take()
        say("done")
        print()
        print(f"  duration            {(time.perf_counter()-t0)/60:.1f} min")
        print(f"  reports delivered   {reader.reports} "
              f"({reader.reports / (time.perf_counter()-t0):.2f}/s)")
        print(f"  empty polls         {reader.empty}")
        print(f"  read errors         {reader.errors}")
        print(f"  sequence breaks     {reader.seq_breaks}")
        print(f"  worst gap           {worst['gap_ms_max']:.3f} ms")
        print(f"  battery             {battery_first}% -> {battery_last}%")
        print(f"  disconnects         {backend.stats['disconnects']}")
        print(f"  reconnects          {backend.stats['reconnects']}")
        print(f"  watchdog trips      {backend.stats['link_watchdog_trips']}")
        print(f"  bt read errors      {backend.stats['bt_read_errors']}")
        print(f"  neutral reports     {backend.stats['input_neutral']}")
        ok = ok and reader.fatal is None and reader.seq_breaks == 0
    finally:
        if reader:
            reader.stop_flag.set()
            reader.join(timeout=5)
        svc.stop()
        if fh:
            fh.close()
    print()
    print("RESULT: PASS" if ok else "RESULT: FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
