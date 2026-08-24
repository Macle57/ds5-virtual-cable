"""Finding the Bluetooth DualSense the bridge should sit behind.

The whole reason this module exists is one hardware fact that has bitten every
phase of this project:

    A DualSense charging on a USB cable STILL ENUMERATES OVER BLUETOOTH, as a
    stale entry whose feature reads fail -- and `enumerate_devices()` orders by
    path, so the dead one can sort first.

So "pick the first BT controller" is wrong, and "there is exactly one BT
controller" is false on a machine that has ever paired two. The only reliable
test of liveness is to open the device and read a feature report: the stale
entry raises, the live one answers.

Nothing here writes a feature report. Feature *writes* are how a DualSense is
re-paired (0x09) and how its firmware is touched; reads of 0x20 (firmware) and
0x05 (calibration) are the two Phase 1 proved safe.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from . import _bootstrap  # noqa: F401  (puts prototype/ on sys.path)

from ds5bridge import device as DEV
from ds5bridge import protocol as P

#: How long to wait for one input report while probing for battery. Bluetooth
#: delivers at 320-480 Hz, so anything past this is not a live link.
PROBE_READ_MS = 400

#: Warn loudly below this. A DualSense below ~15 % produces failure modes that
#: look exactly like protocol bugs (STATUS.md 16.2) -- every phase of this
#: project has lost time to it.
BATTERY_WARN_PERCENT = 20
BATTERY_CRITICAL_PERCENT = 15


@dataclass
class Candidate:
    info: DEV.DeviceInfo
    alive: bool
    firmware: str = ""
    battery_percent: int | None = None
    battery_state: str = ""
    error: str = ""

    @property
    def serial(self) -> str:
        return self.info.serial or ""

    def describe(self) -> str:
        if not self.alive:
            return f"{self.serial or '(no serial)'}  -- not responding ({self.error})"
        bat = "battery unknown"
        if self.battery_percent is not None:
            bat = f"battery {self.battery_percent}% ({self.battery_state})"
        fw = f", firmware {self.firmware}" if self.firmware else ""
        return f"{self.serial}  {bat}{fw}"


def _probe(info: DEV.DeviceInfo, want_battery: bool = True) -> Candidate:
    """Open, read firmware, optionally read one input report, close.

    Every hardware call is bounded. A controller that is absent or dead hangs a
    bare blocking read forever and looks exactly like a driver fault
    (STATUS.md 15.5 trap 5).
    """
    dev = DEV.DualSense(info)
    try:
        dev.open(flip_extended=False)
    except Exception as e:  # noqa: BLE001
        return Candidate(info, alive=False, error=str(e))
    try:
        try:
            fw = dev.firmware_info()
            firmware = bytes(fw[1:]).split(b"\x00")[0].decode("ascii", "replace").strip()
        except Exception as e:  # noqa: BLE001
            return Candidate(info, alive=False, error=str(e))

        cand = Candidate(info, alive=True, firmware=firmware)
        if want_battery and info.transport == "BT":
            try:
                # Reading feature 0x05 flips the controller into extended
                # 78-byte 0x31 reports; without it a BT unit emits the minimal
                # 0x01 report and there is no battery field to read.
                dev.flip_to_extended()
                deadline = time.monotonic() + PROBE_READ_MS / 1000.0
                while time.monotonic() < deadline:
                    st = dev.read_state(timeout_ms=100)
                    if st is not None:
                        cand.battery_percent = min(100, st.battery_level * 10)
                        cand.battery_state = st.battery_state
                        break
            except Exception as e:  # noqa: BLE001
                cand.error = str(e)
        return cand
    finally:
        try:
            dev.close()
        except Exception:  # noqa: BLE001
            pass


def probe_all(transport: str = "BT", want_battery: bool = True) -> list[Candidate]:
    """Every enumerated controller on `transport`, probed for liveness."""
    return [_probe(i, want_battery)
            for i in DEV.enumerate_devices() if i.transport == transport]


def live(transport: str = "BT", want_battery: bool = True) -> list[Candidate]:
    return [c for c in probe_all(transport, want_battery) if c.alive]


class NoControllerError(RuntimeError):
    pass


class AmbiguousControllerError(RuntimeError):
    def __init__(self, candidates: list[Candidate]):
        self.candidates = candidates
        super().__init__("more than one Bluetooth DualSense is connected")


def select(serial: str | None = None, want_battery: bool = True) -> Candidate:
    """Choose the controller to bridge.

    * `serial` given  -> that one, or an error naming what was actually seen.
    * exactly one live -> it.
    * several live     -> `AmbiguousControllerError`, for a caller that can ask.
    """
    cands = probe_all("BT", want_battery)
    if serial:
        want = serial.lower()
        for c in cands:
            if c.serial.lower() == want:
                if not c.alive:
                    raise NoControllerError(
                        f"controller {serial} is enumerated but not responding "
                        f"({c.error}). If it is charging on a USB cable its "
                        f"Bluetooth radio is off -- unplug it, or bridge a "
                        f"different one.")
                return c
        seen = ", ".join(c.serial or "?" for c in cands) or "none"
        raise NoControllerError(
            f"no Bluetooth DualSense with serial {serial}. Enumerated: {seen}")

    alive = [c for c in cands if c.alive]
    if not alive:
        if cands:
            raise NoControllerError(
                "a DualSense is paired but not responding. If it is charging on "
                "a USB cable its Bluetooth radio is off -- unplug it. Otherwise "
                "hold the PS button until the light bar pulses.")
        raise NoControllerError(
            "no DualSense found over Bluetooth. Pair it first: hold CREATE + PS "
            "until the light bar flashes, then add it in Windows Settings > "
            "Bluetooth & devices.")
    if len(alive) > 1:
        raise AmbiguousControllerError(alive)
    return alive[0]


def battery_note(percent: int | None, state: str = "") -> str:
    """A one-line battery verdict, loud below 20 %."""
    if percent is None:
        return "battery unknown"
    if state.startswith("charging"):
        return f"battery {percent}% ({state})"
    if percent <= BATTERY_CRITICAL_PERCENT:
        return (f"battery {percent}% -- CRITICAL. A DualSense this low produces "
                f"dropouts and timeouts that look exactly like software faults. "
                f"Charge it.")
    if percent <= BATTERY_WARN_PERCENT:
        return f"battery {percent}% -- LOW. Charge it soon; below 15 % this gets flaky."
    return f"battery {percent}%"


class BatteryWatcher:
    """Polls a running backend's battery and calls back when it crosses down.

    Deliberately dumb and off any hot path: it reads a decoded field the
    backend already keeps, once every `interval` seconds, from its own thread.
    """

    def __init__(self, backend, on_report, interval: float = 60.0,
                 on_warn=None):
        self.backend = backend
        self.on_report = on_report
        self.on_warn = on_warn
        self.interval = interval
        self._stop = threading.Event()
        self._t: threading.Thread | None = None
        self._warned_at: int | None = None

    def start(self) -> None:
        self._t = threading.Thread(target=self._loop, name="ds5-battery", daemon=True)
        self._t.start()

    def stop(self) -> None:
        self._stop.set()
        if self._t is not None:
            self._t.join(timeout=2.0)

    def _loop(self) -> None:
        # First reading after a short settle, then on the interval.
        if self._stop.wait(5.0):
            return
        while True:
            try:
                st = self.backend.device_status()
            except Exception:  # noqa: BLE001
                st = None
            if st and st.get("battery_percent") is not None:
                pct = st["battery_percent"]
                self.on_report(pct, st.get("battery_state", ""))
                if (not str(st.get("battery_state", "")).startswith("charging")
                        and pct <= BATTERY_WARN_PERCENT
                        and (self._warned_at is None or pct < self._warned_at)):
                    self._warned_at = pct
                    if self.on_warn:
                        self.on_warn(pct, st.get("battery_state", ""))
            if self._stop.wait(self.interval):
                return
