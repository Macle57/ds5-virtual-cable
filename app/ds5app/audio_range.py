"""Make Windows forget the volume ranges an older bridge declared.

The problem, measured 2026-10-28/29 on legion (Windows 11 26200)
----------------------------------------------------------------
Up to 1.2.0 the virtual pad's UAC1 feature units answered GET_MIN/MAX/RES
with assumed values, -96..0 dB in 1 dB steps, on both the speaker and the
microphone. They now answer what a real wired DualSense answers (emulator
`uac.py`): speaker -100..0 dB, microphone 0..+48 dB in 0.4765625 dB steps.
After upgrading, Windows still showed the OLD range for the virtual pad:
pycaw's `GetVolumeRange` said -96..0 on both endpoints, so every slider
position meant <= 0 dB and the microphone never got its gain.

Where the old range lives, from a UAC request log on the device side and
pycaw on the host side:

  * NOT in the device or the driver. On every attach usbaudio.sys asks the
    device GET_CUR/GET_MIN/GET_MAX/GET_RES on both feature units, and the new
    build answered 0x0000/0x3000/0x007A (mic) and 0x9C00/0x0000/0x0100
    (speaker), while `GetVolumeRange` kept reporting -96..0 / 1 dB.
  * NOT on disk. A full export of MMDevices, DeviceClasses, Enum\\USB, the
    MEDIA class keys and SWD\\MMDEVAPI showed nothing range-like changing;
    re-attaching, a devnode disable/enable (`pnputil /disable-device`,
    `/enable-device`, the driver re-queried the device and got the new values)
    and an IPolicyConfig endpoint visibility toggle all left it stale.
  * IN THE WINDOWS AUDIO SERVICE'S MEMORY, per endpoint id, for the life of
    the service. Reproduced on demand: old build attached while Audiosrv
    starts -> -96..0; detach, attach the new build -> still -96..0. Restarting
    Audiosrv (attached or detached) -> 0..+48 / -100..0 on the next look, and
    the endpoint id, its stored level and its settings are kept. Removing the
    phantom devnode while detached also clears it, but that hands the pad NEW
    endpoint ids (the user's default-device and per-app choices are lost) and
    is PnP surgery this project has hung the machine with before -- so not that.

A reboot clears it too, which is why only a machine that ran an old build
since its last boot is affected. That is exactly the "just upgraded" case, so
the bridge checks once after every attach: if any active DualSense endpoint
reports the old -96..0 dB / 1 dB range -- which no real DualSense and no
current build ever declares -- the Windows Audio service is restarted (one
second without sound, once) when we are elevated, as the tray always is, and
otherwise the user is told how to do it.

Two guard rails:

  * At most ONE restart per boot, across every bridge process. A marker in the
    config directory (`audiosrv-refresh.json`, next to echo-delays.json) holds
    the boot time it was written in, and is written BEFORE the restart. If a
    stale range survives the restart -- an old build bridging a second pad,
    say -- later attaches (tray launches, reconnects, power-cycle respawns)
    only log the advice instead of cutting the PC's sound every time.
  * The restart cannot be left half-done by a child process. It drives the
    Service Control Manager in-process (ControlService STOP, wait,
    StartService): a powershell child would have joined the tray's
    kill-on-close job (manager.py, no breakaway) and could die between the
    stop and the start. The bridge process itself can still be killed in that
    gap, so every bridge, and the tray at startup, first starts Audiosrv again
    if it is stopped AND this boot's marker says we were the ones touching it
    (a service the user stopped on purpose is left alone).
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from dataclasses import dataclass

log = logging.getLogger("ds5app.audio_range")

#: (min dB, max dB, step dB) the bridge declared on BOTH feature units up to
#: 1.2.0. Real pads: speaker -100..0 / 1.0, mic 0..+48 / 0.4765625.
STALE_RANGE = (-96.0, 0.0, 1.0)
PAD_NAME_NEEDLE = "dualsense"

#: After attach Windows needs a moment to build the endpoints; look this long.
WATCH_S = 20.0
POLL_S = 1.0

AUDIO_SERVICE = "Audiosrv"
#: Serialises the restart between the per-pad bridge processes: two pads
#: attaching together must not restart the audio service twice.
MUTEX_NAME = "Global\\ds5bridge-audiosrv-refresh"

#: The once-per-boot marker, in the config directory.
MARKER_NAME = "audiosrv-refresh.json"
#: Two boot-time readings (wall clock minus uptime) of one boot agree to
#: within a second on legion; this absorbs clock corrections.
BOOT_TOLERANCE_S = 120.0
#: How long to wait for the service to reach STOPPED / RUNNING.
SERVICE_WAIT_S = 30.0

OK = "ok"                     # nothing stale seen
REFRESHED = "refreshed"       # stale, Audiosrv restarted, now correct
NEED_ADMIN = "need-admin"     # stale, not elevated: told the user
FAILED = "failed"             # stale, restart failed or did not help
ALREADY = "already"           # stale, but this boot already had its restart

ADVICE = ("Windows is still using the volume range an older ds5bridge "
          "reported for the controller, so the microphone slider cannot add "
          "gain. Restart the Windows Audio service (or reboot) once to fix it.")


@dataclass(frozen=True)
class EndpointRange:
    id: str
    name: str
    min_db: float
    max_db: float
    step_db: float


def is_stale(r: EndpointRange) -> bool:
    """A DualSense endpoint still carrying the range 1.2.0 and earlier assumed."""
    if PAD_NAME_NEEDLE not in (r.name or "").lower():
        return False
    return all(abs(a - b) < 1e-3 for a, b in
               zip((r.min_db, r.max_db, r.step_db), STALE_RANGE))


def stale_endpoints(ranges) -> list:
    return [r for r in ranges if is_stale(r)]


class RangeRefresher:
    """The decision, over seams: `audio.volume_ranges()` lists the active
    endpoints' ranges, `restart()` restarts the audio service and returns
    success, `is_admin()` says whether we may. Pure enough to unit test."""

    def __init__(self, audio, restart, is_admin, lock=None, marker=None,
                 sleep=time.sleep, clock=time.monotonic,
                 watch_s: float = WATCH_S, poll_s: float = POLL_S):
        self.audio = audio
        self.restart = restart
        self.is_admin = is_admin
        self.lock = lock
        #: `BootMarker`-shaped: `used()` / `mark()`. None = no limit (tests).
        self.marker = marker
        self.sleep = sleep
        self.clock = clock
        self.watch_s = watch_s
        self.poll_s = poll_s

    def _stale(self) -> list:
        try:
            return stale_endpoints(self.audio.volume_ranges())
        except Exception:  # noqa: BLE001
            log.debug("reading endpoint volume ranges failed", exc_info=True)
            return []

    def run(self) -> str:
        deadline = self.clock() + self.watch_s
        stale = self._stale()
        while not stale and self.clock() < deadline:
            self.sleep(self.poll_s)
            stale = self._stale()
        if not stale:
            return OK
        names = ", ".join(sorted({r.name for r in stale}))
        log.warning("stale volume range on %s: %.1f..%.1f dB / %.1f dB", names,
                    *STALE_RANGE)
        if not self.is_admin():
            return NEED_ADMIN
        with (self.lock if self.lock is not None else _NullLock()):
            # Another bridge may have restarted the service while we waited.
            if not self._stale():
                return REFRESHED
            if self.marker is not None:
                if self.marker.used():
                    log.warning("the Windows Audio service was already "
                                "restarted once this boot; not again")
                    return ALREADY
                # Before the restart: a process killed half-way must still
                # count as this boot's one restart (and lets the next start
                # put a stopped service back, `ensure_running_now`).
                self.marker.mark()
            log.warning("restarting the Windows Audio service to drop it")
            if not self.restart():
                return FAILED
            # The endpoints come back a moment after the service does.
            deadline = self.clock() + self.watch_s
            while self.clock() < deadline:
                self.sleep(self.poll_s)
                ranges = self._ranges_or_none()
                if ranges and any(PAD_NAME_NEEDLE in r.name.lower() for r in ranges):
                    return FAILED if stale_endpoints(ranges) else REFRESHED
            return FAILED

    def _ranges_or_none(self):
        try:
            return self.audio.volume_ranges()
        except Exception:  # noqa: BLE001
            return None


def boot_time() -> float:
    """This boot's start as a wall-clock timestamp (time.time() minus the
    uptime, sleep included). 0.0 when unknown."""
    if sys.platform != "win32":
        return 0.0
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        k32.GetTickCount64.restype = ctypes.c_ulonglong
        return time.time() - k32.GetTickCount64() / 1000.0
    except Exception:  # noqa: BLE001
        return 0.0


class BootMarker:
    """"Did we restart the audio service during THIS boot?" as a tiny JSON
    file holding the boot time it was written in. Shared by every bridge
    process through the config directory. Unreadable = not used; a write that
    fails is logged and the restart goes ahead (the mutex still stops two
    restarts at once)."""

    def __init__(self, path: str, boot=boot_time,
                 tolerance: float = BOOT_TOLERANCE_S):
        self.path = path
        self.boot = boot
        self.tolerance = tolerance

    def used(self) -> bool:
        try:
            with open(self.path, encoding="utf-8") as f:
                stamp = float(json.load(f)["boot"])
        except (OSError, ValueError, KeyError, TypeError):
            return False
        now = self.boot()
        return bool(now) and abs(stamp - now) <= self.tolerance

    def mark(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"boot": self.boot(), "written": time.time()}, f)
            os.replace(tmp, self.path)
        except OSError:
            log.warning("could not write %s", self.path, exc_info=True)


class _NullLock:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# ---------------------------------------------------------------------------
# the real thing
# ---------------------------------------------------------------------------

# Service states (winsvc.h) and the Win32 errors the logic cares about.
SERVICE_STOPPED, SERVICE_START_PENDING, SERVICE_STOP_PENDING, SERVICE_RUNNING = 1, 2, 3, 4
ERROR_DEPENDENT_SERVICES_RUNNING = 1051
ERROR_SERVICE_ALREADY_RUNNING = 1056
ERROR_SERVICE_NOT_ACTIVE = 1062


def _wait_state(sc, want: int, sleep, clock, timeout: float) -> bool:
    deadline = clock() + timeout
    while True:
        if sc.state() == want:
            return True
        if clock() >= deadline:
            return False
        sleep(0.2)


def ensure_service_running(sc, sleep=time.sleep, clock=time.monotonic,
                           timeout: float = SERVICE_WAIT_S) -> bool:
    """Make sure the service ends up RUNNING. `sc` is `ServiceControl`-shaped:
    `state()`, `stop()`, `start()` (Win32 error codes, 0 = ok),
    `active_dependents()`, `stop_named(name)`."""
    st = sc.state()
    if st in (SERVICE_RUNNING, SERVICE_START_PENDING):
        return _wait_state(sc, SERVICE_RUNNING, sleep, clock, timeout)
    if st == SERVICE_STOP_PENDING:
        _wait_state(sc, SERVICE_STOPPED, sleep, clock, timeout)
    err = sc.start()
    if err not in (0, ERROR_SERVICE_ALREADY_RUNNING):
        log.warning("starting %s failed (error %d)", AUDIO_SERVICE, err)
        return False
    return _wait_state(sc, SERVICE_RUNNING, sleep, clock, timeout)


def restart_service(sc, sleep=time.sleep, clock=time.monotonic,
                    timeout: float = SERVICE_WAIT_S) -> bool:
    """Stop, wait for STOPPED, start, wait for RUNNING -- in this process.

    Once a stop was accepted this always ends by starting the service again:
    it never returns with the service left stopped by us. A stop that is
    refused changes nothing and returns False.
    """
    err = sc.stop()
    if err == ERROR_DEPENDENT_SERVICES_RUNNING:
        # Audiosrv's dependents are demand-start helpers (AarSvc on legion);
        # stopping them is what `Restart-Service -Force` does, and they start
        # again on use.
        for name in sc.active_dependents():
            sc.stop_named(name)
        err = sc.stop()
    if err not in (0, ERROR_SERVICE_NOT_ACTIVE):
        log.warning("stopping %s failed (error %d)", AUDIO_SERVICE, err)
        return False
    if not _wait_state(sc, SERVICE_STOPPED, sleep, clock, timeout):
        log.warning("%s did not stop within %.0f s", AUDIO_SERVICE, timeout)
    return ensure_service_running(sc, sleep, clock, timeout)


def ensure_running_now(marker=None, sc=None, is_admin=None) -> bool:
    """Start Audiosrv again if it is stopped and this boot's marker says we
    were restarting it (a bridge or tray killed between stop and start).
    Called by every bridge before its range check and by the tray at startup.
    Returns True when it had to start the service. Never raises."""
    if sys.platform != "win32" and sc is None:
        return False
    try:
        marker = marker if marker is not None else default_marker()
        sc = sc if sc is not None else ServiceControl(AUDIO_SERVICE)
        admin = is_admin if is_admin is not None else _is_admin
        if sc.state() == SERVICE_RUNNING or not marker.used() or not admin():
            return False
        with (_NamedMutex(MUTEX_NAME) if sys.platform == "win32" else _NullLock()):
            if sc.state() == SERVICE_RUNNING:
                return False
            log.warning("%s is stopped after our restart this boot; "
                        "starting it again", AUDIO_SERVICE)
            return ensure_service_running(sc)
    except Exception:  # noqa: BLE001
        log.warning("checking %s failed", AUDIO_SERVICE, exc_info=True)
        return False


def default_marker() -> BootMarker:
    try:
        from . import config as K
        d = K.config_dir()
    except Exception:  # noqa: BLE001
        d = os.path.join(os.environ.get("APPDATA") or ".", "ds5bridge")
    return BootMarker(os.path.join(d, MARKER_NAME))


def _is_admin() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001
        return False


class _NamedMutex:
    """A machine-wide mutex; degrades to no locking if it cannot be made."""

    def __init__(self, name: str):
        self.name = name
        self.handle = None

    def __enter__(self):
        try:
            import ctypes
            k32 = ctypes.windll.kernel32
            k32.CreateMutexW.restype = ctypes.c_void_p
            k32.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_ulong)
            h = k32.CreateMutexW(None, False, self.name)
            if h:
                k32.WaitForSingleObject(h, 120_000)
                self.handle = h
        except Exception:  # noqa: BLE001
            self.handle = None
        return self

    def __exit__(self, *exc):
        if self.handle:
            import ctypes
            k32 = ctypes.windll.kernel32
            k32.ReleaseMutex.argtypes = (ctypes.c_void_p,)
            k32.CloseHandle.argtypes = (ctypes.c_void_p,)
            k32.ReleaseMutex(self.handle)
            k32.CloseHandle(self.handle)
            self.handle = None
        return False


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    from . import audio_default as AD

    _adv = ctypes.WinDLL("advapi32", use_last_error=True)
    _SC_MANAGER_CONNECT = 0x0001
    _SERVICE_QUERY_STATUS = 0x0004
    _SERVICE_ENUMERATE_DEPENDENTS = 0x0008
    _SERVICE_START = 0x0010
    _SERVICE_STOP = 0x0020
    _SERVICE_CONTROL_STOP = 0x00000001
    _SERVICE_ACTIVE = 0x00000001

    class _SERVICE_STATUS(ctypes.Structure):
        _fields_ = [(n, wintypes.DWORD) for n in (
            "dwServiceType", "dwCurrentState", "dwControlsAccepted",
            "dwWin32ExitCode", "dwServiceSpecificExitCode", "dwCheckPoint",
            "dwWaitHint")]

    class _ENUM_SERVICE_STATUSW(ctypes.Structure):
        _fields_ = (("lpServiceName", wintypes.LPWSTR),
                    ("lpDisplayName", wintypes.LPWSTR),
                    ("ServiceStatus", _SERVICE_STATUS))

    _adv.OpenSCManagerW.restype = wintypes.HANDLE
    _adv.OpenSCManagerW.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD)
    _adv.OpenServiceW.restype = wintypes.HANDLE
    _adv.OpenServiceW.argtypes = (wintypes.HANDLE, wintypes.LPCWSTR, wintypes.DWORD)
    _adv.CloseServiceHandle.argtypes = (wintypes.HANDLE,)
    _adv.QueryServiceStatus.argtypes = (wintypes.HANDLE, ctypes.POINTER(_SERVICE_STATUS))
    _adv.ControlService.argtypes = (wintypes.HANDLE, wintypes.DWORD,
                                    ctypes.POINTER(_SERVICE_STATUS))
    _adv.StartServiceW.argtypes = (wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p)
    _adv.EnumDependentServicesW.argtypes = (
        wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD))

    class ServiceControl:
        """The SCM calls `restart_service` needs, each opening and closing its
        own handles. Returns Win32 error codes (0 = ok); raises nothing."""

        def __init__(self, name: str):
            self.name = name

        @staticmethod
        def _call(name: str, access: int, fn) -> int:
            scm = _adv.OpenSCManagerW(None, None, _SC_MANAGER_CONNECT)
            if not scm:
                return ctypes.get_last_error() or 5
            try:
                h = _adv.OpenServiceW(scm, name, access)
                if not h:
                    return ctypes.get_last_error() or 5
                try:
                    return fn(h)
                finally:
                    _adv.CloseServiceHandle(h)
            finally:
                _adv.CloseServiceHandle(scm)

        def state(self) -> int:
            out = []

            def q(h):
                st = _SERVICE_STATUS()
                if not _adv.QueryServiceStatus(h, ctypes.byref(st)):
                    return ctypes.get_last_error()
                out.append(int(st.dwCurrentState))
                return 0
            self._call(self.name, _SERVICE_QUERY_STATUS, q)
            return out[0] if out else 0

        def _stop(self, name: str) -> int:
            def c(h):
                st = _SERVICE_STATUS()
                ok = _adv.ControlService(h, _SERVICE_CONTROL_STOP, ctypes.byref(st))
                return 0 if ok else ctypes.get_last_error()
            return self._call(name, _SERVICE_STOP | _SERVICE_QUERY_STATUS, c)

        def stop(self) -> int:
            return self._stop(self.name)

        def stop_named(self, name: str) -> int:
            return self._stop(name)

        def start(self) -> int:
            def go(h):
                return 0 if _adv.StartServiceW(h, 0, None) else ctypes.get_last_error()
            return self._call(self.name, _SERVICE_START | _SERVICE_QUERY_STATUS, go)

        def active_dependents(self) -> list:
            names: list = []

            def e(h):
                need, count = wintypes.DWORD(), wintypes.DWORD()
                _adv.EnumDependentServicesW(h, _SERVICE_ACTIVE, None, 0,
                                            ctypes.byref(need), ctypes.byref(count))
                if not need.value:
                    return 0
                buf = ctypes.create_string_buffer(need.value)
                if not _adv.EnumDependentServicesW(h, _SERVICE_ACTIVE, buf, need.value,
                                                   ctypes.byref(need), ctypes.byref(count)):
                    return ctypes.get_last_error()
                arr = ctypes.cast(buf, ctypes.POINTER(_ENUM_SERVICE_STATUSW))
                names.extend(arr[i].lpServiceName for i in range(count.value))
                return 0
            self._call(self.name, _SERVICE_ENUMERATE_DEPENDENTS, e)
            return names

    def restart_audio_service() -> bool:
        return restart_service(ServiceControl(AUDIO_SERVICE))

    IID_IAudioEndpointVolume = "{5CDF2C82-841E-4546-9722-0CF74078229A}"
    _E_ALL = 2

    class EndpointVolumes:
        """IMMDeviceEnumerator -> every ACTIVE endpoint (render and capture)
        -> IAudioEndpointVolume::GetVolumeRange. Raw vtables, like
        audio_default (the app layer is stdlib-only)."""

        def volume_ranges(self) -> list:
            out: list = []
            with AD._Com():
                enum = AD._create(AD.CLSID_MMDeviceEnumerator,
                                  AD.IID_IMMDeviceEnumerator)
                try:
                    coll = ctypes.c_void_p()
                    AD._method(enum, 3, ctypes.c_int, wintypes.DWORD,
                               ctypes.POINTER(ctypes.c_void_p))(
                        enum, _E_ALL, AD.DEVICE_STATE_ACTIVE, ctypes.byref(coll))
                    try:
                        n = wintypes.UINT()
                        AD._method(coll, 3, ctypes.POINTER(wintypes.UINT))(
                            coll, ctypes.byref(n))
                        for i in range(int(n.value)):
                            dev = ctypes.c_void_p()
                            AD._method(coll, 4, wintypes.UINT,
                                       ctypes.POINTER(ctypes.c_void_p))(
                                coll, i, ctypes.byref(dev))
                            try:
                                r = self._range(dev)
                                if r is not None:
                                    out.append(r)
                            finally:
                                AD._release(dev)
                    finally:
                        AD._release(coll)
                finally:
                    AD._release(enum)
            return out

        @staticmethod
        def _range(dev):
            try:
                name = AD._friendly_name(dev)
                if PAD_NAME_NEEDLE not in name.lower():
                    return None
                vol = ctypes.c_void_p()
                # IMMDevice::Activate(REFIID, DWORD, PROPVARIANT*, void**)
                AD._method(dev, 3, ctypes.POINTER(AD._GUID), wintypes.DWORD,
                           ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))(
                    dev, ctypes.byref(AD._GUID.of(IID_IAudioEndpointVolume)),
                    AD._CLSCTX_ALL, None, ctypes.byref(vol))
                try:
                    lo, hi, step = ctypes.c_float(), ctypes.c_float(), ctypes.c_float()
                    # IAudioEndpointVolume::GetVolumeRange is vtable slot 20.
                    AD._method(vol, 20, *(ctypes.POINTER(ctypes.c_float),) * 3)(
                        vol, ctypes.byref(lo), ctypes.byref(hi), ctypes.byref(step))
                    return EndpointRange(AD._device_id(dev), name, float(lo.value),
                                         float(hi.value), float(step.value))
                finally:
                    AD._release(vol)
            except OSError:
                log.debug("endpoint volume range unreadable", exc_info=True)
                return None


def check_after_attach(on_event=None) -> str:
    """Run the check once (blocking, up to ~2 x WATCH_S); report through
    `on_event(kind, text)` when the user should know. Never raises."""
    if sys.platform != "win32":
        return OK
    try:
        marker = default_marker()
        ensure_running_now(marker)
        result = RangeRefresher(EndpointVolumes(), restart_audio_service,
                                _is_admin, lock=_NamedMutex(MUTEX_NAME),
                                marker=marker).run()
    except Exception:  # noqa: BLE001
        log.warning("volume range check failed", exc_info=True)
        return FAILED
    msg = {
        REFRESHED: ("info", "Windows Audio restarted once to load the "
                            "controller's real volume ranges"),
        NEED_ADMIN: ("warn", ADVICE),
        ALREADY: ("warn", ADVICE),
        FAILED: ("warn", ADVICE),
    }.get(result)
    if msg and on_event is not None:
        try:
            on_event(*msg)
        except Exception:  # noqa: BLE001
            pass
    return result


def check_in_background(on_event=None) -> threading.Thread:
    t = threading.Thread(target=check_after_attach, args=(on_event,),
                         name="audio-range-check", daemon=True)
    t.start()
    return t
