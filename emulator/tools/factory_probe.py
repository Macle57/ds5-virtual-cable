"""Phase 4c probe: read a real DualSense's factory-test channel, straight over HID.

This is the ground-truth instrument for the `0x80` -> `0x81` request/response
pair that `bridge.py` forwards. It talks to the physical controller through the
Phase-1 `ds5bridge.device` helpers -- no emulator, no USB/IP, no drivers -- so
whatever it prints is what the pad actually said, and any disagreement with the
bridge path is the bridge's fault, not the protocol's.

What it does, in order:

    GET feature 0x05   calibration; on Bluetooth this GET is also what flips the
                       controller out of minimal 0x01 mode into extended 0x31
    GET feature 0x20   firmware/build info (Factory Info's whole top half)
    GET feature 0x22   Bluetooth patch info
    SET 0x80 / GET 0x81 for every entry in bridge.TEST_COMMAND_ALLOWLIST, each
                       polled 10 ms apart with a 1000 ms cap, honouring the
                       status byte (2 = COMPLETE, 3 = COMPLETE_2 / more pages)
    SET 0x80 / GET 0x81 for the paged telemetry block (device 0x70 action 1)

SAFETY. The command set is taken from `bridge.TEST_COMMAND_ALLOWLIST` at import
time and is never extended here. Every entry is a read. Nothing in this file
writes controller state: no pairing report 0x09, no WRITE_*/ERASE_*/AGING_*/
NVS_*/bootloader action, no 0x84/0x85 individual-data channel. If you are
tempted to add a command, add it to the allowlist in `bridge.py` first, with a
reason, or do not add it.

    cd emulator
    ..\\prototype\\.venv\\Scripts\\python.exe tools\\factory_probe.py
    ..\\prototype\\.venv\\Scripts\\python.exe tools\\factory_probe.py --transport USB
    ..\\prototype\\.venv\\Scripts\\python.exe tools\\factory_probe.py --framing --raw
"""

from __future__ import annotations

import argparse
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ds5emu.bridge import (            # noqa: E402
    FEATURE_TEST_CMD,
    FEATURE_TEST_PAYLOAD_LEN,
    FEATURE_TEST_RESULT,
    TEST_COMMAND_ALLOWLIST,
)

from ds5bridge import device as DEV            # noqa: E402
from ds5bridge.crc import fill_feature_checksum  # noqa: E402

#: The tester's own pacing (`ds.util.ts`): sleep 10 ms between 0x81 polls, give
#: up on a command after 1000 ms of wall clock.
POLL_INTERVAL = 0.010
COMMAND_TIMEOUT = 1.000

#: `TestStatus` in `ds.type.ts`.
ST_IDLE, ST_RUNNING, ST_COMPLETE, ST_COMPLETE_2, ST_TIMEOUT = 0, 1, 2, 3, 0xFF

#: One 0x81 answer carries 56 payload bytes: 63 data bytes minus the 3-byte
#: header (deviceId, actionId, status) minus the 4-byte Bluetooth CRC tail.
PAGE_SIZE = 56

#: `telemetry.util.ts`: 4 pages for a DualSense, 6 for an Edge.
TELEMETRY_PAGES = 4

#: Serial-number characters 4..6 pick the shell colour (`DualSenseColorMap`).
COLOR_MAP = {
    "00": "White", "01": "Midnight Black", "02": "Cosmic Red",
    "03": "Nova Pink", "04": "Galactic Purple", "05": "Starlight Blue",
    "06": "Grey Camouflage", "07": "Volcanic Red", "08": "Sterling Silver",
    "09": "Cobalt Blue", "10": "Chroma Teal", "11": "Chroma Indigo",
    "12": "Chroma Pearl", "30": "30th Anniversary",
    "Z1": "God of War Ragnarok", "Z2": "Spider-Man 2", "Z3": "Astro Bot",
    "Z4": "Fortnite", "Z6": "The Last of Us", "Z7": "Ghost of Yotei",
    "ZB": "Icon Blue Limited Edition", "ZC": "Astro Bot Joyful",
    "ZE": "Genshin Impact",
}


# ---------------------------------------------------------------------------
# small formatting helpers -- ports of the tester's format.util.ts
# ---------------------------------------------------------------------------


def sjis(data: bytes) -> str:
    """Shift-JIS, NULs stripped, exactly like the tester's `decodeShiftJIS`."""
    try:
        s = data.decode("shift_jis", errors="replace")
    except Exception:  # noqa: BLE001
        s = data.decode("latin-1", errors="replace")
    return s.replace("\x00", "")


def hex_le(data: bytes) -> str:
    """The tester's `mapDataViewToU8Hex(..., littleEndian=True)`."""
    return "".join(f"{b:02X}" for b in reversed(data))


def mac(data: bytes) -> str:
    return ":".join(f"{b:02X}" for b in reversed(data[:6]))


def three_part(ver: int) -> str:
    return f"{(ver >> 24) & 0xFF}.{(ver >> 16) & 0xFF}.{ver & 0xFFFF}"


def update_version(ver: int) -> str:
    return f"{(ver >> 8) & 0xFF:X}.{ver & 0xFF:02X}"


def dsp_version(ver: int) -> str:
    return f"{(ver >> 16) & 0xFFFF:04X}_{ver & 0xFFFF:04X}"


def duration(seconds: int) -> str:
    s = max(0, int(seconds))
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    parts = []
    if d:
        parts.append(f"{d}d")
    if h or d:
        parts.append(f"{h}h")
    if m or h or d:
        parts.append(f"{m}m")
    parts.append(f"{s}s")
    return " ".join(parts)


class Table:
    """Two-column label/value printer with the columns aligned at the end."""

    def __init__(self, title: str):
        self.title = title
        self.rows: list[tuple[str, str]] = []

    def add(self, label: str, value) -> None:
        self.rows.append((label, "" if value is None else str(value)))

    def print(self) -> None:
        print(f"\n== {self.title} " + "=" * max(0, 60 - len(self.title)))
        if not self.rows:
            print("   (nothing)")
            return
        w = max(len(r[0]) for r in self.rows)
        for label, value in self.rows:
            head, _, tail = value.partition("\n")
            print(f"  {label.ljust(w)}  {head}")
            for extra in tail.splitlines():
                print(f"  {' ' * w}  {extra}")


# ---------------------------------------------------------------------------
# the factory-test channel
# ---------------------------------------------------------------------------


class Probe:
    """One open controller plus the 0x80/0x81 conversation on top of it."""

    def __init__(self, dev: DEV.DualSense, crc: bool | None = None, raw: bool = False):
        self.dev = dev
        #: Whether to sign 0x80 with the 0x53-seeded CRC32. Defaults to "when
        #: the transport is Bluetooth", which is what the tester does.
        self.crc = dev.is_bt if crc is None else crc
        self.raw = raw
        self.sent = 0
        self.reads = 0

    # -- plain feature reads ------------------------------------------------

    def get_feature(self, report_id: int, length: int = 64) -> bytes | None:
        try:
            return self.dev.get_feature(report_id, length)
        except Exception as e:  # noqa: BLE001
            print(f"  GET feature 0x{report_id:02x} failed: {e}")
            return None

    # -- SET 0x80 -----------------------------------------------------------

    def build_command(self, device_id: int, action_id: int, params: bytes = b"") -> bytes:
        """The 63-byte 0x80 body, CRC-signed when the link is Bluetooth.

        Report id goes in front as byte 0 because hidapi wants it there; WebHID
        passes it separately, which is why the tester's buffer looks one byte
        shorter for the same wire report.
        """
        buf = bytearray(FEATURE_TEST_PAYLOAD_LEN)
        buf[0] = device_id
        buf[1] = action_id
        buf[2:2 + len(params)] = params
        if self.crc:
            fill_feature_checksum(FEATURE_TEST_CMD, buf)
        return bytes([FEATURE_TEST_CMD]) + bytes(buf)

    def send_command(self, device_id: int, action_id: int, params: bytes = b"") -> bool:
        """Write one 0x80. hidapi returns -1 rather than raising when Windows
        rejects the report, so the return value is checked, not just exceptions."""
        report = self.build_command(device_id, action_id, params)
        if self.raw:
            print(f"  -> 0x80 [{len(report)}] {report.hex(' ')}")
        try:
            rc = self.dev.h.send_feature_report(report)
        except Exception as e:  # noqa: BLE001
            print(f"  SET 0x80 {device_id:#04x}/{action_id:#04x} raised: {e}")
            return False
        self.sent += 1
        if rc < 0:
            print(f"  SET 0x80 {device_id:#04x}/{action_id:#04x} rejected (rc={rc})")
            return False
        return True

    # -- GET 0x81 -----------------------------------------------------------

    def read_result(self) -> bytes | None:
        try:
            raw = bytes(self.dev.h.get_feature_report(
                FEATURE_TEST_RESULT, FEATURE_TEST_PAYLOAD_LEN + 1))
        except Exception as e:  # noqa: BLE001
            print(f"  GET 0x81 raised: {e}")
            return None
        self.reads += 1
        if self.raw and raw:
            print(f"  <- 0x81 [{len(raw)}] {raw.hex(' ')}")
        return raw or None

    def await_page(self, device_id: int, action_id: int, deadline: float):
        """Poll 0x81 until this command's header comes back with a terminal
        status. Returns (status, 56-byte page) or (None, None).

        Port of `getTestResult`. The header is echoed at bytes 1..3 of the
        hidapi buffer (report id at 0), one byte later than the tester's
        DataView offsets because WebHID hands the id back at offset 0 too --
        same layout, and that is the point.
        """
        while True:
            raw = self.read_result()
            if raw and len(raw) >= 4 and raw[0] == FEATURE_TEST_RESULT \
                    and raw[1] == device_id and raw[2] == action_id:
                status = raw[3]
                if status in (ST_COMPLETE, ST_COMPLETE_2):
                    return status, raw[4:4 + PAGE_SIZE]
            if time.perf_counter() >= deadline:
                return None, None
            time.sleep(POLL_INTERVAL)

    def call(self, device_id: int, action_id: int, result_len: int,
             params: bytes = b"") -> bytes | None:
        """One complete `sendTestCommand`: 0x80, then 0x81 pages until COMPLETE."""
        if not self.send_command(device_id, action_id, params):
            return None
        deadline = time.perf_counter() + COMMAND_TIMEOUT
        out = bytearray()
        while True:
            status, page = self.await_page(device_id, action_id, deadline)
            if status is None:
                return None
            out += page
            if status == ST_COMPLETE:
                break
        return bytes(out[:result_len])

    def call_paged(self, device_id: int, action_id: int,
                   max_pages: int = TELEMETRY_PAGES) -> list[bytes]:
        """`readPagedTestBlock`: every page answers COMPLETE_2 and the device
        then clears the 0x81 header, so the end of the block is "the header
        stopped echoing us", not a COMPLETE status. Returns the pages it got."""
        if not self.send_command(device_id, action_id):
            return []
        pages: list[bytes] = []
        deadline = time.perf_counter() + COMMAND_TIMEOUT * max_pages
        attempts = max_pages * 4 + 8
        for _ in range(attempts):
            if len(pages) >= max_pages or time.perf_counter() >= deadline:
                break
            raw = self.read_result()
            matched = (raw and len(raw) >= 4 and raw[0] == FEATURE_TEST_RESULT
                       and raw[1] == device_id and raw[2] == action_id)
            if matched:
                status = raw[3]
                if status in (ST_COMPLETE, ST_COMPLETE_2):
                    pages.append(raw[4:4 + PAGE_SIZE])
                    if status == ST_COMPLETE:
                        break
            elif pages:
                break  # header cleared: the block is finished
            time.sleep(POLL_INTERVAL)
        return pages


# ---------------------------------------------------------------------------
# parsing -- ports of FactoryInfo.vue and telemetry.util.ts
# ---------------------------------------------------------------------------


def parse_firmware(raw: bytes) -> dict:
    """Feature 0x20. Offsets are the tester's, i.e. report id at byte 0."""
    d = raw
    u16 = lambda o: struct.unpack_from("<H", d, o)[0]   # noqa: E731
    u32 = lambda o: struct.unpack_from("<I", d, o)[0]   # noqa: E731
    return {
        "build_date": d[1:12].decode("ascii", "replace").strip("\x00"),
        "build_time": d[12:20].decode("ascii", "replace").strip("\x00"),
        "fw_type": u16(20),
        "sw_series": u16(22),
        "hw_info": u32(24),
        "main_fw": u32(28),
        "device_info": d[32:44],
        "update_version": u16(44),
        "sbl_fw": u32(48),
        "dsp_fw": u32(52),
        "mcu_dsp_fw": u32(56),
    }


def _read_low(d: bytes, off: int) -> int:
    """u24 out of a u32; a still-erased NVRAM cell reads back as ~0 and means 0."""
    v = struct.unpack_from("<I", d, off)[0] & 0xFFFFFF
    return v if v < 0xFFFF00 else 0


def _read_count(d: bytes, off: int) -> int:
    v = struct.unpack_from("<I", d, off)[0]
    low, high = v & 0xFFFF, (v >> 16) & 0xFFFF
    if high == 0:
        return low
    if high >= 0xFF00 and low >= 0xFF00:
        return 0
    return low


def _read_ushort(d: bytes, off: int) -> int:
    return struct.unpack_from("<H", d, off)[0] if d[off + 1] < 0xE0 else d[off]


TELEMETRY_BUTTONS = [
    ("D-pad up", 110), ("D-pad down", 114), ("D-pad left", 118),
    ("D-pad right", 122), ("Triangle", 126), ("Cross", 130), ("Square", 134),
    ("Circle", 138), ("L1", 142), ("L2", 146), ("L3", 150), ("R1", 154),
    ("R2", 158), ("R3", 162), ("Touchpad touch", 166), ("Touchpad press", 170),
    ("Options", 174), ("Create", 178), ("PS", 182), ("Mute", 186),
]


def parse_telemetry(block: bytes) -> tuple[dict, list[tuple[str, int]]]:
    """`block` is the concatenated pages; byte 0 is a status byte and the struct
    starts after it (`new DataView(block.buffer, 1)`)."""
    d = block[1:]
    if len(d) < 190:
        d = d + b"\x00" * (190 - len(d))
    u16 = lambda o: struct.unpack_from("<H", d, o)[0]   # noqa: E731
    u32 = lambda o: struct.unpack_from("<I", d, o)[0]   # noqa: E731
    common = {
        "peripheral serial": sjis(d[0:17]).rstrip(),
        "total record count": u32(17),
        "total active time": f"{_read_low(d, 21)} s  ({duration(_read_low(d, 21))})",
        "total charge time": f"{_read_low(d, 25)} s  ({duration(_read_low(d, 25))})",
        "haptic active time": f"{_read_low(d, 29)} s  ({duration(_read_low(d, 29))})",
        "AT left active time": _read_count(d, 33),
        "AT right active time": _read_count(d, 37),
        "battery charge count": _read_ushort(d, 41),
        "USB SDP detect": u16(43),
        "USB CDP detect": _read_ushort(d, 45),
        "USB DCP detect": u16(47),
        "USB Type-C 1.5A detect": u16(49),
        "USB Type-C 3.0A detect": u16(51),
        "USB unknown detect": u16(53),
        "charger connect count": u16(55),
        "charge voltage errors": d[57],
        "charge temperature errors": d[58],
        "charge PMIC errors": d[59],
        "battery full-charge count": u16(60),
        "headset detect count": u16(62),
        "headphone detect count": u16(64),
        "USB connect count": u16(66),
        "Bluetooth connect count": u16(68),
        "Bluetooth connect timeouts": u16(70),
        "sub-CPU startup errors": u16(72),
        "auth challenge count": _read_count(d, 74),
        "auth success count": _read_count(d, 78),
        "auth fail count": _read_count(d, 82),
        "LX round trip / range": f"{u32(86)} / {u16(90)}",
        "LY round trip / range": f"{u32(92)} / {u16(96)}",
        "RX round trip / range": f"{u32(98)} / {u16(102)}",
        "RY round trip / range": f"{u32(104)} / {u16(108)}",
    }
    buttons = [(name, _read_count(d, off)) for name, off in TELEMETRY_BUTTONS]
    return common, buttons


# ---------------------------------------------------------------------------
# the probe run
# ---------------------------------------------------------------------------


def probe_plain(p: Probe) -> dict:
    t = Table("plain feature reports")
    cal = p.get_feature(0x05, 64)
    t.add("0x05 calibration", f"{len(cal)} bytes" if cal else "FAILED")

    fw = {}
    raw20 = p.get_feature(0x20, 64)
    if raw20 and len(raw20) >= 60:
        fw = parse_firmware(raw20)
        t.add("0x20 length", f"{len(raw20)} bytes")
        t.add("build time", f"{fw['build_date']} {fw['build_time']}")
        t.add("hw info", f"0x{fw['hw_info']:08X}")
        t.add("device info", "0x" + hex_le(fw["device_info"]))
        t.add("fw type", f"0x{fw['fw_type']:04X}")
        t.add("sw series", f"0x{fw['sw_series']:04X}")
        t.add("update version", f"{update_version(fw['update_version'])}"
                                f"  (raw {fw['update_version']})")
        t.add("SBL fw version", three_part(fw["sbl_fw"]))
        t.add("main fw version", three_part(fw["main_fw"]))
        t.add("DSP fw version", dsp_version(fw["dsp_fw"]))
        t.add("MCU-DSP fw version", three_part(fw["mcu_dsp_fw"]))
    else:
        t.add("0x20", "FAILED")

    raw22 = p.get_feature(0x22, 64)
    if raw22 and len(raw22) >= 35:
        t.add("0x22 length", f"{len(raw22)} bytes")
        t.add("0x22 raw", raw22.hex(" "))
        t.add("BT patch version",
              f"0x{struct.unpack_from('<I', raw22, 31)[0]:08X}")
    else:
        t.add("0x22", f"{len(raw22) if raw22 else 0} bytes -- too short to parse")
    t.print()
    return fw


def probe_factory(p: Probe, fw: dict) -> None:
    t = Table("factory info (SET 0x80 -> GET 0x81)")

    def show(label: str, data: bytes | None, render=None) -> None:
        if data is None:
            t.add(label, "no answer (stalled)")
            return
        t.add(label, render(data) if render else data.hex(" "))

    pcba = p.call(0x01, 0x04, 6)
    show("PCBA id (legacy)", pcba, lambda b: "0x" + hex_le(b))

    pcba_full = p.call(0x01, 0x11, 24)
    show("PCBA id (full)", pcba_full,
         lambda b: sjis(b).rstrip()[::-1])

    serial = p.call(0x01, 0x13, 32)
    if serial is None:
        t.add("serial number", "no answer (stalled)")
    else:
        s = sjis(serial)
        t.add("serial number", s)
        t.add("colour", COLOR_MAP.get(s[4:6], f"unknown ({s[4:6]!r})"))
        board = s[1:2]
        t.add("board version",
              f"BDM-0{board}0" if board in "12345" else f"unknown ({board!r})")

    show("assemble parts info", p.call(0x01, 0x15, 32), lambda b: "0x" + hex_le(b))
    show("battery barcode", p.call(0x01, 0x18, 32), lambda b: sjis(b).rstrip())
    show("VCM barcode L", p.call(0x01, 0x1A, 32), lambda b: sjis(b).rstrip())
    show("VCM barcode R", p.call(0x01, 0x1C, 32), lambda b: sjis(b).rstrip())

    uid = p.call(0x01, 0x09, 9)
    if uid is None:
        t.add("MCU unique id", "no answer (stalled)")
    elif uid[0] != 0:
        t.add("MCU unique id", f"error byte 0x{uid[0]:02X}")
    else:
        t.add("MCU unique id", f"0x{struct.unpack_from('<Q', uid, 1)[0]:016X}")

    show("BD MAC address", p.call(0x09, 0x02, 6), mac)

    for side, param in (("L", 1), ("R", 2)):
        info = p.call(0x07, 0x25, 43, bytes([param]))
        if info is None:
            t.add(f"AT traceability {side}", "no answer (stalled)")
        elif info[0] != 0:
            t.add(f"AT traceability {side}", f"error byte 0x{info[0]:02X}")
        else:
            t.add(f"AT traceability {side}",
                  f"serial {info[12:19].hex().upper()}  motor {sjis(info[19:27]).rstrip()}")

    # Read-only, but the controller is documented not to answer these over
    # Bluetooth. Kept in the run precisely so the stall is on the record.
    show("touchpad UID", p.call(0x05, 0x02, 8))
    show("touchpad fw version", p.call(0x05, 0x04, 8))

    volt = p.call(0x04, 0x03, 4)
    show("battery voltage", volt,
         lambda b: f"{struct.unpack_from('<H', b, 0)[0]} mV")

    t.print()
    if fw:
        gate_new = (fw["hw_info"] & 0xFFFF) >= 777 and fw["main_fw"] >= 65655
        print(f"  (fw_type={fw['fw_type']} -> panel "
              f"{'enabled' if fw['fw_type'] in (2, 3) else 'DISABLED'}; "
              f"new-traceability branch {'on' if gate_new else 'off'})")


def probe_telemetry(p: Probe, fw: dict) -> None:
    pages = p.call_paged(0x70, 0x01, TELEMETRY_PAGES)
    print(f"\n== telemetry (device 0x70 action 1) "
          + "=" * 27)
    print(f"  pages returned: {len(pages)} of at most {TELEMETRY_PAGES}")
    if not pages:
        print("  no answer -- the controller does not expose diagnostic counters, "
              "or the 0x80 never landed")
        return
    distinct = len({bytes(pg) for pg in pages})
    print(f"  distinct pages: {distinct}"
          + ("" if distinct == len(pages) else "   <-- REPEATED PAGE, read is broken"))
    block = b"".join(pages)
    common, buttons = parse_telemetry(block)
    uv = fw.get("update_version", 0)
    if uv <= 1106:
        print(f"  update version {uv} <= 1106: the tester would show only the "
              f"active time; the full parse below may be junk")
    t = Table("telemetry counters")
    for k, v in common.items():
        t.add(k, v)
    t.print()
    t = Table("telemetry button presses")
    for name, count in buttons:
        t.add(name, count)
    t.print()


def probe_framing(p: Probe) -> None:
    """Prove which 0x80 framing the pad accepts, using one allowlisted read.

    READ_BDADR is the probe of choice: 6 bytes, single page, and its answer is
    independently checkable against the Bluetooth serial hidapi enumerated.
    """
    print("\n== 0x80 framing check " + "=" * 41)
    original = p.crc
    for label, crc in (("63 bytes + CRC32(seed 0x53) at [59:63]", True),
                       ("63 bytes, no CRC", False)):
        p.crc = crc
        ok = p.send_command(0x09, 0x02)
        answer = None
        if ok:
            deadline = time.perf_counter() + COMMAND_TIMEOUT
            status, page = p.await_page(0x09, 0x02, deadline)
            answer = None if status is None else page[:6]
        verdict = "ACCEPTED " + mac(answer) if answer else "REJECTED / no answer"
        print(f"  {label:<42} -> {verdict}")
        time.sleep(0.3)
    p.crc = original


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--transport", default="BT", choices=["BT", "USB"],
                    help="which link to probe (default BT)")
    ap.add_argument("--index", type=int, default=0,
                    help="which controller on that transport (default 0)")
    ap.add_argument("--serial", default=None,
                    help="pick by hidapi serial (the BD address on Bluetooth)")
    ap.add_argument("--raw", action="store_true",
                    help="dump every 0x80 written and every 0x81 read")
    ap.add_argument("--framing", action="store_true",
                    help="also run the CRC-on/CRC-off framing check")
    ap.add_argument("--no-crc", dest="crc", action="store_false", default=None,
                    help="force the 0x80 body to go out unsigned")
    args = ap.parse_args(argv)

    print("devices:")
    for i, d in enumerate(DEV.enumerate_devices()):
        print(f"  [{i}] {DEV.describe(d)}")

    if args.serial:
        want = args.serial.lower().replace(":", "")
        matches = [d for d in DEV.enumerate_devices()
                   if (d.serial or "").lower() == want]
        if not matches:
            print(f"\nno controller with serial {args.serial!r}")
            return 2
        info = matches[0]
    else:
        info = DEV.pick(args.transport, args.index)

    print(f"\nprobing {DEV.describe(info)}")
    dev = DEV.DualSense(info)
    # flip_extended reads feature 0x05, which is also the first thing the probe
    # wants; on USB it is a no-op.
    dev.open(flip_extended=True)
    p = Probe(dev, crc=args.crc, raw=args.raw)
    print(f"transport={info.transport}  0x80 CRC={'on' if p.crc else 'off'}  "
          f"payload={FEATURE_TEST_PAYLOAD_LEN} bytes after the report id")
    print(f"allowlisted commands: {len(TEST_COMMAND_ALLOWLIST)}")
    try:
        if args.framing:
            probe_framing(p)
        fw = probe_plain(p)
        probe_factory(p, fw)
        probe_telemetry(p, fw)
    finally:
        dev.close()
    print(f"\n0x80 writes: {p.sent}   0x81 reads: {p.reads}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
