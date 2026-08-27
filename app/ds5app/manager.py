"""`BridgeManager` -- more than one controller at once, each in its own process.

Why processes and not threads
-----------------------------
`service.py` deliberately runs ONE bridge in-process, and says why: a child
whose command line cannot be read and that cannot be killed from a later shell
is STATUS.md 17.8 traps 7 and 8. That reasoning is still right for one bridge.
It stops being right at two, and this was settled by measurement rather than
argument -- `app/tools/multi_soak.py`, two real controllers, 120 s per
configuration, a 250 Hz reader per virtual device running in a third process so
neither configuration carried the other's load:

    2026-08-25, d42f4ba1485d + a0fa9c0dd8bb, 120 s each

                            two BridgeService      two child
                            in ONE process         processes
    reports/s               241.15 / 241.16        249.07 / 249.07
    inter-report gap p99    8.33 / 8.28 ms         5.79 / 5.82 ms
    gap max                 51.8 / 40.5 ms         11.5 / 11.1 ms
    sequence breaks         0 / 0                  0 / 0
    empty polls             2 / 0                  0 / 0
    Bluetooth read errors   0 / 0                  0 / 0
    process CPU             85 % of ONE core       70 % + 72 % = 142 %

    ... and again on an independent 20 s pair, so it is not one bad run:
    reports/s               241.89 / 241.54        250.17 / 250.17
    gap p99 (worst)         8.24 ms                5.84 ms
    process CPU             89 % of ONE core       70 % + 74 % = 145 %

The CPU row is the whole story. Two bridges want about 1.4 cores of
Python-level work; one interpreter can hand out at most 1.0, so in-process runs
pinned at the GIL ceiling and pays for it in the tail. 241 reports/s with ZERO
sequence breaks is not lost data -- it is the emulated interrupt IN endpoint
under-delivering, which is exactly the signature STATUS.md 17.4(2) recorded the
first time this happened ("the gate then under-delivered -- 220.2/s, with 10 %
of gaps at 8 ms"). A 52 ms input stall is perceptible in a game, and there is
no headroom left for a third controller or for audio.

Two honest caveats about the numbers above. The isochronous audio path was NOT
driven under control -- Windows makes a freshly attached virtual DualSense the
default render endpoint and streams whatever it feels like into it, which is
not the same load twice, so the underrun and queue-drop counters differ between
runs in BOTH directions and prove nothing either way. And Bluetooth airtime is
shared: with two controllers live, per-controller BT report rates fell to
93-450/s against the ~476 Hz a single unit manages (STATUS.md 16.6 #11). That
is a radio effect, identical in both configurations, and `repeat_stale_input`
absorbs it -- which is why the USB side still reaches 250 Hz.

So: one child process per controller, and the orphan problem solved properly
rather than hoped away -- see `create_kill_on_close_job()` below.

What this module refuses to do
------------------------------
* **It never acts on "all attached ports".** `service.py` earns per-bridge
  isolation with `_our_ports`, and `usbip.our_ports()` attributes a port by
  `host:port`. One controller failing must never detach another's device --
  that regression already cost a 30-minute soak once (usbip.py `our_ports`).
* **The hotplug watcher never opens a HID handle.** `controller.probe_all()`
  opens each device and reads a feature report; doing that to a controller a
  bridge already has open either fails or disturbs the live link. So the
  watcher uses `enumerate_devices()`, which only lists, and never probes at
  all. There is deliberately no "skip the ones that are bridged" bookkeeping,
  because that is the version of this code that has a bug in it.
* **It never allocates TCP 3240.** That is usbipd-win's, and this project does
  not touch it (usbip.py).
"""

from __future__ import annotations

import ctypes
import logging
import os
import re
import subprocess
import sys
import threading
import time

from . import _bootstrap  # noqa: F401  (sys.path side effect)
from . import service as S
from .usbip import DEFAULT_PORT

log = logging.getLogger("ds5app.manager")

#: First port a controller may be given. usbipd-win owns 3240.
BASE_PORT = DEFAULT_PORT
USBIPD_PORT = 3240

#: How far up from the base to look before giving up. A machine that has 200
#: consecutive busy ports has a problem this module cannot fix.
PORT_SPAN = 200

STOPPED = S.STOPPED
STARTING = S.STARTING
RUNNING = S.RUNNING
DEGRADED = S.DEGRADED
STOPPING = S.STOPPING
ERROR = S.ERROR

_CREATE_NO_WINDOW = 0x08000000
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_CTRL_BREAK_EVENT = 1


# ---------------------------------------------------------------------------
# the job object -- what makes a child architecture as safe as an in-process one
# ---------------------------------------------------------------------------


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong)]


class _JOB_BASIC(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", ctypes.c_uint32),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", ctypes.c_uint32),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", ctypes.c_uint32),
                ("SchedulingClass", ctypes.c_uint32)]


class _JOB_EXTENDED(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _JOB_BASIC),
                ("IoInfo", _IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t)]


_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JobObjectExtendedLimitInformation = 9


def create_kill_on_close_job():
    """A job handle whose closure kills every process in it. -> handle or None.

    Without this, child processes are strictly worse than threads. `atexit`,
    the signal handlers and `SetConsoleCtrlHandler` that `service.py` installs
    all assume this process gets to run code on its way out; `taskkill /F` on
    the tray gives it none of that, and what survives is a bridge still holding
    TCP 3241 whose command line the next shell cannot read -- STATUS.md 17.8
    trap 7, exactly, and the reason the in-process design was chosen for one
    bridge in the first place.

    The kernel closes every handle of a killed process, so the job closes, so
    the children die. It needs no cooperation from anybody, which is the only
    property a last-resort teardown can be built on.
    """
    if sys.platform != "win32":
        return None
    try:
        k32 = ctypes.windll.kernel32
        k32.CreateJobObjectW.restype = ctypes.c_void_p
        h = k32.CreateJobObjectW(None, None)
        if not h:
            return None
        info = _JOB_EXTENDED()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k32.SetInformationJobObject(ctypes.c_void_p(h),
                                           _JobObjectExtendedLimitInformation,
                                           ctypes.byref(info), ctypes.sizeof(info)):
            k32.CloseHandle(ctypes.c_void_p(h))
            return None
        return h
    except Exception:  # noqa: BLE001
        log.exception("could not create the job object")
        return None


def assign_to_job(job, proc: subprocess.Popen) -> bool:
    """Put a freshly spawned child in the job.

    Called immediately after `Popen` returns, deliberately: at that moment the
    child is still inside the ntdll loader and has not run a line of its own
    code, so it cannot yet have spawned a grandchild that would escape.
    """
    if job is None or sys.platform != "win32":
        return False
    try:
        return bool(ctypes.windll.kernel32.AssignProcessToJobObject(
            ctypes.c_void_p(job), ctypes.c_void_p(int(proc._handle))))
    except Exception:  # noqa: BLE001
        log.exception("could not put pid %s in the job object", proc.pid)
        return False


def close_job(job) -> None:
    if job is not None and sys.platform == "win32":
        try:
            ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(job))
        except Exception:  # noqa: BLE001
            pass


def _have_console() -> bool:
    """True when this process has a console a child can share.

    It decides whether a polite Ctrl+Break shutdown is even possible:
    `GenerateConsoleCtrlEvent` only reaches processes on the CALLER's console,
    so a tray app started with no console cannot send one and must fall back to
    a hard kill plus `service.cleanup()`. Getting this backwards means every
    tray shutdown waits out the full stop timeout for nothing.
    """
    if sys.platform != "win32":
        return False
    try:
        buf = (ctypes.c_uint * 4)()
        return ctypes.windll.kernel32.GetConsoleProcessList(buf, 4) > 0
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# discovery -- listing only, never probing
# ---------------------------------------------------------------------------


def enumerate_serials() -> list[str]:
    """Bluetooth DualSense serials, WITHOUT opening any of them.

    `controller.probe_all()` opens every enumerated device and reads a feature
    report, which is exactly what must not happen to a controller a bridge
    already has open. Enumeration answers the only question hotplug asks --
    did something appear or disappear -- and answers it without touching the
    radio.

    The cost is that a stale entry counts as present: a DualSense charging on a
    cable still enumerates over Bluetooth with its radio off (controller.py's
    opening fact). So a start attempted from here can legitimately fail, and
    `BridgeManager` treats a failed start as a reason to back off rather than
    as a reason to keep trying.
    """
    from ds5bridge import device as DEV
    out = []
    for info in DEV.enumerate_devices():
        if info.transport == "BT" and info.serial:
            out.append(info.serial.lower())
    # dict.fromkeys, not sorted(): the caller must not depend on order, and
    # snapshot() is on a 2 s path where sorting is not free (17.8 trap 10).
    return list(dict.fromkeys(out))


def _hidden_serials() -> list[str]:
    """Serials WE have hidden with HidHide, from its journal. [] on any failure.

    Kept out of `poll_once` behind an injectable so the oscillation guard can be
    tested with no driver, and wrapped so that a HidHide problem can never stop
    hotplug from working.
    """
    try:
        from . import hidhide as HH

        return HH.hidden_serials()
    except Exception:  # noqa: BLE001
        log.debug("could not read the HidHide journal", exc_info=True)
        return []


def default_child_command(serial: str, port: int, usbip_exe: str | None = None,
                          audio_target: str = "speaker",
                          status_every: float = 2.0,
                          hide_bluetooth: bool = False,
                          hidhide_cli: str | None = None) -> list[str]:
    """The command line for one bridge child.

    It runs the SAME `ds5bridge run` the CLI runs and the same `BridgeService`
    the tray runs -- one code path, one set of counters, one set of traps. The
    child reports itself through its stdout, which is why `--status-every` is
    small here and not the CLI's user-facing 30 s.

    Frozen: `sys.executable` is whichever exe is hosting us, and for the tray
    that is `ds5bridge-tray.exe`, which has no subcommands. The bridge exe is
    its neighbour, so look there rather than trusting `sys.executable`.
    """
    args = ["run", "--serial", serial, "--port", str(port),
            "--status-every", str(status_every), "--audio-target", audio_target]
    if usbip_exe:
        args += ["--usbip", usbip_exe]
    # The child owns its own hide/unhide, because `BridgeService.stop()` is the
    # one place every stop path converges on. Passing it as a flag rather than
    # letting the child read config.json keeps a `--serial` one-run override
    # honest and keeps the child's behaviour a function of its command line.
    if hide_bluetooth:
        args += ["--hide-bluetooth"]
    if hidhide_cli:
        args += ["--hidhide-cli", hidhide_cli]
    if getattr(sys, "frozen", False):
        here = os.path.dirname(os.path.abspath(sys.executable))
        exe = os.path.join(here, "ds5bridge.exe")
        return [exe if os.path.isfile(exe) else sys.executable] + args
    return [sys.executable, "-m", "ds5app"] + args


# ---------------------------------------------------------------------------
# one child bridge
# ---------------------------------------------------------------------------

#: `BridgeService.status_line()`, as `cli.cmd_run` prints it:
#:     running  d42f4ba1485d  battery 40%  250 reports/s  up 61s
_STATUS_RE = re.compile(
    r"^(?P<state>.+?)\s\s+(?P<serial>[0-9a-fA-F?]{4,})\s\s+"
    r"battery (?P<bat>\d+|\?)%?\s\s+(?P<rps>[\d.]+) reports/s\s\s+"
    r"up (?P<up>\d+)s\s*$")

#: The prefixes `cli._log` puts in front of each event.
_EVENT_PREFIX = {"  *  ": "ready", "  !  ": "warn", " !!! ": "error",
                 "  ~  ": "battery", "  -  ": "info"}


class ChildBridge:
    """One controller's bridge, running as a child process.

    Everything the tray needs is kept in `_snap`, updated by the stdout reader
    thread and copied by `snapshot()`. Nothing here shells out, polls a socket
    or asks usbip anything on the read path: `snapshot()` is called every
    couple of seconds from a tray icon, and `service.py`'s own `snapshot()`
    docstring explains why that path must stay a dict copy.
    """

    def __init__(self, serial: str, port: int, command: list[str],
                 job=None, on_event=None, stop_timeout: float = 15.0,
                 usbip_exe: str | None = None, cleanup_fn=None):
        self.serial = serial.lower()
        self.port = port
        self.command = list(command)
        self.job = job
        self.usbip_exe = usbip_exe
        self.on_event = on_event or (lambda serial, kind, text: None)
        self.stop_timeout = stop_timeout
        #: Injected so tests can watch that teardown cleans only THIS port.
        self.cleanup_fn = cleanup_fn or S.cleanup
        self.proc: subprocess.Popen | None = None
        self.error: str | None = None
        self._lock = threading.RLock()
        self._reader: threading.Thread | None = None
        self._snap = self._blank()

    def _blank(self) -> dict:
        return {"serial": self.serial, "port": self.port, "state": STOPPED,
                "error": None, "pid": None, "battery_percent": None,
                "reports_per_s": 0.0, "uptime_s": 0, "attached": False,
                "last_event": ""}

    # -- lifecycle ---------------------------------------------------------

    @property
    def alive(self) -> bool:
        p = self.proc
        return p is not None and p.poll() is None

    def start(self) -> None:
        with self._lock:
            if self.alive:
                return
            self._snap = self._blank()
            self._snap["state"] = STARTING
            self.error = None
            flags = _CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
            if sys.platform == "win32" and not _have_console():
                # A tray host has no console; without this every child would
                # flash a terminal window on screen.
                flags |= _CREATE_NO_WINDOW
            self.proc = subprocess.Popen(
                self.command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, text=True, bufsize=1,
                errors="replace", creationflags=flags)
            if self.job is not None and not assign_to_job(self.job, self.proc):
                log.warning("pid %s is NOT in the job object; a hard kill of "
                            "this process could orphan it", self.proc.pid)
            self._snap["pid"] = self.proc.pid
            self._reader = threading.Thread(
                target=self._read_stdout, args=(self.proc,),
                name=f"ds5-child-{self.serial}", daemon=True)
            self._reader.start()

    def wait_ready(self, timeout: float = 60.0) -> bool:
        """Block until the child says it attached, or died, or ran out of time.

        60 s because a onefile PyInstaller child unpacks itself before it runs
        a line, and `BridgeService.start()` alone allows 30 s for the USB/IP
        server to come up.
        """
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            st = self._snap["state"]
            if st in (RUNNING, DEGRADED):
                return True
            if st == ERROR or not self.alive:
                return False
            time.sleep(0.1)
        self.error = f"{self.serial}: the bridge did not come up within {timeout:g}s"
        self._fail(self.error)
        return False

    def stop(self) -> None:
        """Ask, kill, then clean up THIS port -- in that order, always.

        Step three is not a fallback, it runs every time. There is no reliable
        polite kill on Windows: `Popen.terminate()` is `TerminateProcess`,
        which runs no atexit hook and no console handler, and Ctrl+Break only
        reaches a child that shares our console -- which a tray host does not
        have. So correctness cannot rest on the child tearing itself down.

        `service.cleanup(port)` is safe next to a running sibling and that is
        not an accident: it detaches only `Usbip(port=port).our_ports()`, which
        is attributed by host:port. It does call `attach -X`, which is global,
        but that only disarms the background auto-re-attach -- it detaches
        nothing, and `service.BridgeService.stop()` already does it with
        siblings running.

        What a hard kill does cost: `BridgeBackend.stop()` never runs, so the
        controller's microphone is never disarmed and keeps draining until the
        next open re-primes it. Windows closes the HID handle for us.
        """
        with self._lock:
            p = self.proc
            self.proc = None
            if p is not None:
                self._snap["state"] = STOPPING
        if p is not None and p.poll() is None:
            if not self._ctrl_break(p):
                self._kill(p)
            else:
                try:
                    p.wait(timeout=self.stop_timeout)
                except subprocess.TimeoutExpired:
                    log.warning("%s did not stop in %.0fs; killing pid %s",
                                self.serial, self.stop_timeout, p.pid)
                    self._kill(p)
        # Only this port. Never "everything attached".
        try:
            self.cleanup_fn(self.port, self.usbip_exe,
                            log_fn=lambda t: self._emit("info", t))
        except Exception:  # noqa: BLE001
            log.exception("cleanup of port %d failed", self.port)
        with self._lock:
            self._snap = self._blank()

    def _ctrl_break(self, p: subprocess.Popen) -> bool:
        if sys.platform != "win32" or not _have_console():
            return False
        try:
            return bool(ctypes.windll.kernel32.GenerateConsoleCtrlEvent(
                _CTRL_BREAK_EVENT, p.pid))
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _kill(p: subprocess.Popen) -> None:
        try:
            p.kill()
            p.wait(timeout=10.0)
        except Exception:  # noqa: BLE001
            log.exception("could not kill pid %s", p.pid)

    # -- what the child tells us -------------------------------------------

    def _emit(self, kind: str, text: str) -> None:
        try:
            self.on_event(self.serial, kind, text)
        except Exception:  # noqa: BLE001
            log.exception("event handler failed")

    def _fail(self, text: str) -> None:
        with self._lock:
            self.error = text
            self._snap["state"] = ERROR
            self._snap["error"] = text

    def _read_stdout(self, p: subprocess.Popen) -> None:
        """Blocking readline on its own thread -- the child's only status channel.

        Blocking I/O, so it releases the GIL while it waits; this thread costs
        nothing between lines. It also has to keep draining after a failure,
        because a full stdout pipe would block the child mid-teardown.
        """
        # The LAST EVENT, not the last line. A dying child's last line is
        # usually its most recent status line, and "the bridge exited with code
        # 1: running d42f4ba1485d battery 30% 250 reports/s" reads like a
        # contradiction in a tray tooltip.
        last = ""
        try:
            for raw in p.stdout:  # type: ignore[union-attr]
                line = raw.rstrip("\r\n")
                if not line.strip():
                    continue
                m = _STATUS_RE.match(line.strip())
                if m:
                    bat = m.group("bat")
                    with self._lock:
                        self._snap.update(
                            state=m.group("state").strip(),
                            battery_percent=None if bat == "?" else int(bat),
                            reports_per_s=float(m.group("rps")),
                            uptime_s=int(m.group("up")),
                            attached=True)
                    continue
                kind = _EVENT_PREFIX.get(raw[:5], None)
                if kind is None:
                    continue
                text = line.strip()[len(raw[:5].strip()):].strip() or line.strip()
                last = text
                with self._lock:
                    self._snap["last_event"] = text
                if kind == "ready":
                    with self._lock:
                        self._snap.update(state=RUNNING, attached=True,
                                          error=None)
                elif kind == "error":
                    self._fail(text)
                self._emit(kind, text)
        except Exception:  # noqa: BLE001
            log.debug("stdout reader for %s ended", self.serial, exc_info=True)
        finally:
            try:
                p.stdout.close()  # type: ignore[union-attr]
            except Exception:  # noqa: BLE001
                pass
            code = p.poll()
            with self._lock:
                if self._snap["state"] not in (STOPPING, STOPPED):
                    if code:
                        self._snap["state"] = ERROR
                        self._snap["error"] = (
                            self._snap["error"]
                            or (f"the bridge exited with code {code}"
                                + (f": {last}" if last else "")))
                    else:
                        self._snap["state"] = STOPPED
                    self._snap["attached"] = False
                    self._snap["reports_per_s"] = 0.0

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._snap)


# ---------------------------------------------------------------------------
# the manager
# ---------------------------------------------------------------------------


class BridgeManager:
    """Owns N bridges, keyed by lowercased Bluetooth serial.

    Deliberately knows nothing about where its settings are persisted. The
    enabled flags and the master switch arrive as constructor arguments and
    leave through callbacks, so `config.py` can own the file and this class can
    be tested without one.
    """

    def __init__(self,
                 base_port: int = BASE_PORT,
                 ports: dict | None = None,
                 enabled: dict | None = None,
                 master_enabled: bool = True,
                 default_enabled: bool = True,
                 hotplug_interval: float = 5.0,
                 vanish_grace: float = 30.0,
                 retry_after: float = 60.0,
                 usbip_exe: str | None = None,
                 audio_target: str = "speaker",
                 hide_bluetooth: dict | None = None,
                 hide_default: bool = False,
                 hidhide_cli: str | None = None,
                 child_command=None,
                 discover=None,
                 bridge_factory=None,
                 port_free=None,
                 hidden_serials=None,
                 on_event=None,
                 on_enabled_changed=None,
                 on_master_changed=None,
                 on_port_assigned=None,
                 on_hide_changed=None,
                 use_job: bool = True):
        # 3240 is usbipd-win's. Refuse it as a base rather than silently
        # sliding off it, so a caller that passes it hears about it.
        if base_port == USBIPD_PORT:
            raise ValueError("3240 belongs to usbipd-win and must not be used")
        self.base_port = base_port
        self.master_enabled = bool(master_enabled)
        self.default_enabled = bool(default_enabled)
        self.hotplug_interval = hotplug_interval
        self.vanish_grace = vanish_grace
        self.retry_after = retry_after
        self.usbip_exe = usbip_exe
        self.audio_target = audio_target
        self.hide_default = bool(hide_default)
        self.hidhide_cli = hidhide_cli
        self.on_event = on_event or (lambda serial, kind, text: None)
        self.on_enabled_changed = on_enabled_changed or (lambda s, v: None)
        self.on_master_changed = on_master_changed or (lambda v: None)
        self.on_port_assigned = on_port_assigned or (lambda s, p: None)
        self.on_hide_changed = on_hide_changed or (lambda s, v: None)

        self._child_command = child_command or (
            lambda serial, port: default_child_command(
                serial, port, usbip_exe=self.usbip_exe,
                audio_target=self.audio_target,
                hide_bluetooth=self.is_hiding(serial),
                hidhide_cli=self.hidhide_cli))
        self._discover = discover or enumerate_serials
        self._bridge_factory = bridge_factory or ChildBridge
        self._port_free = port_free or S.port_free
        #: Injected so the oscillation guard in `poll_once` is testable without
        #: a driver. Returns the serials WE currently have hidden.
        self._hidden_serials = hidden_serials or _hidden_serials

        self._lock = threading.RLock()
        self._enabled: dict[str, bool] = {k.lower(): bool(v)
                                          for k, v in (enabled or {}).items()}
        self._hide: dict[str, bool] = {k.lower(): bool(v)
                                       for k, v in (hide_bluetooth or {}).items()}
        # Seeded from the caller's persisted map, because enumeration order is
        # NOT stable (STATUS.md 17.5: the controllers swap, and both enumerate).
        # Stickiness within one run is not enough on its own -- without a
        # persisted map the same controller can come back on a different port
        # after a restart, which is a different devnode to Windows and loses
        # whatever the user had configured against it.
        self._ports: dict[str, int] = {k.lower(): int(v)
                                       for k, v in (ports or {}).items()
                                       if int(v) != USBIPD_PORT}
        self._bridges: dict[str, object] = {}
        self._errors: dict[str, str] = {}
        #: When a serial may next be started automatically. A failed start must
        #: not turn into a hot loop that opens a HID handle every five seconds.
        self._retry_at: dict[str, float] = {}
        #: When a serial was first seen missing. See `poll_once`.
        self._missing_since: dict[str, float] = {}
        #: Serials the CALLER stopped. Found on hardware: `stop(serial)` used to
        #: be undone by the very next hotplug pass five seconds later, because
        #: the controller was obviously still enumerated -- so "Stop" in a tray
        #: menu did nothing you could see. An explicit stop holds until an
        #: explicit start, or until the controller actually goes away.
        self._held: set = set()
        #: Serials with a start in flight. `start()` blocks for as long as the
        #: child takes to attach, and the hotplug thread calls it too; without
        #: this, a manual start and a hotplug pass spawn two children for one
        #: controller and the second dies on the port's named mutex.
        self._inflight: set = set()
        self._present: list[str] = []

        self._job = create_kill_on_close_job() if use_job else None
        self._watch: threading.Thread | None = None
        self._watch_stop = threading.Event()

        S.install_crash_handlers()
        # `service._ACTIVE` holds BridgeService objects and this manager holds
        # none, so the hook -- not the list -- is the integration point. It is
        # what makes Ctrl+C, a closed console and a logoff tear every child
        # down; the job object covers the one case a hook cannot, a hard kill.
        self._hook = self._teardown_hook
        S.ON_TEARDOWN.append(self._hook)

    # -- events ------------------------------------------------------------

    def _emit(self, serial: str, kind: str, text: str) -> None:
        log.info("%s %s: %s", serial, kind, text)
        try:
            self.on_event(serial, kind, text)
        except Exception:  # noqa: BLE001
            log.exception("event handler failed")

    # -- ports -------------------------------------------------------------

    def port_for(self, serial: str) -> int:
        """This controller's port, allocated once and then never moved.

        Sticky on purpose. A controller that came back on a different port
        would appear to Windows as a different device -- new devnode, new
        friendly name, and every per-device setting a game or the sound control
        panel had attached to it is gone. Stickiness costs a dict entry; the
        alternative costs the user their configuration.
        """
        serial = serial.lower()
        new = None
        with self._lock:
            if serial in self._ports:
                return self._ports[serial]
            taken = set(self._ports.values())
            for p in range(self.base_port, self.base_port + PORT_SPAN):
                if p == USBIPD_PORT or p in taken:
                    continue
                if not self._port_free(p):
                    continue
                self._ports[serial] = new = p
                break
        if new is None:
            raise RuntimeError(
                f"no free TCP port between {self.base_port} and "
                f"{self.base_port + PORT_SPAN}; run `ds5bridge cleanup`")
        self.on_port_assigned(serial, new)
        return new

    # -- enable / disable --------------------------------------------------

    def is_enabled(self, serial: str) -> bool:
        with self._lock:
            return self._enabled.get(serial.lower(), self.default_enabled)

    def set_enabled(self, serial: str, enabled: bool) -> None:
        serial = serial.lower()
        with self._lock:
            self._enabled[serial] = bool(enabled)
            self._retry_at.pop(serial, None)
        self.on_enabled_changed(serial, bool(enabled))
        if enabled:
            if self.master_enabled:
                self.start(serial)
        else:
            self.stop(serial)

    def is_hiding(self, serial: str) -> bool:
        with self._lock:
            return self._hide.get(serial.lower(), self.hide_default)

    def set_hide_bluetooth(self, serial: str, hide: bool) -> None:
        """Flip the hide toggle, and apply it to a running bridge immediately.

        Applying now rather than at the next start is what the user expects from
        a checkbox, and it is safe in both directions because everything in
        `hidhide` is idempotent and keyed by serial: whichever process ends up
        owning the journal entry, every stop path and the startup sweep both
        clear it.

        What it CANNOT do is evict a game that already has the pad open --
        HidHide intercepts `IRP_MJ_CREATE`, so an existing handle is unaffected.
        The caller's notification has to say so, which is why the tray's text is
        "games started from now on".
        """
        serial = serial.lower()
        with self._lock:
            self._hide[serial] = bool(hide)
            running = serial in self._bridges
        self.on_hide_changed(serial, bool(hide))
        if not running:
            return
        try:
            from . import hidhide as HH

            if hide:
                HH.hide_for_bridge(serial, cli_override=self.hidhide_cli,
                                   log_fn=lambda t: self._emit(serial, "info", t))
            else:
                HH.unhide_for_bridge(serial, cli_override=self.hidhide_cli,
                                     log_fn=lambda t: self._emit(serial, "info", t))
        except Exception:  # noqa: BLE001
            log.exception("applying the hide toggle for %s failed", serial)

    def set_master_enabled(self, enabled: bool) -> None:
        """The one switch that turns everything off, without forgetting anything.

        Per-controller enabled flags are untouched, so turning it back on
        restores exactly what was running before.
        """
        with self._lock:
            self.master_enabled = bool(enabled)
        self.on_master_changed(bool(enabled))
        if enabled:
            self.start_all()
        else:
            self.stop_all()

    # -- start / stop ------------------------------------------------------

    def start(self, serial: str) -> bool:
        """Bring up one controller. Never raises at the caller for one failure.

        Returns True when the child reported itself attached. A False here must
        leave every other bridge exactly as it was -- that is the entire point
        of this class, so the failure path cleans up only this controller's
        port and records the error against this controller's serial.
        """
        serial = serial.lower()
        with self._lock:
            if not self.master_enabled:
                return False
            b = self._bridges.get(serial)
            if b is not None and b.alive:
                return True
            if serial in self._inflight:
                return False
            self._inflight.add(serial)
            self._held.discard(serial)
            self._errors.pop(serial, None)
        try:
            port = self.port_for(serial)
            b = self._bridge_factory(serial, port,
                                     self._child_command(serial, port),
                                     job=self._job,
                                     usbip_exe=self.usbip_exe,
                                     on_event=self._emit)
            with self._lock:
                self._bridges[serial] = b
            b.start()
            ok = b.wait_ready()
        except Exception as e:  # noqa: BLE001
            log.exception("starting %s failed", serial)
            self._record_failure(serial, str(e))
            return False
        finally:
            with self._lock:
                self._inflight.discard(serial)
        if not ok:
            self._record_failure(serial, (b.snapshot().get("error")
                                          or b.error or "the bridge did not start"))
            return False
        with self._lock:
            self._retry_at.pop(serial, None)
        return True

    def _record_failure(self, serial: str, text: str) -> None:
        with self._lock:
            self._errors[serial] = text
            self._retry_at[serial] = time.monotonic() + self.retry_after
            b = self._bridges.pop(serial, None)
        self._emit(serial, "error", text)
        if b is not None:
            try:
                b.stop()          # tears down only this serial's port
            except Exception:  # noqa: BLE001
                log.exception("cleaning up a failed start of %s", serial)

    def stop(self, serial: str) -> None:
        serial = serial.lower()
        with self._lock:
            b = self._bridges.pop(serial, None)
            self._missing_since.pop(serial, None)
            # Under the SAME lock as the pop. A hotplug pass that saw the bridge
            # gone but not the hold would spawn a replacement while this one was
            # still tearing down, and the replacement dies on the port's named
            # mutex ("ds5bridge is already running").
            self._held.add(serial)
        if b is None:
            return                                    # idempotent by design
        try:
            b.stop()
        except Exception:  # noqa: BLE001
            log.exception("stopping %s failed", serial)

    def start_all(self) -> list[str]:
        """Start every enabled controller that is present. -> the ones that came up.

        One failure is recorded and skipped, never propagated: a flat
        controller must not stop its sibling from working.
        """
        started = []
        if not self.master_enabled:
            return started
        now = time.monotonic()
        with self._lock:
            self._held.clear()
        for serial in self._refresh_present():
            if not self.is_enabled(serial):
                continue
            with self._lock:
                if self._retry_at.get(serial, 0.0) > now:
                    continue
            if self.start(serial):
                started.append(serial)
        return started

    def stop_all(self) -> None:
        """Safe when nothing is running, and safe twice."""
        with self._lock:
            serials = list(self._bridges)
        for serial in serials:
            self.stop(serial)

    # -- hotplug -----------------------------------------------------------

    def _refresh_present(self) -> list[str]:
        try:
            present = [s.lower() for s in self._discover()]
        except Exception:  # noqa: BLE001
            log.exception("controller discovery failed")
            return list(self._present)
        with self._lock:
            self._present = present
        return present

    def poll_once(self) -> None:
        """One hotplug pass. Public so a caller can tick it without a thread.

        Two rules, both learned from the hardware:

        1. **Never probe.** `_discover` only enumerates; see
           `enumerate_serials()`. A probe of a bridged controller opens a HID
           handle the bridge already holds.
        2. **A controller that vanishes is not immediately gone.** A Bluetooth
           DualSense drops off enumeration for a few seconds routinely, and
           `BridgeBackend` is built for it -- it keeps the virtual device
           attached and reports a neutral controller until the link returns
           (measured 2026-08-25: a 4 s silence tripped the link watchdog on
           both controllers mid-soak, and both recovered on their own). Tearing
           the bridge down on the first miss would turn a recoverable dropout
           into a device removal Windows shows the user. So a disappearance
           only counts after `vanish_grace` seconds of continuous absence.
        3. **A controller WE hid counts as present.** This is the oscillation
           trap from scoping section 6.7, and it is not obvious. HidHide blocks
           `CreateFile` on the HID interface, and hidapi's `hid_enumerate` opens
           every interface to read its strings and skips the ones it cannot --
           so a hidden pad silently drops out of `_discover()` even though it is
           sitting right there. Without the union below: hide -> the tray stops
           seeing it -> `vanish_grace` expires -> "controller gone, stopping its
           bridge" -> stop unhides -> it reappears -> "controller appeared,
           starting" -> hide -> ... A ~40 second self-sustaining flap that looks
           exactly like a flaky Bluetooth link, on hardware where flaky
           Bluetooth links are a documented real failure mode.

           Whitelisting `ds5bridge-tray.exe` (`hidhide.our_images()`) is the
           other half and should make this union redundant. Take both: a
           whitelist entry is one absolute path away from being wrong -- moved
           folder, reinstall, junction -- and the failure it protects against
           costs a day to diagnose.

           Note this is NOT the "skip the ones that are bridged" bookkeeping
           this module's docstring rejects. That rejection is about not PROBING
           a bridged controller; this adds no HID call at all, it reads a
           directory listing.
        """
        present = set(self._refresh_present())
        try:
            present |= {s.lower() for s in self._hidden_serials()}
        except Exception:  # noqa: BLE001
            log.debug("hidden-serial union failed", exc_info=True)
        now = time.monotonic()

        with self._lock:
            running = list(self._bridges.items())
        for serial, b in running:
            if serial in present:
                with self._lock:
                    self._missing_since.pop(serial, None)
            else:
                with self._lock:
                    first = self._missing_since.setdefault(serial, now)
                if now - first >= self.vanish_grace:
                    self._emit(serial, "warn",
                               f"controller gone for {self.vanish_grace:.0f}s "
                               f"-- stopping its bridge")
                    self.stop(serial)
                    continue
            # A child that died on its own -- crash, or the user killed it.
            # Its siblings are untouched; only this serial is cleaned up.
            if not b.alive:
                snap = b.snapshot()
                self._emit(serial, "error",
                           snap.get("error") or "the bridge process exited")
                self.stop(serial)
                with self._lock:
                    self._errors[serial] = (snap.get("error")
                                            or "the bridge process exited")
                    self._retry_at[serial] = now + self.retry_after

        with self._lock:
            # A held controller that really did disappear is no longer "stopped
            # by the user", it is unplugged -- so plugging it back in starts it.
            self._held &= present

        if not self.master_enabled:
            return
        for serial in present:
            with self._lock:
                if serial in self._bridges or serial in self._inflight:
                    continue
                if serial in self._held:
                    continue
                if self._retry_at.get(serial, 0.0) > now:
                    continue
            if self.is_enabled(serial):
                self._emit(serial, "info", "controller appeared -- starting")
                self.start(serial)

    def start_hotplug(self) -> None:
        """Poll on a modest interval, forever, on one daemon thread.

        Modest is load-bearing even though nothing here opens a device:
        `enumerate_devices()` still walks the HID device set, and this thread
        shares an interpreter with nothing that must not be delayed only
        because every bridge lives in its own process.
        """
        if self._watch is not None:
            return
        self._watch_stop.clear()

        def loop() -> None:
            while not self._watch_stop.wait(self.hotplug_interval):
                try:
                    self.poll_once()
                except Exception:  # noqa: BLE001
                    log.exception("hotplug pass failed")

        self._watch = threading.Thread(target=loop, name="ds5-hotplug",
                                       daemon=True)
        self._watch.start()

    def stop_hotplug(self) -> None:
        self._watch_stop.set()
        t, self._watch = self._watch, None
        if t is not None:
            t.join(timeout=self.hotplug_interval + 2.0)

    # -- introspection -----------------------------------------------------

    def snapshot(self) -> dict:
        """Per-controller dicts plus an aggregate. Cheap enough for a 2 s tray tick.

        Dict copies and integer arithmetic only -- no sort, no subprocess, no
        socket bind, no HID call. `BridgeService.snapshot()` says why in more
        detail: the GIL is needed by the 1 ms isochronous endpoints, and in
        this architecture those endpoints are in OTHER processes whose stdout
        this process must keep draining. A snapshot that blocks is a snapshot
        that stalls every child's status channel at once.
        """
        with self._lock:
            bridges = list(self._bridges.items())
            present = list(self._present)
            errors = dict(self._errors)
            enabled = dict(self._enabled)
            hide = dict(self._hide)
            ports = dict(self._ports)
            master = self.master_enabled
            default_enabled = self.default_enabled
            hide_default = self.hide_default

        controllers = {}
        running = attached = 0
        total_rate = 0.0
        for serial, b in bridges:
            snap = b.snapshot()
            snap["enabled"] = enabled.get(serial, default_enabled)
            snap["hide_bluetooth"] = hide.get(serial, hide_default)
            snap["present"] = serial in present
            controllers[serial] = snap
            if snap["state"] in (RUNNING, DEGRADED):
                running += 1
            if snap.get("attached"):
                attached += 1
            total_rate += snap.get("reports_per_s") or 0.0

        for serial in present:
            if serial not in controllers:
                controllers[serial] = {
                    "serial": serial, "port": ports.get(serial),
                    "state": STOPPED, "error": errors.get(serial), "pid": None,
                    "battery_percent": None, "reports_per_s": 0.0,
                    "uptime_s": 0, "attached": False, "last_event": "",
                    "enabled": enabled.get(serial, default_enabled),
                    "hide_bluetooth": hide.get(serial, hide_default),
                    "present": True}
        return {
            "master_enabled": master,
            "controllers": controllers,
            "aggregate": {
                "present": len(present),
                "bridges": len(bridges),
                "running": running,
                "attached": attached,
                "reports_per_s": round(total_rate, 1),
                "errors": sum(1 for c in controllers.values() if c.get("error")),
            },
        }

    def statuses(self) -> list[str]:
        """One human-readable line per controller, newest state first-hand."""
        snap = self.snapshot()
        out = []
        for serial, c in snap["controllers"].items():
            if not c["enabled"]:
                out.append(f"{serial}  disabled")
                continue
            if c["state"] == STOPPED:
                out.append(f"{serial}  stopped"
                           + (f"  ({c['error']})" if c.get("error") else ""))
                continue
            bat = ("battery ?" if c["battery_percent"] is None
                   else f"battery {c['battery_percent']}%")
            out.append(f"{serial}  {c['state']}  port {c['port']}  {bat}  "
                       f"{c['reports_per_s']:.0f} reports/s  "
                       f"up {int(c['uptime_s'])}s")
        if not out:
            out.append("no controllers")
        return out

    def known(self) -> list[str]:
        with self._lock:
            return list(dict.fromkeys(list(self._bridges) + list(self._present)))

    # -- teardown ----------------------------------------------------------

    def _teardown_hook(self) -> None:
        try:
            self.stop_all()
        except Exception:  # noqa: BLE001
            log.exception("manager teardown failed")

    def close(self) -> None:
        """Stop everything and let go of the job object. Safe twice.

        The hook has to come off `service.ON_TEARDOWN`: it is a module-level
        list, so a manager that is closed and replaced would otherwise leave a
        dead one behind to be called at exit.
        """
        self.stop_hotplug()
        self.stop_all()
        try:
            S.ON_TEARDOWN.remove(self._hook)
        except ValueError:
            pass
        close_job(self._job)
        self._job = None
