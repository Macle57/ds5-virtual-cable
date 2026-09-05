"""The on-screen keyboard's model and pad driver -- no window, no desktop.

The layout is data, the highlight is arithmetic and typing is a list of
`SendInput` tuples, so all of it is asserted here exactly: which key the
highlight lands on, what Shift/Caps make a key type, the emits each pad
button produces, auto-repeat timing, and that the engine hands the keyboard
the pad while it is open (and the game a neutral one).
"""

from __future__ import annotations

import unittest

from ds5app import actions as A
from ds5app import osk as OSK


def key_at(model, row, col):
    return model.layout[row][col]


def label_row(model, row):
    return [k.label for k in model.layout[row]]


class Layout(unittest.TestCase):
    def test_five_rows_as_specified(self):
        m = OSK.KeyboardModel()
        self.assertEqual(len(m.layout), 5)
        self.assertEqual(label_row(m, 0)[:13], list("`1234567890-="))
        self.assertEqual(label_row(m, 0)[-1], "Backspace")
        self.assertEqual(label_row(m, 1)[0], "Tab")
        self.assertEqual("".join(label_row(m, 1)[1:]), "qwertyuiop[]\\")
        self.assertEqual(label_row(m, 2)[0], "Caps")
        self.assertEqual(label_row(m, 2)[-1], "Enter")
        self.assertEqual(label_row(m, 3)[0], "Shift")
        self.assertEqual(label_row(m, 3)[-1], "Shift")
        self.assertEqual(label_row(m, 4)[1], "Space")
        self.assertEqual(label_row(m, 4)[-2:], ["Paste", "Move"])

    def test_the_pad_glyph_hints_sit_on_the_steam_keys(self):
        m = OSK.KeyboardModel()
        self.assertEqual(key_at(m, 0, 13).hint, OSK.GLYPH_SQUARE)      # Backspace
        self.assertEqual(key_at(m, 2, 0).hint, OSK.GLYPH_L3)          # Caps
        self.assertEqual(key_at(m, 2, 12).hint, OSK.GLYPH_R2)         # Enter
        self.assertEqual(key_at(m, 3, 0).hint, OSK.GLYPH_L2)          # Shift
        self.assertEqual(key_at(m, 4, 1).hint, OSK.GLYPH_TRIANGLE)    # Space

    def test_shifted_symbols_follow_a_us_keyboard(self):
        m = OSK.KeyboardModel()
        m.set_shift_held(True)
        self.assertEqual([m.key_text(k) for k in m.layout[0][:13]],
                         list("~!@#$%^&*()_+"))
        self.assertEqual([m.key_text(k) for k in m.layout[1][11:]],
                         list("{}|"))
        self.assertEqual([m.key_text(k) for k in m.layout[2][10:12]],
                         [":", '"'])
        self.assertEqual([m.key_text(k) for k in m.layout[3][8:11]],
                         list("<>?"))

    def test_snapshot_geometry_is_consistent(self):
        m = OSK.KeyboardModel()
        snap = m.snapshot(unit=50, gap=4, margin=8)
        self.assertEqual(len(snap["keys"]), sum(len(r) for r in m.layout))
        self.assertEqual(sum(1 for k in snap["keys"] if k[6]), 1)  # one highlight
        for x, y, w, h, *_ in snap["keys"]:
            self.assertGreaterEqual(x, 8)
            self.assertLessEqual(x + w, snap["width"] - 8 + 4)
            self.assertLessEqual(y + h, snap["height"] - 8 + 4)
            self.assertEqual(h, 46)
        self.assertEqual(snap["width"], 2 * 8 + 15 * 50 - 4)


class Highlight(unittest.TestCase):
    def test_starts_on_the_home_row(self):
        m = OSK.KeyboardModel()
        self.assertEqual(m.current.label, "g")

    def test_sideways_moves_one_key_and_clamps(self):
        m = OSK.KeyboardModel()
        m.move(1, 0)
        self.assertEqual(m.current.label, "h")
        for _ in range(20):
            m.move(1, 0)
        self.assertEqual(m.current.label, "Enter")
        self.assertFalse(m.move(1, 0))            # already at the edge

    def test_vertical_moves_land_on_the_nearest_key_centre(self):
        m = OSK.KeyboardModel()                   # on "g" (centre 6.25 units)
        m.move(0, -1)
        self.assertEqual(m.current.label, "t")    # 6.0, not "y" at 7.0
        # From the wide keys down the right edge: each lands on the key
        # under it, not on "the same column index".
        m.row, m.col = 0, 13                      # Backspace, centre 14.0
        m.move(0, 1)
        self.assertEqual(m.current.label, "\\")
        m.move(0, 1)
        self.assertEqual(m.current.label, "Enter")
        m.move(0, 1)
        self.assertEqual(m.current.label, "Shift")
        self.assertEqual(m.col, len(m.layout[3]) - 1)   # the RIGHT shift
        m.move(0, 1)
        self.assertEqual(m.current.label, "Move")
        self.assertFalse(m.move(0, 1))            # bottom row
        m.row, m.col = 4, 1                       # Space, centre 4.5
        m.move(0, -1)
        self.assertEqual(m.current.label, "c")

    def test_the_nearest_rule_holds_for_every_key_pair_of_rows(self):
        m = OSK.KeyboardModel()
        for r in range(len(m.layout) - 1):
            for c in range(len(m.layout[r])):
                m.row, m.col = r, c
                cx = m._center(r, c)
                m.move(0, 1)
                best = min(abs(m._center(r + 1, i) - cx)
                           for i in range(len(m.layout[r + 1])))
                self.assertAlmostEqual(abs(m._center(m.row, m.col) - cx), best)

    def test_every_move_bumps_the_version(self):
        m = OSK.KeyboardModel()
        v = m.version
        m.move(-1, 0)
        self.assertEqual(m.version, v + 1)
        m.move(0, 0)
        self.assertEqual(m.version, v + 1)        # no move, no repaint


class Typing(unittest.TestCase):
    def test_a_letter_types_lowercase_by_default(self):
        m = OSK.KeyboardModel()
        self.assertEqual(m.activate(), [("text", "g")])

    def test_shift_held_types_uppercase_and_stays(self):
        m = OSK.KeyboardModel()
        m.set_shift_held(True)
        self.assertEqual(m.activate(), [("text", "G")])
        self.assertEqual(m.activate(), [("text", "G")])

    def test_the_shift_key_latches_for_one_character(self):
        m = OSK.KeyboardModel()
        m.activate(key_at(m, 3, 0))               # Shift on the layout
        self.assertTrue(m.shift)
        self.assertEqual(m.activate(), [("text", "G")])
        self.assertFalse(m.shift)
        self.assertEqual(m.activate(), [("text", "g")])

    def test_caps_affects_letters_only_and_shift_undoes_it(self):
        m = OSK.KeyboardModel()
        m.toggle_caps()
        self.assertEqual(m.key_text(key_at(m, 2, 5)), "G")
        self.assertEqual(m.key_text(key_at(m, 0, 1)), "1")      # not "!"
        m.set_shift_held(True)
        self.assertEqual(m.key_text(key_at(m, 2, 5)), "g")      # XOR
        self.assertEqual(m.key_text(key_at(m, 0, 1)), "!")

    def test_the_key_keys_emit_vk_combos(self):
        m = OSK.KeyboardModel()
        self.assertEqual(m.activate(key_at(m, 0, 13)), [("keys", (A.VK_BACK,))])
        self.assertEqual(m.activate(key_at(m, 2, 12)), [("keys", (A.VK_RETURN,))])
        self.assertEqual(m.activate(key_at(m, 1, 0)), [("keys", (A.VK_TAB,))])
        self.assertEqual(m.activate(key_at(m, 4, 0)),
                         [("keys", (A.VK_LWIN, A.VK_OEM_PERIOD))])    # emoji
        self.assertEqual(m.activate(key_at(m, 4, 6)),
                         [("keys", (A.VK_CONTROL, A.VK_V))])          # paste
        self.assertEqual(m.activate(key_at(m, 4, 1)), [("text", " ")])

    def test_move_key_toggles_move_mode_and_types_nothing(self):
        m = OSK.KeyboardModel()
        self.assertEqual(m.activate(key_at(m, 4, 7)), [])
        self.assertTrue(m.move_mode)

    def test_unicode_events_are_press_release_pairs_per_utf16_unit(self):
        self.assertEqual(A.unicode_events("a"),
                         [("unicode", 0x61, True), ("unicode", 0x61, False)])
        ev = A.unicode_events("\U0001F600")       # an emoji: two surrogates
        self.assertEqual([e[1] for e in ev], [0xD83D, 0xD83D, 0xDE00, 0xDE00])
        self.assertEqual(A.unicode_events(""), [])


class Driver(unittest.TestCase):
    def setUp(self):
        self.m = OSK.KeyboardModel()
        self.d = OSK.PadDriver(self.m)
        self.t = 100.0
        self.d.frame(set(), 0x80, 0x80, 0x80, 0x80, 0, 0, self.t)  # arming frame

    def step(self, buttons=(), lx=0x80, ly=0x80, rx=0x80, ry=0x80, l2=0, r2=0,
             dt=0.004):
        self.t += dt
        return self.d.frame(set(buttons), lx, ly, rx, ry, l2, r2, self.t)

    def test_the_first_frame_only_arms(self):
        d = OSK.PadDriver(OSK.KeyboardModel())
        s = d.frame({"cross"}, 0x80, 0x80, 0x80, 0x80, 0, 0, 1.0)
        self.assertEqual(s.emits, [])               # the opener's button
        s = d.frame({"cross"}, 0x80, 0x80, 0x80, 0x80, 0, 0, 1.1)
        self.assertEqual(s.emits, [])               # still held: not a press
        d.frame(set(), 0x80, 0x80, 0x80, 0x80, 0, 0, 1.2)
        s = d.frame({"cross"}, 0x80, 0x80, 0x80, 0x80, 0, 0, 1.3)
        self.assertEqual(s.emits, [("text", "g")])  # a real press

    def test_cross_presses_the_highlighted_key(self):
        self.assertEqual(self.step(("cross",)).emits, [("text", "g")])
        self.assertEqual(self.step(("cross",)).emits, [])   # held: no repeat

    def test_face_button_shortcuts(self):
        self.assertEqual(self.step(("square",)).emits, [("keys", (A.VK_BACK,))])
        self.step()
        self.assertEqual(self.step(("triangle",)).emits, [("text", " ")])
        self.step()
        self.assertEqual(self.step(("r1",)).emits, [("keys", (A.VK_RIGHT,))])
        self.step()
        self.assertEqual(self.step(("l1",)).emits, [("keys", (A.VK_LEFT,))])

    def test_r2_is_enter_on_the_edge_only(self):
        self.assertEqual(self.step(r2=255).emits, [("keys", (A.VK_RETURN,))])
        self.assertEqual(self.step(r2=255).emits, [])
        self.step(r2=0)
        self.assertEqual(self.step(r2=200).emits, [("keys", (A.VK_RETURN,))])

    def test_l2_held_is_shift(self):
        self.step(l2=255)
        self.assertTrue(self.m.shift)
        self.assertEqual(self.step(("cross",), l2=255).emits, [("text", "G")])
        self.step(l2=0)
        self.assertFalse(self.m.shift)

    def test_l3_toggles_caps(self):
        self.step(("l3",))
        self.assertTrue(self.m.caps)
        self.step()
        self.step(("l3",))
        self.assertFalse(self.m.caps)

    def test_circle_and_options_close(self):
        self.assertTrue(self.step(("circle",)).close)
        self.step()
        self.assertTrue(self.step(("options",)).close)

    def test_dpad_moves_once_then_repeats_after_the_delay(self):
        s = self.step(("dpad_right",))
        self.assertTrue(s.changed)
        self.assertEqual(self.m.current.label, "h")
        for _ in range(10):
            self.step(("dpad_right",), dt=0.02)    # 0.2 s: inside the delay
        self.assertEqual(self.m.current.label, "h")
        self.step(("dpad_right",), dt=0.2)         # past 0.35 s
        self.assertEqual(self.m.current.label, "j")
        self.step(("dpad_right",), dt=0.1)         # then every 80 ms
        self.assertEqual(self.m.current.label, "k")

    def test_left_stick_nudges_like_the_dpad(self):
        self.step(lx=0xFF)
        self.assertEqual(self.m.current.label, "h")
        self.step(lx=0x80)
        self.step(ly=0x00)
        self.assertIn(self.m.current.label, ("t", "y"))

    def test_backspace_repeats_while_square_is_held(self):
        self.step(("square",))
        n = 0
        for _ in range(6):
            n += len(self.step(("square",), dt=0.1).emits)
        self.assertGreaterEqual(n, 2)

    def test_right_stick_drags_the_window(self):
        total = 0
        for _ in range(25):
            total += self.step(rx=0xFF, dt=0.02).drag[0]
        self.assertGreater(total, 300)              # ~900 px/s * 0.5 s
        self.assertEqual(self.step().drag, (0, 0))

    def test_move_mode_redirects_the_dpad_to_the_window(self):
        self.m.toggle_move_mode()
        s = self.step(("dpad_down",))
        self.assertEqual(s.drag, (0, OSK.MOVE_STEP_PX))
        self.assertEqual(self.m.current.label, "g")   # highlight did not move


class FakeRenderer:
    def __init__(self):
        self.calls = []
        self.snaps = []

    def show(self, snap):
        self.calls.append("show")
        self.snaps.append(snap)

    def update(self, snap):
        self.calls.append("update")
        self.snaps.append(snap)

    def hide(self):
        self.calls.append("hide")

    def position(self):
        return (50, 60)

    def close(self):
        self.calls.append("close")


class _Frame:
    def __init__(self, buttons=(), lx=0x80, ly=0x80, rx=0x80, ry=0x80, l2=0, r2=0):
        self.buttons = set(buttons)
        self.lx, self.ly, self.rx, self.ry, self.l2, self.r2 = lx, ly, rx, ry, l2, r2


class KeyboardObject(unittest.TestCase):
    def setUp(self):
        self.batches = []
        self.acts = A.OsActions(inject=self.batches.append, audio=object(),
                                sleep=lambda s: None)
        self.r = FakeRenderer()
        self.kb = OSK.OnScreenKeyboard(self.acts, renderer_factory=lambda: self.r)

    def run_thunks(self, thunks):
        for fn in thunks:
            fn()

    def test_open_returns_a_thunk_that_shows_and_is_lazy(self):
        self.assertIsNone(self.kb._renderer)      # nothing built until opened
        fn = self.kb.open()
        self.assertTrue(self.kb.is_open)
        self.assertEqual(self.r.calls, [])        # nothing foreign inline
        fn()
        self.assertEqual(self.r.calls, ["show"])
        self.assertIsNone(self.kb.open())         # already open

    def test_toggle_closes_with_a_hide_thunk(self):
        self.kb.open()()
        fn = self.kb.toggle()
        self.assertFalse(self.kb.is_open)
        fn()
        self.assertEqual(self.r.calls[-1], "hide")

    def test_frames_type_through_inject_and_repaint_on_change(self):
        self.kb.open()()
        self.run_thunks(self.kb.on_frame(_Frame(), 1.0))            # arms
        self.run_thunks(self.kb.on_frame(_Frame(("dpad_right",)), 1.1))
        self.assertEqual(self.r.calls[-1], "update")
        self.run_thunks(self.kb.on_frame(_Frame(), 1.2))
        self.run_thunks(self.kb.on_frame(_Frame(("cross",)), 1.3))
        self.assertEqual(self.batches[-1],
                         [("unicode", ord("h"), True), ("unicode", ord("h"), False)])

    def test_circle_closes_from_the_pad(self):
        self.kb.open()()
        self.run_thunks(self.kb.on_frame(_Frame(), 1.0))
        self.run_thunks(self.kb.on_frame(_Frame(("circle",)), 1.1))
        self.assertFalse(self.kb.is_open)
        self.assertEqual(self.r.calls[-1], "hide")
        self.assertEqual(self.kb.on_frame(_Frame(), 1.2), [])       # closed: inert

    def test_dragging_records_a_position_in_the_snapshot(self):
        self.kb.open()()
        self.run_thunks(self.kb.on_frame(_Frame(), 1.0))
        for i in range(30):
            self.run_thunks(self.kb.on_frame(_Frame(rx=0xFF), 1.0 + 0.02 * (i + 1)))
        self.assertIsNotNone(self.kb.pos)
        self.assertGreater(self.kb.pos[0], 50)
        self.assertEqual(self.r.snaps[-1]["pos"], self.kb.pos)

    def test_shutdown_closes_the_renderer(self):
        self.kb.open()()
        self.kb.shutdown()
        self.assertFalse(self.kb.is_open)
        self.assertEqual(self.r.calls[-1], "close")


if __name__ == "__main__":
    unittest.main()
