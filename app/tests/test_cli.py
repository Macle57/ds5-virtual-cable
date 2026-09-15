"""The two parts of `cli.py` that are not argument parsing.

`run_loop` is the status channel every `BridgeManager` child speaks on, and the
only way a parent hears that a Bluetooth link has dropped; the tests below hold
it to reporting a state change when it happens rather than when a timer fires.

`harden_streams` -- the guard that lets the tray run under `pythonw.exe`.

A GUI-subsystem interpreter has no standard handles, so CPython sets
`sys.stdout` and `sys.stderr` to None and the first `print()` anywhere below
raises AttributeError -- in a process with no console for the traceback to reach.
The frozen windowed build has guarded against this since it existed
(`packaging/entry_tray.py`); `cli.main()` is the same guard for the SOURCE form,
which is what `autostart.build_command()` registers on a checkout and what
`tools/dev_tray.ps1 -Windowed` starts.

Verified against a real no-console `pythonw.exe`, which is where this was found;
these tests are the hardware-free half, so a regression fails in CI rather than
in somebody's Run key.
"""

from __future__ import annotations

import contextlib
import io
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ds5app import cli as C    # noqa: E402


class RunLoopTests(unittest.TestCase):
    """`run_loop` is the status channel a `BridgeManager` child speaks on.

    The state in these lines is the only way a parent learns that a Bluetooth
    link has dropped -- enumeration cannot tell it, because a cloaked pad is
    not enumerable. So a state change must not wait for the status timer: on a
    30 s timer it could take half a minute for a parent to hear that the
    controller a game is holding has been switched off.
    """

    class FakeService:
        """A `BridgeService` that walks a scripted list of states."""

        def __init__(self, states):
            self.states = list(states)
            self.state = self.states[0]
            self.lines = 0

        def snapshot(self):
            if len(self.states) > 1:
                self.states.pop(0)
            self.state = self.states[0]
            return {"state": self.state, "serial": "d42f4ba1485d",
                    "battery_percent": 50, "reports_per_s": 250.0,
                    "uptime_s": 3}

        def status_line(self, snap=None):
            self.lines += 1
            assert snap is not None, ("the loop must format the snapshot it "
                                      "already took, not provoke another")
            return f"{snap['state']}  d42f4ba1485d  battery 50%  250 reports/s"

    def _run(self, states, status_every=30.0):
        svc = self.FakeService(states)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            C.run_loop(svc, status_every, tick=0.0)
        return svc, out.getvalue()

    def test_a_disconnect_is_reported_without_waiting_for_the_timer(self):
        svc, text = self._run(["running", "controller offline", "error"])
        self.assertIn("controller offline", text)

    def test_a_steady_state_does_not_spam(self):
        """A 30 s timer means one line per 30 s, not one per tick."""
        svc, text = self._run(["running"] * 20 + ["error"])
        self.assertLessEqual(len(text.strip().splitlines()), 2)

    def test_a_recovery_is_reported_too(self):
        svc, text = self._run(["controller offline", "running", "error"])
        lines = text.strip().splitlines()
        self.assertTrue(any("running" in ln for ln in lines), lines)

    def test_it_returns_when_the_bridge_fails(self):
        svc, text = self._run(["running", "error"])
        self.assertEqual(svc.state, "error")


class HardenStreams(unittest.TestCase):
    def setUp(self):
        self._saved = (sys.stdout, sys.stderr)

    def tearDown(self):
        sys.stdout, sys.stderr = self._saved

    def test_replaces_none(self):
        sys.stdout = sys.stderr = None
        C.harden_streams()
        self.assertIsInstance(sys.stdout, C._Sink)
        self.assertIsInstance(sys.stderr, C._Sink)

    def test_the_replacement_survives_being_printed_to(self):
        sys.stdout = sys.stderr = None
        C.harden_streams()
        print("this must not raise")          # the actual failure being guarded
        print("nor this", file=sys.stderr)
        sys.stdout.flush()
        self.assertFalse(sys.stdout.isatty())

    def test_leaves_a_real_stream_alone(self):
        real = io.StringIO()
        sys.stdout = sys.stderr = real
        C.harden_streams()
        self.assertIs(sys.stdout, real)
        self.assertIs(sys.stderr, real)

    def test_replaces_only_the_missing_one(self):
        real = io.StringIO()
        sys.stdout, sys.stderr = None, real
        C.harden_streams()
        self.assertIsInstance(sys.stdout, C._Sink)
        self.assertIs(sys.stderr, real)



class ServiceVerbTests(unittest.TestCase):
    """`ds5bridge service <action>` reaches winsvc with the right call; the
    module's own functions are replaced so no SCM is touched."""

    def setUp(self):
        from ds5app import winsvc as W

        self.W = W
        self.calls = []
        self.saved = {n: getattr(W, n) for n in
                      ("install", "uninstall", "start", "stop", "query",
                       "migrate_user_config", "run_service", "bin_path")}
        W.install = lambda exe=None, start_at_boot=True: self.calls.append(
            ("install", exe, start_at_boot))
        W.uninstall = lambda: self.calls.append(("uninstall",))
        W.start = lambda wait_s=30.0: self.calls.append(("start",))
        W.stop = lambda wait_s=75.0: self.calls.append(("stop",))
        W.query = lambda name="ds5bridge": {"state": "RUNNING", "start": "AUTO_START",
                                            "binpath": '"x" service'}
        W.migrate_user_config = lambda src=None, dst=None: self.calls.append(
            ("migrate", src)) or []
        W.run_service = lambda: self.calls.append(("run",)) or 0
        W.bin_path = lambda exe=None: '"x" service'
        # A failing verb pauses for Enter when the process owns its console
        # (a double-clicked exe); a test runner must never wait on stdin.
        self._owns = C._owns_console
        C._owns_console = lambda: False

    def tearDown(self):
        for n, f in self.saved.items():
            setattr(self.W, n, f)
        C._owns_console = self._owns

    def run_cli(self, argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = C.main(argv)
        return code, out.getvalue()

    def test_bare_service_is_the_scm_entry(self):
        code, _ = self.run_cli(["service"])
        self.assertEqual(code, 0)
        self.assertEqual(self.calls, [("run",)])

    def test_install_registers_and_migrates(self):
        code, out = self.run_cli(["service", "install"])
        self.assertEqual(code, 0)
        self.assertEqual(self.calls, [("install", None, True), ("migrate", None)])
        self.assertIn("registered", out)

    def test_install_options(self):
        self.run_cli(["service", "install", "--manual", "--no-migrate",
                      "--exe", r"C:\x\ds5bridge.exe", "--start"])
        self.assertEqual(self.calls, [("install", r"C:\x\ds5bridge.exe", False),
                                      ("start",)])

    def test_the_other_verbs(self):
        for verb in ("uninstall", "start", "stop"):
            self.calls.clear()
            self.assertEqual(self.run_cli(["service", verb])[0], 0)
            self.assertEqual(self.calls, [(verb,)])

    def test_status_prints_the_state(self):
        code, out = self.run_cli(["service", "status"])
        self.assertEqual(code, 0)
        self.assertIn("running", out)

    def test_a_service_error_is_a_fail_line_not_a_traceback(self):
        def refuse(exe=None, start_at_boot=True):
            raise self.W.ServiceError("needs an administrator")
        self.W.install = refuse
        code, out = self.run_cli(["service", "install"])
        self.assertEqual(code, 1)
        self.assertIn("[FAIL] needs an administrator", out)

    def test_the_tray_accepts_the_service_child_flag(self):
        args = C.build_parser().parse_args(["tray", "--service-child"])
        self.assertTrue(args.service_child)
        self.assertFalse(C.build_parser().parse_args(["tray"]).service_child)


if __name__ == "__main__":
    unittest.main()
