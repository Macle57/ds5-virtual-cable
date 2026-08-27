"""Byte-level translation between Bluetooth and USB DualSense report shapes.

This module is **pure** — no hardware, no threads, no third-party packages. It
imports `ds5bridge.protocol` (stdlib-only: struct + zlib) so the offsets stay in
exactly one place; `emulator/tests/test_translate.py` exercises every function
here without a controller attached.

The two input shapes
--------------------

    USB  report 0x01   64 bytes = [0]=0x01 + 63-byte body
    BT   report 0x31   78 bytes = [0]=0x31 + 77-byte payload
                                  payload[0] = seq<<4 | payload-type nibble

`ds5bridge.protocol.Offsets` already encodes the whole relationship: it is built
with `n = 0` for USB and `n = 1` for BT, i.e. *every* field in the BT payload
sits exactly one byte later than the same field in the USB body. So the
translation is a single slice:

    usb_body[0:63] == bt_payload[1:64]

and that identity — not a hand-copied offset table — is what this module
implements and what the unit tests assert field by field through
`protocol.decode_input()`.

What the BT report has that the USB one does not: bytes 64..72 of the payload
(9 unknown/reserved bytes) and the 4-byte CRC32 at 73..76. Both are Bluetooth
transport concerns and are dropped. What the USB report has that BT does not:
nothing — the last 8 bytes of the USB body are the AES-CMAC field, which lands
at BT payload 56..63 and is copied through verbatim.

The output direction is even simpler: the USB output report 0x02 body and the
BT output report 0x31 SetState body are the *same 47 bytes* (FINDINGS.md
"Output 0x31 ... Same field layout as the USB 0x02 report body, wrapped with
seq nibble + CRC"), so `usb02_to_bt31()` is unwrap-then-rewrap, never a
re-encode.
"""

from __future__ import annotations

from . import _bootstrap  # noqa: F401  (sys.path side effect)

from ds5bridge import protocol as P  # noqa: E402

# --- sizes ------------------------------------------------------------------

USB_INPUT_ID = P.USB_INPUT_01            # 0x01
USB_INPUT_LEN = P.USB_INPUT_01_LEN       # 64, report id included
USB_INPUT_BODY_LEN = USB_INPUT_LEN - 1   # 63

BT_INPUT_ID = P.BT_INPUT_31              # 0x31
BT_INPUT_LEN = P.BT_INPUT_31_LEN         # 78, report id included
BT_INPUT_PAYLOAD_LEN = BT_INPUT_LEN - 1  # 77

USB_OUTPUT_ID = P.USB_OUT_02             # 0x02
USB_OUTPUT_LEN = P.USB_OUT_02_PAYLOAD + 1  # 48, report id included
SETSTATE_BODY_LEN = P.SETSTATE_BODY_LEN  # 47

#: BT payload byte 0 is the seq/payload-type tag; every field is shifted by it.
BT_BODY_SHIFT = 1
assert P.OFFSETS_BT.stick_lx - P.OFFSETS_USB.stick_lx == BT_BODY_SHIFT
assert P.OFFSETS_BT.status2 - P.OFFSETS_USB.status2 == BT_BODY_SHIFT

#: Offset of the device sequence byte (vendor usage 0x20) inside the *body*.
USB_SEQ_OFFSET = P.OFFSETS_USB.sequence_num          # 6
BT_SEQ_OFFSET = P.OFFSETS_BT.sequence_num            # 7

#: The BT payload is long enough to cover the whole USB body...
assert BT_INPUT_PAYLOAD_LEN >= BT_BODY_SHIFT + USB_INPUT_BODY_LEN
#: ...and the CRC32 tail sits strictly beyond it, so it is never copied.
assert P.OFFSETS_BT.crc32 >= BT_BODY_SHIFT + USB_INPUT_BODY_LEN


# --- input: BT 0x31 -> USB 0x01 ---------------------------------------------


def bt31_payload_to_usb01(payload: bytes, seq: int | None = None) -> bytes | None:
    """Translate a BT 0x31 *payload* (report id already stripped) to a complete
    64-byte USB input report 0x01, report id included.

    Returns None when the payload is not a control payload — BT multiplexes the
    microphone stream onto the same report id as payload type 0x02, and those
    carry no button state at all.

    `seq`, when given, overwrites the device sequence byte. The USB link is
    polled at 250 Hz while Bluetooth delivers ~476 Hz, so roughly every second
    BT report is dropped; a wired DualSense's sequence byte increments once per
    delivered report, and renumbering keeps that invariant true downstream.
    """
    if len(payload) < BT_BODY_SHIFT + USB_INPUT_BODY_LEN:
        return None
    if (payload[0] & P.PAYLOAD_TYPE_MASK) != P.PAYLOAD_TYPE_CONTROL:
        return None
    body = bytearray(payload[BT_BODY_SHIFT : BT_BODY_SHIFT + USB_INPUT_BODY_LEN])
    if seq is not None:
        body[USB_SEQ_OFFSET] = seq & 0xFF
    return bytes([USB_INPUT_ID]) + bytes(body)


def bt31_report_to_usb01(report: bytes, seq: int | None = None) -> bytes | None:
    """Same as `bt31_payload_to_usb01` but takes the full report *with* its id."""
    if len(report) < 1 or report[0] != BT_INPUT_ID:
        return None
    return bt31_payload_to_usb01(report[1:], seq)


def usb01_to_bt31_payload(report: bytes, tag: int = P.PAYLOAD_TYPE_CONTROL) -> bytes:
    """Inverse of `bt31_payload_to_usb01`, for round-trip testing.

    The 9 reserved bytes and the CRC32 that only exist on the Bluetooth side
    cannot be recovered and come back zero-filled; everything the USB report
    carries round-trips byte for byte.
    """
    body = report[1:] if report and report[0] == USB_INPUT_ID else report
    payload = bytearray(BT_INPUT_PAYLOAD_LEN)
    payload[0] = tag & 0xFF
    payload[BT_BODY_SHIFT : BT_BODY_SHIFT + len(body)] = body[:USB_INPUT_BODY_LEN]
    return bytes(payload)


def is_mic_payload(payload: bytes) -> bool:
    """True when a BT 0x31 payload carries a 71-byte Opus microphone frame."""
    return P.is_mic_audio_payload(payload)


#: D-pad "nothing pressed". The low nibble of digital_keys is a hat switch, so
#: 0 means NORTH, not neutral — a zero-filled report reads as "up held".
DPAD_RELEASED = 8


def neutralize_usb01(report: bytes) -> bytes:
    """The same report with every *actuated* control released.

    Used when the Bluetooth link has gone away. The alternative — keep
    repeating the last report the controller sent — hands the game whatever was
    held at the instant the link dropped, so a controller switched off mid-sprint
    leaves the stick pinned forever. A wired DualSense that is unplugged simply
    stops existing; a virtual one that stays attached must at least stop
    pressing things.

    Preserved on purpose: the status bytes (battery, headphone, mic), the
    adaptive-trigger status, the timestamps and the AES-CMAC field. Those are
    device state, not user input, and games read the battery byte.
    """
    o = P.OFFSETS_USB
    body = bytearray(report[1:] if report and report[0] == USB_INPUT_ID else report)
    if len(body) < USB_INPUT_BODY_LEN:
        body.extend(b"\0" * (USB_INPUT_BODY_LEN - len(body)))
    for off in (o.stick_lx, o.stick_ly, o.stick_rx, o.stick_ry):
        body[off] = 0x80                      # centred
    body[o.trigger_l] = 0
    body[o.trigger_r] = 0
    body[o.digital_keys] = DPAD_RELEASED      # hat neutral, face buttons clear
    body[o.digital_keys + 1] = 0
    body[o.digital_keys + 2] = 0
    for off in (o.gyro_pitch, o.gyro_yaw, o.gyro_roll):
        body[off] = 0
        body[off + 1] = 0
    # Touch: bit 7 of the id byte set == no finger down. Both points.
    body[o.touch_data] |= 0x80
    body[o.touch_data + 4] |= 0x80
    return bytes([USB_INPUT_ID]) + bytes(body)


# --- output: USB 0x02 -> BT 0x31 --------------------------------------------


def usb02_body(data: bytes) -> bytes:
    """Extract the 47-byte SetState body from a USB output report 0x02.

    Tolerates both framings Windows can produce: an interrupt-OUT transfer on
    endpoint 0x03 carries the report id as byte 0, whereas a control SET_REPORT
    puts the id in wValue and *may or may not* repeat it in the data stage
    depending on the caller. Anything shorter than 47 bytes is zero-padded, so a
    truncated report degrades to "no valid flags set" rather than a crash.
    """
    if data and data[0] == USB_OUTPUT_ID and len(data) >= SETSTATE_BODY_LEN + 1:
        data = data[1:]
    return bytes(_setstate_body(data))


def usb02_to_bt31(data: bytes, seq: int) -> bytes:
    """USB output report 0x02 -> a 78-byte BT 0x31 output report, CRC32 filled.

    Pure passthrough of the body: only fields whose valid-flag bit is set are
    applied by the controller, so wrapping the host's bytes verbatim cannot
    clobber state the host did not ask to change.
    """
    return P.build_bt_setstate(usb02_body(data), seq)


# --- output: merging two unsent SetState bodies ------------------------------
#
# WHY THIS EXISTS. `BridgeBackend` coalesces SetState passthroughs so a host
# re-sending an unchanged body at 250 Hz costs no Bluetooth airtime. Until Phase
# 4c that coalescing *replaced* the pending body, and that quietly lost data:
# games set the player-LED bits ONCE, in an early output report, and then stream
# rumble/trigger reports that do not carry the player-indicator valid flag at
# all. If the LED report was still pending when the next one arrived it was
# overwritten and the LEDs never lit. A real wired DualSense drops nothing.
#
# Merging is exact rather than a heuristic, because the SetState body is
# valid-flag driven: the firmware applies a field only when its gating bit is
# present in validFlag0/1/2 (bytes 0, 1 and 38). So "the controller saw A then
# B" and "the controller saw merge(A, B)" differ only in fields that BOTH
# reports claimed -- and there the newer value is the right one.
#
# The map below is FINDINGS.md "Output 0x31 ... MERGEABLE", which is
# `ds5bridge.protocol`'s own constants cross-checked against Linux
# `hid-playstation` and the tester's OutputPanel.

#: `validFlag2` "lightbar setup": the tester uses bit 0, Linux hid-playstation
#: uses bit 1, and nobody has settled it. Treat either as gating bytes 41/42 --
#: over-gating is harmless here, because a gate only ever decides whether an
#: older byte is *carried forward*, never whether it is applied.
F2_LIGHTBAR_SETUP_EITHER = P.F2_LIGHTBAR_SETUP | (1 << 1)

#: (valid-flag byte, bit mask, the body offsets that bit gates).
FLAG_GATES = (
    (P.VALID_FLAG0, P.F0_COMPATIBLE_VIBRATION,
     (P.BC_VIBRATION_RIGHT, P.BC_VIBRATION_LEFT)),
    (P.VALID_FLAG0, P.F0_RIGHT_TRIGGER_FFB,
     tuple(range(P.AT_RIGHT_MODE, P.AT_RIGHT_MODE + 11))),
    (P.VALID_FLAG0, P.F0_LEFT_TRIGGER_FFB,
     tuple(range(P.AT_LEFT_MODE, P.AT_LEFT_MODE + 11))),
    (P.VALID_FLAG0, P.F0_HEADPHONE_VOLUME, (P.HEADPHONE_VOLUME,)),
    (P.VALID_FLAG0, P.F0_SPEAKER_VOLUME, (P.SPEAKER_VOLUME,)),
    (P.VALID_FLAG0, P.F0_MIC_VOLUME, (P.MIC_VOLUME,)),
    (P.VALID_FLAG0, P.F0_AUDIO_CONTROL, (P.AUDIO_CONTROL,)),
    (P.VALID_FLAG1, P.F1_MIC_MUTE_LED, (P.MUTE_LED_CONTROL,)),
    (P.VALID_FLAG1, P.F1_POWER_SAVE_MUTE, (P.POWER_SAVE_MUTE_CONTROL,)),
    (P.VALID_FLAG1, P.F1_LIGHTBAR_CONTROL, (P.LED_R, P.LED_G, P.LED_B)),
    (P.VALID_FLAG1, P.F1_PLAYER_INDICATOR, (P.PLAYER_INDICATOR,)),
    (P.VALID_FLAG1, P.F1_OVERALL_EFFECT_POWER | P.F1_AUDIO_CONTROL2,
     (P.HAPTIC_VOLUME, P.AUDIO_CONTROL2)),
    (P.VALID_FLAG2, F2_LIGHTBAR_SETUP_EITHER,
     (P.LIGHTBAR_SETUP, P.LED_BRIGHTNESS)),
)

#: body offset -> the (flag byte, mask) pairs that gate it. Offsets absent from
#: this map (the three flag bytes themselves and the reserved runs) are not
#: gated by anything, so the newer report simply wins.
GATES_BY_OFFSET: dict[int, tuple[tuple[int, int], ...]] = {}
for _flag_off, _mask, _offsets in FLAG_GATES:
    for _off in _offsets:
        GATES_BY_OFFSET[_off] = GATES_BY_OFFSET.get(_off, ()) + ((_flag_off, _mask),)
del _flag_off, _mask, _offsets, _off

#: The three LED-controlling bits of validFlag1, i.e. the ones "release LEDs"
#: takes back.
F1_LED_BITS = P.F1_MIC_MUTE_LED | P.F1_LIGHTBAR_CONTROL | P.F1_PLAYER_INDICATOR


def _setstate_body(data: bytes) -> bytearray:
    body = bytearray(SETSTATE_BODY_LEN)
    n = min(len(data), SETSTATE_BODY_LEN)
    body[:n] = data[:n]
    return body


def merge_setstate(older: bytes, newer: bytes) -> bytes:
    """Fold two *unsent* SetState bodies into one the controller can apply once.

    Both arguments are 47-byte bodies (short input is zero-padded). The result
    carries every field either report asked for; where both asked for the same
    field the newer value wins, and where neither did the newer bytes are kept
    verbatim so reserved/undocumented bytes still track the freshest report.

    `validFlag1` bit 3, "release LEDs", is the one bit that is NOT OR-ed:
    releasing is an instruction to stop driving the LEDs, so the newest report
    decides it. If the newer report releases them, the older report's LED
    control bits are dropped too rather than being resurrected alongside the
    release -- "apply then release" and "release" have the same end state, and
    a single report that claims both is undefined behaviour.
    """
    a = _setstate_body(older)
    b = _setstate_body(newer)
    out = bytearray(b)

    for off, gates in GATES_BY_OFFSET.items():
        if any(b[flag] & mask for flag, mask in gates):
            continue                      # the newer report set this field
        if any(a[flag] & mask for flag, mask in gates):
            out[off] = a[off]             # only the older one did: carry it

    out[P.VALID_FLAG0] = a[P.VALID_FLAG0] | b[P.VALID_FLAG0]
    out[P.VALID_FLAG2] = a[P.VALID_FLAG2] | b[P.VALID_FLAG2]
    f1 = (a[P.VALID_FLAG1] | b[P.VALID_FLAG1]) & ~P.F1_RELEASE_LEDS & 0xFF
    f1 |= b[P.VALID_FLAG1] & P.F1_RELEASE_LEDS
    if b[P.VALID_FLAG1] & P.F1_RELEASE_LEDS:
        f1 = (f1 & ~F1_LED_BITS & 0xFF) | (b[P.VALID_FLAG1] & F1_LED_BITS)
        out[P.VALID_FLAG2] = b[P.VALID_FLAG2]
    out[P.VALID_FLAG1] = f1
    return bytes(out)


def setstate_flags(body: bytes) -> tuple[int, int, int]:
    """(validFlag0, validFlag1, validFlag2) of a SetState body -- for tests/logs."""
    b = _setstate_body(body)
    return b[P.VALID_FLAG0], b[P.VALID_FLAG1], b[P.VALID_FLAG2]


def bt31_output_body(report: bytes) -> bytes:
    """Pull the SetState body back out of a BT 0x31 *output* report (tests)."""
    payload = report[1:] if report and report[0] == P.BT_OUT_31 else report
    return bytes(payload[2 : 2 + SETSTATE_BODY_LEN])


def verify_bt_output_crc(report: bytes) -> bool:
    """True when the CRC32 tail of a host->device BT report is correct."""
    if len(report) < 6:
        return False
    rid, payload = report[0], report[1:]
    from ds5bridge.crc import crc32_seeded, SEED_OUTPUT

    want = crc32_seeded(SEED_OUTPUT, rid, bytes(payload[:-4]))
    return want.to_bytes(4, "little") == bytes(payload[-4:])


# --- field parity check (used by the tests and by the live soak tool) --------

#: Every field `protocol.decode_input()` knows about, except `raw` (which is the
#: payload itself and therefore intentionally different between transports).
PARITY_FIELDS = (
    "lx", "ly", "rx", "ry", "l2", "r2", "dpad", "buttons", "gyro", "accel",
    "motion_timestamp", "temperature", "touch", "battery_level", "battery_state",
    "headphone", "mic", "mic_muted",
)


def field_parity(bt_payload: bytes, usb_report: bytes) -> list[str]:
    """Decode both shapes with the Phase-1 decoder; return mismatching fields.

    An empty list means the translation preserved every decoded field. `seq` is
    checked separately by the caller because renumbering deliberately changes it.
    """
    a = P.decode_input(bt_payload, usb=False)
    b = P.decode_input(usb_report[1:], usb=True)
    if a is None or b is None:
        return ["<undecodable>"]
    bad = []
    for name in PARITY_FIELDS:
        if getattr(a, name) != getattr(b, name):
            bad.append(name)
    return bad
