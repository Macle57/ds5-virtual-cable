"""CRC32 for DualSense Bluetooth reports.

Standard CRC-32 (zlib / IEEE 802.3, poly 0xEDB88320 reflected), computed over a
seed prefix byte + report id + payload, stored little-endian in the last 4 bytes.

Port of ../dualsense-tester/src/utils/dualsense/crc32.util.ts.

Seeds:
    0xA2 -> host->device OUTPUT reports (0x31, 0x32, 0x36, 0x39)
    0x53 -> host->device SET FEATURE reports
    0xA3 -> device->host GET FEATURE reports (community docs; not used by tester)
"""

from __future__ import annotations

import zlib

SEED_OUTPUT = 0xA2
SEED_SET_FEATURE = 0x53
SEED_GET_FEATURE = 0xA3


def crc32_seeded(seed: int, report_id: int, payload: bytes) -> int:
    """CRC32 of  bytes([seed, report_id]) + payload."""
    return zlib.crc32(bytes((seed, report_id)) + payload) & 0xFFFFFFFF


def fill_output_checksum(report_id: int, payload: bytearray) -> bytearray:
    """Write the CRC32 into the last 4 bytes of `payload` (an output report body,
    report id NOT included in the buffer)."""
    crc = crc32_seeded(SEED_OUTPUT, report_id, bytes(payload[:-4]))
    payload[-4:] = crc.to_bytes(4, "little")
    return payload


def fill_feature_checksum(report_id: int, payload: bytearray) -> bytearray:
    crc = crc32_seeded(SEED_SET_FEATURE, report_id, bytes(payload[:-4]))
    payload[-4:] = crc.to_bytes(4, "little")
    return payload


def verify_input_checksum(report_id: int, full_report_without_id: bytes) -> bool:
    """Input reports from the controller carry a CRC32 in their last 4 bytes,
    seeded with 0xA1 (input) per community docs. Returns True if it matches."""
    for seed in (0xA1, 0xA2, 0xA3, 0x53):
        crc = crc32_seeded(seed, report_id, full_report_without_id[:-4])
        if crc.to_bytes(4, "little") == full_report_without_id[-4:]:
            return True
    return False
