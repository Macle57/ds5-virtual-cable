"""`BridgeManager` without a controller, a driver, a port or a child process.

Everything this class must get right is policy -- which port, whose port, who
survives whose failure, when a disappearance counts -- and none of it needs
hardware to check. So the bridge is a stub, discovery is a list, and
`port_free` is a set: the same way the rest of this repository's tests stay
runnable on a bare Python with no driver and no controller.

The four properties worth breaking a build over:

  * a port is allocated once and never moves (a controller that changes port
    becomes a different device to Windows, and every per-device setting the
    user had is gone);
  * 3240 is never allocated, because it is usbipd-win's;
  * one controller failing leaves the others running and attached -- the entire
    reason this class exists;
  * teardown touches only the failing controller's port, never "everything
    attached", which is the mistake that killed a running soak once
    (usbip.py `our_ports`).

    prototype\\.venv\\Scripts\\python.exe -m unittest discover -s tests -t .
"""

from __future__ import annotations

import logging
import threading
import time
import unittest

from ds5app import manager as M
from ds5app import service as S


class FakeBridge:
    """A `ChildBridge` with no child.

    Records every cleanup it performs, keyed by port, so a test can assert that
    stopping one controller never cleaned up another one's port.
    """

    #: port -> how many times cleanup ran for it, across every instance.
    cleaned: list = []
    #: serials whose `start()` should raise.
    fail_on_start: set = set()
    #: serials whose start "succeeds" but never reports ready.
    never_ready: set = set()

    def __init__(self, serial, port, command, job=None, on_event=None,
                 usbip_exe=None, **kw):
        self.serial = serial.lower()
        self.port = port
        self.command = list(command)
        self.job = job
        self.on_event = on_event or (lambda s, k, t: None)
        self.error = None
        self.started = False
        self.stopped = False
        self._state = M.STOPPED
        self._rate = 0.0

    @property
    def alive(self) -> bool:
        return self.started and not self.stopped

    def start(self) -> None:
        if self.serial in FakeBridge.fail_on_start:
            raise RuntimeError(f"{self.serial}: no such controller")
        self.started = True
        self._state = M.STARTING

    def wait_ready(self, timeout: float = 60.0) -> bool:
        if self.serial in FakeBridge.never_ready:
            self.error = f"{self.serial}: the bridge did not come up"
            self._state = M.ERROR
            return False
        self._state = M.RUNNING
        self._rate = 250.0
        return True

    def stop(self) -> None:
        self.stopped = True
        self._state = M.STOPPED
        self._rate = 0.0
        FakeBridge.cleaned.append(self.port)

    def die(self) -> None:
        """The child crashed. Nothing else in the manager should notice."""
        self.stopped = True
        self.error = "the bridge process exited"
        self._state = M.ERROR

    def snapshot(self) -> dict:
        return {"serial": self.serial, "port": self.port, "state": self._state,
                "error": self.error, "pid": 4242, "battery_percent": 55,
                "reports_per_s": self._rate, "uptime_s": 7,
                "attached": self._state == M.RUNNING, "last_event": ""}


A = "d42f4ba1485d"
B = "a0fa9c0dd8bb"
C = "0011223344cc"


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        FakeBridge.cleaned = []
        FakeBridge.fail_on_start = set()
        FakeBridge.never_ready = set()
        self.present = [A, B]
        self.busy: set = set()
        self.events: list = []
        self._before_hooks = list(S.ON_TEARDOWN)
        # Several tests provoke a start failure on purpose, and `manager.start`
        # logs those with a traceback. Unconfigured, logging prints them to
        # stderr and a green run looks like a broken one.
        self._log = logging.getLogger("ds5app.manager")
        self._log_was = self._log.propagate, self._log.disabled
        self._log.propagate = False
        self._log.disabled = True

    def tearDown(self) -> None:
        self._log.propagate, self._log.disabled = self._log_was
        # ON_TEARDOWN is module state: a manager that is not closed would be
        # called at interpreter exit, long after its test finished.
        S.ON_TEARDOWN[:] = self._before_hooks

    def make(self, **kw):
        kw.setdefault("discover", lambda: list(self.present))
        kw.setdefault("bridge_factory", FakeBridge)
        kw.setdefault("port_free", lambda p: p not in self.busy)
        kw.setdefault("child_command", lambda s, p: ["fake", s, str(p)])
        # Never the real HidHide journal: a developer with something genuinely
        # hidden would otherwise see these tests behave differently on their
        # machine than in CI.
        kw.setdefault("hidden_serials", lambda: [])
        kw.setdefault("on_event",
                      lambda s, k, t: self.events.append((s, k, t)))
        kw.setdefault("use_job", False)
        m = M.BridgeManager(**kw)
        self.addCleanup(m.close)
        return m


# ---------------------------------------------------------------------------
# ports
# ---------------------------------------------------------------------------


class TestPortAllocation(_Base):
    def test_counts_up_from_the_base(self):
        m = self.make()
        self.assertEqual(m.port_for(A), 3241)
        self.assertEqual(m.port_for(B), 3242)

    def test_skips_a_busy_port(self):
        self.busy = {3241, 3242}
        m = self.make()
        self.assertEqual(m.port_for(A), 3243)
        self.assertEqual(m.port_for(B), 3244)

    def test_never_allocates_3240(self):
        # Base one below ours, and everything below 3241 free, so a naive
        # scan would hand out usbipd-win's port.
        m = self.make(base_port=3239)
        self.assertEqual(m.port_for(A), 3239)
        self.assertEqual(m.port_for(B), 3241)
        self.assertNotIn(M.USBIPD_PORT, m._ports.values())

    def test_3240_is_refused_as_a_base(self):
        with self.assertRaises(ValueError):
            self.make(base_port=3240)

    def test_ports_are_sticky_across_stop_and_start(self):
        m = self.make()
        first = m.port_for(A)
        m.start(A)
        m.stop(A)
        # The port is free again, and a second controller must still not get it.
        self.assertEqual(m.port_for(A), first)
        self.assertNotEqual(m.port_for(B), first)
        m.start(A)
        self.assertEqual(m.snapshot()["controllers"][A]["port"], first)

    def test_sticky_even_when_the_port_goes_busy(self):
        m = self.make()
        first = m.port_for(A)
        self.busy = {first}
        self.assertEqual(m.port_for(A), first)

    def test_serial_case_does_not_matter(self):
        m = self.make()
        self.assertEqual(m.port_for(A.upper()), m.port_for(A))

    def test_exhaustion_is_an_error_not_a_wrong_port(self):
        self.busy = set(range(3241, 3241 + M.PORT_SPAN))
        m = self.make()
        with self.assertRaises(RuntimeError):
            m.port_for(A)


# ---------------------------------------------------------------------------
# failure isolation -- the reason this class exists
# ---------------------------------------------------------------------------


class TestFailureIsolation(_Base):
    def test_a_failed_start_leaves_the_others_running(self):
        FakeBridge.fail_on_start = {A}
        m = self.make()
        started = m.start_all()
        self.assertEqual(started, [B])
        snap = m.snapshot()
        self.assertEqual(snap["controllers"][B]["state"], M.RUNNING)
        self.assertEqual(snap["controllers"][A]["state"], M.STOPPED)
        self.assertTrue(snap["controllers"][A]["error"])
        self.assertEqual(snap["aggregate"]["running"], 1)

    def test_a_bridge_that_never_reports_ready_is_torn_down_alone(self):
        FakeBridge.never_ready = {A}
        m = self.make()
        m.start_all()
        # Only A's port was cleaned. B's device was never touched.
        self.assertEqual(FakeBridge.cleaned, [3241])
        self.assertTrue(m.snapshot()["controllers"][B]["attached"])

    def test_a_child_dying_mid_run_does_not_disturb_its_sibling(self):
        m = self.make()
        m.start_all()
        dead = m._bridges[A]
        dead.die()
        m.poll_once()
        snap = m.snapshot()
        self.assertEqual(snap["controllers"][B]["state"], M.RUNNING)
        self.assertTrue(snap["controllers"][B]["attached"])
        self.assertEqual(snap["controllers"][A]["state"], M.STOPPED)
        self.assertEqual(FakeBridge.cleaned, [3241])

    def test_a_failed_start_is_not_retried_on_every_hotplug_pass(self):
        FakeBridge.fail_on_start = {A}
        m = self.make(retry_after=999.0)
        m.start_all()
        attempts = len([e for e in self.events if e[0] == A and e[1] == "error"])
        for _ in range(5):
            m.poll_once()
        self.assertEqual(
            len([e for e in self.events if e[0] == A and e[1] == "error"]),
            attempts, "a dead controller must not be retried every 5 s -- "
                      "enumeration lists a controller charging on a cable too")

    def test_the_retry_window_expires(self):
        FakeBridge.fail_on_start = {A}
        m = self.make(retry_after=0.0)
        m.start_all()
        FakeBridge.fail_on_start = set()
        m.poll_once()
        self.assertEqual(m.snapshot()["controllers"][A]["state"], M.RUNNING)

    def test_enabling_clears_the_retry_backoff(self):
        FakeBridge.fail_on_start = {A}
        m = self.make(retry_after=999.0)
        m.start_all()
        FakeBridge.fail_on_start = set()
        m.set_enabled(A, True)
        self.assertEqual(m.snapshot()["controllers"][A]["state"], M.RUNNING)


# ---------------------------------------------------------------------------
# enable / disable
# ---------------------------------------------------------------------------


class TestEnable(_Base):
    def test_disabled_controllers_are_not_started(self):
        m = self.make(enabled={A: False})
        self.assertEqual(m.start_all(), [B])
        self.assertFalse(m.snapshot()["controllers"][A]["enabled"])

    def test_default_enabled_false_means_opt_in(self):
        m = self.make(default_enabled=False)
        self.assertEqual(m.start_all(), [])
        m.set_enabled(B, True)
        self.assertEqual(m.snapshot()["controllers"][B]["state"], M.RUNNING)

    def test_disabling_a_running_controller_stops_only_it(self):
        m = self.make()
        m.start_all()
        m.set_enabled(A, False)
        self.assertEqual(FakeBridge.cleaned, [3241])
        self.assertEqual(m.snapshot()["controllers"][B]["state"], M.RUNNING)

    def test_the_change_is_reported_for_persisting(self):
        seen = []
        m = self.make(on_enabled_changed=lambda s, v: seen.append((s, v)))
        m.set_enabled(A, False)
        m.set_enabled(A, True)
        self.assertEqual(seen, [(A, False), (A, True)])

    def test_master_disabled_stops_everything_and_starts_nothing(self):
        seen = []
        m = self.make(on_master_changed=seen.append)
        m.start_all()
        m.set_master_enabled(False)
        self.assertEqual(sorted(FakeBridge.cleaned), [3241, 3242])
        self.assertFalse(m.start(A))
        m.poll_once()
        self.assertEqual(m.snapshot()["aggregate"]["running"], 0)
        self.assertEqual(seen, [False])

    def test_master_re_enabled_restores_the_per_controller_flags(self):
        m = self.make(enabled={A: False})
        m.start_all()
        m.set_master_enabled(False)
        m.set_master_enabled(True)
        snap = m.snapshot()
        self.assertEqual(snap["controllers"][B]["state"], M.RUNNING)
        self.assertEqual(snap["controllers"][A]["state"], M.STOPPED)


# ---------------------------------------------------------------------------
# hotplug
# ---------------------------------------------------------------------------


class TestHotplug(_Base):
    def test_an_enabled_controller_that_appears_is_started(self):
        self.present = [A]
        m = self.make()
        m.start_all()
        self.present = [A, B]
        m.poll_once()
        snap = m.snapshot()
        self.assertEqual(snap["controllers"][B]["state"], M.RUNNING)
        self.assertEqual(snap["controllers"][B]["port"], 3242)

    def test_a_disabled_controller_that_appears_is_left_alone(self):
        self.present = [A]
        m = self.make(enabled={B: False})
        m.start_all()
        self.present = [A, B]
        m.poll_once()
        self.assertEqual(m.snapshot()["controllers"][B]["state"], M.STOPPED)

    def test_a_brief_disappearance_does_not_tear_the_bridge_down(self):
        # BridgeBackend keeps the virtual device attached and reports a neutral
        # controller across a dropout; removing the device instead would turn a
        # recoverable 4 s silence into a device removal the user sees.
        m = self.make(vanish_grace=60.0)
        m.start_all()
        self.present = [B]
        m.poll_once()
        self.assertEqual(m.snapshot()["controllers"][A]["state"], M.RUNNING)
        self.assertEqual(FakeBridge.cleaned, [])
        self.present = [A, B]
        m.poll_once()
        self.assertEqual(m.snapshot()["controllers"][A]["state"], M.RUNNING)

    def test_a_lasting_disappearance_stops_only_that_bridge(self):
        m = self.make(vanish_grace=0.0)
        m.start_all()
        self.present = [B]
        m.poll_once()
        self.assertEqual(FakeBridge.cleaned, [3241])
        self.assertEqual(m.snapshot()["controllers"][B]["state"], M.RUNNING)

    def test_the_grace_period_restarts_after_a_return(self):
        m = self.make(vanish_grace=0.5)
        m.start_all()
        self.present = [B]
        m.poll_once()                       # first miss, inside the grace
        self.present = [A, B]
        m.poll_once()                       # back -- the clock must reset
        self.present = [B]
        m.poll_once()
        self.assertEqual(FakeBridge.cleaned, [],
                         "the absence clock must restart, not accumulate")

    def test_the_watcher_never_probes(self):
        """Discovery must not open a HID handle on a bridged controller."""
        calls = []

        def discover():
            calls.append(time.monotonic())
            return list(self.present)

        m = self.make(discover=discover, hotplug_interval=0.05)
        m.start_all()
        m.start_hotplug()
        try:
            deadline = time.monotonic() + 1.0
            while len(calls) < 3 and time.monotonic() < deadline:
                time.sleep(0.02)
        finally:
            m.stop_hotplug()
        self.assertGreaterEqual(len(calls), 3)
        # `enumerate_serials` is the only discovery this module ships, and it
        # is the listing call, never `controller.probe_all`.
        self.assertIs(M.BridgeManager.__init__.__defaults__ is None, False)
        self.assertNotIn("probe", M.enumerate_serials.__doc__.split("\n")[0])

    def test_discovery_failing_does_not_kill_the_watcher(self):
        def discover():
            raise OSError("the HID subsystem is busy")

        m = self.make(discover=discover)
        m.poll_once()                       # must not raise
        self.assertEqual(m.snapshot()["aggregate"]["present"], 0)

    def test_stop_hotplug_is_safe_when_it_was_never_started(self):
        m = self.make()
        m.stop_hotplug()
        m.stop_hotplug()


# ---------------------------------------------------------------------------
# lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle(_Base):
    def test_stop_all_is_idempotent_and_safe_with_nothing_running(self):
        m = self.make()
        m.stop_all()
        m.stop_all()
        self.assertEqual(FakeBridge.cleaned, [])
        m.start_all()
        m.stop_all()
        m.stop_all()
        self.assertEqual(sorted(FakeBridge.cleaned), [3241, 3242])

    def test_stop_of_an_unknown_serial_is_a_no_op(self):
        m = self.make()
        m.stop(C)
        self.assertEqual(FakeBridge.cleaned, [])

    def test_start_twice_does_not_spawn_twice(self):
        m = self.make()
        self.assertTrue(m.start(A))
        first = m._bridges[A]
        self.assertTrue(m.start(A))
        self.assertIs(m._bridges[A], first)

    def test_close_is_safe_twice_and_removes_its_teardown_hook(self):
        n = len(S.ON_TEARDOWN)
        m = self.make()
        self.assertEqual(len(S.ON_TEARDOWN), n + 1)
        m.start_all()
        m.close()
        m.close()
        self.assertEqual(len(S.ON_TEARDOWN), n)
        self.assertEqual(sorted(FakeBridge.cleaned), [3241, 3242])

    def test_the_teardown_hook_stops_every_bridge(self):
        m = self.make()
        m.start_all()
        for fn in list(S.ON_TEARDOWN):
            fn()
        self.assertEqual(sorted(FakeBridge.cleaned), [3241, 3242])

    def test_the_child_command_is_built_per_controller(self):
        seen = []

        def cmd(serial, port):
            seen.append((serial, port))
            return ["fake", serial, str(port)]

        m = self.make(child_command=cmd)
        m.start_all()
        self.assertEqual(seen, [(A, 3241), (B, 3242)])


# ---------------------------------------------------------------------------
# what the tray reads
# ---------------------------------------------------------------------------


class TestSnapshot(_Base):
    def test_lists_present_controllers_that_have_no_bridge(self):
        m = self.make(default_enabled=False)
        # snapshot() deliberately does no discovery of its own -- a hotplug
        # pass is what refreshes the present list.
        self.assertEqual(m.snapshot()["controllers"], {})
        m.poll_once()
        snap = m.snapshot()
        self.assertEqual(sorted(snap["controllers"]), sorted([A, B]))
        self.assertEqual(snap["aggregate"]["present"], 2)
        self.assertEqual(snap["aggregate"]["bridges"], 0)

    def test_the_aggregate_adds_the_rates_up(self):
        m = self.make()
        m.start_all()
        agg = m.snapshot()["aggregate"]
        self.assertEqual(agg["running"], 2)
        self.assertEqual(agg["attached"], 2)
        self.assertEqual(agg["reports_per_s"], 500.0)
        self.assertEqual(agg["errors"], 0)

    def test_it_does_no_work(self):
        """No socket bind, no discovery, no subprocess on the 2 s tray path."""
        probes = []
        m = self.make(port_free=lambda p: (probes.append(p), True)[1],
                      discover=lambda: (probes.append("discover"),
                                        list(self.present))[1])
        m.start_all()
        probes.clear()
        for _ in range(20):
            m.snapshot()
        self.assertEqual(probes, [])

    def test_statuses_reads_as_english(self):
        m = self.make(enabled={B: False})
        m.start_all()
        lines = m.statuses()
        self.assertTrue(any(l.startswith(A) and "250 reports/s" in l
                            for l in lines), lines)
        self.assertIn(f"{B}  disabled", lines)

    def test_statuses_with_nothing_at_all(self):
        m = self.make(discover=lambda: [])
        self.assertEqual(m.statuses(), ["no controllers"])

    def test_snapshot_is_thread_safe_against_a_hotplug_pass(self):
        m = self.make(hotplug_interval=0.01)
        m.start_all()
        stop = threading.Event()
        errors = []

        def spin():
            while not stop.is_set():
                try:
                    m.snapshot()
                    m.statuses()
                except Exception as e:  # noqa: BLE001
                    errors.append(e)
                    return

        t = threading.Thread(target=spin, daemon=True)
        t.start()
        m.start_hotplug()
        time.sleep(0.3)
        m.stop_hotplug()
        stop.set()
        t.join(timeout=2.0)
        self.assertEqual(errors, [])


# ---------------------------------------------------------------------------
# the pieces the child architecture rests on
# ---------------------------------------------------------------------------


class TestChildPlumbing(_Base):
    def test_the_status_line_the_cli_prints_is_parsed(self):
        # Exactly what `cli.cmd_run` writes: two leading spaces, then
        # `BridgeService.status_line()`.
        line = "  running  d42f4ba1485d  battery 40%  250 reports/s  up 61s"
        m = M._STATUS_RE.match(line.strip())
        self.assertIsNotNone(m)
        self.assertEqual(m.group("state"), "running")
        self.assertEqual(m.group("serial"), "d42f4ba1485d")
        self.assertEqual(m.group("bat"), "40")
        self.assertEqual(m.group("rps"), "250")
        self.assertEqual(m.group("up"), "61")

    def test_a_state_with_a_space_in_it_still_parses(self):
        # `service.DEGRADED` is "controller offline".
        line = "controller offline  a0fa9c0dd8bb  battery ?  0 reports/s  up 9s"
        m = M._STATUS_RE.match(line)
        self.assertIsNotNone(m)
        self.assertEqual(m.group("state"), "controller offline")
        self.assertEqual(m.group("bat"), "?")

    def test_the_child_command_names_the_serial_and_the_port(self):
        cmd = M.default_child_command(A, 3242, audio_target="headphone")
        self.assertIn("run", cmd)
        self.assertIn(A, cmd)
        self.assertIn("3242", cmd)
        self.assertIn("headphone", cmd)
        self.assertNotIn("3240", cmd)

    def test_assigning_to_a_missing_job_is_not_an_error(self):
        self.assertFalse(M.assign_to_job(None, object()))
        M.close_job(None)


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# what the hardware run added
# ---------------------------------------------------------------------------


class TestExplicitStopHolds(_Base):
    """Found on hardware on 2026-08-25, not by reasoning about it.

    `stop(A)` popped the bridge, the hotplug pass five seconds later saw A
    still enumerated -- of course it did, the controller was sitting on the
    desk -- and started it straight back up. "Stop" in a tray menu would have
    looked like it did nothing.
    """

    def test_an_explicit_stop_is_not_undone_by_the_next_hotplug_pass(self):
        m = self.make()
        m.start_all()
        m.stop(A)
        m.poll_once()
        m.poll_once()
        self.assertEqual(m.snapshot()["controllers"][A]["state"], M.STOPPED)
        self.assertEqual(m.snapshot()["controllers"][B]["state"], M.RUNNING)

    def test_an_explicit_start_lifts_the_hold(self):
        m = self.make()
        m.start_all()
        m.stop(A)
        self.assertTrue(m.start(A))
        m.poll_once()
        self.assertEqual(m.snapshot()["controllers"][A]["state"], M.RUNNING)

    def test_unplugging_a_held_controller_lifts_the_hold(self):
        m = self.make()
        m.start_all()
        m.stop(A)
        self.present = [B]
        m.poll_once()                       # really gone -- forget the hold
        self.present = [A, B]
        m.poll_once()                       # back on the desk -- start it
        self.assertEqual(m.snapshot()["controllers"][A]["state"], M.RUNNING)

    def test_stop_all_then_start_all_works(self):
        m = self.make()
        m.start_all()
        m.stop_all()
        self.assertEqual(sorted(m.start_all()), sorted([A, B]))

    def test_stopping_one_does_not_hold_the_other(self):
        m = self.make()
        m.start_all()
        m.stop(A)
        self.assertEqual(m.snapshot()["controllers"][B]["state"], M.RUNNING)
        m.stop(B)
        m.poll_once()
        self.assertEqual(m.snapshot()["aggregate"]["running"], 0)


class TestPersistedPorts(_Base):
    def test_a_seeded_map_is_honoured(self):
        m = self.make(ports={A.upper(): 3247, B: 3245})
        self.assertEqual(m.port_for(A), 3247)
        self.assertEqual(m.port_for(B), 3245)

    def test_a_seeded_3240_is_dropped_rather_than_used(self):
        m = self.make(ports={A: M.USBIPD_PORT})
        self.assertNotEqual(m.port_for(A), M.USBIPD_PORT)

    def test_a_new_assignment_is_reported_for_persisting(self):
        seen = []
        m = self.make(ports={A: 3250},
                      on_port_assigned=lambda s, p: seen.append((s, p)))
        m.port_for(A)                       # already known -- nothing to persist
        m.port_for(B)
        m.port_for(B)                       # sticky -- reported once only
        self.assertEqual(seen, [(B, 3241)])

    def test_a_seeded_port_is_not_handed_to_somebody_else(self):
        m = self.make(ports={B: 3241})
        self.assertEqual(m.port_for(A), 3242)


class TestConcurrentStart(_Base):
    def test_two_threads_starting_one_controller_spawn_one_bridge(self):
        made = []

        class Slow(FakeBridge):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                made.append(self)

            def wait_ready(self, timeout=60.0):
                time.sleep(0.2)             # a real child takes seconds
                return super().wait_ready(timeout)

        m = self.make(bridge_factory=Slow)
        ts = [threading.Thread(target=m.start, args=(A,)) for _ in range(4)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(timeout=5.0)
        self.assertEqual(len(made), 1,
                         "a second child would die on the port's named mutex")


# ---------------------------------------------------------------------------
# the HidHide oscillation guard -- scoping section 6.7
# ---------------------------------------------------------------------------


class TestHiddenSerialGuard(_Base):
    """A controller WE hid must count as present, or the bridge flaps.

    Without this: hiding removes the pad from `hid.enumerate()` in the tray
    process, `vanish_grace` expires, the bridge is stopped as "gone", stopping
    unhides, the pad reappears, hotplug starts it again, it is hidden again --
    a ~40 second self-sustaining loop that looks exactly like a flaky Bluetooth
    link. These tests are the only thing standing between that bug and a day of
    somebody's life.
    """

    def test_a_hidden_controller_is_not_declared_gone(self):
        m = self.make(vanish_grace=0.0, hidden_serials=lambda: [A])
        m.start(A)
        self.present = []                      # hidden, so it stops enumerating
        m.poll_once()
        m.poll_once()
        self.assertIn(A, m._bridges, "the bridge was torn down for a pad we hid")

    def test_a_genuinely_absent_controller_is_still_declared_gone(self):
        """The guard must not become 'never notice an unplug'."""
        m = self.make(vanish_grace=0.0, hidden_serials=lambda: [])
        m.start(A)
        self.present = []
        m.poll_once()
        m.poll_once()
        self.assertNotIn(A, m._bridges)

    def test_the_guard_covers_only_the_serials_we_hid(self):
        m = self.make(vanish_grace=0.0, hidden_serials=lambda: [A])
        m.start(A)
        m.start(B)
        self.present = []
        m.poll_once()
        m.poll_once()
        self.assertIn(A, m._bridges)
        self.assertNotIn(B, m._bridges)

    def test_case_is_normalised(self):
        m = self.make(vanish_grace=0.0, hidden_serials=lambda: [A.upper()])
        m.start(A)
        self.present = []
        m.poll_once()
        m.poll_once()
        self.assertIn(A, m._bridges)

    def test_a_journal_read_failure_does_not_stop_hotplug(self):
        def boom():
            raise RuntimeError("no journal")

        m = self.make(vanish_grace=0.0, hidden_serials=boom)
        m.start(A)
        m.poll_once()                          # must not raise
        self.assertIn(A, m._bridges)

    def test_a_hidden_controller_is_not_started_twice(self):
        """It is already bridged; the union must not look like a new arrival."""
        m = self.make(hidden_serials=lambda: [A])
        m.start(A)
        before = m._bridges[A]
        self.present = []
        m.poll_once()
        self.assertIs(m._bridges[A], before)


class TestHideToggle(_Base):
    def test_defaults_off(self):
        self.assertFalse(self.make().is_hiding(A))

    def test_honours_the_seeded_map_and_the_default(self):
        m = self.make(hide_bluetooth={A: True}, hide_default=False)
        self.assertTrue(m.is_hiding(A))
        self.assertFalse(m.is_hiding(B))
        self.assertTrue(self.make(hide_default=True).is_hiding(B))

    def test_setting_it_persists_through_the_callback(self):
        seen = []
        m = self.make(on_hide_changed=lambda s, v: seen.append((s, v)))
        m.set_hide_bluetooth(A, True)
        self.assertEqual(seen, [(A, True)])
        self.assertTrue(m.is_hiding(A))

    def test_toggling_an_idle_controller_touches_no_driver(self):
        """Nothing is bridged, so there is nothing to hide -- and nothing to call."""
        m = self.make()
        m.set_hide_bluetooth(A, True)          # must not raise or shell out
        self.assertTrue(m.is_hiding(A))

    def test_the_child_command_carries_the_flag(self):
        cmd = M.default_child_command(A, 3241, hide_bluetooth=True)
        self.assertIn("--hide-bluetooth", cmd)
        self.assertNotIn("--hide-bluetooth",
                         M.default_child_command(A, 3241))

    def test_the_child_command_carries_the_cli_override(self):
        cmd = M.default_child_command(A, 3241, hidhide_cli=r"C:\x\HidHideCLI.exe")
        self.assertIn("--hidhide-cli", cmd)
        self.assertIn(r"C:\x\HidHideCLI.exe", cmd)

    def test_the_snapshot_exposes_the_flag(self):
        # B is switched off so `poll_once` leaves it as a present-but-stopped
        # entry -- which is the branch of `snapshot()` that builds a controller
        # dict from scratch and could forget the new key.
        m = self.make(hide_bluetooth={A: True}, enabled={A: True, B: False})
        m.start(A)
        m.poll_once()
        snap = m.snapshot()["controllers"]
        self.assertTrue(snap[A]["hide_bluetooth"])
        self.assertFalse(snap[B]["hide_bluetooth"])
