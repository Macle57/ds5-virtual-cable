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
"""

from __future__ import annotations

import logging
import subprocess
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

OK = "ok"                     # nothing stale seen
REFRESHED = "refreshed"       # stale, Audiosrv restarted, now correct
NEED_ADMIN = "need-admin"     # stale, not elevated: told the user
FAILED = "failed"             # stale, restart failed or did not help

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

    def __init__(self, audio, restart, is_admin, lock=None,
                 sleep=time.sleep, clock=time.monotonic,
                 watch_s: float = WATCH_S, poll_s: float = POLL_S):
        self.audio = audio
        self.restart = restart
        self.is_admin = is_admin
        self.lock = lock
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


class _NullLock:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# ---------------------------------------------------------------------------
# the real thing
# ---------------------------------------------------------------------------

_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def restart_audio_service(timeout: float = 60.0) -> bool:
    """`Restart-Service -Force` (Audiosrv's dependents are demand-start
    helpers such as AarSvc; -Force stops them too and they restart on use)."""
    try:
        r = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
             f"Restart-Service -Name {AUDIO_SERVICE} -Force -ErrorAction Stop"],
            capture_output=True, text=True, timeout=timeout,
            creationflags=_CREATE_NO_WINDOW)
    except Exception:  # noqa: BLE001
        log.warning("restarting %s failed", AUDIO_SERVICE, exc_info=True)
        return False
    if r.returncode != 0:
        log.warning("restarting %s failed: %s", AUDIO_SERVICE,
                    (r.stderr or r.stdout).strip()[:300])
        return False
    return True


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
        result = RangeRefresher(EndpointVolumes(), restart_audio_service,
                                _is_admin, lock=_NamedMutex(MUTEX_NAME)).run()
    except Exception:  # noqa: BLE001
        log.warning("volume range check failed", exc_info=True)
        return FAILED
    msg = {
        REFRESHED: ("info", "Windows Audio restarted once to load the "
                            "controller's real volume ranges"),
        NEED_ADMIN: ("warn", ADVICE),
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
