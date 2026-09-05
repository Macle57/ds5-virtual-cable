"""The chord/shortcut engine: PS-button chords, touch gestures, a remote mode,
an idle off-timer and lightbar battery management -- all BEFORE the game.

Where it sits
-------------
`BridgeBackend` (emulator/ds5emu/bridge.py) calls three methods on whatever is
assigned to its `interceptor` attribute; this module is the real implementation
and `attach_to_backend()` is how the app layer wires it in:

    on_input(usb01)         reader thread, ~476 Hz, one call per decoded BT
                            control payload. What this returns is the ONLY
                            input state the game can ever see -- the interrupt
                            endpoint and control GET_REPORT both serve it. So
                            "consume a chord" is nothing more than returning
                            the report with those bits cleared.
    rewrite_setstate(body)  writer thread, just before a host SetState goes on
                            the air. Lightbar dimming, battery flashes and the
                            remote-mode colour are applied HERE, which is why
                            the game's own idea of its lightbar is never wrong
                            -- its bytes are rewritten in flight, not answered
                            differently.
    tick()                  writer thread, at least every 50 ms, traffic or
                            not. Drives everything time-based: the tail of a
                            haptic pulse, the tap-replay decision, battery
                            flashes, the idle off-timer.

The engine talks BACK to the pad through two bridge callbacks: `send_setstate`
(one engine-built SetState body -- valid-flag discipline keeps it from touching
anything the game owns) and `power_off` (the captured feature-0x08 mechanism,
see `BridgeBackend.power_off_pad`).

The chord button (default PS)
-----------------------------
While it is held, EVERYTHING digital is swallowed: buttons, dpad, touchpad.
Triggers and motion still pass -- no chord is built from them. The sticks are
LENT TO THE OS by default (`stick_mouse_in_chord`): left stick moves the
pointer, right stick scrolls, with the very same `remote.*` speeds as remote
mode, and the game sees them centred for the duration of the hold -- the
"major controls" never change meaning between a held chord and remote mode.
The cost is a camera frozen mid-aim while the user reaches for a volume
chord; a user who hates that flips `stick_mouse_in_chord` off and the sticks
pass through untouched again. What the game sees at chord start is therefore
nothing at all: the chord button itself is masked from the first report. A
plain tap still works because it is REPLAYED -- if the button comes back up
quickly with no chord fired, the engine waits out the double-press window (a
second press in that window toggles remote mode instead) and then holds the
button down for `tap_replay_ms` in the forwarded reports. The game gets a
slightly late PS press; the PS menu does not care.

Remote mode
-----------
Double-press the chord button (only when `remote.enabled` -- it ships OFF and
is switched on from the dashboard). The game is handed a neutral pad
(centred, released, touch up, motion zeroed -- battery/status bytes intact,
games read those); the pad drives the OS instead. Two layers:

INTRINSIC -- what remote mode IS, not configurable:

    touchpad 1-finger drag      move the mouse pointer
    touchpad 1-finger tap       left click
    touchpad 2-finger tap       right click
    left stick                  move the pointer (rate)
    right stick                 scroll (rate; scrolling is the sticks' and
                                triggers' job -- the touchpad does not scroll)
    L2 / R2                     scroll up / down (analog rate)

BOUND -- every button and the three 2-finger gestures resolve through a
binding table, `self._remote_table` (`_adopt_cfg`):

    remote.same_bindings = True    the `input.chords` table. A button or a
      (default)                    gesture means in remote mode exactly what
                                   it means with the chord button held --
                                   Cross = play/pause, dpad = volume/track,
                                   2-finger slide = Alt-Tab -- so there is
                                   one table to maintain.
    remote.same_bindings = False   `input.remote.chords`, merged over
                                   `config.DEFAULT_REMOTE_CHORDS`: the
                                   classic remote map -- Cross (hold = drag)
                                   = left mouse button, Circle = Esc, Options
                                   = Enter, dpad = arrow keys (repeat while
                                   held), 2-finger slide = Alt-Tab hold.

A bound row fires directly, no chord button held; a row mapped to "none" does
nothing in remote mode (the intrinsic layer above is never a row). Actions
whose `ActionSpec.hold` is set are HELD for the press (a mouse button drags,
Esc stays down); tap actions fire on the press and, if repeatable, again at
`repeat_ms` while held. Remote-mode button actions carry no haptic ack --
a click that rumbles is a click people stop making -- gestures ack as they
do under a chord.

Chords stay active in remote mode. Feedback on switch: a distinct double
haptic pulse and the lightbar held at `remote.lightbar_color` (single pulse
and the game's colour back on exit). Every pulse's amplitude comes from
`haptic_strength` (0-100, mapped linearly onto the motor byte).

The on-screen keyboard
----------------------
The `keyboard` engine action (default chord: PS + touchpad click) toggles
`osk.OnScreenKeyboard`. While it is open the game sees the same neutral pad
as in remote mode and every report goes to the keyboard's `PadDriver`
instead of remote mode's map -- whether or not remote mode is on. Circle or
Options on the pad close it, so do the engine turning off and `close()`.
The engine reports `remote_mode` and `keyboard_open` (attributes read by
the telemetry publisher) and calls `on_mode(remote, keyboard)` whenever
either flips, which is how the child's status line and the dashboard learn.

Threading
---------
`on_input` and `tick`/`rewrite_setstate` run on two different bridge threads;
a third -- `service.InputConfigWatcher`'s poll thread -- calls
`update_config` when the dashboard saves new settings. One lock guards all
mutable state and every critical section is bit-twiddling only. Nothing slow
runs under the lock or on the reader thread: registry actions (one of which
shells out to PowerShell) go through a single dispatch worker; only the
remote-mode `SendInput` primitives -- microseconds -- run inline, and the
keyboard window is only ever poked through thunks (a `PostMessage`). An
injectable `clock` makes every timing rule unit-testable.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque

from . import _bootstrap  # noqa: F401  (sys.path side effect)
from . import config as K
from . import osk as OSK
from .actions import OsActions

from ds5bridge import protocol as P  # noqa: E402  (stdlib-only module)

log = logging.getLogger("ds5app.intercept")

O = P.OFFSETS_USB

# --- report geometry (offsets into the 63-byte body, report id stripped) -----

#: name -> (byte offset from `digital_keys`, bit mask)
BUTTON_BITS = {
    "square": (0, 0x10), "cross": (0, 0x20), "circle": (0, 0x40),
    "triangle": (0, 0x80),
    "l1": (1, 0x01), "r1": (1, 0x02),
    "create": (1, 0x10), "options": (1, 0x20), "l3": (1, 0x40), "r3": (1, 0x80),
    "ps": (2, 0x01), "touchpad_click": (2, 0x02), "mute": (2, 0x04),
}

#: The dpad low nibble is a hat switch; 8 = nothing pressed. Diagonals count
#: as both components, so PS+up fires from NE and NW too.
DPAD_RELEASED = 8
_HAT_DIRS = ({"dpad_up"}, {"dpad_up", "dpad_right"}, {"dpad_right"},
             {"dpad_right", "dpad_down"}, {"dpad_down"},
             {"dpad_down", "dpad_left"}, {"dpad_left"},
             {"dpad_left", "dpad_up"}, set())

STICK_CENTER = 0x80
#: |stick - centre| below this is noise, not activity and not remote motion.
STICK_DEADZONE = 12
TRIGGER_DEADZONE = 8

# --- gesture tuning (touchpad units: 1920 x 1080 points) ---------------------

#: Horizontal 2-finger travel that opens the Alt-Tab switcher, and per further
#: step through it.
ALT_TAB_START_PX = 150
ALT_TAB_STEP_PX = 150
#: Vertical 2-finger travel for Task View / minimize-all.
SWIPE_PX = 200
#: A touch shorter than this that moved less than TAP_MOVE_PX is a tap.
TAP_S = 0.25
TAP_MOVE_PX = 40

# --- remote-mode tuning ------------------------------------------------------

#: Touchpad points per mouse pixel, before `remote.mouse_speed`.
MOUSE_GAIN = 1.0
#: Full-deflection stick speed, px per second, before `mouse_speed`.
STICK_MOUSE_PX_S = 900.0
#: Full-deflection scroll speed, wheel notches (120 units) per second.
STICK_SCROLL_NOTCH_S = 6.0
TRIGGER_SCROLL_NOTCH_S = 8.0
#: Re-trigger cadence for a held dpad arrow in remote mode.
ARROW_REPEAT_S = 0.30

# --- pad feedback ------------------------------------------------------------

ACK_PULSE_S = 0.12
FLASH_BLINK_ON_S = 0.15
FLASH_BLINK_PERIOD_S = 0.30
#: While remote mode holds the lightbar, the colour claim is repeated on this
#: cadence. One engine write can be lost in flight (the GameInput gate has
#: been seen interfering with pad traffic while a client owns the foreground,
#: and a game may overwrite in a gap we cannot see), so the LED is allowed to
#: be wrong for at most this long.
REMOTE_LIGHTBAR_REASSERT_S = 1.0


def _touch_point(body: bytes, base: int) -> tuple[bool, int, int, int]:
    """(active, id, x, y) from 4 bytes of touch data."""
    raw = body[base]
    x = ((body[base + 2] & 0x0F) << 8) | body[base + 1]
    y = (body[base + 3] << 4) | (body[base + 2] >> 4)
    return (raw & 0x80) == 0, raw & 0x7F, x, y


class _Frame:
    """One decoded input report -- only the fields the engine reads.

    Deliberately not `protocol.decode_input`: that builds dataclasses and
    unpacks the IMU on every call, and this runs at ~476 Hz on the Bluetooth
    reader thread. This is a handful of indexed loads.
    """

    __slots__ = ("buttons", "dpad", "lx", "ly", "rx", "ry", "l2", "r2",
                 "t0", "t1", "battery_percent", "discharging")

    def __init__(self, body: bytes):
        d = O.digital_keys
        k0, k1, k2 = body[d], body[d + 1], body[d + 2]
        held = set()
        for name, (off, mask) in BUTTON_BITS.items():
            if body[d + off] & mask:
                held.add(name)
        self.dpad = k0 & 0x0F
        held |= _HAT_DIRS[self.dpad] if self.dpad < 9 else set()
        self.buttons = held
        self.lx, self.ly = body[O.stick_lx], body[O.stick_ly]
        self.rx, self.ry = body[O.stick_rx], body[O.stick_ry]
        self.l2, self.r2 = body[O.trigger_l], body[O.trigger_r]
        self.t0 = _touch_point(body, O.touch_data)
        self.t1 = _touch_point(body, O.touch_data + 4)
        status0 = body[O.status0]
        self.battery_percent = min(100, (status0 & 0x0F) * 10)
        self.discharging = ((status0 & 0xF0) >> 4) == 0x00

    def stick_active(self) -> bool:
        return any(abs(v - STICK_CENTER) > STICK_DEADZONE
                   for v in (self.lx, self.ly, self.rx, self.ry))

    def is_active(self) -> bool:
        """User activity, for the idle timer. IMU is excluded on purpose --
        gyro noise never sleeps, and a pad face-down on the couch must idle."""
        return bool(self.buttons) or self.stick_active() \
            or self.l2 > TRIGGER_DEADZONE or self.r2 > TRIGGER_DEADZONE \
            or self.t0[0] or self.t1[0]


class _Dispatcher:
    """One daemon worker for registry actions, so PowerShell never runs on the
    Bluetooth reader thread. Bounded: a wedged action drops later ones rather
    than growing memory."""

    def __init__(self):
        self._q: deque = deque(maxlen=32)
        self._event = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __call__(self, fn) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run,
                                            name="ds5-input-actions", daemon=True)
            self._thread.start()
        self._q.append(fn)
        self._event.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            self._event.wait(0.5)
            self._event.clear()
            while True:
                try:
                    fn = self._q.popleft()
                except IndexError:
                    break
                try:
                    fn()
                except Exception:  # noqa: BLE001
                    log.exception("action failed")

    def close(self) -> None:
        self._stop.set()
        self._event.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None


class InputInterceptor:
    """See the module docstring. Construct, then hand to the bridge
    (`backend.interceptor = engine`, or use `attach_to_backend`)."""

    def __init__(self, cfg: K.InputConfig, *, actions: OsActions,
                 send_setstate, power_off,
                 clock=time.monotonic, dispatch=None,
                 on_mode=None, osk=None):
        self.actions = actions
        self.send_setstate = send_setstate
        self.power_off = power_off
        self.clock = clock
        self._dispatcher = _Dispatcher() if dispatch is None else None
        self.dispatch = dispatch or self._dispatcher
        #: `on_mode(remote_mode, keyboard_open)`, called (outside the lock)
        #: whenever either flips -- the child's status channel.
        self.on_mode = on_mode
        self._mode_sent: tuple | None = None
        #: The on-screen keyboard. Lazy about its window: nothing is drawn
        #: (no thread started) until the `keyboard` action first fires.
        self._osk = osk if osk is not None else OSK.OnScreenKeyboard(actions)

        self._adopt_cfg(cfg)

        self._lock = threading.Lock()
        self._prev: _Frame | None = None

        # -- chord state ----------------------------------------------------
        self._chord_down = False
        self._chord_down_at = 0.0
        self._chord_used = False
        self._pending_tap_at: float | None = None
        self._replay_until = 0.0
        #: (chord key, next repeat due) while a repeatable chord is held.
        self._repeating: tuple[str, float] | None = None
        self._warned_actions: set[str] = set()

        # -- gesture state (chord held) --------------------------------------
        self._g2_active = False
        self._g2_start: tuple[float, float] = (0.0, 0.0)
        self._g2_decided: str | None = None
        #: Shared by the chord-held and remote-mode alt-tab trackers; never
        #: both live at once (a chord press ends the remote gesture first).
        self._alt_anchor = 0.0

        # -- remote mode ------------------------------------------------------
        self.remote_mode = False
        self._rm_touch_at = 0.0            # when the current contact began
        self._rm_touch_start: tuple[float, float] = (0.0, 0.0)
        self._rm_touch_last: tuple[float, float] = (0.0, 0.0)
        self._rm_touch_moved = 0.0
        self._rm_touch_fingers = 0
        self._rm_touch_max_fingers = 0
        self._rm_mouse_frac = [0.0, 0.0]
        self._rm_scroll_acc = 0.0          # vertical wheel units pending
        #: Buttons remote mode is holding something for, keyed by chord-key
        #: name: {"release": callable|None, "next": repeat due|None,
        #: "repeat": callable|None}. A `hold` action keeps its release here
        #: so a mode change or a chord press can let go of it.
        self._rm_held: dict = {}
        self._rm_needs_release = False
        #: When the remote lightbar colour is next re-asserted (tick()).
        self._rm_lightbar_next = 0.0
        #: 2-finger gesture per contact: None = undecided, "alt_tab" = the
        #: switcher is open and stepping. Nothing else -- the touchpad
        #: deliberately does not scroll.
        self._rm2_decided: str | None = None
        self._rm2_start: tuple[float, float] = (0.0, 0.0)

        # -- rate translation (sticks/triggers -> pointer/scroll) -------------
        #: Timestamp of the last rate-translated report, shared between remote
        #: mode and the chord-held stick translation -- the two are mutually
        #: exclusive per report and use identical dt semantics.
        self._rate_last_at: float | None = None

        # -- effects / lightbar / battery / idle ------------------------------
        self._fx: list[tuple[float, bytes]] = []
        self._game_lightbar: tuple[int, int, int] | None = None
        self._flash_until = 0.0
        self._flash_color = (0, 0, 0)
        self._next_flash_at = 0.0
        self._started_at: float | None = None
        self._dim_engaged = False
        #: PS+<chord> "pad_lightbar_toggle": the LED is held dark until the
        #: next toggle. Runtime state, never persisted -- a fresh session
        #: starts lit, like the pad itself.
        self._lightbar_off = False
        self._last_activity: float | None = None
        self._idle_fired = False

        self.stats = {"chords_fired": 0, "chord_repeats": 0,
                      "gestures_fired": 0, "taps_replayed": 0,
                      "remote_toggles": 0, "remote_actions": 0,
                      "battery_flashes": 0, "idle_power_off": 0,
                      "lightbar_toggles": 0, "keyboard_toggles": 0}

    @property
    def keyboard_open(self) -> bool:
        return bool(self._osk is not None and self._osk.is_open)

    def _adopt_cfg(self, cfg: K.InputConfig) -> None:
        """Every field derived from the config, recomputed as one unit.

        Both the constructor and `update_config` come through here, so a hot
        swap can never miss a derived field that a later feature adds -- the
        bug this prevents (a new `cfg`-derived cache updated in `__init__`
        only) would be invisible until somebody changed that one setting live.
        Callers other than the constructor hold the engine lock.
        """
        self.cfg = cfg
        self.chord_button = cfg.chord_button
        self._double_press_s = cfg.double_press_ms / 1000.0
        self._tap_replay_s = cfg.tap_replay_ms / 1000.0
        self._repeat_s = cfg.repeat_ms / 1000.0
        self._off_timer_s = cfg.off_timer_minutes * 60.0
        #: `haptic_strength` is a percentage; the motor wants a byte. 100 maps
        #: to 0xFF linearly, so the default 25 lands at 0x40 -- felt, never
        #: startling. `haptic_ack` stays the on/off switch.
        self._ack_rumble = max(0, min(0xFF,
                                      round(cfg.haptic_strength * 255 / 100)))
        #: What a chord name resolves to: the built-ins, then the user's
        #: macros. A macro may not shadow a built-in -- "volume_up" must mean
        #: the same thing in every config -- and a malformed one is skipped
        #: by `macro_spec` with a log line, never fatal.
        registry = self.actions.registry()
        for name, macro in cfg.macros.items():
            if name in registry:
                log.warning("macro %r shadows a built-in action -- ignored",
                            name)
                continue
            spec = self.actions.macro_spec(name, macro)
            if spec is not None:
                registry[name] = spec
        self.registry = registry
        #: What a button or gesture does in REMOTE mode (module docstring,
        #: "Remote mode"): the chord table itself when `same_bindings`, else
        #: the remote section's own table. Both dicts are already merged over
        #: their defaults by config.py; this is only a choice between them.
        rm = cfg.remote
        self._remote_table = cfg.chords if rm.same_bindings else rm.chords

    def update_config(self, new_cfg: K.InputConfig) -> None:
        """Adopt a changed config on a RUNNING engine, no bridge restart.

        This is the other end of the dashboard's Save button: the settings
        panel writes config.json, `service.InputConfigWatcher` notices, and
        the new `input` section lands here within a couple of seconds --
        because a user who drags `double_press_ms` and feels no difference
        concludes the feature is broken, not that a restart is owed.

        The swap itself is a handful of assignments under the engine lock.
        The care is all in the TRANSITIONS -- three ways the OLD config can
        have left something held or masked that the NEW config will never
        release on its own:

          * remote mode is on and the new config forbids it (`remote.enabled`
            off, or the whole section off): leave through the SAME exit the
            double-press takes, so the pad gets its goodbye pulse and the
            game gets its lightbar back -- and anything remote mode holds on
            the OS (a dragged mouse button, a held arrow, a mid-slide
            Alt-Tab) is let go HERE, not "on the next report", because a
            disabled engine never processes another report;
          * the chord button changed (or the section turned off) while a
            chord hold was masking the pad: commit any open gesture and drop
            every bit of hold state, so the very next report shows the game
            an honest pad -- the old button, still physically held, simply
            reappears as pressed;
          * the section turned off entirely: the engine stays attached and
            becomes a pure passthrough (see `on_input`). Passthrough rather
            than detach, deliberately: `BridgeBackend.interceptor` is read by
            the reader thread mid-stream, and hot-unhooking it swaps
            correctness for a teardown/attach dance nobody needs when "do
            nothing per report" costs one attribute read. It also makes
            re-enabling later a plain state change instead of a rebuild.

        Held-key releases run OUTSIDE the lock, exactly like `on_input`'s
        thunks -- `SendInput` is microseconds, but nothing slow or foreign
        runs under the engine lock, ever.
        """
        now = self.clock()
        thunks: list = []
        with self._lock:
            was_enabled = bool(self.cfg.enabled)
            old_button = self.chord_button
            self._adopt_cfg(new_cfg)
            disabled = not new_cfg.enabled
            chord_changed = new_cfg.chord_button != old_button
            leave_remote = self.remote_mode and (disabled
                                                 or not new_cfg.remote.enabled)

            if leave_remote:
                # The regular exit path: goodbye pulse, the game's lightbar
                # restored via `_fx` (tick drains those even while disabled).
                self._toggle_remote(now)
            if disabled or leave_remote:
                # `_toggle_remote` defers the release to the next report; a
                # config change cannot wait for one -- do it now.
                self._rm_needs_release = False
                self._remote_release_held(thunks)
            if disabled and self.keyboard_open:
                # A disabled engine processes no reports, so nothing would
                # ever close the keyboard again -- and the game's buttons are
                # back, so a keyboard that still typed would double every press.
                self._keyboard_toggle(thunks)
            if disabled or chord_changed:
                self._end_gesture(thunks)     # commits a mid-slide Alt-Tab
                self._chord_down = False
                self._repeating = None
                self._pending_tap_at = None
                self._replay_until = 0.0
            if disabled:
                # A dimmed or held-off lightbar must not outlive the engine
                # that did it; queue the game's own colour back before going
                # quiet.
                restore = None
                if self._lightbar_off:
                    restore = self._game_lightbar or P.DEFAULT_LIGHTBAR
                elif self._dim_engaged and self._game_lightbar is not None:
                    restore = self._game_lightbar
                if restore is not None:
                    self._fx.append((now, self._lightbar_body(restore)))
                    self._fx.sort(key=lambda e: e[0])
                self._dim_engaged = False
                self._lightbar_off = False
            if not was_enabled and not disabled:
                # Re-enabled: a fresh session. The idle clock in particular
                # must restart from the next report -- `_last_activity` froze
                # the moment passthrough began, and honouring the stale value
                # would power the pad off seconds after the user turned the
                # feature back ON.
                self._started_at = None
                self._last_activity = None
                self._idle_fired = False
                self._dim_engaged = False
                self._prev = None
                self._rate_last_at = None
            self._note_mode(thunks)
        for fn in thunks:
            try:
                fn()
            except Exception:  # noqa: BLE001
                log.exception("config-change release failed")

    # =====================================================================
    # bridge contract
    # =====================================================================

    def on_input(self, report: bytes) -> bytes:
        if len(report) < 1 + O.status0 + 1 or report[0] != 0x01:
            return report
        # Disabled means PASSTHROUGH, not detached (see `update_config` for
        # why passthrough beats unhooking). `update_config` already released
        # everything and cleared all hold state before flipping the flag, so
        # returning untouched is honest from the first disabled report. The
        # unlocked read keeps the ~476 Hz steady state allocation-free; the
        # re-check under the lock below closes the swap race.
        if not self.cfg.enabled:
            return report
        now = self.clock()
        body = bytearray(report[1:])
        frame = _Frame(body)
        thunks: list = []

        with self._lock:
            if not self.cfg.enabled:
                # The swap landed between the fast check and here: this report
                # must not arm a chord on state `update_config` just cleared.
                return report
            if self._started_at is None:
                self._started_at = now
                self._last_activity = now
            if frame.is_active():
                self._last_activity = now
                self._idle_fired = False

            self._chord_edges(frame, now, thunks)
            if self._chord_down:
                self._chord_held(frame, now, thunks)
                self._chord_gestures(frame, now, thunks)
                if self.cfg.stick_mouse_in_chord:
                    # The sticks drive the OS during the hold, same speeds as
                    # remote mode. Deflecting one IS using the chord -- no tap
                    # replay afterwards, or the game would get a phantom PS
                    # press right after the user finished mousing.
                    if frame.stick_active():
                        self._chord_used = True
                    self._stick_rates(frame, now, thunks, triggers=False)

            if self._rm_needs_release:
                self._rm_needs_release = False
                self._remote_release_held(thunks)

            if self.keyboard_open:
                # The keyboard owns the pad: buttons and sticks steer it,
                # remote mode's map is suspended (anything it held is let
                # go), and the game sees nobody touching the pad.
                self._remote_release_held(thunks)
                if not self._chord_down:
                    thunks.extend(self._osk.on_frame(frame, now))
                self._neutralize(body)
            elif self.remote_mode:
                if not self._chord_down:
                    self._remote_inputs(frame, now, thunks)
                else:
                    self._remote_release_held(thunks)
                self._neutralize(body)
            elif self._chord_down:
                self._mask_digital(body)
                if self.cfg.stick_mouse_in_chord:
                    self._center_sticks(body)
            if self._replay_until and now < self._replay_until:
                # The delayed chord-button tap: hold it down for the game.
                self._press_button(body, self.chord_button)
            self._prev = frame
            self._note_mode(thunks)

        for fn in thunks:
            try:
                fn()
            except Exception:  # noqa: BLE001
                log.exception("input action failed")
        return bytes([0x01]) + bytes(body)

    def rewrite_setstate(self, setstate_body: bytes) -> bytes:
        """Writer thread: the last word on what the pad's lightbar shows."""
        body = bytearray(setstate_body)
        if len(body) < P.SETSTATE_BODY_LEN:
            body.extend(bytes(P.SETSTATE_BODY_LEN - len(body)))
        now = self.clock()
        with self._lock:
            has_lightbar = bool(body[P.VALID_FLAG1] & P.F1_LIGHTBAR_CONTROL)
            if has_lightbar:
                # Remember what the game wants -- restores and flash gaps
                # return to THIS, not to darkness. Learnt even while disabled,
                # so a re-enabled engine restores the CURRENT colour, not the
                # one from before the feature was switched off.
                self._game_lightbar = (body[P.LED_R], body[P.LED_G], body[P.LED_B])
            if not self.cfg.enabled:
                # Passthrough: no dim, no flash, no remote colour -- the
                # game's bytes go to the pad as written.
                return bytes(body)
            rgb = self._effective_lightbar(now)
            if has_lightbar and rgb is not None:
                body[P.LED_R], body[P.LED_G], body[P.LED_B] = rgb
        return bytes(body)

    def tick(self) -> None:
        """Timers that must run with no input and no host traffic flowing."""
        now = self.clock()
        due: list[bytes] = []
        power_off = False
        with self._lock:
            enabled = bool(self.cfg.enabled)
            # tap replay: the double-press window expired with no second press
            if (enabled and self._pending_tap_at is not None
                    and now >= self._pending_tap_at):
                self._pending_tap_at = None
                self._replay_until = now + self._tap_replay_s
                self.stats["taps_replayed"] += 1
            # queued pad effects. Drained even while disabled: a disable
            # transition queues its own goodbyes (the remote-exit pulse, the
            # lightbar restores), and swallowing those would strand the pad
            # on the engine's colour. Once `_fx` is empty a disabled tick
            # does nothing at all -- no flashes, no dim, and above all no
            # idle power-off from a timer the user just switched off.
            while self._fx and self._fx[0][0] <= now:
                due.append(self._fx.pop(0)[1])
            if enabled:
                # remote colour re-assert: the LED is allowed to be wrong for
                # at most REMOTE_LIGHTBAR_REASSERT_S (a battery flash mid-burst
                # is never stomped -- it outranks the remote colour).
                if (self.remote_mode and now >= self._rm_lightbar_next
                        and now >= self._flash_until):
                    self._rm_lightbar_next = now + REMOTE_LIGHTBAR_REASSERT_S
                    due.append(self._lightbar_body(
                        self._effective_lightbar(now)
                        or tuple(self.cfg.remote.lightbar_color)))
                # battery flash
                if self._battery_flash_due(now):
                    due.extend(self._queue_flash_locked(now))
                # lightbar dim engages once, mid-play
                if (not self._dim_engaged and self._started_at is not None
                        and self.cfg.lightbar.dim_after_minutes > 0
                        and now - self._started_at
                        >= self.cfg.lightbar.dim_after_minutes * 60.0):
                    self._dim_engaged = True
                    rgb = self._effective_lightbar(now)
                    if rgb is not None:
                        due.append(self._lightbar_body(rgb))
                # idle off-timer
                if (self._off_timer_s > 0 and self._last_activity is not None
                        and not self._idle_fired
                        and now - self._last_activity >= self._off_timer_s):
                    self._idle_fired = True
                    self.stats["idle_power_off"] += 1
                    power_off = True
        for body in due:
            self._send(body)
        if power_off:
            log.info("no input for %.0f min -- powering the pad off",
                     self._off_timer_s / 60.0)
            self.power_off()

    def close(self) -> None:
        """Release anything held on the OS side (keys, the borrowed default
        microphone), take the keyboard down, stop the worker."""
        try:
            if self._osk is not None:
                self._osk.shutdown()
        except Exception:  # noqa: BLE001
            log.exception("closing the on-screen keyboard failed")
        try:
            self.actions.close()
        finally:
            if self._dispatcher is not None:
                self._dispatcher.close()

    def _note_mode(self, thunks: list) -> None:
        """Queue `on_mode` if remote/keyboard state differs from what was
        last reported. Under the lock; the call itself runs with the thunks."""
        state = (bool(self.remote_mode), self.keyboard_open)
        if state == self._mode_sent:
            return
        self._mode_sent = state
        if self.on_mode is not None:
            thunks.append(lambda s=state: self.on_mode(*s))

    # =====================================================================
    # chords
    # =====================================================================

    def _chord_edges(self, frame: _Frame, now: float, thunks: list) -> None:
        was = self._prev.buttons if self._prev is not None else set()
        down_now = self.chord_button in frame.buttons
        if down_now and self.chord_button not in was:
            # `_pending_tap_at` is set only by an UNUSED short tap awaiting its
            # replay decision -- so "a second press while it is pending" IS the
            # double press, exactly. A press after a chord fired can never
            # toggle by accident: a used press schedules nothing.
            if (self.cfg.remote.enabled and self._pending_tap_at is not None
                    and now <= self._pending_tap_at):
                self._pending_tap_at = None
                self._chord_used = True
                self._toggle_remote(now)
            else:
                self._chord_used = False
            self._chord_down = True
            self._chord_down_at = now
            self._repeating = None
        elif not down_now and self.chord_button in was:
            if self._chord_down:
                self._end_gesture(thunks)
                tap = (not self._chord_used
                       and now - self._chord_down_at <= self._double_press_s)
                if tap and self.cfg.remote.enabled:
                    # Wait out the double-press window before replaying, so a
                    # remote-mode toggle never leaks half a chord-button tap.
                    self._pending_tap_at = now + self._double_press_s
                elif tap:
                    self._replay_until = now + self._tap_replay_s
                    self.stats["taps_replayed"] += 1
            self._chord_down = False
            self._repeating = None

    def _chord_held(self, frame: _Frame, now: float, thunks: list) -> None:
        was = self._prev.buttons if self._prev is not None else set()
        for key in sorted(frame.buttons - was):
            if key == self.chord_button:
                continue
            if self._fire_chord(key, now, thunks, first=True):
                break                     # one new chord per report is plenty
        # held repeatable chord (volume ramp, brightness ramp)
        rep = self._repeating
        if rep is not None:
            key, due = rep
            if key not in frame.buttons:
                self._repeating = None
            elif now >= due:
                self._repeating = (key, now + self._repeat_s)
                self.stats["chord_repeats"] += 1
                self._fire_chord(key, now, thunks, first=False)

    def _fire_chord(self, key: str, now: float, thunks: list,
                    first: bool) -> bool:
        name = self.cfg.chords.get(key)
        if not name:
            return False
        self._chord_used = True
        if name in K.ENGINE_ACTIONS:
            if first:
                self.stats["chords_fired"] += 1
                self._ack_locked(now)
                self._engine_action(name, now, thunks)
            return True
        spec = self.registry.get(name)
        if spec is None:
            if name not in self._warned_actions:
                self._warned_actions.add(name)
                log.warning("chord %r names unknown action %r -- ignored",
                            key, name)
            return False
        params = self.cfg.actions.get(name) or {}
        if first:
            self.stats["chords_fired"] += 1
            if self.cfg.haptic_ack:
                self._ack_locked(now)
            if spec.repeatable:
                self._repeating = (key, now + max(self._repeat_s, 0.25))
        thunks.append(lambda run=spec.run: self.dispatch(lambda: run(params)))
        return True

    # =====================================================================
    # chord-held touchpad gestures
    # =====================================================================

    @staticmethod
    def _centroid(frame: _Frame) -> tuple[int, tuple[float, float]]:
        pts = [(t[2], t[3]) for t in (frame.t0, frame.t1) if t[0]]
        if not pts:
            return 0, (0.0, 0.0)
        return len(pts), (sum(p[0] for p in pts) / len(pts),
                          sum(p[1] for p in pts) / len(pts))

    def _chord_gestures(self, frame: _Frame, now: float, thunks: list) -> None:
        fingers, c = self._centroid(frame)
        if fingers < 2:
            if self._g2_active:
                self._end_gesture(thunks)
            return
        if not self._g2_active:
            self._g2_active = True
            self._g2_start = c
            self._g2_decided = None
            return
        dx = c[0] - self._g2_start[0]
        dy = c[1] - self._g2_start[1]
        if self._g2_decided is None:
            decided = self._decide_two_finger(dx, dy, now, thunks,
                                              self.cfg.chords)
            if decided is not None:
                self._g2_decided = decided
                self._chord_used = True
        elif self._g2_decided == "alt_tab":
            self._alt_hold_track(dx, thunks)

    def _decide_two_finger(self, dx: float, dy: float, now: float,
                           thunks: list, table: dict):
        """Classify a 2-finger travel against `table` and fire it.

        -> "alt_tab" (the switcher is open and `_alt_hold_track` steps it),
        "fired" (a one-shot gesture went out, this contact is spent), or None
        (not decisive yet, or decisive but unbound -- stays undecided so a
        later, bound direction can still win). Shared by the chord-held
        tracker and remote mode, which differ only in the table.
        """
        if abs(dx) >= ALT_TAB_START_PX and abs(dx) > abs(dy):
            name = table.get("touch_slide_horizontal")
            if not name:
                return None
            self.stats["gestures_fired"] += 1
            if self.cfg.haptic_ack:
                self._ack_locked(now)
            if name == "alt_tab":
                # Hold semantics: the switcher stays open while the fingers
                # stay down; sliding steps through it; lifting them (or
                # releasing the chord button) commits.
                self._alt_hold_begin(dx, thunks)
                return "alt_tab"
            self._fire_gesture_action("touch_slide_horizontal", now, thunks,
                                      ack=False, table=table)
            return "fired"
        if abs(dy) >= SWIPE_PX and abs(dy) > abs(dx):
            key = "touch_swipe_up" if dy < 0 else "touch_swipe_down"
            if self._fire_gesture_action(key, now, thunks, table=table):
                self.stats["gestures_fired"] += 1
            return "fired"
        return None

    # -- the alt-tab hold tracker, shared with remote mode -------------------

    def _alt_hold_begin(self, dx: float, thunks: list) -> None:
        """Open the switcher mid-slide. The travel already made is the anchor,
        so the opening slide itself is step zero; a leftward opening means the
        user wants the OTHER direction, hence the two immediate back-steps
        (one to undo the Tab that opening implies, one to actually go back)."""
        self._alt_anchor = dx
        first_back = dx < 0
        thunks.append(lambda: self.actions.alt_tab_start())
        if first_back:
            thunks.append(lambda: self.actions.alt_tab_step(False))
            thunks.append(lambda: self.actions.alt_tab_step(False))

    def _alt_hold_track(self, dx: float, thunks: list) -> None:
        """One switcher step per further ALT_TAB_STEP_PX of travel, either way."""
        while dx - self._alt_anchor >= ALT_TAB_STEP_PX:
            self._alt_anchor += ALT_TAB_STEP_PX
            thunks.append(lambda: self.actions.alt_tab_step(True))
        while dx - self._alt_anchor <= -ALT_TAB_STEP_PX:
            self._alt_anchor -= ALT_TAB_STEP_PX
            thunks.append(lambda: self.actions.alt_tab_step(False))

    def _fire_gesture_action(self, key: str, now: float, thunks: list,
                             ack: bool = True, table: dict | None = None) -> bool:
        name = (self.cfg.chords if table is None else table).get(key)
        if not name:
            return False
        if name in K.ENGINE_ACTIONS:
            self._engine_action(name, now, thunks)
            return True
        spec = self.registry.get(name)
        if spec is None:
            if name not in self._warned_actions:
                self._warned_actions.add(name)
                log.warning("gesture %r names unknown action %r -- ignored",
                            key, name)
            return False
        if ack and self.cfg.haptic_ack:
            self._ack_locked(now)
        params = self.cfg.actions.get(name) or {}
        thunks.append(lambda run=spec.run: self.dispatch(lambda: run(params)))
        return True

    def _end_gesture(self, thunks: list) -> None:
        if self._g2_decided == "alt_tab":
            thunks.append(lambda: self.actions.alt_tab_commit())
        self._g2_active = False
        self._g2_decided = None

    def _engine_action(self, name: str, now: float, thunks: list) -> None:
        """The actions that act on the PAD rather than the OS (see
        `config.ENGINE_ACTIONS`). Called under the engine lock. Power-off is
        deferred to the thunk list (it talks to the bridge); the lightbar
        toggle only queues bytes, so it needs no thunk; the keyboard flips
        its state here and pokes its window from a thunk."""
        if name == "pad_power_off":
            thunks.append(self.power_off)
        elif name == "keyboard":
            self._keyboard_toggle(thunks)
        elif name == "pad_lightbar_toggle":
            self._lightbar_off = not self._lightbar_off
            self.stats["lightbar_toggles"] += 1
            # Show it NOW, host traffic or not: dark, or whatever the policy
            # stack says the pad should be showing again.
            rgb = (self._effective_lightbar(now)
                   or self._game_lightbar or P.DEFAULT_LIGHTBAR)
            self._fx.append((now, self._lightbar_body(rgb)))
            self._fx.sort(key=lambda e: e[0])

    def _keyboard_toggle(self, thunks: list) -> None:
        """Open or close the on-screen keyboard. Under the lock: the state
        flips now (this very report is neutralised on the strength of it);
        the window is shown/hidden by the thunk the keyboard hands back."""
        if self._osk is None:
            return
        fn = self._osk.toggle()
        self.stats["keyboard_toggles"] += 1
        if fn is not None:
            thunks.append(fn)

    # =====================================================================
    # remote mode
    # =====================================================================

    def _toggle_remote(self, now: float) -> None:
        self.remote_mode = not self.remote_mode
        self.stats["remote_toggles"] += 1
        log.info("remote mode %s", "ON" if self.remote_mode else "off")
        rm = self.cfg.remote
        if self.remote_mode:
            # Distinct feedback: double pulse + the remote lightbar colour.
            self._queue_pulse(now, count=2)
            self._fx.append((now, self._lightbar_body(
                self._effective_lightbar(now) or tuple(rm.lightbar_color))))
            self._rm_lightbar_next = now + REMOTE_LIGHTBAR_REASSERT_S
        else:
            self._queue_pulse(now, count=1)
            # No game colour yet -> the bridge's default blue, never "leave
            # it red": the pad shows whatever was written last.
            restore = self._effective_lightbar(now, ignore_remote=True) \
                or self._game_lightbar or P.DEFAULT_LIGHTBAR
            self._fx.append((now, self._lightbar_body(restore)))
            # Anything remote mode still holds (mouse button, an arrow key)
            # must be let go; done on the next report, which has the thunk list.
            self._rm_needs_release = True
        self._fx.sort(key=lambda e: e[0])

    def _remote_release_held(self, thunks: list) -> None:
        """Chord pressed (or mode left, or the keyboard opened) while remote
        mode held something: let go of all of it."""
        for key in list(self._rm_held):
            self._remote_release_key(key, thunks)
        if self._rm2_decided == "alt_tab":
            # A mid-slide switcher commits, exactly as lifting the fingers
            # would -- leaving Alt down across a mode change is the stuck-key
            # failure actions.py is built to avoid.
            thunks.append(lambda: self.actions.alt_tab_commit())
        self._rm2_decided = None
        self._rm_touch_fingers = 0
        self._rm_touch_max_fingers = 0

    def _remote_press_key(self, key: str, now: float, thunks: list) -> None:
        """A button went down in remote mode: fire what the table binds it to.

        `hold` actions press now and release with the button (`_rm_held`);
        tap actions dispatch once and, if repeatable, again at `repeat_ms`
        while held. Engine actions (the keyboard, pad power) behave as they
        do under a chord. No haptic ack for buttons here -- see the module
        docstring.
        """
        name = self._remote_table.get(key)
        if not name:
            return
        if name in K.ENGINE_ACTIONS:
            self.stats["remote_actions"] += 1
            self._engine_action(name, now, thunks)
            return
        spec = self.registry.get(name)
        if spec is None:
            if name not in self._warned_actions:
                self._warned_actions.add(name)
                log.warning("remote binding %r names unknown action %r -- "
                            "ignored", key, name)
            return
        self.stats["remote_actions"] += 1
        params = self.cfg.actions.get(name) or {}
        if spec.hold:
            press, release = spec.hold
            thunks.append(press)
            entry = {"release": release, "next": None, "repeat": None}
            if spec.repeatable:
                # Re-trigger like a held keyboard key: up then down again.
                entry["next"] = now + ARROW_REPEAT_S
                entry["repeat"] = lambda: (release(), press())
            self._rm_held[key] = entry
            return
        fire = lambda run=spec.run: self.dispatch(lambda: run(params))  # noqa: E731
        thunks.append(fire)
        if spec.repeatable:
            self._rm_held[key] = {"release": None,
                                  "next": now + max(self._repeat_s, 0.25),
                                  "repeat": fire}

    def _remote_release_key(self, key: str, thunks: list) -> None:
        entry = self._rm_held.pop(key, None)
        if entry is not None and entry["release"] is not None:
            thunks.append(entry["release"])

    def _remote_inputs(self, frame: _Frame, now: float, thunks: list) -> None:
        was = self._prev.buttons if self._prev is not None else set()
        a = self.actions
        rm = self.cfg.remote

        # -- buttons, through the binding table ------------------------------
        for key in sorted(was - frame.buttons):
            self._remote_release_key(key, thunks)
        dpad_held = any(k.startswith("dpad_") for k in self._rm_held)
        for key in sorted(frame.buttons - was):
            if key == self.chord_button:
                continue
            if key.startswith("dpad_"):
                # One direction at a time: rolling onto a diagonal must not
                # fire the second component as a fresh press.
                if dpad_held:
                    continue
                dpad_held = True
            self._remote_press_key(key, now, thunks)
        if self.keyboard_open:
            # A press just opened the keyboard: from here the pad is the
            # keyboard's, so whatever remote mode still holds lets go NOW,
            # not on the next report -- a drag must not outlive the mode.
            self._remote_release_held(thunks)
            return
        for key, entry in list(self._rm_held.items()):
            if entry["next"] is not None and now >= entry["next"]:
                entry["next"] = now + (ARROW_REPEAT_S if entry["release"]
                                       else self._repeat_s)
                thunks.append(entry["repeat"])

        # -- touchpad ---------------------------------------------------------
        fingers, c = self._centroid(frame)
        if fingers and not self._rm_touch_fingers:
            self._rm_touch_at = now
            self._rm_touch_start = c
            self._rm_touch_moved = 0.0
        if fingers:
            if fingers < 2 and self._rm2_decided == "alt_tab":
                # One finger left mid-slide: commit, as a full lift would.
                self._rm2_decided = None
                thunks.append(lambda: self.actions.alt_tab_commit())
            if fingers >= 2 and self._rm_touch_fingers < 2:
                # The second finger just landed: gesture travel counts from
                # HERE, so 1-finger mousing cannot pre-load an alt-tab.
                self._rm2_start = c
                self._rm2_decided = None
            if self._rm_touch_fingers:
                dx = c[0] - self._rm_touch_last[0]
                dy = c[1] - self._rm_touch_last[1]
                self._rm_touch_moved += abs(dx) + abs(dy)
                if fingers == 1 and self._rm_touch_max_fingers <= 1:
                    self._rm_mouse(dx, dy, rm.mouse_speed, thunks)
                elif fingers >= 2:
                    self._rm_two_finger(c, dx, dy, now, thunks)
            self._rm_touch_last = c
            self._rm_touch_max_fingers = max(self._rm_touch_max_fingers, fingers)
        elif self._rm_touch_fingers:
            # contact ended: an open switcher commits; otherwise, was it a tap?
            if self._rm2_decided == "alt_tab":
                thunks.append(lambda: self.actions.alt_tab_commit())
            elif (now - self._rm_touch_at <= TAP_S
                    and self._rm_touch_moved <= TAP_MOVE_PX):
                if self._rm_touch_max_fingers >= 2:
                    thunks.append(lambda: (a.mouse_button("right", True),
                                           a.mouse_button("right", False)))
                else:
                    thunks.append(lambda: (a.mouse_button("left", True),
                                           a.mouse_button("left", False)))
            self._rm2_decided = None
            self._rm_touch_max_fingers = 0
        self._rm_touch_fingers = fingers

        # -- sticks and triggers ----------------------------------------------
        self._stick_rates(frame, now, thunks, triggers=True)

    def _rm_two_finger(self, c, dx: float, dy: float, now: float,
                       thunks: list) -> None:
        """Remote-mode 2-finger movement: the same three gestures as under a
        chord (decisive horizontal slide, swipe up, swipe down), resolved
        through the remote table. The touchpad deliberately does NOT scroll
        -- scrolling belongs to the right stick and triggers, so a sloppy
        2-finger drag can never fight the gesture decision or nudge the page
        by accident."""
        tx = c[0] - self._rm2_start[0]
        ty = c[1] - self._rm2_start[1]
        if self._rm2_decided is None:
            self._rm2_decided = self._decide_two_finger(tx, ty, now, thunks,
                                                        self._remote_table)
        elif self._rm2_decided == "alt_tab":
            self._alt_hold_track(tx, thunks)

    def _stick_rates(self, frame: _Frame, now: float, thunks: list,
                     *, triggers: bool) -> None:
        """Rate-based stick translation: left stick moves the pointer, right
        stick scrolls, at `remote.*` speeds. This is remote mode's stick map,
        and it is also live while the chord button is held
        (`stick_mouse_in_chord`) -- the sticks mean the same thing in both.
        Triggers join the scroll only in remote mode; during a chord they
        still belong to the game."""
        dt = 0.0
        if self._rate_last_at is not None:
            dt = min(0.05, max(0.0, now - self._rate_last_at))
        self._rate_last_at = now
        if dt <= 0:
            return
        rm = self.cfg.remote
        mx = _stick_norm(frame.lx) * STICK_MOUSE_PX_S * rm.mouse_speed * dt
        my = _stick_norm(frame.ly) * STICK_MOUSE_PX_S * rm.mouse_speed * dt
        self._rm_mouse(mx, my, 1.0, thunks, gain=1.0)
        sv = _stick_norm(frame.ry) * STICK_SCROLL_NOTCH_S * 120 * dt
        if triggers:
            sv += ((frame.r2 - frame.l2) / 255.0
                   * TRIGGER_SCROLL_NOTCH_S * 120 * dt)
        self._rm_scroll(sv * rm.scroll_speed, thunks, gain=1.0)

    def _rm_mouse(self, dx: float, dy: float, speed: float, thunks: list,
                  gain: float = MOUSE_GAIN) -> None:
        fx, fy = self._rm_mouse_frac
        fx += dx * gain * speed
        fy += dy * gain * speed
        ix, iy = int(fx), int(fy)
        self._rm_mouse_frac = [fx - ix, fy - iy]
        if ix or iy:
            thunks.append(lambda: self.actions.mouse_move(ix, iy))

    def _rm_scroll(self, dv: float, thunks: list, gain: float = 1.0) -> None:
        """Accumulate vertical scroll from the right stick and triggers --
        the only scroll sources; the touchpad deliberately has none."""
        self._rm_scroll_acc += dv * gain
        # Stick pushed down scrolls the content down = wheel negative,
        # matching every Windows touchpad's default.
        whole = int(self._rm_scroll_acc / 40) * 40   # emit in 1/3-notch steps
        if whole:
            self._rm_scroll_acc -= whole
            thunks.append(lambda d=-whole: self.actions.wheel(d))

    # =====================================================================
    # masking / rewriting the forwarded report
    # =====================================================================

    def _mask_digital(self, body: bytearray) -> None:
        """Chord held: the game sees no buttons, no dpad, no touch. Triggers
        and motion still pass; the sticks pass too unless
        `stick_mouse_in_chord` has lent them to the OS (`_center_sticks`)."""
        d = O.digital_keys
        body[d] = DPAD_RELEASED           # hat neutral, face buttons cleared
        body[d + 1] = 0
        body[d + 2] = 0
        body[O.touch_data] |= 0x80
        body[O.touch_data + 4] |= 0x80

    def _center_sticks(self, body: bytearray) -> None:
        """Chord held with `stick_mouse_in_chord`: the sticks are the OS's for
        the duration, so the game must see them centred -- half a camera turn
        per volume chord is exactly the leak this exists to stop."""
        for off in (O.stick_lx, O.stick_ly, O.stick_rx, O.stick_ry):
            body[off] = STICK_CENTER

    def _neutralize(self, body: bytearray) -> None:
        """Remote mode: the game sees a pad nobody is touching. Battery and
        status bytes stay -- games display those."""
        self._center_sticks(body)
        body[O.trigger_l] = 0
        body[O.trigger_r] = 0
        self._mask_digital(body)
        for off in (O.gyro_pitch, O.gyro_yaw, O.gyro_roll):
            body[off] = 0
            body[off + 1] = 0

    def _press_button(self, body: bytearray, name: str) -> None:
        bit = BUTTON_BITS.get(name)
        if bit is not None:
            body[O.digital_keys + bit[0]] |= bit[1]

    # =====================================================================
    # pad feedback, lightbar policy, battery flash
    # =====================================================================

    def _send(self, body: bytes) -> None:
        try:
            self.send_setstate(body)
        except Exception:  # noqa: BLE001
            log.exception("send_setstate failed")

    @staticmethod
    def _rumble_body(strength: int) -> bytes:
        st = P.SetState()
        st.rumble(0, strength)
        return bytes(st.body)

    @staticmethod
    def _lightbar_body(rgb) -> bytes:
        st = P.SetState()
        st.lightbar(int(rgb[0]), int(rgb[1]), int(rgb[2]))
        return bytes(st.body)

    def _ack_locked(self, now: float) -> None:
        """One short rumble pulse. Queued, not sent inline -- the caller holds
        the lock and the send callback crosses a thread boundary anyway."""
        if not self.cfg.haptic_ack:
            return
        self._queue_pulse(now, count=1)

    def _queue_pulse(self, now: float, count: int) -> None:
        for i in range(count):
            t = now + i * (ACK_PULSE_S + 0.08)
            self._fx.append((t, self._rumble_body(self._ack_rumble)))
            self._fx.append((t + ACK_PULSE_S, self._rumble_body(0)))
        self._fx.sort(key=lambda e: e[0])

    def _effective_lightbar(self, now: float,
                            ignore_remote: bool = False):
        """What the pad's lightbar should show right now, or None for 'whatever
        the game says'. Priority: battery flash > held off > remote colour >
        dimmed game colour > None."""
        if now < self._flash_until:
            return self._flash_color
        if self._lightbar_off:
            return (0, 0, 0)
        if self.remote_mode and not ignore_remote:
            return tuple(self.cfg.remote.lightbar_color)
        if self._dim_engaged and self._game_lightbar is not None:
            lvl = max(0.0, min(1.0, self.cfg.lightbar.dim_level))
            return tuple(int(c * lvl) for c in self._game_lightbar)
        return None

    def _battery_flash_due(self, now: float) -> bool:
        b = self.cfg.battery
        if not b.enabled or self._battery_percent() is None:
            return False
        pct = self._battery_percent()
        if not self._discharging() or pct > b.low_percent:
            return False
        return now >= self._next_flash_at

    def _battery_percent(self):
        return self._prev.battery_percent if self._prev is not None else None

    def _discharging(self) -> bool:
        return bool(self._prev is not None and self._prev.discharging)

    def _queue_flash_locked(self, now: float) -> list[bytes]:
        """Schedule one flash burst; returns bodies due immediately."""
        b = self.cfg.battery
        pct = self._battery_percent() or 0
        critical = pct <= b.critical_percent
        color = tuple(b.critical_color if critical else b.low_color)
        blinks = b.critical_blinks if critical else b.low_blinks
        interval = b.critical_interval_s if critical else b.low_interval_s
        self._next_flash_at = now + interval
        self._flash_until = now + blinks * FLASH_BLINK_PERIOD_S
        self._flash_color = color
        self.stats["battery_flashes"] += 1
        base = self._effective_lightbar(self._flash_until + 0.001)
        restore = base or self._game_lightbar or P.DEFAULT_LIGHTBAR
        due: list[bytes] = []
        for i in range(blinks):
            t = now + i * FLASH_BLINK_PERIOD_S
            on = self._lightbar_body(color)
            off = self._lightbar_body((0, 0, 0))
            if i == 0:
                due.append(on)
            else:
                self._fx.append((t, on))
            self._fx.append((t + FLASH_BLINK_ON_S, off))
        self._fx.append((now + blinks * FLASH_BLINK_PERIOD_S,
                         self._lightbar_body(restore)))
        self._fx.sort(key=lambda e: e[0])
        return due


def _stick_norm(v: int) -> float:
    """-1.0 .. 1.0 with the deadzone removed."""
    d = v - STICK_CENTER
    if abs(d) <= STICK_DEADZONE:
        return 0.0
    span = 127 - STICK_DEADZONE
    return max(-1.0, min(1.0, (d - STICK_DEADZONE * (1 if d > 0 else -1)) / span))


def attach_to_backend(backend, input_cfg: K.InputConfig | None,
                      *, actions: OsActions | None = None,
                      clock=time.monotonic,
                      allow_disabled: bool = False,
                      on_mode=None) -> InputInterceptor | None:
    """Build the engine from config and hang it on a `BridgeBackend`.

    Returns None (and attaches nothing) when the section is missing, or --
    unless `allow_disabled` -- disabled. `BridgeService` passes
    `allow_disabled=True`: its config watcher can flip `input.enabled` on a
    RUNNING bridge, and an engine sitting in passthrough (`update_config`)
    is a live state change away from working, where an engine that was never
    attached would need the hot-attach dance passthrough exists to avoid.
    The backend only ever sees the three-method contract; this is the single
    place the app layer and the emulator meet for input.
    """
    if input_cfg is None:
        return None
    if not getattr(input_cfg, "enabled", False) and not allow_disabled:
        return None
    engine = InputInterceptor(
        input_cfg,
        actions=actions or OsActions(),
        send_setstate=backend.push_setstate_body,
        power_off=backend.power_off_pad,
        clock=clock,
        on_mode=on_mode,
    )
    backend.interceptor = engine
    return engine
