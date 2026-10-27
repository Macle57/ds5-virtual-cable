"""Open the pad's speaker endpoint once, right after it appears, so the first
game to open it finds it warm.

Why this exists
---------------
Games built on Sony's own pad library (libScePad; Genshin Impact is the case
that was reported) drive the DualSense's HD haptics through its USB audio
render endpoint -- the four-channel 48 kHz "Speakers (DualSense Wireless
Controller)" entry -- and they open it ONCE, while initialising the
controller. Against a freshly bridged pad that open is the endpoint's very
first: usbaudio resumes the device, sends SET_INTERFACE alt 1 and the sample
rate, and starts the isochronous stream, every step a round trip over the
usbip loopback. The library does not wait for that or try again, so the game
runs with no haptics at all.

The workaround a user found by hand: open the Sound control panel, right-click
the DualSense speaker, Test -- even cancelling before anything plays -- and
THEN start the game. Haptics work. Whatever has to happen on a cold endpoint
has happened, and the game's own open is fast.

This module is that Test click, done by the app. When a bridge comes up it
waits for the new speaker endpoint to turn ACTIVE (Windows builds endpoints
asynchronously, a second or two after the USB device appears), opens it in
WASAPI shared mode, plays a few hundred milliseconds of silence, stops and
releases it. The bridge sees SET_INTERFACE alt 1, then alt 0 when the audio
engine closes the pin, and nothing reaches the controller but silence.

How
---
`WasapiPrimer` is the only part that talks to Windows: `IMMDeviceEnumerator::
GetDevice`, `IMMDevice::Activate(IAudioClient)`, `GetMixFormat`, `Initialize`,
one silent `IAudioRenderClient` buffer, `Start`, `Stop`. Raw ctypes vtables
over `audio_default`'s helpers -- no comtypes, no pywin32; the app layer is
stdlib-only by rule. It never raises: a failure comes back as a
`PrimeOutcome` carrying the HRESULT.

`AutoPrimer` holds the decisions -- which endpoint, when it is ready, whether
a failure is worth another attempt -- in plain Python over a two-method seam
(`render_endpoints()`, `prime(id, hold_s)`), which is what the unit tests fake.
`schedule()` is the manager's hook: it returns at once and does the waiting on
its own short-lived thread, so neither the hotplug thread nor anything on the
input path ever waits for the audio stack.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from dataclasses import dataclass

from . import audio_default as AD

log = logging.getLogger("ds5app.audio_prime")

#: How long to wait for the new speaker endpoint to turn ACTIVE after the
#: bridge reports the pad attached. Measured on the dev machine the endpoint
#: shows up within a couple of seconds of attach; ten covers a slow boot.
WAIT_S = 10.0
#: How often the endpoint list is read while waiting.
POLL_S = 0.5
#: Between seeing the endpoint ACTIVE and opening it. The endpoint builder
#: marks it active before the audio service has finished with it, and an open
#: in that instant can fail for reasons that have nothing to do with the pad.
SETTLE_S = 1.0
#: How long the silent stream runs. Long enough that the engine has really
#: started the pin (the Sound panel's Test click works even when cancelled
#: early); short enough to be invisible.
HOLD_S = 0.4
#: Opens tried per endpoint, and the pause between them.
ATTEMPTS = 3
RETRY_GAP_S = 1.5

#: Shared-mode buffer asked of `IAudioClient::Initialize`, in 100 ns units.
BUFFER_HNS = 1_000_000          # 100 ms

#: HRESULTs worth naming (audioclient.h, winerror.h), as unsigned 32-bit.
AUDCLNT_E_DEVICE_INVALIDATED = 0x88890004
AUDCLNT_E_DEVICE_IN_USE = 0x8889000A
AUDCLNT_E_ENDPOINT_CREATE_FAILED = 0x8889000F
AUDCLNT_E_SERVICE_NOT_RUNNING = 0x88890010
E_NOTFOUND = 0x80070490

_HR_NAMES = {
    AUDCLNT_E_DEVICE_INVALIDATED: "AUDCLNT_E_DEVICE_INVALIDATED",
    AUDCLNT_E_DEVICE_IN_USE: "AUDCLNT_E_DEVICE_IN_USE",
    AUDCLNT_E_ENDPOINT_CREATE_FAILED: "AUDCLNT_E_ENDPOINT_CREATE_FAILED",
    AUDCLNT_E_SERVICE_NOT_RUNNING: "AUDCLNT_E_SERVICE_NOT_RUNNING",
    E_NOTFOUND: "E_NOTFOUND",
}


def hr_text(hr) -> str:
    """'0x8889000a AUDCLNT_E_DEVICE_IN_USE', or '' for no HRESULT."""
    if hr is None:
        return ""
    hr &= 0xFFFFFFFF
    name = _HR_NAMES.get(hr)
    return f"{hr:#010x}" + (f" {name}" if name else "")


@dataclass(frozen=True)
class PrimeOutcome:
    """What one open of the endpoint came to."""

    ok: bool
    hresult: int | None = None
    detail: str = ""
    #: Initialize + Start, the part a cold endpoint makes slow.
    open_ms: float = 0.0


def pad_speakers(endpoints, needle: str = AD.PAD_NAME_NEEDLE) -> list:
    """The ACTIVE render endpoints that belong to a DualSense, in a stable
    order. Windows keeps an entry for every pad ever plugged in; only an
    active one can be opened, so nothing else counts."""
    n = (needle or "").lower()
    if not n:
        return []
    pads = [e for e in endpoints
            if e.active and n in (e.name or "").lower()]
    pads.sort(key=lambda e: ((e.name or "").lower(), e.id))
    return pads


def worth_retrying(outcome: PrimeOutcome) -> bool:
    """Should a failed open be tried again?

    AUDCLNT_E_DEVICE_IN_USE means another program holds the endpoint in
    exclusive mode: it is open, which is all priming wanted, so stop. Every
    other failure -- an endpoint invalidated or not found because Windows was
    still rebuilding it, the audio service busy, a timeout -- is the kind a
    second look a moment later can get past.
    """
    if outcome.ok:
        return False
    hr = outcome.hresult
    return hr is None or (hr & 0xFFFFFFFF) != AUDCLNT_E_DEVICE_IN_USE


class AutoPrimer:
    """Prime the pad's speaker each time a bridge comes up.

    `audio` is anything with `render_endpoints()` (a list of
    `audio_default.Endpoint`) and `prime(device_id, hold_s)` (a
    `PrimeOutcome`); `enabled` is read on every `schedule()`, so turning the
    setting off in the dashboard takes effect at the next reconnect without
    rebuilding anything.
    """

    def __init__(self, audio=None, enabled=None, wait_s: float = WAIT_S,
                 poll_s: float = POLL_S, settle_s: float = SETTLE_S,
                 hold_s: float = HOLD_S, attempts: int = ATTEMPTS,
                 retry_gap_s: float = RETRY_GAP_S, sleep=None, clock=None,
                 start_thread=None):
        self.audio = audio if audio is not None else WasapiPrimer()
        self._enabled = enabled or (lambda: True)
        self.wait_s = wait_s
        self.poll_s = poll_s
        self.settle_s = settle_s
        self.hold_s = hold_s
        self.attempts = max(1, int(attempts))
        self.retry_gap_s = retry_gap_s
        self.sleep = sleep or time.sleep
        self.clock = clock or time.monotonic
        self._start_thread = start_thread or self._thread
        #: Two pads bridged in the same second each get a run; their opens
        #: take turns rather than racing each other through the audio service.
        self._lock = threading.Lock()

    # -- the hook ----------------------------------------------------------

    def schedule(self, serial: str = "") -> bool:
        """The manager's `on_bridged` hook. Returns at once; never raises.

        -> whether a run was started (False when the setting is off).
        """
        try:
            if not self._enabled():
                log.debug("speaker auto-prime is off; not priming for %s",
                          serial or "the new pad")
                return False
            self._start_thread(lambda: self._run_safely(serial))
            return True
        except Exception:  # noqa: BLE001
            log.exception("could not schedule the speaker auto-prime")
            return False

    @staticmethod
    def _thread(fn) -> None:
        threading.Thread(target=fn, name="ds5-audio-prime", daemon=True).start()

    def _run_safely(self, serial: str) -> None:
        try:
            self.run(serial)
        except Exception:  # noqa: BLE001
            log.exception("speaker auto-prime failed")

    # -- the decisions -----------------------------------------------------

    def _speakers(self) -> list:
        try:
            return pad_speakers(self.audio.render_endpoints())
        except Exception:  # noqa: BLE001
            log.debug("listing render endpoints failed", exc_info=True)
            return []

    def run(self, serial: str = "") -> list:
        """Wait for the new pad's speaker, then prime it. Synchronous.

        -> the ids of the endpoints that were opened successfully.

        "The new pad's speaker" is an active DualSense endpoint that was NOT
        active when this run began: the bridge has just attached a device,
        so its endpoint is the one that turns up. With a second pad already
        bridged, that keeps this run from settling for the other pad's
        endpoint. If nothing new turns up in `wait_s` -- the endpoint was
        quicker than the bridge's "attached" report, or Windows reused it --
        every active DualSense speaker is primed instead; opening a warm one
        again costs a few hundred milliseconds of silence.
        """
        who = serial or "the new pad"
        before = {e.id for e in self._speakers()}
        deadline = self.clock() + self.wait_s
        while True:
            now = self._speakers()
            new = [e for e in now if e.id not in before]
            if new:
                targets = new
                break
            if self.clock() >= deadline:
                targets = now
                break
            self.sleep(self.poll_s)
        if not targets:
            log.info("no DualSense speaker became active within %gs of "
                     "bridging %s -- nothing to prime", self.wait_s, who)
            return []
        if new and self.settle_s > 0:
            self.sleep(self.settle_s)
        primed = []
        with self._lock:
            for ep in targets:
                if self._prime_one(ep, who):
                    primed.append(ep.id)
        return primed

    def _prime_one(self, ep, who: str) -> bool:
        for attempt in range(1, self.attempts + 1):
            try:
                out = self.audio.prime(ep.id, self.hold_s)
            except Exception as e:  # noqa: BLE001
                out = PrimeOutcome(False, None, repr(e))
            if out.ok:
                log.info("primed the speaker endpoint %r for %s (open took "
                         "%.0f ms%s)", ep.name, who, out.open_ms,
                         f", attempt {attempt}" if attempt > 1 else "")
                return True
            if not worth_retrying(out):
                log.info("speaker endpoint %r is held in exclusive mode by "
                         "another program -- already open, nothing to prime",
                         ep.name)
                return False
            if attempt < self.attempts:
                log.debug("priming %r failed (%s %s), retrying", ep.name,
                          hr_text(out.hresult), out.detail)
                self.sleep(self.retry_gap_s)
            else:
                log.warning("could not prime the speaker endpoint %r for %s "
                            "after %d attempts: %s %s", ep.name, who,
                            self.attempts, hr_text(out.hresult), out.detail)
        return False


# ---------------------------------------------------------------------------
# the real thing: WASAPI through ctypes
# ---------------------------------------------------------------------------

IID_IAudioClient = "{1CB9AD4C-DBFA-4C32-B178-C2F568A703B2}"
IID_IAudioRenderClient = "{F294ACFC-3146-4483-A7BF-ADDCA7C260E2}"

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _AUDCLNT_SHAREMODE_SHARED = 0
    _AUDCLNT_BUFFERFLAGS_SILENT = 0x2

    class _WAVEFORMATEX(ctypes.Structure):
        _fields_ = (("wFormatTag", wintypes.WORD), ("nChannels", wintypes.WORD),
                    ("nSamplesPerSec", wintypes.DWORD),
                    ("nAvgBytesPerSec", wintypes.DWORD),
                    ("nBlockAlign", wintypes.WORD),
                    ("wBitsPerSample", wintypes.WORD), ("cbSize", wintypes.WORD))

    def _hresult_of(e: BaseException):
        hr = getattr(e, "winerror", None)
        return None if hr is None else hr & 0xFFFFFFFF

    class WasapiPrimer:
        """`render_endpoints()` and `prime()`, for real. Fail soft."""

        def __init__(self, system=None):
            self._system = system or AD.AudioSystem()

        def render_endpoints(self) -> list:
            return self._system.render_endpoints()

        def prime(self, device_id: str, hold_s: float = HOLD_S) -> PrimeOutcome:
            """Open `device_id` shared, run silence for `hold_s`, close it.

            IMMDeviceEnumerator: 5 GetDevice. IMMDevice: 3 Activate.
            IAudioClient: 3 Initialize, 4 GetBufferSize, 8 GetMixFormat,
            10 Start, 11 Stop, 14 GetService. IAudioRenderClient: 3 GetBuffer,
            4 ReleaseBuffer. (Slots 0-2 are IUnknown's everywhere.)
            """
            m, rel = AD._method, AD._release
            enum = dev = client = render = None
            fmt = ctypes.c_void_p()
            started = False
            open_ms = 0.0
            try:
                with AD._Com():
                    try:
                        enum = AD._create(AD.CLSID_MMDeviceEnumerator,
                                          AD.IID_IMMDeviceEnumerator)
                        dev = ctypes.c_void_p()
                        m(enum, 5, ctypes.c_wchar_p,
                          ctypes.POINTER(ctypes.c_void_p))(
                            enum, device_id, ctypes.byref(dev))
                        client = ctypes.c_void_p()
                        iid = AD._GUID.of(IID_IAudioClient)
                        m(dev, 3, ctypes.POINTER(AD._GUID), wintypes.DWORD,
                          ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))(
                            dev, ctypes.byref(iid), AD._CLSCTX_ALL, None,
                            ctypes.byref(client))
                        m(client, 8, ctypes.POINTER(ctypes.c_void_p))(
                            client, ctypes.byref(fmt))
                        wf = ctypes.cast(fmt, ctypes.POINTER(_WAVEFORMATEX))[0]
                        shape = f"{wf.nChannels} ch {wf.nSamplesPerSec} Hz"
                        t0 = time.perf_counter()
                        m(client, 3, ctypes.c_int, wintypes.DWORD,
                          ctypes.c_longlong, ctypes.c_longlong, ctypes.c_void_p,
                          ctypes.c_void_p)(
                            client, _AUDCLNT_SHAREMODE_SHARED, 0, BUFFER_HNS,
                            0, fmt, None)
                        frames = ctypes.c_uint32()
                        m(client, 4, ctypes.POINTER(ctypes.c_uint32))(
                            client, ctypes.byref(frames))
                        render = ctypes.c_void_p()
                        riid = AD._GUID.of(IID_IAudioRenderClient)
                        m(client, 14, ctypes.POINTER(AD._GUID),
                          ctypes.POINTER(ctypes.c_void_p))(
                            client, ctypes.byref(riid), ctypes.byref(render))
                        # One full buffer, flagged silent: the engine renders
                        # zeros without our writing a byte of it.
                        buf = ctypes.c_void_p()
                        m(render, 3, ctypes.c_uint32,
                          ctypes.POINTER(ctypes.c_void_p))(
                            render, frames.value, ctypes.byref(buf))
                        m(render, 4, ctypes.c_uint32, wintypes.DWORD)(
                            render, frames.value, _AUDCLNT_BUFFERFLAGS_SILENT)
                        m(client, 10)(client)
                        started = True
                        open_ms = (time.perf_counter() - t0) * 1000.0
                        time.sleep(max(0.0, hold_s))
                        return PrimeOutcome(True, None, shape, open_ms)
                    finally:
                        if started:
                            try:
                                m(client, 11)(client)
                            except OSError:
                                pass
                        if fmt.value:
                            AD._ole32.CoTaskMemFree(fmt)
                        rel(render)
                        rel(client)
                        rel(dev)
                        rel(enum)
            except OSError as e:
                return PrimeOutcome(False, _hresult_of(e), str(e), open_ms)
            except Exception as e:  # noqa: BLE001
                return PrimeOutcome(False, None, repr(e), open_ms)

else:  # pragma: no cover - the app targets Windows; keep imports safe elsewhere
    class WasapiPrimer:
        """No WASAPI anywhere but Windows."""

        def __init__(self, system=None):
            pass

        def render_endpoints(self) -> list:
            return []

        def prime(self, device_id: str, hold_s: float = HOLD_S) -> PrimeOutcome:
            return PrimeOutcome(False, None, "not Windows")
