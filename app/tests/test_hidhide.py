"""HidHide, with no HidHide -- every backend faked.

The feature's failure mode is an INVISIBLE CONTROLLER, so what these tests
actually guard is not "does hiding work" (that needs a driver and is Q3/Q4 in
`docs/hidhide-scoping.md`) but the four properties that decide whether a user
can always get their pad back:

    * the journal is written BEFORE the hide and deleted AFTER the unhide, so a
      crash in between leaves a record of something that is not hidden rather
      than something hidden with no record;
    * the sweep unhides what a dead process left and NEVER what a live sibling
      still owns;
    * unhide is on every stop path, including the failure paths;
    * we never remove a blacklist entry we did not add.

Plus the oscillation guard from scoping section 6.7, which is the one bug in
this design that would look like flaky Bluetooth for a day before anybody found
it.

No driver, no registry, no hardware: the IOCTL and CLI backends are replaced
with in-memory fakes and DS5_CONFIG points every test at its own directory.

    prototype\\.venv\\Scripts\\python.exe -m unittest discover -s app\\tests -t app
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock


def _load(name: str):
    """ds5app.<name>, falling back to a direct file load. See test_config.py."""
    try:
        return __import__(f"ds5app.{name}", fromlist=[name])
    except ImportError:
        path = Path(__file__).resolve().parents[1] / "ds5app" / f"{name}.py"
        spec = importlib.util.spec_from_file_location(f"_ds5app_{name}", path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module


HH = _load("hidhide")
CFG = _load("config")

for _name in ("ds5app.hidhide", "ds5app.config", "ds5app.manager",
              "ds5app.service"):
    logging.getLogger(_name).addHandler(logging.NullHandler())

SERIAL = "d42f4ba1485d"
OTHER = "a0fa9c0dd8bb"
IDS = [r"HID\{00001124-0000-1000-8000-00805F9B34FB}_VID&0002054C_PID&0CE6\9&28DA590B&0&0000"]
STRANGER = r"HID\VID_054C&PID_05C4\3&11223344&0&0000"


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


class FakeIoctl:
    """An in-memory HidHide. `available=False` reproduces the real machine.

    On the H0 development machine the control device answered right after the
    install and then stopped answering, which is what makes the CLI fallback
    load-bearing rather than theoretical -- so it has to be testable.
    """

    def __init__(self, available=True, blacklist=None, whitelist=None,
                 active=False):
        self.available = available
        self.blacklist = list(blacklist or [])
        self.whitelist = list(whitelist or [])
        self.is_active = active
        self.sets = 0

    def hidden(self):
        return list(self.blacklist) if self.available else None

    def allowed(self):
        return list(self.whitelist) if self.available else None

    def set_hidden(self, items):
        if not self.available:
            return False
        self.sets += 1
        self.blacklist = list(items)
        return True

    def set_allowed(self, items):
        if not self.available:
            return False
        self.whitelist = list(items)
        return True

    def active(self):
        return self.is_active if self.available else None

    def set_active(self, on):
        if not self.available:
            return False
        self.is_active = bool(on)
        return True


class FakeCli:
    """The `HidHideCLI.exe` surface, add-one/remove-one as the real verbs are."""

    def __init__(self, ok=True, blacklist=None, whitelist=None, active=False):
        self.exe = r"C:\fake\HidHideCLI.exe"
        self.ok = ok
        self.blacklist = list(blacklist or [])
        self.whitelist = list(whitelist or [])
        self.is_active = active
        self.calls = []

    def version(self):
        return "1.5.230.0"

    def hidden(self):
        return list(self.blacklist) if self.ok else None

    def allowed(self):
        return list(self.whitelist) if self.ok else None

    def hide_one(self, i):
        self.calls.append(("hide", i))
        if not self.ok:
            return False
        if i not in self.blacklist:
            self.blacklist.append(i)
        return True

    def unhide_one(self, i):
        self.calls.append(("unhide", i))
        if not self.ok:
            return False
        if i in self.blacklist:
            self.blacklist.remove(i)
        return True

    def allow_one(self, p):
        if not self.ok:
            return False
        if p not in self.whitelist:
            self.whitelist.append(p)
        return True

    def active(self):
        return self.is_active if self.ok else None

    def set_active(self, on):
        if not self.ok:
            return False
        self.is_active = bool(on)
        return True

    def gaming_devices(self):
        return []


def make(ioctl=None, cli=None):
    """A `HidHide` wired to fakes, without touching `detect()`."""
    hh = HH.HidHide.__new__(HH.HidHide)
    hh.ioctl = ioctl if ioctl is not None else FakeIoctl()
    hh.cli = cli
    hh.installed = True
    hh._version = "1.5.230.0"
    return hh


class _Temp(unittest.TestCase):
    """Its own directory, its own DS5_CONFIG, its own journal.

    `revive_serial` is stubbed out here for everybody: the real one talks to
    cfgmgr32 and, when elevated, to `pnputil`, neither of which belongs in a
    test about journals and blacklists. What each unhide path ASKED it to do is
    recorded in `self.revived`, and `TestRevive` exercises the real thing
    against mocked CM and pnputil calls.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        self._saved = os.environ.get("DS5_CONFIG")
        os.environ["DS5_CONFIG"] = self.dir
        self.addCleanup(self._restore)

        self.revived: list = []

        def fake_revive(serial, parent_id="", log_fn=None, **kw):
            self.revived.append((serial, parent_id))
            return {"serial": serial, "visible": True, "action": "none",
                    "parent": parent_id, "hint": ""}

        p = mock.patch.object(HH, "revive_serial", fake_revive)
        p.start()
        self.addCleanup(p.stop)

    def _restore(self):
        if self._saved is None:
            os.environ.pop("DS5_CONFIG", None)
        else:
            os.environ["DS5_CONFIG"] = self._saved
        self._tmp.cleanup()

    def journal(self):
        return HH.journal_dir()


# ---------------------------------------------------------------------------
# MULTI_SZ
# ---------------------------------------------------------------------------


class TestMultiSz(unittest.TestCase):
    def test_round_trip(self):
        for items in ([], ["a"], ["a", "b"], IDS, [r"C:\x\y.exe", r"D:\z.exe"]):
            self.assertEqual(HH.unpack_multi_sz(HH.pack_multi_sz(items)), items)

    def test_always_an_even_number_of_bytes(self):
        """DEVELOPER.md requires it, and an odd length is rejected by the driver."""
        for items in ([], ["a"], ["ab", "cde"]):
            self.assertEqual(len(HH.pack_multi_sz(items)) % 2, 0)

    def test_empty_list_is_a_bare_terminator(self):
        """Clearing the blacklist is a real operation, not a no-op."""
        self.assertEqual(HH.pack_multi_sz([]), "\0".encode("utf-16-le"))

    def test_terminators_are_not_returned_as_entries(self):
        self.assertEqual(HH.unpack_multi_sz("a\0b\0\0".encode("utf-16-le")),
                         ["a", "b"])

    def test_garbage_does_not_raise(self):
        self.assertIsInstance(HH.unpack_multi_sz(b"\x01"), list)
        self.assertEqual(HH.unpack_multi_sz(b""), [])


# ---------------------------------------------------------------------------
# hide / unhide semantics
# ---------------------------------------------------------------------------


class TestHideUnhide(unittest.TestCase):
    def test_hide_is_additive_and_idempotent(self):
        f = FakeIoctl(blacklist=[STRANGER])
        hh = make(f)
        self.assertTrue(hh.hide(IDS))
        self.assertEqual(f.blacklist, [STRANGER] + IDS)
        self.assertTrue(hh.hide(IDS))
        self.assertEqual(f.blacklist, [STRANGER] + IDS)

    def test_hide_of_an_already_present_entry_writes_nothing(self):
        """A no-op must not spend a read-modify-write; that is a chance to lose."""
        f = FakeIoctl(blacklist=list(IDS))
        make(f).hide(IDS)
        self.assertEqual(f.sets, 0)

    def test_unhide_removes_only_ours(self):
        """The user may be hiding other pads for DS4Windows. Never touch them."""
        f = FakeIoctl(blacklist=[STRANGER] + IDS)
        self.assertTrue(make(f).unhide(IDS))
        self.assertEqual(f.blacklist, [STRANGER])

    def test_unhide_of_an_absent_entry_succeeds(self):
        """The record-before-hide ordering depends on this being a no-op."""
        f = FakeIoctl(blacklist=[STRANGER])
        self.assertTrue(make(f).unhide(IDS))
        self.assertEqual(f.blacklist, [STRANGER])

    def test_empty_input_is_a_success(self):
        hh = make()
        self.assertTrue(hh.hide([]))
        self.assertTrue(hh.unhide(None))

    def test_falls_back_to_the_cli_when_the_ioctl_is_unavailable(self):
        """The H0 case: the control device stopped answering, the CLI kept working."""
        f = FakeIoctl(available=False)
        cli = FakeCli()
        hh = make(f, cli)
        self.assertTrue(hh.hide(IDS))
        self.assertEqual(cli.blacklist, IDS)
        self.assertTrue(hh.unhide(IDS))
        self.assertEqual(cli.blacklist, [])

    def test_reports_failure_when_no_surface_works(self):
        hh = make(FakeIoctl(available=False), FakeCli(ok=False))
        self.assertFalse(hh.hide(IDS))
        self.assertFalse(hh.unhide(IDS))

    def test_no_backend_at_all_does_not_raise(self):
        hh = make(FakeIoctl(available=False), None)
        self.assertFalse(hh.hide(IDS))
        self.assertFalse(hh.unhide(IDS))

    def test_active_prefers_the_ioctl_then_the_cli(self):
        self.assertTrue(make(FakeIoctl(active=True)).active())
        self.assertTrue(make(FakeIoctl(available=False), FakeCli(active=True)).active())
        self.assertIsNone(make(FakeIoctl(available=False), None).active())


class TestAllow(unittest.TestCase):
    def test_adds_and_is_idempotent(self):
        f = FakeIoctl()
        hh = make(f)
        me = sys.executable
        self.assertTrue(hh.allow([me]))
        first = list(f.whitelist)
        self.assertTrue(hh.allow([me]))
        self.assertEqual(f.whitelist, first)

    def test_prunes_only_our_own_dead_entries(self):
        """`--app-clean` would drop a stranger's dead path too. We must not."""
        dead_ours = r"C:\gone\ds5bridge.exe"
        dead_theirs = r"C:\gone\SomebodyElse.exe"
        f = FakeIoctl(whitelist=[dead_ours, dead_theirs])
        make(f).allow([sys.executable])
        self.assertNotIn(dead_ours, f.whitelist)
        self.assertIn(dead_theirs, f.whitelist)

    def test_keeps_live_entries_of_any_name(self):
        alive = sys.executable
        f = FakeIoctl(whitelist=[alive])
        make(f).allow([alive])
        self.assertIn(os.path.realpath(alive), [os.path.realpath(p)
                                                for p in f.whitelist])


class TestOurImages(unittest.TestCase):
    def test_includes_the_running_interpreter_realpathed(self):
        """Junctions break whitelisting (HidHide #79), so every path is resolved."""
        images = HH.our_images()
        self.assertIn(os.path.realpath(sys.executable), images)
        for p in images:
            self.assertEqual(p, os.path.realpath(p))

    def test_no_duplicates(self):
        images = HH.our_images()
        self.assertEqual(len(images), len(set(images)))

    @unittest.skipUnless(sys.platform == "win32", "Windows only")
    def test_includes_the_real_process_image_not_just_sys_executable(self):
        """The bug that cost two rounds of hardware testing.

        A Windows venv's Scripts\\python.exe is a launcher that runs the base
        interpreter as a CHILD process. `sys.executable` reports the launcher;
        HidHide matches the child. Whitelisting only `sys.executable` registers
        a shim that never opens a HID device, so blocking works, granting does
        not, and the bridge goes blind.
        """
        real = HH.current_image_path()
        self.assertTrue(real, "could not read our own image path")
        self.assertIn(os.path.realpath(real), HH.our_images())

    @unittest.skipUnless(sys.platform == "win32", "Windows only")
    def test_the_real_image_is_first(self):
        """It is the authoritative one, so it must not be lost to a truncation."""
        self.assertEqual(HH.our_images()[0],
                         os.path.realpath(HH.current_image_path()))

    @unittest.skipUnless(sys.platform == "win32", "Windows only")
    def test_current_image_path_is_an_existing_exe(self):
        p = HH.current_image_path()
        self.assertTrue(os.path.isfile(p), p)
        self.assertTrue(p.lower().endswith(".exe"), p)


class TestWhitelistCoversUs(unittest.TestCase):
    """The read-back guard: does the driver hold OUR image, not just any path?"""

    @unittest.skipUnless(sys.platform == "win32", "Windows only")
    def test_true_when_our_real_image_is_listed(self):
        f = FakeIoctl(whitelist=[HH.current_image_path()])
        self.assertIs(make(f).whitelist_covers_us(), True)

    @unittest.skipUnless(sys.platform == "win32", "Windows only")
    def test_false_when_only_a_launcher_shim_is_listed(self):
        """Exactly the observed failure: the shim is listed, the real image is not."""
        shim = r"D:\somewhere\.venv\Scripts\python.exe"
        f = FakeIoctl(whitelist=[shim])
        self.assertIs(make(f).whitelist_covers_us(), False)

    def test_none_when_the_whitelist_cannot_be_read(self):
        """Unknown is not the same as no, and must not block hiding."""
        self.assertIsNone(make(FakeIoctl(available=False), None)
                          .whitelist_covers_us())

    def test_none_for_an_empty_list(self):
        """HidHide's own clients self-register, so empty means unreadable."""
        self.assertIsNone(make(FakeIoctl(whitelist=[])).whitelist_covers_us())

    @unittest.skipUnless(sys.platform == "win32", "Windows only")
    def test_after_allow_we_are_covered(self):
        """`allow(our_images())` must actually satisfy the guard it feeds."""
        f = FakeIoctl()
        hh = make(f)
        hh.allow(HH.our_images())
        self.assertIs(hh.whitelist_covers_us(), True)


class TestRefusesToBlindItself(_Temp):
    def test_hide_is_refused_when_we_are_not_whitelisted(self):
        """Hiding a pad we cannot open is worse than not hiding it at all."""
        f = FakeIoctl()
        hh = make(f)
        with mock.patch.object(HH.HidHide, "detect", return_value=hh), \
                mock.patch.object(HH, "resolve_serial", return_value=IDS), \
                mock.patch.object(HH.HidHide, "whitelist_covers_us",
                                  return_value=False):
            said = []
            self.assertEqual(
                HH.hide_for_bridge(SERIAL, log_fn=said.append), [])
        self.assertEqual(f.blacklist, [], "the pad was hidden anyway")
        self.assertEqual(HH.read_records(), [], "a record was left behind")
        self.assertTrue(any("whitelist" in s for s in said), said)

    def test_hide_proceeds_when_the_guard_cannot_tell(self):
        """Unknown must not become a refusal, or a CLI-only install never hides."""
        f = FakeIoctl()
        hh = make(f)
        with mock.patch.object(HH.HidHide, "detect", return_value=hh), \
                mock.patch.object(HH, "resolve_serial", return_value=IDS), \
                mock.patch.object(HH.HidHide, "whitelist_covers_us",
                                  return_value=None):
            self.assertEqual(HH.hide_for_bridge(SERIAL), IDS)
        self.assertEqual(f.blacklist, IDS)

    def test_the_guard_runs_after_allow_not_before(self):
        """Otherwise a first-ever run would always refuse."""
        order = []

        class Watcher(FakeIoctl):
            def set_allowed(inner, items):    # noqa: N805
                order.append("allow")
                return FakeIoctl.set_allowed(inner, items)

        hh = make(Watcher())
        real = HH.HidHide.whitelist_covers_us

        def spy(self):
            order.append("guard")
            return real(self)

        with mock.patch.object(HH.HidHide, "detect", return_value=hh), \
                mock.patch.object(HH, "resolve_serial", return_value=IDS), \
                mock.patch.object(HH.HidHide, "whitelist_covers_us", spy):
            HH.hide_for_bridge(SERIAL)
        self.assertEqual(order[:2], ["allow", "guard"])


class TestVerifyMarker(_Temp):
    def test_round_trip(self):
        self.assertTrue(HH.write_verify_marker(True, True, image="x.exe",
                                               version="1.5.230.0"))
        rec = HH.read_verify_marker()
        self.assertTrue(rec["blocking"])
        self.assertTrue(rec["granting"])
        self.assertEqual(rec["image"], "x.exe")
        self.assertIn("at", rec)

    def test_absent_marker_is_an_empty_dict(self):
        self.assertEqual(HH.read_verify_marker(), {})

    def test_corrupt_marker_is_an_empty_dict(self):
        p = HH.verify_marker_path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write("{not json")
        self.assertEqual(HH.read_verify_marker(), {})

    def test_it_is_not_in_the_journal_directory(self):
        """The sweep deletes what it finds there; a measurement is not a debt."""
        HH.write_verify_marker(True, True)
        self.assertNotEqual(
            os.path.dirname(HH.verify_marker_path()).rstrip("\\/"),
            HH.journal_dir().rstrip("\\/"))
        self.assertEqual(HH.read_records(), [])

    def test_the_sweep_does_not_eat_it(self):
        HH.write_verify_marker(True, True)
        HH.write_record(SERIAL, IDS)
        with mock.patch.object(HH, "owner_alive", return_value=False):
            HH.sweep(hh=make(FakeIoctl(blacklist=list(IDS))))
        self.assertTrue(HH.read_verify_marker())


# ---------------------------------------------------------------------------
# the journal
# ---------------------------------------------------------------------------


class TestJournal(_Temp):
    def test_write_read_remove(self):
        self.assertTrue(HH.write_record(SERIAL, IDS))
        recs = HH.read_records()
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["serial"], SERIAL)
        self.assertEqual(recs[0]["instance_ids"], IDS)
        self.assertEqual(recs[0]["pid"], os.getpid())
        HH.remove_record(SERIAL)
        self.assertEqual(HH.read_records(), [])

    def test_honours_ds5_config(self):
        """A test that swept the developer's real hidden/ would be a bad day."""
        HH.write_record(SERIAL, IDS)
        self.assertTrue(HH.journal_dir().startswith(os.path.abspath(self.dir)))
        self.assertTrue(os.path.isfile(
            os.path.join(self.journal(), f"{SERIAL}.json")))

    def test_serial_is_lowercased(self):
        HH.write_record("D42F4BA1485D", IDS)
        self.assertEqual(HH.read_records()[0]["serial"], SERIAL)

    def test_unparseable_record_is_skipped_not_fatal(self):
        os.makedirs(self.journal(), exist_ok=True)
        with open(os.path.join(self.journal(), "broken.json"), "w") as f:
            f.write("{not json")
        HH.write_record(SERIAL, IDS)
        recs = HH.read_records()
        self.assertEqual([r["serial"] for r in recs], [SERIAL])

    def test_non_json_files_are_ignored(self):
        os.makedirs(self.journal(), exist_ok=True)
        open(os.path.join(self.journal(), "notes.txt"), "w").close()
        self.assertEqual(HH.read_records(), [])

    def test_missing_directory_is_not_an_error(self):
        self.assertEqual(HH.read_records(), [])
        self.assertEqual(HH.journal_count(), 0)

    def test_remove_is_idempotent(self):
        HH.remove_record(SERIAL)
        HH.remove_record(SERIAL)

    def test_hidden_serials_reads_the_journal(self):
        HH.write_record(SERIAL, IDS)
        HH.write_record(OTHER, IDS)
        self.assertEqual(sorted(HH.hidden_serials()), sorted([SERIAL, OTHER]))


class TestRecordBeforeHide(_Temp):
    """The single most important ordering in this feature."""

    def test_the_record_exists_before_hide_is_called(self):
        seen = {}

        class Watcher(FakeIoctl):
            def set_hidden(inner, items):     # noqa: N805
                seen["journal"] = os.path.isfile(
                    os.path.join(self.journal(), f"{SERIAL}.json"))
                return FakeIoctl.set_hidden(inner, items)

        hh = make(Watcher())
        with mock.patch.object(HH.HidHide, "detect", return_value=hh), \
                mock.patch.object(HH, "resolve_serial", return_value=IDS):
            HH.hide_for_bridge(SERIAL)
        self.assertTrue(seen.get("journal"),
                        "hide() ran before the journal entry was on disk")

    def test_a_failed_journal_write_aborts_the_hide(self):
        """Hiding something we could not record is what strands a user."""
        f = FakeIoctl()
        hh = make(f)
        with mock.patch.object(HH.HidHide, "detect", return_value=hh), \
                mock.patch.object(HH, "resolve_serial", return_value=IDS), \
                mock.patch.object(HH, "write_record", return_value=False):
            self.assertEqual(HH.hide_for_bridge(SERIAL), [])
        self.assertEqual(f.blacklist, [])

    def test_a_failed_hide_removes_the_record(self):
        """Otherwise the next sweep reports a debt that was never taken on."""
        hh = make(FakeIoctl(available=False), FakeCli(ok=False))
        with mock.patch.object(HH.HidHide, "detect", return_value=hh), \
                mock.patch.object(HH, "resolve_serial", return_value=IDS):
            self.assertEqual(HH.hide_for_bridge(SERIAL), [])
        self.assertEqual(HH.read_records(), [])

    def test_the_record_survives_until_after_the_unhide(self):
        seen = {}
        f = FakeIoctl()

        class Watcher(FakeIoctl):
            def set_hidden(inner, items):     # noqa: N805
                seen["journal_at_unhide"] = os.path.isfile(
                    os.path.join(self.journal(), f"{SERIAL}.json"))
                return FakeIoctl.set_hidden(inner, items)

        HH.write_record(SERIAL, IDS)
        hh = make(Watcher(blacklist=list(IDS)))
        with mock.patch.object(HH.HidHide, "detect", return_value=hh):
            HH.unhide_for_bridge(SERIAL)
        self.assertTrue(seen.get("journal_at_unhide"))
        self.assertEqual(HH.read_records(), [])
        del f


# ---------------------------------------------------------------------------
# hide_for_bridge / unhide_for_bridge
# ---------------------------------------------------------------------------


class TestBridgeHooks(_Temp):
    def test_hide_whitelists_before_it_hides(self):
        """Or the tray blinds itself -- scoping 6.7, the oscillation trap."""
        order = []
        f = FakeIoctl()

        class Watcher(FakeIoctl):
            def set_allowed(inner, items):    # noqa: N805
                order.append("allow")
                return FakeIoctl.set_allowed(inner, items)

            def set_hidden(inner, items):     # noqa: N805
                order.append("hide")
                return FakeIoctl.set_hidden(inner, items)

        hh = make(Watcher())
        with mock.patch.object(HH.HidHide, "detect", return_value=hh), \
                mock.patch.object(HH, "resolve_serial", return_value=IDS):
            HH.hide_for_bridge(SERIAL)
        self.assertEqual(order[:2], ["allow", "hide"])
        del f

    def test_enables_the_cloak_and_records_that_we_did(self):
        f = FakeIoctl(active=False)
        hh = make(f)
        with mock.patch.object(HH.HidHide, "detect", return_value=hh), \
                mock.patch.object(HH, "resolve_serial", return_value=IDS):
            HH.hide_for_bridge(SERIAL)
        self.assertTrue(f.is_active)
        self.assertTrue(HH.read_records()[0]["cloak_enabled_by_us"])

    def test_does_not_claim_a_cloak_that_was_already_on(self):
        """It is a GLOBAL flag: turning it off would unhide somebody else's pad."""
        f = FakeIoctl(active=True)
        hh = make(f)
        with mock.patch.object(HH.HidHide, "detect", return_value=hh), \
                mock.patch.object(HH, "resolve_serial", return_value=IDS):
            HH.hide_for_bridge(SERIAL)
        self.assertFalse(HH.read_records()[0]["cloak_enabled_by_us"])
        with mock.patch.object(HH.HidHide, "detect", return_value=hh):
            HH.unhide_for_bridge(SERIAL)
        self.assertTrue(f.is_active, "we turned off a cloak we did not enable")

    def test_unhide_turns_our_own_cloak_back_off(self):
        f = FakeIoctl(active=False)
        hh = make(f)
        with mock.patch.object(HH.HidHide, "detect", return_value=hh), \
                mock.patch.object(HH, "resolve_serial", return_value=IDS):
            HH.hide_for_bridge(SERIAL)
        with mock.patch.object(HH.HidHide, "detect", return_value=hh):
            HH.unhide_for_bridge(SERIAL)
        self.assertFalse(f.is_active)

    def test_no_hidhide_installed_is_not_a_failure(self):
        with mock.patch.object(HH.HidHide, "detect", return_value=None):
            self.assertEqual(HH.hide_for_bridge(SERIAL), [])
        self.assertEqual(HH.read_records(), [])

    def test_unresolvable_serial_writes_no_record(self):
        hh = make()
        with mock.patch.object(HH.HidHide, "detect", return_value=hh), \
                mock.patch.object(HH, "resolve_serial", return_value=[]):
            self.assertEqual(HH.hide_for_bridge(SERIAL), [])
        self.assertEqual(HH.read_records(), [])

    def test_unhide_without_a_record_is_a_no_op_success(self):
        with mock.patch.object(HH.HidHide, "detect", return_value=make()):
            self.assertTrue(HH.unhide_for_bridge(SERIAL))

    def test_unhide_keeps_the_record_when_it_fails(self):
        """So the next start's sweep tries again instead of forgetting."""
        HH.write_record(SERIAL, IDS)
        hh = make(FakeIoctl(available=False), FakeCli(ok=False))
        with mock.patch.object(HH.HidHide, "detect", return_value=hh):
            self.assertFalse(HH.unhide_for_bridge(SERIAL))
        self.assertEqual(len(HH.read_records()), 1)

    def test_neither_hook_raises_when_everything_explodes(self):
        # A record has to exist, or `unhide_for_bridge` correctly short-circuits
        # to success before it ever looks for HidHide.
        HH.write_record(SERIAL, IDS)
        with mock.patch.object(HH.HidHide, "detect",
                               side_effect=RuntimeError("boom")):
            self.assertEqual(HH.hide_for_bridge(SERIAL), [])
            self.assertFalse(HH.unhide_for_bridge(SERIAL))

    def test_only_this_serial_is_unhidden(self):
        HH.write_record(SERIAL, IDS)
        HH.write_record(OTHER, [STRANGER])
        f = FakeIoctl(blacklist=IDS + [STRANGER])
        with mock.patch.object(HH.HidHide, "detect", return_value=make(f)):
            HH.unhide_for_bridge(SERIAL)
        self.assertEqual(f.blacklist, [STRANGER])
        self.assertEqual([r["serial"] for r in HH.read_records()], [OTHER])


# ---------------------------------------------------------------------------
# the sweep
# ---------------------------------------------------------------------------


class TestSweep(_Temp):
    def test_empty_journal_is_silent_and_cheap(self):
        """The normal case. It must not even go looking for HidHide."""
        with mock.patch.object(HH.HidHide, "detect",
                               side_effect=AssertionError("should not detect")):
            self.assertEqual(HH.sweep(), 0)

    def test_unhides_and_clears_a_dead_owner(self):
        HH.write_record(SERIAL, IDS)
        f = FakeIoctl(blacklist=list(IDS))
        with mock.patch.object(HH, "owner_alive", return_value=False):
            self.assertEqual(HH.sweep(hh=make(f)), 1)
        self.assertEqual(f.blacklist, [])
        self.assertEqual(HH.read_records(), [])

    def test_leaves_a_live_siblings_entry_alone(self):
        """Launching the tray must not unhide a `ds5bridge run`'s controller."""
        HH.write_record(SERIAL, IDS)
        f = FakeIoctl(blacklist=list(IDS))
        with mock.patch.object(HH, "owner_alive", return_value=True):
            self.assertEqual(HH.sweep(hh=make(f)), 0)
        self.assertEqual(f.blacklist, IDS)
        self.assertEqual(len(HH.read_records()), 1)

    def test_force_ignores_liveness(self):
        """The panic button: I do not care what is running."""
        HH.write_record(SERIAL, IDS)
        f = FakeIoctl(blacklist=list(IDS))
        with mock.patch.object(HH, "owner_alive", return_value=True):
            self.assertEqual(HH.sweep(hh=make(f), force=True), 1)
        self.assertEqual(f.blacklist, [])

    def test_never_touches_an_entry_we_did_not_record(self):
        HH.write_record(SERIAL, IDS)
        f = FakeIoctl(blacklist=[STRANGER] + IDS)
        with mock.patch.object(HH, "owner_alive", return_value=False):
            HH.sweep(hh=make(f))
        self.assertEqual(f.blacklist, [STRANGER])

    def test_a_failed_unhide_keeps_the_record(self):
        HH.write_record(SERIAL, IDS)
        hh = make(FakeIoctl(available=False), FakeCli(ok=False))
        with mock.patch.object(HH, "owner_alive", return_value=False):
            self.assertEqual(HH.sweep(hh=hh), 0)
        self.assertEqual(len(HH.read_records()), 1)

    def test_mixed_live_and_dead(self):
        HH.write_record(SERIAL, IDS)
        HH.write_record(OTHER, [STRANGER])
        f = FakeIoctl(blacklist=IDS + [STRANGER])

        def alive(rec):
            return rec.get("serial") == OTHER

        with mock.patch.object(HH, "owner_alive", side_effect=alive):
            self.assertEqual(HH.sweep(hh=make(f)), 1)
        self.assertEqual(f.blacklist, [STRANGER])
        self.assertEqual([r["serial"] for r in HH.read_records()], [OTHER])

    def test_cloak_off_only_when_ours_and_nothing_is_left(self):
        HH.write_record(SERIAL, IDS, cloak_enabled_by_us=True)
        f = FakeIoctl(blacklist=list(IDS), active=True)
        with mock.patch.object(HH, "owner_alive", return_value=False):
            HH.sweep(hh=make(f))
        self.assertFalse(f.is_active)

    def test_cloak_stays_on_when_another_record_remains(self):
        HH.write_record(SERIAL, IDS, cloak_enabled_by_us=True)
        HH.write_record(OTHER, [STRANGER], cloak_enabled_by_us=True)
        f = FakeIoctl(blacklist=IDS + [STRANGER], active=True)

        def alive(rec):
            return rec.get("serial") == OTHER

        with mock.patch.object(HH, "owner_alive", side_effect=alive):
            HH.sweep(hh=make(f))
        self.assertTrue(f.is_active)

    def test_hidhide_uninstalled_drops_stale_records(self):
        """Otherwise the tray offers an escape from a state that is already gone."""
        HH.write_record(SERIAL, IDS)
        with mock.patch.object(HH.HidHide, "detect", return_value=None), \
                mock.patch.object(HH, "owner_alive", return_value=False):
            self.assertEqual(HH.sweep(), 1)
        self.assertEqual(HH.read_records(), [])


class TestOwnerAlive(_Temp):
    def test_our_own_pid_is_never_a_live_owner(self):
        """A record this process wrote and then crashed past is ours to clear."""
        self.assertFalse(HH.owner_alive({"pid": os.getpid()}))

    def test_missing_or_bad_pid(self):
        for rec in ({}, {"pid": None}, {"pid": "abc"}, {"pid": 0}, {"pid": -1}):
            self.assertFalse(HH.owner_alive(rec))

    def test_system_pids_are_rejected(self):
        self.assertFalse(HH.owner_alive({"pid": 4}))

    @unittest.skipUnless(sys.platform == "win32", "Windows only")
    def test_a_live_process_that_is_not_ours_is_not_an_owner(self):
        """A recycled PID must not keep a stale record alive forever."""
        with mock.patch.object(HH, "process_image_name", return_value="notepad.exe"):
            self.assertFalse(HH.owner_alive({"pid": 999999}))

    @unittest.skipUnless(sys.platform == "win32", "Windows only")
    def test_a_live_process_that_is_ours_is_an_owner(self):
        with mock.patch.object(HH, "process_image_name", return_value="ds5bridge.exe"):
            self.assertTrue(HH.owner_alive({"pid": 999999}))

    @unittest.skipUnless(sys.platform == "win32", "Windows only")
    def test_process_image_name_of_this_process(self):
        name = HH.process_image_name(os.getpid())
        self.assertTrue(name.lower().endswith(".exe"), name)


# ---------------------------------------------------------------------------
# serialisation
# ---------------------------------------------------------------------------


class TestMutex(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "win32", "Windows only")
    def test_two_threads_do_not_overlap(self):
        """The list API is replace-the-whole-list; an overlap loses an entry."""
        inside = []
        overlapped = []

        def worker():
            with HH._Mutex(r"Local\ds5bridge-hidhide-test"):
                inside.append(1)
                if len(inside) > 1:
                    overlapped.append(1)
                time.sleep(0.05)
                inside.pop()

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(overlapped, [], "two holders were inside at once")

    @unittest.skipUnless(sys.platform == "win32", "Windows only")
    def test_reentrant_for_one_thread(self):
        """A Win32 mutex is owned per THREAD, so nesting must not deadlock."""
        with HH._Mutex(r"Local\ds5bridge-hidhide-test2"):
            with HH._Mutex(r"Local\ds5bridge-hidhide-test2"):
                pass

    def test_a_busy_mutex_does_not_block_forever(self):
        """Refusing to unhide because a mutex was busy is worse than proceeding."""
        m = HH._Mutex(r"Local\ds5bridge-hidhide-test3", timeout_ms=1)
        with m:
            pass

    def test_hide_holds_the_lock_across_the_read_modify_write(self):
        held = []

        class Watcher(FakeIoctl):
            def hidden(inner):                # noqa: N805
                held.append("read")
                return FakeIoctl.hidden(inner)

            def set_hidden(inner, items):     # noqa: N805
                held.append("write")
                return FakeIoctl.set_hidden(inner, items)

        real = HH._Mutex.__enter__
        entries = []

        def spy(self):
            entries.append("enter")
            return real(self)

        with mock.patch.object(HH._Mutex, "__enter__", spy):
            make(Watcher()).hide(IDS)
        self.assertEqual(held, ["read", "write"])
        self.assertEqual(len(entries), 1, "the lock was taken more than once")


# ---------------------------------------------------------------------------
# detection
# ---------------------------------------------------------------------------


class TestDetect(unittest.TestCase):
    def test_returns_none_when_nothing_is_installed(self):
        with mock.patch.object(HH, "_driver_installed", return_value=False), \
                mock.patch.object(HH, "find_cli", return_value=""):
            self.assertIsNone(HH.HidHide.detect())

    def test_detects_from_the_driver_alone(self):
        with mock.patch.object(HH, "_driver_installed", return_value=True), \
                mock.patch.object(HH, "find_cli", return_value=""), \
                mock.patch.object(sys, "platform", "win32"):
            hh = HH.HidHide.detect()
        self.assertIsNotNone(hh)
        self.assertIsNone(hh.cli)

    def test_detects_from_the_cli_alone(self):
        """The driver key is unreadable on some machines; the CLI still proves it."""
        with mock.patch.object(HH, "_driver_installed", return_value=False), \
                mock.patch.object(HH, "find_cli", return_value=r"C:\x\HidHideCLI.exe"), \
                mock.patch.object(sys, "platform", "win32"):
            self.assertIsNotNone(HH.HidHide.detect())

    def test_never_raises(self):
        with mock.patch.object(HH, "_driver_installed",
                               side_effect=RuntimeError("boom")):
            self.assertIsNone(HH.HidHide.detect())

    def test_find_cli_honours_the_override(self):
        with tempfile.TemporaryDirectory() as d:
            exe = os.path.join(d, "HidHideCLI.exe")
            open(exe, "w").close()
            self.assertEqual(HH.find_cli(exe), exe)

    def test_a_bad_override_falls_through_rather_than_raising(self):
        self.assertNotEqual(HH.find_cli(r"C:\nope\nothing.exe"), r"C:\nope\nothing.exe")

    def test_control_ok_is_not_the_same_question_as_installed(self):
        """H0: HidHide installed and healthy, control device refusing to open."""
        hh = make(FakeIoctl(available=False), None)
        self.assertFalse(hh.control_ok())
        self.assertTrue(make(FakeIoctl()).control_ok())


# ---------------------------------------------------------------------------
# CLI output parsing
# ---------------------------------------------------------------------------


class TestCliParsing(unittest.TestCase):
    def _cli(self, out, code=0):
        cli = HH.CliBackend(r"C:\x\HidHideCLI.exe")
        cli._run = lambda *a: (code, out)
        return cli

    def test_app_list_quoted_form(self):
        """The real `--app-list` prints re-runnable commands, not bare paths."""
        out = ('--app-reg "C:\\Program Files\\Nefarius Software Solutions'
               '\\HidHide\\x64\\HidHideCLI.exe"\n')
        self.assertEqual(
            self._cli(out).allowed(),
            [r"C:\Program Files\Nefarius Software Solutions\HidHide\x64"
             r"\HidHideCLI.exe"])

    def test_dev_list(self):
        self.assertEqual(self._cli(IDS[0] + "\n\n").hidden(), IDS)

    def test_cloak_state(self):
        self.assertFalse(self._cli("--cloak-off\n").active())
        self.assertTrue(self._cli("--cloak-on\n").active())
        self.assertIsNone(self._cli("nonsense\n").active())

    def test_a_failing_cli_reports_none_not_an_empty_list(self):
        """None means 'could not answer'; [] means 'nothing is hidden'."""
        self.assertIsNone(self._cli("", code=1).hidden())
        self.assertIsNone(self._cli("", code=1).allowed())

    def test_gaming_devices_json(self):
        blob = json.dumps([{"friendlyName": "DualSense", "devices": [
            {"serialNumber": SERIAL, "deviceInstancePath": IDS[0],
             "baseContainerDeviceCount": 1}]}])
        devs = self._cli(blob).gaming_devices()
        self.assertEqual(len(devs), 1)
        self.assertEqual(devs[0]["serialNumber"], SERIAL)

    def test_malformed_json_is_not_fatal(self):
        self.assertEqual(self._cli("{{{").gaming_devices(), [])


class TestPathConversion(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "win32", "Windows only")
    def test_dos_to_nt_and_back(self):
        nt = HH.dos_to_nt(sys.executable)
        self.assertTrue(nt.startswith("\\Device\\"), nt)
        self.assertEqual(os.path.normcase(HH.nt_to_dos(nt)),
                         os.path.normcase(os.path.realpath(sys.executable)))

    def test_an_already_nt_path_is_left_alone(self):
        p = r"\Device\HarddiskVolume3\x\y.exe"
        self.assertEqual(HH.dos_to_nt(p), p)

    def test_an_untranslatable_nt_path_comes_back_unchanged(self):
        p = r"\Device\NoSuchVolume999\x.exe"
        self.assertEqual(HH.nt_to_dos(p), p)


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------


class TestConfigKeys(_Temp):
    def test_hide_bluetooth_defaults_off(self):
        """The failure mode is an invisible controller. It ships OFF."""
        self.assertFalse(CFG.Config().hide_bluetooth_default)
        self.assertFalse(CFG.ControllerConfig().hide_bluetooth)
        self.assertFalse(CFG.load().should_hide(SERIAL))

    def test_default_seeds_a_new_controller(self):
        cfg = CFG.Config(hide_bluetooth_default=True)
        self.assertTrue(cfg.get(SERIAL).hide_bluetooth)

    def test_round_trip(self):
        cfg = CFG.load()
        cfg.set_hide_bluetooth(SERIAL, True)
        cfg.hidhide_cli = r"C:\x\HidHideCLI.exe"
        CFG.save(cfg)
        again = CFG.load()
        self.assertTrue(again.should_hide(SERIAL))
        self.assertEqual(again.hidhide_cli, r"C:\x\HidHideCLI.exe")

    def test_an_unknown_value_does_not_turn_it_on(self):
        for bad in ("maybe", [], {}, None):
            cc = CFG.ControllerConfig.from_dict({"hide_bluetooth": bad})
            self.assertFalse(cc.hide_bluetooth)

    def test_a_hand_edited_string_is_honoured(self):
        self.assertTrue(
            CFG.ControllerConfig.from_dict({"hide_bluetooth": "yes"}).hide_bluetooth)

    def test_hidhide_cli_empty_string_is_none(self):
        self.assertIsNone(CFG.Config.from_dict({"hidhide_cli": ""}).hidhide_cli)

    def test_the_keys_are_written_so_an_older_build_preserves_them(self):
        d = CFG.Config().to_dict()
        self.assertIn("hide_bluetooth_default", d)
        self.assertIn("hidhide_cli", d)
        self.assertIn("hide_bluetooth", CFG.ControllerConfig().to_dict())


# ---------------------------------------------------------------------------
# BridgeService: unhide is part of STOP, on every path
# ---------------------------------------------------------------------------

try:
    from ds5app import service as SVC
except Exception:  # noqa: BLE001  (no hidapi on this interpreter)
    SVC = None


class FakeUsbip:
    def __init__(self):
        self.log = []

    def stop_auto_reattach(self):
        self.log.append("attach-X")

    def detach(self, p):
        self.log.append(("detach", p))

    def our_ports(self):
        return []


@unittest.skipIf(SVC is None, "the hardware stack is not importable here")
class TestServiceStopPaths(_Temp):
    """`BridgeService.stop()` unhides, before the detach, on every path.

    The invariant this project needs is "unhide is part of stop", not "unhide is
    part of the tray" -- because the tray toggle, the master switch, hotplug's
    `vanish_grace` expiry, a child crash, Quit, Ctrl+C and a closed console all
    converge on this one method. Testing it here rather than through the tray is
    the point.
    """

    def svc(self, **kw):
        s = SVC.BridgeService(serial=SERIAL, **kw)
        s.state = SVC.RUNNING
        s.usbip = FakeUsbip()
        s._attached = True
        s._our_ports = [1]
        self.calls = []
        s._unhide = lambda quiet=False: (self.calls.append("unhide") or True)
        s._hide = lambda: (self.calls.append("hide") or list(IDS))
        return s

    def test_stop_unhides_before_it_detaches(self):
        s = self.svc()
        s.stop()
        self.assertEqual(self.calls, ["unhide"])
        self.assertEqual(s.usbip.log[0], "attach-X")

    def test_stop_unhides_even_when_hiding_was_never_switched_on(self):
        """The journal on disk is the record, not an in-memory flag."""
        s = self.svc(hide_bluetooth=False)
        s.stop()
        self.assertIn("unhide", self.calls)

    def test_a_quiet_stop_still_unhides(self):
        """`start()` calls `stop(quiet=True)` after a failed bring-up."""
        s = self.svc()
        s.stop(quiet=True)
        self.assertIn("unhide", self.calls)

    def test_an_unhide_that_raises_does_not_stop_the_teardown(self):
        s = self.svc()

        def boom(quiet=False):
            raise RuntimeError("HidHide exploded")

        s._unhide = boom
        s.stop()                                # must not propagate
        self.assertEqual(s.state, SVC.STOPPED)
        self.assertIn("attach-X", s.usbip.log)

    def test_stop_is_idempotent(self):
        s = self.svc()
        s.stop()
        s.stop()
        self.assertEqual(self.calls.count("unhide"), 1)

    def test_hide_is_off_by_default(self):
        self.assertFalse(SVC.BridgeService(serial=SERIAL).hide_bluetooth)

    def test_the_flag_and_the_cli_override_are_carried(self):
        s = SVC.BridgeService(serial=SERIAL, hide_bluetooth=True,
                              hidhide_cli=r"C:\x\HidHideCLI.exe")
        self.assertTrue(s.hide_bluetooth)
        self.assertEqual(s.hidhide_cli, r"C:\x\HidHideCLI.exe")


@unittest.skipIf(SVC is None, "the hardware stack is not importable here")
class TestSweepOnStart(_Temp):
    def test_the_sweep_runs_once_per_process(self):
        SVC._swept = False
        self.addCleanup(setattr, SVC, "_swept", True)
        with mock.patch.object(HH, "sweep", return_value=0) as swept:
            SVC.hidhide_sweep_once()
            SVC.hidhide_sweep_once()
        self.assertEqual(swept.call_count, 1)

    def test_a_broken_sweep_never_stops_a_start(self):
        SVC._swept = False
        self.addCleanup(setattr, SVC, "_swept", True)
        with mock.patch.object(HH, "sweep", side_effect=RuntimeError("boom")):
            self.assertEqual(SVC.hidhide_sweep_once(), 0)


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------

try:
    from ds5app import cli as CLI
except Exception:  # noqa: BLE001
    CLI = None


class _Cand:
    def __init__(self, serial):
        self.serial = serial


@unittest.skipIf(CLI is None, "the hardware stack is not importable here")
class TestDoctorRows(_Temp):
    """`doctor` is what the user guide tells people to paste into an issue.

    "My controller vanished from Windows" is the worst thing this feature can
    do to somebody, so every fact needed to diagnose it has to be in here -- and
    the column alignment has to match the rows around it, which a recent commit
    deliberately fixed.
    """

    def render(self, cfg, live=(), visible=None):
        """`visible` is what hidapi says about every configured controller.

        None by default -- "cannot tell", which is the honest answer with no
        hidapi and the only one that keeps these rows hardware-free. The real
        check enumerates HID devices and reads the Bluetooth devnode tree, and
        on a developer's machine that finds their own controller.
        """
        import io
        import contextlib

        buf = io.StringIO()
        with mock.patch.object(HH, "pad_visible", lambda s: visible), \
                contextlib.redirect_stdout(buf):
            problems = CLI._doctor_hidhide(cfg, list(live))
        return buf.getvalue(), problems

    def test_not_installed_is_a_note_when_nobody_asked_for_it(self):
        with mock.patch.object(HH.HidHide, "detect", return_value=None):
            out, problems = self.render(CFG.Config())
        self.assertIn("[note]", out)
        self.assertIn("not installed", out)
        self.assertEqual(problems, 0)

    def test_not_installed_is_a_warning_when_a_controller_wants_it(self):
        cfg = CFG.Config()
        cfg.set_hide_bluetooth(SERIAL, True)
        with mock.patch.object(HH.HidHide, "detect", return_value=None):
            out, problems = self.render(cfg, [_Cand(SERIAL)])
        self.assertIn("[warn]", out)
        self.assertIn("will do nothing", out)
        self.assertEqual(problems, 1)

    def test_installed_and_reachable(self):
        with mock.patch.object(HH.HidHide, "detect", return_value=make()):
            out, problems = self.render(CFG.Config())
        self.assertIn("1.5.230.0", out)
        self.assertIn("reachable", out)
        self.assertEqual(problems, 0)

    def test_installed_but_unreachable_is_a_warning_naming_the_reboot(self):
        """The H0 state: driver present and healthy, control device refusing."""
        hh = make(FakeIoctl(available=False), None)
        with mock.patch.object(HH.HidHide, "detect", return_value=hh):
            out, problems = self.render(CFG.Config())
        self.assertIn("[warn]", out)
        self.assertIn("reboot", out)
        self.assertEqual(problems, 1)

    def test_granting_is_reported_by_name_when_it_passes(self):
        with mock.patch.object(HH.HidHide, "detect", return_value=make()), \
                mock.patch.object(HH.HidHide, "whitelist_covers_us",
                                  return_value=True):
            out, problems = self.render(CFG.Config())
        self.assertIn("granting", out)
        self.assertIn("whitelisted", out)
        self.assertEqual(problems, 0)

    def test_granting_failure_is_a_named_condition_not_a_mystery(self):
        """Blocking and granting fail independently; conflating them wastes days."""
        cfg = CFG.Config()
        cfg.set_hide_bluetooth(SERIAL, True)
        with mock.patch.object(HH.HidHide, "detect", return_value=make()), \
                mock.patch.object(HH.HidHide, "whitelist_covers_us",
                                  return_value=False):
            out, problems = self.render(cfg, [_Cand(SERIAL)])
        self.assertIn("granting", out)
        self.assertIn("REFUSED", out)
        self.assertEqual(problems, 1)

    def test_not_whitelisted_while_idle_is_not_a_problem(self):
        """We register only when about to hide, so idle-and-absent is normal.

        Counting it as a problem would make `doctor` cry wolf on every healthy
        machine that simply is not using the feature.
        """
        with mock.patch.object(HH.HidHide, "detect", return_value=make()), \
                mock.patch.object(HH.HidHide, "whitelist_covers_us",
                                  return_value=False):
            out, problems = self.render(CFG.Config())
        self.assertIn("[note]", out)
        self.assertIn("registered at the next bridge start", out)
        self.assertEqual(problems, 0)

    def test_an_unknown_grant_is_a_note_not_a_problem(self):
        with mock.patch.object(HH.HidHide, "detect", return_value=make()), \
                mock.patch.object(HH.HidHide, "whitelist_covers_us",
                                  return_value=None):
            out, problems = self.render(CFG.Config())
        self.assertIn("[note]", out)
        self.assertEqual(problems, 0)

    def test_it_says_when_hiding_was_never_verified_end_to_end(self):
        with mock.patch.object(HH.HidHide, "detect", return_value=make()):
            out, _ = self.render(CFG.Config())
        self.assertIn("verified   never", out)
        self.assertIn("hidhide_verify.py", out)

    def test_it_reports_a_past_verification(self):
        HH.write_verify_marker(True, True)
        with mock.patch.object(HH.HidHide, "detect", return_value=make()):
            out, _ = self.render(CFG.Config())
        self.assertIn("blocking and granting both passed", out)

    def test_a_failed_past_verification_is_a_warning(self):
        HH.write_verify_marker(True, False)
        with mock.patch.object(HH.HidHide, "detect", return_value=make()):
            out, _ = self.render(CFG.Config())
        self.assertIn("granting=False", out)

    def test_a_clean_journal_says_so(self):
        with mock.patch.object(HH.HidHide, "detect", return_value=make()):
            out, _ = self.render(CFG.Config())
        self.assertIn("hidden by  us: nothing", out)

    def test_a_connected_pad_nobody_can_enumerate_is_a_finding(self):
        """Every other row can be green while the pad is unusable.

        Blacklist empty, cloak off, no records -- and the HID child devnode is
        a phantom, so no application on the machine can open the controller.
        The 2026-08-27 report, in one command.
        """
        cfg = CFG.Config()
        cfg.set_hide_bluetooth(SERIAL, True)
        with mock.patch.object(HH.HidHide, "detect", return_value=make()), \
                mock.patch.object(HH, "bt_parent_for_serial", lambda s: PARENT), \
                mock.patch.object(HH, "devnode_present", lambda i: True):
            out, problems = self.render(cfg, visible=False)
        self.assertIn("cannot be enumerated", out)
        self.assertIn(SERIAL, out)
        self.assertGreaterEqual(problems, 1)

    def test_a_controller_in_a_drawer_is_not_a_finding(self):
        """It is switched off. A diagnostic that says so about every pad
        anybody owns is a diagnostic people stop reading."""
        cfg = CFG.Config()
        cfg.set_hide_bluetooth(SERIAL, True)
        with mock.patch.object(HH.HidHide, "detect", return_value=make()), \
                mock.patch.object(HH, "bt_parent_for_serial", lambda s: PARENT), \
                mock.patch.object(HH, "devnode_present", lambda i: False):
            out, problems = self.render(cfg, visible=False)
        self.assertNotIn("cannot be enumerated", out)
        self.assertEqual(problems, 0)

    def test_no_hidapi_means_no_verdict(self):
        """`doctor` must work without the hardware stack; it must not guess."""
        cfg = CFG.Config()
        cfg.set_hide_bluetooth(SERIAL, True)
        with mock.patch.object(HH.HidHide, "detect", return_value=make()):
            out, problems = self.render(cfg, visible=None)
        self.assertNotIn("cannot be enumerated", out)
        self.assertEqual(problems, 0)

    def test_a_record_with_a_dead_owner_is_loud(self):
        """This is the signature of a crash that did not get to unhide."""
        HH.write_record(SERIAL, IDS)
        with mock.patch.object(HH.HidHide, "detect", return_value=make()), \
                mock.patch.object(HH, "owner_alive", return_value=False):
            out, problems = self.render(CFG.Config())
        self.assertIn("OWNER IS GONE", out)
        self.assertIn("ds5bridge unhide", out)
        self.assertEqual(problems, 1)

    def test_a_record_with_a_live_owner_is_not_a_problem(self):
        HH.write_record(SERIAL, IDS)
        with mock.patch.object(HH.HidHide, "detect", return_value=make()), \
                mock.patch.object(HH, "owner_alive", return_value=True):
            out, problems = self.render(CFG.Config())
        self.assertNotIn("OWNER IS GONE", out)
        self.assertEqual(problems, 0)

    def test_the_columns_line_up_with_the_rest_of_doctor(self):
        """Every status row is a 7-character bracketed flag then the text."""
        HH.write_record(SERIAL, IDS)
        with mock.patch.object(HH.HidHide, "detect", return_value=make()), \
                mock.patch.object(HH, "owner_alive", return_value=False):
            out, _ = self.render(CFG.Config())
        for line in out.splitlines():
            if line.startswith("["):
                self.assertRegex(line, r"^\[[a-zA-Z]+\]\s*\S")
                self.assertGreaterEqual(len(line) - len(line.lstrip("[")), 1)
            # Continuation rows are indented to the same 7-column gutter.
            elif line.strip():
                self.assertTrue(line.startswith(" " * 7), repr(line))

    def test_it_reports_rather_than_raises_when_hidhide_explodes(self):
        """A diagnostic command that dies with a traceback has failed its job."""
        with mock.patch.object(HH.HidHide, "detect",
                               side_effect=RuntimeError("boom")):
            out, problems = self.render(CFG.Config())
        self.assertIn("[warn]", out)
        self.assertIn("could not be checked", out)
        self.assertEqual(problems, 1)


# ---------------------------------------------------------------------------
# what "unhiding does not unhide" turned out to be (2026-08-27)
# ---------------------------------------------------------------------------


class TestCliListingParsing(unittest.TestCase):
    """`--dev-list` prints re-runnable commands, not bare instance paths.

    Unparsed, every entry HidHide reported was a string beginning `--dev-hide "`
    that no device instance ID could ever equal -- so `unhide()` found nothing
    to remove and called that success.
    """

    def parse(self, out, verb):
        return HH.CliBackend._parse_listing(out, verb)

    def test_a_dev_list_line_yields_the_path_inside_the_quotes(self):
        self.assertEqual(self.parse('--dev-hide "' + IDS[0] + '"\n', "--dev-hide"),
                         [IDS[0]])

    def test_the_app_list_shape_still_works(self):
        exe = r"C:\Program Files\x\y.exe"
        self.assertEqual(self.parse('--app-reg "' + exe + '"\n', "--app-reg"), [exe])

    def test_several_entries_come_back_in_order(self):
        out = '--dev-hide "' + IDS[0] + '"\n--dev-hide "' + STRANGER + '"\n'
        self.assertEqual(self.parse(out, "--dev-hide"), [IDS[0], STRANGER])

    def test_a_bare_value_is_still_accepted(self):
        # Being wrong about which shape a HidHide release prints is how this
        # bug happened; the tolerant branch is deliberate.
        self.assertEqual(self.parse(IDS[0] + "\n", "--dev-hide"), [IDS[0]])

    def test_blank_lines_and_banners_are_not_entries(self):
        out = '\n   \n--dev-hide "' + IDS[0] + '"\n'
        self.assertEqual(self.parse(out, "--dev-hide"), [IDS[0]])

    def test_an_empty_listing_is_empty_not_a_ghost_entry(self):
        self.assertEqual(self.parse("", "--dev-hide"), [])
        self.assertEqual(self.parse("\n\n", "--dev-hide"), [])


class TestCliStdin(unittest.TestCase):
    """HidHideCLI keeps reading verbs from stdin until EOF -- see the docstring.

    With the parent's console inherited it never exits, so every call died on
    the 30 s timeout and looked exactly like "HidHide refused" -- disabling the
    fallback precisely when the IOCTL surface was the thing that had failed.
    """

    def test_every_invocation_closes_stdin(self):
        seen = {}

        class R:
            returncode, stdout, stderr = 0, "", ""

        def fake_run(cmd, **kw):
            seen.update(kw)
            return R()

        with mock.patch("subprocess.run", fake_run):
            HH.CliBackend(r"C:\fake\HidHideCLI.exe")._run("--dev-list")
        self.assertIs(seen.get("stdin"), HH.subprocess.DEVNULL)

    def test_a_timeout_is_a_failure_not_a_silent_empty_list(self):
        def fake_run(cmd, **kw):
            raise HH.subprocess.TimeoutExpired(cmd, 30)

        with mock.patch("subprocess.run", fake_run):
            self.assertEqual(HH.CliBackend(r"C:\x.exe")._run("--dev-list"), (-1, ""))


class TestInstanceIdsAreCaseInsensitive(unittest.TestCase):
    """Windows device instance paths are case-insensitive; our sources disagree.

    Measured on one boot: the interface property and `--dev-gaming` say
    `_VID&0002054c_`, `Get-PnpDevice` says `_VID&0002054C_`. A case-sensitive
    membership test against the blacklist is a coin flip, and the side it lands
    on decides whether a user gets their controller back.
    """

    def test_the_normal_form_folds_case_and_strips_quotes(self):
        self.assertEqual(HH.norm_instance_id('  "' + IDS[0].upper() + '" '),
                         HH.norm_instance_id(IDS[0].lower()))

    def test_unhide_matches_an_entry_that_differs_only_in_case(self):
        f = FakeIoctl(blacklist=[IDS[0].lower()])
        self.assertTrue(make(f).unhide([IDS[0].upper()]))
        self.assertEqual(f.blacklist, [])

    def test_hide_does_not_add_a_second_spelling_of_the_same_device(self):
        f = FakeIoctl(blacklist=[IDS[0].lower()])
        self.assertTrue(make(f).hide([IDS[0].upper()]))
        self.assertEqual(f.blacklist, [IDS[0].lower()])

    def test_a_strangers_entry_is_still_left_alone(self):
        f = FakeIoctl(blacklist=[STRANGER, IDS[0].lower()])
        make(f).unhide([IDS[0].upper()])
        self.assertEqual(f.blacklist, [STRANGER])


class _LyingIoctl(FakeIoctl):
    """Accepts the write and changes nothing. What a stale handle looks like."""

    def set_hidden(self, items):
        self.sets += 1
        return True


class TestUnhideIsProven(unittest.TestCase):
    """Success is a read-back, never a return code.

    True is what deletes the journal record, and the record is the user's last
    way back to a visible controller.
    """

    def test_a_write_that_silently_did_nothing_is_a_failure(self):
        f = _LyingIoctl(blacklist=list(IDS))
        self.assertFalse(make(f).unhide(IDS))
        self.assertEqual(f.blacklist, IDS)

    def test_an_unreadable_blacklist_is_a_failure_not_a_shrug(self):
        # "Nobody can tell" must never round up to success.
        self.assertFalse(make(FakeIoctl(available=False), None).unhide(IDS))

    def test_the_cli_finishes_what_the_ioctl_could_not(self):
        # The CLI is not an `else`: the two surfaces can disagree about what
        # the driver is actually enforcing.
        cli = FakeCli(blacklist=list(IDS))
        hh = make(_LyingIoctl(blacklist=list(IDS)), cli)
        hh.ioctl.hidden = lambda: list(cli.blacklist)
        self.assertTrue(hh.unhide(IDS))
        self.assertEqual(cli.blacklist, [])

    def test_success_still_means_gone(self):
        f = FakeIoctl(blacklist=[STRANGER] + IDS)
        self.assertTrue(make(f).unhide(IDS))
        self.assertEqual(f.blacklist, [STRANGER])


class TestLeftoversWithNoRecord(_Temp):
    """The state one lying unhide leaves behind: an entry nothing points at.

    `journal_count()` reads zero, so every path that trusts the journal --
    `sweep()`, `ds5bridge unhide`, the tray's own panic button -- reports
    all-clear while the pad is invisible to every application on the machine.
    """

    def test_an_entry_of_ours_with_no_record_is_reported(self):
        self.assertEqual(HH.unrecorded_hidden(make(FakeIoctl(blacklist=list(IDS)))),
                         IDS)

    def test_a_recorded_entry_is_not_reported(self):
        HH.write_record("aabbccddeeff", IDS, cloak_enabled_by_us=False)
        self.assertEqual(HH.unrecorded_hidden(make(FakeIoctl(blacklist=list(IDS)))),
                         [])

    def test_the_match_survives_a_different_spelling(self):
        HH.write_record("aabbccddeeff", [IDS[0].lower()], cloak_enabled_by_us=False)
        hh = make(FakeIoctl(blacklist=[IDS[0].upper()]))
        self.assertEqual(HH.unrecorded_hidden(hh), [])

    def test_an_unreadable_blacklist_is_none_not_empty(self):
        # [] is "nothing is hidden"; None is "cannot tell". The tray says
        # different things about them, and must.
        self.assertIsNone(HH.unrecorded_hidden(make(FakeIoctl(available=False), None)))

    def test_no_hidhide_at_all_is_nothing_hidden(self):
        with mock.patch.object(HH.HidHide, "detect",
                               staticmethod(lambda *a, **k: None)):
            self.assertEqual(HH.unrecorded_hidden(), [])

    def test_a_clean_machine_reports_nothing(self):
        self.assertEqual(HH.unrecorded_hidden(make(FakeIoctl())), [])


# ---------------------------------------------------------------------------
# an empty blacklist is not a working controller
# ---------------------------------------------------------------------------

#: The same physical pad, before and after one hide/revive cycle. The `&11&` in
#: the second is the re-enumeration counter -- measured on a user's machine,
#: 2026-08-27, and the reason a recorded instance ID cannot be identity.
OLD_CHILD = r"HID\{00001124-0000-1000-8000-00805F9B34FB}_VID&0002054C_PID&0CE6\8&2FDE51C0&0&0000"
NEW_CHILD = r"HID\{00001124-0000-1000-8000-00805F9B34FB}_VID&0002054C_PID&0CE6\8&110FB383&11&0000"
PARENT = (r"BTHENUM\{00001124-0000-1000-8000-00805F9B34FB}_VID&0002054C_PID&0CE6"
          r"\7&16440032&0&D42F4BA1485D_C00000000")


class TestRevive(unittest.TestCase):
    """`revive_serial`, with cfgmgr32 and pnputil mocked.

    The failure it repairs, measured on a user's machine after a tray Quit:
    HidHide's blacklist was verifiably EMPTY and the pad was still not there,
    because its HID child devnode had gone phantom. Clearing a blacklist does
    not re-create a devnode; only a re-enumeration does.

    The properties worth breaking a build over:

      * the non-admin route is tried FIRST and, when it works, `pnputil` is
        never reached -- this program must never come to need elevation;
      * nothing escalates past what it can verify;
      * a failure says "power-cycle the controller" instead of nothing.
    """

    def _patch(self, *, visible, reenum=True, elevated=False, pnputil=(0, ""),
               parent=PARENT, connected=True):
        """Mock every seam. `visible` is a list of successive answers."""
        self.seen = {"reenum": [], "pnputil": []}
        answers = list(visible)

        def pad_visible(_serial):
            return answers.pop(0) if len(answers) > 1 else answers[0]

        def _reenumerate(iid):
            self.seen["reenum"].append(iid)
            return reenum

        def _pnputil_restart(iid):
            self.seen["pnputil"].append(iid)
            return pnputil

        for name, fn in (("pad_visible", pad_visible),
                         ("_reenumerate", _reenumerate),
                         ("_pnputil_restart", _pnputil_restart),
                         ("devnode_present", lambda i: connected),
                         ("is_elevated", lambda: elevated),
                         ("bt_parent_for_serial", lambda s: parent)):
            p = mock.patch.object(HH, name, fn)
            p.start()
            self.addCleanup(p.stop)

    def test_a_visible_pad_is_left_completely_alone(self):
        """The normal case: one enumeration, no devnode touched."""
        self._patch(visible=[True])
        r = HH.revive_serial(SERIAL, settle=0.0)
        self.assertEqual(r["action"], "none")
        self.assertTrue(r["visible"])
        self.assertEqual(self.seen["reenum"], [])
        self.assertEqual(self.seen["pnputil"], [])

    def test_a_controller_that_is_simply_switched_off_is_left_alone(self):
        """The most ordinary event there is, and it is not a fault.

        Most of this program's teardowns begin with somebody switching a
        controller off. Re-enumerating a Bluetooth node for that, and then
        advising a power-cycle of a pad they turned off deliberately, would
        train the user to ignore the one message that matters.
        """
        said = []
        self._patch(visible=[False], connected=False, elevated=True)
        r = HH.revive_serial(SERIAL, settle=0.0, log_fn=said.append)
        self.assertEqual(r["action"], "disconnected")
        self.assertEqual(self.seen["reenum"], [])
        self.assertEqual(self.seen["pnputil"], [])
        self.assertEqual(said, [], "a switched-off pad must not be nagged about")

    def test_a_blacklist_change_is_given_time_to_take_effect(self):
        """The driver applies an unhide, not us.

        Tearing a devnode down because we looked half a second too early would
        be this feature causing the failure it exists to fix -- and with a
        bridge still running, a devnode restart takes its HID handle with it.
        """
        self._patch(visible=[False, True])
        r = HH.revive_serial(SERIAL, settle=1.0)
        self.assertTrue(r["visible"])
        self.assertEqual(r["action"], "none")
        self.assertEqual(self.seen["reenum"], [],
                         "it restarted a devnode that was about to be fine")

    def test_the_non_admin_route_is_tried_first_and_is_enough(self):
        self._patch(visible=[False, False, True])
        r = HH.revive_serial(SERIAL, settle=0.0)
        self.assertEqual(r["action"], "reenumerate")
        self.assertTrue(r["visible"])
        self.assertEqual(self.seen["reenum"], [PARENT],
                         "the PARENT is what gets re-enumerated, not the child")
        self.assertEqual(self.seen["pnputil"], [],
                         "an elevated restart must never be needed when the "
                         "user-mode call did the job")

    def test_the_recorded_parent_beats_a_fresh_lookup(self):
        """After the hide, nothing can resolve the address any more."""
        self._patch(visible=[False, False, True], parent="")
        r = HH.revive_serial(SERIAL, parent_id=PARENT, settle=0.0)
        self.assertEqual(self.seen["reenum"], [PARENT])
        self.assertTrue(r["visible"])

    def test_unelevated_it_says_what_to_do_instead_of_failing_silently(self):
        said = []
        self._patch(visible=[False], elevated=False)
        r = HH.revive_serial(SERIAL, settle=0.0, log_fn=said.append)
        self.assertFalse(r["visible"])
        self.assertEqual(r["action"], "failed")
        self.assertEqual(self.seen["pnputil"], [],
                         "pnputil unelevated fails with a message about "
                         "administrator rights; predict it, do not provoke it")
        self.assertTrue(any("OFF" in t and "on again" in t for t in said), said)

    def test_elevated_it_escalates_to_pnputil(self):
        self._patch(visible=[False, False, False, True], elevated=True)
        r = HH.revive_serial(SERIAL, settle=0.0)
        self.assertEqual(self.seen["pnputil"], [PARENT])
        self.assertEqual(r["action"], "pnputil")
        self.assertTrue(r["visible"])

    def test_a_pnputil_that_fails_is_not_success(self):
        said = []
        self._patch(visible=[False], elevated=True, pnputil=(1, "denied"))
        r = HH.revive_serial(SERIAL, settle=0.0, log_fn=said.append)
        self.assertFalse(r["visible"])
        self.assertTrue(said)

    def test_no_bluetooth_parent_means_ask_the_user(self):
        """Nothing to restart -- but the pad is still gone, so say so."""
        said = []
        self._patch(visible=[False], parent="")
        r = HH.revive_serial(SERIAL, settle=0.0, log_fn=said.append)
        self.assertEqual(r["action"], "unknown")
        self.assertFalse(r["visible"])
        self.assertEqual(self.seen["reenum"], [])
        self.assertTrue(said)

    def test_an_unverifiable_answer_never_escalates(self):
        """No hidapi: the check cannot be made, so nothing may be restarted.

        None is a third answer. Rounding it to "broken" would restart a devnode
        on a suspicion that cannot be confirmed; rounding it to "fine" would be
        the lie this whole section exists to stop telling.
        """
        self._patch(visible=[None], elevated=True)
        r = HH.revive_serial(SERIAL, settle=0.0)
        self.assertIsNone(r["visible"])
        self.assertEqual(self.seen["reenum"], [])
        self.assertEqual(self.seen["pnputil"], [])

    def test_a_reenumeration_that_cannot_be_confirmed_stops_there(self):
        self._patch(visible=[False, False, None], elevated=True)
        r = HH.revive_serial(SERIAL, settle=0.0)
        self.assertEqual(self.seen["reenum"], [PARENT])
        self.assertEqual(self.seen["pnputil"], [],
                         "an elevated devnode restart on an unverifiable "
                         "suspicion is worse than leaving it")
        self.assertIsNone(r["visible"])

    def test_nothing_it_does_can_raise(self):
        def boom(_s):
            raise OSError("cfgmgr32 is having a day")

        with mock.patch.object(HH, "pad_visible", boom):
            self.assertEqual(HH.revive_serial(SERIAL, settle=0.0)["action"],
                             "none")


class TestParentMatching(unittest.TestCase):
    """Instance IDs churn; the Bluetooth parent does not."""

    def test_entries_are_matched_by_their_parent(self):
        with mock.patch.object(HH, "parent_instance_id",
                               lambda i: PARENT if "0CE6" in i.upper() else ""):
            self.assertEqual(
                HH.entries_under_parent([NEW_CHILD, STRANGER], PARENT),
                [NEW_CHILD])

    def test_a_strangers_pad_is_never_matched(self):
        with mock.patch.object(HH, "parent_instance_id",
                               lambda i: r"BTHENUM\somebody-elses-pad"):
            self.assertEqual(HH.entries_under_parent([STRANGER], PARENT), [])

    def test_no_parent_matches_nothing(self):
        self.assertEqual(HH.entries_under_parent([NEW_CHILD], ""), [])

    def test_the_comparison_is_case_insensitive(self):
        with mock.patch.object(HH, "parent_instance_id",
                               lambda i: PARENT.upper()):
            self.assertEqual(HH.entries_under_parent([NEW_CHILD], PARENT.lower()),
                             [NEW_CHILD])

    def test_the_mac_is_read_out_of_the_parents_own_id(self):
        with mock.patch.object(HH, "device_ids_for_enumerator",
                               lambda *a: [r"BTHENUM\Dev_AABBCCDDEEFF", PARENT]):
            self.assertEqual(HH.bt_parent_for_serial(SERIAL), PARENT)

    def test_an_unknown_address_resolves_to_nothing(self):
        with mock.patch.object(HH, "device_ids_for_enumerator",
                               lambda *a: [r"BTHENUM\Dev_AABBCCDDEEFF"]):
            self.assertEqual(HH.bt_parent_for_serial(SERIAL), "")


class TestReviveIsWiredIn(_Temp):
    """Every path that clears a blacklist entry has to finish the job."""

    def test_the_hide_records_the_bluetooth_parent(self):
        """While the pad is still visible -- afterwards nothing can look it up."""
        hh = make(FakeIoctl())
        with mock.patch.object(HH.HidHide, "detect", return_value=hh), \
                mock.patch.object(HH, "resolve_serial", return_value=[OLD_CHILD]), \
                mock.patch.object(HH, "parent_instance_id", lambda i: PARENT):
            HH.hide_for_bridge(SERIAL)
        self.assertEqual(HH.read_records()[0]["parent_id"], PARENT)

    def test_an_unhide_revives_with_the_recorded_parent(self):
        hh = make(FakeIoctl(blacklist=[OLD_CHILD]))
        HH.write_record(SERIAL, [OLD_CHILD], parent_id=PARENT)
        with mock.patch.object(HH.HidHide, "detect", return_value=hh):
            self.assertTrue(HH.unhide_for_bridge(SERIAL))
        self.assertEqual(self.revived, [(SERIAL, PARENT)])

    def test_the_sweep_revives_what_a_dead_run_left(self):
        HH.write_record(SERIAL, [OLD_CHILD], parent_id=PARENT)
        hh = make(FakeIoctl(blacklist=[OLD_CHILD]))
        with mock.patch.object(HH, "owner_alive", return_value=False):
            self.assertEqual(HH.sweep(hh), 1)
        self.assertEqual(self.revived, [(SERIAL, PARENT)])

    def test_the_sweep_takes_out_the_churned_child_too(self):
        """The record names a phantom; the blacklist holds its replacement."""
        HH.write_record(SERIAL, [OLD_CHILD], parent_id=PARENT)
        f = FakeIoctl(blacklist=[NEW_CHILD, STRANGER])
        with mock.patch.object(HH, "owner_alive", return_value=False), \
                mock.patch.object(
                    HH, "parent_instance_id",
                    lambda i: PARENT if "0CE6" in i.upper() else ""):
            self.assertEqual(HH.sweep(make(f)), 1)
        self.assertEqual(f.blacklist, [STRANGER],
                         "the entry that was actually hiding the pad survived, "
                         "and somebody else's did not")

    def test_an_unhide_widens_to_the_parent_when_the_ids_no_longer_match(self):
        HH.write_record(SERIAL, [OLD_CHILD], parent_id=PARENT)
        f = FakeIoctl(blacklist=[NEW_CHILD])
        with mock.patch.object(HH.HidHide, "detect", return_value=make(f)), \
                mock.patch.object(HH, "resolve_serial", return_value=[]), \
                mock.patch.object(
                    HH, "parent_instance_id",
                    lambda i: PARENT if "0CE6" in i.upper() else ""):
            self.assertTrue(HH.unhide_for_bridge(SERIAL))
        self.assertEqual(f.blacklist, [])
        self.assertEqual(HH.read_records(), [])

    def test_a_leftover_with_no_record_is_matched_by_parent(self):
        """hidapi cannot see a phantom pad, so the address route finds nothing."""
        f = FakeIoctl(blacklist=[NEW_CHILD])
        with mock.patch.object(HH.HidHide, "detect", return_value=make(f)), \
                mock.patch.object(HH, "resolve_serial", return_value=[]), \
                mock.patch.object(HH, "bt_parent_for_serial", lambda s: PARENT), \
                mock.patch.object(
                    HH, "parent_instance_id",
                    lambda i: PARENT if "0CE6" in i.upper() else ""):
            self.assertTrue(HH.unhide_for_bridge(SERIAL))
        self.assertEqual(f.blacklist, [])

    def test_a_failed_unhide_does_not_pretend_to_revive(self):
        HH.write_record(SERIAL, [OLD_CHILD], parent_id=PARENT)
        with mock.patch.object(HH.HidHide, "detect",
                               return_value=make(FakeIoctl(available=False), None)), \
                mock.patch.object(HH, "resolve_serial", return_value=[]):
            self.assertFalse(HH.unhide_for_bridge(SERIAL))
        self.assertEqual(self.revived, [])
        self.assertEqual(len(HH.read_records()), 1, "the record must be kept")


if __name__ == "__main__":
    unittest.main()
