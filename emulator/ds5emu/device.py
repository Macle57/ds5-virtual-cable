"""The emulated wired DualSense as a pure request/response machine.

`DualSenseDevice.handle_submit(cmd, payload) -> bytes` takes a parsed
USBIP_CMD_SUBMIT plus its payload and returns a complete USBIP_RET_SUBMIT
(header + payload). No sockets, no threads, no hardware: the entire USB
behaviour of the device is unit-testable from this one entry point.

Everything device-specific comes from `descriptors.py` (ground truth read off
the physical controller) and everything hardware-facing goes through a
`Backend`.
"""

from __future__ import annotations

import struct
import time
from dataclasses import dataclass

from . import descriptors as D
from . import wire as W
from .backend import Backend
from .timing import FrameClock, UrbMeter, UrbRecord
from .uac import UacState

# ---- bmRequestType ---------------------------------------------------------
RT_DIR_MASK = 0x80
RT_DIR_IN = 0x80
RT_TYPE_MASK = 0x60
RT_TYPE_STANDARD = 0x00
RT_TYPE_CLASS = 0x20
RT_TYPE_VENDOR = 0x40
RT_RECIP_MASK = 0x1F
RT_RECIP_DEVICE = 0x00
RT_RECIP_INTERFACE = 0x01
RT_RECIP_ENDPOINT = 0x02

# ---- standard bRequest -----------------------------------------------------
REQ_GET_STATUS = 0x00
REQ_CLEAR_FEATURE = 0x01
REQ_SET_FEATURE = 0x03
REQ_SET_ADDRESS = 0x05
REQ_GET_DESCRIPTOR = 0x06
REQ_SET_DESCRIPTOR = 0x07
REQ_GET_CONFIGURATION = 0x08
REQ_SET_CONFIGURATION = 0x09
REQ_GET_INTERFACE = 0x0A
REQ_SET_INTERFACE = 0x0B

# ---- descriptor types ------------------------------------------------------
DT_DEVICE = 0x01
DT_CONFIGURATION = 0x02
DT_STRING = 0x03
DT_INTERFACE = 0x04
DT_ENDPOINT = 0x05
DT_DEVICE_QUALIFIER = 0x06
DT_OTHER_SPEED_CONFIG = 0x07
DT_BOS = 0x0F
DT_HID = 0x21
DT_HID_REPORT = 0x22

# ---- HID class bRequest ----------------------------------------------------
HID_GET_REPORT = 0x01
HID_GET_IDLE = 0x02
HID_GET_PROTOCOL = 0x03
HID_SET_REPORT = 0x09
HID_SET_IDLE = 0x0A
HID_SET_PROTOCOL = 0x0B

HID_REPORT_TYPE_INPUT = 1
HID_REPORT_TYPE_OUTPUT = 2
HID_REPORT_TYPE_FEATURE = 3


class Stall(Exception):
    """Raised by a handler to complete the transfer with -EPIPE (STALL)."""


@dataclass(frozen=True)
class SubmitResult:
    """A finished RET_SUBMIT plus, for isochronous URBs, when to send it.

    `deadline` is a `time.perf_counter()` value. `None` means "send now".
    The transport layer is responsible for honouring it; see `timing.py` for
    why the emulator, not the host, owns the isochronous clock.
    """

    reply: bytes
    deadline: float | None = None


class DualSenseDevice:
    """Emulated USB device state machine."""

    #: Micro-frames per millisecond, for the synthetic frame counter.
    FRAMES_PER_SECOND = 1000

    def __init__(self, backend: Backend, pace_iso: bool = True,
                 meter: UrbMeter | None = None):
        self.backend = backend
        self.uac = UacState()
        #: When False the device answers isochronous URBs immediately. That is
        #: only useful for unit tests — against a live UDE client it makes the
        #: audio stream run several times faster than real time (see timing.py).
        self.pace_iso = pace_iso
        self.clock = FrameClock()
        self.meter = meter if meter is not None else UrbMeter()

        self.configuration = 0
        # alt setting per interface; audio streaming interfaces boot at alt 0
        # (zero-bandwidth), which is what the descriptors declare.
        self.alt_setting = {
            D.IFACE_AUDIOCONTROL: 0,
            D.IFACE_AUDIO_OUT: 0,
            D.IFACE_AUDIO_IN: 0,
            D.IFACE_HID: 0,
        }
        self.halted: set[int] = set()
        self.hid_idle = 0
        self.hid_protocol = 1  # report protocol
        self._last_input_report: bytes | None = None

        self._t0 = time.perf_counter()
        self.stats = {
            "control": 0,
            "hid_in": 0,
            "hid_out": 0,
            "iso_out": 0,
            "iso_in": 0,
            "stalled": 0,
        }

    # -- identity ---------------------------------------------------------

    def usbip_device_info(self, busid: str = "1-1", busnum: int = 1, devnum: int = 1,
                          path: str | None = None) -> W.UsbDeviceInfo:
        """The `usbip_usb_device` we advertise in OP_REP_DEVLIST / OP_REP_IMPORT.

        `speed` MUST be USB_SPEED_HIGH. usbip-win2 runs `patch_config()` — which
        rewrites iso bInterval to `min(bInterval + 3, 16)` — only when
        `dev.speed() < USB_SPEED_HIGH`. Reporting anything lower would silently
        turn our 1 ms audio endpoints into 8 ms ones.
        See drivers/ude/wsk_receive.cpp and docs/virtualization-options.md §2.3.
        """
        d = D.DEVICE_DESCRIPTOR
        return W.UsbDeviceInfo(
            path=path or f"/sys/devices/virtual/ds5emu/{busid}",
            busid=busid,
            busnum=busnum,
            devnum=devnum,
            speed=W.USB_SPEED_HIGH,
            idVendor=D.ID_VENDOR,
            idProduct=D.ID_PRODUCT,
            bcdDevice=D.BCD_DEVICE,
            bDeviceClass=d[4],
            bDeviceSubClass=d[5],
            bDeviceProtocol=d[6],
            bConfigurationValue=D.B_CONFIGURATION_VALUE,
            bNumConfigurations=D.B_NUM_CONFIGURATIONS,
            bNumInterfaces=D.B_NUM_INTERFACES,
            interfaces=tuple(W.UsbInterfaceInfo(*i) for i in D.interface_infos()),
        )

    def frame_number(self) -> int:
        """A plausible, monotonically increasing USB frame counter.

        usbip-win2 always sets USBD_START_ISO_TRANSFER_ASAP (it does not
        implement URB_GET_CURRENT_FRAME_NUMBER), and writes our reply's
        `start_frame` straight back into `URB.StartFrame`, so this value has to
        look like a real 1 kHz frame counter.
        """
        return self.clock.current_frame() & 0x7FFFFFFF

    # -- top-level dispatch ------------------------------------------------

    def handle_submit(self, cmd: W.CmdSubmit, payload: bytes) -> bytes:
        """Convenience wrapper: the reply bytes only, pacing discarded.

        Kept because the whole unit-test suite is written against it. The
        transport layer must use `handle_submit_ex` instead, or isochronous
        streams run as fast as the socket allows (timing.py explains why).
        """
        return self.handle_submit_ex(cmd, payload).reply

    def handle_submit_ex(self, cmd: W.CmdSubmit, payload: bytes) -> SubmitResult:
        buf, iso = cmd.split_payload(payload)
        ep = cmd.ep & 0x7F
        try:
            if ep == 0:
                return SubmitResult(self._control(cmd, buf))
            if cmd.is_iso or iso:
                return self._isochronous(cmd, buf, iso)
            return SubmitResult(self._interrupt(cmd, buf))
        except Stall:
            self.stats["stalled"] += 1
            return SubmitResult(self._stall(cmd))

    def _stall(self, cmd: W.CmdSubmit) -> bytes:
        return W.pack_ret_submit(
            seqnum=cmd.seqnum,
            devid=cmd.devid,
            direction=cmd.direction,
            ep=cmd.ep,
            status=-W.EPIPE,
            actual_length=0,
            number_of_packets=0,
        )

    def _ok(self, cmd: W.CmdSubmit, data: bytes = b"") -> bytes:
        return W.pack_ret_submit(
            seqnum=cmd.seqnum,
            devid=cmd.devid,
            direction=cmd.direction,
            ep=cmd.ep,
            status=0,
            actual_length=len(data),
            number_of_packets=0,
            data=data,
        )

    # -- control (endpoint 0) ---------------------------------------------

    def _control(self, cmd: W.CmdSubmit, out_data: bytes) -> bytes:
        self.stats["control"] += 1
        if len(cmd.setup) != 8:
            raise Stall
        bmRequestType, bRequest, wValue, wIndex, wLength = struct.unpack("<BBHHH", cmd.setup)
        is_in = bool(bmRequestType & RT_DIR_IN)
        rtype = bmRequestType & RT_TYPE_MASK

        if rtype == RT_TYPE_STANDARD:
            data = self._standard(bmRequestType, bRequest, wValue, wIndex, wLength, out_data)
        elif rtype == RT_TYPE_CLASS:
            data = self._class(bmRequestType, bRequest, wValue, wIndex, wLength, out_data)
        else:
            data = None  # vendor requests: the real device declares none

        if data is None:
            raise Stall
        if is_in:
            data = data[:wLength]
        else:
            data = b""
        return self._ok(cmd, data)

    def _standard(self, bmRequestType, bRequest, wValue, wIndex, wLength, out_data):
        recipient = bmRequestType & RT_RECIP_MASK

        if bRequest == REQ_GET_DESCRIPTOR and (bmRequestType & RT_DIR_IN):
            return self._get_descriptor(recipient, wValue >> 8, wValue & 0xFF, wIndex)

        if recipient == RT_RECIP_DEVICE:
            if bRequest == REQ_GET_STATUS:
                # bmAttributes 0xC0 in the config descriptor: self-powered, no
                # remote wakeup.
                return struct.pack("<H", 0x0001)
            if bRequest == REQ_SET_CONFIGURATION:
                if (wValue & 0xFF) not in (0, D.B_CONFIGURATION_VALUE):
                    return None
                self.configuration = wValue & 0xFF
                for k in self.alt_setting:
                    self.alt_setting[k] = 0
                self.halted.clear()
                return b""
            if bRequest == REQ_GET_CONFIGURATION:
                return bytes([self.configuration])
            if bRequest in (REQ_SET_FEATURE, REQ_CLEAR_FEATURE):
                return b""
            if bRequest == REQ_SET_ADDRESS:
                # Handled by the host controller; never reaches a real device
                # driver, but accept it rather than stalling.
                return b""
            return None

        if recipient == RT_RECIP_INTERFACE:
            iface = wIndex & 0xFF
            if iface not in self.alt_setting:
                return None
            if bRequest == REQ_GET_STATUS:
                return struct.pack("<H", 0)
            if bRequest == REQ_SET_INTERFACE:
                alt = wValue & 0xFF
                if not self._alt_is_valid(iface, alt):
                    return None
                self.alt_setting[iface] = alt
                return b""
            if bRequest == REQ_GET_INTERFACE:
                return bytes([self.alt_setting[iface]])
            return None

        if recipient == RT_RECIP_ENDPOINT:
            ep = wIndex & 0xFF
            if ep not in (0, D.EP_ISO_OUT, D.EP_ISO_IN, D.EP_HID_IN, D.EP_HID_OUT):
                return None
            if bRequest == REQ_GET_STATUS:
                return struct.pack("<H", 1 if ep in self.halted else 0)
            if bRequest == REQ_CLEAR_FEATURE:
                # ENDPOINT_HALT == feature selector 0
                self.halted.discard(ep)
                return b""
            if bRequest == REQ_SET_FEATURE:
                self.halted.add(ep)
                return b""
            return None

        return None

    def _alt_is_valid(self, iface: int, alt: int) -> bool:
        if iface in (D.IFACE_AUDIO_OUT, D.IFACE_AUDIO_IN):
            return alt in (0, 1)
        return alt == 0

    def _get_descriptor(self, recipient, dtype, index, wIndex):
        if dtype == DT_DEVICE:
            return D.DEVICE_DESCRIPTOR
        if dtype == DT_CONFIGURATION:
            if index != 0:
                return None
            return D.CONFIG_DESCRIPTOR
        if dtype == DT_STRING:
            return D.string_descriptor(index)
        if dtype == DT_HID_REPORT:
            # Recipient is the interface; only interface 3 has a HID descriptor.
            if (wIndex & 0xFF) != D.IFACE_HID:
                return None
            return D.HID_REPORT_DESCRIPTOR
        if dtype == DT_HID:
            if (wIndex & 0xFF) != D.IFACE_HID:
                return None
            return self._hid_descriptor()
        # DEVICE_QUALIFIER / OTHER_SPEED / BOS: the real device has none. Phase 0
        # confirmed there is no BOS descriptor and no MS OS string at 0xEE, so a
        # STALL here is the correct, ground-truth behaviour.
        return None

    @staticmethod
    def _hid_descriptor() -> bytes:
        """The 9-byte HID descriptor embedded in interface 3 of the config."""
        for dtype, d in D.iter_descriptors(D.CONFIG_DESCRIPTOR):
            if dtype == DT_HID:
                return d
        raise AssertionError("no HID descriptor in the configuration descriptor")

    def _class(self, bmRequestType, bRequest, wValue, wIndex, wLength, out_data):
        recipient = bmRequestType & RT_RECIP_MASK
        iface = wIndex & 0xFF

        # HID class requests target interface 3.
        if recipient == RT_RECIP_INTERFACE and iface == D.IFACE_HID:
            return self._hid_class(bRequest, wValue, wLength, out_data)

        # Everything else class-typed belongs to the audio function: the
        # AudioControl interface (0) for unit controls, or the streaming
        # endpoints (which declare no controls, so uac.py stalls them).
        return self.uac.handle(bmRequestType, bRequest, wValue, wIndex, wLength, out_data)

    def _hid_class(self, bRequest, wValue, wLength, out_data):
        report_type = (wValue >> 8) & 0xFF
        report_id = wValue & 0xFF

        if bRequest == HID_GET_REPORT:
            if report_type == HID_REPORT_TYPE_FEATURE:
                body = self.backend.get_feature_report(report_id, wLength)
                return body
            if report_type == HID_REPORT_TYPE_INPUT:
                # A control GET_REPORT(Input) asks for the *current* state, so
                # unlike the interrupt endpoint it must not depend on a new
                # report having been produced. The backend paces reports at
                # 250 Hz and returns None in between, so fall back to the last
                # one we saw rather than stalling the control transfer.
                report = self.backend.read_input_report(wLength)
                if report is None:
                    return self._last_input_report
                self._last_input_report = report
                return report
            return None

        if bRequest == HID_SET_REPORT:
            if report_type == HID_REPORT_TYPE_FEATURE:
                self.backend.set_feature_report(report_id, out_data)
                return b""
            if report_type == HID_REPORT_TYPE_OUTPUT:
                self.backend.write_output_report(out_data)
                return b""
            return None

        if bRequest == HID_GET_IDLE:
            return bytes([self.hid_idle])
        if bRequest == HID_SET_IDLE:
            self.hid_idle = (wValue >> 8) & 0xFF
            return b""
        if bRequest == HID_GET_PROTOCOL:
            return bytes([self.hid_protocol])
        if bRequest == HID_SET_PROTOCOL:
            self.hid_protocol = wValue & 0xFF
            return b""
        return None

    # -- interrupt endpoints ----------------------------------------------

    def _interrupt(self, cmd: W.CmdSubmit, out_data: bytes) -> bytes:
        ep = cmd.ep & 0x7F
        if cmd.is_in:
            if ep != (D.EP_HID_IN & 0x7F):
                raise Stall
            n = min(cmd.transfer_buffer_length, D.HID_MAX_PACKET)
            report = self.backend.read_input_report(n)
            if report is None:
                # Nothing queued. Returning a zero-length transfer is legal and
                # is what the server layer relies on when it decides not to
                # block; the server may also choose to retry instead.
                return self._ok(cmd, b"")
            self.stats["hid_in"] += 1
            self._last_input_report = report
            return self._ok(cmd, report[:n])

        if ep != D.EP_HID_OUT:
            raise Stall
        self.stats["hid_out"] += 1
        self.backend.write_output_report(out_data)
        return W.pack_ret_submit(
            seqnum=cmd.seqnum,
            devid=cmd.devid,
            direction=cmd.direction,
            ep=cmd.ep,
            status=0,
            actual_length=len(out_data),
            number_of_packets=0,
        )

    # -- isochronous endpoints --------------------------------------------

    def _isochronous(self, cmd: W.CmdSubmit, out_data: bytes, iso) -> SubmitResult:
        """Reserve service intervals, build the reply, and say when to send it.

        THE PACING IS NOT OPTIONAL. There is no SOF on a UDE bus and
        usbip2_filter answers QueryBusTime with a constant, so if we complete
        these URBs as fast as they arrive the audio stack simply runs the stream
        at socket speed — measured 3.63x real time on 2026-08-24. See timing.py.
        """
        ep = cmd.ep & 0x7F
        t_recv = time.perf_counter()
        start_frame, deadline = self.clock.reserve(cmd.ep, len(iso))

        if cmd.is_in:
            if ep != (D.EP_ISO_IN & 0x7F):
                raise Stall
            reply = self._iso_in(cmd, iso, start_frame)
            nbytes = W.unpack_ret_submit(reply)["actual_length"]
        else:
            if ep != D.EP_ISO_OUT:
                raise Stall
            reply = self._iso_out(cmd, out_data, iso, start_frame)
            nbytes = len(out_data)

        self.meter.record(cmd.ep, UrbRecord(
            t_recv=t_recv, t_done=time.perf_counter(), packets=len(iso),
            nbytes=nbytes, start_frame=start_frame,
        ))
        return SubmitResult(reply, deadline if self.pace_iso else None)

    def _iso_out(self, cmd: W.CmdSubmit, out_data: bytes, iso, start_frame: int) -> bytes:
        """Speaker + haptics stream from the host.

        Per-packet `length` in the request tells us how the host chopped the
        buffer up; hand each slice to the backend so it can keep its own
        timebase. The reply carries descriptors only — never a data payload —
        and each descriptor must echo the offset the host sent, because
        usbip-win2's validate() hard-fails on `sd.offset != dd.Offset`.
        """
        replies = []
        for p in iso:
            chunk = out_data[p.offset : p.offset + p.length]
            if chunk:
                self.backend.write_audio_out(chunk)
            replies.append(
                W.IsoPacket(offset=p.offset, length=p.length,
                            actual_length=len(chunk), status=0)
            )
        self.stats["iso_out"] += len(iso)
        return W.pack_ret_submit(
            seqnum=cmd.seqnum,
            devid=cmd.devid,
            direction=cmd.direction,
            ep=cmd.ep,
            status=0,
            actual_length=sum(r.actual_length for r in replies),
            start_frame=start_frame,
            number_of_packets=len(replies),
            error_count=0,
            data=b"",
            iso_packets=replies,
        )

    def _iso_in(self, cmd: W.CmdSubmit, iso, start_frame: int) -> bytes:
        """Microphone stream to the host.

        The returned data buffer is **compacted**: packets are concatenated
        with no padding, and `RET_SUBMIT.actual_length` is the sum of the
        per-packet actual lengths. The descriptors keep the *original* offsets;
        that mismatch is deliberate and is exactly what usbip-win2 expects
        ("Buffer from the server has no gaps (compacted) ... as the packet
        offsets are not changed there will be padding between the packets").
        """
        chunks = []
        replies = []
        for p in iso:
            # ONE SERVICE INTERVAL OF AUDIO, not one wMaxPacketSize.
            # The host asks for p.length = wMaxPacketSize = 196 B, but 196 B is
            # 49 stereo frames, and returning that every 1 ms runs the mic at
            # 49 kHz. Measured 2026-08-24: WASAPI reported 48 983 frames/s
            # against a nominal 48 000 until this clamp was added. The 4 bytes of
            # headroom in wMaxPacketSize exist so an *asynchronous* endpoint can
            # occasionally send one extra frame to express clock drift; our
            # frame clock is exactly 48 kHz, so we never need it.
            n = min(p.length, D.ISO_IN_BYTES_PER_MS)
            data = self.backend.read_audio_in(n) if n else b""
            data = data[:n]
            chunks.append(data)
            replies.append(
                W.IsoPacket(offset=p.offset, length=p.length,
                            actual_length=len(data), status=0)
            )
        payload = b"".join(chunks)
        self.stats["iso_in"] += len(iso)
        return W.pack_ret_submit(
            seqnum=cmd.seqnum,
            devid=cmd.devid,
            direction=cmd.direction,
            ep=cmd.ep,
            status=0,
            actual_length=len(payload),
            start_frame=start_frame,
            number_of_packets=len(replies),
            error_count=0,
            data=payload,
            iso_packets=replies,
        )
