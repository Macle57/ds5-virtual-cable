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


if __name__ == "__main__":
    unittest.main()
