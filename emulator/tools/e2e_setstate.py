"""Phase 3c e2e stage (b): OUTPUT through the VIRTUAL wired device.

Phase 3b's `bridge_setstate_test.py` proved the same effect by calling
`BridgeBackend.write_output_report()` in-process. This one writes a HID output
report to the device **Windows enumerated over USB/IP**, the way a game would,
so the whole stack is under test:

    hidapi write, report 0x02
      -> HidUsb -> usbip2_ude -> TCP -> ds5emu interrupt OUT 0x03
      -> BridgeBackend.write_output_report -> coalescing writer thread
      -> translate.usb02_to_bt31 (seq nibble + CRC32 0xA2) -> BT 0x31
      -> controller applies the adaptive-trigger effect
      -> trigger-status bytes change in the controller's own input report
      -> BT 0x31 -> translate -> ds5emu interrupt IN 0x84 -> HidUsb
      -> hidapi read, report 0x01  -> at_status checked here

The adaptive-trigger status bytes are the only output effect on a DualSense
observable without a human watching the controller, which is why Phase 1 chose
them (STATUS.md §5.4) and why they remain the regression oracle for the output
path.

Every read is `timeout_ms`-bounded and the whole run has a wall-clock cap.

    cd emulator
    ..\\prototype\\.venv\\Scripts\\python.exe tools\\e2e_setstate.py ^
        --virtual-path-contains 4&127b94db
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ds5emu import _bootstrap  # noqa: F401,E402  (sys.path side effect)

import hid                                 # noqa: E402
from ds5bridge import device as DEV        # noqa: E402
from ds5bridge import protocol as P        # noqa: E402

O = P.OFFSETS_USB

#: (label, SetState factory, expected (at_status0, at_status1, at_status2)).
#: Expected values measured by Phase 1 on real hardware (STATUS.md §5.4) and
#: reproduced by Phase 3b through the backend API (STATUS.md §16.5(b)).
CASES = [
    ("triggers off (0x05)",
     lambda: P.SetState().trigger_right(P.AT_OFF).trigger_left(P.AT_OFF),
     (9, 9, 0)),
    ("0x21 feedback",
     lambda: P.SetState().trigger_right(P.AT_FEEDBACK, b"\xff\xff")
                         .trigger_left(P.AT_FEEDBACK, b"\xff\xff"),
     (16, 16, 17)),
    ("0x26 vibration",
     lambda: P.SetState().trigger_right(P.AT_VIBRATION, b"\xff\xff\xff")
                         .trigger_left(P.AT_VIBRATION, b"\xff\xff\xff"),
     (1, 1, 51)),
    ("0x05 off again",
     lambda: P.SetState().trigger_right(P.AT_OFF).trigger_left(P.AT_OFF),
     (9, 9, 0)),
]


def find_virtual(substr: str) -> bytes:
    for d in DEV.enumerate_devices():
        if d.transport == "USB" and substr.lower() in d.path_str.lower():
            return d.path
    raise SystemExit(f"FAIL: no USB-transport HID device matching {substr!r}. "
                     f"Is the virtual device attached? "
                     f"Seen: {[d.path_str for d in DEV.enumerate_devices()]}")


def read_at_status(h, settle: float) -> tuple[int, int, int] | None:
    """Poll the virtual interrupt IN endpoint, return the last status triple."""
    out = None
    end = time.perf_counter() + settle
    while time.perf_counter() < end:
        data = h.read(P.USB_INPUT_01_LEN, timeout_ms=50)
        if not data:
            continue
        b = bytes(data)
        if b[0] != P.USB_INPUT_01:
            continue
        body = b[1:]
        out = (body[O.at_status0], body[O.at_status1], body[O.at_status2])
    return out


def battery(h, settle: float = 0.5) -> str:
    end = time.perf_counter() + settle
    while time.perf_counter() < end:
        data = h.read(P.USB_INPUT_01_LEN, timeout_ms=50)
        if data and data[0] == P.USB_INPUT_01:
            st = P.decode_input(bytes(data)[1:], usb=True)
            if st:
                return f"{st.battery_level * 10}%/{st.battery_state}"
    return "unknown"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    # MACHINE-SPECIFIC default: the "4&xxxxxxxx" devnode instance fragment is
    # assigned by Windows at enumeration time. Run tools/e2e_endpoints.ps1 to
    # print HIDNODE= for your machine.
    ap.add_argument("--virtual-path-contains", default="4&127b94db",
                    help="substring of the virtual HID devnode instance path "
                         "(machine-specific; see tools/e2e_endpoints.ps1)")
    ap.add_argument("--settle", type=float, default=0.6,
                    help="seconds to watch the input stream after each write")
    args = ap.parse_args(argv)

    path = find_virtual(args.virtual_path_contains)
    print(f"virtual HID : {path.decode(errors='replace')}")

    h = hid.device()
    h.open_path(path)
    try:
        print(f"battery     : {battery(h)}")
        print()
        ok = True
        for label, make, expected in CASES:
            body = bytes(make().body)
            report = P.build_usb_setstate(body)      # 0x02 + 47-byte body
            h.write(report)
            got = read_at_status(h, args.settle)
            good = got == expected
            ok = ok and good
            print(f"  {label:22s} sent {len(report)} B  ->  at_status {got}  "
                  f"expected {expected}   {'OK' if good else 'MISMATCH'}")
        print()
        print(f"battery     : {battery(h)}")
        print(f"\nRESULT: {'PASS' if ok else 'FAIL'}")
        return 0 if ok else 1
    finally:
        # Always leave the triggers released, whatever happened above.
        try:
            h.write(P.build_usb_setstate(
                bytes(P.SetState().trigger_right(P.AT_OFF)
                                  .trigger_left(P.AT_OFF).body)))
            time.sleep(0.2)
        except Exception:  # noqa: BLE001
            pass
        h.close()


if __name__ == "__main__":
    sys.exit(main())
