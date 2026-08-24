"""End-to-end tests over a real loopback socket, with NO driver installed.

The test client replicates exactly what usbip-win2 does on the wire:

  * `send_op_common(OP_REQ_DEVLIST)` with no request body
    (userspace/libusbip/src/remote.cpp)
  * `op_common{OP_REQ_IMPORT} + op_import_request{busid[32]}`, then verify the
    busid echoed back (drivers/ude/vhci_ioctl.cpp::recv_rep_import)
  * CMD_SUBMIT / CMD_UNLINK on the same socket afterwards
"""

import asyncio
import struct
import unittest

from ds5emu import descriptors as D
from ds5emu import wire as W
from ds5emu.backend import SyntheticBackend
from ds5emu.server import UsbIpServer


class FakeClient:
    """The minimal subset of a USB/IP client that usbip-win2 actually uses."""

    def __init__(self, reader, writer):
        self.reader = reader
        self.writer = writer
        self.seq = 0

    async def send(self, data):
        self.writer.write(data)
        await self.writer.drain()

    async def devlist(self):
        await self.send(W.pack_op_common(W.OP_REQ_DEVLIST))
        version, code, status = W.unpack_op_common(await self.reader.readexactly(8))
        assert version == W.USBIP_VERSION, hex(version)
        assert code == W.OP_REP_DEVLIST, hex(code)
        assert status == W.ST_OK
        (ndev,) = struct.unpack(">I", await self.reader.readexactly(4))
        devices = []
        for _ in range(ndev):
            dev = W.UsbDeviceInfo.unpack(await self.reader.readexactly(W.USB_DEVICE_SIZE))
            await self.reader.readexactly(dev.bNumInterfaces * W.USB_INTERFACE_SIZE)
            devices.append(dev)
        return devices

    async def import_device(self, busid):
        await self.send(W.pack_import_request(busid))
        version, code, status = W.unpack_op_common(await self.reader.readexactly(8))
        assert code == W.OP_REP_IMPORT, hex(code)
        if status != W.ST_OK:
            return None
        return W.UsbDeviceInfo.unpack(await self.reader.readexactly(W.USB_DEVICE_SIZE))

    async def submit(self, *, direction, ep, setup=b"\0" * 8, out_data=b"",
                     in_length=0, iso=None, interval=0):
        self.seq += 1
        npackets = len(iso) if iso is not None else -1
        length = len(out_data) if direction == W.DIR_OUT else in_length
        cmd = W.CmdSubmit(
            seqnum=self.seq, devid=0x00010001, direction=direction, ep=ep,
            transfer_flags=0, transfer_buffer_length=length, start_frame=0,
            number_of_packets=npackets, interval=interval, setup=setup,
        )
        payload = out_data if direction == W.DIR_OUT else b""
        if iso is not None:
            payload += W.pack_iso_packets(iso)
        await self.send(W.pack_cmd_submit(cmd) + payload)
        return await self.recv_ret()

    async def recv_ret(self):
        head = await self.reader.readexactly(W.HEADER_SIZE)
        info = W.unpack_ret_submit(head)
        # usbip-win2 computes the payload as `(dir_in ? actual_length : 0) +
        # number_of_packets * sizeof(iso_packet_descriptor)`: an OUT reply
        # never carries a data buffer, however large actual_length is.
        data = b""
        if info["direction"] == W.DIR_IN and info["actual_length"] > 0:
            data = await self.reader.readexactly(info["actual_length"])
        iso = []
        n = info["number_of_packets"]
        if n > 0:
            raw = await self.reader.readexactly(n * W.ISO_DESC_SIZE)
            iso = W.unpack_iso_packets(raw, n)
        return info, data, iso

    async def close(self):
        self.writer.close()
        try:
            await self.writer.wait_closed()
        except Exception:
            pass


def ctrl_setup(bmRequestType, bRequest, wValue=0, wIndex=0, wLength=0):
    return struct.pack("<BBHHH", bmRequestType, bRequest, wValue, wIndex, wLength)


class ServerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.backend = SyntheticBackend()
        # port 0 => the OS picks a free port, so the tests never collide with a
        # real usbip server or with each other.
        self.server = UsbIpServer(self.backend, host="127.0.0.1", port=0, busid="1-1")
        await self.server.start()
        self.port = self.server._server.sockets[0].getsockname()[1]

    async def asyncTearDown(self):
        await self.server.stop()

    async def connect(self):
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        return FakeClient(reader, writer)

    # -- op phase ---------------------------------------------------------

    async def test_devlist(self):
        c = await self.connect()
        try:
            devices = await c.devlist()
        finally:
            await c.close()
        self.assertEqual(len(devices), 1)
        dev = devices[0]
        self.assertEqual(dev.busid, "1-1")
        self.assertEqual((dev.idVendor, dev.idProduct), (0x054C, 0x0CE6))
        self.assertEqual(dev.speed, W.USB_SPEED_HIGH)
        self.assertEqual(dev.bNumInterfaces, 4)

    async def test_import_echoes_the_busid(self):
        c = await self.connect()
        try:
            dev = await c.import_device("1-1")
            self.assertIsNotNone(dev)
            # usbip-win2 aborts the attach if this does not match.
            self.assertEqual(dev.busid, "1-1")
            self.assertEqual(dev.speed, W.USB_SPEED_HIGH)
        finally:
            await c.close()

    async def test_import_unknown_busid_is_refused(self):
        c = await self.connect()
        try:
            self.assertIsNone(await c.import_device("9-9"))
        finally:
            await c.close()

    # -- enumeration over the wire ----------------------------------------

    async def test_full_enumeration_sequence(self):
        """The sequence Windows actually performs when a device is attached."""
        c = await self.connect()
        try:
            self.assertIsNotNone(await c.import_device("1-1"))

            # 1. 8-byte device descriptor probe
            info, data, _ = await c.submit(
                direction=W.DIR_IN, ep=0,
                setup=ctrl_setup(0x80, 0x06, 0x0100, 0, 8), in_length=8)
            self.assertEqual(info["status"], 0)
            self.assertEqual(data, D.DEVICE_DESCRIPTOR[:8])

            # 2. full device descriptor
            _, data, _ = await c.submit(
                direction=W.DIR_IN, ep=0,
                setup=ctrl_setup(0x80, 0x06, 0x0100, 0, 18), in_length=18)
            self.assertEqual(data, D.DEVICE_DESCRIPTOR)

            # 3. config descriptor header then the whole thing
            _, head, _ = await c.submit(
                direction=W.DIR_IN, ep=0,
                setup=ctrl_setup(0x80, 0x06, 0x0200, 0, 9), in_length=9)
            total = int.from_bytes(head[2:4], "little")
            _, full, _ = await c.submit(
                direction=W.DIR_IN, ep=0,
                setup=ctrl_setup(0x80, 0x06, 0x0200, 0, total), in_length=total)
            self.assertEqual(full, D.CONFIG_DESCRIPTOR)

            # 4. strings
            _, product, _ = await c.submit(
                direction=W.DIR_IN, ep=0,
                setup=ctrl_setup(0x80, 0x06, 0x0302, 0x0409, 255), in_length=255)
            self.assertEqual(product[2:].decode("utf-16-le"),
                             "DualSense Wireless Controller")

            # 5. SET_CONFIGURATION
            info, _, _ = await c.submit(
                direction=W.DIR_OUT, ep=0, setup=ctrl_setup(0x00, 0x09, 1))
            self.assertEqual(info["status"], 0)

            # 6. HID report descriptor
            _, rd, _ = await c.submit(
                direction=W.DIR_IN, ep=0,
                setup=ctrl_setup(0x81, 0x06, 0x2200, D.IFACE_HID, 289), in_length=289)
            self.assertEqual(rd, D.HID_REPORT_DESCRIPTOR)

            # 7. audio streaming interfaces to alt 1
            for iface in (D.IFACE_AUDIO_OUT, D.IFACE_AUDIO_IN):
                info, _, _ = await c.submit(
                    direction=W.DIR_OUT, ep=0,
                    setup=ctrl_setup(0x01, 0x0B, 1, iface))
                self.assertEqual(info["status"], 0)
        finally:
            await c.close()

        self.assertEqual(self.server.device.alt_setting[D.IFACE_AUDIO_OUT], 1)
        self.assertEqual(self.server.device.configuration, 1)

    # -- data endpoints ---------------------------------------------------

    async def test_hid_interrupt_in_over_the_wire(self):
        c = await self.connect()
        try:
            await c.import_device("1-1")
            info, data, _ = await c.submit(
                direction=W.DIR_IN, ep=D.EP_HID_IN & 0x7F, in_length=64, interval=6)
            self.assertEqual(info["status"], 0)
            self.assertEqual(len(data), 64)
            self.assertEqual(data[0], 0x01)
        finally:
            await c.close()

    async def test_hid_interrupt_out_over_the_wire(self):
        c = await self.connect()
        try:
            await c.import_device("1-1")
            payload = bytes([0x02]) + b"\x55" * 47
            info, _, _ = await c.submit(
                direction=W.DIR_OUT, ep=D.EP_HID_OUT, out_data=payload, interval=6)
            self.assertEqual(info["status"], 0)
            self.assertEqual(info["actual_length"], len(payload))
        finally:
            await c.close()
        self.assertEqual(self.backend.output_reports, [payload])

    async def test_iso_out_over_the_wire(self):
        c = await self.connect()
        try:
            await c.import_device("1-1")
            n, pkt = 10, D.ISO_OUT_BYTES_PER_MS
            iso = [W.IsoPacket(offset=i * pkt, length=pkt) for i in range(n)]
            info, data, ret_iso = await c.submit(
                direction=W.DIR_OUT, ep=D.EP_ISO_OUT,
                out_data=b"\x7f" * (n * pkt), iso=iso, interval=4)
            self.assertEqual(info["status"], 0)
            self.assertEqual(info["number_of_packets"], n)
            self.assertEqual(info["error_count"], 0)
            self.assertEqual(data, b"")            # OUT reply carries no data
            self.assertEqual(len(ret_iso), n)
            self.assertEqual([p.offset for p in ret_iso], [i * pkt for i in range(n)])
        finally:
            await c.close()
        self.assertEqual(self.backend.audio_out_bytes, 10 * D.ISO_OUT_BYTES_PER_MS)

    async def test_iso_in_over_the_wire(self):
        c = await self.connect()
        try:
            await c.import_device("1-1")
            n, pkt = 10, D.ISO_IN_BYTES_PER_MS
            iso = [W.IsoPacket(offset=i * pkt, length=pkt) for i in range(n)]
            info, data, ret_iso = await c.submit(
                direction=W.DIR_IN, ep=D.EP_ISO_IN & 0x7F,
                in_length=n * pkt, iso=iso, interval=4)
            self.assertEqual(info["status"], 0)
            self.assertEqual(info["actual_length"], n * pkt)
            self.assertEqual(len(data), n * pkt)
            self.assertEqual(sum(p.actual_length for p in ret_iso), info["actual_length"])
            self.assertNotEqual(data, b"\0" * len(data))
        finally:
            await c.close()

    async def test_sustained_iso_round_trips(self):
        """100 URBs of 10 packets each in both directions = 1 s of real audio.

        This is a correctness and throughput smoke test only; it is NOT the
        timing measurement, which needs the real driver (experiment E1).
        """
        c = await self.connect()
        try:
            await c.import_device("1-1")
            out_pkt, in_pkt = D.ISO_OUT_BYTES_PER_MS, D.ISO_IN_BYTES_PER_MS
            out_iso = [W.IsoPacket(offset=i * out_pkt, length=out_pkt) for i in range(10)]
            in_iso = [W.IsoPacket(offset=i * in_pkt, length=in_pkt) for i in range(10)]
            for _ in range(100):
                info, _, _ = await c.submit(
                    direction=W.DIR_OUT, ep=D.EP_ISO_OUT,
                    out_data=b"\x11" * (10 * out_pkt), iso=out_iso, interval=4)
                self.assertEqual(info["status"], 0)
                info, data, _ = await c.submit(
                    direction=W.DIR_IN, ep=D.EP_ISO_IN & 0x7F,
                    in_length=10 * in_pkt, iso=in_iso, interval=4)
                self.assertEqual(info["status"], 0)
                self.assertEqual(len(data), 10 * in_pkt)
        finally:
            await c.close()
        self.assertEqual(self.backend.audio_out_packets, 1000)
        self.assertEqual(self.backend.audio_out_bytes, 1000 * D.ISO_OUT_BYTES_PER_MS)

    async def test_stall_is_reported_as_negative_epipe(self):
        c = await self.connect()
        try:
            await c.import_device("1-1")
            info, _, _ = await c.submit(
                direction=W.DIR_IN, ep=0,
                setup=ctrl_setup(0x80, 0x06, 0x0F00, 0, 255), in_length=255)
            self.assertEqual(info["status"], -W.EPIPE)
            self.assertEqual(info["actual_length"], 0)
        finally:
            await c.close()

    async def test_unlink_of_an_unknown_seqnum_is_answered(self):
        c = await self.connect()
        try:
            await c.import_device("1-1")
            await c.send(W.pack_cmd_unlink(999, 0x00010001, W.DIR_OUT, 0, 12345))
            head = await c.reader.readexactly(W.HEADER_SIZE)
            command = int.from_bytes(head[0:4], "big")
            seqnum = int.from_bytes(head[4:8], "big")
            self.assertEqual(command, W.RET_UNLINK)
            self.assertEqual(seqnum, 999)
        finally:
            await c.close()

    async def test_out_of_order_completion_is_possible(self):
        """Two IN URBs in flight at once must both complete, tagged by seqnum."""
        c = await self.connect()
        try:
            await c.import_device("1-1")
            for _ in range(2):
                c.seq += 1
                cmd = W.CmdSubmit(
                    seqnum=c.seq, devid=0x00010001, direction=W.DIR_IN,
                    ep=D.EP_HID_IN & 0x7F, transfer_flags=0,
                    transfer_buffer_length=64, start_frame=0,
                    number_of_packets=-1, interval=6, setup=b"\0" * 8,
                )
                await c.send(W.pack_cmd_submit(cmd))
            seen = set()
            for _ in range(2):
                info, data, _ = await c.recv_ret()
                seen.add(info["seqnum"])
                self.assertEqual(len(data), 64)
            self.assertEqual(seen, {1, 2})
        finally:
            await c.close()


if __name__ == "__main__":
    unittest.main()
