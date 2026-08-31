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

import time
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


class _FakeHid:
    """The hidapi handle underneath `ds5bridge.device.DualSense`."""

    def __init__(self):
        self.feature_writes: list[bytes] = []
        self.feature_reads: list[tuple[int, int]] = []
        #: Queue of answers `get_feature_report` hands back, in order. The last
        #: one repeats once the queue is empty, which is what a controller does
        #: while the host keeps polling a finished command.
        self.answers: list[bytes] = []

    def send_feature_report(self, data):
        self.feature_writes.append(bytes(data))
        return len(data)

    def get_feature_report(self, report_id, length):
        self.feature_reads.append((report_id, length))
        if not self.answers:
            return []
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]


class _FakeDev:
    """Stands in for `ds5bridge.device.DualSense` on the bridge's own seam."""

    def __init__(self, is_bt=True):
        self.h = _FakeHid()
        self.is_bt = is_bt
        self.seq = 0
        self.writes: list[bytes] = []

    def _next_seq(self):
        s = self.seq
        self.seq = (self.seq + 1) & 0x0F
        return s

    def write_raw(self, data):
        self.writes.append(bytes(data))
        return len(data)

    def get_feature(self, report_id, length=64):
        return bytes(self.h.get_feature_report(report_id, length))


@unittest.skipUnless(HAVE_DEPS, "numpy / PyAV / hidapi not available")
class FactoryTestFeatureTests(unittest.TestCase):
    """Phase 4c: the 0x80 -> 0x81 factory-diagnostics channel.

    `_defer` runs inline while no threads are started, so every allowlisted
    command here really does reach the fake device on the calling thread -- the
    same code path the writer thread takes in production.
    """

    #: TELEMETRY / GET_INFO -- the Diagnostics panel's only command.
    TELEMETRY = (0x70, 0x01)
    #: SYSTEM / READ_SERIAL_NUMBER -- a Factory Info read.
    SERIAL = (0x01, 0x13)

    def setUp(self):
        self.be = B.BridgeBackend()
        self.dev = _FakeDev()
        self.be._dev = self.dev
        self.be.connected.set()

    def cmd(self, device_id, action_id, params=b"", with_report_id=False):
        body = bytearray(B.FEATURE_TEST_PAYLOAD_LEN)
        body[0] = device_id
        body[1] = action_id
        body[2:2 + len(params)] = params
        return (bytes([B.FEATURE_TEST_CMD]) + bytes(body)) if with_report_id \
            else bytes(body)

    def answer(self, device_id, action_id, status, payload=b"\xab" * 56):
        buf = bytearray(B.FEATURE_TEST_PAYLOAD_LEN + 1)
        buf[0] = B.FEATURE_TEST_RESULT
        buf[1] = device_id
        buf[2] = action_id
        buf[3] = status
        buf[4:4 + len(payload)] = payload
        return bytes(buf)

    # -- the allowlist ------------------------------------------------------

    def test_an_allowlisted_command_is_forwarded_crc_signed(self):
        self.be.set_feature_report(B.FEATURE_TEST_CMD, self.cmd(*self.TELEMETRY))
        self.assertEqual(len(self.dev.h.feature_writes), 1)
        wire = self.dev.h.feature_writes[0]
        self.assertEqual(wire[0], B.FEATURE_TEST_CMD)
        self.assertEqual(len(wire), B.FEATURE_TEST_PAYLOAD_LEN + 1)
        self.assertEqual((wire[1], wire[2]), self.TELEMETRY)
        # 0x53-seeded CRC32 over the payload, inside the same 63 bytes
        from ds5bridge.crc import SEED_SET_FEATURE, crc32_seeded
        want = crc32_seeded(SEED_SET_FEATURE, B.FEATURE_TEST_CMD, wire[1:-4])
        self.assertEqual(want.to_bytes(4, "little"), wire[-4:])
        self.assertEqual(self.be.stats["feature_test_forwarded"], 1)
        self.assertEqual(self.be.stats["feature_test_blocked"], 0)

    def test_a_command_that_is_not_allowlisted_is_recorded_and_dropped(self):
        # SYSTEM / WRITE_PCBAID. A write, and it must never reach the pad.
        self.be.set_feature_report(B.FEATURE_TEST_CMD, self.cmd(0x01, 0x03))
        self.assertEqual(self.dev.h.feature_writes, [])
        self.assertEqual(self.be.stats["feature_test_blocked"], 1)
        self.assertEqual(self.be.stats["feature_test_forwarded"], 0)
        self.assertEqual(self.be.feature_writes[0][0], B.FEATURE_TEST_CMD)

    #: The one deliberate non-read pair: the tester's 1 kHz sine-wave button.
    #: Nothing in it outlives a power cycle -- see the comment on the allowlist.
    AUDIO_WAVEOUT = {(0x06, 0x02), (0x06, 0x04)}

    def test_the_allowlist_holds_only_reads_and_the_wave_out_pair(self):
        # Every entry names a READ_/GET_ action, except the two AUDIO entries
        # that start and stop the built-in test tone, and none of the ids that
        # could write pairing, flash or calibration is present.
        for (dev_id, action), why in B.TEST_COMMAND_ALLOWLIST.items():
            if (dev_id, action) in self.AUDIO_WAVEOUT:
                continue
            self.assertTrue(
                why.startswith(("READ_", "GET_", "BATTERY", "SOLOMON_")),
                f"{dev_id:#04x}/{action:#04x} -> {why}")
        for banned in ((0x01, 0x03), (0x01, 0x07), (0x09, 0x01), (0x01, 0x70),
                       (0x03, 0x01),
                       # the writing halves of the BUILTIN_MIC_CALIB_DATA family
                       # that the wave-out pair sits next to
                       (0x06, 0x01), (0x06, 0x03)):
            self.assertNotIn(banned, B.TEST_COMMAND_ALLOWLIST)

    def test_the_testers_play_sound_button_reaches_the_controller(self):
        """The exact pair `controlWaveOut()` sends, and it must be forwarded.

        dualsense-tester's speaker/headphone 1 kHz test is two fire-and-forget
        SET_REPORT(Feature, 0x80)s -- `setTestCommandWithParams` never reads
        0x81 back -- so whether the button does anything is decided entirely
        here. Measured 2026-08-27: with these two off the allowlist the button
        was silent through the emulator and worked on the same pad over plain
        Bluetooth. See docs/wired-gap-findings.md.
        """
        params = bytearray(20)
        params[2] = 8  # speaker
        self.be.set_feature_report(B.FEATURE_TEST_CMD, self.cmd(0x06, 0x04, params))
        self.be.set_feature_report(B.FEATURE_TEST_CMD,
                                   self.cmd(0x06, 0x02, b"\x01\x01\x00"))
        self.assertEqual([(w[1], w[2]) for w in self.dev.h.feature_writes],
                         [(0x06, 0x04), (0x06, 0x02)])
        self.assertEqual(self.be.stats["feature_test_blocked"], 0)
        self.assertEqual(self.be.stats["feature_test_forwarded"], 2)

    def test_pairing_and_the_individual_data_channel_stay_blocked(self):
        for rid in (0x09, 0x84):
            self.be.set_feature_report(rid, b"\xde\xad\xbe\xef")
        self.assertEqual(self.dev.h.feature_writes, [])
        self.assertEqual([r for r, _ in self.be.feature_writes], [0x09, 0x84])

    def test_the_report_id_may_or_may_not_be_repeated_in_the_data_stage(self):
        for with_id in (False, True):
            be = B.BridgeBackend()
            be._dev = _FakeDev()
            be.connected.set()
            be.set_feature_report(B.FEATURE_TEST_CMD,
                                  self.cmd(*self.SERIAL, with_report_id=with_id))
            self.assertEqual(be.stats["feature_test_forwarded"], 1)
            self.assertEqual(be._dev.h.feature_writes[0][1:3],
                             bytes(self.SERIAL))

    def test_a_report_the_hid_stack_refuses_is_counted_not_ignored(self):
        # hidapi returns -1 instead of raising when Windows rejects a feature
        # write. Measured on hardware: that is exactly what an unsigned 0x80
        # over Bluetooth does. A refused query that looked sent would leave the
        # host polling 0x81 for a second and reading stale bytes.
        self.dev.h.send_feature_report = lambda data: -1
        self.be.set_feature_report(B.FEATURE_TEST_CMD, self.cmd(*self.SERIAL))
        self.assertEqual(self.be.stats["feature_test_errors"], 1)

    def test_a_report_the_hid_stack_accepts_counts_no_error(self):
        self.be.set_feature_report(B.FEATURE_TEST_CMD, self.cmd(*self.SERIAL))
        self.assertEqual(self.be.stats["feature_test_errors"], 0)

    # -- the 0x80 -> 0x81 round trip ----------------------------------------

    def test_the_round_trip_returns_the_controllers_answer(self):
        self.dev.h.answers = [self.answer(*self.TELEMETRY, 3, b"\x11" * 56)]
        self.be.set_feature_report(B.FEATURE_TEST_CMD, self.cmd(*self.TELEMETRY))
        got = self.be.get_feature_report(B.FEATURE_TEST_RESULT, 64)
        self.assertEqual(got[0], B.FEATURE_TEST_RESULT)
        self.assertEqual((got[1], got[2]), self.TELEMETRY)
        self.assertEqual(got[3], 3)                      # COMPLETE_2
        self.assertEqual(got[4:60], b"\x11" * 56)
        self.assertEqual(self.dev.h.feature_reads, [(B.FEATURE_TEST_RESULT, 64)])

    def test_every_get_of_0x81_is_a_fresh_bluetooth_read(self):
        # Diagnostics is FOUR pages behind one 0x80: caching the first answer
        # would truncate the block to a quarter of itself.
        pages = [self.answer(*self.TELEMETRY, 3, bytes([i]) * 56) for i in range(4)]
        self.dev.h.answers = list(pages)
        self.be.set_feature_report(B.FEATURE_TEST_CMD, self.cmd(*self.TELEMETRY))
        seen = [self.be.get_feature_report(B.FEATURE_TEST_RESULT, 64)[4]
                for _ in range(4)]
        self.assertEqual(seen, [0, 1, 2, 3])
        self.assertEqual(len(self.dev.h.feature_reads), 4)

    # -- 0x81 answers idle; it never STALLs --------------------------------
    #
    # MEASURED on a physical wired DualSense 2026-08-27
    # (emulator/tools/hid_diff_probe.py): a cold GET_REPORT(Feature, 0x81)
    # returns 64 bytes of `81 00 00 ...`. It does not STALL, ever. That matters
    # because dualsense-tester polls 0x81 in
    # `while (report = await receiveFeatureReport(item, 0x81))` -- a STALL
    # throws straight out of the loop.

    def idle(self, got):
        self.assertEqual(got, B.FEATURE_TEST_IDLE)
        self.assertEqual(len(got), B.FEATURE_TEST_PAYLOAD_LEN + 1)
        self.assertEqual(got[0], B.FEATURE_TEST_RESULT)
        self.assertEqual(set(got[1:]), {0})

    def test_a_get_of_0x81_with_nothing_armed_answers_idle(self):
        self.idle(self.be.get_feature_report(B.FEATURE_TEST_RESULT, 64))
        # ... without going near the controller for a host that is just probing
        self.assertEqual(self.dev.h.feature_reads, [])
        self.assertEqual(self.be.feature_misses.get(B.FEATURE_TEST_RESULT, 0), 0)

    def test_a_blocked_command_does_not_arm_the_result_read(self):
        self.be.set_feature_report(B.FEATURE_TEST_CMD, self.cmd(0x01, 0x03))
        self.idle(self.be.get_feature_report(B.FEATURE_TEST_RESULT, 64))
        self.assertEqual(self.dev.h.feature_reads, [])

    def test_nothing_is_forwarded_while_the_link_is_down(self):
        self.be.connected.clear()
        self.be.set_feature_report(B.FEATURE_TEST_CMD, self.cmd(*self.TELEMETRY))
        self.assertEqual(self.dev.h.feature_writes, [])
        self.idle(self.be.get_feature_report(B.FEATURE_TEST_RESULT, 64))

    def test_a_slow_bluetooth_read_falls_back_to_idle_not_a_stall(self):
        # Threads "running" but nothing draining the queue: the job is posted
        # and never runs, which is exactly what a wedged writer thread looks
        # like. The USB request path must give up, not hang -- and give up the
        # way the hardware does, with an idle header rather than a STALL.
        self.be._threads = ["pretend the backend is started"]
        self.be._test_armed = self.TELEMETRY
        self.be.feature_timeout = 0.02
        started = time.perf_counter()
        self.idle(self.be.get_feature_report(B.FEATURE_TEST_RESULT, 64))
        self.assertLess(time.perf_counter() - started, 1.0)
        self.assertEqual(self.be.stats["feature_bt_timeouts"], 1)

    def test_a_raising_bluetooth_read_falls_back_to_idle(self):
        def boom(*_a, **_k):
            raise OSError("read error")

        self.dev.h.get_feature_report = boom
        self.be._test_armed = self.TELEMETRY
        self.idle(self.be.get_feature_report(B.FEATURE_TEST_RESULT, 64))
        self.assertEqual(self.be.stats["feature_test_errors"], 1)

    def test_an_empty_answer_is_idle_rather_than_junk(self):
        self.be._test_armed = self.TELEMETRY
        self.idle(self.be.get_feature_report(B.FEATURE_TEST_RESULT, 64))

    # -- the plain GET-only prefetch ----------------------------------------

    def test_bt_patch_info_0x22_is_on_the_prefetch_list(self):
        self.assertIn(0x22, B.PREFETCH_FEATURES)
        self.assertIn(0x20, B.PREFETCH_FEATURES)

    def test_the_mac_report_0x09_is_prefetched(self):
        """The one a libScePad title reads before it will accept the pad.

        WujekFoliarz/duaLib, the open-source libScePad, puts the entire
        registration of a controller inside `if (getMacAddress(...))`
        (src/source/duaLib.cpp:145), and `getMacAddress` for a DualSense is one
        `hid_get_feature_report` of 0x09 (src/source/duaLibUtils.cpp:182-199).
        Measured 2026-08-27: a real wired unit answers 0x09 with 20 bytes; this
        emulator used to STALL it, and a STALL means the pad is never
        registered and the game sees no input at all.
        """
        self.assertIn(0x09, B.PREFETCH_FEATURES)
        self.assertIn(0x0B, B.PREFETCH_FEATURES)

    # -- making a Bluetooth-sourced feature report look like a wired one ----

    def test_the_bluetooth_crc_trailer_is_zeroed(self):
        # A wired DualSense ends 0x05/0x09/0x0b/0x20/0x22 in four zero bytes;
        # the Bluetooth unit ends them in a CRC-32. Measured, both, 2026-08-27.
        # 0x09 also carries the controller MAC, which is separately derived --
        # see test_the_served_mac_is_never_the_real_mac -- so it is exercised
        # there, not here.
        for rid in (0x05, 0x20, 0x22):
            raw = bytes([rid]) + b"\x5a" * 35 + b"\xde\xad\xbe\xef"
            got = self.be._feature_bytes(rid, raw)
            self.assertEqual(len(got), len(raw), f"0x{rid:02x} changed length")
            self.assertEqual(got[-4:], b"\x00\x00\x00\x00")
            self.assertEqual(got[:-4], raw[:-4])

    def test_a_report_with_no_crc_trailer_is_left_alone(self):
        raw = bytes([0x81]) + b"\x5a" * 63
        self.assertEqual(self.be._feature_bytes(0x81, raw), raw)

    def test_0x0b_keeps_the_two_macs_and_drops_the_link_key(self):
        # Bluetooth 0x0b carries a pairing-slot count and link-key material
        # after the host MAC, where a wired unit publishes zeros. Serving the
        # Bluetooth bytes verbatim would differ from the ground truth AND hand
        # the pairing material to anything that can open the HID device.
        raw = (bytes([0x0B]) + b"\x11" * 6 + b"\x08\x25\x00\x00" + b"\x22" * 6
               + b"\x99" * 25)
        got = self.be._feature_bytes(0x0B, raw)
        self.assertEqual(len(got), len(raw))
        self.assertEqual(got[1:6], b"\x11" * 5)      # controller MAC kept ...
        self.assertEqual(got[6], 0x11 | B.WIRED_MAC_LA_BIT)  # ... but derived
        self.assertEqual(got[7:11], b"\x08\x25\x00\x00")
        self.assertEqual(got[11:17], b"\x22" * 6)    # host MAC kept, in full
        self.assertEqual(set(got[B.FEATURE_0B_WIRED_PREFIX:]), {0})

    def test_the_served_mac_is_never_the_real_mac(self):
        """The fix for the TLOU power-off (2026-08-31, see WIRED_MAC_LA_BIT).

        libScePad de-duplicates pads by the MAC in feature 0x09, and its
        response to "this wired pad IS that Bluetooth pad" is to power the
        Bluetooth one off, exactly as a PS5 does when the cable goes in.
        Captured live: TLOU reads 0x09 off the virtual pad and 26 ms later the
        physical pad's link is dead, killed through the game's own handle --
        no write of ours in between. So the identity served over the virtual
        cable must differ from the identity on the air, while remaining stable
        and per-controller: the locally-administered bit of the first MAC
        octet (byte [6]; the MAC is little-endian at [1..6]).
        """
        for rid in (0x09, 0x0B):
            raw = bytes([rid]) + b"\x11" * 6 + b"\x5a" * 30 + bytes(4)
            got = self.be._feature_bytes(rid, raw)
            self.assertNotEqual(got[1:7], raw[1:7],
                                f"0x{rid:02x} served the real MAC")
            self.assertEqual(got[6], 0x11 | B.WIRED_MAC_LA_BIT)
            self.assertEqual(got[1:6], raw[1:6])   # only the one bit differs

    def test_distinct_wired_mac_false_serves_the_real_mac(self):
        # The opt-out exists for A/B-ing the bug; it re-enables a proven kill
        # and must stay off in production.
        be = B.BridgeBackend(distinct_wired_mac=False)
        raw = bytes([0x09]) + b"\x11" * 6 + b"\x5a" * 9 + bytes(4)
        self.assertEqual(be._feature_bytes(0x09, raw)[1:7], raw[1:7])

    def test_the_mac_bit_is_only_applied_to_the_mac_reports(self):
        # 0x20/0x22 also contain the BD address (deeper in), but nothing
        # observed de-duplicates on them; they stay byte-faithful.
        raw = bytes([0x20]) + b"\x11" * 35 + bytes(4)
        got = self.be._feature_bytes(0x20, raw)
        self.assertEqual(got[1:7], raw[1:7])

    def test_the_report_id_is_prepended_when_hidapi_omits_it(self):
        got = self.be._feature_bytes(0x81, b"\x00" * 63)
        self.assertEqual(got[0], 0x81)
        self.assertEqual(len(got), 64)

    def test_a_prefetched_report_is_served_from_the_cache(self):
        self.be._features[0x22] = bytes([0x22]) + b"\x5a" * 63
        got = self.be.get_feature_report(0x22, 64)
        self.assertEqual(len(got), 64)
        self.assertEqual(got[0], 0x22)
        self.assertEqual(self.be.feature_misses.get(0x22, 0), 0)


@unittest.skipUnless(HAVE_DEPS, "numpy / PyAV / hidapi not available")
class SetStateCoalescingTests(unittest.TestCase):
    """Phase 4c: why the player LEDs stayed dark.

    `write_output_report` used to REPLACE an unsent SetState body. Games set the
    player-LED bits once and then stream rumble reports without the
    player-indicator valid flag, so that one report only had to lose one race
    against `SETSTATE_MIN_INTERVAL` to be gone for the whole session.
    """

    def leds(self, mask=0x01):
        from ds5bridge import protocol as P
        return P.build_usb_setstate(bytes(P.SetState().player_leds(mask).body))

    def rumble(self, left=0x40, right=0x80):
        from ds5bridge import protocol as P
        return P.build_usb_setstate(bytes(P.SetState().rumble(left, right).body))

    def setUp(self):
        self.be = B.BridgeBackend()

    def test_a_pending_led_report_is_merged_not_overwritten(self):
        from ds5bridge import protocol as P

        self.be.write_output_report(self.leds(0x04))
        self.be.write_output_report(self.rumble())
        pending = self.be._setstate_pending
        self.assertEqual(pending[P.PLAYER_INDICATOR], 0x04)
        self.assertTrue(pending[P.VALID_FLAG1] & P.F1_PLAYER_INDICATOR)
        self.assertEqual(pending[P.BC_VIBRATION_LEFT], 0x40)
        self.assertEqual(self.be.stats["setstate_merged"], 1)
        self.assertEqual(self.be.stats["setstate_in"], 2)

    def test_a_whole_burst_of_rumble_never_erases_the_leds(self):
        from ds5bridge import protocol as P

        self.be.write_output_report(self.leds(0x1F))
        for i in range(50):
            self.be.write_output_report(self.rumble(i, i))
        self.assertEqual(self.be._setstate_pending[P.PLAYER_INDICATOR], 0x1F)
        self.assertEqual(self.be.stats["setstate_merged"], 50)

    def test_the_first_report_is_still_stored_verbatim(self):
        from ds5bridge import protocol as P

        st = P.SetState().lightbar(1, 2, 3)
        self.be.write_output_report(P.build_usb_setstate(bytes(st.body)))
        self.assertEqual(self.be._setstate_pending, bytes(st.body))
        self.assertEqual(self.be.stats["setstate_merged"], 0)

    # -- pre-connect buffering and replay -----------------------------------

    def test_the_host_state_accumulates_across_sends(self):
        from ds5bridge import protocol as P

        self.be.write_output_report(self.leds(0x02))
        self.be._setstate_pending = None          # pretend the writer sent it
        self.be.write_output_report(self.rumble())
        host = self.be._setstate_host
        self.assertEqual(host[P.PLAYER_INDICATOR], 0x02)
        self.assertEqual(host[P.BC_VIBRATION_LEFT], 0x40)

    def test_the_host_state_is_replayed_after_a_connect(self):
        from ds5emu import translate as T
        from ds5bridge import protocol as P

        dev = _FakeDev()
        self.be.write_output_report(self.leds(0x08))
        self.be._dev = dev
        self.be._prime_setstate()
        # audio routing first, then the host's own state
        self.assertEqual(len(dev.writes), 2)
        primed = T.bt31_output_body(dev.writes[0])
        self.assertFalse(primed[P.VALID_FLAG1] & P.F1_PLAYER_INDICATOR)
        replayed = T.bt31_output_body(dev.writes[1])
        self.assertEqual(replayed[P.PLAYER_INDICATOR], 0x08)
        self.assertTrue(replayed[P.VALID_FLAG1] & P.F1_PLAYER_INDICATOR)
        self.assertTrue(T.verify_bt_output_crc(dev.writes[1]))
        self.assertEqual(self.be.stats["setstate_replayed"], 1)

    def test_nothing_is_replayed_when_the_host_has_said_nothing(self):
        dev = _FakeDev()
        self.be._dev = dev
        self.be._prime_setstate()
        self.assertEqual(len(dev.writes), 1)
        self.assertEqual(self.be.stats["setstate_replayed"], 0)

    def test_the_lightbar_fade_out_prime_is_off_by_default(self):
        # It is wired up as the last once-per-connect candidate if a real pad
        # still refuses to light its player LEDs, but the lightbar demonstrably
        # already obeys us, so it stays off.
        self.assertFalse(B.PRIME_LIGHTBAR_FADE_OUT)


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


@unittest.skipUnless(HAVE_DEPS, "numpy / PyAV / hidapi not available")
class LinkLossTests(unittest.TestCase):
    """Phase 4a: what the interrupt endpoint answers while the link is down.

    No hardware: the reader thread's only contribution to this path is
    `_latest_input` + `_latest_input_at`, so setting those directly reproduces
    "a report arrived, then the controller went away" exactly.
    """

    HELD = 0x20   # digital_keys bit for X, i.e. a button held at the moment of loss

    def held_report(self) -> bytes:
        from ds5emu import translate as T
        from ds5bridge import protocol as P
        body = bytearray(T.USB_INPUT_BODY_LEN)
        body[P.OFFSETS_USB.stick_lx] = 0xFF          # stick shoved fully right
        body[P.OFFSETS_USB.digital_keys] = self.HELD | 8
        body[P.OFFSETS_USB.status0] = 0x08           # 80 %, discharging
        return bytes([T.USB_INPUT_ID]) + bytes(body)

    def backend(self, age_s: float):
        import time
        be = B.BridgeBackend()
        be._latest_input = self.held_report()
        be._latest_input_at = time.perf_counter() - age_s
        be._input_serial = 1
        be._delivered_serial = 1     # nothing new since -> the repeat path
        return be

    def decode(self, report):
        from ds5bridge import protocol as P
        return P.decode_input(report[1:], usb=True)

    def test_a_fresh_stale_repeat_keeps_the_held_state(self):
        be = self.backend(age_s=0.0)
        st = self.decode(be.read_input_report(64))
        self.assertTrue(st.buttons["x"])
        self.assertEqual(st.lx, 0xFF)
        self.assertEqual(be.stats["input_repeated"], 1)
        self.assertEqual(be.stats["input_neutral"], 0)

    def test_past_the_threshold_the_repeat_is_neutralised(self):
        be = self.backend(age_s=B.INPUT_NEUTRAL_S + 0.5)
        st = self.decode(be.read_input_report(64))
        self.assertFalse(st.buttons["x"])
        self.assertEqual(st.lx, 0x80)
        self.assertEqual(be.stats["input_neutral"], 1)
        # ...and the battery the game reads is still the real one
        self.assertEqual(st.battery_level, 8)

    def test_neutralising_never_stops_the_endpoint_answering(self):
        # The device must keep delivering at 250 Hz: a virtual wired DualSense
        # that goes silent is a *removed* controller as far as a game is
        # concerned, which is the outcome this whole policy exists to avoid.
        be = self.backend(age_s=5.0)
        for _ in range(10):
            self.assertIsNotNone(be.read_input_report(64))
        self.assertEqual(be.stats["input_none"], 0)
        self.assertEqual(be.stats["input_neutral"], 10)

    def test_a_new_report_ends_the_neutral_state_immediately(self):
        import time
        be = self.backend(age_s=5.0)
        self.decode(be.read_input_report(64))
        be._latest_input = self.held_report()
        be._latest_input_at = time.perf_counter()
        be._input_serial += 1
        st = self.decode(be.read_input_report(64))
        self.assertTrue(st.buttons["x"])
        self.assertEqual(be.stats["input_neutral"], 1)

    def test_device_status_decodes_battery_and_staleness(self):
        be = self.backend(age_s=2.0)
        st = be.device_status()
        self.assertEqual(st["battery_percent"], 80)
        self.assertEqual(st["battery_state"], "discharging")
        self.assertFalse(st["connected"])
        self.assertGreater(st["stale_s"], 1.5)

    def test_device_status_before_anything_arrives(self):
        st = B.BridgeBackend().device_status()
        self.assertIsNone(st["battery_percent"])
        self.assertIsNone(st["stale_s"])
        self.assertFalse(st["connected"])

    def test_force_disconnect_is_safe_with_no_device_open(self):
        be = B.BridgeBackend()
        be.connected.set()
        be.force_disconnect()
        self.assertFalse(be.connected.is_set())
        self.assertEqual(be.stats["disconnects"], 1)


if __name__ == "__main__":
    unittest.main()
