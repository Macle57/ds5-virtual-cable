"""Settings that survive a reboot -- and a config file that can never stop the app.

The moment a program starts at login and remembers per-controller choices, its
config file becomes a startup dependency, and that is the whole trap this module
is written around:

    A config.json that is empty, truncated, hand-edited into invalid JSON, or an
    array where an object was expected must NEVER be the reason the tray icon
    does not appear.

There is no recovery path for a windowed build that dies before it draws
anything: nothing is on screen, and the traceback goes to a console that a
`pythonw`/PyInstaller-windowed process does not have. So `load()` never raises
-- not for bad JSON, not for the wrong type, not for a file it is not allowed to
read. A file it cannot parse is moved aside to `config.json.bad` (kept, not
deleted -- the user's controller labels are in there) and the defaults are used.

Writes are atomic -- a `.tmp` in the same directory, then `os.replace` -- for the
other half of that same failure. A half-written two-kilobyte file is exactly what
a power cut during "save the toggle the user just clicked" produces, and
`os.replace` on Windows is the atomic rename that makes the old file survive it.

Location
--------
    %APPDATA%\\ds5bridge\\config.json          normal Windows case
    ~/.ds5bridge/config.json                  no APPDATA (non-Windows, service
                                              accounts, a stripped environment)
    $DS5_CONFIG                               an explicit override, which is what
                                              makes every one of these paths
                                              testable without touching the
                                              user's real settings

Serials are lowercased on the way in, always. `controller.select()` compares
`c.serial.lower()` against a lowered `--serial`, and a config keyed by
"D42F4BA1485D" that never matches the "d42f4ba1485d" the rest of the program
sees is a settings file that silently does nothing.

Nothing here imports anything outside the standard library, deliberately: this
module has to be importable and usable on a machine where the hardware stack
(hidapi, pystray, Pillow) is missing or broken, because "which controllers are
enabled" is exactly what a diagnostic path needs to read in that state.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field

log = logging.getLogger("ds5app.config")

#: Default TCP port for the USB/IP server. 3240 belongs to usbipd-win -- a
#: completely different product that may well be installed on the same machine
#: -- and binding it would break it. Never allocate it, for any controller.
DEFAULT_PORT_BASE = 3241
USBIPD_WIN_PORT = 3240

CONFIG_NAME = "config.json"
#: A parse failure is quarantined here rather than overwritten. It is the only
#: copy of the labels the user typed, and it is also the only evidence of what
#: went wrong.
BAD_SUFFIX = ".bad"
TMP_SUFFIX = ".tmp"

AUDIO_TARGETS = ("speaker", "headphone")


class ConfigWriteError(RuntimeError):
    """Saving failed. The message is user-facing text, not a stack trace."""


# ---------------------------------------------------------------------------
# where the file lives
# ---------------------------------------------------------------------------


def config_dir() -> str:
    """The directory the config lives in. Never raises, never creates."""
    override = os.environ.get("DS5_CONFIG")
    if override:
        return os.path.dirname(_resolve_override(override)) or "."
    appdata = os.environ.get("APPDATA")
    if appdata:
        return os.path.join(appdata, "ds5bridge")
    return os.path.join(os.path.expanduser("~"), ".ds5bridge")


def config_path() -> str:
    """Full path to config.json, honouring DS5_CONFIG.

    DS5_CONFIG accepts either form on purpose. Pointing it at a directory is
    what tests and a portable install want ("put your settings HERE"); pointing
    it at a file is what somebody debugging one specific config wants. Guessing
    wrong turns an override into a silent no-op, so the rule is explicit: an
    existing directory, or a path with no ".json" suffix, is a directory.
    """
    override = os.environ.get("DS5_CONFIG")
    if override:
        return _resolve_override(override)
    return os.path.join(config_dir(), CONFIG_NAME)


def _resolve_override(value: str) -> str:
    p = os.path.abspath(os.path.expanduser(value.strip().strip('"')))
    if os.path.isdir(p) or not p.lower().endswith(".json"):
        return os.path.join(p, CONFIG_NAME)
    return p


def norm_serial(serial: str | None) -> str:
    """Lowercase, stripped. The one place serials are normalised.

    Separators are NOT stripped: hidapi hands this project a bdaddr with no
    colons ("d42f4ba1485d") and the rest of the codebase compares that string
    verbatim after `.lower()`. Helpfully rewriting "d4:2f:..." here would key
    the config on a serial no controller ever reports.
    """
    return (serial or "").strip().lower()


# ---------------------------------------------------------------------------
# the data
# ---------------------------------------------------------------------------

_CC_KNOWN = ("enabled", "audio_target", "port", "label", "hide_bluetooth")


@dataclass
class ControllerConfig:
    """What the user decided about one specific controller."""

    enabled: bool = True
    audio_target: str = "speaker"          # "speaker" | "headphone"
    port: int | None = None                # None -> allocate from port_base
    label: str = ""                        # a human name, e.g. "living room"
    #: Hide the real Bluetooth pad from everything else while this controller is
    #: bridged, using HidHide. Ships OFF, and that is not timidity: the failure
    #: mode of this feature is an INVISIBLE CONTROLLER, so the first release
    #: must change nothing for anybody who does not go looking for it. Needs
    #: HidHide installed separately; see `hidhide.py` and docs/USER-GUIDE.md.
    hide_bluetooth: bool = False
    #: Keys this version does not know about, carried through untouched so that
    #: a config written by a newer build and opened by an older one does not
    #: quietly lose the newer build's settings.
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: object) -> "ControllerConfig":
        if not isinstance(data, dict):
            # A controller entry that is a string, a number or null is garbage,
            # but it is garbage about ONE controller: default that entry rather
            # than throwing the whole file away.
            log.warning("controller entry is %s, not an object -- using defaults",
                        type(data).__name__)
            return cls()
        cc = cls(
            enabled=_as_bool(data.get("enabled"), True),
            audio_target=_as_choice(data.get("audio_target"), AUDIO_TARGETS,
                                    "speaker"),
            port=_as_port(data.get("port")),
            label=_as_str(data.get("label"), ""),
            hide_bluetooth=_as_bool(data.get("hide_bluetooth"), False),
        )
        cc.extra = {k: v for k, v in data.items() if k not in _CC_KNOWN}
        return cc

    def to_dict(self) -> dict:
        out = dict(self.extra)
        out.update(enabled=bool(self.enabled), audio_target=self.audio_target,
                   port=self.port, label=self.label,
                   hide_bluetooth=bool(self.hide_bluetooth))
        return out


_CFG_KNOWN = ("enabled", "autostart_on_login", "auto_bridge_new", "port_base",
              "controllers", "hide_bluetooth_default", "hidhide_cli")


@dataclass
class Config:
    #: The master switch. False means bridge NOTHING: every controller is left
    #: to the native Bluetooth driver, which is the only way a user can play a
    #: game that prefers the wireless DualSense without uninstalling this.
    enabled: bool = True
    autostart_on_login: bool = False
    #: A controller seen for the first time -- default to bridging it, so the
    #: common case (one controller, one user) needs no configuration at all.
    auto_bridge_new: bool = True
    port_base: int = DEFAULT_PORT_BASE
    #: Seeds `hide_bluetooth` on a controller seen for the first time, the way
    #: `auto_bridge_new` seeds `enabled`. Ships False.
    hide_bluetooth_default: bool = False
    #: Where HidHideCLI.exe is, when it is not in the standard location. An
    #: escape hatch mirroring the existing `--usbip` flag -- and a necessary one,
    #: because HidHide 1.5.230 records its install path nowhere in the registry.
    hidhide_cli: str | None = None
    #: Keyed by LOWERCASED bdaddr, e.g. "d42f4ba1485d".
    controllers: dict = field(default_factory=dict)
    extra: dict = field(default_factory=dict)
    #: Where this was loaded from, so `save()` round-trips to the same file even
    #: if the environment changes underneath a long-running tray process.
    path: str | None = field(default=None, repr=False, compare=False)

    # -- per-controller ----------------------------------------------------

    def get(self, serial: str) -> ControllerConfig:
        """The entry for `serial`, creating it from `auto_bridge_new` if unseen.

        Returns the LIVE object: mutating what comes back and then calling
        `save()` is the intended way to change one controller's settings.
        """
        key = norm_serial(serial)
        cc = self.controllers.get(key)
        if cc is None:
            cc = ControllerConfig(enabled=bool(self.auto_bridge_new),
                                  hide_bluetooth=bool(self.hide_bluetooth_default))
            self.controllers[key] = cc
        return cc

    def set_enabled(self, serial: str, enabled: bool) -> ControllerConfig:
        cc = self.get(serial)
        cc.enabled = bool(enabled)
        return cc

    def set_hide_bluetooth(self, serial: str, hide: bool) -> ControllerConfig:
        cc = self.get(serial)
        cc.hide_bluetooth = bool(hide)
        return cc

    def should_hide(self, serial: str) -> bool:
        """Should this controller's Bluetooth pad be hidden while bridged?

        No master switch of its own. `config.enabled` already means "bridge
        nothing", and nothing bridged means nothing hidden -- hiding only ever
        happens inside a bridge's lifetime.
        """
        return bool(self.get(serial).hide_bluetooth)

    def set_master_enabled(self, enabled: bool) -> None:
        self.enabled = bool(enabled)

    def is_bridged(self, serial: str) -> bool:
        """Should this controller be bridged right now?

        The master switch wins. A caller that checks only the per-controller
        flag re-bridges everything the moment the user turns the whole thing off.
        """
        return bool(self.enabled) and bool(self.get(serial).enabled)

    def bridged_serials(self) -> list[str]:
        if not self.enabled:
            return []
        return sorted(s for s, cc in self.controllers.items() if cc.enabled)

    def port_for(self, serial: str) -> int:
        """The TCP port this controller should use. PINS it, and that is the point.

        An explicit port wins. Otherwise take the first slot at or above
        `port_base` that no other controller has pinned, skipping 3240 --
        usbipd-win owns that one and stealing it breaks an unrelated product
        that may well be running.

        The assignment is RECORDED on the controller entry rather than merely
        returned. A pure version of this function reads better and is wrong:
        with two controllers and nothing pinned yet it hands out `port_base` to
        both, they race for the same socket, and the second bridge dies with
        "address already in use" -- or worse, wins the race and the first one
        does. Measured on the real two-controller setup before this line
        existed: `port_for` returned 3241 for both.

        Pinning here also makes the port sticky across restarts once the config
        is saved, so a controller keeps its usbip port rather than swapping with
        its sibling depending on which one powered on first.

        This allocates against the CONFIG only. Whether the port is actually
        free on the machine is the caller's problem -- see `BridgeManager`,
        which re-checks with `service.port_free` before binding.
        """
        cc = self.get(serial)
        if cc.port:
            return cc.port
        taken = {c.port for c in self.controllers.values() if c.port}
        port = max(1, int(self.port_base))
        while port in taken or port == USBIPD_WIN_PORT:
            port += 1
        cc.port = port
        return port

    # -- serialisation -----------------------------------------------------

    @classmethod
    def from_dict(cls, data: object) -> "Config":
        """Never raises. Anything unrecognisable becomes its default."""
        if not isinstance(data, dict):
            log.warning("config is %s, not an object -- using defaults",
                        type(data).__name__)
            return cls()
        cfg = cls(
            enabled=_as_bool(data.get("enabled"), True),
            autostart_on_login=_as_bool(data.get("autostart_on_login"), False),
            auto_bridge_new=_as_bool(data.get("auto_bridge_new"), True),
            port_base=_as_port_base(data.get("port_base")),
            hide_bluetooth_default=_as_bool(data.get("hide_bluetooth_default"),
                                            False),
            hidhide_cli=_as_str(data.get("hidhide_cli"), "") or None,
        )
        raw = data.get("controllers")
        if raw is not None and not isinstance(raw, dict):
            log.warning("'controllers' is %s, not an object -- ignoring it",
                        type(raw).__name__)
            raw = None
        for serial, entry in (raw or {}).items():
            # Two keys differing only in case collide here, and the later one
            # wins. That is a hand-edited file, not corruption, so it is a log
            # line rather than a quarantine.
            key = norm_serial(str(serial))
            if not key:
                continue
            if key in cfg.controllers:
                log.warning("duplicate controller %s in the config -- keeping "
                            "the last one", key)
            cfg.controllers[key] = ControllerConfig.from_dict(entry)
        cfg.extra = {k: v for k, v in data.items() if k not in _CFG_KNOWN}
        return cfg

    def to_dict(self) -> dict:
        out = dict(self.extra)
        out.update(enabled=bool(self.enabled),
                   autostart_on_login=bool(self.autostart_on_login),
                   auto_bridge_new=bool(self.auto_bridge_new),
                   port_base=int(self.port_base),
                   hide_bluetooth_default=bool(self.hide_bluetooth_default),
                   hidhide_cli=self.hidhide_cli,
                   controllers={s: cc.to_dict()
                                for s, cc in sorted(self.controllers.items())})
        return out

    def save(self, path: str | None = None) -> str:
        return save(self, path)


# ---------------------------------------------------------------------------
# coercion -- every one of these returns a default instead of raising
# ---------------------------------------------------------------------------


def _as_bool(value: object, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    # A hand-edited "true"/"1"/"no" is a mistake worth honouring rather than
    # discarding: the user's intent is not in doubt.
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("true", "yes", "on", "1"):
            return True
        if v in ("false", "no", "off", "0"):
            return False
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _as_str(value: object, default: str) -> str:
    return value if isinstance(value, str) else default


def _as_choice(value: object, allowed: tuple, default: str) -> str:
    v = value.strip().lower() if isinstance(value, str) else None
    return v if v in allowed else default


def _as_port(value: object) -> int | None:
    """A pinned port, or None for 'allocate one'."""
    if value is None or isinstance(value, bool):
        return None
    try:
        port = int(value)
    except (TypeError, ValueError):
        return None
    if not (1 <= port <= 65535) or port == USBIPD_WIN_PORT:
        # Silently honouring 3240 would hand this controller usbipd-win's port
        # and break whichever of the two started second.
        log.warning("ignoring port %r in the config", value)
        return None
    return port


def _as_port_base(value: object) -> int:
    if value is None or isinstance(value, bool):
        return DEFAULT_PORT_BASE
    try:
        port = int(value)
    except (TypeError, ValueError):
        return DEFAULT_PORT_BASE
    if not (1 <= port <= 65000) or port == USBIPD_WIN_PORT:
        log.warning("port_base %r is not usable -- using %d", value,
                    DEFAULT_PORT_BASE)
        return DEFAULT_PORT_BASE
    return port


# ---------------------------------------------------------------------------
# load / save
# ---------------------------------------------------------------------------


def load(path: str | None = None) -> Config:
    """Read the config. NEVER raises, for any reason, ever.

    Missing file    -> defaults (the first run, and by far the common case)
    Unparseable     -> defaults, and the file is quarantined as config.json.bad
    Unreadable      -> defaults, and the file is left exactly where it is; if it
                       cannot be read it probably cannot be renamed either, and
                       a failed rescue must not become a second failure.
    """
    p = path or config_path()
    cfg = Config()
    cfg.path = p
    try:
        with open(p, "r", encoding="utf-8-sig") as f:
            text = f.read()
    except FileNotFoundError:
        return cfg
    except OSError as e:
        log.warning("cannot read %s (%s) -- using defaults", p, e)
        return cfg

    try:
        data = json.loads(text) if text.strip() else None
        if data is None:
            # An empty or whitespace-only file is the classic truncation: the
            # old contents are already gone, so there is nothing to preserve,
            # but it is still quarantined so "why did my settings vanish?" has
            # an answer sitting next to the config.
            raise ValueError("the file is empty")
        if not isinstance(data, (dict, list)):
            raise ValueError(f"top level is {type(data).__name__}, not an object")
    except (ValueError, UnicodeDecodeError) as e:
        _quarantine(p, str(e))
        return cfg

    cfg = Config.from_dict(data)
    cfg.path = p
    if not isinstance(data, dict):
        # Valid JSON, wrong shape (a list, most often from something that
        # appended instead of replacing). Defaults are already in place; keep
        # the evidence.
        _quarantine(p, "top level is a list, not an object")
    return cfg


def _quarantine(path: str, why: str) -> None:
    bad = path + BAD_SUFFIX
    try:
        os.replace(path, bad)
        log.warning("%s is unusable (%s) -- kept a copy at %s, using defaults",
                    path, why, bad)
    except OSError as e:
        log.warning("%s is unusable (%s) and could not be moved aside (%s) -- "
                    "using defaults", path, why, e)


def save(cfg: Config, path: str | None = None) -> str:
    """Write atomically. Returns the path written.

    Same-directory `.tmp` then `os.replace`, because a rename across volumes is
    not atomic and %APPDATA% can be a redirected network path. The temp file is
    removed on any failure -- a directory slowly filling with config.json.tmp is
    its own bug report.
    """
    p = path or cfg.path or config_path()
    directory = os.path.dirname(p) or "."
    tmp = p + TMP_SUFFIX
    try:
        os.makedirs(directory, exist_ok=True)
        text = json.dumps(cfg.to_dict(), indent=2, sort_keys=True) + "\n"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            # The point of the whole dance: without fsync the rename can land
            # before the bytes do, and a power cut leaves a zero-length file
            # that the next start has to quarantine.
            os.fsync(f.fileno())
        os.replace(tmp, p)
    except (OSError, TypeError, ValueError) as e:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise ConfigWriteError(
            f"could not save settings to {p} ({e}). Check that the folder "
            f"exists and is writable.") from e
    cfg.path = p
    return p


def try_save(cfg: Config, path: str | None = None) -> bool:
    """`save()` for callers with nowhere to show an error -- a tray menu click.

    Losing a setting is annoying; a menu handler that raises through pystray's
    message loop takes the icon with it, which is worse.
    """
    try:
        save(cfg, path)
        return True
    except ConfigWriteError as e:
        log.warning("%s", e)
        return False


# -- module-level conveniences: load, change one thing, write it back -------


def set_enabled(serial: str, enabled: bool, path: str | None = None) -> Config:
    cfg = load(path)
    cfg.set_enabled(serial, enabled)
    save(cfg, path)
    return cfg


def set_master_enabled(enabled: bool, path: str | None = None) -> Config:
    cfg = load(path)
    cfg.set_master_enabled(enabled)
    save(cfg, path)
    return cfg
