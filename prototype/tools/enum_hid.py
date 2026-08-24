"""Enumerate all DualSense HID interfaces visible to hidapi and classify BT vs USB.

Usage:
    python prototype/tools/enum_hid.py            # summary table
    python prototype/tools/enum_hid.py --descriptors   # + HID report descriptors
    python prototype/tools/enum_hid.py --probe          # + open each and probe feature 0x05/0x20

Classification heuristics (verified on this machine, see docs/usb-ground-truth.md):
  - USB path contains  "vid_054c&pid_0ce6&mi_03"  (composite interface 3 = HID)
  - BT  path contains  "{00001124-0000-1000-8000-00805f9b34fb}"  (HID over Bluetooth profile GUID)
  - The definitive test is the length of feature report 0x05: 41 bytes on USB, 45 on BT
    (BT adds the 4-byte CRC32 tail).
"""

from __future__ import annotations

import argparse
import sys

import hid

VID = 0x054C
PID = 0x0CE6

BT_PROFILE_GUID = "{00001124-0000-1000-8000-00805f9b34fb}"


def classify(path: str) -> str:
    p = path.lower()
    if BT_PROFILE_GUID in p:
        return "BT"
    if "vid_054c&pid_0ce6" in p:
        return "USB"
    return "?"


def hexdump(data: bytes, width: int = 16, indent: str = "    ") -> str:
    out = []
    for i in range(0, len(data), width):
        chunk = data[i : i + width]
        out.append(f"{indent}{i:04x}  " + " ".join(f"{b:02x}" for b in chunk))
    return "\n".join(out)


def find_devices() -> list[dict]:
    devs = [d for d in hid.enumerate(VID, PID)]
    # Sort: USB first, then BT, stable by path
    devs.sort(key=lambda d: (classify(d["path"].decode(errors="replace")) != "USB", d["path"]))
    return devs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--descriptors", action="store_true", help="dump HID report descriptors")
    ap.add_argument("--probe", action="store_true", help="open each device, probe feature 0x05/0x20")
    args = ap.parse_args()

    devs = find_devices()
    if not devs:
        print("No DualSense (054C:0CE6) HID interfaces found.")
        return 1

    print(f"Found {len(devs)} HID interface(s) for {VID:04X}:{PID:04X}\n")
    for i, d in enumerate(devs):
        path = d["path"].decode(errors="replace")
        kind = classify(path)
        print(f"[{i}] {kind}")
        print(f"    path            : {path}")
        print(f"    manufacturer    : {d.get('manufacturer_string')!r}")
        print(f"    product         : {d.get('product_string')!r}")
        print(f"    serial          : {d.get('serial_number')!r}")
        print(f"    release         : 0x{d.get('release_number', 0):04x}")
        print(f"    usage_page/usage: 0x{d.get('usage_page', 0):04x} / 0x{d.get('usage', 0):04x}")
        print(f"    interface       : {d.get('interface_number')}")

        if args.descriptors or args.probe:
            h = hid.device()
            try:
                h.open_path(d["path"])
            except Exception as e:  # noqa: BLE001
                print(f"    !! open failed: {e}")
                print()
                continue
            try:
                if args.descriptors:
                    try:
                        rd = bytes(h.get_report_descriptor())
                        print(f"    report descriptor ({len(rd)} bytes):")
                        print(hexdump(rd, indent="      "))
                    except Exception as e:  # noqa: BLE001
                        print(f"    report descriptor: FAILED {e}")
                if args.probe:
                    for rid, length in ((0x05, 64), (0x20, 64)):
                        try:
                            r = bytes(h.get_feature_report(rid, length))
                            print(f"    feature 0x{rid:02x}: {len(r)} bytes")
                            print(hexdump(r, indent="      "))
                        except Exception as e:  # noqa: BLE001
                            print(f"    feature 0x{rid:02x}: FAILED {e}")
            finally:
                h.close()
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
