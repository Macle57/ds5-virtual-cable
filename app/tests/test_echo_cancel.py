"""The `echo_cancel` setting, from config.json to a running bridge.

The canceller is the pad's own (audioControl bit 2; its bridge side is tested in
emulator/tests/test_bridge.py). What is tested here is the app's half: the
per-controller setting and its never-raise coercion, how `ds5bridge run`
resolves flag vs. setting, and the watcher that applies a dashboard change to
a bridge that is already running. No hardware, no audio.
"""

from __future__ import annotations

import contextlib
import io
import json
import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    from ds5app import cli as C
    from ds5app import config as K
    from ds5app import service as SVC
except ImportError:  # pragma: no cover
    K = None

for _name in ("ds5app.config", "ds5app.service", "ds5app.cli"):
    logging.getLogger(_name).addHandler(logging.NullHandler())

SERIAL = "d42f4ba1485d"


class _Temp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "config.json")
        patcher = mock.patch.dict(os.environ, {"DS5_CONFIG": self.tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, data: dict, mtime_s: int | None = None) -> None:
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        if mtime_s is not None:
            os.utime(self.path, ns=(mtime_s * 10**9, mtime_s * 10**9))


@unittest.skipIf(K is None, "the app package is not importable here")
class SettingTests(_Temp):
    def test_it_ships_on_for_a_new_controller(self):
        self.assertTrue(K.load().get(SERIAL).echo_cancel)
        self.assertTrue(K.load().should_echo_cancel(SERIAL))

    def test_it_round_trips(self):
        cfg = K.load()
        cfg.get(SERIAL).echo_cancel = False
        K.save(cfg)
        self.assertFalse(K.load().controllers[SERIAL].echo_cancel)
        with open(self.path, encoding="utf-8") as f:
            self.assertIs(json.load(f)["controllers"][SERIAL]["echo_cancel"], False)

    def test_an_older_file_without_the_key_gets_the_default(self):
        self.write({"controllers": {SERIAL: {"enabled": True}}})
        self.assertTrue(K.load().controllers[SERIAL].echo_cancel)

    def test_a_hand_edited_value_is_coerced_not_fatal(self):
        self.write({"controllers": {SERIAL: {"echo_cancel": "off"}}})
        self.assertFalse(K.load().controllers[SERIAL].echo_cancel)
        self.write({"controllers": {SERIAL: {"echo_cancel": [1, 2]}}})
        self.assertTrue(K.load().controllers[SERIAL].echo_cancel)

    def test_it_is_a_known_key_not_an_extra(self):
        self.write({"controllers": {SERIAL: {"echo_cancel": False}}})
        self.assertNotIn("echo_cancel", K.load().controllers[SERIAL].extra)


@unittest.skipIf(K is None, "the app package is not importable here")
class RunResolutionTests(_Temp):
    """`ds5bridge run`: the flag pins it; otherwise the controller's setting,
    followed live."""

    def _run(self, argv) -> dict:
        seen: dict = {}

        def fake_service(**kw):
            seen.update(kw)
            svc = mock.Mock()
            svc.start.side_effect = RuntimeError("stop here")
            return svc

        with mock.patch.object(C.S, "BridgeService", side_effect=fake_service), \
                contextlib.redirect_stdout(io.StringIO()), \
                mock.patch.object(C.logging, "basicConfig"):
            C.cmd_run(C.build_parser().parse_args(argv))
        return seen

    def test_the_setting_is_used_and_followed_when_there_is_no_flag(self):
        self.write({"controllers": {SERIAL: {"echo_cancel": False}}})
        kw = self._run(["run", "--serial", SERIAL])
        self.assertFalse(kw["echo_cancel"])
        self.assertTrue(kw["follow_echo_config"])

    def test_a_new_controller_defaults_on(self):
        kw = self._run(["run", "--serial", SERIAL])
        self.assertTrue(kw["echo_cancel"])

    def test_the_flag_wins_and_pins_it(self):
        self.write({"controllers": {SERIAL: {"echo_cancel": True}}})
        kw = self._run(["run", "--serial", SERIAL, "--echo-cancel", "off"])
        self.assertFalse(kw["echo_cancel"])
        self.assertFalse(kw["follow_echo_config"])
        kw = self._run(["run", "--serial", SERIAL, "--echo-cancel", "on"])
        self.assertTrue(kw["echo_cancel"])


@unittest.skipIf(K is None, "the app package is not importable here")
class LiveApplyTests(_Temp):
    """The dashboard switch reaches a running bridge through the same
    file-watcher the `input` section uses, keyed to this controller."""

    def _watcher(self, current):
        applied: list = []

        def apply(setting):
            applied.append(setting.on)
            current[0] = setting.on

        w = SVC.InputConfigWatcher(
            apply, lambda: SVC.EchoSetting(current[0]), path=self.path,
            interval=0.01, load_fn=lambda p: SVC._read_echo_setting(p, SERIAL),
            label="pad echo cancellation")
        return w, applied

    def test_turning_it_off_in_the_file_reaches_the_bridge(self):
        current = [True]
        w, applied = self._watcher(current)
        self.write({"controllers": {SERIAL: {"echo_cancel": False}}}, 1)
        self.assertTrue(w.poll_once())
        self.assertEqual(applied, [False])

    def test_an_unrelated_save_does_not_touch_it(self):
        current = [True]
        w, applied = self._watcher(current)
        self.write({"enabled": False,
                    "controllers": {SERIAL: {"echo_cancel": True},
                                    "001122334455": {"echo_cancel": False}}}, 1)
        self.assertFalse(w.poll_once())
        self.assertEqual(applied, [])

    def test_a_controller_with_no_entry_is_on(self):
        self.write({"controllers": {}}, 1)
        self.assertTrue(SVC._read_echo_setting(self.path, SERIAL).on)

    def test_an_unreadable_file_is_skipped_not_applied(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{ half a file")
        self.assertIsNone(SVC._read_echo_setting(self.path, SERIAL))

    def test_the_applied_line_names_the_setting(self):
        current = [True]
        lines: list = []
        w = SVC.InputConfigWatcher(
            lambda s: None, lambda: SVC.EchoSetting(current[0]), path=self.path,
            load_fn=lambda p: SVC._read_echo_setting(p, SERIAL),
            label="pad echo cancellation", on_applied=lines.append)
        self.write({"controllers": {SERIAL: {"echo_cancel": False}}}, 1)
        w.poll_once()
        self.assertEqual(lines, ["pad echo cancellation applied live: "
                                 "echo_cancel changed"])

    def test_the_service_hands_it_to_the_backend(self):
        svc = SVC.BridgeService(serial=SERIAL, echo_cancel=True,
                                follow_echo_config=True)
        svc._backend = be = mock.Mock()
        svc._apply_echo_setting(SVC.EchoSetting(False))
        be.set_echo_cancel.assert_called_once_with(False)
        self.assertFalse(svc.echo_cancel)



@unittest.skipIf(K is None, "the app package is not importable here")
class StatusTests(unittest.TestCase):
    """The canceller's health reaches the parent (manager) and the page."""

    def test_the_backend_status_is_summarised(self):
        self.assertEqual(SVC.echo_summary(None), ("idle", ""))
        self.assertEqual(SVC.echo_summary({"enabled": False, "mode": "off"}), ("off", ""))
        self.assertEqual(SVC.echo_summary({"enabled": True, "mode": "idle"}), ("idle", ""))
        self.assertEqual(SVC.echo_summary({"enabled": True, "mode": "aec", "health": "ok",
                                           "worker_alive": True}), ("on", ""))
        self.assertEqual(
            SVC.echo_summary({"enabled": True, "mode": "passthrough", "health": "degraded",
                              "reason": "no native canceller: ds5aec.dll\n not found",
                              "worker_alive": True}),
            ("degraded", "no native canceller: ds5aec.dll not found"))
        self.assertEqual(
            SVC.echo_summary({"enabled": True, "mode": "aec", "health": "degraded",
                              "reason": "", "worker_alive": False}),
            ("degraded", "the canceller stopped"))

    def test_a_change_is_announced_once_on_the_status_channel(self):
        events = []
        svc = SVC.BridgeService(serial=SERIAL, on_event=lambda k, t: events.append((k, t)))
        svc._backend = be = mock.Mock()
        be.device_status.return_value = {"battery_percent": 50, "battery_state": "",
                                          "connected": True}
        be.stats = {"input_delivered": 0, "disconnects": 0, "reconnects": 0,
                    "input_neutral": 0}
        be.echo_status.return_value = {"enabled": True, "mode": "aec", "health": "ok",
                                       "worker_alive": True}
        snap = svc.snapshot()
        svc.snapshot()
        self.assertEqual((snap["echo"], snap["echo_note"]), ("on", ""))
        be.echo_status.return_value = {"enabled": True, "mode": "passthrough",
                                       "health": "degraded", "reason": "canceller thread died: X",
                                       "worker_alive": False}
        svc.snapshot()
        modes = [t for k, t in events if k == "mode"]
        self.assertEqual(modes, ["echo on", "echo degraded  canceller thread died: X"])

    def test_the_manager_reads_it_back(self):
        from ds5app import manager as M
        child = M.ChildBridge.__new__(M.ChildBridge)
        child._lock = __import__("threading").Lock()
        child._snap = {}
        child._note_mode(SVC.echo_line("degraded", "no native canceller: gone"))
        self.assertEqual((child._snap["echo"], child._snap["echo_note"]),
                         ("degraded", "no native canceller: gone"))
        child._note_mode(SVC.echo_line("on"))
        self.assertEqual((child._snap["echo"], child._snap["echo_note"]), ("on", ""))
        # the remote/keyboard line still parses
        child._note_mode(SVC.mode_line(True, False))
        self.assertTrue(child._snap["remote_mode"])

@unittest.skipIf(K is None, "the app package is not importable here")
class DoctorTests(unittest.TestCase):
    def test_doctor_says_whether_the_native_canceller_loads(self):
        from ds5emu import aec as AEC
        out = io.StringIO()
        with mock.patch.object(AEC, "NativeCanceller", side_effect=OSError("ds5aec.dll not found")),                 contextlib.redirect_stdout(out):
            self.assertEqual(C._doctor_echo(), 1)
        self.assertIn("echo canceller unavailable (ds5aec.dll not found)", out.getvalue())
        if os.path.isfile(AEC._dll_path()):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(C._doctor_echo(), 0)
            self.assertIn("echo cancel", out.getvalue())

if __name__ == "__main__":
    unittest.main()
