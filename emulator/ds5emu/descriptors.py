"""Descriptor tables for the emulated wired DualSense.

Every byte here is transcribed from `docs/usb-ground-truth.md`, which was read
off the **physical USB-connected DualSense on this machine** via hub IOCTLs.
It is NOT copied from DS5Dongle's `fake_ds5.h`.

`emulator/tests/test_descriptors.py` re-parses the hexdumps out of that
markdown file and asserts they match these constants byte for byte, so the two
cannot silently drift apart.

Reminder from docs/STATUS.md gotcha #4: hidapi's get_report_descriptor() on
Windows returns a *reconstruction* (467 bytes for this device). The real report
descriptor is the 289-byte one below and that is what an emulator must replay.
"""

from __future__ import annotations

import struct

# --------------------------------------------------------------------------
# raw descriptors, verbatim from docs/usb-ground-truth.md
# --------------------------------------------------------------------------

DEVICE_DESCRIPTOR = bytes.fromhex(
    "12 01 00 02 00 00 00 40 4c 05 e6 0c 00 01 01 02"
    "00 01".replace(" ", "")
)

CONFIG_DESCRIPTOR = bytes.fromhex(
    (
        "09 02 e3 00 04 01 00 c0 fa 09 04 00 00 00 01 01"
        "00 00 0a 24 01 00 01 49 00 02 01 02 0c 24 02 01"
        "01 01 06 04 33 00 00 00 0c 24 06 02 01 01 03 00"
        "00 00 00 00 09 24 03 03 01 03 04 02 00 0c 24 02"
        "04 02 04 03 02 03 00 00 00 09 24 06 05 04 01 03"
        "00 00 09 24 03 06 01 01 01 05 00 09 04 01 00 00"
        "01 02 00 00 09 04 01 01 01 01 02 00 00 07 24 01"
        "01 01 01 00 0b 24 02 01 04 02 10 01 80 bb 00 09"
        "05 01 09 88 01 04 00 00 07 25 01 00 00 00 00 09"
        "04 02 00 00 01 02 00 00 09 04 02 01 01 01 02 00"
        "00 07 24 01 06 01 01 00 0b 24 02 01 02 02 10 01"
        "80 bb 00 09 05 82 05 c4 00 04 00 00 07 25 01 00"
        "00 00 00 09 04 03 00 02 03 00 00 00 09 21 11 01"
        "00 01 22 21 01 07 05 84 03 40 00 06 07 05 03 03"
        "40 00 06"
    ).replace(" ", "")
)

HID_REPORT_DESCRIPTOR = bytes.fromhex(
    (
        "05 01 09 05 a1 01 85 01 09 30 09 31 09 32 09 35"
        "09 33 09 34 15 00 26 ff 00 75 08 95 06 81 02 06"
        "00 ff 09 20 95 01 81 02 05 01 09 39 15 00 25 07"
        "35 00 46 3b 01 65 14 75 04 95 01 81 42 65 00 05"
        "09 19 01 29 0f 15 00 25 01 75 01 95 0f 81 02 06"
        "00 ff 09 21 95 0d 81 02 06 00 ff 09 22 15 00 26"
        "ff 00 75 08 95 34 81 02 85 02 09 23 95 2f 91 02"
        "85 05 09 33 95 28 b1 02 85 08 09 34 95 2f b1 02"
        "85 09 09 24 95 13 b1 02 85 0a 09 25 95 1a b1 02"
        "85 0b 09 41 95 29 b1 02 85 0c 09 42 95 29 b1 02"
        "85 20 09 26 95 3f b1 02 85 21 09 27 95 04 b1 02"
        "85 22 09 40 95 3f b1 02 85 80 09 28 95 3f b1 02"
        "85 81 09 29 95 3f b1 02 85 82 09 2a 95 09 b1 02"
        "85 83 09 2b 95 3f b1 02 85 84 09 2c 95 3f b1 02"
        "85 85 09 2d 95 02 b1 02 85 a0 09 2e 95 01 b1 02"
        "85 e0 09 2f 95 3f b1 02 85 f0 09 30 95 3f b1 02"
        "85 f1 09 31 95 3f b1 02 85 f2 09 32 95 0f b1 02"
        "85 f4 09 35 95 3f b1 02 85 f5 09 36 95 03 b1 02"
        "c0"
    ).replace(" ", "")
)

assert len(DEVICE_DESCRIPTOR) == 18
assert len(CONFIG_DESCRIPTOR) == 227
assert len(HID_REPORT_DESCRIPTOR) == 289

# --------------------------------------------------------------------------
# strings
# --------------------------------------------------------------------------

LANGID_EN_US = 0x0409

# iSerialNumber is 0 on the real device — there is deliberately no index 3.
STRINGS = {
    1: "Sony Interactive Entertainment",
    2: "DualSense Wireless Controller",
}


def string_descriptor(index: int, langid: int = LANGID_EN_US) -> bytes | None:
    """UTF-16LE string descriptor, or None if the index does not exist."""
    if index == 0:
        return bytes([4, 0x03]) + struct.pack("<H", LANGID_EN_US)
    text = STRINGS.get(index)
    if text is None:
        return None
    raw = text.encode("utf-16-le")
    return bytes([len(raw) + 2, 0x03]) + raw


# --------------------------------------------------------------------------
# derived facts (parsed once, so the tables above stay the single source)
# --------------------------------------------------------------------------

ID_VENDOR = 0x054C
ID_PRODUCT = 0x0CE6
BCD_DEVICE = 0x0100
BCD_USB = 0x0200
B_MAX_PACKET_SIZE0 = 64
B_NUM_CONFIGURATIONS = 1
B_NUM_INTERFACES = 4
B_CONFIGURATION_VALUE = 1

# endpoints, from the configuration descriptor
EP_ISO_OUT = 0x01   # AudioStreaming OUT, 4ch/48k/16, 392 B, bInterval 4 (1 ms)
EP_ISO_IN = 0x82    # AudioStreaming IN,  2ch/48k/16, 196 B, bInterval 4 (1 ms)
EP_HID_IN = 0x84    # interrupt IN,  64 B, bInterval 6 (4 ms)
EP_HID_OUT = 0x03   # interrupt OUT, 64 B, bInterval 6 (4 ms)

ISO_OUT_MAX_PACKET = 392
ISO_IN_MAX_PACKET = 196
HID_MAX_PACKET = 64

# interfaces
IFACE_AUDIOCONTROL = 0
IFACE_AUDIO_OUT = 1
IFACE_AUDIO_IN = 2
IFACE_HID = 3

# UAC1 unit IDs (AudioControl topology, docs/usb-ground-truth.md)
UNIT_IT_USB_STREAMING_OUT = 1   # INPUT_TERMINAL  USB Streaming, 4ch, 0x0033
UNIT_FU_SPEAKER = 2             # FEATURE_UNIT    mute + volume
UNIT_OT_SPEAKER = 3             # OUTPUT_TERMINAL Speaker (0x0301)
UNIT_IT_HEADSET = 4             # INPUT_TERMINAL  Headset (0x0402), 2ch, 0x0003
UNIT_FU_MIC = 5                 # FEATURE_UNIT    mute + volume
UNIT_OT_USB_STREAMING_IN = 6    # OUTPUT_TERMINAL USB Streaming

AUDIO_OUT_CHANNELS = 4
AUDIO_IN_CHANNELS = 2
AUDIO_SAMPLE_RATE = 48000
AUDIO_BYTES_PER_SAMPLE = 2

# One 1 ms service interval of audio.
ISO_OUT_BYTES_PER_MS = AUDIO_OUT_CHANNELS * AUDIO_BYTES_PER_SAMPLE * (AUDIO_SAMPLE_RATE // 1000)
ISO_IN_BYTES_PER_MS = AUDIO_IN_CHANNELS * AUDIO_BYTES_PER_SAMPLE * (AUDIO_SAMPLE_RATE // 1000)
assert ISO_OUT_BYTES_PER_MS == 384  # wMaxPacketSize 392 leaves 8 bytes of slack
assert ISO_IN_BYTES_PER_MS == 192   # wMaxPacketSize 196 leaves 4 bytes of slack


def iter_descriptors(buf: bytes):
    """Walk a descriptor blob, yielding (bDescriptorType, bytes)."""
    off = 0
    while off + 2 <= len(buf):
        length = buf[off]
        if length < 2 or off + length > len(buf):
            raise ValueError(f"malformed descriptor at offset {off}")
        yield buf[off + 1], buf[off : off + length]
        off += length


def interface_infos():
    """(class, subclass, protocol) for each *interface number*, alt 0.

    OP_REP_DEVLIST wants one usbip_usb_interface per bNumInterfaces, so
    alternate settings must be de-duplicated.
    """
    seen = {}
    for dtype, d in iter_descriptors(CONFIG_DESCRIPTOR):
        if dtype == 0x04:  # INTERFACE
            num, alt = d[2], d[3]
            if alt == 0:
                seen[num] = (d[5], d[6], d[7])
    return [seen[n] for n in sorted(seen)]
