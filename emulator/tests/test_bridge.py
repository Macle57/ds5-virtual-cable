"""Phase 3b: BridgeBackend internals, with no controller attached.

`BridgeBackend.__init__` deliberately touches no hardware — `start()` opens the
device — so everything except the three threads can be tested here. What is
covered is the arithmetic and the buffering that a live run cannot easily prove
was *right* rather than merely *working*: the exact rate ratios, the ring
overflow policy, and the fact that `read_audio_in` can never return short.

Skipped when numpy / PyAV / hidapi are missing, so the stdlib-only part of the
suite still runs anywhere.
"""

from __future__ import annotations

import unittest

try:
    from ds5emu import bridge as B
    from ds5bridge import audio as A
    HAVE_DEPS = True
except Exception as exc:  # noqa: BLE001
    HAVE_DEPS = False
    _WHY = str(exc)


@unittest.skipUnless(HAVE_DEPS, "numpy / PyAV / hidapi not available")
class RateArithmeticTests(unittest.TestCase):
    """The whole audio path rests on two integer ratios. Assert them."""

    def test_one_0x39_report_is_exactly_1024_usb_samples(self):
        # 2 frames x 10.6667 ms = 21.3333 ms = 1024 samples at 48 kHz
        self.assertAlmostEqual(B.REPORT_39_MS, 1024 * 1000 / 48000, places=9)

    def test_48k_to_45k_and_48k_to_3k_are_integer_ratios(self):
        self.assertEqual(48000 * 15, 45000 * 16)
        self.assertEqual(1024 * 45000 // 48000, 2 * A.OPUS_SAMPLES_PER_FRAME)   # 960
        self.assertEqual(1024 * 3000 // 48000, 2 * A.HAPTIC_SAMPLES_PER_FRAME)  # 64

    def test_packet_sizes_match_the_ground_truth_endpoints(self):
        self.assertEqual(B.USB_OUT_FRAME_BYTES * 48, 384)   # 1 ms of iso OUT
        self.assertEqual(B.USB_IN_FRAME_BYTES * 48, 192)    # 1 ms of iso IN

    def test_audio_buffer_length_is_inside_the_mandatory_range(self):
        # Out-of-range values make the controller discard every 0x39 report
        # with no error at all (docs/FINDINGS.md banner item 5).
        self.assertGreaterEqual(B.AUDIO_BUFFER_LENGTH, 16)
        self.assertLessEqual(B.AUDIO_BUFFER_LENGTH, 128)


@unittest.skipUnless(HAVE_DEPS, "numpy / PyAV / hidapi not available")
class ByteRingTests(unittest.TestCase):
    def test_overflow_drops_the_oldest_not_the_newest(self):
        r = B._ByteRing(8)
        r.write(b"01234567")
        r.write(b"89")
        self.assertEqual(r.take_all(), b"23456789")
        self.assertEqual(r.dropped, 2)

    def test_read_exact_never_returns_short(self):
        r = B._ByteRing(100)
        r.write(b"abc")
        self.assertEqual(r.read_exact(6), b"abc\0\0\0")
        self.assertEqual(r.underruns, 1)
        self.assertEqual(len(r), 0)

    def test_take_all_with_a_limit_leaves_the_remainder(self):
        r = B._ByteRing(100)
        r.write(b"abcdef")
        self.assertEqual(r.take_all(2), b"ab")
        self.assertEqual(r.take_all(), b"cdef")


@unittest.skipUnless(HAVE_DEPS, "numpy / PyAV / hidapi not available")
class ResamplerAndEncoderTests(unittest.TestCase):
    def _feed(self, rs, blocks=200, n=1024):
        """Return output samples per input sample.

        The measurement converges rather than being exact from the first block:
        swresample holds a fixed number of samples in its filter, so a short
        feed always reads slightly low. Feeding ~4 s amortises that to well
        under a thousandth, which is what makes the assertion meaningful.
        """
        import numpy as np

        total = 0
        for i in range(blocks):
            t = (np.arange(n) + i * n) / 48000.0
            x = np.stack([np.sin(2 * np.pi * 400 * t)] * 2, axis=1).astype(np.float32)
            total += rs.push(x * 0.5).shape[0]
        return total / (blocks * n)

    def test_speaker_resampler_converges_on_15_over_16(self):
        self.assertAlmostEqual(self._feed(B._StreamResampler(45000)), 0.9375, places=3)

    def test_haptic_resampler_converges_on_1_over_16(self):
        self.assertAlmostEqual(self._feed(B._StreamResampler(3000)), 0.0625, places=3)

    def test_encoder_emits_only_200_byte_frames(self):
        import numpy as np

        enc = B._StreamOpusEncoder()
        frames = []
        for i in range(12):
            t = (np.arange(480) + i * 480) / 45000.0
            x = np.stack([np.sin(2 * np.pi * 1000 * t)] * 2, axis=1).astype(np.float32)
            frames += enc.push(x * 0.5)
        self.assertGreater(len(frames), 8)
        self.assertEqual({len(f) for f in frames}, {A.OPUS_FRAME_BYTES})
        self.assertEqual(enc.short_packets, 0)


@unittest.skipUnless(HAVE_DEPS, "numpy / PyAV / hidapi not available")
class BackendWithoutHardwareTests(unittest.TestCase):
    """Everything `start()` does not require. No device is ever opened."""

    def setUp(self):
        self.be = B.BridgeBackend()

    def test_only_report_0x39_is_implemented(self):
        with self.assertRaises(ValueError):
            B.BridgeBackend(report_id=0x36)

    def test_input_is_none_until_bluetooth_delivers_something(self):
        self.assertIsNone(self.be.read_input_report(64))
        self.assertIsNone(self.be.latest_input_report(64))

    def test_stale_input_is_repeated_but_only_after_a_first_report(self):
        from ds5emu import translate as T
        from ds5bridge import protocol as P

        payload = bytearray(T.BT_INPUT_PAYLOAD_LEN)
        payload[0] = P.PAYLOAD_TYPE_CONTROL
        payload[P.OFFSETS_BT.stick_lx] = 0x42
        self.be._latest_input = T.bt31_payload_to_usb01(bytes(payload))
        self.be._input_serial = 1

        first = self.be.read_input_report(64)
        second = self.be.read_input_report(64)
        self.assertIsNotNone(second)
        self.assertEqual(self.be.stats["input_repeated"], 1)
        # same state, but the device sequence byte advanced by exactly one
        self.assertEqual(first[1 + T.USB_SEQ_OFFSET] + 1,
                         second[1 + T.USB_SEQ_OFFSET])
        self.assertEqual(first[1 + P.OFFSETS_USB.stick_lx], 0x42)

        self.be.repeat_stale_input = False
        self.assertIsNone(self.be.read_input_report(64))

    def test_latest_input_report_does_not_consume(self):
        from ds5emu import translate as T
        from ds5bridge import protocol as P

        payload = bytearray(T.BT_INPUT_PAYLOAD_LEN)
        payload[0] = P.PAYLOAD_TYPE_CONTROL
        self.be._latest_input = T.bt31_payload_to_usb01(bytes(payload))
        self.be._input_serial = 1
        self.assertIsNotNone(self.be.latest_input_report(64))
        self.assertEqual(self.be.stats["input_repeated"], 0)

    def test_output_report_is_queued_as_the_raw_setstate_body(self):
        from ds5emu import translate as T
        from ds5bridge import protocol as P

        st = P.SetState().lightbar(1, 2, 3)
        self.be.write_output_report(P.build_usb_setstate(bytes(st.body)))
        self.assertEqual(self.be._setstate_pending, bytes(st.body))
        self.assertTrue(self.be._setstate_event.is_set())
        # and the wire form is exactly what Phase 1 sent
        self.assertEqual(T.usb02_to_bt31(bytes(st.body), 0),
                         P.build_bt_setstate(bytes(st.body), 0))

    def test_iso_in_always_returns_exactly_what_was_asked_for(self):
        for n in (0, 4, 192, 196, 1536):
            self.assertEqual(len(self.be.read_audio_in(n)), n)

    def test_iso_out_fills_the_ring_and_auto_arms(self):
        self.be.write_audio_out(b"\x00" * 384)
        self.assertTrue(self.be._audio_armed.is_set())
        self.assertEqual(len(self.be._out_ring), 384)
        self.assertEqual(self.be.stats["audio_out_bytes"], 384)

    def test_set_alt_setting_arms_and_disarms_the_streams(self):
        from ds5emu import descriptors as D

        self.be.set_alt_setting(D.IFACE_AUDIO_OUT, 1)
        self.be.set_alt_setting(D.IFACE_AUDIO_IN, 1)
        self.assertTrue(self.be._audio_armed.is_set())
        self.assertTrue(self.be._mic_armed.is_set())
        self.be.set_alt_setting(D.IFACE_AUDIO_OUT, 0)
        self.be.set_alt_setting(D.IFACE_AUDIO_IN, 0)
        self.assertFalse(self.be._audio_armed.is_set())
        self.assertFalse(self.be._mic_armed.is_set())

    def test_feature_writes_are_recorded_and_never_forwarded(self):
        # 0x09 is the pairing report. Nothing may ever push one at the
        # physical controller, whatever the host asks for.
        self.be.set_feature_report(0x09, b"\xde\xad\xbe\xef")
        self.assertEqual(self.be.feature_writes, [(0x09, b"\xde\xad\xbe\xef")])

    def test_unknown_feature_reads_stall_and_are_counted(self):
        self.assertIsNone(self.be.get_feature_report(0x22, 64))
        self.assertEqual(self.be.feature_misses[0x22], 1)

    def test_cached_feature_reads_are_truncated_to_wlength(self):
        self.be._features[0x05] = bytes([0x05]) + b"\xaa" * 40
        self.assertEqual(len(self.be.get_feature_report(0x05, 64)), 41)
        self.assertEqual(len(self.be.get_feature_report(0x05, 8)), 8)

    def test_uac_volume_maps_monotonically_onto_the_dualsense_knob(self):
        quiet = B._uac_db_to_byte(-96 * 256)
        mid = B._uac_db_to_byte(-20 * 256)
        loud = B._uac_db_to_byte(0)
        self.assertLess(quiet, mid)
        self.assertLess(mid, loud)
        self.assertLessEqual(loud, 255)
        self.assertGreaterEqual(quiet, 0)


@unittest.skipUnless(HAVE_DEPS, "numpy / PyAV / hidapi not available")
class ClockSeamTests(unittest.TestCase):
    """Phase 3c: where the USB frame clock meets the Bluetooth pacer.

    3a made the emulator the audio clock (`timing.FrameClock`, 1 ms service
    intervals); 3b paced the Bluetooth side with its own 21.3 ms tick. These
    tests pin down the three things that make them compose: the rate identity
    that proves they are one oscillator and not two, the bounded buffering
    between them, and the rule that no Bluetooth I/O may run on the URB path.
    """

    def setUp(self):
        self.be = B.BridgeBackend()

    # -- U <-> B: one oscillator, no drift ---------------------------------

    def test_the_pump_is_a_divider_of_the_usb_frame_clock_not_a_second_clock(self):
        # 1 tick of the pump must consume exactly the audio 1024 ticks of the
        # 1 kHz USB frame clock produce. If this is not exact, the two domains
        # drift and no buffer size saves you.
        reports_per_s = 1000.0 / B.REPORT_39_MS
        self.assertAlmostEqual(reports_per_s, 46.875, places=9)
        self.assertAlmostEqual(reports_per_s * 1024, B.USB_RATE, places=6)
        # and one USB millisecond is a whole number of frames on both endpoints
        self.assertEqual(B.USB_RATE % 1000, 0)

    def test_one_pump_tick_consumes_a_whole_number_of_usb_iso_packets(self):
        from ds5emu import descriptors as D

        # 21.3333 ms of iso OUT is 1024 frames = 8192 B and the host delivers
        # 384 B/ms, so ONE tick is 21 and a third packets: the residue is real
        # and the pump's `tail` carry is mandatory, not decorative.
        per_tick = 1024 * B.USB_OUT_FRAME_BYTES
        self.assertEqual(per_tick, 8192)
        self.assertNotEqual(per_tick % D.ISO_OUT_BYTES_PER_MS, 0)
        # But the phase relationship is periodic and short: three ticks are
        # exactly 64 ms = 64 whole packets, so the residue never accumulates.
        self.assertEqual(3 * per_tick % D.ISO_OUT_BYTES_PER_MS, 0)
        self.assertEqual(3 * per_tick // D.ISO_OUT_BYTES_PER_MS, 64)

    # -- bounded buffering --------------------------------------------------

    def test_the_out_ring_is_a_hard_latency_ceiling(self):
        # 120 ms and not one byte more, whatever the host does.
        self.assertEqual(B.AUDIO_OUT_RING_BYTES,
                         120 * 48 * B.USB_OUT_FRAME_BYTES)
        for _ in range(500):                       # 500 ms of audio, all at once
            self.be.write_audio_out(b"\x11" * 384)
        self.assertEqual(len(self.be._out_ring), B.AUDIO_OUT_RING_BYTES)
        self.assertGreater(self.be._out_ring.dropped, 0)
        # newest data survives, oldest is gone
        self.assertEqual(self.be._out_ring.take_all(1), b"\x11")

    def test_the_encoded_queue_target_is_below_its_ceiling(self):
        self.assertLess(B.AUDIO_Q_TARGET_FRAMES, B.AUDIO_Q_HIGH_FRAMES)
        # the ceiling has to stay under the out-ring's latency budget or the
        # two limits fight each other
        self.assertLess(B.AUDIO_Q_HIGH_FRAMES * 10.667, B.AUDIO_OUT_RING_MS)

    # -- C -> U: the one real drift path ------------------------------------

    def test_mic_governor_bands_are_ordered_and_inside_the_ring(self):
        self.assertLess(B.MIC_LOW_BYTES, B.MIC_PRIME_BYTES)
        self.assertLess(B.MIC_PRIME_BYTES, B.MIC_HIGH_BYTES)
        self.assertLess(B.MIC_HIGH_BYTES, B.MIC_RING_BYTES)

    def test_mic_governor_does_nothing_inside_the_band(self):
        self.assertEqual(self.be._mic_depth_correction(B.MIC_PRIME_BYTES), 0)
        self.assertEqual(self.be.stats["mic_skew_pad_frames"], 0)
        self.assertEqual(self.be.stats["mic_skew_drop_frames"], 0)

    def test_mic_governor_pads_one_frame_when_starved(self):
        self.assertEqual(self.be._mic_depth_correction(0), B.USB_IN_FRAME_BYTES)
        self.assertEqual(self.be.stats["mic_skew_pad_frames"], 1)

    def test_mic_governor_drops_one_frame_when_overfull(self):
        self.be._mic_ring.write(b"\xaa" * (B.MIC_HIGH_BYTES + 400))
        depth = len(self.be._mic_ring)
        self.assertEqual(self.be._mic_depth_correction(depth), 0)
        self.assertEqual(self.be.stats["mic_skew_drop_frames"], 1)
        self.assertEqual(len(self.be._mic_ring), depth - B.USB_IN_FRAME_BYTES)
        # a deliberate skew correction is NOT an overflow
        self.assertEqual(self.be._mic_ring.dropped, 0)
        self.assertEqual(self.be._mic_ring.skew_dropped, B.USB_IN_FRAME_BYTES)

    def test_correction_authority_is_at_most_one_frame_per_call(self):
        # 1000 calls/s x 1 frame = +/-2 %, ~200x the worst crystal error, and
        # small enough that it can never be a jump the ear can hear.
        self.be._mic_ring.write(b"\xaa" * B.MIC_RING_BYTES)
        before = len(self.be._mic_ring)
        self.be._mic_depth_correction(before)
        self.assertEqual(before - len(self.be._mic_ring), B.USB_IN_FRAME_BYTES)

    def test_read_audio_in_never_returns_short_through_the_governor(self):
        # starved: pads. primed then drained: pads. overfull: drops. All 192.
        for depth_bytes in (0, B.MIC_PRIME_BYTES + 8, B.MIC_HIGH_BYTES + 800):
            be = B.BridgeBackend()
            be._mic_ring.write(b"\x7f" * depth_bytes)
            for _ in range(20):
                self.assertEqual(len(be.read_audio_in(192)), 192)

    def test_mic_underrun_forces_a_reprime_rather_than_a_dribble(self):
        self.be._mic_ring.write(b"\x01" * (B.MIC_PRIME_BYTES + 192))
        self.be.read_audio_in(192)
        self.assertTrue(self.be._mic_primed)
        for _ in range(40):                       # drain past the end
            self.be.read_audio_in(192)
        self.assertFalse(self.be._mic_primed)
        self.assertGreaterEqual(self.be.stats["mic_reprimes"], 1)

    # -- no Bluetooth I/O on the URB path -----------------------------------

    def test_control_work_is_deferred_when_the_threads_are_running(self):
        # `_arm_mic_now` sleeps 20 ms twice inside hidapi. On the asyncio
        # server thread that is a 40 ms stall of every endpoint, isochronous
        # included, at exactly the moment the host opens the stream.
        from ds5emu import descriptors as D

        self.be._threads = ["pretend the backend is started"]
        self.be.set_alt_setting(D.IFACE_AUDIO_IN, 1)
        self.assertTrue(self.be._mic_armed.is_set())   # flag flips at once
        self.assertEqual(len(self.be._control_q), 1)   # I/O did not
        self.assertTrue(self.be._setstate_event.is_set())

    def test_uac_control_defers_its_bluetooth_write(self):
        from ds5emu import descriptors as D
        from ds5emu.uac import FU_VOLUME_CONTROL

        self.be._threads = ["pretend the backend is started"]
        self.be.on_uac_control(D.UNIT_FU_SPEAKER, FU_VOLUME_CONTROL, -20 * 256)
        self.assertEqual(len(self.be._control_q), 1)

    def test_the_control_queue_is_bounded(self):
        self.be._threads = ["pretend the backend is started"]
        for _ in range(500):
            self.be._post_control(lambda: None)
        self.assertLessEqual(len(self.be._control_q), self.be._control_q.maxlen)
        self.assertGreater(self.be.stats["control_jobs_dropped"], 0)

    def test_draining_the_control_queue_runs_the_jobs_once(self):
        seen = []
        self.be._threads = ["pretend the backend is started"]
        self.be._post_control(seen.append, "a")
        self.be._post_control(seen.append, "b")
        self.be._drain_control_q()
        self.assertEqual(seen, ["a", "b"])
        self.assertEqual(len(self.be._control_q), 0)
        self.assertEqual(self.be.stats["control_jobs"], 2)

    def test_a_failing_control_job_does_not_kill_the_writer_thread(self):
        def boom():
            raise RuntimeError("bluetooth went away")

        self.be._threads = ["pretend the backend is started"]
        self.be._post_control(boom)
        self.be._drain_control_q()               # must not raise
        self.assertEqual(self.be.stats["control_jobs"], 1)

    def test_control_work_runs_inline_when_nothing_is_started(self):
        # Unit tests and the live harnesses drive the backend directly, with no
        # writer thread to hand the job to.
        from ds5emu import descriptors as D

        self.be.set_alt_setting(D.IFACE_AUDIO_IN, 1)
        self.assertEqual(len(self.be._control_q), 0)
        self.assertTrue(self.be._mic_armed.is_set())

    # -- the seam report ----------------------------------------------------

    def test_seam_report_is_json_able_and_names_both_domains(self):
        import json

        self.be.write_audio_out(b"\x00" * 384)
        self.be.read_audio_in(192)
        s = self.be.clock_seam()
        json.dumps(s)
        for key in ("audio_q_depth_frames", "audio_q_target_frames",
                    "audio_q_drop_frames", "audio_underrun_frames",
                    "out_ring_peak_ms", "out_ring_overflow_bytes",
                    "reports_39", "report_39_errors",
                    "mic_payloads", "mic_decode_errors",
                    "mic_depth_ms_mean", "mic_depth_ms_min", "mic_depth_ms_max",
                    "mic_depth_target_ms", "mic_skew_pad_frames",
                    "mic_skew_drop_frames", "mic_underrun_calls",
                    "mic_reprimes", "mic_ring_overflow_bytes",
                    "control_jobs", "control_jobs_dropped"):
            self.assertIn(key, s)

    def test_seam_report_survives_a_backend_that_never_ran(self):
        # min must not come back as the 1<<30 sentinel
        s = B.BridgeBackend().clock_seam()
        self.assertEqual(s["mic_depth_ms_min"], 0)
        self.assertEqual(s["mic_depth_ms_max"], 0)


if __name__ == "__main__":
    unittest.main()
