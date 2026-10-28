"""Echo cancellation for the PC's OTHER speakers in the pad's microphone.

The pad's own speaker is cancelled by the pad itself (audioControl bit 2,
`ds5bridge.protocol.audio_control`). Everything else this PC plays -- an HDMI
TV, desktop speakers, a Bluetooth speaker, another pad -- reaches the mic
through the room, and this module removes it, using as references what
Windows is actually rendering to each endpoint (WASAPI loopback,
`loopback.py`). Sound that is not on any reference (a person in the room,
music from a device that is not this PC) is kept.

The signal path (all in `ds5aec.dll`, built from `native/`, one ctypes call
per 10 ms frame, so the GIL is released for the whole of it):

  1. LINEAR: one speexdsp MDF adaptive filter PER ENDPOINT (128 ms tail,
     its own double-talk step control), over the endpoint's channels -- a
     stereo endpoint is modelled as two speakers, fed as mid and side --
     run in a chain while the endpoint plays. A filter belongs to its
     endpoint: a silent endpoint costs nothing, keeps what it learned across
     capture sessions, and another endpoint coming or going never touches
     it. It only subtracts.
  2. RESIDUAL: a spectral suppressor keyed on the chain's echo estimate,
     never deeper than -3 dB while anything the references do not explain
     is present and -6 dB while only PC audio is: the linear stages do the
     cancelling; a deeper floor bought 0.2-0.5 dB and dipped a quiet person
     in the room by up to 23 dB when the near-end detector missed them.

MEASURED (scratchpad/aec rounds 4-5, cameo, HDMI speaker): a stereo
speaker playing different content left and right is cancelled by 23 dB with
two channels where one mono mix of it managed 2.8 dB; mono speech 17-21 dB,
music 13-17, noise 21-23 once engaged. See the round-5 results for the live
numbers of this code.

What makes it safe to leave on:

  * nothing happens until a reference is proven to reach this mic: an
    endpoint gets a filter only after two non-overlapping 1 s probes find
    its echo at the same delay with a peak above anything unrelated audio
    produced in calibration, or one probe at twice that -- or, after a
    restart, on the delay it had last time, provisionally: unless a probe or
    the filter confirms it within `PROVISIONAL_S` it is forgotten.
    Headphones never qualify, so they never change the mic;
  * with no slotted reference playing, the output is the input delayed by one
    frame, bit for bit (the residual stage's STFT costs exactly one frame of
    latency, and the idle path keeps that latency so it never changes);
  * a slotted endpoint whose echo is no longer found (the speaker was
    unplugged, the user put headphones on) loses its slot again, decided
    per endpoint;
  * the microphone timeline is exact: the pad numbers its mic packets (BT
    payload byte 1) and every lost packet is replaced by 10 ms of silence,
    so a loss never shifts the echo against its reference;
  * the producers never wait: `push_mic` (BT reader thread) refuses when the
    worker is behind by more than `QUEUE_MAX_MS` and the bridge then writes
    the frame directly -- a stall costs the cancellation, never the mic, and
    no stale canceller frame is written after a direct one;
  * any failure (no DLL, a raising call, a dead worker) falls back to the
    unprocessed mic, and `status()["health"]` says so (the dashboard shows it).

Reference timing: loopback packets carry QPC presentation stamps and are
laid on a 48 kHz `time.perf_counter()` timeline (`RefTimeline`), following
the stamps with a fractional offset. Microphone samples are mapped onto the
same timeline by their least late arrival (`MicClock`), anchored briefly at
each session start and then held, and references are read at
fractional positions (cubic interpolation). Each endpoint's bulk delay is
measured by cross-correlation (GCC-PHAT, `_probe`) and re-checked every
`TRACK_EVERY_S`; its echo is also re-located to a fraction of a sample from
its arrival pattern (`_fine`), which keeps a kept filter aligned across
sessions -- the mic anchor itself moves by 5-15 samples between sessions.
"""

from __future__ import annotations

import ctypes
import json
import logging
import math
import os
import threading
import time
from collections import deque

import numpy as np

log = logging.getLogger("ds5emu.aec")

FS = 48000
FRAME = 480                     # 10 ms: one pad mic packet, one native call
TAIL_MS = 128
#: Endpoints that can be cancelled at once, and the reference channels they
#: may have between them (a stereo endpoint is two): the CPU budget, about
#: 0.1-0.15 ms per channel per 10 ms frame.
MAX_REFS = 3
MAX_CHANNELS = 4
#: Read each reference this far behind the mic's mapped time, so its data has
#: arrived (loopback packets arrive ~12 ms BEFORE their presentation stamp).
REF_BASE_MS = 10
#: Place each reference's echo this far into the filter, so the bulk delay
#: estimate's error never makes the echo acausal.
EARLY_MS = 8
#: Worker backlog beyond which `push_mic` refuses (the bridge writes directly).
QUEUE_MAX_MS = 150
#: Lost mic packets up to this many are filled with silence; more is a gap.
SEQ_MAX_FILL = 25
#: A reference frame quieter than this (-60 dBFS RMS) is silence.
LOUD_RMS = 1e-3
#: A microphone window quieter than this (-90 dBFS RMS, about one LSB of the
#: pad's 16-bit mic) carries nothing to look for an echo in: the probe and
#: the fine alignment skip it. It is NOT the reference's -60 dBFS: the echo of
#: a PC speaker in the pad's mic is routinely far quieter than the audio
#: Windows sends that speaker. The loopback reference is taken BEFORE the
#: endpoint volume (MEASURED round 9, laptop Realtek speakers: a -20 dBFS tone
#: loops back at -20.0 dBFS at volume 1.00 and at 0.30), so turning the PC's
#: volume down weakens the echo and leaves the reference as it was. MEASURED
#: (round 9, the round-8 laptop-speaker dumps replayed with the mic 10 dB
#: quieter, i.e. a lower volume or mic gain): with the old -60 dBFS gate on
#: the mic the echo (-67..-73 dBFS, 5-10 dB above the room) was never probed
#: -- 0 probes, never cancelled -- while the status said "echo on"; with this
#: gate it was found and cancelled exactly as at full level. The probe's
#: thresholds are on GCC-PHAT peak-to-RMS, which does not depend on level,
#: so a quiet mic does not make a false find likelier (null replay, round 9:
#: 96 probes, never slotted, at the mic's level and 20 dB under it). LIVE
#: (round 9, BT pad mic, real music on the laptop speakers at volume 0.15,
#: echo -68 dBFS before the mic gain): the old gate dropped the slot ("no
#: longer found") and cancelled 1.4 dB; this one kept it, 8 dB, the
#: residual at the room's noise floor.
MIC_MIN_RMS = 3e-5
#: The canceller stays active this long after its last loud reference frame
#: (the filter tail plus margin), then drops to the bit-exact idle path.
ACTIVE_HOLD_FRAMES = TAIL_MS // 10 + 10
GAP_S = 0.25
#: The mic mapping is anchored on the least-late packet over this long at the
#: start of a session, then held. Short on purpose: what keeps an endpoint's
#: filter valid from one session to the next is the FINE alignment below, not
#: this anchor. MEASURED live (round 5): the least-late estimate after 20
#: packets is within ~0.3 ms of the one after 2 s, and either differs between
#: sessions by 0.1-0.3 ms -- 5-15 samples, enough on its own to cut a kept,
#: converged filter's cancellation to -6 dB (synthetic: 1 ms of arrival
#: jitter took the first half second after a session start from -168 to
#: -7 dB).
STARTUP_S = 0.05
#: A loopback stamp this far from where the stream says it is re-anchors.
STAMP_JUMP = 96

# --- bulk delay probe ---------------------------------------------------------
PROBE_WINDOW_S = 1.0
PROBE_MAX_LAG_S = 0.6
PROBE_LEAD_MS = 50
PROBE_DECIM = 6
#: GCC-PHAT peak-to-RMS needed for a vote. Calibrated on real audio: an
#: external source in the mic against unrelated loopback audio peaked at
#: 10.4 (median 4.1, 214 one-second windows); real HDMI echo, 13.7-24 median.
PROBE_MIN_PEAK = 12.0
#: A peak this high (twice the null maximum) is enough on its own.
PROBE_SURE_PEAK = 20.0
#: A slotted endpoint's echo counts as still there with a peak this high
#: inside its filter's window (see `_probe`).
PROBE_CONFIRM_PEAK = 8.0
#: A stereo reference is probed in three views (`_probe_views`: the mix, left,
#: right). Every peak threshold here -- MIN, SURE, CONFIRM, PAIR -- applies to
#: the best of the three: they ask whether, and where, the endpoint's echo is
#: in the mic, whichever channel carries it. That costs a little against
#: unrelated audio (synthetic null, 1 s windows: median peak 3.73 -> 3.98 over
#: the whole range, 2.89 -> 3.31 inside a filter's window; the live null
#: maximum, 10.4, was measured on the mix alone, against PROBE_MIN_PEAK 12;
#: live with the three views, round 8: stereo music on an endpoint with no
#: acoustic path, 6 takes, never slotted).
#: WHICH view an endpoint is aligned on (`Ref.view`, its arrival pattern) is a
#: separate choice: the mix, unless one channel's peak beats it by
#: PROBE_VIEW_MARGIN -- over the windows that voted for the delay when the
#: endpoint is slotted, and while slotted from each probe that hears the echo
#: (a channel must then beat the CURRENT view by the margin). With mono content
#: on a stereo endpoint the three views are the same signal and only rounding
#: tells them apart (review of round 8: the plain best peak picked mix, left,
#: right 158 / 42 / 0 times in 200), so the margin keeps the mix, and a slot
#: taken on mono content moves to the right channel by itself once the
#: content turns stereo. MEASURED (these are the probe's confidences, peak /
#: RMS, the numbers `_choose_view` compares): a speaker playing one channel
#: of L != R content, live (round 8b, wired DualSense, right channel only,
#: first probe of a session): channel / mix 12.2 / 9.3 = 1.31, 13.3 / 5.9 =
#: 2.25, and 17.9 / 12.7 = 1.41 on a replayed dump; synthetic (240 windows per
#: case, room noise 0.5-2x the echo): median 1.62-1.66, 5th percentile
#: 1.26-1.34; mono content exactly 1.000; unrelated audio 95th percentile 1.25
#: (it never votes anyway). So the live headroom over the margin can be as
#: thin as 1.31; a switch that misses simply retries at the next probe.
PROBE_VIEW_MARGIN = 1.25
PROBE_VOTES = 2
#: Two arrivals of similar strength (see `_pair_vote`): both peaks at least
#: this high, at most PROBE_PAIR_SPAN_MS apart, at the same places in this
#: many windows in a row.
PROBE_PAIR_PEAK = 7.0
PROBE_PAIR_SPAN_MS = 60
PROBE_PAIR_VOTES = 3
PROBE_AGREE_MS = 2
PROBE_EVERY_S = 0.25
# --- fine alignment ------------------------------------------------------------
#: Each slotted endpoint's echo is re-located to a fraction of a sample (full
#: rate GCC-PHAT, 100 Hz - 7 kHz, +-FINE_SPAN_MS around where the filter
#: expects it) and the reference read point is moved so the echo stays where
#: the filter learned it: right after every session start (the mic mapping is
#: re-anchored then), and every FINE_EVERY_S after that.
FINE_WINDOW_S = 0.25
FINE_SPAN_MS = 30
#: The largest re-alignment searched for (the session anchor's error).
FINE_SHIFT_MS = 20
#: Correlation of the arrival pattern with the one the filter learned at
#: needed to trust a re-alignment.
FINE_MIN_CORR = 0.5
#: ... and this well, one measurement is enough at a session start (otherwise
#: two in a row must agree). MEASURED live (round 5): correct re-alignments
#: correlate 0.5-0.8; measurements below 0.3 were off by up to 870 samples.
FINE_SURE_CORR = 0.7
#: A pattern correlating this well is evidence the echo is still there
#: (unrelated audio: about 0.08, the maximum over the shifts searched).
FINE_HEARD_CORR = 0.3
FINE_EVERY_S = 2.0
#: At a session start a filter waits (not run) at most this long (mic time)
#: for its echo to be re-located before it runs anyway.
FINE_HOLD_S = 1.0
#: Within a session, smaller corrections are left to the adaptive filter.
FINE_MOVE = 0.5
#: Clock drift between a reference and the mic (a Bluetooth speaker, a USB
#: DAC: tens of ppm) is FOLLOWED, not corrected in jumps: the fine
#: measurements of the last `DRIFT_FIT_N` windows are fitted with a line, and
#: once that line explains them to `DRIFT_FIT_TOL` samples over at least
#: `DRIFT_MIN_SPAN_S` and `DRIFT_MIN_MOTION` samples of motion, its slope is
#: the endpoint's rate and the read point is steered at that rate every
#: frame (`Ref.rate`, `_frame`). Rates beyond `DRIFT_MAX_PPM` are not believed.
DRIFT_FIT_N = 6
DRIFT_FIT_TOL = 0.6
DRIFT_MIN_SPAN_S = 6.0
DRIFT_MIN_MOTION = 1.0
DRIFT_MAX_PPM = 400.0
TRACK_EVERY_S = 5.0
#: The echo may wander this far (ms) inside the filter before the read point
#: is moved (the filter covers -EARLY_MS .. TAIL_MS - EARLY_MS).
TRACK_EARLY_MS = EARLY_MS - 3
TRACK_LATE_MS = 40
#: A slotted endpoint whose echo was not found in `UNHEARD_WINDOWS` windows
#: of `UNHEARD_WINDOW_S` of its audio in a row -- neither by a probe peak
#: inside its filter's window nor by its arrival pattern -- loses its slot;
#: the probe may find it again after `UNHEARD_RETRY_S`. (Its filter's own
#: statistics cannot tell: see `_watch`.)
UNHEARD_WINDOW_S = 5.0
UNHEARD_WINDOWS = 4
UNHEARD_RETRY_S = 5.0
#: A delay remembered from an earlier run is trusted for this long of the
#: endpoint's audio; by then a probe must have confirmed it or its filter
#: must be estimating an echo above `PROVISIONAL_EST_DB`, or it is forgotten.
PROVISIONAL_S = 3.0
PROVISIONAL_EST_DB = -15.0
#: ... and remove at least this fraction of its estimate. Both are required,
#: and so is evidence the echo is there (a probe peak inside the filter's
#: window, or two windows agreeing on an arrival pattern): a dead echo with a
#: voice in the room gave -9 dB and 0.4 (round 5), too close to 0.5 alone.
PROVISIONAL_Q = 0.65
#: When a new endpoint qualifies and the slot budget is full, a slotted
#: endpoint whose reference has been silent this long gives up its slot
#: (its delay stays remembered, so it comes back quickly).
EVICT_SILENT_S = 10.0
#: An endpoint silent this long may try its remembered delay again.
STORE_RETRY_SILENT_S = 5.0
#: The delay store is written at most this often per endpoint for small
#: changes; a change over `STORE_MIN_CHANGE` samples (beyond what the
#: endpoint's followed drift explains) is written at once.
STORE_DEBOUNCE_S = 30.0
STORE_MIN_CHANGE = 1.0
#: The arrival pattern is re-taken after this many windows in a row fail to
#: match it while probes still find the echo (the room changed).
FINE_STALE_WINDOWS = 3
#: ... and only while the endpoint's echo estimate is at least this share of
#: the mic (nothing else loud in the room).
FINE_RELEARN_SHARE_DB = -3.0

PAD_NAME_NEEDLE = "dualsense"


# ---------------------------------------------------------------------------
# the native core
# ---------------------------------------------------------------------------


def _dll_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "_native", "ds5aec.dll")


_DLL = None


NATIVE_VERSION = 2


def load_native():
    """The ds5aec DLL, loaded once. Raises OSError if it is not there (or is
    an older build with a different interface)."""
    global _DLL
    if _DLL is None:
        d = ctypes.CDLL(_dll_path())
        vp, ci = ctypes.c_void_p, ctypes.c_int
        d.ds5aec_version.restype = ci
        if d.ds5aec_version() != NATIVE_VERSION:
            raise OSError(f"ds5aec.dll is interface {d.ds5aec_version()}, "
                          f"this code needs {NATIVE_VERSION}: rebuild it")
        d.ds5aec_new.restype = vp
        d.ds5aec_new.argtypes = [ci, ci]
        d.ds5aec_free.argtypes = [vp]
        d.ds5aec_reset.argtypes = [vp]
        d.ds5aec_set.argtypes = [vp] + [ctypes.c_float] * 5 + [ci]
        d.ds5aec_process.argtypes = [vp, vp, ci, vp, vp, vp, ci, vp, vp, vp]
        d.ds5aec_process.restype = ci
        d.ds5mdf_new.restype = vp
        d.ds5mdf_new.argtypes = [ci, ci, ci, ci]
        d.ds5mdf_free.argtypes = [vp]
        d.ds5mdf_reset.argtypes = [vp]
        d.ds5mdf_flush.argtypes = [vp, ci]
        d.ds5rs_new.restype = vp
        d.ds5rs_new.argtypes = [ci, ci, ci, ci]
        d.ds5rs_free.argtypes = [vp]
        d.ds5rs_process.argtypes = [vp, vp, vp, vp, vp]
        d.ds5rs_process.restype = ci
        _DLL = d
    return _DLL


class NativeStage:
    """One render endpoint's linear filter (speexdsp MDF over its `nch`
    reference channels). Lives as long as the endpoint is slotted -- across
    capture sessions and other endpoints coming and going."""

    def __init__(self, nch: int, tail_ms: int = TAIL_MS):
        self._d = load_native()
        self.nch = nch
        self._h = self._d.ds5mdf_new(FRAME, int(tail_ms * FS / 1000), nch, FS)
        if not self._h:
            raise OSError("ds5mdf_new failed")

    @property
    def handle(self) -> int:
        return self._h

    def reset(self) -> None:
        if self._h:
            self._d.ds5mdf_reset(self._h)

    def flush(self, frames: int = TAIL_MS // 10 + 1) -> None:
        """Make the filter's reference history silence (weights untouched)."""
        if self._h:
            self._d.ds5mdf_flush(self._h, frames)

    def close(self) -> None:
        if self._h:
            self._d.ds5mdf_free(self._h)
            self._h = None

    def __del__(self):
        try:
            self.close()
        except Exception:  # noqa: BLE001
            pass


class NativeCanceller:
    """The chain runner + residual stage: one per canceller. Per frame it runs
    the given endpoints' stages in order, then the residual suppressor."""

    def __init__(self):
        self._d = load_native()
        self._h = self._d.ds5aec_new(FRAME, FS)
        if not self._h:
            raise OSError("ds5aec_new failed")
        self._out = np.zeros(FRAME, "<i2")
        self.stats = np.zeros(8, np.float32)
        self.stage_stats = np.zeros(3 * 8, np.float32)
        self._hs = (ctypes.c_void_p * 8)()
        self._rs = (ctypes.c_void_p * 8)()
        self.gains = None              # set to a (481,) float32 array to receive the gains

    def set(self, rho=-1.0, over=-1.0, gmin_near=-1.0, gmin_far=-1.0, near_ratio=-1.0,
            hold_frames=-1) -> None:
        self._d.ds5aec_set(self._h, rho, over, gmin_near, gmin_far, near_ratio, hold_frames)

    def process(self, mic16: np.ndarray, stages: list, refs: list, active: bool) -> np.ndarray:
        """mic16 (FRAME,) int16; stages[i] a NativeStage, refs[i] its
        (FRAME, nch) int16 C-contiguous frame. Returns the output frame (one
        frame late), int16; `stats` and `stage_stats` (per stage: input,
        output and echo-estimate power) are updated."""
        n = len(stages)
        for i in range(n):
            self._hs[i] = stages[i].handle
            self._rs[i] = refs[i].ctypes.data
        g = self.gains
        rc = self._d.ds5aec_process(self._h, mic16.ctypes.data, n, self._hs, self._rs,
                                    self._out.ctypes.data, int(active),
                                    None if g is None else g.ctypes.data,
                                    self.stats.ctypes.data, self.stage_stats.ctypes.data)
        if rc != 0:
            raise RuntimeError(f"ds5aec_process -> {rc}")
        return self._out.copy()

    def reset(self) -> None:
        self._d.ds5aec_reset(self._h)

    def close(self) -> None:
        if self._h:
            self._d.ds5aec_free(self._h)
            self._h = None

    def __del__(self):
        try:
            self.close()
        except Exception:  # noqa: BLE001
            pass


class NativeResampler:
    """speexdsp's resampler, `nch` interleaved float channels -> 48 kHz."""

    def __init__(self, nch: int, in_rate: int, out_rate: int = FS, quality: int = 5):
        self._d = load_native()
        self.nch = nch
        self.ratio = out_rate / in_rate
        self._h = self._d.ds5rs_new(nch, in_rate, out_rate, quality)
        if not self._h:
            raise OSError("ds5rs_new failed")

    def push(self, x: np.ndarray) -> np.ndarray:
        """x (n, nch) float32 -> (m, nch) float32."""
        x = np.ascontiguousarray(x, np.float32)
        n = x.shape[0]
        out = np.zeros((int(n * self.ratio) + 64, self.nch), np.float32)
        done_in = done_out = 0
        while done_in < n:
            il = ctypes.c_uint(n - done_in)
            ol = ctypes.c_uint(out.shape[0] - done_out)
            if ol.value == 0:
                out = np.concatenate([out, np.zeros_like(out)])
                ol = ctypes.c_uint(out.shape[0] - done_out)
            rc = self._d.ds5rs_process(self._h, x[done_in:].ctypes.data, ctypes.byref(il),
                                       out[done_out:].ctypes.data, ctypes.byref(ol))
            if rc != 0:
                raise RuntimeError(f"ds5rs_process -> {rc}")
            if il.value == 0 and ol.value == 0:
                break
            done_in += il.value
            done_out += ol.value
        return out[:done_out]

    def close(self) -> None:
        if self._h:
            self._d.ds5rs_free(self._h)
            self._h = None

    def __del__(self):
        try:
            self.close()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# timelines
# ---------------------------------------------------------------------------


class RefTimeline:
    """One loopback reference (1 or 2 channels) on the 48 kHz perf_counter
    timeline.

    Samples live in a ring indexed by how many were ever written; a timeline
    position is that index plus `off`, a float that follows the packets'
    presentation stamps (they are exact once a stream runs). A stamp that
    disagrees by more than `STAMP_JUMP` re-anchors; silence fills a gap.
    `read` interpolates at fractional positions (cubic), so following a
    drifting clock is a smooth resampling rather than a sample slip.
    """

    def __init__(self, nch: int = 1, seconds: float = 3.0):
        self.nch = nch
        self.cap = int(seconds * FS)
        self._buf = np.zeros((self.cap, nch), np.float32)
        self._lock = threading.Lock()
        self._w = 0
        self._lo = 0
        self.off: float | None = None
        self._last_t = -math.inf
        self.anchors = 0
        self.last_push = -math.inf
        self.last_loud = -math.inf

    @property
    def wpos(self) -> float | None:
        off = self.off
        return None if off is None else self._w + off

    def push(self, x: np.ndarray, t_end: float, epoch: float) -> None:
        x = np.asarray(x, np.float32).reshape(-1, self.nch)
        n = int(x.shape[0])
        if n == 0:
            return
        epos = (t_end - epoch) * FS
        with self._lock:
            if (self.off is None or t_end - self._last_t > GAP_S
                    or abs(epos - (self._w + n + self.off)) > STAMP_JUMP):
                start = epos - n
                w = None if self.off is None else self._w + self.off
                if w is not None and 0 <= start - w < self.cap:
                    self._append(np.zeros((int(round(start - w)), self.nch), np.float32))
                else:
                    self._lo = self._w
                self.off = start - self._w
                self.anchors += 1
            self._append(x)
            self.off += 0.05 * ((epos - self._w) - self.off)
            self._last_t = t_end
            self.last_push = t_end
            if float(np.vdot(x, x)) > LOUD_RMS * LOUD_RMS * n:
                self.last_loud = t_end

    def _append(self, x: np.ndarray) -> None:
        n = int(x.shape[0])
        if n == 0:
            return
        if n > self.cap:
            self._w += n - self.cap
            x = x[-self.cap:]
            n = self.cap
        i = self._w % self.cap
        first = min(n, self.cap - i)
        self._buf[i:i + first] = x[:first]
        if n > first:
            self._buf[:n - first] = x[first:]
        self._w += n

    def read(self, pos: float, n: int) -> np.ndarray:
        """(n, nch) samples starting at fractional timeline position `pos`."""
        with self._lock:
            if self.off is None:
                return np.zeros((n, self.nch), np.float32)
            f = pos - self.off
            i0 = math.floor(f)
            frac = f - i0
            lo = max(self._lo, self._w - self.cap)
            a, b = i0 - 1, i0 + n + 2              # ring samples needed
            seg = np.zeros((b - a, self.nch), np.float32)
            ca, cb = max(a, lo), min(b, self._w)
            if cb > ca:
                idx = np.arange(ca, cb) % self.cap
                seg[ca - a:cb - a] = self._buf[idx]
        if frac < 1e-6:
            return seg[1:1 + n].copy()
        p0, p1, p2, p3 = seg[0:n], seg[1:n + 1], seg[2:n + 2], seg[3:n + 3]
        t = np.float32(frac)
        return (p1 + 0.5 * t * (p2 - p0 + t * (2 * p0 - 5 * p1 + 4 * p2 - p3
                                               + t * (3 * (p1 - p2) + p3 - p0)))).astype(np.float32)


class MicHistory:
    """The raw (pre-cancellation) mic on the timeline, for the delay probe."""

    def __init__(self, seconds: float = 3.0):
        self.cap = int(seconds * FS)
        self._buf = np.zeros(self.cap, np.float32)
        self.wpos: int | None = None

    def put(self, pos: int, x: np.ndarray) -> None:
        n = x.size
        if self.wpos is not None and pos > self.wpos and pos - self.wpos < self.cap:
            self._write(self.wpos, np.zeros(pos - self.wpos, np.float32))
        self._write(pos, x)
        self.wpos = pos + n

    def _write(self, pos: int, x: np.ndarray) -> None:
        idx = np.arange(pos, pos + x.size) % self.cap
        self._buf[idx] = x

    def read(self, pos: int, n: int) -> np.ndarray:
        out = np.zeros(n, np.float32)
        if self.wpos is None:
            return out
        lo = max(pos, self.wpos - self.cap); hi = min(pos + n, self.wpos)
        if hi > lo:
            out[lo - pos:hi - pos] = self._buf[np.arange(lo, hi) % self.cap]
        return out


class MicClock:
    """Maps mic sample indices (exact, thanks to the packet sequence numbers)
    onto the timeline.

    Anchored on the LEAST late packet during the first `STARTUP_S` of a
    session, then HELD for the whole session: per-packet arrival times jitter
    by milliseconds, and following them moved the read point by 2-4 samples,
    which at 48 kHz is enough to undo the linear filter's high-frequency
    cancellation (MEASURED offline, round 4). Nor does it try to follow a
    drift: a slope fitted through per-second arrival minima (round 4) found
    1.2-1.7 samples/s where there was none and walked the mapping 22 samples
    in 5 minutes, taking a live take from -22 to -16 dB (MEASURED, round 5).
    Whatever does move between the mic and a reference -- a Bluetooth
    speaker's own clock, say -- is followed per endpoint by the fine
    alignment (`EchoCanceller._fine`), from the echo itself.
    """

    def __init__(self):
        self.count = 0
        self.offset: float | None = None
        self.off_f = 0.0
        self._last_t = -math.inf
        self._e: deque = deque()
        self.anchors = 0
        self.startup_until = -math.inf

    def advance(self, n: int, t_end: float, epoch: float, force_anchor: bool = False) -> None:
        self.count += n
        e = (t_end - epoch) * FS - self.count
        if self.offset is None or force_anchor or t_end - self._last_t > GAP_S:
            self._e.clear()
            self.anchors += 1
            self.startup_until = t_end + STARTUP_S
        self._last_t = t_end
        if t_end < self.startup_until:
            self._e.append(e)
            self.offset = min(self._e)
            self.off_f = self.offset


# ---------------------------------------------------------------------------
# the delay probe
# ---------------------------------------------------------------------------


def _decimate(x: np.ndarray, k: int = PROBE_DECIM) -> np.ndarray:
    n = (x.size // k) * k
    return x[:n].reshape(-1, k).mean(axis=1)


def estimate_lag(mic: np.ndarray, ref: np.ndarray, max_lag: int, lead: int = 0,
                 near: tuple | None = None, second: bool = False):
    """GCC-PHAT. `mic` is W samples; `ref` covers [max_lag before mic, lead after
    it]. Returns (lag in 48 kHz samples, peak-to-RMS confidence); lag in
    [-lead, max_lag], positive = the echo is later than the reference. With
    `near=(lo, hi)` (lags, 48 kHz samples) a third value is returned: the
    confidence of the highest peak inside that range. With `second`, two more
    values instead: the lag and confidence of the highest peak at least
    PROBE_AGREE_MS away from the first."""
    m = _decimate(mic)
    r = _decimate(ref)
    L = max_lag // PROBE_DECIM
    A = lead // PROBE_DECIM
    if m.size < 64 or r.size < m.size + L + A:
        return 0, 0.0
    nfft = 1 << int(math.ceil(math.log2(m.size + r.size)))
    G = np.fft.rfft(r, nfft) * np.conj(np.fft.rfft(m, nfft))
    G /= np.abs(G) + 1e-12
    cc = np.fft.irfft(G, nfft)[: L + A + 1]
    k = int(np.argmax(cc))
    rms = float(np.sqrt(np.mean(cc * cc))) + 1e-12
    if second:
        g = PROBE_AGREE_MS * FS // 1000 // PROBE_DECIM + 1
        c2 = cc.copy()
        c2[max(0, k - g):k + g + 1] = -np.inf
        k2 = int(np.argmax(c2))
        return ((L - k) * PROBE_DECIM, float(cc[k]) / rms,
                (L - k2) * PROBE_DECIM, float(cc[k2]) / rms)
    if near is None:
        return (L - k) * PROBE_DECIM, float(cc[k]) / rms
    # lag (L - k) * DECIM in [lo, hi]  <=>  k in [L - hi/DECIM, L - lo/DECIM]
    k0 = max(0, int(math.floor(L - near[1] / PROBE_DECIM)))
    k1 = min(cc.size - 1, int(math.ceil(L - near[0] / PROBE_DECIM)))
    local = float(np.max(cc[k0:k1 + 1])) / rms if k1 >= k0 else 0.0
    return (L - k) * PROBE_DECIM, float(cc[k]) / rms, local


def fine_pattern(mic: np.ndarray, ref: np.ndarray, span: int) -> np.ndarray:
    """The echo path's arrival pattern: full-rate GCC-PHAT (100 Hz - 7 kHz)
    of `mic` against `ref`, which is `mic.size + 2*span` samples starting
    `span` before where the echo is expected; 2*span+1 lags, unit norm.
    Index span - x holds the arrival x samples later than expected."""
    W = mic.size
    nfft = 1 << int(math.ceil(math.log2(W + 2 * span)))
    G = np.conj(np.fft.rfft(mic, nfft)) * np.fft.rfft(ref, nfft)
    f = np.fft.rfftfreq(nfft, 1.0 / FS)
    G[(f < 100.0) | (f > 7000.0)] = 0
    G /= np.abs(G) + 1e-12
    cc = np.fft.irfft(G, nfft)[: 2 * span + 1]
    return cc / (float(np.linalg.norm(cc)) + 1e-12)


def pattern_shift(now: np.ndarray, then: np.ndarray, max_shift: int, up: int = 16):
    """How many samples LATER the arrival pattern `now` is than `then`
    (to 1/`up` of a sample: the cross-correlation is interpolated by zero
    padding its spectrum, exact for these band-limited patterns), and the
    correlation coefficient there. The whole pattern is matched, not its
    highest peak: a room with two nearly equal reflections makes the peak
    hop between them, the pattern does not."""
    n = now.size
    nfft = 1 << int(math.ceil(math.log2(2 * n)))
    X = np.fft.rfft(then, nfft) * np.conj(np.fft.rfft(now, nfft))
    c = np.fft.irfft(X, nfft * up) * up
    # c[d * up] = sum_k now[k] then[k + d]: an arrival later by d moves the
    # pattern to LOWER indices (index = span - offset)
    m = max_shift * up
    vals = np.concatenate([c[:m + 1], c[c.size - m:]])
    lags = np.concatenate([np.arange(0, m + 1), np.arange(-m, 0)])
    i = int(np.argmax(vals))
    return float(lags[i]) / up, float(vals[i])


# ---------------------------------------------------------------------------
# references
# ---------------------------------------------------------------------------


def _probe_mix(ref: np.ndarray) -> np.ndarray:
    """(n, nch) -> (n,) for the delay probe: unequal weights, so anti-phase
    content (L = -R) does not vanish while both speakers play it."""
    if ref.shape[1] == 1:
        return ref[:, 0]
    w = (1.0 / (1.0 + 0.5 * np.arange(ref.shape[1]))).astype(np.float32)
    return (ref @ w / w.sum()).astype(np.float32)


#: The views of a reference the probe correlates the mic with (`_probe_views`).
VIEW_NAMES = ("mix", "left", "right")


def _probe_views(ref: np.ndarray) -> list:
    """(n, nch) -> the signals the delay probe tries: the mix and, for two
    channels, each channel on its own. The best peak says whether and where
    the echo is; the view an endpoint's arrival pattern is taken on
    (`Ref.view`) is the mix unless a channel beats it clearly (see
    PROBE_VIEW_MARGIN).

    MEASURED round 8 (2026-10-23): a DualSense on a USB cable plays only the
    RIGHT channel of its stereo endpoint. With different content left and
    right the mix halved the correlation peak (0.05-0.10 against 0.12, 0.20
    and 0.23 for the right channel alone, three takes) and two 18 s takes of
    stereo speech were never cancelled from a cold start.

    The mix is NOT symmetric (`_probe_mix` weights left 1.0 : right 0.67), so
    a speaker that plays only the LEFT channel is a milder case: its left /
    mix confidence ratio is only ~1.15 (synthetic: median 1.15, 5th
    percentile 1.08; 7% of windows reach PROBE_VIEW_MARGIN). It mostly stays
    on the mix, which costs little at that ratio, and may move to left by
    noise at a later probe."""
    return [_probe_view(ref, v) for v in range(1 if ref.shape[1] == 1 else 1 + ref.shape[1])]


def _probe_view(ref: np.ndarray, view: int) -> np.ndarray:
    """One of `_probe_views`, built alone (0: the mix, 1 + c: channel c)."""
    if view == 0 or ref.shape[1] == 1:
        return _probe_mix(ref)
    return np.ascontiguousarray(ref[:, view - 1])


def _choose_view(scores, current: int = 0) -> int:
    """The view to align on, from each view's peak (summed over the windows
    that agree): `current` unless another beats it by PROBE_VIEW_MARGIN."""
    best = max(range(len(scores)), key=lambda i: scores[i])
    if best != current and scores[best] >= PROBE_VIEW_MARGIN * scores[current]:
        return best
    return current


class Ref:
    """One render endpoint seen on loopback."""

    def __init__(self, ref_id: str, name: str, nch: int):
        self.id = ref_id
        self.name = name
        self.nch = nch
        self.timeline = RefTimeline(nch)
        #: Timeline samples between the mic's mapped time and this reference's
        #: read point, beyond REF_BASE: the echo path's bulk delay minus EARLY.
        self.delay: float | None = None
        #: The endpoint's linear filter while it is slotted (a NativeStage).
        self.stage = None
        self.slotted = False
        #: Slotted on a delay remembered from an earlier run, not yet proven.
        self.provisional = False
        #: ... and a tracking probe moved it: the remembered delay was wrong.
        self.prov_moved = False
        #: Agreeing two-arrival probe results (see PROBE_PAIR_*).
        self.pair_votes: list = []
        self.store_tried = False
        self.votes: list = []
        self.last_probe_pos: int | None = None
        self.track_votes: list = []
        self.unheard_until = -math.inf
        self.quiet_frames = 1 << 30
        self.lag_conf = 0.0
        self.est_share_db: float | None = None
        self.removal_ratio: float | None = None
        #: Drift of this endpoint's echo against the mic, in samples per mic
        #: sample (see DRIFT_*). Kept across slots and sessions: it is a
        #: property of the endpoint's clock.
        self.rate = 0.0
        self.stored_ms: float | None = None
        self.stored_at = -math.inf
        self.stored_view = 0
        self.probe_ok_at = -math.inf
        #: Which `_probe_views` view the arrival pattern is taken on (0: the
        #: mix; see PROBE_VIEW_MARGIN). Chosen when slotted, kept in the delay
        #: store, and re-chosen by the probes while slotted.
        self.view = 0
        self._clear_fine()
        self._clear_windows()

    def _clear_fine(self) -> None:
        #: Fine correction of the read point (samples, added to `delay`) and
        #: the echo offset the filter learned at (None: not measured yet).
        self.adj = 0.0
        self.fine_pat: np.ndarray | None = None
        self.fine_cand: np.ndarray | None = None
        self.fine_cand_pos: float | None = None
        self.fine_due = False
        self.fine_pend: float | None = None
        self.fine_last: float | None = None
        #: (mic position, where the echo was found = adj + offset) of recent
        #: fine measurements in this session, for the drift fit
        self.fine_hist: list = []
        self.fine_stale = 0

    def _clear_windows(self) -> None:
        self.win_mic = 0.0
        self.win_in = 0.0
        self.win_out = 0.0
        self.win_est = 0.0
        self.win_frames = 0
        self.heard = False             # a probe found the echo in this window
        self.bad_windows = 0

    def describe(self) -> dict:
        return {"id": self.id, "name": self.name, "channels": self.nch,
                "slotted": self.slotted, "provisional": self.provisional,
                "view": VIEW_NAMES[self.view],
                "delay_ms": None if self.delay is None else round((self.delay + self.adj) * 1000 / FS, 2),
                "conf": round(self.lag_conf, 1), "anchors": self.timeline.anchors,
                "echo_share_db": None if self.est_share_db is None else round(self.est_share_db, 1),
                "removal_ratio": None if self.removal_ratio is None else round(self.removal_ratio, 2),
                "drift_ppm": round(self.rate * 1e6, 2)}


class DelayStore:
    """Bulk delays the probe found, per (pad, endpoint), kept in a small JSON
    file, so that after a bridge restart a known speaker is cancelled from
    its first sound instead of after the probe's ~1.5 s. A remembered delay
    is only provisional until the filter proves it (`PROVISIONAL_S`).
    Never raises: a store that cannot be read or written is just empty."""

    def __init__(self, path: str):
        self.path = path
        self._d: dict = {}
        self._warned = False
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict):
                self._d = {k: v for k, v in d.items()
                           if isinstance(v, dict) and isinstance(v.get("delay_ms"), (int, float))}
        except Exception:  # noqa: BLE001
            self._d = {}

    def get(self, key: str) -> float | None:
        v = self._d.get(key)
        return None if v is None else float(v["delay_ms"])

    def view(self, key: str) -> int:
        """The probe view stored with the delay (`VIEW_NAMES`); 0, the mix,
        for an entry written before views were stored, or an unknown name."""
        v = self._d.get(key) or {}
        name = v.get("view")
        return VIEW_NAMES.index(name) if name in VIEW_NAMES else 0

    def put(self, key: str, delay_ms: float, view: int = 0) -> None:
        self._d[key] = {"delay_ms": round(float(delay_ms), 3), "view": VIEW_NAMES[view],
                        "t": round(time.time())}
        self._save()

    def forget(self, key: str) -> None:
        if self._d.pop(key, None) is not None:
            self._save()

    def _save(self) -> None:
        try:
            d = os.path.dirname(self.path)
            if d:
                os.makedirs(d, exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._d, f, indent=1, sort_keys=True)
            os.replace(tmp, self.path)
        except Exception as e:  # noqa: BLE001
            if not self._warned:
                self._warned = True
                log.warning("echo delays cannot be remembered (%s is not writable: %s); "
                            "each speaker is found again after a restart", self.path, e)
            else:
                log.debug("echo delay store not written", exc_info=True)


# ---------------------------------------------------------------------------
# the canceller
# ---------------------------------------------------------------------------


class EchoCanceller:
    """See the module docstring.

    `write_out(pcm)` receives finished frames as 2-channel interleaved s16le
    on the canceller's thread. `own_usb_ports()` returns the usbip ports this
    bridge's virtual pad is attached on (or None while unknown); a DualSense
    endpoint at one of those addresses is this pad's own speaker and is never
    a reference (the pad cancels it itself). While it is unknown, every
    DualSense endpoint is left out. `delay_store` (a DelayStore) and
    `pad_id` (this pad's id, or a callable returning it) let found delays
    survive a restart.

    Lifecycle: `start()`, then `pause()` / `resume()` as the mic is disarmed
    and armed, `stop()` with the bridge. Delays and every endpoint's filter
    survive a pause; a filter is reset only when its delay moves or its
    endpoint changes.
    """

    def __init__(self, write_out, *, native_factory=None, stage_factory=None,
                 loopback: bool = True, clock=time.perf_counter, loopback_factory=None,
                 own_usb_ports=None, usb_address=None, delay_store=None, pad_id=None):
        self.write_out = write_out
        self._native_factory = native_factory or NativeCanceller
        self._stage_factory = stage_factory or NativeStage
        self._want_loopback = loopback
        self._loopback_factory = loopback_factory
        self._own_ports = own_usb_ports or (lambda: None)
        self._usb_address = usb_address
        self._store = delay_store
        self._pad_id = pad_id if callable(pad_id) else (lambda: pad_id)
        self._clock = clock
        self.epoch = clock()
        self._refs: dict[str, Ref] = {}
        #: Slotted references, in chain order.
        self._slots: list[Ref] = []
        self._lock = threading.Lock()
        #: Held while the worker writes a frame and while the reader switches
        #: to direct writes, so no worker frame can land after a direct one.
        self._out_lock = threading.Lock()
        self._q: deque = deque()
        self._overrun = False
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._active = threading.Event()
        self._active.set()
        #: Capture-session generation, bumped by every pause(): a pause and a
        #: resume quicker than the worker looks is still a new session.
        self._gen = 0
        self._seen_gen = 0
        self._cur_gen = 0
        self._paused = False
        self._thread: threading.Thread | None = None
        self._loopback = None
        self._loopback_retry = 0.0
        self._loopback_backoff = 2.0
        self.native: NativeCanceller | None = None
        self.mic_hist = MicHistory()
        self._reset_session()
        self._probe_at = 0.0
        self._probe_rr = 0
        self.mode = "starting"
        self.reason = ""
        self.stats = {"frames": 0, "frames_active": 0, "lost_packets": 0,
                      "refused": 0, "overruns": 0, "native_errors": 0, "slotted": 0,
                      "slotted_from_store": 0, "store_rejected": 0, "evicted": 0, "pair_slotted": 0,
                      "unslotted": 0, "delay_moves": 0, "fine_moves": 0, "drift_fits": 0, "fine_repatterns": 0,
                      "view_changes": 0, "probes": 0, "sessions": 0,
                      "native_us": 0.0}

    def _reset_session(self) -> None:
        self.mic = MicClock()
        self._seq: int | None = None
        self._acc = np.zeros(0, np.float32)
        self._acc_start = 0

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self.epoch = self._clock()
        self._thread = threading.Thread(target=self._run, name="ds5-aec", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 1.0) -> None:
        self._stop.set()
        self._wake.set()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=timeout)
        self._thread = None
        self._stop_loopback()

    def pause(self) -> None:
        """The mic was disarmed. Never waits."""
        self._gen += 1
        self._active.clear()
        self._wake.set()

    def resume(self) -> None:
        self._active.set()
        self._wake.set()

    @property
    def active(self) -> bool:
        return self._active.is_set()

    @property
    def alive(self) -> bool:
        t = self._thread
        return t is not None and t.is_alive() and not self._stop.is_set()

    @property
    def accepting(self) -> bool:
        return self.alive and self._active.is_set() and self.mode == "aec"

    # -- producers -------------------------------------------------------------

    def push_mic(self, mono: np.ndarray, seq: int | None = None,
                 t: float | None = None) -> bool:
        """One decoded mic packet (BT reader thread). False: not taken -- write
        it yourself. Never waits (beyond one ring write the worker may be in)."""
        if not self.accepting or self._overrun:
            return False
        now = self._clock() if t is None else t
        q = self._q
        if len(q) * FRAME * 1000 // FS >= QUEUE_MAX_MS or (
                q and now - q[0][2] > QUEUE_MAX_MS / 1000):
            # The worker is stalled. From here until it has thrown its queue
            # away, the bridge writes the mic directly: no reordering, no
            # latency growth. Taking the output lock means a frame the worker
            # is writing right now lands before the first direct one, and no
            # worker frame after it.
            with self._out_lock:
                self._overrun = True
            self.stats["overruns"] += 1
            self._wake.set()
            return False
        q.append((mono, seq, now, self._gen))
        self._wake.set()
        return True

    def push_render(self, ref_id: str, name: str, x: np.ndarray, t_end: float) -> None:
        """Loopback audio of one endpoint: (n,) or (n, nch) float32, nch 1 or 2."""
        x = np.asarray(x, np.float32)
        nch = 1 if x.ndim == 1 else int(x.shape[1])
        ref = self._refs.get(ref_id)
        if ref is None or ref.nch != nch:
            # a new endpoint, or its format changed: a fresh reference (the
            # worker notices the old one is gone and drops its slot)
            with self._lock:
                ref = Ref(ref_id, name, nch)
                self._refs[ref_id] = ref
        ref.timeline.push(x, t_end, self.epoch)

    def _on_loopback(self, endpoint, ref, t_start) -> None:
        self.push_render(endpoint.id, endpoint.name, ref, t_start + ref.shape[0] / FS)

    def _exclude(self, endpoint) -> bool:
        """This pad's own speaker endpoint is never a reference."""
        if PAD_NAME_NEEDLE not in (endpoint.name or "").lower():
            return False
        ports = self._own_ports()
        if not ports:
            return True                     # unknown: leave every pad out
        addr = self._usb_address(endpoint.id) if self._usb_address else None
        # None = not a usbip-attached pad (a physically wired one): its
        # speaker is in the room like any other
        return addr is not None and addr in ports

    # -- the worker ------------------------------------------------------------

    def _run(self) -> None:
        try:
            self.prepare()
            while not self._stop.is_set():
                self._wake.wait(0.05)
                self._wake.clear()
                if self._seen_gen != self._gen:
                    self._new_session()
                if not self._active.is_set():
                    if not self._paused:
                        self._paused = True
                        self._q.clear()
                        self._stop_loopback()
                    continue
                self._paused = False
                if self.mode != "aec":
                    continue
                self._ensure_loopback()
                if self._overrun:
                    with self._out_lock:
                        self._q.clear()
                        self._new_session()
                        self._overrun = False
                    continue
                while (self._q and not self._stop.is_set() and self._active.is_set()
                       and not self._overrun and self._seen_gen == self._gen):
                    mono, seq, t, gen = self._q.popleft()
                    if gen != self._seen_gen:
                        continue                # queued before a pause
                    self._cur_gen = gen
                    self.feed_mic(mono, seq, t)
                self.tick()
        except Exception as e:  # noqa: BLE001
            self.mode = "passthrough"
            self.reason = f"canceller thread died: {e!r}"
            log.exception("echo canceller failed -- the microphone is passed through")
        finally:
            self._stop_loopback()

    def prepare(self) -> None:
        try:
            self.native = self._native_factory()
            self.mode = "aec"
        except Exception as e:  # noqa: BLE001
            self.mode = "passthrough"
            self.reason = f"no native canceller: {e}"
            if self._thread is not None:
                log.warning("echo cancellation unavailable (%s); the microphone is "
                            "passed through", self.reason)

    def _new_session(self) -> None:
        """A new capture session (after a pause, or after an overrun threw the
        queue away): the mic timeline starts over. Every endpoint keeps its
        delay and its filter; the filters' reference history is flushed, so
        their first frames are not convolved with audio from before."""
        self._seen_gen = self._gen
        self._cur_gen = self._gen
        self.stats["sessions"] += 1
        self._reset_session()
        for ref in self._slots:
            ref.quiet_frames = 1 << 30
            ref.fine_due = True
            ref.fine_last = None
            ref.fine_hist = []
            if ref.stage is not None:
                ref.stage.flush()
        if self.native is not None:
            self.native.reset()

    def _ensure_loopback(self) -> None:
        if self._loopback is not None or not self._want_loopback:
            return
        now = self._clock()
        if now < self._loopback_retry:
            return
        try:
            if self._loopback_factory is not None:
                lb = self._loopback_factory(self._on_loopback, self._exclude)
            else:
                from .loopback import LoopbackCapture, endpoint_usb_address
                if self._usb_address is None:
                    self._usb_address = endpoint_usb_address
                lb = LoopbackCapture(self._on_loopback, exclude=self._exclude)
            lb.start()
            self._loopback = lb
            self._loopback_backoff = 2.0
        except Exception as e:  # noqa: BLE001
            self._loopback_retry = now + self._loopback_backoff
            log.warning("loopback capture could not start (%s); retrying in %.0f s",
                        e, self._loopback_backoff)
            self._loopback_backoff = min(60.0, self._loopback_backoff * 2)

    def _stop_loopback(self) -> None:
        lb, self._loopback = self._loopback, None
        if lb is not None:
            try:
                lb.stop()
            except Exception:  # noqa: BLE001
                pass

    # -- per frame ---------------------------------------------------------------

    def feed_mic(self, mono: np.ndarray, seq: int | None, t: float) -> None:
        mono = np.asarray(mono, np.float32)
        anchor = False
        if seq is not None:
            if self._seq is not None:
                lost = (seq - self._seq - 1) % 256
                if 0 < lost <= SEQ_MAX_FILL:
                    self.stats["lost_packets"] += lost
                    mono = np.concatenate([np.zeros(lost * FRAME, np.float32), mono])
                elif lost > SEQ_MAX_FILL:
                    anchor = True
            self._seq = seq
        self.mic.advance(int(mono.size), t, self.epoch, force_anchor=anchor)
        self._acc = np.concatenate([self._acc, mono]) if self._acc.size else mono
        while self._acc.size >= FRAME:
            frame = self._acc[:FRAME]
            self._acc = self._acc[FRAME:]
            k = self._acc_start
            self._acc_start += FRAME
            self._frame(frame, k)

    def _frame(self, frame: np.ndarray, k: int) -> None:
        pos = k + self.mic.off_f
        self.mic_hist.put(int(round(pos)), frame)
        base = REF_BASE_MS * FS // 1000
        stages, refs, run, loud = [], [], [], []
        for ref in list(self._slots):
            if self._refs.get(ref.id) is not ref:
                self._unslot(ref)               # the endpoint went away / changed
                continue
            if ref.rate:
                ref.adj += ref.rate * FRAME         # follow the endpoint's clock
            r = ref.timeline.read(pos - base - ref.delay - ref.adj, FRAME)
            is_loud = float(np.vdot(r, r)) > LOUD_RMS * LOUD_RMS * FRAME
            ref.quiet_frames = 0 if is_loud else ref.quiet_frames + 1
            if ref.quiet_frames >= ACTIVE_HOLD_FRAMES:
                continue                        # silent: its filter is not run
            if (ref.fine_due and ref.fine_pat is not None
                    and self._acc_start < FINE_HOLD_S * FS):
                # A new session, and this endpoint's echo has not been
                # re-located yet: its converged filter would subtract a
                # misaligned estimate AND adapt away from what it learned.
                # Leave the mic to it untouched for these few hundred ms.
                continue
            if ref.nch == 2:
                # mid and side rather than left and right: with mono content
                # (side silent) the mid filter learns the true sum of the two
                # paths and nothing is split arbitrarily between channels
                r = np.stack([r[:, 0] + r[:, 1], r[:, 0] - r[:, 1]], 1) * 0.5
            refs.append(np.ascontiguousarray((np.clip(r, -1.0, 1.0) * 32767.0).astype("<i2")))
            stages.append(ref.stage)
            run.append(ref)
            loud.append(is_loud)
        active = bool(run)
        mic16 = (np.clip(frame, -1.0, 1.0) * 32767.0).astype("<i2")
        try:
            t0 = time.perf_counter()
            out = self.native.process(mic16, stages, refs, active)
            self.stats["native_us"] = 0.98 * self.stats["native_us"] + 0.02 * (
                (time.perf_counter() - t0) * 1e6)
        except Exception as e:  # noqa: BLE001
            self.stats["native_errors"] += 1
            self.mode = "passthrough"
            self.reason = f"native call failed: {e!r}"
            log.exception("echo canceller call failed -- passing the microphone through")
            out = mic16
            run = []
        self.stats["frames"] += 1
        if active:
            self.stats["frames_active"] += 1
            st = self.native.stats
            for j, ref in enumerate(run):
                if loud[j] and ref.slotted:
                    ss = self.native.stage_stats
                    self._watch(ref, float(st[0]), float(ss[3 * j]), float(ss[3 * j + 1]),
                                float(ss[3 * j + 2]))
        if self._active.is_set():
            with self._out_lock:
                if not self._overrun and self._cur_gen == self._gen:
                    self.write_out(np.repeat(out, 2).tobytes())

    def _watch(self, ref: Ref, mic_pow: float, in_pow: float, out_pow: float,
               est_pow: float) -> None:
        """Per endpoint, over windows of frames in which IT plays: is its
        echo still in the mic?

        Decided by evidence that the echo is THERE -- a probe peak inside the
        filter's window, or the room's arrival pattern (`_probe`, `_fine`) --
        not by the filter's own statistics. Those cannot tell a masked echo
        from a vanished one: under a louder Echo Dot (live) the stage's echo
        estimate was -4 dB of the mic and it removed 0.4-0.5 of it; with the
        echo gone and a voice in the room (synthetic) speexdsp's MDF kept
        an estimate at -9 dB of the mic, removing 0.4 of it -- it fits the
        voice from the unrelated reference by chance. Its numbers are kept
        for status (`echo_share_db`, `removal_ratio`), and a remembered delay
        must show them (`PROVISIONAL_*`) or a probe peak to be kept.
        """
        ref.win_mic += mic_pow
        ref.win_in += in_pow
        ref.win_out += out_pow
        ref.win_est += est_pow
        ref.win_frames += 1
        need = (PROVISIONAL_S if ref.provisional else UNHEARD_WINDOW_S) * 100
        if ref.win_frames < need:
            return
        share = 10 * math.log10((ref.win_est + 1e-3) / (ref.win_mic + 1e-3))
        q = (ref.win_in - ref.win_out) / (ref.win_est + 1e-3)
        ref.est_share_db = share
        ref.removal_ratio = q
        heard = ref.heard
        ref.win_mic = ref.win_in = ref.win_out = ref.win_est = 0.0
        ref.win_frames = 0
        ref.heard = False
        if ref.provisional:
            if heard and share >= PROVISIONAL_EST_DB and q >= PROVISIONAL_Q:
                ref.provisional = False
                log.info("remembered delay of %s confirmed", ref.name)
                self._remember(ref)
            else:
                # Not proven within PROVISIONAL_S. That is not proof it is
                # wrong: a louder source in the room masks a correct echo
                # (MEASURED on live replays, round 6 review: masked takes gave
                # -5 dB and 0.50-0.57). The store entry is replaced only when a
                # probe finds the echo somewhere else (`prov_moved`, or the
                # next slot through the probe, which writes its own delay).
                log.info("remembered delay of %s not confirmed -- finding it again%s",
                         ref.name, " (and forgetting it: the echo is elsewhere)"
                         if ref.prov_moved else "")
                self.stats["store_rejected"] += 1
                if ref.prov_moved:
                    self._forget(ref)
                self._unslot(ref)
            return
        ref.bad_windows = 0 if heard else ref.bad_windows + 1
        if ref.bad_windows >= UNHEARD_WINDOWS:
            log.info("echo of %s no longer found in the microphone -- "
                     "reference dropped", ref.name)
            self._unslot(ref)
            ref.unheard_until = self._clock() + UNHEARD_RETRY_S

    def _store_key(self, ref: Ref) -> str | None:
        if self._store is None:
            return None
        pad = self._pad_id()
        return None if not pad else f"{pad}|{ref.id}"

    def _remember(self, ref: Ref, force: bool = True) -> None:
        """Write this endpoint's delay and view to the store: always when
        `force` or the view changed, otherwise only if the delay moved by more
        than STORE_MIN_CHANGE samples, or at all once STORE_DEBOUNCE_S has
        passed since the last write."""
        key = self._store_key(ref)
        if not key or ref.delay is None or ref.provisional:
            return                              # only a confirmed delay is kept
        ms = (ref.delay + ref.adj) * 1000 / FS
        now = self._clock()
        if not force and ref.stored_ms is not None and ref.stored_view == ref.view:
            # what a followed drift explains is not news: only the part of
            # the change beyond it counts as a move
            elapsed = (now - ref.stored_at) * FS
            moved = abs((ms - ref.stored_ms) * FS / 1000 - ref.rate * elapsed)
            if moved <= STORE_MIN_CHANGE and now - ref.stored_at < STORE_DEBOUNCE_S:
                return
        self._store.put(key, ms, ref.view)
        ref.stored_ms, ref.stored_at, ref.stored_view = ms, now, ref.view

    def _forget(self, ref: Ref) -> None:
        key = self._store_key(ref)
        if key:
            self._store.forget(key)

    def _fits(self, ref: Ref) -> bool:
        return (len(self._slots) < MAX_REFS
                and sum(r.nch for r in self._slots) + ref.nch <= MAX_CHANNELS)

    def _slot(self, ref: Ref, provisional: bool = False) -> bool:
        if ref.slotted:
            return True
        now = self._clock()
        while not self._fits(ref):
            # The budget is full. An endpoint nobody has played for a while
            # gives way (longest silent first); its delay stays remembered
            # and it is slotted again on its next sound.
            idle = [r for r in self._slots if now - r.timeline.last_loud >= EVICT_SILENT_S]
            if not idle:
                return False
            victim = min(idle, key=lambda r: r.timeline.last_loud)
            log.info("echo of %s: silent for %.0f s, giving its filter slot to %s",
                     victim.name, now - victim.timeline.last_loud, ref.name)
            self._remember(victim)
            self._unslot(victim)
            victim.store_tried = False
            self.stats["evicted"] += 1
        try:
            if ref.stage is None or ref.stage.nch != ref.nch:
                ref.stage = self._stage_factory(ref.nch)
            else:
                ref.stage.reset()               # a new delay: start clean
        except Exception as e:  # noqa: BLE001
            log.warning("echo filter for %s could not be created: %s", ref.name, e)
            return False
        self._slots.append(ref)
        ref.slotted = True
        ref.provisional = provisional
        ref.prov_moved = False
        ref.quiet_frames = 0
        ref._clear_windows()
        ref._clear_fine()
        self.stats["slotted"] += 1
        return True

    def _unslot(self, ref: Ref) -> None:
        if ref in self._slots:
            self._slots.remove(ref)
        if ref.slotted:
            self.stats["unslotted"] += 1
        ref.slotted = False
        ref.provisional = False
        if ref.stage is not None:
            ref.stage.close()
            ref.stage = None
        ref.delay = None
        ref.view = 0
        ref.votes = []
        ref.track_votes = []
        ref._clear_windows()
        ref._clear_fine()

    # -- the probe ---------------------------------------------------------------

    def tick(self) -> None:
        now = self._clock()
        if self.mic_hist.wpos is None:
            return
        if self._store is not None:
            self._slot_remembered(now)
        for ref in list(self._slots):
            self._fine(ref)
        if now < self._probe_at:
            return
        self._probe_at = now + PROBE_EVERY_S
        refs = [r for r in list(self._refs.values())
                if now - r.timeline.last_loud < 1.0 and now >= r.unheard_until]
        if not refs:
            return
        ref = refs[self._probe_rr % len(refs)]
        self._probe_rr += 1
        self._probe(ref, now)

    def _slot_remembered(self, now: float) -> None:
        """An endpoint with a delay remembered from an earlier run is slotted
        on its first sound (provisionally, see `_watch`)."""
        for ref in list(self._refs.values()):
            if (ref.store_tried and not ref.slotted
                    and now - ref.timeline.last_loud >= STORE_RETRY_SILENT_S):
                ref.store_tried = False         # a new appearance: try again
            if (ref.slotted or ref.store_tried or now < ref.unheard_until
                    or now - ref.timeline.last_loud > 0.5):
                continue
            ref.store_tried = True
            key = self._store_key(ref)
            d = self._store.get(key) if key else None
            if d is None:
                continue
            ref.delay = d * FS / 1000
            if self._slot(ref, provisional=True):
                ref.view = self._store.view(key) if ref.nch == 2 else 0
                self.stats["slotted_from_store"] += 1
                log.info("echo of %s: using its remembered delay %.1f ms%s", ref.name, d,
                         f" ({VIEW_NAMES[ref.view]} channel)" if ref.view else "")
            else:
                ref.delay = None

    def _probe(self, ref: Ref, now: float) -> None:
        W = int(PROBE_WINDOW_S * FS)
        P = self.mic_hist.wpos
        # non-overlapping windows: a proven slotted reference is re-checked
        # every TRACK_EVERY_S, anything else at most once per window
        gap = TRACK_EVERY_S * FS if ref.slotted and not ref.provisional else W
        if ref.last_probe_pos is not None and 0 <= P - ref.last_probe_pos < gap:
            return
        M = int(PROBE_MAX_LAG_S * FS)
        A = PROBE_LEAD_MS * FS // 1000
        base = REF_BASE_MS * FS // 1000
        mic = self.mic_hist.read(P - W, W)
        views = _probe_views(ref.timeline.read(P - W - base - M, W + M + A))
        if (float(np.sqrt(np.mean(views[0][M:M + W] ** 2))) < LOUD_RMS
                or float(np.sqrt(np.mean(mic ** 2))) < MIC_MIN_RMS):
            return
        ref.last_probe_pos = P
        self.stats["probes"] += 1
        agree = PROBE_AGREE_MS * FS // 1000
        # Whether and where the echo is: the best peak of the views. Which view
        # the endpoint is aligned on: by margin (see PROBE_VIEW_MARGIN).
        if ref.slotted:
            # is the echo still where the filter has it? A peak inside the
            # filter's window is far less likely by chance than the highest
            # peak over the whole search range, so a lower bar confirms it
            exp = ref.delay + ref.adj + EARLY_MS * FS // 1000
            near = (exp - TRACK_EARLY_MS * FS // 1000, exp + TRACK_LATE_MS * FS // 1000)
            res = [estimate_lag(mic, v, M, A, near=near) for v in views]
            lag, conf, _ = max(res, key=lambda r: r[1])
            local = [r[2] for r in res]
            if max(local) >= PROBE_CONFIRM_PEAK:
                ref.heard = True
                ref.probe_ok_at = now
                self._rechoose_view(ref, local)
        else:
            res = [estimate_lag(mic, v, M, A, second=True) for v in views]
            lag, conf = max(res, key=lambda r: r[1])[:2]
            # each view's peak where it agrees with the best one: a view
            # whose highest peak is elsewhere did not find this echo
            scores = [r[1] if abs(r[0] - lag) <= agree else 0.0 for r in res]
        ref.lag_conf = conf
        if conf < PROBE_MIN_PEAK:
            if not ref.slotted:
                ref.votes = []
                self._pair_vote(ref, res, agree)
            ref.track_votes = []
            return
        delay = lag - EARLY_MS * FS // 1000
        if not ref.slotted:
            ref.votes = [v for v in ref.votes[-(PROBE_VOTES - 1):]
                         if abs(v[0] - delay) <= agree] + [(delay, scores)]
            if len(ref.votes) >= PROBE_VOTES or conf >= PROBE_SURE_PEAK:
                ref.delay = float(np.median([v[0] for v in ref.votes]))
                if self._slot(ref):
                    ref.view = _choose_view(np.sum([v[1] for v in ref.votes], axis=0))
                    log.info("echo of %s found %.1f ms behind its reference (peak %.1f%s) "
                             "-- cancelling it", ref.name, lag * 1000 / FS, conf,
                             f", {VIEW_NAMES[ref.view]} channel" if ref.view else "")
                    self._remember(ref)
                else:
                    ref.delay = None
            return
        # Tracking a slotted reference. The adaptive filter follows any shift
        # inside its window by itself; moving the read point makes it start
        # over. So move only when the echo is about to leave the window --
        # earlier than TRACK_EARLY_MS (it would become acausal) or later than
        # TRACK_LATE_MS -- and only on two agreeing checks.
        shift = delay - (ref.delay + ref.adj)
        ms = FS / 1000
        if -TRACK_EARLY_MS * ms <= shift <= TRACK_LATE_MS * ms:
            ref.heard = True                    # the echo is where the filter is
            ref.track_votes = []
            return
        ref.track_votes = [v for v in ref.track_votes[-1:] if abs(v - shift) <= agree] + [shift]
        if len(ref.track_votes) >= 2:
            ref.delay += ref.adj + float(np.median(ref.track_votes))
            ref.adj = 0.0
            if ref.provisional:
                ref.prov_moved = True
            ref.track_votes = []
            ref.heard = True
            ref._clear_fine()
            if ref.stage is not None:
                ref.stage.reset()               # its taps belonged to the old delay
            self.stats["delay_moves"] += 1
            log.info("echo of %s moved by %+.1f ms -- following it", ref.name,
                     shift * 1000 / FS)
            self._remember(ref)

    def _rechoose_view(self, ref: Ref, local: list) -> None:
        """A slotted endpoint's view, re-chosen from a probe that heard its
        echo (`local`: each view's peak inside the filter's window): a slot
        taken on mono content (the mix) moves to the one channel a speaker
        plays once the content is stereo, and a wrong or stale view -- from
        the store, or from content that has changed -- corrects itself. The
        arrival pattern is kept: it is the room's, and while the views agreed
        (mono) it was taken on the same signal. One taken on a wrong view
        while they differed does not match and is re-learned
        (`FINE_STALE_WINDOWS`); that session is then left to the adaptive
        filter, and the next one is re-aligned (synthetic: 0.5-3 s after the
        start -11.9 dB, against -3.3 with the view left wrong)."""
        view = _choose_view(local, ref.view)
        if view == ref.view:
            return
        log.info("echo of %s: aligning on its %s now (peaks %s)", ref.name,
                 VIEW_NAMES[view] + (" channel" if view else ""),
                 " / ".join(f"{VIEW_NAMES[i]} {c:.1f}" for i, c in enumerate(local)))
        ref.view = view
        self.stats["view_changes"] += 1
        self._remember(ref)

    def _fine(self, ref: Ref) -> None:
        """Re-locate a slotted endpoint's echo to a fraction of a sample and
        keep it where its filter learned it (see FINE_*)."""
        W = int(FINE_WINDOW_S * FS)
        # the window must lie after this session's mic anchor was frozen
        if self._acc_start < STARTUP_S * FS + W or ref.delay is None:
            return
        P = self.mic_hist.wpos
        if (not ref.fine_due and ref.fine_last is not None
                and 0 <= P - ref.fine_last < FINE_EVERY_S * FS):
            return
        S = FINE_SPAN_MS * FS // 1000
        E = EARLY_MS * FS // 1000
        base = REF_BASE_MS * FS // 1000
        mic = self.mic_hist.read(P - W, W)
        # the mic history is kept at whole-sample positions; the filter reads
        # the reference at the exact (fractional) mapping, so must this
        phi = self.mic.off_f - round(self.mic.off_f)
        q = (P - W) + phi - base - ref.delay - ref.adj
        x = _probe_view(ref.timeline.read(q - E - S, W + 2 * S), ref.view)
        if (float(np.sqrt(np.mean(x[S:S + W] ** 2))) < LOUD_RMS
                or float(np.sqrt(np.mean(mic ** 2))) < MIC_MIN_RMS):
            return
        ref.fine_last = P
        pat = fine_pattern(mic, x, S)
        shift_max = FINE_SHIFT_MS * FS // 1000
        if ref.fine_pat is None:
            # the pattern the filter learns at: taken once two windows in a
            # row agree on it (not from a window the near end dominated).
            # Two windows agreeing on an arrival pattern is also evidence
            # that this endpoint's echo is in the mic at all.
            # (They may differ by a shift: a drifting clock moves the echo
            # by up to DRIFT_MAX_PPM between windows -- 38 samples at 2 s.)
            if ref.fine_cand is not None:
                d, corr = pattern_shift(pat, ref.fine_cand, shift_max)
                gap = (P - (ref.fine_cand_pos or P)) or FINE_EVERY_S * FS
                if corr >= FINE_MIN_CORR and abs(d) <= 2.0 + DRIFT_MAX_PPM * 1e-6 * gap:
                    ref.fine_pat = pat
                    ref.heard = True
            ref.fine_cand = pat
            ref.fine_cand_pos = P
            ref.fine_due = False
            return
        d, corr = pattern_shift(pat, ref.fine_pat, shift_max)
        if corr >= FINE_HEARD_CORR:
            ref.heard = True                    # the room's arrival pattern is there
        if corr < FINE_MIN_CORR:
            ref.fine_pend = None
            ref.fine_stale += 1
            if (ref.fine_stale >= FINE_STALE_WINDOWS
                    and self._clock() - ref.probe_ok_at < 3 * TRACK_EVERY_S
                    and self._echo_dominates(ref)):
                # probes still find the echo but its pattern no longer
                # matches: the room changed. Learn the pattern afresh.
                ref.fine_pat, ref.fine_cand, ref.fine_stale = None, pat, 0
                ref.fine_hist = []
                self.stats["fine_repatterns"] += 1
            return
        ref.fine_stale = 0
        pend, ref.fine_pend = ref.fine_pend, None
        if ref.fine_due:
            # a new session: one clear measurement, or two that agree
            if corr >= FINE_SURE_CORR or (pend is not None and abs(pend - d) <= 1.0):
                self._realign(ref, d if corr >= FINE_SURE_CORR else 0.5 * (d + pend), corr)
                ref.fine_due = False
                ref.fine_hist = []
            else:
                ref.fine_pend = d
            return
        # Where the echo is now, in read-point terms. With the clock rate right
        # these stay put; with drift they lie on a line.
        ref.fine_hist = (ref.fine_hist + [(P - W / 2, ref.adj + d)])[-DRIFT_FIT_N:]
        if self._follow_drift(ref, P):
            return
        # no drift to speak of: a real change is small and lasting, so two
        # measurements in a row must agree on it
        if abs(d) < FINE_MOVE:
            return
        if pend is not None and abs(pend - d) <= 0.5:
            self._realign(ref, 0.5 * (d + pend), corr)
        else:
            ref.fine_pend = d

    def _echo_dominates(self, ref: Ref) -> bool:
        """Is the mic, right now, mostly this endpoint's echo? (Its filter's
        echo estimate over the frames of the current watch window, against
        the mic.) A pattern re-learned while something else is loud in the
        room is that something's pattern: round 6 review, a 12 s mask
        under 100 ppm drift left the cancellation 6 dB worse for good."""
        if ref.win_frames < 50:
            return False
        return 10 * math.log10((ref.win_est + 1e-3) / (ref.win_mic + 1e-3)) >= FINE_RELEARN_SHARE_DB

    def _follow_drift(self, ref: Ref, now_pos: float) -> bool:
        """Fit the recent fine measurements with a line (Theil-Sen). If it
        explains them and shows real motion, adopt its slope as the
        endpoint's rate and put the read point on the line now. True if the
        read point was set here."""
        h = ref.fine_hist
        if len(h) < 3:
            return False
        ps = np.array([x[0] for x in h], float)
        ys = np.array([x[1] for x in h], float)
        span = ps[-1] - ps[0]
        if span < DRIFT_MIN_SPAN_S * FS:
            return False
        i, j = np.triu_indices(ps.size, 1)
        slope = float(np.median((ys[j] - ys[i]) / (ps[j] - ps[i])))
        icpt = float(np.median(ys - slope * ps))
        resid = ys - (icpt + slope * ps)
        if float(np.median(np.abs(resid))) > DRIFT_FIT_TOL or abs(slope) * 1e6 > DRIFT_MAX_PPM:
            return False
        if abs(slope * span) < DRIFT_MIN_MOTION and not ref.rate:
            return False                        # nothing moving: no rate
        target = icpt + slope * now_pos
        if abs(slope - ref.rate) * 1e6 > 0.2:
            log.info("echo of %s drifts %+.1f ppm against the mic -- following it",
                     ref.name, slope * 1e6)
        ref.rate = slope
        if abs(target - ref.adj) >= 0.05:
            self._realign(ref, target - ref.adj, 1.0)
        self.stats["drift_fits"] += 1
        return True

    def _realign(self, ref: Ref, d: float, corr: float) -> None:
        ref.adj += d
        self.stats["fine_moves"] += 1
        log.debug("echo of %s re-aligned by %+.2f samples (corr %.2f)", ref.name, d, corr)
        self._remember(ref, force=False)

    def _pair_vote(self, ref: Ref, res: list, agree) -> bool:
        """A room with two near-equal arrivals splits the GCC peak between
        them, and neither reaches PROBE_MIN_PEAK (synthetic, round 6 review:
        9-11). Two peaks that are both above PROBE_PAIR_PEAK and stay at the
        same two places over PROBE_PAIR_VOTES windows in a row are as good as
        one strong peak (unrelated audio puts its peaks somewhere new every
        window); the EARLIER one is the delay, so both arrivals are inside
        the filter. `res`: each view's `estimate_lag(..., second=True)`; the
        pair is the best view's, the view aligned on is chosen as for a
        single peak (each view scored where its highest peak is one of the
        pair). True if the endpoint was slotted."""
        lag, conf, lag2, conf2 = max(res, key=lambda r: r[1])
        if (conf < PROBE_PAIR_PEAK or conf2 < PROBE_PAIR_PEAK
                or abs(lag - lag2) > PROBE_PAIR_SPAN_MS * FS // 1000):
            ref.pair_votes = []
            return False
        pair = (min(lag, lag2), max(lag, lag2))
        scores = [r[1] if min(abs(r[0] - lag), abs(r[0] - lag2)) <= agree else 0.0 for r in res]
        keep = [v for v in ref.pair_votes[-(PROBE_PAIR_VOTES - 1):]
                if abs(v[0][0] - pair[0]) <= agree and abs(v[0][1] - pair[1]) <= agree]
        ref.pair_votes = keep + [(pair, scores)]
        if len(ref.pair_votes) < PROBE_PAIR_VOTES:
            return False
        ref.delay = float(np.median([v[0][0] for v in ref.pair_votes])) - EARLY_MS * FS // 1000
        view = _choose_view(np.sum([v[1] for v in ref.pair_votes], axis=0))
        ref.pair_votes = []
        if not self._slot(ref):
            ref.delay = None
            return False
        ref.view = view
        self.stats["pair_slotted"] += 1
        log.info("echo of %s found as two arrivals %.1f and %.1f ms behind its reference "
                 "(peaks %.1f, %.1f) -- cancelling it", ref.name, pair[0] * 1000 / FS,
                 pair[1] * 1000 / FS, conf, conf2)
        self._remember(ref)
        return True

    # -- reporting -----------------------------------------------------------------

    def status(self) -> dict:
        lb = self._loopback
        alive = self.alive
        degraded = self.mode != "aec" or (self._thread is not None and not alive)
        return {"mode": self.mode, "reason": self.reason, "active": self.active,
                "health": "degraded" if degraded else "ok", "worker_alive": alive,
                "slots": [r.name for r in self._slots],
                "references": [r.describe() for r in list(self._refs.values())],
                "loopback": None if lb is None else {
                    "endpoints": sorted(v[0] for v in lb.open_endpoints.values()),
                    "failed": lb.failed, **lb.stats},
                **{k: (round(v, 1) if isinstance(v, float) else v)
                   for k, v in self.stats.items()}}
