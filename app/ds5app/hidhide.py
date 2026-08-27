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
        try:
            r = subprocess.run([self.exe, *args], capture_output=True, text=True,
                               timeout=30, creationflags=_CREATE_NO_WINDOW)
            return r.returncode, (r.stdout or "") + (r.stderr or "")
        except Exception as e:  # noqa: BLE001
            log.debug("HidHideCLI %s failed: %s", args, e)
            return -1, ""

    def version(self) -> str:
        code, out = self._run("--version")
        return out.strip() if code == 0 else ""

    def hidden(self) -> list[str] | None:
        code, out = self._run("--dev-list")
        if code != 0:
            return None
        return [ln.strip() for ln in out.splitlines() if ln.strip()]

    def allowed(self) -> list[str] | None:
        """`--app-list` prints re-runnable commands, not bare paths.

            --app-reg "C:\\Program Files\\...\\HidHideCLI.exe"

        so the path is what is inside the quotes.
        """
        code, out = self._run("--app-list")
        if code != 0:
            return None
        paths = []
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            if '"' in line:
                parts = line.split('"')
                if len(parts) >= 2 and parts[1].strip():
                    paths.append(parts[1].strip())
            elif line.startswith("--app-reg"):
                rest = line[len("--app-reg"):].strip()
                if rest:
                    paths.append(rest)
        return paths

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
        got = self.ioctl.hidden()
        if got is None and self.cli is not None:
            got = self.cli.hidden()
        return got or []

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
                merged = list(current)
                for i in wanted:
                    if i not in merged:
                        merged.append(i)
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
        """Remove ONLY these from the blacklist. Idempotent.

        Never "clear the blacklist". The user may be hiding other pads with
        HidHide for DS4Windows, and blowing those away would be a serious
        breach of trust in a program they installed to make a controller work.
        """
        wanted = [i for i in (instance_ids or []) if i]
        if not wanted:
            return True
        with _Mutex():
            current = self.ioctl.hidden()
            if current is not None:
                keep = [i for i in current if i not in wanted]
                if keep == current or self.ioctl.set_hidden(keep):
                    return True
            if self.cli is not None:
                return all(self.cli.unhide_one(i) for i in wanted)
        log.warning("could not unhide %s -- no working HidHide control surface",
                    wanted)
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


def write_record(serial: str, instance_ids, cloak_enabled_by_us: bool = False) -> bool:
    """Record what we are ABOUT to hide. Called BEFORE the hide, always.

    Same `.tmp` + `fsync` + `os.replace` dance as `config.save()`, for the same
    reason: a half-written record is what a power cut during this produces, and
    a record that cannot be parsed is a controller nobody knows to un-hide.
    """
    serial = (serial or "").strip().lower()
    if not serial:
        return False
    path = _record_path(serial)
    tmp = path + ".tmp"
    data = {
        "serial": serial,
        "instance_ids": list(instance_ids or []),
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


def journal_count() -> int:
    return len(read_records())


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
        if hh is not None and ids:
            # Idempotent: entries that are already absent are a no-op, which is
            # exactly what the record-before-hide ordering relies on.
            if not hh.unhide(ids):
                say(f"could not unhide {serial}; its record is kept so the next "
                    f"run tries again")
                continue
            say(f"unhid {serial} ({len(ids)} HID interface(s))")
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
                    log_fn=None) -> list[str]:
    """Hide one controller for the duration of a bridge. -> the IDs hidden.

    Returns [] for every failure, including "HidHide is not installed", and
    never raises. Hiding is a convenience; bridging is the product, and a
    HidHide problem must never be why a bridge fails.

    Ordering, which is the whole of section 7 of the scoping document:

        1. resolve the serial FRESH (instance IDs are not identity)
        2. whitelist our exes -- before any hide, or the tray blinds itself
        3. WRITE THE JOURNAL
        4. hide
        5. enable the cloak if it is off, recording that we did

    A failure at step 3 aborts before step 4. That is deliberate: hiding
    something we failed to record is the one outcome that strands the user.
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

        if not write_record(serial, ids, cloak_enabled_by_us=need_cloak):
            return []

        if not hh.hide(ids):
            remove_record(serial)
            say("HidHide would not accept the hide; the pad stays visible")
            return []

        if need_cloak:
            hh.set_active(True)

        say(f"Bluetooth pad hidden ({len(ids)} HID interface(s)). Games started "
            f"from now on will not see it.")
        return ids
    except Exception:  # noqa: BLE001
        log.exception("hiding %s failed", serial)
        return []


def unhide_for_bridge(serial: str, cli_override: str | None = None,
                      log_fn=None) -> bool:
    """The exact reverse, run from every path that stops a bridge.

    Called unconditionally on stop -- there is no "did we hide it" flag to get
    out of step, because the journal on disk IS that flag and it survives things
    an in-memory flag does not. No record means nothing to do.
    """
    say = log_fn or (lambda t: log.info("%s", t))
    serial = (serial or "").strip().lower()
    try:
        rec = None
        for r in read_records():
            if (r.get("serial") or "").lower() == serial:
                rec = r
                break
        if rec is None:
            return True

        hh = HidHide.detect(cli_override)
        ids = [i for i in (rec.get("instance_ids") or []) if i]
        if hh is not None and ids and not hh.unhide(ids):
            say(f"could not unhide {serial}; run `ds5bridge unhide`")
            return False

        if rec.get("cloak_enabled_by_us") and hh is not None:
            remove_record(rec.get("_path") or serial)
            if not read_records():
                hh.set_active(False)
        else:
            remove_record(rec.get("_path") or serial)
        if ids:
            say("Bluetooth pad is visible again")
        return True
    except Exception:  # noqa: BLE001
        log.exception("unhiding %s failed", serial)
        return False


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
