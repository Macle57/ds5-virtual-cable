"""User macros: `input.macros` -> registry entries a chord can bind to.

The seams are the same recorders the actions/intercept suites use; nothing
here touches the desktop or starts a process. What is under test:

  * a key macro is ONE `SendInput` (press in order, release in reverse) --
    the same atomicity rule the built-ins live by;
  * a `run` macro goes through the `launch` seam, off the reader thread;
  * bad macros are skipped one at a time, never fatal, and never shadow a
    built-in;
  * the config layer stores them as written and round-trips them;
  * a live `update_config` re-resolves them (the dashboard's Save button).
"""

from __future__ import annotations

import unittest

try:
    from ds5app import actions as A
    from ds5app import config as K
    from ds5app import intercept as I
except ImportError as exc:  # pragma: no cover
    A = K = I = None
    _WHY = str(exc)

from tests.test_intercept import EngineCase, report


@unittest.skipIf(A is None, "the app package is not importable here")
class KeyNames(unittest.TestCase):
    def test_common_spellings_resolve(self):
        self.assertEqual(A.key_vk("ctrl"), 0x11)
        self.assertEqual(A.key_vk("Control"), 0x11)
        self.assertEqual(A.key_vk(" ESC "), A.VK_ESCAPE)
        self.assertEqual(A.key_vk("Page Up"), 0x21)
        self.assertEqual(A.key_vk("f12"), 0x7B)
        self.assertEqual(A.key_vk("a"), ord("A"))
        self.assertEqual(A.key_vk("7"), ord("7"))
        self.assertEqual(A.key_vk("numpad0"), 0x60)

    def test_unknown_or_non_string_is_none(self):
        self.assertIsNone(A.key_vk("hyperspace"))
        self.assertIsNone(A.key_vk(7))
        self.assertIsNone(A.key_vk(None))

    def test_every_served_name_resolves_to_itself(self):
        for name in A.KEY_NAMES:
            self.assertEqual(A.key_vk(name), A.KEY_NAMES[name], name)


@unittest.skipIf(A is None, "the app package is not importable here")
class MacroSpec(unittest.TestCase):
    def setUp(self):
        self.batches: list[list] = []
        self.launched: list[str] = []
        self.a = A.OsActions(inject=self.batches.append,
                             launch=lambda c: (self.launched.append(c), True)[1])

    def test_key_macro_is_one_atomic_chord(self):
        spec = self.a.macro_spec("task_manager",
                                 {"keys": ["ctrl", "shift", "esc"],
                                  "label": "Task Manager"})
        self.assertIsNotNone(spec)
        self.assertEqual(spec.doc, "Task Manager")
        self.assertFalse(spec.repeatable)
        spec.run({})
        self.assertEqual(len(self.batches), 1)          # ONE SendInput
        self.assertEqual(self.batches[0], [
            ("key", 0x11, True), ("key", A.VK_SHIFT, True),
            ("key", A.VK_ESCAPE, True),
            ("key", A.VK_ESCAPE, False), ("key", A.VK_SHIFT, False),
            ("key", 0x11, False)])

    def test_repeat_flag_and_default_label(self):
        spec = self.a.macro_spec("louder", {"keys": ["volume_up"], "repeat": True})
        self.assertTrue(spec.repeatable)
        self.assertEqual(spec.doc, "louder")

    def test_run_macro_goes_through_the_launch_seam(self):
        spec = self.a.macro_spec("notes", {"run": "  notepad.exe  "})
        self.assertIsNotNone(spec)
        spec.run({})
        self.assertEqual(self.launched, ["notepad.exe"])
        self.assertEqual(self.batches, [])
        self.assertFalse(spec.repeatable)        # never, whatever was asked

    def test_malformed_macros_are_none(self):
        bad = [
            ("Bad-Name", {"keys": ["a"]}),        # name grammar
            ("x", "ctrl+c"),                       # not an object
            ("x", {}),                             # neither keys nor run
            ("x", {"keys": []}),                   # empty
            ("x", {"keys": ["ctrl", "warp"]}),     # unknown key
            ("x", {"keys": list("abcdefghi")}),    # > 8 keys
            ("x", {"run": "   "}),                 # blank command
        ]
        for name, definition in bad:
            with self.assertLogs("ds5app.actions", level="WARNING"):
                self.assertIsNone(self.a.macro_spec(name, definition),
                                  (name, definition))


@unittest.skipIf(K is None, "the app package is not importable here")
class MacroConfig(unittest.TestCase):
    def test_round_trip_and_normalisation(self):
        ic = K.InputConfig.from_dict({"macros": {
            " Task_Manager ": {"keys": ["ctrl", "shift", "esc"], "label": "TM"},
            "notes": {"run": "notepad.exe"},
            "junk": "not an object",
        }})
        self.assertEqual(set(ic.macros), {"task_manager", "notes"})
        out = ic.to_dict()["macros"]
        self.assertEqual(list(out), ["notes", "task_manager"])   # sorted
        self.assertEqual(out["task_manager"]["keys"], ["ctrl", "shift", "esc"])
        self.assertEqual(K.InputConfig.from_dict(ic.to_dict()).macros,
                         ic.macros)

    def test_null_is_a_deletion_tombstone(self):
        ic = K.InputConfig.from_dict({"macros": {"gone": None,
                                                 "kept": {"keys": ["a"]}}})
        self.assertEqual(set(ic.macros), {"kept"})
        self.assertNotIn("gone", ic.to_dict()["macros"])

    def test_absent_and_wrong_type(self):
        self.assertEqual(K.InputConfig.from_dict({}).macros, {})
        self.assertEqual(K.InputConfig.from_dict({"macros": [1, 2]}).macros, {})
        self.assertNotIn("macros", K.InputConfig.from_dict({}).extra)


@unittest.skipIf(I is None, "the app package is not importable here")
class MacroChords(EngineCase):
    def test_a_chord_bound_to_a_key_macro_fires_it(self):
        self.make(chords={"circle": "task_manager"},
                  macros={"task_manager": {"keys": ["ctrl", "shift", "esc"]}})
        self.feed(report(buttons=("ps",)),
                  report(buttons=("ps", "circle")))
        self.assertEqual(len(self.batches), 1)
        self.assertEqual(self.batches[0][0], ("key", 0x11, True))
        self.assertEqual(self.batches[0][-1], ("key", 0x11, False))

    def test_a_chord_bound_to_a_run_macro_launches(self):
        launched = []
        self.make(chords={"circle": "notes"},
                  macros={"notes": {"run": "notepad.exe"}})
        self.acts.launch = lambda c: (launched.append(c), True)[1]
        self.feed(report(buttons=("ps",)),
                  report(buttons=("ps", "circle")))
        self.assertEqual(launched, ["notepad.exe"])
        self.assertEqual(self.events, [])

    def test_a_repeating_macro_repeats_while_held(self):
        self.make(chords={"circle": "louder"}, repeat_ms=150,
                  macros={"louder": {"keys": ["volume_up"], "repeat": True}})
        self.feed(report(buttons=("ps",)),
                  report(buttons=("ps", "circle")))
        for _ in range(5):
            self.clock.advance(0.3)
            self.feed(report(buttons=("ps", "circle")))
        ups = [e for e in self.events if e == ("key", A.VK_VOLUME_UP, True)]
        self.assertGreater(len(ups), 1)

    def test_a_macro_cannot_shadow_a_built_in(self):
        with self.assertLogs("ds5app.intercept", level="WARNING"):
            self.make(chords={"circle": "volume_mute"},
                      macros={"volume_mute": {"run": "evil.exe"}})
        self.feed(report(buttons=("ps",)),
                  report(buttons=("ps", "circle")))
        self.assertIn(("key", A.VK_VOLUME_MUTE, True), self.events)

    def test_a_bad_macro_only_disables_itself(self):
        with self.assertLogs("ds5app.actions", level="WARNING"):
            self.make(chords={"circle": "broken", "square": "fine"},
                      macros={"broken": {"keys": ["warp"]},
                              "fine": {"keys": ["f5"]}})
        self.feed(report(buttons=("ps",)),
                  report(buttons=("ps", "circle")),
                  report(buttons=("ps",)),
                  report(buttons=("ps", "square")))
        self.assertIn(("key", 0x74, True), self.events)     # F5 fired
        self.assertNotIn("broken", self.eng.registry)

    def test_live_update_re_resolves_macros(self):
        self.make(chords={"circle": "later"})
        self.feed(report(buttons=("ps",)),
                  report(buttons=("ps", "circle")),
                  report())
        self.assertEqual(self.events, [])                    # unknown yet
        self.eng.update_config(K.InputConfig.from_dict({
            "chords": {"circle": "later"},
            "macros": {"later": {"keys": ["win", "e"]}}}))
        self.feed(report(buttons=("ps",)),
                  report(buttons=("ps", "circle")))
        self.assertIn(("key", A.VK_LWIN, True), self.events)
        self.assertIn(("key", ord("E"), True), self.events)


if __name__ == "__main__":
    unittest.main()
