"""USB/IP wire protocol — pure bytes in / bytes out.

Every structure here was transcribed from usbip-win2's own headers rather than
from prose, because that is the client we will actually be talking to:

    include/usbip/proto.h      header_basic, header_cmd_submit, header_ret_submit,
                               header_cmd_unlink, header_ret_unlink,
                               iso_packet_descriptor, max_iso_packets
    include/usbip/proto_op.h   op_common, usbip_usb_device, usbip_usb_interface,
                               op_import_request/reply, op_devlist_request/reply
    include/usbip/consts.h     USBIP_VERSION, DEV_PATH_MAX, BUS_ID_SIZE, tcp_port

    https://github.com/vadimgrn/usbip-win2/tree/master/include/usbip

Everything on the wire is **network byte order** (big endian).

This module has no I/O and no dependencies beyond the stdlib, so the whole
protocol is unit-testable with no driver installed.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

# --------------------------------------------------------------------------
# constants
# --------------------------------------------------------------------------

USBIP_VERSION = 0x0111
TCP_PORT = 3240

DEV_PATH_MAX = 256
BUS_ID_SIZE = 32

# op_common.code
OP_REQUEST = 0x80 << 8
OP_REPLY = 0x00 << 8
OP_IMPORT = 3
OP_DEVLIST = 5
OP_REQ_IMPORT = OP_REQUEST | OP_IMPORT   # 0x8003
OP_REP_IMPORT = OP_REPLY | OP_IMPORT     # 0x0003
OP_REQ_DEVLIST = OP_REQUEST | OP_DEVLIST  # 0x8005
OP_REP_DEVLIST = OP_REPLY | OP_DEVLIST    # 0x0005

# op_common.status (usbip::op_status_t)
ST_OK = 0
ST_NA = 1
ST_DEV_BUSY = 2
ST_DEV_ERR = 3
ST_NODEV = 4
ST_ERROR = 5

# header_basic.command (usbip::request_type)
CMD_SUBMIT = 1
CMD_UNLINK = 2
RET_SUBMIT = 3
RET_UNLINK = 4

# header_basic.direction
DIR_OUT = 0
DIR_IN = 1

# enum usb_device_speed (Linux <linux/usb/ch9.h>) — the value that lands in
# usbip_usb_device.speed.  usbip-win2 only skips its descriptor-rewriting
# patch_config() when speed >= USB_SPEED_HIGH, so this MUST be HIGH for us.
USB_SPEED_UNKNOWN = 0
USB_SPEED_LOW = 1
USB_SPEED_FULL = 2
USB_SPEED_HIGH = 3
USB_SPEED_WIRELESS = 4
USB_SPEED_SUPER = 5
USB_SPEED_SUPER_PLUS = 6

MAX_ISO_PACKETS = 1024
NUMBER_OF_PACKETS_NON_ISOCH = -1

# Linux errno values, negated, as they appear in RET_SUBMIT.status.
EPIPE = 32          # -EPIPE == STALL
ENOENT = 2
ECONNRESET = 104
ESHUTDOWN = 108

# --------------------------------------------------------------------------
# struct formats
# --------------------------------------------------------------------------

_OP_COMMON = struct.Struct(">HHI")                     # 8
_USB_DEVICE = struct.Struct(">256s32sIIIHHHBBBBBB")    # 312
_USB_INTERFACE = struct.Struct(">BBBB")                # 4
_DEVLIST_REPLY = struct.Struct(">I")                   # 4
_IMPORT_REQUEST = struct.Struct(">32s")                # 32

_HDR_BASIC = struct.Struct(">IIIII")                   # 20
_CMD_SUBMIT_EXTRA = struct.Struct(">Iiiii8s")          # 28
_RET_SUBMIT_EXTRA = struct.Struct(">iiiii8x")          # 28 (20 + 8 padding)
_CMD_UNLINK_EXTRA = struct.Struct(">I24x")             # 28
_RET_UNLINK_EXTRA = struct.Struct(">i24x")             # 28

_ISO_DESC = struct.Struct(">IIII")                     # 16

OP_COMMON_SIZE = _OP_COMMON.size
USB_DEVICE_SIZE = _USB_DEVICE.size
USB_INTERFACE_SIZE = _USB_INTERFACE.size
HEADER_SIZE = _HDR_BASIC.size + _CMD_SUBMIT_EXTRA.size   # 48 for every command
ISO_DESC_SIZE = _ISO_DESC.size

assert OP_COMMON_SIZE == 8
assert USB_DEVICE_SIZE == 312
assert HEADER_SIZE == 48
assert _RET_SUBMIT_EXTRA.size == _CMD_SUBMIT_EXTRA.size == 28
assert ISO_DESC_SIZE == 16


def _fixed(text: str, size: int) -> bytes:
    """NUL-padded fixed-width ASCII field, always NUL-terminated."""
    raw = text.encode("ascii", "strict")
    if len(raw) >= size:
        raise ValueError(f"{text!r} does not fit in {size} bytes with a NUL")
    return raw + b"\0" * (size - len(raw))


def _unfixed(raw: bytes) -> str:
    return raw.split(b"\0", 1)[0].decode("ascii", "replace")


# --------------------------------------------------------------------------
# op phase
# --------------------------------------------------------------------------


def pack_op_common(code: int, status: int = ST_OK, version: int = USBIP_VERSION) -> bytes:
    return _OP_COMMON.pack(version, code, status)


def unpack_op_common(buf: bytes) -> tuple[int, int, int]:
    """-> (version, code, status)"""
    if len(buf) < OP_COMMON_SIZE:
        raise ValueError("short op_common")
    return _OP_COMMON.unpack_from(buf)


@dataclass(frozen=True)
class UsbInterfaceInfo:
    bInterfaceClass: int
    bInterfaceSubClass: int
    bInterfaceProtocol: int

    def pack(self) -> bytes:
        return _USB_INTERFACE.pack(
            self.bInterfaceClass, self.bInterfaceSubClass, self.bInterfaceProtocol, 0
        )


@dataclass(frozen=True)
class UsbDeviceInfo:
    """usbip_usb_device — 312 bytes on the wire.

    `path` is cosmetic (the Linux server puts a sysfs path there); `busid` is
    NOT cosmetic: usbip-win2's driver compares the busid echoed in
    OP_REP_IMPORT against what it asked for and aborts on mismatch
    (drivers/ude/vhci_ioctl.cpp, "Received busid '%s' != '%s'").
    """

    path: str
    busid: str
    busnum: int
    devnum: int
    speed: int
    idVendor: int
    idProduct: int
    bcdDevice: int
    bDeviceClass: int
    bDeviceSubClass: int
    bDeviceProtocol: int
    bConfigurationValue: int
    bNumConfigurations: int
    bNumInterfaces: int
    interfaces: tuple[UsbInterfaceInfo, ...] = field(default=())

    @property
    def devid(self) -> int:
        """header_basic.devid as the Linux stub computes it."""
        return ((self.busnum & 0xFFFF) << 16) | (self.devnum & 0xFFFF)

    def pack(self) -> bytes:
        return _USB_DEVICE.pack(
            _fixed(self.path, DEV_PATH_MAX),
            _fixed(self.busid, BUS_ID_SIZE),
            self.busnum,
            self.devnum,
            self.speed,
            self.idVendor,
            self.idProduct,
            self.bcdDevice,
            self.bDeviceClass,
            self.bDeviceSubClass,
            self.bDeviceProtocol,
            self.bConfigurationValue,
            self.bNumConfigurations,
            self.bNumInterfaces,
        )

    def pack_with_interfaces(self) -> bytes:
        return self.pack() + b"".join(i.pack() for i in self.interfaces)

    @classmethod
    def unpack(cls, buf: bytes) -> "UsbDeviceInfo":
        f = _USB_DEVICE.unpack_from(buf)
        return cls(_unfixed(f[0]), _unfixed(f[1]), *f[2:])


def pack_devlist_reply(devices) -> bytes:
    devices = list(devices)
    out = [pack_op_common(OP_REP_DEVLIST, ST_OK), _DEVLIST_REPLY.pack(len(devices))]
    out += [d.pack_with_interfaces() for d in devices]
    return b"".join(out)


def pack_import_reply(dev: UsbDeviceInfo | None) -> bytes:
    """On failure the reply is op_common alone — no usbip_usb_device follows."""
    if dev is None:
        return pack_op_common(OP_REP_IMPORT, ST_NODEV)
    return pack_op_common(OP_REP_IMPORT, ST_OK) + dev.pack()


def pack_import_request(busid: str) -> bytes:
    return pack_op_common(OP_REQ_IMPORT) + _IMPORT_REQUEST.pack(_fixed(busid, BUS_ID_SIZE))


def unpack_import_request_body(buf: bytes) -> str:
    (raw,) = _IMPORT_REQUEST.unpack_from(buf)
    return _unfixed(raw)


# --------------------------------------------------------------------------
# command phase
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class IsoPacket:
    offset: int
    length: int
    actual_length: int = 0
    status: int = 0

    def pack(self) -> bytes:
        return _ISO_DESC.pack(self.offset, self.length, self.actual_length, self.status)

    @classmethod
    def unpack_from(cls, buf: bytes, off: int) -> "IsoPacket":
        return cls(*_ISO_DESC.unpack_from(buf, off))


def pack_iso_packets(packets) -> bytes:
    return b"".join(p.pack() for p in packets)


def unpack_iso_packets(buf: bytes, count: int, off: int = 0) -> list[IsoPacket]:
    if count < 0 or count > MAX_ISO_PACKETS:
        raise ValueError(f"number_of_packets {count} out of range")
    if len(buf) - off < count * ISO_DESC_SIZE:
        raise ValueError("short iso descriptor array")
    return [IsoPacket.unpack_from(buf, off + i * ISO_DESC_SIZE) for i in range(count)]


@dataclass(frozen=True)
class CmdSubmit:
    """A parsed USBIP_CMD_SUBMIT header (48 bytes), without its payload."""

    seqnum: int
    devid: int
    direction: int
    ep: int
    transfer_flags: int
    transfer_buffer_length: int
    start_frame: int
    number_of_packets: int
    interval: int
    setup: bytes

    @property
    def is_in(self) -> bool:
        return self.direction == DIR_IN

    @property
    def is_iso(self) -> bool:
        # usbip-win2 sends -1 for non-isochronous; be liberal and treat any
        # negative value as "not iso".
        return self.number_of_packets >= 0 and self.ep != 0 and self.number_of_packets > 0

    @property
    def iso_count(self) -> int:
        return self.number_of_packets if self.number_of_packets > 0 else 0

    def payload_size(self) -> int:
        """Bytes that follow the 48-byte header for this command.

        Layout (usbip-win2 drivers/ude/device_ioctl.cpp::prepare_wsk_buf builds
        exactly this MDL chain): header, then the transfer buffer for OUT only,
        then the iso descriptor array for isochronous transfers.
        """
        n = 0
        if self.direction == DIR_OUT:
            n += self.transfer_buffer_length
        n += self.iso_count * ISO_DESC_SIZE
        return n

    def split_payload(self, payload: bytes) -> tuple[bytes, list[IsoPacket]]:
        """-> (transfer buffer, iso descriptors)"""
        if len(payload) != self.payload_size():
            raise ValueError(
                f"payload {len(payload)} != expected {self.payload_size()} "
                f"(seq {self.seqnum}, ep {self.ep})"
            )
        if self.direction == DIR_OUT:
            buf = payload[: self.transfer_buffer_length]
            rest = payload[self.transfer_buffer_length :]
        else:
            buf = b""
            rest = payload
        return buf, unpack_iso_packets(rest, self.iso_count)


@dataclass(frozen=True)
class CmdUnlink:
    seqnum: int
    devid: int
    direction: int
    ep: int
    unlink_seqnum: int


def unpack_header(buf: bytes):
    """Parse a 48-byte command header into CmdSubmit / CmdUnlink.

    Returns (command, parsed) where `parsed` is None for commands we do not
    expect to receive as a server (RET_SUBMIT / RET_UNLINK).
    """
    if len(buf) < HEADER_SIZE:
        raise ValueError("short usbip header")
    command, seqnum, devid, direction, ep = _HDR_BASIC.unpack_from(buf, 0)
    if command == CMD_SUBMIT:
        (
            transfer_flags,
            transfer_buffer_length,
            start_frame,
            number_of_packets,
            interval,
            setup,
        ) = _CMD_SUBMIT_EXTRA.unpack_from(buf, _HDR_BASIC.size)
        if number_of_packets > MAX_ISO_PACKETS:
            raise ValueError(f"number_of_packets {number_of_packets} > {MAX_ISO_PACKETS}")
        if transfer_buffer_length < 0:
            raise ValueError("negative transfer_buffer_length")
        return command, CmdSubmit(
            seqnum=seqnum,
            devid=devid,
            direction=direction,
            ep=ep,
            transfer_flags=transfer_flags,
            transfer_buffer_length=transfer_buffer_length,
            start_frame=start_frame,
            number_of_packets=number_of_packets,
            interval=interval,
            setup=setup,
        )
    if command == CMD_UNLINK:
        (unlink_seqnum,) = _CMD_UNLINK_EXTRA.unpack_from(buf, _HDR_BASIC.size)
        return command, CmdUnlink(seqnum, devid, direction, ep, unlink_seqnum)
    return command, None


def pack_cmd_submit(cmd: CmdSubmit) -> bytes:
    """Only used by tests / a fake client; a server never sends this."""
    return _HDR_BASIC.pack(CMD_SUBMIT, cmd.seqnum, cmd.devid, cmd.direction, cmd.ep) + (
        _CMD_SUBMIT_EXTRA.pack(
            cmd.transfer_flags,
            cmd.transfer_buffer_length,
            cmd.start_frame,
            cmd.number_of_packets,
            cmd.interval,
            cmd.setup.ljust(8, b"\0")[:8],
        )
    )


def pack_cmd_unlink(seqnum: int, devid: int, direction: int, ep: int, unlink_seqnum: int) -> bytes:
    return _HDR_BASIC.pack(CMD_UNLINK, seqnum, devid, direction, ep) + _CMD_UNLINK_EXTRA.pack(
        unlink_seqnum
    )


def pack_ret_submit(
    *,
    seqnum: int,
    devid: int = 0,
    direction: int = DIR_OUT,
    ep: int = 0,
    status: int = 0,
    actual_length: int = 0,
    start_frame: int = 0,
    number_of_packets: int = 0,
    error_count: int = 0,
    data: bytes = b"",
    iso_packets=(),
) -> bytes:
    """Build a complete USBIP_RET_SUBMIT (header + payload).

    Payload layout, mirroring drivers/ude/wsk_receive.cpp
    ("Layout: transfer buffer(IN only), usbip_iso_packet_descriptor[]"):
    the IN data first, then the iso descriptor array.

    Note on `number_of_packets`: usbip-win2 normalises -1 to 0 on receive and
    otherwise demands 0..1024, so 0 is the safe encoding for a non-isochronous
    reply.  The device layer passes 0 for everything non-iso.
    """
    iso_packets = list(iso_packets)
    if iso_packets and number_of_packets != len(iso_packets):
        raise ValueError("number_of_packets does not match the descriptor array")
    hdr = _HDR_BASIC.pack(RET_SUBMIT, seqnum, devid, direction, ep) + _RET_SUBMIT_EXTRA.pack(
        status, actual_length, start_frame, number_of_packets, error_count
    )
    return hdr + data + pack_iso_packets(iso_packets)


def pack_ret_unlink(*, seqnum: int, devid: int = 0, direction: int = DIR_OUT,
                    ep: int = 0, status: int = 0) -> bytes:
    return _HDR_BASIC.pack(RET_UNLINK, seqnum, devid, direction, ep) + _RET_UNLINK_EXTRA.pack(
        status
    )


def unpack_ret_submit(buf: bytes):
    """Parse a RET_SUBMIT header. Used by tests acting as a fake client."""
    command, seqnum, devid, direction, ep = _HDR_BASIC.unpack_from(buf, 0)
    if command != RET_SUBMIT:
        raise ValueError(f"expected RET_SUBMIT, got command {command}")
    status, actual_length, start_frame, number_of_packets, error_count = (
        _RET_SUBMIT_EXTRA.unpack_from(buf, _HDR_BASIC.size)
    )
    return {
        "seqnum": seqnum,
        "devid": devid,
        "direction": direction,
        "ep": ep,
        "status": status,
        "actual_length": actual_length,
        "start_frame": start_frame,
        "number_of_packets": number_of_packets,
        "error_count": error_count,
    }
