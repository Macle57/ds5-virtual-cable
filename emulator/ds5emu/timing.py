"""Device-side timing: the USB frame clock, endpoint pacing and measurement.

WHY THIS MODULE EXISTS (Phase 3, experiment E1 — read this before changing it)
-----------------------------------------------------------------------------
On a real USB bus the *host controller* paces isochronous traffic: one packet
per (micro)frame, driven by SOF. Over USB/IP + UDE there is no SOF at all.
``usbip2_filter.sys`` fakes ``QueryBusTime`` with a constant frame number (that
is the whole reason ``USBAUDIO.SYS`` works at all over UDE — see
docs/virtualization-options.md §2.3), so nothing between ``usbaudio.sys`` and us
imposes a rate. The stream therefore runs exactly as fast as **we** complete
URBs.

Measured on this machine, 2026-08-24, before this module existed: playing a
48 kHz stream to the attached virtual device consumed **174 429 frames/s**
instead of 48 000 — 3.63x real time — with zero PortAudio underruns, because the
emulator answered every isochronous OUT URB the instant it arrived.

So: **the emulator is the audio clock master.** It must complete an isochronous
URB of N packets N milliseconds after the endpoint's previous packet was due,
not immediately. That is what `FrameClock` does.

Consequences worth remembering:

* The `bInterval`/sync-type fields of the real descriptors (adaptive OUT,
  asynchronous IN) do not pace anything here; they only have to be *plausible*
  so UDE and usbaudio configure the pipes correctly.
* Because we own the clock, both directions are automatically rate-locked to
  each other and to the Bluetooth side's 45 kHz consumption rate later on.
* Nothing about this is specific to the synthetic backend — `BridgeBackend`
  needs exactly the same pacing.
"""

from __future__ import annotations

import ctypes
import statistics
import time
from collections import deque
from itertools import islice
from dataclasses import dataclass, field

#: Isochronous service intervals per second. Both audio endpoints declare
#: bInterval = 4 which, at high speed, is 2^(4-1) = 8 microframes = 1 ms.
FRAMES_PER_SECOND = 1000


class FrameClock:
    """A monotonic 1 kHz frame counter plus a per-endpoint frame allocator.

    `reserve(ep, n)` hands out `n` consecutive service intervals on `ep`,
    never earlier than "now", and returns the frame the first packet lands in
    together with the `time.perf_counter()` deadline at which the last packet
    has been serviced. The caller completes the URB at that deadline.

    Reserving from `max(now, next_free)` means a host that runs ahead (usbaudio
    keeps several URBs in flight) gets paced, while a host that falls behind is
    never made to wait for frames that have already gone by — the endpoint
    simply resynchronises, which is what a real isochronous endpoint does.
    """

    def __init__(self, hz: int = FRAMES_PER_SECOND, t0: float | None = None):
        self.period = 1.0 / hz
        self.t0 = time.perf_counter() if t0 is None else t0
        self._next: dict[int, int] = {}
        #: Times the endpoint ran out of reserved frames and had to restart from
        #: "now". THIS IS THE UNDERRUN COUNTER. One resync is expected when a
        #: stream starts; more than that mid-stream means the host could not
        #: keep the pipe fed and the audio actually glitched.
        self.resyncs: dict[int, int] = {}
        #: Seconds since t0 at which each resync happened, so "was that an
        #: underrun or just the stream starting?" is answerable after the fact.
        self.resync_times: dict[int, list[float]] = {}

    def current_frame(self) -> int:
        return int((time.perf_counter() - self.t0) / self.period)

    def frame_time(self, frame: int) -> float:
        return self.t0 + frame * self.period

    def reserve(self, ep: int, n_frames: int) -> tuple[int, float]:
        """-> (start_frame, completion deadline as perf_counter seconds)"""
        if n_frames <= 0:
            now = self.current_frame()
            return now, self.frame_time(now)
        now = self.current_frame()
        queued = self._next.get(ep, 0)
        if now >= queued:
            self.resyncs[ep] = self.resyncs.get(ep, 0) + 1
            self.resync_times.setdefault(ep, []).append(now * self.period)
            start = now
        else:
            start = queued
        self._next[ep] = start + n_frames
        return start, self.frame_time(start + n_frames)

    def reset_endpoint(self, ep: int) -> None:
        self._next.pop(ep, None)

    def backlog_frames(self, ep: int) -> int:
        """How far ahead of real time this endpoint is currently reserved."""
        return max(0, self._next.get(ep, 0) - self.current_frame())


@dataclass
class UrbRecord:
    t_recv: float
    t_done: float
    packets: int
    nbytes: int
    start_frame: int


class UrbMeter:
    """Per-endpoint URB bookkeeping — the raw material for the E1 verdict.

    Deliberately cheap: two floats and two ints appended per URB, ~100 URBs/s
    per isochronous endpoint. `maxlen` bounds memory on a long run.

    READING IT MUST BE CHEAP TOO (Phase 3c). `summary()` used to sort every gap
    it had ever recorded — tens of thousands after a few minutes — which took
    ~15 ms. `--stats-every 2` therefore blocked the emulator for 15 consecutive
    1 ms service intervals every 2 seconds, and the frame clock dutifully
    resynchronised. The resync times came out spaced at *exactly* 2.00 s:

        --stats-every 2, summarising everything   iso OUT resyncs 41
        no snapshot at all                        iso OUT resyncs 1 (stream start)

    **Moving the snapshot to a thread does not fix this**, which is worth
    knowing: `sorted()` on a list of floats is one C call that never releases
    the GIL, so the event loop is blocked wherever the work runs. The fix is to
    make it cheap. Totals are now running counters, exact and O(1); the
    distribution stats are computed over the last `window` records only.
    """

    #: How many recent URBs the distribution stats look at. 4000 iso URBs is
    #: ~40 s of history at the usbaudio burst rate, and sorting 4000 floats is
    #: ~0.2 ms — under one service interval, so it cannot cost a frame.
    SUMMARY_WINDOW = 4000

    def __init__(self, maxlen: int = 400_000):
        self.records: dict[int, deque[UrbRecord]] = {}
        self.maxlen = maxlen
        self.t0 = time.perf_counter()
        #: Exact cumulative totals, maintained incrementally so that bounding
        #: the summary window never makes a *count* wrong.
        self.totals: dict[int, dict] = {}

    def record(self, ep: int, rec: UrbRecord) -> None:
        d = self.records.get(ep)
        if d is None:
            d = self.records[ep] = deque(maxlen=self.maxlen)
            self.totals[ep] = {"urbs": 0, "packets": 0, "bytes": 0,
                               "t_first": rec.t_recv, "t_last": rec.t_recv}
        d.append(rec)
        t = self.totals[ep]
        t["urbs"] += 1
        t["packets"] += rec.packets
        t["bytes"] += rec.nbytes
        t["t_last"] = rec.t_recv

    def endpoints(self) -> list[int]:
        return sorted(self.records)

    def records_snapshot(self, ep: int, window: int | None = -1) -> list:
        """A stable copy of the last `window` records for one endpoint.

        `window=None` means everything, which is only for offline analysis —
        never call it from a live server (see the class docstring). The default
        is `SUMMARY_WINDOW`.

        `itertools.islice` over the deque takes only the tail without copying
        the whole thing. The snapshot may run while the event loop is still
        appending, and iterating a `deque` during mutation raises
        `RuntimeError`, so retry — the window is microseconds wide and a
        second attempt has always been enough.
        """
        if window == -1:
            window = self.SUMMARY_WINDOW
        for _ in range(4):
            try:
                d = self.records.get(ep, ())
                if window is None or len(d) <= window:
                    return list(d)
                return list(islice(d, len(d) - window, len(d)))
            except RuntimeError:      # "deque mutated during iteration"
                continue
        return []

    def summary(self, ep: int) -> dict:
        # Counts come from the exact running totals; only the distribution is
        # computed over the recent window, so bounding the work never makes a
        # reported count wrong.
        tot = self.totals.get(ep)
        recs = self.records_snapshot(ep)
        if tot is None or len(recs) < 2:
            n = tot["urbs"] if tot else len(recs)
            return {"urbs": n,
                    "packets": tot["packets"] if tot else 0,
                    "bytes": tot["bytes"] if tot else 0}
        span = tot["t_last"] - tot["t_first"]
        gaps = [b.t_recv - a.t_recv for a, b in zip(recs, recs[1:])]
        gaps_sorted = sorted(gaps)
        packets = tot["packets"]
        nbytes = tot["bytes"]
        # Late completions: the deadline we promised vs. when we actually
        # finished the work. `t_done` is stamped after the reply is handed to
        # the socket, so this is the honest end-to-end server-side lateness.
        return {
            "urbs": tot["urbs"],
            "packets": packets,
            "bytes": nbytes,
            "span_s": span,
            "gap_window_urbs": len(recs),
            "urbs_per_s": tot["urbs"] / span if span else 0.0,
            "packets_per_s": packets / span if span else 0.0,
            "bytes_per_s": nbytes / span if span else 0.0,
            "packets_per_urb_mean": packets / tot["urbs"],
            "gap_ms_median": statistics.median(gaps) * 1e3,
            "gap_ms_p99": gaps_sorted[int(len(gaps_sorted) * 0.99)] * 1e3,
            "gap_ms_max": gaps_sorted[-1] * 1e3,
            "gap_ms_min": gaps_sorted[0] * 1e3,
            # NOT an underrun count. usbaudio submits URBs in bursts of one or
            # two every ~15 ms (the Windows audio engine period), so a 15 ms gap
            # after a burst carrying 20 ms of audio is perfectly healthy. The
            # real underrun signal is FrameClock.resyncs.
            "bursts": 1 + sum(1 for g in gaps if g > 1e-3),
        }

    def gap_histogram(self, ep: int, edges_ms=(1, 2, 4, 8, 12, 16, 24, 32, 64)) -> list:
        recs = self.records_snapshot(ep)
        gaps = [(b.t_recv - a.t_recv) * 1e3 for a, b in zip(recs, recs[1:])]
        out = []
        prev = 0.0
        for e in edges_ms:
            out.append((prev, e, sum(1 for g in gaps if prev <= g < e)))
            prev = float(e)
        out.append((prev, float("inf"), sum(1 for g in gaps if g >= prev)))
        return out


class TimerResolution:
    """`timeBeginPeriod(1)` for the life of the block.

    Windows' default timer granularity is ~15.6 ms, which makes every
    `asyncio.sleep()` shorter than that useless — and this module's whole job is
    sleeping for ~1 ms at a time. Mirrors `prototype/ds5bridge/pacing.py`
    (duplicated rather than imported: `emulator/` is stdlib-only by design).
    """

    def __init__(self, ms: int = 1):
        self.ms = ms
        self._winmm = None

    def __enter__(self):
        try:
            self._winmm = ctypes.WinDLL("winmm")
            self._winmm.timeBeginPeriod(self.ms)
        except Exception:  # pragma: no cover - non-Windows
            self._winmm = None
        return self

    def __exit__(self, *exc):
        if self._winmm is not None:
            try:
                self._winmm.timeEndPeriod(self.ms)
            except Exception:  # pragma: no cover
                pass
        self._winmm = None
        return False
