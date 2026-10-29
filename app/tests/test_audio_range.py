"""Dropping the volume ranges Windows' audio service kept from an older build.

`audio_range.EndpointVolumes` is the only part that talks to Windows; the
decision -- what counts as stale, when to restart the audio service, when to
only tell the user -- runs here over fakes, with a fake clock.
"""

from __future__ import annotations

import unittest

from ds5app import audio_range as AR

MIC = "Headset Microphone (3- DualSense Wireless Controller)"
SPK = "Speakers (3- DualSense Wireless Controller)"


def rng(name, lo, hi, step, id_="x"):
    return AR.EndpointRange(id_, name, lo, hi, step)


STALE_MIC = rng(MIC, -96.0, 0.0, 1.0, "m")
STALE_SPK = rng(SPK, -96.0, 0.0, 1.0, "s")
GOOD_MIC = rng(MIC, 0.0, 48.0, 0.4765625, "m")
GOOD_SPK = rng(SPK, -100.0, 0.0, 1.0, "s")


class Staleness(unittest.TestCase):
    def test_the_old_assumed_range_on_a_dualsense_is_stale(self):
        self.assertTrue(AR.is_stale(STALE_MIC))
        self.assertTrue(AR.is_stale(STALE_SPK))

    def test_the_real_pads_ranges_are_not(self):
        # What pycaw reports for a real wired pad and for the fixed bridge.
        self.assertFalse(AR.is_stale(GOOD_MIC))
        self.assertFalse(AR.is_stale(GOOD_SPK))

    def test_other_devices_are_never_touched(self):
        # Ranges seen on legion: Steam's virtual speakers and a webcam mic sit
        # at -96..0 too, in other steps -- and are none of our business anyway.
        for r in (rng("Speakers (Steam Streaming Speakers)", -96.0, 0.0, 1.5),
                  rng("Microphone (Iriun Webcam)", -96.0, 0.0, 0.5),
                  rng("Some Other Headset", -96.0, 0.0, 1.0)):
            self.assertFalse(AR.is_stale(r), r.name)


class FakeAudio:
    """`volume_ranges()` replies from a script; the last entry repeats."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = 0

    def volume_ranges(self):
        self.calls += 1
        r = self.replies[0] if len(self.replies) == 1 else self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


class Refresher(unittest.TestCase):
    def make(self, audio, admin=True, restart_ok=True, after_restart=None):
        self.clock = Clock()
        self.restarts = 0

        def restart():
            self.restarts += 1
            if after_restart is not None:
                audio.replies = list(after_restart)
            return restart_ok

        return AR.RangeRefresher(audio, restart, lambda: admin,
                                 sleep=self.clock.sleep, clock=self.clock,
                                 watch_s=10.0, poll_s=1.0)

    def test_nothing_stale_means_no_restart(self):
        r = self.make(FakeAudio([GOOD_MIC, GOOD_SPK]))
        self.assertEqual(r.run(), AR.OK)
        self.assertEqual(self.restarts, 0)
        self.assertLessEqual(self.clock.t, 10.0)

    def test_stale_and_elevated_restarts_once_and_confirms(self):
        r = self.make(FakeAudio([STALE_MIC, STALE_SPK]),
                      after_restart=[[], [GOOD_MIC, GOOD_SPK]])
        self.assertEqual(r.run(), AR.REFRESHED)
        self.assertEqual(self.restarts, 1)

    def test_an_endpoint_that_shows_up_late_is_still_caught(self):
        # Windows builds the endpoints a moment after usbip attach returns.
        r = self.make(FakeAudio([], [], [GOOD_SPK], [STALE_MIC, GOOD_SPK]),
                      after_restart=[[GOOD_MIC, GOOD_SPK]])
        self.assertEqual(r.run(), AR.REFRESHED)
        self.assertEqual(self.restarts, 1)

    def test_not_elevated_only_reports(self):
        r = self.make(FakeAudio([STALE_MIC]), admin=False)
        self.assertEqual(r.run(), AR.NEED_ADMIN)
        self.assertEqual(self.restarts, 0)

    def test_a_failed_restart_is_reported(self):
        r = self.make(FakeAudio([STALE_MIC]), restart_ok=False)
        self.assertEqual(r.run(), AR.FAILED)

    def test_a_restart_that_did_not_help_is_reported_not_repeated(self):
        r = self.make(FakeAudio([STALE_MIC]), after_restart=[[STALE_MIC]])
        self.assertEqual(r.run(), AR.FAILED)
        self.assertEqual(self.restarts, 1)

    def test_another_bridge_already_refreshed_it(self):
        # Two pads attach together; the other process restarted the service
        # while this one waited for the lock: no second restart.
        audio = FakeAudio([STALE_MIC], [GOOD_MIC])
        r = self.make(audio)
        self.assertEqual(r.run(), AR.REFRESHED)
        self.assertEqual(self.restarts, 0)

    def test_a_broken_enumeration_is_treated_as_nothing_stale(self):
        r = self.make(FakeAudio(OSError("no audio")))
        self.assertEqual(r.run(), AR.OK)
        self.assertEqual(self.restarts, 0)

    def test_the_lock_is_held_around_the_restart(self):
        events = []

        class Lock:
            def __enter__(self):
                events.append("enter")

            def __exit__(self, *exc):
                events.append("exit")
                return False

        audio = FakeAudio([STALE_MIC])
        clock = Clock()

        def restart():
            events.append("restart")
            audio.replies = [[GOOD_MIC]]
            return True

        r = AR.RangeRefresher(audio, restart, lambda: True, lock=Lock(),
                              sleep=clock.sleep, clock=clock, watch_s=5, poll_s=1)
        self.assertEqual(r.run(), AR.REFRESHED)
        self.assertEqual(events, ["enter", "restart", "exit"])


class Reporting(unittest.TestCase):
    def run_with(self, outcome):
        events = []
        orig = AR.RangeRefresher.run
        AR.RangeRefresher.run = lambda self: outcome
        try:
            got = AR.check_after_attach(lambda k, t: events.append((k, t)))
        finally:
            AR.RangeRefresher.run = orig
        return got, events

    @unittest.skipUnless(AR.sys.platform == "win32", "Windows only")
    def test_the_user_hears_about_it_only_when_it_matters(self):
        self.assertEqual(self.run_with(AR.OK), (AR.OK, []))
        got, ev = self.run_with(AR.NEED_ADMIN)
        self.assertEqual(got, AR.NEED_ADMIN)
        self.assertEqual(ev, [("warn", AR.ADVICE)])
        got, ev = self.run_with(AR.REFRESHED)
        self.assertEqual(ev[0][0], "info")


class ServiceWiring(unittest.TestCase):
    def test_a_bare_bridge_service_never_touches_the_audio_service(self):
        # Tests drive `_start_inner` with fakes; the check is opt-in.
        try:
            from ds5app import service as SVC
        except Exception:  # noqa: BLE001
            self.skipTest("the hardware stack is not importable here")
        import inspect

        sig = inspect.signature(SVC.BridgeService.__init__)
        self.assertIs(sig.parameters["check_volume_ranges"].default, False)


if __name__ == "__main__":
    unittest.main()
