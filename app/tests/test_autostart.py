"""autostart.py -- the scheduled task, with no scheduler.

`schtasks.exe` is replaced by a recorder for every test that would change the
machine: a test suite must not register or delete a logon task on the
developer's account. What is tested is what the previous Run-key version's
tests guarded, moved to the new mechanism -- the task document says what the
module docstring promises (current user only, highest privileges, no time
limit, one instance, battery-proof), the read path is tolerant, enable/disable
call schtasks the way the docs say, and the pre-0.5.0 Run value is migrated
exactly once and only when it is ours.

The pure command-building tests stay in test_config.py, where they always were.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import sys
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


AUTO = _load("autostart")
logging.getLogger("ds5app.autostart").addHandler(logging.NullHandler())
logging.getLogger("ds5app.autostart").propagate = False

TRAY = r"C:\Users\me\AppData\Local\ds5bridge\app\ds5bridge-tray.exe"


class FakeSchtasks:
    """Records every call; answers from a script of (exit, output) per verb.

    `tasks` is the fake library: a dict of task name -> XML text. /Create
    stores the document it was given (read back from the temp file, which the
    module deletes afterwards -- so it is read here, at call time), /Query
    returns it, /Delete removes it. `refuse_create` makes /Create fail the way
    an unelevated schtasks does.
    """

    def __init__(self, tasks=None, refuse_create=False):
        self.tasks = dict(tasks or {})
        self.calls = []
        self.refuse_create = refuse_create

    def __call__(self, args):
        self.calls.append(list(args))
        verb = args[0]
        name = args[args.index("/TN") + 1] if "/TN" in args else ""
        if verb == "/Query":
            if name in self.tasks:
                return 0, self.tasks[name]
            return 1, "ERROR: The system cannot find the file specified."
        if verb == "/Create":
            if self.refuse_create:
                return 1, "ERROR: Access is denied."
            path = args[args.index("/XML") + 1]
            with open(path, encoding="utf-16") as f:
                self.tasks[name] = f.read()
            return 0, f'SUCCESS: The scheduled task "{name}" has successfully been created.'
        if verb == "/Delete":
            if name in self.tasks:
                del self.tasks[name]
                return 0, "SUCCESS"
            return 1, "ERROR: The system cannot find the file specified."
        return 1, "unknown verb"


class _Rig(unittest.TestCase):
    """Fake schtasks, fake Run value, Windows pretended present."""

    def setUp(self):
        self.sch = FakeSchtasks()
        self.legacy = {}
        self._saved = (AUTO._run_schtasks, AUTO.available, AUTO.legacy_run_value,
                       AUTO._delete_legacy_run_value, AUTO.resolve_exe)
        AUTO._run_schtasks = self.sch
        AUTO.available = lambda: True
        AUTO.legacy_run_value = lambda: self.legacy.get(AUTO.VALUE_NAME)

        def delete():
            return self.legacy.pop(AUTO.VALUE_NAME, None) is not None
        AUTO._delete_legacy_run_value = delete
        AUTO.resolve_exe = lambda: TRAY

    def tearDown(self):
        (AUTO._run_schtasks, AUTO.available, AUTO.legacy_run_value,
         AUTO._delete_legacy_run_value, AUTO.resolve_exe) = self._saved


class TaskDocumentTests(unittest.TestCase):
    def test_says_what_the_docstring_promises(self):
        xml = AUTO.task_xml(TRAY, "", os.path.dirname(TRAY), user=r"PC\me")
        # The current user only, on both the trigger and the principal.
        self.assertEqual(xml.count(r"<UserId>PC\me</UserId>"), 2)
        self.assertIn("<LogonTrigger>", xml)
        self.assertIn("<RunLevel>HighestAvailable</RunLevel>", xml)
        self.assertIn("<LogonType>InteractiveToken</LogonType>", xml)
        self.assertIn("<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>", xml)
        self.assertIn("<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>", xml)
        self.assertIn("<DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>", xml)
        self.assertIn("<StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>", xml)
        self.assertIn("<StartWhenAvailable>true</StartWhenAvailable>", xml)
        self.assertIn("<AllowHardTerminate>false</AllowHardTerminate>", xml)
        self.assertIn(f"<Command>{TRAY}</Command>", xml)
        self.assertIn(f"<WorkingDirectory>{os.path.dirname(TRAY)}</WorkingDirectory>", xml)
        self.assertNotIn("<Arguments>", xml)
        self.assertIn(f"<URI>\\{AUTO.TASK_NAME}</URI>", xml)

    def test_is_well_formed_xml_in_the_task_namespace(self):
        import xml.etree.ElementTree as ET
        root = ET.fromstring(AUTO.task_xml(TRAY, user="PC\\me").strip())
        self.assertEqual(root.tag, f"{{{AUTO._TASK_NS}}}Task")

    def test_arguments_are_escaped_and_round_trip(self):
        cmd, args = AUTO.build_action(r"C:\Python312\pythonw.exe", app_dir=r"D:\src\app")
        xml = AUTO.task_xml(cmd, args, r"D:\src", user="PC\\me")
        back = AUTO.parse_task_xml(xml)
        self.assertEqual(back["command"], cmd)
        self.assertEqual(back["arguments"], args)
        self.assertEqual(back["working_dir"], r"D:\src")
        self.assertEqual(back["user"], "PC\\me")
        self.assertEqual(back["run_level"], "HighestAvailable")

    def test_working_dir_defaults_to_the_exe_directory(self):
        back = AUTO.parse_task_xml(AUTO.task_xml(TRAY, user="u"))
        self.assertEqual(back["working_dir"], os.path.dirname(TRAY))

    def test_parse_survives_garbage(self):
        self.assertEqual(AUTO.parse_task_xml("")["command"], "")
        self.assertEqual(AUTO.parse_task_xml("<not xml")["command"], "")
        # A hand-edited export that is not valid XML still yields its command.
        got = AUTO.parse_task_xml("<Task><Exec><Command>x.exe</Command></Exec")
        self.assertEqual(got["command"], "x.exe")

    def test_scheduler_priority_is_normal_not_below_normal(self):
        # 7, the scheduler's default, is BELOW_NORMAL_PRIORITY_CLASS; a
        # process driving 1 ms endpoints must not start there.
        self.assertIn("<Priority>4</Priority>", AUTO.task_xml(TRAY, user="u"))


class EnableDisableTests(_Rig):
    def test_enable_creates_the_task_from_a_document(self):
        AUTO.enable()
        create = [c for c in self.sch.calls if c[0] == "/Create"]
        self.assertEqual(len(create), 1)
        self.assertEqual(create[0][:3], ["/Create", "/TN", AUTO.TASK_NAME])
        self.assertIn("/F", create[0])
        self.assertIn("/XML", create[0])
        # The temp document is gone afterwards.
        self.assertFalse(os.path.exists(create[0][create[0].index("/XML") + 1]))
        got = AUTO.parse_task_xml(self.sch.tasks[AUTO.TASK_NAME])
        self.assertEqual(got["command"], TRAY)
        self.assertEqual(got["run_level"], "HighestAvailable")

    def test_read_back_is_the_one_line_form(self):
        AUTO.enable()
        self.assertTrue(AUTO.is_enabled())
        self.assertEqual(AUTO.current_command(), f'"{TRAY}"')

    def test_a_python_launcher_reads_back_with_its_arguments(self):
        AUTO.enable(r"C:\Python312\pythonw.exe")
        cmd = AUTO.current_command()
        self.assertTrue(cmd.startswith('"C:\\Python312\\pythonw.exe" -c "'))
        self.assertIn("from ds5app.cli import main", cmd)

    def test_not_registered_reads_as_off(self):
        self.assertFalse(AUTO.is_enabled())
        self.assertIsNone(AUTO.current_command())
        self.assertIsNone(AUTO.query_task())

    def test_enable_is_idempotent(self):
        AUTO.enable()
        AUTO.enable()
        self.assertTrue(AUTO.is_enabled())
        self.assertEqual(len([c for c in self.sch.calls if c[0] == "/Create"]), 2)

    def test_disable_deletes_the_task_and_absent_is_fine(self):
        AUTO.enable()
        AUTO.disable()
        self.assertFalse(AUTO.is_enabled())
        self.assertEqual(len([c for c in self.sch.calls if c[0] == "/Delete"]), 1)
        AUTO.disable()                       # nothing to delete: no error
        self.assertEqual(len([c for c in self.sch.calls if c[0] == "/Delete"]), 1)

    def test_a_refused_create_is_an_actionable_error(self):
        self.sch.refuse_create = True
        with self.assertRaises(AUTO.AutostartError) as cm:
            AUTO.enable()
        self.assertIn("administrator", str(cm.exception))
        self.assertIn("Access is denied", str(cm.exception))
        self.assertFalse(AUTO.is_enabled())

    def test_set_enabled_routes_both_ways(self):
        AUTO.set_enabled(True)
        self.assertTrue(AUTO.is_enabled())
        AUTO.set_enabled(False)
        self.assertFalse(AUTO.is_enabled())

    def test_enable_and_disable_both_remove_the_old_run_value(self):
        self.legacy[AUTO.VALUE_NAME] = f'"{TRAY}"'
        AUTO.enable()
        self.assertNotIn(AUTO.VALUE_NAME, self.legacy)
        self.legacy[AUTO.VALUE_NAME] = f'"{TRAY}"'
        AUTO.disable()
        self.assertNotIn(AUTO.VALUE_NAME, self.legacy)

    def test_off_windows_it_degrades_instead_of_exploding(self):
        AUTO.available = lambda: False
        self.assertFalse(AUTO.is_enabled())
        self.assertIsNone(AUTO.current_command())
        with self.assertRaises(AUTO.AutostartError):
            AUTO.enable()
        with self.assertRaises(AUTO.AutostartError):
            AUTO.disable()


class MigrationTests(_Rig):
    def test_our_run_value_becomes_the_task(self):
        self.legacy[AUTO.VALUE_NAME] = f'"{TRAY}"'
        self.assertTrue(AUTO.migrate_legacy())
        self.assertTrue(AUTO.is_enabled())
        self.assertNotIn(AUTO.VALUE_NAME, self.legacy)

    def test_a_source_checkout_value_counts_as_ours(self):
        self.legacy[AUTO.VALUE_NAME] = ('"C:\\py\\pythonw.exe" -c "import sys;'
                                        'from ds5app.cli import main;main([\'tray\'])"')
        self.assertTrue(AUTO.migrate_legacy())
        self.assertTrue(AUTO.is_enabled())

    def test_somebody_elses_value_is_left_alone(self):
        self.legacy[AUTO.VALUE_NAME] = r'"C:\Other\thing.exe"'
        self.assertFalse(AUTO.migrate_legacy())
        self.assertFalse(AUTO.is_enabled())
        self.assertIn(AUTO.VALUE_NAME, self.legacy)

    def test_nothing_to_migrate(self):
        self.assertFalse(AUTO.migrate_legacy())
        self.assertEqual(self.sch.calls, [])

    def test_task_already_there_just_drops_the_leftover(self):
        AUTO.enable()
        before = len(self.sch.calls)
        self.legacy[AUTO.VALUE_NAME] = f'"{TRAY}"'
        self.assertTrue(AUTO.migrate_legacy())
        self.assertNotIn(AUTO.VALUE_NAME, self.legacy)
        # One query to see the task exists; no second /Create.
        self.assertEqual([c[0] for c in self.sch.calls[before:]], ["/Query"])

    def test_unelevated_leaves_the_value_for_next_time(self):
        self.sch.refuse_create = True
        self.legacy[AUTO.VALUE_NAME] = f'"{TRAY}"'
        self.assertFalse(AUTO.migrate_legacy())      # no exception
        self.assertIn(AUTO.VALUE_NAME, self.legacy)


class ReadOnlyOnThisMachine(unittest.TestCase):
    """Against the real schtasks, read-only: never creates or deletes."""

    @unittest.skipUnless(sys.platform == "win32", "schtasks is Windows")
    def test_reading_never_raises(self):
        self.assertIsInstance(AUTO.is_enabled(), bool)
        cmd = AUTO.current_command()
        self.assertTrue(cmd is None or isinstance(cmd, str))

    def test_current_user_has_a_name(self):
        self.assertTrue(AUTO.current_user())


if __name__ == "__main__":
    unittest.main()
