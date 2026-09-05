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
impossible from inside the program being overwritten. The answer used here:
download the release's INSTALLER (`ds5bridge-setup-<ver>-bundled.exe`, the one
asset every release carries), verify it, hand a tiny helper script the job of
running it, and exit. The helper waits for this process to die (the lock dies
with it) and runs the installer silently with only the `app` component -- the
same installer a person would run, so the update goes through the same
stop-app / verify-install steps and leaves the same `last-install-check.txt`
-- and the installer, told `/STARTTRAY=1`, starts the new tray itself. If the
installer refuses (another installer running, a usbip-win2 removal waiting for
its reboot) the helper relaunches the OLD tray, so an update never leaves the
user with nothing. Until 0.4 this module unpacked a zip and swapped the `app`
directory itself; the zip is no longer published, and an update that bypassed
the installer's checks was one more code path to keep honest.

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
and actually carries a `ds5bridge-setup-*-bundled.exe` asset. Every "cannot
tell" answers "no update": an unparseable tag, a missing asset or a malformed
JSON document must degrade to silence, never to a notification that leads
nowhere -- the tray has no good place to show "the update check is confused".

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
    1. download  ds5bridge-setup-<ver>-bundled.exe -> %LOCALAPPDATA%\\ds5bridge\\updates\\
    2. verify    SHA-256 against the release's SHA256SUMS asset
    3. write     updates\\apply-update.ps1, start it DETACHED and hidden
    4. exit the tray  (the helper is waiting on our PID)
    5. helper:   setup.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART
                          /COMPONENTS="app" /MERGETASKS="!restorepoint"
                          /LOG=updates\\install-<ver>.log /STARTTRAY=1
                 -> exit 0: the installer has started the new tray
                 -> anything else: relaunch the old tray, leave the log

Step 2 is not optional when SHA256SUMS exists: a file that does not match its
published hash is deleted and the update is abandoned with a notification.
When the release has no SHA256SUMS at all (a release made before the pipeline
grew one) the download proceeds on a size check alone, and says so in the log.

The tray runs elevated (ds5bridge.spec), so the installer -- which needs
administrator rights for its own reasons -- inherits the token from the helper
and shows no UAC prompt of its own. `/COMPONENTS="app"` is deliberate: an
update must never install a driver the user removed in the meantime, and the
two it would otherwise verify are not what changed. (Inno remembers that
selection as the "previous" one, so the next interactive run of the installer
opens with the driver boxes unticked; they read "already installed" anyway.)

A source checkout has no `app` directory to update, so `install_root()`
answers None and "update now" degrades to opening the release page in the
browser -- the person running from source updates with `git pull` anyway.
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

#: The installer the release pipeline publishes -- THE release asset, e.g.
#: "ds5bridge-setup-0.5.0-bundled.exe" (app/packaging/build-installer.ps1
#: -Bundle; the name is ds5bridge.iss's OutputName). Anchored on both ends: a
#: future "ds5bridge-setup-0.5.0-bundled.exe.sig" must not match, and neither
#: may the download-mode "ds5bridge-setup-0.5.0.exe" if one is ever published
#: again -- the updater wants the one that needs no network at install time.
ASSET_RE = re.compile(r"^ds5bridge-setup-[0-9][^/\\]*-bundled\.exe$")
SUMS_NAME = "SHA256SUMS"

CHECK_INTERVAL_S = 24 * 3600.0     # "at startup + daily"
STARTUP_DELAY_S = 20.0             # let the tray finish coming up first
HTTP_TIMEOUT_S = 15.0
CACHE_NAME = "update-cache.json"

#: The sanity ceiling for a download. The bundled installer measures ~73 MB;
#: ten times that is not an update, it is a mistake or an attack.
MAX_DOWNLOAD_BYTES = 1024 * 1024 * 1024
MAX_ZIP_BYTES = MAX_DOWNLOAD_BYTES  # the pre-0.5.0 name, kept for callers

#: The installer's silent switches for an in-place app update. Every one is
#: load-bearing: VERYSILENT + SUPPRESSMSGBOXES because nobody is watching (a
#: refusal goes to the log and the exit code), NORESTART because an app update
#: never needs one and a silent Inno run WITHOUT it would reboot the machine
#: on its own if anything flagged one, COMPONENTS=app so no driver is ever
#: installed by an update, no restore point for the same reason, and
#: STARTTRAY=1 -- our own parameter, read by ds5bridge.iss -- because Inno
#: skips the [Run] "start now" entry in silent mode and the installer, being
#: elevated, is the right process to start the new elevated tray without a
#: prompt.
#: (Unquoted forms: Inno accepts them without spaces, and a quote-free string
#: survives the Python -> powershell.exe -> Start-Process hand-off intact.
#: The only value that can contain a space, the log path, travels as its own
#: parameter and is quoted by the helper.)
INSTALLER_SWITCHES = ("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
                      "/COMPONENTS=app", "/MERGETASKS=!restorepoint",
                      "/STARTTRAY=1")


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
    asset_name: str              # "ds5bridge-setup-0.5.0-bundled.exe"
    asset_url: str               # browser_download_url of the installer
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
    setup_asset = None
    sums_url = None
    for a in assets:
        if not isinstance(a, dict):
            continue
        name = a.get("name")
        if not isinstance(name, str):
            continue
        if ASSET_RE.fullmatch(name) and setup_asset is None:
            setup_asset = a
        elif name == SUMS_NAME:
            u = a.get("browser_download_url")
            sums_url = u if isinstance(u, str) else None
    if setup_asset is None:
        # A release without the installer is a source-only or botched release;
        # notifying about it would send the user somewhere with nothing to
        # download.
        log.info("release %s has no %s asset -- not offering it",
                 tag, "ds5bridge-setup-*-bundled.exe")
        return None
    url = setup_asset.get("browser_download_url")
    if not isinstance(url, str) or not url:
        return None
    size = setup_asset.get("size")
    page = data.get("html_url")
    return UpdateInfo(
        version=".".join(str(n) for n in latest),
        tag=str(tag),
        asset_name=str(setup_asset["name"]),
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
        # 403 is the rate limit; 404 is a repository that is private (the
        # unauthenticated API answers 404, not 403, for those) or has no
        # release yet. Both are "no news", and both keep whatever the cache
        # already knew.
        log.debug("update check: HTTP %s from %s%s", status, url,
                  " (the repository is private, or has no release yet)"
                  if status == 404 else "")
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
    """The menu item's action: download the installer, then hand off and quit.

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
        # hand, and pretending otherwise would run an installer over a git
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
            setup_exe = download_and_verify(info, root)
            spawn_installer(root, setup_exe, info.version)
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
# applying an update: download, verify, hand the installer the job
# ---------------------------------------------------------------------------


class UpdateError(RuntimeError):
    """Applying the update failed. The message is user-facing text."""


def install_root() -> str | None:
    """%LOCALAPPDATA%\\ds5bridge when this process is the packaged install.

    The layout contract with the installer (ds5bridge.iss: `{app}\\app`): the
    exe lives in `<root>\\app\\`, and the updater owns `<root>\\updates\\`.
    Anything else -- a source checkout, a build folder copied somewhere --
    answers None, and the update flow opens the release page instead of running
    an installer over a layout it does not own.
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


def _download(url: str, dest: str, max_bytes: int = MAX_DOWNLOAD_BYTES) -> None:
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


def verify_download(path: str, info: UpdateInfo, sums_text: str | None) -> None:
    """The decision that says a downloaded installer may be run. Raises
    `UpdateError` (and DELETES the file) when it may not.

    `sums_text` is the release's SHA256SUMS, or None when the release has none
    -- the caller fetched it, so this stays a pure function over inputs and is
    testable without a network. The order is deliberate: hash when the release
    publishes one (and then it MUST name this asset -- a SHA256SUMS that lists
    everything but the installer is a broken release, not a pass), size as the
    fallback, and a mismatch removes the download so a file that failed its
    hash never sits on disk looking like a valid update for a later run.
    """
    if sums_text is not None:
        expected = parse_sha256sums(sums_text).get(info.asset_name)
        if not expected:
            _discard(path)
            raise UpdateError(
                "the release publishes checksums but not one for "
                f"{info.asset_name} -- refusing the download.")
        actual = file_sha256(path)
        if actual != expected:
            _discard(path)
            raise UpdateError(
                "the downloaded update failed its checksum -- discarded it. "
                "Try again later; if this repeats, something between you and "
                "GitHub is rewriting downloads.")
        return
    if info.asset_size and os.path.getsize(path) != info.asset_size:
        _discard(path)
        raise UpdateError("the download was truncated -- discarded it. "
                          "Try again.")
    log.warning("release %s has no %s -- installed on a size check only",
                info.tag, SUMS_NAME)


def download_and_verify(info: UpdateInfo, root: str) -> str:
    """Download the installer into `<root>\\updates\\`, verify it, return its path."""
    updates = os.path.join(root, "updates")
    os.makedirs(updates, exist_ok=True)
    setup_exe = os.path.join(updates, info.asset_name)

    _download(info.asset_url, setup_exe)

    sums_text = None
    if info.sums_url:
        try:
            status, _h, body = _http_get(info.sums_url, {
                "User-Agent": f"ds5bridge/{app_version()} (update download)"})
            sums_text = body.decode("utf-8", "replace") if status == 200 else ""
        except Exception:  # noqa: BLE001
            sums_text = ""      # published but unreachable: still not optional
    verify_download(setup_exe, info, sums_text)
    return setup_exe


def _discard(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def installer_args(root: str, version: str) -> tuple[list[str], str]:
    """(switches, log path) for an in-place update: `INSTALLER_SWITCHES` says
    why each switch, the log lands next to the download. Pure, for the tests."""
    log_path = os.path.join(root, "updates", f"install-{version}.log")
    return list(INSTALLER_SWITCHES), log_path


#: The helper. PowerShell 5.1-compatible on purpose (powershell.exe is on every
#: supported Windows; pwsh is not). Parameterised entirely by arguments so the
#: script itself is inert -- nothing is baked in at write time except what
#: `spawn_installer` passes on the command line.
_HELPER_PS1 = r"""# ds5bridge update helper -- written and launched by ds5app/update.py.
# Waits for the tray to exit (its exe is file-locked until then), runs the
# downloaded installer silently (app component only; it starts the new tray
# itself via /STARTTRAY=1), and if the installer refused, relaunches the OLD
# tray so the user is never left with nothing. Every step logs to
# updates\update.log, because when this goes wrong there is no UI left.
param(
    [Parameter(Mandatory)][int]$WaitPid,
    [Parameter(Mandatory)][string]$Root,
    [Parameter(Mandatory)][string]$Setup,
    [string]$SetupArgs = '',
    [string]$LogPath = '',
    [string]$Exe = 'ds5bridge-tray.exe'
)
$ErrorActionPreference = 'Stop'
$logFile = Join-Path $Root 'updates\update.log'
function Log([string]$m) {
    try { Add-Content -Path $logFile -Value ("{0}  {1}" -f (Get-Date -Format s), $m) } catch {}
}
$app = Join-Path $Root 'app'
try {
    Log "waiting for pid $WaitPid"
    try { Wait-Process -Id $WaitPid -Timeout 120 -ErrorAction Stop } catch {}
    Start-Sleep -Seconds 1
    $argLine = $SetupArgs
    if ($LogPath) { $argLine = "$argLine /LOG=`"$LogPath`"" }
    Log "running $Setup $argLine"
    $p = Start-Process -FilePath $Setup -ArgumentList $argLine -Wait -PassThru
    Log "installer exit $($p.ExitCode)"
    if ($p.ExitCode -ne 0) {
        # The installer said no (another installer running, a usbip-win2
        # removal waiting for its reboot, ...): its log says why. The old
        # version is untouched, so bring it back.
        Log 'installer did not complete; relaunching the current version'
        try { Start-Process -FilePath (Join-Path $app $Exe) -WorkingDirectory $app } catch { Log "relaunch failed: $_" }
        exit 1
    }
    # Exit 0: the installer has started the new tray (/STARTTRAY=1). Tidy up.
    Remove-Item -LiteralPath $Setup -Force -ErrorAction SilentlyContinue
    Log 'done'
} catch {
    Log "helper failed: $_"
    try { Start-Process -FilePath (Join-Path $app $Exe) -WorkingDirectory $app } catch { Log "relaunch failed: $_" }
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


def spawn_installer(root: str, setup_exe: str, version: str) -> None:
    """Start the helper detached, then it is the caller's job to exit.

    DETACHED_PROCESS + CREATE_NO_WINDOW so the helper survives this process
    (it must -- it is waiting for this process to die) and never flashes a
    console at the user. `-ExecutionPolicy Bypass` because the helper is a
    local unsigned script and the machine's policy is whatever it is. The
    helper inherits this process's token, which is the elevated one, so the
    installer it starts shows no UAC prompt.
    """
    helper = write_helper(root)
    switches, log_path = installer_args(root, version)
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
         "-Setup", setup_exe,
         "-SetupArgs", " ".join(switches),
         "-LogPath", log_path],
        creationflags=flags,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True)
    log.info("update helper started; exiting so it can run %s", setup_exe)
