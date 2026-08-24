"""Phase 3b test (a): input pipe soak.

Drives `BridgeBackend.read_input_report()` at the USB cadence the real wired
controller runs at — 250 Hz, from `bInterval = 6` at high speed — and checks
three things at once:

  1. **cadence**: the emulated interrupt IN endpoint really produces ~250
     reports/s, and how often the poll finds nothing new (it should be rare,
     because Bluetooth delivers ~476 Hz).
  2. **field correctness**: every report handed to the USB side is re-decoded
     with the Phase-1 decoder in its USB form and compared field by field
     against the Phase-1 decode of the *Bluetooth payload it came from*. A
     single mismatched field fails the run. This is the live version of
     `emulator/tests/test_translate.py`.
  3. **freshness / monotonicity**: the renumbered device sequence byte
     increments by exactly one per delivered report, the way a wired
     controller's does.

Run it, then wiggle the sticks and press buttons — a static controller only
proves the idle bytes.

    cd emulator
    ..\\prototype\\.venv\\Scripts\\python.exe tools\\bridge_input_soak.py --seconds 20
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ds5emu import translate as T                    # noqa: E402
from ds5emu.bridge import BridgeBackend              # noqa: E402

from ds5bridge import protocol as P                  # noqa: E402
from ds5bridge.pacing import Pacer, TimerResolution  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=20.0)
    ap.add_argument("--hz", type=float, default=250.0,
                    help="USB poll rate to emulate (the real one is 250 Hz)")
    ap.add_argument("--print-hz", type=float, default=2.0)
    args = ap.parse_args()

    be = BridgeBackend(keep_raw=True)
    be.start()
    time.sleep(0.3)  # let the reader thread see a first report

    polls = delivered = empty = 0
    parity_fail = 0
    seq_gaps = 0
    prev_seq: int | None = None
    bad_fields: dict[str, int] = {}
    gaps: list[float] = []
    last_t: float | None = None
    last_print = 0.0
    t_end = time.perf_counter() + args.seconds
    pacer = Pacer(frame_ms=1000.0 / args.hz, max_backlog=8, max_burst=4)

    with TimerResolution(1):
        pacer.reset()
        try:
            while time.perf_counter() < t_end:
                due = pacer.frames_due()
                if due == 0:
                    pacer.sleep_until_next()
                    continue
                for _ in range(due):
                    polls += 1
                    rep = be.read_input_report(64)
                    pacer.commit(1)
                    if rep is None:
                        empty += 1
                        continue
                    delivered += 1
                    now = time.perf_counter()
                    if last_t is not None:
                        gaps.append(now - last_t)
                    last_t = now

                    raw = be.last_bt_payload
                    if raw is not None:
                        bad = T.field_parity(raw, rep)
                        if bad:
                            parity_fail += 1
                            for f in bad:
                                bad_fields[f] = bad_fields.get(f, 0) + 1

                    s = rep[1 + T.USB_SEQ_OFFSET]
                    if prev_seq is not None and s != ((prev_seq + 1) & 0xFF):
                        seq_gaps += 1
                    prev_seq = s

                now = time.perf_counter()
                if args.print_hz and now - last_print >= 1.0 / args.print_hz:
                    last_print = now
                    st = P.decode_input(rep[1:], usb=True) if rep else None
                    if st is not None:
                        print(f"  {st.one_line()}")
        except KeyboardInterrupt:
            pass

    elapsed = args.seconds
    be.stop()

    gaps.sort()
    def pct(p):
        return gaps[min(len(gaps) - 1, int(len(gaps) * p))] * 1e3 if gaps else 0.0

    print()
    print(f"--- input pipe soak, {elapsed:.1f}s at a {args.hz:.0f} Hz USB poll ---")
    print(f"  polls              {polls}  ({polls/elapsed:.2f}/s)")
    print(f"  reports delivered  {delivered}  ({delivered/elapsed:.2f}/s)")
    print(f"  polls answered None {empty} ({100.0*empty/max(polls,1):.2f} %)")
    print(f"  repeated (poll inside a BT burst gap) {be.stats['input_repeated']} "
          f"({100.0*be.stats['input_repeated']/max(delivered,1):.2f} % of reports)")
    print(f"  inter-report gap   median {pct(0.5):.3f} ms, p99 {pct(0.99):.3f} ms, "
          f"max {gaps[-1]*1e3 if gaps else 0:.3f} ms")
    print(f"  BT reports seen    {be.stats['bt_reports']} "
          f"({be.stats['bt_reports']/elapsed:.1f}/s), control "
          f"{be.stats['bt_control']} ({be.stats['bt_control']/elapsed:.1f}/s)")
    fresh = delivered - be.stats["input_repeated"]
    print(f"  decimation ratio   {fresh/max(be.stats['bt_control'],1):.3f} "
          f"({fresh} fresh of {be.stats['bt_control']} BT control reports; "
          f"476 Hz BT -> 250 Hz USB should be ~0.52)")
    print(f"  field parity       {delivered - parity_fail}/{delivered} reports exact"
          + (f"  MISMATCHES: {bad_fields}" if bad_fields else ""))
    print(f"  sequence byte      {seq_gaps} discontinuities")
    print(f"  backend            {be.summary()}")

    ok = (parity_fail == 0 and seq_gaps == 0 and delivered > elapsed * args.hz * 0.90)
    print("\nRESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
