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

import contextlib
import io
import logging
import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ds5app import service as S    # noqa: E402
from ds5app import tray as T       # noqa: E402

A = "a0fa9c0dd8bb"
STRAY_ID = r"HID\VID_054C&PID_0CE6\8&2fde51c0&0&0000"
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
                      "degraded": sum(1 for c in controllers
                                      if c["state"] == S.DEGRADED),
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
        # The tray assigns this directly: the manager exposes no setter for the
        # seed it copied at construction.
        self.hide_default = False
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

    def reconcile_stale(self, log_fn=None):
        self.calls.append(("reconcile_stale",))
        return 0


class _FakeConfig:
    """The settings the tray reads and writes, and a count of the saves.

    A real `Config` in a temporary directory would work too, but the tray's own
    `_save` is stubbed out here anyway, and counting saves is how these tests
    say "the choice was persisted" without going near the filesystem. `calls`
    records the setters, because the debounce contract is that CONFIG moves at
    click time while the manager moves at apply time -- so which fake got
    written, and when, is exactly what several tests assert.
    """

    def __init__(self, hide_default=False):
        self.hide_bluetooth_default = hide_default
        self.dashboard_port = 8765
        self.saves = 0
        self.calls = []

    def get(self, serial):
        return types.SimpleNamespace(label="", port=None, enabled=True)

    def set_hide_bluetooth_default(self, hide):
        self.hide_bluetooth_default = bool(hide)

    def set_enabled(self, serial, enabled):
        self.calls.append(("set_enabled", serial, bool(enabled)))

    def set_hide_bluetooth(self, serial, hide):
        self.calls.append(("set_hide_bluetooth", serial, bool(hide)))

    def set_master_enabled(self, enabled):
        self.calls.append(("set_master_enabled", bool(enabled)))


class _FakeTimer:
    """A `threading.Timer` that fires when the TEST says so.

    The debounce window is real time in production and poison in a test suite:
    a test that sleeps `APPLY_DELAY_S` to see the apply happen is slow, and a
    test that sleeps slightly less to see it NOT happen is flaky. The tray
    takes its timer as `_timer_factory` precisely so this stand-in can be
    injected and the clock can be a method call.
    """

    def __init__(self, delay, fn):
        self.delay = delay
        self.fn = fn
        self.started = False
        self.cancelled = False
        self.daemon = False

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True

    def fire(self):
        """What the real timer does at expiry -- unless it was cancelled."""
        if self.started and not self.cancelled:
            self.fn()


def app_with(controllers, master=True, has_hidhide=True, journal=0,
             hide_default=False):
    """A TrayApp with __init__ bypassed -- it would open the real config."""
    app = T.TrayApp.__new__(T.TrayApp)
    app.icon = None
    app.only = None
    app._busy = {}
    import threading
    app._lock = threading.Lock()
    app._snap = snap(controllers, master)
    app.mgr = _FakeManager(app._snap)
    app.cfg = _FakeConfig(hide_default)
    app.notes = []
    app._notify = lambda title, text: app.notes.append((title, text))
    app._save = lambda: setattr(app.cfg, "saves", app.cfg.saves + 1)
    app.hidhide_cli = None
    app._has_hidhide = has_hidhide
    app._menu_key_built = None
    app._menu_open = False
    app._menu_dirty = False
    app._pending = {}
    app._apply_timer = None
    # Fake timers, recorded in order: `flush(app)` is these tests' "two
    # seconds pass with no further clicks".
    app.timers = []

    def factory(delay, fn):
        t = _FakeTimer(delay, fn)
        app.timers.append(t)
        return t
    app._timer_factory = factory
    # Never the real journal: these tests must behave the same on a machine
    # where the developer genuinely has a controller hidden.
    app._journal_count = staticmethod(lambda: journal)
    return app


def flush(app):
    """Let the debounce window expire: fire the newest armed timer, settle.

    Only the newest, because that is what real time does -- every poke
    cancelled the timer before it, and `_FakeTimer.fire` honours `cancel()`
    the way `threading.Timer` does.
    """
    live = [t for t in app.timers if t.started and not t.cancelled]
    if live:
        live[-1].fire()
    settle()


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
        flush(app)                     # the manager moves at apply time
        self.assertIn(("set_enabled", A, False), app.mgr.calls)


def threadlist():
    import threading
    return [t for t in threading.enumerate()
            if t.name.startswith("tray-") and t.is_alive()]


def settle():
    for t in list(threadlist()):
        t.join(timeout=5)


class HideSubmenuTests(unittest.TestCase):
    """The `Hide per controller >` bank of slots.

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
        flush(app)
        self.assertIn(("set_hide_bluetooth", A, True), app.mgr.calls)

    def test_clicking_again_turns_it_off(self):
        app = app_with([controller(A, hide=True)])
        app._hide_slot(0)[3]()
        flush(app)
        self.assertIn(("set_hide_bluetooth", A, False), app.mgr.calls)

    def test_a_label_is_shown_with_the_serial(self):
        app = app_with([controller(A, label="couch")])
        text = app._hide_slot(0)[1]()
        self.assertIn("couch", text)
        self.assertIn("a0fa..d8bb", text)


class HideAllSwitchTests(unittest.TestCase):
    """The top-level checkbox that hides or unhides every pad at once.

    It lives in the MAIN menu now, next to the per-controller submenu, but the
    interesting decision is unchanged: the mixed state. pystray has no
    tri-state, so "one of two pads is hidden" has to render as a plain checked
    or unchecked box, and the rule here is UNCHECKED -- checked means ALL, and
    the click that follows a mixed state must hide the remaining pad rather
    than unhide the one the user already chose to hide.
    """

    def test_it_is_checked_only_when_every_controller_is_hiding(self):
        self.assertTrue(app_with([controller(A, hide=True),
                                  controller(B, hide=True)])._hide_all_checked())
        self.assertFalse(app_with([controller(A, hide=False),
                                   controller(B, hide=False)])._hide_all_checked())

    def test_a_mixed_state_reads_as_unchecked(self):
        app = app_with([controller(A, hide=True), controller(B, hide=False)])
        self.assertFalse(app._hide_all_checked())

    def test_it_reads_intent_so_the_tick_appears_at_click_time(self):
        # Tri-state over DESIRED state: the user un-mixes the selection by
        # ticking the last pad's box, and the outer box must agree
        # immediately -- two seconds before the manager is told anything.
        app = app_with([controller(A, hide=True), controller(B, hide=False)])
        app._toggle_hide(B)()
        self.assertTrue(app._hide_all_checked())
        self.assertEqual([c for c in app.mgr.calls
                          if c[0] == "set_hide_bluetooth"], [])

    def test_clicking_a_mixed_state_hides_the_ones_that_are_not_hidden(self):
        # The whole reason a mixed state renders unchecked: the next click has
        # to finish the job rather than undo half of it.
        app = app_with([controller(A, hide=True), controller(B, hide=False)])
        app._toggle_hide_all()
        flush(app)
        self.assertIn(("set_hide_bluetooth", B, True), app.mgr.calls)
        self.assertNotIn(("set_hide_bluetooth", A, False), app.mgr.calls)

    def test_one_click_sets_every_controller(self):
        app = app_with([controller(A), controller(B)])
        app._toggle_hide_all()
        flush(app)
        self.assertEqual([c for c in app.mgr.calls if c[0] == "set_hide_bluetooth"],
                         [("set_hide_bluetooth", A, True),
                          ("set_hide_bluetooth", B, True)])

    def test_one_click_writes_every_controllers_config_immediately(self):
        # The manager waits for the debounce; the FILE must not. A crash
        # inside the window still remembers what the user chose.
        app = app_with([controller(A), controller(B)])
        app._toggle_hide_all()
        self.assertEqual([c for c in app.cfg.calls
                          if c[0] == "set_hide_bluetooth"],
                         [("set_hide_bluetooth", A, True),
                          ("set_hide_bluetooth", B, True)])
        self.assertEqual(app.cfg.saves, 1)
        self.assertEqual([c for c in app.mgr.calls
                          if c[0] == "set_hide_bluetooth"], [])

    def test_clicking_when_everything_is_hidden_unhides_everything(self):
        app = app_with([controller(A, hide=True), controller(B, hide=True)])
        app._toggle_hide_all()
        flush(app)
        self.assertEqual([c for c in app.mgr.calls if c[0] == "set_hide_bluetooth"],
                         [("set_hide_bluetooth", A, False),
                          ("set_hide_bluetooth", B, False)])

    def test_the_choice_is_seeded_for_controllers_not_seen_yet(self):
        # Both halves, and both at CLICK time: the file, so a pad plugged in
        # tomorrow inherits it, and the live manager's seed, so a pad plugged
        # in a minute from now does too. Seeding is a pure settings write, so
        # it does not wait for the debounce.
        app = app_with([controller(A)])
        app._toggle_hide_all()
        self.assertTrue(app.cfg.hide_bluetooth_default)
        self.assertTrue(app.mgr.hide_default)
        self.assertEqual(app.cfg.saves, 1)

    def test_unhiding_seeds_the_opposite_answer(self):
        app = app_with([controller(A, hide=True)], hide_default=True)
        app._toggle_hide_all()
        self.assertFalse(app.cfg.hide_bluetooth_default)
        self.assertFalse(app.mgr.hide_default)

    def test_with_no_controllers_it_records_the_preference_without_throwing(self):
        app = app_with([])
        app._toggle_hide_all()
        flush(app)
        self.assertTrue(app.cfg.hide_bluetooth_default)
        self.assertEqual([c for c in app.mgr.calls if c[0] == "set_hide_bluetooth"],
                         [])

    def test_with_no_controllers_the_box_shows_the_seed_not_a_vacuous_true(self):
        # all([]) is True, which would tick the box for a state that does not
        # exist -- and the click after that would unhide by default.
        self.assertFalse(app_with([])._hide_all_checked())
        self.assertTrue(app_with([], hide_default=True)._hide_all_checked())

    def test_it_is_invisible_without_hidhide(self):
        visible = app_with([controller(A)], has_hidhide=False)._hide_all_row()[0]
        self.assertFalse(visible())
        self.assertTrue(app_with([controller(A)])._hide_all_row()[0]())

    def test_one_notification_for_the_click_not_one_per_controller(self):
        # N balloons for one click is how a helpful message becomes something
        # the user turns off in Windows settings.
        app = app_with([controller(A), controller(B)])
        app._toggle_hide_all()
        flush(app)
        self.assertEqual(len(app.notes), 1)
        self.assertIn("from now on", app.notes[0][1])

    def test_without_hidhide_the_notification_says_nothing_was_hidden(self):
        # And it says so at CLICK time -- with HidHide absent there is no
        # hardware to wait for, and a delayed "this did nothing" reads like it
        # did something. The apply pass must then stay quiet.
        app = app_with([controller(A)], has_hidhide=False)
        app._toggle_hide_all()
        self.assertEqual(len(app.notes), 1)
        self.assertIn("HidHide", app.notes[0][1])
        flush(app)
        self.assertEqual(len(app.notes), 1)

    def test_the_row_is_callables_over_the_latest_snapshot(self):
        # The menu is built exactly once, so the same callable has to answer
        # for a snapshot that did not exist when the row was made.
        app = app_with([controller(A, hide=False)])
        visible, checked, action = app._hide_all_row()
        self.assertTrue(all(callable(x) for x in (visible, checked, action)))
        self.assertFalse(checked())
        app._snap = snap([controller(A, hide=True)])
        self.assertTrue(checked(), "the row must read the newest snapshot")


class DebounceTests(unittest.TestCase):
    """A click is an intent; the hardware moves `APPLY_DELAY_S` later, once.

    The contract under test: config and checkbox state change at click time,
    the manager is only touched when the timer expires with no further input,
    and the apply pass moves the NET difference -- so on-then-off is a no-op
    and three clicks cost one pass. Timers are `_FakeTimer`s throughout;
    nothing here sleeps.
    """

    def toggle(self, app, serial):
        i = [c["serial"] for c in app._controllers()].index(serial)
        app._slot(i)[3]()

    def test_a_click_arms_the_timer_and_touches_nothing_else(self):
        app = app_with([controller(A, enabled=True)])
        self.toggle(app, A)
        settle()
        self.assertEqual(app.mgr.calls, [])
        self.assertEqual(len(app.timers), 1)
        self.assertTrue(app.timers[0].started)

    def test_the_delay_is_the_module_constant(self):
        app = app_with([controller(A)])
        self.toggle(app, A)
        self.assertEqual(app.timers[0].delay, T.APPLY_DELAY_S)

    def test_the_checkbox_and_config_move_at_click_time(self):
        app = app_with([controller(A, enabled=True)])
        i = [c["serial"] for c in app._controllers()].index(A)
        self.toggle(app, A)
        self.assertFalse(app._slot(i)[2](), "the box must move with the click")
        self.assertIn(("set_enabled", A, False), app.cfg.calls)
        self.assertEqual(app.cfg.saves, 1)
        self.assertEqual(app.mgr.calls, [], "the bridge must NOT move yet")

    def test_each_click_rearms_the_one_timer(self):
        app = app_with([controller(A), controller(B)])
        self.toggle(app, A)
        self.toggle(app, B)
        self.assertEqual(len(app.timers), 2)
        self.assertTrue(app.timers[0].cancelled)
        self.assertFalse(app.timers[1].cancelled)

    def test_on_then_off_inside_the_window_is_a_no_op(self):
        app = app_with([controller(A, enabled=True)])
        self.toggle(app, A)
        self.toggle(app, A)
        flush(app)
        self.assertEqual(app.mgr.calls, [])
        self.assertEqual(app._pending, {}, "the settled intent must be cleared")

    def test_only_the_net_change_is_applied(self):
        # Three clicks -- A off, B off, B back on -- coalesce into one call.
        app = app_with([controller(A, enabled=True), controller(B, enabled=True)])
        self.toggle(app, A)
        self.toggle(app, B)
        self.toggle(app, B)
        flush(app)
        self.assertEqual(app.mgr.calls, [("set_enabled", A, False)])

    def test_the_master_toggle_coalesces_too(self):
        app = app_with([controller(A)], master=True)
        app._toggle_master()
        app._toggle_master()
        flush(app)
        self.assertEqual([c for c in app.mgr.calls
                          if c[0] == "set_master_enabled"], [])

    def test_master_off_lands_before_the_per_controller_flags(self):
        # So the flags that follow are bookkeeping against a stopped fleet
        # rather than one stop apiece.
        app = app_with([controller(A, enabled=True)], master=True)
        app._toggle_master()
        self.toggle(app, A)
        flush(app)
        self.assertEqual(app.mgr.calls, [("set_master_enabled", False),
                                         ("set_enabled", A, False)])

    def test_a_busy_apply_rearms_rather_than_dropping_the_intent(self):
        # The previous apply pass can still be mid-bridge-start when the timer
        # fires again. `_work` refuses re-entry; the intent must survive it.
        app = app_with([controller(A, enabled=True)])
        self.toggle(app, A)
        app._busy["apply"] = True
        flush(app)
        self.assertEqual(app.mgr.calls, [])
        app._busy["apply"] = False
        flush(app)                      # the re-armed timer
        self.assertEqual(app.mgr.calls, [("set_enabled", A, False)])

    def test_quit_cancels_pending_intents_rather_than_flushing(self):
        """The teardown decision, pinned down. See `_quit` for the full why:
        `mgr.close()` unhides everything this process hid no matter what is
        pending, so cancelling cannot strand a pad -- while flushing could lay
        a fresh cloak seconds before the `os._exit` watchdog fires."""
        app = app_with([controller(A, enabled=True)])
        app._shutdown_icon = lambda: None    # never arm the real watchdog here
        self.toggle(app, A)
        app._quit()
        settle()
        self.assertIn(("close",), app.mgr.calls)
        self.assertNotIn(("set_enabled", A, False), app.mgr.calls)
        self.assertEqual(app._pending, {})
        self.assertTrue(app.timers[0].cancelled)
        # The choice itself was not lost: it went to the config at click time.
        self.assertIn(("set_enabled", A, False), app.cfg.calls)

    def test_cancel_pending_runs_before_the_manager_at_teardown(self):
        app = app_with([controller(A)])
        order = []
        app._cancel_pending = lambda: order.append("cancel")
        app.mgr.close = lambda: order.append("close")
        app._shutdown_icon = lambda: order.append("icon")
        hooks = list(S.ON_TEARDOWN)
        try:
            S.ON_TEARDOWN[:] = []
            app._install_teardown_hooks()
            for hook in S.ON_TEARDOWN:
                hook()
        finally:
            S.ON_TEARDOWN[:] = hooks
        self.assertEqual(order, ["cancel", "close", "icon"])

    def test_a_pending_toggle_changes_the_menu_key(self):
        # The click must repaint the checkbox: pystray rebuilds the menu after
        # a click through its own `_handler`, and `_menu_key` has to recognise
        # the pending state as a change or `_sync_menu` would fight it.
        app = app_with([controller(A, hide=False)])
        before = app._menu_key()
        app._toggle_hide(A)()
        self.assertNotEqual(app._menu_key(), before)


class DashboardTests(unittest.TestCase):
    """The `Open dashboard` row: one URL, from the config, in the default
    browser."""

    def open_with(self, app, result=True, elevated=False):
        import webbrowser
        opened = []
        saved = webbrowser.open
        saved_elev = T._is_elevated
        webbrowser.open = lambda url: opened.append(url) or result
        # Pinned, not read from the machine: the suite may itself be running
        # from an elevated shell, and these tests are about the URL.
        T._is_elevated = lambda: elevated
        try:
            app._open_dashboard()
            settle()
        finally:
            webbrowser.open = saved
            T._is_elevated = saved_elev
        return opened

    def test_an_elevated_tray_hands_the_url_to_explorer(self):
        # The tray runs as administrator now; a browser started with our token
        # would be an administrator browser. explorer.exe opens it with the
        # desktop's ordinary token, and webbrowser.open is not used at all.
        import subprocess
        launched = []
        saved = subprocess.Popen
        subprocess.Popen = lambda argv, **kw: launched.append(list(argv))
        try:
            app = app_with([controller(A)])
            opened = self.open_with(app, elevated=True)
        finally:
            subprocess.Popen = saved
        self.assertEqual(opened, [])
        self.assertEqual(launched, [["explorer.exe", "http://127.0.0.1:8765"]])
        self.assertEqual(app.notes, [])

    def test_it_opens_the_configured_port_on_loopback(self):
        app = app_with([controller(A)])
        app.cfg.dashboard_port = 9001
        self.assertEqual(self.open_with(app), ["http://127.0.0.1:9001"])

    def test_the_default_port_is_8765(self):
        app = app_with([controller(A)])
        self.assertEqual(self.open_with(app), ["http://127.0.0.1:8765"])

    def test_a_browser_that_would_not_open_is_reported_with_the_url(self):
        # webbrowser.open returning False is a headless or misconfigured
        # machine; the balloon has to hand over the address it could not open.
        app = app_with([controller(A)])
        self.open_with(app, result=False)
        self.assertEqual(len(app.notes), 1)
        self.assertIn("http://127.0.0.1:8765", app.notes[0][1])

    def test_a_working_browser_is_not_narrated(self):
        app = app_with([controller(A)])
        self.open_with(app, result=True)
        self.assertEqual(app.notes, [])


class UnhideEverythingTests(unittest.TestCase):
    """The panic item. Its visibility rule is the whole point of it."""

    def test_hidden_when_nothing_is_hidden(self):
        app = app_with([controller(A)], journal=0)
        self.assertEqual(app._journal_count(), 0)

    def test_visible_even_when_hidhide_is_undetected(self):
        """That is exactly the state a user needs to escape from."""
        app = app_with([controller(A)], has_hidhide=False, journal=1)
        self.assertGreater(app._journal_count(), 0)

    def _run_unhide_all(self, app, *, sweep_n=1, journal=0, stray=None,
                        owed=(), visible=True):
        """Drive the panic button with every HidHide call faked.

        `unrecorded_hidden` is faked along with the rest deliberately: it is a
        blacklist read, and a unit test that reaches the real driver would both
        depend on the developer's machine and be answered differently in CI.
        The same goes for `read_records` and `pad_visible` -- the second one
        enumerates HID devices, which on a developer's machine finds their own
        controller and in CI finds nothing.

        `owed` is what the journal held BEFORE the sweep, and `visible` is what
        `hid_enumerate` says about those serials afterwards: `visible=False` is
        the 2026-08-27 machine, where the blacklist really was emptied and the
        pad really was still gone.
        """
        seen = {}
        import ds5app.hidhide as HH_real
        saved = (HH_real.sweep, HH_real.journal_count, HH_real.unrecorded_hidden,
                 HH_real.read_records, HH_real.pad_visible)

        def sweep(force=False, log_fn=None, **kw):
            seen["force"] = force
            seen["stopped_first"] = ("stop_all",) in app.mgr.calls
            return sweep_n

        HH_real.sweep = sweep
        HH_real.journal_count = lambda: journal
        HH_real.unrecorded_hidden = lambda *a, **k: stray
        HH_real.read_records = lambda: [{"serial": s} for s in owed]
        HH_real.pad_visible = lambda s: visible
        try:
            app._unhide_all()
            settle()
        finally:
            (HH_real.sweep, HH_real.journal_count, HH_real.unrecorded_hidden,
             HH_real.read_records, HH_real.pad_visible) = saved
        return seen

    def test_it_stops_every_bridge_before_sweeping(self):
        """So the state is coherent afterwards rather than half torn down."""
        app = app_with([controller(A)], journal=1)
        app._notify = lambda *a: None
        seen = self._run_unhide_all(app, stray=[])
        self.assertTrue(seen.get("stopped_first"),
                        "the sweep ran before the bridges were stopped")
        self.assertTrue(seen.get("force"),
                        "the panic path must ignore the liveness check")

    def test_an_empty_journal_alone_is_not_an_all_clear(self):
        # The 2026-08-27 failure: one lying unhide leaves a blacklist entry
        # with no record against it, and this notification was what sent the
        # user away from the only control that could still have helped.
        app = app_with([controller(A)])
        self._run_unhide_all(app, journal=0, stray=[STRAY_ID])
        text = " ".join(t for _title, t in app.notes)
        self.assertIn("still hiding", text)
        self.assertIn("--all-hidhide", text)
        self.assertNotIn("visible to Windows again", text)

    def test_an_unreadable_blacklist_does_not_claim_success(self):
        app = app_with([controller(A)])
        self._run_unhide_all(app, journal=0, stray=None)
        text = " ".join(t for _title, t in app.notes)
        self.assertIn("cannot confirm", text)

    def test_a_genuinely_clean_sweep_still_says_so(self):
        app = app_with([controller(A)])
        self._run_unhide_all(app, sweep_n=2, journal=0, stray=[])
        text = " ".join(t for _title, t in app.notes)
        self.assertIn("Unhid 2 controller(s)", text)
        self.assertIn("visible to Windows again", text)

    def test_an_empty_blacklist_is_not_a_working_controller(self):
        """The 2026-08-27 machine after a tray Quit: list clear, pad gone.

        The HID child devnode had gone phantom, and clearing a blacklist does
        not re-create a devnode. `sweep()` has already tried the re-enumeration
        by the time this runs, so what is left to give the user is the one
        instruction that always works.
        """
        app = app_with([controller(A)])
        self._run_unhide_all(app, sweep_n=1, journal=0, stray=[], owed=[A],
                             visible=False)
        text = " ".join(t for _title, t in app.notes)
        self.assertIn("OFF", text)
        self.assertIn("on again", text)
        self.assertNotIn("Every pad is visible", text)

    def test_a_pad_that_really_did_come_back_is_not_nagged_about(self):
        app = app_with([controller(A)])
        self._run_unhide_all(app, sweep_n=1, journal=0, stray=[], owed=[A],
                             visible=True)
        text = " ".join(t for _title, t in app.notes)
        self.assertIn("visible to Windows again", text)
        self.assertNotIn("on again", text)

    def test_a_record_that_survived_names_the_real_culprit(self):
        """It is the worse failure of the two, so it is the one reported."""
        app = app_with([controller(A)])
        self._run_unhide_all(app, journal=1, stray=[], owed=[A], visible=False)
        text = " ".join(t for _title, t in app.notes)
        self.assertIn("could not be unhidden", text)


class StartupAndShutdownTests(unittest.TestCase):
    """The two orderings that decide whether Quit and Ctrl+C leave a mess."""

    def setUp(self):
        self._hooks = list(S.ON_TEARDOWN)
        self.addCleanup(lambda: S.ON_TEARDOWN.__setitem__(slice(None),
                                                          self._hooks))
        S.ON_TEARDOWN[:] = []

    def test_the_manager_is_closed_before_the_exit_watchdog_is_armed(self):
        """`_shutdown_icon` arms a three-second `os._exit(0)`.

        Stopping two children, detaching their devices and unhiding their pads
        is comfortably more than three seconds of work, so a hook order that
        arms the watchdog first can exit the process mid-detach -- leaving the
        zombie virtual device and the invisible controller this path exists to
        prevent.
        """
        app = app_with([controller(A)])
        order = []
        app._shutdown_icon = lambda: order.append("icon")
        app.mgr.close = lambda: order.append("close")
        app._install_teardown_hooks()
        for hook in S.ON_TEARDOWN:
            hook()
        self.assertEqual(order, ["close", "icon"])

    def test_the_startup_reconciliation_runs(self):
        app = app_with([controller(A)])
        app._startup_reconcile()
        self.assertIn(("reconcile_stale",), app.mgr.calls)

    def test_a_reconciliation_failure_does_not_stop_the_tray(self):
        """usbip.exe missing, a driver fault -- housekeeping, not the product."""
        app = app_with([controller(A)])

        def boom(log_fn=None):
            raise RuntimeError("usbip is not answering")

        app.mgr.reconcile_stale = boom
        log = logging.getLogger("ds5app.tray")
        was = log.propagate, log.disabled
        log.propagate, log.disabled = False, True
        try:
            app._startup_reconcile()      # must not raise
        finally:
            log.propagate, log.disabled = was


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

    def test_an_offline_controller_is_not_counted_as_bridged(self):
        # The line that made the program look wrong while it was being slow:
        # the pad is switched off on the desk and the tray said "2 of 2".
        app = app_with([controller(A), controller(B, state=S.DEGRADED)])
        head = app._title().splitlines()[0]
        self.assertIn("1 of 2 bridged", head)
        self.assertIn("1 offline", head)

    def test_nothing_offline_says_nothing_about_it(self):
        app = app_with([controller(A), controller(B)])
        self.assertEqual(app._title().splitlines()[0],
                         "ds5bridge -- 2 of 2 bridged")

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


#: What pystray's win32 backend actually registers its tray callback under --
#: `WM_USER + 11`, not `WM_NOTIFY`. The value is here only so the fake is
#: honest: `_watch_menu_visibility` finds the handler by IDENTITY precisely so
#: that this private constant is not something this project has to know.
_NOTIFY_CODE = 0x040B


class _FakeIcon:
    """The parts of pystray's win32 `Icon` the refresh path actually touches.

    `_message_handlers` and `_on_notify` are private, which is exactly why they
    are modelled here: the tray wraps them, and a test is the only thing that
    will notice if a future pystray renames them.
    """

    def __init__(self, on_notify=None, fail=False):
        self.updates = 0
        self.title = ""
        self.icon = None
        self.fail = fail
        self.notified = []
        self._on_notify = on_notify or (lambda w, l: self.notified.append(l))
        self._message_handlers = {_NOTIFY_CODE: self._on_notify}

    def update_menu(self):
        if self.fail:
            raise RuntimeError("the menu handle is gone")
        self.updates += 1


class MenuRefreshTests(unittest.TestCase):
    """The native menu is a SNAPSHOT on Windows -- see the module docstring.

    pystray bakes every `text`/`checked`/`visible` callable into an HMENU in
    `_update_menu()` and reuses it for every right-click, so without an explicit
    `update_menu()` the menu shows whatever was true when the icon was created.
    """

    def app(self, controllers, **kw):
        app = app_with(controllers, **kw)
        app.icon = _FakeIcon()
        return app

    def test_a_controller_appearing_changes_the_key(self):
        one = self.app([controller(A)])
        two = self.app([controller(A), controller(B)])
        self.assertNotEqual(one._menu_key(), two._menu_key())

    def test_a_jittering_report_rate_does_not(self):
        # Rebuilding a native menu twice a second forever to keep "250/s"
        # honest would be all cost and no benefit.
        a = self.app([controller(A, rate=249.0)])
        b = self.app([controller(A, rate=251.0)])
        self.assertEqual(a._menu_key(), b._menu_key())

    def test_a_controller_going_offline_does(self):
        up = self.app([controller(A)])
        down = self.app([controller(A, state=S.DEGRADED)])
        self.assertNotEqual(up._menu_key(), down._menu_key())

    def test_a_toggled_checkbox_does(self):
        on = self.app([controller(A, hide=False)])
        off = self.app([controller(A, hide=True)])
        self.assertNotEqual(on._menu_key(), off._menu_key())

    def test_the_first_sync_rebuilds_the_menu(self):
        app = self.app([controller(A)])
        app._sync_menu()
        self.assertEqual(app.icon.updates, 1)

    def test_a_menu_that_has_not_moved_is_not_rebuilt(self):
        app = self.app([controller(A)])
        app._sync_menu()
        for _ in range(5):
            app._sync_menu()
        self.assertEqual(app.icon.updates, 1)

    def test_a_new_controller_rebuilds_it(self):
        app = self.app([controller(A)])
        app._sync_menu()
        app._snap = snap([controller(A), controller(B)])
        app._sync_menu()
        self.assertEqual(app.icon.updates, 2)

    def test_a_rebuild_is_deferred_while_the_menu_is_open(self):
        # update_menu() destroys the handle TrackPopupMenuEx is displaying.
        app = self.app([controller(A)])
        app._sync_menu()
        app._menu_open = True
        app._snap = snap([controller(A), controller(B)])
        app._sync_menu()
        self.assertEqual(app.icon.updates, 1)
        self.assertTrue(app._menu_dirty)

    def test_the_deferred_rebuild_runs_when_the_menu_closes(self):
        app = self.app([controller(A)])
        app._sync_menu()

        def on_notify(_w, _l):
            # The poll thread ticking while the user has the menu open.
            app._snap = snap([controller(A), controller(B)])
            app._sync_menu()
            self.assertEqual(app.icon.updates, 1)

        app.icon = _FakeIcon(on_notify=on_notify)
        app.icon.updates = 1
        app._watch_menu_visibility()
        app.icon._message_handlers[_NOTIFY_CODE](0, 0x0205)
        self.assertEqual(app.icon.updates, 2)
        self.assertFalse(app._menu_open)

    def test_the_wrapper_leaves_the_handler_working(self):
        seen = []
        app = self.app([controller(A)])
        app.icon = _FakeIcon(on_notify=lambda w, l: seen.append(l))
        app._watch_menu_visibility()
        app.icon._message_handlers[_NOTIFY_CODE](0, 0x0205)
        self.assertEqual(seen, [0x0205])

    def test_a_pystray_without_the_private_handler_is_survived(self):
        # A stale menu is a bad day; an exception in the message loop takes the
        # icon with it.
        app = self.app([controller(A)])
        app.icon = types.SimpleNamespace(update_menu=lambda: None)
        app._watch_menu_visibility()          # must not raise

    def test_a_failed_rebuild_is_retried_rather_than_remembered(self):
        app = self.app([controller(A)])
        app.icon = _FakeIcon(fail=True)
        app._sync_menu()
        self.assertIsNone(app._menu_key_built)
        app.icon.fail = False
        app._sync_menu()
        self.assertEqual(app.icon.updates, 1)

    def test_the_refresh_tick_syncs_the_menu(self):
        try:
            from PIL import Image  # noqa: F401
        except ImportError:
            self.skipTest("Pillow is not installed")
        app = self.app([controller(A)])
        app._refresh_now()
        self.assertEqual(app.icon.updates, 1)


class AnnounceSwitchesTests(unittest.TestCase):
    """A switch being off must never look like the program being broken.

    Measured 2026-08-27: the master switch was off in the settings, `poll_once`
    returned at its first line, nothing was bridged, and the console printed
    nothing whatsoever -- which is indistinguishable from a hang.
    """

    def app(self, controllers, master=True, enabled=True):
        app = app_with(controllers, master=master)
        app.mgr.master_enabled = master
        app.mgr.known = lambda: [c["serial"] for c in controllers]
        app.mgr.is_enabled = lambda s: enabled
        return app

    def say(self, app):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            app._announce_switches()
        return out.getvalue()

    def test_the_master_switch_being_off_is_stated(self):
        said = self.say(self.app([controller(A)], master=False))
        self.assertIn("bridging is OFF", said)
        self.assertIn("Bridging enabled", said)

    def test_a_working_setup_says_nothing(self):
        # Silence is correct when there is nothing to explain; the event lines
        # from the bridges themselves are the output that matters.
        self.assertEqual(self.say(self.app([controller(A)])), "")

    def test_one_disabled_controller_is_named(self):
        said = self.say(self.app([controller(A)], enabled=False))
        self.assertIn(T.short(A), said)
        self.assertIn("switched off", said)

    def test_the_master_switch_wins_over_the_per_controller_ones(self):
        # One line about the switch that stops everything, not N about the
        # switches underneath it that no longer matter.
        said = self.say(self.app([controller(A), controller(B)],
                                 master=False, enabled=False))
        self.assertEqual(said.count("switched off"), 0)
        self.assertIn("bridging is OFF", said)


if __name__ == "__main__":
    unittest.main()
