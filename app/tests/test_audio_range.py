"""Dropping the volume ranges Windows' audio service kept from an older build.

`audio_range.EndpointVolumes` is the only part that talks to Windows; the
decision -- what counts as stale, when to restart the audio service, when to
only tell the user -- runs here over fakes, with a fake clock.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

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
        with mock.patch.object(AR.RangeRefresher, "run", lambda self: outcome), \
                mock.patch.object(AR, "ensure_running_now") as ensure, \
                mock.patch.object(AR, "default_marker"):
            got = AR.check_after_attach(lambda k, t: events.append((k, t)))
        ensure.assert_called_once()      # the safety net runs first, every attach
        return got, events

    @unittest.skipUnless(AR.sys.platform == "win32", "Windows only")
    def test_the_user_hears_about_it_only_when_it_matters(self):
        self.assertEqual(self.run_with(AR.OK), (AR.OK, []))
        got, ev = self.run_with(AR.NEED_ADMIN)
        self.assertEqual(got, AR.NEED_ADMIN)
        self.assertEqual(ev, [("warn", AR.ADVICE)])
        got, ev = self.run_with(AR.REFRESHED)
        self.assertEqual(ev[0][0], "info")
        # Restart already spent this boot: the user still gets the advice.
        self.assertEqual(self.run_with(AR.ALREADY), (AR.ALREADY, [("warn", AR.ADVICE)]))


class OncePerBoot(unittest.TestCase):
    """At most one restart per boot, across processes, through the marker."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = os.path.join(self._tmp.name, AR.MARKER_NAME)
        self.boot = 1_000_000.0          # fake boot time, wall clock seconds
        self.restarts = 0

    def attach(self, stale_after_restart=True):
        """One bridge process attaching: its own refresher, fresh marker
        object, the shared file. The range is stale before the restart and,
        unless told otherwise, after it too (an old build on another pad)."""
        audio = FakeAudio([STALE_MIC])
        clock = Clock()
        marker = AR.BootMarker(self.path, boot=lambda: self.boot)

        def restart():
            # The marker is written BEFORE the restart: a process killed
            # half-way still used up this boot's restart.
            self.assertTrue(marker.used())
            self.restarts += 1
            audio.replies = [[STALE_MIC]] if stale_after_restart else [[GOOD_MIC]]
            return True

        return AR.RangeRefresher(audio, restart, lambda: True, marker=marker,
                                 sleep=clock.sleep, clock=clock,
                                 watch_s=5, poll_s=1).run()

    def test_a_range_that_survives_the_restart_restarts_only_once(self):
        self.assertEqual(self.attach(), AR.FAILED)
        # every later attach this boot: tray launch, reconnect, respawn
        for _ in range(5):
            self.assertEqual(self.attach(), AR.ALREADY)
        self.assertEqual(self.restarts, 1)

    def test_a_good_restart_also_counts(self):
        self.assertEqual(self.attach(stale_after_restart=False), AR.REFRESHED)
        self.assertEqual(self.attach(), AR.ALREADY)
        self.assertEqual(self.restarts, 1)

    def test_clock_jitter_within_a_boot_is_the_same_boot(self):
        self.attach()
        self.boot += 30.0                # NTP nudged the wall clock
        self.assertEqual(self.attach(), AR.ALREADY)
        self.assertEqual(self.restarts, 1)

    def test_the_next_boot_gets_its_own_restart(self):
        self.attach()
        self.boot += 3600.0              # rebooted an hour later
        self.assertEqual(self.attach(), AR.FAILED)
        self.assertEqual(self.restarts, 2)

    def test_a_missing_or_garbled_marker_is_unused(self):
        m = AR.BootMarker(self.path, boot=lambda: self.boot)
        self.assertFalse(m.used())
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{not json")
        self.assertFalse(m.used())
        m.mark()
        self.assertTrue(m.used())

    def test_not_elevated_never_writes_the_marker(self):
        audio = FakeAudio([STALE_MIC])
        clock = Clock()
        marker = AR.BootMarker(self.path, boot=lambda: self.boot)
        r = AR.RangeRefresher(audio, lambda: True, lambda: False, marker=marker,
                              sleep=clock.sleep, clock=clock, watch_s=5, poll_s=1)
        self.assertEqual(r.run(), AR.NEED_ADMIN)
        self.assertFalse(os.path.exists(self.path))


class FakeSc:
    """A service as the SCM shows it. Transitions take `lag` state() polls."""

    def __init__(self, state=AR.SERVICE_RUNNING, dependents=(), stop_err=0,
                 start_err=0, lag=2):
        self.st = state
        self.dependents = list(dependents)
        self.stop_err = stop_err
        self.start_err = start_err
        self.lag = lag
        self._pending = None
        self.calls = []

    def state(self):
        if self._pending is not None:
            target, left = self._pending
            if left <= 0:
                self.st, self._pending = target, None
            else:
                self._pending = (target, left - 1)
        return self.st

    def stop(self):
        self.calls.append("stop")
        if self.stop_err:
            return self.stop_err
        if self.dependents:
            return AR.ERROR_DEPENDENT_SERVICES_RUNNING
        if self.st != AR.SERVICE_RUNNING:
            return AR.ERROR_SERVICE_NOT_ACTIVE
        self.st, self._pending = AR.SERVICE_STOP_PENDING, (AR.SERVICE_STOPPED, self.lag)
        return 0

    def start(self):
        self.calls.append("start")
        if self.start_err:
            return self.start_err
        if self.st == AR.SERVICE_RUNNING:
            return AR.ERROR_SERVICE_ALREADY_RUNNING
        self.st, self._pending = AR.SERVICE_START_PENDING, (AR.SERVICE_RUNNING, self.lag)
        return 0

    def active_dependents(self):
        return list(self.dependents)

    def stop_named(self, name):
        self.calls.append("stop:" + name)
        self.dependents.remove(name)
        return 0


class ServiceRestart(unittest.TestCase):
    def go(self, sc, fn=AR.restart_service, timeout=10.0):
        clock = Clock()
        return fn(sc, sleep=clock.sleep, clock=clock, timeout=timeout)

    def test_stop_wait_start_wait(self):
        sc = FakeSc()
        self.assertTrue(self.go(sc))
        self.assertEqual(sc.calls, ["stop", "start"])
        self.assertEqual(sc.st, AR.SERVICE_RUNNING)

    def test_running_dependents_are_stopped_first(self):
        sc = FakeSc(dependents=["AarSvc_1abe44"])
        self.assertTrue(self.go(sc))
        self.assertEqual(sc.calls, ["stop", "stop:AarSvc_1abe44", "stop", "start"])

    def test_a_refused_stop_changes_nothing(self):
        sc = FakeSc(stop_err=5)             # ERROR_ACCESS_DENIED
        self.assertFalse(self.go(sc))
        self.assertEqual(sc.calls, ["stop"])
        self.assertEqual(sc.st, AR.SERVICE_RUNNING)

    def test_a_slow_stop_still_ends_in_a_start(self):
        # STOPPED arrives after our timeout: the start is still attempted
        # (ensure_service_running waits for STOPPED first), never skipped.
        sc = FakeSc(lag=80)
        self.go(sc, timeout=5.0)
        self.assertIn("start", sc.calls)

    def test_a_failed_start_is_reported(self):
        sc = FakeSc(start_err=1058)          # ERROR_SERVICE_DISABLED
        self.assertFalse(self.go(sc))

    def test_ensure_starts_a_stopped_service_and_leaves_a_running_one(self):
        sc = FakeSc(state=AR.SERVICE_STOPPED)
        self.assertTrue(self.go(sc, AR.ensure_service_running))
        self.assertEqual(sc.calls, ["start"])
        sc = FakeSc()
        self.assertTrue(self.go(sc, AR.ensure_service_running))
        self.assertEqual(sc.calls, [])


class EnsureRunningAfterACrash(unittest.TestCase):
    """The tray / next bridge putting back a service a killed process left
    stopped -- but only when this boot's marker says it was us."""

    class Marker:
        def __init__(self, used):
            self._used = used

        def used(self):
            return self._used

    def run_with(self, state, used, admin=True):
        sc = FakeSc(state=state, lag=0)
        with mock.patch.object(AR, "_NamedMutex", lambda name: AR._NullLock()), \
                mock.patch.object(AR, "ensure_service_running",
                                  lambda s: s.start() == 0):
            got = AR.ensure_running_now(self.Marker(used), sc, lambda: admin)
        return got, sc.calls

    def test_stopped_by_our_half_done_restart_is_started(self):
        self.assertEqual(self.run_with(AR.SERVICE_STOPPED, True), (True, ["start"]))

    def test_stopped_by_somebody_else_is_left_alone(self):
        self.assertEqual(self.run_with(AR.SERVICE_STOPPED, False), (False, []))

    def test_running_is_left_alone(self):
        self.assertEqual(self.run_with(AR.SERVICE_RUNNING, True), (False, []))

    def test_not_elevated_cannot(self):
        self.assertEqual(self.run_with(AR.SERVICE_STOPPED, True, admin=False),
                         (False, []))


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

    def _start(self, flag):
        try:
            from ds5app import service as SVC
        except Exception:  # noqa: BLE001
            self.skipTest("the hardware stack is not importable here")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        saved = os.environ.get("DS5_CONFIG")
        os.environ["DS5_CONFIG"] = tmp.name

        def restore():
            if saved is None:
                os.environ.pop("DS5_CONFIG", None)
            else:
                os.environ["DS5_CONFIG"] = saved
        self.addCleanup(restore)

        class Pad:
            serial = "d42f4ba1485d"
            battery_percent = None
            battery_state = ""

            def describe(self):
                return self.serial

        class FakeU:
            exe = "usbip.exe"

            def __init__(self, *a, **kw):
                self.attached = False

            def version(self):
                return "test"

            def stop_auto_reattach(self):
                pass

            def our_ports(self):
                return [1] if self.attached else []

            def attach(self):
                self.attached = True

                class R:
                    ok = True
                    code = 0
                    out = ""
                return R()

        class Backend:
            stats = {"input_delivered": 0}

        s = SVC.BridgeService(serial=Pad.serial, port=39996,
                              check_volume_ranges=flag)
        self.addCleanup(s._lock_file.release)

        def fake_server():
            s._backend = Backend()

        s._start_server = fake_server
        with mock.patch.object(SVC, "Usbip", FakeU), \
                mock.patch.object(SVC.C, "select", lambda serial: Pad()), \
                mock.patch.object(SVC.C, "BatteryWatcher"), \
                mock.patch.object(AR, "check_in_background") as check:
            s._start_inner()
        return s, check

    def test_start_inner_runs_the_check_when_asked(self):
        s, check = self._start(True)
        check.assert_called_once_with(s._emit)

    def test_start_inner_skips_the_check_by_default(self):
        _, check = self._start(False)
        check.assert_not_called()


if __name__ == "__main__":
    unittest.main()
