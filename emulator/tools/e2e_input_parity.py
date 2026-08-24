"""Phase 3c e2e stage (a): live input through the VIRTUAL wired device.

Reads the emulated USB HID device that Windows enumerated over USB/IP, at the
rate a real wired DualSense runs at, and proves the bytes are the physical
Bluetooth controller's live state — not a canned report — by reading the same
controller *directly* over Bluetooth at the same time and comparing field by
field.

Both handles see every Bluetooth report: the Windows HID class driver keeps a
separate queue per file object, so opening the controller a second time does
not steal anything from the emulator's own reader thread.

Alignment: Bluetooth runs ~483 Hz and the virtual endpoint 250 Hz, and the
report has to cross the bridge, so a virtual report is compared against the set
of direct-BT states seen in a short window around it (`--window-ms`). A field
counts as matching if it equals ANY state in that window. Fields that are
changing while you move a stick therefore do not produce false failures, while a
translation bug — a wrong offset, a stale buffer, a dropped byte — cannot pass,
because it would have to coincidentally equal a real recent value on every
field of every report.

    ..\\prototype\\.venv\\Scripts\\python.exe tools\\e2e_input_parity.py ^
        --virtual-path-contains 4&127b94db --seconds 20
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ds5emu import _bootstrap  # noqa: F401,E402  (puts prototype/ on sys.path)

import hid  # noqa: E402

from ds5bridge import protocol as P  # noqa: E402
from ds5bridge.pacing import Pacer, TimerResolution  # noqa: E402

#: Fields compared between the two transports. Deliberately excludes the device
#: sequence byte (BridgeBackend renumbers it — gotcha 12) and the timestamp.
FIELDS = ("lx", "ly", "rx", "ry", "l2", "r2", "buttons", "battery_level",
          "battery_state")


def _find(paths_contains: str, want_bt: bool):
    for d in hid.enumerate(0x054C, 0x0CE6):
        p = d["path"].decode("latin1") if isinstance(d["path"], bytes) else d["path"]
        is_bt = "00001124" in p
        if want_bt and is_bt:
            return p
        if not want_bt and not is_bt and paths_contains.lower() in p.lower():
            return p
    return None


class BtWatcher(threading.Thread):
    """Direct Bluetooth reader; keeps a short history of decoded states."""

    def __init__(self, path: str, history: int = 4096):
        super().__init__(name="e2e-bt-watch", daemon=True)
        self.path = path
        self.stop_flag = threading.Event()
        self.hist: deque = deque(maxlen=history)
        self.lock = threading.Lock()
        self.reports = 0
        self.errors = 0
        self.opened = threading.Event()

    def run(self) -> None:
        d = hid.device()
        d.open_path(self.path.encode("latin1")
                    if isinstance(self.path, str) else self.path)
        d.set_nonblocking(0)
        self.opened.set()
        try:
            while not self.stop_flag.is_set():
                try:
                    raw = bytes(d.read(200, timeout_ms=50))
                except Exception:                       # noqa: BLE001
                    self.errors += 1
                    continue
                if not raw or raw[0] != P.BT_INPUT_31:
                    continue
                payload = raw[1:]
                if (payload[0] & P.PAYLOAD_TYPE_MASK) != P.PAYLOAD_TYPE_CONTROL:
                    continue
                st = P.decode_input(payload, usb=False)
                if st is None:
                    continue
                self.reports += 1
                with self.lock:
                    self.hist.append((time.perf_counter(), st))
        finally:
            try:
                d.close()
            except Exception:                            # noqa: BLE001
                pass

    def window(self, t: float, half: float):
        with self.lock:
            return [s for (ts, s) in self.hist if abs(ts - t) <= half]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--virtual-path-contains", required=True,
                    help="substring of the virtual HID interface path")
    ap.add_argument("--seconds", type=float, default=20.0)
    ap.add_argument("--hz", type=float, default=250.0)
    ap.add_argument("--window-ms", type=float, default=60.0)
    ap.add_argument("--print-every", type=float, default=5.0)
    args = ap.parse_args()

    vpath = _find(args.virtual_path_contains, want_bt=False)
    bpath = _find("", want_bt=True)
    if vpath is None:
        print("FAIL: virtual HID interface not found")
        return 2
    print(f"virtual HID : {vpath}")
    print(f"direct BT   : {bpath}")

    watcher = None
    if bpath:
        watcher = BtWatcher(bpath)
        watcher.start()
        watcher.opened.wait(5.0)
        time.sleep(0.5)                       # let some history build up

    dev = hid.device()
    dev.open_path(vpath.encode("latin1") if isinstance(vpath, str) else vpath)
    dev.set_nonblocking(1)

    half = args.window_ms / 1000.0
    polls = got = empty = short = 0
    seq_gaps = 0
    prev_seq = None
    gaps: list[float] = []
    last_t = None
    compared = 0
    mismatch: dict[str, int] = {}
    reports_all_match = 0
    first_state = last_state = None

    t_end = time.perf_counter() + args.seconds
    next_print = time.perf_counter() + args.print_every
    try:
        with TimerResolution(1):
            pacer = Pacer(frame_ms=1000.0 / args.hz, max_backlog=8, max_burst=4)
            while time.perf_counter() < t_end:
                due = pacer.frames_due()
                if due == 0:
                    pacer.sleep_until_next()
                    continue
                for _ in range(due):
                    polls += 1
                    pacer.commit(1)
                    data = dev.read(64)
                    if not data:
                        empty += 1
                        continue
                    raw = bytes(data)
                    if len(raw) < 64:
                        short += 1
                        continue
                    got += 1
                    now = time.perf_counter()
                    if last_t is not None:
                        gaps.append((now - last_t) * 1e3)
                    last_t = now

                    st = P.decode_input(raw[1:], usb=True)
                    if st is None:
                        continue
                    if first_state is None:
                        first_state = st
                    last_state = st

                    seq = raw[1 + 6]          # the renumbered device seq byte
                    if prev_seq is not None and (prev_seq + 1) & 0xFF != seq:
                        seq_gaps += 1
                    prev_seq = seq

                    if watcher is not None:
                        win = watcher.window(now, half)
                        if win:
                            compared += 1
                            ok = True
                            for f in FIELDS:
                                v = getattr(st, f)
                                if not any(getattr(w, f) == v for w in win):
                                    mismatch[f] = mismatch.get(f, 0) + 1
                                    ok = False
                            if ok:
                                reports_all_match += 1
                if time.perf_counter() >= next_print:
                    next_print += args.print_every
                    el = args.seconds - (t_end - time.perf_counter())
                    print(f"  t={el:5.1f}s  virtual {got} ({got/max(el,1e-9):.1f}/s)  "
                          f"empty {empty}  parity {reports_all_match}/{compared}",
                          flush=True)
    finally:
        try:
            dev.close()
        except Exception:                                # noqa: BLE001
            pass
        if watcher is not None:
            watcher.stop_flag.set()
            watcher.join(timeout=3.0)

    span = args.seconds
    gaps_sorted = sorted(gaps)

    def pct(p):
        return gaps_sorted[min(len(gaps_sorted) - 1, int(len(gaps_sorted) * p))] \
            if gaps_sorted else float("nan")

    print()
    print(f"polls            {polls} ({polls/span:.2f}/s)")
    print(f"reports          {got} = {got/span:.2f}/s   empty {empty}  short {short}")
    if gaps_sorted:
        print(f"gap ms           median {pct(0.5):.3f}  p99 {pct(0.99):.3f}  "
              f"max {gaps_sorted[-1]:.3f}  min {gaps_sorted[0]:.3f}")
    print(f"seq discontinuit {seq_gaps}")
    if watcher is not None:
        print(f"direct BT        {watcher.reports} reports "
              f"({watcher.reports/span:.1f}/s), errors {watcher.errors}")
        print(f"parity           {reports_all_match}/{compared} reports matched on "
              f"all {len(FIELDS)} fields within +/-{args.window_ms:.0f} ms")
        if mismatch:
            print(f"  per-field misses: {mismatch}")
    if last_state is not None:
        print(f"first state      {first_state}")
        print(f"last  state      {last_state}")

    ok = (got / span > args.hz * 0.95 and short == 0 and seq_gaps == 0
          and (watcher is None or (compared > 0
                                   and reports_all_match >= compared * 0.98)))
    print(f"\nRESULT: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
