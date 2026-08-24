"""`BridgeService` -- the whole bring-up and tear-down dance, in one object.

Before Phase 4a, using this project meant two terminals and two commands, and
getting the *shutdown* order wrong left a half-attached device behind that the
next run then tripped over. This module is that dance, done once, correctly,
with teardown that survives Ctrl+C, a closed console window, an unhandled
exception and a hard kill of the parent shell.

Bring-up order
--------------
    1. find usbip.exe                    -> a clear message if it is missing
    2. clear anything stale              -> port 3241 held? device still attached?
    3. pick the controller               -> BY SERIAL when there is a choice
    4. start the USB/IP server           -> in-process, on its own asyncio thread
    5. wait until it is actually listening
    6. usbip attach                      -> the virtual wired DualSense appears
    7. verify it attached                -> `usbip port` must now list it

Tear-down order -- the part that is not obvious
----------------------------------------------
    1. `usbip attach -X` FIRST. `usbip attach` arms a background auto-re-attach;
       detaching without stopping it silently reacquires the device the instant
       a server is listening again (STATUS.md 15.5 trap 1). Doing this last, or
       not at all, is how you end up with a second device on port 02.
    2. detach every attached port
    3. stop the server and close the Bluetooth handle
    4. VERIFY `usbip port` is empty, and say so if it is not

The server runs **in this process**, not as a child. That removes the entire
class of failure STATUS.md 17.8 traps 7 and 8 describe (a stale emulator whose
command line cannot be read, or that cannot be killed from a later shell), and
it means the tray app and the CLI share one code path and one set of counters.
"""

from __future__ import annotations

import asyncio
import atexit
import ctypes
import logging
import re
import signal
import socket
import subprocess
import sys
import threading
import time

from . import _bootstrap  # noqa: F401  (sys.path side effect)
from . import controller as C
from .usbip import DEFAULT_PORT, Usbip, UsbipNotFound  # noqa: F401

log = logging.getLogger("ds5app.service")

_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

STOPPED = "stopped"
STARTING = "starting"
RUNNING = "running"
DEGRADED = "controller offline"
STOPPING = "stopping"
ERROR = "error"


# ---------------------------------------------------------------------------
# port hygiene
# ---------------------------------------------------------------------------


def port_free(port: int, host: str = "127.0.0.1") -> bool:
    s = socket.socket()
    try:
        s.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def port_owner_pids(port: int) -> list[int]:
    """PIDs listening on `port`, via netstat.

    Deliberately not a command-line match. `Win32_Process.CommandLine` comes
    back EMPTY for some python processes -- the venv launcher spawns a child
    whose command line the query cannot read -- so matching on 'ds5emu' misses
    the process that actually holds the socket, the stale server keeps the port,
    and the new one appears to start normally (STATUS.md 17.8 trap 7). The
    socket owner is the only thing that is always right.
    """
    if sys.platform != "win32":
        return []
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True,
                             text=True, timeout=15,
                             creationflags=_CREATE_NO_WINDOW).stdout
    except Exception:  # noqa: BLE001
        return []
    pids = []
    for line in out.splitlines():
        m = re.match(r"\s*TCP\s+\S+:(\d+)\s+\S+\s+LISTENING\s+(\d+)", line)
        if m and int(m.group(1)) == port:
            pids.append(int(m.group(2)))
    return sorted(set(pids))


#: Image names this program is willing to kill to reclaim its port. Anything
#: else holding 3241 is somebody else's program and is none of our business.
_KILLABLE = ("python.exe", "pythonw.exe", "python3.exe", "ds5bridge.exe",
             "ds5bridge-tray.exe")


def process_name(pid: int) -> str:
    """Image name for a PID, or "" .

    The image NAME, deliberately, not the command line. `Win32_Process.
    CommandLine` comes back empty for some python processes -- the venv
    launcher spawns a child whose command line the query cannot read -- which
    is STATUS.md 17.8 trap 7 and the reason a command-line match must never be
    load-bearing. The name is always there.
    """
    if sys.platform != "win32":
        return ""
    try:
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
                             capture_output=True, text=True, timeout=15,
                             creationflags=_CREATE_NO_WINDOW).stdout
    except Exception:  # noqa: BLE001
        return ""
    line = out.strip().splitlines()[0] if out.strip() else ""
    return line.split('","')[0].lstrip('"') if line.startswith('"') else ""


def kill_pids(pids: list[int], log_fn=None) -> list[int]:
    """Kill only processes that could plausibly be ours.

    Reclaiming a port must never turn into killing a stranger's program because
    it happened to bind 3241 first. If the holder is not one of ours, say so and
    leave it alone -- the user can then decide.
    """
    killed = []
    for pid in pids:
        if pid <= 4:
            continue
        name = process_name(pid)
        if name and name.lower() not in _KILLABLE:
            if log_fn:
                log_fn(f"REFUSING to kill PID {pid} ({name}) -- that is not "
                       f"ds5bridge. Close it, or use --port to pick another port.")
            continue
        try:
            subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True,
                           timeout=15, creationflags=_CREATE_NO_WINDOW)
            killed.append(pid)
        except Exception:  # noqa: BLE001
            pass
    return killed


def cleanup(port: int = DEFAULT_PORT, usbip_exe: str | None = None,
            log_fn=print) -> bool:
    """The rescue path. Idempotent, safe twice, safe when nothing is up.

    This is what `ds5bridge cleanup` runs, and what a crashed previous run needs
    somebody to run for it. Returns True when the machine ends up clean.
    """
    ok = True
    try:
        u = Usbip(usbip_exe, port=port)
    except UsbipNotFound as e:
        log_fn(str(e))
        return False

    log_fn("stopping the auto-re-attach ...")
    u.stop_auto_reattach()

    ports = u.our_ports()
    for p in ports:
        log_fn(f"detaching port {p} ...")
        u.detach(p)

    pids = port_owner_pids(port)
    if pids:
        log_fn(f"stopping the process holding TCP {port}: {pids}")
        kill_pids(pids, log_fn=log_fn)
        time.sleep(0.7)

    if not port_free(port):
        log_fn(f"WARNING: TCP {port} is still held by {port_owner_pids(port)}")
        ok = False
    if not u.is_clean():
        log_fn(f"WARNING: usbip still reports attached ports {u.our_ports()}")
        ok = False
    if ok:
        log_fn("clean: nothing attached, port free")
    return ok


# ---------------------------------------------------------------------------
# one bridge at a time
# ---------------------------------------------------------------------------


class AlreadyRunningError(RuntimeError):
    pass


class InstanceLock:
    """A named mutex, so a second launch refuses instead of fighting.

    Found by the launcher test, and it is a nasty one: double-clicking the exe
    twice used to give the second instance a "left over from a previous run"
    verdict -- port held, device attached -- whereupon its auto-cleanup killed
    the *first, healthy* bridge and detached the controller out from under
    whatever was using it. Port and attach state cannot distinguish "a stale
    corpse" from "a working sibling"; a mutex can, so the check happens before
    anything looks at the port.

    Local\\ rather than Global\\: per-session is the right scope (two users can
    each bridge their own controller) and Global\\ needs privileges the tray app
    should not want.
    """

    def __init__(self, name: str):
        self.name = name
        self._h = None

    def acquire(self) -> None:
        if sys.platform != "win32":
            return
        ERROR_ALREADY_EXISTS = 183
        k32 = ctypes.windll.kernel32
        k32.CreateMutexW.restype = ctypes.c_void_p
        h = k32.CreateMutexW(None, False, self.name)
        if not h:
            return                       # cannot tell; do not block the user
        if ctypes.get_last_error() == ERROR_ALREADY_EXISTS or \
                k32.GetLastError() == ERROR_ALREADY_EXISTS:
            k32.CloseHandle(ctypes.c_void_p(h))
            raise AlreadyRunningError(
                "ds5bridge is already running. Stop the other one first "
                "(Ctrl+C in its window, or Quit in the tray menu).")
        self._h = h

    def release(self) -> None:
        if self._h and sys.platform == "win32":
            try:
                ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(self._h))
            except Exception:  # noqa: BLE001
                pass
        self._h = None


# ---------------------------------------------------------------------------
# crash-proof teardown
# ---------------------------------------------------------------------------

_ACTIVE: list["BridgeService"] = []
_handlers_installed = False
_console_handler = None  # keep a reference or ctypes garbage-collects it

#: Extra callables run after every service has been torn down. The tray adds
#: one that stops its icon: `raise KeyboardInterrupt` from a signal handler does
#: NOT escape pystray's Win32 message loop, so the tray tore down correctly on
#: Ctrl+Break and then sat there forever as a live process with a dead bridge.
ON_TEARDOWN: list = []


def _teardown_all() -> None:
    for svc in list(_ACTIVE):
        try:
            svc.stop()
        except Exception:  # noqa: BLE001
            log.exception("teardown failed")
    for fn in list(ON_TEARDOWN):
        try:
            fn()
        except Exception:  # noqa: BLE001
            log.exception("teardown hook failed")


def install_crash_handlers() -> None:
    """atexit + signals + the Windows console-control handler.

    Three separate ways a run can end, and each needs its own hook:

      * a normal return or an unhandled exception   -> atexit
      * Ctrl+C, or a `taskkill` without /F          -> SIGINT / SIGTERM
      * the console window's X button, logoff, or
        Ctrl+Break                                  -> SetConsoleCtrlHandler,
        which is the ONLY one of the three that fires for a window close, and
        without it a closed terminal leaves the device attached
    """
    global _handlers_installed, _console_handler
    if _handlers_installed:
        return
    _handlers_installed = True
    atexit.register(_teardown_all)

    def _sig(signum, frame):  # noqa: ARG001
        _teardown_all()
        raise KeyboardInterrupt

    for s in (signal.SIGINT, signal.SIGTERM, getattr(signal, "SIGBREAK", None)):
        if s is not None:
            try:
                signal.signal(s, _sig)
            except (ValueError, OSError):   # not the main thread / unsupported
                pass

    if sys.platform == "win32":
        HANDLER = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_uint)

        def _console(event):   # CTRL_C=0 CLOSE=2 LOGOFF=5 SHUTDOWN=6
            _teardown_all()
            return False       # let the default handler continue the exit
        _console_handler = HANDLER(_console)
        try:
            ctypes.windll.kernel32.SetConsoleCtrlHandler(_console_handler, True)
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# the service
# ---------------------------------------------------------------------------


class BridgeService:
    def __init__(self, serial: str | None = None, port: int = DEFAULT_PORT,
                 host: str = "127.0.0.1", busid: str = "1-1",
                 usbip_exe: str | None = None, audio_target: str = "speaker",
                 auto_cleanup: bool = True, on_event=None):
        self.serial = serial
        self.port = port
        self.host = host
        self.busid = busid
        self.usbip_exe = usbip_exe
        self.audio_target = audio_target
        self.auto_cleanup = auto_cleanup
        self.on_event = on_event or (lambda kind, text: None)

        self.state = STOPPED
        self.error: str | None = None
        self.controller: C.Candidate | None = None
        self.started_at: float | None = None
        self.usbip: Usbip | None = None

        self._backend = None
        self._server = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._start_error: BaseException | None = None
        self._lock = threading.RLock()
        self._attached = False
        self._battery: C.BatteryWatcher | None = None
        self._rate_prev: tuple[float, int] | None = None
        self._rate_hz = 0.0
        self._lock_file = InstanceLock(rf"Local\ds5bridge-{port}")
        #: usbip ports THIS service attached. Teardown detaches only these --
        #: never "every attached port", which would rip out a sibling's device.
        self._our_ports: list[int] = []

    # -- events ------------------------------------------------------------

    def _emit(self, kind: str, text: str) -> None:
        log.info("%s: %s", kind, text)
        try:
            self.on_event(kind, text)
        except Exception:  # noqa: BLE001
            log.exception("event handler failed")

    # -- start -------------------------------------------------------------

    def start(self) -> None:
        with self._lock:
            if self.state in (RUNNING, STARTING):
                return
            self.state = STARTING
            self.error = None
        install_crash_handlers()
        if self not in _ACTIVE:
            _ACTIVE.append(self)
        try:
            self._start_inner()
        except BaseException as e:  # noqa: BLE001
            self.error = str(e)
            self.state = ERROR
            self._emit("error", str(e))
            # Never leave half a bring-up behind.
            try:
                self.stop(quiet=True)
            except Exception:  # noqa: BLE001
                pass
            raise

    def _start_inner(self) -> None:
        # 0. am I the only one? Before anything else, because everything below
        # interprets a busy port as "stale" and would clean up a live sibling.
        self._lock_file.acquire()

        # 1. usbip.exe, or a message a person can act on
        self.usbip = Usbip(self.usbip_exe, port=self.port, host=self.host,
                           busid=self.busid)
        self._emit("info", f"usbip {self.usbip.version()} at {self.usbip.exe}")

        # 2. stale state from a previous run that did not get to tear down.
        #
        # `attach -X` ALWAYS, before anything else, even when everything looks
        # clean. Measured on 2026-08-25: hard-kill a running bridge and both
        # `usbip port` and TCP 3241 come back empty -- the driver detaches when
        # the connection dies -- but the background auto-re-attach is STILL
        # ARMED, and it fires the instant a server listens again. Starting the
        # server then gives you a device on port 01 that nobody asked for, and
        # our own attach adds a second one on port 02. Nothing about the state
        # of the machine reveals this beforehand, so it is not conditional.
        self.usbip.stop_auto_reattach()

        stale_ports = self.usbip.our_ports()
        busy = not port_free(self.port)
        if stale_ports or busy:
            what = []
            if stale_ports:
                what.append(f"a device still attached on port {stale_ports}")
            if busy:
                what.append(f"TCP {self.port} held by {port_owner_pids(self.port)}")
            msg = "left over from a previous run: " + " and ".join(what)
            if not self.auto_cleanup:
                raise RuntimeError(msg + ". Run `ds5bridge cleanup` first.")
            self._emit("warn", msg + " -- cleaning up")
            cleanup(self.port, self.usbip.exe, log_fn=lambda t: self._emit("info", t))
            if not port_free(self.port):
                raise RuntimeError(
                    f"TCP {self.port} is still in use after cleanup "
                    f"(PIDs {port_owner_pids(self.port)}). Something else is "
                    f"using this port; close it and try again.")

        # 3. the controller -- by serial whenever there is a choice
        self.controller = C.select(self.serial)
        self.serial = self.controller.serial
        self._emit("info", f"controller {self.controller.describe()}")
        note = C.battery_note(self.controller.battery_percent,
                              self.controller.battery_state)
        if (self.controller.battery_percent is not None
                and self.controller.battery_percent <= C.BATTERY_WARN_PERCENT
                and not self.controller.battery_state.startswith("charging")):
            self._emit("warn", note)
        else:
            self._emit("info", note)

        # 4-5. the server, on its own thread, and wait for it to listen
        self._start_server()

        # 6. attach
        self._emit("info", "attaching the virtual controller ...")
        res = self.usbip.attach()
        if not res.ok:
            raise RuntimeError(
                f"usbip attach failed (exit {res.code}): {res.out.strip()}")

        # 7. verify -- `usbip port` prints nothing at all when nothing is attached
        ports: list[int] = []
        for _ in range(20):
            ports = self.usbip.our_ports()
            if ports:
                break
            time.sleep(0.25)
        else:
            raise RuntimeError(
                "usbip reported success but nothing is attached. Check that the "
                "usbip2_ude driver is running (Device Manager -> System devices "
                "-> USBip 3.X Emulated Host Controller).")
        # Belt and braces for step 2: if an armed re-attach did slip in, Windows
        # now has two identical DualSenses and every game sees a phantom second
        # player. Keep the lowest port and detach the rest.
        if len(ports) > 1:
            self._emit("warn", f"{len(ports)} devices attached; detaching the extras")
            for p in sorted(ports)[1:]:
                self.usbip.detach(p)
            ports = sorted(ports)[:1]
        self._our_ports = list(ports)
        self._attached = True
        self.started_at = time.time()
        # Prime the rate differencer so the first snapshot reports a real number
        # rather than 0.0 -- a tray that says "0 reports/s" for its first second
        # reads as broken.
        self._rate_prev = (time.perf_counter(),
                           self._backend.stats["input_delivered"])
        self.state = RUNNING
        self._emit("ready",
                   f"virtual wired DualSense attached (controller {self.serial})")

        self._battery = C.BatteryWatcher(
            self._backend,
            on_report=lambda p, s: self._emit("battery", C.battery_note(p, s)),
            on_warn=lambda p, s: self._emit("warn", C.battery_note(p, s)),
            interval=60.0)
        self._battery.start()

    def _start_server(self) -> None:
        from ds5emu.bridge import BridgeBackend
        from ds5emu.server import UsbIpServer
        from ds5emu.timing import TimerResolution

        self._backend = BridgeBackend(target=self.audio_target, serial=self.serial)
        self._server = UsbIpServer(self._backend, host=self.host, port=self.port,
                                   busid=self.busid)
        self._ready.clear()
        self._start_error = None

        def run() -> None:
            # Windows' default 15.6 ms timer granularity would swamp the 1 ms
            # isochronous service interval this server has to hold.
            with TimerResolution(1):
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                # The usbip driver drops the TCP connection on detach, so the
                # proactor transport raises ConnectionResetError from its own
                # callback during every normal teardown. That is not an error
                # and must not print a traceback at a user.
                def _quiet(_loop, ctx):
                    exc = ctx.get("exception")
                    if isinstance(exc, (ConnectionResetError, ConnectionAbortedError)):
                        log.debug("transport closed: %s", exc)
                        return
                    _loop.default_exception_handler(ctx)
                loop.set_exception_handler(_quiet)
                self._loop = loop
                try:
                    loop.run_until_complete(self._server.start())
                except BaseException as e:  # noqa: BLE001
                    self._start_error = e
                    self._ready.set()
                    loop.close()
                    return
                self._ready.set()
                try:
                    loop.run_forever()
                finally:
                    try:
                        loop.run_until_complete(self._server.stop())
                    except Exception:  # noqa: BLE001
                        log.exception("server stop failed")
                    try:
                        loop.close()
                    finally:
                        self._loop = None

        self._thread = threading.Thread(target=run, name="ds5-usbip-server",
                                        daemon=True)
        self._thread.start()
        # `server.start()` opens the Bluetooth controller, which takes a moment.
        if not self._ready.wait(timeout=30.0):
            raise RuntimeError("the USB/IP server did not start within 30 s")
        if self._start_error is not None:
            raise RuntimeError(f"could not start the bridge: {self._start_error}")

    # -- stop --------------------------------------------------------------

    def stop(self, quiet: bool = False) -> None:
        with self._lock:
            if self.state in (STOPPED, STOPPING):
                return
            self.state = STOPPING
        if not quiet:
            self._emit("info", "shutting down ...")

        if self._battery is not None:
            self._battery.stop()
            self._battery = None

        u = self.usbip
        if u is not None and self._attached:
            # ORDER MATTERS. -X first: `usbip attach` armed a background
            # auto-re-attach, and detaching while it is armed reacquires the
            # device the moment a server listens again.
            try:
                u.stop_auto_reattach()
            except Exception:  # noqa: BLE001
                log.exception("attach -X failed")
            # Only the ports THIS service attached. Detaching "everything"
            # would rip a sibling bridge's device out from under a running game.
            for p in (self._our_ports or self._safe_ports(u)):
                try:
                    u.detach(p)
                except Exception:  # noqa: BLE001
                    log.exception("detach -p %d failed", p)
        self._attached = False
        self._our_ports = []

        # Now the server, so nothing is listening for the auto-re-attach even
        # if the step above somehow failed.
        loop, thread = self._loop, self._thread
        if loop is not None:
            try:
                loop.call_soon_threadsafe(loop.stop)
            except RuntimeError:
                pass
        if thread is not None:
            # The loop thread runs backend.stop() on its way out: three thread
            # joins, a mic disarm (two Bluetooth writes with a 20 ms settle) and
            # the HID close. Bounded by the backend's own 3 s deadline, but a
            # wedged hidapi call can sit on top of it.
            thread.join(timeout=15.0)
            if thread.is_alive():
                log.warning("the server thread did not exit within 15 s")
        self._thread = None

        if u is not None and not quiet:
            left = self._safe_ports(u)
            if left:
                self._emit("warn",
                           f"usbip still lists attached ports {left}; run "
                           f"`ds5bridge cleanup`")
            elif not quiet:
                self._emit("info", "clean: nothing attached, port free")

        self._lock_file.release()
        if self in _ACTIVE:
            _ACTIVE.remove(self)
        self.state = STOPPED
        self.started_at = None

    @staticmethod
    def _safe_ports(u: Usbip) -> list[int]:
        try:
            return u.our_ports()
        except Exception:  # noqa: BLE001
            return []

    # -- introspection -----------------------------------------------------

    def snapshot(self) -> dict:
        """Cheap enough to call once a second from a tray icon.

        O(1) counters only -- no `UrbMeter.summary()`, which sorts. Sorting
        holds the GIL, and the GIL is what the isochronous endpoints need
        released every millisecond (STATUS.md 17.8 trap 10). The HID rate below
        is differenced between two calls rather than computed from a window.
        """
        snap = {
            "state": self.state,
            "error": self.error,
            "serial": self.serial,
            "uptime_s": (time.time() - self.started_at) if self.started_at else 0.0,
            "attached": self._attached,
            "battery_percent": None,
            "battery_state": "",
            "connected": False,
            "reports_per_s": 0.0,
            "input_delivered": 0,
            "disconnects": 0,
            "reconnects": 0,
            "neutral": 0,
        }
        be = self._backend
        if be is None:
            return snap
        try:
            st = be.device_status()
            snap.update(battery_percent=st["battery_percent"],
                        battery_state=st["battery_state"],
                        connected=st["connected"])
            s = be.stats
            snap.update(input_delivered=s["input_delivered"],
                        disconnects=s["disconnects"],
                        reconnects=s["reconnects"],
                        neutral=s["input_neutral"])
            now = time.perf_counter()
            n = s["input_delivered"]
            if self._rate_prev is not None:
                dt = now - self._rate_prev[0]
                if dt > 0.2:
                    self._rate_hz = (n - self._rate_prev[1]) / dt
                    self._rate_prev = (now, n)
            else:
                self._rate_prev = (now, n)
            snap["reports_per_s"] = round(self._rate_hz, 1)
            if self.state == RUNNING and not st["connected"]:
                snap["state"] = DEGRADED
        except Exception:  # noqa: BLE001
            log.exception("snapshot failed")
        return snap

    def status_line(self) -> str:
        s = self.snapshot()
        if s["state"] == STOPPED:
            return "stopped"
        bat = ("battery ?" if s["battery_percent"] is None
               else f"battery {s['battery_percent']}%")
        return (f"{s['state']}  {s['serial'] or '?'}  {bat}  "
                f"{s['reports_per_s']:.0f} reports/s  "
                f"up {int(s['uptime_s'])}s")
