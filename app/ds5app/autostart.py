"""Start-at-login -- one scheduled task, and why it is no longer a Run value.

The task, and why not the Run key any more
------------------------------------------
    Task Scheduler library \\ds5bridge      (schtasks /Query /TN ds5bridge)

Until 0.4 this module wrote `HKCU\\...\\CurrentVersion\\Run`, value "ds5bridge",
and that was the right answer for a program that never needed administrator
rights. Since 0.5.0 the tray RUNS ELEVATED (`ds5bridge-tray.exe` carries a
requireAdministrator manifest -- see ds5bridge.spec for the two things that
need it: restarting a device node so an unhidden pad comes back, and so that
HidHide's filter joins a pad that was already paired when HidHide was
installed). And the Run key cannot start an elevated program. Not "prompts
for UAC at logon": Windows silently skips a Run entry whose target would need
elevation, so the checkbox would have turned on and done nothing, which is the
exact class of bug this program has spent its life removing.

The supported mechanism for "start this at logon, elevated, without a prompt"
is a scheduled task with a logon trigger and `RunLevel = HighestAvailable`, and
that is what this module now creates:

    trigger        at logon of THE CURRENT USER only (not every user)
    principal      the current user, interactive token, highest available
                   privileges -- no UAC prompt, no stored password
    settings       no execution time limit (PT0S: the tray runs for the whole
                   session and the scheduler must never kill it), one instance
                   (IgnoreNew), runs on battery and keeps running on battery,
                   StartWhenAvailable (a logon the scheduler slept through is
                   caught up), priority 4 (NORMAL -- the scheduler's default of
                   7 is BELOW_NORMAL, which is the wrong class for a process
                   feeding 1 ms isochronous endpoints)
    action         the tray exe, started in its own directory

It is created through `schtasks.exe /Create /XML` from an XML document this
module writes, because the command-line switches cannot express half of that
(no time limit, the instances policy, the battery settings) and the XML is what
Task Scheduler itself exports -- so the task reads back in the Task Scheduler
UI exactly as it was written. Registering a HighestAvailable task needs an
elevated caller; the tray is one, the installer's helper is one, and a source
checkout that is not gets an `AutostartError` that says so.

The old Run value is a MIGRATION, not a second mechanism: `enable()` and
`disable()` both delete a leftover `ds5bridge` value, and the tray's startup
calls `migrate_legacy()` so an install upgraded from 0.4 turns its dead Run
entry into the task once, then never thinks about it again.

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

`build_command()` still produces the one-line form (quoted exe, then the
arguments) that the Run key used to hold: it is what `current_command()`
returns, what `doctor` prints, and what the tests compare. `build_action()`
is the same thing split into the `<Command>` and `<Arguments>` the task wants.
The path is ALWAYS quoted in the one-line form. "C:\\Program Files\\..."
unquoted makes Windows try "C:\\Program.exe" first, and that has been the
classic Run-key bug for twenty years; the XML form needs no quoting because
Command and Arguments are separate elements.

Since 1.0 there is a second mechanism, and it is the recommended one: the
WINDOWS SERVICE (`winsvc.py`), which starts the tray at boot, before anyone
signs in. This module does not create or remove the service -- the installer
and `ds5bridge service install` do -- but it is the one place that answers
"how does this machine start ds5bridge?" (`mode()`: `service`, `task` or
`none`) and it maps the tray's "Start with Windows" switch and the config's
`autostart_on_login` onto whichever mechanism is installed: in service mode
the switch is the service's start type (automatic vs manual; the service
stays registered either way, so the running tray is not touched), in task
mode it is the task's existence, as before. `migrate_legacy()` never creates
the task when the service is installed.

Everything here is safe on a machine that is not Windows and on a machine where
the change is refused (no elevation, group policy, a security product that
guards the task library): the read paths return False/None and the write paths
raise `AutostartError`, whose message is text to show a user. A tray checkbox
must never turn into an OSError traceback in a process with no console to print
it.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import tempfile
from xml.sax.saxutils import escape as _xml_escape

log = logging.getLogger("ds5app.autostart")

#: The scheduled task. Also what Task Scheduler's library shows at the root.
TASK_NAME = "ds5bridge"

#: The per-user Run key and value the versions before 0.5.0 wrote. Kept only
#: so the leftover can be removed (`_delete_legacy_run_value`).
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "ds5bridge"

#: The windowed launcher, preferred over everything else at login.
TRAY_EXE = "ds5bridge-tray.exe"
#: Interpreter names that mean "we are running from source" and therefore need
#: a sys.path entry and an explicit call appended -- see `build_action`.
#: Anything else is a launcher that already knows how to start itself.
_PYTHON_NAMES = ("python.exe", "pythonw.exe", "python3.exe", "python", "python3")

_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
_TASK_NS = "http://schemas.microsoft.com/windows/2004/02/mit/task"


class AutostartError(RuntimeError):
    """Start-at-login could not be changed. The message is user-facing text."""


# ---------------------------------------------------------------------------
# the command -- pure, so it can be tested without a scheduler
# ---------------------------------------------------------------------------


def quote(path: str) -> str:
    """A path as a command-line word. Always quoted; never double-quoted."""
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


def build_action(exe: str, app_dir: str | None = None,
                 extra_args: tuple[str, ...] = ()) -> tuple[str, str]:
    """(command, arguments) for a given launcher. No filesystem, no scheduler.

    `extra_args` are the tray's own arguments after the verb (the service
    passes `("tray", "--service-child")`); empty means the plain tray, and
    the launcher form then has no arguments at all, exactly as before.

    A packaged launcher already knows how to start itself and needs nothing but
    its path. A python interpreter does not, and the naive
    `python -m ds5app tray` is BROKEN AT LOGIN in a way that is invisible when
    you test it from a shell:

        A logon task's working directory is whatever the XML says (this module
        sets the exe's own directory), `ds5app` is not installed and not on
        PYTHONPATH, and the import fails -- silently, because the whole point
        of `pythonw` is that there is no console for the traceback to reach.

    So the source form carries its own sys.path entry instead of hoping for one.
    `-c` is used rather than `-m` for exactly that reason. The injected path is
    an r-string in SINGLE quotes: the whole `-c` program has to sit inside one
    pair of DOUBLE quotes when it is rendered as a command line, and a Windows
    path ending in a backslash would otherwise escape the quote that closes it.
    """
    exe = (exe or "").strip().strip('"')
    if not exe:
        raise AutostartError("no launcher to register for start-at-login.")
    args = tuple(a for a in extra_args if a)
    if not is_python(exe):
        # A launcher knows it is the tray; only what comes AFTER the verb
        # is worth passing (entry_tray.py adds "tray" itself).
        return exe, " ".join(a for a in args if a != "tray")
    root = (app_dir or APP_DIR).rstrip("\\/")
    argv = list(args) or ["tray"]
    if argv[0] != "tray":
        argv.insert(0, "tray")
    rendered = ",".join(f"'{a}'" for a in argv)
    program = (f"import sys;sys.path.insert(0,r'{root}');"
               f"from ds5app.cli import main;sys.exit(main([{rendered}]))")
    return exe, f'-c "{program}"'


def build_command(exe: str, app_dir: str | None = None) -> str:
    """The one-line form: quoted launcher, then its arguments (if any)."""
    command, arguments = build_action(exe, app_dir)
    return quote(command) + (f" {arguments}" if arguments else "")


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
    """What `enable()` would register. Useful for showing the user, and for tests."""
    return build_command(exe or resolve_exe())


# ---------------------------------------------------------------------------
# the task document -- pure as well
# ---------------------------------------------------------------------------


def current_user() -> str:
    """`DOMAIN\\user` for the account this process runs as.

    From the environment rather than a token API: it is what Task Scheduler
    itself displays, it is right for local and domain accounts alike, and it is
    the same answer in an elevated and an unelevated process of the same user
    -- which matters, because the installer's helper registers this task from
    an elevated process on behalf of the person who is logged on.
    """
    user = os.environ.get("USERNAME") or ""
    if not user:
        try:
            import getpass

            user = getpass.getuser()
        except Exception:  # noqa: BLE001
            user = ""
    domain = os.environ.get("USERDOMAIN") or os.environ.get("COMPUTERNAME") or ""
    return f"{domain}\\{user}" if domain and user else user


def task_xml(command: str, arguments: str = "", working_dir: str = "",
             user: str | None = None) -> str:
    """The Task Scheduler XML for our task. Every choice is spelled out in the
    module docstring; the element ORDER is the schema's and is not negotiable
    (`schtasks /Create /XML` rejects a reordered document).

    The document says UTF-16 because that is what schtasks expects to read
    from a file; `_write_task_xml` writes it that way.
    """
    user = user if user is not None else current_user()
    if not working_dir:
        working_dir = os.path.dirname(command) or ""
    args_el = (f"      <Arguments>{_xml_escape(arguments)}</Arguments>\n"
               if arguments else "")
    wd_el = (f"      <WorkingDirectory>{_xml_escape(working_dir)}</WorkingDirectory>\n"
             if working_dir else "")
    return (
        '<?xml version="1.0" encoding="UTF-16"?>\n'
        f'<Task version="1.4" xmlns="{_TASK_NS}">\n'
        "  <RegistrationInfo>\n"
        "    <Author>ds5bridge</Author>\n"
        "    <Description>Starts the ds5bridge tray when you log in, with "
        "administrator rights and no prompt. Written by ds5bridge (the tray "
        "menu's \"Start at login\" switch, or its installer); ds5bridge "
        "removes it when that switch is turned off or it is uninstalled."
        "</Description>\n"
        f"    <URI>\\{TASK_NAME}</URI>\n"
        "  </RegistrationInfo>\n"
        "  <Triggers>\n"
        "    <LogonTrigger>\n"
        "      <Enabled>true</Enabled>\n"
        f"      <UserId>{_xml_escape(user)}</UserId>\n"
        "    </LogonTrigger>\n"
        "  </Triggers>\n"
        "  <Principals>\n"
        '    <Principal id="Author">\n'
        f"      <UserId>{_xml_escape(user)}</UserId>\n"
        "      <LogonType>InteractiveToken</LogonType>\n"
        "      <RunLevel>HighestAvailable</RunLevel>\n"
        "    </Principal>\n"
        "  </Principals>\n"
        "  <Settings>\n"
        "    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>\n"
        "    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>\n"
        "    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>\n"
        "    <AllowHardTerminate>false</AllowHardTerminate>\n"
        "    <StartWhenAvailable>true</StartWhenAvailable>\n"
        "    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>\n"
        "    <IdleSettings>\n"
        "      <StopOnIdleEnd>false</StopOnIdleEnd>\n"
        "      <RestartOnIdle>false</RestartOnIdle>\n"
        "    </IdleSettings>\n"
        "    <AllowStartOnDemand>true</AllowStartOnDemand>\n"
        "    <Enabled>true</Enabled>\n"
        "    <Hidden>false</Hidden>\n"
        "    <RunOnlyIfIdle>false</RunOnlyIfIdle>\n"
        "    <WakeToRun>false</WakeToRun>\n"
        "    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>\n"
        "    <Priority>4</Priority>\n"
        "  </Settings>\n"
        '  <Actions Context="Author">\n'
        "    <Exec>\n"
        f"      <Command>{_xml_escape(command)}</Command>\n"
        f"{args_el}{wd_el}"
        "    </Exec>\n"
        "  </Actions>\n"
        "</Task>\n"
    )


def parse_task_xml(text: str) -> dict:
    """The fields we care about out of a task document (ours or an export).

    `{"command", "arguments", "working_dir", "user", "run_level"}`, each ""
    when absent. Tolerant: a document that does not parse as XML is scanned
    with regular expressions, because a `current_command()` that raised on a
    task somebody edited by hand would take the tray's menu down with it.
    """
    out = {"command": "", "arguments": "", "working_dir": "", "user": "",
           "run_level": ""}
    if not text:
        return out
    try:
        import xml.etree.ElementTree as ET

        root = ET.fromstring(text.strip())
        ns = {"t": _TASK_NS}

        def find(path: str) -> str:
            el = root.find(path, ns)
            return (el.text or "").strip() if el is not None else ""

        out["command"] = find("t:Actions/t:Exec/t:Command")
        out["arguments"] = find("t:Actions/t:Exec/t:Arguments")
        out["working_dir"] = find("t:Actions/t:Exec/t:WorkingDirectory")
        out["user"] = find("t:Principals/t:Principal/t:UserId")
        out["run_level"] = find("t:Principals/t:Principal/t:RunLevel")
        return out
    except Exception:  # noqa: BLE001
        pass
    for key, tag in (("command", "Command"), ("arguments", "Arguments"),
                     ("working_dir", "WorkingDirectory"), ("user", "UserId"),
                     ("run_level", "RunLevel")):
        m = re.search(rf"<{tag}>(.*?)</{tag}>", text, re.S)
        if m:
            out[key] = m.group(1).strip()
    return out


# ---------------------------------------------------------------------------
# schtasks
# ---------------------------------------------------------------------------


def _decode(raw: bytes) -> str:
    """schtasks writes UTF-16 with a BOM when it prints XML and the console
    code page otherwise; both arrive here as bytes."""
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16", "replace")
    for enc in ("utf-8", "mbcs" if sys.platform == "win32" else "latin-1"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("latin-1", "replace")


def _run_schtasks(args: list[str]) -> tuple[int, str]:
    """`schtasks.exe <args>` -> (exit code, combined output). Never raises.

    `stdin=DEVNULL` for the same reason hidhide.CliBackend needs it: a console
    child that inherits a live stdin can sit there. Module-level and looked up
    at call time, so the tests replace it with a recorder.
    """
    try:
        r = subprocess.run(["schtasks.exe", *args], capture_output=True,
                           timeout=30, stdin=subprocess.DEVNULL,
                           creationflags=_CREATE_NO_WINDOW)
        return r.returncode, _decode(r.stdout or b"") + _decode(r.stderr or b"")
    except Exception as e:  # noqa: BLE001
        log.debug("schtasks %s failed: %s", args, e)
        return -1, str(e)


def _schtasks(args: list[str]) -> tuple[int, str]:
    # Indirection so a test can rebind `_run_schtasks` on the module and have
    # every caller in this file see the fake.
    return _run_schtasks(args)


def _write_task_xml(text: str) -> str:
    """The document to a temp file, UTF-16 with BOM (what schtasks reads)."""
    fd, path = tempfile.mkstemp(prefix="ds5bridge-task-", suffix=".xml")
    with os.fdopen(fd, "w", encoding="utf-16", newline="\r\n") as f:
        f.write(text)
    return path


def available() -> bool:
    """Can start-at-login work here at all? False anywhere but Windows."""
    return sys.platform == "win32"


# ---------------------------------------------------------------------------
# which mechanism this machine uses
# ---------------------------------------------------------------------------

MODE_SERVICE = "service"
MODE_TASK = "task"
MODE_NONE = "none"


def _service_installed() -> bool:
    """Seam over winsvc.installed(); the tests rebind it."""
    try:
        from . import winsvc as W

        return W.installed()
    except Exception:  # noqa: BLE001
        return False


def _service_starts_at_boot() -> bool:
    try:
        from . import winsvc as W

        return W.starts_at_boot()
    except Exception:  # noqa: BLE001
        return False


def _service_set_start_at_boot(enabled: bool) -> None:
    from . import winsvc as W

    try:
        W.set_start_at_boot(enabled)
    except W.ServiceError as e:
        raise AutostartError(str(e)) from e


def mode() -> str:
    """`service` when the Windows service is registered (whatever its start
    type), else `task` when the logon task exists, else `none`. Never raises.
    The service wins when both exist: the installer removes the other when
    one is chosen, so both is a leftover, and the service is what actually
    starts first at boot."""
    if not available():
        return MODE_NONE
    if _service_installed():
        return MODE_SERVICE
    if query_task() is not None:
        return MODE_TASK
    return MODE_NONE


def label() -> str:
    """The tray row's text for the current mode."""
    return ("Start with Windows (service)" if mode() == MODE_SERVICE
            else "Start at login")


def describe() -> str:
    """One line for `doctor`: how (and whether) this machine starts ds5bridge."""
    m = mode()
    if m == MODE_SERVICE:
        try:
            from . import winsvc as W

            return f"yes -- Windows service '{W.SERVICE_NAME}' ({W.status_text()})"
        except Exception:  # noqa: BLE001
            return "yes -- Windows service"
    if m == MODE_TASK:
        return "yes -- " + (current_command() or "")
    return "no"


def query_task() -> dict | None:
    """The registered task's fields (`parse_task_xml`), or None when there is
    no task. Never raises."""
    if not available():
        return None
    code, out = _schtasks(["/Query", "/TN", TASK_NAME, "/XML", "ONE"])
    if code != 0:
        return None
    got = parse_task_xml(out)
    return got if got.get("command") else None


def current_command() -> str | None:
    """The registered command in its one-line form, or None. Never raises."""
    got = query_task()
    if not got:
        return None
    return quote(got["command"]) + (f" {got['arguments']}" if got["arguments"] else "")


def is_enabled() -> bool:
    """Does this machine start ds5bridge on its own? Never raises, never
    guesses True.

    Service mode: the service's start type is automatic. Task mode: the task
    exists -- deliberately not compared against `command()`: a task written by
    the frozen build and read back from a source checkout would compare
    unequal, and a checkbox that reads "off" while the machine really does
    start the program at login is worse than one that is merely out of date.
    """
    if not available():
        return False
    if _service_installed():
        return _service_starts_at_boot()
    return bool(current_command())


def enable(exe: str | None = None) -> None:
    """Register start-at-login. Idempotent -- re-creating the task is fine.

    Also removes the pre-0.5.0 Run value if one is there: a Run entry for an
    elevated program is a dead entry, and two mechanisms for one switch is one
    too many.
    """
    if not available():
        raise AutostartError(
            "start-at-login uses the Windows Task Scheduler, and this is not Windows.")
    launcher = exe or resolve_exe()
    cmd, args = build_action(launcher)
    if is_python(launcher):
        # This form works (build_action injects the sys.path entry), but it
        # pins start-at-login to THIS checkout at THIS path -- move or rename
        # the repo and the login silently stops working. The packaged build has
        # no such tie.
        log.info("registering a source checkout for start-at-login (%s %s); it "
                 "is tied to this directory. The packaged ds5bridge-tray.exe is "
                 "what should be registered on an installed machine.", cmd, args)
    xml_text = task_xml(cmd, args, os.path.dirname(cmd))
    path = ""
    code, out = -1, "the task document could not be written"
    try:
        path = _write_task_xml(xml_text)
        code, out = _schtasks(["/Create", "/TN", TASK_NAME, "/XML", path, "/F"])
    finally:
        if path:
            try:
                os.remove(path)
            except OSError:
                pass
    if code != 0:
        detail = " ".join((out or "").split())[:300]
        raise AutostartError(
            f"could not turn on start-at-login (schtasks exit {code}: {detail}). "
            f"This registers the scheduled task '{TASK_NAME}' with highest "
            f"privileges, which needs an administrator: the installed tray has "
            f"them; from a source checkout, run the tray from an elevated "
            f"prompt once to set it.")
    _delete_legacy_run_value()
    log.info("start-at-login enabled: task %s -> %s %s", TASK_NAME, cmd, args)


def disable() -> None:
    """Remove the task. Already absent is success, not an error."""
    if not available():
        raise AutostartError(
            "start-at-login uses the Windows Task Scheduler, and this is not Windows.")
    _delete_legacy_run_value()
    if query_task() is None:
        return
    code, out = _schtasks(["/Delete", "/TN", TASK_NAME, "/F"])
    if code != 0 and query_task() is not None:
        detail = " ".join((out or "").split())[:300]
        raise AutostartError(
            f"could not turn off start-at-login (schtasks exit {code}: {detail}). "
            f"Delete the task '{TASK_NAME}' in Task Scheduler by hand.")
    log.info("start-at-login disabled")


def set_enabled(enabled: bool, exe: str | None = None) -> None:
    """Apply `config.autostart_on_login`. One call for a checkbox handler.

    Under the service this flips the service's start type and nothing else:
    the service stays registered and the running tray keeps running; only
    the next boot changes. Under the task it registers or deletes the task.
    """
    if available() and _service_installed():
        _service_set_start_at_boot(bool(enabled))
        log.info("start-with-Windows (service) %s", "on" if enabled else "off")
        return
    if enabled:
        enable(exe)
    else:
        disable()


# ---------------------------------------------------------------------------
# the pre-0.5.0 Run value
# ---------------------------------------------------------------------------


def legacy_run_value() -> str | None:
    """The old HKCU Run value, or None. Never raises."""
    if not available():
        return None
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_READ) as key:
            value, _kind = winreg.QueryValueEx(key, VALUE_NAME)
        return value if isinstance(value, str) and value else None
    except OSError:
        return None


def _delete_legacy_run_value() -> bool:
    """Best effort; True when a value was removed. Logged, never raised."""
    if not available():
        return False
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, VALUE_NAME)
        log.info("removed the pre-0.5.0 Run value '%s'", VALUE_NAME)
        return True
    except FileNotFoundError:
        return False
    except OSError as e:
        log.warning("could not remove the old Run value '%s': %s", VALUE_NAME, e)
        return False


def migrate_legacy() -> bool:
    """Turn a pre-0.5.0 Run value into the task, once. True when it did.

    Only when the value points at one of OUR launchers (the tray exe, or a
    python form that names ds5app) -- a value somebody else wrote under the
    same name is not ours to rewrite -- and only when the task does not exist
    yet. Registering the task needs elevation; when that fails the value is
    left alone so the next elevated start can try again, and the failure is a
    log line, never an exception: this runs during the tray's startup.
    """
    old = legacy_run_value()
    if not old:
        return False
    low = old.lower()
    if TRAY_EXE not in low and "ds5app" not in low and "ds5bridge" not in low:
        log.info("Run value '%s' (%s) is not ours; leaving it", VALUE_NAME, old)
        return False
    if _service_installed():
        # The service starts ds5bridge; a Run value would start a SECOND tray
        # at logon (and could not, being elevated). Just drop it.
        _delete_legacy_run_value()
        return True
    if query_task() is not None:
        # The task already exists; the value is just a leftover.
        _delete_legacy_run_value()
        return True
    try:
        enable()
    except AutostartError as e:
        log.warning("start-at-login is still the old Run entry, which cannot "
                    "start the elevated tray; it will be migrated on the next "
                    "elevated start (%s)", e)
        return False
    log.info("migrated start-at-login from the Run key to the task '%s'",
             TASK_NAME)
    return True
