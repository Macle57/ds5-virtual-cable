"""The chord engine, fed synthetic USB 0x01 reports -- no pad, no desktop.

Everything time-based runs on an injected clock, every action lands in a
recorder (`OsActions` with a fake `inject`), the dispatch worker is replaced
with an inline call, and pad feedback is collected from the `send_setstate`
callback. What is asserted is the CONTRACT the game and the OS see:

  * which bits of the forwarded report are masked, when;
  * which synthetic key/mouse events fire, in what order;
  * the chord button's tap-replay and double-press timing, to the millisecond;
  * the idle off-timer and the battery flash schedule;
  * the SetState bodies rewritten toward the pad.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import unittest

try:
    from ds5app import actions as A
    from ds5app import config as K
    from ds5app import intercept as I
    from ds5app import osk as OSK
    from ds5app import service as SVC
    from ds5bridge import protocol as P
except ImportError:  # pragma: no cover
    A = None


class FakeRenderer:
    """Records what the keyboard window would have been told."""

    def __init__(self):
        self.calls: list = []
        self.snaps: list = []

    def show(self, snap):
        self.calls.append("show")
        self.snaps.append(snap)

    def update(self, snap):
        self.calls.append("update")
        self.snaps.append(snap)

    def hide(self):
        self.calls.append("hide")

    def position(self):
        return (100, 200)

    def close(self):
        self.calls.append("close")


class FakeTouch:
    """A `actions.TouchInjector` stand-in: records contact frames."""

    def __init__(self):
        self.frames: list = []
        self._points: list = []

    def down(self, points):
        self._points = [tuple(p) for p in points]
        self.frames.append(("down", self._points))

    def move(self, points):
        self._points = [tuple(p) for p in points]
        self.frames.append(("move", self._points))

    def up(self):
        self._points = []
        self.frames.append(("up",))

    @property
    def active(self):
        return bool(self._points)


class FakeAudio:
    """An `audio_default.AudioSystem` stand-in that never touches COM."""

    def __init__(self, endpoints=(), default="mic-a"):
        self.endpoints = list(endpoints)
        self.defaults = {0: default, 1: default, 2: default}
        self.set_calls: list = []

    def capture_endpoints(self):
        return list(self.endpoints)

    def default_capture_id(self, role):
        return self.defaults.get(role)

    def set_default_capture(self, device_id, role):
        self.set_calls.append((device_id, role))
        self.defaults[role] = device_id
        return True

if A is not None:
    logging.getLogger("ds5app.intercept").addHandler(logging.NullHandler())

# --- report builder ---------------------------------------------------------

_HATS = {(): 8, ("dpad_up",): 0, ("dpad_right", "dpad_up"): 1,
         ("dpad_right",): 2, ("dpad_down", "dpad_right"): 3, ("dpad_down",): 4,
         ("dpad_down", "dpad_left"): 5, ("dpad_left",): 6,
         ("dpad_left", "dpad_up"): 7}


def report(buttons=(), lx=0x80, ly=0x80, rx=0x80, ry=0x80, l2=0, r2=0,
           touches=(), battery=8, charging=False):
    """A 64-byte USB input report 0x01. `touches`: up to two (x, y) points."""
    body = bytearray(63)
    body[0:6] = bytes([lx, ly, rx, ry, l2, r2])
    dpads = tuple(sorted(b for b in buttons if b.startswith("dpad_")))
    k = [0, 0, 0]
    for name in buttons:
        bit = I.BUTTON_BITS.get(name)
        if bit is not None:
            k[bit[0]] |= bit[1]
    body[7] = k[0] | _HATS[dpads]
    body[8], body[9] = k[1], k[2]
    for slot in (32, 36):
        body[slot] = 0x80                       # no finger
    for n, (x, y) in enumerate(touches[:2]):
        base = 32 + 4 * n
        body[base] = n                          # active, id = n
        body[base + 1] = x & 0xFF
        body[base + 2] = ((x >> 8) & 0x0F) | ((y & 0x0F) << 4)
        body[base + 3] = (y >> 4) & 0xFF
    body[52] = ((0x01 if charging else 0x00) << 4) | (battery & 0x0F)
    return bytes([0x01]) + bytes(body)


def buttons_of(out: bytes) -> set:
    """The digital state the GAME would decode from a forwarded report."""
    st = P.decode_input(out[1:], usb=True)
    held = {k for k, v in st.buttons.items() if v}
    if st.dpad != "-":
        held.add("dpad_" + {"N": "up", "E": "right", "S": "down",
                            "W": "left"}.get(st.dpad, st.dpad))
    return held


class Clock:
    def __init__(self, t=100.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


@unittest.skipIf(A is None, "the app package is not importable here")
class EngineCase(unittest.TestCase):
    """Fixture: an engine with recorders on every seam."""

    def make(self, **over):
        self.batches: list[list] = []
        self.scripts: list[str] = []
        self.launched: list[str] = []
        self.sent: list[bytes] = []
        self.modes: list[tuple] = []
        self.power_offs = 0
        self.toasts: list[tuple] = []
        self.clock = Clock()
        self.audio = FakeAudio()
        self.touch = FakeTouch()
        self.acts = A.OsActions(inject=self.batches.append,
                                run_ps=lambda s, timeout=10.0:
                                (self.scripts.append(s), True)[1],
                                launch=lambda c: (self.launched.append(c), True)[1],
                                audio=self.audio, sleep=lambda s: None,
                                touch=self.touch, cursor=lambda: (1000, 600))
        cfg = K.InputConfig.from_dict(over)

        def power_off():
            self.power_offs += 1

        self.renderer = FakeRenderer()
        self.osk = OSK.OnScreenKeyboard(self.acts,
                                        renderer_factory=lambda: self.renderer)
        self.eng = I.InputInterceptor(
            cfg, actions=self.acts, send_setstate=self.sent.append,
            power_off=power_off, clock=self.clock, dispatch=lambda fn: fn(),
            on_mode=lambda r, k: self.modes.append((r, k)), osk=self.osk,
            on_toast=lambda c, t, b: self.toasts.append((c, t, b)))
        return self.eng

    def make_remote(self, **over):
        """An engine with remote mode switched on -- it ships OFF, so every
        test that toggles into it has to opt in the way a user would -- and
        on the CLASSIC remote map (`same_bindings` and `same_gestures` off):
        these tests pin the map remote mode shipped with; `RemoteSameBindings`
        covers the default."""
        rm = over.setdefault("remote", {})
        rm.setdefault("enabled", True)
        rm.setdefault("same_bindings", False)
        rm.setdefault("same_gestures", False)
        return self.make(**over)

    @property
    def events(self):
        return [e for b in self.batches for e in b]

    def feed(self, *reports):
        out = None
        for r in reports:
            out = self.eng.on_input(r)
        return out


class Passthrough(EngineCase):
    def test_untouched_input_passes_byte_for_byte(self):
        self.make()
        r = report(buttons=("cross", "l1"), lx=0x20, r2=200,
                   touches=((960, 540),))
        self.assertEqual(self.feed(r), r)

    def test_non_input_reports_pass_untouched(self):
        self.make()
        junk = bytes([0x31]) + bytes(70)
        self.assertEqual(self.eng.on_input(junk), junk)


class ChordMasking(EngineCase):
    def test_chord_button_is_masked_from_the_first_report(self):
        self.make()
        out = self.feed(report(buttons=("ps",)))
        self.assertEqual(buttons_of(out), set())

    def test_everything_digital_is_swallowed_while_chord_held(self):
        self.make()
        out = self.feed(report(buttons=("ps",)),
                        report(buttons=("ps", "circle", "dpad_left"),
                               touches=((100, 100),)))
        self.assertEqual(buttons_of(out), set())
        st = P.decode_input(out[1:], usb=True)
        self.assertFalse(st.touch[0].active)

    def test_triggers_still_pass_during_a_chord(self):
        self.make()
        out = self.feed(report(buttons=("ps",), r2=180))
        self.assertEqual(out[6], 180)

    def test_sticks_are_centred_during_a_chord_by_default(self):
        # stick_mouse_in_chord ships ON: the sticks belong to the OS while the
        # chord is held, so the game must not see the deflection.
        self.make()
        out = self.feed(report(buttons=("ps",), lx=0x10, ry=0xF0))
        self.assertEqual(out[1], 0x80)
        self.assertEqual(out[4], 0x80)

    def test_sticks_pass_through_when_stick_mouse_in_chord_is_off(self):
        self.make(stick_mouse_in_chord=False)
        out = self.feed(report(buttons=("ps",), lx=0x10, r2=180))
        self.assertEqual(out[1], 0x10)
        self.assertEqual(out[6], 180)


class ChordActions(EngineCase):
    def test_ps_dpad_up_is_volume_up(self):
        self.make()
        self.feed(report(buttons=("ps",)),
                  report(buttons=("ps", "dpad_up")))
        self.assertIn(("key", A.VK_VOLUME_UP, True), self.events)

    def test_a_diagonal_hat_fires_exactly_one_component_chord(self):
        # NE is ambiguous between the up and right chords; the rule is one
        # chord per press, deterministically -- never both, never neither.
        self.make()
        self.feed(report(buttons=("ps",)),
                  report(buttons=("ps", "dpad_up", "dpad_right")))
        fired = [e for e in self.events
                 if e in (("key", A.VK_VOLUME_UP, True),
                          ("key", A.VK_MEDIA_NEXT_TRACK, True))]
        self.assertEqual(len(fired), 1)

    def test_ps_triangle_powers_the_pad_off(self):
        self.make()
        self.feed(report(buttons=("ps",)),
                  report(buttons=("ps", "triangle")))
        self.assertEqual(self.power_offs, 1)

    def test_each_chord_fires_once_per_press(self):
        self.make()
        self.feed(report(buttons=("ps",)),
                  report(buttons=("ps", "cross")),
                  report(buttons=("ps", "cross")),
                  report(buttons=("ps",)),
                  report(buttons=("ps", "cross")))
        plays = [e for e in self.events
                 if e == ("key", A.VK_MEDIA_PLAY_PAUSE, True)]
        self.assertEqual(len(plays), 2)

    def test_repeatable_chords_repeat_while_held(self):
        self.make()
        self.feed(report(buttons=("ps",)),
                  report(buttons=("ps", "dpad_up")))
        for _ in range(3):
            self.clock.advance(0.3)
            self.feed(report(buttons=("ps", "dpad_up")))
        ups = [e for e in self.events if e == ("key", A.VK_VOLUME_UP, True)]
        self.assertGreaterEqual(len(ups), 3)

    def test_non_repeatable_chords_do_not_repeat(self):
        self.make()
        self.feed(report(buttons=("ps",)),
                  report(buttons=("ps", "square")))
        for _ in range(5):
            self.clock.advance(0.3)
            self.feed(report(buttons=("ps", "square")))
        mutes = [e for e in self.events if e == ("key", A.VK_VOLUME_MUTE, True)]
        self.assertEqual(len(mutes), 1)

    def test_an_unknown_action_name_is_ignored(self):
        self.make(chords={"circle": "warp_drive"})
        out = self.feed(report(buttons=("ps",)),
                        report(buttons=("ps", "circle")))
        self.assertEqual(buttons_of(out), set())    # still swallowed
        self.assertEqual(self.events, [])

    def test_a_chord_removed_in_config_does_nothing(self):
        self.make(chords={"cross": "none"})
        self.feed(report(buttons=("ps",)),
                  report(buttons=("ps", "cross")))
        self.assertEqual(self.events, [])

    def test_haptic_ack_pulse_reaches_the_pad(self):
        self.make()
        self.feed(report(buttons=("ps",)),
                  report(buttons=("ps", "cross")))
        self.eng.tick()                              # the pulse is queued
        rumble = [b for b in self.sent
                  if b[P.VALID_FLAG0] & P.F0_COMPATIBLE_VIBRATION]
        self.assertTrue(rumble)
        # the default strength (25 of 100) maps linearly onto the motor byte
        self.assertEqual(rumble[0][P.BC_VIBRATION_RIGHT], 0x40)
        self.clock.advance(0.2)
        self.eng.tick()                              # and its tail returns to 0
        self.assertEqual(self.sent[-1][P.BC_VIBRATION_RIGHT], 0)

    def test_haptic_strength_scales_the_pulse(self):
        for strength, amplitude in ((100, 0xFF), (50, 0x80), (10, 0x1A)):
            self.make(haptic_strength=strength)
            self.feed(report(buttons=("ps",)),
                      report(buttons=("ps", "cross")))
            self.eng.tick()
            rumble = [b for b in self.sent
                      if b[P.VALID_FLAG0] & P.F0_COMPATIBLE_VIBRATION]
            self.assertEqual(rumble[0][P.BC_VIBRATION_RIGHT], amplitude,
                             f"strength {strength}")

    def test_haptic_strength_out_of_range_falls_back_to_default(self):
        self.make(haptic_strength=400)
        self.feed(report(buttons=("ps",)),
                  report(buttons=("ps", "cross")))
        self.eng.tick()
        rumble = [b for b in self.sent
                  if b[P.VALID_FLAG0] & P.F0_COMPATIBLE_VIBRATION]
        self.assertEqual(rumble[0][P.BC_VIBRATION_RIGHT], 0x40)

    def test_haptic_ack_can_be_configured_off(self):
        self.make(haptic_ack=False)
        self.feed(report(buttons=("ps",)),
                  report(buttons=("ps", "cross")))
        self.clock.advance(1.0)
        self.eng.tick()
        self.assertEqual(self.sent, [])


class TapReplay(EngineCase):
    def test_a_plain_tap_is_replayed_after_the_double_press_window(self):
        # Only with remote mode on is there a double press to wait out.
        self.make(remote={"enabled": True})
        self.feed(report(buttons=("ps",)), report())
        # nothing yet: the window must expire first
        self.clock.advance(0.5)
        self.eng.tick()
        out = self.feed(report())
        self.assertIn("PS", {k for k, v in
                             P.decode_input(out[1:], usb=True).buttons.items()
                             if v})
        # and it is a TAP: it ends after tap_replay_ms
        self.clock.advance(0.15)
        out = self.feed(report())
        self.assertEqual(buttons_of(out), set())

    def test_replay_is_immediate_when_remote_mode_is_disabled(self):
        self.make(remote={"enabled": False})
        self.feed(report(buttons=("ps",)))
        out = self.feed(report())
        self.assertIn("ps", {b.lower() for b in buttons_of(out)})

    def test_replay_is_immediate_by_default(self):
        # Remote mode ships OFF, so out of the box a PS tap costs no 400 ms.
        self.make()
        self.feed(report(buttons=("ps",)))
        out = self.feed(report())
        self.assertIn("ps", {b.lower() for b in buttons_of(out)})

    def test_a_used_chord_swallows_the_tap(self):
        self.make()
        self.feed(report(buttons=("ps",)),
                  report(buttons=("ps", "cross")),
                  report())
        self.clock.advance(1.0)
        self.eng.tick()
        out = self.feed(report())
        self.assertEqual(buttons_of(out), set())
        self.assertEqual(self.eng.stats["taps_replayed"], 0)

    def test_a_long_hold_alone_is_not_a_tap(self):
        self.make()
        self.feed(report(buttons=("ps",)))
        self.clock.advance(2.0)
        self.feed(report(buttons=("ps",)), report())
        self.clock.advance(1.0)
        self.eng.tick()
        out = self.feed(report())
        self.assertEqual(buttons_of(out), set())


#: PS held and the pad clicked: the shell gestures (Alt-Tab, Task View,
#: minimize-all) live on the PRESSED 2-finger rows since v1.0.
PSC = ("ps", "touchpad_click")


class Gestures(EngineCase):
    def two(self, cx, cy=500):
        """Two fingers around centroid (cx, cy)."""
        return ((cx - 50, cy), (cx + 50, cy))

    def test_horizontal_slide_holds_alt_tab_and_steps_both_ways(self):
        self.make()
        self.feed(report(buttons=("ps",)),
                  report(buttons=PSC, touches=self.two(500)),
                  report(buttons=PSC, touches=self.two(660)))
        self.assertTrue(self.acts.alt_tab_open)
        n_tabs = len([e for e in self.events if e == ("key", A.VK_TAB, True)])
        # a further step forward
        self.feed(report(buttons=PSC, touches=self.two(820)))
        self.assertEqual(
            len([e for e in self.events if e == ("key", A.VK_TAB, True)]),
            n_tabs + 1)
        # slide back: Shift+Tab
        self.feed(report(buttons=PSC, touches=self.two(660)))
        self.assertIn(("key", A.VK_SHIFT, True), self.events)
        # lifting the fingers commits (Alt up)
        self.feed(report(buttons=("ps",)))
        self.assertFalse(self.acts.alt_tab_open)
        self.assertEqual(self.events[-1], ("key", A.VK_MENU, False))

    def test_releasing_the_chord_button_commits_too(self):
        self.make()
        self.feed(report(buttons=("ps",)),
                  report(buttons=PSC, touches=self.two(500)),
                  report(buttons=PSC, touches=self.two(700)))
        self.assertTrue(self.acts.alt_tab_open)
        self.feed(report(buttons=("touchpad_click",), touches=self.two(700)))
        self.assertFalse(self.acts.alt_tab_open)

    def test_two_finger_swipe_up_is_task_view(self):
        self.make()
        self.feed(report(buttons=("ps",)),
                  report(buttons=PSC, touches=((900, 700), (1000, 700))),
                  report(buttons=PSC, touches=((900, 450), (1000, 450))))
        self.assertIn(("key", A.VK_LWIN, True), self.events)
        self.assertIn(("key", A.VK_TAB, True), self.events)

    def test_two_finger_swipe_down_is_minimize_all(self):
        self.make()
        self.feed(report(buttons=("ps",)),
                  report(buttons=PSC, touches=((900, 300), (1000, 300))),
                  report(buttons=PSC, touches=((900, 550), (1000, 550))))
        self.assertIn(("key", A.VK_M, True), self.events)

    def test_a_swipe_fires_once_per_contact(self):
        self.make()
        self.feed(report(buttons=("ps",)),
                  report(buttons=PSC, touches=((900, 700), (1000, 700))),
                  report(buttons=PSC, touches=((900, 450), (1000, 450))),
                  report(buttons=PSC, touches=((900, 200), (1000, 200))))
        wins = [e for e in self.events if e == ("key", A.VK_LWIN, True)]
        self.assertEqual(len(wins), 1)

    def test_an_unpressed_vertical_slide_scrolls_under_the_chord(self):
        self.make()
        self.feed(report(buttons=("ps",)),
                  report(buttons=("ps",), touches=((900, 300), (1000, 300))),
                  report(buttons=("ps",), touches=((900, 550), (1000, 550))))
        self.assertNotIn(("key", A.VK_M, True), self.events)
        self.assertTrue([e for e in self.events if e[0] == "wheel"])

    def test_the_touchpad_click_chord_is_not_fired_with_two_fingers_down(self):
        # PS + click = keyboard; PS + 2 fingers + click = `touch_click_2f`.
        self.make()
        self.feed(report(buttons=("ps",)),
                  report(buttons=("ps",), touches=((900, 500), (1000, 500))),
                  report(buttons=PSC, touches=((900, 500), (1000, 500))),
                  report(buttons=("ps",), touches=((900, 500), (1000, 500))))
        self.assertFalse(self.eng.keyboard_open)
        self.assertIn(("button", "right", True), self.events)

    def test_one_finger_under_the_chord_moves_the_pointer_and_is_masked(self):
        # `stick_mouse_in_chord` (ships ON) lends the 1-finger touchpad to
        # the OS during the hold, like remote mode; the game sees no touch.
        self.make()
        out = self.feed(report(buttons=("ps",)),
                        report(buttons=("ps",), touches=((500, 500),)),
                        report(buttons=("ps",), touches=((900, 500),)))
        moves = [e for e in self.events if e[0] == "move"]
        self.assertTrue(moves)
        self.assertGreater(sum(m[1] for m in moves), 0)
        self.assertFalse(P.decode_input(out[1:], usb=True).touch[0].active)
        # ... and mousing IS using the chord: no PS tap replayed afterwards
        self.feed(report())
        self.clock.advance(1.0)
        self.eng.tick()
        self.assertEqual(self.eng.stats["taps_replayed"], 0)

    def test_one_finger_tap_under_the_chord_is_a_left_click(self):
        self.make()
        self.feed(report(buttons=("ps",)),
                  report(buttons=("ps",), touches=((500, 500),)))
        self.clock.advance(0.1)
        self.feed(report(buttons=("ps",)))
        self.assertIn(("button", "left", True), self.events)
        self.assertIn(("button", "left", False), self.events)

    def test_a_finger_resting_on_the_pad_when_the_chord_lands_never_clicks(self):
        # The contact is adopted mid-way at the chord edge: it may move the
        # pointer from there, but lifting it soon after is not a tap.
        self.make()
        self.feed(report(touches=((500, 500),)),
                  report(buttons=("ps",), touches=((500, 500),)))
        self.clock.advance(0.1)
        self.feed(report(buttons=("ps",)))
        self.assertNotIn(("button", "left", True), self.events)

    def test_one_finger_under_the_chord_is_swallowed_when_the_flag_is_off(self):
        self.make(stick_mouse_in_chord=False)
        out = self.feed(report(buttons=("ps",)),
                        report(buttons=("ps",), touches=((500, 500),)),
                        report(buttons=("ps",), touches=((900, 500),)))
        self.assertEqual(self.events, [])
        self.assertFalse(P.decode_input(out[1:], usb=True).touch[0].active)


class ChordStickMouse(EngineCase):
    """`stick_mouse_in_chord` (ships ON): while the chord button is held the
    sticks drive the OS at `remote.*` speeds -- the same "major controls" as
    remote mode -- and the game sees them centred. Remote mode itself ships
    OFF; the chord-held translation must not depend on it."""

    def deflect(self, n=60, **kw):
        for _ in range(n):
            self.clock.advance(0.004)
            self.feed(report(buttons=("ps",), **kw))

    def test_left_stick_moves_the_pointer_during_a_chord(self):
        self.make()                       # remote.enabled False, and no matter
        self.feed(report(buttons=("ps",)))
        self.deflect(lx=0xFF)
        moves = [e for e in self.events if e[0] == "move"]
        self.assertTrue(moves)
        self.assertGreater(sum(m[1] for m in moves), 0)

    def test_right_stick_scrolls_during_a_chord(self):
        self.make()
        self.feed(report(buttons=("ps",)))
        self.deflect(n=120, ry=0xFF)
        wheels = [e for e in self.events if e[0] == "wheel"]
        self.assertTrue(wheels)
        self.assertLess(sum(w[1] for w in wheels), 0)

    def test_triggers_do_not_scroll_during_a_chord(self):
        # Triggers still belong to the game while a chord is held; only
        # remote mode borrows them.
        self.make()
        self.feed(report(buttons=("ps",)))
        self.deflect(n=120, r2=255)
        self.assertEqual([e for e in self.events if e[0] == "wheel"], [])

    def test_no_translation_when_the_flag_is_off(self):
        self.make(stick_mouse_in_chord=False)
        self.feed(report(buttons=("ps",)))
        self.deflect(lx=0xFF)
        self.assertEqual([e for e in self.events if e[0] == "move"], [])

    def test_stick_speed_honours_remote_mouse_speed(self):
        # The gains are cfg.remote's, so the pointer feels identical in both
        # modes -- and one dashboard slider tunes them together.
        self.make(remote={"mouse_speed": 1.0})
        self.feed(report(buttons=("ps",)))
        self.deflect(lx=0xFF)
        slow = sum(m[1] for m in self.events if m[0] == "move")
        self.make(remote={"mouse_speed": 3.0})
        self.feed(report(buttons=("ps",)))
        self.deflect(lx=0xFF)
        fast = sum(m[1] for m in self.events if m[0] == "move")
        self.assertGreater(fast, 2 * slow)

    def test_stick_use_swallows_the_tap_replay(self):
        # Mousing IS using the chord: releasing PS right after must not hand
        # the game a phantom PS press.
        self.make()
        self.feed(report(buttons=("ps",)))
        self.clock.advance(0.05)
        self.feed(report(buttons=("ps",), lx=0xFF))
        out = self.feed(report())
        self.assertEqual(buttons_of(out), set())
        self.assertEqual(self.eng.stats["taps_replayed"], 0)


class RemoteModeToggle(EngineCase):
    def enter(self):
        """A clean double press of the chord button."""
        self.feed(report(buttons=("ps",)), report())
        self.clock.advance(0.15)
        self.feed(report(buttons=("ps",)), report())

    def test_double_press_toggles_and_the_tap_never_leaks(self):
        self.make_remote()
        self.enter()
        self.assertTrue(self.eng.remote_mode)
        self.clock.advance(1.0)
        self.eng.tick()
        out = self.feed(report())
        self.assertEqual(buttons_of(out), set())
        self.assertEqual(self.eng.stats["taps_replayed"], 0)

    def test_double_press_again_leaves_remote_mode(self):
        self.make_remote()
        self.enter()
        self.clock.advance(1.0)
        self.eng.tick()
        self.enter()
        self.assertFalse(self.eng.remote_mode)

    def test_two_slow_presses_do_not_toggle(self):
        self.make_remote()
        self.feed(report(buttons=("ps",)), report())
        self.clock.advance(0.6)
        self.eng.tick()
        self.feed(report(buttons=("ps",)), report())
        self.assertFalse(self.eng.remote_mode)

    def test_entering_gives_a_double_pulse_and_the_remote_lightbar(self):
        self.make_remote()
        self.enter()
        self.eng.tick()
        lightbars = [b for b in self.sent
                     if b[P.VALID_FLAG1] & P.F1_LIGHTBAR_CONTROL]
        self.assertTrue(lightbars)
        self.assertEqual((lightbars[0][P.LED_R], lightbars[0][P.LED_G],
                          lightbars[0][P.LED_B]), (255, 120, 0))
        # the double pulse: two rumble-on bodies over the next half second
        ons = 0
        for _ in range(12):
            self.clock.advance(0.05)
            self.eng.tick()
        for b in self.sent:
            if (b[P.VALID_FLAG0] & P.F0_COMPATIBLE_VIBRATION
                    and b[P.BC_VIBRATION_RIGHT]):
                ons += 1
        self.assertEqual(ons, 2)

    def test_the_remote_colour_is_reasserted_while_the_mode_holds(self):
        # One engine write can be lost in flight (GameInput gate, a game
        # writing in a gap) -- while remote mode is on the colour claim
        # repeats about once a second, and stops the moment the mode ends.
        self.make_remote()
        self.enter()
        self.eng.tick()
        self.sent.clear()
        for _ in range(36):                       # 3.6 s in the mode
            self.clock.advance(0.1)
            self.eng.tick()
        orange = [b for b in self.sent
                  if b[P.VALID_FLAG1] & P.F1_LIGHTBAR_CONTROL
                  and (b[P.LED_R], b[P.LED_G], b[P.LED_B]) == (255, 120, 0)]
        # ~once a second: 3-4 claims over 3.6 s depending on tick phase
        self.assertIn(len(orange), (3, 4))
        self.enter()                              # leave
        self.eng.tick()
        self.sent.clear()
        for _ in range(30):
            self.clock.advance(0.1)
            self.eng.tick()
        self.assertFalse([b for b in self.sent
                          if b[P.VALID_FLAG1] & P.F1_LIGHTBAR_CONTROL])

    def test_leaving_restores_the_games_lightbar(self):
        self.make_remote()
        game = P.SetState()
        game.lightbar(10, 20, 30)
        self.eng.rewrite_setstate(bytes(game.body))   # engine learns the colour
        self.enter()
        self.clock.advance(1.0)
        self.eng.tick()
        self.sent.clear()
        self.enter()                                   # leave
        self.eng.tick()
        restored = [b for b in self.sent
                    if b[P.VALID_FLAG1] & P.F1_LIGHTBAR_CONTROL]
        self.assertTrue(restored)
        self.assertEqual((restored[-1][P.LED_R], restored[-1][P.LED_G],
                          restored[-1][P.LED_B]), (10, 20, 30))

    def test_a_game_write_mid_mode_is_restored_on_exit(self):
        # The game keeps talking while remote mode is on; its colour is learnt
        # from the rewritten traffic, so leaving restores the LATEST one.
        self.make_remote()
        self.enter()
        self.clock.advance(1.0)
        self.eng.tick()
        game = P.SetState()
        game.lightbar(40, 50, 60)                      # written mid-mode
        out = self.eng.rewrite_setstate(bytes(game.body))
        self.assertEqual((out[P.LED_R], out[P.LED_G], out[P.LED_B]),
                         (255, 120, 0))                # still overridden
        self.sent.clear()
        self.enter()                                   # leave
        self.eng.tick()
        restored = [b for b in self.sent
                    if b[P.VALID_FLAG1] & P.F1_LIGHTBAR_CONTROL]
        self.assertEqual((restored[-1][P.LED_R], restored[-1][P.LED_G],
                          restored[-1][P.LED_B]), (40, 50, 60))

    def test_remote_mode_can_be_disabled(self):
        self.make(remote={"enabled": False})
        self.enter()
        self.assertFalse(self.eng.remote_mode)

    def test_remote_mode_is_off_by_default(self):
        # It ships OFF; a double press on an untouched config must do nothing
        # (the dashboard is where a user opts in).
        self.make()
        self.enter()
        self.assertFalse(self.eng.remote_mode)
        self.assertEqual(self.eng.stats["remote_toggles"], 0)


class RemoteModeInputs(EngineCase):
    def enter(self):
        self.feed(report(buttons=("ps",)), report())
        self.clock.advance(0.15)
        self.feed(report(buttons=("ps",)), report())
        self.batches.clear()

    def test_the_game_sees_a_neutral_pad(self):
        self.make_remote()
        self.enter()
        out = self.feed(report(buttons=("cross", "dpad_up"), lx=0x00, r2=255,
                               touches=((500, 500),)))
        st = P.decode_input(out[1:], usb=True)
        self.assertEqual(buttons_of(out), set())
        self.assertEqual((st.lx, st.r2), (0x80, 0))
        self.assertFalse(st.touch[0].active)

    def test_battery_bytes_survive_neutralisation(self):
        self.make_remote()
        self.enter()
        out = self.feed(report(battery=3))
        st = P.decode_input(out[1:], usb=True)
        self.assertEqual(st.battery_level, 3)

    def test_cross_is_the_left_mouse_button_with_drag(self):
        self.make_remote()
        self.enter()
        self.feed(report(buttons=("cross",)))
        self.assertEqual(self.events[-1], ("button", "left", True))
        self.feed(report())
        self.assertEqual(self.events[-1], ("button", "left", False))

    def test_circle_is_esc_and_options_is_enter(self):
        self.make_remote()
        self.enter()
        self.feed(report(buttons=("circle",)), report(),
                  report(buttons=("options",)), report())
        self.assertIn(("key", A.VK_ESCAPE, True), self.events)
        self.assertIn(("key", A.VK_ESCAPE, False), self.events)
        self.assertIn(("key", A.VK_RETURN, True), self.events)
        self.assertIn(("key", A.VK_RETURN, False), self.events)

    def test_dpad_is_arrow_keys_held_and_released(self):
        self.make_remote()
        self.enter()
        self.feed(report(buttons=("dpad_left",)))
        self.assertEqual(self.events[-1], ("key", A.VK_LEFT, True))
        self.feed(report())
        self.assertEqual(self.events[-1], ("key", A.VK_LEFT, False))

    def test_a_held_arrow_retriggers(self):
        self.make_remote()
        self.enter()
        self.feed(report(buttons=("dpad_right",)))
        for _ in range(4):
            self.clock.advance(0.31)
            self.feed(report(buttons=("dpad_right",)))
        downs = [e for e in self.events if e == ("key", A.VK_RIGHT, True)]
        self.assertGreaterEqual(len(downs), 4)

    def test_one_finger_drag_moves_the_pointer(self):
        self.make_remote()
        self.enter()
        self.feed(report(touches=((500, 500),)),
                  report(touches=((530, 510),)))
        moves = [e for e in self.events if e[0] == "move"]
        self.assertTrue(moves)
        self.assertGreater(sum(m[1] for m in moves), 0)   # net right
        self.assertGreater(sum(m[2] for m in moves), 0)   # net down

    def test_one_finger_tap_is_a_left_click(self):
        self.make_remote()
        self.enter()
        self.feed(report(touches=((500, 500),)))
        self.clock.advance(0.1)
        self.feed(report())
        self.assertIn(("button", "left", True), self.events)
        self.assertIn(("button", "left", False), self.events)

    def test_a_long_press_is_not_a_click(self):
        self.make_remote()
        self.enter()
        self.feed(report(touches=((500, 500),)))
        self.clock.advance(0.6)
        self.feed(report(touches=((500, 500),)), report())
        self.assertNotIn(("button", "left", True), self.events)

    def test_two_finger_tap_is_a_right_click(self):
        self.make_remote()
        self.enter()
        self.feed(report(touches=((500, 500), (600, 500))))
        self.clock.advance(0.1)
        self.feed(report())
        self.assertIn(("button", "right", True), self.events)

    def test_two_finger_vertical_drag_scrolls(self):
        # Unpressed 2-finger vertical travel is a scroll, like a precision
        # touchpad: fingers moving DOWN = wheel negative (content down).
        self.make_remote()
        self.enter()
        self.feed(report(touches=((500, 300), (600, 300))))
        for y in range(360, 900, 60):
            self.feed(report(touches=((500, y), (600, y))))
        wheels = [e for e in self.events if e[0] == "wheel"]
        self.assertTrue(wheels)
        self.assertTrue(all(w[1] < 0 for w in wheels))
        self.assertFalse([e for e in self.events if e[0] == "hwheel"])

    def test_two_finger_horizontal_slide_is_alt_tab(self):
        # The chord-held gesture, available without the chord: a decisive
        # horizontal 2-finger slide WITH THE PAD CLICKED opens the switcher
        # with hold semantics.
        self.make_remote()
        self.enter()
        tc = ("touchpad_click",)
        self.feed(report(buttons=tc, touches=((450, 500), (550, 500))),
                  report(buttons=tc, touches=((610, 500), (710, 500))))
        self.assertTrue(self.acts.alt_tab_open)
        n_tabs = len([e for e in self.events if e == ("key", A.VK_TAB, True)])
        self.feed(report(buttons=tc, touches=((770, 500), (870, 500))))
        self.assertEqual(
            len([e for e in self.events if e == ("key", A.VK_TAB, True)]),
            n_tabs + 1)
        self.feed(report(buttons=tc), report())               # lift commits
        self.assertFalse(self.acts.alt_tab_open)
        self.assertIn(("key", A.VK_MENU, False), self.events)
        # the click that carried the slide never became a right click
        self.assertNotIn(("button", "right", True), self.events)

    def test_an_alt_tab_contact_stops_scrolling(self):
        self.make_remote()
        self.enter()
        tc = ("touchpad_click",)
        self.feed(report(buttons=tc, touches=((450, 500), (550, 500))),
                  report(buttons=tc, touches=((610, 500), (710, 500))))
        self.assertTrue(self.acts.alt_tab_open)
        before = [e for e in self.events if e[0] in ("wheel", "hwheel")]
        self.feed(report(buttons=tc, touches=((650, 560), (750, 560))))
        after = [e for e in self.events if e[0] in ("wheel", "hwheel")]
        self.assertEqual(before, after)

    def test_an_unpressed_horizontal_slide_scrolls_sideways(self):
        self.make_remote()
        self.enter()
        self.feed(report(touches=((450, 500), (550, 500))))
        for x in (500, 560, 620, 680):
            self.feed(report(touches=((x - 50, 500), (x + 50, 500))))
        self.assertFalse(self.acts.alt_tab_open)
        hw = [e for e in self.events if e[0] == "hwheel"]
        self.assertTrue(hw)
        self.assertTrue(all(h[1] > 0 for h in hw))         # right = positive
        self.assertFalse([e for e in self.events if e[0] == "wheel"])

    def test_a_chord_press_commits_a_remote_alt_tab(self):
        self.make_remote()
        self.enter()
        tc = ("touchpad_click",)
        self.feed(report(buttons=tc, touches=((450, 500), (550, 500))),
                  report(buttons=tc, touches=((610, 500), (710, 500))))
        self.assertTrue(self.acts.alt_tab_open)
        self.feed(report(buttons=("ps", "touchpad_click"),
                         touches=((610, 500), (710, 500))))
        self.assertFalse(self.acts.alt_tab_open)

    def test_triggers_scroll(self):
        self.make_remote()
        self.enter()
        self.feed(report())
        for _ in range(120):
            self.clock.advance(0.004)
            self.feed(report(r2=255))
        wheels = [e for e in self.events if e[0] == "wheel"]
        self.assertTrue(wheels)
        self.assertLess(sum(w[1] for w in wheels), 0)

    def test_left_stick_moves_the_pointer(self):
        self.make_remote()
        self.enter()
        self.feed(report())
        for _ in range(60):
            self.clock.advance(0.004)
            self.feed(report(lx=0xFF))
        moves = [e for e in self.events if e[0] == "move"]
        self.assertGreater(sum(m[1] for m in moves), 0)

    def test_chords_still_fire_in_remote_mode(self):
        self.make_remote()
        self.enter()
        self.feed(report(buttons=("ps",)),
                  report(buttons=("ps", "dpad_up")))
        self.assertIn(("key", A.VK_VOLUME_UP, True), self.events)
        self.assertTrue(self.eng.remote_mode)      # and it did not toggle

    def test_leaving_remote_mode_releases_held_keys(self):
        self.make_remote()
        self.enter()
        self.feed(report(buttons=("cross",)))      # left button held
        self.feed(report(buttons=("cross", "ps")))  # first toggle press
        self.assertIn(("button", "left", False), self.events)


class IdleTimer(EngineCase):
    def test_fifteen_idle_minutes_power_the_pad_off_once(self):
        self.make()
        self.feed(report())
        self.clock.advance(901)
        self.eng.tick()
        self.eng.tick()
        self.assertEqual(self.power_offs, 1)

    def test_activity_resets_the_timer(self):
        self.make()
        self.feed(report())
        self.clock.advance(600)
        self.feed(report(buttons=("cross",)))      # activity at t+600
        self.clock.advance(600)
        self.eng.tick()                            # only 600 s since activity
        self.assertEqual(self.power_offs, 0)
        self.clock.advance(301)
        self.eng.tick()
        self.assertEqual(self.power_offs, 1)

    def test_a_held_stick_counts_as_activity(self):
        self.make()
        self.feed(report())
        self.clock.advance(890)
        self.feed(report(lx=0x00))
        self.clock.advance(20)
        self.eng.tick()
        self.assertEqual(self.power_offs, 0)

    def test_off_timer_zero_disables(self):
        self.make(off_timer_minutes=0)
        self.feed(report())
        self.clock.advance(10_000)
        self.eng.tick()
        self.assertEqual(self.power_offs, 0)

    def test_no_power_off_before_any_report_ever_arrived(self):
        self.make()
        self.clock.advance(10_000)
        self.eng.tick()
        self.assertEqual(self.power_offs, 0)


class BatteryAlerts(EngineCase):
    def lightbars(self):
        return [b for b in self.sent
                if b[P.VALID_FLAG1] & P.F1_LIGHTBAR_CONTROL]

    def test_low_battery_flashes_the_low_colour(self):
        self.make()
        self.feed(report(battery=2))               # 20 %
        self.eng.tick()
        lb = self.lightbars()
        self.assertTrue(lb)
        self.assertEqual((lb[0][P.LED_R], lb[0][P.LED_G], lb[0][P.LED_B]),
                         (255, 140, 0))

    def test_critical_battery_uses_the_critical_colour(self):
        self.make()
        self.feed(report(battery=1))               # 10 %
        self.eng.tick()
        lb = self.lightbars()
        self.assertEqual((lb[0][P.LED_R], lb[0][P.LED_G], lb[0][P.LED_B]),
                         (255, 0, 0))

    def test_flashes_are_spaced_by_the_configured_interval(self):
        self.make()
        self.feed(report(battery=2))
        self.eng.tick()
        first = self.eng.stats["battery_flashes"]
        self.clock.advance(5)
        self.eng.tick()
        self.assertEqual(self.eng.stats["battery_flashes"], first)
        self.clock.advance(26)
        self.feed(report(battery=2))
        self.eng.tick()
        self.assertEqual(self.eng.stats["battery_flashes"], first + 1)

    def test_critical_flashes_come_faster(self):
        self.make()
        self.feed(report(battery=1))
        self.eng.tick()
        self.clock.advance(11)
        self.eng.tick()
        self.assertEqual(self.eng.stats["battery_flashes"], 2)

    def test_charging_never_flashes(self):
        self.make()
        self.feed(report(battery=1, charging=True))
        self.eng.tick()
        self.assertEqual(self.sent, [])

    def test_healthy_battery_never_flashes(self):
        self.make()
        self.feed(report(battery=8))
        self.eng.tick()
        self.assertEqual(self.sent, [])

    def test_alerts_can_be_disabled(self):
        self.make(battery={"enabled": False})
        self.feed(report(battery=1))
        self.eng.tick()
        self.assertEqual(self.sent, [])

    def test_the_flash_ends_by_restoring_the_games_colour(self):
        self.make()
        game = P.SetState()
        game.lightbar(0, 0, 200)
        self.eng.rewrite_setstate(bytes(game.body))
        self.feed(report(battery=2))
        for _ in range(30):
            self.clock.advance(0.1)
            self.eng.tick()
        lb = self.lightbars()
        self.assertEqual((lb[-1][P.LED_R], lb[-1][P.LED_G], lb[-1][P.LED_B]),
                         (0, 0, 200))


class LightbarRewrites(EngineCase):
    def game_body(self, r, g, b):
        st = P.SetState()
        st.lightbar(r, g, b)
        return bytes(st.body)

    def test_passthrough_when_no_policy_is_engaged(self):
        self.make()
        body = self.game_body(200, 100, 50)
        self.assertEqual(self.eng.rewrite_setstate(body), body)

    def test_dim_engages_after_the_configured_minutes(self):
        self.make(lightbar={"dim_after_minutes": 1, "dim_level": 0.5})
        self.feed(report())
        body = self.game_body(200, 100, 50)
        self.assertEqual(self.eng.rewrite_setstate(body), body)   # too early
        self.clock.advance(61)
        self.eng.tick()
        out = self.eng.rewrite_setstate(body)
        self.assertEqual((out[P.LED_R], out[P.LED_G], out[P.LED_B]),
                         (100, 50, 25))

    def test_dim_level_zero_turns_the_lightbar_off(self):
        self.make(lightbar={"dim_after_minutes": 1, "dim_level": 0})
        self.feed(report())
        self.clock.advance(61)
        self.eng.tick()
        out = self.eng.rewrite_setstate(self.game_body(200, 100, 50))
        self.assertEqual((out[P.LED_R], out[P.LED_G], out[P.LED_B]), (0, 0, 0))

    def test_remote_mode_wins_over_the_games_colour(self):
        self.make_remote()
        self.feed(report(buttons=("ps",)), report())
        self.clock.advance(0.15)
        self.feed(report(buttons=("ps",)), report())
        out = self.eng.rewrite_setstate(self.game_body(1, 2, 3))
        self.assertEqual((out[P.LED_R], out[P.LED_G], out[P.LED_B]),
                         (255, 120, 0))

    # -- PS+<chord> lightbar toggle (an engine action, like pad_power_off) --

    def _led(self, body):
        return (body[P.LED_R], body[P.LED_G], body[P.LED_B])

    def _sent_leds(self):
        """LED colours of every SetState the engine pushed to the pad."""
        out = []
        for b in self.sent:
            body = b[1:] if b and b[0] == 0x02 else b
            if len(body) > P.LED_B and body[P.VALID_FLAG1] & P.F1_LIGHTBAR_CONTROL:
                out.append(self._led(body))
        return out

    def _toggle(self):
        self.feed(report(buttons=("ps",)),
                  report(buttons=("ps", "circle")),
                  report())
        self.eng.tick()

    def test_toggle_darkens_the_pad_and_the_games_writes(self):
        self.make(chords={"circle": "pad_lightbar_toggle"})
        self.eng.rewrite_setstate(self.game_body(200, 100, 50))  # learn it
        self._toggle()
        self.assertIn((0, 0, 0), self._sent_leds())        # pushed at once
        out = self.eng.rewrite_setstate(self.game_body(200, 100, 50))
        self.assertEqual(self._led(out), (0, 0, 0))         # and held
        self.assertEqual(self.eng.stats["lightbar_toggles"], 1)

    def test_second_toggle_restores_the_games_colour(self):
        self.make(chords={"circle": "pad_lightbar_toggle"})
        self.eng.rewrite_setstate(self.game_body(200, 100, 50))
        self._toggle()
        self.sent.clear()
        self._toggle()
        self.assertIn((200, 100, 50), self._sent_leds())
        out = self.eng.rewrite_setstate(self.game_body(9, 8, 7))
        self.assertEqual(self._led(out), (9, 8, 7))          # passthrough again

    def test_off_wins_over_dim_and_remote_but_not_the_battery_flash(self):
        self.make_remote(chords={"circle": "pad_lightbar_toggle"},
                         lightbar={"dim_after_minutes": 1, "dim_level": 0.5})
        self.eng.rewrite_setstate(self.game_body(200, 100, 50))
        self._toggle()
        self.clock.advance(61)
        self.eng.tick()                                       # dim engages
        self.assertEqual(self._led(self.eng.rewrite_setstate(
            self.game_body(200, 100, 50))), (0, 0, 0))
        # enter remote mode: the double-press feedback must not light it
        self.sent.clear()
        self.feed(report(buttons=("ps",)), report())
        self.clock.advance(0.15)
        self.feed(report(buttons=("ps",)), report())
        self.eng.tick()
        self.assertTrue(self.eng.remote_mode)
        self.assertNotIn((255, 120, 0), self._sent_leds())
        self.assertEqual(self._led(self.eng.rewrite_setstate(
            self.game_body(1, 2, 3))), (0, 0, 0))
        # a battery flash still shows (it outranks everything)
        self.eng._flash_until = self.clock() + 1.0
        self.eng._flash_color = (255, 0, 0)
        self.assertEqual(self._led(self.eng.rewrite_setstate(
            self.game_body(1, 2, 3))), (255, 0, 0))

    def test_disabling_the_engine_lets_the_light_back(self):
        self.make(chords={"circle": "pad_lightbar_toggle"})
        self.eng.rewrite_setstate(self.game_body(200, 100, 50))
        self._toggle()
        self.sent.clear()
        self.eng.update_config(K.InputConfig.from_dict({"enabled": False}))
        self.eng.tick()
        self.assertIn((200, 100, 50), self._sent_leds())
        self.assertFalse(self.eng._lightbar_off)

    def test_bodies_without_the_lightbar_flag_are_untouched(self):
        self.make(lightbar={"dim_after_minutes": 1, "dim_level": 0})
        self.feed(report())
        self.clock.advance(61)
        self.eng.tick()
        st = P.SetState()
        st.rumble(0x40, 0x40)
        self.assertEqual(self.eng.rewrite_setstate(bytes(st.body)),
                         bytes(st.body))


class Wiring(EngineCase):
    def test_attach_to_backend(self):
        class Backend:
            interceptor = None

            def push_setstate_body(self, body):
                pass

            def power_off_pad(self):
                pass

        be = Backend()
        eng = I.attach_to_backend(be, K.InputConfig())
        self.assertIsNotNone(eng)
        self.assertIs(be.interceptor, eng)
        eng.close()

    def test_attach_respects_the_master_switch(self):
        class Backend:
            interceptor = None
        be = Backend()
        self.assertIsNone(I.attach_to_backend(be, K.InputConfig(enabled=False)))
        self.assertIsNone(I.attach_to_backend(be, None))
        self.assertIsNone(be.interceptor)

    def test_close_releases_a_mid_gesture_alt(self):
        self.make()
        self.feed(report(buttons=("ps",)),
                  report(buttons=PSC, touches=((450, 500), (550, 500))),
                  report(buttons=PSC, touches=((650, 500), (750, 500))))
        self.assertTrue(self.acts.alt_tab_open)
        self.eng.close()
        self.assertFalse(self.acts.alt_tab_open)

    def test_attach_allow_disabled_attaches_a_passthrough_engine(self):
        # `BridgeService` attaches even a disabled engine so the config
        # watcher can switch it on live; until then it must be a pure
        # passthrough.
        class Backend:
            interceptor = None

            def push_setstate_body(self, body):
                pass

            def power_off_pad(self):
                pass

        be = Backend()
        eng = I.attach_to_backend(be, K.InputConfig(enabled=False),
                                  allow_disabled=True)
        self.assertIsNotNone(eng)
        self.assertIs(be.interceptor, eng)
        r = report(buttons=("ps", "cross"), lx=0x10)
        self.assertEqual(eng.on_input(r), r)
        eng.close()


class UpdateConfig(EngineCase):
    """`update_config`: the dashboard's Save, landing on a running engine."""

    def enter(self):
        """A clean double press of the chord button (default window)."""
        self.feed(report(buttons=("ps",)), report())
        self.clock.advance(0.15)
        self.feed(report(buttons=("ps",)), report())

    def press_pair(self, gap: float) -> None:
        """Two chord-button taps `gap` seconds apart."""
        self.feed(report(buttons=("ps",)), report())
        self.clock.advance(gap)
        self.feed(report(buttons=("ps",)), report())

    def test_a_new_double_press_window_takes_effect(self):
        # THE user-visible bug: `double_press_ms` tuned in the dashboard did
        # nothing until a bridge restart. Two taps a second apart must fail
        # the shipped 400 ms window and pass a live-applied 1500 ms one.
        self.make_remote()
        self.press_pair(1.0)
        self.assertFalse(self.eng.remote_mode)
        self.eng.update_config(K.InputConfig.from_dict(
            {"remote": {"enabled": True}, "double_press_ms": 1500}))
        self.clock.advance(2.0)
        self.press_pair(1.0)
        self.assertTrue(self.eng.remote_mode)

    def test_haptic_strength_is_recomputed(self):
        self.make()
        self.eng.update_config(K.InputConfig.from_dict(
            {"haptic_strength": 100}))
        self.feed(report(buttons=("ps",)), report(buttons=("ps", "cross")))
        self.eng.tick()
        rumble = [b for b in self.sent
                  if b[P.VALID_FLAG0] & P.F0_COMPATIBLE_VIBRATION]
        self.assertTrue(rumble)
        self.assertEqual(rumble[0][P.BC_VIBRATION_RIGHT], 0xFF)

    def test_disable_releases_everything_and_passes_through(self):
        self.make_remote()
        game = P.SetState()
        game.lightbar(10, 20, 30)
        self.eng.rewrite_setstate(bytes(game.body))   # engine learns the colour
        self.enter()
        self.clock.advance(1.0)
        self.eng.tick()
        self.assertTrue(self.eng.remote_mode)
        self.feed(report(buttons=("cross",)))          # left mouse held (drag)
        self.assertIn(("button", "left", True), self.events)
        self.sent.clear()

        self.eng.update_config(K.InputConfig.from_dict({"enabled": False}))
        # remote mode exited, the held OS button let go IMMEDIATELY -- there
        # is no "next report" release path once the engine is passthrough
        self.assertFalse(self.eng.remote_mode)
        self.assertIn(("button", "left", False), self.events)
        # tick drains the goodbye effects (exit pulse, lightbar restore) ...
        for _ in range(30):
            self.clock.advance(0.05)
            self.eng.tick()
        lightbars = [b for b in self.sent
                     if b[P.VALID_FLAG1] & P.F1_LIGHTBAR_CONTROL]
        self.assertTrue(lightbars)
        self.assertEqual((lightbars[-1][P.LED_R], lightbars[-1][P.LED_G],
                          lightbars[-1][P.LED_B]), (10, 20, 30))
        # ... and then the game sees an honest pad, byte for byte
        r = report(buttons=("cross", "ps"), lx=0x10, touches=((500, 500),))
        self.assertEqual(self.feed(r), r)
        # no idle power-off from a timer the user just switched off
        self.clock.advance(3600)
        self.eng.tick()
        self.assertEqual(self.power_offs, 0)

    def test_setstate_rewrites_stop_while_disabled(self):
        self.make(lightbar={"dim_after_minutes": 1.0, "dim_level": 0.5})
        self.feed(report())
        self.clock.advance(61)
        self.eng.tick()                                # dim engages
        game = P.SetState()
        game.lightbar(100, 100, 100)
        out = self.eng.rewrite_setstate(bytes(game.body))
        self.assertEqual(out[P.LED_R], 50)             # dimmed
        self.eng.update_config(K.InputConfig.from_dict({"enabled": False}))
        out = self.eng.rewrite_setstate(bytes(game.body))
        self.assertEqual(out[P.LED_R], 100)            # passthrough

    def test_reenable_resumes_with_a_fresh_idle_clock(self):
        self.make()
        self.feed(report())
        self.eng.update_config(K.InputConfig.from_dict({"enabled": False}))
        # An hour passes with the engine off; the stale `_last_activity`,
        # if honoured, would power the pad off seconds after re-enabling.
        self.clock.advance(3600)
        self.eng.update_config(K.InputConfig())
        self.eng.tick()
        self.assertEqual(self.power_offs, 0)
        out = self.feed(report(buttons=("ps",)))       # masking is back
        self.assertEqual(buttons_of(out), set())
        self.feed(report())
        self.clock.advance(901)                        # and the timer works,
        self.eng.tick()                                # counted from NOW
        self.assertEqual(self.power_offs, 1)

    def test_chord_button_change_mid_hold_frees_the_old_button(self):
        self.make()
        out = self.feed(report(buttons=("ps",)))
        self.assertEqual(buttons_of(out), set())       # held and masked
        self.eng.update_config(K.InputConfig.from_dict(
            {"chord_button": "mute"}))
        out = self.feed(report(buttons=("ps",)))
        self.assertEqual(buttons_of(out), {"PS"})      # honest on the NEXT
                                                       # report (decoder name)
        self.feed(report())
        self.assertEqual(self.eng.stats["taps_replayed"], 0)   # no phantom tap
        out = self.feed(report(buttons=("mute",)))
        self.assertEqual(buttons_of(out), set())       # the new button arms
        self.feed(report(buttons=("mute", "dpad_up")))
        self.assertIn(("key", A.VK_VOLUME_UP, True), self.events)

    def test_disabling_remote_alone_exits_remote_mode(self):
        self.make_remote()
        self.enter()
        self.assertTrue(self.eng.remote_mode)
        self.eng.update_config(K.InputConfig.from_dict(
            {"remote": {"enabled": False}}))
        self.assertFalse(self.eng.remote_mode)
        out = self.feed(report(buttons=("ps",)))       # chords still armed
        self.assertEqual(buttons_of(out), set())

    def test_disable_commits_a_mid_gesture_alt_tab(self):
        self.make()
        self.feed(report(buttons=("ps",)),
                  report(buttons=PSC, touches=((450, 500), (550, 500))),
                  report(buttons=PSC, touches=((650, 500), (750, 500))))
        self.assertTrue(self.acts.alt_tab_open)
        self.eng.update_config(K.InputConfig.from_dict({"enabled": False}))
        self.assertFalse(self.acts.alt_tab_open)       # no stuck Alt, ever


@unittest.skipIf(A is None, "the app package is not importable here")
class LiveSettingsWatcher(unittest.TestCase):
    """`service.InputConfigWatcher`: the file-to-engine half of the pipeline.

    Tested here rather than beside `BridgeService` because the watcher exists
    for the engine: it is the delivery mechanism for every `update_config`
    case above. All I/O is against a temp file with mtimes set explicitly --
    `os.utime` -- so nothing sleeps and nothing depends on filesystem
    timestamp granularity.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "config.json")
        self.applied: list = []
        self.current = K.InputConfig()

        def apply(cfg):
            self.applied.append(cfg)
            self.current = cfg

        self.watcher = SVC.InputConfigWatcher(
            apply, lambda: self.current, path=self.path, interval=0.01)

    def write(self, data: dict, mtime_s: int) -> None:
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.utime(self.path, ns=(mtime_s * 10**9, mtime_s * 10**9))

    def write_text(self, text: str, mtime_s: int) -> None:
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(text)
        os.utime(self.path, ns=(mtime_s * 10**9, mtime_s * 10**9))

    def test_a_changed_input_section_is_applied(self):
        self.write({"input": {"double_press_ms": 900}}, 1)
        self.assertTrue(self.watcher.poll_once())
        self.assertEqual(len(self.applied), 1)
        self.assertEqual(self.applied[0].double_press_ms, 900)

    def test_an_identical_section_is_not_applied(self):
        # A save that only flipped a bridging switch must not touch the engine.
        self.write({"enabled": False,
                    "input": K.InputConfig().to_dict()}, 1)
        self.assertFalse(self.watcher.poll_once())
        self.assertEqual(self.applied, [])

    def test_an_unmoved_mtime_is_stat_only(self):
        self.write({"input": {"double_press_ms": 900}}, 1)
        self.assertTrue(self.watcher.poll_once())
        self.assertFalse(self.watcher.poll_once())     # same mtime: no re-read
        self.assertEqual(len(self.applied), 1)

    def test_a_missing_file_is_nothing_to_do(self):
        self.assertFalse(self.watcher.poll_once())
        self.assertEqual(self.applied, [])

    def test_an_unreadable_file_is_skipped_not_quarantined(self):
        self.write({"input": {"double_press_ms": 900}}, 1)
        self.assertTrue(self.watcher.poll_once())
        self.write_text("{ this is not json", 2)
        self.assertFalse(self.watcher.poll_once())
        self.assertEqual(len(self.applied), 1)         # the engine keeps 900
        # and, unlike `config.load`, the user's file is left exactly in place
        self.assertTrue(os.path.exists(self.path))
        self.assertFalse(os.path.exists(self.path + ".bad"))

    def test_a_fixed_file_applies_on_the_next_touch(self):
        self.write_text("{ this is not json", 1)
        self.assertFalse(self.watcher.poll_once())
        self.write({"input": {"double_press_ms": 1200}}, 2)
        self.assertTrue(self.watcher.poll_once())
        self.assertEqual(self.applied[-1].double_press_ms, 1200)

    def test_start_and_stop_join_the_thread(self):
        self.watcher.start()
        self.watcher.stop()
        self.assertIsNone(self.watcher._thread)

    def test_service_apply_updates_the_engine_and_its_own_copy(self):
        # The seam the watcher calls into: `BridgeService._apply_input_config`
        # must move `input_config` (what `current_fn` reports next poll) and
        # the engine together.
        svc = SVC.BridgeService(serial="ab", input_config=K.InputConfig())
        got: list = []

        class Eng:
            def update_config(self, cfg):
                got.append(cfg)

        svc._interceptor = Eng()
        new = K.InputConfig.from_dict({"double_press_ms": 900})
        svc._apply_input_config(new)
        self.assertIs(svc.input_config, new)
        self.assertEqual(got, [new])


# ---------------------------------------------------------------------------
# table-driven remote mode, the on-screen keyboard, mode reporting
# ---------------------------------------------------------------------------


class RemoteBindings(EngineCase):
    """Remote mode's buttons and gestures resolve through a table."""

    def enter(self):
        self.feed(report(buttons=("ps",)), report())
        self.clock.advance(0.15)
        self.feed(report(buttons=("ps",)), report())
        self.batches.clear()

    def test_the_classic_table_reproduces_the_shipped_map(self):
        # DEFAULT_REMOTE_CHORDS is today's remote mode, spelt out.
        d = K.DEFAULT_REMOTE_CHORDS
        self.assertEqual(d["cross"], "left_click")
        self.assertEqual(d["circle"], "escape")
        self.assertEqual(d["options"], "enter")
        self.assertEqual((d["dpad_up"], d["dpad_down"], d["dpad_left"],
                          d["dpad_right"]),
                         ("arrow_up", "arrow_down", "arrow_left", "arrow_right"))
        self.assertEqual(d["touch_slide_left_pressed"], "alt_tab")
        self.assertEqual(d["touch_slide_right_pressed"], "alt_tab")
        self.assertEqual(d["touch_slide_left"], "scroll_left")
        self.assertEqual(d["touch_slide_right"], "scroll_right")
        self.assertEqual(d["touch_tap_2f"], "right_click")
        self.assertEqual(d["touch_click_2f"], "right_click")
        self.assertNotIn("touch_pinch_pressed", d)

    def test_a_rebound_button_fires_the_new_action_directly(self):
        self.make_remote(remote={"chords": {"cross": "media_play_pause"}})
        self.enter()
        self.feed(report(buttons=("cross",)), report())
        self.assertIn(("key", A.VK_MEDIA_PLAY_PAUSE, True), self.events)
        self.assertNotIn(("button", "left", True), self.events)

    def test_none_removes_a_default_and_the_button_does_nothing(self):
        self.make_remote(remote={"chords": {"cross": "none"}})
        self.enter()
        self.feed(report(buttons=("cross",)), report())
        self.assertEqual(self.events, [])

    def test_a_hold_action_is_held_for_the_press(self):
        self.make_remote(remote={"chords": {"square": "right_click"}})
        self.enter()
        self.feed(report(buttons=("square",)))
        self.assertEqual(self.events[-1], ("button", "right", True))
        self.feed(report(buttons=("square",)))
        self.assertEqual(len(self.events), 1)               # held, not repeated
        self.feed(report())
        self.assertEqual(self.events[-1], ("button", "right", False))

    def test_a_repeatable_tap_action_repeats_at_repeat_ms(self):
        self.make_remote(remote={"chords": {"square": "volume_up"}},
                         repeat_ms=300)
        self.enter()
        self.feed(report(buttons=("square",)))
        for _ in range(4):
            self.clock.advance(0.31)
            self.feed(report(buttons=("square",)))
        ups = [e for e in self.events if e == ("key", A.VK_VOLUME_UP, True)]
        self.assertGreaterEqual(len(ups), 4)
        self.feed(report())
        n = len(ups)
        self.clock.advance(1.0)
        self.feed(report())
        self.assertEqual(len([e for e in self.events
                              if e == ("key", A.VK_VOLUME_UP, True)]), n)

    def test_a_diagonal_dpad_fires_one_arrow(self):
        self.make_remote()
        self.enter()
        self.feed(report(buttons=("dpad_up", "dpad_right")))
        downs = [e for e in self.events if e[0] == "key" and e[2]]
        self.assertEqual(len(downs), 1)

    def test_remote_buttons_do_not_rumble(self):
        self.make_remote()
        self.enter()
        for _ in range(20):
            self.clock.advance(0.05)
            self.eng.tick()
        self.sent.clear()
        self.feed(report(buttons=("cross",)), report())
        self.eng.tick()
        self.assertFalse([b for b in self.sent
                          if b[P.VALID_FLAG0] & P.F0_COMPATIBLE_VIBRATION
                          and b[P.BC_VIBRATION_RIGHT]])

    def test_a_remote_swipe_fires_when_bound(self):
        # An up row bound to a one-shot is a swipe up.
        self.make_remote(remote={"chords": {"touch_slide_up": "task_view",
                                            "touch_slide_down": "none"}})
        self.enter()
        self.feed(report(touches=((450, 800), (550, 800))))
        for y in (740, 680, 620, 560, 500):
            self.feed(report(touches=((450, y), (550, y))))
        self.assertIn(("key", A.VK_TAB, True), self.events)
        self.assertIn(("key", A.VK_LWIN, True), self.events)
        self.assertFalse(self.acts.alt_tab_open)

    def test_a_pad_action_from_remote_mode(self):
        self.make_remote(remote={"chords": {"triangle": "pad_power_off"}})
        self.enter()
        self.feed(report(buttons=("triangle",)))
        self.assertEqual(self.power_offs, 1)

    def test_an_unknown_action_is_ignored_once_with_a_log_line(self):
        self.make_remote(remote={"chords": {"cross": "no_such_thing"}})
        self.enter()
        with self.assertLogs("ds5app.intercept", level="WARNING") as cm:
            self.feed(report(buttons=("cross",)), report(),
                      report(buttons=("cross",)), report())
        self.assertEqual(len(cm.output), 1)
        self.assertEqual(self.events, [])

    def test_a_config_change_swaps_the_table_live(self):
        self.make_remote()
        self.enter()
        new = K.InputConfig.from_dict(
            {"remote": {"enabled": True, "same_bindings": False,
                        "chords": {"cross": "volume_mute"}}})
        self.eng.update_config(new)
        self.feed(report(buttons=("cross",)), report())
        self.assertIn(("key", A.VK_VOLUME_MUTE, True), self.events)
        self.assertNotIn(("button", "left", True), self.events)

    def test_leaving_the_mode_releases_a_held_binding(self):
        self.make_remote()
        self.enter()
        self.feed(report(buttons=("circle",)))                  # Esc held
        self.assertEqual(self.events[-1], ("key", A.VK_ESCAPE, True))
        new = K.InputConfig.from_dict({"remote": {"enabled": False}})
        self.eng.update_config(new)
        self.assertIn(("key", A.VK_ESCAPE, False), self.events)


class RemoteSameBindings(EngineCase):
    """`same_bindings` (the default): remote mode uses the chord table."""

    def enter(self):
        self.feed(report(buttons=("ps",)), report())
        self.clock.advance(0.15)
        self.feed(report(buttons=("ps",)), report())
        self.batches.clear()

    def test_same_bindings_is_the_default(self):
        self.assertTrue(K.RemoteMode().same_bindings)
        self.assertTrue(K.InputConfig.from_dict({}).remote.same_bindings)

    def test_buttons_mean_what_the_chord_means(self):
        self.make(remote={"enabled": True})
        self.enter()
        self.feed(report(buttons=("cross",)), report())
        self.assertIn(("key", A.VK_MEDIA_PLAY_PAUSE, True), self.events)
        self.assertNotIn(("button", "left", True), self.events)
        self.feed(report(buttons=("dpad_up",)), report())
        self.assertIn(("key", A.VK_VOLUME_UP, True), self.events)

    def test_gestures_mean_what_the_chord_gesture_means(self):
        self.make(remote={"enabled": True})
        self.enter()
        tc = ("touchpad_click",)
        self.feed(report(buttons=tc, touches=((450, 300), (550, 300))))
        for y in (360, 420, 480, 540, 600):
            self.feed(report(buttons=tc, touches=((450, y), (550, y))))
        self.assertIn(("key", A.VK_M, True), self.events)          # minimize_all

    def test_same_gestures_is_the_default_and_independent_of_bindings(self):
        self.assertTrue(K.RemoteMode().same_gestures)
        rm = K.InputConfig.from_dict({"remote": {"same_bindings": False}}).remote
        self.assertTrue(rm.same_gestures)
        rm = K.InputConfig.from_dict({"remote": {"same_gestures": False}}).remote
        self.assertTrue(rm.same_bindings)
        self.assertFalse(rm.same_gestures)

    def test_same_gestures_takes_the_chord_tables_gesture_rows(self):
        # The chord table leaves `touch_tap_2f` unbound; the remote table
        # binds it to right_click. With same_gestures (default) the chord
        # table wins even though the BUTTONS come from the remote table.
        self.make(remote={"enabled": True, "same_bindings": False})
        self.enter()
        self.feed(report(touches=((500, 500), (600, 500))))
        self.clock.advance(0.1)
        self.feed(report())
        self.assertNotIn(("button", "right", True), self.events)
        self.feed(report(buttons=("cross",)))                     # remote buttons
        self.assertEqual(self.events[-1], ("button", "left", True))

    def test_same_gestures_off_takes_the_remote_tables_gesture_rows(self):
        self.make(remote={"enabled": True, "same_gestures": False,
                          "chords": {"touch_slide_down": "volume_up"}})
        self.enter()
        self.feed(report(touches=((500, 500), (600, 500))))
        self.clock.advance(0.1)
        self.feed(report())
        self.assertIn(("button", "right", True), self.events)     # tap_2f
        self.batches.clear()
        self.feed(report(touches=((500, 300), (600, 300))),
                  report(touches=((500, 550), (600, 550))))
        self.assertIn(("key", A.VK_VOLUME_UP, True), self.events)
        self.assertFalse([e for e in self.events if e[0] == "wheel"])
        # ... while the buttons still follow `same_bindings` (the chord table)
        self.feed(report(buttons=("cross",)), report())
        self.assertIn(("key", A.VK_MEDIA_PLAY_PAUSE, True), self.events)

    def test_the_gesture_tables_swap_live(self):
        self.make(remote={"enabled": True})
        self.enter()
        self.eng.update_config(K.InputConfig.from_dict(
            {"remote": {"enabled": True, "same_gestures": False}}))
        self.feed(report(touches=((500, 500), (600, 500))))
        self.clock.advance(0.1)
        self.feed(report())
        self.assertIn(("button", "right", True), self.events)

    def test_the_intrinsic_pointer_controls_stay(self):
        self.make(remote={"enabled": True})
        self.enter()
        self.feed(report(touches=((500, 500),)))
        self.clock.advance(0.1)
        self.feed(report())
        self.assertIn(("button", "left", True), self.events)       # tap = click
        self.feed(report(touches=((500, 500),)), report(touches=((540, 520),)))
        self.assertTrue([e for e in self.events if e[0] == "move"])

    def test_the_remote_table_is_ignored_while_same_bindings(self):
        self.make(remote={"enabled": True, "chords": {"cross": "volume_mute"}})
        self.enter()
        self.feed(report(buttons=("cross",)), report())
        self.assertNotIn(("key", A.VK_VOLUME_MUTE, True), self.events)
        self.assertIn(("key", A.VK_MEDIA_PLAY_PAUSE, True), self.events)

    def test_the_chord_table_still_binds_through_a_live_flip(self):
        self.make_remote()                                          # classic
        self.enter()
        self.eng.update_config(K.InputConfig.from_dict(
            {"remote": {"enabled": True, "same_bindings": True}}))
        self.feed(report(buttons=("cross",)), report())
        self.assertIn(("key", A.VK_MEDIA_PLAY_PAUSE, True), self.events)


class Keyboard(EngineCase):
    """The `keyboard` engine action and what the game sees meanwhile."""

    def open_by_chord(self):
        self.feed(report(buttons=("ps",)),
                  report(buttons=("ps", "touchpad_click")),
                  report(buttons=("ps",)), report())
        self.batches.clear()

    def test_the_default_chord_opens_it_and_the_window_is_shown(self):
        self.make()
        self.open_by_chord()
        self.assertTrue(self.eng.keyboard_open)
        self.assertEqual(self.renderer.calls[0], "show")
        self.assertEqual(self.eng.stats["keyboard_toggles"], 1)

    def test_the_game_sees_a_neutral_pad_while_open(self):
        self.make()
        self.open_by_chord()
        out = self.feed(report(buttons=("cross", "dpad_up"), lx=0x00, r2=255,
                               touches=((500, 500),)))
        st = P.decode_input(out[1:], usb=True)
        self.assertEqual(buttons_of(out), set())
        self.assertEqual((st.lx, st.r2), (0x80, 0))
        self.assertFalse(st.touch[0].active)

    def test_cross_types_the_highlighted_key(self):
        self.make()
        self.open_by_chord()
        self.feed(report())                                   # arming frame
        self.feed(report(buttons=("cross",)))
        self.assertEqual(self.events[-2:], [("unicode", ord("g"), True),
                                            ("unicode", ord("g"), False)])

    def test_navigation_repaints_and_square_backspaces(self):
        self.make()
        self.open_by_chord()
        self.feed(report(), report(buttons=("dpad_right",)))
        self.assertEqual(self.renderer.calls[-1], "update")
        self.assertTrue(self.renderer.snaps[-1]["keys"])
        self.feed(report(), report(buttons=("square",)))
        self.assertIn(("key", A.VK_BACK, True), self.events)

    def test_circle_closes_it_and_the_press_never_reaches_the_game(self):
        self.make()
        self.open_by_chord()
        self.feed(report())
        out = self.feed(report(buttons=("circle",)))
        self.assertEqual(buttons_of(out), set())
        self.assertFalse(self.eng.keyboard_open)
        self.assertEqual(self.renderer.calls[-1], "hide")
        out = self.feed(report(), report(buttons=("cross",)))
        self.assertIn("x", buttons_of(out))                   # game has the pad back

    def test_the_chord_toggles_it_closed_again(self):
        self.make()
        self.open_by_chord()
        self.open_by_chord()
        self.assertFalse(self.eng.keyboard_open)

    def test_it_works_on_top_of_remote_mode_and_suspends_its_map(self):
        self.make_remote(remote={"chords": {"square": "keyboard"}})
        self.feed(report(buttons=("ps",)), report())
        self.clock.advance(0.15)
        self.feed(report(buttons=("ps",)), report())
        self.feed(report(buttons=("cross",)))                 # left button held
        self.assertEqual(self.events[-1], ("button", "left", True))
        self.feed(report(buttons=("cross", "square")))        # opens the keyboard
        self.assertTrue(self.eng.keyboard_open)
        self.assertIn(("button", "left", False), self.events) # released
        self.batches.clear()
        self.feed(report())                                   # arming
        self.feed(report(buttons=("triangle",)))
        self.assertEqual(self.events[-2][0], "unicode")       # Space typed
        self.assertTrue(self.eng.remote_mode)

    def test_chords_still_fire_while_open(self):
        self.make()
        self.open_by_chord()
        self.feed(report(buttons=("ps",)), report(buttons=("ps", "dpad_up")))
        self.assertIn(("key", A.VK_VOLUME_UP, True), self.events)
        self.assertTrue(self.eng.keyboard_open)

    def test_disabling_the_engine_closes_it(self):
        self.make()
        self.open_by_chord()
        self.eng.update_config(K.InputConfig.from_dict({"enabled": False}))
        self.assertFalse(self.eng.keyboard_open)
        self.assertEqual(self.renderer.calls[-1], "hide")

    def test_close_shuts_the_window_down(self):
        self.make()
        self.open_by_chord()
        self.eng.close()
        self.assertEqual(self.renderer.calls[-1], "close")
        self.assertFalse(self.eng.keyboard_open)

    def test_the_keyboard_action_is_an_engine_action_for_the_pickers(self):
        self.assertIn("keyboard", K.ENGINE_ACTIONS)
        self.assertEqual(K.DEFAULT_CHORDS["touchpad_click"], "keyboard")
        self.assertEqual(K.DEFAULT_CHORDS["mute"], "dictation")


class ModeReporting(EngineCase):
    def enter(self):
        self.feed(report(buttons=("ps",)), report())
        self.clock.advance(0.15)
        self.feed(report(buttons=("ps",)), report())

    def test_on_mode_fires_on_every_flip_only(self):
        self.make_remote()
        self.feed(report(), report(), report())
        self.assertEqual(self.modes, [(False, False)])
        self.enter()
        self.assertEqual(self.modes[-1], (True, False))
        self.feed(report(buttons=("ps",)),
                  report(buttons=("ps", "touchpad_click")),
                  report(buttons=("ps",)), report())
        self.assertEqual(self.modes[-1], (True, True))
        self.assertEqual(len(self.modes), 3)
        self.eng.update_config(K.InputConfig.from_dict({"enabled": False}))
        self.assertEqual(self.modes[-1], (False, False))

    def test_the_attributes_the_telemetry_publisher_reads(self):
        self.make_remote()
        self.assertFalse(self.eng.remote_mode)
        self.assertFalse(self.eng.keyboard_open)
        self.enter()
        self.assertTrue(self.eng.remote_mode)

    def test_attach_passes_on_mode_through(self):
        class Backend:
            interceptor = None

            def push_setstate_body(self, b):
                pass

            def power_off_pad(self):
                pass

        seen = []
        acts = A.OsActions(inject=lambda e: None, audio=object(),
                           sleep=lambda s: None)
        eng = I.attach_to_backend(Backend(), K.InputConfig(), actions=acts,
                                  on_mode=lambda r, k: seen.append((r, k)))
        try:
            eng.on_input(report())
            self.assertEqual(seen, [(False, False)])
        finally:
            eng.close()


class DisplayAndDictationChords(EngineCase):
    def test_display_actions_launch_displayswitch(self):
        self.make(chords={"square": "display_extend", "cross": "display_cycle"})
        self.feed(report(buttons=("ps",)), report(buttons=("ps", "square")))
        self.assertEqual(len(self.launched), 1)
        self.assertIn("DisplaySwitch.exe", self.launched[0])
        self.assertTrue(self.launched[0].endswith("/extend"))
        self.feed(report(buttons=("ps",)), report(buttons=("ps", "cross")))
        self.assertTrue(self.launched[-1].endswith("/external"))  # after extend

    def test_dictation_chord_borrows_the_pad_mic_and_close_restores(self):
        from ds5app import audio_default as AD
        self.make()
        self.audio.endpoints = [AD.Endpoint(
            "pad", "Headset Microphone (2- DualSense Wireless Controller)")]
        self.feed(report(buttons=("ps",)), report(buttons=("ps", "mute")))
        self.assertEqual(self.audio.defaults[0], "pad")
        self.assertIn(("key", A.VK_H, True), self.events)
        self.eng.close()
        self.assertEqual(self.audio.defaults[0], "mic-a")


if __name__ == "__main__":
    unittest.main()
