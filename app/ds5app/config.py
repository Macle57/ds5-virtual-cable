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

#: The 2-finger touchpad gestures, in the order the dashboard shows them.
#: Every one is a key in BOTH binding tables (`input.chords` and
#: `input.remote.chords`). Three groups (`GESTURE_META`): "taps" -- a short
#: 2-finger touch and a physical click with two fingers down; "unpressed" --
#: 2-finger movement while the pad is NOT clicked; "pressed" -- the same
#: movement while the pad IS clicked. A contact is exactly one of these: a
#: click never doubles as a tap, a slide never fires the tap, and the click
#: OUTRANKS the unpressed family -- the moment the pad goes down, whatever
#: unpressed gesture the contact was making ends and the travel is measured
#: afresh from the click, against the `_pressed` rows only.
#:
#: The four slide directions are separate rows so the dashboard can offer
#: "scroll up" on the up row and "scroll down" on the down row -- and they
#: come in PAIRS (`GESTURE_META[...]["pair"]`): the two rows of an axis are
#: either bound to one paired action (`ActionSpec.pair`: the scroll pairs,
#: `alt_tab` with itself) or to independent one-shots. The engine reads the
#: rows literally; the pairing is a dashboard rule that keeps a config
#: sane (docs/input-shortcuts.md, "Two-finger gestures").
CHORD_GESTURES = (
    "touch_tap_2f", "touch_click_2f",
    "touch_slide_up", "touch_slide_down",
    "touch_slide_left", "touch_slide_right", "touch_pinch",
    "touch_slide_up_pressed", "touch_slide_down_pressed",
    "touch_slide_left_pressed", "touch_slide_right_pressed",
    "touch_pinch_pressed",
)

#: What /api/actions serves as `gestures` -- the dashboard renders its
#: Gestures tab from this list, verbatim and in this order. `pair` names the
#: row that shares the axis; `dir` is the direction a paired action must
#: match (`ActionSpec.dir`) to be offered on that row.
GESTURE_META = (
    {"key": "touch_tap_2f", "group": "taps",
     "label": "Two-finger tap",
     "help": "two fingers touch briefly without clicking the pad"},
    {"key": "touch_click_2f", "group": "taps",
     "label": "Two-finger click",
     "help": "click the pad while two fingers rest on it (a Windows "
             "touchpad's right click)"},
    {"key": "touch_slide_up", "group": "unpressed",
     "pair": "touch_slide_down", "dir": "up",
     "label": "Two-finger slide up",
     "help": "two fingers slide up, pad not clicked; paired with slide down"},
    {"key": "touch_slide_down", "group": "unpressed",
     "pair": "touch_slide_up", "dir": "down",
     "label": "Two-finger slide down",
     "help": "two fingers slide down, pad not clicked; paired with slide up"},
    {"key": "touch_slide_left", "group": "unpressed",
     "pair": "touch_slide_right", "dir": "left",
     "label": "Two-finger slide left",
     "help": "two fingers slide left, pad not clicked; paired with slide "
             "right"},
    {"key": "touch_slide_right", "group": "unpressed",
     "pair": "touch_slide_left", "dir": "right",
     "label": "Two-finger slide right",
     "help": "two fingers slide right, pad not clicked; paired with slide "
             "left"},
    {"key": "touch_pinch", "group": "unpressed",
     "label": "Pinch / spread",
     "help": "two fingers move apart or together, pad not clicked"},
    {"key": "touch_slide_up_pressed", "group": "pressed",
     "pair": "touch_slide_down_pressed", "dir": "up",
     "label": "Slide up while clicked",
     "help": "two fingers slide up with the pad held down"},
    {"key": "touch_slide_down_pressed", "group": "pressed",
     "pair": "touch_slide_up_pressed", "dir": "down",
     "label": "Slide down while clicked",
     "help": "two fingers slide down with the pad held down"},
    {"key": "touch_slide_left_pressed", "group": "pressed",
     "pair": "touch_slide_right_pressed", "dir": "left",
     "label": "Slide left while clicked",
     "help": "two fingers slide left with the pad held down"},
    {"key": "touch_slide_right_pressed", "group": "pressed",
     "pair": "touch_slide_left_pressed", "dir": "right",
     "label": "Slide right while clicked",
     "help": "two fingers slide right with the pad held down"},
    {"key": "touch_pinch_pressed", "group": "pressed",
     "label": "Pinch / spread while clicked",
     "help": "two fingers move apart or together with the pad held down"},
)

#: The gesture rows of 1.0 and 0.5. They are DROPPED on load, not carried
#: over: the directional rows above start from their defaults whatever an
#: older file said, so every install gets the same, documented gesture map
#: (the old rows' meanings never mapped cleanly -- the unpressed flicks
#: were dead beside a scrolling vertical slide, and a 0.5 file still lists
#: them). The file loses them on its next save.
LEGACY_GESTURE_KEYS = (
    "touch_slide_vertical", "touch_slide_horizontal",
    "touch_swipe_up", "touch_swipe_down",
    "touch_swipe_up_pressed", "touch_swipe_down_pressed",
    "touch_slide_horizontal_pressed",
)


def gesture_meta() -> list:
    """`GESTURE_META` as fresh dicts, for /api/actions."""
    return [dict(g) for g in GESTURE_META]


def is_gesture_key(key: str) -> bool:
    """A gesture row (as opposed to a button row) in either binding table:
    every key that starts with `touch_`, except the physical click."""
    return key.startswith("touch_") and key != "touchpad_click"


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
    "r3": "show_battery",
    "options": "projection_cycle",
    "create": "show_desktop",
    # Gestures. Unpressed 2-finger movement behaves like a precision
    # touchpad (scroll, pinch-zoom); the shell gestures live on the CLICKED
    # variants. `touch_tap_2f` and the clicked pinch are unbound here.
    "touch_click_2f": "right_click",
    "touch_slide_up": "scroll_up",
    "touch_slide_down": "scroll_down",
    "touch_slide_left": "scroll_left",
    "touch_slide_right": "scroll_right",
    "touch_pinch": "pinch_zoom",
    "touch_slide_left_pressed": "alt_tab",
    "touch_slide_right_pressed": "alt_tab",
    "touch_slide_up_pressed": "task_view",
    "touch_slide_down_pressed": "minimize_all",
    # Two buttons nothing else claimed: the touchpad click opens the pad-driven
    # on-screen keyboard, the mute button (the one with the microphone on it)
    # starts voice typing through the pad's own microphone.
    "touchpad_click": "keyboard",
    "mute": "dictation",
}

#: The REMOTE-MODE binding table's defaults -- what a button or gesture does
#: while the pad is being an OS remote and `remote.same_bindings` is off.
#: These reproduce the hard-coded remote map this engine shipped with, so a
#: user who unticks "same bindings" gets exactly the mode they knew: Cross is
#: the left mouse button (hold to drag), Circle Esc, Options Enter, the dpad
#: the arrow keys, a 2-finger horizontal slide the Alt-Tab hold. Same
#: vocabulary and the same merge/"none" rules as `DEFAULT_CHORDS`. The
#: intrinsic pointer controls (touchpad = mouse, taps = clicks, sticks and
#: triggers = pointer/scroll) are NOT bindings -- they are what remote mode IS.
DEFAULT_REMOTE_CHORDS = {
    "cross": "left_click",
    "circle": "escape",
    "options": "enter",
    "dpad_up": "arrow_up",
    "dpad_down": "arrow_down",
    "dpad_left": "arrow_left",
    "dpad_right": "arrow_right",
    # The same gesture rows as the chord table, plus the classic remote
    # right click on a 2-finger tap. `touchpad_click` is deliberately not a
    # row: in remote mode a 1-finger physical click is the left mouse button.
    "touch_tap_2f": "right_click",
    "touch_click_2f": "right_click",
    "touch_slide_up": "scroll_up",
    "touch_slide_down": "scroll_down",
    "touch_slide_left": "scroll_left",
    "touch_slide_right": "scroll_right",
    "touch_pinch": "pinch_zoom",
    "touch_slide_left_pressed": "alt_tab",
    "touch_slide_right_pressed": "alt_tab",
    "touch_slide_up_pressed": "task_view",
    "touch_slide_down_pressed": "minimize_all",
}


def merge_chords(defaults: dict, raw: object, where: str) -> dict:
    """`defaults` with a user's table merged over it. Never raises.

    The one merge rule both binding tables live by: naming a key changes that
    key and keeps the rest; ""/"none"/"off" removes a default. Keys and action
    names are lowercased so a hand-edited "Cross" still binds. The gesture
    rows of older builds (`LEGACY_GESTURE_KEYS`) are dropped, with one log
    line, so today's rows keep their defaults.
    """
    out = dict(defaults)
    if raw is not None and not isinstance(raw, dict):
        log.warning("'%s' is %s, not an object -- keeping the defaults",
                    where, type(raw).__name__)
        raw = None
    dropped = []
    for key, action in (raw or {}).items():
        key = str(key).strip().lower()
        if not key:
            continue
        if key in LEGACY_GESTURE_KEYS:
            dropped.append(key)
            continue
        name = action.strip().lower() if isinstance(action, str) else ""
        if name in ("", "none", "off"):
            out.pop(key, None)
        else:
            out[key] = name
    if dropped:
        log.info("%s: ignoring the gesture rows of an older build (%s); "
                 "the current rows keep their defaults", where,
                 ", ".join(sorted(dropped)))
    return out


def chords_to_dict(defaults: dict, chords: dict) -> dict:
    """The table as written to disk: a REMOVED default is written as "none".

    Merely absent would resurrect it on the next `merge_chords` and the
    removal would survive exactly one session. Sorted, so the file diffs.
    """
    out = dict(chords)
    for key in defaults:
        if key not in out:
            out[key] = "none"
    return {k: out[k] for k in sorted(out)}


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


_GESTURES_KNOWN = ("tap_ms", "tap_move_px", "slide_px", "swipe_px",
                   "alt_tab_step_px", "pinch_px", "scroll_px_per_notch",
                   "zoom_px_per_notch", "pinch_gain", "pinch_delay_ms",
                   "scroll_sensitivity", "scroll_reverse",
                   "zoom_sensitivity", "zoom_reverse")


@dataclass
class GestureTuning:
    """Thresholds for the 2-finger gestures, in touchpad points (the pad is
    1920 x 1080 points over roughly 52 x 23 mm, so ~37 points per mm).
    Every default was chosen against real pad traffic; they are here so a
    user with a different thumb can move them without a code change."""

    #: A touch shorter than this (ms) that travelled less than `tap_move_px`
    #: is a tap; a physical click that travelled less is a click.
    tap_ms: int = 250
    tap_move_px: int = 40
    #: Unpressed 2-finger travel at which the contact commits to a slide
    #: (horizontal or vertical, whichever dominates) -- scrolling begins.
    slide_px: int = 40
    #: Vertical travel that fires a swipe (the pressed swipes, and the
    #: unpressed compatibility flicks).
    swipe_px: int = 200
    #: Horizontal travel that opens Alt-Tab, and per further step through it.
    alt_tab_step_px: int = 150
    #: Change in finger spread at which a contact commits to a pinch ...
    pinch_px: int = 80
    #: ... but never before this long after the second finger landed, and
    #: only while the spread change clearly beats the travel: a scroll that
    #: starts with a little spread wobble is a scroll (the Windows touchpad
    #: rule -- scrolling wins the ambiguous opening of a contact).
    pinch_delay_ms: int = 120
    #: Finger travel per wheel notch (120 units) for the `scroll` actions,
    #: before `remote.scroll_speed`.
    scroll_px_per_notch: int = 100
    #: Spread change per Ctrl+wheel notch for `ctrl_zoom`.
    zoom_px_per_notch: int = 80
    #: Screen pixels the injected touch contacts move per point of finger
    #: spread change, for `pinch_zoom`.
    pinch_gain: float = 1.0
    #: The user-facing knobs on the two continuous families, applied on top
    #: of the per-notch figures above: a multiplier on how far a slide
    #: scrolls (`scroll_*` rows) and on how much a pinch zooms (`pinch_zoom`
    #: and `ctrl_zoom`), and a direction flip for each. Touch scrolling is
    #: NOT scaled by `remote.scroll_speed` -- that knob is the sticks' and
    #: the triggers'.
    scroll_sensitivity: float = 1.0
    scroll_reverse: bool = False
    zoom_sensitivity: float = 1.0
    zoom_reverse: bool = False
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: object) -> "GestureTuning":
        if not isinstance(data, dict):
            return cls()
        g = cls(
            tap_ms=_as_int(data.get("tap_ms"), 250, 50, 2000),
            tap_move_px=_as_int(data.get("tap_move_px"), 40, 5, 500),
            slide_px=_as_int(data.get("slide_px"), 40, 5, 1000),
            swipe_px=_as_int(data.get("swipe_px"), 200, 20, 1000),
            alt_tab_step_px=_as_int(data.get("alt_tab_step_px"), 150, 20, 1000),
            pinch_px=_as_int(data.get("pinch_px"), 80, 10, 1000),
            pinch_delay_ms=_as_int(data.get("pinch_delay_ms"), 120, 0, 1000),
            scroll_px_per_notch=_as_int(data.get("scroll_px_per_notch"),
                                        100, 5, 2000),
            zoom_px_per_notch=_as_int(data.get("zoom_px_per_notch"),
                                      80, 5, 2000),
            pinch_gain=_as_float(data.get("pinch_gain"), 1.0, 0.05),
            scroll_sensitivity=min(10.0, _as_float(
                data.get("scroll_sensitivity"), 1.0, 0.05)),
            scroll_reverse=_as_bool(data.get("scroll_reverse"), False),
            zoom_sensitivity=min(10.0, _as_float(
                data.get("zoom_sensitivity"), 1.0, 0.05)),
            zoom_reverse=_as_bool(data.get("zoom_reverse"), False),
        )
        g.extra = {k: v for k, v in data.items() if k not in _GESTURES_KNOWN}
        return g

    def to_dict(self) -> dict:
        out = dict(self.extra)
        out.update(tap_ms=int(self.tap_ms), tap_move_px=int(self.tap_move_px),
                   slide_px=int(self.slide_px), swipe_px=int(self.swipe_px),
                   alt_tab_step_px=int(self.alt_tab_step_px),
                   pinch_px=int(self.pinch_px),
                   pinch_delay_ms=int(self.pinch_delay_ms),
                   scroll_px_per_notch=int(self.scroll_px_per_notch),
                   zoom_px_per_notch=int(self.zoom_px_per_notch),
                   pinch_gain=float(self.pinch_gain),
                   scroll_sensitivity=float(self.scroll_sensitivity),
                   scroll_reverse=bool(self.scroll_reverse),
                   zoom_sensitivity=float(self.zoom_sensitivity),
                   zoom_reverse=bool(self.zoom_reverse))
        return out


_REMOTE_KNOWN = ("enabled", "mouse_speed", "scroll_speed", "lightbar_color",
                 "same_bindings", "same_gestures", "chords")


@dataclass
class RemoteMode:
    """Double-press of the chord button: the pad becomes an OS remote.

    No input reaches the game (it sees a neutral pad); the touchpad drives the
    mouse pointer, taps click, the sticks and triggers move the pointer and
    scroll -- those are intrinsic. Every BUTTON and touch GESTURE, on the
    other hand, resolves through a binding table, and which table is the
    `same_bindings` switch:

        same_bindings = True (default)   the `input.chords` table: a button or
                                         gesture means in remote mode exactly
                                         what it means with the chord button
                                         held -- one table to maintain.
        same_bindings = False            this section's own `chords`, merged
                                         over `DEFAULT_REMOTE_CHORDS` (the
                                         classic remote map: Cross = left
                                         mouse button, Circle = Esc, ...).

    The full map lives in `docs/input-shortcuts.md` and `intercept.py`.

    Ships OFF: a mode the pad can fall into from a mistimed double-tap of the
    PS button reads as "my controller broke" to anyone who did not turn it on.
    The dashboard is where a user opts in. Note the speed/colour fields below
    stay meaningful even while `enabled` is False -- the chord-held stick
    translation (`InputConfig.stick_mouse_in_chord`) borrows them, so the
    pointer feels identical in both modes.
    """

    enabled: bool = False
    #: Pointer speed multiplier for touchpad drags and the left stick.
    mouse_speed: float = 1.6
    scroll_speed: float = 1.0
    #: The lightbar while remote mode is on -- the visible "you are not in the
    #: game any more" cue, alongside the haptic pattern.
    lightbar_color: list = field(default_factory=lambda: [255, 120, 0])
    #: Remote mode's BUTTON rows come from the `input.chords` table (True)
    #: or from its own `chords`.
    same_bindings: bool = True
    #: Remote mode's GESTURE rows (`is_gesture_key`) come from the
    #: `input.chords` table (True) or from its own `chords`. Independent of
    #: `same_bindings`: a user who wants the classic remote buttons can still
    #: keep one gesture table, and vice versa.
    same_gestures: bool = True
    #: The remote-mode binding table, MERGED over `DEFAULT_REMOTE_CHORDS`
    #: exactly the way `InputConfig.chords` merges over `DEFAULT_CHORDS`.
    #: Consulted only while `same_bindings` / `same_gestures` is False, but
    #: always parsed and always written, so a user can flip a switch without
    #: losing the table.
    chords: dict = field(default_factory=lambda: dict(DEFAULT_REMOTE_CHORDS))
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: object) -> "RemoteMode":
        if not isinstance(data, dict):
            return cls()
        rm = cls(
            enabled=_as_bool(data.get("enabled"), False),
            mouse_speed=_as_float(data.get("mouse_speed"), 1.6, 0.1),
            scroll_speed=_as_float(data.get("scroll_speed"), 1.0, 0.1),
            lightbar_color=_as_color(data.get("lightbar_color"), [255, 120, 0]),
            same_bindings=_as_bool(data.get("same_bindings"), True),
            same_gestures=_as_bool(data.get("same_gestures"), True),
            chords=merge_chords(DEFAULT_REMOTE_CHORDS, data.get("chords"),
                                "input.remote.chords"),
        )
        rm.extra = {k: v for k, v in data.items() if k not in _REMOTE_KNOWN}
        return rm

    def to_dict(self) -> dict:
        out = dict(self.extra)
        out.update(enabled=bool(self.enabled),
                   mouse_speed=float(self.mouse_speed),
                   scroll_speed=float(self.scroll_speed),
                   lightbar_color=list(self.lightbar_color),
                   same_bindings=bool(self.same_bindings),
                   same_gestures=bool(self.same_gestures),
                   chords=chords_to_dict(DEFAULT_REMOTE_CHORDS, self.chords))
        return out


#: Actions the ENGINE owns -- they act on the pad or on the engine's own
#: state, not on the desktop -- so they are not in
#: `actions.OsActions.registry()`. A chord names them like any other action;
#: /api/actions serves them as `engine_actions`.
ENGINE_ACTIONS = {
    "pad_power_off": "power the pad off",
    "pad_lightbar_toggle": "lightbar off; press again to bring it back",
    "keyboard": "on-screen keyboard driven by the pad (Steam-style); "
                "press again, or Circle/Options on it, to close",
    "show_battery": "a notification with this pad's battery level and "
                    "charging state",
}

_INPUT_KNOWN = ("enabled", "chord_button", "chords", "actions", "macros",
                "double_press_ms", "tap_replay_ms", "repeat_ms",
                "haptic_ack", "haptic_strength", "stick_mouse_in_chord",
                "off_timer_minutes",
                "battery", "lightbar", "remote", "gestures")


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
    #: User-defined actions, keyed by the name a chord binds to:
    #: {"task_manager": {"keys": ["ctrl", "shift", "esc"], "label": "..."}}
    #: or {"notes": {"run": "notepad.exe"}}. Stored as written; the engine
    #: compiles them (`actions.OsActions.macro_spec`) and skips bad ones.
    macros: dict = field(default_factory=dict)
    #: Two chord-button presses within this window toggle remote mode.
    double_press_ms: int = 400
    #: How long the replayed chord-button tap is held down for the game.
    tap_replay_ms: int = 100
    #: Repeat cadence for repeatable chord actions (volume, brightness) while
    #: the chord is held.
    repeat_ms: int = 150
    #: A short rumble pulse on the pad whenever a chord is accepted.
    haptic_ack: bool = True
    #: How hard that pulse (and the remote-mode toggle pulses) hits, 0-100.
    #: 100 is the motor flat out; the default is deliberately gentle -- the
    #: first hardware test found even 1/3 strength startling in a quiet room.
    #: `haptic_ack` stays the on/off switch; this is only the volume knob.
    haptic_strength: int = 25
    #: While the chord button is held, lend the pointer controls to the OS:
    #: left stick moves the pointer, right stick scrolls, with the same
    #: `remote.*` speeds as remote mode -- and the game sees them centred --
    #: and a ONE-finger touchpad drag moves the pointer too (a short 1-finger
    #: tap clicks), exactly as in remote mode. The cost is a frozen camera
    #: for as long as the chord is held, which is why this is a switch:
    #: turning it off restores pure stick passthrough during chords and
    #: leaves the 1-finger touchpad to the 2-finger tracker alone.
    stick_mouse_in_chord: bool = True
    #: Minutes without input activity before the pad is powered off
    #: (feature 0x08, the same mechanism as PS+Triangle). 0 disables.
    off_timer_minutes: float = 15.0
    battery: BatteryAlerts = field(default_factory=BatteryAlerts)
    lightbar: LightbarPolicy = field(default_factory=LightbarPolicy)
    remote: RemoteMode = field(default_factory=RemoteMode)
    #: Thresholds for the 2-finger gestures (`GestureTuning`).
    gestures: GestureTuning = field(default_factory=GestureTuning)
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
            haptic_strength=_as_int(data.get("haptic_strength"), 25, 0, 100),
            stick_mouse_in_chord=_as_bool(data.get("stick_mouse_in_chord"),
                                          True),
            off_timer_minutes=_as_float(data.get("off_timer_minutes"), 15.0, 0.0),
            battery=BatteryAlerts.from_dict(data.get("battery")),
            lightbar=LightbarPolicy.from_dict(data.get("lightbar")),
            remote=RemoteMode.from_dict(data.get("remote")),
            gestures=GestureTuning.from_dict(data.get("gestures")),
        )
        ic.chords = merge_chords(DEFAULT_CHORDS, data.get("chords"),
                                 "input.chords")
        acts = data.get("actions")
        ic.actions = dict(acts) if isinstance(acts, dict) else {}
        macros = data.get("macros")
        if macros is not None and not isinstance(macros, dict):
            log.warning("'input.macros' is %s, not an object -- ignored",
                        type(macros).__name__)
            macros = None
        for name, macro in (macros or {}).items():
            name = str(name).strip().lower()
            if not name:
                continue
            if macro is None:
                # The settings page's tombstone: a POST deep-merges onto the
                # file, so "delete this macro" arrives as {"name": null}.
                continue
            if not isinstance(macro, dict):
                log.warning("macro %r is %s, not an object -- dropped",
                            name, type(macro).__name__)
                continue
            ic.macros[name] = dict(macro)
        ic.extra = {k: v for k, v in data.items() if k not in _INPUT_KNOWN}
        return ic

    def to_dict(self) -> dict:
        # A default chord the user removed must be WRITTEN as "none", not
        # merely absent: `from_dict` merges the file over `DEFAULT_CHORDS`, so
        # an absent key would resurrect the default on the very next load and
        # the removal would survive exactly one session (`chords_to_dict`).
        out = dict(self.extra)
        out.update(enabled=bool(self.enabled),
                   chord_button=self.chord_button,
                   chords=chords_to_dict(DEFAULT_CHORDS, self.chords),
                   actions=dict(self.actions),
                   macros={k: dict(self.macros[k]) for k in sorted(self.macros)},
                   double_press_ms=int(self.double_press_ms),
                   tap_replay_ms=int(self.tap_replay_ms),
                   repeat_ms=int(self.repeat_ms),
                   haptic_ack=bool(self.haptic_ack),
                   haptic_strength=int(self.haptic_strength),
                   stick_mouse_in_chord=bool(self.stick_mouse_in_chord),
                   off_timer_minutes=float(self.off_timer_minutes),
                   battery=self.battery.to_dict(),
                   lightbar=self.lightbar.to_dict(),
                   remote=self.remote.to_dict(),
                   gestures=self.gestures.to_dict())
        return out


# ---------------------------------------------------------------------------
# the `notifications` section: which tray balloons are shown
# ---------------------------------------------------------------------------
#
# Every balloon the tray shows carries a category; `tray._on_event` looks the
# category up here before calling `_notify`. `enabled` is the master switch.
# The categories are the vocabulary of the `toast` child event
# (`<category>|<title>|<body>`, see `intercept.InputInterceptor.on_toast`)
# and of the dashboard's Notifications card.

NOTIFICATION_CATEGORIES = ("battery_low", "remote_mode", "keyboard",
                           "connection", "hide", "update")
_NOTIFY_KNOWN = ("enabled",) + NOTIFICATION_CATEGORIES


@dataclass
class Notifications:
    enabled: bool = True
    #: The once-per-threshold low-battery toasts (`manager.LowBatteryAlerts`).
    battery_low: bool = True
    #: "Remote mode on/off" when the pad flips modes.
    remote_mode: bool = True
    #: "On-screen keyboard opened/closed". Off: the keyboard is on screen,
    #: the balloon would only repeat it.
    keyboard: bool = False
    #: A controller going offline / vanishing / being detached.
    connection: bool = True
    #: HidHide cloak applied or lifted.
    hide: bool = True
    #: "Update available" from the background check.
    update: bool = True
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: object) -> "Notifications":
        if not isinstance(data, dict):
            if data is not None:
                log.warning("'notifications' is %s, not an object -- using "
                            "defaults", type(data).__name__)
            return cls()
        n = cls()
        for key in _NOTIFY_KNOWN:
            setattr(n, key, _as_bool(data.get(key), getattr(n, key)))
        n.extra = {k: v for k, v in data.items() if k not in _NOTIFY_KNOWN}
        return n

    def to_dict(self) -> dict:
        out = dict(self.extra)
        out.update({k: bool(getattr(self, k)) for k in _NOTIFY_KNOWN})
        return out

    def allows(self, category: str) -> bool:
        """Should a balloon of `category` be shown? Unknown categories (an
        engine newer than this tray) are allowed by the master switch alone;
        `show_battery` -- category "battery" -- is gated only by it too."""
        if not self.enabled:
            return False
        if category in NOTIFICATION_CATEGORIES:
            return bool(getattr(self, category))
        return True


_CFG_KNOWN = ("enabled", "autostart_on_login", "auto_bridge_new", "port_base",
              "controllers", "hide_bluetooth_default", "hidhide_cli",
              "dashboard_port", "update_check", "update_auto_install", "input",
              "notifications")


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
    # -- SERVICE region (update.py's auto-install; see its docstring) --------
    #: When a check finds a newer release, install it without a click: the
    #: updater downloads the installer, verifies it against the release's
    #: SHA256SUMS and runs it silently a minute later; the tray (or the
    #: service) restarts itself on the new version. False keeps the old
    #: "Install update" menu row / dashboard button as the only way. Nothing
    #: without `update_check`.
    update_auto_install: bool = True
    # -- end SERVICE region ---------------------------------------------------
    #: Keyed by LOWERCASED bdaddr, e.g. "d42f4ba1485d".
    controllers: dict = field(default_factory=dict)
    #: The chord/shortcut engine (PS-button chords, touch gestures, remote
    #: mode, idle off-timer, battery lightbar alerts). Global, not
    #: per-controller: a chord means the same thing on every pad.
    input: InputConfig = field(default_factory=InputConfig)
    #: Which tray balloons are shown (`Notifications`).
    notifications: Notifications = field(default_factory=Notifications)
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
            update_auto_install=_as_bool(data.get("update_auto_install"), True),
            input=InputConfig.from_dict(data.get("input")),
            notifications=Notifications.from_dict(data.get("notifications")),
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
                   update_auto_install=bool(self.update_auto_install),
                   input=self.input.to_dict(),
                   notifications=self.notifications.to_dict(),
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
