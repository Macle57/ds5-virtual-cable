"""Phase 3b: the BT<->USB report translation, tested without any hardware.

These are the tests that stop the input path from silently drifting. They run
in ~30 ms and need no controller, no driver and no third-party package beyond
what `ds5bridge.protocol` already uses (struct + zlib).
"""

from __future__ import annotations

import random
import struct
import unittest

from ds5emu import translate as T

from ds5bridge import protocol as P


def make_bt_payload(seed: int = 0, payload_type: int = P.PAYLOAD_TYPE_CONTROL,
                    seq: int = 0) -> bytes:
    """A 77-byte BT 0x31 payload with every byte distinct-ish and decodable."""
    rng = random.Random(seed)
    p = bytearray(rng.randrange(256) for _ in range(T.BT_INPUT_PAYLOAD_LEN))
    p[0] = ((seq & 0x0F) << 4) | (payload_type & 0x0F)
    # keep the battery nibble inside the documented table so decode is stable
    p[P.OFFSETS_BT.status0] = 0x0A
    return bytes(p)


class OffsetInvariantTests(unittest.TestCase):
    """The whole translation rests on one fact: BT = USB + 1 for every field."""

    FIELDS = (
        "stick_lx", "stick_ly", "stick_rx", "stick_ry", "trigger_l", "trigger_r",
        "sequence_num", "digital_keys", "incremental", "gyro_pitch", "gyro_yaw",
        "gyro_roll", "accel_x", "accel_y", "accel_z", "motion_timestamp",
        "motion_temperature", "touch_data", "at_status0", "at_status1",
        "host_timestamp", "at_status2", "device_timestamp", "status0", "status1",
        "status2", "aes_cmac",
    )

    def test_every_field_is_shifted_by_exactly_one(self):
        for name in self.FIELDS:
            with self.subTest(field=name):
                self.assertEqual(
                    getattr(P.OFFSETS_BT, name) - getattr(P.OFFSETS_USB, name),
                    T.BT_BODY_SHIFT,
                )

    def test_usb_body_fits_inside_the_bt_payload_before_the_crc(self):
        self.assertLessEqual(
            T.BT_BODY_SHIFT + T.USB_INPUT_BODY_LEN, P.OFFSETS_BT.crc32
        )

    def test_sizes_match_the_ground_truth_descriptors(self):
        # HID report descriptor: report 0x01 is 6 + 1 + 4 + 52 = 63 body bytes.
        self.assertEqual(T.USB_INPUT_LEN, 64)
        self.assertEqual(T.BT_INPUT_LEN, 78)
        self.assertEqual(T.SETSTATE_BODY_LEN, 47)


class InputTranslationTests(unittest.TestCase):
    def test_byte_exact_slice(self):
        payload = make_bt_payload(1)
        usb = T.bt31_payload_to_usb01(payload)
        self.assertIsNotNone(usb)
        self.assertEqual(len(usb), 64)
        self.assertEqual(usb[0], 0x01)
        self.assertEqual(usb[1:], payload[1:64])

    def test_accepts_the_full_report_too(self):
        payload = make_bt_payload(2)
        report = bytes([P.BT_INPUT_31]) + payload
        self.assertEqual(T.bt31_report_to_usb01(report),
                         T.bt31_payload_to_usb01(payload))
        self.assertIsNone(T.bt31_report_to_usb01(b"\x01" + payload))

    def test_every_decoded_field_survives(self):
        for seed in range(25):
            payload = make_bt_payload(seed)
            usb = T.bt31_payload_to_usb01(payload)
            with self.subTest(seed=seed):
                self.assertEqual(T.field_parity(payload, usb), [])

    def test_specific_fields_land_where_the_usb_descriptor_says(self):
        p = bytearray(make_bt_payload(3))
        p[P.OFFSETS_BT.stick_lx] = 0x11
        p[P.OFFSETS_BT.stick_ly] = 0x22
        p[P.OFFSETS_BT.trigger_r] = 0xFE
        struct.pack_into("<h", p, P.OFFSETS_BT.gyro_yaw, -1234)
        p[P.OFFSETS_BT.status1] = 0x03          # headphone + mic present
        usb = T.bt31_payload_to_usb01(bytes(p))
        body = usb[1:]
        self.assertEqual(body[P.OFFSETS_USB.stick_lx], 0x11)
        self.assertEqual(body[P.OFFSETS_USB.stick_ly], 0x22)
        self.assertEqual(body[P.OFFSETS_USB.trigger_r], 0xFE)
        self.assertEqual(struct.unpack_from("<h", body, P.OFFSETS_USB.gyro_yaw)[0], -1234)
        st = P.decode_input(body, usb=True)
        self.assertTrue(st.headphone)
        self.assertTrue(st.mic)

    def test_touchpad_coordinates_survive(self):
        p = bytearray(make_bt_payload(4))
        base = P.OFFSETS_BT.touch_data
        p[base] = 0x07            # active (bit 7 clear), id 7
        p[base + 1] = 0x34
        p[base + 2] = 0x12
        p[base + 3] = 0x56
        usb = T.bt31_payload_to_usb01(bytes(p))
        a = P.decode_input(bytes(p), usb=False).touch[0]
        b = P.decode_input(usb[1:], usb=True).touch[0]
        self.assertEqual((a.active, a.id, a.x, a.y), (b.active, b.id, b.x, b.y))
        self.assertTrue(b.active)
        self.assertEqual(b.x, 0x234)
        self.assertEqual(b.y, 0x561)

    def test_mic_audio_payload_is_not_input_state(self):
        payload = make_bt_payload(5, payload_type=P.PAYLOAD_TYPE_AUDIO)
        self.assertIsNone(T.bt31_payload_to_usb01(payload))
        self.assertTrue(T.is_mic_payload(payload))
        self.assertFalse(T.is_mic_payload(make_bt_payload(5)))

    def test_short_payload_is_rejected_not_padded(self):
        self.assertIsNone(T.bt31_payload_to_usb01(make_bt_payload(6)[:40]))

    def test_sequence_renumbering(self):
        payload = make_bt_payload(7)
        usb = T.bt31_payload_to_usb01(payload, seq=0xAB)
        self.assertEqual(usb[1 + T.USB_SEQ_OFFSET], 0xAB)
        # ...and nothing else moved
        untouched = T.bt31_payload_to_usb01(payload)
        self.assertEqual(usb[1 : 1 + T.USB_SEQ_OFFSET], untouched[1 : 1 + T.USB_SEQ_OFFSET])
        self.assertEqual(usb[2 + T.USB_SEQ_OFFSET :], untouched[2 + T.USB_SEQ_OFFSET :])

    def test_round_trip_usb_to_bt_to_usb(self):
        for seed in range(10):
            payload = make_bt_payload(seed + 100)
            usb = T.bt31_payload_to_usb01(payload)
            back = T.usb01_to_bt31_payload(usb)
            again = T.bt31_payload_to_usb01(back)
            with self.subTest(seed=seed):
                self.assertEqual(usb, again)
                # the BT-only tail cannot be reconstructed and must be zeroed,
                # never filled with stale bytes
                self.assertEqual(back[64:], b"\x00" * (T.BT_INPUT_PAYLOAD_LEN - 64))


class OutputTranslationTests(unittest.TestCase):
    def body(self) -> bytes:
        st = P.SetState().lightbar(0x00, 0xFF, 0x80).rumble(0x40, 0x80)
        st.trigger_right(P.AT_FEEDBACK, bytes([0xFF, 0xFF]))
        return bytes(st.body)

    def test_wrapped_report_shape(self):
        report = T.usb02_to_bt31(bytes([0x02]) + self.body(), seq=5)
        self.assertEqual(len(report), 78)
        self.assertEqual(report[0], P.BT_OUT_31)
        self.assertEqual(report[1], 5 << 4)     # seq nibble
        self.assertEqual(report[2], 0x10)       # the fixed tag ds.util.ts writes
        self.assertEqual(T.bt31_output_body(report), self.body())

    def test_crc32_is_correct_and_flipping_one_bit_is_detected(self):
        report = T.usb02_to_bt31(bytes([0x02]) + self.body(), seq=0)
        self.assertTrue(T.verify_bt_output_crc(report))
        broken = bytearray(report)
        broken[-1] ^= 0x01
        self.assertFalse(T.verify_bt_output_crc(bytes(broken)))

    def test_matches_the_phase1_builder_exactly(self):
        """Passthrough must be byte-identical to what Phase 1 sent on hardware."""
        body = self.body()
        self.assertEqual(
            T.usb02_to_bt31(bytes([0x02]) + body, seq=9),
            P.build_bt_setstate(body, 9),
        )

    def test_both_report_framings_are_accepted(self):
        body = self.body()
        with_id = bytes([0x02]) + body
        self.assertEqual(T.usb02_body(with_id), body)
        self.assertEqual(T.usb02_body(body), body)

    def test_64_byte_interrupt_out_transfer_is_truncated_to_the_body(self):
        # the endpoint is 64 bytes, so Windows may pad past the 48-byte report
        padded = (bytes([0x02]) + self.body()).ljust(64, b"\x00")
        self.assertEqual(T.usb02_body(padded), self.body())

    def test_truncated_report_is_zero_padded_never_short(self):
        # Too short for the report id to be unambiguous, so the bytes are taken
        # as body bytes and the rest is zero-filled -- a truncated transfer must
        # never produce a short body that would then read past the end.
        body = T.usb02_body(b"\x00\x00\x00")
        self.assertEqual(len(body), T.SETSTATE_BODY_LEN)
        self.assertEqual(body, bytes(T.SETSTATE_BODY_LEN))
        self.assertEqual(len(T.usb02_body(b"")), T.SETSTATE_BODY_LEN)
        self.assertEqual(len(T.usb02_to_bt31(b"", 0)), 78)

    def test_ambiguous_leading_0x02_is_treated_as_a_report_id_only_when_long_enough(self):
        # a bare 47-byte body whose first byte happens to be 0x02 (validFlag0
        # = COMPATIBLE_VIBRATION | HAPTICS_SELECT would be 0x03, so 0x02 is a
        # realistic value) must NOT lose its first byte
        body = bytearray(T.SETSTATE_BODY_LEN)
        body[0] = 0x02
        body[3] = 0x77
        self.assertEqual(T.usb02_body(bytes(body)), bytes(body))


class NeutralizeTests(unittest.TestCase):
    """Phase 4a: what the virtual device reports while the link is gone.

    The rule is "release everything the user was touching, keep everything the
    device was telling you". Getting the split wrong in either direction is a
    real bug: keep the sticks and a game strafes into a wall forever; blank the
    status bytes and it thinks the battery just hit 0 %.
    """

    def report(self, seed: int = 7) -> bytes:
        usb = T.bt31_payload_to_usb01(make_bt_payload(seed))
        assert usb is not None
        return usb

    def test_every_control_reads_as_released(self):
        st = P.decode_input(T.neutralize_usb01(self.report())[1:], usb=True)
        self.assertEqual((st.lx, st.ly, st.rx, st.ry), (0x80, 0x80, 0x80, 0x80))
        self.assertEqual((st.l2, st.r2), (0, 0))
        self.assertEqual(st.dpad, "-")
        self.assertFalse(any(st.buttons.values()), st.buttons)
        self.assertEqual(st.gyro, (0, 0, 0))
        self.assertFalse(st.touch[0].active)
        self.assertFalse(st.touch[1].active)

    def test_device_state_survives(self):
        src = self.report()
        before = P.decode_input(src[1:], usb=True)
        after = P.decode_input(T.neutralize_usb01(src)[1:], usb=True)
        self.assertEqual(after.battery_level, before.battery_level)
        self.assertEqual(after.battery_state, before.battery_state)
        self.assertEqual(after.headphone, before.headphone)
        self.assertEqual(after.mic, before.mic)
        self.assertEqual(after.motion_timestamp, before.motion_timestamp)

    def test_shape_is_unchanged_and_idempotent(self):
        src = self.report()
        once = T.neutralize_usb01(src)
        self.assertEqual(len(once), T.USB_INPUT_LEN)
        self.assertEqual(once[0], T.USB_INPUT_ID)
        self.assertEqual(T.neutralize_usb01(once), once)

    def test_accepts_a_bare_body_and_a_short_one(self):
        body = self.report()[1:]
        self.assertEqual(len(T.neutralize_usb01(body)), T.USB_INPUT_LEN)
        self.assertEqual(len(T.neutralize_usb01(b"\x01\x00\x00")), T.USB_INPUT_LEN)

    def test_a_zero_filled_report_would_read_as_dpad_north(self):
        # Why DPAD_RELEASED is 8 and not 0: this is the trap the constant exists
        # to avoid. A "cleared" report built by zeroing bytes holds UP forever.
        zeros = bytes([T.USB_INPUT_ID]) + bytes(T.USB_INPUT_BODY_LEN)
        self.assertEqual(P.decode_input(zeros[1:], usb=True).dpad, "N")
        self.assertEqual(P.decode_input(T.neutralize_usb01(zeros)[1:], usb=True).dpad, "-")


if __name__ == "__main__":
    unittest.main()
