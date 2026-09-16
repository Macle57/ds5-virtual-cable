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
held** — buttons, dpad, touchpad; triggers still pass. The pointer controls
are lent to the OS by default (`stick_mouse_in_chord`): left stick moves the
pointer, right stick scrolls, at the same `remote.*` speeds as remote mode,
and the game sees them centred for the duration of the hold; a **1-finger
touchpad drag moves the pointer and a short 1-finger tap clicks**, exactly
as in remote mode — the "major controls" mean the same thing whether PS is
held or remote mode is on. The cost is a camera frozen while reaching for a
shortcut; set `"stick_mouse_in_chord": false` to restore pure stick
passthrough during chords (the 1-finger touchpad is then swallowed too).

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
| R3 | `show_battery` | a notification: "*label*: 80 % (discharging)" for the pad that pressed it |
| Touchpad click (1 finger) | `keyboard` | the pad-driven on-screen keyboard (below); press again to close |
| Mute (the mic button) | `dictation` | Windows voice typing, listening through the **pad's** microphone (below) |
| 2-finger click | `right_click` | the pad physically clicked with two fingers on it, like a Windows touchpad |
| 2-finger slide up / down, pad **not** clicked | `scroll_up` / `scroll_down` | one continuous scroll, proportional to travel, like a precision touchpad (`gestures.scroll_sensitivity`, `scroll_reverse`) |
| 2-finger slide left / right, not clicked | `scroll_left` / `scroll_right` | the same, sideways |
| 2-finger pinch / spread, not clicked | `pinch_zoom` | a real touch-screen pinch (Chrome zooms its viewport), low-passed and paced at ~120 moves/s so the pad's coordinate wobble never reads as jitter; `gestures.zoom_sensitivity`, `zoom_reverse` |
| 2-finger slide left / right **while clicked** | `alt_tab` (both rows) | **hold semantics**: switcher opens on ~150 px of travel, each further 150 px steps (slide back = step back), lifting the fingers or releasing PS commits (Alt-up) |
| 2-finger slide up / down while clicked | `task_view` / `minimize_all` | Win+Tab / Win+M, after ~200 px |
| 2-finger pinch while clicked | *(none)* | `ctrl_zoom` (Ctrl + wheel) is the natural pick |

The full gesture vocabulary, with the remote-mode column, is in
[Two-finger gestures](#two-finger-gestures) below.

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
| continuous (follow the fingers) | `scroll_up` `scroll_down` `scroll_left` `scroll_right` (wheel events proportional to travel; `gestures.scroll_sensitivity` / `scroll_reverse` apply) `ctrl_zoom` (Ctrl + wheel, whole notches) `pinch_zoom` (Windows touch injection: two synthetic contacts spread or close around the pointer — Chrome/Edge zoom the **visual viewport** the way a precision-touchpad pinch does, which is not the Ctrl zoom level; `gestures.zoom_sensitivity` / `zoom_reverse`). Bound to a *button* they do one step (`input.actions.<name>.step` notches) in the named direction. The scroll directions are **paired** (`pair` / `dir` on `/api/actions`): on a slide axis `scroll_up` + `scroll_down` is one scroll that follows the fingers both ways, and the dashboard sets the two rows together |
| voice typing | `dictation` — see below |
| the engine's own (`engine_actions`) | `pad_power_off` `pad_lightbar_toggle` `keyboard` `show_battery` |

## Two-finger gestures

Every 2-finger touchpad gesture is a row in **both** binding tables
(`input.chords` while PS is held, and remote mode — see
[`same_gestures`](#remote-mode)). `/api/actions` serves the rows as
`gestures` (`{key, label, help, group}`, group `taps | unpressed | pressed`),
which is what the dashboard's Gestures tab renders.

| key | what you do | default (PS held) | default (remote mode) |
|---|---|---|---|
| `touch_tap_2f` | two fingers touch briefly, **no** click | *(none)* | `right_click` |
| `touch_click_2f` | click the pad with two fingers resting on it | `right_click` | `right_click` |
| `touch_slide_up` / `touch_slide_down` | 2-finger slide up / down, pad not clicked (a pair) | `scroll_up` / `scroll_down` | `scroll_up` / `scroll_down` |
| `touch_slide_left` / `touch_slide_right` | 2-finger slide left / right, pad not clicked (a pair) | `scroll_left` / `scroll_right` | `scroll_left` / `scroll_right` |
| `touch_pinch` | fingers apart / together, pad not clicked | `pinch_zoom` | `pinch_zoom` |
| `touch_slide_up_pressed` / `touch_slide_down_pressed` | slide up / down **while the pad is clicked** (a pair) | `task_view` / `minimize_all` | `task_view` / `minimize_all` |
| `touch_slide_left_pressed` / `touch_slide_right_pressed` | slide left / right while clicked (a pair) | `alt_tab` / `alt_tab` | `alt_tab` / `alt_tab` |
| `touch_pinch_pressed` | pinch / spread while clicked | *(none)* | *(none)* |

The two rows of a slide axis are a **pair**. A *paired* action (`scroll_up`
↔ `scroll_down`, `scroll_left` ↔ `scroll_right`, `alt_tab` ↔ itself; served
as `pair` / `dir` on `/api/actions`) takes the axis whole: picking it on one
row binds the partner to its pair, and the pair is one gesture that follows
the fingers both ways. Anything else on a row is an independent one-shot,
and picking it while the partner holds a paired action unbinds the partner
— so an axis is either one paired gesture or two one-shots (either may be
unbound), never "scroll up" one way and Task View the other. The dashboard
enforces this and offers a directional action only on its own row; the
engine reads the rows literally.

The rows of older builds (`touch_slide_vertical`, `touch_slide_horizontal`,
`touch_swipe_up` / `_down`, and their `_pressed` forms) are **dropped** on
load, with one log line: today's rows keep their defaults whatever an older
file said, so every install has the gesture map above, and the file loses
the old rows on its next save. The old action names `scroll` /
`scroll_horizontal` are unknown too (a row or button bound to one is ignored
with a log line).

One contact is exactly **one** gesture:

- A contact that ends as a **click** (`touch_click_2f`) never also fires the
  tap. The click fires when the physical button comes back **up**, so a
  click that turns into a slide fires only its `_pressed` gesture and never
  the right click.
- A contact that becomes a slide or pinch fires neither tap.
- Travel is measured from the moment the second finger lands, so 1-finger
  mousing cannot pre-load a gesture.
- The dominant motion picks the row — the pinch when the spread change
  clearly beats both travel components (1.5×) **and** `pinch_delay_ms` has
  passed since the second finger landed, else the larger travel component's
  direction. Before the window closes the contact waits, so a slide that
  opens with a little spread wobble comes out a slide: **scrolling wins an
  ambiguous start**, the way a Windows touchpad prefers it. What the row
  names decides the travel it takes to commit: a
  continuous action (`scroll_*`, `*_zoom`) commits at `slide_px` and then
  tracks the fingers until they lift; `alt_tab` at `alt_tab_step_px`, with
  hold semantics; anything else at `swipe_px`, fired once, and the contact
  is spent. An unbound row leaves the contact undecided, so a later, bound
  direction can still win. Once decided, the contact stays that gesture: a
  scroll that began vertically ignores later sideways drift.
- **The click outranks the unpressed family.** The moment the pad goes down
  mid-contact, whatever the fingers were doing unpressed ends (a scroll or
  pinch ends, an open switcher commits) and travel is measured afresh from
  the click point against the `_pressed` rows only — a click-and-release
  with no travel from there is the 2-finger click. A contact that started
  clicked is a pressed gesture from its first frame.
- Under PS, a 2-finger click is **not** the `touchpad_click` chord (that
  needs one finger or none). In remote mode a 1-finger physical click is the
  intrinsic left mouse button, held for the press (drag).

Thresholds and feel live in `input.gestures` (touchpad points, 1920 × 1080
over ~52 × 23 mm), and apply live:

| key | default | meaning |
|---|---|---|
| `scroll_sensitivity` | 1.0 | multiplier on how far a 2-finger slide scrolls (the touchpad only — `remote.scroll_speed` is the sticks' and triggers') |
| `scroll_reverse` | false | flip the scroll direction of a slide |
| `zoom_sensitivity` | 1.0 | multiplier on how much a pinch zooms (`pinch_zoom` and `ctrl_zoom`) |
| `zoom_reverse` | false | flip the pinch: apart = out |
| `tap_ms` | 250 | a touch shorter than this that moved less than `tap_move_px` is a tap |
| `tap_move_px` | 40 | … and the most a tap or a 2-finger click may travel |
| `slide_px` | 40 | travel at which a row bound to a continuous action commits (scrolling starts) |
| `swipe_px` | 200 | travel at which a row bound to a one-shot fires |
| `alt_tab_step_px` | 150 | horizontal travel that opens Alt-Tab, and per further step |
| `pinch_px` | 80 | spread change at which a contact becomes a pinch … |
| `pinch_delay_ms` | 120 | … but never before this long after the second finger landed (scroll priority) |
| `scroll_px_per_notch` | 100 | finger travel per wheel notch (120 units), before `scroll_sensitivity` |
| `zoom_px_per_notch` | 80 | spread change per Ctrl+wheel notch, before `zoom_sensitivity` |
| `pinch_gain` | 1.0 | screen px the injected contacts move per point of spread change, before `zoom_sensitivity` |

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
| touchpad 1-finger physical click | left mouse button, held for the press (drag) |
| left stick | move the pointer (rate) |
| right stick (vertical) | scroll (rate) |
| L2 / R2 | scroll up / down (analog rate) |

Every **button** and every **2-finger gesture** resolves through a binding
table. Two switches, one per row family:

- **`remote.same_bindings`** (default `true`) — the BUTTON rows come from
  `input.chords`: a button means in remote mode exactly what it means with
  PS held (Cross = play/pause, dpad = volume/track, Options = Win+P). Off:
  the button rows of `input.remote.chords`.
- **`remote.same_gestures`** (default `true`) — the GESTURE rows (every key
  starting with `touch_`, except `touchpad_click`) come from `input.chords`.
  Off: the gesture rows of `input.remote.chords`. The dashboard's Gestures
  tab shows this as "Remote mode uses the same gestures" and, when off, a
  second gesture table.

`input.remote.chords` is merged over `DEFAULT_REMOTE_CHORDS` with the same
rules as `input.chords` (name a key to change it, `"none"` to remove it,
removals written back as `"none"`). Its defaults are the classic remote map
this feature shipped with, plus the gesture rows:

  | key | default remote action |
  |---|---|
  | `cross` | `left_click` (held while Cross is: drag) |
  | `circle` | `escape` |
  | `options` | `enter` |
  | `dpad_up` / `dpad_down` / `dpad_left` / `dpad_right` | `arrow_up` … `arrow_right` (held; re-trigger every 300 ms) |
  | `touch_tap_2f` / `touch_click_2f` | `right_click` |
  | `touch_slide_up` / `touch_slide_down` | `scroll_up` / `scroll_down` |
  | `touch_slide_left` / `touch_slide_right` | `scroll_left` / `scroll_right` |
  | `touch_pinch` | `pinch_zoom` (`touch_pinch_pressed` unbound) |
  | `touch_slide_left_pressed` / `touch_slide_right_pressed` | `alt_tab` (hold semantics, as under a chord) |
  | `touch_slide_up_pressed` / `touch_slide_down_pressed` | `task_view` / `minimize_all` |

Note that with the default `same_gestures: true` the chord table governs,
where `touch_tap_2f` is unbound — a 2-finger **click** right-clicks in both
modes; bind `touch_tap_2f` in `input.chords` (or switch `same_gestures` off)
to get the tap back.

A bound row fires directly — no chord button held. A row set to `"none"`
does nothing in remote mode; the intrinsic layer above is never a row, so
taps still click and the touchpad still moves the pointer whatever the
table says. Actions with hold semantics (`*_click`, `escape`, `enter`,
`arrow_*`) are held for the press; other repeatable actions (volume,
brightness, hold macros) repeat while held. Buttons in remote mode give
**no haptic ack** (a click that rumbles is a click you stop making), and
neither do the two taps or the continuous gestures; the one-shot shell
gestures ack as they do under a chord. Changing either table or a switch
applies live, and anything held is released on the way out.

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

## Notifications

Every balloon the tray shows carries a category, and the top-level
`notifications` section of `config.json` says which are shown (the
dashboard's Notifications card, applied live on save):

```json
"notifications": { "enabled": true, "battery_low": true, "remote_mode": true,
                   "keyboard": false, "connection": true, "hide": true,
                   "update": true }
```

| key | gates |
|---|---|
| `enabled` | the master switch — off, no balloon at all (errors included) |
| `battery_low` | the once-per-threshold low-battery toasts below |
| `remote_mode` | "Remote mode on/off (*label*)" |
| `keyboard` | "On-screen keyboard opened/closed" (off by default: the keyboard is on screen already) |
| `connection` | a controller going offline / vanishing / extras detached (the manager's warnings) |
| `hide` | HidHide cloak applied or lifted |
| `update` | "Update available" from the background check |

`show_battery` (the R3 chord by default; bindable to any chord, remote
button or gesture) shows "*label*: 80 % (discharging|charging|full)" for the
pad that pressed it, and is gated by the master switch only. A bridge error
balloon is likewise master-switch only.

**Transport.** The engine runs in the bridge child; the balloons are the
tray's. The child prints one status line per toast, `  >  category|title|body`
(`service.toast_line`), the manager parses it into a `toast` event
(`manager.parse_toast_event`), and the tray's `_on_event` looks the category
up in `config.Notifications.allows()` before calling `_notify(title, body)`.
`{pad}` in the body is replaced with the pad's label (or short serial) by
the tray, which is the only side that knows it.

### Low-battery notifications

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
  "chords": { "triangle": "pad_power_off", "r3": "show_battery",
              "touch_click_2f": "right_click", "touch_slide_up": "scroll_up",
              "touch_slide_down": "scroll_down", "touch_pinch": "pinch_zoom",
              "touch_slide_left_pressed": "alt_tab",
              "touch_slide_right_pressed": "alt_tab",
              "...": "see the tables above" },
  "actions": { "volume_up": {"step": 1} },
  "macros": { "task_manager": { "keys": ["ctrl", "shift", "esc"],
                                "label": "Task Manager" },
              "spotify": { "run": "spotify.exe" },
              "spam_click": { "keys": ["space"], "label": "Auto space",
                              "repeat": { "mode": "toggle", "interval_ms": 50,
                                          "max_runs": 0 } } },
  "battery": { "enabled": true, "low_percent": 20, "critical_percent": 10,
               "low_interval_s": 30.0, "critical_interval_s": 10.0,
               "low_color": [255, 140, 0], "critical_color": [255, 0, 0],
               "low_blinks": 2, "critical_blinks": 3 },
  "lightbar": { "dim_after_minutes": 0.0, "dim_level": 0.3 },
  "gestures": { "tap_ms": 250, "tap_move_px": 40, "slide_px": 40,
                "swipe_px": 200, "alt_tab_step_px": 150, "pinch_px": 80,
                "pinch_delay_ms": 120,
                "scroll_px_per_notch": 100, "zoom_px_per_notch": 80,
                "pinch_gain": 1.0,
                "scroll_sensitivity": 1.0, "scroll_reverse": false,
                "zoom_sensitivity": 1.0, "zoom_reverse": false },
  "remote": { "enabled": false, "mouse_speed": 1.6, "scroll_speed": 1.0,
              "lightbar_color": [255, 120, 0],
              "same_bindings": true, "same_gestures": true,
              "chords": { "cross": "left_click", "circle": "escape",
                          "options": "enter", "dpad_up": "arrow_up",
                          "dpad_down": "arrow_down", "dpad_left": "arrow_left",
                          "dpad_right": "arrow_right",
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
                          "touch_slide_down_pressed": "minimize_all" } }
}
```

(and, top level, next to `input`: the `notifications` section above.)

Four action names are the engine's own rather than OS actions
(`config.ENGINE_ACTIONS`, served as `engine_actions` by `/api/actions`):
`pad_power_off` and `pad_lightbar_toggle` act on the pad through the bridge,
`keyboard` toggles the on-screen keyboard, `show_battery` sends a toast.
None of them touches the desktop through `SendInput`.

`/api/actions` also serves `defaults.chords`, `defaults.remote_chords`,
`chord_keys` (buttons a chord may bind), `remote_keys` (what the remote table
binds: every button but the arming one, plus every gesture), `gesture_keys`
and `gestures` (the ordered `{key, label, help, group}` rows of the Gestures
tab).

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
  `del`) are accepted. At most 8 keys.
- `{"run": "..."}` — a command line, started detached with no console and
  never waited for, the way the Run box would.

Either kind may carry `repeat`:

```json
"repeat": { "mode": "hold", "interval_ms": 50, "max_runs": 0 }
```

| `mode` | behaviour |
|---|---|
| `once` (default when absent) | fires once per press |
| `hold` | fires again every `interval_ms` for as long as the bound button is held (a chord, or a remote-mode button) |
| `toggle` | the first press starts it repeating every `interval_ms` on the engine's own timer — no input needed — and the next press of the same macro stops it. The engine turning off, leaving remote mode and the bridge stopping all stop it too |

`max_runs` (0 = unlimited) caps the number of runs per activation, the
first one included. `"repeat": true` is still accepted and means `hold` at
the engine's `repeat_ms`. A macro bound to a gesture repeats the same way
(a toggle macro on a swipe starts and stops with alternate swipes). The
dashboard's macro editor shows a Repeat select and interval field.

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
- `app/tests/test_gestures_v1.py` — the 2-finger tracker: click-vs-tap
  exclusivity in both contexts, pressed vs unpressed row families, the
  compat flicks, tunable thresholds; scroll proportionality and
  `scroll_speed`; pinch direction through a recorded touch-injection seam
  and through Ctrl+wheel (whole notches); every exit path lifting a live
  pinch; hold/toggle macro timing and what stops a toggle; `show_battery`
  text and the remote/keyboard toasts; the child's toast line through
  `ChildBridge`; `Notifications` and the tray's category gating; the config
  watcher carrying the new keys (58)
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
9. The v1 gestures from real fingers: 2-finger click vs tap under thumb
   jitter (`tap_move_px` 40), the unpressed scroll feel
   (`scroll_px_per_notch` 100), the pinch commit threshold (`pinch_px` 80)
   against the natural spread drift of a 2-finger slide, and whether a
   clicked slide ever reaches `alt_tab_step_px` before the click is released.
   The actions themselves (`pinch_zoom` on Chrome's viewport, `ctrl_zoom`,
   `scroll`, `right_click`, `show_battery`) were verified on the desktop by
   driving `OsActions` directly.
