"""The v1.0 input-engine additions, fed synthetic 0x01 reports -- no pad, no
desktop (CONTRACT sections 1-3):

  * the 2-finger tracker: a click is never a tap, a slide never a tap, a
    clicked-then-slid contact fires only its `_pressed` gesture; pressed vs
    unpressed row families; pinch direction (touch injection and Ctrl+wheel);
    scroll proportional to travel and `remote.scroll_speed`;
  * the continuous actions end on every exit path (lift, chord release,
    disable, close) -- two synthetic touch contacts must never be left down;
  * macro `repeat`: hold cadence and max_runs, toggle on the engine's timer
    and what stops it;
  * `show_battery` and the remote-mode / keyboard toasts, the child's
    toast line through the manager, and the tray's category gating.
"""

from __future__ import annotations

import io
import unittest

try:
    from ds5app import actions as A
    from ds5app import config as K
    from ds5app import intercept as I
    from ds5app import manager as M
    from ds5app import service as SVC
except ImportError:  # pragma: no cover
    A = None

from tests.test_intercept import EngineCase, report

PSC = ("ps", "touchpad_click")
TC = ("touchpad_click",)


def two(cx, cy=500, spread=100):
    """Two fingers `spread` apart around centroid (cx, cy)."""
    return ((cx - spread // 2, cy), (cx + spread // 2, cy))


@unittest.skipIf(A is None, "the app package is not importable here")
class ClickVersusTap(EngineCase):
    """`touch_click_2f` and `touch_tap_2f` are mutually exclusive, and a
    contact that moves fires neither."""

    def rights(self):
        return [e for e in self.events if e == ("button", "right", True)]

    def test_a_two_finger_click_under_the_chord_is_one_right_click(self):
        self.make()
        self.feed(report(buttons=("ps",)),
                  report(buttons=("ps",), touches=two(900)),
                  report(buttons=PSC, touches=two(900)),
                  report(buttons=PSC, touches=two(905)),
                  report(buttons=("ps",), touches=two(905)),    # click up
                  report(buttons=("ps",)))                       # lift
        self.assertEqual(len(self.rights()), 1)
        self.assertEqual(self.eng.stats["gestures_fired"], 1)

    def test_a_two_finger_tap_under_the_chord_is_unbound_by_default(self):
        self.make()
        self.feed(report(buttons=("ps",)),
                  report(buttons=("ps",), touches=two(900)))
        self.clock.advance(0.1)
        self.feed(report(buttons=("ps",)))
        self.assertEqual(self.rights(), [])
        self.assertEqual(self.eng.stats["gestures_fired"], 0)

    def test_a_two_finger_tap_fires_when_bound(self):
        self.make(chords={"touch_tap_2f": "middle_click"})
        self.feed(report(buttons=("ps",)),
                  report(buttons=("ps",), touches=two(900)))
        self.clock.advance(0.1)
        self.feed(report(buttons=("ps",)))
        self.assertIn(("button", "middle", True), self.events)

    def test_a_click_is_never_also_a_tap(self):
        # Both rows bound to different things: a short 2-finger click fires
        # only the click row.
        self.make(chords={"touch_tap_2f": "middle_click"})
        self.feed(report(buttons=("ps",)),
                  report(buttons=("ps",), touches=two(900)),
                  report(buttons=PSC, touches=two(900)),
                  report(buttons=("ps",), touches=two(900)))
        self.clock.advance(0.05)
        self.feed(report(buttons=("ps",)))
        self.assertEqual(len(self.rights()), 1)
        self.assertNotIn(("button", "middle", True), self.events)

    def test_lifting_with_the_click_still_down_is_still_one_click(self):
        self.make()
        self.feed(report(buttons=("ps",)),
                  report(buttons=("ps",), touches=two(900)),
                  report(buttons=PSC, touches=two(900)),
                  report(buttons=PSC),                         # fingers off
                  report(buttons=("ps",)))                     # click up
        self.assertEqual(len(self.rights()), 1)

    def test_a_clicked_then_slid_contact_fires_only_the_pressed_gesture(self):
        self.make()
        self.feed(report(buttons=("ps",)),
                  report(buttons=("ps",), touches=two(500)),
                  report(buttons=PSC, touches=two(500)),
                  report(buttons=PSC, touches=two(700)),       # slide: alt-tab
                  report(buttons=("ps",), touches=two(700)),   # click up
                  report(buttons=("ps",)))
        self.assertEqual(self.rights(), [])
        self.assertIn(("key", A.VK_MENU, True), self.events)

    def test_a_slow_touch_is_not_a_tap(self):
        self.make(chords={"touch_tap_2f": "middle_click"})
        self.feed(report(buttons=("ps",)),
                  report(buttons=("ps",), touches=two(900)))
        self.clock.advance(0.6)
        self.feed(report(buttons=("ps",), touches=two(900)),
                  report(buttons=("ps",)))
        self.assertNotIn(("button", "middle", True), self.events)

    def test_a_tap_that_moved_is_not_a_tap(self):
        self.make(chords={"touch_tap_2f": "middle_click",
                          "touch_slide_horizontal": "none"})
        self.feed(report(buttons=("ps",)),
                  report(buttons=("ps",), touches=two(900)),
                  report(buttons=("ps",), touches=two(960)))
        self.clock.advance(0.05)
        self.feed(report(buttons=("ps",)))
        self.assertNotIn(("button", "middle", True), self.events)

    def test_remote_mode_classic_two_finger_tap_and_click_both_right_click(self):
        self.make_remote()
        self.feed(report(buttons=("ps",)), report())
        self.clock.advance(0.15)
        self.feed(report(buttons=("ps",)), report())
        self.batches.clear()
        self.feed(report(touches=two(900)))
        self.clock.advance(0.1)
        self.feed(report())
        self.assertEqual(len(self.rights()), 1)
        self.feed(report(touches=two(900)), report(buttons=TC, touches=two(900)),
                  report(touches=two(900)), report())
        self.assertEqual(len(self.rights()), 2)

    def test_remote_mode_one_finger_click_is_the_left_button_held(self):
        self.make_remote()
        self.feed(report(buttons=("ps",)), report())
        self.clock.advance(0.15)
        self.feed(report(buttons=("ps",)), report())
        self.batches.clear()
        self.feed(report(touches=((500, 500),)),
                  report(buttons=TC, touches=((500, 500),)))
        self.assertEqual(self.events[-1], ("button", "left", True))
        self.feed(report(buttons=TC, touches=((540, 520),)))   # drag
        self.assertTrue([e for e in self.events if e[0] == "move"])
        self.feed(report(touches=((540, 520),)))
        self.assertEqual(self.events[-1], ("button", "left", False))
        self.feed(report())                                    # lift: no tap
        self.assertEqual(
            len([e for e in self.events if e == ("button", "left", True)]), 1)
        self.assertFalse(self.eng.keyboard_open)              # not the chord


@unittest.skipIf(A is None, "the app package is not importable here")
class ChordGesturesInsideRemoteMode(EngineCase):
    """Chords stay active in remote mode, and so do the chord-held gestures:
    remote mode's per-frame release of what it holds must not end a contact
    the chord context began (with `same_gestures` both use the same table
    object, so the tracker tags the contact with its context)."""

    def enter(self):
        self.feed(report(buttons=("ps",)), report())
        self.clock.advance(0.15)
        self.feed(report(buttons=("ps",)), report())
        self.batches.clear()

    def test_a_pressed_slide_under_the_chord_in_remote_mode_holds_alt_tab(self):
        self.make(remote={"enabled": True})
        self.enter()
        self.assertTrue(self.eng.remote_mode)
        self.clock.advance(1.0)
        self.feed(report(buttons=("ps",)),
                  report(buttons=PSC, touches=two(500)),
                  report(buttons=PSC, touches=two(660)),
                  report(buttons=PSC, touches=two(820)))
        self.assertTrue(self.acts.alt_tab_open)
        self.feed(report(buttons=("ps",)))                    # fingers off
        self.assertFalse(self.acts.alt_tab_open)

    def test_an_unpressed_scroll_under_the_chord_in_remote_mode(self):
        self.make(remote={"enabled": True})
        self.enter()
        self.clock.advance(1.0)
        self.feed(report(buttons=("ps",)),
                  report(buttons=("ps",), touches=two(900, 300)),
                  report(buttons=("ps",), touches=two(900, 400)),
                  report(buttons=("ps",), touches=two(900, 500)))
        self.assertTrue([e for e in self.events if e[0] == "wheel"])
        self.assertTrue(self.eng._tf_active)
        self.assertEqual(self.eng._tf_context, "chord")
        self.feed(report(buttons=("ps",)))
        self.assertFalse(self.eng._tf_active)

    def test_a_chord_press_still_ends_a_remote_contact(self):
        self.make_remote()
        self.enter()
        self.feed(report(touches=two(900, 300)),
                  report(touches=two(900, 400)))
        self.assertEqual(self.eng._tf_context, "remote")
        self.feed(report(buttons=("ps",), touches=two(900, 400)))
        self.assertFalse(self.eng._tf_active)


@unittest.skipIf(A is None, "the app package is not importable here")
class PressedVersusUnpressed(EngineCase):
    def test_the_same_vertical_travel_scrolls_unpressed_and_swipes_pressed(self):
        self.make()
        self.feed(report(buttons=("ps",)),
                  report(buttons=("ps",), touches=two(900, 300)),
                  report(buttons=("ps",), touches=two(900, 550)),
                  report(buttons=("ps",)))
        self.assertTrue([e for e in self.events if e[0] == "wheel"])
        self.assertNotIn(("key", A.VK_M, True), self.events)
        self.batches.clear()
        self.feed(report(buttons=PSC, touches=two(900, 300)),
                  report(buttons=PSC, touches=two(900, 550)),
                  report(buttons=("ps",)))
        self.assertIn(("key", A.VK_M, True), self.events)
        self.assertFalse([e for e in self.events if e[0] == "wheel"])

    def test_a_pressed_slide_needs_the_alt_tab_threshold_not_the_slide_one(self):
        self.make()
        self.feed(report(buttons=("ps",)),
                  report(buttons=PSC, touches=two(500)),
                  report(buttons=PSC, touches=two(580)))      # 80 px: not yet
        self.assertFalse(self.acts.alt_tab_open)
        self.feed(report(buttons=PSC, touches=two(660)))      # 160 px
        self.assertTrue(self.acts.alt_tab_open)

    def test_a_contact_that_starts_pressed_is_a_pressed_gesture(self):
        # Fingers and click land in the same frame: the click is not the
        # keyboard chord (two fingers are down) and the contact is pressed.
        self.make()
        self.feed(report(buttons=("ps",)),
                  report(buttons=PSC, touches=two(900, 700)),
                  report(buttons=PSC, touches=two(900, 450)))
        self.assertIn(("key", A.VK_LWIN, True), self.events)   # task_view
        self.assertFalse(self.eng.keyboard_open)

    def test_thresholds_are_tunable(self):
        self.make(gestures={"swipe_px": 60})
        self.feed(report(buttons=("ps",)),
                  report(buttons=PSC, touches=two(900, 700)),
                  report(buttons=PSC, touches=two(900, 630)))
        self.assertIn(("key", A.VK_LWIN, True), self.events)

    def test_the_compat_swipes_fire_only_while_the_vertical_slide_is_none(self):
        self.make(chords={"touch_swipe_up": "task_view"})
        self.feed(report(buttons=("ps",)),
                  report(buttons=("ps",), touches=two(900, 700)),
                  report(buttons=("ps",), touches=two(900, 450)),
                  report(buttons=("ps",)))
        self.assertNotIn(("key", A.VK_LWIN, True), self.events)   # scrolled
        self.batches.clear()
        self.eng.update_config(K.InputConfig.from_dict(
            {"chords": {"touch_swipe_up": "task_view",
                        "touch_slide_vertical": "none"}}))
        self.feed(report(buttons=("ps",), touches=two(900, 700)),
                  report(buttons=("ps",), touches=two(900, 450)),
                  report(buttons=("ps",)))
        self.assertIn(("key", A.VK_LWIN, True), self.events)
        self.assertFalse([e for e in self.events if e[0] == "wheel"])


@unittest.skipIf(A is None, "the app package is not importable here")
class Scrolling(EngineCase):
    def scroll(self, ys, speed=None, **over):
        if speed is not None:
            over.setdefault("remote", {})["scroll_speed"] = speed
        self.make(**over)
        self.feed(report(buttons=("ps",)),
                  report(buttons=("ps",), touches=two(900, ys[0])))
        for y in ys[1:]:
            self.feed(report(buttons=("ps",), touches=two(900, y)))
        self.feed(report(buttons=("ps",)))
        return sum(e[1] for e in self.events if e[0] == "wheel")

    def test_wheel_units_are_proportional_to_travel(self):
        # 100 px per notch: ~300 px of downward travel = ~ -360 units, in
        # third-notch (40) steps. The first `slide_px` are the dead zone.
        total = self.scroll([300, 400, 500, 600])
        self.assertTrue(-360 <= total <= -240, total)
        self.assertTrue(all(e[1] % 40 == 0 for e in self.events
                            if e[0] == "wheel"))

    def test_scroll_speed_scales_it(self):
        slow = self.scroll([300, 400, 500, 600])
        fast = self.scroll([300, 400, 500, 600], speed=2.0)
        self.assertAlmostEqual(fast / slow, 2.0, delta=0.4)

    def test_px_per_notch_is_tunable(self):
        coarse = self.scroll([300, 400, 500, 600])
        fine = self.scroll([300, 400, 500, 600],
                           gestures={"scroll_px_per_notch": 50})
        self.assertGreater(abs(fine), abs(coarse))

    def test_up_is_positive(self):
        self.assertGreater(self.scroll([600, 500, 400, 300]), 0)

    def test_a_button_bound_to_scroll_does_one_notch(self):
        self.make(chords={"cross": "scroll", "circle": "scroll_horizontal"})
        self.feed(report(buttons=("ps",)), report(buttons=("ps", "cross")),
                  report(buttons=("ps",)), report(buttons=("ps", "circle")))
        self.assertIn(("wheel", -120), self.events)
        self.assertIn(("hwheel", 120), self.events)


@unittest.skipIf(A is None, "the app package is not importable here")
class Pinch(EngineCase):
    def pinch(self, spreads, pressed=False, **over):
        self.make(**over)
        btn = PSC if pressed else ("ps",)
        self.feed(report(buttons=("ps",)),
                  report(buttons=btn, touches=two(900, 500, spreads[0])))
        for s in spreads[1:]:
            self.feed(report(buttons=btn, touches=two(900, 500, s)))
        self.feed(report(buttons=("ps",)))

    def spread_of(self, frame):
        pts = frame[1]
        return pts[1][0] - pts[0][0]

    def test_spreading_unpressed_is_a_touch_pinch_out_around_the_cursor(self):
        self.pinch([100, 150, 200, 260, 320])
        kinds = [f[0] for f in self.touch.frames]
        self.assertEqual(kinds[0], "down")
        self.assertIn("move", kinds)
        self.assertEqual(kinds[-1], "up")
        down = self.touch.frames[0]
        self.assertEqual(sum(p[0] for p in down[1]) / 2, 1000)  # cursor x
        self.assertTrue(all(p[1] == 600 for p in down[1]))      # cursor y
        moves = [f for f in self.touch.frames if f[0] == "move"]
        self.assertGreater(self.spread_of(moves[-1]), self.spread_of(down))
        self.assertFalse(self.acts.pinch_active)
        self.assertFalse([e for e in self.events if e[0] == "wheel"])

    def test_closing_unpressed_pinches_in(self):
        self.pinch([320, 260, 200, 150, 100])
        down = self.touch.frames[0]
        moves = [f for f in self.touch.frames if f[0] == "move"]
        self.assertLess(self.spread_of(moves[-1]), self.spread_of(down))

    def test_a_small_spread_change_is_not_a_pinch(self):
        self.pinch([100, 120, 140])
        self.assertEqual(self.touch.frames, [])

    def test_pinch_gain_is_tunable(self):
        self.pinch([100, 200, 300])
        base = self.spread_of(self.touch.frames[-2])
        self.pinch([100, 200, 300], gestures={"pinch_gain": 2.0})
        self.assertGreater(self.spread_of(self.touch.frames[-2]), base)

    def test_spreading_pressed_is_ctrl_wheel_in_whole_notches(self):
        self.pinch([100, 180, 260, 340], pressed=True)
        self.assertEqual(self.touch.frames, [])
        zooms = [b for b in self.batches if b and b[0] == ("key", A.VK_CONTROL, True)]
        self.assertTrue(zooms)
        for b in zooms:
            self.assertEqual(b[-1], ("key", A.VK_CONTROL, False))
            self.assertEqual(b[1][0], "wheel")
            self.assertGreater(b[1][1], 0)
            self.assertEqual(b[1][1] % 120, 0)

    def test_closing_pressed_zooms_out(self):
        self.pinch([340, 260, 180, 100], pressed=True)
        wheels = [b[1][1] for b in self.batches
                  if b and b[0] == ("key", A.VK_CONTROL, True)]
        self.assertTrue(wheels and all(w < 0 for w in wheels))

    def test_a_pinch_ends_when_the_chord_is_released(self):
        self.make()
        self.feed(report(buttons=("ps",)),
                  report(buttons=("ps",), touches=two(900, 500, 100)),
                  report(buttons=("ps",), touches=two(900, 500, 300)))
        self.assertTrue(self.acts.pinch_active)
        self.feed(report(touches=two(900, 500, 300)))
        self.assertFalse(self.acts.pinch_active)
        self.assertEqual(self.touch.frames[-1], ("up",))

    def test_disable_and_close_lift_a_live_pinch(self):
        for how in ("disable", "close"):
            self.make()
            self.feed(report(buttons=("ps",)),
                      report(buttons=("ps",), touches=two(900, 500, 100)),
                      report(buttons=("ps",), touches=two(900, 500, 300)))
            self.assertTrue(self.acts.pinch_active)
            if how == "disable":
                self.eng.update_config(K.InputConfig.from_dict({"enabled": False}))
            else:
                self.eng.close()
            self.assertFalse(self.acts.pinch_active, how)

    def test_a_button_bound_to_pinch_zoom_does_one_spread(self):
        self.make(chords={"cross": "pinch_zoom"})
        self.feed(report(buttons=("ps",)), report(buttons=("ps", "cross")))
        self.assertEqual([f[0] for f in self.touch.frames], ["down", "move", "up"])
        self.assertFalse(self.acts.pinch_active)


@unittest.skipIf(A is None, "the app package is not importable here")
class MacroRepeat(EngineCase):
    SPAM = {"spam": {"keys": ["a"], "repeat": {"mode": "hold",
                                              "interval_ms": 50}}}
    TOGGLE = {"auto": {"keys": ["b"], "repeat": {"mode": "toggle",
                                                "interval_ms": 100}}}

    def presses(self, vk):
        return len([e for e in self.events if e == ("key", vk, True)])

    def test_hold_repeats_at_its_own_cadence_while_the_chord_is_held(self):
        self.make(macros=self.SPAM, chords={"cross": "spam"})
        self.feed(report(buttons=("ps",)), report(buttons=("ps", "cross")))
        self.assertEqual(self.presses(ord("A")), 1)
        for _ in range(10):
            self.clock.advance(0.05)
            self.feed(report(buttons=("ps", "cross")))
        self.assertEqual(self.presses(ord("A")), 11)
        self.feed(report(buttons=("ps",)))
        self.clock.advance(1.0)
        self.feed(report(buttons=("ps",)))
        self.assertEqual(self.presses(ord("A")), 11)          # released: stops

    def test_hold_honours_max_runs(self):
        macros = {"spam": {"keys": ["a"], "repeat": {"mode": "hold",
                                                    "interval_ms": 50,
                                                    "max_runs": 3}}}
        self.make(macros=macros, chords={"cross": "spam"})
        self.feed(report(buttons=("ps",)), report(buttons=("ps", "cross")))
        for _ in range(10):
            self.clock.advance(0.05)
            self.feed(report(buttons=("ps", "cross")))
        self.assertEqual(self.presses(ord("A")), 3)

    def test_the_old_boolean_repeat_still_means_hold_at_repeat_ms(self):
        self.make(macros={"louder": {"keys": ["volume_up"], "repeat": True}},
                  chords={"cross": "louder"}, repeat_ms=100)
        self.feed(report(buttons=("ps",)), report(buttons=("ps", "cross")))
        self.clock.advance(0.3)                                # after the 250 ms floor
        self.feed(report(buttons=("ps", "cross")))
        self.assertEqual(self.presses(A.VK_VOLUME_UP), 2)

    def test_toggle_runs_on_the_timer_until_pressed_again(self):
        self.make(macros=self.TOGGLE, chords={"cross": "auto"})
        self.feed(report(buttons=("ps",)), report(buttons=("ps", "cross")),
                  report(buttons=("ps",)), report())           # tap and let go
        self.assertEqual(self.presses(ord("B")), 1)
        for _ in range(5):
            self.clock.advance(0.1)
            self.eng.tick()                                    # no reports at all
        self.assertEqual(self.presses(ord("B")), 6)
        self.assertEqual(self.eng.stats["macro_repeats"], 5)
        # second press: stop (and it does not fire once more)
        self.clock.advance(0.5)
        self.feed(report(buttons=("ps",)), report(buttons=("ps", "cross")),
                  report(buttons=("ps",)), report())
        n = self.presses(ord("B"))
        for _ in range(5):
            self.clock.advance(0.1)
            self.eng.tick()
        self.assertEqual(self.presses(ord("B")), n)

    def test_toggle_honours_max_runs(self):
        macros = {"auto": {"keys": ["b"], "repeat": {"mode": "toggle",
                                                    "interval_ms": 100,
                                                    "max_runs": 4}}}
        self.make(macros=macros, chords={"cross": "auto"})
        self.feed(report(buttons=("ps",)), report(buttons=("ps", "cross")),
                  report(buttons=("ps",)), report())
        for _ in range(10):
            self.clock.advance(0.1)
            self.eng.tick()
        self.assertEqual(self.presses(ord("B")), 4)

    def test_toggle_stops_on_disable_remote_exit_and_close(self):
        for how in ("disable", "remote", "close"):
            self.make(macros=self.TOGGLE, chords={"cross": "auto"},
                      remote={"enabled": True})
            self.feed(report(buttons=("ps",)), report(buttons=("ps", "cross")),
                      report(buttons=("ps",)), report())
            self.clock.advance(0.5)
            if how == "disable":
                self.eng.update_config(K.InputConfig.from_dict({"enabled": False}))
            elif how == "remote":
                self.feed(report(buttons=("ps",)), report())    # into remote
                self.clock.advance(0.15)
                self.feed(report(buttons=("ps",)), report())
                self.assertTrue(self.eng.remote_mode)
                self.clock.advance(0.5)
                self.feed(report(buttons=("ps",)), report())    # and out
                self.clock.advance(0.15)
                self.feed(report(buttons=("ps",)), report())
                self.assertFalse(self.eng.remote_mode)
            else:
                self.eng.close()
            n = self.presses(ord("B"))
            for _ in range(5):
                self.clock.advance(0.1)
                self.eng.tick()
            self.assertEqual(self.presses(ord("B")), n, how)

    def test_toggle_from_a_remote_button_and_a_gesture(self):
        self.make_remote(macros=self.TOGGLE,
                         remote={"chords": {"square": "auto",
                                            "touch_swipe_up_pressed": "auto"}})
        self.feed(report(buttons=("ps",)), report())
        self.clock.advance(0.15)
        self.feed(report(buttons=("ps",)), report())
        self.batches.clear()
        self.feed(report(buttons=("square",)), report())
        self.assertEqual(self.presses(ord("B")), 1)
        self.clock.advance(0.1)
        self.eng.tick()
        self.assertEqual(self.presses(ord("B")), 2)
        self.feed(report(buttons=TC, touches=two(900, 700)),
                  report(buttons=TC, touches=two(900, 450)), report())
        self.clock.advance(0.1)
        self.eng.tick()
        self.assertEqual(self.presses(ord("B")), 2)             # gesture stopped it

    def test_hold_from_a_remote_button_uses_its_own_interval(self):
        self.make_remote(macros=self.SPAM, remote={"chords": {"square": "spam"}})
        self.feed(report(buttons=("ps",)), report())
        self.clock.advance(0.15)
        self.feed(report(buttons=("ps",)), report())
        self.batches.clear()
        self.feed(report(buttons=("square",)))
        for _ in range(4):
            self.clock.advance(0.05)
            self.feed(report(buttons=("square",)))
        self.assertEqual(self.presses(ord("A")), 5)

    def test_the_spec_carries_the_repeat_object(self):
        self.make()
        spec = self.acts.macro_spec("x", {"keys": ["a"], "repeat": {
            "mode": "toggle", "interval_ms": 20, "max_runs": 7}})
        self.assertEqual((spec.repeat_mode, spec.repeat_interval_s, spec.max_runs),
                         ("toggle", 0.02, 7))
        self.assertFalse(spec.repeatable)
        spec = self.acts.macro_spec("y", {"keys": ["a"], "repeat": {"mode": "hold"}})
        self.assertTrue(spec.repeatable)
        self.assertEqual(spec.repeat_interval_s, 0.05)          # the default
        spec = self.acts.macro_spec("z", {"keys": ["a"], "repeat": {"mode": "bogus",
                                                                    "interval_ms": "x"}})
        self.assertEqual(spec.repeat_mode, "once")


@unittest.skipIf(A is None, "the app package is not importable here")
class Toasts(EngineCase):
    def test_show_battery_is_the_default_r3_chord_and_says_the_level(self):
        self.make()
        self.assertEqual(K.DEFAULT_CHORDS["r3"], "show_battery")
        self.feed(report(battery=8), report(buttons=("ps",), battery=8),
                  report(buttons=("ps", "r3"), battery=8))
        self.assertEqual(self.toasts, [("battery", "Battery",
                                        "{pad}: 80 % (discharging)")])
        self.assertEqual(self.eng.stats["toasts"], 1)

    def test_charging_and_full(self):
        self.make()
        self.feed(report(buttons=("ps",), battery=5, charging=True),
                  report(buttons=("ps", "r3"), battery=5, charging=True))
        self.assertEqual(self.toasts[-1][2], "{pad}: 50 % (charging)")
        full = bytearray(report(buttons=("ps",), battery=5))
        full[1 + 52] = 0x20 | 0x0A                              # charging_complete
        self.feed(bytes(full))
        full = bytearray(report(buttons=("ps", "r3"), battery=5))
        full[1 + 52] = 0x20 | 0x0A
        self.feed(bytes(full))
        self.assertEqual(self.toasts[-1][2], "{pad}: 100 % (full)")

    def test_show_battery_from_remote_mode_and_a_gesture(self):
        self.make_remote(remote={"chords": {"square": "show_battery",
                                            "touch_tap_2f": "show_battery"}})
        self.feed(report(buttons=("ps",)), report())
        self.clock.advance(0.15)
        self.feed(report(buttons=("ps",)), report())
        self.toasts.clear()
        self.feed(report(buttons=("square",)), report())
        self.assertEqual(len(self.toasts), 1)
        self.feed(report(touches=two(900)))
        self.clock.advance(0.1)
        self.feed(report())
        self.assertEqual(len(self.toasts), 2)
        self.assertEqual(self.toasts[-1][0], "battery")

    def test_remote_mode_toasts_on_and_off(self):
        self.make(remote={"enabled": True})
        self.feed(report(buttons=("ps",)), report())
        self.clock.advance(0.15)
        self.feed(report(buttons=("ps",)), report())
        self.assertEqual(self.toasts[-1],
                         ("remote_mode", "Remote mode", "Remote mode on ({pad})"))
        self.clock.advance(1.0)
        self.feed(report(buttons=("ps",)), report())
        self.clock.advance(0.15)
        self.feed(report(buttons=("ps",)), report())
        self.assertEqual(self.toasts[-1][2], "Remote mode off ({pad})")

    def test_a_config_change_that_leaves_remote_mode_toasts_too(self):
        self.make(remote={"enabled": True})
        self.feed(report(buttons=("ps",)), report())
        self.clock.advance(0.15)
        self.feed(report(buttons=("ps",)), report())
        self.eng.update_config(K.InputConfig.from_dict({"remote": {"enabled": False}}))
        self.assertEqual(self.toasts[-1][2], "Remote mode off ({pad})")

    def test_keyboard_toasts_opened_and_closed(self):
        self.make()
        self.feed(report(buttons=("ps",)), report(buttons=PSC))
        self.assertEqual(self.toasts[-1], ("keyboard", "On-screen keyboard",
                                           "On-screen keyboard opened"))
        self.feed(report(buttons=("ps",)), report(buttons=PSC))
        self.assertEqual(self.toasts[-1][2], "On-screen keyboard closed")

    def test_no_callback_no_toast_no_error(self):
        self.make()
        self.eng.on_toast = None
        self.feed(report(buttons=("ps",)), report(buttons=("ps", "r3")))
        self.assertEqual(self.toasts, [])

    def test_the_toast_line_round_trips_through_the_manager(self):
        line = SVC.toast_line("battery", "Battery", "{pad}: 80 % (discharging)")
        self.assertEqual(M.parse_toast_event(line),
                         ("battery", "Battery", "{pad}: 80 % (discharging)"))
        # pipes and newlines in the parts cannot break the framing
        line = SVC.toast_line("x", "a|b", "c\nd")
        self.assertEqual(M.parse_toast_event(line), ("x", "a/b", "c d"))
        self.assertIsNone(M.parse_toast_event("battery 80%"))
        self.assertIsNone(M.parse_toast_event("|x|y"))
        self.assertEqual(M._EVENT_PREFIX["  >  "], "toast")

    def test_the_childs_toast_line_reaches_the_parent_as_a_toast_event(self):
        class Proc:
            def __init__(self, lines):
                self.stdout = io.StringIO("".join(l + "\n" for l in lines))

            def poll(self):
                return 0

        events = []
        b = M.ChildBridge("d42f4ba1485d", 3241, ["x"],
                          on_event=lambda s, k, t: events.append((k, t)),
                          cleanup_fn=lambda *a, **k: None)
        b._snap["state"] = M.RUNNING
        b._read_stdout(Proc(["  >  remote_mode|Remote mode|Remote mode on ({pad})",
                             "  -  something else"]))
        self.assertIn(("toast", "remote_mode|Remote mode|Remote mode on ({pad})"),
                      events)


@unittest.skipIf(A is None, "the app package is not importable here")
class NotificationsConfig(unittest.TestCase):
    def test_defaults_and_round_trip(self):
        n = K.Notifications()
        self.assertEqual(n.to_dict(), {"enabled": True, "battery_low": True,
                                       "remote_mode": True, "keyboard": False,
                                       "connection": True, "hide": True,
                                       "update": True})
        c = K.Config.from_dict({"notifications": {"keyboard": True, "hide": "no",
                                                  "future": 1}})
        self.assertTrue(c.notifications.keyboard)
        self.assertFalse(c.notifications.hide)
        self.assertEqual(c.notifications.extra, {"future": 1})
        back = K.Config.from_dict(c.to_dict())
        self.assertEqual(back.notifications.to_dict(), c.notifications.to_dict())
        self.assertIn("notifications", c.to_dict())

    def test_allows(self):
        n = K.Notifications(keyboard=False, hide=False)
        self.assertTrue(n.allows("battery_low"))
        self.assertFalse(n.allows("keyboard"))
        self.assertFalse(n.allows("hide"))
        self.assertTrue(n.allows("battery"))          # show_battery: master only
        self.assertTrue(n.allows("something_new"))
        n.enabled = False
        self.assertFalse(n.allows("battery_low"))
        self.assertFalse(n.allows("battery"))

    def test_garbage_is_the_defaults(self):
        self.assertTrue(K.Config.from_dict({"notifications": 7}).notifications.enabled)
        self.assertTrue(K.Config.from_dict({"notifications": []}).notifications.hide)


@unittest.skipIf(A is None, "the app package is not importable here")
class TrayGating(unittest.TestCase):
    def app(self, **notes):
        from tests.test_tray import app_with, controller
        app = app_with([controller(label="blue")])
        app.icon = object()
        app.cfg.notifications = K.Notifications(**notes)
        app.cfg.controllers = {}
        return app

    def test_a_toast_event_is_gated_by_its_category_and_the_pad_is_named(self):
        app = self.app(remote_mode=True)
        app._on_event("a0fa9c0dd8bb", "toast",
                      "remote_mode|Remote mode|Remote mode on ({pad})")
        self.assertEqual(app.notes, [("Remote mode", "Remote mode on (a0fa..d8bb)")])
        app = self.app(remote_mode=False)
        app._on_event("a0fa9c0dd8bb", "toast",
                      "remote_mode|Remote mode|Remote mode on ({pad})")
        self.assertEqual(app.notes, [])

    def test_the_label_replaces_the_placeholder(self):
        app = self.app()
        app.cfg.controllers = {"a0fa9c0dd8bb": K.ControllerConfig(label="blue")}
        app._on_event("a0fa9c0dd8bb", "toast", "battery|Battery|{pad}: 80 % (full)")
        self.assertEqual(app.notes, [("Battery", "blue: 80 % (full)")])

    def test_show_battery_is_gated_by_the_master_switch_only(self):
        app = self.app(battery_low=False, remote_mode=False, keyboard=False)
        app._on_event("a0fa9c0dd8bb", "toast", "battery|Battery|{pad}: 80 %")
        self.assertEqual(len(app.notes), 1)
        app = self.app(enabled=False)
        app._on_event("a0fa9c0dd8bb", "toast", "battery|Battery|{pad}: 80 %")
        self.assertEqual(app.notes, [])

    def test_existing_balloons_follow_their_categories(self):
        app = self.app(battery_low=False, connection=False)
        app._on_event("a0fa9c0dd8bb", "battery_low", "battery 20% -- charge soon")
        app._on_event("a0fa9c0dd8bb", "warn", "controller gone for 15s")
        self.assertEqual(app.notes, [])
        app._on_event("a0fa9c0dd8bb", "error", "the bridge process exited")
        self.assertEqual(len(app.notes), 1)                  # errors: master only
        app = self.app(enabled=False)
        app._on_event("a0fa9c0dd8bb", "error", "the bridge process exited")
        self.assertEqual(app.notes, [])

    def test_hide_balloons_are_gated(self):
        app = self.app(hide=False)
        app._notify_hide_applied(["a0fa9c0dd8bb"], [])
        self.assertEqual(app.notes, [])
        app = self.app(hide=True)
        app._notify_hide_applied(["a0fa9c0dd8bb"], [])
        self.assertEqual(len(app.notes), 1)

    def test_a_malformed_toast_line_is_ignored(self):
        app = self.app()
        app._on_event("a0fa9c0dd8bb", "toast", "no separators here")
        self.assertEqual(app.notes, [])

    def test_a_config_without_notifications_allows_everything(self):
        from tests.test_tray import app_with, controller
        app = app_with([controller()])
        app.icon = object()
        app._on_event("a0fa9c0dd8bb", "battery_low", "battery 20% -- charge soon")
        self.assertEqual(len(app.notes), 1)


@unittest.skipIf(A is None, "the app package is not importable here")
class LiveApply(EngineCase):
    """The config watcher carries the new keys: a saved change to
    `same_gestures`, the thresholds or a macro's repeat lands live."""

    def test_the_watcher_sees_the_new_keys_as_changes(self):
        import json
        import os
        import tempfile

        self.make(remote={"enabled": True})
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "config.json")
            applied = []
            w = SVC.InputConfigWatcher(apply_fn=lambda c: (applied.append(c),
                                                           self.eng.update_config(c)),
                                       current_fn=lambda: self.eng.cfg,
                                       path=path)
            with open(path, "w") as f:
                json.dump({"input": {"remote": {"enabled": True,
                                                "same_gestures": False},
                                     "gestures": {"swipe_px": 60}}}, f)
            self.assertTrue(w.poll_once())
            self.assertIs(self.eng._remote_gestures, self.eng.cfg.remote.chords)
            self.assertEqual(self.eng._g.swipe_px, 60)
            with open(path, "w") as f:
                json.dump({"input": {"remote": {"enabled": True,
                                                "same_gestures": False},
                                     "gestures": {"swipe_px": 60},
                                     "macros": {"m": {"keys": ["a"],
                                                      "repeat": {"mode": "toggle"}}}}},
                          f)
            os.utime(path, (1, 1))
            self.assertTrue(w.poll_once())
            self.assertEqual(self.eng.registry["m"].repeat_mode, "toggle")


if __name__ == "__main__":
    unittest.main()
