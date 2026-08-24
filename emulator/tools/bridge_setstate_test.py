"""Phase 3b test (b): SetState passthrough, proven by machine-readable readback.

This is the Phase-1 §5.4 experiment re-run through `BridgeBackend`, so it
proves the *whole* USB-shaped path, not just the Bluetooth builder:

    USB output report 0x02  ->  BridgeBackend.write_output_report()
      ->  translate.usb02_to_bt31()  ->  seq nibble + CRC32(0xA2)
      ->  BT 0x31  ->  controller
      ->  adaptive-trigger status bytes change in the *input* report
      ->  BT 0x31 type 0x01  ->  translate  ->  USB input report 0x01
      ->  BridgeBackend.read_input_report()  ->  at_status read back here

The trigger-status bytes are the only output effect on a DualSense that is
observable without a human looking at the controller, which is why Phase 1
chose them and why they stay the regression test for the output path.

The `--crc-negative` run additionally flips one CRC bit on the wire and shows
the controller silently ignoring the report — proving the CRC we generate is
actually being checked, rather than the effect being a coincidence.

    cd emulator
    ..\\prototype\\.venv\\Scripts\\python.exe tools\\bridge_setstate_test.py
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ds5emu import translate as T          # noqa: E402
from ds5emu.bridge import BridgeBackend    # noqa: E402

from ds5bridge import protocol as P        # noqa: E402

O = P.OFFSETS_USB

#: (label, SetState factory, expected (at_status0, at_status1, at_status2))
#: The expected values are the ones Phase 1 measured on this exact controller
#: (docs/STATUS.md §5.4).
CASES = [
    ("idle",              lambda: P.SetState().trigger_right(P.AT_OFF)
                                              .trigger_left(P.AT_OFF),      (9, 9, 0)),
    ("0x21 feedback",     lambda: P.SetState().trigger_right(P.AT_FEEDBACK, b"\xff\xff")
                                              .trigger_left(P.AT_FEEDBACK, b"\xff\xff"),
                                                                            (16, 16, 17)),
    ("0x26 vibration",    lambda: P.SetState().trigger_right(P.AT_VIBRATION, b"\xff\xff\xff")
                                              .trigger_left(P.AT_VIBRATION, b"\xff\xff\xff"),
                                                                            (1, 1, 51)),
    ("0x05 off",          lambda: P.SetState().trigger_right(P.AT_OFF)
                                              .trigger_left(P.AT_OFF),      (9, 9, 0)),
]


def at_status(be: BridgeBackend, settle: float = 0.45) -> tuple[int, int, int]:
    """Poll the emulated interrupt IN endpoint for `settle` seconds, return the
    last trigger-status triple seen."""
    end = time.perf_counter() + settle
    out = (-1, -1, -1)
    while time.perf_counter() < end:
        rep = be.read_input_report(64)
        if rep is not None:
            b = rep[1:]
            out = (b[O.at_status0], b[O.at_status1], b[O.at_status2])
        time.sleep(0.004)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--settle", type=float, default=0.45)
    ap.add_argument("--crc-negative", action="store_true",
                    help="also flip one CRC bit and show the report is ignored")
    args = ap.parse_args()

    be = BridgeBackend()
    be.start()
    time.sleep(0.3)

    failures = 0
    print(f"{'case':<18} {'sent body[10]/[21]':<20} {'observed':<14} expected")
    for label, factory, expected in CASES:
        st = factory()
        usb_report = P.build_usb_setstate(bytes(st.body))
        assert len(usb_report) == 48 and usb_report[0] == 0x02
        be.write_output_report(usb_report)
        got = at_status(be, args.settle)
        ok = got == expected
        failures += not ok
        modes = f"0x{st.body[P.AT_RIGHT_MODE]:02x}/0x{st.body[P.AT_LEFT_MODE]:02x}"
        print(f"{label:<18} {modes:<20} {str(got):<14} {expected}  "
              f"{'OK' if ok else 'MISMATCH'}")

    if args.crc_negative:
        print("\n--- CRC enforcement (negative control) ---")
        st = P.SetState().trigger_right(P.AT_FEEDBACK, b"\xff\xff") \
                         .trigger_left(P.AT_FEEDBACK, b"\xff\xff")
        usb_report = P.build_usb_setstate(bytes(st.body))

        # baseline: make sure we are starting from "off"
        be.write_output_report(P.build_usb_setstate(
            bytes(P.SetState().trigger_right(P.AT_OFF).trigger_left(P.AT_OFF).body)))
        before = at_status(be, args.settle)

        # the exact bytes the backend would have sent, with one CRC bit flipped
        wire = bytearray(T.usb02_to_bt31(usb_report, 3))
        assert T.verify_bt_output_crc(bytes(wire))
        wire[-1] ^= 0x01
        assert not T.verify_bt_output_crc(bytes(wire))
        be._dev.write_raw(bytes(wire))  # noqa: SLF001 - deliberate raw injection
        bad = at_status(be, args.settle)

        # ...and now the same report with its CRC intact
        be.write_output_report(usb_report)
        good = at_status(be, args.settle)

        print(f"  before                 {before}")
        print(f"  0x21 with a bad CRC    {bad}   (must equal 'before')")
        print(f"  same 0x21, valid CRC   {good}  (must be (16, 16, 17))")
        if bad != before or good != (16, 16, 17):
            print("  CRC enforcement NOT reproduced")
            failures += 1
        else:
            print("  CRC is enforced: a one-bit corruption is silently dropped")

    # leave the controller clean
    be.write_output_report(P.build_usb_setstate(
        bytes(P.SetState().trigger_right(P.AT_OFF).trigger_left(P.AT_OFF).rumble(0, 0).body)))
    time.sleep(0.3)
    print(f"\n  backend: {be.summary()}")
    be.stop()

    print("\nRESULT:", "PASS" if failures == 0 else f"FAIL ({failures})")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
