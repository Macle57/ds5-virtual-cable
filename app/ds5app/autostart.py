"""Start-at-login -- one registry value, and the traps hiding in it.

The Run key, and why HKCU and not HKLM
--------------------------------------
    HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run    value "ds5bridge"

Per-user, deliberately. The HKLM twin of this key starts the program for every
account on the machine and writing to it needs administrator rights, which means
a UAC prompt on a checkbox in a tray menu -- and a tray utility that asks for
administrator has to justify holding it for the rest of the session. Nothing
this program does needs it: the bridge talks to a per-user Bluetooth radio, the
usbip attach is per-session, and the instance lock is `Local\\` for exactly the
same reason. HKCU needs no elevation, is trivially inspectable by the user
(Task Manager -> Startup apps shows it), and is removed with the user profile.

The command, and why it is not just sys.executable
--------------------------------------------------
Three different launchers can be the running program, and only one of them is
right at login:

    frozen windowed    ds5bridge-tray.exe        <- what login should start
    frozen console     ds5bridge.exe             a console window at every login
    from source        python.exe -c "..."       a console window at every login

A console window that pops up on every single login is a bug report, not a
feature, so the windowed sibling of `sys.executable` is preferred whenever it
exists -- `ds5bridge-tray.exe` next to `ds5bridge.exe` in an install, or next to
`python.exe` in a venv's Scripts directory -- and failing that, `pythonw.exe`
next to `python.exe`, which is the same trick for a source checkout.

The path is ALWAYS quoted. "C:\\Program Files\\..." unquoted makes Windows try
"C:\\Program.exe" first, and that has been the classic Run-key bug for twenty
years. Arguments after it are not quoted because none of them contain spaces.

Everything here is safe on a machine that is not Windows and on a machine where
the write is refused (locked-down profile, group policy, an antivirus that
guards the Run key): the read paths return False/None and the write paths raise
`AutostartError`, whose message is text to show a user. A tray checkbox must
never turn into an OSError traceback in a process with no console to print it.
"""

from __future__ import annotations

import logging
import os
import sys

log = logging.getLogger("ds5app.autostart")

#: The per-user Run key. Relative to HKEY_CURRENT_USER.
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
#: The value name. Also what shows up in Task Manager -> Startup apps.
VALUE_NAME = "ds5bridge"

#: The windowed launcher, preferred over everything else at login.
TRAY_EXE = "ds5bridge-tray.exe"
#: Interpreter names that mean "we are running from source" and therefore need
#: a sys.path entry and an explicit call appended -- see `build_command`.
#: Anything else is a launcher that already knows how to start itself.
_PYTHON_NAMES = ("python.exe", "pythonw.exe", "python3.exe", "python", "python3")



class AutostartError(RuntimeError):
    """Start-at-login could not be changed. The message is user-facing text."""


# ---------------------------------------------------------------------------
# the command string -- pure, so it can be tested without a registry
# ---------------------------------------------------------------------------


def quote(path: str) -> str:
    """A path as a Run value. Always quoted; never double-quoted."""
    p = (path or "").strip()
    if p.startswith('"') and p.endswith('"') and len(p) > 1:
        return p
    return f'"{p}"'


def is_python(path: str) -> bool:
    return os.path.basename(path or "").lower() in _PYTHON_NAMES


#: <repo>/app -- the directory `ds5app` lives in, so a source checkout can put
#: itself on sys.path at login. `_bootstrap` handles prototype/ and emulator/
#: on its own once `ds5app` is importable; this is the one it cannot do.
APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def build_command(exe: str, app_dir: str | None = None) -> str:
    """The Run-key value for a given launcher. No filesystem, no registry.

    A packaged launcher already knows how to start itself and needs nothing but
    a quoted path. A python interpreter does not, and the naive
    `python -m ds5app tray` is BROKEN AT LOGIN in a way that is invisible when
    you test it from a shell:

        The Run key has no working directory. Windows starts the value in
        %USERPROFILE% (sometimes system32), `ds5app` is not installed and not on
        PYTHONPATH, and the import fails -- silently, because the whole point of
        `pythonw` is that there is no console for the traceback to reach.

    So the source form carries its own sys.path entry instead of hoping for one.
    `-c` is used rather than `-m` for exactly that reason. The injected path is
    an r-string in SINGLE quotes: the Run value is parsed by CreateProcess, the
    whole `-c` program has to sit inside one pair of DOUBLE quotes, and a
    Windows path ending in a backslash would otherwise escape the quote that
    closes it.
    """
    exe = (exe or "").strip().strip('"')
    if not exe:
        raise AutostartError("no launcher to register for start-at-login.")
    if not is_python(exe):
        return quote(exe)
    root = (app_dir or APP_DIR).rstrip("\\/")
    program = (f"import sys;sys.path.insert(0,r'{root}');"
               f"from ds5app.cli import main;sys.exit(main(['tray']))")
    return f'{quote(exe)} -c "{program}"'


def resolve_exe() -> str:
    """The best launcher available on THIS machine. Touches the filesystem."""
    exe = sys.executable or ""
    directory = os.path.dirname(exe)
    if directory:
        # An install directory and a venv's Scripts directory both hold the
        # windowed entry point next to the thing that is running now.
        tray = os.path.join(directory, TRAY_EXE)
        if os.path.isfile(tray):
            return tray
        # Source checkout: pythonw.exe runs the same code with no console.
        if os.path.basename(exe).lower() == "python.exe":
            pythonw = os.path.join(directory, "pythonw.exe")
            if os.path.isfile(pythonw):
                return pythonw
    return exe


def command(exe: str | None = None) -> str:
    """What `enable()` would write. Useful for showing the user, and for tests."""
    return build_command(exe or resolve_exe())


# ---------------------------------------------------------------------------
# the registry
# ---------------------------------------------------------------------------


def available() -> bool:
    """Can start-at-login work here at all? False anywhere but Windows."""
    if sys.platform != "win32":
        return False
    try:
        import winreg  # noqa: F401
    except ImportError:  # pragma: no cover -- a Windows build without winreg
        return False
    return True


def current_command() -> str | None:
    """The registered command, or None. Never raises."""
    if not available():
        return None
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_READ) as key:
            value, _kind = winreg.QueryValueEx(key, VALUE_NAME)
    except FileNotFoundError:
        return None                       # no key, or no value -- same answer
    except OSError as e:
        log.warning("cannot read the Run key: %s", e)
        return None
    return value if isinstance(value, str) else None


def is_enabled() -> bool:
    """Never raises, and never guesses True.

    Deliberately does not compare against `command()`: an entry written by the
    frozen build and read back from a source checkout would compare unequal, and
    a checkbox that reads "off" while the machine really does start the program
    at login is worse than one that is merely out of date.
    """
    return bool(current_command())


def enable(exe: str | None = None) -> None:
    """Register start-at-login. Idempotent -- rewriting the same value is fine."""
    if not available():
        raise AutostartError(
            "start-at-login uses the Windows registry, and this is not Windows.")
    import winreg

    launcher = exe or resolve_exe()
    value = build_command(launcher)
    if is_python(launcher):
        # This form works (build_command injects the sys.path entry), but it
        # pins start-at-login to THIS checkout at THIS path -- move or rename
        # the repo and the login silently stops working. The packaged build has
        # no such tie.
        log.info("registering a source checkout for start-at-login (%s); it is "
                 "tied to this directory. The packaged ds5bridge-tray.exe is "
                 "what should be registered on an installed machine.", value)
    try:
        # CreateKey rather than OpenKey: Run exists on every sane Windows, but
        # it is not guaranteed on a freshly-provisioned or policy-stripped
        # profile, and failing there with "file not found" would be a mystery.
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                                winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, value)
    except OSError as e:
        raise AutostartError(
            f"could not turn on start-at-login ({e}). This writes "
            f"HKCU\\{RUN_KEY}, which needs no administrator rights -- if it is "
            f"refused, a group policy or a security product is guarding it.") from e
    log.info("start-at-login enabled: %s", value)


def disable() -> None:
    """Remove the value. Already absent is success, not an error."""
    if not available():
        raise AutostartError(
            "start-at-login uses the Windows registry, and this is not Windows.")
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, VALUE_NAME)
    except FileNotFoundError:
        return
    except OSError as e:
        raise AutostartError(
            f"could not turn off start-at-login ({e}). Remove the "
            f"'{VALUE_NAME}' value under HKCU\\{RUN_KEY} by hand, or switch it "
            f"off in Task Manager -> Startup apps.") from e
    log.info("start-at-login disabled")


def set_enabled(enabled: bool, exe: str | None = None) -> None:
    """Apply `config.autostart_on_login`. One call for a checkbox handler."""
    if enabled:
        enable(exe)
    else:
        disable()
