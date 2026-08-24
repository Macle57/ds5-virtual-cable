"""Tests for the isochronous frame clock and the pacing it drives.

These cover the single most important thing Phase 3 learned on real hardware:
over USB/IP + UDE nothing but the emulator paces isochronous traffic, so if
`FrameClock` regresses the audio stream silently runs at the wrong speed.
See `ds5emu/timing.py` for the measurements.
"""

from __future__ import annotations

import time
import unittest

from ds5emu import descriptors as D
from ds5emu import wire as W
from ds5emu.backend import SyntheticBackend
from ds5emu.device import DualSenseDevice
from ds5emu.timing import FrameClock


class FrameClockTests(unittest.TestCase):
    @staticmethod
    def _future_clock():
        """A clock whose epoch is 1000 s away, so `current_frame()` is far
        negative and only the explicit reservations below can win the max."""
        return FrameClock(t0=time.perf_counter() + 1000.0)

    def test_reserve_hands_out_consecutive_intervals(self):
        c = self._future_clock()
        c._next[0x01] = 1000  # pretend we are already booked to frame 1000
        start, deadline = c.reserve(0x01, 10)
        self.assertEqual(start, 1000)
        self.assertAlmostEqual(deadline, c.frame_time(1010), places=9)
        start2, deadline2 = c.reserve(0x01, 10)
        self.assertEqual(start2, 1010)
        # Ten packets is ten service intervals: exactly 10 ms more.
        self.assertAlmostEqual(deadline2 - deadline, 0.010, places=9)

    def test_endpoints_are_scheduled_independently(self):
        c = self._future_clock()
        c._next[0x01] = 5000
        c._next[0x82] = 9000
        self.assertEqual(c.reserve(0x01, 4)[0], 5000)
        self.assertEqual(c.reserve(0x82, 4)[0], 9000)

    def test_a_host_that_falls_behind_resyncs_instead_of_queueing_the_past(self):
        c = FrameClock(t0=time.perf_counter() - 1000.0)  # epoch 1000 s ago
        c._next[0x01] = 1  # far in the past relative to "now"
        start, _ = c.reserve(0x01, 10)
        self.assertGreater(start, 900_000)
        self.assertEqual(c.resyncs[0x01], 1)
        self.assertEqual(len(c.resync_times[0x01]), 1)

    def test_no_resync_while_the_pipe_stays_fed(self):
        c = FrameClock()
        c.reserve(0x01, 10)          # the first one always resyncs
        self.assertEqual(c.resyncs[0x01], 1)
        for _ in range(50):
            c.reserve(0x01, 10)      # 500 ms of audio booked instantly
        self.assertEqual(c.resyncs[0x01], 1)

    def test_backlog_reports_how_far_ahead_we_are(self):
        c = FrameClock()
        for _ in range(11):
            c.reserve(0x01, 10)
        self.assertGreater(c.backlog_frames(0x01), 90)


class IsoPacingTests(unittest.TestCase):
    """The device must hand the transport a deadline, not just bytes."""

    def setUp(self):
        self.dev = DualSenseDevice(SyntheticBackend())

    def _out_cmd(self, npackets):
        return W.CmdSubmit(
            seqnum=1, devid=0, direction=W.DIR_OUT, ep=D.EP_ISO_OUT,
            transfer_flags=0, transfer_buffer_length=npackets * 384,
            start_frame=0, number_of_packets=npackets, interval=1, setup=b"\0" * 8,
        )

    def test_deadline_is_one_millisecond_per_packet(self):
        cmd = self._out_cmd(10)
        descs = [W.IsoPacket(offset=i * 384, length=384) for i in range(10)]
        payload = b"\0" * 3840 + W.pack_iso_packets(descs)

        first = self.dev.handle_submit_ex(cmd, payload)
        second = self.dev.handle_submit_ex(cmd, payload)
        self.assertIsNotNone(first.deadline)
        self.assertIsNotNone(second.deadline)
        # Ten packets is ten service intervals: 10 ms later, to the microsecond.
        self.assertAlmostEqual(second.deadline - first.deadline, 0.010, places=5)

    def test_pacing_can_be_disabled_for_tests(self):
        dev = DualSenseDevice(SyntheticBackend(), pace_iso=False)
        cmd = self._out_cmd(1)
        descs = [W.IsoPacket(offset=0, length=384)]
        res = dev.handle_submit_ex(cmd, b"\0" * 384 + W.pack_iso_packets(descs))
        self.assertIsNone(res.deadline)

    def test_control_and_interrupt_are_never_paced(self):
        cmd = W.CmdSubmit(
            seqnum=1, devid=0, direction=W.DIR_IN, ep=0, transfer_flags=0,
            transfer_buffer_length=18, start_frame=0, number_of_packets=-1,
            interval=0, setup=bytes([0x80, 0x06, 0x00, 0x01, 0x00, 0x00, 18, 0x00]),
        )
        self.assertIsNone(self.dev.handle_submit_ex(cmd, b"").deadline)


class InputReportPacingTests(unittest.TestCase):
    """Interrupt IN must NAK between service intervals, not free-run.

    Unpaced, hidapi measured 15 526 reports/s against the real device's 250 Hz.
    """

    def test_backend_withholds_reports_between_intervals(self):
        b = SyntheticBackend()
        self.assertIsNotNone(b.read_input_report(64))
        self.assertIsNone(b.read_input_report(64))   # 4 ms have not passed
        self.assertIsNone(b.read_input_report(64))

    def test_pacing_can_be_switched_off(self):
        b = SyntheticBackend(input_report_hz=0)
        for _ in range(5):
            self.assertIsNotNone(b.read_input_report(64))

    def test_control_get_report_input_never_stalls_between_intervals(self):
        """GET_REPORT(Input) asks for current state, so it must not depend on a
        fresh report having been produced."""
        dev = DualSenseDevice(SyntheticBackend())
        cmd = W.CmdSubmit(
            seqnum=1, devid=0, direction=W.DIR_IN, ep=0, transfer_flags=0,
            transfer_buffer_length=64, start_frame=0, number_of_packets=-1,
            interval=0,
            # bmRequestType=0xA1 (IN|class|interface), GET_REPORT, Input id 1, iface 3
            setup=bytes([0xA1, 0x01, 0x01, 0x01, 0x03, 0x00, 64, 0x00]),
        )
        for _ in range(5):
            info = W.unpack_ret_submit(dev.handle_submit(cmd, b""))
            self.assertEqual(info["status"], 0)
            self.assertEqual(info["actual_length"], 64)


class EndpointOwnsTheHidInIntervalTests(unittest.TestCase):
    """Phase 3c: the 4 ms gate belongs to endpoint 0x84, not to a backend.

    Phase 3a fixed the free-running interrupt IN endpoint inside
    `SyntheticBackend`. `BridgeBackend` — written in parallel, and correct to
    repeat unchanged state, because a wired DualSense does — therefore answered
    every poll, and the first live attach of the merged emulator measured
    12 684 reports/s against the real 250 Hz. The gate is in `device.py` now,
    so a backend cannot opt out of physics by accident.
    """

    @staticmethod
    def _in_urb():
        return W.CmdSubmit(
            seqnum=1, devid=0, direction=W.DIR_IN, ep=D.EP_HID_IN & 0x7F,
            transfer_flags=0, transfer_buffer_length=64, start_frame=0,
            number_of_packets=-1, interval=6, setup=b"\0" * 8,
        )

    class _AlwaysReady(SyntheticBackend):
        """A backend with no pacing of its own — like the real BridgeBackend,
        which repeats the current state rather than ever saying 'nothing new'."""

        def __init__(self):
            super().__init__(input_report_hz=0)

    def test_an_unpaced_backend_is_still_held_to_250_hz(self):
        dev = DualSenseDevice(self._AlwaysReady())
        cmd = self._in_urb()
        first = W.unpack_ret_submit(dev.handle_submit(cmd, b""))
        self.assertEqual(first["actual_length"], 64)
        for _ in range(20):          # same millisecond: the interval is open
            info = W.unpack_ret_submit(dev.handle_submit(cmd, b""))
            self.assertEqual(info["actual_length"], 0)
        self.assertEqual(dev.stats["hid_in"], 1)

    def test_the_backend_is_not_even_asked_while_the_interval_is_open(self):
        # Otherwise the backend's own freshness bookkeeping — BridgeBackend
        # renumbers the device sequence byte per call — advances 50 counts
        # between two reports the host actually keeps.
        b = self._AlwaysReady()
        dev = DualSenseDevice(b)
        cmd = self._in_urb()
        dev.handle_submit(cmd, b"")
        before = b._counter
        for _ in range(20):
            dev.handle_submit(cmd, b"")
        self.assertEqual(b._counter, before)

    def test_the_interval_opens_again_after_4_ms(self):
        dev = DualSenseDevice(self._AlwaysReady())
        cmd = self._in_urb()
        dev.handle_submit(cmd, b"")
        time.sleep(0.005)
        info = W.unpack_ret_submit(dev.handle_submit(cmd, b""))
        self.assertEqual(info["actual_length"], 64)
        self.assertEqual(dev.stats["hid_in"], 2)

    def test_a_stall_resynchronises_instead_of_bursting(self):
        # After a long gap the endpoint must not deliver the backlog it "owes";
        # a real one simply NAKs until the next interval. Same rule as
        # FrameClock.reserve().
        dev = DualSenseDevice(self._AlwaysReady())
        cmd = self._in_urb()
        dev.handle_submit(cmd, b"")
        dev._next_hid_in_at = time.perf_counter() - 1.0     # pretend a 1 s stall
        self.assertEqual(
            W.unpack_ret_submit(dev.handle_submit(cmd, b""))["actual_length"], 64)
        self.assertEqual(
            W.unpack_ret_submit(dev.handle_submit(cmd, b""))["actual_length"], 0)

    def test_pacing_can_be_switched_off_for_tests(self):
        dev = DualSenseDevice(self._AlwaysReady(), hid_in_hz=0)
        cmd = self._in_urb()
        for _ in range(5):
            info = W.unpack_ret_submit(dev.handle_submit(cmd, b""))
            self.assertEqual(info["actual_length"], 64)


if __name__ == "__main__":
    unittest.main()
