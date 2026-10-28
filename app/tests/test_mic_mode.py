"""The `mic_mode` setting, from config.json to a running bridge.

Three modes: "voice" (the pad's own canceller, audioControl bit 2, plus the
bridge's for the PC's other speakers), "pad" (the pad's only) and "studio"
(nothing). The bridge side is tested in emulator/tests/test_bridge.py. What is
tested here is the app's half: the per-controller setting, its migration from
the old bool `echo_cancel` and its never-raise coercion, how `ds5bridge run`
resolves flag vs. setting, and the watcher that applies a tray or dashboard
change to a bridge that is already running. No hardware, no audio.
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


OTHER = "001122334455"


@unittest.skipIf(K is None, "the app package is not importable here")
class SettingTests(_Temp):
    def test_it_ships_voice_for_a_new_controller(self):
        self.assertEqual(K.load().get(SERIAL).mic_mode, "voice")
        self.assertEqual(K.load().mic_mode_for(SERIAL), "voice")
        self.assertEqual(K.MIC_MODES, ("voice", "pad", "studio"))

    def test_every_mode_round_trips(self):
        for mode in K.MIC_MODES:
            cfg = K.load()
            cfg.set_mic_mode(SERIAL, mode)
            K.save(cfg)
            self.assertEqual(K.load().controllers[SERIAL].mic_mode, mode)
            with open(self.path, encoding="utf-8") as f:
                entry = json.load(f)["controllers"][SERIAL]
            self.assertEqual(entry["mic_mode"], mode)
            self.assertNotIn("echo_cancel", entry)

    def test_an_older_file_without_either_key_gets_the_default(self):
        self.write({"controllers": {SERIAL: {"enabled": True}}})
        self.assertEqual(K.load().controllers[SERIAL].mic_mode, "voice")

    def test_the_old_bool_migrates(self):
        self.write({"controllers": {SERIAL: {"echo_cancel": False},
                                    OTHER: {"echo_cancel": True}}})
        cfg = K.load()
        # false switched both cancellers off: what "studio" is now
        self.assertEqual(cfg.controllers[SERIAL].mic_mode, "studio")
        self.assertEqual(cfg.controllers[OTHER].mic_mode, "voice")

    def test_save_drops_the_old_key(self):
        self.write({"controllers": {SERIAL: {"echo_cancel": False, "label": "x"}}})
        K.save(K.load())
        with open(self.path, encoding="utf-8") as f:
            entry = json.load(f)["controllers"][SERIAL]
        self.assertEqual(entry["mic_mode"], "studio")
        self.assertNotIn("echo_cancel", entry)
        self.assertEqual(entry["label"], "x")

    def test_the_new_key_wins_over_the_old_one(self):
        # A dashboard save merged over an old file can carry both.
        self.write({"controllers": {SERIAL: {"echo_cancel": False,
                                             "mic_mode": "pad"}}})
        self.assertEqual(K.load().controllers[SERIAL].mic_mode, "pad")

    def test_a_hand_edited_value_is_coerced_not_fatal(self):
        self.write({"controllers": {SERIAL: {"echo_cancel": "off"}}})
        self.assertEqual(K.load().controllers[SERIAL].mic_mode, "studio")
        self.write({"controllers": {SERIAL: {"echo_cancel": [1, 2]}}})
        self.assertEqual(K.load().controllers[SERIAL].mic_mode, "voice")
        self.write({"controllers": {SERIAL: {"mic_mode": " Studio "}}})
        self.assertEqual(K.load().controllers[SERIAL].mic_mode, "studio")
        self.write({"controllers": {SERIAL: {"mic_mode": "loud"}}})
        self.assertEqual(K.load().controllers[SERIAL].mic_mode, "voice")
        # an unknown new value falls back to the old key, if there is one
        self.write({"controllers": {SERIAL: {"mic_mode": 3, "echo_cancel": False}}})
        self.assertEqual(K.load().controllers[SERIAL].mic_mode, "studio")

    def test_both_keys_are_known_not_extras(self):
        self.write({"controllers": {SERIAL: {"echo_cancel": False, "mic_mode": "pad"}}})
        extra = K.load().controllers[SERIAL].extra
        self.assertNotIn("echo_cancel", extra)
        self.assertNotIn("mic_mode", extra)

    def test_an_unknown_mode_is_refused_by_the_setter(self):
        with self.assertRaises(ValueError):
            K.load().set_mic_mode(SERIAL, "loud")

    def test_the_shared_mode(self):
        cfg = K.load()
        self.assertEqual(cfg.shared_mic_mode(), "voice")         # nothing yet
        cfg.set_mic_mode(SERIAL, "studio")
        cfg.set_mic_mode(OTHER, "studio")
        self.assertEqual(cfg.shared_mic_mode(), "studio")
        cfg.set_mic_mode(OTHER, "pad")
        self.assertIsNone(cfg.shared_mic_mode())
        self.assertEqual(cfg.shared_mic_mode([SERIAL]), "studio")


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
        self.write({"controllers": {SERIAL: {"mic_mode": "studio"}}})
        kw = self._run(["run", "--serial", SERIAL])
        self.assertEqual(kw["mic_mode"], "studio")
        self.assertTrue(kw["follow_mic_config"])

    def test_an_old_setting_is_read_through_the_migration(self):
        self.write({"controllers": {SERIAL: {"echo_cancel": False}}})
        self.assertEqual(self._run(["run", "--serial", SERIAL])["mic_mode"], "studio")

    def test_a_new_controller_defaults_to_voice(self):
        kw = self._run(["run", "--serial", SERIAL])
        self.assertEqual(kw["mic_mode"], "voice")

    def test_the_flag_wins_and_pins_it(self):
        self.write({"controllers": {SERIAL: {"mic_mode": "voice"}}})
        for mode in ("voice", "pad", "studio"):
            kw = self._run(["run", "--serial", SERIAL, "--mic-mode", mode])
            self.assertEqual(kw["mic_mode"], mode)
            self.assertFalse(kw["follow_mic_config"])

    def test_the_old_flag_is_an_alias(self):
        self.write({"controllers": {SERIAL: {"mic_mode": "pad"}}})
        kw = self._run(["run", "--serial", SERIAL, "--echo-cancel", "off"])
        self.assertEqual(kw["mic_mode"], "studio")
        self.assertFalse(kw["follow_mic_config"])
        kw = self._run(["run", "--serial", SERIAL, "--echo-cancel", "on"])
        self.assertEqual(kw["mic_mode"], "voice")
        # both given: the new flag wins
        kw = self._run(["run", "--serial", SERIAL, "--echo-cancel", "on",
                        "--mic-mode", "studio"])
        self.assertEqual(kw["mic_mode"], "studio")

    def test_a_bad_mode_is_an_argparse_error(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            C.build_parser().parse_args(["run", "--mic-mode", "loud"])


@unittest.skipIf(K is None, "the app package is not importable here")
class LiveApplyTests(_Temp):
    """The tray's Microphone rows and the dashboard reach a running bridge
    through the same file-watcher the `input` section uses, keyed to this
    controller."""

    def _watcher(self, current):
        applied: list = []

        def apply(setting):
            applied.append(setting.mode)
            current[0] = setting.mode

        w = SVC.InputConfigWatcher(
            apply, lambda: SVC.MicSetting(current[0]), path=self.path,
            interval=0.01, load_fn=lambda p: SVC._read_mic_setting(p, SERIAL),
            label="microphone mode")
        return w, applied

    def test_a_change_in_the_file_reaches_the_bridge(self):
        current = ["voice"]
        w, applied = self._watcher(current)
        self.write({"controllers": {SERIAL: {"mic_mode": "studio"}}}, 1)
        self.assertTrue(w.poll_once())
        self.write({"controllers": {SERIAL: {"mic_mode": "pad"}}}, 2)
        self.assertTrue(w.poll_once())
        self.write({"controllers": {SERIAL: {"mic_mode": "voice"}}}, 3)
        self.assertTrue(w.poll_once())
        self.assertEqual(applied, ["studio", "pad", "voice"])

    def test_an_old_file_is_read_through_the_migration(self):
        current = ["voice"]
        w, applied = self._watcher(current)
        self.write({"controllers": {SERIAL: {"echo_cancel": False}}}, 1)
        self.assertTrue(w.poll_once())
        self.assertEqual(applied, ["studio"])

    def test_an_unrelated_save_does_not_touch_it(self):
        current = ["voice"]
        w, applied = self._watcher(current)
        self.write({"enabled": False,
                    "controllers": {SERIAL: {"mic_mode": "voice"},
                                    OTHER: {"mic_mode": "studio"}}}, 1)
        self.assertFalse(w.poll_once())
        self.assertEqual(applied, [])

    def test_a_controller_with_no_entry_is_voice(self):
        self.write({"controllers": {}}, 1)
        self.assertEqual(SVC._read_mic_setting(self.path, SERIAL).mode, "voice")

    def test_an_unreadable_file_is_skipped_not_applied(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{ half a file")
        self.assertIsNone(SVC._read_mic_setting(self.path, SERIAL))

    def test_the_applied_line_names_the_setting(self):
        current = ["voice"]
        lines: list = []
        w = SVC.InputConfigWatcher(
            lambda s: None, lambda: SVC.MicSetting(current[0]), path=self.path,
            load_fn=lambda p: SVC._read_mic_setting(p, SERIAL),
            label="microphone mode", on_applied=lines.append)
        self.write({"controllers": {SERIAL: {"mic_mode": "studio"}}}, 1)
        w.poll_once()
        self.assertEqual(lines, ["microphone mode applied live: mic_mode changed"])

    def test_the_service_hands_it_to_the_backend(self):
        svc = SVC.BridgeService(serial=SERIAL, mic_mode="voice",
                                follow_mic_config=True)
        svc._backend = be = mock.Mock()
        svc._apply_mic_setting(SVC.MicSetting("studio"))
        be.set_mic_mode.assert_called_once_with("studio")
        self.assertEqual(svc.mic_mode, "studio")

    def test_studio_reaches_the_pad_without_its_canceller(self):
        # End to end inside one process: the file says studio, the watcher
        # applies it, the real backend sends an audioControl byte without
        # AC_ECHO_CANCEL -- live, with no re-bridge.
        try:
            from ds5emu.bridge import BridgeBackend
            from ds5bridge import protocol as P
        except ImportError:  # pragma: no cover
            self.skipTest("the bridge stack is not importable here")
        svc = SVC.BridgeService(serial=SERIAL, mic_mode="voice",
                                follow_mic_config=True)
        svc._backend = be = BridgeBackend(serial=SERIAL)
        sent: list = []
        be._write_setstate_body = sent.append
        w = SVC.InputConfigWatcher(
            svc._apply_mic_setting, lambda: SVC.MicSetting(svc.mic_mode),
            path=self.path, load_fn=lambda p: SVC._read_mic_setting(p, SERIAL))
        self.write({"controllers": {SERIAL: {"mic_mode": "studio"}}}, 1)
        self.assertTrue(w.poll_once())
        self.assertEqual(be.mic_mode, "studio")
        self.assertTrue(sent[-1][P.VALID_FLAG0] & P.F0_AUDIO_CONTROL)
        self.assertFalse(sent[-1][P.AUDIO_CONTROL] & P.AC_ECHO_CANCEL)
        self.write({"controllers": {SERIAL: {"mic_mode": "pad"}}}, 2)
        self.assertTrue(w.poll_once())
        self.assertTrue(sent[-1][P.AUDIO_CONTROL] & P.AC_ECHO_CANCEL)



@unittest.skipIf(K is None, "the app package is not importable here")
class StatusTests(unittest.TestCase):
    """The canceller's health reaches the parent (manager) and the page."""

    def test_the_backend_status_is_summarised(self):
        self.assertEqual(SVC.echo_summary(None), ("idle", ""))
        self.assertEqual(SVC.echo_summary({"enabled": False, "mode": "off"}), ("off", ""))
        # the modes: studio removes nothing, pad only the pad's own speaker
        self.assertEqual(SVC.echo_summary({"enabled": False, "pad": False,
                                           "mic_mode": "studio", "mode": "off"}), ("off", ""))
        self.assertEqual(SVC.echo_summary({"enabled": False, "pad": True,
                                           "mic_mode": "pad", "mode": "aec",
                                           "health": "degraded"}), ("pad", ""))
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
        child._note_mode(SVC.echo_line("pad"))
        self.assertEqual(child._snap["echo"], "pad")
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
