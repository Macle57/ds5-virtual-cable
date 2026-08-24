"""Byte-exact tests for the USB/IP wire format.

These run with no driver installed and no hardware attached. Expected byte
strings are written out literally, not computed with the same code under test.
"""

import struct
import unittest

from ds5emu import wire as W


class TestSizes(unittest.TestCase):
    def test_struct_sizes_match_usbip_win2_headers(self):
        # include/usbip/proto.h: static_assert(sizeof(header) == 48)
        self.assertEqual(W.HEADER_SIZE, 48)
        self.assertEqual(W.OP_COMMON_SIZE, 8)
        # 256 + 32 + 3*4 + 3*2 + 6*1
        self.assertEqual(W.USB_DEVICE_SIZE, 312)
        self.assertEqual(W.USB_INTERFACE_SIZE, 4)
        self.assertEqual(W.ISO_DESC_SIZE, 16)

    def test_protocol_constants(self):
        self.assertEqual(W.USBIP_VERSION, 0x0111)
        self.assertEqual(W.TCP_PORT, 3240)
        self.assertEqual(W.OP_REQ_DEVLIST, 0x8005)
        self.assertEqual(W.OP_REP_DEVLIST, 0x0005)
        self.assertEqual(W.OP_REQ_IMPORT, 0x8003)
        self.assertEqual(W.OP_REP_IMPORT, 0x0003)
        self.assertEqual(W.MAX_ISO_PACKETS, 1024)


class TestOpPhase(unittest.TestCase):
    def test_op_common_is_big_endian(self):
        self.assertEqual(
            W.pack_op_common(W.OP_REP_IMPORT, W.ST_OK),
            bytes.fromhex("0111" "0003" "00000000"),
        )
        self.assertEqual(
            W.pack_op_common(W.OP_REP_DEVLIST, W.ST_NODEV),
            bytes.fromhex("0111" "0005" "00000004"),
        )

    def test_op_common_roundtrip(self):
        raw = W.pack_op_common(W.OP_REQ_IMPORT, W.ST_OK)
        self.assertEqual(W.unpack_op_common(raw), (0x0111, 0x8003, 0))

    def test_import_request_roundtrip(self):
        raw = W.pack_import_request("1-1")
        self.assertEqual(len(raw), W.OP_COMMON_SIZE + W.BUS_ID_SIZE)
        self.assertEqual(raw[8:11], b"1-1")
        self.assertEqual(raw[11:40], b"\0" * 29)  # NUL padded, NUL terminated
        self.assertEqual(W.unpack_import_request_body(raw[8:]), "1-1")

    def test_usb_device_layout(self):
        dev = W.UsbDeviceInfo(
            path="/sys/x", busid="1-1", busnum=1, devnum=2, speed=W.USB_SPEED_HIGH,
            idVendor=0x054C, idProduct=0x0CE6, bcdDevice=0x0100,
            bDeviceClass=0, bDeviceSubClass=0, bDeviceProtocol=0,
            bConfigurationValue=1, bNumConfigurations=1, bNumInterfaces=4,
        )
        raw = dev.pack()
        self.assertEqual(len(raw), 312)
        self.assertEqual(raw[:6], b"/sys/x")
        self.assertEqual(raw[256:259], b"1-1")
        tail = raw[288:]
        self.assertEqual(
            tail,
            struct.pack(">IIIHHHBBBBBB", 1, 2, 3, 0x054C, 0x0CE6, 0x0100, 0, 0, 0, 1, 1, 4),
        )
        self.assertEqual(W.UsbDeviceInfo.unpack(raw).busid, "1-1")

    def test_devid_encoding(self):
        dev = W.UsbDeviceInfo("p", "3-2", 3, 2, 3, 1, 1, 1, 0, 0, 0, 1, 1, 1)
        self.assertEqual(dev.devid, (3 << 16) | 2)

    def test_import_reply_failure_has_no_device_body(self):
        self.assertEqual(len(W.pack_import_reply(None)), W.OP_COMMON_SIZE)

    def test_devlist_reply_shape(self):
        dev = W.UsbDeviceInfo(
            "p", "1-1", 1, 1, 3, 1, 1, 1, 0, 0, 0, 1, 1, 2,
            interfaces=(W.UsbInterfaceInfo(1, 1, 0), W.UsbInterfaceInfo(3, 0, 0)),
        )
        raw = W.pack_devlist_reply([dev])
        self.assertEqual(raw[:8], W.pack_op_common(W.OP_REP_DEVLIST, W.ST_OK))
        self.assertEqual(raw[8:12], struct.pack(">I", 1))
        self.assertEqual(len(raw), 8 + 4 + 312 + 2 * 4)
        self.assertEqual(raw[-8:], bytes([1, 1, 0, 0, 3, 0, 0, 0]))

    def test_busid_too_long_is_rejected(self):
        with self.assertRaises(ValueError):
            W.pack_import_request("x" * 32)


class TestCommandHeaders(unittest.TestCase):
    def _submit(self, **kw):
        base = dict(
            seqnum=1, devid=0x00010001, direction=W.DIR_IN, ep=1,
            transfer_flags=0, transfer_buffer_length=0, start_frame=0,
            number_of_packets=-1, interval=0, setup=b"\0" * 8,
        )
        base.update(kw)
        return W.CmdSubmit(**base)

    def test_cmd_submit_bytes_are_big_endian(self):
        cmd = self._submit(
            seqnum=0x11223344, devid=0x00010002, direction=W.DIR_OUT, ep=3,
            transfer_flags=0x00000004, transfer_buffer_length=64,
            number_of_packets=-1, interval=6,
            setup=bytes.fromhex("2109 0200 0300 4000"),
        )
        raw = W.pack_cmd_submit(cmd)
        self.assertEqual(len(raw), 48)
        expected = bytes.fromhex(
            "00000001"   # CMD_SUBMIT
            "11223344"   # seqnum
            "00010002"   # devid
            "00000000"   # direction OUT
            "00000003"   # ep
            "00000004"   # transfer_flags
            "00000040"   # transfer_buffer_length = 64
            "00000000"   # start_frame
            "ffffffff"   # number_of_packets = -1 (non-iso)
            "00000006"   # interval
            "21090200 03004000"  # setup
        )
        self.assertEqual(raw, expected)

    def test_unpack_header_roundtrip(self):
        cmd = self._submit(seqnum=7, ep=4, transfer_buffer_length=64)
        command, parsed = W.unpack_header(W.pack_cmd_submit(cmd))
        self.assertEqual(command, W.CMD_SUBMIT)
        self.assertEqual(parsed, cmd)

    def test_ret_submit_is_48_bytes_with_padding(self):
        raw = W.pack_ret_submit(seqnum=5, ep=4, direction=W.DIR_IN,
                                actual_length=0, status=0)
        self.assertEqual(len(raw), 48)
        self.assertEqual(raw[:4], bytes.fromhex("00000003"))  # RET_SUBMIT
        self.assertEqual(raw[-8:], b"\0" * 8)                 # explicit padding

    def test_ret_submit_carries_data_after_header(self):
        raw = W.pack_ret_submit(seqnum=5, ep=4, direction=W.DIR_IN,
                                actual_length=3, data=b"abc")
        self.assertEqual(len(raw), 51)
        self.assertEqual(raw[48:], b"abc")
        info = W.unpack_ret_submit(raw)
        self.assertEqual(info["actual_length"], 3)
        self.assertEqual(info["seqnum"], 5)

    def test_stall_status_is_negative_epipe(self):
        raw = W.pack_ret_submit(seqnum=1, status=-W.EPIPE)
        self.assertEqual(W.unpack_ret_submit(raw)["status"], -32)

    def test_cmd_unlink_roundtrip(self):
        raw = W.pack_cmd_unlink(9, 0x00010001, W.DIR_OUT, 0, 4)
        self.assertEqual(len(raw), 48)
        command, parsed = W.unpack_header(raw)
        self.assertEqual(command, W.CMD_UNLINK)
        self.assertEqual(parsed.seqnum, 9)
        self.assertEqual(parsed.unlink_seqnum, 4)

    def test_ret_unlink_is_48_bytes(self):
        raw = W.pack_ret_unlink(seqnum=9, status=-W.ECONNRESET)
        self.assertEqual(len(raw), 48)
        self.assertEqual(raw[:4], bytes.fromhex("00000004"))

    def test_oversized_iso_count_rejected(self):
        raw = bytearray(W.pack_cmd_submit(self._submit(number_of_packets=1)))
        raw[32:36] = (W.MAX_ISO_PACKETS + 1).to_bytes(4, "big")
        with self.assertRaises(ValueError):
            W.unpack_header(bytes(raw))


class TestPayloadLayout(unittest.TestCase):
    """CMD_SUBMIT payload = [buffer if OUT][iso descriptors if iso].

    Mirrors the MDL chain usbip-win2 builds in
    drivers/ude/device_ioctl.cpp::prepare_wsk_buf.
    """

    def _cmd(self, direction, length, npackets):
        return W.CmdSubmit(
            seqnum=1, devid=0, direction=direction, ep=1, transfer_flags=0,
            transfer_buffer_length=length, start_frame=0,
            number_of_packets=npackets, interval=4, setup=b"\0" * 8,
        )

    def test_non_iso_out_payload_is_just_the_buffer(self):
        cmd = self._cmd(W.DIR_OUT, 64, -1)
        self.assertEqual(cmd.payload_size(), 64)
        buf, iso = cmd.split_payload(b"\xAA" * 64)
        self.assertEqual(buf, b"\xAA" * 64)
        self.assertEqual(iso, [])

    def test_non_iso_in_payload_is_empty(self):
        cmd = self._cmd(W.DIR_IN, 64, -1)
        self.assertEqual(cmd.payload_size(), 0)
        buf, iso = cmd.split_payload(b"")
        self.assertEqual((buf, iso), (b"", []))

    def test_iso_in_payload_is_descriptors_only(self):
        cmd = self._cmd(W.DIR_IN, 1920, 10)
        self.assertEqual(cmd.payload_size(), 10 * 16)
        descs = [W.IsoPacket(offset=i * 192, length=192) for i in range(10)]
        buf, iso = cmd.split_payload(W.pack_iso_packets(descs))
        self.assertEqual(buf, b"")
        self.assertEqual(len(iso), 10)
        self.assertEqual(iso[3].offset, 3 * 192)
        self.assertEqual(iso[3].length, 192)

    def test_iso_out_payload_is_buffer_then_descriptors(self):
        cmd = self._cmd(W.DIR_OUT, 3840, 10)
        self.assertEqual(cmd.payload_size(), 3840 + 10 * 16)
        descs = [W.IsoPacket(offset=i * 384, length=384) for i in range(10)]
        payload = bytes(range(256)) * 15 + W.pack_iso_packets(descs)
        self.assertEqual(len(payload), 3840 + 160)
        buf, iso = cmd.split_payload(payload)
        self.assertEqual(len(buf), 3840)
        self.assertEqual(len(iso), 10)
        self.assertEqual(iso[-1].offset, 9 * 384)

    def test_short_payload_is_rejected(self):
        cmd = self._cmd(W.DIR_OUT, 64, -1)
        with self.assertRaises(ValueError):
            cmd.split_payload(b"\0" * 63)

    def test_iso_descriptor_is_big_endian(self):
        p = W.IsoPacket(offset=0x11223344, length=0x00000188,
                        actual_length=0x00000188, status=0)
        self.assertEqual(
            p.pack(),
            bytes.fromhex("11223344" "00000188" "00000188" "00000000"),
        )


if __name__ == "__main__":
    unittest.main()
