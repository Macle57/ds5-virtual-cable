"""Voice typing through the pad's microphone: the decisions, with COM faked.

`audio_default.AudioSystem` is the only thing that talks to Windows and it
fails soft by construction; everything here exercises the pure half --
which endpoint is the pad, what the toggle remembers and restores, and that
a broken audio layer still lets Win+H go out.
"""

from __future__ import annotations

import unittest

from ds5app import actions as A
from ds5app import audio_default as AD


def ep(id_, name, state=AD.DEVICE_STATE_ACTIVE):
    return AD.Endpoint(id_, name, state)


class Selection(unittest.TestCase):
    def test_picks_the_active_dualsense_microphone(self):
        eps = [ep("a", "Microphone (Realtek(R) Audio)"),
               ep("b", "Headset Microphone (2- DualSense Wireless Controller)")]
        self.assertEqual(AD.select_pad_mic(eps).id, "b")

    def test_ignores_dualsense_entries_that_are_not_active(self):
        # Windows keeps the endpoint of every pad ever plugged in; only an
        # ACTIVE one can capture. These are the states this machine showed.
        eps = [ep("x", "Headset Microphone (DualSense Wireless Controller)",
                  AD.DEVICE_STATE_NOTPRESENT),
               ep("y", "Headset Microphone (3- DualSense Wireless Controller)",
                  AD.DEVICE_STATE_UNPLUGGED),
               ep("z", "Microphone (Realtek(R) Audio)")]
        self.assertIsNone(AD.select_pad_mic(eps))

    def test_case_and_instance_prefix_do_not_matter(self):
        eps = [ep("q", "headset microphone (7- DUALSENSE wireless controller)")]
        self.assertEqual(AD.select_pad_mic(eps).id, "q")

    def test_two_pads_give_a_stable_answer(self):
        eps = [ep("b", "Headset Microphone (3- DualSense Wireless Controller)"),
               ep("a", "Headset Microphone (2- DualSense Wireless Controller)")]
        self.assertEqual(AD.select_pad_mic(eps).id, "a")
        self.assertEqual(AD.select_pad_mic(list(reversed(eps))).id, "a")

    def test_no_endpoints_no_pick(self):
        self.assertIsNone(AD.select_pad_mic([]))


class FakeAudio:
    def __init__(self, endpoints, default="realtek", fail_set=False):
        self.endpoints = endpoints
        self.defaults = {0: default, 1: default, 2: default}
        self.sets = []
        self.fail_set = fail_set

    def capture_endpoints(self):
        return list(self.endpoints)

    def default_capture_id(self, role):
        return self.defaults.get(role)

    def set_default_capture(self, device_id, role):
        if self.fail_set:
            raise OSError("IPolicyConfig said no")
        self.sets.append((device_id, role))
        self.defaults[role] = device_id
        return True


class Toggle(unittest.TestCase):
    PAD = ep("pad", "Headset Microphone (2- DualSense Wireless Controller)")

    def make(self, audio):
        self.presses = 0
        self.slept = []

        def press():
            self.presses += 1
        return AD.DictationToggle(audio, press, sleep=self.slept.append)

    def test_first_press_borrows_the_mic_then_opens_voice_typing(self):
        audio = FakeAudio([self.PAD])
        t = self.make(audio)
        self.assertTrue(t.toggle())
        self.assertEqual(audio.sets, [("pad", 0), ("pad", 1), ("pad", 2)])
        self.assertEqual(self.slept, [AD.SWITCH_SETTLE_S])   # settle BEFORE Win+H
        self.assertEqual(self.presses, 1)

    def test_second_press_closes_and_restores_every_role(self):
        audio = FakeAudio([self.PAD])
        t = self.make(audio)
        t.toggle()
        audio.sets.clear()
        self.assertFalse(t.toggle())
        self.assertEqual(self.presses, 2)
        self.assertEqual(sorted(audio.sets),
                         [("realtek", 0), ("realtek", 1), ("realtek", 2)])
        self.assertEqual(audio.defaults, {0: "realtek", 1: "realtek", 2: "realtek"})

    def test_restore_on_close_gives_the_mic_back_without_keystrokes(self):
        audio = FakeAudio([self.PAD])
        t = self.make(audio)
        t.toggle()
        audio.sets.clear()
        t.restore()
        self.assertEqual(self.presses, 1)                    # no Win+H
        self.assertEqual(audio.defaults[0], "realtek")
        self.assertFalse(t.active)
        t.restore()                                           # idempotent
        self.assertEqual(len(audio.sets), 3)

    def test_no_pad_mic_still_toggles_voice_typing(self):
        audio = FakeAudio([ep("r", "Microphone (Realtek(R) Audio)")])
        t = self.make(audio)
        self.assertTrue(t.toggle())
        self.assertEqual(audio.sets, [])
        self.assertEqual(self.slept, [])
        self.assertEqual(self.presses, 1)
        self.assertFalse(t.toggle())
        self.assertEqual(self.presses, 2)

    def test_pad_already_default_means_nothing_to_restore(self):
        audio = FakeAudio([self.PAD], default="pad")
        t = self.make(audio)
        t.toggle()
        self.assertEqual(audio.sets, [])
        t.toggle()
        self.assertEqual(audio.sets, [])

    def test_a_failing_audio_layer_never_reaches_the_caller(self):
        class Boom:
            def capture_endpoints(self):
                raise OSError("CoCreateInstance failed")

            def default_capture_id(self, role):
                raise OSError("no")

            def set_default_capture(self, device_id, role):
                raise OSError("no")

        t = self.make(Boom())
        self.assertTrue(t.toggle())
        self.assertEqual(self.presses, 1)
        self.assertFalse(t.toggle())
        t.restore()

    def test_set_default_failing_leaves_nothing_to_restore(self):
        audio = FakeAudio([self.PAD], fail_set=True)
        t = self.make(audio)
        t.toggle()
        self.assertIsNone(t._previous)
        self.assertEqual(self.slept, [])


class ThroughOsActions(unittest.TestCase):
    def test_the_dictation_action_sends_win_h_through_inject(self):
        batches = []
        audio = FakeAudio([Toggle.PAD])
        acts = A.OsActions(inject=batches.append, audio=audio,
                           sleep=lambda s: None)
        acts.registry()["dictation"].run({})
        self.assertEqual(batches[-1], [("key", A.VK_LWIN, True), ("key", A.VK_H, True),
                                       ("key", A.VK_H, False), ("key", A.VK_LWIN, False)])
        self.assertEqual(audio.defaults[0], "pad")
        acts.close()                                          # restores
        self.assertEqual(audio.defaults[0], "realtek")

    def test_the_real_audio_system_fails_soft_when_asked_nonsense(self):
        # Whatever the platform, a bad device id is a False, not a raise.
        self.assertFalse(AD.AudioSystem().set_default_capture("", 0))
        self.assertIsInstance(AD.AudioSystem().capture_endpoints(), list)


if __name__ == "__main__":
    unittest.main()
