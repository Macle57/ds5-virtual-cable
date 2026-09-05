# Chords, gestures and remote mode — the input engine

The bridge sits between the pad and the game anyway, so it can do what a
console does with the PS button: while the **chord button** (default: PS) is
held, presses become shortcuts and *never reach the game*. The interception
happens on the decoded Bluetooth `0x31` input stream, before the emulated USB
layer — the game just sees a pad nobody is pressing chords on.

Everything below is configurable in the `input` section of `config.json`
(`%APPDATA%\ds5bridge\config.json`); every default works untouched. The engine
runs inside every bridge (`ds5bridge run`, `--all`, the tray's children) and
can be switched off wholesale with `"input": {"enabled": false}`. A saved
change to the `input` section — from the dashboard or a hand edit — reaches
every running bridge within a couple of seconds; no restart needed.

## Where it hooks (for developers)

`app/ds5app/intercept.py` implements a three-method contract that
`emulator/ds5emu/bridge.py` calls on its `interceptor` attribute; the app layer
attaches it in `BridgeService._attach_interceptor` (the emulator itself has no
dependency on the engine):

| seam | thread | what happens |
|---|---|---|
| `on_input(usb01)` | BT reader, ~476 Hz | chord detection, gesture state machines, masking. The returned report is the only input state the game can see (interrupt endpoint *and* control GET_REPORT). Hooking at the reader — not at `read_input_report` — means no 250 Hz decimation can eat a button edge. |
| `rewrite_setstate(body)` | SetState writer | the last word on the lightbar: dimming, battery flashes and the remote-mode colour rewrite the game's outgoing bytes in flight. The **host's** body stays the coalescing key, so a time-varying override never masquerades as host state. |
| `tick()` | SetState writer, ≤ 50 ms | timers: pulse tails, the tap-replay decision, battery flash cadence, the idle off-timer. |

The engine talks back to the pad through `backend.push_setstate_body` (its own
valid-flag-disciplined SetState bodies — haptic acks, flashes; deliberately
NOT merged into the host's replayed state) and `backend.power_off_pad`.

Every call into the engine is wrapped: a bug costs one report and an
`interceptor_errors` counter, never the bridge.

## Powering the pad off (PS+Triangle, idle timer)

The mechanism is this project's own hardware capture, not folklore
(docs/wired-gap-findings.md, symptom 3): **SET feature report `0x08` with
action byte `0x02`** — `DS_FEATURE_REPORT_BLUETOOTH_CONTROL` in the public
research (nondebug/dualsense) — tells a DualSense to drop its Bluetooth link,
and with no console to fall back to it powers off. TLOU was captured sending
exactly `08 02 00…` at a pad it considered redundant; the link was dead 26 ms
later, 2/2 runs. `BridgeBackend._bt_power_off_now` sends the same command down
our own handle, framed at the length the pad's **own Bluetooth report
descriptor** gives feature `0x08`: 47 data bytes (48 with the id, TLOU's
captured length), `0x53`-seeded CRC32 in the last 4.

> **Verified on hardware 2026-09-03.** The first cut used the 63-byte framing
> of the `0x80` test channel, and that is why PS+Triangle "went through" but
> the pad stayed on: Windows' HID stack refuses a feature write whose length
> disagrees with the descriptor (`HidD_SetFeature` fails, hidapi `-1`) before
> anything reaches the air. The 48-byte signed write switches the pad off
> within a second.

## Default keymap

Chords fire while the chord button (PS) is held; a short haptic pulse acks
each accepted chord (`haptic_ack` to silence it, `haptic_strength` 0–100 for
how hard it hits, default 25). **Everything digital is swallowed while PS is
held** — buttons, dpad, touchpad; triggers still pass. The sticks are lent to
the OS by default (`stick_mouse_in_chord`): left stick moves the pointer,
right stick scrolls, at the same `remote.*` speeds as remote mode, and the
game sees them centred for the duration of the hold — the "major controls"
mean the same thing whether PS is held or remote mode is on. The cost is a
camera frozen while reaching for a shortcut; set
`"stick_mouse_in_chord": false` to restore pure stick passthrough during
chords.

| input (with PS held) | action | notes |
|---|---|---|
| Triangle | `pad_power_off` | feature 0x08 — same as holding PS on a console |
| *(unbound by default)* | `pad_lightbar_toggle` | lightbar off until the next press; outranks dim and the remote-mode colour, but a battery flash still shows; the engine turning off or the session ending brings it back |
| Cross | `media_play_pause` | |
| Square | `volume_mute` | |
| Dpad up / down | `volume_up` / `volume_down` | repeats while held |
| Dpad left / right | `media_prev` / `media_next` | |
| L1 / R1 | `brightness_down` / `brightness_up` | WMI via PowerShell; repeats; no-op on monitors without WMI brightness |
| Options | `projection_cycle` | Win+P |
| Create (Share) | `show_desktop` | Win+D |
| Touchpad click | `keyboard` | the pad-driven on-screen keyboard (below); press again to close |
| Mute (the mic button) | `dictation` | Windows voice typing, listening through the **pad's** microphone (below) |
| 2-finger horizontal slide | `alt_tab` | **hold semantics**: switcher opens on ~150 px of travel, each further 150 px steps (slide back = step back), lifting the fingers or releasing PS commits (Alt-up) |
| 2-finger swipe up | `task_view` | Win+Tab |
| 2-finger swipe down | `minimize_all` | Win+M |

A **plain PS tap** still reaches the game: if PS comes up quickly with no
chord fired, the engine waits out the double-press window (400 ms) and then
replays a 100 ms PS press into the forwarded reports. A long PS hold with no
chord is swallowed entirely. A diagonal dpad press fires exactly one of its
two component chords.

Action names resolve against the registry in `app/ds5app/actions.py`
(`OsActions.registry()`); everything is `ctypes SendInput` — no new
dependencies. Per-action parameters go in `input.actions`, e.g.
`{"volume_up": {"step": 2}}` (taps of 2 % each), `{"brightness_up": {"step": 25}}`.

### The whole action vocabulary

Served live by `/api/actions` (`actions`, each with a `doc`); listed here so
the table above can be re-bound without guessing:

| group | actions |
|---|---|
| volume / media | `volume_up` `volume_down` (repeat) `volume_mute` `media_play_pause` `media_next` `media_prev` |
| brightness | `brightness_up` `brightness_down` (repeat; WMI via PowerShell) |
| shell | `show_desktop` (Win+D) `minimize_all` (Win+M) `task_view` (Win+Tab) `alt_tab` `projection_cycle` (Win+P chooser) |
| projection, no chooser | `display_extend` (`DisplaySwitch.exe /extend`) `display_second_only` (`/external`) `display_pc_only` (`/internal`) `display_duplicate` (`/clone`) `display_cycle` — PC only → duplicate → extend → second only → …, remembering where it is for the life of the bridge (an explicit `display_*` moves the cycle too) |
| mouse and keys | `left_click` `right_click` `middle_click` `escape` `enter` `arrow_up` `arrow_down` `arrow_left` `arrow_right` — from a chord these tap; in remote mode they are **held** for the length of the press (Cross = drag) and the arrows re-trigger every 300 ms |
| voice typing | `dictation` — see below |
| the engine's own (`engine_actions`) | `pad_power_off` `pad_lightbar_toggle` `keyboard` |

### Dictation through the pad's microphone

`dictation` (default: PS + Mute) is Windows voice typing, Win+H — except
that Win+H listens on the *default* microphone, which is never the
controller. The bridge presents the pad as a wired DualSense, and a wired
DualSense is a USB audio device with an active capture endpoint called
"Headset Microphone (2- DualSense Wireless Controller)". So the first press:
remembers the current default capture device (all three roles: console,
multimedia, communications), makes the pad's endpoint the default
(`IPolicyConfig::SetDefaultEndpoint`, the undocumented interface every
"SoundSwitch"-style tool uses — there is no documented way), waits 150 ms
for the audio service to re-route, and presses Win+H. The second press:
Win+H (closes voice typing) and the previous default is put back. The
bridge stopping also puts it back. If no *active* DualSense capture endpoint
exists (the pad is not bridged, or a game has the mic) voice typing still
toggles, on whatever the default is. `app/ds5app/audio_default.py`; every
COM failure is one log line, never a raised error into the input path.

### The on-screen keyboard

`keyboard` (default: PS + touchpad click; bindable to any chord or gesture,
in either table) toggles a Steam-style keyboard — five rows of dark keys with
a white highlight, a topmost tool window that **never takes focus** (the
characters go to whatever had the caret: the game's chat box, a browser
field, the Run dialog). While it is open the game sees a neutral pad, exactly
as in remote mode, whether or not remote mode is on; chords still work.

| pad | on the keyboard |
|---|---|
| left stick / dpad | move the highlight (auto-repeat after 350 ms, every 80 ms) |
| Cross | press the highlighted key |
| Square | Backspace (repeats while held) |
| Triangle | Space |
| L2 held | Shift (the Shift keys on the layout latch for one character) |
| L3 | Caps Lock (letters only, like the real key; Shift undoes it) |
| R2 | Enter |
| L1 / R1 | cursor left / right (arrow keys; repeat) |
| right stick | drag the keyboard; the *Move* key makes the left stick / dpad move it too |
| Circle or Options | close |

Layout: `` ` 1 … 0 - = Backspace `` / `Tab q … p [ ] \` / `Caps a … ' Enter`
/ `Shift z … / Shift` / emoji panel (Win+.), Space, ← ↑ ↓ →, Paste (Ctrl+V),
Move. Each key that has a pad shortcut shows the glyph in its corner.
Characters are typed with `KEYEVENTF_UNICODE` (layout-independent; an emoji
arrives as its two surrogates), Backspace/Enter/Tab/arrows as VK codes.
`app/ds5app/osk.py`: the model and the pad driver are pure and unit-tested;
the window is raw Win32/GDI on its own thread (per-monitor DPI aware, so a
125 % screen gets a crisp 125 % keyboard, not a DWM-stretched one).

## Remote mode

**Ships OFF** (`remote.enabled`, switched on from the dashboard) — a mode a
mistimed PS double-tap can fall into must be opted into. When enabled,
**double-press PS** (two presses within `double_press_ms`) — the pad stops
driving the game (which sees a neutral pad with real battery/status bytes) and
drives the OS instead. Feedback: a double haptic pulse and the lightbar held
orange (`remote.lightbar_color`); single pulse and the game's colour restored
on exit. Chords stay active in remote mode.

Two layers. The **intrinsic** pointer controls are what remote mode *is* and
are not bindings:

| input (remote mode) | intrinsic |
|---|---|
| touchpad 1-finger drag | move the pointer (`remote.mouse_speed`) |
| touchpad 1-finger tap | left click |
| touchpad 2-finger tap | right click |
| left stick | move the pointer (rate) |
| right stick (vertical) | scroll (rate — the touchpad deliberately does not scroll; sticks and triggers own it) |
| L2 / R2 | scroll up / down (analog rate) |

Every **button** and the **three 2-finger gestures** resolve through a
binding table, and which table is `remote.same_bindings`:

- **`same_bindings: true` (default)** — the `input.chords` table. A button or
  gesture means in remote mode exactly what it means with PS held: Cross =
  play/pause, dpad = volume/track, Options = Win+P, 2-finger slide = Alt-Tab,
  swipes = Task View / minimize. One table to maintain; the dashboard shows
  the checkbox ticked.
- **`same_bindings: false`** — `input.remote.chords`, merged over
  `DEFAULT_REMOTE_CHORDS` with the same rules as `input.chords` (name a key
  to change it, `"none"` to remove it, removals written back as `"none"`).
  The defaults are the classic remote map this feature shipped with:

  | key | default remote action |
  |---|---|
  | `cross` | `left_click` (held while Cross is: drag) |
  | `circle` | `escape` |
  | `options` | `enter` |
  | `dpad_up` / `dpad_down` / `dpad_left` / `dpad_right` | `arrow_up` … `arrow_right` (held; re-trigger every 300 ms) |
  | `touch_slide_horizontal` | `alt_tab` (hold semantics, as under a chord) |

A bound row fires directly — no chord button held. A row set to `"none"`
does nothing in remote mode; the intrinsic layer above is never a row, so
taps still click and the touchpad still moves the pointer whatever the
table says. Actions with hold semantics (`*_click`, `escape`, `enter`,
`arrow_*`) are held for the press; other repeatable actions (volume,
brightness) repeat at `repeat_ms` while held. Buttons in remote mode give
**no haptic ack** (a click that rumbles is a click you stop making);
gestures ack as they do under a chord. Changing either table or the
checkbox applies live, and anything held is released on the way out.

### Mode reporting

The engine exposes `remote_mode` and `keyboard_open` and the bridge tells its
parent on every flip, two ways: the telemetry datagram carries both booleans
(`/api/state` → `controllers[serial].telemetry.remote_mode` /
`.keyboard_open`), and the child prints a status line

    #  remote on  keyboard closed

(`service.mode_line`, the `  #  ` prefix of `cli._log`) which the manager
parses into `/api/state` → `controllers[serial].remote_mode` /
`.keyboard_open` — `true`/`false`, or `null` before the child has said —
so the dashboard's badges work with telemetry switched off.

## Low-battery notifications

The child already logs every new low battery reading; the lightbar flashes on
its own schedule. Neither is what a Windows notification should follow. The
tray shows **one toast per threshold per discharge**: "*label or short
serial* battery 20% -- charge soon" and "… battery 10% -- charge it now" —
the same 20 % / 10 % the lightbar turns amber / red at. A threshold re-arms
only when the pad charges back above it or the bridge reconnects; a reading
that merely flaps back over 20 % while still discharging does not (the pad
reports in 10 % steps and flaps at the boundary). `manager.LowBatteryAlerts`
holds the rule; the child's per-percent battery warnings no longer toast.

## Idle off-timer

`off_timer_minutes` (default **15**, `0` disables) of no input activity powers
the pad off by the same 0x08 mechanism. Activity = any button, dpad, stick
outside the deadzone, trigger, or touch contact — the IMU is deliberately
excluded (gyro noise never sleeps; a pad face-down on the couch must idle).

## Lightbar battery management

- **Battery alerts** (`input.battery`, on by default): discharging at ≤ 20 %
  flashes the lightbar amber (2 blinks every 30 s); at ≤ 10 % red, 3 blinks
  every 10 s. Colours, thresholds, blink counts and intervals are all
  configurable; the flash always ends by restoring whatever colour the game
  last asked for.
- **Dimming** (`input.lightbar`, ships OFF): `dim_after_minutes > 0` scales
  the game's lightbar RGB by `dim_level` (0 = off) from that point on, by
  rewriting outgoing SetState bodies — the game is untouched.

## Config reference

```json
"input": {
  "enabled": true,
  "chord_button": "ps",
  "double_press_ms": 400,
  "tap_replay_ms": 100,
  "repeat_ms": 150,
  "haptic_ack": true,
  "haptic_strength": 25,
  "stick_mouse_in_chord": true,
  "off_timer_minutes": 15.0,
  "chords": { "triangle": "pad_power_off", "...": "see the table above" },
  "actions": { "volume_up": {"step": 1} },
  "macros": { "task_manager": { "keys": ["ctrl", "shift", "esc"],
                                "label": "Task Manager" },
              "spotify": { "run": "spotify.exe" } },
  "battery": { "enabled": true, "low_percent": 20, "critical_percent": 10,
               "low_interval_s": 30.0, "critical_interval_s": 10.0,
               "low_color": [255, 140, 0], "critical_color": [255, 0, 0],
               "low_blinks": 2, "critical_blinks": 3 },
  "lightbar": { "dim_after_minutes": 0.0, "dim_level": 0.3 },
  "remote": { "enabled": false, "mouse_speed": 1.6, "scroll_speed": 1.0,
              "lightbar_color": [255, 120, 0],
              "same_bindings": true,
              "chords": { "cross": "left_click", "circle": "escape",
                          "options": "enter", "dpad_up": "arrow_up",
                          "dpad_down": "arrow_down", "dpad_left": "arrow_left",
                          "dpad_right": "arrow_right",
                          "touch_slide_horizontal": "alt_tab" } }
}
```

Three action names are the engine's own rather than OS actions
(`config.ENGINE_ACTIONS`, served as `engine_actions` by `/api/actions`):
`pad_power_off` and `pad_lightbar_toggle` act on the pad through the bridge,
`keyboard` toggles the on-screen keyboard. None of them touches the desktop
through `SendInput`.

`/api/actions` also serves `defaults.chords`, `defaults.remote_chords`,
`chord_keys` (buttons a chord may bind), `remote_keys` (what the remote table
binds: every button but the arming one, plus the three gestures) and
`gesture_keys`.

`chords` entries MERGE over the defaults — name one to change one; map a key
to `"none"` to remove it (removals are written back as `"none"` so they
survive a save/load cycle). Unknown action names are ignored with one log
line; unknown keys everywhere ride along untouched, so a config written by a
newer build loses nothing here.

### Custom macros

`macros` are user-defined actions, keyed by the name a chord binds to
(`a-z`, digits, `_`, starting with a letter). Two kinds:

- `{"keys": [...]}` — one keyboard chord, pressed in order and released in
  reverse inside a single `SendInput` (the same atomicity rule the built-ins
  live by). Key names are the ones `/api/actions` serves as `macro_keys`:
  `ctrl shift alt win`, `enter esc tab space backspace delete insert home end
  pageup pagedown up down left right`, `f1`..`f24`, letters, digits,
  `numpad0`..`numpad9`, the media/volume keys and punctuation (`minus`,
  `equals`, `lbracket`, ...). Common aliases (`control`, `escape`, `pgup`,
  `del`) are accepted. At most 8 keys. `"repeat": true` makes it fire again
  at `repeat_ms` while the chord is held.
- `{"run": "..."}` — a command line, started detached with no console and
  never waited for, the way the Run box would.

`label` is what the settings page shows. A macro may not shadow a built-in
name; a malformed one is skipped with one log line and takes nothing else
down. The dashboard's *Macros* tab edits these (with a press-the-shortcut
capture box), and its Save POSTs `"name": null` to delete one — the config
API deep-merges, so absence alone cannot express removal.

## Testing

No hardware anywhere: synthetic `0x01` reports feed the engine, an injected
clock drives every timing rule, `SendInput` is a recorder, PowerShell a stub.

- `app/tests/test_intercept.py` — masking, chord edges/repeats, tap replay
  and double-press timing, all gestures (chord-held and remote), chord-held
  stick translation, the classic remote-mode map, the table-driven remote
  bindings (rebind, `none`, hold/repeat, live swap, `same_bindings`), the
  keyboard's takeover of the pad and the neutral report, `on_mode`
  reporting, display/dictation chords, haptic strength scaling, idle timer,
  battery flash schedule, lightbar rewrites, wiring (140 tests)
- `app/tests/test_actions.py` — exact VK sequences, one-`SendInput` atomicity,
  Alt-Tab hold lifecycle and stuck-Alt protection, every default of both
  tables resolves (18)
- `app/tests/test_osk.py` — the layout as specified, highlight arithmetic
  (nearest-centre rule checked for every key), Shift/Caps/latch typing,
  Unicode surrogates, every pad button of the driver with auto-repeat timing,
  the arming frame, window drag, the keyboard object's thunks (34)
- `app/tests/test_audio_default.py` — endpoint selection against the states
  this machine really lists, the borrow/restore toggle for all three roles,
  a raising COM layer, the action through `OsActions` (14)
- `app/tests/test_low_battery.py` — the once-per-threshold rule, re-arming,
  the child's battery/mode lines through `ChildBridge._read_stdout` (14)
- `app/tests/test_input_config.py` — defaults, merge/removal durability,
  never-raise coercion, round-trips, the remote table (31)
- `app/tests/test_telemetry.py` — plus `remote_mode`/`keyboard_open` in the
  datagram, a flip forcing a send, old datagrams reading as off (17)
- `app/tests/test_macros.py` — key-name vocabulary, macro compilation (one
  `SendInput`, the `launch` seam, every malformed shape), config round-trip
  and null tombstones, chords bound to macros, built-in shadowing, live
  `update_config` re-resolution (16)
- `emulator/tests/test_bridge.py::InterceptorSeamTests` — containment (a
  raising engine costs a counter, never the bridge), `push_setstate_body`
  signing and non-contamination of host state, the 0x08 power-off bytes (10)

## Still needs the hardware window

1. `power_off_pad` framing on a live pad (fallbacks listed above), via
   PS+Triangle and the idle timer.
2. Haptic ack strength/length feel; battery flash visibility in a lit room.
3. Gesture thresholds (150 px Alt-Tab step, 200 px swipe) against real
   touchpad traffic; tap detection under thumb jitter.
4. Chord masking in-game (The Last of Us Part I): PS tap still opens the game
   overlay after the 400 ms replay delay; chords invisible to the game.
5. Remote-mode pointer feel (`mouse_speed`/`scroll_speed` defaults).
6. `dictation` on a bridged pad: the endpoint enumeration was verified
   read-only on this machine (both DualSense entries listed, state 4 = not
   present while unbridged), but the switch itself, the 150 ms settle before
   Win+H, and voice typing actually hearing the pad's mic have not been.
7. The on-screen keyboard was verified as a window (opened, navigated, typed
   into a recorder, screenshotted at 125 % DPI) but never from a pad: the
   feel of the 350/80 ms navigation repeat, the L2 Shift threshold (trigger
   byte ≥ 96), right-stick drag speed, and whether a game's overlay ever
   fights the topmost tool window.
8. Low-battery toasts on a real discharge (the pad's 10 % steps; the
   re-arm after charging).
