"""The OS action layer, with the `SendInput` seam replaced by a recorder.

Nothing here touches the desktop: `inject` collects event lists and `run_ps`
collects scripts. What is under test is the exact key sequences -- ordering
matters for real (`Win` must be down before `D`, Alt must OUTLIVE every Tab of
a switcher session) -- and the atomicity rule that one gesture is one
`SendInput` call.
"""

from __future__ import annotations

import unittest

try:
    from ds5app import actions as A
except ImportError as exc:  # pragma: no cover
    A = None
    _WHY = str(exc)


@unittest.skipIf(A is None, "the app package is not importable here")
class RecorderCase(unittest.TestCase):
    def setUp(self):
        self.batches: list[list] = []
        self.scripts: list[str] = []
        self.a = A.OsActions(inject=self.batches.append,
                             run_ps=lambda s, timeout=10.0:
                             (self.scripts.append(s), True)[1])

    @property
    def events(self):
        return [e for batch in self.batches for e in batch]


class TapAndCombos(RecorderCase):
    def test_tap_presses_in_order_and_releases_in_reverse(self):
        self.a.tap(A.VK_LWIN, A.VK_D)
        self.assertEqual(self.events, [
            ("key", A.VK_LWIN, True), ("key", A.VK_D, True),
            ("key", A.VK_D, False), ("key", A.VK_LWIN, False)])

    def test_a_combo_is_one_atomic_sendinput_call(self):
        # Two separate calls can interleave with the user's real typing;
        # Win+D split across calls is how you get a stray 'd' in a chat box.
        self.a.tap(A.VK_LWIN, A.VK_D)
        self.assertEqual(len(self.batches), 1)

    def test_the_shell_combos(self):
        reg = self.a.registry()
        for name, vks in (("show_desktop", (A.VK_LWIN, A.VK_D)),
                          ("minimize_all", (A.VK_LWIN, A.VK_M)),
                          ("task_view", (A.VK_LWIN, A.VK_TAB)),
                          ("projection_cycle", (A.VK_LWIN, A.VK_P))):
            self.batches.clear()
            reg[name].run({})
            self.assertEqual(self.events[:2],
                             [("key", vks[0], True), ("key", vks[1], True)],
                             name)


class VolumeAndMedia(RecorderCase):
    def test_volume_up_default_is_one_tap(self):
        self.a.registry()["volume_up"].run({})
        self.assertEqual(self.events, [("key", A.VK_VOLUME_UP, True),
                                       ("key", A.VK_VOLUME_UP, False)])

    def test_volume_step_parameter_multiplies_the_taps(self):
        self.a.registry()["volume_down"].run({"step": 3})
        downs = [e for e in self.events if e == ("key", A.VK_VOLUME_DOWN, True)]
        self.assertEqual(len(downs), 3)

    def test_media_keys(self):
        reg = self.a.registry()
        for name, vk in (("media_play_pause", A.VK_MEDIA_PLAY_PAUSE),
                         ("media_next", A.VK_MEDIA_NEXT_TRACK),
                         ("media_prev", A.VK_MEDIA_PREV_TRACK),
                         ("volume_mute", A.VK_VOLUME_MUTE)):
            self.batches.clear()
            reg[name].run({})
            self.assertEqual(self.events, [("key", vk, True), ("key", vk, False)],
                             name)


class DefaultChordsResolve(RecorderCase):
    """Every action a DEFAULT chord names must exist -- otherwise a factory
    install prints `chord names unknown action ... -- ignored` and that chord
    silently does nothing. This is the guard that keeps `config.DEFAULT_CHORDS`
    and `OsActions.registry()` from drifting apart: a rename on one side that is
    not mirrored on the other fails here rather than on a user's pad."""

    def test_every_default_chord_action_resolves(self):
        from ds5app import config as K

        reg = self.a.registry()
        unresolved = {}
        for table in (K.DEFAULT_CHORDS, K.DEFAULT_REMOTE_CHORDS):
            for key, name in table.items():
                # `K.ENGINE_ACTIONS` (pad power/lightbar, the on-screen
                # keyboard) are the engine's own, deliberately not OsActions
                # (see registry() docstring); everything else is a name in
                # the registry.
                if name in K.ENGINE_ACTIONS:
                    continue
                if name not in reg:
                    unresolved[key] = name
        self.assertEqual(unresolved, {},
                         f"default chord names not in the action registry: "
                         f"{unresolved}")


class Brightness(RecorderCase):
    def test_brightness_goes_through_wmi_with_a_signed_step(self):
        reg = self.a.registry()
        reg["brightness_up"].run({})
        reg["brightness_down"].run({"step": 25})
        self.assertEqual(len(self.scripts), 2)
        self.assertIn("WmiSetBrightness", self.scripts[0])
        self.assertIn("(10)", self.scripts[0])
        self.assertIn("(-25)", self.scripts[1])
        self.assertEqual(self.events, [])     # no synthetic keys for this one


class AltTabHold(RecorderCase):
    """Alt must go down once, stay down across every step, and come up exactly
    once -- a stuck Alt key is the failure mode of this entire feature."""

    def test_start_step_commit_sequence(self):
        self.a.alt_tab_start()
        self.a.alt_tab_step(True)
        self.a.alt_tab_step(False)
        self.a.alt_tab_commit()
        ev = self.events
        self.assertEqual(ev[0], ("key", A.VK_MENU, True))
        self.assertEqual(ev[-1], ("key", A.VK_MENU, False))
        # exactly one Alt down and one Alt up in the whole session
        self.assertEqual([e for e in ev if e[1] == A.VK_MENU],
                         [("key", A.VK_MENU, True), ("key", A.VK_MENU, False)])
        # the backward step is Shift+Tab with Shift released after
        self.assertIn(("key", A.VK_SHIFT, True), ev)
        self.assertIn(("key", A.VK_SHIFT, False), ev)

    def test_start_twice_is_one_session(self):
        self.a.alt_tab_start()
        self.a.alt_tab_start()
        self.assertEqual(
            [e for e in self.events if e == ("key", A.VK_MENU, True)],
            [("key", A.VK_MENU, True)])

    def test_commit_without_start_does_nothing(self):
        self.a.alt_tab_commit()
        self.a.alt_tab_cancel()
        self.assertEqual(self.events, [])

    def test_step_without_start_does_nothing(self):
        self.a.alt_tab_step(True)
        self.assertEqual(self.events, [])

    def test_cancel_escapes_then_releases_alt(self):
        self.a.alt_tab_start()
        self.a.alt_tab_cancel()
        ev = self.events
        esc_at = ev.index(("key", A.VK_ESCAPE, True))
        alt_up_at = ev.index(("key", A.VK_MENU, False))
        self.assertLess(esc_at, alt_up_at)
        self.assertFalse(self.a.alt_tab_open)

    def test_close_releases_a_held_alt(self):
        self.a.alt_tab_start()
        self.a.close()
        self.assertIn(("key", A.VK_MENU, False), self.events)


class MousePrimitives(RecorderCase):
    def test_move_click_wheel(self):
        self.a.mouse_move(5, -3)
        self.a.mouse_button("left", True)
        self.a.mouse_button("left", False)
        self.a.wheel(-120)
        self.a.wheel(120, horizontal=True)
        self.assertEqual(self.events, [
            ("move", 5, -3),
            ("button", "left", True), ("button", "left", False),
            ("wheel", -120), ("hwheel", 120)])

    def test_zero_deltas_send_nothing(self):
        self.a.mouse_move(0, 0)
        self.a.wheel(0)
        self.assertEqual(self.events, [])


class Registry(RecorderCase):
    def test_every_default_chord_action_resolves(self):
        from ds5app import config as K

        reg = self.a.registry()
        for key, name in K.DEFAULT_CHORDS.items():
            if name in K.ENGINE_ACTIONS:    # the engine's, not OS actions
                continue
            self.assertIn(name, reg, f"chord {key!r}")

    def test_repeatable_flags(self):
        reg = self.a.registry()
        for name in ("volume_up", "volume_down", "brightness_up",
                     "brightness_down"):
            self.assertTrue(reg[name].repeatable, name)
        for name in ("media_next", "show_desktop", "task_view", "volume_mute"):
            self.assertFalse(reg[name].repeatable, name)


if __name__ == "__main__":
    unittest.main()
