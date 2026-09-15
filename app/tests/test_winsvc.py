"""winsvc.py -- the Windows service, with no service control manager.

`sc.exe` is replaced by a recorder (the same trick test_autostart.py plays on
schtasks), the supervisor's child and console session are fakes, and the
clock is a counter: what is tested is the POLICY -- what `install` asks the
SCM for, how a crashed child is restarted and how a quitting one is not, what
a console-session change does, and the stop handshake with its timeout --
none of which needs a service, a session or a process.

The SCM glue itself (`run_service`: StartServiceCtrlDispatcher, HandlerEx,
SetServiceStatus) and `launch_in_session` were proven by the 2026-09-15 spike
on the development machine (docs/installer-handoff.md); they are ctypes over
kernel32/advapi32 and cannot be unit tested honestly.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path


def _load(name: str):
    try:
        return __import__(f"ds5app.{name}", fromlist=[name])
    except ImportError:
        path = Path(__file__).resolve().parents[1] / "ds5app" / f"{name}.py"
        spec = importlib.util.spec_from_file_location(f"_ds5app_{name}", path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module


W = _load("winsvc")
logging.getLogger("ds5app.winsvc").addHandler(logging.NullHandler())
logging.getLogger("ds5app.winsvc").propagate = False

EXE = r"C:\Users\me\AppData\Local\ds5bridge\app\ds5bridge.exe"

QUERY_RUNNING = """
SERVICE_NAME: ds5bridge
        TYPE               : 10  WIN32_OWN_PROCESS
        STATE              : 4  RUNNING
                                (STOPPABLE, NOT_PAUSABLE, ACCEPTS_SHUTDOWN)
        WIN32_EXIT_CODE    : 0  (0x0)
"""
QUERY_STOPPED = QUERY_RUNNING.replace("4  RUNNING", "1  STOPPED")
QC_AUTO = """
[SC] QueryServiceConfig SUCCESS

SERVICE_NAME: ds5bridge
        TYPE               : 10  WIN32_OWN_PROCESS
        START_TYPE         : 2   AUTO_START
        ERROR_CONTROL      : 1   NORMAL
        BINARY_PATH_NAME   : "C:\\x\\ds5bridge.exe" service
        DISPLAY_NAME       : ds5bridge
"""
QC_DEMAND = QC_AUTO.replace("2   AUTO_START", "3   DEMAND_START")
ABSENT = "[SC] EnumQueryServicesStatus:OpenService FAILED 1060:\n\nThe specified service does not exist as an installed service.\n"


class FakeSc:
    """A tiny SCM: one service, its state and start type, every call recorded."""

    def __init__(self, present=False, state="STOPPED", start="auto"):
        self.present = present
        self.state = state
        self.start = start
        self.calls = []
        #: How many `query` calls a start/stop takes to land (the real SCM
        #: answers START_PENDING for a moment).
        self.pending = 0

    def __call__(self, args):
        self.calls.append(list(args))
        verb = args[0]
        if verb == "query":
            if not self.present:
                return 1060, ABSENT
            if self.pending > 0:
                self.pending -= 1
                return 0, QUERY_RUNNING.replace("4  RUNNING", "2  START_PENDING")
            return 0, (QUERY_RUNNING if self.state == "RUNNING" else QUERY_STOPPED)
        if verb == "qc":
            if not self.present:
                return 1060, ABSENT
            return 0, (QC_AUTO if self.start == "auto" else QC_DEMAND)
        if verb == "create":
            if self.present:
                return 1073, "[SC] CreateService FAILED 1073:\n\nThe specified service already exists.\n"
            self.present = True
            self.start = args[args.index("start=") + 1]
            return 0, "[SC] CreateService SUCCESS"
        if verb == "config":
            if "start=" in args:
                self.start = args[args.index("start=") + 1]
            return 0, "[SC] ChangeServiceConfig SUCCESS"
        if verb in ("description", "failure"):
            return 0, "[SC] SUCCESS"
        if verb == "delete":
            self.present = False
            return 0, "[SC] DeleteService SUCCESS"
        if verb == "start":
            if self.state == "RUNNING":
                return 1056, "An instance of the service is already running."
            self.state = "RUNNING"
            return 0, "START_PENDING"
        if verb == "stop":
            if self.state != "RUNNING":
                return 1062, "The service has not been started."
            self.state = "STOPPED"
            return 0, "STOP_PENDING"
        return 1, "unknown"

    def verbs(self):
        return [c[0] for c in self.calls]


class _Rig(unittest.TestCase):
    def setUp(self):
        self.sc = FakeSc()
        self._saved = (W._run_sc, W.available, W.service_exe)
        W._run_sc = self.sc
        W.available = lambda: True
        W.service_exe = lambda: EXE

    def tearDown(self):
        W._run_sc, W.available, W.service_exe = self._saved


class QueryTests(_Rig):
    def test_absent(self):
        self.assertIsNone(W.query())
        self.assertFalse(W.installed())
        self.assertFalse(W.is_running())
        self.assertFalse(W.starts_at_boot())
        self.assertEqual(W.status_text(), "not installed")

    def test_running_and_automatic(self):
        self.sc.present, self.sc.state = True, "RUNNING"
        q = W.query()
        self.assertEqual(q["state"], "RUNNING")
        self.assertEqual(q["start"], "AUTO_START")
        self.assertIn("ds5bridge.exe", q["binpath"])
        self.assertTrue(W.is_running())
        self.assertTrue(W.starts_at_boot())
        self.assertEqual(W.status_text(), "running, starts with Windows")

    def test_stopped_and_manual(self):
        self.sc.present, self.sc.start = True, "demand"
        self.assertFalse(W.is_running())
        self.assertFalse(W.starts_at_boot())
        self.assertEqual(W.status_text(), "stopped, manual start")


class InstallTests(_Rig):
    def test_install_creates_with_the_documented_shape(self):
        W.install()
        create = [c for c in self.sc.calls if c[0] == "create"][0]
        self.assertEqual(create[1], W.SERVICE_NAME)
        self.assertEqual(create[create.index("binPath=") + 1], f'"{EXE}" service')
        self.assertEqual(create[create.index("start=") + 1], "auto")
        self.assertEqual(create[create.index("obj=") + 1], "LocalSystem")
        # Description and a failure policy follow, so the SCM restarts a
        # dead supervisor on its own.
        self.assertIn("description", self.sc.verbs())
        failure = [c for c in self.sc.calls if c[0] == "failure"][0]
        self.assertIn("actions=", failure)
        self.assertTrue(self.sc.present)

    def test_install_over_an_existing_service_reconfigures_it(self):
        self.sc.present, self.sc.start = True, "demand"
        W.install()
        self.assertIn("create", self.sc.verbs())      # tried, answered 1073
        self.assertIn("config", self.sc.verbs())      # then updated in place
        self.assertEqual(self.sc.start, "auto")

    def test_install_manual_start(self):
        W.install(start_at_boot=False)
        create = [c for c in self.sc.calls if c[0] == "create"][0]
        self.assertEqual(create[create.index("start=") + 1], "demand")

    def test_bin_path_quotes_the_exe(self):
        self.assertEqual(W.bin_path(r"C:\p q\ds5bridge.exe"), '"C:\\p q\\ds5bridge.exe" service')

    def test_no_exe_is_a_clear_error(self):
        W.service_exe = lambda: ""
        with self.assertRaises(W.ServiceError):
            W.install()

    def test_uninstall_absent_is_fine(self):
        W.uninstall()
        self.assertNotIn("delete", self.sc.verbs())

    def test_uninstall_stops_then_deletes(self):
        self.sc.present, self.sc.state = True, "RUNNING"
        W.uninstall()
        self.assertEqual([v for v in self.sc.verbs() if v in ("stop", "delete")],
                         ["stop", "delete"])
        self.assertFalse(self.sc.present)

    def test_set_start_at_boot(self):
        self.sc.present = True
        W.set_start_at_boot(False)
        self.assertEqual(self.sc.start, "demand")
        W.set_start_at_boot(True)
        self.assertEqual(self.sc.start, "auto")

    def test_set_start_at_boot_without_a_service_is_an_error(self):
        with self.assertRaises(W.ServiceError):
            W.set_start_at_boot(True)


class StartStopTests(_Rig):
    def setUp(self):
        super().setUp()
        self._sleep = W.time.sleep
        W.time.sleep = lambda s: None       # _wait_state polls; no real waiting

    def tearDown(self):
        W.time.sleep = self._sleep
        super().tearDown()

    def test_start_waits_for_running(self):
        self.sc.present = True
        self.sc.pending = 2
        W.start(wait_s=10)
        self.assertEqual(self.sc.state, "RUNNING")

    def test_start_when_already_running_is_success(self):
        self.sc.present, self.sc.state = True, "RUNNING"
        W.start(wait_s=1)                   # 1056 is not an error

    def test_start_without_a_service_is_an_error(self):
        with self.assertRaises(W.ServiceError):
            W.start()

    def test_stop_waits_for_stopped(self):
        self.sc.present, self.sc.state = True, "RUNNING"
        W.stop(wait_s=5)
        self.assertEqual(self.sc.state, "STOPPED")

    def test_stop_when_stopped_or_absent_is_nothing(self):
        W.stop()
        self.sc.present = True
        W.stop()
        self.assertNotIn("stop", self.sc.verbs())


class MigrationTests(unittest.TestCase):
    def test_config_is_copied_once_and_journal_moved(self):
        with tempfile.TemporaryDirectory() as tmp:
            src, dst = os.path.join(tmp, "user"), os.path.join(tmp, "svc")
            os.makedirs(os.path.join(src, "hidden"))
            with open(os.path.join(src, "config.json"), "w") as f:
                json.dump({"dashboard_port": 9000}, f)
            with open(os.path.join(src, "hidden", "abc.json"), "w") as f:
                json.dump({"serial": "abc"}, f)
            done = W.migrate_user_config(src, dst)
            self.assertEqual(sorted(os.path.basename(d) for d in done),
                             ["abc.json", "config.json"])
            with open(os.path.join(dst, "config.json")) as f:
                self.assertEqual(json.load(f)["dashboard_port"], 9000)
            self.assertTrue(os.path.exists(os.path.join(dst, "hidden", "abc.json")))
            self.assertFalse(os.path.exists(os.path.join(src, "hidden", "abc.json")))
            # The user's config stays where it was (a by-hand tray still reads it).
            self.assertTrue(os.path.exists(os.path.join(src, "config.json")))
            # A second run changes nothing: the service's copy is now the truth.
            with open(os.path.join(src, "config.json"), "w") as f:
                json.dump({"dashboard_port": 1}, f)
            self.assertEqual(W.migrate_user_config(src, dst), [])
            with open(os.path.join(dst, "config.json")) as f:
                self.assertEqual(json.load(f)["dashboard_port"], 9000)

    def test_nothing_to_migrate(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(W.migrate_user_config(os.path.join(tmp, "nope"),
                                                   os.path.join(tmp, "svc")), [])
            self.assertTrue(os.path.isdir(os.path.join(tmp, "svc")))


class ChildEnvTests(unittest.TestCase):
    def test_child_env_points_config_at_program_data_and_names_the_event(self):
        env = W.child_env({"PATH": "x"})
        self.assertEqual(env["PATH"], "x")
        self.assertEqual(env["DS5_CONFIG"], W.program_data_dir())
        self.assertEqual(env[W.STOP_EVENT_ENV], W.STOP_EVENT_NAME)
        self.assertTrue(env["DS5_CONFIG"].lower().endswith("ds5bridge"))

    def test_split_args(self):
        self.assertEqual(W._split_args("tray --service-child"), ["tray", "--service-child"])
        self.assertEqual(W._split_args('-c "import x"'), ["-c", "import x"])
        self.assertEqual(W._split_args(""), [])

    def test_log_stream_writes_whole_lines(self):
        lines = []
        logger = logging.getLogger("test.winsvc.stream")
        logger.propagate = False

        class H(logging.Handler):
            def emit(self, record):
                lines.append(record.getMessage())
        logger.addHandler(H())
        logger.setLevel(logging.INFO)
        s = W.LogStream(logger)
        s.write("  half")
        self.assertEqual(lines, [])
        s.write(" line\n  second\n")
        self.assertEqual(lines, ["  half line", "  second"])
        s.write("tail")
        s.flush()
        self.assertEqual(lines[-1], "tail")
        self.assertFalse(s.isatty())


# ---------------------------------------------------------------------------
# the supervisor
# ---------------------------------------------------------------------------


class FakeChild:
    """A child whose `wait` walks a script: None = still running, an int =
    exited with that code. `on_stop_exit` is what it does when asked to stop
    (None = ignores the request, so the supervisor has to kill it)."""

    def __init__(self, pid, session, waits=(), on_stop_exit=0):
        self.pid = pid
        self.session = session
        self._waits = list(waits)
        self.on_stop_exit = on_stop_exit
        self.stop_asked = False
        self.killed = False
        self.closed = False
        self.exit = None
        #: The rig's clock: every wait is a second of the child's life.
        self.clock = None

    def poll(self):
        return self.exit

    def wait(self, timeout_s):
        if self.clock is not None:
            self.clock.t += timeout_s
        if self.exit is not None:
            return self.exit
        if self.killed:
            self.exit = 1
            return 1
        if self.stop_asked and self.on_stop_exit is not None:
            self.exit = self.on_stop_exit
            return self.exit
        if self._waits:
            r = self._waits.pop(0)
            if r is not None:
                self.exit = r
            return r
        return None

    def ask_to_stop(self):
        self.stop_asked = True

    def kill(self):
        self.killed = True

    def close(self):
        self.closed = True


class FakeClock:
    def __init__(self, step=1.0):
        self.t = 0.0
        self.step = step

    def __call__(self):
        self.t += self.step
        return self.t


class SupervisorRig(unittest.TestCase):
    def build(self, children, sessions=None, **kw):
        """A supervisor over a queue of fake children. When the queue runs
        dry the next spawn requests a stop, so every scenario terminates."""
        self.children = list(children)
        self.spawned = []
        self.sleeps = []
        #: How many sleeps had happened when each child was spawned: a
        #: restart after a crash shows a backoff before it, a move after a
        #: session change does not.
        self.spawn_after_sleeps = []
        self.clock = FakeClock()
        sessions = list(sessions) if sessions is not None else [1]
        self.sessions = sessions

        def session_fn():
            if len(sessions) > 1:
                return sessions.pop(0)
            return sessions[0]

        def spawn(session):
            if not self.children:
                self.sup.request_stop()
                raise RuntimeError("no more children scripted")
            c = self.children.pop(0)
            c.session = session
            c.clock = self.clock
            self.spawned.append(c)
            self.spawn_after_sleeps.append(len(self.sleeps))
            return c

        def sleep(s):
            self.sleeps.append(s)
            return self.sup.stopping

        self.sup = W.Supervisor(spawn, session_fn, sleep=sleep, clock=self.clock,
                                child_stop_s=kw.pop("child_stop_s", 5.0),
                                backoff_min_s=2.0, backoff_max_s=8.0,
                                stable_s=kw.pop("stable_s", 100.0), poll_s=1.0)
        return self.sup


class SupervisorTests(SupervisorRig):
    def test_a_crashed_child_is_restarted_with_doubling_backoff(self):
        sup = self.build([FakeChild(1, 1, waits=[None, 1]),
                          FakeChild(2, 1, waits=[1]),
                          FakeChild(3, 1, waits=[1])])
        self.assertEqual(sup.run(), "stop")
        self.assertEqual([c.pid for c in self.spawned], [1, 2, 3])
        # 2, 4, 8 -- the third sleep is the one after the last scripted child
        # (the spawn that found the queue empty requested the stop).
        self.assertEqual(self.sleeps[:3], [2.0, 4.0, 8.0])
        self.assertTrue(all(c.closed for c in self.spawned))

    def test_backoff_resets_after_a_stable_run(self):
        # Two quick crashes push the delay to 4 s; child 3 then runs 200
        # fake seconds (each wait advances the clock a second) before dying
        # -- longer than stable_s -- so its restart is back at the minimum.
        sup = self.build([FakeChild(1, 1, waits=[1]),
                          FakeChild(2, 1, waits=[1]),
                          FakeChild(3, 1, waits=[None] * 200 + [1]),
                          FakeChild(4, 1, waits=[1])], stable_s=100.0)
        sup.run()
        self.assertEqual(self.sleeps[:3], [2.0, 4.0, 2.0])

    def test_the_user_quitting_stops_the_service_instead_of_restarting(self):
        sup = self.build([FakeChild(1, 1, waits=[None, W.EXIT_STOP_SERVICE]),
                          FakeChild(2, 1)])
        self.assertEqual(sup.run(), "quit")
        self.assertEqual(len(self.spawned), 1)
        self.assertEqual(self.sleeps, [])

    def test_a_stop_request_asks_the_child_and_waits(self):
        child = FakeChild(1, 1, waits=[None] * 50, on_stop_exit=0)
        sup = self.build([child])
        # Stop after the first poll.
        orig_wait = child.wait

        def wait(t):
            sup.request_stop()
            return orig_wait(t)
        child.wait = wait
        self.assertEqual(sup.run(), "stop")
        self.assertTrue(child.stop_asked)
        self.assertFalse(child.killed)
        self.assertTrue(child.closed)

    def test_a_child_that_ignores_the_stop_is_terminated_after_the_grace(self):
        child = FakeChild(1, 1, waits=[None] * 50, on_stop_exit=None)
        statuses = []
        sup = self.build([child], child_stop_s=5.0)
        sup._on_status = lambda *a: statuses.append(a)
        orig_wait = child.wait

        def wait(t):
            sup.request_stop()
            return orig_wait(t)
        child.wait = wait
        self.assertEqual(sup.run(), "stop")
        self.assertTrue(child.stop_asked)
        self.assertTrue(child.killed)
        # STOP_PENDING checkpoints went to the SCM while it waited.
        self.assertTrue(statuses)
        self.assertEqual(statuses[0][0], "STOP_PENDING")
        self.assertEqual(statuses[0][1], 1)
        self.assertGreater(statuses[-1][1], statuses[0][1])

    def test_a_console_session_change_moves_the_child(self):
        first = FakeChild(1, 1, waits=[None] * 50, on_stop_exit=0)
        second = FakeChild(2, 2, waits=[1])
        # Session 1 for the first poll, then 2.
        sup = self.build([first, second], sessions=[1, 1, 2, 2, 2])
        sup.run()
        self.assertTrue(first.stop_asked)
        self.assertFalse(first.killed)
        self.assertEqual(first.session, 1)
        self.assertEqual(second.session, 2)
        # A move is not a crash: no backoff before the new child; the first
        # sleep is the one after `second` died, at the minimum.
        self.assertEqual(self.spawn_after_sleeps, [0, 0])
        self.assertEqual(self.sleeps[0], 2.0)

    def test_no_console_session_yet_is_waited_for(self):
        child = FakeChild(1, 1, waits=[1])
        sup = self.build([child], sessions=[None, None, 1])
        sup.run()
        self.assertEqual(child.session, 1)
        self.assertEqual(self.sleeps[:2], [2.0, 2.0])

    def test_a_failed_spawn_backs_off_and_retries(self):
        calls = []
        child = FakeChild(1, 1, waits=[W.EXIT_STOP_SERVICE])

        def spawn(session):
            calls.append(session)
            if len(calls) == 1:
                raise RuntimeError("CreateProcessAsUser failed")
            return child
        sleeps = []
        sup = W.Supervisor(spawn, lambda: 1, sleep=lambda s: sleeps.append(s),
                           clock=FakeClock(), backoff_min_s=2.0)
        self.assertEqual(sup.run(), "quit")
        self.assertEqual(len(calls), 2)
        self.assertEqual(sleeps, [2.0])


class DriverWaitTests(_Rig):
    def test_absent_driver_means_nothing_to_wait_for(self):
        self.assertTrue(W.wait_for_driver(timeout_s=0, sleep=lambda s: None))

    def test_running_driver(self):
        self.sc.present, self.sc.state = True, "RUNNING"
        self.assertTrue(W.wait_for_driver(timeout_s=0, sleep=lambda s: None))

    def test_a_stopped_driver_times_out_but_does_not_block_the_service(self):
        self.sc.present, self.sc.state = True, "STOPPED"
        self.assertFalse(W.wait_for_driver(timeout_s=0, sleep=lambda s: None))


if __name__ == "__main__":
    unittest.main()
