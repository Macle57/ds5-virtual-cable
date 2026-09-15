"""update.py -- versions, release parsing, the decision, the cache, the stage.

Everything here runs with NOTHING installed and NO network: the HTTP layer is
injected (`http_get=`), the release documents are fixtures, and the filesystem
work happens in a tempdir. That is a constraint, not a convenience -- the CI
stdlib-only job runs this file, which is what keeps update.py honest about
being stdlib-only.
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
    """Import ds5app.<name>, or load the file directly on a bare interpreter.

    Same rig as test_config.py, for the same reason: update.py is deliberately
    stdlib-only, and the package `__init__` imports the hardware stack.
    """
    try:
        return __import__(f"ds5app.{name}", fromlist=[name])
    except ImportError:
        path = Path(__file__).resolve().parents[1] / "ds5app" / f"{name}.py"
        spec = importlib.util.spec_from_file_location(f"_ds5app_{name}", path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module


U = _load("update")

# update.py logs its "survived something bad" warnings; without a handler they
# land on stderr and make a green run read like a failing one.
logging.getLogger("ds5app.update").addHandler(logging.NullHandler())
logging.getLogger("ds5app.update").propagate = False


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def release(tag="v0.5.0", assets=None, **extra):
    """A plausible releases/latest document, overridable per test."""
    if assets is None:
        ver = tag.lstrip("v").lstrip(".")
        assets = [
            {"name": f"ds5bridge-setup-{ver}-bundled.exe",
             "browser_download_url": f"https://example.invalid/{ver}.exe",
             "size": 123456789},
            {"name": "SHA256SUMS",
             "browser_download_url": "https://example.invalid/SHA256SUMS",
             "size": 100},
        ]
    doc = {"tag_name": tag, "html_url": f"https://example.invalid/rel/{tag}",
           "draft": False, "prerelease": False, "assets": assets}
    doc.update(extra)
    return doc


class FakeHttp:
    """A scriptable `http_get`. Each call pops the next (status, headers, body)
    and records what was asked, so a test can assert If-None-Match was sent."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, headers, timeout=None):
        self.calls.append({"url": url, "headers": dict(headers)})
        if not self.responses:
            raise OSError("no network in tests")
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def ok(doc, etag='W/"abc"'):
    return (200, {"ETag": etag}, json.dumps(doc).encode())


# ---------------------------------------------------------------------------
# version arithmetic
# ---------------------------------------------------------------------------


class TestParseVersion(unittest.TestCase):
    def test_plain_and_prefixed(self):
        for text in ("0.5.0", "v0.5.0", "V0.5.0", " v0.5.0 ", "v.0.5.0"):
            self.assertEqual(U.parse_version(text), (0, 5, 0), text)

    def test_lengths(self):
        self.assertEqual(U.parse_version("1"), (1,))
        self.assertEqual(U.parse_version("v1.2.3.4"), (1, 2, 3, 4))

    def test_garbage(self):
        for text in ("", "v", "latest", "0.5.x", "v0..5", "0.5-rc1", "1.2.3-rc1",
                     None, 5, ["v0.5.0"], "v0.5.0+build"):
            self.assertIsNone(U.parse_version(text), repr(text))

    def test_prerelease_tag(self):
        self.assertTrue(U.is_prerelease_tag("v0.5.0-rc1"))
        self.assertFalse(U.is_prerelease_tag("v0.5.0"))
        self.assertFalse(U.is_prerelease_tag(None))


class TestIsNewer(unittest.TestCase):
    def test_ordering(self):
        self.assertTrue(U.is_newer((0, 5, 0), (0, 4, 0)))
        self.assertFalse(U.is_newer((0, 4, 0), (0, 5, 0)))
        self.assertFalse(U.is_newer((0, 4, 0), (0, 4, 0)))

    def test_numeric_not_lexicographic(self):
        # The classic: "0.10.0" < "0.9.9" as strings.
        self.assertTrue(U.is_newer((0, 10, 0), (0, 9, 9)))

    def test_length_padding(self):
        self.assertFalse(U.is_newer((1, 0), (1, 0, 0)))     # equal, padded
        self.assertTrue(U.is_newer((1, 0, 1), (1, 0)))

    def test_unparseable_is_no(self):
        self.assertFalse(U.is_newer(None, (0, 4, 0)))
        self.assertFalse(U.is_newer((0, 5, 0), None))
        self.assertFalse(U.is_newer(None, None))


# ---------------------------------------------------------------------------
# the decision, from a release document
# ---------------------------------------------------------------------------


class TestEvaluateRelease(unittest.TestCase):
    def test_newer_release_is_offered_with_all_fields(self):
        info = U.evaluate_release(release("v0.5.0"), current_version="0.4.0")
        self.assertIsNotNone(info)
        self.assertEqual(info.version, "0.5.0")
        self.assertEqual(info.tag, "v0.5.0")
        self.assertEqual(info.asset_name, "ds5bridge-setup-0.5.0-bundled.exe")
        self.assertEqual(info.asset_url, "https://example.invalid/0.5.0.exe")
        self.assertEqual(info.asset_size, 123456789)
        self.assertEqual(info.sums_url, "https://example.invalid/SHA256SUMS")
        self.assertEqual(info.page_url, "https://example.invalid/rel/v0.5.0")

    def test_same_and_older_are_not(self):
        self.assertIsNone(U.evaluate_release(release("v0.4.0"), "0.4.0"))
        self.assertIsNone(U.evaluate_release(release("v0.3.9"), "0.4.0"))

    def test_prerelease_and_draft_are_never_offered(self):
        self.assertIsNone(U.evaluate_release(
            release("v9.9.9", prerelease=True), "0.4.0"))
        self.assertIsNone(U.evaluate_release(
            release("v9.9.9", draft=True), "0.4.0"))
        self.assertIsNone(U.evaluate_release(release("v9.9.9-rc1"), "0.4.0"))

    def test_release_without_the_installer_is_not_offered(self):
        doc = release("v0.5.0", assets=[
            {"name": "SHA256SUMS", "browser_download_url": "u", "size": 1},
            {"name": "install.ps1", "browser_download_url": "u", "size": 1}])
        self.assertIsNone(U.evaluate_release(doc, "0.4.0"))

    def test_asset_name_must_match_exactly(self):
        # Similar-but-wrong names must not be picked up as the installer: not
        # the old zip, not the download-mode exe, not a signature file.
        doc = release("v0.5.0", assets=[
            {"name": "ds5bridge-0.5.0-win-x64.zip",
             "browser_download_url": "u", "size": 1},
            {"name": "ds5bridge-setup-0.5.0.exe",
             "browser_download_url": "u", "size": 1},
            {"name": "ds5bridge-setup-0.5.0-bundled.exe.asc",
             "browser_download_url": "u", "size": 1}])
        self.assertIsNone(U.evaluate_release(doc, "0.4.0"))

    def test_the_bundled_installer_is_the_asset(self):
        doc = release("v0.5.0", assets=[
            {"name": "ds5bridge-setup-0.5.0.exe",
             "browser_download_url": "plain", "size": 1},
            {"name": "ds5bridge-setup-0.5.0-bundled.exe",
             "browser_download_url": "bundled", "size": 1}])
        self.assertEqual(U.evaluate_release(doc, "0.4.0").asset_url, "bundled")

    def test_missing_sums_is_allowed_but_recorded_as_none(self):
        doc = release("v0.5.0")
        doc["assets"] = [a for a in doc["assets"] if a["name"] != "SHA256SUMS"]
        info = U.evaluate_release(doc, "0.4.0")
        self.assertIsNotNone(info)
        self.assertIsNone(info.sums_url)

    def test_malformed_documents_answer_none(self):
        for doc in (None, [], "v0.5.0", 42,
                    {"tag_name": "not a version"},
                    {"tag_name": ["v0.5.0"]},
                    release("v0.5.0", assets="not-a-list"),
                    release("v0.5.0", assets=[None, "x", 3])):
            self.assertIsNone(U.evaluate_release(doc, "0.4.0"), repr(doc))

    def test_asset_entry_with_missing_url_is_skipped(self):
        doc = release("v0.5.0", assets=[
            {"name": "ds5bridge-setup-0.5.0-bundled.exe", "size": 5}])
        self.assertIsNone(U.evaluate_release(doc, "0.4.0"))

    def test_missing_page_url_falls_back_to_the_releases_page(self):
        doc = release("v0.5.0")
        del doc["html_url"]
        info = U.evaluate_release(doc, "0.4.0")
        self.assertIn("/releases", info.page_url)


# ---------------------------------------------------------------------------
# check_now -- the conditional GET and its cache
# ---------------------------------------------------------------------------


class TestCheckNow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = os.path.join(self.tmp.name, "update-cache.json")

    def test_first_check_stores_etag_and_answers(self):
        http = FakeHttp(ok(release("v0.5.0")))
        info = U.check_now("0.4.0", self.cache, http_get=http)
        self.assertEqual(info.version, "0.5.0")
        self.assertNotIn("If-None-Match", http.calls[0]["headers"])
        with open(self.cache, encoding="utf-8") as f:
            stored = json.load(f)
        self.assertEqual(stored["etag"], 'W/"abc"')
        self.assertEqual(stored["release"]["tag_name"], "v0.5.0")

    def test_304_reuses_the_cache_and_sent_the_etag(self):
        http = FakeHttp(ok(release("v0.5.0")), (304, {}, b""))
        U.check_now("0.4.0", self.cache, http_get=http)
        info = U.check_now("0.4.0", self.cache, http_get=http)
        self.assertEqual(info.version, "0.5.0")
        self.assertEqual(http.calls[1]["headers"]["If-None-Match"], 'W/"abc"')

    def test_network_failure_falls_back_to_the_cache(self):
        http = FakeHttp(ok(release("v0.5.0")), OSError("offline"))
        U.check_now("0.4.0", self.cache, http_get=http)
        info = U.check_now("0.4.0", self.cache, http_get=http)
        self.assertEqual(info.version, "0.5.0")

    def test_network_failure_with_no_cache_is_silence(self):
        http = FakeHttp(OSError("offline"))
        self.assertIsNone(U.check_now("0.4.0", self.cache, http_get=http))

    def test_rate_limit_403_keeps_the_cached_answer(self):
        http = FakeHttp(ok(release("v0.5.0")), (403, {}, b"rate limited"))
        U.check_now("0.4.0", self.cache, http_get=http)
        info = U.check_now("0.4.0", self.cache, http_get=http)
        self.assertEqual(info.version, "0.5.0")

    def test_unparseable_body_is_silence_not_a_crash(self):
        http = FakeHttp((200, {}, b"<!doctype html>not json"))
        self.assertIsNone(U.check_now("0.4.0", self.cache, http_get=http))

    def test_corrupt_cache_file_is_tolerated(self):
        with open(self.cache, "w", encoding="utf-8") as f:
            f.write("{ definitely not json")
        http = FakeHttp(ok(release("v0.5.0")))
        info = U.check_now("0.4.0", self.cache, http_get=http)
        self.assertEqual(info.version, "0.5.0")
        # And the corrupt cache was replaced with a good one.
        with open(self.cache, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["release"]["tag_name"], "v0.5.0")

    def test_current_version_already_newest_answers_none_but_still_caches(self):
        http = FakeHttp(ok(release("v0.4.0")))
        self.assertIsNone(U.check_now("0.4.0", self.cache, http_get=http))
        self.assertTrue(os.path.isfile(self.cache))


# ---------------------------------------------------------------------------
# the Updater object and the tray-facing helpers
# ---------------------------------------------------------------------------


class _Cfg:
    def __init__(self, update_check, update_auto_install=True):
        self.update_check = update_check
        self.update_auto_install = update_auto_install


class TestTrayHelpers(unittest.TestCase):
    def test_disabled_config_means_no_updater(self):
        self.assertIsNone(U.start_if_enabled(_Cfg(False)))

    def test_none_updater_is_safe_everywhere(self):
        self.assertFalse(U.menu_visible(None))
        self.assertEqual(U.menu_text(None), "Install update")
        U.install(None)  # must not raise

    def test_menu_reflects_availability(self):
        up = U.Updater(current_version="0.4.0")
        self.assertFalse(U.menu_visible(up))
        info = U.evaluate_release(release("v0.5.0"), "0.4.0")
        up._info = info  # noqa: SLF001 -- direct injection, no network in tests
        self.assertTrue(U.menu_visible(up))
        self.assertEqual(U.menu_text(up), "Install update 0.5.0")

    def test_notification_fires_once_per_version(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cache = os.path.join(tmp.name, "c.json")
        # Seed the cache, then run check_once() offline twice: same version,
        # one notification.
        http = FakeHttp(ok(release("v0.5.0")))
        U.check_now("0.4.0", cache, http_get=http)
        seen = []
        up = U.Updater(current_version="0.4.0", cache_file=cache,
                       notify=lambda title, text: seen.append((title, text)))
        up.check_once()
        up.check_once()
        self.assertEqual(len(seen), 1)
        self.assertIn("0.5.0", seen[0][1])
        self.assertEqual(up.available.version, "0.5.0")

    def test_notify_that_raises_does_not_kill_the_check(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cache = os.path.join(tmp.name, "c.json")
        U.check_now("0.4.0", cache, http_get=FakeHttp(ok(release("v0.5.0"))))

        def bad_notify(title, text):
            raise RuntimeError("balloon burst")

        up = U.Updater(current_version="0.4.0", cache_file=cache,
                       notify=bad_notify)
        self.assertEqual(up.check_once().version, "0.5.0")


# ---------------------------------------------------------------------------
# checksums and staging
# ---------------------------------------------------------------------------


class TestSha256Sums(unittest.TestCase):
    def test_standard_and_binary_marker_lines(self):
        text = ("a" * 64 + "  ds5bridge-0.5.0-win-x64.zip\n"
                + "b" * 64 + " *install.ps1\n"
                + "\n"
                + "not a sum line\n"
                + "C" * 64 + "  UPPER.zip\n")
        sums = U.parse_sha256sums(text)
        self.assertEqual(sums["ds5bridge-0.5.0-win-x64.zip"], "a" * 64)
        self.assertEqual(sums["install.ps1"], "b" * 64)
        self.assertEqual(sums["UPPER.zip"], "c" * 64)  # hash lowercased
        self.assertEqual(len(sums), 3)

    def test_empty_and_none(self):
        self.assertEqual(U.parse_sha256sums(""), {})
        self.assertEqual(U.parse_sha256sums(None), {})

    def test_file_sha256_matches_hashlib(self):
        import hashlib

        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "x.bin")
            with open(p, "wb") as f:
                f.write(b"ds5bridge" * 1000)
            self.assertEqual(U.file_sha256(p),
                             hashlib.sha256(b"ds5bridge" * 1000).hexdigest())


class TestInstallRoot(unittest.TestCase):
    def test_source_checkout_has_no_install_root(self):
        # These tests run from source, so this is the honest direct check.
        self.assertIsNone(U.install_root())


class TestHelperScript(unittest.TestCase):
    def test_write_helper_creates_the_script_fresh(self):
        with tempfile.TemporaryDirectory() as root:
            # A stale helper from "a previous version" must be replaced.
            os.makedirs(os.path.join(root, "updates"))
            stale = os.path.join(root, "updates", "apply-update.ps1")
            with open(stale, "w", encoding="utf-8") as f:
                f.write("# old junk")
            path = U.write_helper(root)
            self.assertEqual(path, stale)
            with open(path, encoding="utf-8") as f:
                text = f.read()
            self.assertIn("Wait-Process", text)
            self.assertIn("Start-Process", text)
            self.assertIn("param(", text)
            self.assertNotIn("old junk", text)

    def test_helper_takes_everything_as_parameters(self):
        # Nothing environment-specific may be baked into the file itself:
        # the same bytes must be correct on every machine.
        with tempfile.TemporaryDirectory() as root:
            path = U.write_helper(root)
            with open(path, encoding="utf-8") as f:
                text = f.read()
            self.assertNotIn(root.replace("\\", "\\\\"), text)
            self.assertNotIn(os.environ.get("USERNAME", "\x00nope"), text)


class TestVerifyDownload(unittest.TestCase):
    """The decision that lets a downloaded installer run -- with no network:
    the SHA256SUMS text is an input, the file is a tempfile."""

    NAME = "ds5bridge-setup-0.5.0-bundled.exe"

    def _info(self, size=0, sums=True):
        return U.UpdateInfo(version="0.5.0", tag="v0.5.0", asset_name=self.NAME,
                            asset_url="u", asset_size=size,
                            sums_url="s" if sums else None, page_url="p")

    def _file(self, root, data=b"MZ" + b"x" * 100):
        path = os.path.join(root, self.NAME)
        with open(path, "wb") as f:
            f.write(data)
        return path

    def test_matching_hash_passes_and_keeps_the_file(self):
        with tempfile.TemporaryDirectory() as root:
            path = self._file(root)
            sums = f"{U.file_sha256(path)}  {self.NAME}\n"
            U.verify_download(path, self._info(), sums)
            self.assertTrue(os.path.exists(path))

    def test_wrong_hash_deletes_the_file(self):
        with tempfile.TemporaryDirectory() as root:
            path = self._file(root)
            sums = f"{'0' * 64}  {self.NAME}\n"
            with self.assertRaises(U.UpdateError) as cm:
                U.verify_download(path, self._info(), sums)
            self.assertIn("checksum", str(cm.exception))
            self.assertFalse(os.path.exists(path))

    def test_sums_without_our_line_is_a_refusal_not_a_pass(self):
        # A SHA256SUMS that lists everything except the installer is a broken
        # release; it must not degrade to "no hash, so fine".
        with tempfile.TemporaryDirectory() as root:
            path = self._file(root)
            sums = f"{'a' * 64}  ds5bridge-setup-0.5.0.exe\n"
            with self.assertRaises(U.UpdateError):
                U.verify_download(path, self._info(), sums)
            self.assertFalse(os.path.exists(path))

    def test_unreachable_sums_is_a_refusal_too(self):
        # The release HAS a SHA256SUMS (sums_url set) but it could not be
        # fetched: download_and_verify passes "" -- and "" names no asset.
        with tempfile.TemporaryDirectory() as root:
            path = self._file(root)
            with self.assertRaises(U.UpdateError):
                U.verify_download(path, self._info(), "")

    def test_no_sums_at_all_falls_back_to_the_size(self):
        with tempfile.TemporaryDirectory() as root:
            path = self._file(root)
            U.verify_download(path, self._info(size=102, sums=False), None)
            self.assertTrue(os.path.exists(path))
            with self.assertRaises(U.UpdateError) as cm:
                U.verify_download(path, self._info(size=999, sums=False), None)
            self.assertIn("truncated", str(cm.exception))
            self.assertFalse(os.path.exists(path))

    def test_download_and_verify_uses_the_fetched_sums(self):
        # End to end through download_and_verify with the two network calls
        # replaced: the file lands in <root>\updates\ and is checked against
        # the SHA256SUMS the fake served.
        with tempfile.TemporaryDirectory() as root:
            payload = b"installer bytes"
            import hashlib
            digest = hashlib.sha256(payload).hexdigest()
            saved = (U._download, U._http_get)

            def fake_download(url, dest, max_bytes=0):
                with open(dest, "wb") as f:
                    f.write(payload)
            U._download = fake_download
            U._http_get = lambda url, headers, timeout=None: (
                200, {}, f"{digest}  {self.NAME}\n".encode())
            try:
                path = U.download_and_verify(self._info(), root)
            finally:
                U._download, U._http_get = saved
            self.assertEqual(path, os.path.join(root, "updates", self.NAME))
            self.assertTrue(os.path.exists(path))


class TestInstallerHandOff(unittest.TestCase):
    def test_switches_are_the_documented_ones(self):
        switches, log_path = U.installer_args(r"C:\u\ds5bridge", "0.5.0")
        for s in ("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
                  "/COMPONENTS=app", "/MERGETASKS=!restorepoint", "/STARTTRAY=1"):
            self.assertIn(s, switches)
        # Never a driver, never a restore point, never a reboot.
        self.assertFalse(any("usbip" in s or "hidhide" in s for s in switches))
        self.assertEqual(log_path, os.path.join(r"C:\u\ds5bridge", "updates",
                                                "install-0.5.0.log"))
        # No quotes inside any switch: they would not survive the
        # Python -> powershell.exe -> Start-Process hand-off intact.
        self.assertFalse(any('"' in s for s in switches))

    def test_helper_runs_the_installer_and_falls_back_to_the_old_tray(self):
        with tempfile.TemporaryDirectory() as root:
            path = U.write_helper(root)
            with open(path, encoding="utf-8") as f:
                text = f.read()
            self.assertIn("Wait-Process", text)
            self.assertIn("Start-Process -FilePath $Setup", text)
            self.assertIn("/LOG=", text)
            # A refused install relaunches what is still there.
            self.assertIn("relaunching the current version", text)
            self.assertIn("ds5bridge-tray.exe", text)
            # And nothing of the old zip swap remains.
            self.assertNotIn("Move-Item", text)
            # Under the service: stop it first, start it again on a refusal.
            self.assertIn("[string]$Service", text)
            self.assertIn("sc.exe stop $Service", text)
            self.assertIn("sc.exe start $Service", text)

    def test_helper_command_names_the_service_only_when_there_is_one(self):
        root = r"C:\u\ds5bridge"
        plain = U.helper_command("h.ps1", root, "s.exe", "1.0.0", 42)
        self.assertNotIn("-Service", plain)
        self.assertEqual(plain[plain.index("-WaitPid") + 1], "42")
        svc = U.helper_command("h.ps1", root, "s.exe", "1.0.0", 42, service="ds5bridge")
        self.assertEqual(svc[svc.index("-Service") + 1], "ds5bridge")
        self.assertEqual(svc[:-2], plain)

    def test_service_name_comes_from_the_child_flag(self):
        argv = sys.argv
        try:
            sys.argv = ["ds5bridge-tray.exe", "tray", "--service-child"]
            self.assertEqual(U.service_name_if_child(), "ds5bridge")
            sys.argv = ["ds5bridge-tray.exe", "tray"]
            self.assertIsNone(U.service_name_if_child())
        finally:
            sys.argv = argv


class TestAutoInstall(unittest.TestCase):
    """`update_auto_install`: the decision, the act, and once per version."""

    def info(self):
        return U.evaluate_release(release("v0.5.0"), "0.4.0")

    def test_off_by_config(self):
        up = U.Updater(current_version="0.4.0", auto_install=False,
                       install_fn=lambda: None)
        self.assertFalse(up.should_auto_install(self.info()))

    def test_nothing_to_install_or_nothing_to_call(self):
        up = U.Updater(current_version="0.4.0", auto_install=True,
                       install_fn=lambda: None)
        self.assertFalse(up.should_auto_install(None))
        self.assertFalse(U.Updater(current_version="0.4.0", auto_install=True)
                         .should_auto_install(self.info()))

    def test_installs_once_per_version(self):
        calls = []
        up = U.Updater(current_version="0.4.0", auto_install=True,
                       install_fn=lambda: calls.append(1))
        info = self.info()
        self.assertTrue(up.should_auto_install(info))
        self.assertTrue(up.run_auto_install(info))
        self.assertEqual(calls, [1])
        # The daily check finds the same version again: no second attempt.
        self.assertFalse(up.should_auto_install(info))
        self.assertFalse(up.run_auto_install(info))
        self.assertEqual(calls, [1])

    def test_an_install_already_under_way_is_not_started_twice(self):
        up = U.Updater(current_version="0.4.0", auto_install=True,
                       install_fn=lambda: None)
        up.installing = True
        self.assertFalse(up.should_auto_install(self.info()))

    def test_a_failing_install_is_reported_and_not_retried(self):
        seen = []

        def boom():
            raise RuntimeError("no disk")
        up = U.Updater(current_version="0.4.0", auto_install=True, install_fn=boom,
                       notify=lambda t, b: seen.append((t, b)))
        self.assertFalse(up.run_auto_install(self.info()))
        self.assertEqual(seen[0][0], "Update failed")
        self.assertFalse(up.should_auto_install(self.info()))

    def test_start_if_enabled_wires_the_config_and_the_callable(self):
        up = U.start_if_enabled(_Cfg(True, True), install_fn=lambda: None)
        try:
            self.assertTrue(up.auto_install)
        finally:
            up.stop()
        up = U.start_if_enabled(_Cfg(True, False), install_fn=lambda: None)
        try:
            self.assertFalse(up.auto_install)
        finally:
            up.stop()
        # No callable: nothing automatic, whatever the config says.
        up = U.start_if_enabled(_Cfg(True, True))
        try:
            self.assertFalse(up.auto_install)
        finally:
            up.stop()
        self.assertEqual(up.auto_delay_s, U.AUTO_INSTALL_DELAY_S)


if __name__ == "__main__":
    unittest.main()
