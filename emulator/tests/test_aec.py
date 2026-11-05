"""The software echo canceller (`ds5emu.aec` + the native `ds5aec.dll`), offline.

Everything here runs the REAL DSP -- the built DLL -- on synthetic signals: a
reference, its delayed and filtered echo, and an independent near-end signal
that must survive. The canceller is driven synchronously on a simulated
clock with the timing the bridge gives it: mic packets of 10 ms with the
pad's sequence numbers, arriving some milliseconds late; loopback packets
stamped with their presentation time.

Skipped when numpy is missing or the DLL has not been built
(`python native\\build.py` from emulator/; CI builds it before the tests).
"""

from __future__ import annotations

import math
import os
import tempfile
import threading
import time
import unittest

try:
    import numpy as np

    from ds5emu import aec as AEC
    HAVE_NUMPY = True
except Exception:  # noqa: BLE001
    HAVE_NUMPY = False

HAVE_DLL = HAVE_NUMPY and os.path.isfile(AEC._dll_path())

FS = 48000
F = 480


def speechlike(n: int, seed: int, rate: float = 3.7, amp: float = 0.3):
    r = np.random.default_rng(seed)
    x = np.convolve(r.standard_normal(n), np.exp(-np.arange(40) / 6.0), "same")
    t = np.arange(n) / FS
    env = ((np.sin(2 * np.pi * rate * t + seed) > -0.2)
           * (0.5 + 0.5 * np.abs(np.sin(2 * np.pi * 0.7 * t + seed))))
    x = x * env
    return (x / np.max(np.abs(x)) * amp).astype(np.float32)


def room(seed: int, gain: float, n: int = 2400):
    r = np.random.default_rng(seed)
    h = r.standard_normal(n) * np.exp(-np.arange(n) / 400.0)
    return h / np.sqrt(np.sum(h * h)) * gain


def echo_of(src, h, delay_ms):
    e = np.convolve(src, h)[: src.size]
    d = int(delay_ms * FS / 1000)
    return np.concatenate([np.zeros(d), e])[: src.size].astype(np.float32)


def db(a, b=None):
    pa = float(np.mean(np.square(a))) + 1e-20
    if b is None:
        return 10 * np.log10(pa)
    return 10 * np.log10(pa / (float(np.mean(np.square(b))) + 1e-20))


def to16(x):
    """The bridge's own float -> int16 conversion (truncating)."""
    return (np.clip(x, -1.0, 1.0) * 32767.0).astype("<i2")


class Sim:
    """Drives an `EchoCanceller` on a simulated clock.

    `ref` is played on endpoint "Speakers (Test)" from t=0 (presentation
    stamps 12 ms ahead of delivery, as measured on a real endpoint); the mic
    signal's sample k is captured at k/FS and arrives `mic_latency_ms` later
    with up to `jitter_ms` more. `lose` = mic packet indices that never arrive.
    """

    def __init__(self, native_factory=None, mic_latency_ms=30.0, jitter_ms=6.0, **kw):
        self.now = 0.0
        self.out: list = []
        self.eng = AEC.EchoCanceller(
            lambda pcm: self.out.append(np.frombuffer(pcm, "<i2")[0::2].copy()),
            loopback=False, clock=lambda: self.now,
            native_factory=native_factory or AEC.NativeCanceller, **kw)
        self.eng.epoch = 0.0
        self.eng.prepare()
        self.lat = mic_latency_ms / 1000
        self.jit = jitter_ms / 1000
        self.rng = np.random.default_rng(3)
        self.seq = 0
        self.t0 = 0.0

    def run(self, mic, ref=None, lose=(), endpoint="Speakers (Test)"):
        """Returns the output (int16, one frame late: index i = input i - 480).
        `ref`: one endpoint's audio ((n,) or (n, 2)), or {endpoint: audio}."""
        start = len(self.out)
        n = (mic.size // F) * F
        pending = []
        refs = {} if ref is None else (ref if isinstance(ref, dict) else {endpoint: ref})
        for j in range(n // F):
            t_cap = self.t0 + (j + 1) * F / FS
            for ep, x in refs.items():
                self.now = t_cap - 0.012
                self.eng.push_render(ep, ep, x[j * F:(j + 1) * F], t_cap)
            if j not in lose:
                pending.append((t_cap + self.lat + self.rng.uniform(0, self.jit),
                                mic[j * F:(j + 1) * F], self.seq))
            self.seq = (self.seq + 1) % 256
            while pending and pending[0][0] <= t_cap + 0.012:
                t, blk, sq = pending.pop(0)
                self.now = t
                self.eng.feed_mic(blk, sq, t)
                self.eng.tick()
        for t, blk, sq in pending:
            self.now = t
            self.eng.feed_mic(blk, sq, t)
            self.eng.tick()
        self.t0 += n / FS + 1.0
        got = self.out[start:]
        return np.concatenate(got) if got else np.zeros(0, "<i2")


def aligned(out16, n):
    """Output as float, the one-frame delay removed, length n."""
    y = out16.astype(np.float64)[F:] / 32767.0
    return np.pad(y, (0, max(0, n - y.size)))[:n]


@unittest.skipUnless(HAVE_DLL, "numpy or ds5aec.dll missing (build: python native/build.py)")
class CancellerTests(unittest.TestCase):
    def test_a_silent_reference_is_a_bit_exact_pass_through(self):
        sim = Sim()
        mic = speechlike(3 * FS, 7)
        out = sim.run(mic, ref=np.zeros(3 * FS, np.float32))
        want = to16(mic)
        np.testing.assert_array_equal(out[F:F + want.size - F], want[:want.size - F])

    def test_a_loud_reference_the_mic_cannot_hear_changes_nothing(self):
        # Headphones: the PC plays loudly, none of it reaches the pad.
        sim = Sim()
        n = 12 * FS
        mic = speechlike(n, 7)
        out = sim.run(mic, ref=speechlike(n, 2, rate=5.1))
        self.assertEqual(sim.eng.status()["slots"], [])
        want = to16(mic)
        np.testing.assert_array_equal(out[F:F + want.size - F], want[:want.size - F])

    def test_an_echo_is_found_and_cancelled(self):
        sim = Sim()
        n = 14 * FS
        ref = speechlike(n, 2, rate=5.1)
        mic = echo_of(ref, room(6, 0.6), 40) + np.float32(1e-4) * \
            np.random.default_rng(1).standard_normal(n).astype(np.float32)
        out = aligned(sim.run(mic, ref=ref), n)
        st = sim.eng.status()
        self.assertEqual(st["slots"][0], "Speakers (Test)")
        steady = slice(8 * FS, 14 * FS - FS)
        reduction = db(out[steady], mic[steady])
        self.assertLess(reduction, -20.0, f"echo only reduced {reduction:.1f} dB")

    def test_an_echo_the_linear_filter_cannot_model_is_suppressed_to_the_room(self):
        # Round 10: a laptop speaker at full volume. Part of its echo is not a
        # linear function of the reference (driver excursion, processing the
        # loopback never sees), so the linear stage leaves it; the residual
        # stage must take it down to the room's noise -- and not below it
        # (the room keeps its sound; a canceller that gates the room pumps).
        n = 16 * FS
        ref = speechlike(n, 2, rate=5.1)
        lin = echo_of(ref, room(6, 0.6), 40)
        # the nonlinear part: a soft-clipped copy, the clipping varying in time
        drive = 3.0 + 2.0 * np.sin(2 * np.pi * 0.3 * np.arange(n) / FS)
        nl = (np.tanh(drive * lin * 4) / 4 - lin * drive / 4 * 0.9).astype(np.float32)
        noise = np.random.default_rng(1).standard_normal(n).astype(np.float32) * np.float32(1e-3)
        mic = lin + nl + noise

        def bypassed():
            nat = AEC.NativeCanceller()
            nat.set(bypass=1)
            return nat
        outs = {}
        for name, fac in (("full", None), ("linear", bypassed)):
            sim = Sim(native_factory=fac)
            outs[name] = aligned(sim.run(mic, ref=ref), n)
            self.assertEqual(sim.eng.status()["slots"], ["Speakers (Test)"])
        steady = slice(8 * FS, n - FS)
        left_lin = db(outs["linear"][steady], mic[steady])
        left = db(outs["full"][steady], mic[steady])
        self.assertGreater(left_lin, -25.0, f"the test's echo is too linear ({left_lin:.1f} dB)")
        # (9 dB, not 10: a residual the linear stage cannot model partly reads as near end to NER,
        # whose near-end mode then holds back; measured 9.7 dB with it, 11.5 without)
        self.assertLess(left, left_lin - 9.0,
                        f"residual stage took {left_lin:.1f} -> {left:.1f} dB only")

    def test_the_room_keeps_its_noise_while_an_echo_is_cancelled(self):
        # The residual stage suppresses down to the room's own noise level in
        # each bin, never below it: a room that goes quieter whenever the PC
        # plays, and comes back when it stops, pumps.
        n = 16 * FS
        ref = speechlike(n, 2, rate=5.1)
        noise = np.random.default_rng(1).standard_normal(n).astype(np.float32) * np.float32(3e-3)
        out = aligned(Sim().run(echo_of(ref, room(6, 0.6), 40) + noise, ref=ref), n)
        lost = [db(out[k:k + 4800], noise[k:k + 4800]) for k in range(8 * FS, n - FS, 4800)]
        # MEASURED (round 10): median -2.2 dB, worst -4.0 (the min-statistics
        # floor sits a little under the noise's mean)
        self.assertGreater(float(np.median(lost)), -3.5, f"room noise median {np.median(lost):.1f} dB")
        self.assertGreater(min(lost), -6.0, f"room noise worst 100 ms {min(lost):.1f} dB")

    def test_lost_packets_do_not_pull_the_room_noise_down(self):
        # Round-10 review (H1): lost Bluetooth packets arrive as exact zeros, and a noise
        # floor that follows every dip at once dragged the room 17 dB under itself for half
        # a minute. 200 ms lost at 10 s: the room must still be kept 20 s later.
        n = 34 * FS
        ref = speechlike(n, 2, rate=5.1)
        noise = np.random.default_rng(1).standard_normal(n).astype(np.float32) * np.float32(3e-3)
        out = aligned(Sim().run(echo_of(ref, room(6, 0.6), 40) + noise, ref=ref,
                                lose=set(range(1000, 1020))), n)
        lost = [db(out[k:k + 4800], noise[k:k + 4800]) for k in range(28 * FS, n - FS, 4800)]
        self.assertGreater(float(np.median(lost)), -3.5, f"room noise median {np.median(lost):.1f} dB")
        self.assertGreater(min(lost), -6.0, f"room noise worst 100 ms {min(lost):.1f} dB")

    def test_the_new_hooks_are_off_by_default(self):
        nat = AEC.NativeCanceller()
        for name, want in (("dt_adapt", 1.0), ("dt_beta", 1.0), ("dt_floor_db", -100.0), ("dt_force", 0.0),
                           ("gate_l_db", -100.0), ("bypass", 0.0)):
            self.assertEqual(nat.get(name), want, name)

    def test_bad_residual_parameters_are_refused(self):
        nat = AEC.NativeCanceller()
        for name, v in (("release", 1.5), ("q", 1.0), ("fsm", 20), ("beta", float("nan")),
                        ("dt_adapt", -0.1), ("hold", 99)):
            with self.assertRaises(ValueError, msg=name):
                nat.set(**{name: v})
        with self.assertRaises(ValueError):
            nat.set(nonsense=1)

    def test_the_free_learning_window_is_not_rearmed_on_return_from_idle(self):
        # Round-10 review (M2): the 1.5 s of unconditional learning restarted every time the
        # PC resumed after 220 ms of quiet, so a local talker was learned as echo each time.
        # A voice in the room that starts as the PC resumes must not be cut more with the
        # window than without it.
        def run(fresh):
            def fac():
                c = AEC.NativeCanceller()
                c.set(fresh=fresh)
                return c
            sim = Sim(native_factory=fac)
            n = 24 * FS
            ref = speechlike(n, 2, rate=5.1)
            ref[12 * FS:13 * FS] = 0                       # the remote side pauses for 1 s
            near = speechlike(n, 9, rate=2.3, amp=0.2)
            near[:13 * FS] = 0                             # ... and the local talker starts as it resumes
            out = aligned(sim.run(echo_of(ref, room(6, 0.6), 40) + near, ref=ref), n)
            talk = slice(13 * FS, 15 * FS)
            return db(out[talk]) - db(near[talk])
        self.assertGreater(run(150) - run(0), -1.0)

    def test_a_quiet_echo_of_a_loud_reference_is_found_and_cancelled(self):
        # Round 9 (user report: laptop speakers still heard in the pad mic).
        # The loopback reference is taken before the endpoint volume, so a
        # speaker turned down, or a pad mic with little gain, gives an echo
        # 50 dB under its reference: here ~-77 dBFS in the mic with room noise
        # 10 dB under it. The probe skipped every mic window under -60 dBFS,
        # so this was never probed, never slotted, never cancelled -- while
        # the status said "echo on".
        sim = Sim()
        n = 16 * FS
        ref = speechlike(n, 2, rate=5.1)
        echo = echo_of(ref, room(6, 0.003), 40)
        noise = np.random.default_rng(1).standard_normal(n).astype(np.float32)
        mic = echo + noise * np.float32(0.3 * np.sqrt(np.mean(echo ** 2)))
        self.assertLess(db(mic), -70.0)
        out = aligned(sim.run(mic, ref=ref), n)
        self.assertEqual(sim.eng.status()["slots"], ["Speakers (Test)"])
        steady = slice(8 * FS, n - FS)
        # the noise is 10.5 dB under the mic: most of that must go
        reduction = db(out[steady], mic[steady])
        self.assertLess(reduction, -7.0, f"quiet echo only reduced {reduction:.1f} dB")

    def test_a_silent_mic_is_never_probed(self):
        # A muted pad sends digital silence: nothing to look for an echo in.
        sim = Sim()
        n = 6 * FS
        sim.run(np.zeros(n, np.float32), ref=speechlike(n, 2, rate=5.1))
        self.assertEqual(sim.eng.stats["probes"], 0)
        self.assertEqual(sim.eng.status()["slots"], [])

    def test_the_near_end_survives_double_talk(self):
        sim = Sim()
        n = 16 * FS
        ref = speechlike(n, 2, rate=5.1)
        near = speechlike(n, 9, rate=2.3, amp=0.2)
        near[: 9 * FS] = 0
        gains = []
        orig = AEC.NativeCanceller.process

        def process(self, mic16, stages, refs, active):
            if self.gains is None:
                self.gains = np.ones(481, np.float32)
            out = orig(self, mic16, stages, refs, active)
            gains.append(self.gains.copy())
            return out

        AEC.NativeCanceller.process = process
        try:
            sim.run(echo_of(ref, room(6, 0.6), 40) + near, ref=ref)
        finally:
            AEC.NativeCanceller.process = orig
        # the residual stage's gains, applied to the near end alone, through
        # the same STFT framing the native code uses
        win = np.sqrt(0.5 - 0.5 * np.cos(2 * np.pi * np.arange(2 * F) / (2 * F)))
        g = np.array(gains)
        prev = np.zeros(F); ola = np.zeros(F); thru = np.zeros(g.shape[0] * F)
        for j in range(g.shape[0]):
            cur = near[j * F:(j + 1) * F].astype(np.float64)
            if cur.size < F:
                cur = np.pad(cur, (0, F - cur.size))
            t = np.fft.irfft(np.fft.rfft(np.concatenate([prev, cur]) * win) * g[j], 2 * F) * win
            thru[j * F:(j + 1) * F] = ola + t[:F]; ola = t[F:]; prev = cur
        thru = thru[F:]
        talk = slice(10 * FS, 15 * FS)
        whole = db(thru[talk], near[talk])
        self.assertGreater(whole, -1.5, f"near end lost {-whole:.1f} dB in double talk")
        worst = min(db(thru[k:k + 4800], near[k:k + 4800])
                    for k in range(10 * FS, 15 * FS, 4800) if db(near[k:k + 4800]) > -50)
        self.assertGreater(worst, -6.0, f"worst 100 ms window {worst:.1f} dB")

    def test_the_output_is_never_louder_than_the_input(self):
        sim = Sim()
        n = 12 * FS
        ref = speechlike(n, 2, rate=5.1)
        mic = echo_of(ref, room(6, 0.6), 40) + speechlike(n, 9, rate=2.3, amp=0.2)
        out = aligned(sim.run(mic, ref=ref), n)
        for k in range(FS, n - FS, 4800):
            self.assertLessEqual(db(out[k:k + 4800], mic[k:k + 4800]), 0.5)

    def test_lost_packets_are_filled_and_do_not_shift_the_echo(self):
        sim = Sim()
        n = 16 * FS
        ref = speechlike(n, 2, rate=5.1)
        mic = echo_of(ref, room(6, 0.6), 40)
        lose = {1000, 1001, 1002, 1200}
        out = sim.run(mic, ref=ref, lose=lose)
        # every lost packet became exactly one frame of output
        self.assertEqual(out.size, (n // F) * F)
        self.assertEqual(sim.eng.stats["lost_packets"], 4)
        y = aligned(out, n)
        after = slice(13 * FS, 15 * FS)
        self.assertLess(db(y[after], mic[after]), -20.0)

    def test_a_new_capture_session_keeps_the_delay_and_the_filter(self):
        sim = Sim()
        n = 10 * FS
        ref = speechlike(n, 2, rate=5.1)
        mic = echo_of(ref, room(6, 0.6), 40)
        sim.run(mic, ref=ref)
        delay = sim.eng._refs["Speakers (Test)"].delay
        self.assertIsNotNone(delay)
        stage = sim.eng._refs["Speakers (Test)"].stage
        sim.eng.pause(); sim.eng._new_session(); sim.eng.resume()
        out = aligned(sim.run(mic[:4 * FS], ref=ref[:4 * FS]), 4 * FS)
        self.assertEqual(sim.eng._refs["Speakers (Test)"].delay, delay)
        # the same filter, still converged, re-aligned to the new session's
        # mic timing within half a second: the first sentence is cancelled
        self.assertIs(sim.eng._refs["Speakers (Test)"].stage, stage)
        self.assertGreaterEqual(sim.eng.stats["fine_moves"], 1)
        first = slice(int(0.6 * FS), int(2.0 * FS))
        self.assertLess(db(out[first], mic[first]), -30.0)

    def test_an_echo_under_louder_room_sound_keeps_its_slot(self):
        # Music in the room (the Echo Dot), 12 dB above the TV's echo: the
        # linear stage removes well under 1 dB of the mic, but the echo is
        # still there and must stay cancelled (live 2026-10-05: the HDMI
        # reference was dropped after 20 s of this).
        sim = Sim()
        n = 45 * FS
        ref = speechlike(n, 2, rate=5.1)
        echo = echo_of(ref, room(6, 0.6), 40)
        t = np.arange(n) / FS
        r = np.random.default_rng(11)
        music = np.zeros(n)
        for k, f0 in enumerate((220.0, 277.2, 329.6, 110.0)):
            swell = 0.6 + 0.4 * np.sin(2 * np.pi * 0.3 * (k + 1) * t)
            for h in range(1, 9):
                music += np.sin(2 * np.pi * f0 * h * t + r.uniform(0, 6.3)) / h * swell
        music = (music / np.sqrt(np.mean(music ** 2))
                 * np.sqrt(np.mean(echo ** 2)) * 10 ** (12 / 20)).astype(np.float32)
        sim.run(echo + music, ref=ref)
        self.assertEqual(sim.eng.status()["slots"][0], "Speakers (Test)")
        self.assertEqual(sim.eng.stats["unslotted"], 0)

    def test_an_echo_that_goes_away_loses_its_slot(self):
        # Headphones plugged into the TV: the PC still plays, the room is quiet
        # but for a voice, and the echo is gone.
        sim = Sim()
        n = 50 * FS
        ref = speechlike(n, 2, rate=5.1)
        mic = speechlike(n, 9, rate=2.3, amp=0.1)
        mic[: 8 * FS] += echo_of(ref, room(6, 0.6), 40)[: 8 * FS]
        sim.run(mic, ref=ref)
        self.assertEqual(sim.eng.stats["unslotted"], 1)
        self.assertEqual(sim.eng.status()["slots"], [])

    def test_slow_analysis_does_not_hold_frames_back(self):
        # Round 10: the delay probe and the fine alignment ran between frames on the
        # worker; a slow one delayed the mic (bridge ring dropouts, raw frames past
        # QUEUE_MAX_MS). They run on their own thread now: a 300 ms analysis step must
        # not delay a frame.
        got = []
        eng = AEC.EchoCanceller(lambda pcm: got.append(time.perf_counter()), loopback=False)
        orig = AEC.EchoCanceller.tick

        def slow_tick(self):
            time.sleep(0.3)
            orig(self)
        eng.tick = slow_tick.__get__(eng)
        eng.start()
        try:
            deadline = time.time() + 5
            while eng.mode != "aec" and time.time() < deadline:
                time.sleep(0.01)
            lat = []
            for j in range(60):
                t = time.perf_counter()
                n0 = len(got)
                self.assertTrue(eng.push_mic(np.zeros(F, np.float32), j % 256, t))
                while len(got) == n0 and time.perf_counter() - t < 1.0:
                    time.sleep(0.001)
                lat.append(time.perf_counter() - t)
                time.sleep(0.01)
            self.assertLess(max(lat), 0.1, f"a frame waited {max(lat) * 1000:.0f} ms")
        finally:
            eng.stop()
        self.assertIsNone(getattr(eng, "_analysis", None))

    def test_a_playback_whose_stamps_jumped_is_cancelled_within_seconds(self):
        # Round 10, live: between two playbacks the Realtek loopback's stamps moved by
        # -70.6 / +59.2 / +60.7 ms against the mic. An echo that moved EARLIER than the
        # filter's window cannot be re-adapted to: the tracker wanted two probes 5 s apart
        # and then reset the filter, so the first 5-8 s of a new playback leaked. Now a
        # re-anchored stream is probed every window, its kept filter runs frozen meanwhile,
        # and the move keeps the weights.
        sim = Sim()
        n = 14 * FS
        ref = speechlike(n, 2, rate=5.1)
        h = room(6, 0.6)
        sim.run(echo_of(ref, h, 100), ref=ref)              # learn the path
        self.assertEqual(sim.eng.status()["slots"], ["Speakers (Test)"])
        ref2 = speechlike(n, 4, rate=5.1)
        mic2 = echo_of(ref2, h, 40)                          # the same path, 60 ms earlier vs the stamps
        out = aligned(sim.run(mic2, ref=ref2), n)
        span = slice(2 * FS, 10 * FS)
        red = db(out[span], mic2[span])
        # MEASURED: -20 dB; the old tracker -2 (nothing cancelled until 6.5 s, then a reset)
        self.assertLess(red, -8.0, f"2-10 s into the new playback: {red:.1f} dB")

    def test_a_stalled_worker_hands_the_mic_back(self):
        block = threading.Event()

        class Slow(AEC.NativeCanceller):
            def process(self, mic16, stages, refs, active):
                block.wait(5.0)
                return super().process(mic16, stages, refs, active)

        got = []
        eng = AEC.EchoCanceller(got.append, loopback=False, native_factory=Slow)
        eng.start()
        try:
            deadline = time.monotonic() + 5
            while eng.mode != "aec" and time.monotonic() < deadline:
                time.sleep(0.01)
            x = np.zeros(F, np.float32)
            taken = refused = 0
            for i in range(40):
                if eng.push_mic(x, i % 256):
                    taken += 1
                else:
                    refused += 1
                time.sleep(0.01)
            self.assertGreater(refused, 20)            # the bridge writes those
            self.assertLessEqual(taken, AEC.QUEUE_MAX_MS // 10 + 2)
            block.set()
            deadline = time.monotonic() + 5
            while eng._overrun and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(eng.push_mic(x, 41))       # back in business
        finally:
            block.set()
            eng.stop()

    def test_a_stereo_speaker_is_cancelled_per_channel(self):
        # different content left and right through different paths: one mono
        # reference cannot model it (live: -2.8 dB), two channels can
        sim = Sim()
        n = 16 * FS
        L, R = speechlike(n, 2, rate=5.1), speechlike(n, 3, rate=4.3)
        mic = echo_of(L, room(6, 0.5), 40) + echo_of(R, room(8, 0.5), 44)
        out = aligned(sim.run(mic, ref=np.stack([L, R], 1)), n)
        ref = sim.eng._refs["Speakers (Test)"]
        self.assertEqual(ref.nch, 2)
        self.assertEqual(ref.stage.nch, 2)
        steady = slice(10 * FS, 15 * FS)
        self.assertLess(db(out[steady], mic[steady]), -25.0)

    def test_a_speaker_that_plays_one_channel_of_a_stereo_endpoint_is_found(self):
        # MEASURED round 8: a DualSense on a cable plays only the RIGHT channel
        # of its stereo endpoint. With different content left and right, the
        # probe's L-weighted mix halved the correlation peak and two 18 s takes
        # of stereo speech were never cancelled from a cold start.
        sim = Sim()
        n = 18 * FS
        L, R = speechlike(n, 2, rate=5.1), speechlike(n, 3, rate=4.3)
        noise = np.random.default_rng(5).standard_normal(n).astype(np.float32)
        er = echo_of(R, room(8, 0.5), 40)
        mic = er + noise * np.float32(0.5 * np.sqrt(np.mean(er ** 2)))
        out = aligned(sim.run(mic, ref=np.stack([L, R], 1)), n)
        self.assertEqual(sim.eng.status()["slots"], ["Speakers (Test)"])
        self.assertEqual(AEC.VIEW_NAMES[sim.eng._refs["Speakers (Test)"].view], "right")
        steady = slice(10 * FS, 17 * FS)
        # the room noise is 6 dB under the echo, i.e. 7 dB under the mic: at
        # least 5 of those 7 dB must go (what is left is the noise, not the echo)
        self.assertLess(db(out[steady], mic[steady]), -5.0)

    def test_a_speaker_that_plays_both_channels_stays_on_the_mix(self):
        # One speaker that plays both channels of L != R content (a single
        # driver fed L + R): neither channel alone beats the mix by
        # PROBE_VIEW_MARGIN, so the arrival pattern is taken on the mix.
        sim = Sim()
        n = 16 * FS
        L, R = speechlike(n, 2, rate=5.1), speechlike(n, 3, rate=4.3)
        noise = np.random.default_rng(7).standard_normal(n).astype(np.float32)
        e = echo_of(0.5 * (L + R), room(6, 0.5), 40)
        mic = e + noise * np.float32(0.3 * np.sqrt(np.mean(e ** 2)))
        out = aligned(sim.run(mic, ref=np.stack([L, R], 1)), n)
        r = sim.eng._refs["Speakers (Test)"]
        self.assertEqual(sim.eng.status()["slots"], ["Speakers (Test)"])
        self.assertEqual(AEC.VIEW_NAMES[r.view], "mix")
        self.assertEqual(r.describe()["view"], "mix")
        steady = slice(10 * FS, 15 * FS)
        self.assertLess(db(out[steady], mic[steady]), -9.0)    # the room noise: -10.5

    def test_a_speaker_that_plays_only_the_left_channel_is_found(self):
        # The mix weights left 1.0 : right 0.67, so a left-only speaker's
        # left / mix ratio is only ~1.15: it mostly stays on the mix (which
        # costs little) and may move to left by noise. Never to right.
        sim = Sim()
        n = 18 * FS
        L, R = speechlike(n, 2, rate=5.1), speechlike(n, 3, rate=4.3)
        noise = np.random.default_rng(9).standard_normal(n).astype(np.float32)
        el = echo_of(L, room(6, 0.5), 40)
        mic = el + noise * np.float32(0.5 * np.sqrt(np.mean(el ** 2)))
        out = aligned(sim.run(mic, ref=np.stack([L, R], 1)), n)
        r = sim.eng._refs["Speakers (Test)"]
        self.assertEqual(sim.eng.status()["slots"], ["Speakers (Test)"])
        self.assertIn(AEC.VIEW_NAMES[r.view], ("mix", "left"))
        steady = slice(10 * FS, 17 * FS)
        # as for the right-only speaker: the room noise is 7 dB under the mic
        self.assertLess(db(out[steady], mic[steady]), -5.0)

    @staticmethod
    def _one_channel_take(sim, seed, mono, noise, n=14 * FS, shift_ms=None):
        """A speaker that plays only the RIGHT channel of its stereo endpoint,
        with mono content (L == R) or stereo (L != R), room noise 10 dB under
        its echo. With `shift_ms`, in a new capture session whose mic anchor
        is that far off the last one's. Returns (mic, output)."""
        if shift_ms is not None:
            sim.eng.pause(); sim.eng._new_session(); sim.eng.resume()
            sim.lat += shift_ms / 1000
        L = speechlike(n, seed, rate=5.1)
        R = L if mono else speechlike(n, seed + 1, rate=4.3)
        e = echo_of(R, room(8, 0.5), 40)
        mic = e + noise.standard_normal(n).astype(np.float32) * np.float32(0.3 * np.sqrt(np.mean(e ** 2)))
        return mic, aligned(sim.run(mic, ref=np.stack([L, R], 1)), n)

    def test_mono_content_on_a_one_channel_speaker_does_not_pick_a_channel_by_noise(self):
        # Review of round 8: with L == R the three probe views are one signal,
        # and the plain best peak picked mix / left / right by rounding noise.
        # A wired pad (right channel only) slotted on mono content with the
        # LEFT view kept it: once the content turned stereo its arrival
        # pattern was taken on a channel the speaker does not play, every
        # session-start re-alignment failed, and only an unslot reset it.
        sim = Sim()
        sim.rng = np.random.default_rng(4)      # a jitter on which the old probe took "left"
        noise = np.random.default_rng(104)
        self._one_channel_take(sim, 2, True, noise, n=8 * FS)
        r = sim.eng._refs["Speakers (Test)"]
        self.assertTrue(r.slotted)
        self.assertEqual(AEC.VIEW_NAMES[r.view], "mix")
        moves = sim.eng.stats["fine_moves"]
        mic, out = self._one_channel_take(sim, 12, False, noise, shift_ms=4)
        # the slotted probe moved it to the channel the speaker plays ...
        self.assertEqual(AEC.VIEW_NAMES[r.view], "right")
        self.assertEqual(sim.eng.stats["unslotted"], 0)
        # ... and the new session was re-aligned (on the mix: before the fix,
        # on the left channel, it was not: -1.4 and -5.4 dB)
        self.assertGreaterEqual(sim.eng.stats["fine_moves"], moves + 1)
        first, steady = slice(FS // 2, 3 * FS), slice(3 * FS, 13 * FS)
        self.assertLess(db(out[first], mic[first]), -5.0)
        self.assertLess(db(out[steady], mic[steady]), -9.0)    # the room noise: -10.8

    def test_a_stale_view_corrects_itself_while_slotted(self):
        # However a slotted endpoint came to align on a channel its speaker
        # does not play (an old store entry, a channel that went quiet), the
        # probes move it to the right one without an unslot; from the next
        # session on the session-start re-alignment works again.
        sim = Sim()
        noise = np.random.default_rng(101)
        self._one_channel_take(sim, 2, True, noise, n=8 * FS)
        r = sim.eng._refs["Speakers (Test)"]
        self.assertTrue(r.slotted)
        r.view = AEC.VIEW_NAMES.index("left")
        self._one_channel_take(sim, 12, False, noise, shift_ms=4)
        self.assertEqual(AEC.VIEW_NAMES[r.view], "right")
        self.assertGreaterEqual(sim.eng.stats["view_changes"], 1)
        self.assertEqual(sim.eng.stats["unslotted"], 0)
        moves = sim.eng.stats["fine_moves"]
        mic, out = self._one_channel_take(sim, 22, False, noise, shift_ms=-3)
        self.assertGreaterEqual(sim.eng.stats["fine_moves"], moves + 1)
        first = slice(FS // 2, 3 * FS)
        self.assertLess(db(out[first], mic[first]), -8.0)      # left kept: -3.3 dB

    def test_the_view_is_remembered_with_the_delay(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "echo-delays.json")
            store = AEC.DelayStore(path)
            store.put("pad1|A", 50.0, AEC.VIEW_NAMES.index("right"))
            store.put("pad1|B", 60.0)
            again = AEC.DelayStore(path)
            self.assertEqual(again.get("pad1|A"), 50.0)
            self.assertEqual(AEC.VIEW_NAMES[again.view("pad1|A")], "right")
            self.assertEqual(again.view("pad1|B"), 0)
            # an entry written before views were stored, and one with a view
            # this build does not know, still load: on the mix
            with open(path, "w", encoding="utf-8") as f:
                f.write('{"pad1|A": {"delay_ms": 40.0, "t": 1}, '
                        '"pad1|B": {"delay_ms": 41.0, "view": "centre", "t": 1}}')
            old = AEC.DelayStore(path)
            self.assertEqual(old.get("pad1|A"), 40.0)
            self.assertEqual(old.view("pad1|A"), 0)
            self.assertEqual(old.get("pad1|B"), 41.0)
            self.assertEqual(old.view("pad1|B"), 0)
            self.assertEqual(old.view("pad1|C"), 0)
            # a one-channel speaker found on its channel is slotted on it again
            # after a restart, from its first sound
            first = Sim(delay_store=AEC.DelayStore(path), pad_id="pad1")
            self._one_channel_take(first, 2, False, np.random.default_rng(5), n=8 * FS)
            self.assertEqual(AEC.VIEW_NAMES[AEC.DelayStore(path).view("pad1|Speakers (Test)")], "right")
            second = Sim(delay_store=AEC.DelayStore(path), pad_id="pad1")
            n = FS // 2
            L, R = speechlike(n, 2, rate=5.1), speechlike(n, 3, rate=4.3)
            second.run(echo_of(R, room(8, 0.5), 40), ref=np.stack([L, R], 1))
            self.assertEqual(second.eng.stats["slotted_from_store"], 1)
            self.assertEqual(AEC.VIEW_NAMES[second.eng._refs["Speakers (Test)"].view], "right")

    def test_a_stereo_reference_the_mic_cannot_hear_is_not_slotted(self):
        # the probe takes the best of three views (mix, left, right): unrelated
        # audio on either channel must still never qualify -- headphones, with
        # someone talking in the room
        sim = Sim()
        n = 40 * FS
        L, R = speechlike(n, 2, rate=5.1), speechlike(n, 3, rate=4.3)
        mic = speechlike(n, 9, rate=2.3, amp=0.2) + np.float32(1e-3) * \
            np.random.default_rng(4).standard_normal(n).astype(np.float32)
        out = sim.run(mic, ref=np.stack([L, R], 1))
        self.assertEqual(sim.eng.status()["slots"], [])
        self.assertEqual(sim.eng.stats["pair_slotted"], 0)
        want = to16(mic)
        np.testing.assert_array_equal(out[F:F + want.size - F], want[:want.size - F])

    def test_a_second_speaker_does_not_disturb_the_first(self):
        sim = Sim()
        n = 26 * FS
        a, b = speechlike(n, 2, rate=5.1), speechlike(n, 3, rate=4.3)
        b[: 12 * FS] = 0
        ea, eb = echo_of(a, room(6, 0.5), 40), echo_of(b, room(8, 0.5), 90)
        mic = ea + eb
        stages = []
        orig = AEC.EchoCanceller._slot

        def slot(eng, ref, provisional=False):
            ok = orig(eng, ref, provisional)
            stages.append((ref.name, ref.stage))
            return ok

        AEC.EchoCanceller._slot = slot
        try:
            out = aligned(sim.run(mic, ref={"TV": a, "Desk": b}), n)
        finally:
            AEC.EchoCanceller._slot = orig
        self.assertEqual([s[0] for s in stages], ["TV", "Desk"])
        self.assertEqual(sim.eng.status()["slots"], ["TV", "Desk"])
        # the TV keeps the filter it started with
        self.assertIs(sim.eng._refs["TV"].stage, stages[0][1])
        self.assertLess(db(out[8 * FS:12 * FS], mic[8 * FS:12 * FS]), -25.0)
        # while the desk speaker is found and converges, what is left is no
        # more than the desk's own echo: the TV stays cancelled
        during = slice(12 * FS, 14 * FS)
        self.assertLess(db(out[during], eb[during]), 0.5)
        self.assertLess(db(out[20 * FS:25 * FS], mic[20 * FS:25 * FS]), -25.0)

    def test_a_dead_speaker_is_dropped_while_a_live_one_stays(self):
        # two slotted endpoints; one's echo stops (its speaker unplugged) while
        # both keep playing: only that one loses its slot
        sim = Sim()
        n = 50 * FS
        a, b = speechlike(n, 2, rate=5.1), speechlike(n, 3, rate=4.3)
        eb = echo_of(b, room(8, 0.5), 90)
        eb[10 * FS:] = 0
        mic = echo_of(a, room(6, 0.5), 40) + eb
        sim.run(mic, ref={"TV": a, "Desk": b})
        self.assertEqual(sim.eng.status()["slots"], ["TV"])
        self.assertEqual(sim.eng.stats["unslotted"], 1)

    def test_a_remembered_delay_cancels_from_the_first_sound(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "echo-delays.json")
            n = 10 * FS
            ref = speechlike(n, 2, rate=5.1)
            mic = echo_of(ref, room(6, 0.6), 40)
            first = Sim(delay_store=AEC.DelayStore(path), pad_id="pad1")
            out1 = aligned(first.run(mic, ref=ref), n)
            self.assertIsNotNone(AEC.DelayStore(path).get("pad1|Speakers (Test)"))
            # a new bridge run: no probe needed
            second = Sim(delay_store=AEC.DelayStore(path), pad_id="pad1")
            out2 = aligned(second.run(mic, ref=ref), n)
            self.assertEqual(second.eng.stats["slotted_from_store"], 1)
            self.assertEqual(second.eng.stats["store_rejected"], 0)
            early = slice(int(0.5 * FS), int(2.0 * FS))
            self.assertLess(db(out2[early], mic[early]), db(out1[early], mic[early]) - 6)
            self.assertLess(db(out2[early], mic[early]), -10.0)
            # another pad has its own delays
            third = Sim(delay_store=AEC.DelayStore(path), pad_id="pad2")
            third.run(mic[:2 * FS], ref=ref[:2 * FS])
            self.assertEqual(third.eng.stats["slotted_from_store"], 0)

    def test_a_wrong_remembered_delay_is_corrected(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "echo-delays.json")
            AEC.DelayStore(path).put("pad1|Speakers (Test)", 400.0)    # far off
            n = 12 * FS
            ref = speechlike(n, 2, rate=5.1)
            mic = echo_of(ref, room(6, 0.6), 40)
            sim = Sim(delay_store=AEC.DelayStore(path), pad_id="pad1")
            out = aligned(sim.run(mic, ref=ref), n)
            self.assertEqual(sim.eng.stats["slotted_from_store"], 1)
            # the probe finds the echo elsewhere and moves the read point (or,
            # failing that, the remembered delay is forgotten and re-probed)
            self.assertEqual(sim.eng.stats["store_rejected"] + sim.eng.stats["delay_moves"], 1)
            self.assertEqual(sim.eng.status()["slots"], ["Speakers (Test)"])
            self.assertLess(AEC.DelayStore(path).get("pad1|Speakers (Test)"), 200.0)
            # corrected within ~10 s of a wrong remembered delay
            self.assertLess(db(out[8 * FS:10 * FS], mic[8 * FS:10 * FS]), -20.0)

    def test_a_remembered_speaker_that_is_not_heard_is_dropped(self):
        # headphones on today: the remembered delay is tried, cancels
        # nothing, and its slot is dropped without touching the voice for
        # long. The store keeps it (round 7: not being heard is no proof it
        # is wrong -- a masked echo looks the same); only a probe finding the
        # echo elsewhere replaces it.
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "echo-delays.json")
            AEC.DelayStore(path).put("pad1|Speakers (Test)", 50.0)
            n = 10 * FS
            ref = speechlike(n, 2, rate=5.1)
            voice = speechlike(n, 9, rate=2.3, amp=0.1)
            sim = Sim(delay_store=AEC.DelayStore(path), pad_id="pad1")
            out = aligned(sim.run(voice, ref=ref), n)
            self.assertEqual(sim.eng.stats["slotted_from_store"], 1)
            self.assertEqual(sim.eng.stats["store_rejected"], 1)
            self.assertEqual(sim.eng.status()["slots"], [])
            self.assertEqual(AEC.DelayStore(path).get("pad1|Speakers (Test)"), 50.0)
            self.assertGreater(db(out[: 4 * FS], voice[: 4 * FS]), -1.0)
            after = slice(5 * FS, 9 * FS)
            self.assertAlmostEqual(db(out[after], voice[after]), 0.0, places=1)

    def test_after_an_overrun_no_worker_frame_follows_a_direct_one(self):
        block = threading.Event()
        log = []

        class Slow(AEC.NativeCanceller):
            def process(self, mic16, stages, refs, active):
                block.wait(5.0)
                return super().process(mic16, stages, refs, active)

        eng = AEC.EchoCanceller(lambda b: log.append("w"), loopback=False, native_factory=Slow)
        eng.start()
        try:
            deadline = time.monotonic() + 5
            while eng.mode != "aec" and time.monotonic() < deadline:
                time.sleep(0.01)
            x = np.zeros(F, np.float32)
            for i in range(40):                  # the worker is stuck on frame 0
                if not eng.push_mic(x, i % 256):
                    log.append("d")              # the bridge writes this one
                time.sleep(0.005)
            self.assertIn("d", log)
            block.set()                          # the worker comes back
            deadline = time.monotonic() + 5
            while eng._overrun and time.monotonic() < deadline:
                time.sleep(0.01)
            time.sleep(0.1)
            # the frame the worker was in the middle of may land, but before the
            # first direct write; nothing from the old queue after it
            self.assertNotIn("w", log[log.index("d"):])
            self.assertTrue(eng.push_mic(x, 41))
        finally:
            block.set()
            eng.stop()

    def test_a_pause_and_resume_faster_than_the_worker_is_a_new_session(self):
        eng = AEC.EchoCanceller(lambda b: None, loopback=False)
        eng.start()
        try:
            deadline = time.monotonic() + 5
            while eng.mode != "aec" and time.monotonic() < deadline:
                time.sleep(0.01)
            x = np.zeros(F, np.float32)
            for i in range(5):
                eng.push_mic(x, i)
            time.sleep(0.2)
            before = eng.stats["sessions"]
            eng.pause(); eng.resume()            # no time for the worker to look
            eng.push_mic(x, 200)                 # a new stream, new numbering
            deadline = time.monotonic() + 2
            while eng.stats["frames"] < 6 and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(eng.stats["sessions"], before + 1)
            self.assertEqual(eng.stats["lost_packets"], 0)
        finally:
            eng.stop()

    def test_the_native_core_refuses_bad_arguments(self):
        d = AEC.load_native()
        nat = AEC.NativeCanceller()
        out = np.zeros(F, "<i2")
        mic = np.zeros(F, "<i2")
        self.assertEqual(d.ds5aec_process(nat._h, None, 0, None, None, out.ctypes.data,
                                          1, None, None, None), -1)
        self.assertEqual(d.ds5aec_process(nat._h, mic.ctypes.data, 1, None, None,
                                          out.ctypes.data, 1, None, None, None), -1)
        self.assertEqual(d.ds5aec_process(None, mic.ctypes.data, 0, None, None,
                                          out.ctypes.data, 1, None, None, None), -1)

    def test_the_resampler_is_clean(self):
        rs = AEC.NativeResampler(2, 44100)
        t = np.arange(44100 * 2) / 44100
        x = (np.stack([np.sin(2 * np.pi * 1000 * t), np.sin(2 * np.pi * 7000 * t)], 1)
             .astype(np.float32) * 0.5)
        y = np.concatenate([rs.push(x[i:i + 441]) for i in range(0, x.shape[0], 441)])
        self.assertAlmostEqual(y.shape[0] / 2.0, 48000, delta=200)
        seg = y[24000:72000]
        for ch, f in ((0, 1000), (1, 7000)):
            s = seg[:, ch].astype(np.float64)
            tt = np.arange(s.size) / FS
            basis = np.stack([np.sin(2 * np.pi * f * tt), np.cos(2 * np.pi * f * tt)], 1)
            coef = np.linalg.lstsq(basis, s, rcond=None)[0]
            self.assertLess(db(s - basis @ coef, s), -60.0, f"{f} Hz: resampling error")

    def _drifting(self, ppm, seconds):
        n = seconds * FS
        ref = speechlike(n, 2, rate=5.1)
        # the speaker's clock runs `ppm` fast against the mic's
        played = np.interp(np.arange(n) * (1 + ppm * 1e-6), np.arange(n), ref).astype(np.float32)
        mic = echo_of(played, room(6, 0.6), 40) + np.float32(1e-4) * \
            np.random.default_rng(1).standard_normal(n).astype(np.float32)
        return ref, mic

    def test_a_drifting_speaker_is_followed_without_resets(self):
        # A Bluetooth speaker or USB DAC: 50 and 100 ppm against the mic over
        # three minutes. The read point is steered at the fitted rate; the
        # filter is never reset by a delay move, and the cancellation holds.
        for ppm in (50.0, 100.0):
            with self.subTest(ppm=ppm):
                n = 180 * FS
                ref, mic = self._drifting(ppm, 180)
                writes = []
                with tempfile.TemporaryDirectory() as d:
                    store = AEC.DelayStore(os.path.join(d, "s.json"))
                    put = store.put
                    store.put = lambda k, v, *a: (writes.append(v), put(k, v, *a))
                    sim = Sim(delay_store=store, pad_id="pad1")
                    out = aligned(sim.run(mic, ref=ref), n)
                r = sim.eng._refs["Speakers (Test)"]
                self.assertEqual(sim.eng.stats["delay_moves"], 0)
                self.assertEqual(sim.eng.stats["unslotted"], 0)
                self.assertAlmostEqual(abs(r.rate) * 1e6, ppm, delta=ppm * 0.05)
                for k in range(30 * FS, n - 30 * FS + 1, 30 * FS):
                    self.assertLess(db(out[k:k + 30 * FS], mic[k:k + 30 * FS]), -20.0)
                # the store is not rewritten on every re-alignment
                self.assertLess(len(writes), 12)

    def test_a_steady_speaker_is_not_given_a_drift(self):
        # HDMI was measured at +0.56 ppm: that is left to the filter, as before
        n = 90 * FS
        ref, mic = self._drifting(0.56, 90)
        sim = Sim()
        out = aligned(sim.run(mic, ref=ref), n)
        self.assertEqual(sim.eng._refs["Speakers (Test)"].rate, 0.0)
        self.assertEqual(sim.eng.stats["delay_moves"], 0)
        self.assertLess(db(out[30 * FS:], mic[30 * FS:]), -25.0)

    def test_a_silent_endpoint_gives_its_slot_to_a_new_one(self):
        # three stereo speakers used one after another; two fill the channel
        # budget, the third still gets cancelled
        with tempfile.TemporaryDirectory() as d:
            store = AEC.DelayStore(os.path.join(d, "s.json"))
            sim = Sim(delay_store=store, pad_id="pad1")
            outs = {}
            for name, seed, delay in (("A", 6, 40), ("B", 12, 70), ("C", 9, 100), ("A", 6, 40)):
                n = 14 * FS
                left = speechlike(n, seed, rate=5.1)
                st = np.stack([left, 0.7 * left + 0.3 * speechlike(n, seed + 20, rate=4.3)], 1)
                mic = echo_of(st[:, 0], room(seed, 0.5), delay) + echo_of(st[:, 1], room(seed + 1, 0.5), delay + 3)
                outs.setdefault(name, []).append((aligned(sim.run(mic, ref={name: st}), n), mic))
                self.assertIn(name, sim.eng.status()["slots"], f"{name} not slotted")
            self.assertEqual(sim.eng.stats["evicted"], 2)          # A for C, then B for A
            self.assertGreaterEqual(sim.eng.stats["slotted_from_store"], 1)
            for name, runs in outs.items():
                out, mic = runs[-1]
                self.assertLess(db(out[10 * FS:13 * FS], mic[10 * FS:13 * FS]), -15.0, name)

    def test_an_unwritable_delay_store_is_reported_once(self):
        with tempfile.TemporaryDirectory() as d:
            store = AEC.DelayStore(d)            # a directory: cannot be written
            with self.assertLogs("ds5emu.aec", level="WARNING") as cm:
                store.put("k", 1.0)
                store.put("k", 2.0)
                store.put("k", 3.0)
            self.assertEqual(len([m for m in cm.output if "cannot be remembered" in m]), 1)

    def test_the_arrival_pattern_is_relearned_after_the_room_changes(self):
        # the furniture moved: the echo's arrival pattern no longer matches the
        # one the fine alignment learned, but probes still find the echo
        sim = Sim()
        n = 50 * FS
        ref = speechlike(n, 2, rate=5.1)
        mic = np.concatenate([echo_of(ref, room(6, 0.6), 40)[: 15 * FS],
                              echo_of(ref, room(11, 0.6), 40)[15 * FS:]])
        out = aligned(sim.run(mic, ref=ref), n)
        self.assertGreaterEqual(sim.eng.stats["fine_repatterns"], 1)
        self.assertIsNotNone(sim.eng._refs["Speakers (Test)"].fine_pat)
        self.assertEqual(sim.eng.status()["slots"], ["Speakers (Test)"])
        self.assertLess(db(out[40 * FS:49 * FS], mic[40 * FS:49 * FS]), -20.0)

    def test_a_masked_but_correct_remembered_delay_is_kept(self):
        # The right delay, but a loud source in the room hides the echo for
        # the provisional 3 s: it may be dropped for now, but the store
        # keeps it (only a probe finding the echo elsewhere replaces it).
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "echo-delays.json")
            first = Sim(delay_store=AEC.DelayStore(path), pad_id="pad1")
            n = 10 * FS
            ref = speechlike(n, 2, rate=5.1)
            echo = echo_of(ref, room(6, 0.6), 40)
            first.run(echo, ref=ref)
            good = AEC.DelayStore(path).get("pad1|Speakers (Test)")
            self.assertIsNotNone(good)
            # next run: a talker 20 dB above the echo for the first 6 s
            talk = speechlike(n, 9, rate=2.3, amp=0.3) * np.float32(
                10 ** (20 / 20) * np.sqrt(np.mean(echo ** 2)) / 0.3 / 0.25)
            talk[6 * FS:] = 0
            sim = Sim(delay_store=AEC.DelayStore(path), pad_id="pad1")
            sim.run(echo + talk, ref=ref)
            self.assertEqual(sim.eng.stats["slotted_from_store"], 1)
            self.assertEqual(sim.eng.stats["store_rejected"], 1)     # masked: not proven
            self.assertEqual(sim.eng.status()["slots"], ["Speakers (Test)"])  # found again
            self.assertAlmostEqual(AEC.DelayStore(path).get("pad1|Speakers (Test)"), good, delta=0.5)

    def test_a_provisional_delay_is_never_written(self):
        # neither by a re-alignment nor when its slot is taken by eviction
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "echo-delays.json")
            store = AEC.DelayStore(path)
            store.put("pad1|A", 50.0)
            eng = AEC.EchoCanceller(lambda b: None, loopback=False, clock=lambda: 100.0,
                                    delay_store=store, pad_id="pad1")
            eng.prepare()
            a = AEC.Ref("A", "A", 2)
            a.delay = 70.0 * FS / 1000
            a.timeline.last_loud = 50.0                     # silent for 50 s
            self.assertTrue(eng._slot(a, provisional=True))
            eng._remember(a)
            eng._realign(a, 3.0, 1.0)
            self.assertEqual(AEC.DelayStore(path).get("pad1|A"), 50.0)
            b = AEC.Ref("B", "B", 2)
            b.delay = 10.0 * FS / 1000
            b.timeline.last_loud = 99.0
            self.assertTrue(eng._slot(b))
            c = AEC.Ref("C", "C", 2)
            c.delay = 20.0 * FS / 1000
            c.timeline.last_loud = 100.0
            self.assertTrue(eng._slot(c))                   # evicts A, the silent one
            self.assertEqual(eng.stats["evicted"], 1)
            self.assertNotIn(a, eng._slots)
            self.assertEqual(AEC.DelayStore(path).get("pad1|A"), 50.0)

    def test_mirrored_audio_keeps_each_echo_on_its_own_endpoint(self):
        # the same stream on two endpoints (laptop speakers + a cast display 150 ms behind): each endpoint's
        # probe also sees the other's echo, and the cast's (louder) echo peaks higher in the laptop's probe
        # than the laptop's own. Without the ghost ranges both slots sat on the cast's echo (7.7 dB). Which
        # endpoint is probed first must not matter: with the cast first the laptop slots on the cast's echo,
        # and gives it up when the cast asks (PROBE_YIELD_S).
        n = 50 * FS
        a = speechlike(n, 2, rate=5.1)
        sh = int(0.150 * FS)
        b = np.concatenate([np.zeros(sh, np.float32), a[:-sh]])
        mic = echo_of(a, room(6, 0.35), 20) + echo_of(b, room(8, 0.6), 300) + np.float32(1e-4) * \
            np.random.default_rng(1).standard_normal(n).astype(np.float32)
        for order in (("Laptop", "Cast"), ("Cast", "Laptop")):
            with self.subTest(first=order[0]):
                sim = Sim()
                refs = {"Laptop": a, "Cast": b}
                out = aligned(sim.run(mic, ref={k: refs[k] for k in order}), n)
                self.assertEqual(sorted(sim.eng.status()["slots"]), ["Cast", "Laptop"])
                laptop = sim.eng._refs["Laptop"]
                self.assertLess((laptop.delay + laptop.adj) * 1000 / FS, 100.0)    # its own echo, not the cast's
                self.assertLessEqual(sim.eng.stats["delay_moves"], 1)
                self.assertLess(db(out[35 * FS:49 * FS], mic[35 * FS:49 * FS]), -20.0)

    def test_an_early_delay_step_beyond_the_fine_range_is_followed(self):
        # the echo arrives 30 ms earlier from 20 s on (an AV receiver's latency change) and the room has a
        # reflection 30 ms after the direct sound, so the old window still holds an echo: the move is taken
        def h_with_reflection():
            h = room(6, 0.3).copy()
            h[0] += 0.6
            h[int(0.030 * FS)] += 0.3
            return h
        sim = Sim()
        n = 40 * FS
        x = speechlike(n, 2, rate=5.1)
        e = np.convolve(x, h_with_reflection())[:n]
        k0, k1, ts = int(0.040 * FS), int(0.010 * FS), 20 * FS
        mic = np.zeros(n, np.float32)
        mic[k0:ts] = e[:ts - k0]
        mic[ts:] = e[ts - k1:n - k1]
        mic += np.float32(1e-4) * np.random.default_rng(1).standard_normal(n).astype(np.float32)
        out = aligned(sim.run(mic, ref=x), n)
        self.assertGreaterEqual(sim.eng.stats["delay_moves"], 1)
        self.assertLess(db(out[25 * FS:39 * FS], mic[25 * FS:39 * FS]), -10.0)

    def test_a_software_volume_endpoint_turned_down_is_cancelled(self):
        # the endpoint applies its volume before the loopback tap: a reference at -67 dBFS RMS whose echo the
        # room still hears (spacedesk at volume 0.1). The gate follows the endpoint's level and the stage is
        # fed the reference boosted (speexdsp's MDF does not adapt at that level).
        sim = Sim()
        n = 40 * FS
        x = speechlike(n, 2, rate=5.1)
        ref = x * np.float32(0.01)
        mic = echo_of(x * np.float32(0.1), room(6, 0.6), 40) + np.float32(1e-4) * \
            np.random.default_rng(1).standard_normal(n).astype(np.float32)
        out = aligned(sim.run(mic, ref=ref), n)
        self.assertEqual(sim.eng.status()["slots"], ["Speakers (Test)"])
        self.assertGreater(sim.eng._refs["Speakers (Test)"].norm_db, 20.0)
        # (the boost waits for REF_NORM_MATURE_S of audio over the -70 dB floor: ~17 s of this speech)
        self.assertLess(db(out[30 * FS:39 * FS], mic[30 * FS:39 * FS]), -20.0)

    def test_hiss_with_blips_on_an_endpoint_leaves_the_mic_bit_exact(self):
        # -75 dBFS hiss with an occasional -60 dBFS blip, on an endpoint the mic does not hear
        sim = Sim()
        n = 12 * FS
        mic = speechlike(n, 7)
        ref = (np.random.default_rng(5).standard_normal(n) * 10 ** (-75 / 20)).astype(np.float32)
        for s in range(FS, n - FS, 2 * FS):
            ref[s:s + F] *= np.float32(10 ** (15 / 20))
        out = sim.run(mic, ref=ref)
        self.assertEqual(sim.eng.status()["slots"], [])
        want = to16(mic)
        np.testing.assert_array_equal(out[F:F + want.size - F], want[:want.size - F])

    def test_a_cancelled_endpoint_that_falls_to_hiss_goes_idle_again(self):
        sim = Sim()
        n = 24 * FS
        ref = speechlike(n, 2, rate=5.1)
        ref[14 * FS:] = (np.random.default_rng(6).standard_normal(n - 14 * FS) * 10 ** (-75 / 20)).astype(np.float32)
        mic = echo_of(ref, room(6, 0.6), 40) + speechlike(n, 7, amp=0.05)
        out = sim.run(mic, ref=ref)
        self.assertEqual(sim.eng.status()["slots"], ["Speakers (Test)"])
        want = to16(mic)
        tail = slice(18 * FS, n - FS)                      # the hold and the tail are over: bit for bit again
        np.testing.assert_array_equal(out[tail.start + F:tail.stop + F], want[tail])
        self.assertEqual(sim.eng._refs["Speakers (Test)"].norm_db, 0.0)    # a full-scale endpoint: no boost

    def test_rescaling_a_stage_keeps_what_it_learned(self):
        # the reference fed at a new gain from one frame on (REF_NORM_*): with the state rescaled the converged
        # filter stays right; a blind gain step makes it 4x wrong and it takes seconds to re-learn
        n = 9 * FS
        x = (speechlike(n, 2, rate=5.1) * 32767 * 0.3).astype(np.float64)
        y = np.convolve(x, room(6, 0.6))[:n]
        noise = np.random.default_rng(8).standard_normal(n)
        k0 = 6 * FS // F

        def erle_after_step(g, rescale):
            st, can = AEC.NativeStage(1), AEC.NativeCanceller()
            can.set(bypass=1)
            o = []
            for j in range(n // F):
                if j == k0 and rescale:
                    st.scale_ref(g)
                xf = x[j * F:(j + 1) * F] * (g if j >= k0 else 1.0)
                mic = np.clip(np.round(y[j * F:(j + 1) * F] + noise[j * F:(j + 1) * F]), -32768, 32767)
                o.append(can.process(mic.astype("<i2"), [st],
                                     [np.ascontiguousarray(np.clip(np.round(xf), -32768, 32767).astype("<i2")[:, None])], True))
            st.close()
            can.close()
            out = np.concatenate(o).astype(np.float64)[F:]
            s = slice(6 * FS, 7 * FS)                     # the first second after the step
            return -db(out[s], y[s])

        steady = erle_after_step(1.0, False)
        for g in (4.0, 0.25):
            self.assertGreater(erle_after_step(g, True), steady - 3.0)
            self.assertLess(erle_after_step(g, False), steady - 15.0)

    def test_a_remembered_delay_with_a_fade_in_is_not_boosted(self):
        # a full-scale endpoint, its delay remembered: playback starts with a 2 s fade-in from -60 dB. A boost
        # chosen from the fade-in's level would clip the reference for the hold (review B1: 14-16 dB for 30 s)
        n = 30 * FS
        x = speechlike(n, 2, rate=5.1, amp=0.9)
        h = room(6, 0.6)
        noise = lambda s: np.float32(1e-4) * np.random.default_rng(s).standard_normal(n).astype(np.float32)  # noqa: E731
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "delays.json")
            Sim(delay_store=AEC.DelayStore(path), pad_id="pad1").run(echo_of(x * np.float32(0.5), h, 40) + noise(1),
                                                                      ref=x)
            fade = np.ones(n, np.float32)
            fade[:2 * FS] = 10 ** (np.linspace(-60, 0, 2 * FS) / 20)
            y = speechlike(n, 4, rate=4.3, amp=0.9) * fade
            mic = echo_of(y * np.float32(0.5), h, 40) + noise(2)
            sim = Sim(delay_store=AEC.DelayStore(path), pad_id="pad1")
            out = aligned(sim.run(mic, ref=y), n)
        self.assertEqual(sim.eng.stats["slotted_from_store"], 1)
        self.assertEqual(sim.eng._refs["Speakers (Test)"].norm_db, 0.0)
        for s0 in range(4, 28, 4):
            self.assertLess(db(out[s0 * FS:(s0 + 4) * FS], mic[s0 * FS:(s0 + 4) * FS]), -40.0)

    def test_a_quiet_intro_on_a_full_scale_endpoint_never_clips_the_fed_reference(self):
        n = 40 * FS
        x = speechlike(n, 2, rate=5.1, amp=0.9)
        vol = np.ones(n, np.float32)
        vol[:12 * FS] = 10 ** (-30 / 20)                    # a quiet intro (-48 dBFS), then -18 dBFS
        ref = x * vol
        mic = echo_of(ref * np.float32(0.5), room(6, 0.6), 40) + np.float32(1e-4) * \
            np.random.default_rng(1).standard_normal(n).astype(np.float32)
        peak = [0]
        orig = AEC.NativeCanceller.process

        def process(nc, mic16, stages, refs, active):
            for r in refs:
                peak[0] = max(peak[0], int(np.max(np.abs(r.astype(np.int32)))))
            return orig(nc, mic16, stages, refs, active)

        AEC.NativeCanceller.process = process
        try:
            sim = Sim()
            out = aligned(sim.run(mic, ref=ref), n)
        finally:
            AEC.NativeCanceller.process = orig
        self.assertLess(peak[0], 32767)
        self.assertEqual(sim.eng._refs["Speakers (Test)"].norm_db, 0.0)
        self.assertLess(db(out[16 * FS:39 * FS], mic[16 * FS:39 * FS]), -40.0)

    def test_steady_keep_alive_noise_lets_the_canceller_go_idle(self):
        # -65 dBFS steady noise after the content stops (a keep-alive noise player): over the old -70 floor,
        # but it is the endpoint's own noise floor, so the bit-exact idle path comes back -- heard or not
        n = 34 * FS
        content = speechlike(n, 2, rate=5.1)
        content[10 * FS:] = (np.random.default_rng(6).standard_normal(n - 10 * FS) * 10 ** (-65 / 20)).astype(np.float32)
        for heard in (False, True):
            with self.subTest(heard=heard):
                sim = Sim()
                mic = speechlike(n, 7, amp=0.05) + (echo_of(content, room(6, 0.6), 40) if heard else 0)
                out = sim.run(mic.astype(np.float32), ref=content)
                want = to16(mic.astype(np.float32))
                tail = slice(28 * FS, n - FS)
                np.testing.assert_array_equal(out[tail.start + F:tail.stop + F], want[tail])

    def test_an_unheard_mirror_does_not_keep_the_heard_endpoint_yielding(self):
        # headphones (or a muted cast) playing the same stream 150 ms AHEAD of the laptop: the laptop's echo shows
        # in their probe as another endpoint's ghost, forever. They may ask once; then they back off
        n = 60 * FS
        a = speechlike(n, 2, rate=5.1)
        sh = int(0.150 * FS)
        phones = np.concatenate([a[sh:], np.zeros(sh, np.float32)])
        mic = echo_of(a, room(6, 0.6), 20) + np.float32(1e-4) * \
            np.random.default_rng(1).standard_normal(n).astype(np.float32)
        for order in (("Laptop", "Phones"), ("Phones", "Laptop")):
            with self.subTest(first=order[0]):
                sim = Sim()
                refs = {"Laptop": a, "Phones": phones}
                out = aligned(sim.run(mic, ref={k: refs[k] for k in order}), n)
                self.assertLessEqual(sim.eng.stats["yield_asks"], 1)
                laptop = sim.eng._refs["Laptop"]
                self.assertTrue(laptop.slotted)
                self.assertLess((laptop.delay + laptop.adj) * 1000 / FS, 100.0)
                self.assertLess(db(out[30 * FS:59 * FS], mic[30 * FS:59 * FS]), -40.0)

    def test_two_near_equal_arrivals_are_found(self):
        # two reflections of the same strength 25 ms apart split the probe's
        # peak; each alone stays under PROBE_MIN_PEAK
        h = room(6, 0.6)
        h[1200:2400] += np.roll(h, 1200)[1200:2400] * 0.95
        h = h / np.sqrt(np.sum(h * h)) * 0.6
        sim = Sim()
        n = 40 * FS
        ref = speechlike(n, 2, rate=5.1)
        mic = echo_of(ref, h, 40)
        out = aligned(sim.run(mic, ref=ref), n)
        self.assertEqual(sim.eng.status()["slots"], ["Speakers (Test)"])
        self.assertLess(db(out[25 * FS:39 * FS], mic[25 * FS:39 * FS]), -25.0)

    def test_a_mask_does_not_spoil_drift_following(self):
        # 100 ppm drift, a loud talker for 12 s: afterwards the cancellation
        # is back to what it was before (the pattern is not re-learned on
        # the talker)
        n = 130 * FS
        ref = speechlike(n, 2, rate=5.1)
        played = np.interp(np.arange(n) * (1 + 100e-6), np.arange(n), ref).astype(np.float32)
        mic = echo_of(played, room(6, 0.6), 40) + np.float32(1e-4) * \
            np.random.default_rng(1).standard_normal(n).astype(np.float32)
        mic[40 * FS:52 * FS] += speechlike(12 * FS, 9, rate=4.2, amp=0.6)
        sim = Sim()
        out = aligned(sim.run(mic, ref=ref), n)
        before = db(out[20 * FS:40 * FS], mic[20 * FS:40 * FS])
        after = db(out[70 * FS:129 * FS], mic[70 * FS:129 * FS])
        self.assertEqual(sim.eng.stats["fine_repatterns"], 0)
        self.assertLess(after, before + 2.0, f"before {before:.1f} after {after:.1f}")

    def test_no_dll_means_no_canceller_not_a_dead_mic(self):
        def broken():
            raise OSError("ds5aec.dll not found")

        eng = AEC.EchoCanceller(lambda b: None, loopback=False, native_factory=broken)
        eng.prepare()
        self.assertEqual(eng.mode, "passthrough")
        self.assertFalse(eng.push_mic(np.zeros(F, np.float32), 0))


@unittest.skipUnless(HAVE_NUMPY, "numpy not available")
class LoopbackShapeTests(unittest.TestCase):
    def test_stereo_stays_stereo_and_surround_folds_to_two(self):
        from ds5emu import loopback as LB
        st = np.random.default_rng(1).standard_normal((100, 2)).astype(np.float32)
        np.testing.assert_array_equal(LB.speaker_channels(st, "Speakers"), st)
        self.assertEqual(LB.speaker_channels(st[:, 0], "Speakers").shape, (100, 1))
        five1 = np.zeros((10, 6), np.float32)
        five1[:, 0] = 1.0                                       # front left only
        y = LB.speaker_channels(five1, "HDMI", mask=0x3F)
        self.assertEqual(y.shape, (10, 2))
        self.assertGreater(y[0, 0], 0.4)
        self.assertEqual(y[0, 1], 0.0)
        five1[:, :] = 0
        five1[:, 2] = 1.0                                       # centre: both sides
        y = LB.speaker_channels(five1, "HDMI", mask=0x3F)
        self.assertAlmostEqual(float(y[0, 0]), float(y[0, 1]))
        pad = np.zeros((10, 4), np.float32)
        pad[:, 2:] = 1.0                                        # haptics only
        y = LB.speaker_channels(pad, "Speakers (DualSense Wireless Controller)")
        self.assertEqual(y.shape, (10, 2))
        self.assertEqual(float(np.abs(y).max()), 0.0)

    def test_only_this_pads_usbip_speaker_is_left_out(self):
        class Ep:
            def __init__(self, i, name):
                self.id, self.name = i, name

        addr = {"ours": 1, "sibling": 2, "wired": None}
        eng = AEC.EchoCanceller(lambda b: None, loopback=False,
                                own_usb_ports=lambda: {1}, usb_address=addr.get)
        pad = "Speakers (DualSense Wireless Controller)"
        self.assertTrue(eng._exclude(Ep("ours", pad)))
        self.assertFalse(eng._exclude(Ep("sibling", pad)))
        # a pad wired to a real USB port (whatever its number): not ours
        self.assertFalse(eng._exclude(Ep("wired", pad)))
        self.assertFalse(eng._exclude(Ep("x", "HDMI (NVIDIA High Definition Audio)")))
        unknown = AEC.EchoCanceller(lambda b: None, loopback=False, usb_address=addr.get)
        self.assertTrue(unknown._exclude(Ep("sibling", pad)))   # ports not known yet


@unittest.skipUnless(HAVE_NUMPY, "numpy not available")
class TimingTests(unittest.TestCase):
    def test_the_reference_timeline_follows_presentation_stamps(self):
        tl = AEC.RefTimeline()
        x = np.arange(1, 4801, dtype=np.float32)
        for i in range(10):
            tl.push(x[i * F:(i + 1) * F], (i + 1) * 0.01, 0.0)
        np.testing.assert_allclose(tl.read(0.0, 4800)[:, 0], x, atol=1e-3)

    def test_a_fractional_read_interpolates(self):
        tl = AEC.RefTimeline()
        x = np.sin(2 * np.pi * 440 * np.arange(4800) / FS).astype(np.float32)
        for i in range(10):
            tl.push(x[i * F:(i + 1) * F], (i + 1) * 0.01, 0.0)
        y = tl.read(100.5, 1000)[:, 0]
        want = np.sin(2 * np.pi * 440 * (np.arange(1000) + 100.5) / FS)
        np.testing.assert_allclose(y, want, atol=2e-3)

    def _wobbly_stamps(self, n, seed=7, std_ms=0.31, rho=0.9):
        """Presentation stamps of a steady 10 ms stream that wobble like spacedesk's (round 10, block K):
        0.31 ms std, correlated over a few packets, never near STAMP_JUMP."""
        r = np.random.default_rng(seed)
        j = np.zeros(n)
        for i in range(1, n):
            j[i] = rho * j[i - 1] + np.sqrt(1 - rho * rho) * r.normal(0.0, std_ms * 1e-3)
        return (np.arange(n) + 1) * 0.01 + np.clip(j, -1.5e-3, 1.5e-3)

    def test_jittering_stamps_leave_the_read_point_steady(self):
        # the audio is contiguous; only its stamps wobble. Following them packet by packet moved the read
        # point 5 samples std within each second and cost the linear stage its whole cancellation (block K)
        tl = AEC.RefTimeline()
        st = self._wobbly_stamps(3000)
        x = np.zeros(F, np.float32)
        offs = []
        for t in st:
            tl.push(x, t, 0.0)
            offs.append(tl.off)
        offs = np.array(offs[1000:])
        self.assertEqual(tl.anchors, 1)
        self.assertLess(float(np.std(offs)), 2.0)                  # samples; 15 in the stamps (0.05 per packet: ~5)
        self.assertLess(float(np.max(np.abs(np.diff(offs)))), 0.1)
        self.assertLess(abs(float(np.mean(offs))), 4.0)            # and in the right place

    def test_a_real_offset_step_still_re_anchors(self):
        tl = AEC.RefTimeline()
        x = np.arange(1, 4801, dtype=np.float32)
        for i in range(200):
            tl.push(x[:F], (i + 1) * 0.01, 0.0)
        self.assertEqual(tl.anchors, 1)
        # the stream jumps 5 ms (240 samples > STAMP_JUMP): the new packets sit where their stamps say at once
        step = 0.005
        for i in range(200, 210):
            k = i - 200
            tl.push(x[k * F:(k + 1) * F], (i + 1) * 0.01 + step, 0.0)
        self.assertEqual(tl.anchors, 2)
        start = (200 * 0.01 + step) * FS
        np.testing.assert_allclose(tl.read(start, 10 * F)[:, 0], x[:10 * F], atol=1e-3)

    def test_a_drifting_stream_clock_is_still_followed(self):
        # 100 ppm against QPC for 90 s (432 samples in all): followed without a re-anchor. The slow follower
        # settles to trailing the ramp by a CONSTANT slope / REF_STAMP_ALPHA (0.048 / 0.001 = 48 samples
        # here), which the fine alignment absorbs like any delay; it must not keep growing.
        tl = AEC.RefTimeline()
        x = np.zeros(F, np.float32)
        lag = {}
        for i in range(9000):
            tl.push(x, (i + 1) * 0.01 * (1 + 100e-6), 0.0)
            if i + 1 in (6000, 9000):
                lag[i + 1] = (i + 1) * 0.01 * 100e-6 * FS - tl.off
        self.assertEqual(tl.anchors, 1)
        self.assertLess(abs(lag[9000] - lag[6000]), 1.0)
        self.assertLess(abs(lag[9000]), 0.048 / AEC.REF_STAMP_ALPHA + 5)

    def test_excluded_lags_are_not_the_best_peak(self):
        r = np.random.default_rng(4)
        src = r.standard_normal(3 * FS).astype(np.float32)
        M, A = int(0.6 * FS), int(0.05 * FS)
        d1, d2 = int(0.100 * FS), int(0.300 * FS)
        x = np.concatenate([np.zeros(M, np.float32), src])
        mic = 0.5 * np.roll(x, d1)[M:M + FS] + 1.0 * np.roll(x, d2)[M:M + FS]
        ref = x[M - M:M + FS + A]
        self.assertAlmostEqual(AEC.estimate_lag(mic, ref, M, A)[0], d2, delta=12)
        lag, conf = AEC.estimate_lag(mic, ref, M, A, exclude=((d2 - 480, d2 + 480),))
        self.assertAlmostEqual(lag, d1, delta=12)
        self.assertGreater(conf, AEC.PROBE_SURE_PEAK)
        # whether an echo is inside a filter's window is not affected by the exclusion
        near = (d2 - 240, d2 + 240)
        self.assertAlmostEqual(AEC.estimate_lag(mic, ref, M, A, near=near, exclude=(near,))[2],
                               AEC.estimate_lag(mic, ref, M, A, near=near)[2], places=6)

    def _decider(self):
        class Stage:
            def __init__(self, nch):
                self.nch = nch

            def reset(self): pass
            def freeze(self, frozen): pass
            def flush(self, frames=0): pass
            def close(self): pass

        eng = AEC.EchoCanceller(lambda b: None, loopback=False, clock=lambda: 100.0,
                                stage_factory=Stage)
        ref = AEC.Ref("ep", "Speakers (Test)", 1)
        eng._refs[ref.id] = ref
        return eng, ref

    def test_a_run_of_weaker_peaks_at_one_lag_slots_the_endpoint(self):
        eng, ref = self._decider()
        lag = 2000
        res = [(lag, 9.5, lag + 900, 4.0)] * 3                # each window under PROBE_MIN_PEAK
        for _ in range(3):                                     # literal counts: the rule is four in a row
            eng._probe_apply(ref, 100.0, res, False)
            self.assertFalse(ref.slotted)
        eng._probe_apply(ref, 100.0, res, False)
        self.assertTrue(ref.slotted)
        self.assertEqual(eng.stats["run_slotted"], 1)
        self.assertAlmostEqual(ref.delay, lag - AEC.EARLY_MS * FS // 1000, delta=1)

    def test_weaker_peaks_that_wander_never_slot(self):
        eng, ref = self._decider()
        for k in range(40):
            lag = 2000 + (k % 5) * 1500                        # unrelated audio: somewhere new every window
            eng._probe_apply(ref, 100.0, [(lag, 10.0, lag + 700, 4.0)] * 3, False)
        self.assertFalse(ref.slotted)
        eng2, ref2 = self._decider()
        for k in range(40):                                    # the same lag, but a weak window between
            eng2._probe_apply(ref2, 100.0, [(2000, 9.0 if k % 3 else 6.0, 2900, 4.0)] * 3, False)
        self.assertFalse(ref2.slotted)

    def test_the_reference_boost_rises_rarely_and_falls_at_once(self):
        now = [100.0]
        calls = []

        class Stage:
            nch = 1
            ok = True

            def scale_ref(self, g):
                calls.append(round(20 * math.log10(g), 1))
                return self.ok

            def reset(self):
                calls.append("reset")

        eng = AEC.EchoCanceller(lambda b: None, loopback=False, clock=lambda: now[0], stage_factory=lambda n: Stage())
        ref = AEC.Ref("ep", "Speakers (Test)", 1)
        ref.stage = Stage()
        tl = ref.timeline
        tl.level_slow_db, tl.peak_db, tl.level_secs = -54.0, -40.0, 5.0
        eng._normalise(ref)
        self.assertEqual((ref.norm_db, calls), (0.0, []))            # not 10 s of audio yet: no boost
        tl.level_secs = 12.0
        eng._normalise(ref)
        self.assertEqual((ref.norm_db, calls), (24.0, [24.0]))        # the first mature choice
        tl.level_slow_db = -40.0
        eng._normalise(ref)
        self.assertEqual((ref.norm_db, calls[-1]), (12.0, -12.0))     # lower: at once
        tl.level_slow_db = -48.0
        now[0] += 60
        eng._normalise(ref)
        self.assertEqual(ref.norm_db, 12.0)                           # +6 only: not worth a change
        tl.level_slow_db = -60.0
        eng._normalise(ref)
        self.assertEqual((ref.norm_db, calls[-1]), (30.0, 18.0))      # +18, the hold long over
        tl.level_slow_db = -72.0
        eng._normalise(ref)
        self.assertEqual(ref.norm_db, 30.0)                           # +12 more, but within the hold
        tl.peak_db = -20.0
        eng._normalise(ref)
        self.assertEqual((ref.norm_db, calls[-1]), (12.0, -18.0))     # the headroom: peak + gain <= -6 dBFS, at once
        tl.level_slow_db, tl.peak_db = -10.0, -1.0
        eng._normalise(ref)
        self.assertEqual(ref.norm_db, 0.0)                            # a want of 0 always wins
        ref.stage.ok = False
        tl.level_slow_db, tl.peak_db = -50.0, -30.0
        now[0] += 60
        eng._normalise(ref)
        self.assertEqual(calls[-1], "reset")                          # the native rescale refused: start over

    def test_a_probe_is_not_decided_when_the_slots_changed_meanwhile(self):
        eng, ref = self._decider()
        decided = []
        eng._probe_apply = lambda *a, **k: decided.append(a)
        rng = np.random.default_rng(1)
        W, M, A = int(AEC.PROBE_WINDOW_S * FS), int(AEC.PROBE_MAX_LAG_S * FS), AEC.PROBE_LEAD_MS * FS // 1000
        mic = rng.standard_normal(W).astype(np.float32)
        views = [rng.standard_normal(W + M + A).astype(np.float32)] * 3
        gen = eng._slot_gen
        eng._probe_gather = lambda r: (mic, views, M, A, None, ([], 0), gen, False)
        eng._probe(ref, 100.0)
        self.assertEqual(len(decided), 1)
        eng._slot_gen += 1                                           # an endpoint slotted between gather and apply
        eng._probe(ref, 100.0)
        self.assertEqual(len(decided), 1)

    def test_a_yielding_endpoint_still_follows_its_own_echo(self):
        eng, ref = self._decider()
        flushed, reset = [], []
        ref.delay = 2000.0
        self.assertTrue(eng._slot(ref))
        ref.stage.flush = lambda frames=0: flushed.append(1)
        ref.stage.reset = lambda: reset.append(1)
        ref.yield_until = 100.0 + AEC.PROBE_YIELD_S
        E = AEC.EARLY_MS * FS // 1000
        early = 2000 + E - int(0.030 * FS)                    # its echo 30 ms earlier: a normal move, taps kept
        for _ in range(2):
            eng._probe_apply(ref, 100.0, [(early, 15.0, 2.0)] * 3, True)
        self.assertAlmostEqual(ref.delay, early - E, delta=1)
        self.assertEqual((len(flushed), len(reset), eng.stats["yield_moves"]), (1, 0, 0))
        far = int(ref.delay) + E + int(0.300 * FS)            # beyond the tail: the yield move, at a lower bar
        ref.unsettled = True
        ref.stage.freeze = lambda frozen: None
        for k in range(5):                                     # four agreeing windows; a weak one between
            eng._probe_apply(ref, 100.0, [(far, 9.0 if k != 2 else 5.0, 2.0)] * 3, True)
        self.assertAlmostEqual(ref.delay, far - E, delta=1)
        self.assertEqual((len(reset), eng.stats["yield_moves"]), (1, 1))
        self.assertFalse(ref.unsettled)                        # the fresh filter adapts at once

    def test_quiet_continuous_music_counts_as_playing(self):
        # gapless content only dips a few dB under its median: it must not be taken for the endpoint's noise floor
        rng = np.random.default_rng(5)
        n = 40 * FS
        t = np.arange(n) / FS
        music = np.convolve(rng.standard_normal(n), np.exp(-np.arange(30) / 8.0), "same") * \
            (1 + 0.4 * np.sin(2 * np.pi * 0.5 * t))
        music = (music / np.sqrt(np.mean(music ** 2)) * 10 ** (-63 / 20)).astype(np.float32)
        tl = AEC.RefTimeline()
        loud = []
        for i in range(n // F):
            before = tl.last_loud
            tl.push(music[i * F:(i + 1) * F], (i + 1) * 0.01, 0.0)
            loud.append(tl.last_loud != before)
        self.assertGreater(np.mean(loud[1500:]), 0.5)

    def test_a_slot_whose_echo_is_still_there_is_not_moved(self):
        eng, ref = self._decider()
        ref.delay = 600.0
        self.assertTrue(eng._slot(ref))
        far = 600 + AEC.EARLY_MS * FS // 1000 + 13000         # a stronger peak 270 ms away
        for _ in range(4):
            eng._probe_apply(ref, 100.0, [(far, 25.0, 15.0)] * 3, True)    # own echo still at 15 in the window
        self.assertEqual(ref.delay, 600.0)
        self.assertEqual(eng.stats["delay_moves"], 0)
        for _ in range(2):
            eng._probe_apply(ref, 100.0, [(far, 25.0, 2.0)] * 3, True)     # the echo left the window
        self.assertEqual(eng.stats["delay_moves"], 1)
        self.assertAlmostEqual(ref.delay, far - AEC.EARLY_MS * FS // 1000, delta=1)

    def test_the_activity_gate_follows_the_endpoint_level_but_not_into_silence(self):
        tl = AEC.RefTimeline()
        quiet = (np.random.default_rng(2).standard_normal(F) * 10 ** (-65 / 20)).astype(np.float32)
        for i in range(300):
            tl.push(quiet, (i + 1) * 0.01, 0.0)
        self.assertLess(tl.loud_thr, 10 ** (-66 / 20))                        # well under -60 dBFS now
        self.assertGreater(tl.last_loud, 2.9)                               # and the quiet stream is "playing"
        t_loud = tl.last_loud
        for i in range(300, 400):
            tl.push(np.zeros(F, np.float32), (i + 1) * 0.01, 0.0)          # digital silence
        self.assertEqual(tl.last_loud, t_loud)
        hiss = (np.random.default_rng(3).standard_normal(F) * 10 ** (-90 / 20)).astype(np.float32)
        tl2 = AEC.RefTimeline()
        for i in range(300):
            tl2.push(hiss, (i + 1) * 0.01, 0.0)
        self.assertEqual(tl2.last_loud, -math.inf)                          # faint hiss never counts
        self.assertEqual(tl2.loud_thr, AEC.LOUD_RMS)

    def test_the_mic_clock_takes_the_least_late_packet(self):
        mc = AEC.MicClock()
        r = np.random.default_rng(2)
        for i in range(300):
            late = 0.002 if i % 37 == 5 else r.uniform(0.010, 0.030)
            mc.advance(F, 0.5 + (i + 1) * 0.01 + late, 0.0)
        self.assertAlmostEqual(mc.off_f, (0.5 + 0.002) * FS, delta=6)

    def test_the_mic_mapping_is_held_for_the_session(self):
        # 5 minutes of jittery arrivals: the mapping does not wander (a
        # fitted "drift" walked it 22 samples live and cost 6 dB)
        mc = AEC.MicClock()
        r = np.random.default_rng(4)
        for i in range(30000):
            mc.advance(F, 0.5 + (i + 1) * 0.01 + r.gamma(1.5, 0.003), 0.0)
            if i == 10:
                held = mc.off_f
        self.assertEqual(mc.off_f, held)

    def test_the_probe_finds_a_delay_and_ignores_unrelated_audio(self):
        M = int(0.6 * FS)
        ref = speechlike(3 * FS, 1)
        echo = echo_of(ref, room(5, 0.5), 300)
        a = 2 * FS                                 # the mic window [a, a + FS)
        lag, conf = AEC.estimate_lag(echo[a:a + FS], ref[a - M:a + FS], M)
        self.assertGreater(conf, AEC.PROBE_MIN_PEAK)
        self.assertLess(abs(lag - 300 * 48), 2 * 48)
        lag2, conf2 = AEC.estimate_lag(speechlike(FS, 8), speechlike(FS + int(0.6 * FS), 4),
                                       int(0.6 * FS))
        self.assertLess(conf2, AEC.PROBE_MIN_PEAK)


if __name__ == "__main__":
    unittest.main()
