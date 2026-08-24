"""What happens to a game when the Bluetooth controller goes away mid-session?

STATUS.md 17.6(6) lists reconnect as "implemented, never force-tested"
(`reconnects` stayed 0 through all of Phase 3). This forces it, with the
virtual device attached and a reader on it behaving exactly like a game.

What is scriptable and what is not
----------------------------------
A long PS-button press is not scriptable, and neither is walking out of
Bluetooth range. What IS scriptable is closing the HID handle underneath the
reader thread (`BridgeBackend.force_disconnect()`), which produces the same
sequence of events the bridge sees for a real dropout: the next `hid.read()`
raises, `connected` clears, the reconnect loop reopens by BD address, and the
mic is re-armed. What it does NOT reproduce is the device leaving Windows'
enumeration entirely -- so the reopen here always succeeds on the first try,
whereas a genuinely switched-off controller makes `_pick_device()` raise until
it comes back. Both paths run the same 2 s-backoff loop; only the number of
laps differs. The manual test for the rest is at the bottom of this docstring.

The four questions, and the answers this asserts:

  1. Does the virtual device survive?   `usbip port` must still list it, and the
                                        HID handle a game holds must stay valid.
  2. Do reports keep flowing?           A wired DualSense emits one every 4 ms.
                                        Going silent reads as "controller
                                        removed" and pauses the game.
  3. Do they stop pressing things?      Anything held at the moment of the drop
                                        must be released within INPUT_NEUTRAL_S.
  4. Does live input come back?         Without the user doing anything.

MANUAL TEST, for the half that cannot be scripted:
  1. `ds5bridge` and start a game.
  2. Hold the PS button ~10 s until the light bar goes out.
  3. The game must keep running with a centred, unpressed controller.
  4. Press PS once. The controller reconnects and the game responds again.
  5. Ctrl+C. `usbip port` prints nothing.
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from collections import deque
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
REPO = APP_DIR.parent
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(REPO / "emulator"))

from ds5app import service as S            # noqa: E402
from ds5app.usbip import Usbip             # noqa: E402

import hid                                 # noqa: E402
from ds5bridge import protocol as P        # noqa: E402
from ds5emu import bridge as B             # noqa: E402

FAILURES: list[str] = []


def say(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def check(name: str, cond: bool, detail: str = "") -> bool:
    print(f"    {'PASS' if cond else 'FAIL'}  {name}{'  ' + detail if detail else ''}",
          flush=True)
    if not cond:
        FAILURES.append(name)
    return cond


def usb_hid_paths() -> set[str]:
    out = set()
    for d in hid.enumerate(0x054C, 0x0CE6):
        p = d["path"].decode("latin1") if isinstance(d["path"], bytes) else d["path"]
        if "00001124" not in p:            # not the Bluetooth HID profile GUID
            out.add(p)
    return out


class GameReader(threading.Thread):
    """A reader on the VIRTUAL device that behaves like a game: 250 Hz polls,
    and it records what it was handed."""

    def __init__(self, path: str):
        super().__init__(name="game-reader", daemon=True)
        self.path = path
        self.stop_flag = threading.Event()
        self.lock = threading.Lock()
        self.samples: deque = deque(maxlen=200_000)   # (t, state or None)
        self.reads = 0
        self.empty = 0
        self.errors = 0
        self.fatal: str | None = None

    def run(self) -> None:
        d = hid.device()
        try:
            d.open_path(self.path.encode("latin1")
                        if isinstance(self.path, str) else self.path)
        except Exception as e:              # noqa: BLE001
            self.fatal = f"open failed: {e}"
            return
        try:
            while not self.stop_flag.is_set():
                try:
                    raw = bytes(d.read(64, timeout_ms=50))
                except Exception as e:      # noqa: BLE001
                    self.errors += 1
                    if self.errors > 200:
                        self.fatal = f"read failed {self.errors}x: {e}"
                        return
                    continue
                self.reads += 1
                if not raw:
                    self.empty += 1
                    continue
                st = P.decode_input(raw[1:], usb=True)
                if st is not None:
                    with self.lock:
                        self.samples.append((time.perf_counter(), st))
        finally:
            try:
                d.close()
            except Exception:               # noqa: BLE001
                pass

    def window(self, t0: float, t1: float) -> list:
        with self.lock:
            return [s for t, s in self.samples if t0 <= t <= t1]

    def count_between(self, t0: float, t1: float) -> int:
        with self.lock:
            return sum(1 for t, _ in self.samples if t0 <= t <= t1)


def moving(st) -> bool:
    """Is any control actuated? The neutral report must answer no to all."""
    return (st.lx != 0x80 or st.ly != 0x80 or st.rx != 0x80 or st.ry != 0x80
            or st.l2 or st.r2 or st.dpad != "-" or any(st.buttons.values())
            or st.gyro != (0, 0, 0))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--serial", default=None)
    ap.add_argument("--port", type=int, default=3241)
    ap.add_argument("--cycles", type=int, default=3)
    ap.add_argument("--settle", type=float, default=6.0,
                    help="seconds of healthy running before each disconnect")
    ap.add_argument("--down", type=float, default=6.0,
                    help="seconds to observe the disconnected state")
    ap.add_argument("--hold", type=float, default=5.0,
                    help="seconds to keep the reconnect from succeeding, so the "
                         "down-state behaviour is actually observable. 0 measures "
                         "the bare reopen latency instead")
    a = ap.parse_args()

    u = Usbip(port=a.port)
    S.cleanup(a.port, u.exe, log_fn=lambda t: print("    " + t, flush=True))

    before = usb_hid_paths()
    svc = S.BridgeService(serial=a.serial, port=a.port,
                          on_event=lambda k, t: say(f"  {k}: {t}"))
    reader: GameReader | None = None
    try:
        svc.start()
        time.sleep(2.0)
        new = usb_hid_paths() - before
        if not check("the virtual HID device appeared", bool(new),
                     f"{len(new)} new path(s)"):
            return 1
        vpath = sorted(new)[0]
        say(f"virtual HID: {vpath[:90]}")
        reader = GameReader(vpath)
        reader.start()

        backend = svc._backend
        for cycle in range(1, a.cycles + 1):
            say(f"--- cycle {cycle}/{a.cycles} ---")
            time.sleep(a.settle)
            if reader.fatal:
                check(f"cycle {cycle}: the game's handle survived", False, reader.fatal)
                break

            pre_reconnects = backend.stats["reconnects"]
            pre_neutral = backend.stats["input_neutral"]
            t_drop = time.perf_counter()
            backend.force_disconnect(hold_s=a.hold)
            say(f"  link dropped (held down {a.hold:.0f}s)")

            # 1. the virtual device must NOT go away
            time.sleep(1.0)
            ports = u.our_ports()
            check(f"cycle {cycle}: still attached through the drop", len(ports) == 1,
                  f"ports={ports}")

            # wait out the observation window, noting the exact instant the
            # backend got the controller back
            t_reconnect = None
            while time.perf_counter() - t_drop < a.down + 1.0:
                if (t_reconnect is None
                        and backend.stats["reconnects"] > pre_reconnects):
                    t_reconnect = time.perf_counter()
                time.sleep(0.02)
            t_down_end = time.perf_counter()
            if t_reconnect is not None:
                say(f"  link back {t_reconnect - t_drop:.2f}s after the drop "
                    f"(held down {a.hold:.1f}s, so {t_reconnect - t_drop - a.hold:.2f}s "
                    f"to notice and reopen)")

            # 2. reports kept flowing at the wired cadence
            n = reader.count_between(t_drop + 1.5, t_down_end)
            span = t_down_end - (t_drop + 1.5)
            rate = n / span if span else 0
            check(f"cycle {cycle}: reports kept flowing while down",
                  rate > 150, f"{rate:.1f}/s over {span:.1f}s")

            # 3. and they were neutral -- measured only while the link is
            # actually held down. Live reports resume the instant it reopens and
            # a resting controller still has non-zero gyro, so a window that
            # runs past the reconnect counts real input as a failure.
            if a.hold >= B.INPUT_NEUTRAL_S + 1.5:
                neutral_end = min(t_down_end, t_drop + a.hold - 0.5)
                win = reader.window(t_drop + B.INPUT_NEUTRAL_S + 0.5, neutral_end)
                actuated = sum(1 for st in win if moving(st))
                check(f"cycle {cycle}: every report was neutral while down",
                      bool(win) and actuated == 0,
                      f"{actuated} actuated of {len(win)}")
                check(f"cycle {cycle}: the neutral counter moved",
                      backend.stats["input_neutral"] > pre_neutral,
                      f"+{backend.stats['input_neutral'] - pre_neutral}")
            else:
                # A blip shorter than INPUT_NEUTRAL_S must NOT neutralise --
                # that is the point of the threshold, not a gap in the test.
                say(f"  (hold {a.hold:.1f}s < INPUT_NEUTRAL_S+1.5; measuring "
                    f"reopen latency only)")
                check(f"cycle {cycle}: a sub-threshold blip did not neutralise",
                      backend.stats["input_neutral"] == pre_neutral,
                      f"+{backend.stats['input_neutral'] - pre_neutral}")
            # How long the game held a stale, pressed report before release.
            live = reader.window(t_drop, t_drop + 3.0)
            first_neutral = next((i for i, st in enumerate(live) if not moving(st)),
                                 None)
            if first_neutral is not None:
                with reader.lock:
                    times = [t for t, _ in reader.samples
                             if t_drop <= t <= t_drop + 3.0]
                say(f"  controls released {times[first_neutral] - t_drop:.2f}s "
                    f"after the drop (INPUT_NEUTRAL_S = {B.INPUT_NEUTRAL_S}s)")

            # 4. it came back on its own
            t_wait = time.perf_counter()
            while (time.perf_counter() - t_wait < 30
                   and backend.stats["reconnects"] == pre_reconnects):
                time.sleep(0.25)
            took = time.perf_counter() - t_drop
            back = backend.stats["reconnects"] > pre_reconnects
            check(f"cycle {cycle}: reconnected without help", back,
                  f"after {took:.1f}s")
            if back:
                svc.snapshot()          # prime the rate differencer
                time.sleep(2.0)
                st = svc.snapshot()
                check(f"cycle {cycle}: live again", st["connected"]
                      and st["reports_per_s"] > 150,
                      f"{st['reports_per_s']:.0f} reports/s, "
                      f"battery {st['battery_percent']}%")

        say("final backend counters:")
        for k in ("bt_reports", "bt_read_errors", "input_delivered",
                  "input_repeated", "input_neutral", "disconnects", "reconnects",
                  "link_watchdog_trips"):
            print(f"      {k:22s} {backend.stats[k]}", flush=True)
        if reader:
            print(f"      {'game reads':22s} {reader.reads} "
                  f"(empty {reader.empty}, errors {reader.errors}, "
                  f"fatal {reader.fatal})", flush=True)
            check("the game's HID handle never died", reader.fatal is None,
                  reader.fatal or "")
    finally:
        if reader:
            reader.stop_flag.set()
            reader.join(timeout=5)
        svc.stop()

    print()
    if FAILURES:
        print(f"RESULT: FAIL ({len(FAILURES)}): " + "; ".join(FAILURES))
        return 1
    print("RESULT: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
