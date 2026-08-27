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
                 usbip_exe=None, on_change=None, **kw):
        self.serial = serial.lower()
        self.port = port
        self.command = list(command)
        self.job = job
        self.on_event = on_event or (lambda s, k, t: None)
        self.on_change = on_change or (lambda s: None)
        self.error = None
        self.started = False
        self.stopped = False
        #: Was the last stop the impolite one? A disconnect teardown must not
        #: spend `stop_timeout` waiting for a graceful shutdown of a bridge
        #: whose controller is already gone.
        self.stopped_hard = None
        self._state = M.STOPPED
        self._rate = 0.0
        #: Set when the child first says the link is down, exactly as the real
        #: `ChildBridge._note_state` stamps it.
        self._offline_since = None

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

    def stop(self, hard: bool = False) -> None:
        self.stopped = True
        self.stopped_hard = bool(hard)
        self._state = M.STOPPED
        self._offline_since = None
        self._rate = 0.0
        FakeBridge.cleaned.append(self.port)

    def die(self) -> None:
        """The child crashed. Nothing else in the manager should notice."""
        self.stopped = True
        self.error = "the bridge process exited"
        self._state = M.ERROR
        self.on_change(self.serial)

    def go_offline(self, at=None) -> None:
        """The Bluetooth link dropped, and the child said so.

        What a real child does across a dropout: `BridgeService.snapshot()`
        turns `device_status()["connected"] is False` into DEGRADED, and the
        virtual device stays attached, because most of these recover. The
        timestamp is the child's own, as it is in `ChildBridge`.
        """
        self._state = M.DEGRADED
        self._rate = 0.0
        if self._offline_since is None:
            self._offline_since = time.monotonic() if at is None else at
        self.on_change(self.serial)

    def come_back(self) -> None:
        self._state = M.RUNNING
        self._rate = 250.0
        self._offline_since = None
        self.on_change(self.serial)

    def snapshot(self) -> dict:
        return {"serial": self.serial, "port": self.port, "state": self._state,
                "error": self.error, "pid": 4242, "battery_percent": 55,
                "reports_per_s": self._rate, "uptime_s": 7,
                "attached": self._state in (M.RUNNING, M.DEGRADED),
                "last_event": "", "offline_since": self._offline_since}


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
        #: Serials the manager asked to have unhidden, in order.
        self.unhidden: list = []
        #: One entry per exit sweep the manager ran.
        self.swept: list = []
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
        # machine than in CI -- and, since every stop repays its hide debt,
        # would have their own controller unhidden by a green test run.
        kw.setdefault("hidden_serials", lambda: [])
        kw.setdefault("unhide_serial", lambda s: self.unhidden.append(s))
        # Same reasoning for the exit sweep: it reads the real journal and
        # unhides anything whose owner is gone, which on a developer's machine
        # is their own controller.
        kw.setdefault("sweep_fn", lambda: self.swept.append(True))
        kw.setdefault("usbip_factory", lambda: None)
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


class TestOfflineIsVisible(_Base):
    """What the tray is told while a bridge's controller is offline."""

    def test_the_aggregate_separates_offline_from_bridged(self):
        m = self.make()
        m.start_all()
        m._bridges[A].go_offline()
        agg = m.snapshot()["aggregate"]
        # `running` keeps its meaning -- the device is still attached and the
        # game is still being fed -- and `degraded` is what lets the tray say
        # so without claiming the controller is there.
        self.assertEqual(agg["running"], 2)
        self.assertEqual(agg["degraded"], 1)

    def test_a_healthy_pair_reports_none_offline(self):
        m = self.make()
        m.start_all()
        self.assertEqual(m.snapshot()["aggregate"]["degraded"], 0)

    def test_the_offline_grace_is_shorter_than_the_vanish_grace(self):
        # The child has already spent bridge.LINK_DEAD_S deciding the link is
        # dead before this clock starts, so waiting a full vanish_grace on top
        # is waiting twice for the same answer.
        m = self.make()
        self.assertLess(m.offline_grace, m.vanish_grace)


class TestHotplugStartup(_Base):
    """When the first pass runs. See `start_hotplug`."""

    def test_the_first_pass_does_not_wait_for_the_interval(self):
        # Starting the program with a controller already switched on is the
        # common case, and an interval's silence at that moment reads as "it
        # did not see my controller" -- which is what sent a user looking for
        # Rescan. The interval separates the passes AFTER the first one.
        calls = []

        def discover():
            calls.append(time.monotonic())
            return list(self.present)

        m = self.make(discover=discover, hotplug_interval=30.0)
        m.start_hotplug()
        try:
            deadline = time.monotonic() + 2.0
            while not calls and time.monotonic() < deadline:
                time.sleep(0.01)
        finally:
            m.stop_hotplug()
        self.assertTrue(calls, "hotplug waited a full interval before looking")

    def test_a_controller_already_connected_is_bridged_without_a_click(self):
        m = self.make(hotplug_interval=30.0)
        m.start_hotplug()
        try:
            deadline = time.monotonic() + 2.0
            while len(m.snapshot()["controllers"]) < 2                     and time.monotonic() < deadline:
                time.sleep(0.01)
        finally:
            m.stop_hotplug()
        self.assertEqual(m.snapshot()["aggregate"]["running"], 2)

    def test_stopping_the_watcher_still_ends_the_thread_promptly(self):
        # The loop now polls before it waits, so the stop event has to be
        # checked on the way round rather than only on the way in.
        m = self.make(hotplug_interval=30.0)
        m.start_hotplug()
        t = m._watch
        m.stop_hotplug()
        self.assertFalse(t.is_alive())


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


# ---------------------------------------------------------------------------
# the child's own verdict on the Bluetooth link -- what cloaking hides from us
# ---------------------------------------------------------------------------


class TestOfflineTeardown(_Base):
    """Both halves of the bug a user reported with hiding switched on.

    Presence used to be computed two different ways. `poll_once` unioned the
    HidHide journal in, so a cloaked pad counted; `snapshot()` read the raw
    enumeration, which structurally CANNOT list a cloaked pad, so the same pad
    did not -- and the tray rendered "2 of 1 controllers bridged" and called a
    perfectly healthy hidden controller "not connected".

    Worse, the journal entry stands whether or not the controller is switched
    on, so the absence clock never started for a hidden pad and switching one
    off left its virtual device attached to Windows on the end of a dead radio
    link, for ever. The child knows better: it reports DEGRADED straight from
    `device_status()["connected"]`, which needs no enumeration and therefore
    still works through the cloak.
    """

    def test_a_hidden_healthy_controller_counts_as_present(self):
        m = self.make(hidden_serials=lambda: [A])
        m.start(A)
        self.present = []                      # cloaked: it cannot enumerate
        m.poll_once()
        snap = m.snapshot()
        self.assertTrue(snap["controllers"][A]["present"],
                        "a bridged pad we hid is not 'not connected'")
        self.assertEqual(snap["aggregate"]["present"], 1)
        self.assertEqual(snap["aggregate"]["running"], 1)

    def test_the_tray_can_never_be_told_more_are_running_than_are_present(self):
        m = self.make(hidden_serials=lambda: [A])
        m.start(A)
        m.start(B)
        self.present = [B]                     # A is cloaked, B is not
        m.poll_once()
        agg = m.snapshot()["aggregate"]
        self.assertEqual((agg["running"], agg["present"]), (2, 2),
                         "2 of 1 controllers bridged is this assertion failing")
        self.assertLessEqual(agg["running"], agg["present"])

    def test_a_bridge_started_before_any_hotplug_pass_is_counted_too(self):
        """`start()` on its own must not produce running > present either."""
        m = self.make(discover=lambda: [], hidden_serials=lambda: [])
        m.start(A)
        agg = m.snapshot()["aggregate"]
        self.assertLessEqual(agg["running"], agg["present"])

    def test_a_hidden_controller_that_goes_offline_is_torn_down(self):
        m = self.make(offline_grace=0.0, vanish_grace=999.0,
                      hidden_serials=lambda: [A])
        m.start(A)
        self.present = []
        m._bridges[A].go_offline()
        m.poll_once()
        self.assertNotIn(A, m._bridges,
                         "a cloaked pad that was switched off kept its virtual "
                         "device attached to Windows for ever")
        self.assertEqual(FakeBridge.cleaned, [3241])
        self.assertTrue(any(s == A and k == "warn" and "offline" in t
                            for s, k, t in self.events), self.events)

    def test_a_brief_dropout_inside_the_grace_does_not_tear_it_down(self):
        # Measured 2026-08-25: a 4 s silence tripped the link watchdog on both
        # controllers mid-soak and both recovered on their own. Tearing down
        # here would turn that into a device removal the user sees.
        m = self.make(offline_grace=60.0, vanish_grace=60.0,
                      hidden_serials=lambda: [A])
        m.start(A)
        self.present = []
        m._bridges[A].go_offline()
        m.poll_once()
        self.assertIn(A, m._bridges)
        self.assertEqual(FakeBridge.cleaned, [])
        m._bridges[A].come_back()
        m.poll_once()
        self.assertEqual(m.snapshot()["controllers"][A]["state"], M.RUNNING)

    def test_the_offline_clock_restarts_after_a_recovery(self):
        m = self.make(offline_grace=0.5)
        m.start(A)
        b = m._bridges[A]
        b.go_offline()
        m.poll_once()                          # first offline pass
        b.come_back()
        m.poll_once()                          # recovered -- the clock resets
        b.go_offline()
        m.poll_once()
        self.assertEqual(FakeBridge.cleaned, [],
                         "the offline clock must restart, not accumulate")

    def test_the_journal_stops_vouching_for_a_pad_whose_link_is_down(self):
        """The union must not mask a real disconnect.

        A journal entry records that WE hid this serial. It says nothing about
        whether the controller is switched on, and treating it as if it did is
        what stopped the absence clock from ever starting.
        """
        m = self.make(offline_grace=999.0, vanish_grace=999.0,
                      hidden_serials=lambda: [A])
        m.start(A)
        self.present = []
        m._bridges[A].go_offline()
        m.poll_once()
        self.assertNotIn(A, m._present)
        self.assertIn(A, m._missing_since,
                      "the absence clock never started, so the bridge never fell")

    def test_the_oscillation_guard_still_holds_for_a_healthy_hidden_pad(self):
        """Hiding must still not be able to start the ~40 s flap.

        Both graces at zero, so anything that can tear this bridge down will.
        The child says RUNNING, so nothing may.
        """
        m = self.make(vanish_grace=0.0, offline_grace=0.0,
                      hidden_serials=lambda: [A])
        m.start(A)
        self.present = []
        for _ in range(5):
            m.poll_once()
        self.assertIn(A, m._bridges)
        self.assertEqual(FakeBridge.cleaned, [])

    def test_an_offline_teardown_settles_instead_of_looping(self):
        # The pad is switched off but still enumerated -- a DualSense charging
        # on a cable does exactly that. Without the retry window this is a
        # start/tear-down loop that attaches a virtual device every pass.
        built = []

        def factory(*a, **kw):
            b = FakeBridge(*a, **kw)
            built.append(b)
            return b

        self.present = [A]
        m = self.make(bridge_factory=factory, offline_grace=0.0,
                      retry_after=999.0)
        m.start(A)
        built[0].go_offline()
        m.poll_once()
        for _ in range(5):
            m.poll_once()
        self.assertNotIn(A, m._bridges)
        self.assertEqual(len(built), 1)

    def test_a_controller_switched_back_on_is_bridged_again_by_itself(self):
        """The teardown must not leave a hold behind.

        A hold means "the user asked for this", and it only lifts when the
        serial leaves the present set -- which a pad that keeps enumerating
        never does. That would be a promise never to bridge this controller
        again until somebody clicked Start.
        """
        m = self.make(offline_grace=0.0, retry_after=0.0)
        m.start(A)
        first = m._bridges[A]
        first.go_offline()
        m.poll_once()                          # torn down, and the radio is back
        self.assertNotIn(A, m._held)
        self.assertIsNot(m._bridges[A], first)
        self.assertEqual(m.snapshot()["controllers"][A]["state"], M.RUNNING)

    def test_every_stop_repays_the_hide_debt_the_child_may_not_have(self):
        """A tray host has no console, so its children die without unhiding.

        `ChildBridge.stop()` is then a `TerminateProcess`: no atexit, no
        `BridgeService.stop()`, no unhide. A cloaked pad cannot be enumerated,
        so it cannot be seen to come back -- the controller stays gone for the
        rest of the session.
        """
        m = self.make(offline_grace=0.0)
        m.start(A)
        m.start(B)
        m.stop(B)
        self.assertEqual(self.unhidden, [B])
        m._bridges[A].go_offline()
        m.poll_once()
        self.assertEqual(self.unhidden, [B, A])

    def test_an_offline_bridge_is_still_counted_present_while_it_lives(self):
        """Otherwise the tray's own arithmetic goes negative mid-dropout."""
        m = self.make(offline_grace=999.0, vanish_grace=999.0,
                      hidden_serials=lambda: [A])
        m.start(A)
        self.present = []
        m._bridges[A].go_offline()
        m.poll_once()
        agg = m.snapshot()["aggregate"]
        self.assertEqual(agg["running"], 1)
        self.assertLessEqual(agg["running"], agg["present"])


# ---------------------------------------------------------------------------
# switching the controller off during a game
# ---------------------------------------------------------------------------


class TestPromptDisconnect(_Base):
    """How long the game keeps seeing a controller that is not there.

    The user-visible complaint this class exists for: switch the pad off
    mid-game and the virtual wired DualSense stayed attached, feeding neutral
    input, long enough to be indistinguishable from a hang. Three separate
    delays were stacked on top of each other, and all three are app-layer:

      * the child's status line only went out on its own timer;
      * the offline clock started when a POLL noticed, not when the link died;
      * the teardown then waited for the next poll, and asked the child nicely
        first -- a graceful shutdown for a controller that is already gone.
    """

    def test_two_witnesses_tear_down_on_the_short_grace(self):
        """Link dead AND not enumerated: the pad is off, not merely quiet."""
        m = self.make(offline_grace=999.0, gone_grace=0.0, vanish_grace=999.0)
        m.start(A)
        self.present = [B]                       # A is switched off
        m._bridges[A].go_offline()
        m.poll_once()
        self.assertNotIn(A, m._bridges)
        self.assertEqual(FakeBridge.cleaned, [3241],
                         "only the gone controller's port may be cleaned up")

    def test_one_witness_still_waits_out_the_long_grace(self):
        """A pad that is quiet but still enumerated may only be dropping out.

        Measured 2026-08-25: a 4 s silence tripped the link watchdog on both
        controllers mid-soak and both recovered. Enumeration did not blink.
        """
        m = self.make(offline_grace=999.0, gone_grace=0.0, vanish_grace=999.0)
        m.start(A)
        m._bridges[A].go_offline()               # still in self.present
        m.poll_once()
        self.assertIn(A, m._bridges)
        self.assertEqual(FakeBridge.cleaned, [])

    def test_the_short_grace_can_never_be_the_longer_of_the_two(self):
        """Two witnesses agreeing must shorten the wait, never lengthen it."""
        m = self.make(offline_grace=0.0, gone_grace=999.0)
        m.start(A)
        self.present = []
        m._bridges[A].go_offline()
        m.poll_once()
        self.assertNotIn(A, m._bridges)

    def test_the_clock_starts_when_the_link_died_not_when_a_poll_looked(self):
        """The child stamps it; the poll only reads it.

        Otherwise every disconnect costs a whole poll interval before anybody
        even starts counting -- and the pad has already been silent for
        `bridge.LINK_DEAD_S` by then.
        """
        m = self.make(offline_grace=5.0, gone_grace=5.0, vanish_grace=999.0)
        m.start(A)
        self.present = []
        # The child noticed six seconds ago; this is the first pass to see it.
        m._bridges[A].go_offline(at=time.monotonic() - 6.0)
        m.poll_once()
        self.assertNotIn(A, m._bridges,
                         "the grace was measured from the poll, not from the "
                         "moment the link died")

    def test_a_disconnect_teardown_does_not_wait_for_a_polite_shutdown(self):
        """There is nothing left for the child to shut down gracefully.

        On a console host the polite route is a Ctrl+Break and up to
        `stop_timeout` seconds of waiting -- seconds in which Windows still
        shows a game a wired DualSense on the end of a dead radio link.
        """
        m = self.make(offline_grace=0.0, gone_grace=0.0)
        m.start(A)
        b = m._bridges[A]
        self.present = []
        b.go_offline()
        m.poll_once()
        self.assertTrue(b.stopped_hard)

    def test_a_user_asking_for_stop_is_still_asked_politely(self):
        """The controller is right there; let its child disarm the mic."""
        m = self.make()
        m.start(A)
        b = m._bridges[A]
        m.stop(A)
        self.assertFalse(b.stopped_hard)

    def test_a_child_going_offline_wakes_the_watcher(self):
        """Without this the news waits out a whole poll interval, twice."""
        m = self.make(hotplug_interval=999.0)
        m.start(A)
        m._wake.clear()
        m._bridges[A].go_offline()
        self.assertTrue(m._wake.is_set(),
                        "a disconnect must not wait for the next tick")

    def test_a_child_dying_wakes_the_watcher_too(self):
        m = self.make(hotplug_interval=999.0)
        m.start(A)
        m._wake.clear()
        m._bridges[A].die()
        self.assertTrue(m._wake.is_set())

    def test_the_watcher_sleeps_only_until_the_grace_expires(self):
        """A 5 s interval must not add 5 s to a 3 s grace."""
        m = self.make(hotplug_interval=5.0, gone_grace=1.0, offline_grace=999.0)
        self.assertAlmostEqual(m._next_interval(), 5.0, places=2)
        m.start(A)
        self.present = []
        m._bridges[A].go_offline()
        m.poll_once()
        self.assertLessEqual(m._next_interval(), 1.0)
        self.assertGreater(m._next_interval(), 0.0)

    def test_the_interval_is_never_zero(self):
        """A grace of zero must not turn the watcher into a spin loop."""
        m = self.make(hotplug_interval=5.0, gone_grace=0.0, offline_grace=0.0)
        m.start(A)
        with m._lock:
            m._offline_since[A] = time.monotonic() - 100.0
        self.assertGreaterEqual(m._next_interval(), 0.1)


# ---------------------------------------------------------------------------
# switching it back on again
# ---------------------------------------------------------------------------


class TestReconnect(_Base):
    """Every transition has to converge, including the ones the user repeats.

    A teardown leaves a retry window behind so that a pad which enumerates
    while its radio is silent -- a DualSense charging on a cable does exactly
    that -- cannot become a start/tear-down loop. Left unqualified, that window
    is also a minute of nothing happening after somebody switches a controller
    off and straight back on, which is the single most common thing a user does
    when something looks wrong.
    """

    def test_a_power_cycle_clears_the_backoff(self):
        m = self.make(offline_grace=0.0, gone_grace=0.0, retry_after=999.0)
        m.start(A)
        self.present = [B]                       # switched off
        m._bridges[A].go_offline()
        m.poll_once()
        self.assertNotIn(A, m._bridges)
        m.poll_once()                            # still off: nothing happens
        self.assertNotIn(A, m._bridges)

        self.present = [A, B]                    # switched back on
        m.poll_once()
        self.assertIn(A, m._bridges,
                      "a controller switched back on waited out the retry "
                      "window it was given for being switched off")
        self.assertEqual(m.snapshot()["controllers"][A]["state"], M.RUNNING)

    def test_a_pad_that_never_left_enumeration_keeps_its_backoff(self):
        """The case the backoff exists for: still listed, radio silent."""
        m = self.make(offline_grace=0.0, gone_grace=0.0, retry_after=999.0)
        m.start(A)
        first = m._bridges[A]
        first.go_offline()                       # A stays in self.present
        m.poll_once()
        self.assertNotIn(A, m._bridges)
        for _ in range(5):
            m.poll_once()
        self.assertNotIn(A, m._bridges, "a start/tear-down loop")

    def test_a_reappearance_clears_the_recorded_error(self):
        FakeBridge.fail_on_start = {A}
        m = self.make(retry_after=999.0)
        m.start_all()
        self.assertIn(A, m._errors)
        self.present = [B]
        m.poll_once()
        FakeBridge.fail_on_start = set()
        self.present = [A, B]
        m.poll_once()
        self.assertNotIn(A, m._errors)
        self.assertEqual(m.snapshot()["controllers"][A]["state"], M.RUNNING)

    def test_off_on_off_on_settles_every_time(self):
        """Ten cycles, one bridge at the end of each 'on'."""
        m = self.make(offline_grace=0.0, gone_grace=0.0, retry_after=999.0)
        for _ in range(10):
            self.present = [A]
            m.poll_once()
            self.assertIn(A, m._bridges)
            m._bridges[A].go_offline()
            self.present = []
            m.poll_once()
            self.assertNotIn(A, m._bridges)
        self.assertEqual(len(FakeBridge.cleaned), 10)


# ---------------------------------------------------------------------------
# the way out
# ---------------------------------------------------------------------------


class TestTeardownIsFinal(_Base):
    """Closing has to mean closed, from any thread and at any moment.

    The failure this guards is not a leak of memory, it is a leak of DEVICE
    STATE: a bridge started while the manager is being torn down is a child
    nobody will stop, an attached virtual controller nobody will detach and a
    cloaked pad nobody will unhide.
    """

    def test_closing_stops_the_watcher_before_it_can_start_anything(self):
        m = self.make()
        m.close()
        self.present = [A, B]
        m.poll_once()
        self.assertEqual(m._bridges, {})
        self.assertFalse(m.start(A), "a closed manager must not start bridges")

    def test_closing_sweeps_the_hide_journal_last(self):
        """Whatever a hard-killed child never unhid for itself."""
        m = self.make()
        m.start(A)
        m.close()
        self.assertEqual(self.swept, [True])
        self.assertEqual(FakeBridge.cleaned, [3241])

    def test_the_teardown_hook_sweeps_too(self):
        """Ctrl+C and a closed console reach the hook, not `close()`."""
        m = self.make()
        m.start(A)
        m._teardown_hook()
        self.assertEqual(self.swept, [True])
        self.assertNotIn(A, m._bridges)

    def test_a_start_that_lands_after_a_stop_is_torn_back_down(self):
        """`start()` blocks for as long as the child takes to attach.

        Quit, the master switch, a Stop click and a disconnect teardown can all
        land inside that window, and none of them can stop a child that has not
        attached yet. Without the arrival check, what is left is an attached
        virtual controller with no owner.
        """
        gate = threading.Event()

        class Slow(FakeBridge):
            def wait_ready(self, timeout=60.0):
                gate.wait(5.0)
                return super().wait_ready(timeout)

        m = self.make(bridge_factory=Slow)
        t = threading.Thread(target=lambda: m.start(A))
        t.start()
        try:
            for _ in range(200):                 # until it is registered
                if A in m._bridges:
                    break
                time.sleep(0.01)
            m.stop(A)                            # the user clicked Stop
        finally:
            gate.set()
            t.join(timeout=5.0)
        self.assertNotIn(A, m._bridges)
        self.assertEqual(FakeBridge.cleaned, [3241, 3241],
                         "the bridge that attached after the stop was left "
                         "attached to Windows")
        self.assertIn(A, self.unhidden)

    def test_a_hotplug_pass_does_not_kill_a_start_in_flight(self):
        """A bridge is registered BEFORE its child has a pid.

        A pass that landed in that window used to read "not alive" as "the
        bridge process exited", report an error and tear down a bridge that was
        coming up perfectly well -- then back it off for a minute for good
        measure.
        """
        gate = threading.Event()

        class Slow(FakeBridge):
            def start(self):
                gate.wait(5.0)
                super().start()

        m = self.make(bridge_factory=Slow)
        t = threading.Thread(target=lambda: m.start(A))
        t.start()
        try:
            for _ in range(200):
                if A in m._bridges:
                    break
                time.sleep(0.01)
            m.poll_once()
            self.assertIn(A, m._bridges)
            self.assertEqual([e for e in self.events if e[1] == "error"], [])
        finally:
            gate.set()
            t.join(timeout=5.0)
        self.assertEqual(m.snapshot()["controllers"][A]["state"], M.RUNNING)


# ---------------------------------------------------------------------------
# what a previous run's death left behind
# ---------------------------------------------------------------------------


class _Res:
    def __init__(self, code=0, out=""):
        self.code, self.out = code, out

    @property
    def ok(self):
        return self.code == 0


class FakeUsbip:
    """`usbip port` as a table, plus a record of what was done to it."""

    def __init__(self, rows=()):
        self.rows = list(rows)
        self.calls: list = []

    def parse_ports(self):
        return list(self.rows)

    def stop_auto_reattach(self):
        self.calls.append("attach -X")
        return _Res()

    def detach(self, port_no):
        self.calls.append(("detach", port_no))
        return _Res()


class TestReconcileStale(_Base):
    """The startup sweep -- the only thing that can undo a `taskkill /F`.

    usbip's port table and its armed auto-re-attach both live in the driver and
    outlive every process on this side. Nothing in this program's own memory can
    reveal them, so reconciliation has to ask.
    """

    def test_the_auto_reattach_is_disarmed_unconditionally(self):
        """Nothing about a clean-looking machine reveals that it is armed."""
        u = FakeUsbip([])
        m = self.make(usbip_factory=lambda: u)
        self.assertEqual(m.reconcile_stale(), 0)
        self.assertEqual(u.calls, ["attach -X"])

    def test_a_device_whose_server_is_gone_is_detached(self):
        u = FakeUsbip([(1, "127.0.0.1:3241/1-1")])
        m = self.make(usbip_factory=lambda: u, port_free=lambda p: True)
        self.assertEqual(m.reconcile_stale(), 1)
        self.assertIn(("detach", 1), u.calls)

    def test_a_live_siblings_device_is_left_alone(self):
        """Its port is bound, which is the whole distinction.

        Detaching on "is anything attached?" is the regression that killed a
        30-minute soak once (usbip.py `our_ports`), and a second instance of
        this program is exactly the case that made it possible.
        """
        u = FakeUsbip([(1, "127.0.0.1:3241/1-1"), (2, "127.0.0.1:3242/1-1")])
        self.busy = {3242}
        m = self.make(usbip_factory=lambda: u,
                      port_free=lambda p: p not in self.busy)
        self.assertEqual(m.reconcile_stale(), 1)
        self.assertIn(("detach", 1), u.calls)
        self.assertNotIn(("detach", 2), u.calls)

    def test_a_real_remote_server_is_never_touched(self):
        u = FakeUsbip([(1, "192.168.1.50:3240/2-3")])
        m = self.make(usbip_factory=lambda: u, port_free=lambda p: True)
        self.assertEqual(m.reconcile_stale(), 0)
        self.assertEqual(u.calls, ["attach -X"])

    def test_an_unattributable_port_is_never_touched(self):
        """`usbip port` prints a device with no URL line if it cannot resolve."""
        u = FakeUsbip([(1, "")])
        m = self.make(usbip_factory=lambda: u, port_free=lambda p: True)
        self.assertEqual(m.reconcile_stale(), 0)

    def test_no_usbip_is_not_an_error(self):
        """A machine without usbip-win2 has nothing to reconcile."""
        m = self.make(usbip_factory=lambda: None)
        self.assertEqual(m.reconcile_stale(), 0)

    def test_a_failure_to_read_the_table_detaches_nothing(self):
        class Broken(FakeUsbip):
            def parse_ports(self):
                raise OSError("usbip.exe is not answering")

        u = Broken()
        m = self.make(usbip_factory=lambda: u)
        self.assertEqual(m.reconcile_stale(), 0)
        self.assertEqual(u.calls, ["attach -X"])


# ---------------------------------------------------------------------------
# service.py's teardown funnel -- reached from three different directions
# ---------------------------------------------------------------------------


class TestTeardownFunnel(unittest.TestCase):
    """`_teardown_all` is what atexit, the signal handlers and the console
    control handler all reach, and more than one of them fires per exit."""

    def setUp(self):
        self._hooks = list(S.ON_TEARDOWN)
        self.addCleanup(lambda: S.ON_TEARDOWN.__setitem__(
            slice(None), self._hooks))
        S.ON_TEARDOWN[:] = []
        # One test raises from a hook on purpose, and an unconfigured logger
        # prints that traceback to stderr -- a green run that looks broken.
        log = logging.getLogger("ds5app.service")
        was = log.propagate, log.disabled
        log.propagate, log.disabled = False, True
        self.addCleanup(lambda: setattr(log, "disabled", was[1]))
        self.addCleanup(lambda: setattr(log, "propagate", was[0]))

    def test_a_second_ctrl_c_does_not_interleave_a_second_teardown(self):
        """The second signal arrives on the SAME thread, mid-teardown.

        Interleaving two sets of stops is how a bridge ends up half detached --
        one pass issuing `attach -X` while the other issues the detach it was
        protecting. A plain lock would deadlock here instead, which is worse:
        the impatient second Ctrl+C would hang the program.
        """
        calls = []

        def hook():
            calls.append("in")
            S._teardown_all()          # the second Ctrl+C, re-entering
            calls.append("out")

        S.ON_TEARDOWN.append(hook)
        S._teardown_all()
        self.assertEqual(calls, ["in", "out"], "re-entry must return, not "
                                               "recurse and not deadlock")

    def test_another_thread_waits_rather_than_running_in_parallel(self):
        """A tray Quit on the message-loop thread, and SIGTERM on the main one.

        `atexit` must not be allowed to return while another thread is still
        detaching devices, so the second caller waits and then finds everything
        already stopped.
        """
        started = threading.Event()
        release = threading.Event()
        order = []

        def slow():
            order.append("in")
            started.set()
            release.wait(5.0)
            order.append("out")

        S.ON_TEARDOWN.append(slow)
        first = threading.Thread(target=S._teardown_all)
        first.start()
        try:
            self.assertTrue(started.wait(5.0))
            second = threading.Thread(target=S._teardown_all)
            second.start()
            second.join(0.3)
            self.assertTrue(second.is_alive(),
                            "two teardowns ran at once")
        finally:
            release.set()
            first.join(5.0)
            second.join(5.0)
        self.assertEqual(order, ["in", "out", "in", "out"],
                         "the two passes overlapped")

    def test_one_failing_hook_does_not_stop_the_others(self):
        ran = []
        S.ON_TEARDOWN.append(lambda: (_ for _ in ()).throw(RuntimeError("x")))
        S.ON_TEARDOWN.append(lambda: ran.append(True))
        S._teardown_all()
        self.assertEqual(ran, [True])
