"""Dump the FULL USB descriptor set of the wired DualSense, without touching drivers.

Technique (same as Microsoft's USBView): enumerate USB hubs via SetupAPI, then
ask each hub for its per-port connection info and descriptors with
IOCTL_USB_GET_NODE_CONNECTION_INFORMATION_EX / _GET_DESCRIPTOR_FROM_NODE_CONNECTION.
This is read-only and needs no driver change -- the audio/HID class drivers keep
owning the device.

    python prototype/tools/usb_descriptors.py             # DualSense only
    python prototype/tools/usb_descriptors.py --vid 054C --pid 0CE6
    python prototype/tools/usb_descriptors.py --all       # every USB device

Output is markdown-ish so it can be pasted into docs/usb-ground-truth.md.
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import sys

# ---------------------------------------------------------------------------
# Win32 plumbing
# ---------------------------------------------------------------------------

setupapi = ctypes.WinDLL("setupapi", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

INVALID_HANDLE_VALUE = wt.HANDLE(-1).value
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3

DIGCF_PRESENT = 0x02
DIGCF_DEVICEINTERFACE = 0x10

FILE_DEVICE_USB = 0x22
METHOD_BUFFERED = 0
FILE_ANY_ACCESS = 0


def CTL_CODE(dev, func, method, access):
    return (dev << 16) | (access << 14) | (func << 2) | method


USB_GET_NODE_CONNECTION_INFORMATION_EX = 274
USB_GET_DESCRIPTOR_FROM_NODE_CONNECTION = 260
USB_GET_NODE_CONNECTION_NAME = 261
USB_GET_NODE_INFORMATION = 258

IOCTL_USB_GET_NODE_CONNECTION_INFORMATION_EX = CTL_CODE(
    FILE_DEVICE_USB, USB_GET_NODE_CONNECTION_INFORMATION_EX, METHOD_BUFFERED, FILE_ANY_ACCESS
)
IOCTL_USB_GET_DESCRIPTOR_FROM_NODE_CONNECTION = CTL_CODE(
    FILE_DEVICE_USB, USB_GET_DESCRIPTOR_FROM_NODE_CONNECTION, METHOD_BUFFERED, FILE_ANY_ACCESS
)
IOCTL_USB_GET_NODE_CONNECTION_NAME = CTL_CODE(
    FILE_DEVICE_USB, USB_GET_NODE_CONNECTION_NAME, METHOD_BUFFERED, FILE_ANY_ACCESS
)
IOCTL_USB_GET_NODE_INFORMATION = CTL_CODE(
    FILE_DEVICE_USB, USB_GET_NODE_INFORMATION, METHOD_BUFFERED, FILE_ANY_ACCESS
)

GUID_DEVINTERFACE_USB_HUB = "{f18a0e88-c30c-11d0-8815-00a0c906bed8}"


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wt.DWORD),
        ("Data2", wt.WORD),
        ("Data3", wt.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    @classmethod
    def from_string(cls, s: str) -> "GUID":
        s = s.strip("{}")
        p = s.split("-")
        d4 = bytes.fromhex(p[3] + p[4])
        return cls(int(p[0], 16), int(p[1], 16), int(p[2], 16), (ctypes.c_ubyte * 8)(*d4))


class SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.DWORD),
        ("InterfaceClassGuid", GUID),
        ("Flags", wt.DWORD),
        ("Reserved", ctypes.POINTER(ctypes.c_ulonglong)),
    ]


class SP_DEVICE_INTERFACE_DETAIL_DATA_W(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("DevicePath", wt.WCHAR * 1)]


setupapi.SetupDiGetClassDevsW.restype = wt.HANDLE
setupapi.SetupDiGetClassDevsW.argtypes = [
    ctypes.POINTER(GUID), wt.LPCWSTR, wt.HWND, wt.DWORD
]
kernel32.CreateFileW.restype = wt.HANDLE
kernel32.CreateFileW.argtypes = [
    wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p, wt.DWORD, wt.DWORD, wt.HANDLE
]
setupapi.SetupDiEnumDeviceInterfaces.restype = wt.BOOL
setupapi.SetupDiEnumDeviceInterfaces.argtypes = [
    wt.HANDLE, ctypes.c_void_p, ctypes.POINTER(GUID), wt.DWORD,
    ctypes.POINTER(SP_DEVICE_INTERFACE_DATA),
]
setupapi.SetupDiGetDeviceInterfaceDetailW.restype = wt.BOOL
setupapi.SetupDiGetDeviceInterfaceDetailW.argtypes = [
    wt.HANDLE, ctypes.POINTER(SP_DEVICE_INTERFACE_DATA), ctypes.c_void_p,
    wt.DWORD, ctypes.POINTER(wt.DWORD), ctypes.c_void_p,
]
setupapi.SetupDiDestroyDeviceInfoList.restype = wt.BOOL
setupapi.SetupDiDestroyDeviceInfoList.argtypes = [wt.HANDLE]
kernel32.DeviceIoControl.restype = wt.BOOL
kernel32.DeviceIoControl.argtypes = [
    wt.HANDLE, wt.DWORD, ctypes.c_void_p, wt.DWORD, ctypes.c_void_p, wt.DWORD,
    ctypes.POINTER(wt.DWORD), ctypes.c_void_p,
]
kernel32.CloseHandle.restype = wt.BOOL
kernel32.CloseHandle.argtypes = [wt.HANDLE]


def hub_paths() -> list[str]:
    guid = GUID.from_string(GUID_DEVINTERFACE_USB_HUB)
    hdev = setupapi.SetupDiGetClassDevsW(
        ctypes.byref(guid), None, None, DIGCF_PRESENT | DIGCF_DEVICEINTERFACE
    )
    if hdev == INVALID_HANDLE_VALUE:
        raise ctypes.WinError(ctypes.get_last_error())
    paths = []
    try:
        i = 0
        while True:
            did = SP_DEVICE_INTERFACE_DATA()
            did.cbSize = ctypes.sizeof(did)
            if not setupapi.SetupDiEnumDeviceInterfaces(
                hdev, None, ctypes.byref(guid), i, ctypes.byref(did)
            ):
                break
            need = wt.DWORD(0)
            setupapi.SetupDiGetDeviceInterfaceDetailW(
                hdev, ctypes.byref(did), None, 0, ctypes.byref(need), None
            )
            buf = ctypes.create_string_buffer(need.value)
            detail = ctypes.cast(buf, ctypes.POINTER(SP_DEVICE_INTERFACE_DETAIL_DATA_W))
            detail.contents.cbSize = 8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 6
            if setupapi.SetupDiGetDeviceInterfaceDetailW(
                hdev, ctypes.byref(did), detail, need.value, ctypes.byref(need), None
            ):
                paths.append(ctypes.wstring_at(ctypes.addressof(buf) + 4))
            i += 1
    finally:
        setupapi.SetupDiDestroyDeviceInfoList(hdev)
    return paths


def open_dev(path: str):
    h = kernel32.CreateFileW(
        path, GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE, None,
        OPEN_EXISTING, 0, None,
    )
    if h == INVALID_HANDLE_VALUE:
        return None
    return h


def ioctl(h, code: int, inbuf: bytes, outlen: int) -> bytes | None:
    out = ctypes.create_string_buffer(outlen)
    ret = wt.DWORD(0)
    ok = kernel32.DeviceIoControl(
        wt.HANDLE(h), wt.DWORD(code),
        ctypes.c_char_p(inbuf), wt.DWORD(len(inbuf)),
        out, wt.DWORD(outlen), ctypes.byref(ret), None,
    )
    if not ok:
        return None
    return out.raw[: ret.value]


# ---------------------------------------------------------------------------
# USB structures
# ---------------------------------------------------------------------------

# USB_NODE_CONNECTION_INFORMATION_EX:
#   ULONG ConnectionIndex; USB_DEVICE_DESCRIPTOR(18) DeviceDescriptor;
#   UCHAR CurrentConfigurationValue; UCHAR Speed; BOOLEAN DeviceIsHub;
#   USHORT DeviceAddress; ULONG NumberOfOpenPipes; USB_CONNECTION_STATUS;
#   USB_PIPE_INFO PipeList[0]
NCIEX_HEADER = 4 + 18 + 1 + 1 + 1 + 2 + 4 + 4  # 35 -> struct is packed(1) in the DDK
NCIEX_SIZE = 35 + 20 * 30

SPEEDS = {0: "Low", 1: "Full", 2: "High", 3: "Super"}
CONN_STATUS = {
    0: "NoDeviceConnected", 1: "DeviceConnected", 2: "DeviceFailedEnumeration",
    3: "DeviceGeneralFailure", 4: "DeviceCausedOvercurrent",
    5: "DeviceNotEnoughPower", 6: "DeviceNotEnoughBandwidth",
    7: "DeviceHubNestedTooDeeply", 8: "DeviceInLegacyHub",
}

DESC_DEVICE = 1
DESC_CONFIG = 2
DESC_STRING = 3


def get_descriptor(hub, port: int, desc_type: int, index: int, langid: int, length: int) -> bytes | None:
    # USB_DESCRIPTOR_REQUEST { ULONG ConnectionIndex;
    #   USB_DEFAULT_PIPE_SETUP_PACKET { UCHAR bmRequest, bRequest; USHORT wValue, wIndex, wLength; }
    #   UCHAR Data[0]; }
    import struct as _s

    hdr = _s.pack(
        "<IBBHHH", port, 0x80, 0x06, (desc_type << 8) | index, langid, length
    )
    total = len(hdr) + length
    res = ioctl(hub, IOCTL_USB_GET_DESCRIPTOR_FROM_NODE_CONNECTION, hdr + b"\x00" * length, total)
    if not res or len(res) <= len(hdr):
        return None
    return res[len(hdr):]


def get_hid_report_descriptor(hub, port: int, interface: int, length: int) -> bytes | None:
    """GET_DESCRIPTOR(type=0x22 HID REPORT) with interface recipient (bmRequest 0x81).

    This is the REAL report descriptor as the device emits it. Note that hidapi's
    get_report_descriptor() on Windows returns a *reconstruction* built from the
    parsed HIDP caps -- functionally equivalent, but not byte-identical, and
    noticeably longer. Emulation must replay the bytes below, not hidapi's.
    """
    import struct as _s

    hdr = _s.pack("<IBBHHH", port, 0x81, 0x06, (0x22 << 8) | 0, interface, length)
    res = ioctl(hub, IOCTL_USB_GET_DESCRIPTOR_FROM_NODE_CONNECTION,
                hdr + b"\x00" * length, len(hdr) + length)
    if not res or len(res) <= len(hdr):
        return None
    return res[len(hdr):]


def get_string(hub, port: int, index: int, langid: int = 0x0409) -> str:
    if index == 0:
        return ""
    d = get_descriptor(hub, port, DESC_STRING, index, langid, 255)
    if not d or len(d) < 2 or d[1] != DESC_STRING:
        return ""
    return d[2 : d[0]].decode("utf-16-le", errors="replace")


# ---------------------------------------------------------------------------
# Descriptor parsing
# ---------------------------------------------------------------------------

CLASS_NAMES = {
    0x00: "(per-interface)", 0x01: "Audio", 0x02: "CDC-Control", 0x03: "HID",
    0x05: "Physical", 0x06: "Image", 0x07: "Printer", 0x08: "Mass Storage",
    0x09: "Hub", 0x0A: "CDC-Data", 0x0B: "Smart Card", 0x0E: "Video",
    0xEF: "Misc", 0xFE: "App Specific", 0xFF: "Vendor Specific",
}

AUDIO_SUBCLASS = {0x01: "AUDIOCONTROL", 0x02: "AUDIOSTREAMING", 0x03: "MIDISTREAMING"}
AC_SUBTYPE = {
    0x01: "HEADER", 0x02: "INPUT_TERMINAL", 0x03: "OUTPUT_TERMINAL",
    0x04: "MIXER_UNIT", 0x05: "SELECTOR_UNIT", 0x06: "FEATURE_UNIT",
    0x07: "PROCESSING_UNIT", 0x08: "EXTENSION_UNIT",
}
AS_SUBTYPE = {0x01: "AS_GENERAL", 0x02: "FORMAT_TYPE", 0x03: "FORMAT_SPECIFIC"}
EP_AS_SUBTYPE = {0x01: "EP_GENERAL"}
TERMINAL_TYPES = {
    0x0100: "USB Undefined", 0x0101: "USB Streaming", 0x0201: "Microphone",
    0x0301: "Speaker", 0x0302: "Headphones", 0x0402: "Headset",
    0x0603: "Line Connector",
}
XFER = {0: "Control", 1: "Isochronous", 2: "Bulk", 3: "Interrupt"}
ISO_SYNC = {0: "NoSync", 1: "Async", 2: "Adaptive", 3: "Sync"}
ISO_USAGE = {0: "Data", 1: "Feedback", 2: "ImplicitFeedbackData"}


def hexdump(data: bytes, indent: str = "") -> str:
    lines = []
    for i in range(0, len(data), 16):
        c = data[i : i + 16]
        lines.append(f"{indent}{i:04x}  " + " ".join(f"{b:02x}" for b in c))
    return "\n".join(lines)


def parse_device_descriptor(d: bytes) -> list[str]:
    import struct as _s

    (bLength, bDescriptorType, bcdUSB, bDeviceClass, bDeviceSubClass, bDeviceProtocol,
     bMaxPacketSize0, idVendor, idProduct, bcdDevice, iManufacturer, iProduct,
     iSerialNumber, bNumConfigurations) = _s.unpack("<BBHBBBBHHHBBBB", d[:18])
    return [
        "### Device Descriptor",
        "",
        "| field | value |",
        "|---|---|",
        f"| bcdUSB | 0x{bcdUSB:04X} |",
        f"| bDeviceClass | 0x{bDeviceClass:02X} {CLASS_NAMES.get(bDeviceClass,'')} |",
        f"| bDeviceSubClass | 0x{bDeviceSubClass:02X} |",
        f"| bDeviceProtocol | 0x{bDeviceProtocol:02X} |",
        f"| bMaxPacketSize0 | {bMaxPacketSize0} |",
        f"| idVendor | 0x{idVendor:04X} |",
        f"| idProduct | 0x{idProduct:04X} |",
        f"| bcdDevice | 0x{bcdDevice:04X} |",
        f"| iManufacturer | {iManufacturer} |",
        f"| iProduct | {iProduct} |",
        f"| iSerialNumber | {iSerialNumber} |",
        f"| bNumConfigurations | {bNumConfigurations} |",
    ]


def parse_config(cfg: bytes) -> list[str]:
    import struct as _s

    out: list[str] = []
    i = 0
    cur_iface_class = 0
    cur_iface_sub = 0
    while i < len(cfg):
        blen = cfg[i]
        if blen == 0:
            out.append(f"  !! zero-length descriptor at {i}, stopping")
            break
        btype = cfg[i + 1]
        d = cfg[i : i + blen]

        if btype == 0x02:  # CONFIGURATION
            (_, _, wTotalLength, bNumInterfaces, bConfigurationValue, iConfiguration,
             bmAttributes, bMaxPower) = _s.unpack("<BBHBBBBB", d[:9])
            out += [
                "",
                "### Configuration Descriptor",
                "",
                f"- wTotalLength: {wTotalLength}",
                f"- bNumInterfaces: {bNumInterfaces}",
                f"- bConfigurationValue: {bConfigurationValue}",
                f"- bmAttributes: 0x{bmAttributes:02X}"
                + (" (self-powered)" if bmAttributes & 0x40 else " (bus-powered)")
                + (" remote-wakeup" if bmAttributes & 0x20 else ""),
                f"- bMaxPower: {bMaxPower*2} mA",
            ]
        elif btype == 0x0B:  # INTERFACE ASSOCIATION
            (_, _, bFirstInterface, bInterfaceCount, bFunctionClass,
             bFunctionSubClass, bFunctionProtocol, iFunction) = _s.unpack("<BBBBBBBB", d[:8])
            out += [
                "",
                f"**Interface Association**: interfaces {bFirstInterface}"
                f"..{bFirstInterface+bInterfaceCount-1}, class 0x{bFunctionClass:02X} "
                f"{CLASS_NAMES.get(bFunctionClass,'')} subclass 0x{bFunctionSubClass:02X} "
                f"protocol 0x{bFunctionProtocol:02X}",
            ]
        elif btype == 0x04:  # INTERFACE
            (_, _, bInterfaceNumber, bAlternateSetting, bNumEndpoints, bInterfaceClass,
             bInterfaceSubClass, bInterfaceProtocol, iInterface) = _s.unpack("<BBBBBBBBB", d[:9])
            cur_iface_class, cur_iface_sub = bInterfaceClass, bInterfaceSubClass
            extra = ""
            if bInterfaceClass == 0x01:
                extra = f" / {AUDIO_SUBCLASS.get(bInterfaceSubClass,'?')}"
            out += [
                "",
                f"#### Interface {bInterfaceNumber} alt {bAlternateSetting} "
                f"-- class 0x{bInterfaceClass:02X} {CLASS_NAMES.get(bInterfaceClass,'')}"
                f"{extra}, subclass 0x{bInterfaceSubClass:02X}, "
                f"protocol 0x{bInterfaceProtocol:02X}, {bNumEndpoints} endpoint(s)",
            ]
        elif btype == 0x05:  # ENDPOINT
            (_, _, bEndpointAddress, bmAttributes, wMaxPacketSize, bInterval) = _s.unpack(
                "<BBBBHB", d[:7]
            )
            direction = "IN" if bEndpointAddress & 0x80 else "OUT"
            ttype = bmAttributes & 0x03
            desc = f"{XFER[ttype]}"
            if ttype == 1:
                desc += f" ({ISO_SYNC[(bmAttributes>>2)&3]}, {ISO_USAGE.get((bmAttributes>>4)&3,'?')})"
            line = (
                f"- EP 0x{bEndpointAddress:02X} {direction}: {desc}, "
                f"wMaxPacketSize={wMaxPacketSize & 0x7FF}, bInterval={bInterval}"
            )
            if len(d) >= 9:
                line += f", bRefresh={d[7]}, bSynchAddress=0x{d[8]:02X}"
            out.append(line)
        elif btype == 0x21 and cur_iface_class == 0x03:  # HID descriptor
            (_, _, bcdHID, bCountryCode, bNumDescriptors) = _s.unpack("<BBHBB", d[:6])
            parts = []
            for k in range(bNumDescriptors):
                t, l = _s.unpack("<BH", d[6 + k * 3 : 9 + k * 3])
                parts.append(f"type 0x{t:02X} len {l}")
            out.append(
                f"- HID descriptor: bcdHID 0x{bcdHID:04X}, country {bCountryCode}, "
                + "; ".join(parts)
            )
        elif btype == 0x24 and cur_iface_class == 0x01:  # CS_INTERFACE (audio)
            sub = d[2]
            if cur_iface_sub == 0x01:  # AudioControl
                name = AC_SUBTYPE.get(sub, f"0x{sub:02X}")
                detail = ""
                if sub == 0x01 and len(d) >= 8:
                    bcdADC, wTotalLength, bInCollection = _s.unpack("<HHB", d[3:8])
                    ifaces = list(d[8 : 8 + bInCollection])
                    detail = (f" bcdADC=0x{bcdADC:04X} wTotalLength={wTotalLength} "
                              f"streaming ifaces={ifaces}")
                elif sub == 0x02 and len(d) >= 12:  # INPUT_TERMINAL
                    (bTerminalID, wTerminalType, bAssocTerminal, bNrChannels,
                     wChannelConfig) = _s.unpack("<BHBBH", d[3:10])
                    detail = (f" id={bTerminalID} type=0x{wTerminalType:04X} "
                              f"({TERMINAL_TYPES.get(wTerminalType,'?')}) "
                              f"nrChannels={bNrChannels} chConfig=0x{wChannelConfig:04X}")
                elif sub == 0x03 and len(d) >= 9:  # OUTPUT_TERMINAL
                    (bTerminalID, wTerminalType, bAssocTerminal, bSourceID) = _s.unpack(
                        "<BHBB", d[3:8]
                    )
                    detail = (f" id={bTerminalID} type=0x{wTerminalType:04X} "
                              f"({TERMINAL_TYPES.get(wTerminalType,'?')}) source={bSourceID}")
                elif sub == 0x06 and len(d) >= 7:  # FEATURE_UNIT
                    bUnitID, bSourceID, bControlSize = d[3], d[4], d[5]
                    controls = d[6 : len(d) - 1]
                    detail = (f" id={bUnitID} source={bSourceID} controlSize={bControlSize} "
                              f"controls={controls.hex(' ')}")
                out.append(f"- AC {name}:{detail}   raw[{d.hex(' ')}]")
            elif cur_iface_sub == 0x02:  # AudioStreaming
                name = AS_SUBTYPE.get(sub, f"0x{sub:02X}")
                detail = ""
                if sub == 0x01 and len(d) >= 7:
                    bTerminalLink, bDelay, wFormatTag = _s.unpack("<BBH", d[3:7])
                    detail = (f" terminalLink={bTerminalLink} delay={bDelay} "
                              f"formatTag=0x{wFormatTag:04X}")
                elif sub == 0x02 and len(d) >= 8:  # FORMAT_TYPE I
                    (bFormatType, bNrChannels, bSubframeSize, bBitResolution,
                     bSamFreqType) = d[3], d[4], d[5], d[6], d[7]
                    freqs = []
                    for k in range(bSamFreqType):
                        o = 8 + k * 3
                        freqs.append(d[o] | (d[o + 1] << 8) | (d[o + 2] << 16))
                    detail = (f" formatType={bFormatType} nrChannels={bNrChannels} "
                              f"subframeSize={bSubframeSize} bitResolution={bBitResolution} "
                              f"freqs={freqs}")
                out.append(f"- AS {name}:{detail}   raw[{d.hex(' ')}]")
            else:
                out.append(f"- CS_INTERFACE subtype 0x{sub:02X}  raw[{d.hex(' ')}]")
        elif btype == 0x25:  # CS_ENDPOINT
            sub = d[2]
            detail = ""
            if sub == 0x01 and len(d) >= 7:
                bmAttributes, bLockDelayUnits, wLockDelay = d[3], d[4], d[5] | (d[6] << 8)
                detail = (f" bmAttributes=0x{bmAttributes:02X}"
                          f"{' (sampling-freq control)' if bmAttributes & 1 else ''}"
                          f" lockDelayUnits={bLockDelayUnits} lockDelay={wLockDelay}")
            out.append(f"- CS_ENDPOINT {EP_AS_SUBTYPE.get(sub, hex(sub))}:{detail}   raw[{d.hex(' ')}]")
        else:
            out.append(f"- unknown descriptor type 0x{btype:02X} len {blen}: {d.hex(' ')}")
        i += blen
    return out


# ---------------------------------------------------------------------------
# Walk
# ---------------------------------------------------------------------------


def walk_hub(path: str, vid: int | None, pid: int | None, results: list, depth: int = 0):
    import struct as _s

    h = open_dev(path)
    if h is None:
        return
    try:
        info = ioctl(h, IOCTL_USB_GET_NODE_INFORMATION, b"\x00" * 76, 76)
        if not info or len(info) < 12:
            return
        # USB_NODE_INFORMATION: ULONG NodeType; union { USB_HUB_INFORMATION {
        #   USB_HUB_DESCRIPTOR(bLength,bType,bNumberOfPorts,...) ... } }
        num_ports = info[6]
        for port in range(1, num_ports + 1):
            req = _s.pack("<I", port) + b"\x00" * (NCIEX_SIZE - 4)
            res = ioctl(h, IOCTL_USB_GET_NODE_CONNECTION_INFORMATION_EX, req, NCIEX_SIZE)
            if not res or len(res) < 35:
                continue
            dev_desc = res[4:22]
            cur_cfg, speed, is_hub = res[22], res[23], res[24]
            conn_status = _s.unpack_from("<I", res, 31)[0]
            if conn_status != 1:
                continue
            idVendor, idProduct = _s.unpack_from("<HH", dev_desc, 8)

            if is_hub:
                # recurse into the external hub
                namebuf = ioctl(h, IOCTL_USB_GET_NODE_CONNECTION_NAME,
                                _s.pack("<II", port, 0) + b"\x00" * 256, 264)
                if namebuf and len(namebuf) > 8:
                    n = _s.unpack_from("<I", namebuf, 4)[0]
                    sub = ctypes.wstring_at(
                        ctypes.cast(ctypes.create_string_buffer(namebuf[8:]), ctypes.c_void_p).value
                    ) if False else namebuf[8:8 + n].decode("utf-16-le", errors="ignore").rstrip("\x00")
                    if sub:
                        walk_hub("\\\\.\\" + sub, vid, pid, results, depth + 1)

            if vid is not None and (idVendor != vid or idProduct != pid):
                continue

            entry = {
                "hub": path, "port": port, "speed": SPEEDS.get(speed, str(speed)),
                "vid": idVendor, "pid": idProduct, "dev_desc": dev_desc,
                "config": None, "strings": {}, "hid_rd": {},
            }
            # full config descriptor: read 9 bytes first for wTotalLength
            head = get_descriptor(h, port, DESC_CONFIG, 0, 0, 9)
            if head and len(head) >= 9:
                total = _s.unpack_from("<H", head, 2)[0]
                full = get_descriptor(h, port, DESC_CONFIG, 0, 0, total)
                entry["config"] = full
                # walk the config for HID descriptors, pull the real report descriptors
                if full:
                    j, cur_if, cur_cls = 0, None, 0
                    while j < len(full):
                        blen2 = full[j]
                        if blen2 == 0:
                            break
                        btype2 = full[j + 1]
                        if btype2 == 0x04:
                            cur_if, cur_cls = full[j + 2], full[j + 5]
                        elif btype2 == 0x21 and cur_cls == 0x03 and cur_if is not None:
                            for k in range(full[j + 5]):
                                dt = full[j + 6 + k * 3]
                                dl = full[j + 7 + k * 3] | (full[j + 8 + k * 3] << 8)
                                if dt == 0x22:
                                    rd = get_hid_report_descriptor(h, port, cur_if, dl)
                                    if rd:
                                        entry["hid_rd"][cur_if] = rd
                        j += blen2
            # BOS descriptor (type 0x0F) -- carries the MS OS 2.0 platform capability
            bos_head = get_descriptor(h, port, 0x0F, 0, 0, 5)
            if bos_head and len(bos_head) >= 5 and bos_head[1] == 0x0F:
                btotal = _s.unpack_from("<H", bos_head, 2)[0]
                entry["bos"] = get_descriptor(h, port, 0x0F, 0, 0, btotal)
            # MS OS 1.0 magic string descriptor at index 0xEE
            ee = get_descriptor(h, port, DESC_STRING, 0xEE, 0, 18)
            if ee and len(ee) >= 4 and ee[1] == DESC_STRING:
                entry["ms_os_string"] = ee
            for idx_name, idx in (("manufacturer", dev_desc[14]),
                                  ("product", dev_desc[15]),
                                  ("serial", dev_desc[16])):
                entry["strings"][idx_name] = get_string(h, port, idx)
            results.append(entry)
    finally:
        kernel32.CloseHandle(wt.HANDLE(h))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--vid", default="054C")
    ap.add_argument("--pid", default="0CE6")
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args()
    vid = None if args.all else int(args.vid, 16)
    pid = None if args.all else int(args.pid, 16)

    results: list = []
    for p in hub_paths():
        walk_hub(p, vid, pid, results)

    if not results:
        print("No matching USB device found on any hub port.")
        return 1

    for e in results:
        print(f"\n## USB device {e['vid']:04X}:{e['pid']:04X} "
              f"(port {e['port']}, {e['speed']} speed)")
        print(f"\n- hub: `{e['hub']}`")
        for k, v in e["strings"].items():
            print(f"- {k}: `{v}`")
        print()
        print("\n".join(parse_device_descriptor(e["dev_desc"])))
        print("\n<details><summary>device descriptor raw (18 bytes)</summary>\n")
        print("```")
        print(hexdump(e["dev_desc"]))
        print("```\n</details>")
        if e["config"]:
            print("\n".join(parse_config(e["config"])))
            print(f"\n<details><summary>full configuration descriptor raw "
                  f"({len(e['config'])} bytes)</summary>\n")
            print("```")
            print(hexdump(e["config"]))
            print("```\n</details>")
            for ifnum, rd in sorted(e.get("hid_rd", {}).items()):
                print(f"\n#### HID report descriptor, interface {ifnum} "
                      f"({len(rd)} bytes) -- verbatim from the device\n")
                print("```")
                print(hexdump(rd))
                print("```")
            if e.get("bos"):
                print(f"\n#### BOS descriptor ({len(e['bos'])} bytes)\n")
                print("```")
                print(hexdump(e["bos"]))
                print("```")
            if e.get("ms_os_string"):
                s = e["ms_os_string"]
                print(f"\n#### MS OS 1.0 string descriptor (index 0xEE, {len(s)} bytes)\n")
                print("```")
                print(hexdump(s))
                print("```")
                if len(s) >= 18:
                    print(f"\nsignature=`{s[2:16].decode('utf-16-le', errors='replace')}` "
                          f"bMS_VendorCode=0x{s[16]:02X}")
        else:
            print("\n(configuration descriptor unavailable)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
