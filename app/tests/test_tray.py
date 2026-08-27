"""The tray's pure rendering and its menu wiring, with no icon and no hardware.

`TrayApp` is mostly lambdas over a `BridgeManager`, so the parts worth testing
are the two that a person actually sees -- the text of a menu row and the state
of its checkbox -- plus the slot mechanism that lets the menu be built once and
never rebuilt (see tray.py's module docstring for why that matters).

Nothing here imports pystray or Pillow: those are only touched inside `run()`
and `_menu()`, and a test suite that needed a GUI stack to check a string would
not run in CI.
"""

from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ds5app import service as S    # noqa: E402
from ds5app import tray as T       # noqa: E402

A = "a0fa9c0dd8bb"
B = "d42f4ba1485d"


def controller(serial=A, state=S.RUNNING, enabled=True, present=True,
               rate=250.0, battery=50, error=None, label="", hide=False):
    return {"serial": serial, "state": state, "enabled": enabled,
            "present": present, "reports_per_s": rate, "battery_percent": battery,
            "error": error, "label": label, "attached": state == S.RUNNING,
            "port": 3241, "uptime_s": 10, "pid": 1234, "last_event": "",
            "hide_bluetooth": hide}


def snap(controllers, master=True):
    running = sum(1 for c in controllers if c["state"] in (S.RUNNING, S.DEGRADED))
    return {
        "master_enabled": master,
        "controllers": {c["serial"]: c for c in controllers},
        "aggregate": {"present": sum(1 for c in controllers if c["present"]),
                      "bridges": len(controllers), "running": running,
                      "attached": running,
                      "reports_per_s": sum(c["reports_per_s"] for c in controllers),
                      "errors": sum(1 for c in controllers if c["error"])},
    }


class ShortTests(unittest.TestCase):
    def test_a_bdaddr_keeps_both_ends(self):
        # The middle of a bdaddr is noise; the ends are what tells two
        # controllers apart in a menu that is 30 characters wide.
        self.assertEqual(T.short(A), "a0fa..d8bb")
        self.assertEqual(T.short(B), "d42f..485d")
        self.assertNotEqual(T.short(A), T.short(B))

    def test_something_already_short_is_left_alone(self):
        self.assertEqual(T.short("abc"), "abc")

    def test_no_serial_does_not_crash(self):
        self.assertEqual(T.short(""), "")
        self.assertEqual(T.short(None), "")


class DescribeTests(unittest.TestCase):
    def test_a_running_controller_shows_its_rate(self):
        self.assertEqual(T.describe(controller(rate=250.4)),
                         "a0fa..d8bb  250/s  50%")

    def test_a_disabled_controller_says_off_not_its_rate(self):
        # The switch is the point of the row. A disabled controller that still
        # advertised "250/s" would read as though the toggle had not worked.
        text = T.describe(controller(enabled=False))
        self.assertIn("off", text)
        self.assertNotIn("/s", text)

    def test_a_controller_that_is_not_connected_says_so(self):
        text = T.describe(controller(enabled=True, present=False, battery=None))
        self.assertIn("not connected", text)

    def test_a_failed_start_is_visible_in_the_row(self):
        text = T.describe(controller(state=S.STOPPED, error="usbip attach failed",
                                     rate=0.0))
        self.assertIn("failed", text)

    def test_an_unknown_battery_is_omitted_rather_than_guessed(self):
        self.assertNotIn("%", T.describe(controller(battery=None)))

    def test_a_label_is_used_and_keeps_the_serial(self):
        # A label has to stay attributable: two pads called "mine" would be
        # worse than no label at all.
        text = T.describe(controller(label="couch"))
        self.assertIn("couch", text)
        self.assertIn("a0fa..d8bb", text)


class _FakeManager:
    """Just enough BridgeManager for the tray's rendering paths."""

    def __init__(self, snapshot):
        self._snap = snapshot
        self.master_enabled = snapshot["master_enabled"]
        self.calls = []

    def snapshot(self):
        return self._snap

    def is_enabled(self, serial):
        return self._snap["controllers"][serial]["enabled"]

    def set_enabled(self, serial, value):
        self.calls.append(("set_enabled", serial, value))

    def set_master_enabled(self, value):
        self.calls.append(("set_master_enabled", value))

    def is_hiding(self, serial):
        return self._snap["controllers"][serial].get("hide_bluetooth", False)

    def set_hide_bluetooth(self, serial, value):
        self.calls.append(("set_hide_bluetooth", serial, value))

    def stop_all(self):
        self.calls.append(("stop_all",))

    def close(self):
        self.calls.append(("close",))


def app_with(controllers, master=True, has_hidhide=True, journal=0):
    """A TrayApp with __init__ bypassed -- it would open the real config."""
    app = T.TrayApp.__new__(T.TrayApp)
    app.icon = None
    app.only = None
    app._busy = {}
    import threading
    app._lock = threading.Lock()
    app._snap = snap(controllers, master)
    app.mgr = _FakeManager(app._snap)
    app.cfg = types.SimpleNamespace(
        get=lambda s: types.SimpleNamespace(label="", port=None, enabled=True))
    app.hidhide_cli = None
    app._has_hidhide = has_hidhide
    # Never the real journal: these tests must behave the same on a machine
    # where the developer genuinely has a controller hidden.
    app._journal_count = staticmethod(lambda: journal)
    return app


class SlotTests(unittest.TestCase):
    """The fixed slots that let the menu be built once and never rebuilt."""

    def test_only_as_many_slots_are_visible_as_there_are_controllers(self):
        app = app_with([controller(A), controller(B)])
        vis = [app._slot(i)[0]() for i in range(T.MAX_SLOTS)]
        self.assertEqual(vis[:2], [True, True])
        self.assertTrue(all(v is False for v in vis[2:]))

    def test_an_invisible_slot_still_answers_safely(self):
        # pystray evaluates text/checked on hidden items too, and an
        # IndexError inside the Win32 message loop takes the icon with it.
        app = app_with([controller(A)])
        _vis, text, checked, action = app._slot(5)
        self.assertEqual(text(), "")
        self.assertFalse(checked())
        action()                       # must be a no-op, not an exception

    def test_rows_are_in_a_stable_order(self):
        # Enumeration order is not stable, and a menu whose rows swap places
        # between two openings cannot be clicked reliably.
        forward = app_with([controller(A), controller(B)])
        reverse = app_with([controller(B), controller(A)])
        self.assertEqual([c["serial"] for c in forward._controllers()],
                         [c["serial"] for c in reverse._controllers()])

    def test_the_checkbox_follows_the_enabled_flag(self):
        app = app_with([controller(A, enabled=False), controller(B, enabled=True)])
        rows = app._controllers()
        checks = [app._slot(i)[2]() for i in range(len(rows))]
        self.assertEqual(dict(zip([r["serial"] for r in rows], checks)),
                         {A: False, B: True})

    def test_clicking_a_row_toggles_that_controller(self):
        app = app_with([controller(A, enabled=True), controller(B)])
        i = [c["serial"] for c in app._controllers()].index(A)
        app._slot(i)[3]()
        for t in list(threadlist()):
            t.join(timeout=5)
        self.assertIn(("set_enabled", A, False), app.mgr.calls)


def threadlist():
    import threading
    return [t for t in threading.enumerate()
            if t.name.startswith("tray-") and t.is_alive()]


def settle():
    for t in list(threadlist()):
        t.join(timeout=5)


class HideSubmenuTests(unittest.TestCase):
    """The `Hide Bluetooth pad while bridged >` bank of slots.

    Same fixed-slot discipline as the bridging rows: the menu object is built
    once and every dynamic value is a lambda, so an invisible slot still has to
    answer `text()` and `checked()` without raising -- pystray evaluates those
    on hidden items too, and an IndexError inside the Win32 message loop takes
    the whole icon with it.
    """

    def test_one_visible_slot_per_controller(self):
        app = app_with([controller(A), controller(B)])
        vis = [app._hide_slot(i)[0]() for i in range(T.MAX_SLOTS)]
        self.assertEqual(vis[:2], [True, True])
        self.assertTrue(all(v is False for v in vis[2:]))

    def test_no_slots_at_all_without_hidhide(self):
        """Hidden, not greyed: an unexplained grey checkbox invites a ticket."""
        app = app_with([controller(A)], has_hidhide=False)
        self.assertFalse(app._hide_slot(0)[0]())

    def test_an_invisible_slot_answers_safely(self):
        app = app_with([controller(A)])
        _vis, text, checked, action = app._hide_slot(5)
        self.assertEqual(text(), "")
        self.assertFalse(checked())
        action()

    def test_the_checkbox_follows_the_hide_flag(self):
        app = app_with([controller(A, hide=True), controller(B, hide=False)])
        rows = app._controllers()
        checks = [app._hide_slot(i)[2]() for i in range(len(rows))]
        self.assertEqual(dict(zip([r["serial"] for r in rows], checks)),
                         {A: True, B: False})

    def test_clicking_a_row_toggles_that_controller(self):
        app = app_with([controller(A, hide=False), controller(B)])
        i = [c["serial"] for c in app._controllers()].index(A)
        app._hide_slot(i)[3]()
        settle()
        self.assertIn(("set_hide_bluetooth", A, True), app.mgr.calls)

    def test_clicking_again_turns_it_off(self):
        app = app_with([controller(A, hide=True)])
        app._hide_slot(0)[3]()
        settle()
        self.assertIn(("set_hide_bluetooth", A, False), app.mgr.calls)

    def test_a_label_is_shown_with_the_serial(self):
        app = app_with([controller(A, label="couch")])
        text = app._hide_slot(0)[1]()
        self.assertIn("couch", text)
        self.assertIn("a0fa..d8bb", text)


class UnhideEverythingTests(unittest.TestCase):
    """The panic item. Its visibility rule is the whole point of it."""

    def test_hidden_when_nothing_is_hidden(self):
        app = app_with([controller(A)], journal=0)
        self.assertEqual(app._journal_count(), 0)

    def test_visible_even_when_hidhide_is_undetected(self):
        """That is exactly the state a user needs to escape from."""
        app = app_with([controller(A)], has_hidhide=False, journal=1)
        self.assertGreater(app._journal_count(), 0)

    def test_it_stops_every_bridge_before_sweeping(self):
        """So the state is coherent afterwards rather than half torn down."""
        app = app_with([controller(A)], journal=1)
        seen = {}

        class FakeHH:
            @staticmethod
            def sweep(force=False):
                seen["force"] = force
                seen["stopped_first"] = ("stop_all",) in app.mgr.calls
                return 1

            @staticmethod
            def journal_count():
                return 0

        app._notify = lambda *a: None
        real_import = T.TrayApp.__dict__.get("_unhide_all")
        self.assertIsNotNone(real_import)
        import ds5app.hidhide as HH_real
        saved = (HH_real.sweep, HH_real.journal_count)
        HH_real.sweep, HH_real.journal_count = FakeHH.sweep, FakeHH.journal_count
        try:
            app._unhide_all()
            settle()
        finally:
            HH_real.sweep, HH_real.journal_count = saved
        self.assertTrue(seen.get("stopped_first"),
                        "the sweep ran before the bridges were stopped")
        self.assertTrue(seen.get("force"),
                        "the panic path must ignore the liveness check")


class ReconcileAutostartTests(unittest.TestCase):
    """The Run key is the truth; the config field only mirrors it."""

    def setUp(self):
        self.saved = (T.A.available, T.A.is_enabled, T.K.try_save)
        self.written = []
        T.K.try_save = lambda cfg, path=None: self.written.append(
            cfg.autostart_on_login) or True

    def tearDown(self):
        T.A.available, T.A.is_enabled, T.K.try_save = self.saved

    def app(self, field):
        app = T.TrayApp.__new__(T.TrayApp)
        app.cfg = types.SimpleNamespace(autostart_on_login=field)
        return app

    def test_a_run_key_set_from_outside_updates_the_config(self):
        # Exactly what happened on the development machine: enable() was
        # called directly, so the registry said yes and the file still said no.
        T.A.available = lambda: True
        T.A.is_enabled = lambda: True
        app = self.app(False)
        app._reconcile_autostart()
        self.assertTrue(app.cfg.autostart_on_login)
        self.assertEqual(self.written, [True])

    def test_a_cleared_run_key_updates_the_config_too(self):
        T.A.available = lambda: True
        T.A.is_enabled = lambda: False
        app = self.app(True)
        app._reconcile_autostart()
        self.assertFalse(app.cfg.autostart_on_login)
        self.assertEqual(self.written, [False])

    def test_agreement_writes_nothing(self):
        # Rewriting the file on every launch for no reason is its own bug.
        T.A.available = lambda: True
        T.A.is_enabled = lambda: True
        self.app(True)._reconcile_autostart()
        self.assertEqual(self.written, [])

    def test_no_registry_at_all_leaves_the_field_alone(self):
        T.A.available = lambda: False
        T.A.is_enabled = lambda: False
        app = self.app(True)
        app._reconcile_autostart()
        self.assertTrue(app.cfg.autostart_on_login)
        self.assertEqual(self.written, [])


class TitleTests(unittest.TestCase):
    def test_the_title_counts_what_is_bridged(self):
        app = app_with([controller(A), controller(B, state=S.STOPPED, rate=0.0)])
        self.assertIn("1 of 2 bridged", app._title())

    def test_master_off_is_stated_plainly(self):
        app = app_with([controller(A)], master=False)
        self.assertEqual(app._title(), "ds5bridge -- off")

    def test_no_controllers_says_so_rather_than_zero_of_zero(self):
        app = app_with([])
        self.assertIn("no controller", app._title().lower())

    def test_the_title_fits_a_win32_tooltip(self):
        # Win32 caps the tray tooltip at 128 characters and silently truncates.
        app = app_with([controller(f"{i:012x}") for i in range(T.MAX_SLOTS)])
        self.assertLessEqual(len(app._title()), 127)


class ArtTests(unittest.TestCase):
    def test_master_off_is_its_own_colour(self):
        app = app_with([controller(A)], master=False)
        self.assertEqual(app._art()[0], T.DISABLED_COLOR)

    def test_an_error_wins_over_a_running_bridge(self):
        # If one controller is broken the icon must not look healthy just
        # because the other one is fine.
        app = app_with([controller(A), controller(B, state=S.STOPPED,
                                                  error="boom", rate=0.0)])
        self.assertEqual(app._art()[0], T.COLORS[S.ERROR])

    def test_the_battery_shown_is_the_lowest_one(self):
        app = app_with([controller(A, battery=80), controller(B, battery=15)])
        self.assertEqual(app._art()[1], 15)

    def test_a_disabled_controllers_battery_is_not_counted(self):
        app = app_with([controller(A, battery=80),
                        controller(B, battery=9, enabled=False)])
        self.assertEqual(app._art()[1], 80)

    def test_the_icon_renders_at_tray_size(self):
        try:
            from PIL import Image  # noqa: F401
        except ImportError:
            self.skipTest("Pillow is not installed")
        for count in (0, 1, 2):
            img = T._icon_image((60, 170, 90), 50, count, size=16)
            self.assertEqual(img.size, (16, 16))


if __name__ == "__main__":
    unittest.main()
