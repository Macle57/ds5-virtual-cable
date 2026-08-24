"""Locating and driving `usbip.exe` (vadimgrn/usbip-win2).

Two jobs, and the second one is the reason this is a module rather than three
lines of `subprocess.run`:

1. **Find `usbip.exe`.** A packaged exe cannot assume a PATH entry or a
   hard-coded `C:\\Program Files\\USBip`. Probed in order: an explicit override,
   `DS5_USBIP_EXE`, the Inno Setup uninstall registry key (which is where the
   real install location lives), the usual Program Files paths, then PATH.
2. **Fail with something a person can act on.** usbip-win2 is a signed kernel
   driver package; this project deliberately does NOT bundle or install it (see
   `docs/USER-GUIDE.md`). When it is absent the only useful thing to do is say
   exactly which release to install and where the instructions are.

Everything here is read-only with respect to the system except `attach`,
`detach` and `attach -X`, which are the three operations the bridge owns.
**`usbipd` (the unrelated usbipd-win service on port 3240) is never touched.**
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass

#: Release this project is validated against. 0.9.7.8 carries the maintainer's
#: own memory-corruption/BSOD warning -- do not point anyone at it.
REQUIRED_RELEASE = "0.9.7.7"
RELEASE_URL = "https://github.com/vadimgrn/usbip-win2/releases/tag/v0.9.7.7"

#: Ours. `usbipd-win` owns 3240 and must never be disturbed.
DEFAULT_PORT = 3241

_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


class UsbipNotFound(RuntimeError):
    """usbip.exe could not be located. The message is the user-facing text."""


class UsbipError(RuntimeError):
    """usbip.exe ran and failed."""


MISSING_MESSAGE = f"""\
usbip-win2 is not installed, so there is nothing for the virtual controller to
attach to.

  1. Download USBip-{REQUIRED_RELEASE}-x64.exe from
     {RELEASE_URL}
  2. Run it (it installs two Microsoft-signed drivers; no test-signing, no
     reboot was needed on the development machine).
  3. Start this program again.

Do not install 0.9.7.8 -- its own maintainer warns it can corrupt memory.
Full instructions: docs/USER-GUIDE.md in this project.\
"""


def _from_registry() -> list[str]:
    """Read the Inno Setup uninstall key usbip-win2 writes.

    Deliberately narrow: `usbipd-win` also matches a naive "usbip" search and is
    a completely different product that we must not drive.
    """
    if sys.platform != "win32":
        return []
    try:
        import winreg
    except ImportError:  # pragma: no cover - non-Windows
        return []
    out: list[str] = []
    roots = (
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
    )
    for hive, path in roots:
        try:
            key = winreg.OpenKey(hive, path)
        except OSError:
            continue
        with key:
            for i in range(winreg.QueryInfoKey(key)[0]):
                try:
                    sub = winreg.EnumKey(key, i)
                    with winreg.OpenKey(key, sub) as k:
                        name = winreg.QueryValueEx(k, "DisplayName")[0]
                        if not str(name).lower().startswith("usbip version"):
                            continue
                        loc = winreg.QueryValueEx(k, "InstallLocation")[0]
                except OSError:
                    continue
                if loc:
                    out.append(os.path.join(str(loc), "usbip.exe"))
    return out


def find_usbip(explicit: str | None = None) -> str:
    """Absolute path to usbip.exe, or raise `UsbipNotFound` with the guide text."""
    candidates: list[str] = []
    if explicit:
        candidates.append(explicit)
    env = os.environ.get("DS5_USBIP_EXE")
    if env:
        candidates.append(env)
    candidates += _from_registry()
    for base in (os.environ.get("ProgramFiles", r"C:\Program Files"),
                 os.environ.get("ProgramW6432", r"C:\Program Files"),
                 os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")):
        if base:
            candidates.append(os.path.join(base, "USBip", "usbip.exe"))
    found = shutil.which("usbip")
    if found:
        candidates.append(found)

    seen = set()
    for c in candidates:
        c = os.path.abspath(c)
        if c in seen:
            continue
        seen.add(c)
        if os.path.isfile(c):
            return c
    raise UsbipNotFound(MISSING_MESSAGE)


@dataclass
class Result:
    code: int
    out: str

    @property
    def ok(self) -> bool:
        return self.code == 0


class Usbip:
    """Thin, always-timed-out wrapper around usbip.exe.

    Every call carries a timeout. A hardware-facing command that can hang
    forever looks exactly like a driver fault (STATUS.md 15.5 trap 5), and this
    one talks to a kernel driver.
    """

    def __init__(self, exe: str | None = None, port: int = DEFAULT_PORT,
                 host: str = "127.0.0.1", busid: str = "1-1"):
        self.exe = find_usbip(exe)
        self.port = port
        self.host = host
        self.busid = busid

    # -- plumbing ---------------------------------------------------------

    def _run(self, args: list[str], timeout: float = 20.0) -> Result:
        try:
            p = subprocess.run([self.exe] + args, capture_output=True, text=True,
                               timeout=timeout, creationflags=_CREATE_NO_WINDOW)
        except subprocess.TimeoutExpired:
            return Result(-1, f"timed out after {timeout}s: usbip {' '.join(args)}")
        return Result(p.returncode, (p.stdout or "") + (p.stderr or ""))

    def version(self) -> str:
        return self._run(["--version"], timeout=10.0).out.strip()

    # -- the three operations the bridge owns -----------------------------

    def attach(self, timeout: float = 25.0) -> Result:
        # --tcp-port is a GLOBAL option and must precede the subcommand.
        return self._run(["--tcp-port", str(self.port), "attach",
                          "-r", self.host, "-b", self.busid], timeout=timeout)

    def detach(self, port_no: int) -> Result:
        return self._run(["detach", "-p", str(port_no)])

    def stop_auto_reattach(self) -> Result:
        """`attach -X` / --stop-all.

        NOT optional. `usbip attach` arms a background auto-re-attach, so
        detaching without stopping it silently reacquires the device the moment
        a server comes back on the port (STATUS.md 15.5 trap 1).
        """
        return self._run(["attach", "-X"])

    def list_ports(self) -> Result:
        """`usbip port`. Prints nothing at all when nothing is attached.

        Not named `port()`: `self.port` is the TCP port number, and the two
        collided silently until the first call raised "'int' object is not
        callable" at exactly the wrong moment (during teardown).
        """
        return self._run(["port"], timeout=15.0)

    #: `usbip port` prints, per attached device:
    #:     Port 01: device in use at High Speed(480Mbps)
    #:              Sony Corp. : DualSense wireless controller (PS5) (054c:0ce6)
    #:                -> usbip://127.0.0.1:3241/1-1
    #: The third line is what makes a port attributable to a particular server.
    _PORT_RE = re.compile(r"Port\s+(\d+):")
    _URL_RE = re.compile(r"usbip://([^/\s]+)/(\S+)")

    def parse_ports(self, text: str | None = None) -> list[tuple[int, str]]:
        """-> [(port number, `host:port/busid` or "")] for every attached device."""
        out: list[tuple[int, str]] = []
        cur: int | None = None
        for line in (self.list_ports().out if text is None else text).splitlines():
            m = self._PORT_RE.search(line)
            if m:
                if cur is not None:
                    out.append((cur, ""))
                cur = int(m.group(1))
                continue
            u = self._URL_RE.search(line)
            if u and cur is not None:
                out.append((cur, f"{u.group(1)}/{u.group(2)}"))
                cur = None
        if cur is not None:
            out.append((cur, ""))
        return out

    def attached_ports(self) -> list[int]:
        """EVERY attached usbip device, whoever owns it."""
        return [p for p, _ in self.parse_ports()]

    def our_ports(self) -> list[int]:
        """Only devices served by THIS host:port.

        The distinction is not academic. `usbip port` is a machine-wide table,
        so "is anything attached?" answers a different question from "is
        anything of MINE attached?" -- and treating the first as the second
        makes stale-state cleanup detach somebody else's device. Measured
        2026-08-25: a second bridge started on port 3242 read the first one's
        device on 3241 as leftovers and detached it, killing a running soak six
        minutes in.
        """
        want = f"{self.host}:{self.port}/"
        return [p for p, url in self.parse_ports() if url.startswith(want)]

    def is_clean(self) -> bool:
        return not self.our_ports()
