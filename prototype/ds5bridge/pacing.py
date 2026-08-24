"""Clock-driven frame pacing.

Naive `sleep(0.010667)` drifts badly on Windows (default timer granularity is
~15.6 ms, and every sleep overshoots). Instead we run a monotonic-clock catch-up
loop, like the tester's player: at each tick we compute how many frames *should*
have been emitted by now and emit exactly that many, dropping backlog beyond a
cap so a stall doesn't produce a burst that overruns the controller's buffer.
"""

from __future__ import annotations

import ctypes
import time
from dataclasses import dataclass, field


class TimerResolution:
    """Raise Windows timer resolution to 1 ms for the duration of the block."""

    def __init__(self, ms: int = 1):
        self.ms = ms
        self.ok = False

    def __enter__(self) -> "TimerResolution":
        try:
            self.ok = ctypes.windll.winmm.timeBeginPeriod(self.ms) == 0
        except Exception:  # noqa: BLE001
            self.ok = False
        return self

    def __exit__(self, *exc) -> None:
        if self.ok:
            try:
                ctypes.windll.winmm.timeEndPeriod(self.ms)
            except Exception:  # noqa: BLE001
                pass


@dataclass
class Pacer:
    """Emit frames at `frame_ms` intervals, measured against a monotonic clock.

    frames_due() returns how many frames to emit now (0..max_burst).
    Frames further behind than `max_backlog` are dropped and counted.
    """

    frame_ms: float
    max_backlog: int = 8
    max_burst: int = 4
    start: float = field(default_factory=time.perf_counter)
    emitted: int = 0
    dropped: int = 0

    def reset(self) -> None:
        self.start = time.perf_counter()
        self.emitted = 0
        self.dropped = 0

    @property
    def elapsed(self) -> float:
        return time.perf_counter() - self.start

    def frames_due(self) -> int:
        target = int(self.elapsed * 1000.0 / self.frame_ms)
        behind = target - self.emitted
        if behind <= 0:
            return 0
        if behind > self.max_backlog:
            drop = behind - self.max_backlog
            self.dropped += drop
            self.emitted += drop
            behind = self.max_backlog
        return min(behind, self.max_burst)

    def commit(self, n: int = 1) -> None:
        self.emitted += n

    def sleep_until_next(self) -> None:
        """Sleep just short of the next frame boundary, then spin."""
        next_at = self.start + (self.emitted + 1) * self.frame_ms / 1000.0
        now = time.perf_counter()
        delta = next_at - now
        if delta <= 0:
            return
        if delta > 0.0015:
            time.sleep(delta - 0.001)
        # sleep(0) rather than a bare `pass`: a tight `pass` loop never releases the
        # GIL, which starves other Python threads. That is not theoretical -- it made
        # a concurrent PortAudio capture return near-silence while this loop paced
        # audio out, and looked exactly like a hardware failure.
        while time.perf_counter() < next_at:
            time.sleep(0)

    def stats(self) -> str:
        el = self.elapsed
        rate = self.emitted / el if el > 0 else 0.0
        return (
            f"{self.emitted} frames in {el:.2f}s = {rate:.2f} fps "
            f"(target {1000.0/self.frame_ms:.2f}), dropped {self.dropped}"
        )


class RateMeter:
    """Measure observed report rate with a sliding window."""

    def __init__(self, window: float = 1.0):
        self.window = window
        self.times: list[float] = []
        self.total = 0
        self.start = time.perf_counter()

    def tick(self) -> None:
        now = time.perf_counter()
        self.total += 1
        self.times.append(now)
        cutoff = now - self.window
        while self.times and self.times[0] < cutoff:
            self.times.pop(0)

    @property
    def hz(self) -> float:
        if len(self.times) < 2:
            return 0.0
        span = self.times[-1] - self.times[0]
        return (len(self.times) - 1) / span if span > 0 else 0.0

    @property
    def mean_hz(self) -> float:
        el = time.perf_counter() - self.start
        return self.total / el if el > 0 else 0.0
