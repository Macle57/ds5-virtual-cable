"""Side-by-side HID control-pipe probe: real wired DualSense vs the emulated one.

Read-only. Sends no SET_REPORT of any kind, so it can be pointed at a physical
controller without risk: every request it issues is a GET.

    python emulator/tools/hid_diff_probe.py --list
    python emulator/tools/hid_diff_probe.py --pick usb  --out real.json
    python emulator/tools/hid_diff_probe.py --pick usb2 --out virt.json
    python emulator/tools/hid_diff_probe.py --diff real.json virt.json

What it exercises, and what each maps to on the wire:

    get_feature_report(id, n)   -> control GET_REPORT(Feature, id)
    get_input_report(id, n)     -> control GET_REPORT(Input, id)   [HidD_GetInputReport]
    get_report_descriptor()     -> the HID report descriptor as Windows parsed it

An id that STALLs comes back as an OSError/empty read; one that is answered
comes back with its bytes. That distinction -- data vs stall -- is exactly what
a strict game or the dualsense-tester keys off, so it is recorded verbatim.
"""

from __future__ import annotations

import argparse
import binascii
import json
import sys
import time

import hid

VID = 0x054C
PID = 0x0CE6


def enumerate_pads() -> list[dict]:
    out = []
    for d in hid.enumerate(VID, PID):
        path = d["path"].decode("ascii", "replace")
        if "{00001124-0000-1000-8000-00805f9b34fb}" in path:
            kind = "bt"
        elif "MI_03" in path or "MI_" in path:
            kind = "usb"
        else:
            kind = "usb"
        out.append({"kind": kind, "path": path, "raw": d["path"],
                    "serial": d.get("serial_number") or "",
                    "product": d.get("product_string") or ""})
    # stable, and usb before bt
    out.sort(key=lambda e: (e["kind"] != "usb", e["path"]))
    seen: dict[str, int] = {}
    for e in out:
        n = seen.get(e["kind"], 0) + 1
        seen[e["kind"]] = n
        e["tag"] = e["kind"] if n == 1 else f"{e['kind']}{n}"
    return out


def _probe(fn, rid: int, size: int) -> dict:
    t0 = time.perf_counter()
    try:
        data = fn(rid, size)
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "error": str(e),
                "ms": round((time.perf_counter() - t0) * 1000, 2)}
    ms = round((time.perf_counter() - t0) * 1000, 2)
    if data is None or len(data) == 0:
        return {"status": "empty", "ms": ms}
    return {"status": "ok", "len": len(data),
            "hex": binascii.hexlify(bytes(data)).decode(), "ms": ms}


def probe(path: bytes, ids: range, size: int, do_input: bool) -> dict:
    h = hid.device()
    h.open_path(path)
    res: dict = {"path": path.decode("ascii", "replace"), "feature": {}, "input": {}}
    try:
        try:
            rd = h.get_report_descriptor()
            res["report_descriptor"] = binascii.hexlify(bytes(rd)).decode()
        except Exception as e:  # noqa: BLE001
            res["report_descriptor_error"] = str(e)
        for rid in ids:
            res["feature"][f"0x{rid:02x}"] = _probe(h.get_feature_report, rid, size)
        if do_input:
            for rid in ids:
                res["input"][f"0x{rid:02x}"] = _probe(h.get_input_report, rid, size)
    finally:
        h.close()
    return res


def summarise(res: dict, key: str) -> str:
    ok = [k for k, v in res[key].items() if v["status"] == "ok"]
    return f"{key}: {len(ok)} answered -> " + ", ".join(
        f"{k}({res[key][k]['len']})" for k in ok)


def diff(a: dict, b: dict, name_a: str, name_b: str) -> None:
    for key in ("feature", "input"):
        if not a.get(key) or not b.get(key):
            continue
        print(f"\n=== {key.upper()} GET_REPORT: {name_a} vs {name_b} ===")
        rows = []
        for rid in sorted(set(a[key]) | set(b[key])):
            ra = a[key].get(rid, {"status": "-"})
            rb = b[key].get(rid, {"status": "-"})
            sa = ra["status"] if ra["status"] != "ok" else f"ok/{ra['len']}"
            sb = rb["status"] if rb["status"] != "ok" else f"ok/{rb['len']}"
            same_bytes = (ra.get("hex") == rb.get("hex"))
            if sa == sb and same_bytes:
                continue
            rows.append((rid, sa, sb, "same bytes" if same_bytes else "bytes differ"))
        if not rows:
            print("  identical")
        for rid, sa, sb, note in rows:
            print(f"  {rid}: {name_a}={sa:<10} {name_b}={sb:<10} {note}")
    ra = a.get("report_descriptor")
    rb = b.get("report_descriptor")
    print("\n=== HID REPORT DESCRIPTOR ===")
    if ra and rb:
        print(f"  {'IDENTICAL' if ra == rb else 'DIFFERENT'} "
              f"({len(ra) // 2} vs {len(rb) // 2} bytes)")
    else:
        print(f"  {name_a}: {'present' if ra else a.get('report_descriptor_error')}")
        print(f"  {name_b}: {'present' if rb else b.get('report_descriptor_error')}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--pick", help="tag from --list (usb, usb2, bt ...)")
    ap.add_argument("--match", metavar="SUBSTRING",
                    help="select by a substring of the device path instead of a "
                         "tag. USE THIS when a real and a virtual pad are both "
                         "attached: the tags are ordered by path and therefore "
                         "move when the virtual device comes and goes, but the "
                         "instance id in the path does not. Take it from --list.")
    ap.add_argument("--out")
    ap.add_argument("--diff", nargs=2, metavar=("A", "B"))
    ap.add_argument("--first", type=lambda s: int(s, 0), default=0x00)
    ap.add_argument("--last", type=lambda s: int(s, 0), default=0xFF)
    ap.add_argument("--size", type=int, default=64)
    ap.add_argument("--no-input", action="store_true")
    args = ap.parse_args()

    if args.diff:
        with open(args.diff[0], encoding="utf-8") as f:
            a = json.load(f)
        with open(args.diff[1], encoding="utf-8") as f:
            b = json.load(f)
        diff(a, b, args.diff[0], args.diff[1])
        return 0

    pads = enumerate_pads()
    if args.list or not (args.pick or args.match):
        for p in pads:
            print(f"{p['tag']:<6} {p['product']!r} ser={p['serial']!r}")
            print(f"       {p['path']}")
        return 0

    if args.match:
        sel = [p for p in pads if args.match in p["path"]]
        what = args.match
    else:
        sel = [p for p in pads if p["tag"] == args.pick]
        what = args.pick
    if len(sel) != 1:
        print(f"{len(sel)} devices match {what!r}; try --list", file=sys.stderr)
        return 2
    res = probe(sel[0]["raw"], range(args.first, args.last + 1), args.size,
                not args.no_input)
    res["tag"] = sel[0]["tag"]
    print(summarise(res, "feature"))
    if res["input"]:
        print(summarise(res, "input"))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(res, f, indent=1)
        print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
