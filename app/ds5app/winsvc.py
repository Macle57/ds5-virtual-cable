"""The Windows service -- a supervisor for the tray, and nothing else.

    sc query ds5bridge            the service `ds5bridge`, LocalSystem, automatic start
    ds5bridge.exe service         what the SCM runs: `run_service()` below
    ds5bridge-tray.exe --service-child
                                  the ONE child it keeps alive, in the console session

Why a service at all
--------------------
"Start with Windows" used to mean a scheduled task at logon. That is the
right answer for a program that only matters once somebody is sitting at the
PC, and the wrong one for a controller bridge: a game console does not ask you
to sign in to a desktop before the pad works, and neither should this. A
service starts when Windows boots, before the sign-in screen, and keeps
running across log-off and log-on. Chrome Remote Desktop's host works the same
way, for the same reason.

Why the service is only a supervisor
------------------------------------
Everything the program does -- the bridges, HidHide, the input engine's
SendInput, the tray icon, the balloons, the dashboard -- is in today's tray,
and half of it needs the interactive desktop: a tray icon exists only in a
session with a taskbar, SendInput reaches only the desktop of the session it
is called from, and a balloon is a Shell_NotifyIcon message to that taskbar.
Session 0, where a service lives, has none of that. So the service does one
thing: it launches `ds5bridge-tray.exe --service-child` in the ACTIVE CONSOLE
SESSION (`WTSGetActiveConsoleSessionId`), with its own LocalSystem token
duplicated and re-stamped with that session id (`SetTokenInformation(
TokenSessionId)`, which needs SeTcbPrivilege -- SYSTEM has it), on the
session's interactive desktop (`lpDesktop = winsta0\\default`), and keeps it
there: restarted when it exits (with a backoff so a crash loop cannot peg the
CPU), moved when the console session changes (fast user switching, a log-off
that tears the old session down), stopped when the service stops.

The child is SYSTEM in the user's session, not the user. That is deliberate:
the tray needs administrator rights (devnode restarts, HidHide), the user's
own token in that session is the FILTERED one under UAC, and there is nobody
to answer a prompt before sign-in. Consequences the child has to live with,
all handled in tray.py and config.py: `%APPDATA%` is SYSTEM's, so the config
dir is passed explicitly (`DS5_CONFIG=%ProgramData%\\ds5bridge`); the browser
must be opened with the user's token, not ours (`open_url_as_user`); and the
tray icon can only appear once explorer has a taskbar -- before sign-in the
child bridges pads with no icon and pystray re-adds the icon on the shell's
TaskbarCreated broadcast.

How the child is told to stop
-----------------------------
A named, manual-reset event (`Global\\ds5bridge-service-stop`), whose name the
child gets in `DS5_SERVICE_STOP_EVENT`. Window messages cannot cross sessions
and a `TerminateProcess` would skip the teardown that unhides the pads and
detaches the virtual ones, so the child waits on the event and runs its
normal Quit path when it fires. The supervisor gives it `CHILD_STOP_S` to do
so, reporting STOP_PENDING checkpoints to the SCM meanwhile, and terminates
it only after that.

The other direction, "Quit" in the tray menu: with a supervisor that restarts
every exit, the menu item would be a restart button. So the child exits with
`EXIT_STOP_SERVICE` when the USER asked it to quit (menu Quit, or the updater
handing off to the installer), and the supervisor stops the service instead of
restarting; every other exit code is a crash and is restarted.

Everything the SCM sees is in `run_service()`: `StartServiceCtrlDispatcherW`,
a `HandlerEx` that accepts stop, shutdown, pre-shutdown and session-change,
and `SetServiceStatus`. All ctypes, no pywin32 -- one more dependency in the
frozen build for three calls was not worth it. The supervisor itself
(`Supervisor`) is plain Python with every Win32 call behind an injectable
seam, so its policy -- backoff, session moves, the stop handshake -- is unit
tested without a service control manager anywhere near the tests.

`install`/`uninstall`/`start`/`stop`/`status` go through `sc.exe`, the same
tool a person would use, and so a `sc query ds5bridge` in a terminal shows
exactly what this module created.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time

log = logging.getLogger("ds5app.winsvc")

SERVICE_NAME = "ds5bridge"
DISPLAY_NAME = "ds5bridge"
DESCRIPTION = ("Presents Bluetooth DualSense controllers to games as wired "
               "USB pads. Starts before anyone signs in and keeps the "
               "ds5bridge tray running in the signed-in user's session; "
               "Quit in the tray menu stops this service.")

#: The flag the supervisor starts the tray with. The tray reads it.
CHILD_FLAG = "--service-child"
#: Environment variables the child receives from the supervisor.
STOP_EVENT_ENV = "DS5_SERVICE_STOP_EVENT"
STOP_EVENT_NAME = "Global\\ds5bridge-service-stop"
#: The child's exit code for "the user asked me to quit -- stop the service".
EXIT_STOP_SERVICE = 3

#: How long the child gets to tear down after the stop event before it is
#: terminated. Two pads: detach, unhide, devnode restarts -- measured well
#: under 20 s; this is the ceiling, not the expectation.
CHILD_STOP_S = 60.0
#: Restart backoff: first retry after this, doubling up to the maximum; reset
#: once a child has stayed up for `STABLE_S`.
BACKOFF_MIN_S = 2.0
BACKOFF_MAX_S = 60.0
STABLE_S = 300.0
#: How often the loop looks at the child and the console session.
POLL_S = 1.0
#: At boot the usbip driver may still be coming up when the service starts;
#: the first child is delayed until `usbip2_ude` reports RUNNING, at most this.
DRIVER_WAIT_S = 60.0
DRIVER_SERVICE = "usbip2_ude"

_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
_INVALID_SESSION = 0xFFFFFFFF


class ServiceError(RuntimeError):
    """A service operation failed. The message is user-facing text."""


# ---------------------------------------------------------------------------
# paths
# ---------------------------------------------------------------------------


def program_data_dir() -> str:
    """`%ProgramData%\\ds5bridge` -- the service child's home for config, the
    hide journal, the update cache and the logs. Machine-wide on purpose: a
    process that runs before anyone signs in has no user profile to keep its
    settings in, and the installer already uses this directory."""
    base = os.environ.get("ProgramData") or os.environ.get("ALLUSERSPROFILE") \
        or r"C:\ProgramData"
    return os.path.join(base, "ds5bridge")


def log_dir() -> str:
    return os.path.join(program_data_dir(), "logs")


def setup_file_logging(name: str, level: int = logging.INFO) -> str | None:
    """Rotating log at `<ProgramData>\\ds5bridge\\logs\\<name>.log`. Returns the
    path, or None when the directory cannot be written (logging then stays
    wherever it was; a service must never fail to start over a log file)."""
    try:
        from logging.handlers import RotatingFileHandler

        d = log_dir()
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, f"{name}.log")
        handler = RotatingFileHandler(path, maxBytes=2 * 1024 * 1024,
                                      backupCount=3, encoding="utf-8")
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
        root = logging.getLogger()
        root.addHandler(handler)
        if root.level > level or root.level == logging.NOTSET:
            root.setLevel(level)
        return path
    except Exception:  # noqa: BLE001
        return None


class LogStream:
    """A file-like stdout/stderr replacement that writes lines to a logger.

    The tray narrates through `print()`; under the service there is no console
    and the narration is exactly what a bug report needs, so it goes to the
    same rotating file as the logging module's records."""

    def __init__(self, logger: logging.Logger, level: int = logging.INFO):
        self._log = logger
        self._level = level
        self._buf = ""

    def write(self, s) -> int:
        try:
            text = str(s)
        except Exception:  # noqa: BLE001
            return 0
        self._buf += text
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if line.strip():
                self._log.log(self._level, line.rstrip())
        return len(text)

    def flush(self) -> None:
        if self._buf.strip():
            self._log.log(self._level, self._buf.rstrip())
        self._buf = ""

    def isatty(self) -> bool:
        return False


# ---------------------------------------------------------------------------
# sc.exe -- install / query / start / stop
# ---------------------------------------------------------------------------


def available() -> bool:
    return sys.platform == "win32"


def _decode(raw: bytes) -> str:
    for enc in ("utf-8", "mbcs" if sys.platform == "win32" else "latin-1"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("latin-1", "replace")


def _run_sc(args: list[str]) -> tuple[int, str]:
    """`sc.exe <args>` -> (exit code, combined output). Never raises.
    Module-level and looked up at call time so the tests replace it."""
    try:
        r = subprocess.run(["sc.exe", *args], capture_output=True, timeout=60,
                           stdin=subprocess.DEVNULL,
                           creationflags=_CREATE_NO_WINDOW)
        return r.returncode, _decode(r.stdout or b"") + _decode(r.stderr or b"")
    except Exception as e:  # noqa: BLE001
        log.debug("sc %s failed: %s", args, e)
        return -1, str(e)


def _sc(args: list[str]) -> tuple[int, str]:
    return _run_sc(args)


_STATE_RE = re.compile(r"STATE\s*:\s*\d+\s+([A-Z_]+)")
_START_RE = re.compile(r"START_TYPE\s*:\s*\d+\s+([A-Z_ ()]+)")
_BINPATH_RE = re.compile(r"BINARY_PATH_NAME\s*:\s*(.+)")


def query(name: str = SERVICE_NAME) -> dict | None:
    """`{"state": "RUNNING"|"STOPPED"|..., "start": "AUTO_START"|"DEMAND_START"|
    "DISABLED"|..., "binpath": str}`, or None when no such service. Never raises."""
    if not available():
        return None
    code, out = _sc(["query", name])
    if code != 0:
        # 1060: no such service. Anything else (a refused query) is also
        # "cannot see it", which callers treat as absent.
        return None
    m = _STATE_RE.search(out)
    state = m.group(1) if m else "UNKNOWN"
    start, binpath = "UNKNOWN", ""
    code, qc = _sc(["qc", name])
    if code == 0:
        m = _START_RE.search(qc)
        if m:
            start = m.group(1).strip()
        m = _BINPATH_RE.search(qc)
        if m:
            binpath = m.group(1).strip()
    return {"state": state, "start": start, "binpath": binpath}


def installed() -> bool:
    return query() is not None


def is_running() -> bool:
    q = query()
    return bool(q) and q["state"] == "RUNNING"


def starts_at_boot() -> bool:
    """Is the service set to start automatically (with or without delay)?"""
    q = query()
    return bool(q) and q["start"].startswith("AUTO_START")


def service_exe() -> str:
    """`ds5bridge.exe` next to whatever is running -- the console exe is the
    service host (a windowed exe has no business under the SCM, and the tray
    is the child). A source checkout has no such exe; `install` says so."""
    exe = sys.executable or ""
    d = os.path.dirname(exe)
    cand = os.path.join(d, "ds5bridge.exe")
    if os.path.isfile(cand):
        return cand
    return ""


def bin_path(exe: str | None = None) -> str:
    exe = (exe or service_exe()).strip().strip('"')
    if not exe:
        raise ServiceError("the service needs the packaged ds5bridge.exe; run "
                           "this from the installed app directory.")
    return f'"{exe}" service'


def install(exe: str | None = None, start_at_boot: bool = True) -> None:
    """Register (or update) the service. Idempotent. Does not start it.

    `sc create` on an existing name answers 1073; `sc config` then updates the
    path and start type in place, so re-running the installer (and every
    update) refreshes the registration without a delete/create that would
    lose a start-type the user changed.
    """
    if not available():
        raise ServiceError("Windows services exist only on Windows.")
    path = bin_path(exe)
    start = "auto" if start_at_boot else "demand"
    code, out = _sc(["create", SERVICE_NAME, "binPath=", path, "start=", start,
                     "DisplayName=", DISPLAY_NAME, "obj=", "LocalSystem"])
    if code == 1073 or (code != 0 and "1073" in out):
        code, out = _sc(["config", SERVICE_NAME, "binPath=", path,
                         "start=", start, "DisplayName=", DISPLAY_NAME,
                         "obj=", "LocalSystem"])
    if code != 0:
        detail = " ".join((out or "").split())[:300]
        raise ServiceError(f"could not register the service '{SERVICE_NAME}' "
                           f"(sc exit {code}: {detail}). This needs an "
                           f"administrator prompt.")
    _sc(["description", SERVICE_NAME, DESCRIPTION])
    # If the supervisor itself ever dies, the SCM brings it back.
    _sc(["failure", SERVICE_NAME, "reset=", "86400",
         "actions=", "restart/5000/restart/10000/restart/30000"])
    log.info("service %s registered: %s (start=%s)", SERVICE_NAME, path, start)


def uninstall(wait_s: float = CHILD_STOP_S + 15) -> None:
    """Stop (gracefully) and delete the service. Absent is success."""
    if not available():
        raise ServiceError("Windows services exist only on Windows.")
    if query() is None:
        return
    try:
        stop(wait_s=wait_s)
    except ServiceError as e:
        log.warning("stopping before delete: %s", e)
    code, out = _sc(["delete", SERVICE_NAME])
    if code != 0 and query() is not None:
        detail = " ".join((out or "").split())[:300]
        raise ServiceError(f"could not remove the service '{SERVICE_NAME}' "
                           f"(sc exit {code}: {detail}).")
    log.info("service %s removed", SERVICE_NAME)


def set_start_at_boot(enabled: bool) -> None:
    """Automatic vs manual start: what "Start with Windows" means in service
    mode. The service stays registered either way, so a running tray keeps
    running; only the next boot changes."""
    q = query()
    if q is None:
        raise ServiceError(f"the service '{SERVICE_NAME}' is not installed.")
    code, out = _sc(["config", SERVICE_NAME, "start=",
                     "auto" if enabled else "demand"])
    if code != 0:
        detail = " ".join((out or "").split())[:300]
        raise ServiceError(f"could not change the service's start type "
                           f"(sc exit {code}: {detail}). This needs an "
                           f"administrator prompt.")


def _wait_state(want: str, wait_s: float, sleep=time.sleep) -> bool:
    deadline = time.monotonic() + max(0.0, wait_s)
    while True:
        q = query()
        state = q["state"] if q else "ABSENT"
        if state == want or (want == "STOPPED" and state == "ABSENT"):
            return True
        if time.monotonic() >= deadline:
            return False
        sleep(1.0)


def start(wait_s: float = 30.0) -> None:
    if query() is None:
        raise ServiceError(f"the service '{SERVICE_NAME}' is not installed.")
    code, out = _sc(["start", SERVICE_NAME])
    # 1056: already running -- that is the state we want.
    if code != 0 and code != 1056 and "1056" not in out:
        detail = " ".join((out or "").split())[:300]
        raise ServiceError(f"could not start the service '{SERVICE_NAME}' "
                           f"(sc exit {code}: {detail}).")
    if not _wait_state("RUNNING", wait_s):
        raise ServiceError(f"the service '{SERVICE_NAME}' did not reach "
                           f"RUNNING within {wait_s:g} s; see "
                           f"{os.path.join(log_dir(), 'service.log')}.")


def stop(wait_s: float = CHILD_STOP_S + 15) -> None:
    q = query()
    if q is None or q["state"] == "STOPPED":
        return
    code, out = _sc(["stop", SERVICE_NAME])
    # 1062: not started; 1061: cannot accept controls now (already stopping)
    # -- both mean "wait for STOPPED", not "fail".
    if code not in (0, 1061, 1062) and not any(c in out for c in ("1061", "1062")):
        detail = " ".join((out or "").split())[:300]
        raise ServiceError(f"could not stop the service '{SERVICE_NAME}' "
                           f"(sc exit {code}: {detail}).")
    if not _wait_state("STOPPED", wait_s):
        raise ServiceError(f"the service '{SERVICE_NAME}' did not stop within "
                           f"{wait_s:g} s.")


def status_text() -> str:
    """One human line for `ds5bridge service status` and `doctor`."""
    q = query()
    if q is None:
        return "not installed"
    start = {"AUTO_START": "starts with Windows",
             "AUTO_START  (DELAYED)": "starts with Windows (delayed)",
             "DEMAND_START": "manual start",
             "DISABLED": "disabled"}.get(q["start"], q["start"].lower())
    return f"{q['state'].lower()}, {start}"


# ---------------------------------------------------------------------------
# migrating the user's settings into ProgramData
# ---------------------------------------------------------------------------


def user_config_dir() -> str:
    """The per-user settings directory the tray used before service mode --
    the caller's `%APPDATA%\\ds5bridge`. Read here rather than through
    config.config_dir(): that honours DS5_CONFIG, and the installer sets
    nothing of the kind."""
    appdata = os.environ.get("APPDATA")
    if appdata:
        return os.path.join(appdata, "ds5bridge")
    return os.path.join(os.path.expanduser("~"), ".ds5bridge")


def migrate_user_config(src_dir: str | None = None,
                        dst_dir: str | None = None) -> list[str]:
    """Copy the user's config.json (and move any hide-journal records) into
    the service's directory, ONCE: a config.json already there is never
    overwritten, because it is the one the service has been saving to.

    Returns the list of files migrated (for the installer's summary). The
    journal records are MOVED, not copied: a record is a debt ("this pad is
    hidden, by pid N"), and the process that will repay it from now on is the
    service child, which reads the new directory. Never raises.
    """
    src = src_dir or user_config_dir()
    dst = dst_dir or program_data_dir()
    done: list[str] = []
    try:
        os.makedirs(dst, exist_ok=True)
    except OSError as e:
        log.warning("cannot create %s: %s", dst, e)
        return done
    src_cfg = os.path.join(src, "config.json")
    dst_cfg = os.path.join(dst, "config.json")
    if os.path.isfile(src_cfg) and not os.path.exists(dst_cfg):
        try:
            shutil.copy2(src_cfg, dst_cfg)
            done.append(dst_cfg)
        except OSError as e:
            log.warning("could not copy %s -> %s: %s", src_cfg, dst_cfg, e)
    src_hidden = os.path.join(src, "hidden")
    if os.path.isdir(src_hidden):
        dst_hidden = os.path.join(dst, "hidden")
        for name in sorted(os.listdir(src_hidden)):
            if not name.lower().endswith(".json"):
                continue
            s, d = os.path.join(src_hidden, name), os.path.join(dst_hidden, name)
            try:
                os.makedirs(dst_hidden, exist_ok=True)
                if os.path.exists(d):
                    os.remove(s)
                else:
                    shutil.move(s, d)
                done.append(d)
            except OSError as e:
                log.warning("could not move journal record %s: %s", s, e)
    return done


# ---------------------------------------------------------------------------
# Win32 -- sessions, tokens, the child, the stop event
# ---------------------------------------------------------------------------

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _adv = ctypes.WinDLL("advapi32", use_last_error=True)
    _wts = ctypes.WinDLL("wtsapi32", use_last_error=True)
    _uenv = ctypes.WinDLL("userenv", use_last_error=True)

    TOKEN_ALL_ACCESS = 0xF01FF
    TOKEN_QUERY = 0x0008
    TOKEN_ADJUST_PRIVILEGES = 0x0020
    SecurityImpersonation = 2
    TokenPrimary = 1
    TokenSessionId = 12
    SE_PRIVILEGE_ENABLED = 0x2
    CREATE_UNICODE_ENVIRONMENT = 0x00000400
    CREATE_NEW_PROCESS_GROUP = 0x00000200
    SYNCHRONIZE = 0x00100000
    EVENT_MODIFY_STATE = 0x0002
    PROCESS_QUERY_INFORMATION = 0x0400
    WAIT_OBJECT_0 = 0
    WAIT_TIMEOUT = 0x102
    STILL_ACTIVE = 259
    INFINITE = 0xFFFFFFFF

    class STARTUPINFOW(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR),
                    ("lpDesktop", wintypes.LPWSTR), ("lpTitle", wintypes.LPWSTR),
                    ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
                    ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD),
                    ("dwXCountChars", wintypes.DWORD),
                    ("dwYCountChars", wintypes.DWORD),
                    ("dwFillAttribute", wintypes.DWORD),
                    ("dwFlags", wintypes.DWORD), ("wShowWindow", wintypes.WORD),
                    ("cbReserved2", wintypes.WORD),
                    ("lpReserved2", ctypes.POINTER(ctypes.c_byte)),
                    ("hStdInput", wintypes.HANDLE),
                    ("hStdOutput", wintypes.HANDLE),
                    ("hStdError", wintypes.HANDLE)]

    class PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE),
                    ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD)]

    class LUID(ctypes.Structure):
        _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]

    class LUID_AND_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("Luid", LUID), ("Attributes", wintypes.DWORD)]

    class TOKEN_PRIVILEGES(ctypes.Structure):
        _fields_ = [("PrivilegeCount", wintypes.DWORD),
                    ("Privileges", LUID_AND_ATTRIBUTES * 1)]

    _k32.WTSGetActiveConsoleSessionId.restype = wintypes.DWORD
    _k32.ProcessIdToSessionId.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    _k32.GetCurrentProcessId.restype = wintypes.DWORD
    _k32.SetEvent.argtypes = [wintypes.HANDLE]
    _k32.ResetEvent.argtypes = [wintypes.HANDLE]
    _adv.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                      ctypes.POINTER(wintypes.HANDLE)]
    _adv.DuplicateTokenEx.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                      ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                                      ctypes.POINTER(wintypes.HANDLE)]
    _adv.SetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                         ctypes.c_void_p, wintypes.DWORD]
    _adv.CreateProcessAsUserW.argtypes = [
        wintypes.HANDLE, wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p,
        ctypes.c_void_p, wintypes.BOOL, wintypes.DWORD, ctypes.c_void_p,
        wintypes.LPCWSTR, ctypes.POINTER(STARTUPINFOW),
        ctypes.POINTER(PROCESS_INFORMATION)]
    _adv.LookupPrivilegeValueW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR,
                                           ctypes.POINTER(LUID)]
    _adv.AdjustTokenPrivileges.argtypes = [wintypes.HANDLE, wintypes.BOOL,
                                           ctypes.POINTER(TOKEN_PRIVILEGES),
                                           wintypes.DWORD, ctypes.c_void_p,
                                           ctypes.c_void_p]
    _adv.ConvertStringSidToSidW.argtypes = [wintypes.LPCWSTR,
                                            ctypes.POINTER(ctypes.c_void_p)]
    _adv.CheckTokenMembership.argtypes = [wintypes.HANDLE, ctypes.c_void_p,
                                          ctypes.POINTER(wintypes.BOOL)]
    _wts.WTSQueryUserToken.argtypes = [wintypes.ULONG, ctypes.POINTER(wintypes.HANDLE)]
    _uenv.CreateEnvironmentBlock.argtypes = [ctypes.POINTER(ctypes.c_void_p),
                                             wintypes.HANDLE, wintypes.BOOL]
    _uenv.DestroyEnvironmentBlock.argtypes = [ctypes.c_void_p]
    _k32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL,
                                  wintypes.LPCWSTR]
    _k32.CreateEventW.restype = wintypes.HANDLE
    _k32.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    _k32.OpenEventW.restype = wintypes.HANDLE
    _k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    _k32.WaitForSingleObject.restype = wintypes.DWORD
    _k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE,
                                        ctypes.POINTER(wintypes.DWORD)]
    _k32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _k32.GetCurrentProcess.restype = wintypes.HANDLE
    _k32.LocalFree.argtypes = [ctypes.c_void_p]

    def _winerr(what: str) -> ServiceError:
        err = ctypes.get_last_error()
        return ServiceError(f"{what} failed: [{err}] {ctypes.FormatError(err).strip()}")

    def is_system() -> bool:
        """Is this process LocalSystem (S-1-5-18)? Cheap, never raises."""
        sid = ctypes.c_void_p()
        if not _adv.ConvertStringSidToSidW("S-1-5-18", ctypes.byref(sid)):
            return False
        try:
            member = wintypes.BOOL(0)
            if not _adv.CheckTokenMembership(None, sid, ctypes.byref(member)):
                return False
            return bool(member.value)
        finally:
            _k32.LocalFree(sid)

    def current_session_id() -> int:
        """The session THIS process runs in (ProcessIdToSessionId)."""
        sid = wintypes.DWORD(0)
        if _k32.ProcessIdToSessionId(_k32.GetCurrentProcessId(), ctypes.byref(sid)):
            return int(sid.value)
        return 0

    def active_console_session() -> int | None:
        """The session attached to the physical console, or None while there
        is none (Windows is switching sessions)."""
        sid = _k32.WTSGetActiveConsoleSessionId()
        return None if sid == _INVALID_SESSION else int(sid)

    def _enable_privilege(name: str) -> bool:
        """Turn a privilege on in our own token. SYSTEM holds SeTcb,
        SeAssignPrimaryToken and SeIncreaseQuota but they are not all enabled
        by default; failing is logged, not fatal -- the next call reports
        the real error."""
        tok = wintypes.HANDLE()
        if not _adv.OpenProcessToken(_k32.GetCurrentProcess(),
                                     TOKEN_ADJUST_PRIVILEGES | TOKEN_QUERY,
                                     ctypes.byref(tok)):
            return False
        try:
            tp = TOKEN_PRIVILEGES()
            tp.PrivilegeCount = 1
            if not _adv.LookupPrivilegeValueW(None, name,
                                              ctypes.byref(tp.Privileges[0].Luid)):
                return False
            tp.Privileges[0].Attributes = SE_PRIVILEGE_ENABLED
            ok = _adv.AdjustTokenPrivileges(tok, False, ctypes.byref(tp), 0,
                                            None, None)
            return bool(ok) and ctypes.get_last_error() == 0
        finally:
            _k32.CloseHandle(tok)

    def _env_block(env: dict) -> ctypes.Array:
        """A CREATE_UNICODE_ENVIRONMENT block: NAME=value\\0 ... \\0\\0, sorted
        the way Windows keeps its own (case-insensitive by name)."""
        items = sorted(((str(k), str(v)) for k, v in env.items()),
                       key=lambda kv: kv[0].upper())
        text = "".join(f"{k}={v}\0" for k, v in items) + "\0"
        buf = (ctypes.c_wchar * (len(text) + 1))()
        buf[:len(text)] = text
        return buf

    def _cmdline(cmd: list[str]) -> str:
        return subprocess.list2cmdline(cmd)

    class Child:
        """A process started in another session, with the handshake to stop it."""

        def __init__(self, handle, pid: int, session: int, stop_event):
            self._h = handle
            self.pid = pid
            self.session = session
            self._stop_event = stop_event
            self.started = time.monotonic()

        def poll(self) -> int | None:
            code = wintypes.DWORD(0)
            if not _k32.GetExitCodeProcess(self._h, ctypes.byref(code)):
                return -1
            return None if code.value == STILL_ACTIVE else int(code.value)

        def wait(self, timeout_s: float) -> int | None:
            ms = INFINITE if timeout_s is None else max(0, int(timeout_s * 1000))
            if _k32.WaitForSingleObject(self._h, ms) == WAIT_OBJECT_0:
                return self.poll()
            return None

        def ask_to_stop(self) -> None:
            if self._stop_event:
                _k32.SetEvent(self._stop_event)

        def kill(self) -> None:
            _k32.TerminateProcess(self._h, 1)

        def close(self) -> None:
            if self._h:
                _k32.CloseHandle(self._h)
                self._h = None

    def create_stop_event(name: str = STOP_EVENT_NAME):
        """The manual-reset event the child waits on. Created (or opened, if
        a previous supervisor left it) and RESET, so a stale signal from the
        last stop cannot end the next child at birth."""
        h = _k32.CreateEventW(None, True, False, name)
        if not h:
            raise _winerr("CreateEvent")
        _k32.ResetEvent(h)
        return h

    def open_stop_event(name: str | None = None):
        """The child's side: the event by name, or None when not under the
        service (the name arrives in the environment)."""
        name = name or os.environ.get(STOP_EVENT_ENV) or ""
        if not name:
            return None
        h = _k32.OpenEventW(SYNCHRONIZE | EVENT_MODIFY_STATE, False, name)
        return h or None

    def wait_event(handle, timeout_s: float | None = None) -> bool:
        ms = INFINITE if timeout_s is None else max(0, int(timeout_s * 1000))
        return _k32.WaitForSingleObject(handle, ms) == WAIT_OBJECT_0

    def _system_token_for_session(session: int):
        tok = wintypes.HANDLE()
        if not _adv.OpenProcessToken(_k32.GetCurrentProcess(), TOKEN_ALL_ACCESS,
                                     ctypes.byref(tok)):
            raise _winerr("OpenProcessToken")
        try:
            dup = wintypes.HANDLE()
            if not _adv.DuplicateTokenEx(tok, TOKEN_ALL_ACCESS, None,
                                         SecurityImpersonation, TokenPrimary,
                                         ctypes.byref(dup)):
                raise _winerr("DuplicateTokenEx")
        finally:
            _k32.CloseHandle(tok)
        sid = wintypes.DWORD(session)
        if session != current_session_id():
            if not _adv.SetTokenInformation(dup, TokenSessionId, ctypes.byref(sid),
                                            ctypes.sizeof(sid)):
                err = _winerr("SetTokenInformation(TokenSessionId)")
                _k32.CloseHandle(dup)
                raise err
        return dup

    def launch_in_session(cmd: list[str], session: int, env: dict,
                          cwd: str | None = None, stop_event=None,
                          desktop: str = "winsta0\\default") -> Child:
        """`cmd` in `session` on its interactive desktop, as OUR account
        (LocalSystem under the service). Raises ServiceError."""
        for p in ("SeTcbPrivilege", "SeAssignPrimaryTokenPrivilege",
                  "SeIncreaseQuotaPrivilege"):
            _enable_privilege(p)
        token = _system_token_for_session(session)
        try:
            si = STARTUPINFOW()
            si.cb = ctypes.sizeof(si)
            si.lpDesktop = desktop
            pi = PROCESS_INFORMATION()
            block = _env_block(env)
            cmdline = ctypes.create_unicode_buffer(_cmdline(cmd))
            ok = _adv.CreateProcessAsUserW(
                token, None, cmdline, None, None, False,
                CREATE_UNICODE_ENVIRONMENT | CREATE_NEW_PROCESS_GROUP,
                block, cwd or None, ctypes.byref(si), ctypes.byref(pi))
            if not ok:
                raise _winerr("CreateProcessAsUser")
            _k32.CloseHandle(pi.hThread)
            return Child(pi.hProcess, int(pi.dwProcessId), session, stop_event)
        finally:
            _k32.CloseHandle(token)

    def open_url_as_user(url: str, session: int | None = None) -> bool:
        """Open `url` in the SIGNED-IN USER's default browser, with the user's
        own token: from SYSTEM, ShellExecute would start the browser as SYSTEM
        (and Chrome refuses to run elevated next to a normal instance).
        `WTSQueryUserToken` needs SeTcbPrivilege, i.e. this works from the
        service child and not from an ordinary tray; False then."""
        if session is None:
            session = current_session_id()
        _enable_privilege("SeTcbPrivilege")
        tok = wintypes.HANDLE()
        if not _wts.WTSQueryUserToken(session, ctypes.byref(tok)):
            log.debug("WTSQueryUserToken(%s) failed: %s", session,
                      ctypes.FormatError(ctypes.get_last_error()))
            return False
        try:
            env = ctypes.c_void_p()
            if not _uenv.CreateEnvironmentBlock(ctypes.byref(env), tok, False):
                env = None
            try:
                si = STARTUPINFOW()
                si.cb = ctypes.sizeof(si)
                si.lpDesktop = "winsta0\\default"
                pi = PROCESS_INFORMATION()
                sysdir = os.environ.get("SystemRoot", r"C:\Windows") + "\\System32"
                cmd = ctypes.create_unicode_buffer(
                    f'"{sysdir}\\rundll32.exe" url.dll,FileProtocolHandler {url}')
                ok = _adv.CreateProcessAsUserW(
                    tok, None, cmd, None, None, False,
                    CREATE_UNICODE_ENVIRONMENT | _CREATE_NO_WINDOW,
                    env, None, ctypes.byref(si), ctypes.byref(pi))
                if not ok:
                    log.debug("CreateProcessAsUser(rundll32) failed: %s",
                              ctypes.FormatError(ctypes.get_last_error()))
                    return False
                _k32.CloseHandle(pi.hThread)
                _k32.CloseHandle(pi.hProcess)
                return True
            finally:
                if env:
                    _uenv.DestroyEnvironmentBlock(env)
        finally:
            _k32.CloseHandle(tok)

else:  # pragma: no cover - the app targets Windows; keep imports safe elsewhere
    def is_system() -> bool:
        return False

    def current_session_id() -> int:
        return 0

    def active_console_session() -> int | None:
        return None

    def open_stop_event(name=None):
        return None

    def wait_event(handle, timeout_s=None) -> bool:
        return False

    def open_url_as_user(url: str, session=None) -> bool:
        return False


# ---------------------------------------------------------------------------
# the supervisor -- policy, with every Win32 call injectable
# ---------------------------------------------------------------------------


def child_command() -> list[str]:
    """`ds5bridge-tray.exe --service-child` next to this exe, or the source
    form (`pythonw -c ...`) from a checkout -- via autostart's launcher logic,
    so the two agree on what "the tray" is."""
    from . import autostart as A

    exe = A.resolve_exe()
    cmd, args = A.build_action(exe, extra_args=("tray", CHILD_FLAG))
    return [cmd, *(_split_args(args) if args else [])]


def _split_args(args: str) -> list[str]:
    # build_action's argument string is either "" , "tray --service-child"
    # (a launcher) or `-c "<program>"` (python). Only those two shapes.
    if args.startswith("-c "):
        return ["-c", args[3:].strip().strip('"')]
    return args.split()


def child_env(base: dict | None = None) -> dict:
    env = dict(os.environ if base is None else base)
    env["DS5_CONFIG"] = program_data_dir()
    env[STOP_EVENT_ENV] = STOP_EVENT_NAME
    return env


class Supervisor:
    """Keep one child alive in the console session until told to stop.

    Injectable seams (all keyword arguments) so the loop is testable with no
    SCM, no sessions and no processes:

        spawn(session)        -> a child with poll()/wait(s)/ask_to_stop()/kill()/close()
        session_fn()          -> the active console session id, or None
        sleep(s)              -> interruptible wait; returns True when stop was requested
        clock()               -> monotonic seconds
        on_status(state, checkpoint, wait_hint_ms)   -> SCM progress while stopping
    """

    def __init__(self, spawn, session_fn=active_console_session, *,
                 sleep=None, clock=time.monotonic, on_status=None,
                 child_stop_s: float = CHILD_STOP_S,
                 backoff_min_s: float = BACKOFF_MIN_S,
                 backoff_max_s: float = BACKOFF_MAX_S,
                 stable_s: float = STABLE_S, poll_s: float = POLL_S):
        self._spawn = spawn
        self._session_fn = session_fn
        self._stop = threading.Event()
        self._sleep = sleep or self._stop.wait
        self._clock = clock
        self._on_status = on_status or (lambda *a: None)
        self.child_stop_s = child_stop_s
        self.backoff_min_s = backoff_min_s
        self.backoff_max_s = backoff_max_s
        self.stable_s = stable_s
        self.poll_s = poll_s
        self.child = None
        #: Why the loop ended: "stop" (asked), "quit" (the user quit the tray).
        self.reason = ""

    def request_stop(self) -> None:
        self._stop.set()

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def _wait_session(self) -> int | None:
        """A valid console session, or None when stop was requested first."""
        while not self.stopping:
            s = self._session_fn()
            if s is not None:
                return s
            log.info("no console session yet; waiting")
            self._sleep(2.0)
        return None

    def _stop_child(self, why: str) -> int | None:
        """The handshake: ask, wait `child_stop_s` reporting progress, then kill."""
        child = self.child
        if child is None:
            return None
        log.info("stopping child pid %s (%s)", child.pid, why)
        code = child.poll()
        if code is None:
            child.ask_to_stop()
            deadline = self._clock() + self.child_stop_s
            checkpoint = 1
            while code is None and self._clock() < deadline:
                code = child.wait(self.poll_s)
                self._on_status("STOP_PENDING", checkpoint,
                                int(self.child_stop_s * 1000))
                checkpoint += 1
            if code is None:
                log.warning("child pid %s ignored the stop request for %.0f s; "
                            "terminating it", child.pid, self.child_stop_s)
                child.kill()
                code = child.wait(5.0)
        log.info("child pid %s exited with %s", child.pid, code)
        child.close()
        self.child = None
        return code

    def run(self) -> str:
        """Until `request_stop()` or the user quits the tray. Returns the reason."""
        backoff = self.backoff_min_s
        while not self.stopping:
            session = self._wait_session()
            if session is None:
                break
            try:
                self.child = self._spawn(session)
            except Exception as e:  # noqa: BLE001
                log.error("could not start the tray in session %s: %s", session, e)
                self._sleep(backoff)
                backoff = min(backoff * 2, self.backoff_max_s)
                continue
            log.info("tray started in session %s (pid %s)", session, self.child.pid)
            started = self._clock()
            code = None
            moved = False
            while not self.stopping:
                code = self.child.wait(self.poll_s)
                if code is not None:
                    break
                now_session = self._session_fn()
                if now_session is not None and now_session != session:
                    log.info("console session changed %s -> %s", session, now_session)
                    self._stop_child("console session changed")
                    moved = True
                    break
            if self.stopping:
                self._stop_child("service stopping")
                self.reason = "stop"
                break
            if moved:
                backoff = self.backoff_min_s
                continue
            # The child exited on its own.
            if self.child is not None:
                self.child.close()
                self.child = None
            if code == EXIT_STOP_SERVICE:
                log.info("the user quit the tray; stopping the service")
                self.reason = "quit"
                break
            uptime = self._clock() - started
            if uptime >= self.stable_s:
                backoff = self.backoff_min_s
            log.warning("tray exited with %s after %.0f s; restarting in %.0f s",
                        code, uptime, backoff)
            self._sleep(backoff)
            backoff = min(backoff * 2, self.backoff_max_s)
        return self.reason or "stop"


def wait_for_driver(timeout_s: float = DRIVER_WAIT_S, sleep=time.sleep) -> bool:
    """At boot our service can start before usbip-win2's UDE driver; give it
    a minute. True when it is running (or absent: nothing to wait for)."""
    deadline = time.monotonic() + timeout_s
    while True:
        q = query(DRIVER_SERVICE)
        if q is None or q["state"] == "RUNNING":
            return True
        if time.monotonic() >= deadline:
            log.warning("%s is %s after %.0f s; starting anyway", DRIVER_SERVICE,
                        q["state"], timeout_s)
            return False
        sleep(2.0)


# ---------------------------------------------------------------------------
# the SCM entry point
# ---------------------------------------------------------------------------


def run_service(child_cmd: list[str] | None = None,
                env: dict | None = None) -> int:
    """`ds5bridge.exe service` under the Service Control Manager. Blocks until
    the service stops. Returns 0, or 1 when not started by the SCM (a person
    typed it in a terminal: say so). `child_cmd`/`env` exist for the spike
    and for tests; the real service takes `child_command()`/`child_env()`."""
    if sys.platform != "win32":
        print("Windows services exist only on Windows.")
        return 1
    import ctypes
    from ctypes import wintypes

    adv = ctypes.WinDLL("advapi32", use_last_error=True)

    SERVICE_WIN32_OWN_PROCESS = 0x10
    SERVICE_STOPPED, SERVICE_START_PENDING, SERVICE_STOP_PENDING, SERVICE_RUNNING = 1, 2, 3, 4
    SERVICE_ACCEPT_STOP = 0x1
    SERVICE_ACCEPT_SHUTDOWN = 0x4
    SERVICE_ACCEPT_SESSIONCHANGE = 0x80
    SERVICE_ACCEPT_PRESHUTDOWN = 0x100
    SERVICE_CONTROL_STOP, SERVICE_CONTROL_INTERROGATE = 1, 4
    SERVICE_CONTROL_SHUTDOWN, SERVICE_CONTROL_SESSIONCHANGE = 5, 0xE
    SERVICE_CONTROL_PRESHUTDOWN = 0xF
    NO_ERROR, ERROR_CALL_NOT_IMPLEMENTED = 0, 120
    ERROR_FAILED_SERVICE_CONTROLLER_CONNECT = 1063

    class SERVICE_STATUS(ctypes.Structure):
        _fields_ = [("dwServiceType", wintypes.DWORD),
                    ("dwCurrentState", wintypes.DWORD),
                    ("dwControlsAccepted", wintypes.DWORD),
                    ("dwWin32ExitCode", wintypes.DWORD),
                    ("dwServiceSpecificExitCode", wintypes.DWORD),
                    ("dwCheckPoint", wintypes.DWORD),
                    ("dwWaitHint", wintypes.DWORD)]

    class SERVICE_TABLE_ENTRYW(ctypes.Structure):
        _fields_ = [("lpServiceName", wintypes.LPWSTR),
                    ("lpServiceProc", ctypes.c_void_p)]

    class WTSSESSION_NOTIFICATION(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("dwSessionId", wintypes.DWORD)]

    HANDLER_EX = ctypes.WINFUNCTYPE(wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                                    ctypes.c_void_p, ctypes.c_void_p)
    SERVICE_MAIN = ctypes.WINFUNCTYPE(None, wintypes.DWORD,
                                      ctypes.POINTER(wintypes.LPWSTR))
    adv.RegisterServiceCtrlHandlerExW.argtypes = [wintypes.LPCWSTR, HANDLER_EX,
                                                  ctypes.c_void_p]
    adv.RegisterServiceCtrlHandlerExW.restype = ctypes.c_void_p
    adv.SetServiceStatus.argtypes = [ctypes.c_void_p, ctypes.POINTER(SERVICE_STATUS)]
    adv.StartServiceCtrlDispatcherW.argtypes = [ctypes.POINTER(SERVICE_TABLE_ENTRYW)]

    setup_file_logging("service")
    slog = logging.getLogger("ds5app.winsvc.service")
    state = {"handle": None, "sup": None, "checkpoint": 0}
    controls = (SERVICE_ACCEPT_STOP | SERVICE_ACCEPT_SHUTDOWN
                | SERVICE_ACCEPT_SESSIONCHANGE | SERVICE_ACCEPT_PRESHUTDOWN)

    def report(current: int, checkpoint: int = 0, wait_hint_ms: int = 0,
               exit_code: int = NO_ERROR) -> None:
        st = SERVICE_STATUS()
        st.dwServiceType = SERVICE_WIN32_OWN_PROCESS
        st.dwCurrentState = current
        st.dwControlsAccepted = 0 if current in (SERVICE_START_PENDING,) else controls
        st.dwWin32ExitCode = exit_code
        st.dwServiceSpecificExitCode = 0
        st.dwCheckPoint = checkpoint
        st.dwWaitHint = wait_hint_ms
        if state["handle"]:
            adv.SetServiceStatus(state["handle"], ctypes.byref(st))

    def on_status(name: str, checkpoint: int, wait_hint_ms: int) -> None:
        report(SERVICE_STOP_PENDING, checkpoint, wait_hint_ms)

    def handler(control, event_type, event_data, context):
        sup = state["sup"]
        if control in (SERVICE_CONTROL_STOP, SERVICE_CONTROL_SHUTDOWN,
                       SERVICE_CONTROL_PRESHUTDOWN):
            slog.info("control %s received; stopping", hex(control))
            report(SERVICE_STOP_PENDING, 1, int(CHILD_STOP_S * 1000) + 5000)
            if sup is not None:
                sup.request_stop()
            return NO_ERROR
        if control == SERVICE_CONTROL_INTERROGATE:
            return NO_ERROR
        if control == SERVICE_CONTROL_SESSIONCHANGE:
            sid = "?"
            try:
                if event_data:
                    sid = ctypes.cast(event_data,
                                      ctypes.POINTER(WTSSESSION_NOTIFICATION)).contents.dwSessionId
            except Exception:  # noqa: BLE001
                pass
            slog.info("session change: event %s, session %s (console now %s)",
                      event_type, sid, active_console_session())
            return NO_ERROR
        return ERROR_CALL_NOT_IMPLEMENTED

    handler_cb = HANDLER_EX(handler)

    def service_main(argc, argv):
        state["handle"] = adv.RegisterServiceCtrlHandlerExW(SERVICE_NAME, handler_cb, None)
        if not state["handle"]:
            slog.error("RegisterServiceCtrlHandlerEx failed: %s",
                       ctypes.FormatError(ctypes.get_last_error()))
            return
        report(SERVICE_START_PENDING, 1, 30000)
        slog.info("ds5bridge service starting (pid %s, exe %s)", os.getpid(),
                  sys.executable)
        try:
            cmd = child_cmd or child_command()
            cenv = child_env(env)
            cwd = os.path.dirname(cmd[0]) or None
            stop_event = create_stop_event()

            def spawn(session):
                return launch_in_session(cmd, session, cenv, cwd, stop_event)

            sup = Supervisor(spawn, on_status=on_status)
            state["sup"] = sup
            report(SERVICE_RUNNING)
            slog.info("running; child command: %s; DS5_CONFIG=%s", cmd, cenv["DS5_CONFIG"])
            wait_for_driver()
            reason = sup.run()
            slog.info("supervisor ended (%s)", reason)
        except Exception:  # noqa: BLE001
            slog.exception("the service failed")
        report(SERVICE_STOPPED)

    main_cb = SERVICE_MAIN(service_main)
    table = (SERVICE_TABLE_ENTRYW * 2)()
    table[0].lpServiceName = SERVICE_NAME
    table[0].lpServiceProc = ctypes.cast(main_cb, ctypes.c_void_p)
    table[1].lpServiceName = None
    table[1].lpServiceProc = None
    if not adv.StartServiceCtrlDispatcherW(table):
        err = ctypes.get_last_error()
        if err == ERROR_FAILED_SERVICE_CONTROLLER_CONNECT:
            print("`ds5bridge service` is what the Windows Service Control Manager "
                  "runs; it is not meant to be started from a terminal.\n"
                  "    ds5bridge service install     register it (administrator)\n"
                  "    ds5bridge service start       start it\n"
                  "    ds5bridge service status      is it running?")
        else:
            print(f"StartServiceCtrlDispatcher failed: [{err}] "
                  f"{ctypes.FormatError(err).strip()}")
        return 1
    return 0
