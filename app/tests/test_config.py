"""The config loader, hammered with the files a real machine produces.

`load()` is a startup dependency: it runs before the tray icon exists, in a
windowed process with no console to print a traceback to. If it can raise, the
failure mode is "the program does not start and says nothing", which is the
worst one this project has. So the interesting tests here are not the happy
round-trip -- they are the empty file, the half-written file, the JSON array
somebody's editor left behind, and the config that is a directory.

No hardware, no registry writes, no third-party packages: DS5_CONFIG points at
a fresh temporary directory for every single test, and the autostart tests only
exercise the pure command-string construction.

    prototype\\.venv\\Scripts\\python.exe -m unittest discover -s app\\tests -t app
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
    """Import ds5app.<name> without importing the ds5app PACKAGE.

    `ds5app/__init__.py` imports the service, which imports the controller,
    which imports hidapi. These two modules are deliberately stdlib-only and
    have to keep working on an interpreter that has none of that installed --
    that is the entire point of a settings loader that cannot fail -- so the
    package import is tried first (it is the real path in the venv) and the file
    is loaded directly when the hardware stack is not there.
    """
    try:
        module = __import__(f"ds5app.{name}", fromlist=[name])
        return module
    except ImportError:
        path = Path(__file__).resolve().parents[1] / "ds5app" / f"{name}.py"
        spec = importlib.util.spec_from_file_location(f"_ds5app_{name}", path)
        module = importlib.util.module_from_spec(spec)
        # In sys.modules BEFORE it is executed: @dataclass resolves a field's
        # type by looking its defining module up there, and a module that is
        # not registered yet makes that lookup return None and explode.
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module


CFG = _load("config")
AUTO = _load("autostart")

# Both modules log a warning for every bad file they survive, which is the
# point of them -- but with no handler configured, logging falls back to
# printing at stderr, and a green test run that is full of "config.json is
# unusable" reads exactly like a failing one. A NullHandler on each logger is
# enough to stop that, and does not touch anybody else's logging.
for _name in ("ds5app.config", "ds5app.autostart"):
    logging.getLogger(_name).addHandler(logging.NullHandler())

SERIAL = "d42f4ba1485d"


class _Temp(unittest.TestCase):
    """Every test gets its own directory and its own DS5_CONFIG."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        self._saved = os.environ.get("DS5_CONFIG")
        os.environ["DS5_CONFIG"] = self.dir
        self.addCleanup(self._restore)

    def _restore(self):
        if self._saved is None:
            os.environ.pop("DS5_CONFIG", None)
        else:
            os.environ["DS5_CONFIG"] = self._saved
        self._tmp.cleanup()

    # -- helpers -----------------------------------------------------------

    @property
    def path(self) -> str:
        return os.path.join(self.dir, "config.json")

    def write(self, text: str) -> None:
        with open(self.path, "w", encoding="utf-8", newline="") as f:
            f.write(text)

    def listing(self) -> list[str]:
        return sorted(os.listdir(self.dir))


class PathTests(_Temp):
    def test_ds5_config_pointing_at_a_directory(self):
        self.assertEqual(CFG.config_path(), self.path)

    def test_ds5_config_pointing_at_a_file(self):
        target = os.path.join(self.dir, "elsewhere.json")
        os.environ["DS5_CONFIG"] = target
        self.assertEqual(CFG.config_path(), target)

    def test_appdata_is_used_when_there_is_no_override(self):
        os.environ.pop("DS5_CONFIG", None)
        os.environ["APPDATA"] = self.dir
        self.assertEqual(CFG.config_path(),
                         os.path.join(self.dir, "ds5bridge", "config.json"))

    def test_no_appdata_falls_back_to_a_home_directory(self):
        # A service account, a stripped environment, or anything not Windows.
        os.environ.pop("DS5_CONFIG", None)
        saved = os.environ.pop("APPDATA", None)
        try:
            p = CFG.config_path()
        finally:
            if saved is not None:
                os.environ["APPDATA"] = saved
        self.assertTrue(p.endswith(os.path.join(".ds5bridge", "config.json")), p)


class DefaultsTests(_Temp):
    def test_no_file_at_all_is_the_first_run_not_an_error(self):
        cfg = CFG.load()
        self.assertTrue(cfg.enabled)
        self.assertFalse(cfg.autostart_on_login)
        self.assertTrue(cfg.auto_bridge_new)
        self.assertEqual(cfg.controllers, {})
        # Reading must not create anything. A config file that appears just
        # because something asked a question is a file nobody chose to have.
        self.assertEqual(self.listing(), [])

    def test_port_base_default_is_3241_not_3240(self):
        # 3240 is usbipd-win's. Defaulting to it would break an unrelated
        # product that is very likely installed on the same machine.
        self.assertEqual(CFG.load().port_base, 3241)
        self.assertEqual(CFG.DEFAULT_PORT_BASE, 3241)

    def test_a_config_asking_for_3240_does_not_get_it(self):
        self.write(json.dumps({"port_base": 3240}))
        self.assertEqual(CFG.load().port_base, 3241)

    def test_a_junk_port_base_falls_back(self):
        for junk in ("banana", None, -1, 999999, [3241]):
            with self.subTest(junk=junk):
                self.write(json.dumps({"port_base": junk}))
                self.assertEqual(CFG.load().port_base, 3241)


class RoundTripTests(_Temp):
    def test_save_then_load_returns_the_same_settings(self):
        cfg = CFG.load()
        cfg.enabled = False
        cfg.autostart_on_login = True
        cfg.auto_bridge_new = False
        cfg.port_base = 3250
        cc = cfg.get(SERIAL)
        cc.enabled = False
        cc.audio_target = "headphone"
        cc.port = 3251
        cc.label = "living room"
        CFG.save(cfg)

        back = CFG.load()
        self.assertFalse(back.enabled)
        self.assertTrue(back.autostart_on_login)
        self.assertFalse(back.auto_bridge_new)
        self.assertEqual(back.port_base, 3250)
        got = back.controllers[SERIAL]
        self.assertFalse(got.enabled)
        self.assertEqual(got.audio_target, "headphone")
        self.assertEqual(got.port, 3251)
        self.assertEqual(got.label, "living room")

    def test_the_file_is_json_a_person_can_edit(self):
        CFG.save(CFG.load())
        with open(self.path, encoding="utf-8") as f:
            data = json.load(f)
        self.assertIn("controllers", data)

    def test_save_leaves_no_stray_tmp_behind(self):
        # The atomic write is a .tmp plus os.replace; a directory that slowly
        # fills with config.json.tmp is its own bug report.
        cfg = CFG.load()
        for _ in range(3):
            CFG.save(cfg)
        self.assertEqual(self.listing(), ["config.json"])

    def test_module_level_setters_persist(self):
        CFG.set_enabled(SERIAL, False)
        CFG.set_master_enabled(False)
        back = CFG.load()
        self.assertFalse(back.enabled)
        self.assertFalse(back.controllers[SERIAL].enabled)

    def test_save_to_a_directory_that_does_not_exist_yet(self):
        nested = os.path.join(self.dir, "a", "b", "config.json")
        CFG.save(CFG.load(), nested)
        self.assertTrue(os.path.isfile(nested))


class BadFileTests(_Temp):
    """The whole reason this module exists."""

    def assert_defaults_and_quarantined(self):
        cfg = CFG.load()
        self.assertTrue(cfg.enabled)
        self.assertEqual(cfg.port_base, 3241)
        self.assertEqual(cfg.controllers, {})
        self.assertIn("config.json.bad", self.listing())
        # And the config itself is gone, so the next save starts clean rather
        # than trying to merge into rubble.
        self.assertNotIn("config.json", self.listing())
        return cfg

    def test_truncated_json_falls_back_and_keeps_the_evidence(self):
        self.write('{"enabled": false, "controllers": {"d42f4ba148')
        self.assert_defaults_and_quarantined()

    def test_not_json_at_all(self):
        self.write("this is not a config, it is a note to self\n")
        self.assert_defaults_and_quarantined()

    def test_an_empty_file(self):
        # What a power cut during a write actually leaves: zero bytes.
        self.write("")
        self.assert_defaults_and_quarantined()

    def test_whitespace_only(self):
        self.write("   \n\t\n")
        self.assert_defaults_and_quarantined()

    def test_a_list_where_an_object_was_expected(self):
        self.write(json.dumps([{"enabled": False}]))
        self.assert_defaults_and_quarantined()

    def test_a_bare_scalar(self):
        self.write("42")
        self.assert_defaults_and_quarantined()

    def test_the_bad_copy_holds_what_the_user_typed(self):
        broken = '{"controllers": {"d42f4ba1485d": {"label": "living room"'
        self.write(broken)
        CFG.load()
        with open(os.path.join(self.dir, "config.json.bad"), encoding="utf-8") as f:
            self.assertEqual(f.read(), broken)

    def test_a_second_bad_file_does_not_raise(self):
        self.write("nonsense")
        CFG.load()
        self.write("more nonsense")
        CFG.load()
        self.assertIn("config.json.bad", self.listing())

    def test_saving_after_a_quarantine_works(self):
        self.write("nonsense")
        cfg = CFG.load()
        cfg.set_master_enabled(False)
        CFG.save(cfg)
        self.assertFalse(CFG.load().enabled)

    def test_a_config_that_is_a_directory_is_survivable(self):
        # An unreadable path is not quarantined: if it cannot be read it very
        # likely cannot be renamed either, and a failed rescue must not become
        # a second failure.
        os.mkdir(self.path)
        cfg = CFG.load()
        self.assertTrue(cfg.enabled)
        self.assertEqual(cfg.controllers, {})

    def test_wrong_types_everywhere_still_load(self):
        self.write(json.dumps({
            "enabled": "yes",              # a hand-edited string, honoured
            "autostart_on_login": 1,
            "auto_bridge_new": "no",
            "controllers": {
                SERIAL: {"enabled": "false", "audio_target": "SPEAKERS",
                         "port": "not a port", "label": 7},
            },
        }))
        cfg = CFG.load()
        self.assertTrue(cfg.enabled)
        self.assertTrue(cfg.autostart_on_login)
        self.assertFalse(cfg.auto_bridge_new)
        cc = cfg.controllers[SERIAL]
        self.assertFalse(cc.enabled)
        self.assertEqual(cc.audio_target, "speaker")   # unknown choice -> default
        self.assertIsNone(cc.port)
        self.assertEqual(cc.label, "")                 # not a string -> default
        # Nothing above is corruption, so nothing is quarantined.
        self.assertNotIn("config.json.bad", self.listing())

    def test_controllers_of_the_wrong_shape_are_ignored_not_fatal(self):
        self.write(json.dumps({"controllers": ["d42f4ba1485d"]}))
        cfg = CFG.load()
        self.assertEqual(cfg.controllers, {})
        self.assertTrue(cfg.enabled)

    def test_one_junk_entry_does_not_lose_the_others(self):
        self.write(json.dumps({"controllers": {SERIAL: "junk",
                                               "aabbccddeeff": {"label": "spare"}}}))
        cfg = CFG.load()
        self.assertEqual(cfg.controllers[SERIAL].enabled, True)   # defaulted
        self.assertEqual(cfg.controllers["aabbccddeeff"].label, "spare")


class UnknownKeyTests(_Temp):
    def test_unknown_keys_survive_a_round_trip(self):
        # A config written by a newer build must not lose the newer build's
        # settings just because an older one opened it.
        self.write(json.dumps({
            "enabled": True,
            "future_setting": {"deep": [1, 2, 3]},
            "controllers": {SERIAL: {"enabled": True, "future_per_pad": "x"}},
        }))
        cfg = CFG.load()
        CFG.save(cfg)
        with open(self.path, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["future_setting"], {"deep": [1, 2, 3]})
        self.assertEqual(data["controllers"][SERIAL]["future_per_pad"], "x")

    def test_unknown_keys_never_shadow_a_known_one(self):
        self.write(json.dumps({"enabled": False, "unknown": "x"}))
        cfg = CFG.load()
        cfg.enabled = True
        CFG.save(cfg)
        with open(self.path, encoding="utf-8") as f:
            data = json.load(f)
        self.assertTrue(data["enabled"])
        self.assertEqual(data["unknown"], "x")


class SerialTests(_Temp):
    def test_serials_are_normalised_to_lowercase_on_the_way_in(self):
        # The rest of the codebase compares `c.serial.lower()`; a config keyed
        # on "D42F..." would match nothing and silently do nothing.
        self.write(json.dumps({"controllers": {"D42F4BA1485D": {"label": "mine"}}}))
        cfg = CFG.load()
        self.assertEqual(list(cfg.controllers), [SERIAL])
        self.assertEqual(cfg.get("D42F4BA1485D").label, "mine")
        self.assertEqual(cfg.get(" d42f4ba1485d ").label, "mine")

    def test_lookup_is_case_insensitive_in_both_directions(self):
        cfg = CFG.load()
        cfg.set_enabled("D42F4BA1485D", False)
        CFG.save(cfg)
        self.assertFalse(CFG.load().get(SERIAL).enabled)
        self.assertEqual(list(CFG.load().controllers), [SERIAL])

    def test_norm_serial_leaves_the_bdaddr_shape_alone(self):
        self.assertEqual(CFG.norm_serial("  D42F4BA1485D "), SERIAL)
        self.assertEqual(CFG.norm_serial(None), "")


class NewControllerTests(_Temp):
    def test_auto_bridge_new_true_means_an_unseen_pad_is_bridged(self):
        cfg = CFG.load()
        self.assertTrue(cfg.get("aabbccddeeff").enabled)
        self.assertTrue(cfg.is_bridged("aabbccddeeff"))

    def test_auto_bridge_new_false_means_an_unseen_pad_is_left_alone(self):
        self.write(json.dumps({"auto_bridge_new": False}))
        cfg = CFG.load()
        self.assertFalse(cfg.get("aabbccddeeff").enabled)
        self.assertFalse(cfg.is_bridged("aabbccddeeff"))

    def test_a_known_pad_keeps_its_own_setting(self):
        self.write(json.dumps({"auto_bridge_new": False,
                               "controllers": {SERIAL: {"enabled": True}}}))
        cfg = CFG.load()
        self.assertTrue(cfg.get(SERIAL).enabled)

    def test_get_returns_the_live_object_so_edits_stick(self):
        cfg = CFG.load()
        cfg.get(SERIAL).label = "mine"
        self.assertEqual(cfg.controllers[SERIAL].label, "mine")

    def test_the_master_switch_beats_every_per_controller_flag(self):
        cfg = CFG.load()
        cfg.set_enabled(SERIAL, True)
        cfg.set_master_enabled(False)
        self.assertFalse(cfg.is_bridged(SERIAL))
        self.assertEqual(cfg.bridged_serials(), [])


class PortTests(_Temp):
    def test_an_unpinned_controller_gets_the_port_base(self):
        self.assertEqual(CFG.load().port_for(SERIAL), 3241)

    def test_a_pinned_port_wins(self):
        self.write(json.dumps({"controllers": {SERIAL: {"port": 3250}}}))
        self.assertEqual(CFG.load().port_for(SERIAL), 3250)

    def test_two_controllers_never_share_a_port(self):
        self.write(json.dumps({"controllers": {SERIAL: {"port": 3241}}}))
        cfg = CFG.load()
        self.assertNotEqual(cfg.port_for("aabbccddeeff"), 3241)

    def test_two_UNPINNED_controllers_never_share_a_port(self):
        # The regression that shipped: with nothing pinned yet, an allocator
        # that only skips already-pinned ports hands out `port_base` to every
        # caller. Measured on the real two-controller setup -- both got 3241,
        # so the second bridge would have raced the first for the socket.
        cfg = CFG.load()
        got = [cfg.port_for(s) for s in (SERIAL, "aabbccddeeff", "112233445566")]
        self.assertEqual(len(set(got)), 3, f"ports collided: {got}")
        self.assertEqual(got, [3241, 3242, 3243])

    def test_an_allocated_port_is_sticky(self):
        # A controller must keep its port for the life of the config, otherwise
        # two controllers swap usbip ports depending on which powered on first.
        cfg = CFG.load()
        first = cfg.port_for(SERIAL)
        cfg.port_for("aabbccddeeff")
        self.assertEqual(cfg.port_for(SERIAL), first)

    def test_an_allocated_port_survives_a_save_and_load(self):
        cfg = CFG.load()
        a, b = cfg.port_for(SERIAL), cfg.port_for("aabbccddeeff")
        CFG.save(cfg)
        again = CFG.load()
        self.assertEqual(again.port_for(SERIAL), a)
        self.assertEqual(again.port_for("aabbccddeeff"), b)

    def test_3240_is_never_allocated(self):
        # usbipd-win owns it.
        self.write(json.dumps({"port_base": 3239,
                               "controllers": {SERIAL: {"port": 3239}}}))
        cfg = CFG.load()
        self.assertEqual(cfg.port_for("aabbccddeeff"), 3241)

    def test_a_pinned_3240_is_refused_and_becomes_allocate(self):
        self.write(json.dumps({"controllers": {SERIAL: {"port": 3240}}}))
        cfg = CFG.load()
        self.assertIsNone(cfg.controllers[SERIAL].port)
        self.assertEqual(cfg.port_for(SERIAL), 3241)


class SaveFailureTests(_Temp):
    def test_a_save_that_cannot_work_raises_something_printable(self):
        # A path whose parent is a FILE cannot be created on any platform.
        blocker = os.path.join(self.dir, "blocker")
        with open(blocker, "w", encoding="utf-8") as f:
            f.write("x")
        target = os.path.join(blocker, "config.json")
        with self.assertRaises(CFG.ConfigWriteError) as cm:
            CFG.save(CFG.load(), target)
        self.assertIn("config.json", str(cm.exception))
        self.assertFalse(os.path.exists(target + ".tmp"))

    def test_try_save_reports_failure_instead_of_raising(self):
        # What a tray menu handler calls: losing a setting is annoying, an
        # exception through pystray's message loop takes the icon with it.
        blocker = os.path.join(self.dir, "blocker")
        with open(blocker, "w", encoding="utf-8") as f:
            f.write("x")
        self.assertFalse(CFG.try_save(CFG.load(),
                                      os.path.join(blocker, "config.json")))
        self.assertTrue(CFG.try_save(CFG.load()))


class AutostartCommandTests(unittest.TestCase):
    """Pure command-string construction only. Nothing here touches HKCU."""

    #: A stand-in repo path. Deliberately free of "\r", "\a" and "\t": a path
    #: written into a NON-raw string is how this very test file got a literal
    #: carriage return baked into it once already.
    APP = r"D:\proj\app"

    def test_a_launcher_exe_is_registered_on_its_own(self):
        cmd = AUTO.build_command(r"C:\Apps\ds5bridge\ds5bridge-tray.exe")
        self.assertEqual(cmd, r'"C:\Apps\ds5bridge\ds5bridge-tray.exe"')

    def test_python_carries_its_own_sys_path_entry(self):
        # The regression this replaces: `python -m ds5app tray` in the Run key.
        # The Run key has NO working directory, so login starts the value in
        # %USERPROFILE% and the import fails. Verified by hand on this machine:
        # from C:\Users\<me>, `python -m ds5app tray` really does die with
        # "No module named ds5app" -- and under pythonw there is no console for
        # that traceback to appear in, so start-at-login would have been a
        # checkbox that turned on and silently did nothing.
        cmd = AUTO.build_command(r"C:\Python312\python.exe", app_dir=self.APP)
        self.assertNotIn("-m ds5app", cmd)
        self.assertIn("sys.path.insert(0,r'" + self.APP + "')", cmd)
        self.assertIn("from ds5app.cli import main", cmd)
        self.assertIn("['tray']", cmd)

    def test_the_python_form_is_a_single_quoted_argument(self):
        # CreateProcess parses the Run value, so the -c program has to sit
        # inside one pair of double quotes and may contain none of its own.
        cmd = AUTO.build_command(r"C:\Python312\python.exe", app_dir=self.APP)
        program = cmd.split(" -c ", 1)[1]
        self.assertTrue(program.startswith('"') and program.endswith('"'))
        self.assertNotIn('"', program[1:-1])

    def test_a_trailing_backslash_cannot_escape_the_closing_quote(self):
        # r'...\' is not a valid Python literal either, so a path with a
        # trailing separator would break the -c program it is embedded in.
        cmd = AUTO.build_command(r"C:\Python312\python.exe", app_dir=self.APP + "\\")
        self.assertIn("r'" + self.APP + "'", cmd)

    def test_pythonw_is_still_python(self):
        self.assertIn("from ds5app.cli import main",
                      AUTO.build_command(r"C:\Python312\pythonw.exe"))

    def test_a_path_with_spaces_is_quoted(self):
        # Unquoted, Windows tries "C:\Program.exe" first. Twenty-year-old bug.
        cmd = AUTO.build_command(r"C:\Program Files\ds5bridge\ds5bridge-tray.exe")
        self.assertTrue(cmd.startswith('"'))
        self.assertTrue(cmd.endswith('"'))
        self.assertIn(r"Program Files", cmd)

    def test_an_already_quoted_path_is_not_quoted_twice(self):
        cmd = AUTO.build_command(r'"C:\Program Files\app\ds5bridge-tray.exe"')
        self.assertEqual(cmd, r'"C:\Program Files\app\ds5bridge-tray.exe"')
        self.assertNotIn('""', cmd)

    def test_an_empty_launcher_is_an_actionable_error_not_an_indexerror(self):
        with self.assertRaises(AUTO.AutostartError):
            AUTO.build_command("")

    def test_is_python_recognises_the_interpreter_names(self):
        self.assertTrue(AUTO.is_python(r"C:\x\PYTHON.EXE"))
        self.assertFalse(AUTO.is_python(r"C:\x\ds5bridge.exe"))


class AutostartReadTests(unittest.TestCase):
    """Read-only. These never write to HKCU -- a test suite must not change
    what the machine does at login."""

    def test_reading_the_state_never_raises(self):
        self.assertIsInstance(AUTO.is_enabled(), bool)
        cmd = AUTO.current_command()
        self.assertTrue(cmd is None or isinstance(cmd, str))

    def test_available_on_windows(self):
        self.assertTrue(AUTO.available())

    @unittest.skipIf(sys.platform == "win32", "the non-Windows behaviour")
    def test_off_windows_it_degrades_instead_of_exploding(self):
        # The read paths answer "no" and the write paths raise something with
        # text in it. A tray checkbox must never become an OSError traceback.
        self.assertFalse(AUTO.available())
        self.assertFalse(AUTO.is_enabled())
        self.assertIsNone(AUTO.current_command())
        with self.assertRaises(AUTO.AutostartError):
            AUTO.enable()


if __name__ == "__main__":
    unittest.main()
