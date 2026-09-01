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

import logging
import unittest

try:
    from ds5app import actions as A
    from ds5app import config as K
    from ds5app import intercept as I
    from ds5bridge import protocol as P
except ImportError:  # pragma: no cover
    A = None

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
        self.sent: list[bytes] = []
        self.power_offs = 0
        self.clock = Clock()
        self.acts = A.OsActions(inject=self.batches.append,
                                run_ps=lambda s, timeout=10.0:
                                (self.scripts.append(s), True)[1])
        cfg = K.InputConfig.from_dict(over)

        def power_off():
            self.power_offs += 1

        self.eng = I.InputInterceptor(
            cfg, actions=self.acts, send_setstate=self.sent.append,
            power_off=power_off, clock=self.clock, dispatch=lambda fn: fn())
        return self.eng

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

    def test_sticks_and_triggers_still_pass_during_a_chord(self):
        self.make()
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
        self.assertEqual(rumble[0][P.BC_VIBRATION_RIGHT], I.ACK_RUMBLE)
        self.clock.advance(0.2)
        self.eng.tick()                              # and its tail returns to 0
        self.assertEqual(self.sent[-1][P.BC_VIBRATION_RIGHT], 0)

    def test_haptic_ack_can_be_configured_off(self):
        self.make(haptic_ack=False)
        self.feed(report(buttons=("ps",)),
                  report(buttons=("ps", "cross")))
        self.clock.advance(1.0)
        self.eng.tick()
        self.assertEqual(self.sent, [])


class TapReplay(EngineCase):
    def test_a_plain_tap_is_replayed_after_the_double_press_window(self):
        self.make()
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


class Gestures(EngineCase):
    def two(self, cx, cy=500):
        """Two fingers around centroid (cx, cy)."""
        return ((cx - 50, cy), (cx + 50, cy))

    def test_horizontal_slide_holds_alt_tab_and_steps_both_ways(self):
        self.make()
        self.feed(report(buttons=("ps",)),
                  report(buttons=("ps",), touches=self.two(500)),
                  report(buttons=("ps",), touches=self.two(660)))
        self.assertTrue(self.acts.alt_tab_open)
        n_tabs = len([e for e in self.events if e == ("key", A.VK_TAB, True)])
        # a further step forward
        self.feed(report(buttons=("ps",), touches=self.two(820)))
        self.assertEqual(
            len([e for e in self.events if e == ("key", A.VK_TAB, True)]),
            n_tabs + 1)
        # slide back: Shift+Tab
        self.feed(report(buttons=("ps",), touches=self.two(660)))
        self.assertIn(("key", A.VK_SHIFT, True), self.events)
        # lifting the fingers commits (Alt up)
        self.feed(report(buttons=("ps",)))
        self.assertFalse(self.acts.alt_tab_open)
        self.assertEqual(self.events[-1], ("key", A.VK_MENU, False))

    def test_releasing_the_chord_button_commits_too(self):
        self.make()
        self.feed(report(buttons=("ps",)),
                  report(buttons=("ps",), touches=self.two(500)),
                  report(buttons=("ps",), touches=self.two(700)),
                  report(touches=self.two(700)))
        self.assertFalse(self.acts.alt_tab_open)

    def test_two_finger_swipe_up_is_task_view(self):
        self.make()
        self.feed(report(buttons=("ps",)),
                  report(buttons=("ps",), touches=((900, 700), (1000, 700))),
                  report(buttons=("ps",), touches=((900, 450), (1000, 450))))
        self.assertIn(("key", A.VK_LWIN, True), self.events)
        self.assertIn(("key", A.VK_TAB, True), self.events)

    def test_two_finger_swipe_down_is_minimize_all(self):
        self.make()
        self.feed(report(buttons=("ps",)),
                  report(buttons=("ps",), touches=((900, 300), (1000, 300))),
                  report(buttons=("ps",), touches=((900, 550), (1000, 550))))
        self.assertIn(("key", A.VK_M, True), self.events)

    def test_a_swipe_fires_once_per_contact(self):
        self.make()
        self.feed(report(buttons=("ps",)),
                  report(buttons=("ps",), touches=((900, 700), (1000, 700))),
                  report(buttons=("ps",), touches=((900, 450), (1000, 450))),
                  report(buttons=("ps",), touches=((900, 200), (1000, 200))))
        wins = [e for e in self.events if e == ("key", A.VK_LWIN, True)]
        self.assertEqual(len(wins), 1)

    def test_one_finger_under_the_chord_is_just_swallowed(self):
        self.make()
        out = self.feed(report(buttons=("ps",)),
                        report(buttons=("ps",), touches=((500, 500),)),
                        report(buttons=("ps",), touches=((900, 500),)))
        self.assertEqual(self.events, [])
        self.assertFalse(P.decode_input(out[1:], usb=True).touch[0].active)


class RemoteModeToggle(EngineCase):
    def enter(self):
        """A clean double press of the chord button."""
        self.feed(report(buttons=("ps",)), report())
        self.clock.advance(0.15)
        self.feed(report(buttons=("ps",)), report())

    def test_double_press_toggles_and_the_tap_never_leaks(self):
        self.make()
        self.enter()
        self.assertTrue(self.eng.remote_mode)
        self.clock.advance(1.0)
        self.eng.tick()
        out = self.feed(report())
        self.assertEqual(buttons_of(out), set())
        self.assertEqual(self.eng.stats["taps_replayed"], 0)

    def test_double_press_again_leaves_remote_mode(self):
        self.make()
        self.enter()
        self.clock.advance(1.0)
        self.eng.tick()
        self.enter()
        self.assertFalse(self.eng.remote_mode)

    def test_two_slow_presses_do_not_toggle(self):
        self.make()
        self.feed(report(buttons=("ps",)), report())
        self.clock.advance(0.6)
        self.eng.tick()
        self.feed(report(buttons=("ps",)), report())
        self.assertFalse(self.eng.remote_mode)

    def test_entering_gives_a_double_pulse_and_the_remote_lightbar(self):
        self.make()
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

    def test_leaving_restores_the_games_lightbar(self):
        self.make()
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

    def test_remote_mode_can_be_disabled(self):
        self.make(remote={"enabled": False})
        self.enter()
        self.assertFalse(self.eng.remote_mode)


class RemoteModeInputs(EngineCase):
    def enter(self):
        self.feed(report(buttons=("ps",)), report())
        self.clock.advance(0.15)
        self.feed(report(buttons=("ps",)), report())
        self.batches.clear()

    def test_the_game_sees_a_neutral_pad(self):
        self.make()
        self.enter()
        out = self.feed(report(buttons=("cross", "dpad_up"), lx=0x00, r2=255,
                               touches=((500, 500),)))
        st = P.decode_input(out[1:], usb=True)
        self.assertEqual(buttons_of(out), set())
        self.assertEqual((st.lx, st.r2), (0x80, 0))
        self.assertFalse(st.touch[0].active)

    def test_battery_bytes_survive_neutralisation(self):
        self.make()
        self.enter()
        out = self.feed(report(battery=3))
        st = P.decode_input(out[1:], usb=True)
        self.assertEqual(st.battery_level, 3)

    def test_cross_is_the_left_mouse_button_with_drag(self):
        self.make()
        self.enter()
        self.feed(report(buttons=("cross",)))
        self.assertEqual(self.events[-1], ("button", "left", True))
        self.feed(report())
        self.assertEqual(self.events[-1], ("button", "left", False))

    def test_circle_is_esc_and_options_is_enter(self):
        self.make()
        self.enter()
        self.feed(report(buttons=("circle",)), report(),
                  report(buttons=("options",)), report())
        self.assertIn(("key", A.VK_ESCAPE, True), self.events)
        self.assertIn(("key", A.VK_ESCAPE, False), self.events)
        self.assertIn(("key", A.VK_RETURN, True), self.events)
        self.assertIn(("key", A.VK_RETURN, False), self.events)

    def test_dpad_is_arrow_keys_held_and_released(self):
        self.make()
        self.enter()
        self.feed(report(buttons=("dpad_left",)))
        self.assertEqual(self.events[-1], ("key", A.VK_LEFT, True))
        self.feed(report())
        self.assertEqual(self.events[-1], ("key", A.VK_LEFT, False))

    def test_a_held_arrow_retriggers(self):
        self.make()
        self.enter()
        self.feed(report(buttons=("dpad_right",)))
        for _ in range(4):
            self.clock.advance(0.31)
            self.feed(report(buttons=("dpad_right",)))
        downs = [e for e in self.events if e == ("key", A.VK_RIGHT, True)]
        self.assertGreaterEqual(len(downs), 4)

    def test_one_finger_drag_moves_the_pointer(self):
        self.make()
        self.enter()
        self.feed(report(touches=((500, 500),)),
                  report(touches=((530, 510),)))
        moves = [e for e in self.events if e[0] == "move"]
        self.assertTrue(moves)
        self.assertGreater(sum(m[1] for m in moves), 0)   # net right
        self.assertGreater(sum(m[2] for m in moves), 0)   # net down

    def test_one_finger_tap_is_a_left_click(self):
        self.make()
        self.enter()
        self.feed(report(touches=((500, 500),)))
        self.clock.advance(0.1)
        self.feed(report())
        self.assertIn(("button", "left", True), self.events)
        self.assertIn(("button", "left", False), self.events)

    def test_a_long_press_is_not_a_click(self):
        self.make()
        self.enter()
        self.feed(report(touches=((500, 500),)))
        self.clock.advance(0.6)
        self.feed(report(touches=((500, 500),)), report())
        self.assertNotIn(("button", "left", True), self.events)

    def test_two_finger_tap_is_a_right_click(self):
        self.make()
        self.enter()
        self.feed(report(touches=((500, 500), (600, 500))))
        self.clock.advance(0.1)
        self.feed(report())
        self.assertIn(("button", "right", True), self.events)

    def test_two_finger_drag_scrolls(self):
        self.make()
        self.enter()
        self.feed(report(touches=((500, 300), (600, 300))))
        for y in range(360, 900, 60):
            self.feed(report(touches=((500, y), (600, y))))
        wheels = [e for e in self.events if e[0] == "wheel"]
        self.assertTrue(wheels)
        # fingers moved down -> wheel negative, like every Windows touchpad
        self.assertLess(sum(w[1] for w in wheels), 0)

    def test_triggers_scroll(self):
        self.make()
        self.enter()
        self.feed(report())
        for _ in range(120):
            self.clock.advance(0.004)
            self.feed(report(r2=255))
        wheels = [e for e in self.events if e[0] == "wheel"]
        self.assertTrue(wheels)
        self.assertLess(sum(w[1] for w in wheels), 0)

    def test_left_stick_moves_the_pointer(self):
        self.make()
        self.enter()
        self.feed(report())
        for _ in range(60):
            self.clock.advance(0.004)
            self.feed(report(lx=0xFF))
        moves = [e for e in self.events if e[0] == "move"]
        self.assertGreater(sum(m[1] for m in moves), 0)

    def test_chords_still_fire_in_remote_mode(self):
        self.make()
        self.enter()
        self.feed(report(buttons=("ps",)),
                  report(buttons=("ps", "dpad_up")))
        self.assertIn(("key", A.VK_VOLUME_UP, True), self.events)
        self.assertTrue(self.eng.remote_mode)      # and it did not toggle

    def test_leaving_remote_mode_releases_held_keys(self):
        self.make()
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
        self.make()
        self.feed(report(buttons=("ps",)), report())
        self.clock.advance(0.15)
        self.feed(report(buttons=("ps",)), report())
        out = self.eng.rewrite_setstate(self.game_body(1, 2, 3))
        self.assertEqual((out[P.LED_R], out[P.LED_G], out[P.LED_B]),
                         (255, 120, 0))

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
                  report(buttons=("ps",), touches=((450, 500), (550, 500))),
                  report(buttons=("ps",), touches=((650, 500), (750, 500))))
        self.assertTrue(self.acts.alt_tab_open)
        self.eng.close()
        self.assertFalse(self.acts.alt_tab_open)


if __name__ == "__main__":
    unittest.main()
