"""Live input telemetry: bridge process -> dashboard, over localhost UDP.

The dashboard (`dashboard.py`) wants to draw the controller moving at 30-60 Hz.
The data it needs already exists in every bridge process -- `BridgeBackend`
keeps the freshest translated USB 0x01 input report at all times -- but the
bridge and the dashboard are in DIFFERENT processes: `manager.py` runs one
child per controller, on purpose, and the only channel between them today is
the child's stdout, which is a line-per-2-seconds status protocol that must
never be asked to carry 60 Hz of binary.

So this module is that missing channel, and it is UDP datagrams to
127.0.0.1 by deliberate choice:

* **It can never block the hot path.** A UDP `sendto` on loopback either
  completes immediately or drops; there is no connection to make, no peer to
  wait for, no backpressure. The publisher samples the backend's existing
  `latest_input_report()` -- a lock plus a memcpy, the exact same peek the USB
  control path uses -- so the 250 Hz interrupt pipeline never learns that
  telemetry exists. Measured overhead budget: ~60 datagrams/s of ~300 bytes,
  which is noise next to the ~476 Hz Bluetooth reader.
* **Nobody has to be alive.** The child publishes whether or not a dashboard
  is listening (dropped datagrams cost nothing), and the dashboard serves
  whether or not any bridge is up (it just has nothing fresh to show). Either
  side can restart freely -- there is no session to re-establish, which
  matters in a system where children are torn down and respawned by hotplug.
* **Loss is the correct failure mode.** Every datagram carries the complete
  current state, so a lost one is obsoleted by the next 16 ms later. A stream
  protocol would instead queue stale frames behind a stalled reader.

The datagram is JSON with the raw 64-byte report hex-encoded inside it.
Decoding to fields happens on the RECEIVING side (`decode_report`), so the
publisher stays a memcpy + hex + sendto, and the one decoder both ends agree
on is Phase 1's `ds5bridge.protocol.decode_input` -- the same code the parity
tests trust, not a re-implementation.

Everything here is standard library only (`ds5bridge.protocol` itself is
struct + zlib), so the CI's stdlib-only constraint holds and the dashboard
works on a machine where the hardware stack is broken.
"""

from __future__ import annotations

import json
import logging
import socket
import threading
import time

from . import _bootstrap  # noqa: F401  (sys.path side effect)

from ds5bridge import protocol as P

log = logging.getLogger("ds5app.telemetry")

#: Datagram schema version. Bumped only if the JSON shape changes
#: incompatibly; a receiver drops versions it does not know.
TELEMETRY_VERSION = 1

#: Where telemetry may go and come from. Never anything but loopback: this is
#: private input state (every button the user presses), and it stays on the
#: machine.
LOOPBACK = "127.0.0.1"

#: Sampling rate. 60 Hz is the ceiling of what a browser usefully renders and
#: an eighth of the Bluetooth report rate, so nothing perceptible is lost.
DEFAULT_HZ = 60.0

#: A datagram is sent when the report CHANGED, or this often regardless, so an
#: untouched controller still refreshes its battery/connection state and a
#: freshly opened dashboard is never more than half a second from first paint.
KEEPALIVE_S = 0.5

#: Largest datagram the hub will read. A v1 datagram is ~300 bytes; anything
#: bigger is not ours.
MAX_DATAGRAM = 2048

#: An entry older than this stops being served by `TelemetryHub.latest()`:
#: with the publisher's keepalive at 0.5 s, five seconds of silence means the
#: bridge process is gone, not busy.
DEFAULT_MAX_AGE_S = 5.0


# ---------------------------------------------------------------------------
# decoding -- the receiver's half, shared with tests and the fake feed
# ---------------------------------------------------------------------------


def decode_report(report: bytes) -> dict | None:
    """A 64-byte USB 0x01 input report -> a JSON-able dict, or None.

    One call into Phase 1's `decode_input` and a reshaping into plain types.
    Field names are the wire contract with `dashboard.html`; anything added
    here must be additive, because an old page may be open against a new
    server across a refresh.

    Sticks and triggers stay RAW (0..255) on purpose: the page normalises for
    display, and raw bytes are what a person debugging a drifting stick needs
    to see.
    """
    if not report or report[0] != P.USB_INPUT_01:
        return None
    st = P.decode_input(report[1:], usb=True)
    if st is None:
        return None
    return {
        "lx": st.lx, "ly": st.ly, "rx": st.rx, "ry": st.ry,
        "l2": st.l2, "r2": st.r2,
        "seq": st.seq,
        "dpad": st.dpad,
        "buttons": dict(st.buttons),
        "touch": [{"active": t.active, "id": t.id, "x": t.x, "y": t.y}
                  for t in st.touch],
        "gyro": list(st.gyro),
        "accel": list(st.accel),
        "battery_percent": min(100, st.battery_level * 10),
        "battery_state": st.battery_state,
        "headphone": st.headphone,
        "mic": st.mic,
        "mic_muted": st.mic_muted,
    }


# ---------------------------------------------------------------------------
# building reports -- for tests and the dashboard's fake feed, never for
# production traffic (production reports come off a real controller)
# ---------------------------------------------------------------------------

#: decode_input's button names -> (key byte index 0..2, bit mask). The exact
#: inverse of the table in `ds5bridge.protocol.decode_input`.
_BUTTON_BITS = {
    "sq": (0, 0x10), "x": (0, 0x20), "o": (0, 0x40), "tri": (0, 0x80),
    "L1": (1, 0x01), "R1": (1, 0x02), "L2": (1, 0x04), "R2": (1, 0x08),
    "create": (1, 0x10), "options": (1, 0x20), "L3": (1, 0x40), "R3": (1, 0x80),
    "PS": (2, 0x01), "touchpad": (2, 0x02), "mute": (2, 0x04),
}

_BATTERY_NIBBLE = {v: k for k, v in P.BATTERY_STATE.items()}


def build_usb01(lx: int = 0x80, ly: int = 0x80, rx: int = 0x80, ry: int = 0x80,
                l2: int = 0, r2: int = 0, dpad: str = "-",
                buttons=(), touch=(), gyro=(0, 0, 0), accel=(0, 0, 0),
                battery_level: int = 8, battery_state: str = "discharging",
                headphone: bool = False, mic: bool = False,
                mic_muted: bool = False, seq: int = 0) -> bytes:
    """A synthetic but structurally correct 64-byte USB 0x01 input report.

    `decode_report(build_usb01(...))` round-trips every argument, which is
    what the unit tests assert and what makes the fake feed honest: the
    dashboard's development diet is the same byte layout the real bridge
    publishes, through the same decoder.

    `touch` is up to two (x, y) pairs (0..1919, 0..1079); `buttons` is an
    iterable of the names `decode_input` uses ("x", "sq", "L1", "PS", ...).
    """
    o = P.OFFSETS_USB
    body = bytearray(63)
    body[o.stick_lx], body[o.stick_ly] = lx & 0xFF, ly & 0xFF
    body[o.stick_rx], body[o.stick_ry] = rx & 0xFF, ry & 0xFF
    body[o.trigger_l], body[o.trigger_r] = l2 & 0xFF, r2 & 0xFF
    body[o.sequence_num] = seq & 0xFF

    keys = [P.DPAD_NAMES.index(dpad) if dpad in P.DPAD_NAMES else 8, 0, 0]
    for name in buttons:
        idx, mask = _BUTTON_BITS[name]
        keys[idx] |= mask
    body[o.digital_keys:o.digital_keys + 3] = keys

    for i, (g, a) in enumerate(zip(gyro, accel)):
        body[o.gyro_pitch + 2 * i:o.gyro_pitch + 2 * i + 2] = \
            int(g).to_bytes(2, "little", signed=True)
        body[o.accel_x + 2 * i:o.accel_x + 2 * i + 2] = \
            int(a).to_bytes(2, "little", signed=True)

    # Touch: bit 7 of the id byte SET means "no finger". Both slots start
    # inactive; each provided point activates one with ids 0 and 1.
    body[o.touch_data] = 0x80
    body[o.touch_data + 4] = 0x80
    for i, pt in enumerate(list(touch)[:2]):
        if pt is None:
            continue
        x, y = int(pt[0]), int(pt[1])
        base = o.touch_data + 4 * i
        body[base] = i & 0x7F
        body[base + 1] = x & 0xFF
        body[base + 2] = ((x >> 8) & 0x0F) | ((y & 0x0F) << 4)
        body[base + 3] = (y >> 4) & 0xFF

    state = _BATTERY_NIBBLE.get(battery_state, 0x00)
    body[o.status0] = ((state & 0x0F) << 4) | (battery_level & 0x0F)
    body[o.status1] = ((0x01 if headphone else 0) | (0x02 if mic else 0)
                       | (0x04 if mic_muted else 0))
    return bytes([P.USB_INPUT_01]) + bytes(body)


# ---------------------------------------------------------------------------
# the publisher -- lives in the bridge process
# ---------------------------------------------------------------------------


class TelemetryPublisher:
    """Samples a `BridgeBackend` and fires UDP datagrams at the dashboard.

    One daemon thread, started by `BridgeService` when it was given a
    `telemetry_port`. Everything it touches on the backend is a documented
    read path: `latest_input_report()` (the non-consuming peek the USB control
    path uses -- it does NOT advance the delivered-serial bookkeeping, so
    telemetry can never eat a report the game was owed), `device_status()`
    (explicitly built to be called off the hot path) and the `stats` counters
    (plain dict reads).

    Never raises out of its thread, and failure to send is failure to send --
    the bridge's job is bridging, and telemetry is strictly a passenger.
    """

    def __init__(self, backend, port: int, serial: str = "",
                 host: str = LOOPBACK, hz: float = DEFAULT_HZ):
        self.backend = backend
        self.addr = (host, int(port))
        self.serial = (serial or "").lower()
        self.interval = 1.0 / max(1.0, float(hz))
        self._stop = threading.Event()
        self._t: threading.Thread | None = None
        self._sock: socket.socket | None = None
        self._last_sent: bytes | None = None
        self._last_sent_at = 0.0
        #: (perf_counter, input_delivered) for the rate differencer -- the same
        #: trick `BridgeService.snapshot()` uses, for the same reason: O(1).
        self._rate_prev: tuple[float, int] | None = None
        self._rate_hz = 0.0
        self.sent = 0          # observability, read by tests

    def start(self) -> None:
        if self._t is not None:
            return
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._t = threading.Thread(target=self._loop, name="ds5-telemetry",
                                   daemon=True)
        self._t.start()

    def stop(self) -> None:
        self._stop.set()
        t, self._t = self._t, None
        if t is not None:
            t.join(timeout=2.0)
        s, self._sock = self._sock, None
        if s is not None:
            try:
                s.close()
            except OSError:
                pass

    # -- internals ---------------------------------------------------------

    def _rate(self) -> float:
        """Delivered-reports/s, differenced over at least 0.5 s."""
        try:
            n = int(self.backend.stats.get("input_delivered", 0))
        except Exception:  # noqa: BLE001
            return self._rate_hz
        now = time.perf_counter()
        if self._rate_prev is None:
            self._rate_prev = (now, n)
            return 0.0
        dt = now - self._rate_prev[0]
        if dt >= 0.5:
            self._rate_hz = (n - self._rate_prev[1]) / dt
            self._rate_prev = (now, n)
        return self._rate_hz

    def _tick(self) -> None:
        be = self.backend
        report = be.latest_input_report(64)
        now = time.monotonic()
        due = now - self._last_sent_at >= KEEPALIVE_S
        if report == self._last_sent and not due:
            return
        try:
            status = be.device_status()
        except Exception:  # noqa: BLE001
            status = {}
        msg = {
            "v": TELEMETRY_VERSION,
            "serial": self.serial or (status.get("serial") or ""),
            "report": report.hex() if report else None,
            "connected": bool(status.get("connected")),
            "stale_s": status.get("stale_s"),
            "rps": round(self._rate(), 1),
            "t": time.time(),
        }
        try:
            self._sock.sendto(json.dumps(msg, separators=(",", ":")).encode(),
                              self.addr)
            self.sent += 1
            self._last_sent = report
            self._last_sent_at = now
        except OSError as e:
            # Nobody listening (WSAECONNRESET on loopback), or the socket is
            # on its way down. Both are normal; neither is the bridge's
            # problem.
            log.debug("telemetry send failed: %s", e)

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self._tick()
            except Exception:  # noqa: BLE001
                # One bad tick must not end the thread -- and must not spin.
                log.debug("telemetry tick failed", exc_info=True)


# ---------------------------------------------------------------------------
# the hub -- lives in the dashboard's process
# ---------------------------------------------------------------------------


class TelemetryHub:
    """Receives datagrams from every bridge and keeps only the newest state.

    Bound to an EPHEMERAL loopback port by default (`port=0`): the process
    that owns the hub tells each child where to send via `--telemetry-port`,
    so no config field, no port collision with a second instance, and no
    firewall prompt (loopback binds do not trigger one).

    `latest()` is a dict copy under a lock -- cheap enough for the SSE loop to
    call 30 times a second, in keeping with the project rule that read paths
    for UIs never do work (`BridgeService.snapshot()` explains why).

    Trust model: anything on this machine can send to the port. That is the
    same trust localhost HTTP extends, and the worst a forged datagram can do
    is draw wrong buttons on a page; still, garbage is dropped by structure
    (version, types, decode) rather than crashing the reader.
    """

    def __init__(self, host: str = LOOPBACK, port: int = 0):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.bind((host, port))
        self.port = self._sock.getsockname()[1]
        self._lock = threading.Lock()
        self._latest: dict[str, dict] = {}
        self._t: threading.Thread | None = None
        self._closed = False
        self.received = 0      # observability, read by tests
        self.dropped = 0

    def start(self) -> None:
        if self._t is not None:
            return
        self._t = threading.Thread(target=self._loop, name="ds5-telemetry-hub",
                                   daemon=True)
        self._t.start()

    def stop(self) -> None:
        self._closed = True
        try:
            self._sock.close()   # unblocks recvfrom with an OSError
        except OSError:
            pass
        t, self._t = self._t, None
        if t is not None:
            t.join(timeout=2.0)

    def latest(self, max_age: float = DEFAULT_MAX_AGE_S) -> dict[str, dict]:
        """{serial: entry} for every controller heard from within `max_age` s.

        Each entry: {"decoded": <decode_report dict or None>, "connected",
        "stale_s", "rps", "t", "age_s"}. The raw report bytes are NOT kept
        past decoding -- nothing downstream re-parses them.
        """
        now = time.monotonic()
        out = {}
        with self._lock:
            for serial, (at, entry) in self._latest.items():
                age = now - at
                if age <= max_age:
                    e = dict(entry)
                    e["age_s"] = round(age, 3)
                    out[serial] = e
        return out

    def _ingest(self, data: bytes) -> None:
        try:
            msg = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            self.dropped += 1
            return
        if not isinstance(msg, dict) or msg.get("v") != TELEMETRY_VERSION:
            self.dropped += 1
            return
        serial = msg.get("serial")
        if not isinstance(serial, str) or not serial:
            self.dropped += 1
            return
        decoded = None
        rep = msg.get("report")
        if isinstance(rep, str):
            try:
                decoded = decode_report(bytes.fromhex(rep))
            except ValueError:
                self.dropped += 1
                return
        entry = {
            "decoded": decoded,
            "connected": bool(msg.get("connected")),
            "stale_s": msg.get("stale_s"),
            "rps": msg.get("rps") or 0.0,
            "t": msg.get("t"),
        }
        with self._lock:
            self._latest[serial.lower()] = (time.monotonic(), entry)
        self.received += 1

    def _loop(self) -> None:
        while True:
            try:
                data, addr = self._sock.recvfrom(MAX_DATAGRAM)
            except OSError:
                if self._closed:
                    return
                continue
            # Only loopback speaks here. The bind already guarantees delivery
            # is local; this guards the source too, cheaply.
            if addr[0] != LOOPBACK:
                self.dropped += 1
                continue
            try:
                self._ingest(data)
            except Exception:  # noqa: BLE001
                self.dropped += 1
                log.debug("telemetry ingest failed", exc_info=True)
