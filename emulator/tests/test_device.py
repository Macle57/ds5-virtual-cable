"""Device-level tests: CMD_SUBMIT in, RET_SUBMIT out. No driver, no hardware."""

import struct
import unittest

from ds5emu import descriptors as D
from ds5emu import wire as W
from ds5emu.backend import SyntheticBackend
from ds5emu.device import DualSenseDevice


def control(bmRequestType, bRequest, wValue=0, wIndex=0, wLength=0, seqnum=1):
    direction = W.DIR_IN if bmRequestType & 0x80 else W.DIR_OUT
    return W.CmdSubmit(
        seqnum=seqnum, devid=0x00010001, direction=direction, ep=0,
        transfer_flags=0, transfer_buffer_length=wLength, start_frame=0,
        number_of_packets=-1, interval=0,
        setup=struct.pack("<BBHHH", bmRequestType, bRequest, wValue, wIndex, wLength),
    )


def split(reply):
    return W.unpack_ret_submit(reply), reply[W.HEADER_SIZE:]


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.backend = SyntheticBackend()
        self.dev = DualSenseDevice(self.backend)

    def submit(self, cmd, payload=b""):
        return self.dev.handle_submit(cmd, payload)

    # -- descriptors ------------------------------------------------------

    def test_get_device_descriptor(self):
        info, data = split(self.submit(control(0x80, 0x06, 0x0100, 0, 18)))
        self.assertEqual(info["status"], 0)
        self.assertEqual(info["actual_length"], 18)
        self.assertEqual(data, D.DEVICE_DESCRIPTOR)

    def test_get_device_descriptor_short_read(self):
        """Windows always asks for 8 bytes first to learn bMaxPacketSize0."""
        info, data = split(self.submit(control(0x80, 0x06, 0x0100, 0, 8)))
        self.assertEqual(data, D.DEVICE_DESCRIPTOR[:8])
        self.assertEqual(data[7], 64)

    def test_get_config_descriptor_short_then_full(self):
        _, head = split(self.submit(control(0x80, 0x06, 0x0200, 0, 9)))
        self.assertEqual(len(head), 9)
        total = int.from_bytes(head[2:4], "little")
        self.assertEqual(total, 227)
        _, full = split(self.submit(control(0x80, 0x06, 0x0200, 0, total)))
        self.assertEqual(full, D.CONFIG_DESCRIPTOR)

    def test_get_hid_report_descriptor(self):
        # bmRequestType 0x81: IN, standard, recipient=interface
        info, data = split(
            self.submit(control(0x81, 0x06, 0x2200, D.IFACE_HID, 289))
        )
        self.assertEqual(info["status"], 0)
        self.assertEqual(data, D.HID_REPORT_DESCRIPTOR)

    def test_hid_report_descriptor_from_wrong_interface_stalls(self):
        info, _ = split(self.submit(control(0x81, 0x06, 0x2200, 0, 289)))
        self.assertEqual(info["status"], -W.EPIPE)

    def test_get_hid_descriptor(self):
        info, data = split(self.submit(control(0x81, 0x06, 0x2100, D.IFACE_HID, 9)))
        self.assertEqual(info["status"], 0)
        self.assertEqual(len(data), 9)
        self.assertEqual(data[1], 0x21)
        self.assertEqual(data[6], 0x22)
        self.assertEqual(int.from_bytes(data[7:9], "little"), 289)

    def test_string_descriptors(self):
        _, langs = split(self.submit(control(0x80, 0x06, 0x0300, 0, 255)))
        self.assertEqual(langs, bytes.fromhex("04030904"))
        _, product = split(self.submit(control(0x80, 0x06, 0x0302, 0x0409, 255)))
        self.assertEqual(product[2:].decode("utf-16-le"), "DualSense Wireless Controller")

    def test_missing_string_stalls(self):
        info, _ = split(self.submit(control(0x80, 0x06, 0x0303, 0x0409, 255)))
        self.assertEqual(info["status"], -W.EPIPE)

    def test_bos_and_device_qualifier_stall(self):
        """Ground truth: the real device has no BOS descriptor."""
        for wValue in (0x0F00, 0x0600, 0x0700):
            info, _ = split(self.submit(control(0x80, 0x06, wValue, 0, 255)))
            self.assertEqual(info["status"], -W.EPIPE, hex(wValue))

    def test_ms_os_string_at_0xee_stalls(self):
        """Phase 0 correction #2: index 0xEE is absent on the real device."""
        info, _ = split(self.submit(control(0x80, 0x06, 0x03EE, 0, 18)))
        self.assertEqual(info["status"], -W.EPIPE)

    # -- configuration / interfaces ---------------------------------------

    def test_set_and_get_configuration(self):
        info, _ = split(self.submit(control(0x00, 0x09, 1)))
        self.assertEqual(info["status"], 0)
        self.assertEqual(self.dev.configuration, 1)
        _, data = split(self.submit(control(0x80, 0x08, 0, 0, 1)))
        self.assertEqual(data, b"\x01")

    def test_bad_configuration_stalls(self):
        info, _ = split(self.submit(control(0x00, 0x09, 7)))
        self.assertEqual(info["status"], -W.EPIPE)

    def test_set_interface_alt1_on_audio_streaming(self):
        """This is what starts the audio stream; alt 0 is zero-bandwidth."""
        for iface in (D.IFACE_AUDIO_OUT, D.IFACE_AUDIO_IN):
            info, _ = split(self.submit(control(0x01, 0x0B, 1, iface)))
            self.assertEqual(info["status"], 0)
            self.assertEqual(self.dev.alt_setting[iface], 1)
            _, data = split(self.submit(control(0x81, 0x0A, 0, iface, 1)))
            self.assertEqual(data, b"\x01")

    def test_set_interface_alt1_on_hid_stalls(self):
        info, _ = split(self.submit(control(0x01, 0x0B, 1, D.IFACE_HID)))
        self.assertEqual(info["status"], -W.EPIPE)

    def test_set_configuration_resets_alt_settings(self):
        self.submit(control(0x01, 0x0B, 1, D.IFACE_AUDIO_OUT))
        self.submit(control(0x00, 0x09, 1))
        self.assertEqual(self.dev.alt_setting[D.IFACE_AUDIO_OUT], 0)

    def test_get_status_device_is_self_powered(self):
        _, data = split(self.submit(control(0x80, 0x00, 0, 0, 2)))
        self.assertEqual(data, b"\x01\x00")

    def test_endpoint_halt_set_get_clear(self):
        ep = D.EP_ISO_OUT
        self.submit(control(0x02, 0x03, 0, ep))          # SET_FEATURE HALT
        _, data = split(self.submit(control(0x82, 0x00, 0, ep, 2)))
        self.assertEqual(data, b"\x01\x00")
        self.submit(control(0x02, 0x01, 0, ep))          # CLEAR_FEATURE HALT
        _, data = split(self.submit(control(0x82, 0x00, 0, ep, 2)))
        self.assertEqual(data, b"\x00\x00")

    def test_vendor_requests_stall(self):
        info, _ = split(self.submit(control(0xC0, 0x01, 0, 0, 8)))
        self.assertEqual(info["status"], -W.EPIPE)

    # -- HID class --------------------------------------------------------

    def test_hid_get_feature_report(self):
        # bmRequestType 0xA1 = IN | class | interface
        info, data = split(
            self.submit(control(0xA1, 0x01, 0x0305, D.IFACE_HID, 41))
        )
        self.assertEqual(info["status"], 0)
        self.assertEqual(len(data), 41)      # feature 0x05 is 41 bytes
        self.assertEqual(data[0], 0x05)

    def test_hid_get_unknown_feature_stalls(self):
        info, _ = split(self.submit(control(0xA1, 0x01, 0x0399, D.IFACE_HID, 64)))
        self.assertEqual(info["status"], -W.EPIPE)

    def test_hid_set_feature_report_reaches_backend(self):
        payload = bytes([0x08]) + b"\x11" * 46
        cmd = control(0x21, 0x09, 0x0308, D.IFACE_HID, len(payload))
        info, _ = split(self.submit(cmd, payload))
        self.assertEqual(info["status"], 0)
        self.assertEqual(self.backend.feature_writes, [(0x08, payload)])

    def test_hid_set_output_report_reaches_backend(self):
        payload = bytes([0x02]) + b"\x00" * 47
        cmd = control(0x21, 0x09, 0x0202, D.IFACE_HID, len(payload))
        self.submit(cmd, payload)
        self.assertEqual(self.backend.output_reports, [payload])

    def test_hid_idle_and_protocol(self):
        self.submit(control(0x21, 0x0A, 0x0A00, D.IFACE_HID))
        _, data = split(self.submit(control(0xA1, 0x02, 0, D.IFACE_HID, 1)))
        self.assertEqual(data, b"\x0a")
        _, data = split(self.submit(control(0xA1, 0x03, 0, D.IFACE_HID, 1)))
        self.assertEqual(data, b"\x01")

    # -- UAC1 class -------------------------------------------------------

    def _uac_index(self, unit):
        return (unit << 8) | D.IFACE_AUDIOCONTROL

    def test_uac_mute_roundtrip(self):
        idx = self._uac_index(D.UNIT_FU_SPEAKER)
        info, _ = split(self.submit(control(0x21, 0x01, 0x0100, idx, 1), b"\x01"))
        self.assertEqual(info["status"], 0)
        _, data = split(self.submit(control(0xA1, 0x81, 0x0100, idx, 1)))
        self.assertEqual(data, b"\x01")

    def test_uac_volume_min_max_res(self):
        idx = self._uac_index(D.UNIT_FU_SPEAKER)
        _, mn = split(self.submit(control(0xA1, 0x82, 0x0200, idx, 2)))
        _, mx = split(self.submit(control(0xA1, 0x83, 0x0200, idx, 2)))
        _, rs = split(self.submit(control(0xA1, 0x84, 0x0200, idx, 2)))
        self.assertEqual(struct.unpack("<h", mn)[0], -96 * 256)
        self.assertEqual(struct.unpack("<h", mx)[0], 0)
        self.assertEqual(struct.unpack("<h", rs)[0], 256)

    def test_uac_volume_set_cur_is_clamped(self):
        idx = self._uac_index(D.UNIT_FU_MIC)
        self.submit(control(0x21, 0x01, 0x0200, idx, 2), struct.pack("<h", 30000))
        _, cur = split(self.submit(control(0xA1, 0x81, 0x0200, idx, 2)))
        self.assertEqual(struct.unpack("<h", cur)[0], 0)

    def test_uac_unknown_unit_stalls(self):
        info, _ = split(self.submit(control(0xA1, 0x81, 0x0100, (9 << 8) | 0, 1)))
        self.assertEqual(info["status"], -W.EPIPE)

    def test_uac_endpoint_requests_stall(self):
        """The CS_ENDPOINT descriptors declare bmAttributes = 0x00, i.e. no
        sampling-frequency control, so the real device stalls these."""
        # SET_CUR SAMPLING_FREQ_CONTROL, OUT | class | endpoint
        info, _ = split(
            self.submit(control(0x22, 0x01, 0x0100, D.EP_ISO_OUT, 3), b"\x80\xbb\x00")
        )
        self.assertEqual(info["status"], -W.EPIPE)
        # GET_CUR SAMPLING_FREQ_CONTROL, IN | class | endpoint
        info, _ = split(self.submit(control(0xA2, 0x81, 0x0100, D.EP_ISO_IN, 3)))
        self.assertEqual(info["status"], -W.EPIPE)


class InterruptEndpointTests(unittest.TestCase):
    def setUp(self):
        self.backend = SyntheticBackend()
        self.dev = DualSenseDevice(self.backend)

    def test_hid_interrupt_in_returns_a_report(self):
        cmd = W.CmdSubmit(
            seqnum=10, devid=0, direction=W.DIR_IN, ep=D.EP_HID_IN & 0x7F,
            transfer_flags=0, transfer_buffer_length=64, start_frame=0,
            number_of_packets=-1, interval=6, setup=b"\0" * 8,
        )
        info, data = split(self.dev.handle_submit(cmd, b""))
        self.assertEqual(info["status"], 0)
        self.assertEqual(info["actual_length"], 64)
        self.assertEqual(len(data), 64)
        self.assertEqual(data[0], 0x01)   # USB input report id
        self.assertEqual(data[1], 0x80)   # centred stick

    def test_hid_interrupt_out_reaches_backend(self):
        payload = bytes([0x02]) + b"\x33" * 47
        cmd = W.CmdSubmit(
            seqnum=11, devid=0, direction=W.DIR_OUT, ep=D.EP_HID_OUT,
            transfer_flags=0, transfer_buffer_length=len(payload), start_frame=0,
            number_of_packets=-1, interval=6, setup=b"\0" * 8,
        )
        info, data = split(self.dev.handle_submit(cmd, payload))
        self.assertEqual(info["status"], 0)
        self.assertEqual(info["actual_length"], len(payload))
        self.assertEqual(data, b"")
        self.assertEqual(self.backend.output_reports, [payload])

    def test_unknown_interrupt_endpoint_stalls(self):
        cmd = W.CmdSubmit(
            seqnum=12, devid=0, direction=W.DIR_IN, ep=5,
            transfer_flags=0, transfer_buffer_length=64, start_frame=0,
            number_of_packets=-1, interval=6, setup=b"\0" * 8,
        )
        info, _ = split(self.dev.handle_submit(cmd, b""))
        self.assertEqual(info["status"], -W.EPIPE)


class IsochronousTests(unittest.TestCase):
    """The rules here come from usbip-win2 drivers/ude/wsk_receive.cpp
    (validate() / fill_isoc_data() / isoch_transfer). Getting them wrong is the
    difference between working audio and an endless pipe-reset loop."""

    def setUp(self):
        self.backend = SyntheticBackend()
        self.dev = DualSenseDevice(self.backend)

    def _iso_out_cmd(self, npackets=10, pkt=384, seqnum=20):
        return W.CmdSubmit(
            seqnum=seqnum, devid=0, direction=W.DIR_OUT, ep=D.EP_ISO_OUT,
            transfer_flags=0, transfer_buffer_length=npackets * pkt, start_frame=0,
            number_of_packets=npackets, interval=4, setup=b"\0" * 8,
        )

    def _iso_in_cmd(self, npackets=10, pkt=192, seqnum=30):
        return W.CmdSubmit(
            seqnum=seqnum, devid=0, direction=W.DIR_IN, ep=D.EP_ISO_IN & 0x7F,
            transfer_flags=0, transfer_buffer_length=npackets * pkt, start_frame=0,
            number_of_packets=npackets, interval=4, setup=b"\0" * 8,
        )

    def test_iso_out_delivers_every_packet_to_the_backend(self):
        cmd = self._iso_out_cmd()
        descs = [W.IsoPacket(offset=i * 384, length=384) for i in range(10)]
        buf = bytes(range(256)) * 15
        reply = self.dev.handle_submit(cmd, buf + W.pack_iso_packets(descs))

        info = W.unpack_ret_submit(reply)
        self.assertEqual(info["status"], 0)
        self.assertEqual(info["number_of_packets"], 10)
        self.assertEqual(info["error_count"], 0)
        self.assertEqual(info["actual_length"], 3840)
        self.assertEqual(self.backend.audio_out_packets, 10)
        self.assertEqual(self.backend.audio_out_bytes, 3840)

    def test_iso_out_reply_has_descriptors_but_no_data(self):
        cmd = self._iso_out_cmd(npackets=4)
        descs = [W.IsoPacket(offset=i * 384, length=384) for i in range(4)]
        reply = self.dev.handle_submit(cmd, b"\0" * 1536 + W.pack_iso_packets(descs))
        payload = reply[W.HEADER_SIZE:]
        self.assertEqual(len(payload), 4 * W.ISO_DESC_SIZE)
        got = W.unpack_iso_packets(payload, 4)
        for i, p in enumerate(got):
            self.assertEqual(p.offset, i * 384)   # echoed unchanged — required
            self.assertEqual(p.length, 384)
            self.assertEqual(p.actual_length, 384)
            self.assertEqual(p.status, 0)

    def test_iso_in_payload_is_compacted_and_lengths_sum(self):
        cmd = self._iso_in_cmd()
        descs = [W.IsoPacket(offset=i * 192, length=192) for i in range(10)]
        reply = self.dev.handle_submit(cmd, W.pack_iso_packets(descs))
        info = W.unpack_ret_submit(reply)
        payload = reply[W.HEADER_SIZE:]

        data = payload[: info["actual_length"]]
        iso = W.unpack_iso_packets(payload, 10, info["actual_length"])

        self.assertEqual(info["actual_length"], 10 * 192)
        self.assertEqual(len(data), 10 * 192)
        self.assertEqual(sum(p.actual_length for p in iso), info["actual_length"])
        self.assertLessEqual(info["actual_length"], cmd.transfer_buffer_length)
        # offsets are the ORIGINAL ones, not the compacted positions
        self.assertEqual([p.offset for p in iso], [i * 192 for i in range(10)])
        self.assertTrue(all(p.actual_length <= p.length for p in iso))

    def test_iso_in_data_is_the_synthetic_tone(self):
        cmd = self._iso_in_cmd(npackets=1, pkt=192)
        descs = [W.IsoPacket(offset=0, length=192)]
        reply = self.dev.handle_submit(cmd, W.pack_iso_packets(descs))
        info = W.unpack_ret_submit(reply)
        data = reply[W.HEADER_SIZE : W.HEADER_SIZE + info["actual_length"]]
        self.assertEqual(len(data), 192)
        # 2ch s16le: the synthetic backend writes both channels identically
        left, right = struct.unpack_from("<hh", data, 8)
        self.assertEqual(left, right)
        self.assertNotEqual(data, b"\0" * 192)

    def test_iso_in_clamps_to_wmaxpacketsize(self):
        cmd = self._iso_in_cmd(npackets=1, pkt=1024)
        descs = [W.IsoPacket(offset=0, length=1024)]
        reply = self.dev.handle_submit(cmd, W.pack_iso_packets(descs))
        info = W.unpack_ret_submit(reply)
        self.assertEqual(info["actual_length"], D.ISO_IN_MAX_PACKET)
        iso = W.unpack_iso_packets(reply[W.HEADER_SIZE:], 1, info["actual_length"])
        self.assertEqual(iso[0].length, 1024)
        self.assertEqual(iso[0].actual_length, D.ISO_IN_MAX_PACKET)

    def test_iso_start_frame_is_monotonic(self):
        cmd = self._iso_out_cmd(npackets=1)
        descs = [W.IsoPacket(offset=0, length=384)]
        payload = b"\0" * 384 + W.pack_iso_packets(descs)
        frames = []
        for _ in range(3):
            reply = self.dev.handle_submit(cmd, payload)
            frames.append(W.unpack_ret_submit(reply)["start_frame"])
        self.assertEqual(frames, sorted(frames))
        self.assertTrue(all(f >= 0 for f in frames))

    def test_iso_on_the_wrong_endpoint_stalls(self):
        cmd = W.CmdSubmit(
            seqnum=40, devid=0, direction=W.DIR_OUT, ep=D.EP_HID_OUT,
            transfer_flags=0, transfer_buffer_length=384, start_frame=0,
            number_of_packets=1, interval=4, setup=b"\0" * 8,
        )
        descs = [W.IsoPacket(offset=0, length=384)]
        info, _ = split(self.dev.handle_submit(cmd, b"\0" * 384 + W.pack_iso_packets(descs)))
        self.assertEqual(info["status"], -W.EPIPE)

    def test_realistic_one_millisecond_packets(self):
        """A 1 ms service interval carries 384 B out / 192 B in of PCM,
        inside wMaxPacketSize 392 / 196."""
        self.assertLessEqual(D.ISO_OUT_BYTES_PER_MS, D.ISO_OUT_MAX_PACKET)
        self.assertLessEqual(D.ISO_IN_BYTES_PER_MS, D.ISO_IN_MAX_PACKET)
        cmd = self._iso_out_cmd(npackets=8, pkt=D.ISO_OUT_BYTES_PER_MS)
        descs = [W.IsoPacket(offset=i * 384, length=384) for i in range(8)]
        reply = self.dev.handle_submit(
            cmd, b"\x01" * (8 * 384) + W.pack_iso_packets(descs)
        )
        self.assertEqual(W.unpack_ret_submit(reply)["error_count"], 0)
        self.assertEqual(self.backend.audio_out_bytes, 8 * 384)


class IdentityTests(unittest.TestCase):
    def test_usbip_device_info_reports_high_speed(self):
        """Non-negotiable: usbip-win2's patch_config() rewrites iso bInterval
        (+3) for anything below USB_SPEED_HIGH, which would turn our 1 ms audio
        endpoints into 8 ms ones."""
        dev = DualSenseDevice(SyntheticBackend())
        info = dev.usbip_device_info()
        self.assertEqual(info.speed, W.USB_SPEED_HIGH)
        self.assertEqual(info.speed, 3)

    def test_usbip_device_info_matches_the_device_descriptor(self):
        dev = DualSenseDevice(SyntheticBackend())
        info = dev.usbip_device_info(busid="2-3", busnum=2, devnum=3)
        self.assertEqual(info.idVendor, 0x054C)
        self.assertEqual(info.idProduct, 0x0CE6)
        self.assertEqual(info.bcdDevice, 0x0100)
        self.assertEqual(info.bDeviceClass, 0x00)      # per-interface
        self.assertEqual(info.bNumInterfaces, 4)
        self.assertEqual(info.bNumConfigurations, 1)
        self.assertEqual(len(info.interfaces), 4)
        self.assertEqual(info.busid, "2-3")
        self.assertEqual(info.devid, (2 << 16) | 3)
        self.assertEqual(len(info.pack_with_interfaces()), 312 + 4 * 4)


if __name__ == "__main__":
    unittest.main()
