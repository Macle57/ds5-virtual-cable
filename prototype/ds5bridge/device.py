"""hidapi wrapper for a DualSense, BT or USB, with the extended-mode handshake."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

import hid

from . import protocol as P

BT_PROFILE_GUID = "{00001124-0000-1000-8000-00805f9b34fb}"
USAGE_PAGE_GENERIC_DESKTOP = 0x0001
USAGE_GAMEPAD = 0x0005


@dataclass
class DeviceInfo:
    path: bytes
    transport: str  # "BT" | "USB"
    serial: str
    product: str

    @property
    def path_str(self) -> str:
        return self.path.decode(errors="replace")


def _classify(path: str) -> str:
    p = path.lower()
    if BT_PROFILE_GUID in p:
        return "BT"
    if "vid_054c&pid_0ce6" in p:
        return "USB"
    return "?"


def enumerate_devices() -> list[DeviceInfo]:
    out: list[DeviceInfo] = []
    for d in hid.enumerate(P.VID_SONY, P.PID_DUALSENSE):
        if d.get("usage_page") not in (0, USAGE_PAGE_GENERIC_DESKTOP):
            continue
        if d.get("usage") not in (0, USAGE_GAMEPAD):
            continue
        path = d["path"]
        out.append(
            DeviceInfo(
                path=path,
                transport=_classify(path.decode(errors="replace")),
                serial=d.get("serial_number") or "",
                product=d.get("product_string") or "",
            )
        )
    out.sort(key=lambda i: (i.transport != "USB", i.path))
    return out


def pick(transport: str | None = None, index: int = 0) -> DeviceInfo:
    devs = enumerate_devices()
    if transport:
        devs = [d for d in devs if d.transport == transport.upper()]
    if not devs:
        raise RuntimeError(
            f"No DualSense found (transport={transport!r}). "
            f"Seen: {[ (d.transport, d.serial) for d in enumerate_devices() ]}"
        )
    return devs[index]


class DualSense:
    """Open device + protocol helpers.

    On BT the controller boots in minimal report-0x01 mode; reading feature
    report 0x05 (calibration) is what flips it into extended report-0x31 mode.
    `open()` does that automatically for BT.
    """

    def __init__(self, info: DeviceInfo):
        self.info = info
        self.h = hid.device()
        self.seq = 0
        self._write_lock = threading.Lock()
        self.opened = False

    # -- lifecycle -------------------------------------------------------
    def open(self, flip_extended: bool = True) -> "DualSense":
        self.h.open_path(self.info.path)
        self.opened = True
        if self.is_bt and flip_extended:
            self.flip_to_extended()
        return self

    def close(self) -> None:
        if self.opened:
            try:
                self.h.close()
            finally:
                self.opened = False

    def __enter__(self) -> "DualSense":
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()

    @property
    def is_bt(self) -> bool:
        return self.info.transport == "BT"

    # -- feature reports (READ ONLY -- never write 0x09/firmware) --------
    def get_feature(self, report_id: int, length: int = 64) -> bytes:
        return bytes(self.h.get_feature_report(report_id, length))

    def flip_to_extended(self) -> bytes:
        """Read feature 0x05 => controller switches to extended input mode."""
        return self.get_feature(0x05, 64)

    def firmware_info(self) -> bytes:
        return self.get_feature(0x20, 64)

    # -- output ----------------------------------------------------------
    def _next_seq(self) -> int:
        s = self.seq
        self.seq = (self.seq + 1) & 0x0F
        return s

    def write_raw(self, data: bytes) -> int:
        """data[0] must be the report id (hidapi/Windows requirement)."""
        with self._write_lock:
            return self.h.write(data)

    def send_setstate(self, st: P.SetState) -> int:
        if self.is_bt:
            return self.write_raw(P.build_bt_setstate(bytes(st.body), self._next_seq()))
        return self.write_raw(P.build_usb_setstate(bytes(st.body)))

    def send_report_36(self, opus: bytes, haptic: bytes, frame_counter: int,
                       target: str = "speaker", volume: int = 0x4B) -> int:
        return self.write_raw(
            P.build_report_36(opus, haptic, self._next_seq(), frame_counter, target, volume)
        )

    def send_report_39(self, opus2: tuple[bytes, bytes], hap2: tuple[bytes, bytes],
                       packet_counter: int, target: str = "speaker",
                       mic_enabled: bool = False, audio_buffer_length: int = 0x04) -> int:
        return self.write_raw(
            P.build_report_39(opus2, hap2, self._next_seq(), packet_counter, target,
                              mic_enabled, audio_buffer_length)
        )

    def send_mic_state(self, active: bool, muted: bool = False,
                       headset_plugged: bool = False) -> int:
        return self.write_raw(
            P.build_bt_mic_state(self._next_seq(), active, muted, headset_plugged)
        )

    def send_mic_control(self, active: bool) -> int:
        return self.write_raw(P.build_bt_mic_control(self._next_seq(), active))

    # -- input -----------------------------------------------------------
    def read_raw(self, timeout_ms: int = 1000, length: int | None = None) -> bytes:
        n = length or (P.BT_INPUT_31_LEN if self.is_bt else P.USB_INPUT_01_LEN)
        return bytes(self.h.read(n, timeout_ms))

    def read_state(self, timeout_ms: int = 1000) -> P.InputState | None:
        """Read one report, return decoded state (skipping mic-audio payloads)."""
        data = self.read_raw(timeout_ms)
        if not data:
            return None
        rid, payload = data[0], data[1:]
        expected = P.BT_INPUT_31 if self.is_bt else P.USB_INPUT_01
        if rid != expected:
            return None
        return P.decode_input(payload, usb=not self.is_bt)


def describe(d: DeviceInfo) -> str:
    return f"{d.transport} serial={d.serial or '(none)'} path={d.path_str}"


def wait(seconds: float) -> None:
    """Precise-ish sleep that doesn't rely on Windows' 15.6 ms timer granularity
    for the last millisecond."""
    end = time.perf_counter() + seconds
    coarse = seconds - 0.002
    if coarse > 0:
        time.sleep(coarse)
    while time.perf_counter() < end:
        pass
