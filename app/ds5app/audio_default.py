"""Voice typing through the PAD's microphone: switch Windows' default capture
device to the DualSense, open Win+H, and put the old microphone back after.

Why this exists
---------------
Windows voice typing (Win+H) listens on the *default* communications/console
capture device, and that is almost never the controller -- it is the laptop's
array mic, or a headset. But the bridge presents the pad as a wired DualSense,
and a wired DualSense is a USB audio device: Windows shows an active capture
endpoint called something like "Headset Microphone (2- DualSense Wireless
Controller)". So "dictate into the pad" is: make that endpoint the default,
press Win+H, and -- because nobody wants their meeting microphone silently
swapped for good -- restore the previous default when dictation ends.

How, without dependencies
-------------------------
Two COM interfaces, driven through raw ctypes vtables (no comtypes, no
pywin32; the app layer is stdlib-only by rule):

  * `IMMDeviceEnumerator` (documented) enumerates capture endpoints with
    their state and `PKEY_Device_FriendlyName`, and answers "what is the
    default for role X".
  * `IPolicyConfig` (UNdocumented, stable since Vista, what every
    "SoundSwitch"-style tool uses) has `SetDefaultEndpoint(id, role)`. There
    is no documented way to set the default device; this is the one there is.

Everything COM lives in `AudioSystem` and fails SOFT: an empty list, None or
False plus one log line, never an exception. The interface a bad HRESULT or a
missing DLL reaches is the chord engine's dispatch thread, and a chord that
raises must cost one log line, not the feature.

The decisions -- which endpoint is the pad, what to remember, what to restore
-- are pure Python (`select_pad_mic`, `DictationToggle`) over a tiny
`AudioSystem`-shaped seam, and are what the unit tests exercise with fakes.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from dataclasses import dataclass

log = logging.getLogger("ds5app.audio_default")

#: ERole
E_CONSOLE, E_MULTIMEDIA, E_COMMUNICATIONS = 0, 1, 2
ALL_ROLES = (E_CONSOLE, E_MULTIMEDIA, E_COMMUNICATIONS)
#: EDataFlow
E_RENDER, E_CAPTURE = 0, 1
#: DEVICE_STATE_xxx (mmdeviceapi.h)
DEVICE_STATE_ACTIVE = 0x1
DEVICE_STATE_DISABLED = 0x2
DEVICE_STATE_NOTPRESENT = 0x4
DEVICE_STATE_UNPLUGGED = 0x8
DEVICE_STATE_ALL = 0xF

#: What the pad's capture endpoint is called, case-insensitively. Windows
#: builds the friendly name from the USB product string, "DualSense Wireless
#: Controller", with a "2- "/"3- " instance prefix that moves between runs
#: (CONTRIBUTING.md warns never to trust it) -- so match the substring only.
PAD_NAME_NEEDLE = "dualsense"

#: Between switching the default device and pressing Win+H. The audio
#: service re-routes on a device change asynchronously; opening voice typing
#: in the same instant has been seen to bind it to the OLD default.
SWITCH_SETTLE_S = 0.15

CLSID_MMDeviceEnumerator = "{BCDE0395-E52F-467C-8E3D-C4579291692E}"
IID_IMMDeviceEnumerator = "{A95664D2-9614-4F35-A746-DE8DB63617E6}"
CLSID_PolicyConfigClient = "{870AF99C-171D-4F9E-AF0D-E63DF40C2BC9}"
IID_IPolicyConfig = "{F8679F50-850A-41CF-9C72-430F290290C8}"
#: PKEY_Device_FriendlyName = {a45c254e-df1c-4efd-8020-67d146a850e0}, 14
PKEY_Device_FriendlyName_FMTID = "{A45C254E-DF1C-4EFD-8020-67D146A850E0}"
PKEY_Device_FriendlyName_PID = 14


@dataclass(frozen=True)
class Endpoint:
    """One audio capture endpoint as Windows lists it."""

    id: str
    name: str
    state: int = DEVICE_STATE_ACTIVE

    @property
    def active(self) -> bool:
        return self.state == DEVICE_STATE_ACTIVE


def select_pad_mic(endpoints, needle: str = PAD_NAME_NEEDLE):
    """The endpoint voice typing should listen on, or None.

    Only an ACTIVE endpoint can capture, so nothing else qualifies -- an
    unplugged or disabled DualSense entry (Windows keeps them around) would
    make Win+H open onto silence. Among several active pads, the one whose
    name says "Microphone" wins over a generic entry, then the alphabetically
    first, so two bridged pads give the same answer every time.
    """
    n = (needle or "").lower()
    pads = [e for e in endpoints
            if e.active and n and n in (e.name or "").lower()]
    if not pads:
        return None
    pads.sort(key=lambda e: (0 if "microphone" in e.name.lower() else 1,
                             e.name.lower(), e.id))
    return pads[0]


class DictationToggle:
    """Two presses: borrow the microphone and open voice typing; close it and
    give the microphone back.

    `audio` is anything with `capture_endpoints()`, `default_capture_id(role)`
    and `set_default_capture(id, role)`; `send_win_h` presses the shortcut.
    Whether or not a pad microphone was found, the toggle still opens and
    closes voice typing -- the user asked to dictate, and Win+H itself is a
    toggle, so this must stay in step with it.
    """

    def __init__(self, audio, send_win_h, sleep=None, roles=ALL_ROLES,
                 settle_s: float = SWITCH_SETTLE_S):
        self.audio = audio
        self.send_win_h = send_win_h
        self.sleep = sleep or time.sleep
        self.roles = tuple(roles)
        self.settle_s = settle_s
        self._lock = threading.Lock()
        #: role -> endpoint id that was the default before we switched. None
        #: while nothing is borrowed, so `restore()` is a no-op then.
        self._previous: dict | None = None
        self.active = False

    def toggle(self) -> bool:
        """-> whether voice typing is now considered open."""
        with self._lock:
            if not self.active:
                self._borrow()
                self._press()
                self.active = True
            else:
                self._press()
                self._give_back()
                self.active = False
            return self.active

    def restore(self) -> None:
        """The close path: the previous microphone back, no keystrokes.

        Sending Win+H from a teardown would be a keystroke into whatever the
        user is doing; leaving voice typing open costs nothing and closes on
        its own. The borrowed default device is the one thing that must not
        outlive the bridge.
        """
        with self._lock:
            self._give_back()
            self.active = False

    # -- internals ---------------------------------------------------------

    def _press(self) -> None:
        try:
            self.send_win_h()
        except Exception:  # noqa: BLE001
            log.exception("Win+H did not go out")

    def _borrow(self) -> None:
        try:
            endpoints = list(self.audio.capture_endpoints())
        except Exception:  # noqa: BLE001
            log.warning("could not list capture devices", exc_info=True)
            endpoints = []
        pick = select_pad_mic(endpoints)
        if pick is None:
            log.info("no active DualSense microphone -- voice typing uses "
                     "the current default")
            return
        previous = {}
        for role in self.roles:
            try:
                cur = self.audio.default_capture_id(role)
            except Exception:  # noqa: BLE001
                cur = None
            if cur:
                previous[role] = cur
        if previous and all(previous.get(r) == pick.id for r in self.roles):
            return                      # already the default: nothing to undo
        switched = False
        for role in self.roles:
            try:
                switched |= bool(self.audio.set_default_capture(pick.id, role))
            except Exception:  # noqa: BLE001
                log.warning("setting the default microphone failed",
                            exc_info=True)
        if switched:
            self._previous = previous
            log.info("default microphone -> %s", pick.name)
            if self.settle_s > 0:
                self.sleep(self.settle_s)

    def _give_back(self) -> None:
        previous, self._previous = self._previous, None
        if not previous:
            return
        for role, dev in previous.items():
            try:
                self.audio.set_default_capture(dev, role)
            except Exception:  # noqa: BLE001
                log.warning("restoring the default microphone failed",
                            exc_info=True)
        log.info("default microphone restored")


# ---------------------------------------------------------------------------
# the real thing: ctypes over the two COM interfaces
# ---------------------------------------------------------------------------

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _ole32 = ctypes.windll.ole32
    _ole32.CoInitializeEx.restype = ctypes.c_long
    _ole32.CoInitializeEx.argtypes = (ctypes.c_void_p, wintypes.DWORD)
    _ole32.CoUninitialize.restype = None
    _ole32.CoTaskMemFree.restype = None
    _ole32.CoTaskMemFree.argtypes = (ctypes.c_void_p,)

    _COINIT_APARTMENTTHREADED = 0x2
    _CLSCTX_ALL = 0x17
    _S_OK, _S_FALSE = 0, 1
    _RPC_E_CHANGED_MODE = -2147417850          # 0x80010106
    _VT_LPWSTR = 31
    _STGM_READ = 0

    class _GUID(ctypes.Structure):
        _fields_ = (("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                    ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8))

        @classmethod
        def of(cls, text: str) -> "_GUID":
            g = cls()
            hr = _ole32.CLSIDFromString(text, ctypes.byref(g))
            if hr != 0:
                raise OSError(f"CLSIDFromString({text}) -> {hr:#x}")
            return g

    class _PROPERTYKEY(ctypes.Structure):
        _fields_ = (("fmtid", _GUID), ("pid", wintypes.DWORD))

    class _PROPVARIANT(ctypes.Structure):
        # vt + 3 reserved WORDs, then the 16-byte (x64) / 8-byte (x86) union;
        # only the LPWSTR arm is read here.
        _fields_ = (("vt", wintypes.USHORT), ("r1", wintypes.WORD),
                    ("r2", wintypes.WORD), ("r3", wintypes.WORD),
                    ("pwszVal", ctypes.c_void_p), ("pad", ctypes.c_void_p))

    _ole32.PropVariantClear.argtypes = (ctypes.POINTER(_PROPVARIANT),)
    _ole32.PropVariantClear.restype = ctypes.c_long

    def _method(ptr, index: int, *argtypes):
        """Bound COM method `index` of the interface behind `ptr`.

        A COM interface pointer points at a vtable: an array of function
        pointers whose first three are IUnknown's. `HRESULT` as restype makes
        ctypes raise OSError on a failure code, which the callers turn into
        the soft failure this module promises.
        """
        vtbl = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
        proto = ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, *argtypes)
        return proto(vtbl[index])

    def _release(ptr) -> None:
        if not ptr or not ptr.value:
            return
        try:
            vtbl = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
            ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtbl[2])(ptr)
        except Exception:  # noqa: BLE001
            pass

    class _Com:
        """CoInitializeEx on entry, CoUninitialize on exit -- when we own it."""

        def __enter__(self):
            hr = _ole32.CoInitializeEx(None, _COINIT_APARTMENTTHREADED)
            # S_OK/S_FALSE: ours to undo. RPC_E_CHANGED_MODE: the thread is
            # already an MTA -- usable as is, and not ours to uninitialise.
            self._owned = hr in (_S_OK, _S_FALSE)
            if hr < 0 and hr != _RPC_E_CHANGED_MODE:
                raise OSError(f"CoInitializeEx -> {hr & 0xFFFFFFFF:#x}")
            return self

        def __exit__(self, *exc):
            if self._owned:
                _ole32.CoUninitialize()
            return False

    def _create(clsid: str, iid: str):
        ptr = ctypes.c_void_p()
        hr = _ole32.CoCreateInstance(ctypes.byref(_GUID.of(clsid)), None,
                                     _CLSCTX_ALL, ctypes.byref(_GUID.of(iid)),
                                     ctypes.byref(ptr))
        if hr != 0 or not ptr.value:
            raise OSError(f"CoCreateInstance({clsid}) -> {hr & 0xFFFFFFFF:#x}")
        return ptr

    def _device_id(dev) -> str:
        raw = ctypes.c_void_p()
        _method(dev, 5, ctypes.POINTER(ctypes.c_void_p))(dev, ctypes.byref(raw))
        try:
            return ctypes.wstring_at(raw.value) if raw.value else ""
        finally:
            _ole32.CoTaskMemFree(raw)

    def _device_state(dev) -> int:
        st = wintypes.DWORD()
        _method(dev, 6, ctypes.POINTER(wintypes.DWORD))(dev, ctypes.byref(st))
        return int(st.value)

    def _friendly_name(dev) -> str:
        store = ctypes.c_void_p()
        _method(dev, 4, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p))(
            dev, _STGM_READ, ctypes.byref(store))
        try:
            key = _PROPERTYKEY(_GUID.of(PKEY_Device_FriendlyName_FMTID),
                               PKEY_Device_FriendlyName_PID)
            pv = _PROPVARIANT()
            _method(store, 5, ctypes.POINTER(_PROPERTYKEY),
                    ctypes.POINTER(_PROPVARIANT))(store, ctypes.byref(key),
                                                  ctypes.byref(pv))
            try:
                if pv.vt == _VT_LPWSTR and pv.pwszVal:
                    return ctypes.wstring_at(pv.pwszVal)
                return ""
            finally:
                _ole32.PropVariantClear(ctypes.byref(pv))
        finally:
            _release(store)

    class AudioSystem:
        """The real `IMMDeviceEnumerator` / `IPolicyConfig` calls. Fail soft."""

        def capture_endpoints(self) -> list:
            out: list = []
            try:
                with _Com():
                    enum = _create(CLSID_MMDeviceEnumerator,
                                   IID_IMMDeviceEnumerator)
                    try:
                        coll = ctypes.c_void_p()
                        _method(enum, 3, ctypes.c_int, wintypes.DWORD,
                                ctypes.POINTER(ctypes.c_void_p))(
                            enum, E_CAPTURE, DEVICE_STATE_ALL,
                            ctypes.byref(coll))
                        try:
                            n = wintypes.UINT()
                            _method(coll, 3, ctypes.POINTER(wintypes.UINT))(
                                coll, ctypes.byref(n))
                            for i in range(int(n.value)):
                                dev = ctypes.c_void_p()
                                _method(coll, 4, wintypes.UINT,
                                        ctypes.POINTER(ctypes.c_void_p))(
                                    coll, i, ctypes.byref(dev))
                                try:
                                    out.append(Endpoint(_device_id(dev),
                                                        _friendly_name(dev),
                                                        _device_state(dev)))
                                except OSError:
                                    log.debug("capture endpoint %d unreadable",
                                              i, exc_info=True)
                                finally:
                                    _release(dev)
                        finally:
                            _release(coll)
                    finally:
                        _release(enum)
            except Exception:  # noqa: BLE001
                log.warning("enumerating capture devices failed", exc_info=True)
            return out

        def default_capture_id(self, role: int = E_CONSOLE):
            try:
                with _Com():
                    enum = _create(CLSID_MMDeviceEnumerator,
                                   IID_IMMDeviceEnumerator)
                    try:
                        dev = ctypes.c_void_p()
                        _method(enum, 4, ctypes.c_int, ctypes.c_int,
                                ctypes.POINTER(ctypes.c_void_p))(
                            enum, E_CAPTURE, int(role), ctypes.byref(dev))
                        try:
                            return _device_id(dev) or None
                        finally:
                            _release(dev)
                    finally:
                        _release(enum)
            except Exception:  # noqa: BLE001
                # E_NOTFOUND when the machine has no capture device at all.
                log.debug("no default capture device for role %d", role,
                          exc_info=True)
                return None

        def set_default_capture(self, device_id: str, role: int) -> bool:
            if not device_id:
                return False
            try:
                with _Com():
                    pc = _create(CLSID_PolicyConfigClient, IID_IPolicyConfig)
                    try:
                        _method(pc, 13, ctypes.c_wchar_p, ctypes.c_int)(
                            pc, device_id, int(role))
                        return True
                    finally:
                        _release(pc)
            except Exception:  # noqa: BLE001
                log.warning("IPolicyConfig.SetDefaultEndpoint failed (role %d)",
                            role, exc_info=True)
                return False

else:  # pragma: no cover - the app targets Windows; keep imports safe elsewhere
    class AudioSystem:
        """No audio endpoints anywhere but Windows."""

        def capture_endpoints(self) -> list:
            return []

        def default_capture_id(self, role: int = E_CONSOLE):
            return None

        def set_default_capture(self, device_id: str, role: int) -> bool:
            return False
