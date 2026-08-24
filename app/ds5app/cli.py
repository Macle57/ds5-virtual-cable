"""`ds5bridge` -- one command that turns a Bluetooth DualSense into a wired one.

    ds5bridge                 do the whole thing; Ctrl+C to stop
    ds5bridge devices         which controllers can I see, and how charged?
    ds5bridge cleanup         rescue: undo a run that crashed without tearing down
    ds5bridge doctor          is this machine set up correctly?
    ds5bridge tray            the same thing with a system-tray icon

Everything the two-terminal Phase-3 procedure did by hand, in order, with the
teardown that used to be easy to get wrong. See `docs/USER-GUIDE.md`.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time

from . import _bootstrap  # noqa: F401  (sys.path side effect)
from . import controller as C
from . import service as S
from .usbip import DEFAULT_PORT, REQUIRED_RELEASE, Usbip, UsbipNotFound

VERSION = "0.4.0"

BANNER = r"""
  ds5bridge -- a Bluetooth DualSense, presented to Windows as a wired one
"""


def _log(kind: str, text: str) -> None:
    prefix = {"warn": "  !  ", "error": " !!! ", "ready": "  *  ",
              "battery": "  ~  "}.get(kind, "  -  ")
    print(prefix + text, flush=True)


# ---------------------------------------------------------------------------


def cmd_devices(args) -> int:
    cands = C.probe_all("BT", want_battery=True)
    if not cands:
        print("No DualSense is paired over Bluetooth.")
        print("Pair it: hold CREATE + PS until the light bar flashes, then add")
        print("it in Windows Settings > Bluetooth & devices.")
        return 1
    print(f"{len(cands)} controller(s) enumerated over Bluetooth:\n")
    for c in cands:
        mark = "  " if c.alive else "x "
        print(f" {mark}{c.describe()}")
    dead = [c for c in cands if not c.alive]
    if dead:
        print("\n'x' means it enumerates but does not answer. The usual cause is")
        print("that it is charging on a USB cable -- a DualSense on a cable turns")
        print("its Bluetooth radio off but leaves a stale pairing entry behind.")
    live = [c for c in cands if c.alive]
    if len(live) > 1:
        print(f"\nMore than one is live, so pass --serial <address>, e.g.")
        print(f"  ds5bridge --serial {live[0].serial}")
    return 0


def cmd_cleanup(args) -> int:
    ok = S.cleanup(port=args.port, usbip_exe=args.usbip, log_fn=print)
    return 0 if ok else 1


def cmd_doctor(args) -> int:
    problems = 0
    print(BANNER.strip() + f"   v{VERSION}\n")

    try:
        u = Usbip(args.usbip, port=args.port)
        ver = u.version()
        print(f"{'[ok]':7}usbip.exe  {u.exe}")
        print(f"{'':7}version    {ver}")
        if REQUIRED_RELEASE not in ver:
            print(f"{'[warn]':7}this project is validated against {REQUIRED_RELEASE}. "
                  f"Do NOT use 0.9.7.8 -- its maintainer warns it can corrupt memory.")
    except UsbipNotFound as e:
        print("[FAIL] usbip.exe not found\n")
        print(str(e))
        return 1

    ports = u.our_ports()
    if ports:
        print(f"{'[warn]':7}something is already attached on port(s) {ports} -- "
              f"run `ds5bridge cleanup`")
        problems += 1
    else:
        print(f"{'[ok]':7}nothing attached")

    if S.port_free(args.port):
        print(f"{'[ok]':7}TCP {args.port} free")
    else:
        print(f"{'[warn]':7}TCP {args.port} held by PID(s) {S.port_owner_pids(args.port)}"
              f" -- run `ds5bridge cleanup`")
        problems += 1

    cands = C.probe_all("BT", want_battery=True)
    live = [c for c in cands if c.alive]
    if not live:
        print(f"{'[FAIL]':7}no live Bluetooth DualSense")
        problems += 1
    else:
        for c in live:
            note = C.battery_note(c.battery_percent, c.battery_state)
            flag = "warn" if (c.battery_percent is not None
                              and c.battery_percent <= C.BATTERY_WARN_PERCENT
                              and not c.battery_state.startswith("charging")) else "ok"
            print(f"{'[' + flag + ']':7}controller {c.serial}  {note}")
    for c in cands:
        if not c.alive:
            print(f"{'[note]':7}{c.serial} enumerates but does not answer "
                  f"(charging on a cable?)")

    print()
    print("no problems found" if not problems else f"{problems} thing(s) to fix")
    return 0 if not problems else 1


def cmd_run(args) -> int:
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    print(BANNER)

    svc = S.BridgeService(serial=args.serial, port=args.port, usbip_exe=args.usbip,
                          audio_target=args.audio_target,
                          auto_cleanup=not args.no_auto_cleanup,
                          on_event=_log)
    try:
        svc.start()
    except C.AmbiguousControllerError as e:
        print("\nMore than one Bluetooth DualSense is connected. Pick one:\n")
        for c in e.candidates:
            print(f"    ds5bridge --serial {c.serial}      ({c.describe()})")
        return 2
    except UsbipNotFound as e:
        print("\n" + str(e))
        return 3
    except S.AlreadyRunningError as e:
        print(f"\n{e}")
        return 5
    except (C.NoControllerError, RuntimeError) as e:
        print(f"\nCould not start: {e}")
        return 4
    except KeyboardInterrupt:
        return 130

    print("\n  Windows now sees a wired DualSense. Start your game.")
    print("  Press Ctrl+C here to stop and put everything back.\n")

    try:
        last = 0.0
        while True:
            time.sleep(0.5)
            if args.status_every and time.time() - last >= args.status_every:
                last = time.time()
                print("  " + svc.status_line(), flush=True)
            if svc.state == S.ERROR:
                break
    except KeyboardInterrupt:
        print()
    finally:
        svc.stop()
    return 0


def cmd_tray(args) -> int:
    from .tray import run_tray
    return run_tray(args)


# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ds5bridge", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"ds5bridge {VERSION}")

    def common(sp):
        sp.add_argument("--serial", default=None, metavar="BDADDR",
                        help="which controller, by Bluetooth address (see "
                             "`ds5bridge devices`). Required when more than one "
                             "is connected -- index order is NOT stable")
        sp.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"TCP port for the USB/IP server (default "
                             f"{DEFAULT_PORT}; 3240 belongs to usbipd-win and is "
                             f"never touched)")
        sp.add_argument("--usbip", default=None, metavar="PATH",
                        help="path to usbip.exe, if it is somewhere unusual")
        return sp

    sub = p.add_subparsers(dest="cmd")

    r = common(sub.add_parser("run", help="start the bridge (the default)"))
    r.add_argument("--audio-target", choices=("speaker", "headphone"),
                   default="speaker")
    r.add_argument("--no-auto-cleanup", action="store_true",
                   help="fail instead of clearing leftovers from a crashed run")
    r.add_argument("--status-every", type=float, default=30.0, metavar="SECONDS",
                   help="print a status line this often (0 to stay quiet)")
    r.add_argument("-v", "--verbose", action="store_true")
    r.set_defaults(func=cmd_run)

    d = sub.add_parser("devices", help="list controllers and their battery level")
    d.set_defaults(func=cmd_devices)

    c = common(sub.add_parser("cleanup", help="undo a run that crashed"))
    c.set_defaults(func=cmd_cleanup)

    dr = common(sub.add_parser("doctor", help="check this machine's setup"))
    dr.set_defaults(func=cmd_doctor)

    t = common(sub.add_parser("tray", help="run with a system-tray icon"))
    t.add_argument("--audio-target", choices=("speaker", "headphone"),
                   default="speaker")
    t.add_argument("--no-auto-cleanup", action="store_true")
    t.add_argument("--autostart", action="store_true",
                   help="start bridging immediately instead of waiting for a click")
    t.add_argument("-v", "--verbose", action="store_true")
    t.set_defaults(func=cmd_tray)

    return p


def _owns_console() -> bool:
    """True when this process is the only one on its console.

    Which is the same as "somebody double-clicked me": launched from an existing
    terminal, that terminal's shell is on the console too. It matters because a
    double-clicked program that exits with an error closes its window
    instantly, taking the error message with it -- and the error message is the
    entire point of `doctor` and of every failure path here.
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        buf = (ctypes.c_uint * 8)()
        n = ctypes.windll.kernel32.GetConsoleProcessList(buf, 8)
        return n == 1
    except Exception:  # noqa: BLE001
        return False


def _pause_if_double_clicked() -> None:
    if not _owns_console():
        return
    try:
        input("\nPress Enter to close this window ...")
    except (EOFError, KeyboardInterrupt, OSError):
        pass


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # Bare `ds5bridge` means `ds5bridge run`, and so does `ds5bridge --serial X`.
    if not argv or argv[0].startswith("-") and argv[0] not in ("--version", "-h", "--help"):
        argv.insert(0, "run")
    args = build_parser().parse_args(argv)
    if not getattr(args, "func", None):
        args = build_parser().parse_args(["run"])
    try:
        code = args.func(args)
    except KeyboardInterrupt:
        return 130
    if code:
        _pause_if_double_clicked()
    return code


if __name__ == "__main__":
    sys.exit(main())
