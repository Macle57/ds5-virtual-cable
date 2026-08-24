"""Battery surfacing, tested against a fake backend rather than a flat controller.

The one thing this project has been fooled by in every single phase is a
DualSense below ~15 %, which produces dropouts and timeouts that read exactly
like protocol bugs. So the warning has to fire, has to be loud, and must not
cry wolf while the thing is charging.
"""

from __future__ import annotations

import time
import unittest

from ds5app import controller as C


class FakeBackend:
    def __init__(self, *levels):
        self.levels = list(levels)
        self.calls = 0

    def device_status(self) -> dict:
        pct, state = self.levels[min(self.calls, len(self.levels) - 1)]
        self.calls += 1
        return {"battery_percent": pct, "battery_state": state,
                "connected": True, "serial": "aabbccddeeff", "stale_s": 0.01}


class BatteryNoteTests(unittest.TestCase):
    def test_healthy(self):
        self.assertEqual(C.battery_note(70, "discharging"), "battery 70%")

    def test_unknown(self):
        self.assertEqual(C.battery_note(None), "battery unknown")

    def test_low_is_called_low(self):
        self.assertIn("LOW", C.battery_note(20, "discharging"))
        self.assertIn("LOW", C.battery_note(18, "discharging"))

    def test_critical_says_what_it_will_look_like(self):
        note = C.battery_note(10, "discharging")
        self.assertIn("CRITICAL", note)
        # The whole point of the message: pre-empt the wrong diagnosis.
        self.assertIn("look exactly like software faults", note)

    def test_charging_is_never_a_warning(self):
        for pct in (0, 5, 10, 20):
            note = C.battery_note(pct, "charging")
            self.assertNotIn("LOW", note)
            self.assertNotIn("CRITICAL", note)

    def test_the_boundary_is_where_the_constants_say(self):
        self.assertIn("LOW", C.battery_note(C.BATTERY_WARN_PERCENT, "discharging"))
        self.assertEqual(C.battery_note(C.BATTERY_WARN_PERCENT + 10, "discharging"),
                         f"battery {C.BATTERY_WARN_PERCENT + 10}%")
        self.assertIn("CRITICAL",
                      C.battery_note(C.BATTERY_CRITICAL_PERCENT, "discharging"))


class BatteryWatcherTests(unittest.TestCase):
    """Fast intervals; the watcher's first reading is deliberately delayed 5 s,
    so these poke `_loop`'s body through short waits rather than waiting it out."""

    def run_watcher(self, backend, samples: int, interval: float = 0.02):
        reports, warns = [], []
        w = C.BatteryWatcher(backend, on_report=lambda p, s: reports.append((p, s)),
                             on_warn=lambda p, s: warns.append((p, s)),
                             interval=interval)
        w._stop.wait = lambda t: False       # skip the 5 s settle and the sleeps
        t = __import__("threading").Thread(target=w._loop, daemon=True)
        t.start()
        deadline = time.monotonic() + 5.0
        while len(reports) < samples and time.monotonic() < deadline:
            time.sleep(0.01)
        w._stop.set()
        w._stop.wait = lambda t: True
        t.join(timeout=2.0)
        return reports, warns

    def test_a_healthy_battery_reports_and_never_warns(self):
        reports, warns = self.run_watcher(FakeBackend((70, "discharging")), 3)
        self.assertGreaterEqual(len(reports), 3)
        self.assertEqual(warns, [])

    def test_a_low_battery_warns_once_not_every_minute(self):
        reports, warns = self.run_watcher(FakeBackend((15, "discharging")), 5)
        # One warning at 15 %, then silence -- an alert every 60 s for an hour
        # is an alert people learn to ignore.
        self.assertEqual(len(warns), 1, warns)
        self.assertIn("CRITICAL", C.battery_note(*warns[0]))

    def test_it_warns_again_when_it_gets_worse(self):
        b = FakeBackend((18, "discharging"), (18, "discharging"),
                        (12, "discharging"), (12, "discharging"))
        reports, warns = self.run_watcher(b, 4)
        self.assertEqual([p for p, _ in warns], [18, 12])

    def test_charging_never_warns_however_low(self):
        reports, warns = self.run_watcher(FakeBackend((5, "charging")), 3)
        self.assertEqual(warns, [])

    def test_a_backend_that_raises_does_not_kill_the_watcher(self):
        class Boom:
            n = 0

            def device_status(self):
                Boom.n += 1
                if Boom.n < 3:
                    raise RuntimeError("bluetooth went away")
                return {"battery_percent": 60, "battery_state": "discharging"}

        reports, warns = self.run_watcher(Boom(), 2)
        self.assertGreaterEqual(len(reports), 2)


if __name__ == "__main__":
    unittest.main()
