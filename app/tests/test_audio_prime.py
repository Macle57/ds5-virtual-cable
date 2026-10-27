"""Speaker auto-prime: the decisions, with WASAPI faked.

`audio_prime.WasapiPrimer` is the only thing that talks to Windows and it
fails soft by construction; everything here exercises the pure half -- which
endpoint is the new pad's speaker, how long to wait for it, which failures
are worth another try, and that the setting and a broken audio layer can
never reach the manager as an exception.
"""

from __future__ import annotations

import logging
import unittest

from ds5app import audio_default as AD
from ds5app import audio_prime as P

ACTIVE, GONE = AD.DEVICE_STATE_ACTIVE, AD.DEVICE_STATE_NOTPRESENT
PAD1 = "Speakers (2- DualSense Wireless Controller)"
PAD2 = "Speakers (3- DualSense Wireless Controller)"


def ep(id_, name, state=ACTIVE):
    return AD.Endpoint(id_, name, state)


class Clock:
    """A clock the fake sleep advances, so waits cost no wall time."""

    def __init__(self):
        self.t = 0.0
        self.sleeps = []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


class FakeAudio:
    """`render_endpoints()` answers from a script keyed by time; `prime()`
    answers from a queue of outcomes (default: success)."""

    def __init__(self, clock, timeline, outcomes=None, raise_on_list=False):
        self.clock = clock
        #: [(from_time, [endpoints])], the last entry whose time has passed wins
        self.timeline = timeline
        self.outcomes = list(outcomes or [])
        self.primed = []
        self.raise_on_list = raise_on_list

    def render_endpoints(self):
        if self.raise_on_list:
            raise OSError("enumerator gone")
        cur = []
        for t, eps in self.timeline:
            if self.clock() >= t:
                cur = eps
        return list(cur)

    def prime(self, device_id, hold_s):
        self.primed.append(device_id)
        if self.outcomes:
            out = self.outcomes.pop(0)
            if isinstance(out, Exception):
                raise out
            return out
        return P.PrimeOutcome(True, None, "4 ch 48000 Hz", 12.0)


def primer(audio, clock, **kw):
    kw.setdefault("sleep", clock.sleep)
    kw.setdefault("clock", clock)
    kw.setdefault("start_thread", lambda fn: fn())
    return P.AutoPrimer(audio=audio, **kw)


class _Quiet(unittest.TestCase):
    def setUp(self):
        self._log = logging.getLogger("ds5app.audio_prime")
        self._was = self._log.disabled
        self._log.disabled = True

    def tearDown(self):
        self._log.disabled = self._was


class Selection(unittest.TestCase):
    def test_only_active_dualsense_speakers(self):
        # Windows keeps an endpoint for every pad ever attached (this machine
        # lists five "N- DualSense" speakers); only an active one opens.
        eps = [ep("r", "Speakers (Realtek(R) Audio)"),
               ep("old", "Speakers (5- DualSense Wireless Controller)", GONE),
               ep("pad", PAD1)]
        self.assertEqual([e.id for e in P.pad_speakers(eps)], ["pad"])

    def test_case_and_prefix_do_not_matter_and_order_is_stable(self):
        eps = [ep("b", PAD2.upper()), ep("a", PAD1.lower())]
        self.assertEqual([e.id for e in P.pad_speakers(eps)], ["a", "b"])
        self.assertEqual([e.id for e in P.pad_speakers(list(reversed(eps)))],
                         ["a", "b"])

    def test_empty_needle_matches_nothing(self):
        self.assertEqual(P.pad_speakers([ep("a", PAD1)], needle=""), [])


class Retrying(unittest.TestCase):
    def test_exclusive_use_is_not_retried(self):
        out = P.PrimeOutcome(False, P.AUDCLNT_E_DEVICE_IN_USE)
        self.assertFalse(P.worth_retrying(out))

    def test_signed_hresult_is_understood(self):
        # ctypes hands HRESULTs back signed.
        signed = P.AUDCLNT_E_DEVICE_IN_USE - (1 << 32)
        self.assertFalse(P.worth_retrying(P.PrimeOutcome(False, signed)))

    def test_rebuild_and_unknown_failures_are_retried(self):
        for hr in (P.AUDCLNT_E_DEVICE_INVALIDATED, P.E_NOTFOUND,
                   P.AUDCLNT_E_ENDPOINT_CREATE_FAILED, None):
            self.assertTrue(P.worth_retrying(P.PrimeOutcome(False, hr)), hr)

    def test_success_is_not_retried(self):
        self.assertFalse(P.worth_retrying(P.PrimeOutcome(True)))

    def test_hr_text(self):
        self.assertEqual(P.hr_text(P.AUDCLNT_E_DEVICE_IN_USE - (1 << 32)),
                         "0x8889000a AUDCLNT_E_DEVICE_IN_USE")
        self.assertEqual(P.hr_text(0x80004005), "0x80004005")
        self.assertEqual(P.hr_text(None), "")


class Waiting(_Quiet):
    def test_waits_for_the_endpoint_then_primes_it_once(self):
        c = Clock()
        audio = FakeAudio(c, [(0.0, []), (2.0, [ep("pad", PAD1)])])
        self.assertEqual(primer(audio, c, settle_s=1.0).run("d42f"), ["pad"])
        self.assertEqual(audio.primed, ["pad"])
        # Polled until it appeared at t=2, then settled before opening.
        self.assertEqual(c.sleeps[-1], 1.0)
        self.assertLess(c.t, 4.0)

    def test_new_pad_wins_over_an_already_bridged_one(self):
        # Pad 1 is bridged and ACTIVE before this run begins; pad 2's endpoint
        # arrives later. Only pad 2's is opened, and the run does not settle
        # for pad 1's just because it is there first.
        c = Clock()
        audio = FakeAudio(c, [(0.0, [ep("one", PAD1)]),
                              (3.0, [ep("one", PAD1), ep("two", PAD2)])])
        self.assertEqual(primer(audio, c).run(), ["two"])
        self.assertEqual(audio.primed, ["two"])

    def test_already_active_endpoint_is_primed_after_the_wait(self):
        # The endpoint beat the bridge's "attached" report: nothing NEW will
        # turn up, so after `wait_s` every active pad speaker is primed.
        c = Clock()
        audio = FakeAudio(c, [(0.0, [ep("pad", PAD1)])])
        self.assertEqual(primer(audio, c, wait_s=5.0).run(), ["pad"])
        self.assertGreaterEqual(c.t, 5.0)
        self.assertNotIn(P.SETTLE_S, c.sleeps[-1:])  # no settle: it is old

    def test_nothing_appears_nothing_primed(self):
        c = Clock()
        audio = FakeAudio(c, [(0.0, [ep("r", "Speakers (Realtek(R) Audio)")])])
        self.assertEqual(primer(audio, c, wait_s=4.0).run(), [])
        self.assertEqual(audio.primed, [])
        self.assertLess(c.t, 4.0 + P.POLL_S + 1e-9)   # bounded

    def test_inactive_entries_never_count_as_new(self):
        c = Clock()
        audio = FakeAudio(c, [(0.0, []), (1.0, [ep("pad", PAD1, GONE)])])
        self.assertEqual(primer(audio, c, wait_s=3.0).run(), [])

    def test_a_broken_enumerator_is_survived(self):
        c = Clock()
        audio = FakeAudio(c, [], raise_on_list=True)
        self.assertEqual(primer(audio, c, wait_s=2.0).run(), [])


class Priming(_Quiet):
    def test_retries_a_transient_failure(self):
        c = Clock()
        audio = FakeAudio(c, [(0.0, []), (1.0, [ep("pad", PAD1)])], outcomes=[
            P.PrimeOutcome(False, P.AUDCLNT_E_DEVICE_INVALIDATED),
            P.PrimeOutcome(True, None, "", 900.0)])
        self.assertEqual(primer(audio, c, retry_gap_s=1.5).run(), ["pad"])
        self.assertEqual(audio.primed, ["pad", "pad"])
        self.assertIn(1.5, c.sleeps)

    def test_gives_up_after_the_attempts(self):
        c = Clock()
        bad = P.PrimeOutcome(False, P.E_NOTFOUND)
        audio = FakeAudio(c, [(0.0, []), (1.0, [ep("pad", PAD1)])],
                          outcomes=[bad, bad, bad, bad])
        self.assertEqual(primer(audio, c, attempts=3).run(), [])
        self.assertEqual(len(audio.primed), 3)

    def test_exclusive_holder_stops_at_once(self):
        c = Clock()
        audio = FakeAudio(c, [(0.0, []), (1.0, [ep("pad", PAD1)])],
                          outcomes=[P.PrimeOutcome(False,
                                                   P.AUDCLNT_E_DEVICE_IN_USE)])
        self.assertEqual(primer(audio, c).run(), [])
        self.assertEqual(audio.primed, ["pad"])

    def test_a_raising_prime_is_a_failed_attempt(self):
        c = Clock()
        audio = FakeAudio(c, [(0.0, []), (1.0, [ep("pad", PAD1)])],
                          outcomes=[OSError("boom")])
        self.assertEqual(primer(audio, c).run(), ["pad"])   # 2nd try succeeds
        self.assertEqual(len(audio.primed), 2)


class Scheduling(_Quiet):
    def test_off_means_no_run(self):
        c = Clock()
        started = []
        audio = FakeAudio(c, [(0.0, [ep("pad", PAD1)])])
        p = primer(audio, c, enabled=lambda: False,
                   start_thread=lambda fn: started.append(fn))
        self.assertFalse(p.schedule("d42f"))
        self.assertEqual(started, [])

    def test_setting_is_read_at_every_schedule(self):
        c = Clock()
        flag = {"on": False}
        started = []
        p = primer(FakeAudio(c, []), c, enabled=lambda: flag["on"],
                   start_thread=lambda fn: started.append(fn))
        p.schedule()
        flag["on"] = True
        p.schedule()
        self.assertEqual(len(started), 1)

    def test_schedule_returns_before_the_run(self):
        # The run goes to the thread starter; nothing is listed or opened on
        # the caller's (the manager's) thread.
        c = Clock()
        audio = FakeAudio(c, [(0.0, []), (1.0, [ep("pad", PAD1)])])
        started = []
        p = primer(audio, c, start_thread=lambda fn: started.append(fn))
        self.assertTrue(p.schedule("d42f"))
        self.assertEqual(audio.primed, [])
        started[0]()
        self.assertEqual(audio.primed, ["pad"])

    def test_nothing_escapes_schedule(self):
        c = Clock()

        def boom(fn):
            raise RuntimeError("no threads today")

        p = primer(FakeAudio(c, []), c, start_thread=boom)
        self.assertFalse(p.schedule())
        p2 = primer(FakeAudio(c, []), c,
                    enabled=lambda: (_ for _ in ()).throw(ValueError()))
        self.assertFalse(p2.schedule())

    def test_nothing_escapes_the_run(self):
        c = Clock()
        p = primer(FakeAudio(c, []), c, wait_s=1.0)
        p.run = lambda serial="": (_ for _ in ()).throw(RuntimeError("x"))
        p._run_safely("d42f")              # logged, not raised

    def test_real_thread_starter_runs_it(self):
        c = Clock()
        audio = FakeAudio(c, [(0.0, []), (0.5, [ep("pad", PAD1)])])
        p = P.AutoPrimer(audio=audio, sleep=c.sleep, clock=c)
        done = __import__("threading").Event()
        orig = p.run

        def run(serial=""):
            try:
                return orig(serial)
            finally:
                done.set()

        p.run = run
        self.assertTrue(p.schedule("d42f"))
        self.assertTrue(done.wait(5.0))
        self.assertEqual(audio.primed, ["pad"])


if __name__ == "__main__":
    unittest.main()
