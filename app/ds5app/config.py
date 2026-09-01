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

#: Default TCP port for the local status dashboard. Loopback HTTP, so the only
#: constraint is "not something else on this machine" -- 8765 is memorable and
#: unregistered. The tray's "Open dashboard" item reads this field; the server
#: that answers on it lives elsewhere.
DEFAULT_DASHBOARD_PORT = 8765

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


# ---------------------------------------------------------------------------
# the `input` section: chords, gestures, remote mode, idle timer, lightbar
# ---------------------------------------------------------------------------
#
# Consumed by `ds5app.intercept.InputInterceptor`, which sits on the decoded
# Bluetooth input stream BEFORE the emulated USB layer -- so everything
# configured here is invisible to the game. This module only owns the SHAPE:
# plain JSON-able dataclasses, defaults that work untouched, and the same
# never-raise coercion discipline as the rest of the file, so a hand-edited
# chord table can be wrong without costing the user their tray icon.

#: Buttons a chord can be built from / act as the chord button. The names are
#: the config vocabulary; `intercept.py` maps them onto report bits.
CHORD_BUTTONS = ("cross", "circle", "square", "triangle",
                 "dpad_up", "dpad_down", "dpad_left", "dpad_right",
                 "l1", "r1", "l3", "r3", "create", "options", "ps",
                 "touchpad_click", "mute")

#: Touchpad gestures that can carry an action while the chord button is held.
CHORD_GESTURES = ("touch_slide_horizontal", "touch_swipe_up", "touch_swipe_down")

#: chord key (a button or gesture name) -> action name. Action names resolve
#: against `ds5app.actions.registry()`; an unknown name is ignored with one
#: log line, so a config written for a newer build degrades instead of dying.
#: A user entry of "" or "none" REMOVES a default binding.
DEFAULT_CHORDS = {
    "triangle": "pad_power_off",
    "cross": "media_play_pause",
    "square": "volume_mute",
    "dpad_up": "volume_up",
    "dpad_down": "volume_down",
    "dpad_left": "media_prev",
    "dpad_right": "media_next",
    "l1": "brightness_down",
    "r1": "brightness_up",
    "options": "projection_cycle",
    "create": "show_desktop",
    "touch_slide_horizontal": "alt_tab",
    "touch_swipe_up": "task_view",
    "touch_swipe_down": "minimize_all",
}

_BATTERY_KNOWN = ("enabled", "low_percent", "critical_percent",
                  "low_interval_s", "critical_interval_s",
                  "low_color", "critical_color", "low_blinks", "critical_blinks")


@dataclass
class BatteryAlerts:
    """Flash the lightbar when the pad is running down. All overridable."""

    enabled: bool = True
    low_percent: int = 20
    critical_percent: int = 10
    low_interval_s: float = 30.0
    critical_interval_s: float = 10.0
    low_color: list = field(default_factory=lambda: [255, 140, 0])   # amber
    critical_color: list = field(default_factory=lambda: [255, 0, 0])
    low_blinks: int = 2
    critical_blinks: int = 3
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: object) -> "BatteryAlerts":
        if not isinstance(data, dict):
            return cls()
        b = cls(
            enabled=_as_bool(data.get("enabled"), True),
            low_percent=_as_int(data.get("low_percent"), 20, 0, 100),
            critical_percent=_as_int(data.get("critical_percent"), 10, 0, 100),
            low_interval_s=_as_float(data.get("low_interval_s"), 30.0, 1.0),
            critical_interval_s=_as_float(data.get("critical_interval_s"), 10.0, 1.0),
            low_color=_as_color(data.get("low_color"), [255, 140, 0]),
            critical_color=_as_color(data.get("critical_color"), [255, 0, 0]),
            low_blinks=_as_int(data.get("low_blinks"), 2, 1, 10),
            critical_blinks=_as_int(data.get("critical_blinks"), 3, 1, 10),
        )
        b.extra = {k: v for k, v in data.items() if k not in _BATTERY_KNOWN}
        return b

    def to_dict(self) -> dict:
        out = dict(self.extra)
        out.update(enabled=bool(self.enabled),
                   low_percent=int(self.low_percent),
                   critical_percent=int(self.critical_percent),
                   low_interval_s=float(self.low_interval_s),
                   critical_interval_s=float(self.critical_interval_s),
                   low_color=list(self.low_color),
                   critical_color=list(self.critical_color),
                   low_blinks=int(self.low_blinks),
                   critical_blinks=int(self.critical_blinks))
        return out


_LIGHTBAR_KNOWN = ("dim_after_minutes", "dim_level")


@dataclass
class LightbarPolicy:
    """Dim (or kill) the lightbar after a while, to save the pad's battery.

    The override happens by rewriting the game's outgoing SetState before it
    reaches the pad, so the game's own idea of its lightbar is untouched.
    """

    #: Minutes of bridged play before the dim engages. 0 disables (default:
    #: this ships OFF -- a lightbar that goes dark unasked reads as a fault).
    dim_after_minutes: float = 0.0
    #: 0.0 = lightbar fully off, 1.0 = untouched. Applied as a multiplier on
    #: the RGB the game asked for.
    dim_level: float = 0.3
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: object) -> "LightbarPolicy":
        if not isinstance(data, dict):
            return cls()
        lp = cls(
            dim_after_minutes=_as_float(data.get("dim_after_minutes"), 0.0, 0.0),
            dim_level=min(1.0, _as_float(data.get("dim_level"), 0.3, 0.0)),
        )
        lp.extra = {k: v for k, v in data.items() if k not in _LIGHTBAR_KNOWN}
        return lp

    def to_dict(self) -> dict:
        out = dict(self.extra)
        out.update(dim_after_minutes=float(self.dim_after_minutes),
                   dim_level=float(self.dim_level))
        return out


_REMOTE_KNOWN = ("enabled", "mouse_speed", "scroll_speed", "lightbar_color")


@dataclass
class RemoteMode:
    """Double-press of the chord button: the pad becomes an OS remote.

    No input reaches the game (it sees a neutral pad); the touchpad drives the
    mouse pointer, Cross clicks, dpad is arrow keys. The full map lives in
    `docs/input-shortcuts.md` and `intercept.py`.
    """

    enabled: bool = True
    #: Pointer speed multiplier for touchpad drags and the left stick.
    mouse_speed: float = 1.6
    scroll_speed: float = 1.0
    #: The lightbar while remote mode is on -- the visible "you are not in the
    #: game any more" cue, alongside the haptic pattern.
    lightbar_color: list = field(default_factory=lambda: [255, 120, 0])
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: object) -> "RemoteMode":
        if not isinstance(data, dict):
            return cls()
        rm = cls(
            enabled=_as_bool(data.get("enabled"), True),
            mouse_speed=_as_float(data.get("mouse_speed"), 1.6, 0.1),
            scroll_speed=_as_float(data.get("scroll_speed"), 1.0, 0.1),
            lightbar_color=_as_color(data.get("lightbar_color"), [255, 120, 0]),
        )
        rm.extra = {k: v for k, v in data.items() if k not in _REMOTE_KNOWN}
        return rm

    def to_dict(self) -> dict:
        out = dict(self.extra)
        out.update(enabled=bool(self.enabled),
                   mouse_speed=float(self.mouse_speed),
                   scroll_speed=float(self.scroll_speed),
                   lightbar_color=list(self.lightbar_color))
        return out


_INPUT_KNOWN = ("enabled", "chord_button", "chords", "actions",
                "double_press_ms", "tap_replay_ms", "repeat_ms",
                "haptic_ack", "off_timer_minutes",
                "battery", "lightbar", "remote")


@dataclass
class InputConfig:
    """The chord/shortcut engine. One section, global to every bridged pad."""

    enabled: bool = True
    #: Which button arms chords while held. Held alone and released quickly it
    #: is REPLAYED to the game, so a plain PS tap still opens the game's menu.
    chord_button: str = "ps"
    #: chord key -> action name. Stored MERGED over `DEFAULT_CHORDS`: a config
    #: that names one chord changes that one and keeps the rest, and mapping a
    #: key to ""/"none" removes it.
    chords: dict = field(default_factory=lambda: dict(DEFAULT_CHORDS))
    #: Per-action parameter overrides, keyed by action name, e.g.
    #: {"volume_up": {"step": 2}}. Passed verbatim to the action registry.
    actions: dict = field(default_factory=dict)
    #: Two chord-button presses within this window toggle remote mode.
    double_press_ms: int = 400
    #: How long the replayed chord-button tap is held down for the game.
    tap_replay_ms: int = 100
    #: Repeat cadence for repeatable chord actions (volume, brightness) while
    #: the chord is held.
    repeat_ms: int = 150
    #: A short rumble pulse on the pad whenever a chord is accepted.
    haptic_ack: bool = True
    #: Minutes without input activity before the pad is powered off
    #: (feature 0x08, the same mechanism as PS+Triangle). 0 disables.
    off_timer_minutes: float = 15.0
    battery: BatteryAlerts = field(default_factory=BatteryAlerts)
    lightbar: LightbarPolicy = field(default_factory=LightbarPolicy)
    remote: RemoteMode = field(default_factory=RemoteMode)
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: object) -> "InputConfig":
        if not isinstance(data, dict):
            if data is not None:
                log.warning("'input' is %s, not an object -- using defaults",
                            type(data).__name__)
            return cls()
        ic = cls(
            enabled=_as_bool(data.get("enabled"), True),
            chord_button=_as_choice(data.get("chord_button"), CHORD_BUTTONS, "ps"),
            double_press_ms=_as_int(data.get("double_press_ms"), 400, 100, 2000),
            tap_replay_ms=_as_int(data.get("tap_replay_ms"), 100, 20, 1000),
            repeat_ms=_as_int(data.get("repeat_ms"), 150, 30, 2000),
            haptic_ack=_as_bool(data.get("haptic_ack"), True),
            off_timer_minutes=_as_float(data.get("off_timer_minutes"), 15.0, 0.0),
            battery=BatteryAlerts.from_dict(data.get("battery")),
            lightbar=LightbarPolicy.from_dict(data.get("lightbar")),
            remote=RemoteMode.from_dict(data.get("remote")),
        )
        raw = data.get("chords")
        if raw is not None and not isinstance(raw, dict):
            log.warning("'input.chords' is %s, not an object -- keeping the "
                        "defaults", type(raw).__name__)
            raw = None
        for key, action in (raw or {}).items():
            key = str(key).strip().lower()
            if not key:
                continue
            name = action.strip().lower() if isinstance(action, str) else ""
            if name in ("", "none", "off"):
                ic.chords.pop(key, None)
            else:
                ic.chords[key] = name
        acts = data.get("actions")
        ic.actions = dict(acts) if isinstance(acts, dict) else {}
        ic.extra = {k: v for k, v in data.items() if k not in _INPUT_KNOWN}
        return ic

    def to_dict(self) -> dict:
        # A default chord the user removed must be WRITTEN as "none", not
        # merely absent: `from_dict` merges the file over `DEFAULT_CHORDS`, so
        # an absent key would resurrect the default on the very next load and
        # the removal would survive exactly one session.
        chords = dict(self.chords)
        for key in DEFAULT_CHORDS:
            if key not in chords:
                chords[key] = "none"
        out = dict(self.extra)
        out.update(enabled=bool(self.enabled),
                   chord_button=self.chord_button,
                   chords={k: chords[k] for k in sorted(chords)},
                   actions=dict(self.actions),
                   double_press_ms=int(self.double_press_ms),
                   tap_replay_ms=int(self.tap_replay_ms),
                   repeat_ms=int(self.repeat_ms),
                   haptic_ack=bool(self.haptic_ack),
                   off_timer_minutes=float(self.off_timer_minutes),
                   battery=self.battery.to_dict(),
                   lightbar=self.lightbar.to_dict(),
                   remote=self.remote.to_dict())
        return out


_CFG_KNOWN = ("enabled", "autostart_on_login", "auto_bridge_new", "port_base",
              "controllers", "hide_bluetooth_default", "hidhide_cli",
              "dashboard_port", "update_check", "input")


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
    #: Where the local status dashboard answers, as in http://127.0.0.1:8765.
    #: The tray's "Open dashboard" item is the only consumer in this module's
    #: orbit; the HTTP server itself binds it elsewhere.
    dashboard_port: int = DEFAULT_DASHBOARD_PORT

    #: Ask the GitHub releases API (twice a day, ETag-cached, unauthenticated)
    #: whether a newer ds5bridge exists, and say so in the tray. The check is
    #: the ONLY thing this gates -- nothing downloads and nothing installs
    #: without a click. See `update.py` for what a check actually sends
    #: (nothing about you; a conditional GET of public release metadata).
    update_check: bool = True
    #: Keyed by LOWERCASED bdaddr, e.g. "d42f4ba1485d".
    controllers: dict = field(default_factory=dict)
    #: The chord/shortcut engine (PS-button chords, touch gestures, remote
    #: mode, idle off-timer, battery lightbar alerts). Global, not
    #: per-controller: a chord means the same thing on every pad.
    input: InputConfig = field(default_factory=InputConfig)
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

    def set_hide_bluetooth_default(self, hide: bool) -> None:
        """The seed `get()` stamps on a controller this machine has never seen.

        Every other field the UI can change is per controller, so this one was
        read-only for a long time: it existed to be hand-edited. Once the tray
        grew a single switch for "hide all of them", that stopped being
        defensible -- a user who hides both pads and then plugs in a third
        means the third one too, and without writing the seed the answer would
        silently be "no" for every controller bought after the click.
        """
        self.hide_bluetooth_default = bool(hide)

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
            dashboard_port=_as_dashboard_port(data.get("dashboard_port")),
            update_check=_as_bool(data.get("update_check"), True),
            input=InputConfig.from_dict(data.get("input")),
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
                   dashboard_port=int(self.dashboard_port),
                   update_check=bool(self.update_check),
                   input=self.input.to_dict(),
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


def _as_int(value: object, default: int, lo: int, hi: int) -> int:
    if isinstance(value, bool) or value is None:
        return default
    try:
        v = int(value)
    except (TypeError, ValueError):
        return default
    return v if lo <= v <= hi else default


def _as_float(value: object, default: float, lo: float) -> float:
    if isinstance(value, bool) or value is None:
        return default
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    return v if v >= lo else default


def _as_color(value: object, default: list) -> list:
    """[r, g, b], each 0..255. Anything else -> the default, whole."""
    if (isinstance(value, (list, tuple)) and len(value) == 3
            and all(isinstance(c, (int, float)) and not isinstance(c, bool)
                    for c in value)):
        return [max(0, min(255, int(c))) for c in value]
    return list(default)


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


def _as_dashboard_port(value: object) -> int:
    """The dashboard's TCP port, or the default for anything unusable.

    3240 is refused like everywhere else in this module: the dashboard is an
    HTTP server, but it genuinely BINDS this port, and 3240 is where usbipd-win
    listens on machines that have it -- an HTTP server there would fight a real
    service, not merely break the dashboard.
    """
    if value is None or isinstance(value, bool):
        return DEFAULT_DASHBOARD_PORT
    try:
        port = int(value)
    except (TypeError, ValueError):
        return DEFAULT_DASHBOARD_PORT
    if not (1 <= port <= 65535) or port == USBIPD_WIN_PORT:
        log.warning("dashboard_port %r is not usable -- using %d", value,
                    DEFAULT_DASHBOARD_PORT)
        return DEFAULT_DASHBOARD_PORT
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
