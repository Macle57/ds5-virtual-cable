"""CLI: `python -m ds5emu <command>`

    serve     run the USB/IP server (nothing is installed; a client must attach)
    descr     dump the descriptors we would serve, for eyeballing
    selftest  run the protocol unit tests

None of these modify the system. `serve` only binds a loopback TCP socket.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time

from . import descriptors as D
from . import wire as W
from .backend import SyntheticBackend
from .device import DualSenseDevice
from .server import DEFAULT_BUSID, UsbIpServer
from .timing import TimerResolution


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


class RecordingBackend(SyntheticBackend):
    """SyntheticBackend that also dumps the speaker stream to a raw file.

    Lives here rather than in backend.py so that E1's measurement plumbing does
    not collide with the Bluetooth work happening in `BridgeBackend`. The dump
    is headerless 4-channel s16le at 48 kHz -- exactly the bytes the host put on
    the wire -- so it can be FFT'd to prove the isochronous OUT path carries the
    audio intact, not merely at the right rate.
    """

    def __init__(self, path: str, **kw):
        super().__init__(**kw)
        self._fh = open(path, "wb")

    def write_audio_out(self, pcm: bytes) -> None:
        super().write_audio_out(pcm)
        self._fh.write(pcm)

    def stop(self) -> None:
        try:
            self._fh.close()
        except Exception:  # pragma: no cover
            pass


def _snapshot(server, backend) -> dict:
    """Everything experiment E1 needs, as plain JSON-able data."""
    dev = server.device
    snap = {
        "wall_clock": time.time(),
        "uptime_s": time.perf_counter() - dev.meter.t0,
        "connections": server.connections,
        "stats": dict(dev.stats),
        "output_reports": len(backend.output_reports),
        "feature_writes": len(backend.feature_writes),
        "audio_out_packets": backend.audio_out_packets,
        "audio_out_bytes": backend.audio_out_bytes,
        "audio_in_bytes": backend.audio_in_bytes,
        "endpoints": {},
    }
    for ep in dev.meter.endpoints():
        s = dev.meter.summary(ep)
        s["gap_histogram_ms"] = [
            {"lo": lo, "hi": None if hi == float("inf") else hi, "n": n}
            for lo, hi, n in dev.meter.gap_histogram(ep)
        ]
        s["backlog_frames"] = dev.clock.backlog_frames(ep)
        s["resyncs"] = dev.clock.resyncs.get(ep, 0)
        s["resync_times_s"] = [round(t, 4) for t in dev.clock.resync_times.get(ep, [])[:64]]
        snap["endpoints"][f"0x{ep:02x}"] = s
    return snap


_EP_NAMES = {0x01: "iso OUT 0x01 (speaker+haptics, 4ch)",
             0x82: "iso IN  0x82 (mic, 2ch)",
             0x84: "int IN  0x84 (HID input)",
             0x03: "int OUT 0x03 (HID output)"}


def _print_report(snap: dict) -> None:
    st = snap["stats"]
    print()
    print(f"uptime {snap['uptime_s']:.2f}s  connections {snap['connections']}")
    print(f"control {st['control']}  hid_in {st['hid_in']}  hid_out {st['hid_out']}  "
          f"iso_out {st['iso_out']}  iso_in {st['iso_in']}  stalled {st['stalled']}")
    print(f"audio OUT {snap['audio_out_bytes']} B in {snap['audio_out_packets']} packets; "
          f"audio IN {snap['audio_in_bytes']} B; "
          f"HID output reports {snap['output_reports']}; feature writes {snap['feature_writes']}")
    for key, s in snap["endpoints"].items():
        ep = int(key, 16)
        print(f"\n  {_EP_NAMES.get(ep, key)}")
        if s.get("urbs", 0) < 2:
            print(f"    urbs {s.get('urbs', 0)} packets {s.get('packets', 0)} "
                  f"bytes {s.get('bytes', 0)}  (too few to measure)")
            continue
        print(f"    urbs {s['urbs']} ({s['urbs_per_s']:.1f}/s, "
              f"{s['packets_per_urb_mean']:.2f} packets/urb) over {s['span_s']:.2f}s")
        print(f"    packets {s['packets']} = {s['packets_per_s']:.2f}/s   "
              f"bytes {s['bytes']} = {s['bytes_per_s']/1000:.2f} kB/s")
        print(f"    urb inter-arrival: min {s['gap_ms_min']:.3f} median "
              f"{s['gap_ms_median']:.3f} p99 {s['gap_ms_p99']:.3f} max {s['gap_ms_max']:.3f} ms")
        print(f"    UNDERRUNS (frame-clock resyncs): {s.get('resyncs', 0)} at t="
              f"{s.get('resync_times_s', [])}s   backlog now "
              f"{s.get('backlog_frames', 0)} frames")
        hist = " ".join(
            f"[{h['lo']:g}-{'inf' if h['hi'] is None else format(h['hi'], 'g')}):{h['n']}"
            for h in s["gap_histogram_ms"] if h["n"]
        )
        print(f"    gap histogram (ms): {hist}")


def cmd_serve(args) -> int:
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    if args.record_out:
        backend = RecordingBackend(args.record_out, tone_hz=args.tone)
    else:
        backend = SyntheticBackend(tone_hz=args.tone)
    server = UsbIpServer(backend, host=args.host, port=args.port, busid=args.busid,
                         pace_iso=not args.no_pace_iso)

    print(f"ds5emu serving {D.ID_VENDOR:04X}:{D.ID_PRODUCT:04X} on "
          f"{args.host}:{args.port} as busid {args.busid}", flush=True)
    print(f'  "C:\\Program Files\\USBip\\usbip.exe" --tcp-port {args.port} '
          f'list   -r {args.host}', flush=True)
    print(f'  "C:\\Program Files\\USBip\\usbip.exe" --tcp-port {args.port} '
          f'attach -r {args.host} -b {args.busid}', flush=True)
    print("Ctrl+C to stop.", flush=True)

    async def snapshotter():
        while True:
            await asyncio.sleep(args.stats_every)
            snap = _snapshot(server, backend)
            if args.stats_json:
                tmp = args.stats_json + ".tmp"
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(snap, fh, indent=1)
                os.replace(tmp, args.stats_json)

    async def main():
        await server.start()
        task = asyncio.create_task(snapshotter()) if args.stats_every > 0 else None
        try:
            await server.serve_forever()
        finally:
            if task is not None:
                task.cancel()
            await server.stop()

    # Windows' default 15.6 ms timer granularity would swamp the 1 ms
    # isochronous service interval this server has to hold. See timing.py.
    with TimerResolution(1):
        try:
            asyncio.run(main())
        except KeyboardInterrupt:
            pass
        finally:
            snap = _snapshot(server, backend)
            if args.stats_json:
                with open(args.stats_json, "w", encoding="utf-8") as fh:
                    json.dump(snap, fh, indent=1)
            _print_report(snap)
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
    s.add_argument("--record-out", default=None, metavar="PATH",
                   help="dump the received speaker stream as raw 4ch s16le 48 kHz")
    s.add_argument("--stats-json", default=None,
                   help="write a measurement snapshot to this file (atomically)")
    s.add_argument("--stats-every", type=float, default=2.0,
                   help="snapshot interval in seconds; 0 disables (final one still written)")
    s.add_argument("--no-pace-iso", action="store_true",
                   help="DEBUG ONLY: answer isochronous URBs immediately. Makes the "
                        "host's audio clock run at socket speed -- see ds5emu/timing.py")
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
