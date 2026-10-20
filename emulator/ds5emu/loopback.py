"""WASAPI loopback capture of the PC's render endpoints -- the echo
canceller's reference for every speaker that is not the pad's own (aec.py).

Every ACTIVE render endpoint is opened in shared mode with
`AUDCLNT_STREAMFLAGS_LOOPBACK` and the set is re-enumerated every `RESCAN_S`,
so plug-in, unplug and default-device changes all arrive through one diff.
Each packet is delivered as float32 at 48 kHz, one or two channels (stereo
stays stereo; surround is folded down to two), with the QPC presentation
time of its first frame (`qpc * 1e-7` is directly on `time.perf_counter()`'s
timeline on Windows). An endpoint with nothing playing delivers no packets
and costs one `GetNextPacketSize` per poll.

Everything fails SOFT and recovers: an endpoint that cannot be opened is
retried with a growing backoff, a stream that raises anything is closed and
reopened at the next rescan, and the capture thread itself restarts with a
backoff if something escapes it. None of it can reach the microphone path.
Raw ctypes vtables; numpy, plus the canceller's DLL for an endpoint that
needs resampling.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from dataclasses import dataclass

import numpy as np

log = logging.getLogger("ds5emu.loopback")

#: The rate every reference is delivered at: the canceller's (and the USB
#: audio interface's) 48 kHz.
OUT_RATE = 48000

#: How often the endpoint set is re-enumerated.
RESCAN_S = 2.0
#: Backoff for an endpoint that refused to open, and for the capture thread
#: after an unexpected failure: doubling from the first value up to the second.
RETRY_S = (2.0, 60.0)
#: How often open streams are drained. Packets are ~10 ms (the engine period),
#: so 5 ms keeps the newest reference at most ~15 ms stale.
POLL_S = 0.005
#: Shared-mode buffer asked for. Only matters if this thread stalls: anything
#: older is overwritten by the engine (and flagged as a discontinuity).
BUFFER_HNS = 2_000_000          # 200 ms in 100 ns units

#: Endpoints whose friendly name contains this are DualSense speakers --
#: ours or a sibling bridge's virtual pad. Their mix is 4 channels, of which
#: only the first two are the speaker; 2 and 3 drive the haptic actuators.
PAD_NAME_NEEDLE = "dualsense"


@dataclass(frozen=True)
class Endpoint:
    """One render endpoint as Windows lists it."""

    id: str
    name: str


#: WAVEFORMATEXTENSIBLE channel-mask bits -> (weight into L, weight into R).
#: An ITU-style fold-down: centres at -3 dB into both sides, surrounds and
#: backs into their side, LFE at -6 dB into both (it is in the room too).
_FOLD = {
    0x1: (1.0, 0.0), 0x2: (0.0, 1.0),                    # front L / R
    0x4: (0.707, 0.707), 0x8: (0.5, 0.5),                # front centre, LFE
    0x10: (0.707, 0.0), 0x20: (0.0, 0.707),              # back L / R
    0x40: (0.85, 0.35), 0x80: (0.35, 0.85),              # front left/right of centre
    0x100: (0.5, 0.5),                                   # back centre
    0x200: (0.707, 0.0), 0x400: (0.0, 0.707),            # side L / R
}
#: The mask Windows implies for a format that does not carry one.
_DEFAULT_MASK = {1: 0x4, 2: 0x3, 3: 0x7, 4: 0x33, 5: 0x37, 6: 0x3F, 7: 0x13F, 8: 0x63F}


def speaker_channels(frames: np.ndarray, name: str, mask: int = 0) -> np.ndarray:
    """(n, ch) float32 -> (n, 1) or (n, 2) float32: the reference channels.

    Mono stays mono and stereo stays stereo -- the canceller models the two
    speakers of a stereo pair separately, which is worth 20 dB when L and R
    differ (MEASURED, round 5: one mono reference for a stereo HDMI speaker
    removed 2.8 dB of L != R speech, two removed 23 dB). Anything wider (5.1,
    7.1) is folded down to two channels by its channel mask: the canceller's
    filters are per channel and capped at two per endpoint, and a surround
    set is several speakers per side anyway. A DualSense endpoint's channels
    2-3 are haptics, which do not come out of the speaker: only 0-1 count.
    """
    if frames.ndim == 1:
        return frames.astype(np.float32, copy=False).reshape(-1, 1)
    ch = frames.shape[1]
    if ch >= 4 and PAD_NAME_NEEDLE in (name or "").lower():
        return np.ascontiguousarray(frames[:, :2], np.float32)
    if ch <= 2:
        return np.ascontiguousarray(frames, np.float32)
    bits = [b for b in (1 << i for i in range(32)) if (mask or _DEFAULT_MASK.get(ch, 0)) & b]
    w = np.zeros((ch, 2), np.float32)
    for i in range(ch):
        w[i] = _FOLD.get(bits[i], (0.5, 0.5)) if i < len(bits) else (0.5, 0.5)
    # keep a full-scale signal on every speaker within range
    w /= max(1.0, float(w.sum(axis=0).max()) / 2.0)
    return (frames @ w).astype(np.float32)


def probe_mix(ref: np.ndarray) -> np.ndarray:
    """(n, nch) -> (n,): one signal for the delay probe. Unequal weights, so
    anti-phase content (L = -R) does not vanish from it while both speakers
    still play it into the room."""
    if ref.ndim == 1 or ref.shape[1] == 1:
        return ref.reshape(-1)
    w = (1.0 / (1.0 + 0.5 * np.arange(ref.shape[1]))).astype(np.float32)
    return (ref @ w / w.sum()).astype(np.float32)


class Resampler:
    """A stream of (n, nch) frames at `in_rate` -> 48 kHz, through speexdsp's
    resampler (quality 5, windowed-sinc) in ds5aec.dll; only ever used for an
    endpoint whose mix format is not 48 kHz (44.1 kHz on some DACs and HDMI
    sinks). The DLL is the canceller's, which is what this capture serves."""

    def __init__(self, nch: int, in_rate: int, out_rate: int = OUT_RATE):
        self.in_rate = int(in_rate)
        self.out_rate = int(out_rate)
        self._rs = None
        if self.in_rate != self.out_rate:
            from .aec import NativeResampler
            self._rs = NativeResampler(nch, self.in_rate, self.out_rate)

    def push(self, x: np.ndarray) -> np.ndarray:
        if self._rs is None:
            return x
        return self._rs.push(x)


# ---------------------------------------------------------------------------
# the real thing: ctypes over the WASAPI COM interfaces
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

    _COINIT_MULTITHREADED = 0x0
    _CLSCTX_ALL = 0x17
    _S_OK, _S_FALSE = 0, 1
    _RPC_E_CHANGED_MODE = -2147417850          # 0x80010106
    _VT_LPWSTR = 31
    _STGM_READ = 0
    _E_RENDER = 0
    _DEVICE_STATE_ACTIVE = 0x1
    _AUDCLNT_SHAREMODE_SHARED = 0
    _AUDCLNT_STREAMFLAGS_LOOPBACK = 0x00020000
    _AUDCLNT_BUFFERFLAGS_DATA_DISCONTINUITY = 0x1
    _AUDCLNT_BUFFERFLAGS_SILENT = 0x2
    _AUDCLNT_BUFFERFLAGS_TIMESTAMP_ERROR = 0x4
    _WAVE_FORMAT_PCM = 1
    _WAVE_FORMAT_IEEE_FLOAT = 3
    _WAVE_FORMAT_EXTENSIBLE = 0xFFFE

    CLSID_MMDeviceEnumerator = "{BCDE0395-E52F-467C-8E3D-C4579291692E}"
    IID_IMMDeviceEnumerator = "{A95664D2-9614-4F35-A746-DE8DB63617E6}"
    IID_IAudioClient = "{1CB9AD4C-DBFA-4C32-B178-C2F568A703B2}"
    IID_IAudioCaptureClient = "{C8ADBD64-E71E-48A0-A4DE-185C395CD317}"
    PKEY_Device_FriendlyName_FMTID = "{A45C254E-DF1C-4EFD-8020-67D146A850E0}"
    PKEY_Device_FriendlyName_PID = 14
    #: KSDATAFORMAT_SUBTYPE_IEEE_FLOAT's Data1; PCM's is 1. The other GUID
    #: fields are the shared `-0000-0010-8000-00aa00389b71` tail.
    _SUBTYPE_FLOAT, _SUBTYPE_PCM = 3, 1

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
        _fields_ = (("vt", wintypes.USHORT), ("r1", wintypes.WORD),
                    ("r2", wintypes.WORD), ("r3", wintypes.WORD),
                    ("pwszVal", ctypes.c_void_p), ("pad", ctypes.c_void_p))

    class _WAVEFORMATEX(ctypes.Structure):
        _pack_ = 1
        _fields_ = (("wFormatTag", wintypes.WORD), ("nChannels", wintypes.WORD),
                    ("nSamplesPerSec", wintypes.DWORD),
                    ("nAvgBytesPerSec", wintypes.DWORD),
                    ("nBlockAlign", wintypes.WORD),
                    ("wBitsPerSample", wintypes.WORD), ("cbSize", wintypes.WORD))

    class _WAVEFORMATEXTENSIBLE(ctypes.Structure):
        _pack_ = 1
        _fields_ = (("Format", _WAVEFORMATEX), ("wValidBitsPerSample", wintypes.WORD),
                    ("dwChannelMask", wintypes.DWORD), ("SubFormat", _GUID))

    _ole32.PropVariantClear.argtypes = (ctypes.POINTER(_PROPVARIANT),)
    _ole32.PropVariantClear.restype = ctypes.c_long

    def _method(ptr, index: int, *argtypes):
        """Bound COM method `index` of the interface behind `ptr` (HRESULT
        restype: a failure code raises OSError, a success code like
        AUDCLNT_S_BUFFER_EMPTY does not)."""
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

    class _Com:
        """CoInitializeEx(MTA) for the life of a thread -- when we own it."""

        def __enter__(self):
            hr = _ole32.CoInitializeEx(None, _COINIT_MULTITHREADED)
            self._owned = hr in (_S_OK, _S_FALSE)
            if hr < 0 and hr != _RPC_E_CHANGED_MODE:
                raise OSError(f"CoInitializeEx -> {hr & 0xFFFFFFFF:#x}")
            return self

        def __exit__(self, *exc):
            if self._owned:
                _ole32.CoUninitialize()
            return False

    def _active_render_devices(enum) -> list:
        """[(Endpoint, IMMDevice ptr)] -- the caller releases every pointer."""
        out = []
        coll = ctypes.c_void_p()
        _method(enum, 3, ctypes.c_int, wintypes.DWORD,
                ctypes.POINTER(ctypes.c_void_p))(
            enum, _E_RENDER, _DEVICE_STATE_ACTIVE, ctypes.byref(coll))
        try:
            n = wintypes.UINT()
            _method(coll, 3, ctypes.POINTER(wintypes.UINT))(coll, ctypes.byref(n))
            for i in range(int(n.value)):
                dev = ctypes.c_void_p()
                try:
                    _method(coll, 4, wintypes.UINT,
                            ctypes.POINTER(ctypes.c_void_p))(coll, i, ctypes.byref(dev))
                    out.append((Endpoint(_device_id(dev), _friendly_name(dev)), dev))
                except OSError:
                    log.debug("render endpoint %d unreadable", i, exc_info=True)
                    _release(dev)
        finally:
            _release(coll)
        return out

    _cfg = ctypes.windll.cfgmgr32
    _CR_SUCCESS = 0
    #: DEVPKEY_Device_Address = {a45c254e-df1c-4efd-8020-67d146a850e0}, 30
    _DEVPKEY_ADDRESS = ("{A45C254E-DF1C-4EFD-8020-67D146A850E0}", 30)

    class _DEVPROPKEY(ctypes.Structure):
        _fields_ = (("fmtid", _GUID), ("pid", wintypes.ULONG))

    def _devnode_id(dn) -> str:
        buf = ctypes.create_unicode_buffer(512)
        if _cfg.CM_Get_Device_IDW(dn, buf, 512, 0):
            return ""
        return buf.value

    def endpoint_usb_address(endpoint_id: str):
        """The usbip port of the virtual pad behind a render endpoint, or None.

        endpoint `SWD\\MMDEVAPI\\<id>` -> parent (the audio function) -> its
        parent (the composite USB device), which must be a Sony device on a
        root hub whose host controller is ROOT-enumerated -- usbip-win2's
        emulated host controller (`ROOT\\USB\\0000`), not a PCI one. Its
        DEVPKEY_Device_Address is then the vhci port `usbip port` lists, which
        is how a bridge recognises its OWN speaker endpoint. A Sony pad wired
        to a real USB port returns None, whatever its port number: its
        speaker is a speaker in the room like any other. Never raises."""
        try:
            dn = wintypes.DWORD()
            if _cfg.CM_Locate_DevNodeW(ctypes.byref(dn), "SWD\\MMDEVAPI\\" + endpoint_id, 0):
                return None
            for _ in range(2):
                parent = wintypes.DWORD()
                if _cfg.CM_Get_Parent(ctypes.byref(parent), dn, 0):
                    return None
                dn = parent
            dev = dn
            if not _devnode_id(dev).upper().startswith("USB\\VID_054C&"):
                return None
            # up to the root hub, then its host controller
            node = dev
            for _ in range(8):
                parent = wintypes.DWORD()
                if _cfg.CM_Get_Parent(ctypes.byref(parent), node, 0):
                    return None
                node = parent
                if _devnode_id(node).upper().startswith("USB\\ROOT_HUB"):
                    break
            else:
                return None
            hc = wintypes.DWORD()
            if (_cfg.CM_Get_Parent(ctypes.byref(hc), node, 0)
                    or not _devnode_id(hc).upper().startswith("ROOT\\")):
                return None
            key = _DEVPROPKEY(_GUID.of(_DEVPKEY_ADDRESS[0]), _DEVPKEY_ADDRESS[1])
            ptype = wintypes.ULONG(); val = wintypes.ULONG(); size = wintypes.ULONG(4)
            if _cfg.CM_Get_DevNode_PropertyW(dev, ctypes.byref(key), ctypes.byref(ptype),
                                             ctypes.byref(val), ctypes.byref(size), 0):
                return None
            return int(val.value)
        except Exception:  # noqa: BLE001
            return None

    class _Stream:
        """One endpoint opened for loopback capture."""

        def __init__(self, endpoint: Endpoint, dev):
            self.endpoint = endpoint
            self.client = ctypes.c_void_p()
            self.capture = ctypes.c_void_p()
            _method(dev, 3, ctypes.POINTER(_GUID), wintypes.DWORD, ctypes.c_void_p,
                    ctypes.POINTER(ctypes.c_void_p))(
                dev, ctypes.byref(_GUID.of(IID_IAudioClient)), _CLSCTX_ALL, None,
                ctypes.byref(self.client))
            try:
                fmt_ptr = ctypes.c_void_p()
                _method(self.client, 8, ctypes.POINTER(ctypes.c_void_p))(
                    self.client, ctypes.byref(fmt_ptr))
                try:
                    self._parse_format(fmt_ptr)
                    _method(self.client, 3, ctypes.c_int, wintypes.DWORD,
                            ctypes.c_longlong, ctypes.c_longlong, ctypes.c_void_p,
                            ctypes.c_void_p)(
                        self.client, _AUDCLNT_SHAREMODE_SHARED,
                        _AUDCLNT_STREAMFLAGS_LOOPBACK, BUFFER_HNS, 0, fmt_ptr, None)
                finally:
                    _ole32.CoTaskMemFree(fmt_ptr)
                _method(self.client, 14, ctypes.POINTER(_GUID),
                        ctypes.POINTER(ctypes.c_void_p))(
                    self.client, ctypes.byref(_GUID.of(IID_IAudioCaptureClient)),
                    ctypes.byref(self.capture))
                _method(self.client, 10)(self.client)            # Start
            except Exception:
                self.close()
                raise
            self.nref = speaker_channels(np.zeros((1, self.channels), np.float32),
                                         endpoint.name, self.mask).shape[1]
            self.resampler = Resampler(self.nref, self.rate)
            self.packets = 0
            self.discontinuities = 0
            self._get_buffer = _method(
                self.capture, 3, ctypes.POINTER(ctypes.c_void_p),
                ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(wintypes.DWORD),
                ctypes.POINTER(ctypes.c_uint64), ctypes.POINTER(ctypes.c_uint64))
            self._release_buffer = _method(self.capture, 4, ctypes.c_uint32)
            self._next_size = _method(self.capture, 5, ctypes.POINTER(ctypes.c_uint32))

        def _parse_format(self, fmt_ptr) -> None:
            wf = ctypes.cast(fmt_ptr, ctypes.POINTER(_WAVEFORMATEX)).contents
            tag = int(wf.wFormatTag)
            ext = None
            if tag == _WAVE_FORMAT_EXTENSIBLE and wf.cbSize >= 22:
                ext = ctypes.cast(fmt_ptr, ctypes.POINTER(_WAVEFORMATEXTENSIBLE)).contents
                tag = {_SUBTYPE_FLOAT: _WAVE_FORMAT_IEEE_FLOAT,
                       _SUBTYPE_PCM: _WAVE_FORMAT_PCM}.get(int(ext.SubFormat.Data1), 0)
            bits = int(wf.wBitsPerSample)
            if tag == _WAVE_FORMAT_IEEE_FLOAT and bits == 32:
                self.dtype, self.scale = np.float32, 1.0
            elif tag == _WAVE_FORMAT_PCM and bits == 16:
                self.dtype, self.scale = np.int16, 1.0 / 32768.0
            elif tag == _WAVE_FORMAT_PCM and bits == 32:
                self.dtype, self.scale = np.int32, 1.0 / 2147483648.0
            else:
                raise OSError(f"unsupported mix format tag={tag} bits={bits}")
            self.channels = int(wf.nChannels)
            self.mask = int(ext.dwChannelMask) if ext is not None else 0
            self.rate = int(wf.nSamplesPerSec)
            self.block_align = int(wf.nBlockAlign)

        def drain(self, sink) -> None:
            """Every packet waiting, to `sink(endpoint, ref48, t_start)`; ref48 is
            (n, 1) or (n, 2) float32 at 48 kHz."""
            size = ctypes.c_uint32()
            data = ctypes.c_void_p()
            frames = ctypes.c_uint32()
            flags = wintypes.DWORD()
            devpos = ctypes.c_uint64()
            qpc = ctypes.c_uint64()
            while True:
                self._next_size(self.capture, ctypes.byref(size))
                if not size.value:
                    return
                self._get_buffer(self.capture, ctypes.byref(data), ctypes.byref(frames),
                                 ctypes.byref(flags), ctypes.byref(devpos),
                                 ctypes.byref(qpc))
                n = int(frames.value)
                try:
                    if flags.value & _AUDCLNT_BUFFERFLAGS_SILENT or not data.value:
                        ref = np.zeros((n, self.nref), np.float32)
                    else:
                        raw = ctypes.string_at(data.value, n * self.block_align)
                        pcm = np.frombuffer(raw, dtype=self.dtype).reshape(n, self.channels)
                        if self.dtype is not np.float32:
                            pcm = pcm.astype(np.float32) * self.scale
                        ref = speaker_channels(pcm, self.endpoint.name, self.mask)
                finally:
                    self._release_buffer(self.capture, n)
                if flags.value & _AUDCLNT_BUFFERFLAGS_DATA_DISCONTINUITY:
                    self.discontinuities += 1
                self.packets += 1
                out = self.resampler.push(ref)
                if out.size:
                    sink(self.endpoint, out, self._t_start(qpc.value, flags.value, n))

        def _t_start(self, qpc: int, flags: int, n: int) -> float:
            """The packet's first-frame time on the perf_counter timeline.

            MEASURED 2026-10-04 on this machine (Realtek endpoint, 48 kHz):
            packets arrive 15-25 ms BEFORE `qpc + duration`, i.e. the loopback
            timestamp is the frame's presentation time, ~12 ms ahead of
            delivery. That is the better anchor for an echo reference, so it
            is used as is; only a flagged or absurd timestamp falls back to
            the arrival time.
            """
            now = time.perf_counter()
            t = qpc * 1e-7
            if (not qpc or flags & _AUDCLNT_BUFFERFLAGS_TIMESTAMP_ERROR
                    or abs(t - now) > 1.0):
                return now - n / self.rate
            return t

        def close(self) -> None:
            if self.client and self.client.value:
                try:
                    _method(self.client, 11)(self.client)        # Stop
                except Exception:  # noqa: BLE001
                    pass
            _release(self.capture)
            _release(self.client)
            self.capture = ctypes.c_void_p()
            self.client = ctypes.c_void_p()

else:  # pragma: no cover - the bridge targets Windows; keep imports safe elsewhere
    def endpoint_usb_address(endpoint_id: str):
        return None


class LoopbackCapture:
    """Captures every active render endpoint until `stop()`.

    `sink(endpoint, ref48, t_start)` is called on the capture thread with
    (n, 1) or (n, 2) float32 at 48 kHz and the perf_counter time of its first
    sample. It
    must be quick and must not raise (anything it raises is logged and the
    packet dropped). `exclude(endpoint) -> bool` is asked at every rescan for
    every endpoint not open yet; True keeps it closed.
    """

    def __init__(self, sink, *, rescan_s: float = RESCAN_S, poll_s: float = POLL_S,
                 exclude=None):
        self.sink = sink
        self.rescan_s = rescan_s
        self.poll_s = poll_s
        self.exclude = exclude
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        #: endpoint id -> (name, rate, channels) of what is open right now.
        #: Replaced wholesale by the capture thread, so readers never see a
        #: dict mid-mutation.
        self.open_endpoints: dict = {}
        self.stats = {"opens": 0, "open_failures": 0, "closes": 0,
                      "packets": 0, "sink_errors": 0, "discontinuities": 0}
        self.failed: str | None = None

    def start(self) -> None:
        if self._thread is not None or sys.platform != "win32":
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="ds5-aec-loopback",
                                        daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 1.0) -> None:
        self._stop.set()
        t = self._thread
        if t is not None:
            t.join(timeout=timeout)
        self._thread = None

    def _sink(self, endpoint, ref, t_start) -> None:
        self.stats["packets"] += 1
        try:
            self.sink(endpoint, ref, t_start)
        except Exception:  # noqa: BLE001
            self.stats["sink_errors"] += 1
            log.debug("loopback sink failed", exc_info=True)

    def _run(self) -> None:  # pragma: no cover - needs real WASAPI
        backoff = RETRY_S[0]
        while not self._stop.is_set():
            if self._run_once():
                return
            self.stats["restarts"] = self.stats.get("restarts", 0) + 1
            if self._stop.wait(backoff):
                return
            backoff = min(RETRY_S[1], backoff * 2)

    def _run_once(self) -> bool:  # pragma: no cover - needs real WASAPI
        """One life of the capture loop. True: stopped on request."""
        streams: dict[str, _Stream] = {}
        refused: dict = {}
        try:
            with _Com():
                enum = _create(CLSID_MMDeviceEnumerator, IID_IMMDeviceEnumerator)
                try:
                    next_scan = 0.0
                    while not self._stop.is_set():
                        now = time.monotonic()
                        if now >= next_scan:
                            next_scan = now + self.rescan_s
                            self._rescan(enum, streams, refused)
                        for sid, st in list(streams.items()):
                            try:
                                st.drain(self._sink)
                            except Exception as e:  # noqa: BLE001
                                # AUDCLNT_E_DEVICE_INVALIDATED and friends: the
                                # endpoint went away or changed format. Close
                                # it; the next rescan reopens it if it is back.
                                log.info("loopback on %r ended: %s",
                                         st.endpoint.name, e)
                                self._close(streams, sid)
                        self._stop.wait(self.poll_s)
                finally:
                    for sid in list(streams):
                        self._close(streams, sid)
                    _release(enum)
            return True
        except Exception as e:  # noqa: BLE001
            self.failed = repr(e)
            log.warning("loopback capture failed (%s); restarting it after a "
                        "backoff -- the microphone is unaffected", e, exc_info=True)
            return False

    def _rescan(self, enum, streams: dict, refused: dict) -> None:  # pragma: no cover
        try:
            pairs = _active_render_devices(enum)
        except OSError:
            log.debug("render endpoint rescan failed", exc_info=True)
            return
        present = set()
        try:
            for ep, dev in pairs:
                present.add(ep.id)
                if ep.id in streams:
                    continue
                retry_at, backoff = refused.get(ep.id, (0.0, RETRY_S[0]))
                if time.monotonic() < retry_at:
                    continue
                if self.exclude is not None and self.exclude(ep):
                    continue
                try:
                    streams[ep.id] = _Stream(ep, dev)
                    self.stats["opens"] += 1
                    log.info("loopback: listening to %r (%d ch @ %d Hz)", ep.name,
                             streams[ep.id].channels, streams[ep.id].rate)
                except Exception as e:  # noqa: BLE001
                    # A format we do not read, or an endpoint that refuses
                    # loopback right now: retry with a growing backoff.
                    self.stats["open_failures"] += 1
                    refused[ep.id] = (time.monotonic() + backoff,
                                      min(RETRY_S[1], backoff * 2))
                    log.info("loopback: cannot capture %r: %s (retry in %.0f s)",
                             ep.name, e, backoff)
        finally:
            for _ep, dev in pairs:
                _release(dev)
        for sid in [s for s in streams if s not in present]:
            self._close(streams, sid)
        for sid in [s for s in refused if s not in present]:
            del refused[sid]
        self.open_endpoints = {sid: (st.endpoint.name, st.rate, st.channels)
                               for sid, st in streams.items()}

    def _close(self, streams: dict, sid: str) -> None:  # pragma: no cover
        st = streams.pop(sid, None)
        if st is None:
            return
        self.stats["discontinuities"] += st.discontinuities
        self.stats["closes"] += 1
        st.close()
        self.open_endpoints = {k: v for k, v in self.open_endpoints.items() if k != sid}
