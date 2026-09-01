# Chords, gestures and remote mode — the input engine

The bridge sits between the pad and the game anyway, so it can do what a
console does with the PS button: while the **chord button** (default: PS) is
held, presses become shortcuts and *never reach the game*. The interception
happens on the decoded Bluetooth `0x31` input stream, before the emulated USB
layer — the game just sees a pad nobody is pressing chords on.

Everything below is configurable in the `input` section of `config.json`
(`%APPDATA%\ds5bridge\config.json`); every default works untouched. The engine
runs inside every bridge (`ds5bridge run`, `--all`, the tray's children) and
can be switched off wholesale with `"input": {"enabled": false}`.

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
our own handle, framed the one way this hardware has accepted BT feature
writes (63-byte payload, `0x53`-seeded CRC32 in the last 4 bytes — the framing
verified for `0x80`).

This is the one deliberate non-`0x80` feature write in the project. It is
neither of the two the safety rail forbids (pairing `0x09`, firmware), and it
changes nothing persistent — one PS press brings the pad back. Host-issued
`0x08` writes are still recorded and dropped.

> **Hardware window:** the `0x08` framing (63-byte signed payload over BT) is
> the one part of this feature not yet proven on a live pad — TLOU's captured
> kill went over USB framing to a raw BT handle via the HID stack. If the pad
> ignores it, the fallbacks to try, in order: 48-byte signed payload (TLOU's
> observed length), then a Windows-side ACL disconnect
> (`IOCTL_BTH_DISCONNECT_DEVICE` on the radio handle, the DS4Windows way).

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
| Cross | `media_play_pause` | |
| Square | `volume_mute` | |
| Dpad up / down | `volume_up` / `volume_down` | repeats while held |
| Dpad left / right | `media_prev` / `media_next` | |
| L1 / R1 | `brightness_down` / `brightness_up` | WMI via PowerShell; repeats; no-op on monitors without WMI brightness |
| Options | `projection_cycle` | Win+P |
| Create (Share) | `show_desktop` | Win+D |
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

## Remote mode

**Ships OFF** (`remote.enabled`, switched on from the dashboard) — a mode a
mistimed PS double-tap can fall into must be opted into. When enabled,
**double-press PS** (two presses within `double_press_ms`) — the pad stops
driving the game (which sees a neutral pad with real battery/status bytes) and
drives the OS instead. Feedback: a double haptic pulse and the lightbar held
orange (`remote.lightbar_color`); single pulse and the game's colour restored
on exit. Chords stay active in remote mode.

| input (remote mode) | action |
|---|---|
| touchpad 1-finger drag | move the pointer (`remote.mouse_speed`) |
| touchpad 1-finger tap | left click |
| touchpad 2-finger drag | scroll, vertical + horizontal (`remote.scroll_speed`) |
| touchpad 2-finger horizontal slide | alt-tab hold, same semantics as the chord-held gesture — a decisive ≥150 px horizontal travel (before any vertical wheel has been emitted) opens the switcher; further travel steps it; lifting commits |
| touchpad 2-finger tap | right click |
| left stick | move the pointer (rate) |
| right stick (vertical) | scroll (rate) |
| L2 / R2 | scroll up / down (analog rate) |
| Cross (hold to drag) | left mouse button down/up |
| Circle | Esc |
| Options | Enter |
| dpad | arrow keys (retrigger every 300 ms while held) |

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
  "battery": { "enabled": true, "low_percent": 20, "critical_percent": 10,
               "low_interval_s": 30.0, "critical_interval_s": 10.0,
               "low_color": [255, 140, 0], "critical_color": [255, 0, 0],
               "low_blinks": 2, "critical_blinks": 3 },
  "lightbar": { "dim_after_minutes": 0.0, "dim_level": 0.3 },
  "remote": { "enabled": false, "mouse_speed": 1.6, "scroll_speed": 1.0,
              "lightbar_color": [255, 120, 0] }
}
```

`chords` entries MERGE over the defaults — name one to change one; map a key
to `"none"` to remove it (removals are written back as `"none"` so they
survive a save/load cycle). Unknown action names are ignored with one log
line; unknown keys everywhere ride along untouched, so a config written by a
newer build loses nothing here.

## Testing

No hardware anywhere: synthetic `0x01` reports feed the engine, an injected
clock drives every timing rule, `SendInput` is a recorder, PowerShell a stub.

- `app/tests/test_intercept.py` — masking, chord edges/repeats, tap replay
  and double-press timing, all gestures (chord-held and remote), chord-held
  stick translation, the full remote-mode map, haptic strength scaling, idle
  timer, battery flash schedule, lightbar rewrites, wiring (84 tests)
- `app/tests/test_actions.py` — exact VK sequences, one-`SendInput` atomicity,
  Alt-Tab hold lifecycle and stuck-Alt protection (18)
- `app/tests/test_input_config.py` — defaults, merge/removal durability,
  never-raise coercion, round-trips (23)
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
