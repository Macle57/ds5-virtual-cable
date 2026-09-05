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

#: Child states that mean this process is HOLDING the controller: a bridge
#: exists and owns an open HID handle to it. That is first-hand evidence the
#: pad is there, and it is better evidence than enumeration -- HidHide can stop
#: `hid_enumerate` from listing a device, it cannot stop a handle somebody
#: already has from working. `snapshot()` counts these as present.
_HOLDING = (STARTING, RUNNING, DEGRADED)

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


def _hide_status(serial: str) -> dict:
    """`{"hide_effective", "hide_note"}` for a serial -- what THIS process's
    last hide of it found (hidhide.hide_status). Never raises, never touches
    the machine: it is read inside `snapshot()`, on the 2 s tray tick.

    The two fields are the contract with the dashboard: `hide_effective` is
    None until a hide has been checked, True when HidHide's filter is verifiably
    in the pad's device stack, False when it verifiably is not (the 2026-09-05
    "UI says hidden, games see the pad" report), and `hide_note` says why.
    """
    try:
        from . import hidhide as HH

        return HH.hide_status(serial)
    except Exception:  # noqa: BLE001
        return {"hide_effective": None, "hide_note": ""}


def _unhide_serial(serial: str, cli_override: str | None = None,
                   log_fn=None) -> bool:
    """Repay one controller's hide debt from THIS process. Never raises.

    `BridgeService.stop()` already unhides, and on a machine with a console it
    gets to: `ChildBridge.stop()` sends Ctrl+Break and the child tears itself
    down properly. A tray host has no console (`_have_console()`), so the same
    stop is a `TerminateProcess`, the child runs no teardown, and the journal
    entry -- and the cloak -- outlive the bridge that owned them.

    That leftover is not cosmetic. A cloaked pad cannot be enumerated, so it
    cannot be seen to come back: the user powers the controller on again and
    nothing happens, for the rest of the session, because `hidhide.sweep()`
    only runs once per process start. So the parent repays the debt itself
    after every stop. Idempotent and cheap -- no record means no work, which is
    the normal case on the Ctrl+Break path.
    """
    try:
        from . import hidhide as HH

        return HH.unhide_for_bridge(serial, cli_override=cli_override,
                                    log_fn=log_fn)
    except Exception:  # noqa: BLE001
        log.debug("could not unhide %s", serial, exc_info=True)
        return False


def _adopt_record(serial: str) -> bool:
    """Keep one controller's cloak alive across a disconnect. Never raises.

    The other verb a stopping bridge can apply to its hide debt. A DISCONNECT
    teardown -- the pad was switched off, and this manager will re-bridge it
    the moment it returns -- must NOT unhide: the pad would re-enumerate
    visible, and a game that is already running grabs the raw Bluetooth device
    in the seconds before the next bridge re-hides it. That ghost handle is a
    controller slot the game never gives back (docs/wired-gap-findings.md,
    symptom 4: the re-bridged pad came back as player 3 with no rumble).

    So the cloak is kept, and the journal record is re-owned by THIS process
    (`hidhide.adopt_record`) so that no sweep -- not the next child's startup
    sweep, not a sibling's -- reads the dead child's pid as an abandoned hide.
    The debt is still repaid on every deliberate path: a user Stop, the hide
    toggle, Quit and the exit sweep all unhide exactly as before.
    """
    try:
        from . import hidhide as HH

        return HH.adopt_record(serial)
    except Exception:  # noqa: BLE001
        log.debug("could not adopt the hide record for %s", serial,
                  exc_info=True)
        return False


def _prehide_serial(serial: str, cli_override: str | None = None,
                    log_fn=None) -> list[str]:
    """Hide one controller from THIS process, before its child even spawns.

    `BridgeService` hides too (it must -- a bare `ds5bridge run` has no
    manager), but a child process takes seconds to spawn, import and attach,
    and for all of them a freshly appeared pad used to sit visible next to a
    running game. Hiding at DETECTION time -- the moment the hotplug pass
    decides to start a bridge -- closes most of that window, and it also
    re-applies the cloak to a devnode whose instance path churned across the
    reconnect (HidHide matches by instance path; `hide_for_bridge` resolves
    fresh every time). Idempotent: the child's own hide then finds everything
    already in the blacklist and merely re-owns the journal record.
    """
    try:
        from . import hidhide as HH

        # allow_restart: no child exists yet, so nothing of ours holds the
        # pad open, and this is the one moment a device restart -- what makes
        # HidHide's filter join a pad that was paired before HidHide was
        # installed -- costs nobody a handle. The child's own hide, and the
        # toggle on a running bridge, never restart.
        return HH.hide_for_bridge(serial, cli_override=cli_override,
                                  log_fn=log_fn, allow_restart=True)
    except Exception:  # noqa: BLE001
        log.debug("could not pre-hide %s", serial, exc_info=True)
        return []


def _sweep_hidhide(log_fn=None) -> int:
    """Unhide anything whose owning process is gone. Never raises. -> how many.

    The last thing that happens on the way out, and it is not the same promise
    as `stop()`'s unhide. A child that was hard-killed never ran its own
    teardown; a bridge whose manager entry had already been dropped (a crash the
    poll noticed, a start abandoned mid-flight) has nobody left to speak for it.
    `sweep()` is keyed on the journal on disk rather than on what this object
    remembers, so it catches both.

    NOT forced: `owner_alive()` is respected, so a `ds5bridge run` bridging in
    another terminal keeps its own cloak. This process quitting is not a reason
    to unhide somebody else's controller.
    """
    try:
        from . import hidhide as HH

        n = HH.sweep(log_fn=log_fn)
        if n:
            log.info("unhid %d controller(s) on the way out", n)
        return n
    except Exception:  # noqa: BLE001
        log.debug("the exit sweep failed", exc_info=True)
        return 0


def _open_usbip(exe: str | None = None):
    """A `Usbip` for the reconciliation, or None when usbip-win2 is not there.

    None rather than an exception: startup reconciliation is best-effort by
    definition, and a machine without usbip-win2 has no attached devices to
    reconcile. The user-facing "install usbip-win2" message belongs to a bridge
    that is trying to start, not to a housekeeping pass.
    """
    try:
        from .usbip import Usbip

        return Usbip(exe)
    except Exception:  # noqa: BLE001
        log.debug("usbip.exe is not available for the reconciliation",
                  exc_info=True)
        return None


#: Hosts a usbip URL may name for a device THIS machine is serving. Anything
#: else is a real remote server and none of our business.
_LOOPBACK = ("127.0.0.1", "localhost", "::1", "[::1]")


def default_child_command(serial: str, port: int, usbip_exe: str | None = None,
                          audio_target: str = "speaker",
                          status_every: float = 2.0,
                          hide_bluetooth: bool = False,
                          hidhide_cli: str | None = None,
                          telemetry_port: int | None = None) -> list[str]:
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
    # Where the child should fire its live-input datagrams (telemetry.py).
    # The parent's hub owns the port; a child with no flag publishes nothing.
    if telemetry_port:
        args += ["--telemetry-port", str(telemetry_port)]
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
                 usbip_exe: str | None = None, cleanup_fn=None,
                 on_change=None):
        self.serial = serial.lower()
        self.port = port
        self.command = list(command)
        self.job = job
        self.usbip_exe = usbip_exe
        self.on_event = on_event or (lambda serial, kind, text: None)
        #: Called whenever the child reports a DIFFERENT state, and when it
        #: exits. The manager uses it to wake its watcher: a controller that was
        #: switched off should not wait out a poll interval before anybody
        #: starts counting, and the interval is time a game spends being fed
        #: neutral input by a pad that is not there.
        self.on_change = on_change or (lambda serial: None)
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
                "last_event": "", "offline_since": None}

    # -- state, and who is told about it -----------------------------------

    def _note_state(self, state: str) -> bool:
        """Record a state the child just reported. -> did it CHANGE.

        Call with `self._lock` held.

        `offline_since` is stamped the moment the child FIRST says the
        Bluetooth link is down, and cleared by anything else it says. That
        timestamp -- not the poll that happens to notice it -- is what the
        manager's offline grace is measured from. Measuring from the poll adds a
        whole hotplug interval to every disconnect, on top of the interval the
        child's own status tick already costs, and all of it is time in which
        Windows still shows a game a controller that is switched off.
        """
        prev = self._snap["state"]
        self._snap["state"] = state
        if state == DEGRADED:
            if self._snap.get("offline_since") is None:
                self._snap["offline_since"] = time.monotonic()
        else:
            self._snap["offline_since"] = None
        return state != prev

    def _changed(self) -> None:
        try:
            self.on_change(self.serial)
        except Exception:  # noqa: BLE001
            log.exception("state-change handler failed")

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

    def stop(self, hard: bool = False) -> None:
        """Ask, kill, then clean up THIS port -- in that order, always.

        `hard=True` skips the asking. It is for the one case where the polite
        path buys nothing and costs everything: the controller is GONE, so the
        graceful shutdown the child would run has no controller to disarm, no
        microphone to quieten and nothing to unhide that this process is not
        about to repay anyway -- while `stop_timeout` seconds of waiting for it
        are seconds in which a game still sees a wired DualSense that cannot
        answer. Disconnect teardown passes it; a user asking for Stop does not.

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
            if hard or not self._ctrl_break(p):
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
            changed = self._note_state(ERROR)
            self._snap["error"] = text
        if changed:
            self._changed()

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
                        changed = self._note_state(m.group("state").strip())
                        self._snap.update(
                            battery_percent=None if bat == "?" else int(bat),
                            reports_per_s=float(m.group("rps")),
                            uptime_s=int(m.group("up")),
                            attached=True)
                    if changed:
                        self._changed()
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
                        changed = self._note_state(RUNNING)
                        self._snap.update(attached=True, error=None)
                    if changed:
                        self._changed()
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
                        self._note_state(ERROR)
                        self._snap["error"] = (
                            self._snap["error"]
                            or (f"the bridge exited with code {code}"
                                + (f": {last}" if last else "")))
                    else:
                        self._note_state(STOPPED)
                    self._snap["attached"] = False
                    self._snap["reports_per_s"] = 0.0
            # Unconditionally, not only on a change: a child that has EXITED is
            # news the manager must act on within a tick, and it is the one
            # transition that can arrive with the state already looking right.
            self._changed()

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
                 offline_grace: float = 12.0,
                 gone_grace: float = 3.0,
                 retry_after: float = 60.0,
                 usbip_exe: str | None = None,
                 audio_target: str = "speaker",
                 hide_bluetooth: dict | None = None,
                 hide_default: bool = False,
                 hidhide_cli: str | None = None,
                 telemetry_port: int | None = None,
                 child_command=None,
                 discover=None,
                 bridge_factory=None,
                 port_free=None,
                 hidden_serials=None,
                 unhide_serial=None,
                 adopt_record=None,
                 prehide_serial=None,
                 sweep_fn=None,
                 usbip_factory=None,
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
        #: How long a child may keep saying "controller offline" before its
        #: bridge is torn down.
        #:
        #: Shorter than `vanish_grace` on purpose, because by the time this
        #: clock starts the child has ALREADY waited. `bridge.LINK_DEAD_S` is
        #: 4 s of silence before `connected` is cleared at all, and the reader
        #: then retries `_open_device()` every 2 s for as long as it takes. So
        #: this grace is not "how long might a healthy pad be quiet" -- the
        #: watchdog has answered that -- it is "how many reconnect attempts
        #: deserve to be waited out", and twelve seconds is about six of them.
        #:
        #: The cost of being generous here is not neutral, which is what the
        #: first version of this got wrong at 30 s: for all of it, the tray says
        #: the controller is bridged, a virtual pad Windows can see stays
        #: attached to a radio link that is gone, and the user -- who switched
        #: the controller off themselves and knows perfectly well what happened
        #: -- is left clicking things to make the program agree with them.
        self.offline_grace = offline_grace
        #: The same clock, for when TWO independent witnesses agree the pad is
        #: gone: the child says the Bluetooth link is dead AND the controller is
        #: no longer enumerated at all.
        #:
        #: `offline_grace` is generous because one witness can be wrong -- a
        #: healthy pad goes quiet for four seconds and comes back (measured
        #: 2026-08-25, mid-soak, both controllers), and tearing down on that
        #: turns a recoverable dropout into a device removal Windows shows the
        #: user. Two witnesses being wrong at once is a different proposition:
        #: a dropout does not remove a device from the HID device set, and a
        #: pad that is neither answering nor enumerated has been switched off.
        #:
        #: So this is a settling time, not a wait-and-see: long enough that the
        #: two observations are not read from different instants, short enough
        #: that switching a controller off during a game does what the user
        #: expects it to do -- the controller disconnects.
        self.gone_grace = gone_grace
        self.retry_after = retry_after
        self.usbip_exe = usbip_exe
        self.audio_target = audio_target
        self.hide_default = bool(hide_default)
        self.hidhide_cli = hidhide_cli
        #: Handed to every child as `--telemetry-port` so the dashboard's hub
        #: hears all of them. None -- no dashboard -- means no flag and no
        #: telemetry, which is also what every existing test constructs.
        self.telemetry_port = telemetry_port
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
                hidhide_cli=self.hidhide_cli,
                telemetry_port=self.telemetry_port))
        self._discover = discover or enumerate_serials
        self._bridge_factory = bridge_factory or ChildBridge
        self._port_free = port_free or S.port_free
        #: Injected so the oscillation guard in `poll_once` is testable without
        #: a driver. Returns the serials WE currently have hidden.
        self._hidden_serials = hidden_serials or _hidden_serials
        #: The other half of that seam: a test must never unhide a controller
        #: its developer genuinely had hidden.
        self._unhide_serial = unhide_serial or (
            lambda serial: _unhide_serial(
                serial, cli_override=self.hidhide_cli,
                log_fn=lambda t: self._emit(serial, "info", t)))
        #: What a DISCONNECT teardown does instead of unhiding: keep the cloak
        #: and re-own its journal record, so the pad comes back already hidden
        #: and a running game never sees the raw Bluetooth device. Injected for
        #: the same reason as `unhide_serial`.
        self._adopt_record = adopt_record or _adopt_record
        #: Hiding at detection time, before the child spawns. Injected so a
        #: test run never cloaks the developer's own controller.
        self._prehide_serial = prehide_serial or (
            lambda serial: _prehide_serial(
                serial, cli_override=self.hidhide_cli,
                log_fn=lambda t: self._emit(serial, "info", t)))
        #: The last-resort unhide, run once on the way out with everything
        #: already stopped. Injected for the same reason as the two above: a
        #: green test run must never unhide the developer's own controller.
        self._sweep = sweep_fn or _sweep_hidhide
        #: How `reconcile_stale()` reaches usbip. Injected so the startup
        #: reconciliation can be tested with no driver installed.
        self._usbip_factory = usbip_factory or (
            lambda: _open_usbip(self.usbip_exe))

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
        #: When a bridge's child first said "controller offline", cleared the
        #: moment it says anything else. This is the only disconnect signal that
        #: survives cloaking, because it needs no enumeration -- see `poll_once`.
        #: Normally a copy of the child's own `offline_since`, which is stamped
        #: when the child says it rather than when a poll notices.
        self._offline_since: dict[str, float] = {}
        #: What the last pass ENUMERATED (not the vouched-for union). Its only
        #: job is to spot the absent -> present edge, which is the moment a
        #: retry backoff stops being justified: see `poll_once`.
        self._seen_last: set = set()
        #: Serials whose HidHide cloak THIS manager kept across a disconnect
        #: (`stop(keep_cloak=True)`). A kept cloak has no bridge and no child,
        #: so nothing else will repay it if the user then turns bridging or
        #: hiding OFF for that pad -- the deliberate-release paths all used to
        #: stop at "nothing is running". Tracked in memory, deliberately NOT
        #: read from the journal: the journal also lists cloaks owned by OTHER
        #: live processes (a `ds5bridge run` in a terminal), which this
        #: manager must never unhide.
        self._kept_cloaks: set = set()
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
        #: Set once teardown has begun. Nothing may start a bridge after this,
        #: including the watcher thread, which is otherwise perfectly capable of
        #: spawning a child while `close()` is stopping its siblings -- and a
        #: child spawned during teardown is a child nobody will ever stop.
        self._closing = False

        self._job = create_kill_on_close_job() if use_job else None
        self._watch: threading.Thread | None = None
        self._watch_stop = threading.Event()
        #: "Look now." Set by a child changing state or exiting, so a disconnect
        #: is acted on when it happens rather than at the next tick of a
        #: five-second timer.
        self._wake = threading.Event()

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
            # The stop above unhides only what it stopped. A cloak KEPT across
            # a disconnect has no bridge, and disabling the controller is the
            # user saying it will not be re-bridged -- the one promise the
            # kept cloak was resting on.
            self._release_kept_cloak(serial)

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
            if not hide:
                # A cloak can outlive its bridge now (kept across a
                # disconnect). Turning the hide toggle OFF must release it
                # even with nothing running, or the user's "stop hiding this
                # pad" quietly waits for the next app exit.
                self._release_kept_cloak(serial)
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
            # Kept cloaks belong to pads with no bridge, which `stop_all()`
            # therefore never visits. The master switch is "stop bridging,
            # full stop", so they are released too.
            with self._lock:
                kept = list(self._kept_cloaks)
            for serial in kept:
                self._release_kept_cloak(serial)

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
            if not self.master_enabled or self._closing:
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
            # Hide FIRST, before the child process even exists. The child hides
            # too (a bare `ds5bridge run` has no manager), but it takes seconds
            # to spawn and attach, and a game that is already running grabs a
            # visible pad in far less -- the ghost-slot bug of symptom 4. This
            # is also what re-applies the cloak when the instance path churned
            # across a reconnect: `hide_for_bridge` resolves the serial fresh.
            if self.is_hiding(serial):
                try:
                    self._prehide_serial(serial)
                except Exception:  # noqa: BLE001
                    log.exception("pre-hiding %s failed", serial)
            port = self.port_for(serial)
            b = self._bridge_factory(serial, port,
                                     self._child_command(serial, port),
                                     job=self._job,
                                     usbip_exe=self.usbip_exe,
                                     on_event=self._emit,
                                     on_change=self._child_changed)
            with self._lock:
                self._bridges[serial] = b
                # The new child re-hides and re-owns the journal record; the
                # cloak is no longer this manager's to keep or release.
                self._kept_cloaks.discard(serial)
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
        # DID ANYBODY STILL WANT THIS? `start()` blocks for as long as the child
        # takes to attach -- up to a minute for a frozen build -- and Quit, the
        # master switch, a per-controller Stop and a disconnect teardown can all
        # land inside that window. Every one of them looked at `self._bridges`,
        # found this serial (it is registered before the wait) and stopped it;
        # what they could not do is stop a child that had not attached yet. So
        # the arrival check is here, at the one point where both facts are
        # known, and a bridge nobody wants is torn straight back down instead of
        # being left attached to Windows with no owner.
        with self._lock:
            abandoned = (self._closing or serial in self._held
                         or self._bridges.get(serial) is not b)
            if not abandoned:
                self._retry_at.pop(serial, None)
        if abandoned:
            with self._lock:
                if self._bridges.get(serial) is b:
                    del self._bridges[serial]
            self._emit(serial, "info",
                       "the bridge attached after it had been stopped -- "
                       "tearing it down again")
            try:
                b.stop(hard=True)
            except Exception:  # noqa: BLE001
                log.exception("tearing down an abandoned start of %s", serial)
            try:
                self._unhide_serial(serial)
            except Exception:  # noqa: BLE001
                log.exception("unhiding %s after an abandoned start", serial)
            return False
        return True

    def _record_failure(self, serial: str, text: str) -> None:
        with self._lock:
            self._errors[serial] = text
            self._retry_at[serial] = time.monotonic() + self.retry_after
            b = self._bridges.pop(serial, None)
            # The unhide below repays everything, kept cloaks included.
            self._kept_cloaks.discard(serial)
        self._emit(serial, "error", text)
        if b is not None:
            try:
                b.stop()          # tears down only this serial's port
            except Exception:  # noqa: BLE001
                log.exception("cleaning up a failed start of %s", serial)
        # Every path that drops a bridge repays the hide debt, without deciding
        # for itself whether there is one -- the journal on disk knows, and no
        # record means no work. A start that got far enough to hide and then
        # failed is rare; a controller nobody can open afterwards is not a rare
        # enough consequence to leave to a rule of thumb.
        try:
            self._unhide_serial(serial)
        except Exception:  # noqa: BLE001
            log.exception("unhiding %s after a failed start", serial)

    def stop(self, serial: str, hard: bool = False,
             keep_cloak: bool = False) -> None:
        """Stop one bridge and put its controller back. Idempotent.

        `hard` goes straight through to `ChildBridge.stop()`: skip the polite
        Ctrl+Break for a controller that is already gone. See there.

        `keep_cloak=True` is the disconnect variant (symptom 4): the pad was
        switched off and this manager will re-bridge it when it returns, so the
        HidHide cloak is KEPT -- the pad re-enumerates hidden, a running game
        never sees the raw Bluetooth device, and no ghost controller slot is
        created. The journal record is re-owned by this process so no startup
        sweep mistakes the kept cloak for an abandoned one; every deliberate
        stop (tray Stop, hide toggle, master switch, Quit) still unhides, and
        the exit sweep repays whatever is left. Callers passing it should pass
        `hard=True` too: a kept cloak only stays kept if the child never runs
        its own graceful teardown, whose unhide is unconditional.
        """
        serial = serial.lower()
        with self._lock:
            b = self._bridges.pop(serial, None)
            self._missing_since.pop(serial, None)
            self._offline_since.pop(serial, None)
            # Under the SAME lock as the pop. A hotplug pass that saw the bridge
            # gone but not the hold would spawn a replacement while this one was
            # still tearing down, and the replacement dies on the port's named
            # mutex ("ds5bridge is already running").
            self._held.add(serial)
        if b is None:
            return                                    # idempotent by design
        try:
            b.stop(hard=hard)
        except Exception:  # noqa: BLE001
            log.exception("stopping %s failed", serial)
        if keep_cloak:
            with self._lock:
                self._kept_cloaks.add(serial)
            try:
                self._adopt_record(serial)
            except Exception:  # noqa: BLE001
                log.exception("keeping the cloak of %s failed", serial)
            return
        with self._lock:
            self._kept_cloaks.discard(serial)
        # Belt and braces for the child's own unhide, which a hard kill never
        # gets to run. `_unhide_serial` says why leaving that debt unpaid costs
        # the user their controller for the rest of the session.
        try:
            self._unhide_serial(serial)
        except Exception:  # noqa: BLE001
            log.exception("unhiding %s after its stop failed", serial)

    def _release_kept_cloak(self, serial: str) -> None:
        """Unhide a cloak this manager kept across a disconnect, if it did.

        The deliberate-release half of `keep_cloak`. A kept cloak belongs to a
        pad with NO running bridge, so `stop()` -- which every user-facing off
        switch goes through -- finds nothing to stop and used to return before
        its unhide. This is called by exactly those switches (per-controller
        disable, the master switch, the hide toggle) so that "stop bridging
        this pad" gives the pad back immediately rather than at app exit. A
        serial never kept is a no-op, which is the common case.
        """
        serial = serial.lower()
        with self._lock:
            kept = serial in self._kept_cloaks
            self._kept_cloaks.discard(serial)
        if not kept:
            return
        try:
            self._unhide_serial(serial)
        except Exception:  # noqa: BLE001
            log.exception("releasing the kept cloak of %s failed", serial)

    def _stop_offline(self, serial: str, now: float) -> None:
        """Stop a bridge whose controller stopped answering, without holding it.

        `stop()` records a hold, and a hold means "the user asked for this, so
        hotplug must not undo it" -- it is what makes Stop in the tray menu
        stick. This stop is hotplug's own decision, and a hold here would be a
        promise never to bridge that controller again until somebody clicked
        Start: the hold only lifts when the serial leaves the present set, and a
        pad that is switched off but still enumerated (a DualSense charging on a
        cable does exactly that -- `enumerate_serials()`) never leaves it.

        So the hold comes straight back off and the retry window takes its
        place. A controller that is genuinely off has by now been unhidden and
        is not enumerated either, so nothing tries to start it at all; one that
        merely enumerates while its radio is silent is retried once per
        `retry_after` rather than on every five-second pass, which is the same
        bargain `_record_failure` already strikes for a start that fails.

        Hard, and that is the point of the whole path: the reason this bridge is
        coming down is that its controller is not there, so there is nothing
        left for a graceful child shutdown to do gracefully -- and on a console
        host the polite route waits up to `stop_timeout` seconds for a teardown
        whose only remaining work is the teardown we are doing anyway. Those
        seconds are seconds in which a game still sees a wired DualSense on the
        end of a dead radio link.

        And it KEEPS THE CLOAK whenever this manager would re-bridge the pad on
        its return (symptom 4). Unhiding here is what handed a running game the
        raw Bluetooth device for the few seconds between the pad powering back
        on and the new bridge's hide -- a ghost handle HidHide cannot sever,
        a controller slot the game never gives back, and a re-bridged pad that
        came back as player 3 with its rumble routed at a ghost. `hard=True`
        is part of the same promise: the child is terminated, never asked, so
        its own unconditional unhide cannot run either.
        """
        keep = self._would_rebridge(serial)
        self.stop(serial, hard=True, keep_cloak=keep)
        with self._lock:
            self._held.discard(serial)
            self._retry_at[serial] = now + self.retry_after

    def _would_rebridge(self, serial: str) -> bool:
        """Would a returning `serial` be bridged again without user action?

        The condition under which a disconnect teardown keeps the HidHide cloak
        rather than unhiding. If any part of it is false -- the app is closing,
        the master switch is off, the controller is disabled -- nothing would
        re-hide the pad later, so keeping the cloak would strand it and the
        stop unhides exactly as it always did.
        """
        with self._lock:
            if self._closing or not self.master_enabled:
                return False
        return self.is_enabled(serial)

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
        """Enumerate, and notice anything that has just COME BACK.

        The reappearance half lives here rather than in `poll_once` because it
        is a property of observing enumeration, and `start_all()` observes it
        too. It is rule 6 in `poll_once`: a serial that left the enumeration and
        returned has been switched off and on again, which is the most explicit
        "try this one again" a user can give without opening a menu -- so the
        retry backoff a teardown or a failed start left behind is dropped, along
        with the error it was recorded with.

        A backoff must survive the case it exists for, which is a pad that is
        switched off but STILL enumerated -- a DualSense charging on a cable
        does exactly that. Such a serial never leaves the set, so it never
        returns to it, so nothing here touches its backoff.
        """
        try:
            present = [s.lower() for s in self._discover()]
        except Exception:  # noqa: BLE001
            log.exception("controller discovery failed")
            return list(self._present)
        seen = set(present)
        with self._lock:
            self._present = present
            returned = seen - self._seen_last
            self._seen_last = seen
            for serial in returned:
                self._retry_at.pop(serial, None)
                self._errors.pop(serial, None)
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
        4. **The journal says we hid it, not that it is switched on.** Point 3
           on its own is how a hidden controller became impossible to unplug:
           the journal entry stands whether or not the pad is powered, so a
           cloaked pad that was switched off stayed in the present set forever,
           the absence clock in point 2 never started, and the virtual device
           sat attached to Windows on the end of a dead Bluetooth link while the
           tray cheerfully reported it running.

           The child knows better than either the journal or enumeration.
           `BridgeService.snapshot()` turns the backend's own
           `device_status()["connected"]` into `DEGRADED`, that reaches us in
           the status line, and it needs no enumeration at all -- which is
           exactly why it still works while the pad is cloaked. So a child
           saying DEGRADED withdraws the journal's vouching for its own serial,
           and after `offline_grace` seconds of it the bridge comes down.

           Both halves of point 3 survive: a hidden pad whose child says RUNNING
           is still present, so hiding one still cannot start the ~40 s flap.
        5. **Two witnesses beat one.** A child saying DEGRADED is one witness
           and it is treated cautiously (point 4) because a healthy pad really
           does go quiet for a few seconds. Enumeration is another, and a
           dropout does NOT remove a device from the HID device set. When both
           agree -- the link is dead AND the controller is not listed -- the pad
           has been switched off, and the wait drops from `offline_grace` to
           `gone_grace`. That is the difference between a game seeing the
           controller disconnect when the user switches it off and a game seeing
           it twenty seconds later.
        6. **A controller that comes BACK is new information.** A teardown
           leaves a retry window behind so a pad that enumerates while its radio
           is silent (a DualSense charging on a cable) cannot become a
           start/tear-down loop. That reasoning stops applying the instant the
           serial leaves enumeration and returns: somebody switched the
           controller off and on again, which is the most explicit "try again"
           a user can give without opening a menu. So the absent -> present edge
           clears the backoff and the error with it.
        """
        now = time.monotonic()
        with self._lock:
            if self._closing:
                return
            running = list(self._bridges.items())
            # In-flight starts are NOT part of this pass. Their bridge object is
            # registered before `wait_ready()` returns, so a pass that landed in
            # the middle of one used to find a child with no pid yet, read that
            # as "the bridge process exited" and tear down a bridge that was
            # coming up perfectly well.
            inflight = set(self._inflight)
        # One snapshot per child per pass, reused below: it is a dict copy under
        # the child's lock, and taking it twice is two chances to see the state
        # change halfway through a decision.
        snaps = {serial: b.snapshot() for serial, b in running}

        enumerated = self._refresh_present()
        seen = set(enumerated)
        try:
            hidden = list(dict.fromkeys(s.lower()
                                        for s in self._hidden_serials()))
        except Exception:  # noqa: BLE001
            log.debug("hidden-serial union failed", exc_info=True)
            hidden = []
        vouched = [s for s in hidden
                   if s not in seen and snaps.get(s, {}).get("state") != DEGRADED]
        # What the tray counts. Written back so `snapshot()` -- which must not
        # do any work of its own -- reads a present list that a cloaked pad is
        # in, rather than the enumeration that structurally cannot list it.
        present_list = enumerated + vouched
        present = set(present_list)
        with self._lock:
            # Rule 6's edge was computed inside `_refresh_present`, on the RAW
            # enumeration -- deliberately before the vouched-for union is added
            # here, or a pad we cloaked would look like it had just been plugged
            # in every time the journal spoke up for it.
            self._present = present_list

        for serial, b in running:
            if serial in inflight:
                continue
            if snaps[serial].get("state") == DEGRADED:
                # The child's own timestamp when it has one: it was stamped when
                # the link died, not when this pass happened to look.
                stamped = snaps[serial].get("offline_since")
                with self._lock:
                    if stamped is None:
                        first = self._offline_since.setdefault(serial, now)
                    else:
                        first = self._offline_since[serial] = stamped
                gone = serial not in seen
                # `min`, not just `gone_grace`: two witnesses agreeing may
                # shorten the wait and must never lengthen it, whatever a
                # caller has set the two graces to.
                grace = (min(self.gone_grace, self.offline_grace) if gone
                         else self.offline_grace)
                if now - first >= grace:
                    self._emit(serial, "warn",
                               ("the controller is offline and no longer "
                                "connected -- switched off, or out of range"
                                if gone else
                                f"the controller has been offline for "
                                f"{grace:.0f}s")
                               + " -- stopping its bridge")
                    self._stop_offline(serial, now)
                    continue
            else:
                with self._lock:
                    self._offline_since.pop(serial, None)

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
                    # A disconnect-shaped teardown, same as `_stop_offline`:
                    # the pad is gone, so the stop is hard (nothing graceful
                    # left to do, and the child's own unhide must not run) and
                    # the cloak is kept for the pad's return. The hold `stop()`
                    # records is cleared by `_held &= present` below, exactly
                    # as before.
                    self.stop(serial, hard=True,
                              keep_cloak=self._would_rebridge(serial))
                    continue
            # A child that died on its own -- crash, or the user killed it.
            # Its siblings are untouched; only this serial is cleaned up.
            if not b.alive:
                # Freshly, not from `snaps`: the child may have died since, and
                # the error it wrote on its way out is the one worth reporting.
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

        if not self.master_enabled or self._closing:
            return
        # ENUMERATED serials only, never the vouched-for union. The union
        # exists so a cloaked pad is not declared gone (rule 3); it must not
        # also mean "try to bridge it". With the cloak now KEPT across a
        # disconnect (symptom 4), a powered-off pad keeps a journal record and
        # would otherwise be retried every `retry_after` -- and every failed
        # start unhides, handing a running game the raw device the kept cloak
        # exists to withhold. A pad that can actually be bridged is one the
        # (whitelisted) enumeration can see, cloaked or not.
        for serial in enumerated:
            with self._lock:
                if self._closing:
                    return
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

        The FIRST pass runs immediately, and the interval only separates the
        passes after it. Waiting first is the obvious way to write this loop and
        it is wrong in the one case that matters most: starting the program with
        a controller already switched on. Nothing happens for five seconds, the
        reasonable conclusion is that it did not see the controller, and the
        user goes looking for the button that makes it look -- which teaches
        them that hotplug needs a click.

        The interval is a CEILING, not a metronome. Two things shorten it:
        `_child_changed` sets `_wake` the moment a child reports a different
        state or exits, and `_next_interval()` shortens the sleep to whatever is
        left on a running offline clock. Together they take the app-layer cost
        of noticing a disconnect from "up to one interval, plus up to another
        one before the grace is judged" down to about a tenth of a second --
        which matters because every one of those seconds is a game holding a
        controller that is not there.
        """
        if self._watch is not None:
            return
        self._watch_stop.clear()

        def loop() -> None:
            while True:
                # Cleared BEFORE the pass, never after: a child that changes
                # state while the pass is running has news this pass may
                # already have missed, and clearing afterwards would throw it
                # away for a whole interval.
                self._wake.clear()
                try:
                    self.poll_once()
                except Exception:  # noqa: BLE001
                    log.exception("hotplug pass failed")
                if self._watch_stop.is_set():
                    return
                self._wake.wait(self._next_interval())
                if self._watch_stop.is_set():
                    return

        self._watch = threading.Thread(target=loop, name="ds5-hotplug",
                                       daemon=True)
        self._watch.start()

    def _child_changed(self, serial: str) -> None:
        """A child said something new. Look now rather than at the next tick."""
        self._wake.set()

    def _next_interval(self) -> float:
        """How long the watcher may sleep before it MUST look again.

        The poll interval, unless a bridge is sitting on an offline clock -- in
        which case it is whatever is left of the shortest grace, so the teardown
        lands within a tenth of a second of the grace expiring instead of up to
        a full interval later. `gone_grace` is used as the deadline even for a
        controller that is still enumerated: waking early costs one enumeration
        and the pass simply decides not to act yet.
        """
        now = time.monotonic()
        with self._lock:
            starts = list(self._offline_since.values())
        soonest = self.hotplug_interval
        for first in starts:
            soonest = min(soonest, first + self.gone_grace - now)
        # Never a hot loop, and never longer than asked for.
        return max(0.1, min(self.hotplug_interval, soonest))

    def stop_hotplug(self) -> None:
        self._watch_stop.set()
        self._wake.set()
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

        Which is why presence is decided here from what is already in memory,
        and never by asking anything. `self._present` is what the last hotplug
        pass worked out (enumeration plus the cloaked pads it vouched for), and
        a bridge in one of the `_HOLDING` states adds its own serial regardless:
        it has the controller open, so the controller is there. Before that, a
        cloaked pad counted as present inside `poll_once` and as absent in the
        tray, which is where "2 of 1 controllers bridged" came from -- and the
        same arithmetic rendered a perfectly healthy hidden controller as "not
        connected" in the menu. Counting the holders makes `running` a subset of
        `present` by construction rather than by luck.
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
        running = degraded = attached = 0
        total_rate = 0.0
        present_set = set(present)
        for serial, b in bridges:
            snap = b.snapshot()
            snap["enabled"] = enabled.get(serial, default_enabled)
            snap["hide_bluetooth"] = hide.get(serial, hide_default)
            # Whether that hide actually bites (hidhide.hide_status): the
            # filter check the pre-hide ran in this process. Only meaningful
            # while hiding is on; a dict lookup either way.
            snap.update(_hide_status(serial) if snap["hide_bluetooth"]
                        else {"hide_effective": None, "hide_note": ""})
            snap["present"] = (serial in present_set
                               or snap["state"] in _HOLDING)
            controllers[serial] = snap
            if snap["state"] in (RUNNING, DEGRADED):
                running += 1
            if snap["state"] == DEGRADED:
                degraded += 1
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
                    **(_hide_status(serial) if hide.get(serial, hide_default)
                       else {"hide_effective": None, "hide_note": ""}),
                    "present": True}
        return {
            "master_enabled": master,
            "controllers": controllers,
            "aggregate": {
                "present": sum(1 for c in controllers.values()
                               if c.get("present")),
                "bridges": len(bridges),
                "running": running,
                # A subset of `running`, not a rival to it: the virtual device
                # really is still attached and a game really is still being fed
                # (neutralised) input, which is what `running` claims. What it
                # cannot say on its own is that the Bluetooth link behind one of
                # them is gone -- and a count of "2 of 2 bridged" while a
                # controller sits switched off on the desk is how a user learns
                # not to believe the tray.
                "degraded": degraded,
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
        """What Ctrl+C, a closed console, a logoff and `atexit` all reach.

        Identical to `close()` except that it keeps the job object: the hook can
        run while the process is on its way out through a path that will not
        come back here, and the job is the last-resort kill for any child that
        outlived its stop. Closing it early would only remove that safety net a
        few milliseconds sooner.
        """
        try:
            self._begin_close()
            self.stop_all()
            self._final_sweep()
        except Exception:  # noqa: BLE001
            log.exception("manager teardown failed")

    def _begin_close(self) -> None:
        """Shut the door before emptying the room. Idempotent.

        The flag goes up FIRST and the watcher is stopped SECOND, in that order
        and never the other way round: between the two, the watcher may be
        halfway through a pass, and `_closing` is what stops that pass from
        starting a bridge nobody will ever be around to stop. A `stop_hotplug()`
        on its own cannot -- it can only wait for a pass that may currently be
        blocked in `start()` waiting up to a minute for a child to attach.
        """
        with self._lock:
            self._closing = True
        self._wake.set()
        self.stop_hotplug()

    def _final_sweep(self) -> None:
        """Repay any hide debt nothing else did, once everything is stopped.

        Deliberately last. Every bridge has been stopped by now, so every record
        that still exists belongs to a child that died without unhiding -- which
        is exactly the set `sweep()`'s liveness check will clear and the set a
        `stop()` could not know about.
        """
        try:
            self._sweep()
        except Exception:  # noqa: BLE001
            log.exception("the exit sweep failed")

    def close(self) -> None:
        """Stop everything and let go of the job object. Safe twice.

        The hook has to come off `service.ON_TEARDOWN`: it is a module-level
        list, so a manager that is closed and replaced would otherwise leave a
        dead one behind to be called at exit.
        """
        self._begin_close()
        self.stop_all()
        try:
            S.ON_TEARDOWN.remove(self._hook)
        except ValueError:
            pass
        close_job(self._job)
        self._job = None
        self._final_sweep()

    # -- startup reconciliation --------------------------------------------

    def reconcile_stale(self, log_fn=None) -> int:
        """Undo what a previous run's death left on this machine. -> ports freed.

        Called once, at startup, BEFORE anything of ours goes near a port. It
        exists because two pieces of state outlive the process that created them
        and neither can be discovered by looking at this program's own memory:

        * **An armed auto-re-attach.** `usbip attach` arms a background
          re-attach that survives the death of everything on both sides of it,
          and it fires the instant ANY server listens on that port again.
          `attach -X` is the only thing that disarms it, and nothing about the
          state of the machine reveals that it is armed -- measured 2026-08-25
          (`BridgeService._start_inner` step 2): hard-kill a bridge and both
          `usbip port` and the TCP port come back clean while the re-attach is
          still waiting. So it is issued unconditionally.
        * **Attached virtual devices with nobody behind them.** A hard-killed
          bridge leaves Windows holding a wired DualSense whose server is gone.
          Games see a controller that answers nothing, and the user sees a
          device that no amount of restarting this program removes, because
          every start only ever cleaned up the ONE port it was about to use.

        What may be detached is deliberately narrow, and it is the same rule
        `usbip.our_ports()` exists for: a port whose URL says loopback AND whose
        TCP port has no listener. A live sibling -- another instance, a
        `ds5bridge run` in a terminal, this program's own child from a moment
        ago -- is holding its port bound, so it is never a candidate. A genuine
        remote USB/IP server is not loopback, so it is never a candidate either.
        """
        say = log_fn or (lambda t: log.info("%s", t))
        u = self._usbip_factory()
        if u is None:
            return 0
        try:
            u.stop_auto_reattach()
        except Exception:  # noqa: BLE001
            log.exception("attach -X during the startup reconciliation failed")
        freed = 0
        for port_no, tcp in self._zombie_ports(u):
            say(f"detaching a leftover virtual controller on usbip port "
                f"{port_no} -- nothing is serving TCP {tcp} any more")
            try:
                if u.detach(port_no).ok:
                    freed += 1
            except Exception:  # noqa: BLE001
                log.exception("detach -p %s failed", port_no)
        return freed

    def _zombie_ports(self, u) -> list[tuple[int, int]]:
        """[(usbip port, TCP port)] for attached devices with no live server."""
        out: list[tuple[int, int]] = []
        try:
            rows = u.parse_ports()
        except Exception:  # noqa: BLE001
            log.exception("could not read `usbip port`")
            return out
        for port_no, url in rows:
            host = (url or "").partition("/")[0]
            addr, _, tcp = host.rpartition(":")
            if not tcp.isdigit() or addr.lower() not in _LOOPBACK:
                continue
            try:
                if not self._port_free(int(tcp)):
                    continue          # somebody is still serving it
            except Exception:  # noqa: BLE001
                continue              # cannot tell -> leave it alone
            out.append((int(port_no), int(tcp)))
        return out
