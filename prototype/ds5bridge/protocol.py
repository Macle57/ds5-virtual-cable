"""DualSense report layouts: SetState body, BT wrappers, input report decoding.

Sources:
  - SetState body field order:  ../dualsense-tester/src/router/DualSense/views/_OutputPanel/outputStruct.ts
  - valid-flag bit meanings:    ../dualsense-tester/src/router/DualSense/views/OutputPanel.vue (setValidFlagN calls)
  - BT 0x31 output wrapper:     ../dualsense-tester/src/utils/dualsense/ds.util.ts  (sendOutputReportFactory)
  - input report offsets:       ../dualsense-tester/src/router/DualSense/_utils/offset.util.ts
  - 0x36 audio report:          ../dualsense-tester/src/utils/dualsense/btAudioStream.ts (buildReportSix)
  - 0x39 audio report:          ../DS5Dongle/src/audio.cpp (audio_bt_task)
  - 0x32 mic control:           ../dualsense-tester/src/utils/dualsense/microphoneProtocol.ts
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

from .crc import fill_output_checksum

VID_SONY = 0x054C
PID_DUALSENSE = 0x0CE6

# ---------------------------------------------------------------------------
# Report ids and sizes
# ---------------------------------------------------------------------------

# BT
BT_INPUT_31 = 0x31
BT_INPUT_31_LEN = 78  # incl. report id
BT_OUT_31 = 0x31
BT_OUT_31_PAYLOAD = 77  # excl. report id
BT_OUT_32 = 0x32
BT_OUT_32_PAYLOAD = 141
BT_OUT_36 = 0x36
BT_OUT_36_PAYLOAD = 397
BT_OUT_39 = 0x39
BT_OUT_39_LEN = 547  # incl. report id (DS5Dongle builds it with the id in pkt[0])

# USB
USB_INPUT_01 = 0x01
USB_INPUT_01_LEN = 64
USB_OUT_02 = 0x02
USB_OUT_02_PAYLOAD = 47

SETSTATE_BODY_LEN = 47

# ---------------------------------------------------------------------------
# SetState body: 47 bytes, identical on USB (report 0x02 body) and
# BT (report 0x31 payload starting at offset 2).
# ---------------------------------------------------------------------------

VALID_FLAG0 = 0
VALID_FLAG1 = 1
BC_VIBRATION_RIGHT = 2
BC_VIBRATION_LEFT = 3
HEADPHONE_VOLUME = 4
SPEAKER_VOLUME = 5
MIC_VOLUME = 6
AUDIO_CONTROL = 7
MUTE_LED_CONTROL = 8
POWER_SAVE_MUTE_CONTROL = 9
AT_RIGHT_MODE = 10          # + 10 params at 11..20
AT_LEFT_MODE = 21           # + 10 params at 22..31
RESERVED0 = 32
HAPTIC_VOLUME = 36
AUDIO_CONTROL2 = 37
VALID_FLAG2 = 38
LIGHTBAR_SETUP = 41
LED_BRIGHTNESS = 42
PLAYER_INDICATOR = 43
LED_R = 44
LED_G = 45
LED_B = 46

# validFlag0 bits
F0_COMPATIBLE_VIBRATION = 1 << 0
F0_HAPTICS_SELECT = 1 << 1
F0_RIGHT_TRIGGER_FFB = 1 << 2
F0_LEFT_TRIGGER_FFB = 1 << 3
F0_HEADPHONE_VOLUME = 1 << 4
F0_SPEAKER_VOLUME = 1 << 5
F0_MIC_VOLUME = 1 << 6
F0_AUDIO_CONTROL = 1 << 7

# validFlag1 bits
F1_MIC_MUTE_LED = 1 << 0
F1_POWER_SAVE_MUTE = 1 << 1
F1_LIGHTBAR_CONTROL = 1 << 2
F1_RELEASE_LEDS = 1 << 3
F1_PLAYER_INDICATOR = 1 << 4
F1_OVERALL_EFFECT_POWER = 1 << 5
F1_AUDIO_CONTROL2 = 1 << 7

# validFlag2 bits
#: Gates LIGHTBAR_SETUP (byte 41). Linux hid-playstation's
#: DS_OUTPUT_VALID_FLAG2_LIGHTBAR_SETUP_CONTROL_ENABLE -- and the bit VERIFIED
#: on two pads over Bluetooth (2026-09-03): with bit 1 the setup byte takes
#: effect, with bit 0 it is silently ignored. docs/FINDINGS.md, "lightbar setup".
F2_LIGHTBAR_SETUP = 1 << 1
#: What daidr's tester sets for ledBrightness (byte 42): `setValidFlag2(0)` in
#: OutputPanel.vue. Not verified here; kept as the tester's convention.
F2_LED_BRIGHTNESS = 1 << 0
F2_COMPATIBLE_VIBRATION2 = 1 << 2

#: LIGHTBAR_SETUP values. LIGHT_OUT ends the pad's own Bluetooth connect
#: animation -- and until a host has sent it once, a Bluetooth DualSense
#: IGNORES every lightbar colour write while applying rumble and player LEDs
#: from the very same reports (verified on hardware, see FINDINGS). Sending it
#: in the same report as a colour works: the colour is what ends up shown.
LIGHTBAR_SETUP_LIGHT_OUT = 1 << 1

#: The colour a pad shows once its lightbar is unlocked and no host has painted
#: one yet: hid-playstation's player-1 blue (`player_leds_info` colours).
DEFAULT_LIGHTBAR = (0x00, 0x00, 0x40)

# Mute LED control values
MUTE_LED_OFF = 0
MUTE_LED_ON = 1
MUTE_LED_BLINK = 2

# Adaptive trigger effect modes (community naming)
AT_OFF = 0x05
AT_FEEDBACK = 0x21
AT_WEAPON = 0x25
AT_VIBRATION = 0x26
AT_SLOPE_FEEDBACK = 0x22
AT_MULTIPLE_FEEDBACK = 0x21
AT_BOW = 0x22
AT_GALLOPING = 0x23
AT_MACHINE = 0x27


@dataclass
class SetState:
    """The 47-byte SetState body. Only fields whose valid-flag bit is set are applied."""

    body: bytearray = field(default_factory=lambda: bytearray(SETSTATE_BODY_LEN))

    # -- flags -----------------------------------------------------------
    def flag0(self, bits: int) -> "SetState":
        self.body[VALID_FLAG0] |= bits
        return self

    def flag1(self, bits: int) -> "SetState":
        self.body[VALID_FLAG1] |= bits
        return self

    def flag2(self, bits: int) -> "SetState":
        self.body[VALID_FLAG2] |= bits
        return self

    # -- features --------------------------------------------------------
    def rumble(self, left: int, right: int) -> "SetState":
        """Classic (BC) rumble. left = low-frequency (large) motor."""
        self.body[BC_VIBRATION_LEFT] = left & 0xFF
        self.body[BC_VIBRATION_RIGHT] = right & 0xFF
        # bit1 (HAPTICS_SELECT) routes rumble emulation through the voice coils
        return self.flag0(F0_COMPATIBLE_VIBRATION | F0_HAPTICS_SELECT)

    def lightbar(self, r: int, g: int, b: int) -> "SetState":
        self.body[LED_R] = r & 0xFF
        self.body[LED_G] = g & 0xFF
        self.body[LED_B] = b & 0xFF
        self.flag1(F1_LIGHTBAR_CONTROL)
        self.body[VALID_FLAG1] &= ~F1_RELEASE_LEDS & 0xFF
        return self

    def player_leds(self, bitmask: int, brightness: int | None = None) -> "SetState":
        """bitmask: 5 LEDs, bit0 = leftmost. brightness 0=high,1=medium,2=low."""
        self.body[PLAYER_INDICATOR] = bitmask & 0x1F
        self.flag1(F1_PLAYER_INDICATOR)
        self.body[VALID_FLAG1] &= ~F1_RELEASE_LEDS & 0xFF
        if brightness is not None:
            self.body[LED_BRIGHTNESS] = brightness & 0xFF
            self.flag2(F2_LED_BRIGHTNESS)
        return self

    def lightbar_setup(self, value: int = LIGHTBAR_SETUP_LIGHT_OUT) -> "SetState":
        """The lightbar-setup control (byte 41, gated by validFlag2 bit 1).

        A Bluetooth DualSense applies no lightbar colour until a host has
        sent this once per connection; the bridge sends it in its connect-time
        prime. Combines with `lightbar()` in one report.
        """
        self.body[LIGHTBAR_SETUP] = value & 0xFF
        return self.flag2(F2_LIGHTBAR_SETUP)

    def mute_led(self, mode: int) -> "SetState":
        self.body[MUTE_LED_CONTROL] = mode & 0xFF
        self.flag1(F1_MIC_MUTE_LED)
        self.body[VALID_FLAG1] &= ~F1_RELEASE_LEDS & 0xFF
        return self

    def trigger_left(self, mode: int, params: bytes = b"") -> "SetState":
        self.body[AT_LEFT_MODE] = mode & 0xFF
        p = bytes(params[:10]).ljust(10, b"\x00")
        self.body[AT_LEFT_MODE + 1 : AT_LEFT_MODE + 11] = p
        return self.flag0(F0_LEFT_TRIGGER_FFB)

    def trigger_right(self, mode: int, params: bytes = b"") -> "SetState":
        self.body[AT_RIGHT_MODE] = mode & 0xFF
        p = bytes(params[:10]).ljust(10, b"\x00")
        self.body[AT_RIGHT_MODE + 1 : AT_RIGHT_MODE + 11] = p
        return self.flag0(F0_RIGHT_TRIGGER_FFB)

    def speaker_volume(self, vol: int) -> "SetState":
        self.body[SPEAKER_VOLUME] = vol & 0xFF
        self.body[AUDIO_CONTROL] = 3 << 4  # output path = internal speaker
        return self.flag0(F0_SPEAKER_VOLUME | F0_AUDIO_CONTROL)

    def headphone_volume(self, vol: int) -> "SetState":
        self.body[HEADPHONE_VOLUME] = vol & 0xFF
        self.body[AUDIO_CONTROL] = 0 << 4  # output path = headphone jack
        return self.flag0(F0_HEADPHONE_VOLUME | F0_AUDIO_CONTROL)

    def mic_volume(self, vol: int) -> "SetState":
        self.body[MIC_VOLUME] = vol & 0xFF
        return self.flag0(F0_MIC_VOLUME)

    def haptic_volume(self, vol: int) -> "SetState":
        self.body[HAPTIC_VOLUME] = vol & 0xFF
        return self.flag1(F1_AUDIO_CONTROL2)


# ---------------------------------------------------------------------------
# BT output wrappers
# ---------------------------------------------------------------------------


def build_bt_setstate(body: bytes, seq: int) -> bytes:
    """Wrap a 47-byte SetState body into a 78-byte BT 0x31 output report
    (report id included as byte 0, ready for hid.write()).

    ds.util.ts sendOutputReportFactory: payload[0] = seq<<4, payload[1] = 0x10,
    payload[2:] = body, tail 4 bytes = CRC32(0xA2, 0x31, ...).
    """
    payload = bytearray(BT_OUT_31_PAYLOAD)
    payload[0] = (seq & 0x0F) << 4
    payload[1] = 0x10
    payload[2 : 2 + len(body)] = body
    fill_output_checksum(BT_OUT_31, payload)
    return bytes([BT_OUT_31]) + bytes(payload)


def build_usb_setstate(body: bytes) -> bytes:
    """USB output report 0x02, 48 bytes incl. report id."""
    payload = bytearray(USB_OUT_02_PAYLOAD)
    payload[: len(body)] = body
    return bytes([USB_OUT_02]) + bytes(payload)


# --- 0x36: one Opus frame + one haptic frame -------------------------------

OPUS_FRAME_BYTES = 200
HAPTIC_FRAME_BYTES = 64


def build_report_36(
    opus_frame: bytes,
    haptic_frame: bytes,
    seq: int,
    frame_counter: int,
    target: str = "speaker",
    volume: int = 0x4B,
    mic_active: bool = False,
) -> bytes:
    """398-byte BT audio+haptics report (report id included).

    Port of btAudioStream.ts:buildReportSix. Payload index = report offset - 1.

    `mic_active` controls p[68], the low bit of the audio subpacket header that
    the 0x32 mic-control report also carries (0xFF active / 0xFE inactive, see
    microphoneProtocol.ts:buildBtMicControlReport report[3]). The tester hardcodes
    0xFE, which silently tears down mic streaming the moment audio playback starts
    -- verified on hardware here: mic payloads drop from ~105/s to 0 as soon as
    0x36 reports with 0xFE begin. Pass mic_active=True for full duplex.
    DS5Dongle expresses the same bit as pkt[4] 0x7F/0x7E in report 0x39.
    """
    p = bytearray(BT_OUT_36_PAYLOAD)
    p[0] = (seq & 0x0F) << 4  # seq high nibble, flags low nibble = 0

    # control subpacket -- only the audio valid-flag bits, so LEDs/rumble are untouched
    p[1] = 0x90
    p[2] = 0x3F
    if target == "headphone":
        p[3] = F0_HEADPHONE_VOLUME | F0_AUDIO_CONTROL  # 0x90
        p[7] = volume & 0xFF
    else:
        p[3] = F0_SPEAKER_VOLUME | F0_AUDIO_CONTROL  # 0xA0
        p[8] = volume & 0xFF
    p[4] = 0x00
    p[10] = 0x09  # audioControl

    # audio subpacket
    p[66] = 0x91
    p[67] = 0x07
    p[68] = 0xFF if mic_active else 0xFE
    p[69:74] = b"\x40" * 5
    p[74] = frame_counter & 0xFF
    p[75] = 0x96 if target == "headphone" else 0x93
    p[76] = OPUS_FRAME_BYTES  # 0xC8
    p[77 : 77 + OPUS_FRAME_BYTES] = bytes(opus_frame[:OPUS_FRAME_BYTES]).ljust(
        OPUS_FRAME_BYTES, b"\x00"
    )

    # haptic subpacket
    p[277] = 0x92
    p[278] = HAPTIC_FRAME_BYTES  # 0x40
    p[279 : 279 + HAPTIC_FRAME_BYTES] = bytes(haptic_frame[:HAPTIC_FRAME_BYTES]).ljust(
        HAPTIC_FRAME_BYTES, b"\x00"
    )

    fill_output_checksum(BT_OUT_36, p)
    return bytes([BT_OUT_36]) + bytes(p)


# --- 0x39: two Opus frames + two haptic frames -----------------------------


def build_report_39(
    opus_frames: tuple[bytes, bytes],
    haptic_frames: tuple[bytes, bytes],
    seq: int,
    packet_counter: int,
    target: str = "speaker",
    mic_enabled: bool = False,
    audio_buffer_length: int = 48,
) -> bytes:
    """547-byte BT audio+haptics report carrying two 10 ms frames.

    Port of DS5Dongle/src/audio.cpp:audio_bt_task. Note: unlike 0x36, the report
    id lives at pkt[0] and all documented offsets are absolute.
    """
    pkt = bytearray(BT_OUT_39_LEN)
    pkt[0] = BT_OUT_39
    pkt[1] = (seq & 0x0F) << 4
    pkt[2] = 0x11 | (0 << 6) | (1 << 7)  # 0x91
    pkt[3] = 6
    pkt[4] = 0x7F if mic_enabled else 0x7E
    pkt[5] = audio_buffer_length
    pkt[6] = audio_buffer_length
    pkt[7] = audio_buffer_length
    pkt[8] = audio_buffer_length
    pkt[9] = packet_counter & 0xFF
    pkt[10] = 0x12 | (1 << 6) | (1 << 7)  # 0xD2
    pkt[11] = HAPTIC_FRAME_BYTES
    pkt[12 : 12 + 64] = bytes(haptic_frames[0][:64]).ljust(64, b"\x00")
    pkt[76 : 76 + 64] = bytes(haptic_frames[1][:64]).ljust(64, b"\x00")
    route = 0x16 if target == "headphone" else 0x13
    pkt[140] = route | (1 << 6) | (1 << 7)
    pkt[141] = OPUS_FRAME_BYTES
    pkt[142 : 142 + 200] = bytes(opus_frames[0][:200]).ljust(200, b"\x00")
    pkt[342 : 342 + 200] = bytes(opus_frames[1][:200]).ljust(200, b"\x00")

    # CRC over the payload after the report id, seeded (0xA2, 0x39)
    body = bytearray(pkt[1:])
    fill_output_checksum(BT_OUT_39, body)
    pkt[1:] = body
    return bytes(pkt)


# --- 0x32 mic control + 0x31 mic state -------------------------------------

BT_MIC_OPUS_BYTES = 71
BT_MIC_OPUS_OFFSET = 2  # within the 0x31 payload (after report id)


def build_bt_mic_state(seq: int, active: bool, muted: bool = False,
                       headset_plugged: bool = False) -> bytes:
    """78-byte 0x31 output that arms/disarms the mic stream.

    Port of microphoneProtocol.ts:buildBtMicStateReport.
    """
    p = bytearray(BT_OUT_31_PAYLOAD)
    p[0] = (seq & 0x0F) << 4
    p[1] = 0x10
    off = 2
    p[off + VALID_FLAG0] = F0_MIC_VOLUME | F0_AUDIO_CONTROL          # 0xC0
    p[off + VALID_FLAG1] = F1_MIC_MUTE_LED | F1_POWER_SAVE_MUTE | F1_AUDIO_CONTROL2  # 0x83
    p[off + MIC_VOLUME] = 0x08 if (active and not muted) else 0x00
    p[off + AUDIO_CONTROL] = 0x08 if headset_plugged else 0x09
    p[off + MUTE_LED_CONTROL] = 0x01 if muted else 0x00
    p[off + POWER_SAVE_MUTE_CONTROL] = 0x0F if (active and not muted) else 0x1F
    p[off + AUDIO_CONTROL2] = 0x01
    fill_output_checksum(BT_OUT_31, p)
    return bytes([BT_OUT_31]) + bytes(p)


def build_bt_mic_control(seq: int, active: bool) -> bytes:
    """142-byte 0x32 companion report. Port of buildBtMicControlReport."""
    p = bytearray(BT_OUT_32_PAYLOAD)
    p[0] = (seq & 0x0F) << 4
    p[1] = 0x91
    p[2] = 0x07
    p[3] = 0xFF if active else 0xFE
    p[4:9] = b"\x40" * 5
    p[9] = seq & 0x0F
    p[10] = 0x92
    p[11] = HAPTIC_FRAME_BYTES
    fill_output_checksum(BT_OUT_32, p)
    return bytes([BT_OUT_32]) + bytes(p)


# ---------------------------------------------------------------------------
# Input report decoding
# ---------------------------------------------------------------------------

PAYLOAD_TYPE_MASK = 0x0F
PAYLOAD_TYPE_CONTROL = 0x01
PAYLOAD_TYPE_AUDIO = 0x02


class Offsets:
    """Offsets into the input report payload (report id already stripped).

    Port of ../dualsense-tester/src/router/DualSense/_utils/offset.util.ts:
    BT adds 1 because payload byte 0 is the seq/payload-type tag.
    """

    def __init__(self, usb: bool):
        n = 0 if usb else 1
        self.usb = usb
        self.stick_lx = 0 + n
        self.stick_ly = 1 + n
        self.stick_rx = 2 + n
        self.stick_ry = 3 + n
        self.trigger_l = 4 + n
        self.trigger_r = 5 + n
        self.sequence_num = 6 + n
        self.digital_keys = 7 + n
        self.incremental = 11 + n
        self.gyro_pitch = 15 + n
        self.gyro_yaw = 17 + n
        self.gyro_roll = 19 + n
        self.accel_x = 21 + n
        self.accel_y = 23 + n
        self.accel_z = 25 + n
        self.motion_timestamp = 27 + n
        self.motion_temperature = 31 + n
        self.touch_data = 32 + n
        self.at_status0 = 41 + n
        self.at_status1 = 42 + n
        self.host_timestamp = 43 + n
        self.at_status2 = 47 + n
        self.device_timestamp = 48 + n
        self.status0 = 52 + n
        self.status1 = 53 + n
        self.status2 = 54 + n
        self.aes_cmac = 55 + n
        self.crc32 = 73 if not usb else 0


OFFSETS_USB = Offsets(True)
OFFSETS_BT = Offsets(False)

DPAD_NAMES = ["N", "NE", "E", "SE", "S", "SW", "W", "NW", "-"]

BATTERY_STATE = {
    0x00: "discharging",
    0x01: "charging",
    0x02: "charging_complete",
    0x0A: "abnormal_voltage",
    0x0B: "abnormal_temperature",
    0x0F: "charging_error",
}


@dataclass
class TouchPoint:
    active: bool
    id: int
    x: int
    y: int


@dataclass
class InputState:
    lx: int
    ly: int
    rx: int
    ry: int
    l2: int
    r2: int
    seq: int
    dpad: str
    buttons: dict
    gyro: tuple
    accel: tuple
    motion_timestamp: int
    temperature: int
    touch: tuple
    battery_level: int
    battery_state: str
    headphone: bool
    mic: bool
    mic_muted: bool
    raw: bytes = b""

    def one_line(self) -> str:
        pressed = " ".join(k for k, v in self.buttons.items() if v) or "-"
        t = self.touch[0]
        tp = f"({t.x:4d},{t.y:4d})" if t.active else "(  -  ,  - )"
        return (
            f"L({self.lx:3d},{self.ly:3d}) R({self.rx:3d},{self.ry:3d}) "
            f"L2={self.l2:3d} R2={self.r2:3d} D={self.dpad:2s} "
            f"gyro=({self.gyro[0]:6d},{self.gyro[1]:6d},{self.gyro[2]:6d}) "
            f"acc=({self.accel[0]:6d},{self.accel[1]:6d},{self.accel[2]:6d}) "
            f"tp{tp} bat={self.battery_level*10:3d}%/{self.battery_state[:4]} "
            f"[{pressed}]"
        )


def _touch(payload: bytes, base: int) -> TouchPoint:
    raw_id = payload[base]
    x = ((payload[base + 2] & 0x0F) << 8) | payload[base + 1]
    y = (payload[base + 3] << 4) | (payload[base + 2] >> 4)
    return TouchPoint(active=(raw_id & 0x80) == 0, id=raw_id & 0x7F, x=x, y=y)


def decode_input(payload: bytes, usb: bool) -> InputState | None:
    """Decode an input report payload (report id already stripped).

    For BT this must be a type-0x01 (control) payload; type 0x02 is mic audio and
    returns None.
    """
    o = OFFSETS_USB if usb else OFFSETS_BT
    if not usb:
        if len(payload) < 77:
            return None
        if (payload[0] & PAYLOAD_TYPE_MASK) != PAYLOAD_TYPE_CONTROL:
            return None
    elif len(payload) < 63:
        return None

    k0 = payload[o.digital_keys]
    k1 = payload[o.digital_keys + 1]
    k2 = payload[o.digital_keys + 2]
    buttons = {
        "sq": bool(k0 & 0x10),
        "x": bool(k0 & 0x20),
        "o": bool(k0 & 0x40),
        "tri": bool(k0 & 0x80),
        "L1": bool(k1 & 0x01),
        "R1": bool(k1 & 0x02),
        "L2": bool(k1 & 0x04),
        "R2": bool(k1 & 0x08),
        "create": bool(k1 & 0x10),
        "options": bool(k1 & 0x20),
        "L3": bool(k1 & 0x40),
        "R3": bool(k1 & 0x80),
        "PS": bool(k2 & 0x01),
        "touchpad": bool(k2 & 0x02),
        "mute": bool(k2 & 0x04),
    }
    dpad_idx = k0 & 0x0F
    gyro = struct.unpack_from("<hhh", payload, o.gyro_pitch)
    accel = struct.unpack_from("<hhh", payload, o.accel_x)
    status0 = payload[o.status0]
    status1 = payload[o.status1]
    state = BATTERY_STATE.get((status0 & 0xF0) >> 4, "unknown")
    level = status0 & 0x0F
    if state == "charging_complete":
        level = 10

    return InputState(
        lx=payload[o.stick_lx],
        ly=payload[o.stick_ly],
        rx=payload[o.stick_rx],
        ry=payload[o.stick_ry],
        l2=payload[o.trigger_l],
        r2=payload[o.trigger_r],
        seq=payload[o.sequence_num],
        dpad=DPAD_NAMES[dpad_idx] if dpad_idx < len(DPAD_NAMES) else "?",
        buttons=buttons,
        gyro=gyro,
        accel=accel,
        motion_timestamp=struct.unpack_from("<I", payload, o.motion_timestamp)[0],
        temperature=struct.unpack_from("<b", payload, o.motion_temperature)[0],
        touch=(_touch(payload, o.touch_data), _touch(payload, o.touch_data + 4)),
        battery_level=level,
        battery_state=state,
        headphone=bool(status1 & 0x01),
        mic=bool(status1 & 0x02),
        mic_muted=bool(status1 & 0x04),
        raw=payload,
    )


def is_mic_audio_payload(payload: bytes) -> bool:
    return (
        len(payload) >= BT_MIC_OPUS_OFFSET + BT_MIC_OPUS_BYTES
        and (payload[0] & PAYLOAD_TYPE_MASK) == PAYLOAD_TYPE_AUDIO
    )


def get_mic_opus(payload: bytes) -> bytes:
    return bytes(payload[BT_MIC_OPUS_OFFSET : BT_MIC_OPUS_OFFSET + BT_MIC_OPUS_BYTES])
