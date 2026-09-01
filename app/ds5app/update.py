"""Update check and self-update -- stdlib only, and a running exe cannot
overwrite itself.

Those two constraints shaped everything in this file, so they go first:

**Stdlib only, deliberately.** The check runs in the tray at startup, on a
machine where the whole point is that nothing extra is installed. `urllib` plus
`json` plus `hashlib` cover a conditional GET, a release document and a SHA-256
-- adding `requests` for that would put a hard dependency on every install for
the least important feature in the program. CI runs these tests with nothing
installed, which is what keeps this property from eroding.

**A running one-dir build holds its own files open.** `ds5bridge-tray.exe` and
every DLL under `app\\_internal` are locked by the Windows loader for as long as
the process lives, so "download the new version and overwrite the old one" is
impossible from inside the program being overwritten. The classic answer is the
one used here: stage the new version NEXT TO the install, hand a tiny helper
script the job of swapping directories, and exit. The helper waits for this
process to die (the lock dies with it), renames `app` aside, moves the staged
version in, relaunches the tray, and deletes the old directory. Renaming a
directory is atomic on NTFS within a volume, and both directories live under
the same `%LOCALAPPDATA%\\ds5bridge`, so the swap either happens or it does not
-- there is no state where half the files are new.

What a check actually is
------------------------
    GET https://api.github.com/repos/<repo>/releases/latest
        If-None-Match: <the ETag from last time>

Unauthenticated, so it counts against GitHub's 60-requests-per-hour-per-IP
budget -- which is why the ETag matters beyond politeness: a 304 Not Modified
does not count against that budget at all, and "nothing changed" is the answer
99% of days. The ETag and the last full answer live in
`%APPDATA%\\ds5bridge\\update-cache.json`, next to the config. Nothing about
the machine or the user is sent; there is no telemetry channel here, and
adding one would be a different feature requiring a different conversation.

`releases/latest` rather than `releases` is also a decision: GitHub excludes
drafts and prereleases from that endpoint on its own, so a nightly published
as a prerelease can never be offered to users, even before this module's own
prerelease guard.

The decision rule
-----------------
Offer an update when the latest release parses as a version, is strictly newer
than `ds5app.__version__` by numeric semver comparison, is not a prerelease,
and actually carries a `ds5bridge-*-win-x64.zip` asset. Every "cannot tell"
answers "no update": an unparseable tag, a missing asset or a malformed JSON
document must degrade to silence, never to a notification that leads nowhere
-- the tray has no good place to show "the update check is confused".

The tray touchpoint (for the parallel tray rework: this is the whole contract)
------------------------------------------------------------------------------
The tray wires this module in four one-liners, all null-safe, so the merge
with a reworked tray.py stays trivial:

    self.updater = U.start_if_enabled(self.cfg, notify=self._notify)   # __init__
    U.menu_visible(self.updater)     # `visible=` of one menu item
    U.menu_text(self.updater)        # `text=`    of the same item
    U.install(self.updater, notify=self._notify, quit_cb=...)  # its action

plus one entry in the menu fingerprint (`_menu_key`) so the item appears when
the background check lands. `start_if_enabled` returns None when
`config.update_check` is off, and every helper accepts None -- a tray built
without an updater renders exactly as before. Nothing else in this module
touches, imports or assumes anything about the tray.

"Update now", start to finish
-----------------------------
    1. download  ds5bridge-<ver>-win-x64.zip   -> %LOCALAPPDATA%\\ds5bridge\\updates\\
    2. verify    SHA-256 against the release's SHA256SUMS asset
    3. unpack    -> updates\\stage-<ver>\\ds5bridge\\
    4. write     updates\\apply-update.ps1, start it DETACHED and hidden
    5. exit the tray  (the helper is waiting on our PID)
    6. helper:   app -> app.old, stage -> app, relaunch tray, delete app.old

Step 2 is not optional when SHA256SUMS exists: a zip that does not match its
published hash is deleted and the update is abandoned with a notification.
When the release has no SHA256SUMS at all (a release made before the pipeline
grew one) the download proceeds on a size check alone, and says so in the log.

A source checkout has no `app` directory to swap, so `install_root()` answers
None and "update now" degrades to opening the release page in the browser --
the person running from source updates with `git pull` anyway.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

log = logging.getLogger("ds5app.update")


def app_version() -> str:
    """`ds5app.__version__` -- the single source of truth -- without making the
    package import a hard requirement of THIS module.

    `ds5app/__init__.py` pulls in the service, which pulls in hidapi. This
    module has to stay importable on a bare interpreter (the stdlib-only CI
    job loads it directly, the way it loads config.py), so when the package
    cannot import, the version is read out of `__init__.py` as text. Same
    number, same file, no second copy.
    """
    try:
        from ds5app import __version__

        return __version__
    except Exception:  # noqa: BLE001 -- no hidapi on this interpreter
        init = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "__init__.py")
        try:
            with open(init, encoding="utf-8") as f:
                m = re.search(r'__version__\s*=\s*"([^"]+)"', f.read())
            return m.group(1) if m else "0"
        except OSError:
            return "0"

#: Where releases live. The env override exists for testing against a fork
#: without editing source, mirroring DS5_CONFIG.
REPO = os.environ.get("DS5_UPDATE_REPO", "Macle57/ds5-virtual-cable")

#: The one-dir zip the release pipeline publishes, e.g.
#: "ds5bridge-0.5.0-win-x64.zip". Anchored on both ends: a future
#: "ds5bridge-debug-symbols-...zip" must not match.
ASSET_RE = re.compile(r"^ds5bridge-[0-9][^/\\]*-win-x64\.zip$")
SUMS_NAME = "SHA256SUMS"

CHECK_INTERVAL_S = 24 * 3600.0     # "at startup + daily"
STARTUP_DELAY_S = 20.0             # let the tray finish coming up first
HTTP_TIMEOUT_S = 15.0
CACHE_NAME = "update-cache.json"

#: The sanity ceiling for a download. The one-dir build measures ~120 MB
#: zipped; ten times that is not an update, it is a mistake or an attack.
MAX_ZIP_BYTES = 1024 * 1024 * 1024


# ---------------------------------------------------------------------------
# versions -- pure functions, unit-tested with no network anywhere near them
# ---------------------------------------------------------------------------


def parse_version(text: object) -> tuple | None:
    """"v0.5.0" / "0.5.0" / "v.0.5.0" -> (0, 5, 0). None when it is not a version.

    The `v.` form is accepted because a neighbouring project this one depends
    on tags that way (usbip-win2's `v.0.9.7.7`), and the cost of accepting it
    is nil. A pre-release suffix does NOT parse here -- see `is_prerelease`,
    and the decision rule in the module docstring: prereleases are never
    offered, so their base version must not be either.
    """
    if not isinstance(text, str):
        return None
    t = text.strip().lower()
    if t.startswith("v"):
        t = t[1:]
        if t.startswith("."):
            t = t[1:]
    if not t or not re.fullmatch(r"\d+(\.\d+)*", t):
        return None
    return tuple(int(p) for p in t.split("."))


def is_prerelease_tag(text: object) -> bool:
    """Does the tag carry a -suffix ("v0.5.0-rc1")? Cheap and deliberately wide:
    anything after a dash is treated as "not for users"."""
    return isinstance(text, str) and "-" in text.strip().lstrip("vV")


def is_newer(candidate: tuple | None, current: tuple | None) -> bool:
    """Strictly newer, comparing numerically with zero-padding.

    Padding matters: (0, 10) must beat (0, 9, 9), and a lexicographic string
    compare gets exactly that case wrong -- "0.10.0" < "0.9.9" as strings.
    Unparseable on either side is False: "cannot tell" is "no update".
    """
    if not candidate or not current:
        return False
    n = max(len(candidate), len(current))
    a = candidate + (0,) * (n - len(candidate))
    b = current + (0,) * (n - len(current))
    return a > b


# ---------------------------------------------------------------------------
# the release document -> a decision
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UpdateInfo:
    """Everything the tray and the installer flow need about one newer release."""

    version: str                 # "0.5.0", dots only, for display
    tag: str                     # "v0.5.0", for URLs
    asset_name: str              # "ds5bridge-0.5.0-win-x64.zip"
    asset_url: str               # browser_download_url of the zip
    asset_size: int              # bytes, per the API; the fallback check
    sums_url: str | None         # SHA256SUMS asset, when the release has one
    page_url: str                # the human release page


def evaluate_release(data: object,
                     current_version: str | None = None) -> UpdateInfo | None:
    """The whole decision, from one release JSON document. Never raises.

    Fed either straight from the API or from the ETag cache, so it must treat
    the input as untrusted in shape: a dict where a list was expected, a
    missing key, an asset without a size -- each of those is an old cache, an
    API change or a hand-broken file, and every one of them answers None.
    """
    if current_version is None:
        current_version = app_version()
    if not isinstance(data, dict):
        return None
    tag = data.get("tag_name")
    latest = parse_version(tag)
    if latest is None:
        log.debug("latest release tag %r is not a version -- ignoring", tag)
        return None
    # releases/latest already excludes both, but the cache can hold anything
    # and being wrong here means offering users a draft.
    if data.get("draft") or data.get("prerelease") or is_prerelease_tag(tag):
        return None
    if not is_newer(latest, parse_version(current_version)):
        return None

    assets = data.get("assets")
    if not isinstance(assets, list):
        return None
    zip_asset = None
    sums_url = None
    for a in assets:
        if not isinstance(a, dict):
            continue
        name = a.get("name")
        if not isinstance(name, str):
            continue
        if ASSET_RE.fullmatch(name) and zip_asset is None:
            zip_asset = a
        elif name == SUMS_NAME:
            u = a.get("browser_download_url")
            sums_url = u if isinstance(u, str) else None
    if zip_asset is None:
        # A release without the zip is a source-only or botched release;
        # notifying about it would send the user somewhere with nothing to
        # download.
        log.info("release %s has no %s asset -- not offering it",
                 tag, "ds5bridge-*-win-x64.zip")
        return None
    url = zip_asset.get("browser_download_url")
    if not isinstance(url, str) or not url:
        return None
    size = zip_asset.get("size")
    page = data.get("html_url")
    return UpdateInfo(
        version=".".join(str(n) for n in latest),
        tag=str(tag),
        asset_name=str(zip_asset["name"]),
        asset_url=url,
        asset_size=size if isinstance(size, int) and size > 0 else 0,
        sums_url=sums_url,
        page_url=page if isinstance(page, str) and page
        else f"https://github.com/{REPO}/releases",
    )


# ---------------------------------------------------------------------------
# the network -- one conditional GET, one cache file
# ---------------------------------------------------------------------------


def cache_path() -> str:
    """Next to config.json, for the same reasons config.json lives there.

    Asks config.py when it is importable so there is exactly one definition of
    "the settings directory"; falls back to the same three-step rule when the
    package is not (the stdlib-only rig loading this file directly).
    """
    try:
        from ds5app import config as K

        return os.path.join(K.config_dir(), CACHE_NAME)
    except Exception:  # noqa: BLE001
        override = os.environ.get("DS5_CONFIG")
        if override:
            base = override.strip().strip('"')
            d = base if not base.lower().endswith(".json") \
                else os.path.dirname(base)
            return os.path.join(os.path.abspath(os.path.expanduser(d)),
                                CACHE_NAME)
        appdata = os.environ.get("APPDATA")
        base = os.path.join(appdata, "ds5bridge") if appdata \
            else os.path.join(os.path.expanduser("~"), ".ds5bridge")
        return os.path.join(base, CACHE_NAME)


def _http_get(url: str, headers: dict, timeout: float = HTTP_TIMEOUT_S):
    """(status, headers, body). An HTTPError is an ANSWER here, not an error:
    304 is the good case, and 403/404 are decisions to make, not exceptions to
    propagate. Network-level failures still raise."""
    req = urllib.request.Request(url, headers=dict(headers), method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        body = b""
        try:
            body = e.read()
        except Exception:  # noqa: BLE001
            pass
        return e.code, dict(e.headers or {}), body


def _load_cache(path: str) -> dict:
    """Tolerant the way config.load is tolerant, for the same reason: a corrupt
    cache must cost one full GET, not the tray."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_cache(path: str, data: dict) -> None:
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
    except OSError as e:  # a cache that cannot be written is just a slower cache
        log.debug("cannot write %s: %s", path, e)


def _trim_release(data: dict) -> dict:
    """Only what `evaluate_release` reads. The API document is ~30 KB of body
    text and uploader avatars; the cache wants the 1 KB that matters."""
    assets = []
    for a in data.get("assets") or []:
        if isinstance(a, dict):
            assets.append({k: a.get(k)
                           for k in ("name", "browser_download_url", "size")})
    return {k: data.get(k)
            for k in ("tag_name", "html_url", "draft", "prerelease")} | {
        "assets": assets}


def check_now(current_version: str | None = None,
              cache_file: str | None = None,
              repo: str | None = None,
              http_get=_http_get) -> UpdateInfo | None:
    """One check: conditional GET, cache maintenance, decision. Never raises.

    `http_get` is injectable so the tests exercise the 200/304/failure paths
    with fixtures instead of a network -- same pattern as the fakes in
    test_manager.py.
    """
    if current_version is None:
        current_version = app_version()
    path = cache_file or cache_path()
    cache = _load_cache(path)
    headers = {
        # GitHub rejects requests without a User-Agent outright.
        "User-Agent": f"ds5bridge/{current_version} (update check)",
        "Accept": "application/vnd.github+json",
    }
    etag = cache.get("etag")
    if isinstance(etag, str) and etag:
        headers["If-None-Match"] = etag

    url = f"https://api.github.com/repos/{repo or REPO}/releases/latest"
    try:
        status, resp_headers, body = http_get(url, headers)
    except Exception as e:  # noqa: BLE001 -- offline is Tuesday, not an error
        log.debug("update check failed (%s) -- using the cached answer", e)
        return evaluate_release(cache.get("release"), current_version)

    if status == 304:
        # The budget-free path, and the usual one.
        cache["checked_at"] = time.time()
        _save_cache(path, cache)
        return evaluate_release(cache.get("release"), current_version)
    if status != 200:
        # 403 is the rate limit, 404 is a repo with no releases yet. Both are
        # "no news", and both keep whatever the cache already knew.
        log.debug("update check: HTTP %s from %s", status, url)
        return evaluate_release(cache.get("release"), current_version)

    try:
        data = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        log.debug("update check: unparseable response (%s)", e)
        return evaluate_release(cache.get("release"), current_version)
    if not isinstance(data, dict):
        return evaluate_release(cache.get("release"), current_version)

    new_etag = None
    for k, v in resp_headers.items():
        if str(k).lower() == "etag":
            new_etag = v
            break
    _save_cache(path, {"etag": new_etag, "checked_at": time.time(),
                       "release": _trim_release(data)})
    return evaluate_release(data, current_version)


# ---------------------------------------------------------------------------
# the background checker the tray owns
# ---------------------------------------------------------------------------


class Updater:
    """Startup + daily checks on a daemon thread; the newest answer, held.

    `available` is read from menu lambdas on the tray's threads and written
    from this one, so it sits behind a lock. The notification fires once per
    version per process: a check every day of a two-week tray session must not
    nag daily, and a restart renotifying is the correct reminder.
    """

    def __init__(self, current_version: str | None = None,
                 cache_file: str | None = None,
                 notify=None):
        self._current = current_version or app_version()
        self._cache_file = cache_file
        self._notify = notify
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._info: UpdateInfo | None = None
        self._told: set[str] = set()
        self._thread: threading.Thread | None = None

    @property
    def available(self) -> UpdateInfo | None:
        with self._lock:
            return self._info

    def start(self) -> "Updater":
        self._thread = threading.Thread(target=self._loop,
                                        name="update-check", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def check_once(self) -> UpdateInfo | None:
        """One synchronous check. The loop's body, callable from tests."""
        info = check_now(self._current, self._cache_file)
        with self._lock:
            self._info = info
            fresh = info is not None and info.version not in self._told
            if fresh:
                self._told.add(info.version)
        if fresh and self._notify is not None:
            try:
                self._notify("Update available",
                             f"ds5bridge {info.version} is out (you have "
                             f"{self._current}). Right-click the tray icon "
                             f"to install it.")
            except Exception:  # noqa: BLE001 -- a notification is never worth a thread
                log.debug("update notification failed", exc_info=True)
        return info

    def _loop(self) -> None:
        if self._stop.wait(STARTUP_DELAY_S):
            return
        while True:
            try:
                self.check_once()
            except Exception:  # noqa: BLE001 -- the loop must survive anything
                log.exception("update check failed")
            if self._stop.wait(CHECK_INTERVAL_S):
                return


# -- the four tray-facing helpers, all None-safe (see the module docstring) --


def start_if_enabled(cfg, notify=None) -> Updater | None:
    """The tray's one constructor call. None when the config says no."""
    if not getattr(cfg, "update_check", True):
        log.info("update checks are off (update_check: false)")
        return None
    return Updater(notify=notify).start()


def menu_visible(updater: Updater | None) -> bool:
    return updater is not None and updater.available is not None


def menu_text(updater: Updater | None) -> str:
    info = updater.available if updater is not None else None
    return f"Install update {info.version}" if info else "Install update"


def install(updater: Updater | None, notify=None, quit_cb=None) -> None:
    """The menu item's action: stage the update, then hand off and quit.

    Runs the download on its own daemon thread -- this is called from a menu
    handler, and a 120 MB download on pystray's message loop would freeze the
    icon for minutes. Every failure ends in a notification, never an exception:
    there is no console to print to.
    """
    info = updater.available if updater is not None else None
    if info is None:
        return

    root = install_root()
    if root is None:
        # Source checkout, or an install layout this module does not own.
        # Opening the page is honest: the person who set that up updates by
        # hand, and pretending otherwise would swap directories under a git
        # checkout.
        _say(notify, "Update available",
             f"ds5bridge {info.version}: this copy is not the packaged "
             f"install, so grab it from the release page (opening it now).")
        try:
            import webbrowser

            webbrowser.open(info.page_url)
        except Exception:  # noqa: BLE001
            pass
        return

    def work():
        try:
            _say(notify, "Updating",
                 f"Downloading ds5bridge {info.version} ...")
            staged = download_and_stage(info, root)
            spawn_swap_helper(root, staged)
            _say(notify, "Updating",
                 f"Installing {info.version}; the tray will restart itself.")
        except UpdateError as e:
            _say(notify, "Update failed", str(e))
            return
        except Exception:  # noqa: BLE001
            log.exception("update failed")
            _say(notify, "Update failed",
                 "Something unexpected went wrong; nothing was changed. "
                 "The current version keeps running.")
            return
        # Only after the helper is actually running: it waits on our PID, so
        # from here the fastest exit is the best one.
        if quit_cb is not None:
            try:
                quit_cb()
            except Exception:  # noqa: BLE001
                os._exit(0)  # noqa: SLF001 -- the helper needs this PID gone

    threading.Thread(target=work, name="update-install", daemon=True).start()


def _say(notify, title: str, text: str) -> None:
    log.info("%s: %s", title, text)
    if notify is not None:
        try:
            notify(title, text)
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# applying an update: stage, verify, swap
# ---------------------------------------------------------------------------


class UpdateError(RuntimeError):
    """Applying the update failed. The message is user-facing text."""


def install_root() -> str | None:
    """%LOCALAPPDATA%\\ds5bridge when this process is the packaged install.

    The layout contract with install.ps1: the exe lives in `<root>\\app\\`, and
    the updater owns `<root>\\updates\\`. Anything else -- source checkout,
    a zip unpacked on the Desktop -- answers None, and the update flow opens
    the release page instead of guessing at a directory swap it might lose.
    """
    if not getattr(sys, "frozen", False):
        return None
    exe_dir = os.path.dirname(os.path.abspath(sys.executable))
    if os.path.basename(exe_dir).lower() != "app":
        return None
    return os.path.dirname(exe_dir)


def parse_sha256sums(text: str) -> dict:
    """`sha256sum` format: "<64 hex>  <name>". Lowercased hashes, keyed by name.

    The optional "*" binary marker in front of the name is stripped -- both
    forms are in the wild and the pipeline's own file should not be the only
    one this can read. Unrecognisable lines are skipped, not fatal: one broken
    line must not invalidate the hashes that are fine.
    """
    out: dict[str, str] = {}
    for line in (text or "").splitlines():
        m = re.fullmatch(r"\s*([0-9a-fA-F]{64})\s+\*?(\S.*?)\s*", line)
        if m:
            out[m.group(2)] = m.group(1).lower()
    return out


def file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _download(url: str, dest: str, max_bytes: int = MAX_ZIP_BYTES) -> None:
    req = urllib.request.Request(url, headers={
        "User-Agent": f"ds5bridge/{app_version()} (update download)"})
    got = 0
    with urllib.request.urlopen(req, timeout=60) as resp, \
            open(dest, "wb") as f:  # noqa: S310
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            got += len(chunk)
            if got > max_bytes:
                raise UpdateError("the download is implausibly large -- "
                                  "stopping rather than filling the disk.")
            f.write(chunk)


def download_and_stage(info: UpdateInfo, root: str) -> str:
    """Download, verify, unpack. Returns the staged one-dir folder.

    The verification order is deliberate: hash when the release publishes one,
    size as the fallback, and a mismatch DELETES the download -- a zip that
    failed its hash must not sit on disk looking like a valid update for a
    later run to find.
    """
    import zipfile

    updates = os.path.join(root, "updates")
    os.makedirs(updates, exist_ok=True)
    zip_path = os.path.join(updates, info.asset_name)

    _download(info.asset_url, zip_path)

    if info.sums_url:
        try:
            status, _h, body = _http_get(info.sums_url, {
                "User-Agent": f"ds5bridge/{app_version()} (update download)"})
            sums = parse_sha256sums(body.decode("utf-8", "replace")) \
                if status == 200 else {}
        except Exception:  # noqa: BLE001
            sums = {}
        expected = sums.get(info.asset_name)
        if not expected:
            _discard(zip_path)
            raise UpdateError(
                "the release publishes checksums but not one for "
                f"{info.asset_name} -- refusing the download.")
        actual = file_sha256(zip_path)
        if actual != expected:
            _discard(zip_path)
            raise UpdateError(
                "the downloaded update failed its checksum -- discarded it. "
                "Try again later; if this repeats, something between you and "
                "GitHub is rewriting downloads.")
    elif info.asset_size and os.path.getsize(zip_path) != info.asset_size:
        _discard(zip_path)
        raise UpdateError("the download was truncated -- discarded it. "
                          "Try again.")
    else:
        log.warning("release %s has no %s -- installed on a size check only",
                    info.tag, SUMS_NAME)

    stage = os.path.join(updates, f"stage-{info.version}")
    if os.path.isdir(stage):
        import shutil

        shutil.rmtree(stage, ignore_errors=True)
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(stage)
    _discard(zip_path)  # verified and unpacked; the zip has done its job

    # The zip carries a top-level ds5bridge/ folder (the pipeline zips the
    # dist folder itself). Find the directory that holds the tray exe rather
    # than hardcoding one layout deep.
    for cand in (os.path.join(stage, "ds5bridge"), stage):
        if os.path.isfile(os.path.join(cand, "ds5bridge-tray.exe")):
            return cand
    raise UpdateError("the update zip does not look like a ds5bridge build "
                      "(no ds5bridge-tray.exe inside) -- not installing it.")


def _discard(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


#: The swap helper. PowerShell 5.1-compatible on purpose (powershell.exe is on
#: every supported Windows; pwsh is not). Parameterised entirely by arguments
#: so the script itself is inert -- nothing is baked in at write time except
#: what `spawn_swap_helper` passes on the command line.
_HELPER_PS1 = r"""# ds5bridge update helper -- written and launched by ds5app/update.py.
# Waits for the tray to exit (its exe is file-locked until then), swaps the
# staged new version into place, relaunches the tray, cleans up. Every step
# logs to updates\update.log, because when this goes wrong there is no UI left.
param(
    [Parameter(Mandatory)][int]$WaitPid,
    [Parameter(Mandatory)][string]$Root,
    [Parameter(Mandatory)][string]$Staged,
    [string]$Exe = 'ds5bridge-tray.exe'
)
$ErrorActionPreference = 'Stop'
$logFile = Join-Path $Root 'updates\update.log'
function Log([string]$m) {
    try { Add-Content -Path $logFile -Value ("{0}  {1}" -f (Get-Date -Format s), $m) } catch {}
}
try {
    Log "waiting for pid $WaitPid"
    try { Wait-Process -Id $WaitPid -Timeout 120 -ErrorAction Stop } catch {}
    $app = Join-Path $Root 'app'
    $old = Join-Path $Root ('app.old-' + [IO.Path]::GetRandomFileName().Substring(0,8))
    # The rename is the swap's atom, and the retry loop is for the tail of the
    # dying process: the exe can stay locked for a moment after the PID is gone.
    $renamed = $false
    for ($i = 0; $i -lt 15; $i++) {
        try { Move-Item -LiteralPath $app -Destination $old -ErrorAction Stop; $renamed = $true; break }
        catch { Start-Sleep -Seconds 2 }
    }
    if (-not $renamed) { Log 'could not move app aside; leaving everything as it was'; exit 1 }
    try {
        Move-Item -LiteralPath $Staged -Destination $app -ErrorAction Stop
    } catch {
        # Roll back: an install with no app directory is strictly worse than
        # the old version.
        Log "swap failed: $_ -- rolling back"
        Move-Item -LiteralPath $old -Destination $app -ErrorAction SilentlyContinue
        exit 1
    }
    Log 'swap complete'
    try { Start-Process -FilePath (Join-Path $app $Exe) -WorkingDirectory $app } catch { Log "relaunch failed: $_" }
    Start-Sleep -Seconds 2
    Remove-Item -LiteralPath $old -Recurse -Force -ErrorAction SilentlyContinue
    $stageParent = Split-Path -Parent $Staged
    Get-ChildItem -LiteralPath $stageParent -Filter 'stage-*' -Directory -ErrorAction SilentlyContinue |
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
    Log 'done'
} catch {
    Log "helper failed: $_"
    exit 1
}
"""


def write_helper(root: str) -> str:
    """The helper script, (re)written fresh every time it is needed -- an old
    copy from a previous version must never be what runs."""
    updates = os.path.join(root, "updates")
    os.makedirs(updates, exist_ok=True)
    path = os.path.join(updates, "apply-update.ps1")
    with open(path, "w", encoding="utf-8", newline="\r\n") as f:
        f.write(_HELPER_PS1)
    return path


def spawn_swap_helper(root: str, staged: str) -> None:
    """Start the helper detached, then it is the caller's job to exit.

    DETACHED_PROCESS + CREATE_NO_WINDOW so the helper survives this process
    (it must -- it is waiting for this process to die) and never flashes a
    console at the user. `-ExecutionPolicy Bypass` because the helper is a
    local unsigned script and the machine's policy is whatever it is.
    """
    helper = write_helper(root)
    flags = 0
    if os.name == "nt":
        flags = (subprocess.DETACHED_PROCESS
                 | subprocess.CREATE_NO_WINDOW
                 | subprocess.CREATE_NEW_PROCESS_GROUP)
    subprocess.Popen(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
         "-File", helper,
         "-WaitPid", str(os.getpid()),
         "-Root", root,
         "-Staged", staged],
        creationflags=flags,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True)
    log.info("update helper started; exiting so it can swap %s", root)
