"""Low-battery toasts: once per threshold per discharge, and the manager's
routing of the child's battery lines that feeds them.

`LowBatteryAlerts` is pure; the `ChildBridge` half is exercised through the
same `_read_stdout` path a real child's pipe goes through, with a fake pipe.
"""

from __future__ import annotations

import io
import unittest

from ds5app import manager as M
from ds5app import controller as C


class Dedupe(unittest.TestCase):
    def test_fires_once_at_twenty_and_once_at_ten(self):
        a = M.LowBatteryAlerts()
        seen = [a.observe(p, False) for p in (50, 40, 30, 20, 20, 20, 10, 10, 0)]
        self.assertEqual(seen, [None, None, None, 20, None, None, 10, None, None])

    def test_a_pad_that_arrives_already_low_gets_the_lower_toast_only(self):
        a = M.LowBatteryAlerts()
        self.assertEqual(a.observe(10, False), 10)
        self.assertIsNone(a.observe(10, False))
        self.assertIsNone(a.observe(20, False))       # 20 was spent with it

    def test_charging_re_arms_only_thresholds_it_climbs_above(self):
        a = M.LowBatteryAlerts()
        a.observe(20, False)
        a.observe(10, False)
        self.assertIsNone(a.observe(10, True))         # charging: never a toast
        self.assertIsNone(a.observe(20, True))         # above 10: 10 re-armed
        self.assertIsNone(a.observe(20, False))        # 20 itself still spent
        self.assertEqual(a.observe(10, False), 10)
        self.assertIsNone(a.observe(50, True))
        self.assertEqual(a.observe(20, False), 20)     # both re-armed at 50

    def test_bouncing_while_discharging_does_not_re_arm(self):
        # The pad reports in 10 % steps and flaps at the boundary; a toast
        # every flap is the failure this class exists to stop.
        a = M.LowBatteryAlerts()
        self.assertEqual(a.observe(20, False), 20)
        self.assertIsNone(a.observe(30, False))
        self.assertIsNone(a.observe(20, False))

    def test_reset_re_arms_everything(self):
        a = M.LowBatteryAlerts()
        a.observe(10, False)
        a.reset()
        self.assertEqual(a.observe(20, False), 20)

    def test_none_is_ignored(self):
        self.assertIsNone(M.LowBatteryAlerts().observe(None, False))

    def test_thresholds_are_the_lightbar_ones(self):
        self.assertEqual(M.LOW_BATTERY_TOASTS, (C.BATTERY_WARN_PERCENT, 10))
        self.assertEqual(C.BATTERY_WARN_PERCENT, 20)


class Parsing(unittest.TestCase):
    def test_the_childs_battery_lines(self):
        self.assertEqual(M.parse_battery_event("battery 40%"), (40, False))
        self.assertEqual(M.parse_battery_event("battery 40% (charging)"), (40, True))
        self.assertEqual(M.parse_battery_event("battery 100% (charging_full)"),
                         (100, True))
        self.assertEqual(M.parse_battery_event(C.battery_note(20, "discharging")),
                         (20, False))
        self.assertEqual(M.parse_battery_event(C.battery_note(10, "discharging")),
                         (10, False))
        self.assertIsNone(M.parse_battery_event("2 devices attached; detaching"))

    def test_the_toast_text(self):
        self.assertEqual(M.low_battery_text(20), "battery 20% -- charge soon")
        self.assertEqual(M.low_battery_text(10), "battery 10% -- charge it now")


class _Proc:
    """A dead child whose stdout is a canned pipe."""

    def __init__(self, lines):
        self.stdout = io.StringIO("".join(l + "\n" for l in lines))

    def poll(self):
        return 0


class ChildRouting(unittest.TestCase):
    def read(self, lines):
        events = []
        b = M.ChildBridge("d42f4ba1485d", 3241, ["x"],
                          on_event=lambda s, k, t: events.append((k, t)),
                          cleanup_fn=lambda *a, **k: None)
        b._snap["state"] = M.RUNNING
        b._read_stdout(_Proc(lines))
        return b, events

    def test_a_battery_warn_becomes_one_toast_not_a_warn(self):
        b, events = self.read([
            "  *  virtual wired DualSense attached (controller d42f4ba1485d)",
            "  ~  battery 30%",
            "  !  " + C.battery_note(20, "discharging"),
            "  ~  " + C.battery_note(20, "discharging"),
            "  !  " + C.battery_note(10, "discharging"),
        ])
        kinds = [k for k, _ in events]
        self.assertNotIn("warn", kinds)                 # never toasted raw
        toasts = [t for k, t in events if k == "battery_low"]
        self.assertEqual(toasts, ["battery 20% -- charge soon",
                                  "battery 10% -- charge it now"])

    def test_a_non_battery_warn_still_passes_as_a_warn(self):
        b, events = self.read(["  !  2 devices attached; detaching the extras"])
        self.assertEqual(events, [("warn", "2 devices attached; detaching the extras")])

    def test_mode_lines_land_in_the_snapshot(self):
        b, events = self.read(["  #  remote off  keyboard closed",
                               "  #  remote on  keyboard closed",
                               "  #  remote on  keyboard open"])
        snap = b.snapshot()
        self.assertTrue(snap["remote_mode"])
        self.assertTrue(snap["keyboard_open"])
        self.assertEqual([k for k, _ in events], ["mode"] * 3)

    def test_modes_are_unknown_until_the_child_says(self):
        b = M.ChildBridge("d42f4ba1485d", 3241, ["x"],
                          cleanup_fn=lambda *a, **k: None)
        snap = b.snapshot()
        self.assertIsNone(snap["remote_mode"])
        self.assertIsNone(snap["keyboard_open"])

    def test_the_status_regex_and_mode_line_agree_with_the_service(self):
        from ds5app import service as S
        self.assertIsNotNone(M._MODE_RE.match(S.mode_line(True, False)))
        self.assertIsNotNone(M._MODE_RE.match(S.mode_line(False, True)))
        self.assertEqual(M._EVENT_PREFIX["  #  "], "mode")


if __name__ == "__main__":
    unittest.main()
