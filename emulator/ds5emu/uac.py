"""UAC1 (USB Audio Class 1.0) control state for the emulated DualSense.

Scope: only what the real device's descriptors actually declare, per
`docs/usb-ground-truth.md`:

    [1] IT USB Streaming 4ch -> [2] FU (mute+volume) -> [3] OT Speaker
    [4] IT Headset 2ch       -> [5] FU (mute+volume) -> [6] OT USB Streaming

Both feature units declare `bmaControls[0] = 0x03` (MUTE | VOLUME) on the
master channel and nothing on the per-channel entries, and `bControlSize = 1`.
Both CS_ENDPOINT descriptors have `bmAttributes = 0x00`, i.e. **no sampling
frequency control** — so endpoint-recipient class requests must STALL.

Volume ranges (risk R7, closed 2026-10-28)
------------------------------------------
Hub IOCTLs return descriptors, not class-request responses, so Phase 0 could
not read the feature units' GET_MIN/GET_MAX/GET_RES and this module shipped
assumed values (-96..0 dB in 1 dB steps on both units). They are now MEASURED,
read-only, from two real DualSenses on USB cables: Windows' usbaudio driver
asks the device for exactly these three values when the endpoint starts and
hands them to `IAudioEndpointVolume::GetVolumeRange` unchanged (pycaw, both
pads identical):

    Speakers (DualSense Wireless Controller)            -100.0 .. 0.0 dB, step 1.0 dB
    Headset Microphone (DualSense Wireless Controller)     0.0 .. +48.0 dB, step 0.4765625 dB

0.4765625 dB is 122/256, i.e. RES = 0x007A in the UAC1 1/256 dB unit; +48 dB
is MAX = 0x3000. The microphone's feature unit is a GAIN (0 dB at the bottom,
not an attenuator with 0 dB at the top), which matters: Windows' capture slider
on a real wired pad goes from no gain at 0 % to +48 dB at 100 % -- the same
pad on this PC sat at +14.9 dB for a slider at 65 %. With the old assumed
range the same slider was an attenuator, which is half of why a bridged pad's
mic was quiet (bridge.py `on_uac_control` has the other half).

"Unchanged" holds for a device the Windows Audio service has not seen before.
For one it HAS seen -- a machine that ran a bridge with the old assumed ranges
since its last boot -- the service keeps the old range in memory per endpoint,
while usbaudio.sys does re-ask the device on every attach (measured
2026-10-29: the device answered the values above, `GetVolumeRange` still said
-96..0 dB). app/ds5app/audio_range.py detects that after attach and restarts
the audio service once; its docstring has the measurements.
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

# 1/256 dB, signed 16-bit. MEASURED from a real wired pad -- module docstring.
SPEAKER_VOLUME_MIN = -100 * 256   # -100.0 dB
SPEAKER_VOLUME_MAX = 0            #    0.0 dB
SPEAKER_VOLUME_RES = 256          #    1.0 dB
SPEAKER_VOLUME_DEFAULT = 0

MIC_VOLUME_MIN = 0                #    0.0 dB  (no gain; not muted)
MIC_VOLUME_MAX = 48 * 256         #  +48.0 dB
MIC_VOLUME_RES = 0x7A             #   0.4765625 dB
#: What GET_CUR answers before Windows has set a level: 31 steps, +14.8 dB --
#: where a real pad's slider sat on this PC (65 %, +14.9 dB).
MIC_VOLUME_DEFAULT = 31 * MIC_VOLUME_RES

FEATURE_UNITS = (D.UNIT_FU_SPEAKER, D.UNIT_FU_MIC)

#: unit -> (MIN, MAX, RES, default CUR)
VOLUME_RANGES = {
    D.UNIT_FU_SPEAKER: (SPEAKER_VOLUME_MIN, SPEAKER_VOLUME_MAX,
                        SPEAKER_VOLUME_RES, SPEAKER_VOLUME_DEFAULT),
    D.UNIT_FU_MIC: (MIC_VOLUME_MIN, MIC_VOLUME_MAX,
                    MIC_VOLUME_RES, MIC_VOLUME_DEFAULT),
}


class UacState:
    """Mutable mixer state, one entry per (unit, control selector, channel)."""

    def __init__(self) -> None:
        self.mute = {u: False for u in FEATURE_UNITS}
        self.volume = {u: VOLUME_RANGES[u][3] for u in FEATURE_UNITS}

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
            lo, hi, _res, _cur = VOLUME_RANGES[unit]
            self.volume[unit] = max(lo, min(hi, value))
            return b""
        lo, hi, res, _cur = VOLUME_RANGES[unit]
        table = {
            GET_CUR: self.volume[unit],
            GET_MIN: lo,
            GET_MAX: hi,
            GET_RES: res,
        }
        if bRequest in table:
            return struct.pack("<h", table[bRequest])[:wLength]
        return None
