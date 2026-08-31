"""`BlackBox` — a flight recorder for the seconds before a Bluetooth link death.

Why this exists. On 2026-08-31 a reproducible failure was chased: with The
Last of Us Part I running, attaching the virtual pad made the *physical*
controller power itself off within seconds — a behaviour no other tested game
triggers, and one that TLOU itself does not trigger over plain Bluetooth or a
real cable. The killer is therefore something in what the host sends a newly
arrived wired DualSense, as this bridge translates it onto the Bluetooth link.
Debugging that from logs alone failed twice: each reproduction costs a manual
PS-button press, and the interesting bytes are exactly the ones nobody was
printing. So: record everything cheap, all the time, in a bounded ring, and
write it to disk the moment the link dies.

Design rules, in order:

  * **Recording must be safe on the hot paths.** `record()` is one tuple
    append to a bounded `collections.deque` (thread-safe under the GIL) plus
    at most one `bytes()` copy. No formatting, no I/O, no locks on the record
    side; the hex rendering happens at dump time, off the ring's snapshot.
  * **The ring is time, not truth.** `maxlen` entries at the observed rates
    (~1000/s of iso audio summaries + ~50/s of Bluetooth writes + control
    traffic) keep well over ten seconds of history — enough to hold the whole
    life of a bridge that dies at attach, which is the scenario this was
    built for.
  * **A dump must never make things worse.** `dump()` snapshots the deque,
    formats and writes outside any lock the backend holds, and swallows its
    own I/O errors: the flight recorder must not crash the aircraft.

Wiring: `BridgeBackend` exposes a `blackbox` attribute (None by default) and
calls `record()` at every host->device seam — USB output reports, feature
reads/writes, interface/UAC control, audio-out summaries — and at every
post-translation Bluetooth write. `python -m ds5emu serve --capture PATH`
turns it on; the backend dumps automatically when the link dies and once more
at shutdown.
"""

from __future__ import annotations

import threading
import time
from collections import deque

__all__ = ["BlackBox"]


def _hex(data: bytes, limit: int = 96) -> str:
    """`data` as spaced hex, elided in the middle beyond `limit` bytes."""
    if len(data) <= limit:
        return data.hex(" ")
    head = data[: limit - 16].hex(" ")
    tail = data[-16:].hex(" ")
    return f"{head} ..[{len(data) - limit} more].. {tail}"


class BlackBox:
    """Bounded ring of timestamped events, dumped to a file on demand.

    One instance is shared by every thread of a backend. `record()` may be
    called from any thread at any rate; `dump()` may be called from any
    thread and writes `<path_prefix>-NN-<reason>.txt`.
    """

    def __init__(self, path_prefix: str, capacity: int = 30000):
        self.path_prefix = path_prefix
        self._ring: deque = deque(maxlen=capacity)
        self._t0 = time.perf_counter()
        self._wall0 = time.time()
        #: Serialises dumps only — never taken by `record()`.
        self._dump_lock = threading.Lock()
        self.dumps = 0
        #: Filled by the owner with a zero-argument callable returning extra
        #: header lines (stats snapshot, device status) for each dump.
        self.context_fn = None

    # -- recording (hot path) ------------------------------------------------

    def record(self, kind: str, data: bytes | None = None, note: str = "") -> None:
        """Append one event. `data`, when given, is copied (callers reuse
        buffers); everything else is deferred to dump time."""
        self._ring.append((
            time.perf_counter(),
            threading.current_thread().name,
            kind,
            None if data is None else bytes(data),
            note,
        ))

    # -- dumping -------------------------------------------------------------

    def dump(self, reason: str) -> str | None:
        """Write everything currently in the ring; returns the path or None.

        Never raises: a flight recorder that can take down the bridge while
        the bridge is busy dying would defeat its purpose.
        """
        with self._dump_lock:
            self.dumps += 1
            n = self.dumps
        snapshot = list(self._ring)   # atomic enough: item tuples are immutable
        slug = "".join(c if c.isalnum() or c in "-_" else "-" for c in reason)[:40]
        path = f"{self.path_prefix}-{n:02d}-{slug}.txt"
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(f"# BlackBox dump {n}: {reason}\n")
                fh.write(f"# wall time now: {time.strftime('%Y-%m-%d %H:%M:%S')}"
                         f"  (t0 was {time.strftime('%H:%M:%S', time.localtime(self._wall0))};"
                         f" +t below is seconds since t0)\n")
                fh.write(f"# entries: {len(snapshot)} (ring capacity {self._ring.maxlen})\n")
                if self.context_fn is not None:
                    try:
                        for line in self.context_fn():
                            fh.write(f"# {line}\n")
                    except Exception as e:  # noqa: BLE001
                        fh.write(f"# context_fn failed: {e!r}\n")
                fh.write("#\n")
                for t, thread, kind, data, note in snapshot:
                    rel = t - self._t0
                    parts = [f"+{rel:11.4f}", f"{thread:<16}", f"{kind:<18}"]
                    if note:
                        parts.append(note)
                    if data is not None:
                        parts.append(f"({len(data)}B) {_hex(data)}")
                    fh.write("  ".join(parts) + "\n")
            return path
        except Exception:  # noqa: BLE001  — see the docstring
            return None
