"""`cli.harden_streams` -- the guard that lets the tray run under `pythonw.exe`.

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

import io
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ds5app import cli as C    # noqa: E402


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
