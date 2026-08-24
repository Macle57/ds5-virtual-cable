"""CLI: `python -m ds5emu <command>`

    serve     run the USB/IP server (nothing is installed; a client must attach)
    descr     dump the descriptors we would serve, for eyeballing
    selftest  run the protocol unit tests

None of these modify the system. `serve` only binds a loopback TCP socket.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from . import descriptors as D
from . import wire as W
from .backend import SyntheticBackend
from .device import DualSenseDevice
from .server import DEFAULT_BUSID, UsbIpServer


def _hexdump(data: bytes, indent: str = "  ") -> str:
    out = []
    for off in range(0, len(data), 16):
        chunk = data[off : off + 16]
        hexs = " ".join(f"{b:02x}" for b in chunk)
        out.append(f"{indent}{off:04x}  {hexs}")
    return "\n".join(out)


def cmd_descr(args) -> int:
    dev = DualSenseDevice(SyntheticBackend())
    info = dev.usbip_device_info(busid=args.busid)
    print(f"busid            {info.busid}")
    print(f"VID:PID          {info.idVendor:04X}:{info.idProduct:04X}")
    print(f"speed            {info.speed} (USB_SPEED_HIGH; must not be lower:")
    print("                 usbip-win2 rewrites iso bInterval below high speed)")
    print(f"interfaces       {info.bNumInterfaces} {list(D.interface_infos())}")
    print(f"usbip_usb_device {len(info.pack())} bytes")
    print()
    print(f"device descriptor ({len(D.DEVICE_DESCRIPTOR)} bytes)")
    print(_hexdump(D.DEVICE_DESCRIPTOR))
    print(f"configuration descriptor ({len(D.CONFIG_DESCRIPTOR)} bytes)")
    print(_hexdump(D.CONFIG_DESCRIPTOR))
    print(f"HID report descriptor ({len(D.HID_REPORT_DESCRIPTOR)} bytes)")
    print(_hexdump(D.HID_REPORT_DESCRIPTOR))
    for i in (0, 1, 2, 3):
        s = D.string_descriptor(i)
        print(f"string[{i}] {'-- absent (matches the real device)' if s is None else ''}")
        if s is not None:
            print(_hexdump(s))
    return 0


def cmd_serve(args) -> int:
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    backend = SyntheticBackend(tone_hz=args.tone)
    server = UsbIpServer(backend, host=args.host, port=args.port, busid=args.busid)

    print(f"ds5emu serving {D.ID_VENDOR:04X}:{D.ID_PRODUCT:04X} on "
          f"{args.host}:{args.port} as busid {args.busid}")
    print("No driver has been installed. To attach (Phase 3, after approval):")
    print(f'  "C:\\Program Files\\USBip\\usbip.exe" list   -r {args.host}')
    print(f'  "C:\\Program Files\\USBip\\usbip.exe" attach -r {args.host} -b {args.busid}')
    print("Ctrl+C to stop.")

    async def main():
        await server.start()
        try:
            await server.serve_forever()
        finally:
            await server.stop()

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    finally:
        st = server.device.stats
        print()
        print(f"connections {server.connections}  control {st['control']}  "
              f"hid_in {st['hid_in']}  hid_out {st['hid_out']}  "
              f"iso_out {st['iso_out']}  iso_in {st['iso_in']}  stalled {st['stalled']}")
        ts = backend.iso_out_timestamps
        if len(ts) > 1:
            span = ts[-1] - ts[0]
            gaps = [b - a for a, b in zip(ts, ts[1:])]
            gaps.sort()
            print(f"iso OUT packets {len(ts)} over {span:.3f}s = {len(ts)/span:.1f}/s; "
                  f"median gap {gaps[len(gaps)//2]*1e3:.3f} ms, "
                  f"p99 {gaps[int(len(gaps)*0.99)]*1e3:.3f} ms, "
                  f"max {gaps[-1]*1e3:.3f} ms")
    return 0


def cmd_selftest(args) -> int:
    import unittest

    loader = unittest.TestLoader()
    suite = loader.discover(start_dir="tests", top_level_dir=".")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m ds5emu", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="run the USB/IP server on loopback")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=W.TCP_PORT)
    s.add_argument("--busid", default=DEFAULT_BUSID)
    s.add_argument("--tone", type=float, default=1000.0,
                   help="synthetic microphone tone in Hz")
    s.add_argument("-v", "--verbose", action="store_true")
    s.set_defaults(func=cmd_serve)

    d = sub.add_parser("descr", help="dump the descriptors we serve")
    d.add_argument("--busid", default=DEFAULT_BUSID)
    d.set_defaults(func=cmd_descr)

    t = sub.add_parser("selftest", help="run the protocol unit tests")
    t.set_defaults(func=cmd_selftest)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
