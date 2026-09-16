"""Hiding the Bluetooth DualSense from everything except us, via HidHide.

While a controller is bridged Windows has TWO DualSenses -- the real Bluetooth
one and the virtual wired one -- and some games assign both to player slots, or
prefer the wrong one. HidHide is a kernel upper-filter on the HID classes that
fails `CreateFile` on a blacklisted device for every process that is not on its
whitelist, which is the only mechanism that expresses what we actually want:
hidden from everyone, still open to the bridge. `docs/hidhide-scoping.md` is the
design; this is the only file in the app that knows HidHide exists.

The three rules this module is built around
-------------------------------------------
**1. Nothing here may ever raise at a caller.** Same contract as `config.load()`
and for the same reason: hiding is a convenience, bridging is the product, and a
HidHide problem must never be why a bridge fails or why the tray icon does not
appear. Every public function swallows and logs. `detect()` returning None is
the normal "not installed" answer, not an error.

**2. No imports outside the standard library.** `doctor` has to be able to
report on HidHide from a machine where hidapi, pystray or Pillow are broken --
that is precisely the machine somebody is running `doctor` on. hidapi is
imported inside `resolve_serial()` and nowhere else, and that function has a
CLI-based fallback for when the import fails.

**3. Hiding is a DEBT, and this module is how it is always repaid.** HidHide's
blacklist is registry state: it survives our crash, a `taskkill /F`, a bugcheck
and a reboot. Nothing unhides on its own. So every hide is preceded by a journal
entry on disk (`%APPDATA%\\ds5bridge\\hidden\\<serial>.json`) and every process
start runs `sweep()`, which unhides anything whose owning process is gone. The
ordering -- journal BEFORE hiding, delete AFTER unhiding -- is load-bearing: a
crash in that window leaves a record of something that is not hidden, and
unhiding an absent entry is a no-op, whereas the opposite order leaves something
hidden with no record of it, which is the one outcome that strands a user with a
controller they cannot use.

What H0 measured on this machine (Windows 11 25H2, build 26200)
---------------------------------------------------------------
The scoping document's section 3 recommended "IOCTL for writes, registry for
reads, CLI as a fallback". Two of those three turned out to be wrong here, and
the backend order below is what the hardware actually supports:

* **CLI: works, every time.** HidHide issue #215 -- the report that
  `HidHideCLI.exe` dies at startup on exactly this build with
  `ERROR_INVALID_PARAMETER` out of `GetWhitelist()` -- does NOT reproduce.
  Every verb tried returned exit 0 with correct data.
* **IOCTL: works, but the control device comes and goes.** Immediately after
  installing, `\\\\.\\HidHide` opened and every released IOCTL answered. Minutes
  later the same call from the same elevated process failed with
  ERROR_FILE_NOT_FOUND, and the interface path
  (`\\\\?\\ROOT#SYSTEM#0007#{0c320ff7-...}`) failed with ERROR_INVALID_FUNCTION,
  for every combination of access mask and share mode. The devnode reports
  CM_PROB_NONE throughout and the CLI keeps working. The machine has not been
  rebooted since the install, and HidHide's own setup guide requires one, so
  this is most likely the pre-reboot state -- but "most likely" is not
  something to build on.
* **Registry: unusable.** `HKLM\\SYSTEM\\CurrentControlSet\\Services\\HidHide\\
  Parameters` has an EMPTY DACL. Reading it fails with ERROR_ACCESS_DENIED even
  from an elevated process, so the scoping document's option C -- "read the
  lists from the registry as the #215 workaround, and as doctor's source of
  truth" -- does not exist on this build. It is not implemented here.

The conclusion that survives both the pre- and post-reboot cases: **try the
IOCTL, fall back to the CLI, and re-decide on every single call.** Nothing
caches "the IOCTL works", because this machine demonstrates that answer
changing underneath a running process.

What 2026-08-27 measured, after "unhiding does not unhide"
-----------------------------------------------------------
A user turned the hide toggle off and the pad stayed invisible, while every
layer of this module reported success. Three separate defects were reproduced
on this machine, and each one on its own is enough to produce that outcome:

* **`HidHideCLI.exe` reads further commands from STDIN until EOF.** The verb on
  the command line is executed and printed, and then the process keeps reading.
  Measured, HidHide 1.5.230 on build 26200::

      HidHideCLI --version  <stdin=NUL>       -> "1.5.230.0", exit 0,  95 ms
      HidHideCLI --version  <stdin=console>   -> "1.5.230.0", then HANGS forever
      printf -- '--cloak-state\n' | HidHideCLI --version
                                              -> "1.5.230.0"  AND  "--cloak-on"

  That last line is the proof: the piped verb was executed too. `subprocess`
  inherits the parent's stdin unless told otherwise, so every CLI call this
  module made from a console host blocked until the 30 s timeout and came back
  as `(-1, "")` -- indistinguishable from "HidHide refused". The CLI fallback,
  which exists precisely for when the IOCTL device has gone away, was therefore
  dead exactly when it was needed. `CliBackend._run` now passes
  `stdin=subprocess.DEVNULL`, which is the whole fix and takes the call from
  "never returns" to 95 ms.

* **`--dev-list` prints re-runnable commands, not bare paths** -- the same shape
  `--app-list` uses, which this module already knew about and stripped for the
  whitelist only::

      --dev-hide "HID\\{00001124-...}_VID&0002054c_PID&0ce6\\8&2fde51c0&0&0000"

  `CliBackend.hidden()` returned that entire line as if it were a device
  instance path, so nothing ever compared equal to it.

* **Device instance paths are case-insensitive and every source spells them
  differently.** For one physical pad, on one boot::

      CM_Get_Device_Interface_PropertyW  HID\\{00001124-...f9b34fb}_VID&0002054c_...
      HidHideCLI --dev-gaming            HID\\{00001124-...f9b34fb}_VID&0002054c_...
      Get-PnpDevice                      HID\\{00001124-...F9B34FB}_VID&0002054C_...

  A case-sensitive `not in` comparison against the blacklist therefore matches
  nothing, and the old code read "I found nothing to remove" as "there is
  nothing to remove" and returned success. Verified here: `unhide([ID.upper()])`
  returned True with the entry still in the list afterwards.

The rule those three add up to, and the reason `unhide()` looks the way it does
now: **a removal is not a success because a call returned zero, it is a success
because the entry is verifiably gone from HidHide's own list.** Nothing in this
module may delete a journal record on any weaker evidence than a read-back.
Neither hiding nor unhiding needs elevation on this build -- both the write
IOCTL and `--dev-hide`/`--dev-unhide` were measured working from a normal
non-elevated process -- so a write failure here is a real failure to report,
never a silent "you should have run as admin".

Concurrency
-----------
Only one process may hold `\\\\.\\HidHide` open at a time, and every list change is
a whole-list read-modify-write, so two concurrent changers silently lose an
entry. With `ds5bridge --all` and two controllers this repo runs two children
that both want to hide. Every mutating sequence therefore runs inside the
`Local\\ds5bridge-hidhide` named mutex (`_Mutex` below) -- held across the whole
read-modify-write, never just the write.

`_Mutex` is the "wait, do not refuse" sibling of `service.InstanceLock`, which
is the same Win32 object used for the opposite purpose (its `acquire()` treats
ERROR_ALREADY_EXISTS as a reason to refuse, because a second bridge on one port
must not start). It lives here rather than in `service.py` because rule 2 above
forbids importing `service` -- that module pulls in `controller`, and therefore
hidapi, which is exactly the dependency `doctor` must work without.

This only serialises US. A user clicking in `HidHideClient.exe` at the same
moment can still lose an entry, which is one more reason `sweep()` exists.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import json
import logging
import os
import subprocess
import sys
import time

log = logging.getLogger("ds5app.hidhide")

_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

#: The named mutex every mutating sequence is serialised under. `Local\` rather
#: than `Global\`, for the reason `service.InstanceLock` gives: per-session is
#: the right scope, and `Global\` needs privileges a tray app should not want.
MUTEX_NAME = r"Local\ds5bridge-hidhide"

#: How long to wait for a sibling to finish its read-modify-write. Generous,
#: because the alternative to waiting is losing somebody's blacklist entry, and
#: the operations under the lock are milliseconds of registry work.
MUTEX_TIMEOUT_MS = 10_000

#: Image names that count as "one of ours" when `sweep()` decides whether a
#: journal entry still has a live owner. The image NAME, not the command line --
#: `service.process_name()` documents why at length: `Win32_Process.CommandLine`
#: comes back empty for some python processes, so a command-line match must
#: never be load-bearing.
OUR_IMAGES = ("ds5bridge.exe", "ds5bridge-tray.exe", "python.exe",
              "pythonw.exe", "python3.exe")

#: Where HidHide installs when nobody has moved it. Checked in order. There is
#: deliberately no registry lookup: the scoping document expected the install
#: path at `HKCR\SOFTWARE\Nefarius Software Solutions e.U.\...\Path`, and H0
#: found that key does not exist for 1.5.230 installed from the official
#: installer -- neither in HKLM nor HKCU, 32- or 64-bit view. The config's
#: `hidhide_cli` override is the escape hatch for a non-standard location.
_CLI_CANDIDATES = (
    r"C:\Program Files\Nefarius Software Solutions\HidHide\x64\HidHideCLI.exe",
    r"C:\Program Files\Nefarius Software Solutions e.U\HidHide\x64\HidHideCLI.exe",
    r"C:\Program Files\Nefarius Software Solutions\HidHide\HidHideCLI.exe",
)

#: The driver's own service key. Readable by anyone (unlike its Parameters
#: subkey), so it is a reliable "is HidHide installed" signal that costs no
#: process spawn and works when the control device is unavailable.
_SERVICE_KEY = r"SYSTEM\CurrentControlSet\Services\HidHide"
_DRIVER_SYS = r"C:\Windows\System32\drivers\HidHide.sys"


# ---------------------------------------------------------------------------
# IOCTL constants -- CTL_CODE(32769, N, METHOD_BUFFERED, FILE_READ_DATA)
# ---------------------------------------------------------------------------

def _ctl(function: int) -> int:
    return 0x80010000 | 0x4000 | (function << 2)


IOCTL_GET_WHITELIST = _ctl(2048)
IOCTL_SET_WHITELIST = _ctl(2049)
IOCTL_GET_BLACKLIST = _ctl(2050)
IOCTL_SET_BLACKLIST = _ctl(2051)
IOCTL_GET_ACTIVE = _ctl(2052)
IOCTL_SET_ACTIVE = _ctl(2053)
# 2054/2055 are the whitelist INVERSION flag. Deliberately absent: it is global,
# and flipping it would change the behaviour of every other HidHide consumer on
# the machine. 2056/2057 are the session blacklist, which would delete this
# module's entire crash-safety story -- H0 confirmed they are not implemented in
# 1.5.230 (both return ERROR_INVALID_PARAMETER). Re-check on each release.

_DEVICE_PATH = r"\\.\HidHide"

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3
_INVALID_HANDLE = ctypes.c_void_p(-1).value


# ---------------------------------------------------------------------------
# the named mutex
# ---------------------------------------------------------------------------


class _Mutex:
    """`Local\\ds5bridge-hidhide`, held across a whole read-modify-write.

    A context manager that WAITS rather than refusing -- the opposite of
    `service.InstanceLock`, which uses the same Win32 object to make a second
    bridge on one port give up. Failing to acquire is not fatal here: it is
    logged and the operation proceeds unserialised, because refusing to unhide
    because a mutex was busy would be a worse outcome than a rare lost entry
    that `sweep()` will pick up anyway.

    Abandonment is handled: WAIT_ABANDONED (0x80) means the previous owner died
    holding it, which leaves the list in whatever state it was in -- we take
    ownership and carry on, and the next `sweep()` reconciles.
    """

    def __init__(self, name: str = MUTEX_NAME, timeout_ms: int = MUTEX_TIMEOUT_MS):
        self.name = name
        self.timeout_ms = timeout_ms
        self._h = None

    def __enter__(self) -> "_Mutex":
        if sys.platform != "win32":
            return self
        try:
            k32 = ctypes.windll.kernel32
            k32.CreateMutexW.restype = ctypes.c_void_p
            h = k32.CreateMutexW(None, False, self.name)
            if not h:
                return self
            rc = k32.WaitForSingleObject(ctypes.c_void_p(h), self.timeout_ms)
            if rc in (0x00000000, 0x00000080):      # SIGNALED, ABANDONED
                self._h = h
            else:
                log.warning("could not take %s within %d ms (rc=0x%x); "
                            "proceeding unserialised", self.name,
                            self.timeout_ms, rc)
                k32.CloseHandle(ctypes.c_void_p(h))
        except Exception:  # noqa: BLE001
            log.debug("mutex acquire failed", exc_info=True)
        return self

    def __exit__(self, *exc) -> bool:
        if self._h is not None and sys.platform == "win32":
            try:
                k32 = ctypes.windll.kernel32
                k32.ReleaseMutex(ctypes.c_void_p(self._h))
                k32.CloseHandle(ctypes.c_void_p(self._h))
            except Exception:  # noqa: BLE001
                pass
        self._h = None
        return False


# ---------------------------------------------------------------------------
# MULTI_SZ
# ---------------------------------------------------------------------------


def pack_multi_sz(items) -> bytes:
    """A list of strings as a MULTI_SZ, terminators included.

    DEVELOPER.md is explicit that the buffer "must be an even number of bytes
    and include all terminators". An empty list is a single L'\\0', which is the
    correct way to say "the list is now empty" and is how a full unhide clears
    the blacklist.
    """
    return ("".join(s + "\0" for s in items) + "\0").encode("utf-16-le")


def unpack_multi_sz(raw: bytes) -> list[str]:
    """MULTI_SZ bytes back to a list, dropping the terminators and any blanks."""
    if not raw:
        return []
    text = raw[:len(raw) - (len(raw) % 2)].decode("utf-16-le", errors="replace")
    return [s for s in text.split("\0") if s]


# ---------------------------------------------------------------------------
# NT path <-> DOS path
# ---------------------------------------------------------------------------


def _volume_map() -> list[tuple[str, str]]:
    """[(dos "C:", nt "\\Device\\HarddiskVolume3"), ...] for every drive letter.

    The whitelist stores volume-relative NT paths, which is why this exists at
    all. `QueryDosDeviceW` is the supported translation; guessing at
    HarddiskVolume numbers is not.
    """
    out = []
    if sys.platform != "win32":
        return out
    buf = ctypes.create_unicode_buffer(1024)
    for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        dos = letter + ":"
        try:
            if ctypes.windll.kernel32.QueryDosDeviceW(dos, buf, 1024):
                out.append((dos, buf.value))
        except Exception:  # noqa: BLE001
            continue
    return out


def dos_to_nt(path: str) -> str:
    """`C:\\x\\y.exe` -> `\\Device\\HarddiskVolume3\\x\\y.exe`.

    Realpath first. HidHide issue #79: a whitelisted path reached through a
    directory junction does not match, because the driver compares the image
    path the kernel reports, which is always the resolved one.
    """
    # The NT check comes FIRST. `realpath` treats `\Device\HarddiskVolume3\...`
    # as a rooted path on the CURRENT DRIVE and cheerfully prefixes it with that
    # drive's own device name, so realpath-then-check silently produces
    # `\Device\HarddiskVolume6\Device\HarddiskVolume3\...` -- a whitelist entry
    # that matches nothing, for a caller that passed an already-correct path.
    if path.startswith("\\Device\\"):
        return path
    try:
        path = os.path.realpath(path)
    except Exception:  # noqa: BLE001
        pass
    for dos, nt in _volume_map():
        if path[:2].upper() == dos:
            return nt + path[2:]
    return path


def nt_to_dos(path: str) -> str:
    """The inverse, best effort. Anything untranslatable comes back unchanged."""
    if not path.startswith("\\Device\\"):
        return path
    for dos, nt in _volume_map():
        if path.lower().startswith(nt.lower() + "\\"):
            return dos + path[len(nt):]
    return path


# ---------------------------------------------------------------------------
# the two backends
# ---------------------------------------------------------------------------


class IoctlBackend:
    """`DeviceIoControl` against `\\.\\HidHide`. Preferred: no process spawn.

    Every method returns None to mean "this backend could not answer", which is
    what makes the CLI fallback in `HidHide` possible. That is not a theoretical
    path here -- see the module docstring: on this machine the control device
    disappeared minutes after a successful open and stayed gone.
    """

    def _open(self, write: bool = False):
        if sys.platform != "win32":
            return None
        access = GENERIC_READ | (GENERIC_WRITE if write else 0)
        try:
            k32 = ctypes.windll.kernel32
            k32.CreateFileW.restype = wintypes.HANDLE
            h = k32.CreateFileW(_DEVICE_PATH, access,
                                FILE_SHARE_READ | FILE_SHARE_WRITE, None,
                                OPEN_EXISTING, 0, None)
        except Exception:  # noqa: BLE001
            return None
        if not h or h == _INVALID_HANDLE:
            return None
        return h

    @staticmethod
    def _close(h) -> None:
        try:
            ctypes.windll.kernel32.CloseHandle(wintypes.HANDLE(h))
        except Exception:  # noqa: BLE001
            pass

    def _get_list(self, code: int) -> list[str] | None:
        h = self._open()
        if h is None:
            return None
        try:
            got = wintypes.DWORD(0)
            k32 = ctypes.windll.kernel32
            # Size probe: a zero-length output buffer reports the requirement.
            k32.DeviceIoControl(wintypes.HANDLE(h), code, None, 0, None, 0,
                                ctypes.byref(got), None)
            need = got.value
            if need <= 0:
                return []
            buf = ctypes.create_string_buffer(need)
            ok = k32.DeviceIoControl(wintypes.HANDLE(h), code, None, 0, buf,
                                     need, ctypes.byref(got), None)
            if not ok:
                return None
            return unpack_multi_sz(buf.raw[:got.value])
        except Exception:  # noqa: BLE001
            log.debug("ioctl get 0x%x failed", code, exc_info=True)
            return None
        finally:
            self._close(h)

    def _set_list(self, code: int, items) -> bool:
        h = self._open(write=True)
        if h is None:
            return False
        try:
            payload = pack_multi_sz(items)
            buf = ctypes.create_string_buffer(payload, len(payload))
            got = wintypes.DWORD(0)
            ok = ctypes.windll.kernel32.DeviceIoControl(
                wintypes.HANDLE(h), code, buf, len(payload), None, 0,
                ctypes.byref(got), None)
            return bool(ok)
        except Exception:  # noqa: BLE001
            log.debug("ioctl set 0x%x failed", code, exc_info=True)
            return False
        finally:
            self._close(h)

    # -- the interface the facade uses ------------------------------------

    def hidden(self) -> list[str] | None:
        return self._get_list(IOCTL_GET_BLACKLIST)

    def allowed(self) -> list[str] | None:
        got = self._get_list(IOCTL_GET_WHITELIST)
        return None if got is None else [nt_to_dos(p) for p in got]

    def set_hidden(self, items) -> bool:
        return self._set_list(IOCTL_SET_BLACKLIST, items)

    def set_allowed(self, items) -> bool:
        return self._set_list(IOCTL_SET_WHITELIST, [dos_to_nt(p) for p in items])

    def active(self) -> bool | None:
        """The global cloak flag.

        EXACTLY ONE BYTE. H0 measured this: the BOOLEAN IOCTLs answer correctly
        with a 1-byte output buffer and fail with ERROR_INVALID_PARAMETER for a
        4-byte one, and the zero-length size probe that the MULTI_SZ calls need
        is itself an invalid parameter here. A `sizeof(BOOL)` buffer, which is
        the obvious thing to write, is the wrong thing to write.
        """
        h = self._open()
        if h is None:
            return None
        try:
            buf = ctypes.create_string_buffer(1)
            got = wintypes.DWORD(0)
            ok = ctypes.windll.kernel32.DeviceIoControl(
                wintypes.HANDLE(h), IOCTL_GET_ACTIVE, None, 0, buf, 1,
                ctypes.byref(got), None)
            return bool(buf.raw[0]) if ok else None
        except Exception:  # noqa: BLE001
            return None
        finally:
            self._close(h)

    def set_active(self, on: bool) -> bool:
        h = self._open(write=True)
        if h is None:
            return False
        try:
            buf = ctypes.create_string_buffer(bytes([1 if on else 0]), 1)
            got = wintypes.DWORD(0)
            return bool(ctypes.windll.kernel32.DeviceIoControl(
                wintypes.HANDLE(h), IOCTL_SET_ACTIVE, buf, 1, None, 0,
                ctypes.byref(got), None))
        except Exception:  # noqa: BLE001
            return False
        finally:
            self._close(h)


class CliBackend:
    """`HidHideCLI.exe`. The fallback, and on this machine the reliable one.

    Its great advantage is that it owns the read-modify-write and the DOS ->
    full-image-name conversion, so `--dev-hide` really is add-one-entry rather
    than replace-the-list. Its cost is a process spawn per operation, which is
    tens of milliseconds at bridge start and stop and never on the input path.
    """

    def __init__(self, exe: str):
        self.exe = exe

    def _run(self, *args) -> tuple[int, str]:
        """One CLI invocation. `stdin=DEVNULL` is load-bearing, not tidiness.

        `HidHideCLI.exe` executes the verbs on its command line and then goes on
        reading MORE verbs from stdin until EOF -- the module docstring has the
        measurement that proves it, including a piped `--cloak-state` being
        obeyed by a process invoked as `--version`. `subprocess` inherits the
        parent's stdin by default, so from a console host (`ds5bridge run`, the
        dev launcher, any terminal) the child never saw EOF and never exited:
        every verb blocked for the full timeout and came back as a failure.

        That is what made "unhide reports success but the pad stays hidden"
        possible, because the CLI is the fallback for exactly the situation --
        the `\\\\.\\HidHide` control device having gone away -- in which unhiding
        matters most. NUL gives an immediate EOF, and the same call that never
        returned now takes 95 ms.
        """
        try:
            r = subprocess.run([self.exe, *args], capture_output=True, text=True,
                               timeout=30, stdin=subprocess.DEVNULL,
                               creationflags=_CREATE_NO_WINDOW)
            return r.returncode, (r.stdout or "") + (r.stderr or "")
        except Exception as e:  # noqa: BLE001
            log.debug("HidHideCLI %s failed: %s", args, e)
            return -1, ""

    def version(self) -> str:
        code, out = self._run("--version")
        return out.strip() if code == 0 else ""

    @staticmethod
    def _parse_listing(out: str, verb: str) -> list[str]:
        """Both HidHide listings print re-runnable commands, not bare values::

            --app-reg  "C:\\Program Files\\...\\HidHideCLI.exe"
            --dev-hide "HID\\{00001124-...}_VID&0002054c_PID&0ce6\\8&2fde...&0&0000"

        so the value is what is inside the quotes. This module knew that about
        `--app-list` and not about `--dev-list`, and the consequence was that
        `hidden()` handed every caller the string `--dev-hide "HID\\..."` as if
        it were a device instance path. Nothing ever compared equal to it, so
        `unhide()` concluded there was nothing to remove and reported success
        while the pad stayed cloaked, and `ds5bridge unhide --all-hidhide`
        offered to clear a list of entries none of which it could match.

        A bare value is still accepted: it costs one branch, and being wrong
        about which shape a HidHide release prints is how this got here.
        """
        values = []
        for line in (out or "").splitlines():
            line = line.strip()
            if not line:
                continue
            if '"' in line:
                parts = line.split('"')
                if len(parts) >= 2 and parts[1].strip():
                    values.append(parts[1].strip())
            elif line.startswith(verb):
                rest = line[len(verb):].strip()
                if rest:
                    values.append(rest)
            elif not line.startswith("--"):
                values.append(line)
        return values

    def hidden(self) -> list[str] | None:
        code, out = self._run("--dev-list")
        if code != 0:
            return None
        return self._parse_listing(out, "--dev-hide")

    def allowed(self) -> list[str] | None:
        code, out = self._run("--app-list")
        if code != 0:
            return None
        return self._parse_listing(out, "--app-reg")

    def hide_one(self, instance_id: str) -> bool:
        return self._run("--dev-hide", instance_id)[0] == 0

    def unhide_one(self, instance_id: str) -> bool:
        return self._run("--dev-unhide", instance_id)[0] == 0

    def allow_one(self, path: str) -> bool:
        return self._run("--app-reg", path)[0] == 0

    def unallow_one(self, path: str) -> bool:
        return self._run("--app-unreg", path)[0] == 0

    def active(self) -> bool | None:
        code, out = self._run("--cloak-state")
        if code != 0:
            return None
        text = out.strip().lower()
        if "--cloak-on" in text:
            return True
        if "--cloak-off" in text:
            return False
        return None

    def set_active(self, on: bool) -> bool:
        return self._run("--cloak-on" if on else "--cloak-off")[0] == 0

    def gaming_devices(self) -> list[dict]:
        """`--dev-all` / `--dev-gaming` JSON, flattened to one dict per device.

        H0 found this is a complete serial -> instance-path map on its own:
        every entry carries `serialNumber`, `deviceInstancePath` and
        `baseContainerDeviceInstancePath`. That makes it a genuine fallback for
        `resolve_serial()` on a machine where hidapi cannot be imported, which
        is exactly the machine `doctor` runs on.
        """
        out = []
        for verb in ("--dev-gaming", "--dev-all"):
            code, text = self._run(verb)
            if code != 0 or not text.strip():
                continue
            try:
                data = json.loads(text)
            except ValueError:
                continue
            for group in data if isinstance(data, list) else []:
                for dev in (group or {}).get("devices", []) or []:
                    if isinstance(dev, dict):
                        out.append(dev)
            if out:
                break
        return out


# ---------------------------------------------------------------------------
# device instance resolution
# ---------------------------------------------------------------------------

def norm_instance_id(value: str) -> str:
    """A device instance path folded to its comparable form.

    Windows device instance paths are case-INSENSITIVE, and on 2026-08-27 this
    machine produced three different spellings of one physical DualSense within
    a single boot: `CM_Get_Device_Interface_PropertyW` and HidHide's own
    `--dev-gaming` both say `..._VID&0002054c_PID&0ce6\\8&2fde51c0...`, while
    `Get-PnpDevice` -- and anything else reading the devnode directly -- says
    `..._VID&0002054C_PID&0CE6\\8&2FDE51C0...`.

    So `entry not in wanted` is not a membership test, it is a coin flip, and
    the side it lands on decides whether a user gets their controller back.
    Every comparison against HidHide's lists goes through here.

    The stray-quote strip is for the same reason `_parse_listing` exists: a
    value that arrived from a CLI listing may still be wearing them.
    """
    return (value or "").strip().strip('"').strip().casefold()


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]

    def __str__(self) -> str:
        d4 = "".join(f"{b:02X}" for b in self.Data4)
        return (f"{{{self.Data1:08X}-{self.Data2:04X}-{self.Data3:04X}-"
                f"{d4[:4]}-{d4[4:]}}}")


class _DEVPROPKEY(ctypes.Structure):
    _fields_ = [("fmtid", _GUID), ("pid", ctypes.c_ulong)]


def _guid(d1, d2, d3, *rest) -> _GUID:
    return _GUID(d1, d2, d3, (ctypes.c_ubyte * 8)(*rest))


GUID_DEVINTERFACE_HID = _guid(0x4D1E55B2, 0xF16F, 0x11CF,
                              0x88, 0xCB, 0x00, 0x11, 0x11, 0x00, 0x00, 0x30)

#: {78c34fc8-104a-4aca-9ea4-524d52996e57}, 256 -- the one devnode property that
#: can also be queried straight off a device INTERFACE, which is what makes
#: interface-path -> instance-ID a single supported call.
DEVPKEY_Device_InstanceId = _DEVPROPKEY(
    _guid(0x78C34FC8, 0x104A, 0x4ACA,
          0x9E, 0xA4, 0x52, 0x4D, 0x52, 0x99, 0x6E, 0x57), 256)

#: {8c7ed206-3f8a-4827-b3ab-ae9e1faefc6c}, 2 -- the device container. Hiding by
#: container is what makes "hide this controller" hide ALL of its HID
#: collections rather than only the gamepad one; HidHide's own GUI groups by
#: exactly this.
DEVPKEY_Device_ContainerId = _DEVPROPKEY(
    _guid(0x8C7ED206, 0x3F8A, 0x4827,
          0xB3, 0xAB, 0xAE, 0x9E, 0x1F, 0xAE, 0xFC, 0x6C), 2)

_CR_SUCCESS = 0
_CR_BUFFER_SMALL = 26


def _cfgmgr():
    return ctypes.WinDLL("cfgmgr32", use_last_error=True)


def instance_id_for_interface(interface_path: str) -> str:
    """A hidapi interface path -> the device instance ID HidHide wants. "" on failure.

    These two look close enough to tempt string surgery::

        \\\\?\\HID#{0000...}_VID&0002054c_PID&0ce6#9&28da590b&0&0000#{4d1e...}
        HID\\{0000...}_VID&0002054C_PID&0CE6\\9&28DA590B&0&0000

    Swapping `#` for `\\` and dropping the trailing GUID happens to work for
    this shape today and silently produces a NON-MATCHING blacklist entry the
    day a `&Col02` suffix or a container ID moves. There is a supported call
    that returns the real answer, so ask Windows.
    """
    if sys.platform != "win32":
        return ""
    try:
        cm = _cfgmgr()
        ptype = ctypes.c_ulong(0)
        size = ctypes.c_ulong(0)
        cr = cm.CM_Get_Device_Interface_PropertyW(
            ctypes.c_wchar_p(interface_path),
            ctypes.byref(DEVPKEY_Device_InstanceId), ctypes.byref(ptype),
            None, ctypes.byref(size), 0)
        if cr != _CR_BUFFER_SMALL or not size.value:
            return ""
        buf = ctypes.create_string_buffer(size.value)
        cr = cm.CM_Get_Device_Interface_PropertyW(
            ctypes.c_wchar_p(interface_path),
            ctypes.byref(DEVPKEY_Device_InstanceId), ctypes.byref(ptype),
            buf, ctypes.byref(size), 0)
        if cr != _CR_SUCCESS:
            return ""
        return buf.raw[:size.value].decode("utf-16-le", "replace").rstrip("\0")
    except Exception:  # noqa: BLE001
        log.debug("instance id lookup failed for %s", interface_path,
                  exc_info=True)
        return ""


def _container_id(instance_id: str) -> str:
    """DEVPKEY_Device_ContainerId for a device instance ID, as "{...}". "" on failure."""
    if sys.platform != "win32" or not instance_id:
        return ""
    try:
        cm = _cfgmgr()
        devinst = ctypes.c_ulong(0)
        if cm.CM_Locate_DevNodeW(ctypes.byref(devinst),
                                 ctypes.c_wchar_p(instance_id), 0) != _CR_SUCCESS:
            return ""
        ptype = ctypes.c_ulong(0)
        size = ctypes.c_ulong(ctypes.sizeof(_GUID))
        out = _GUID()
        cr = cm.CM_Get_DevNode_PropertyW(devinst,
                                         ctypes.byref(DEVPKEY_Device_ContainerId),
                                         ctypes.byref(ptype), ctypes.byref(out),
                                         ctypes.byref(size), 0)
        return str(out) if cr == _CR_SUCCESS else ""
    except Exception:  # noqa: BLE001
        return ""


def _all_hid_interfaces() -> list[str]:
    if sys.platform != "win32":
        return []
    try:
        cm = _cfgmgr()
        size = ctypes.c_ulong(0)
        if cm.CM_Get_Device_Interface_List_SizeW(
                ctypes.byref(size), ctypes.byref(GUID_DEVINTERFACE_HID), None,
                0) != _CR_SUCCESS:
            return []
        buf = ctypes.create_unicode_buffer(size.value)
        if cm.CM_Get_Device_Interface_ListW(
                ctypes.byref(GUID_DEVINTERFACE_HID), None, buf, size.value,
                0) != _CR_SUCCESS:
            return []
        return [s for s in buf[:size.value].split("\0") if s]
    except Exception:  # noqa: BLE001
        return []


def instances_in_container(instance_id: str) -> list[str]:
    """Every HID device instance sharing this one's container. Includes itself.

    `enumerate_devices()` filters on usage page 1 / usage 5, i.e. it looks at
    ONE collection. Blacklisting only that leaves the controller's other HID
    collections open and a tool that opens a different one still finds the pad.
    The right unit is the device container.
    """
    want = _container_id(instance_id)
    found = [instance_id] if instance_id else []
    if not want:
        return found
    for iface in _all_hid_interfaces():
        other = instance_id_for_interface(iface)
        if not other or other in found:
            continue
        if _container_id(other) == want:
            found.append(other)
    return found


def resolve_serial(serial: str, cli: "CliBackend | None" = None) -> list[str]:
    """A controller's bdaddr -> every HID instance ID of that controller.

    Resolved FRESH every time, never cached and never persisted as identity.
    HidHide discussion #63 is this exact hardware: unpair and re-pair a
    DualSense and its instance ID changes, so a stored path stops matching. The
    identity is the serial, as it is everywhere else in this codebase; a stored
    instance ID is only ever a cleanup token -- "we put this string in the
    blacklist, take it back out" -- and removing a stale one is harmless.

    hidapi first, because that is the same enumeration the bridge itself picks
    devices with. The CLI's `--dev-gaming` JSON is the fallback, and it is a
    real one: it needs no hidapi at all.
    """
    serial = (serial or "").strip().lower()
    if not serial:
        return []

    ids: list[str] = []
    try:
        from ds5bridge import device as DEV

        for info in DEV.enumerate_devices():
            if (info.serial or "").strip().lower() != serial:
                continue
            iid = instance_id_for_interface(info.path if isinstance(info.path, str)
                                            else info.path.decode("utf-8", "replace"))
            if iid and iid not in ids:
                ids.append(iid)
    except Exception:  # noqa: BLE001
        log.debug("hidapi enumeration unavailable for %s", serial, exc_info=True)

    if not ids and cli is not None:
        for dev in cli.gaming_devices():
            if (dev.get("serialNumber") or "").strip().lower() != serial:
                continue
            iid = (dev.get("deviceInstancePath") or "").strip()
            if iid and iid not in ids:
                ids.append(iid)

    out: list[str] = []
    for iid in ids:
        for member in instances_in_container(iid):
            if member not in out:
                out.append(member)
    return out


# ---------------------------------------------------------------------------
# bringing the devnode back -- what an unhide on its own does NOT do
# ---------------------------------------------------------------------------
#
# Measured on a user's machine, 2026-08-27, after a tray Quit:
#
#     HidHideCLI --dev-list        (empty)          <- the blacklist IS clear
#     hid_enumerate                no DualSense     <- and the pad is still gone
#
# Clearing the blacklist is not what makes a device usable again; it only stops
# HidHide failing the next IRP_MJ_CREATE. On that machine the HID child devnode
# under the pad's Bluetooth node had gone PHANTOM, and nothing re-creates a
# phantom child until somebody asks the bus to enumerate again. What fixed it:
#
#     pnputil /restart-device "BTHENUM\{00001124-...}_VID&0002054C_PID&0CE6\
#                              7&16440032&0&<MAC>_C00000000"
#
# -- on the PARENT, not on the HID child, and elevated.
#
# Two consequences run through everything below:
#
# 1. **An unhide is not finished until the pad is visible again.** Every removal
#    is followed by a re-enumeration and then VERIFIED with the same
#    `enumerate_devices()` the bridge itself uses. If it cannot be made visible,
#    the user is told to power-cycle the controller -- out loud, in the tray and
#    on the console. Silence here is what left a user with a dead pad and a
#    program insisting everything was fine.
#
# 2. **The instance ID that went in is not the instance ID that comes out.**
#    Same machine, same physical pad, across one hide/revive cycle:
#
#        hidden:  HID\{00001124-...}\8&2FDE51C0&0&0000
#        back as: HID\{00001124-...}\8&110FB383&11&0000   (&11& == re-enumerated)
#
#    So a recorded instance ID is a cleanup token for a string that may now be a
#    phantom, and matching by it alone misses the entry that actually matters.
#    The stable identity is the Bluetooth parent, whose own ID carries the MAC
#    and does not churn -- `bt_parent_for_serial()`.


#: `CM_Locate_DevNode` flags. PHANTOM is the whole point: the devnode this has
#: to reach is, by definition, one Windows is no longer presenting.
_CM_LOCATE_DEVNODE_NORMAL = 0x00000000
_CM_LOCATE_DEVNODE_PHANTOM = 0x00000001

#: `CM_Reenumerate_DevNode` flags. SYNCHRONOUS so the call returns after the
#: bus has actually looked, rather than after it has been asked to.
_CM_REENUMERATE_SYNCHRONOUS = 0x00000001

#: `CM_Get_Device_ID_List` filter: everything under one enumerator. Deliberately
#: NOT combined with FILTER_PRESENT -- a phantom BTHENUM node is still the right
#: thing to restart.
_CM_GETIDLIST_FILTER_ENUMERATOR = 0x00000001

#: The enumerator a Bluetooth HID device's parent lives under.
BT_ENUMERATOR = "BTHENUM"

#: How long to give the bus to re-create the HID child, and how often to look.
#: Two seconds is generous for a re-enumeration that has already returned
#: synchronously; it costs nothing on the happy path, which returns before any
#: of this on the first `pad_visible()` call.
REVIVE_SETTLE_S = 2.0
REVIVE_POLL_S = 0.25


def _hex_only(value: str) -> str:
    return "".join(c for c in (value or "").lower() if c in "0123456789abcdef")


def _locate_devnode(instance_id: str, phantom: bool = True):
    """A `DEVINST` for an instance ID, or None. Phantoms included by default."""
    if sys.platform != "win32" or not instance_id:
        return None
    try:
        cm = _cfgmgr()
        devinst = ctypes.c_ulong(0)
        flags = _CM_LOCATE_DEVNODE_PHANTOM if phantom else _CM_LOCATE_DEVNODE_NORMAL
        if cm.CM_Locate_DevNodeW(ctypes.byref(devinst),
                                 ctypes.c_wchar_p(instance_id.strip().strip('"')),
                                 flags) != _CR_SUCCESS:
            return None
        return devinst
    except Exception:  # noqa: BLE001
        log.debug("could not locate devnode %s", instance_id, exc_info=True)
        return None


def devnode_present(instance_id: str) -> bool:
    """Is this devnode PRESENT, as opposed to merely known to Windows?

    The difference between the two states this feature has to tell apart, and
    it is the whole of `revive_serial`'s first decision:

    * a controller that is switched off has no present BTHENUM node -- there is
      nothing wrong, nothing to repair, and nothing to tell the user;
    * a controller that is connected, whose BTHENUM node IS present but whose
      HID child has gone phantom, is the 2026-08-27 failure, and it is the only
      state in which restarting a devnode is the right thing to do.

    Without this, every ordinary "user switched the controller off" teardown
    would re-enumerate a Bluetooth node for nothing and then advise the user to
    power-cycle a controller they had just deliberately turned off.

    Measured again 2026-09-15 (Windows 11 26200, both pads, one switched off
    by feature 0x08): the FIRST bullet does not hold on this machine. All
    three BTHENUM nodes of a paired pad -- `Dev_<addr>`, the 00001124 HID
    service node and the 00001200 PnP-information node -- stay present and
    started (`CM_Get_DevNode_Status` 0x180600a) for as long as the pad is
    paired, on or off. What comes and goes with the link is the HID CHILD of
    the 00001124 node, which is why `hid_child_present()` -- not this -- is
    the presence witness `manager.poll_once` uses. This function still
    answers "does Windows currently present this node", which is the right
    question for a HID child instance path (the 2026-08-27 phantom).
    """
    return _locate_devnode(instance_id, phantom=False) is not None


#: `CM_Get_DevNode_Status` bits: the devnode's driver stack is up.
_DN_STARTED = 0x00000008


def _child_devinsts(devinst) -> list:
    """Present children of a devnode (`CM_Get_Child` + `CM_Get_Sibling`).

    The device tree these walk holds present devnodes only; a phantom child
    is reachable through `CM_Locate_DevNode(PHANTOM)` but is not a child
    here, which is exactly the distinction the caller wants.
    """
    out = []
    try:
        cm = _cfgmgr()
        child = ctypes.c_ulong(0)
        if cm.CM_Get_Child(ctypes.byref(child), devinst, 0) != _CR_SUCCESS:
            return out
        out.append(child.value)
        cur = child
        while True:
            sib = ctypes.c_ulong(0)
            if cm.CM_Get_Sibling(ctypes.byref(sib), cur, 0) != _CR_SUCCESS:
                break
            out.append(sib.value)
            cur = ctypes.c_ulong(sib.value)
    except Exception:  # noqa: BLE001
        log.debug("could not walk the children of devinst %s", devinst,
                  exc_info=True)
    return out


def _devnode_started(devinst) -> bool:
    try:
        cm = _cfgmgr()
        st = ctypes.c_ulong(0)
        pn = ctypes.c_ulong(0)
        if cm.CM_Get_DevNode_Status(ctypes.byref(st), ctypes.byref(pn),
                                    ctypes.c_ulong(devinst), 0) != _CR_SUCCESS:
            return False
        return bool(st.value & _DN_STARTED)
    except Exception:  # noqa: BLE001
        return False


def hid_child_present(serial: str):
    """Is this pad's Bluetooth link UP, as the PnP tree sees it? True/False/None.

    The HidHide-independent presence witness. HidHide filters the HID device
    set that `hid_enumerate` reads; it does not touch the PnP device tree,
    and the tree carries the one fact that tracks the radio link: a paired
    DualSense's BTHENUM HID-service node (`bt_parent_for_serial`, the
    00001124 one) has a HID child devnode while the pad is connected and
    NONE while it is off. Measured 2026-09-15 on both pads (see
    `devnode_present`): the BTHENUM nodes themselves never leave.

    * True  -- the service node is present and has a started HID child;
    * False -- the service node is present and has no started child: the
      pad is off (or, the 2026-08-27 case, its child has gone phantom --
      either way the link is not usable);
    * None  -- cannot say: not Windows, no 00001124 node for this address
      (never paired, or the enumerator listing failed), or an exception.

    Never raises; about a millisecond.
    """
    if sys.platform != "win32":
        return None
    try:
        parent = bt_parent_for_serial(serial)
        if not parent or "00001124" not in parent.lower():
            return None
        devinst = _locate_devnode(parent, phantom=False)
        if devinst is None:
            # Present nodes never leave on this machine (see above), so a
            # missing one is an unusual state -- unpaired mid-session -- and
            # "cannot say" is the honest answer rather than "off".
            return None
        return any(_devnode_started(c) for c in _child_devinsts(devinst))
    except Exception:  # noqa: BLE001
        log.debug("HID-child witness for %s failed", serial, exc_info=True)
        return None


def parent_instance_id(instance_id: str) -> str:
    """The devnode one level up. For a Bluetooth pad's HID child, its BTHENUM node.

    "" when it cannot be answered, which includes the case where the child does
    not exist even as a phantom -- there is then nothing to walk up FROM, and
    `bt_parent_for_serial()` is the way in.
    """
    devinst = _locate_devnode(instance_id)
    if devinst is None:
        return ""
    try:
        cm = _cfgmgr()
        parent = ctypes.c_ulong(0)
        if cm.CM_Get_Parent(ctypes.byref(parent), devinst, 0) != _CR_SUCCESS:
            return ""
        size = ctypes.c_ulong(0)
        if cm.CM_Get_Device_ID_Size(ctypes.byref(size), parent, 0) != _CR_SUCCESS:
            return ""
        buf = ctypes.create_unicode_buffer(size.value + 1)
        if cm.CM_Get_Device_IDW(parent, buf, size.value + 1, 0) != _CR_SUCCESS:
            return ""
        return buf.value
    except Exception:  # noqa: BLE001
        log.debug("could not read the parent of %s", instance_id, exc_info=True)
        return ""


def device_ids_for_enumerator(enumerator: str = BT_ENUMERATOR) -> list[str]:
    """Every device instance ID under one enumerator, phantoms included."""
    if sys.platform != "win32":
        return []
    try:
        cm = _cfgmgr()
        size = ctypes.c_ulong(0)
        if cm.CM_Get_Device_ID_List_SizeW(
                ctypes.byref(size), ctypes.c_wchar_p(enumerator),
                _CM_GETIDLIST_FILTER_ENUMERATOR) != _CR_SUCCESS or not size.value:
            return []
        buf = ctypes.create_unicode_buffer(size.value)
        if cm.CM_Get_Device_ID_ListW(
                ctypes.c_wchar_p(enumerator), buf, size.value,
                _CM_GETIDLIST_FILTER_ENUMERATOR) != _CR_SUCCESS:
            return []
        return [s for s in buf[:size.value].split("\0") if s]
    except Exception:  # noqa: BLE001
        log.debug("could not list the %s enumerator", enumerator, exc_info=True)
        return []


def bt_parent_for_serial(serial: str) -> str:
    """A controller's bdaddr -> its BTHENUM devnode ID, phantom or not. "" if none.

    Found by MAC rather than by walking up from the HID child, because the child
    is precisely what is missing whenever this is needed. Measured 2026-08-27::

        BTHENUM\\{00001124-0000-1000-8000-00805F9B34FB}_VID&0002054C_PID&0CE6\\
            7&16440032&0&D42F4BA1485D_C00000000

    The address is in the parent's own ID and it does not churn, which is what
    makes this the stable identity when the HID child's `8&2FDE51C0&0&0000` has
    already become `8&110FB383&11&0000`.

    A paired DualSense has more than one BTHENUM node (a device node and one
    service node per profile). The HID service node is the one that parents the
    gamepad, and it is the one carrying the HID service class GUID, so that is
    preferred; anything else matching the address is a fallback rather than a
    guess thrown away.
    """
    mac = _hex_only(serial)
    if not mac:
        return ""
    fallback = ""
    for dev in device_ids_for_enumerator(BT_ENUMERATOR):
        low = dev.lower()
        # The address appears as a bare hex run in the instance part; the rest
        # of the ID carries VID/PID hex too, so the address is looked for in the
        # LAST backslash-separated component only.
        if mac not in _hex_only(low.rsplit("\\", 1)[-1]):
            continue
        if "00001124" in low:
            return dev
        fallback = fallback or dev
    return fallback


def entries_under_parent(entries, parent_id: str) -> list[str]:
    """Which of these blacklist entries hang off `parent_id`. Phantoms included.

    The answer to "the instance ID we recorded is not the one in the list any
    more". Both the recorded token and the entry may name devnodes that no
    longer exist, and `_locate_devnode` finds phantoms, so the relationship is
    still readable after the child has churned.

    Narrow by construction: an entry belonging to somebody else's controller has
    a different parent, so a DS4Windows entry can never be swept up by this.
    """
    want = norm_instance_id(parent_id)
    if not want:
        return []
    out = []
    for entry in entries or []:
        parent = parent_instance_id(entry)
        if parent and norm_instance_id(parent) == want:
            out.append(entry)
    return out


def is_elevated() -> bool:
    """Is this process running as an administrator?

    Asked before `pnputil` is reached for, not after it fails: unelevated it
    fails with a message about administrator rights, and a failure we could have
    predicted is one we should be explaining to the user instead of surfacing as
    a mystery.

    Since 0.5.0 the TRAY runs elevated by design (`ds5bridge-tray.exe` carries a
    requireAdministrator manifest, and start-at-login is a scheduled task with
    RunLevel Highest for exactly that reason): restarting a devnode is the only
    fix for both "unhide does not unhide" and "hide does not hide on a fresh
    install", and neither can be done from an ordinary token. The console
    `ds5bridge.exe` and a source checkout still run unelevated, and everything
    here still works without elevation -- hiding and unhiding were measured
    working from a normal process -- so for THEM this stays an opportunity,
    never a requirement: the restart is skipped and the message says why.
    """
    if sys.platform != "win32":
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001
        return False


def _reenumerate(instance_id: str) -> bool:
    """Ask the bus to look at this devnode again. NO elevation required.

    The non-admin route, and the one tried first for that reason:
    `pnputil /restart-device` and SetupDi's `DICS_PROPCHANGE` both need an
    elevated process, and a tray utility that demands administrator rights to
    give a controller back is a tray utility people stop running.
    """
    devinst = _locate_devnode(instance_id)
    if devinst is None:
        return False
    try:
        cm = _cfgmgr()
        return cm.CM_Reenumerate_DevNode(
            devinst, _CM_REENUMERATE_SYNCHRONOUS) == _CR_SUCCESS
    except Exception:  # noqa: BLE001
        log.debug("re-enumerating %s failed", instance_id, exc_info=True)
        return False


def _pnputil_restart(instance_id: str) -> tuple[int, str]:
    """`pnputil /restart-device <id>` -> (exit code, output). Needs elevation.

    The measured fix on the machine that produced this whole section, and the
    heavier hammer: it tears the devnode down and brings it back, which is what
    finally re-created the HID child. `stdin=DEVNULL` for the same reason
    `CliBackend._run` needs it -- a console child that inherits a live stdin is
    a child that can sit there.
    """
    if sys.platform != "win32" or not instance_id:
        return (1, "")
    try:
        r = subprocess.run(["pnputil", "/restart-device", instance_id],
                           capture_output=True, text=True, timeout=60,
                           stdin=subprocess.DEVNULL,
                           creationflags=_CREATE_NO_WINDOW)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except Exception as e:  # noqa: BLE001
        return (1, str(e))


# ---------------------------------------------------------------------------
# the filter -- is HidHide actually IN this device's stack?
# ---------------------------------------------------------------------------
#
# Reported from a fresh install on a new machine, 2026-09-05: "hide controller"
# ticked, the tray says the pad is hidden, and every other program still sees
# it. HidHide's hide list is honoured by a class UPPER FILTER on the HID
# classes, and a class filter is attached to a device stack when that stack is
# BUILT -- at device start. A pad that was already paired and connected when
# HidHide was installed has a stack that was built without it, so its entry in
# the hide list is correct and does nothing, until the device is restarted or
# the machine rebooted. Every layer of this module said "hidden" on that
# machine because every layer was checking the LIST, and the list was right.
#
# The list is not the truth; the stack is. `DEVPKEY_Device_Stack` on a devnode
# is the ordered list of driver objects in its stack. Measured 2026-09-05 on a
# paired DualSense's HIDClass node under BTHENUM, after HidHide's reboot:
#
#     \Driver\HidHide  \Driver\HidBth  \Driver\steamxbox  \Driver\BthEnum
#
# and before that reboot the same node reads without the first entry. So a hide
# is EFFECTIVE only when `\Driver\HidHide` is in the stack of the device the
# entry names -- or of the HIDClass node it hangs off: HidHide sits where
# hidclass sits, and a Bluetooth pad's collection PDOs (`HID\...`, what the hide
# list names) hang under the BTHENUM HID service node that carries the filter.
# (The pad's other BTHENUM nodes -- `DEV_<mac>` and the non-HID service GUIDs --
# read `\Driver\BthEnum` alone and are not HID; they are never checked.)
#
# When the filter is missing and this process is elevated, `pnputil
# /restart-device` on that node rebuilds the stack with the filter in it -- the
# same call that revives a phantom child -- and on the machine where everything
# is fine the whole thing costs one property read per hide.

#: {540b947e-8b40-45bc-a8a2-6a0b894cbda2}, 14 -- DEVPKEY_Device_Stack, the
#: driver objects in a devnode's stack, top first. DEVPROP_TYPE_STRING_LIST.
#: (Not {3ab22e31-...}: that GUID is the DEVPKEY_PciDevice_* set, and asking
#: it for pid 14 answers CR_NO_SUCH_VALUE on every devnode -- measured
#: 2026-09-06, the reason the first 0.5.0 build reported every hide as
#: "unknown".)
DEVPKEY_Device_Stack = _DEVPROPKEY(
    _guid(0x540B947E, 0x8B40, 0x45BC,
          0xA8, 0xA2, 0x6A, 0x0B, 0x89, 0x4C, 0xBD, 0xA2), 14)

#: The driver object name HidHide's filter shows up under. Compared casefolded.
HIDHIDE_DRIVER_OBJECT = r"\Driver\HidHide"

#: The notes a hide can end with. `hide_note` in `/api/state` and `doctor`
#: carry exactly one of these (or ""), so the dashboard can say the truth
#: without knowing anything about device stacks.
NOTE_FILTER_UNKNOWN = ("could not read the device's driver stack, so whether "
                       "HidHide's filter is attached is unknown -- if games "
                       "still see the pad, restart the device or reboot")
NOTE_FILTER_MISSING = ("HidHide is on the hide list but its filter is not "
                       "attached to this device -- restart the device or "
                       "reboot")
NOTE_FILTER_MISSING_UNELEVATED = (
    NOTE_FILTER_MISSING + " (run ds5bridge as administrator and it restarts "
    "the device for you)")
NOTE_FILTER_STILL_MISSING = ("HidHide is on the hide list but its filter is "
                             "not attached to this device even after "
                             "restarting it -- reboot")
NOTE_FILTER_RESTARTED = ("HidHide's filter was not attached to this device; "
                         "ds5bridge restarted the device and it is now")


def device_stack(instance_id: str) -> list[str] | None:
    """DEVPKEY_Device_Stack for a PRESENT devnode, top driver first. None if it
    cannot be read -- which includes a devnode that is phantom or gone, because
    a stack that does not exist has nothing attached to it either way."""
    devinst = _locate_devnode(instance_id, phantom=False)
    if devinst is None:
        return None
    try:
        cm = _cfgmgr()
        ptype = ctypes.c_ulong(0)
        size = ctypes.c_ulong(0)
        cr = cm.CM_Get_DevNode_PropertyW(devinst, ctypes.byref(DEVPKEY_Device_Stack),
                                         ctypes.byref(ptype), None,
                                         ctypes.byref(size), 0)
        if cr != _CR_BUFFER_SMALL or not size.value:
            return None
        buf = ctypes.create_string_buffer(size.value)
        cr = cm.CM_Get_DevNode_PropertyW(devinst, ctypes.byref(DEVPKEY_Device_Stack),
                                         ctypes.byref(ptype), buf,
                                         ctypes.byref(size), 0)
        if cr != _CR_SUCCESS:
            return None
        return unpack_multi_sz(buf.raw[:size.value])
    except Exception:  # noqa: BLE001
        log.debug("could not read the device stack of %s", instance_id,
                  exc_info=True)
        return None


def filter_attached(instance_id: str) -> bool | None:
    """Is `\\Driver\\HidHide` in this devnode's stack? None = could not be read.

    The one question that decides whether a hide-list entry does anything. A
    None is a third answer and is never rounded: "unknown" is reported as
    unknown, because rounding it to True is the exact bug this exists to fix.
    """
    stack = device_stack(instance_id)
    if stack is None:
        return None
    want = HIDHIDE_DRIVER_OBJECT.casefold()
    return any((s or "").strip().casefold() == want for s in stack)


def filter_covers(instance_ids, parent_id: str = "") -> bool | None:
    """Does the filter cover every one of these hide-list entries?

    An entry is covered when HidHide is in its own stack OR in the stack of the
    devnode it hangs off (`parent_instance_id`; for a Bluetooth pad that is the
    BTHENUM HID service node the filter actually lives on). `parent_id` is
    consulted when an entry itself cannot be read -- the record's parent is
    exactly what is left when the child has gone phantom.

    False if any entry is verifiably uncovered, True if every readable entry is
    covered, None if nothing could be read at all.
    """
    answers = []
    for iid in [i for i in (instance_ids or []) if i]:
        got = filter_attached(iid)
        if got is not True:
            up = parent_instance_id(iid) or parent_id
            if up:
                above = filter_attached(up)
                if above is not None:
                    got = above if got is None else (got or above)
        answers.append(got)
    if parent_id and all(a is None for a in answers):
        answers.append(filter_attached(parent_id))
    known = [a for a in answers if a is not None]
    if not known:
        return None
    return all(known)


def ensure_filter(serial: str, instance_ids, parent_id: str = "",
                  allow_restart: bool = False, log_fn=None,
                  settle: float = REVIVE_SETTLE_S, *,
                  covers=None, restart=None, elevated=None, resolve=None,
                  wait=None) -> dict:
    """Check that a hide will actually bite, and make it bite if we can.

    -> `{"effective": True|False|None, "note": str, "restarted": bool, "ids": [...]}`

    `effective` is the answer `hide_effective` carries to the dashboard and
    `doctor`; `note` is the sentence that goes with it (one of the `NOTE_*`
    constants, or "" when there is nothing to say); `ids` are the instance IDs
    the caller should hide -- the same ones it passed in unless a restart made
    the pad re-enumerate under new ones, which is why this runs BEFORE the hide
    and not after it: an entry written for an ID that a restart then churns is
    a stale entry with a journal record pointing at nothing.

    The escalation, and where it stops:

        covered                   -> effective True, no note, nothing touched
        not covered, no restart   -> False + "restart the device or reboot"
          allowed (a bridge is holding the pad open, or a bare `ds5bridge run`
          whose own handle a restart would sever)
        not covered, unelevated   -> False + the same, plus "run as admin"
        not covered, elevated     -> pnputil /restart-device on the HIDClass
          node the filter belongs on, wait for the pad to come back, resolve
          the serial afresh, check again: True + "restarted" or False + "even
          after restarting it -- reboot"
        unreadable                -> None + "unknown"; a restart is NEVER made
          on the strength of a check that could not be performed

    The keyword arguments are the seams the unit tests use to run this decision
    table with no cfgmgr32, no pnputil and no controller.
    """
    say = log_fn or (lambda t: log.info("%s", t))
    covers = covers or filter_covers
    restart = restart or _pnputil_restart
    elevated = elevated or is_elevated
    resolve = resolve or (lambda s: resolve_serial(s))
    wait = wait or _wait_visible
    ids = [i for i in (instance_ids or []) if i]
    out = {"effective": None, "note": "", "restarted": False, "ids": list(ids)}
    if not ids:
        return out
    try:
        got = covers(ids, parent_id)
        if got is True:
            out["effective"] = True
            return out
        if got is None:
            out["note"] = NOTE_FILTER_UNKNOWN
            return out
        # Verifiably not attached. The filter joins the stack when the stack is
        # rebuilt, and only a devnode restart (or a reboot) rebuilds it.
        if not allow_restart:
            out["effective"] = False
            out["note"] = NOTE_FILTER_MISSING
            return out
        if not elevated():
            out["effective"] = False
            out["note"] = NOTE_FILTER_MISSING_UNELEVATED
            return out
        # The HIDClass node the filter belongs on is the entries' parent (the
        # BTHENUM HID service node); failing that, each entry itself.
        targets = []
        for iid in ids:
            up = parent_instance_id(iid) or parent_id
            t = up or iid
            if t and t not in targets:
                targets.append(t)
        say(f"HidHide's filter is not attached to {serial}'s device yet "
            f"(the pad was enumerated before HidHide was installed); "
            f"restarting the device so it joins ...")
        for t in targets:
            code, text = restart(t)
            log.debug("pnputil /restart-device %s -> %s %s", t, code,
                      (text or "").strip())
        out["restarted"] = True
        wait(serial, settle)
        fresh = [i for i in (resolve(serial) or []) if i]
        if fresh:
            out["ids"] = fresh
        again = covers(out["ids"], parent_id)
        if again is True:
            out["effective"] = True
            out["note"] = NOTE_FILTER_RESTARTED
        elif again is None:
            out["note"] = NOTE_FILTER_UNKNOWN
        else:
            out["effective"] = False
            out["note"] = NOTE_FILTER_STILL_MISSING
        return out
    except Exception:  # noqa: BLE001
        log.exception("checking HidHide's filter for %s failed", serial)
        out["note"] = NOTE_FILTER_UNKNOWN
        return out


# -- what the last hide of each serial found ---------------------------------
#
# In-process memory, deliberately not on disk: it describes THIS process's
# last attempt, the manager that pre-hides a pad is the process whose
# `/api/state` reports it, and a value that outlived a reboot would be wrong
# after the reboot that fixes it. `unhide_for_bridge` forgets the serial.

_HIDE_STATUS: dict[str, dict] = {}


def _set_hide_status(serial: str, effective, note: str) -> None:
    _HIDE_STATUS[(serial or "").strip().lower()] = {
        "hide_effective": effective, "hide_note": note or ""}


def clear_hide_status(serial: str) -> None:
    _HIDE_STATUS.pop((serial or "").strip().lower(), None)


def hide_status(serial: str) -> dict:
    """`{"hide_effective": True|False|None, "hide_note": str}` for a serial.

    None / "" when this process has not hidden that controller -- the fields
    the manager adds to every controller in its snapshot, and the contract
    with the dashboard: `hide_effective` is null until a hide has been checked,
    true when HidHide's filter is verifiably in the pad's stack, false when it
    verifiably is not, and `hide_note` says what to do about it.
    """
    got = _HIDE_STATUS.get((serial or "").strip().lower())
    if not got:
        return {"hide_effective": None, "hide_note": ""}
    return dict(got)


def pad_visible(serial: str) -> bool | None:
    """Can the bridge's own enumeration see this controller? None = cannot tell.

    Deliberately `enumerate_devices()` and not some cheaper devnode query: it
    filters on usage page 1 / usage 5 and it is what `controller.select()` uses,
    so a True here means visible TO THE THING THAT HAS TO OPEN IT rather than
    merely present in some list.

    None is a third answer and must not be rounded to either: no hidapi (the
    stdlib-only salvage case) means the verification simply cannot be performed,
    and acting as if it had failed would restart a devnode for no reason.
    """
    serial = (serial or "").strip().lower()
    if not serial:
        return None
    try:
        from ds5bridge import device as DEV

        return any((i.serial or "").strip().lower() == serial
                   for i in DEV.enumerate_devices())
    except Exception:  # noqa: BLE001
        log.debug("hidapi cannot say whether %s is visible", serial,
                  exc_info=True)
        return None


def _wait_visible(serial: str, settle: float) -> bool | None:
    """Poll `pad_visible` until it says yes, or `settle` runs out."""
    seen = pad_visible(serial)
    if seen is not False:
        return seen
    end = time.monotonic() + max(0.0, settle)
    while time.monotonic() < end:
        time.sleep(REVIVE_POLL_S)
        seen = pad_visible(serial)
        if seen is not False:
            return seen
    return False


def bt_connected(serial: str) -> bool | None:
    """Is the radio link to this controller UP, as the Bluetooth stack sees it?

    True/False from `BluetoothGetDeviceInfo` (bthprops.cpl) for a paired
    address; None when it cannot say (not Windows, no such pairing, the call
    failed). About a millisecond, and no PnP involved.

    The witness `revive_serial` needed all along. A paired pad's BTHENUM nodes
    stay PRESENT whether it is on or off (`devnode_present`, measured
    2026-09-15), so presence could not tell a pad in a drawer from a connected
    pad whose HID child has gone phantom. On 2026-09-16 that cost the
    uninstaller: `ds5bridge unhide` re-enumerated and restarted the Bluetooth
    node of a switched-OFF pad, the process sat in that call for minutes and
    could not be killed, and the uninstaller waited on it. `fConnected` is the
    link itself, which is the only thing that separates the two states.
    """
    if sys.platform != "win32":
        return None
    mac = _hex_only(serial)
    if len(mac) != 12:
        return None
    try:
        W = wintypes

        class _SYSTEMTIME(ctypes.Structure):
            _fields_ = [(n, W.WORD) for n in (
                "wYear", "wMonth", "wDayOfWeek", "wDay", "wHour", "wMinute",
                "wSecond", "wMilliseconds")]

        class _BLUETOOTH_DEVICE_INFO(ctypes.Structure):
            _fields_ = [("dwSize", W.DWORD), ("Address", ctypes.c_ulonglong),
                        ("ulClassofDevice", W.ULONG), ("fConnected", W.BOOL),
                        ("fRemembered", W.BOOL), ("fAuthenticated", W.BOOL),
                        ("stLastSeen", _SYSTEMTIME), ("stLastUsed", _SYSTEMTIME),
                        ("szName", W.WCHAR * 248)]

        bt = ctypes.WinDLL("bthprops.cpl")
        fn = bt.BluetoothGetDeviceInfo
        fn.argtypes = [W.HANDLE, ctypes.POINTER(_BLUETOOTH_DEVICE_INFO)]
        fn.restype = W.DWORD
        info = _BLUETOOTH_DEVICE_INFO()
        info.dwSize = ctypes.sizeof(info)
        info.Address = int(mac, 16)
        # A NULL radio handle asks every local radio (measured 2026-09-16:
        # ERROR_SUCCESS with fConnected set for the pad that was on, clear
        # for the one that was off, ERROR_NOT_FOUND for an unpaired address).
        if fn(None, ctypes.byref(info)) != 0:
            return None
        return bool(info.fConnected)
    except Exception:  # noqa: BLE001
        log.debug("BluetoothGetDeviceInfo for %s failed", serial, exc_info=True)
        return None


def pad_connected(serial: str, parent_id: str = "") -> bool:
    """Is this controller connected right now?

    The Bluetooth stack's answer (`bt_connected`) when it has one; only when it
    has none, whether the BTHENUM parent is present -- which on a machine that
    keeps paired nodes present forever says "connected" for a pad that is off,
    so it is the fallback and never the first word.
    """
    linked = bt_connected(serial)
    if linked is not None:
        return linked
    parent = parent_id or bt_parent_for_serial(serial)
    return bool(parent) and devnode_present(parent)


def power_cycle_hint(serial: str) -> str:
    return (f"{serial} is unhidden but Windows still is not showing it. "
            f"Switch the controller OFF (hold the PS button for ~10 s) and on "
            f"again -- that re-creates its HID device. Running ds5bridge as "
            f"administrator lets it do this for you.")


def revive_serial(serial: str, parent_id: str = "", log_fn=None,
                  settle: float = REVIVE_SETTLE_S) -> dict:
    """Make an unhidden controller usable again, and PROVE it. Never raises.

    -> `{"serial", "visible": True|False|None, "action", "parent", "hint"}`.

    `visible` is the only field that means anything on its own, and `None` is
    "could not be checked", never "fine". `action` is what it took:

        "none"          it was already visible -- the normal case, one
                        enumeration and out
        "disconnected"  the controller is switched off or out of range, so
                        there is nothing wrong and nothing to repair
        "reenumerate"   `CM_Reenumerate_DevNode` on the Bluetooth parent, from
                        an ordinary user process
        "pnputil"       `pnputil /restart-device`, only when we are elevated
        "unknown"       no Bluetooth parent could be found for this address
        "failed"        everything was tried and the pad is still not there

    The escalation stops the moment the pad is visible, and it never escalates
    past what it can verify: if `pad_visible()` cannot answer at all, the
    elevated restart is NOT attempted, because restarting a devnode on the
    strength of an unverifiable suspicion is a worse failure than leaving it.

    A "failed" says so out loud through `log_fn`. That is the whole point -- the
    user this was written for was told everything had worked.
    """
    serial = (serial or "").strip().lower()
    say = log_fn or (lambda t: log.info("%s", t))
    out = {"serial": serial, "visible": None, "action": "none",
           "parent": parent_id or "", "hint": ""}
    try:
        seen = pad_visible(serial)
        if seen is not False:
            out["visible"] = seen
            return out

        parent = parent_id or bt_parent_for_serial(serial)
        out["parent"] = parent
        if not parent:
            out["action"] = "unknown"
            out["visible"] = False
            out["hint"] = power_cycle_hint(serial)
            say(out["hint"])
            return out

        # IS THE CONTROLLER EVEN CONNECTED? A pad that is switched off is not
        # broken and cannot be repaired, and this is the difference between the
        # two states (`pad_connected`: the Bluetooth link, not the BTHENUM
        # node, which stays present for a pad that is off). Silent on purpose:
        # switching a controller off is the most ordinary thing a user does,
        # it is how most of this program's teardowns begin, and being told to
        # power-cycle it every single time would train them to ignore the one
        # message that matters. And never re-enumerate or restart the node of
        # a pad that is off: that call sits in the kernel for minutes and the
        # process cannot even be killed while it does (2026-09-16).
        if not pad_connected(serial, parent):
            out["action"] = "disconnected"
            out["visible"] = False
            log.debug("%s is not connected over Bluetooth; nothing to revive",
                      serial)
            return out

        # The pad IS connected and still not enumerable. Before restarting
        # anything, let a blacklist change that was made moments ago actually
        # take effect -- an unhide is applied by the driver, not by us, and
        # tearing a devnode down because we asked half a second too early would
        # be this feature causing the failure it exists to fix.
        seen = _wait_visible(serial, settle)
        if seen is not False:
            out["visible"] = seen
            return out

        if _reenumerate(parent):
            out["action"] = "reenumerate"
            seen = _wait_visible(serial, settle)
            if seen is not False:
                out["visible"] = seen
                if seen:
                    say(f"{serial} is visible to Windows again")
                return out

        if is_elevated():
            code, text = _pnputil_restart(parent)
            out["action"] = "pnputil"
            log.debug("pnputil /restart-device %s -> %s %s", parent, code,
                      (text or "").strip())
            if code == 0:
                seen = _wait_visible(serial, settle)
                if seen is not False:
                    out["visible"] = seen
                    if seen:
                        say(f"{serial} is visible to Windows again")
                    return out

        out["visible"] = False
        out["action"] = "failed"
        out["hint"] = power_cycle_hint(serial)
        say(out["hint"])
        return out
    except Exception:  # noqa: BLE001
        log.exception("reviving %s failed", serial)
        return out


# ---------------------------------------------------------------------------
# the facade
# ---------------------------------------------------------------------------


def find_cli(override: str | None = None) -> str:
    """Where HidHideCLI.exe is, or "". `override` is config's `hidhide_cli`."""
    if override:
        p = os.path.expandvars(os.path.expanduser(override.strip().strip('"')))
        if os.path.isfile(p):
            return p
        log.warning("hidhide_cli points at %s, which is not a file", p)
    for p in _CLI_CANDIDATES:
        if os.path.isfile(p):
            return p
    return ""


def _driver_installed() -> bool:
    """Is the HidHide driver on this machine?

    The service key, not the Parameters subkey: H0 found Parameters has an empty
    DACL and cannot be read even elevated, while the service key itself is
    world-readable. This also stays true when the control device is unavailable,
    which is the state a freshly installed, not-yet-rebooted machine is in.
    """
    if sys.platform != "win32":
        return False
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _SERVICE_KEY, 0,
                            winreg.KEY_READ | winreg.KEY_WOW64_64KEY):
            return True
    except OSError:
        pass
    return os.path.isfile(_DRIVER_SYS)


class HidHide:
    """The backend-neutral interface. `detect()` is the only constructor to use.

    Every method is idempotent and none of them raise. `hide()` of something
    already hidden, `unhide()` of something that was never hidden and `allow()`
    of an already-allowed path are all no-ops that report success.
    """

    def __init__(self, cli_path: str = "", installed: bool = True):
        self.cli = CliBackend(cli_path) if cli_path else None
        self.ioctl = IoctlBackend()
        self.installed = installed
        self._version = ""

    # -- construction ------------------------------------------------------

    @classmethod
    def detect(cls, cli_override: str | None = None) -> "HidHide | None":
        """An instance, or None when HidHide is not installed. Never raises.

        "Installed" is the driver being present, NOT the control device being
        openable. Those are different questions on this machine (module
        docstring), and answering the first with the second would make the whole
        feature disappear from `doctor` exactly when somebody needs `doctor` to
        explain why it is not working.
        """
        try:
            if sys.platform != "win32":
                return None
            cli = find_cli(cli_override)
            if not _driver_installed() and not cli:
                return None
            return cls(cli_path=cli, installed=_driver_installed())
        except Exception:  # noqa: BLE001
            log.exception("HidHide detection failed")
            return None

    # -- state -------------------------------------------------------------

    def version(self) -> str:
        if self._version:
            return self._version
        if self.cli is not None:
            self._version = self.cli.version()
        if not self._version and sys.platform == "win32":
            # No CLI, or a CLI that would not answer. The driver binary's own
            # version is a weaker answer -- H0 measured HidHide.sys reporting
            # 1.4.181.0 inside the 1.5.230 package -- but it beats "unknown".
            try:
                import ctypes.wintypes as wt  # noqa: F401

                if os.path.isfile(_DRIVER_SYS):
                    self._version = _file_version(_DRIVER_SYS)
            except Exception:  # noqa: BLE001
                pass
        return self._version

    def control_ok(self) -> bool:
        """Can we actually read HidHide's state right now, by any route?

        This is the question `doctor` should ask, and it is NOT the same as
        "is HidHide installed" -- see the module docstring.
        """
        return self.active() is not None

    def hidden(self) -> list[str]:
        return self.hidden_raw() or []

    def allowed(self) -> list[str]:
        got = self.ioctl.allowed()
        if got is None and self.cli is not None:
            got = self.cli.allowed()
        return got or []

    def active(self) -> bool | None:
        got = self.ioctl.active()
        if got is None and self.cli is not None:
            got = self.cli.active()
        return got

    def set_active(self, on: bool) -> bool:
        with _Mutex():
            if self.ioctl.set_active(on):
                return True
            return bool(self.cli and self.cli.set_active(on))

    # -- mutation ----------------------------------------------------------

    def still_hidden(self, instance_ids) -> list[str] | None:
        """Which of these HidHide still holds. None when the list is unreadable.

        The read-back that every mutation is judged against. `None` is not
        "none of them" -- it is "nobody can tell", which is the one answer that
        must never be rounded up to success, because rounding it up is how a
        journal record gets deleted while the pad is still cloaked.
        """
        current = self.hidden_raw()
        if current is None:
            return None
        want = {norm_instance_id(i) for i in (instance_ids or []) if i}
        return [e for e in current if norm_instance_id(e) in want]

    def hidden_raw(self) -> list[str] | None:
        """The blacklist as HidHide holds it, or None if no surface can answer.

        `hidden()` flattens that None to `[]` for display callers, which is the
        right shape for `doctor` and the wrong shape for anything deciding
        whether a removal worked.
        """
        got = self.ioctl.hidden()
        if got is None and self.cli is not None:
            got = self.cli.hidden()
        return got

    def hide(self, instance_ids) -> bool:
        """Add these to the blacklist. Idempotent. -> did the state end up right.

        The whole read-modify-write is inside one mutex hold, because the IOCTL
        list API is replace-the-list and two concurrent children would otherwise
        lose an entry.
        """
        wanted = [i for i in (instance_ids or []) if i]
        if not wanted:
            return True
        with _Mutex():
            current = self.ioctl.hidden()
            if current is not None:
                have = {norm_instance_id(e) for e in current}
                merged = list(current)
                for i in wanted:
                    if norm_instance_id(i) not in have:
                        merged.append(i)
                        have.add(norm_instance_id(i))
                if merged == current or self.ioctl.set_hidden(merged):
                    return True
            if self.cli is not None:
                # --dev-hide is add-one, so the CLI owns the read-modify-write
                # and there is no list to lose.
                return all(self.cli.hide_one(i) for i in wanted)
        log.warning("could not hide %s -- no working HidHide control surface",
                    wanted)
        return False

    def unhide(self, instance_ids) -> bool:
        """Remove ONLY these from the blacklist, and PROVE it. Idempotent.

        Never "clear the blacklist". The user may be hiding other pads with
        HidHide for DS4Windows, and blowing those away would be a serious
        breach of trust in a program they installed to make a controller work.

        The return value is a read-back, not a report of what the writes said.
        Every part of the old "did it work" story turned out to be a lie on
        2026-08-27 (module docstring): a CLI verb that blocked on stdin and
        timed out, a `--dev-list` line that could not be matched because it was
        never parsed, and a case-sensitive comparison that found nothing to
        remove and called that done. All three produced True with the pad still
        invisible, and True is what deletes the journal record that was the
        user's last way back. So:

            1. remove, by whatever surface answers -- IOCTL first, then the CLI
               for anything the IOCTL could not take out;
            2. read the blacklist again;
            3. report success only if none of `instance_ids` survive.

        An unreadable list in step 2 is a failure. That is deliberately harsh
        and deliberately safe: the caller keeps its record, `sweep()` tries
        again on the next process start, and the worst case is one redundant
        unhide of something already gone -- against a worst case on the other
        side of a controller that no application can open.
        """
        wanted = [i for i in (instance_ids or []) if i]
        if not wanted:
            return True
        with _Mutex():
            current = self.ioctl.hidden()
            if current is not None:
                drop = {norm_instance_id(i) for i in wanted}
                keep = [e for e in current if norm_instance_id(e) not in drop]
                if keep != current:
                    self.ioctl.set_hidden(keep)

            # The CLI is not an "else". The IOCTL may have written nothing
            # because it could not read the list, or because the list it read
            # is not the list the driver is enforcing; --dev-unhide of an entry
            # that is already gone is a no-op, so trying both costs one process
            # spawn and closes the gap between the two surfaces.
            left = self.still_hidden(wanted)
            if left and self.cli is not None:
                for entry in left:
                    self.cli.unhide_one(entry)
                left = self.still_hidden(wanted)

        if left == []:
            return True
        if left is None:
            log.warning("unhid %s but could not read HidHide's blacklist back, "
                        "so the removal is unproven -- treating it as a failure "
                        "and keeping the record", wanted)
        else:
            log.warning("HidHide still holds %s after an unhide. Run "
                        "`ds5bridge unhide --all-hidhide`, or untick the device "
                        "in HidHideClient.exe.", left)
        return False

    def whitelist_covers_us(self) -> bool | None:
        """Is the image the KERNEL says we are actually on the whitelist?

        None when the whitelist cannot be read at all.

        This is the guard that would have caught the bug that cost this feature
        two rounds of hardware testing. `allow()` reported success, the entry
        appeared in the list, and the grant still did not apply -- because the
        path we registered (`sys.executable`, a venv launcher shim) was not the
        path the driver compares against (the base interpreter that the shim
        spawns as a child). Reading the list back and checking for our REAL
        image closes the loop for the whole class: shimmed interpreters, a
        botched DOS -> NT conversion, a junction, a moved install.

        It is one list read, at bridge start only, and it is the difference
        between "hiding quietly blinded the bridge" and a message that says so.
        """
        me = current_image_path()
        if not me:
            return None
        current = self.allowed()
        if not current:
            # An empty list is indistinguishable from an unreadable one here:
            # HidHide's own clients self-register, so a genuinely empty
            # whitelist does not happen on a working install.
            return None
        target = os.path.normcase(os.path.realpath(me))
        for p in current:
            try:
                if os.path.normcase(os.path.realpath(p)) == target:
                    return True
            except Exception:  # noqa: BLE001
                continue
        return False

    def allow(self, exe_paths) -> bool:
        """Whitelist these images, and prune OUR OWN dead entries. Idempotent.

        Re-registered on every start on purpose: HidHide keys on the absolute
        path, so moving or reinstalling the app silently invalidates the entry,
        and this is the cheapest possible repair for the single most common
        support question in this ecosystem.

        The prune is deliberately narrow. HidHide's own `--app-clean` drops
        every whitelist entry whose file is gone, including other programs'; we
        drop only entries that no longer exist AND are named like one of ours,
        so an install-location change does not accumulate a dead path forever
        without ever touching a stranger's entry.
        """
        wanted = []
        for p in (exe_paths or []):
            if not p:
                continue
            try:
                rp = os.path.realpath(p)
            except Exception:  # noqa: BLE001
                rp = p
            if rp not in wanted:
                wanted.append(rp)
        if not wanted:
            return True

        with _Mutex():
            current = self.ioctl.allowed()
            if current is not None:
                keep = [p for p in current
                        if os.path.exists(p)
                        or os.path.basename(p).lower() not in OUR_IMAGES]
                merged = list(keep)
                for p in wanted:
                    if not any(os.path.normcase(p) == os.path.normcase(q)
                               for q in merged):
                        merged.append(p)
                if merged == current or self.ioctl.set_allowed(merged):
                    return True
            if self.cli is not None:
                ok = all(self.cli.allow_one(p) for p in wanted)
                return ok
        log.warning("could not whitelist %s", wanted)
        return False


# ---------------------------------------------------------------------------
# our own images
# ---------------------------------------------------------------------------


def current_image_path() -> str:
    """The image path of THIS process, as the KERNEL reports it. "" on failure.

    Not `sys.executable`, and the difference is the entire reason the whitelist
    silently failed to grant anything on the development machine.

    A Windows venv's `Scripts\\python.exe` is not an interpreter. `python -m venv`
    installs `venvlauncher.exe` under that name, and in redirect mode it reads
    `pyvenv.cfg` and runs the base interpreter as a SEPARATE CHILD PROCESS.
    Measured 2026-08-27::

        pid 28972  D:\\...\\prototype\\.venv\\Scripts\\python.exe   <- the shim
        pid 12788  C:\\...\\pyenv-win\\versions\\3.12.5\\python.exe  <- runs our code

    `sys.executable` reports the shim in both, because the launcher arranges it
    that way. But HidHide matches the requesting process's image path and its
    whitelist is NOT inherited by children -- so whitelisting `sys.executable`
    registered a launcher that never opens a HID device, while the process that
    actually calls `hid.enumerate()` stayed unwhitelisted and blocked. Blocking
    worked, granting did not, and the pad was invisible to us too.

    The same trap is waiting in every shimmed interpreter -- pyenv-win, `py.exe`,
    Store Python, a `uv` venv. Asking the kernel is the only answer that does not
    have to enumerate them.
    """
    if sys.platform != "win32":
        return ""
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        k32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD)]
        k32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        size = wintypes.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if k32.QueryFullProcessImageNameW(k32.GetCurrentProcess(), 0, buf,
                                          ctypes.byref(size)):
            return buf.value
    except Exception:  # noqa: BLE001
        log.debug("could not read our own image path", exc_info=True)
    return ""


def our_images() -> list[str]:
    """Every executable of OURS that opens or enumerates HID, realpath'd.

    All of them, not just the one holding the handle. The whitelist is matched
    per image and is NOT inherited by child processes, so the tray exe needs its
    own entry even though it never opens a controller -- it calls
    `hid.enumerate()` every five seconds, and section 6.7 of the scoping
    document is a description of what happens when it cannot: the tray stops
    seeing a controller it just hid, `vanish_grace` expires, the bridge stops,
    the unhide makes the pad reappear, and the whole thing starts again. A
    ~40-second self-sustaining flap that looks exactly like a flaky Bluetooth
    link, on hardware where flaky Bluetooth links are real.

    The FIRST entry is what the kernel says this process's image is, because
    that -- not `sys.executable` -- is what HidHide compares against. See
    `current_image_path()` for the venv-launcher trap that makes those two
    different, and for why getting this wrong looks exactly like "HidHide is
    broken on this Windows build".

    realpath because a whitelisted path reached through a directory junction
    does not match (HidHide issue #79).
    """
    out = []

    def add(p):
        if not p:
            return
        try:
            rp = os.path.realpath(p)
        except Exception:  # noqa: BLE001
            rp = p
        if os.path.isfile(rp) and rp not in out:
            out.append(rp)

    # The authoritative one, first.
    add(current_image_path())

    exe = sys.executable
    add(exe)
    if getattr(sys, "frozen", False):
        here = os.path.dirname(os.path.abspath(exe))
        add(os.path.join(here, "ds5bridge.exe"))
        add(os.path.join(here, "ds5bridge-tray.exe"))
    else:
        # Running from source: this grants ANY script run by that interpreter
        # the ability to open hidden devices. It is the developer's own venv, so
        # it is a defensible grant -- CONTRIBUTING.md states it rather than
        # leaving it silent.
        #
        # `_base_executable` is the belt to `current_image_path()`'s braces: on a
        # venv whose Scripts\python.exe is a redirecting launcher it names the
        # real interpreter directly, and it costs nothing when the two agree.
        add(getattr(sys, "_base_executable", None))
        for base in {os.path.dirname(os.path.abspath(exe)),
                     os.path.dirname(current_image_path() or exe)}:
            add(os.path.join(base, "pythonw.exe"))
    return out


# ---------------------------------------------------------------------------
# the journal -- what makes a crash recoverable
# ---------------------------------------------------------------------------


def journal_dir() -> str:
    """`%APPDATA%\\ds5bridge\\hidden`. Follows DS5_CONFIG so tests never touch
    the user's real state, exactly as `config.config_dir()` does."""
    try:
        from . import config as K

        return os.path.join(K.config_dir(), "hidden")
    except Exception:  # noqa: BLE001
        # Loaded as a bare file rather than as part of the package -- which is
        # how the stdlib-only modules are tested, and how a salvage run on a
        # broken install would reach this. DS5_CONFIG still has to win, or a
        # test would sweep the developer's own hidden/ directory.
        override = os.environ.get("DS5_CONFIG")
        if override:
            p = os.path.abspath(os.path.expanduser(override.strip().strip('"')))
            if p.lower().endswith(".json"):
                p = os.path.dirname(p)
            return os.path.join(p, "hidden")
        appdata = os.environ.get("APPDATA")
        base = (os.path.join(appdata, "ds5bridge") if appdata
                else os.path.join(os.path.expanduser("~"), ".ds5bridge"))
        return os.path.join(base, "hidden")


def _record_path(serial: str) -> str:
    return os.path.join(journal_dir(), f"{(serial or '').strip().lower()}.json")


def write_record(serial: str, instance_ids, cloak_enabled_by_us: bool = False,
                 parent_id: str = "") -> bool:
    """Record what we are ABOUT to hide. Called BEFORE the hide, always.

    Same `.tmp` + `fsync` + `os.replace` dance as `config.save()`, for the same
    reason: a half-written record is what a power cut during this produces, and
    a record that cannot be parsed is a controller nobody knows to un-hide.

    `parent_id` is the controller's BTHENUM devnode, and it is written for the
    benefit of a run that is not this one. A crash leaves the pad hidden; the
    next start's `sweep()` clears the blacklist and then has to make the devnode
    come back -- and by then hidapi cannot see the pad, so nothing can resolve
    the address to a devnode any more. Recording the parent while the pad IS
    visible is what makes that recoverable (`revive_serial`).
    """
    serial = (serial or "").strip().lower()
    if not serial:
        return False
    path = _record_path(serial)
    tmp = path + ".tmp"
    data = {
        "serial": serial,
        "instance_ids": list(instance_ids or []),
        "parent_id": parent_id or "",
        "pid": os.getpid(),
        "image": os.path.basename(sys.executable),
        "cloak_enabled_by_us": bool(cloak_enabled_by_us),
        "written_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        return True
    except OSError as e:
        try:
            os.remove(tmp)
        except OSError:
            pass
        log.warning("could not journal the hide of %s (%s) -- NOT hiding it",
                    serial, e)
        return False


def read_records() -> list[dict]:
    """Every journal entry. Unparseable ones are skipped, never fatal."""
    out = []
    d = journal_dir()
    try:
        names = sorted(os.listdir(d))
    except OSError:
        return out
    for name in names:
        if not name.lower().endswith(".json"):
            continue
        path = os.path.join(d, name)
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                data.setdefault("serial", os.path.splitext(name)[0].lower())
                data["_path"] = path
                out.append(data)
        except (OSError, ValueError) as e:
            log.warning("journal entry %s is unreadable (%s)", path, e)
    return out


def remove_record(serial_or_path: str) -> None:
    """Delete a record. The commit point for 'we are clean'. Called AFTER unhiding."""
    path = (serial_or_path if serial_or_path.lower().endswith(".json")
            else _record_path(serial_or_path))
    try:
        os.remove(path)
    except OSError:
        pass


def adopt_record(serial: str) -> bool:
    """Re-own an existing hide record from THIS process, keeping the cloak.

    The disconnect half of the 2026-08-31 "player 3, no rumble" bug
    (`docs/wired-gap-findings.md`, symptom 4). When a bridged controller is
    switched off mid-game, tearing the bridge down used to unhide it -- so the
    pad re-enumerated *visible*, the game grabbed the raw Bluetooth device in
    the seconds before the next bridge re-hid it, and from then on the game
    held a ghost controller slot. The structural fix is to keep the cloak
    across the disconnect, so a returning pad re-enumerates already hidden and
    goes straight to bridging.

    But a kept cloak must not look abandoned. The journal record was written
    by the (now dead) bridge child, and `sweep()` -- which every process start
    runs, including the NEXT bridge child's -- unhides anything whose owner is
    gone. So the manager that decided to keep the cloak rewrites the record
    with its own pid. `owner_alive()` then vouches for it exactly as long as
    the manager lives:

    * a sibling child starting meanwhile leaves it alone (owner alive);
    * the manager's own exit sweep clears it (`owner_alive` refuses
      `os.getpid()`, deliberately -- our own records are our own debt);
    * a crashed manager leaves a dead pid, and the next start's sweep
      unhides and revives as it always did.

    Everything else in the record -- the instance IDs, the Bluetooth parent,
    `cloak_enabled_by_us` -- is preserved verbatim. Returns False (harmless)
    when there is no record: hiding was off, so there is no cloak to keep.
    Never raises.
    """
    serial = (serial or "").strip().lower()
    if not serial:
        return False
    try:
        for rec in read_records():
            if (rec.get("serial") or "").lower() == serial:
                return write_record(
                    serial,
                    [i for i in (rec.get("instance_ids") or []) if i],
                    cloak_enabled_by_us=bool(rec.get("cloak_enabled_by_us")),
                    parent_id=(rec.get("parent_id") or "").strip())
    except Exception:  # noqa: BLE001
        log.exception("adopting the hide record for %s failed", serial)
    return False


def journal_count() -> int:
    return len(read_records())


def unrecorded_hidden(hh: "HidHide | None" = None) -> list[str] | None:
    """Blacklist entries no journal record accounts for. None if unreadable.

    The journal answers "what do we still owe an unhide"; this answers the
    question a user actually asks, which is "is anything still hidden". They
    came apart on 2026-08-27 and the gap is the whole bug: a record is deleted
    when an unhide claims success, so one lying unhide leaves an entry that no
    record points at -- and from then on `journal_count()` says zero while the
    pad is invisible to every application on the machine.

    Nothing here removes anything. Some entries legitimately belong to somebody
    else (DS4Windows hides pads through the same driver), so this reports and
    lets the human decide, which is the same rule `ds5bridge unhide` follows.
    """
    if hh is None:
        hh = HidHide.detect()
    if hh is None:
        return []
    current = hh.hidden_raw()
    if current is None:
        return None
    ours = {norm_instance_id(i)
            for rec in read_records()
            for i in (rec.get("instance_ids") or []) if i}
    return [e for e in current if norm_instance_id(e) not in ours]


# -- the verification marker ------------------------------------------------
#
# Deliberately NOT in the journal directory: a journal entry means "something is
# hidden right now and owes an unhide", and the sweep is entitled to delete
# anything it finds there. This is a durable measurement, not a debt.

VERIFY_NAME = "hidhide-verified.json"


def verify_marker_path() -> str:
    return os.path.join(os.path.dirname(journal_dir()), VERIFY_NAME)


def write_verify_marker(blocking: bool, granting: bool, image: str = "",
                        version: str = "") -> bool:
    """Record what `hidhide_verify.py` measured, so `doctor` can report it.

    Only that tool can answer "does hiding really hide, and can we really still
    open it" -- it needs a real controller and a real hidden window, which is
    not something a diagnostic command may do behind a user's back. Persisting
    the answer is how the cheap checks stop pretending to be the whole story.
    """
    path = verify_marker_path()
    tmp = path + ".tmp"
    data = {
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "blocking": bool(blocking),
        "granting": bool(granting),
        "image": image or current_image_path(),
        "hidhide_version": version,
    }
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        return True
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False


def read_verify_marker() -> dict:
    try:
        with open(verify_marker_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


# -- liveness ---------------------------------------------------------------


def process_image_name(pid: int) -> str:
    """Image name for a PID, or "".

    The image NAME, deliberately, for the reason `service.process_name()` sets
    out: `Win32_Process.CommandLine` comes back empty for some python processes,
    so a command-line match must never be load-bearing. This one uses
    `QueryFullProcessImageNameW` rather than shelling out to `tasklist`, because
    the sweep runs on every process start and must work on a machine where the
    rest of the app cannot even be imported.
    """
    if sys.platform != "win32" or not pid or pid <= 4:
        return ""
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    try:
        k32 = ctypes.windll.kernel32
        k32.OpenProcess.restype = wintypes.HANDLE
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not h:
            return ""
        try:
            size = ctypes.c_ulong(1024)
            buf = ctypes.create_unicode_buffer(size.value)
            if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                return os.path.basename(buf.value)
            return ""
        finally:
            k32.CloseHandle(h)
    except Exception:  # noqa: BLE001
        return ""


def owner_alive(record: dict) -> bool:
    """Is the process that wrote this record still running, and is it ours?

    Both halves matter. Without the liveness check, launching the tray while a
    `ds5bridge run` is bridging in a terminal would unhide that one's controller
    out from under it. Without the "is it ours" half, a recycled PID belonging
    to an unrelated program would keep a stale record alive forever.
    """
    try:
        pid = int(record.get("pid") or 0)
    except (TypeError, ValueError):
        return False
    if pid <= 0 or pid == os.getpid():
        return False
    name = process_image_name(pid)
    return bool(name) and name.lower() in OUR_IMAGES


# ---------------------------------------------------------------------------
# the sweep -- the actual safety net
# ---------------------------------------------------------------------------


def sweep(hh: "HidHide | None" = None, force: bool = False, log_fn=None) -> int:
    """Unhide everything whose owner is gone, and delete its record. -> how many.

    Runs on EVERY process start, before anything else HidHide-related, and
    unconditionally -- including when the feature is switched off and when no
    controller is connected. Whichever of `ds5bridge run`, the tray or
    `ds5bridge cleanup` the user reaches for next, it fixes itself.

    `force=True` skips the liveness check. That is the tray's "Unhide everything
    now" and `ds5bridge unhide`: the "I do not care what is running, give me my
    controller back" path.

    Every record it clears is followed by `revive_serial()`, for the reason that
    section of this module exists: the run this is cleaning up after died
    without unhiding, so its pad's HID child is very likely a phantom, and an
    empty blacklist does not bring a phantom back. This is the one place that
    can fix it before the user has even noticed, because it runs on every
    process start -- and if it cannot, it says "power-cycle the controller"
    rather than leaving the user to work that out.

    Silent on the happy path -- an empty journal is the normal case.
    """
    records = read_records()
    if not records:
        return 0
    if hh is None:
        hh = HidHide.detect()

    say = log_fn or (lambda t: log.info("%s", t))
    cleared = 0
    cloak_ours = False
    for rec in records:
        if not force and owner_alive(rec):
            log.debug("%s is still owned by live pid %s", rec.get("serial"),
                      rec.get("pid"))
            continue
        ids = [i for i in (rec.get("instance_ids") or []) if i]
        serial = rec.get("serial") or "?"
        parent = (rec.get("parent_id") or "").strip()
        if hh is not None and ids:
            # Idempotent: entries that are already absent are a no-op, which is
            # exactly what the record-before-hide ordering relies on. The
            # recorded IDs may also be phantoms by now, so anything else in the
            # blacklist under the same Bluetooth parent goes with them.
            targets = list(ids)
            for entry in entries_under_parent(hh.hidden_raw(), parent):
                if not any(norm_instance_id(entry) == norm_instance_id(t)
                           for t in targets):
                    targets.append(entry)
            if not hh.unhide(targets):
                say(f"could not unhide {serial}; its record is kept so the next "
                    f"run tries again")
                continue
            say(f"unhid {serial} ({len(targets)} HID interface(s))")
            revive_serial(serial, parent_id=parent, log_fn=say)
        elif hh is None and ids:
            # No HidHide at all any more -- the driver was uninstalled while we
            # had something hidden. The blacklist went with it, so the record is
            # stale rather than a debt; drop it so the tray stops offering an
            # escape from a state that no longer exists.
            say(f"HidHide is gone; dropping the stale record for {serial}")
        if rec.get("cloak_enabled_by_us"):
            cloak_ours = True
        remove_record(rec.get("_path") or serial)
        cleared += 1

    # Only ever turn the cloak off if WE turned it on and nothing of ours is
    # left hidden. It is a global flag: a user hiding other pads for DS4Windows
    # would find them all visible again.
    if cloak_ours and hh is not None and not read_records():
        try:
            hh.set_active(False)
            say("cloak disabled (we had enabled it, and nothing of ours is hidden)")
        except Exception:  # noqa: BLE001
            log.debug("could not disable the cloak", exc_info=True)
    return cleared


# ---------------------------------------------------------------------------
# what BridgeService calls
# ---------------------------------------------------------------------------


def hide_for_bridge(serial: str, cli_override: str | None = None,
                    log_fn=None, allow_restart: bool = False) -> list[str]:
    """Hide one controller for the duration of a bridge. -> the IDs hidden.

    Returns [] for every failure, including "HidHide is not installed", and
    never raises. Hiding is a convenience; bridging is the product, and a
    HidHide problem must never be why a bridge fails.

    Ordering, which is the whole of section 7 of the scoping document plus the
    2026-09-05 lesson (the filter section above):

        1. resolve the serial FRESH (instance IDs are not identity)
        2. whitelist our exes -- before any hide, or the tray blinds itself
        3. check that HidHide's FILTER is in the pad's device stack, and if it
           is not and `allow_restart` says we may, restart the device so it is
           (`ensure_filter`) -- before the hide, so that an ID a restart
           churns is never the one written to the list and the journal
        4. WRITE THE JOURNAL
        5. hide
        6. enable the cloak if it is off, recording that we did
        7. remember what step 3 found (`hide_status`) and SAY IT: "hidden"
           only when the filter is verifiably attached, otherwise the note

    A failure at step 4 aborts before step 5. That is deliberate: hiding
    something we failed to record is the one outcome that strands the user.

    `allow_restart` is False by default because a restart severs every open
    handle on the device: the manager passes True from its pre-hide (no child
    exists yet), and False when a running bridge already holds the pad. A
    bare `ds5bridge run` never restarts -- its own handle is on the line.
    """
    say = log_fn or (lambda t: log.info("%s", t))
    serial = (serial or "").strip().lower()
    try:
        hh = HidHide.detect(cli_override)
        if hh is None:
            say("HidHide is not installed -- the Bluetooth pad stays visible. "
                "See docs/USER-GUIDE.md.")
            return []

        ids = resolve_serial(serial, cli=hh.cli)
        if not ids:
            say(f"could not resolve {serial} to a device instance; "
                f"not hiding it")
            return []

        hh.allow(our_images())

        # REFUSE TO HIDE IF WE ARE NOT DEMONSTRABLY WHITELISTED.
        #
        # Hiding a pad we cannot then open is strictly worse than not hiding it:
        # the bridge loses its own controller and the user gets a dead virtual
        # device plus a pad that has vanished from Windows. Measured on this
        # machine before `current_image_path()` existed -- `allow()` succeeded,
        # the entry was in the list, and the grant still did not apply. So the
        # check is a read-back of what the driver actually holds, not a check of
        # whether our own write returned success.
        covered = hh.whitelist_covers_us()
        if covered is False:
            say("NOT hiding: this program is not on HidHide's whitelist "
                f"({current_image_path() or 'unknown image'}), so hiding would "
                f"stop the bridge from reading the controller too. Run "
                f"`ds5bridge doctor`.")
            return []

        was_active = hh.active()
        need_cloak = was_active is False

        # Read the Bluetooth parent NOW, while the pad is still enumerable. It
        # is the only identity that survives both the cloak and a re-enumeration
        # of the HID child, and after the hide nothing can look it up any more.
        parent = parent_instance_id(ids[0]) or bt_parent_for_serial(serial)

        # IS THE FILTER ACTUALLY THERE? The list can be right and the hide still
        # do nothing (the section above). Checked -- and, when allowed and
        # elevated, repaired by a device restart -- BEFORE the list is written,
        # so the IDs that go into the list and the journal are the ones the
        # pad has after any restart. A restart can churn them.
        check = ensure_filter(serial, ids, parent, allow_restart=allow_restart,
                              log_fn=say)
        if check["ids"] and check["ids"] != ids:
            ids = check["ids"]
            parent = parent_instance_id(ids[0]) or parent

        if not write_record(serial, ids, cloak_enabled_by_us=need_cloak,
                            parent_id=parent):
            return []

        if not hh.hide(ids):
            remove_record(serial)
            _set_hide_status(serial, False,
                             "HidHide would not accept the hide")
            say("HidHide would not accept the hide; the pad stays visible")
            return []

        if need_cloak:
            hh.set_active(True)

        _set_hide_status(serial, check["effective"], check["note"])
        n = len(ids)
        if check["effective"] is True:
            say(f"Bluetooth pad hidden ({n} HID interface(s)). Games started "
                f"from now on will not see it."
                + (f" ({check['note']})" if check["note"] else ""))
        elif check["effective"] is False:
            # The old message here was "hidden", and it was a lie on the
            # machine that produced this code. Say what is true instead.
            say(f"Bluetooth pad is on HidHide's hide list ({n} HID "
                f"interface(s)) but is NOT actually hidden yet: {check['note']}")
        else:
            say(f"Bluetooth pad hidden ({n} HID interface(s)), unverified: "
                f"{check['note']}")
        return ids
    except Exception:  # noqa: BLE001
        log.exception("hiding %s failed", serial)
        return []


def unhide_for_bridge(serial: str, cli_override: str | None = None,
                      log_fn=None, revive: bool = True) -> bool:
    """The exact reverse, run from every path that stops a bridge.

    Called unconditionally on stop -- there is no "did we hide it" flag to get
    out of step, because the journal on disk IS that flag and it survives things
    an in-memory flag does not.

    "No record means nothing to do" is NOT good enough, and that assumption is
    half of the 2026-08-27 bug report. A record is deleted the instant an unhide
    reports success, and until this commit an unhide could report success having
    removed nothing at all. One such round leaves a blacklist entry with no
    record pointing at it, and from then on this function, `sweep()` and
    `ds5bridge unhide` all agree there is nothing to do while the pad is
    invisible to every application on the machine -- permanently, because
    nothing else ever looks.

    So the no-record case now asks HidHide instead of asking the journal. It
    stays free in the normal case: an empty blacklist IS the proof that nothing
    of ours is hidden, and that is one list read with no device resolution
    behind it. Only a blacklist with something in it costs a `resolve_serial()`.

    `revive=False` stops at the blacklist. Nothing in this program passes it;
    it exists so a caller that is about to restart the devnode itself, or that
    is deliberately only editing the list, can say so.
    """
    say = log_fn or (lambda t: log.info("%s", t))
    serial = (serial or "").strip().lower()
    try:
        rec = None
        for r in read_records():
            if (r.get("serial") or "").lower() == serial:
                rec = r
                break

        hh = HidHide.detect(cli_override)
        if rec is None:
            ok = _unhide_unrecorded(hh, serial, say, revive=revive)
            if ok:
                clear_hide_status(serial)
            return ok

        ids = [i for i in (rec.get("instance_ids") or []) if i]
        parent = (rec.get("parent_id") or "").strip()
        if hh is not None and ids:
            # THE RECORDED IDS ARE A CLEANUP TOKEN, NOT IDENTITY
            # (`resolve_serial`), and the two can disagree: a re-pair between
            # the hide and the stop changes the instance ID, a DualSense
            # exposes more than one HID collection, and a hide/revive cycle
            # bumps the child's re-enumeration counter -- `8&2FDE51C0&0&0000`
            # became `8&110FB383&11&0000` on the 2026-08-27 machine.
            #
            # That last one is why the parent widening is UNCONDITIONAL rather
            # than a fallback for a failed removal. Removing an ID that is not
            # in the list is a verifiable success by every test this module can
            # make -- the entry really is gone -- while the entry that is
            # actually hiding the pad, its churned sibling, sits there
            # untouched and the record is deleted on the strength of it. The
            # parent is readable even for a phantom child, so it is the only
            # thing that still identifies the pad in that state.
            targets = list(ids)
            for extra in entries_under_parent(hh.hidden_raw(), parent):
                if not any(norm_instance_id(extra) == norm_instance_id(i)
                           for i in targets):
                    targets.append(extra)
            if not hh.unhide(targets):
                # Only now is a device enumeration worth its cost -- and only
                # while HidHide is still answering, because an unreadable
                # blacklist is not something a second resolve can fix.
                widened = list(targets)
                if hh.hidden_raw():
                    for extra in resolve_serial(serial, cli=hh.cli):
                        if not any(norm_instance_id(extra) == norm_instance_id(i)
                                   for i in widened):
                            widened.append(extra)
                if len(widened) == len(targets) or not hh.unhide(widened):
                    say(f"could not unhide {serial}; the record is kept. Run "
                        f"`ds5bridge unhide`")
                    return False
                targets = widened
            ids = targets

        # AFTER the verified removal, never before: the record is the only
        # thing that will bring anybody back here if this went wrong.
        remove_record(rec.get("_path") or serial)
        clear_hide_status(serial)
        if rec.get("cloak_enabled_by_us") and hh is not None and not read_records():
            hh.set_active(False)
        if ids and revive:
            # An empty blacklist is NOT a visible controller. Measured on the
            # reporting user's machine: `--dev-list` empty, cloak harmless, and
            # `hid_enumerate` still showing nothing, because the HID child under
            # the pad's Bluetooth node had gone phantom and only a
            # re-enumeration brings it back.
            revive_serial(serial, parent_id=parent, log_fn=say)
        elif ids:
            say("Bluetooth pad is visible again")
        return True
    except Exception:  # noqa: BLE001
        log.exception("unhiding %s failed", serial)
        return False


def _unhide_unrecorded(hh: "HidHide | None", serial: str, say,
                       revive: bool = True) -> bool:
    """No journal record -- so check HidHide itself before claiming success.

    This is the tray's "hide toggle off" for a controller whose record was
    already consumed by a lying unhide, and it is the difference between the
    tray believing it unhid the pad and the pad actually being usable.

    Deliberately narrow. It removes only entries that belong to THIS serial --
    either resolving to its live HID instances, or hanging off its Bluetooth
    parent devnode. A stranger's DS4Windows entry satisfies neither test.

    The parent half is not redundant: `resolve_serial()` goes through hidapi,
    and a pad whose HID child has gone phantom is invisible to hidapi by
    definition, so on the machine that needs this most the address route
    returns nothing at all.
    """
    if hh is None:
        return True
    current = hh.hidden_raw()
    if not current:
        # [] is "nothing is hidden, by us or anyone". None is "unreadable", and
        # with no record there is no debt to keep alive over it -- `sweep()` and
        # `doctor` are the places that report an unreachable HidHide.
        return True
    parent = bt_parent_for_serial(serial)
    ids = resolve_serial(serial, cli=hh.cli)
    stale = list(hh.still_hidden(ids) or [])
    for entry in entries_under_parent(current, parent):
        if not any(norm_instance_id(entry) == norm_instance_id(s) for s in stale):
            stale.append(entry)
    if not stale:
        if revive:
            revive_serial(serial, parent_id=parent, log_fn=say)
        return True
    say(f"{serial} is in HidHide's blacklist with no record of ours -- "
        f"removing {len(stale)} leftover entr(y/ies)")
    if not hh.unhide(stale):
        say(f"could not unhide {serial}; run `ds5bridge unhide --all-hidhide`")
        return False
    if revive:
        revive_serial(serial, parent_id=parent, log_fn=say)
    else:
        say("Bluetooth pad is visible again")
    return True


def hidden_serials() -> list[str]:
    """Serials WE currently have hidden, from the journal.

    `manager.poll_once()` unions this into the present set. A controller we hid
    that the tray cannot enumerate is present, not gone -- see `our_images()`
    for what happens without this.
    """
    return [(r.get("serial") or "").lower() for r in read_records()
            if r.get("serial")]


# ---------------------------------------------------------------------------


def _file_version(path: str) -> str:
    """FileVersion out of a PE's version resource, or ""."""
    if sys.platform != "win32":
        return ""
    try:
        ver = ctypes.windll.version
        size = ver.GetFileVersionInfoSizeW(ctypes.c_wchar_p(path), None)
        if not size:
            return ""
        buf = ctypes.create_string_buffer(size)
        if not ver.GetFileVersionInfoW(ctypes.c_wchar_p(path), 0, size, buf):
            return ""
        block = ctypes.c_void_p()
        length = ctypes.c_uint(0)
        if not ver.VerQueryValueW(buf, ctypes.c_wchar_p("\\"),
                                  ctypes.byref(block), ctypes.byref(length)):
            return ""
        # VS_FIXEDFILEINFO: dwSignature, dwStrucVersion, then the two version
        # DWORDs we want, most-significant first.
        fixed = ctypes.cast(block, ctypes.POINTER(ctypes.c_uint * 4)).contents
        ms, ls = fixed[2], fixed[3]
        return (f"{(ms >> 16) & 0xFFFF}.{ms & 0xFFFF}."
                f"{(ls >> 16) & 0xFFFF}.{ls & 0xFFFF}")
    except Exception:  # noqa: BLE001
        return ""
