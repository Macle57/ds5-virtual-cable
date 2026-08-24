"""UAC1 (USB Audio Class 1.0) control state for the emulated DualSense.

Scope: only what the real device's descriptors actually declare, per
`docs/usb-ground-truth.md`:

    [1] IT USB Streaming 4ch -> [2] FU (mute+volume) -> [3] OT Speaker
    [4] IT Headset 2ch       -> [5] FU (mute+volume) -> [6] OT USB Streaming

Both feature units declare `bmaControls[0] = 0x03` (MUTE | VOLUME) on the
master channel and nothing on the per-channel entries, and `bControlSize = 1`.
Both CS_ENDPOINT descriptors have `bmAttributes = 0x00`, i.e. **no sampling
frequency control** — so endpoint-recipient class requests must STALL.

!! ASSUMED VALUES !!
The volume MIN/MAX/RES words below were NOT captured from the physical
controller. Hub IOCTLs return descriptors, not class-request responses, so
Phase 0 could not read them. They are plausible UAC1 defaults (1/256 dB units).
This is risk R7 in `docs/virtualization-options.md`; Phase 3 should sniff the
real wired controller with USBPcap and replace them.
"""

from __future__ import annotations

import struct

from . import descriptors as D

# bRequest, UAC1 spec §5.2.1
SET_CUR = 0x01
GET_CUR = 0x81
GET_MIN = 0x82
GET_MAX = 0x83
GET_RES = 0x84

# Feature Unit control selectors, UAC1 spec §A.10.2
FU_MUTE_CONTROL = 0x01
FU_VOLUME_CONTROL = 0x02

CH_MASTER = 0

# 1/256 dB, signed 16-bit. ASSUMED — see the module docstring.
VOLUME_MIN = -96 * 256   # -96.0 dB
VOLUME_MAX = 0           #   0.0 dB
VOLUME_RES = 256         #   1.0 dB
VOLUME_DEFAULT = 0

FEATURE_UNITS = (D.UNIT_FU_SPEAKER, D.UNIT_FU_MIC)


class UacState:
    """Mutable mixer state, one entry per (unit, control selector, channel)."""

    def __init__(self) -> None:
        self.mute = {u: False for u in FEATURE_UNITS}
        self.volume = {u: VOLUME_DEFAULT for u in FEATURE_UNITS}

    # -- request handling -------------------------------------------------

    def handle(self, bmRequestType: int, bRequest: int, wValue: int, wIndex: int,
               wLength: int, data: bytes) -> bytes | None:
        """Handle one UAC1 class request.

        Returns the IN data (possibly b"" for an OUT request that was accepted)
        or None to signal STALL.
        """
        recipient = bmRequestType & 0x1F

        # Endpoint-recipient requests (SAMPLING_FREQ_CONTROL and friends): the
        # real device declares no endpoint controls, so it would STALL these
        # and Windows should never issue them. Mirror that exactly.
        if recipient == 0x02:
            return None

        if recipient != 0x01:
            return None

        unit = (wIndex >> 8) & 0xFF
        interface = wIndex & 0xFF
        selector = (wValue >> 8) & 0xFF
        channel = wValue & 0xFF

        if interface != D.IFACE_AUDIOCONTROL:
            return None
        if unit not in FEATURE_UNITS:
            return None
        if channel != CH_MASTER:
            # Only bmaControls[0] (master) declares any controls.
            return None

        if selector == FU_MUTE_CONTROL:
            return self._mute(unit, bRequest, wLength, data)
        if selector == FU_VOLUME_CONTROL:
            return self._volume(unit, bRequest, wLength, data)
        return None

    def _mute(self, unit: int, bRequest: int, wLength: int, data: bytes) -> bytes | None:
        if bRequest == SET_CUR:
            if len(data) < 1:
                return None
            self.mute[unit] = bool(data[0])
            return b""
        if bRequest == GET_CUR:
            return bytes([1 if self.mute[unit] else 0])[:wLength]
        # MIN/MAX/RES are not defined for a boolean control.
        return None

    def _volume(self, unit: int, bRequest: int, wLength: int, data: bytes) -> bytes | None:
        if bRequest == SET_CUR:
            if len(data) < 2:
                return None
            (value,) = struct.unpack_from("<h", data, 0)
            self.volume[unit] = max(VOLUME_MIN, min(VOLUME_MAX, value))
            return b""
        table = {
            GET_CUR: self.volume[unit],
            GET_MIN: VOLUME_MIN,
            GET_MAX: VOLUME_MAX,
            GET_RES: VOLUME_RES,
        }
        if bRequest in table:
            return struct.pack("<h", table[bRequest])[:wLength]
        return None
