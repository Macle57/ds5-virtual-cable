"""The local web dashboard: a live controller you can look at, in a browser.

    http://127.0.0.1:8765     (config.dashboard_port)

One page, served by this process, showing every bridged DualSense moving in
real time -- buttons lighting, sticks travelling, triggers filling, touchpad
touches where the fingers are -- plus battery, link state and report rate, and
a settings panel that reads and writes the same config.json everything else
uses. In the spirit of daidr's dualsense-tester (see docs/provenance.md), but
fed by OUR bridge's telemetry rather than WebHID, so it also works while the
pad is cloaked by HidHide and shows exactly what the games are being fed.

Why SSE and not WebSocket
-------------------------
The browser needs a one-way stream of small JSON states at 30 Hz. Server-Sent
Events do precisely that over plain HTTP: no handshake to implement, no frame
masking, no `Upgrade` negotiation -- `http.server` can serve it with a loop
and a `flush()`, and `EventSource` in the page reconnects by itself when this
process restarts. A WebSocket buys bidirectionality this page does not need
(config writes are ordinary POSTs) at the cost of hand-rolling RFC 6455 in
stdlib. At 30 Hz x ~2 KB on loopback, transport efficiency is not a factor.

Why the server must never matter
--------------------------------
This is a diagnostic window onto a system whose actual job is holding 250 Hz
input and 1 ms isochronous audio deadlines in OTHER processes. So the rules:
localhost binding only, always; every request handler catches its own
failures; `snapshot_fn` and `TelemetryHub.latest()` are both O(1) reads by
their own contracts; and every caller that starts a `DashboardServer` treats
failure to start as a log line, never as a failed bridge.

Trust model: whatever can make an HTTP request to 127.0.0.1 is the user (the
same trust every localhost devtool extends). Two cheap hardenings anyway,
because they cost nothing: the Host header must be a loopback name -- which
defeats DNS-rebinding, the one way a web page COULD reach this server -- and
config POSTs must be `application/json`, which keeps cross-site HTML forms
(which cannot set that header) out of the config file.

Development without hardware:  python -m ds5app.dashboard --fake
serves the same page against a synthetic controller feed -- the full pipeline
(build report -> UDP datagram -> hub -> decode -> SSE) with everything real
except the pad. `--fake static` freezes a known state for screenshot tests.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import socket
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import _bootstrap  # noqa: F401  (sys.path side effect)
from . import actions as ACT
from . import config as K
from . import telemetry as TM

log = logging.getLogger("ds5app.dashboard")

#: The page, as one self-contained file next to this module (inline CSS/JS/
#: fonts, no external resources -- nothing to fetch, nothing to leak to).
#: It is BUILT, not hand-edited: the source is the Vite/React project in
#: app/dashboard-ui, and `npm run build` there writes this file. Shipped as
#: a data file by app/packaging/ds5bridge.spec.
PAGE_NAME = "dashboard.html"

#: SSE frame rate. 30 Hz is indistinguishable from 60 for a status page and
#: halves the JSON encoding; the TELEMETRY samples underneath arrive at 60.
STREAM_HZ = 30.0

#: Config POST bodies larger than this are refused. The real config is ~2 KB.
MAX_CONFIG_BYTES = 1 << 20

#: Hostnames a request may carry. Anything else is some OTHER site's DNS name
#: resolving to us -- the rebinding trick -- and is refused.
_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "[::1]")


def _host_ok(value: str | None) -> bool:
    host = (value or "").strip().lower()
    if host.startswith("["):
        # Bracketed IPv6: the name ends at "]", whatever follows is the port.
        host = host.split("]", 1)[0] + "]"
    else:
        host = host.rsplit(":", 1)[0]
    return host in _LOOPBACK_HOSTS


def deep_merge(base: dict, updates: dict) -> dict:
    """`updates` folded onto `base`: dicts merge recursively, all else replaces.

    This is what makes the settings panel forward-compatible: a page that
    POSTs only `{"input": {"off_timer_minutes": 30}}` changes that one key and
    nothing else, whether or not this build knows what an `input` section is
    (an unknown section rides through `Config.extra` untouched).
    """
    out = dict(base)
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


# ---------------------------------------------------------------------------
# the server
# ---------------------------------------------------------------------------


class DashboardServer:
    """Serves the page, the SSE stream and the config API. Localhost only.

    `hub` is a started `telemetry.TelemetryHub` (live input state, or None for
    a config-only dashboard); `snapshot_fn` is `BridgeManager.snapshot` or any
    callable with that shape (or None). Both are optional so the pieces can be
    tested apart and so `--fake` can substitute either.

    `on_config_saved(cfg)` is called after every successful POST with the
    freshly saved `Config`, so a host holding a live config object (the tray)
    can fall in line instead of clobbering the change at its next save.
    """

    def __init__(self, hub=None, snapshot_fn=None,
                 port: int = K.DEFAULT_DASHBOARD_PORT, host: str = "127.0.0.1",
                 config_path: str | None = None, on_config_saved=None):
        self.hub = hub
        self.snapshot_fn = snapshot_fn
        self.host = host
        self.port = int(port)
        self.config_path = config_path
        self.on_config_saved = on_config_saved
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._stopping = threading.Event()
        self._page_cache: tuple[float, bytes] | None = None
        #: Labels come from the config file; reading it 30 times a second for
        #: the stream would be silly, so it is cached and refreshed on a TTL
        #: (and immediately by any successful POST).
        self._cfg_cache: tuple[float, "K.Config"] | None = None
        self._cfg_lock = threading.Lock()
        #: /api/actions is static per process (the registry and the config
        #: vocabulary are code, not state) -- built once, served forever.
        self._actions_meta: dict | None = None

    # -- lifecycle ---------------------------------------------------------

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/"

    def start(self) -> None:
        """Bind and serve. Raises OSError if the port is taken -- the caller
        decides whether that is fatal (tests) or a warning (the tray)."""
        if self._httpd is not None:
            return
        server = self

        class Handler(_Handler):
            dash = server

        httpd = ThreadingHTTPServer((self.host, self.port), Handler)
        # Every connection gets its own daemon thread; an SSE stream held open
        # by a browser must never block process exit.
        httpd.daemon_threads = True
        self.port = httpd.server_address[1]      # resolves port=0 for tests
        self._httpd = httpd
        self._stopping.clear()
        self._thread = threading.Thread(target=httpd.serve_forever,
                                        name="ds5-dashboard", daemon=True)
        self._thread.start()
        log.info("dashboard serving at %s", self.url)

    def stop(self) -> None:
        self._stopping.set()                     # ends every SSE loop
        httpd, self._httpd = self._httpd, None
        if httpd is not None:
            try:
                httpd.shutdown()
                httpd.server_close()
            except Exception:  # noqa: BLE001
                pass
        t, self._thread = self._thread, None
        if t is not None:
            t.join(timeout=3.0)

    # -- the state the page renders -----------------------------------------

    def _config(self, fresh: bool = False) -> "K.Config":
        with self._cfg_lock:
            if (not fresh and self._cfg_cache is not None
                    and time.monotonic() - self._cfg_cache[0] < 5.0):
                return self._cfg_cache[1]
            cfg = K.load(self.config_path)
            self._cfg_cache = (time.monotonic(), cfg)
            return cfg

    def state(self) -> dict:
        """Everything the page shows, merged from the two sources.

        The manager snapshot is the authority on lifecycle (state, enabled,
        port, presence); telemetry is the authority on what the pad is DOING.
        A controller known to either appears; the page renders whatever half
        it gets, so a telemetry-only run (--fake) and a manager-only run (a
        bridge with no --telemetry-port) both draw something honest.
        """
        controllers: dict[str, dict] = {}
        aggregate: dict = {}
        if self.snapshot_fn is not None:
            try:
                snap = self.snapshot_fn()
                aggregate = snap.get("aggregate", {})
                aggregate["master_enabled"] = snap.get("master_enabled", True)
                for serial, c in snap.get("controllers", {}).items():
                    controllers[serial.lower()] = dict(c)
            except Exception:  # noqa: BLE001
                log.debug("snapshot_fn failed", exc_info=True)
        if self.hub is not None:
            try:
                for serial, entry in self.hub.latest().items():
                    controllers.setdefault(serial, {"serial": serial})
                    controllers[serial]["telemetry"] = entry
            except Exception:  # noqa: BLE001
                log.debug("hub.latest failed", exc_info=True)
        try:
            cfg = self._config()
            for serial, c in controllers.items():
                if serial in cfg.controllers:
                    c.setdefault("label", cfg.controllers[serial].label)
        except Exception:  # noqa: BLE001
            log.debug("label lookup failed", exc_info=True)
        return {"ts": time.time(), "controllers": controllers,
                "aggregate": aggregate}

    # -- config API --------------------------------------------------------

    def config_get(self) -> dict:
        cfg = self._config(fresh=True)
        return {"path": cfg.path or K.config_path(), "config": cfg.to_dict()}

    def config_post(self, updates: dict) -> dict:
        """Merge `updates` onto the CURRENT file and save atomically.

        Read-merge-write against the file rather than against any in-memory
        copy, so a toggle the tray saved a second ago is not resurrected by a
        page that loaded before it. `Config.from_dict` then coerces: a bad
        value degrades to its default (the config module's standing contract)
        instead of poisoning the file, and unknown keys ride through `extra`.
        Raises `K.ConfigWriteError` -- the caller turns that into a 500 with
        the message, which is user-facing text by that error's own contract.
        """
        current = K.load(self.config_path)
        merged = deep_merge(current.to_dict(), updates)
        cfg = K.Config.from_dict(merged)
        cfg.path = current.path
        K.save(cfg, self.config_path)
        with self._cfg_lock:
            self._cfg_cache = (time.monotonic(), cfg)
        if self.on_config_saved is not None:
            try:
                self.on_config_saved(cfg)
            except Exception:  # noqa: BLE001
                log.exception("on_config_saved handler failed")
        return {"path": cfg.path or K.config_path(), "config": cfg.to_dict()}

    def actions_meta(self) -> dict:
        """What the settings page needs to build its chord/gesture pickers.

        Sourced from the SAME registry and constants the engine resolves
        against (`actions.OsActions().registry()`, `config.CHORD_BUTTONS`
        et al.), never hardcoded in the page -- an action added to the
        registry appears in the dropdowns with no HTML change. Constructing
        `OsActions` is cheap and side-effect-free by its own design (the
        injector seams default to plain function references); nothing is
        injected by merely building the registry.

        `chord_keys` is the button vocabulary a chord binding may use --
        today identical to `chord_buttons` (any chordable button can also BE
        the chord button), but served separately so the two can diverge
        without a page change. `remote_keys` is the same for the remote-mode
        table (`input.remote.chords`, defaults in `defaults.remote_chords`);
        both tables also accept `gesture_keys`.
        """
        if self._actions_meta is None:
            registry = ACT.OsActions().registry()
            self._actions_meta = {
                # `show_battery` is an engine action (no OS side), but the
                # dashboard lists it among the ordinary picks, so it is
                # served here (sorted in) as well as in `engine_actions`.
                "actions": sorted(
                    [{"name": spec.name, "doc": spec.doc,
                      "repeatable": bool(spec.repeatable)}
                     for spec in registry.values()]
                    + [{"name": "show_battery",
                        "doc": K.ENGINE_ACTIONS["show_battery"],
                        "repeatable": False, "engine": True}],
                    key=lambda a: a["name"]),
                "chord_buttons": list(K.CHORD_BUTTONS),
                "chord_keys": list(K.CHORD_BUTTONS),
                # what the remote-mode table binds: every button but the
                # (default) arming one -- the chord button is never a remote
                # binding -- plus the three gestures, in one list the page
                # renders verbatim
                "remote_keys": ([b for b in K.CHORD_BUTTONS if b != "ps"]
                                + list(K.CHORD_GESTURES)),
                "gesture_keys": list(K.CHORD_GESTURES),
                # the Gestures tab: ordered {key, label, help, group}
                "gestures": K.gesture_meta(),
                # the key vocabulary a user macro may use, engine order
                "macro_keys": list(ACT.KEY_NAMES),
                # the engine's own actions (pad power / lightbar), which the
                # OS registry deliberately does not contain
                "engine_actions": [{"name": n, "doc": d}
                                   for n, d in K.ENGINE_ACTIONS.items()],
                "defaults": {"chords": dict(K.DEFAULT_CHORDS),
                             "remote_chords": dict(K.DEFAULT_REMOTE_CHORDS)},
            }
        return self._actions_meta

    # -- the page ----------------------------------------------------------

    def page(self) -> bytes:
        """dashboard.html, cached against its mtime (free live-reload in dev)."""
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            PAGE_NAME)
        mtime = os.path.getmtime(path)
        if self._page_cache is None or self._page_cache[0] != mtime:
            with open(path, "rb") as f:
                self._page_cache = (mtime, f.read())
        return self._page_cache[1]


# ---------------------------------------------------------------------------
# request handling
# ---------------------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    #: Set by DashboardServer.start() on a per-server subclass.
    dash: DashboardServer = None  # type: ignore[assignment]
    protocol_version = "HTTP/1.1"

    # http.server logs every request to stderr by default; a 30 Hz SSE client
    # plus a polling tab would turn the console into noise.
    def log_message(self, fmt, *args):  # noqa: A003
        log.debug("%s %s", self.address_string(), fmt % args)

    def _refuse(self, code: HTTPStatus, text: str) -> None:
        body = (text + "\n").encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, obj, code: int = 200) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _guard(self) -> bool:
        if not _host_ok(self.headers.get("Host")):
            self._refuse(HTTPStatus.FORBIDDEN,
                         "this dashboard answers loopback names only")
            return False
        return True

    # -- GET ---------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 (http.server's contract)
        try:
            if not self._guard():
                return
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                body = self.dash.page()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            elif path == "/api/state":
                self._send_json(self.dash.state())
            elif path == "/api/config":
                self._send_json(self.dash.config_get())
            elif path == "/api/actions":
                self._send_json(self.dash.actions_meta())
            elif path == "/api/stream":
                self._stream()
            else:
                self._refuse(HTTPStatus.NOT_FOUND, "not found")
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass                     # the browser went away; entirely normal
        except Exception:  # noqa: BLE001
            log.exception("GET %s failed", self.path)
            try:
                self._refuse(HTTPStatus.INTERNAL_SERVER_ERROR, "server error")
            except OSError:
                pass

    def _stream(self) -> None:
        """The SSE loop: the full state, 30 times a second, until either end
        hangs up. Each frame is complete (never a delta), so a dropped or
        late frame costs nothing and a fresh tab is current at its first one.
        """
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        # This response never ends, so opt out of keep-alive bookkeeping.
        self.send_header("Connection", "close")
        self.end_headers()
        interval = 1.0 / STREAM_HZ
        stopping = self.dash._stopping
        while not stopping.wait(interval):
            payload = json.dumps(self.dash.state())
            self.wfile.write(b"data: " + payload.encode() + b"\n\n")
            self.wfile.flush()

    # -- POST --------------------------------------------------------------

    def do_POST(self) -> None:  # noqa: N802
        try:
            if not self._guard():
                return
            if self.path.split("?", 1)[0] != "/api/config":
                self._refuse(HTTPStatus.NOT_FOUND, "not found")
                return
            ctype = (self.headers.get("Content-Type") or "").split(";")[0]
            if ctype.strip().lower() != "application/json":
                self._refuse(HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                             "config writes must be application/json")
                return
            length = int(self.headers.get("Content-Length") or 0)
            if not 0 < length <= MAX_CONFIG_BYTES:
                self._refuse(HTTPStatus.BAD_REQUEST, "bad content length")
                return
            try:
                updates = json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                self._refuse(HTTPStatus.BAD_REQUEST, "body is not valid JSON")
                return
            if not isinstance(updates, dict):
                self._refuse(HTTPStatus.BAD_REQUEST,
                             "body must be a JSON object of settings")
                return
            try:
                self._send_json(self.dash.config_post(updates))
            except K.ConfigWriteError as e:
                # Its message is user-facing text by contract; show it.
                self._send_json({"error": str(e)}, code=500)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception:  # noqa: BLE001
            log.exception("POST %s failed", self.path)
            try:
                self._refuse(HTTPStatus.INTERNAL_SERVER_ERROR, "server error")
            except OSError:
                pass


# ---------------------------------------------------------------------------
# the fake feed -- development and screenshot tests, no hardware, no driver
# ---------------------------------------------------------------------------


class FakeFeed:
    """Publishes synthetic-but-valid telemetry datagrams at a hub.

    Exercises the REAL pipeline -- `telemetry.build_usb01` (the byte layout the
    tests verify against Phase 1's decoder) -> a real UDP datagram -> the real
    hub -> the real SSE stream -- so what the page renders under `--fake` is
    what it will render against hardware, minus the hardware.

    `static` holds one fixed, documented state so a screenshot has a ground
    truth to be compared against; `animate` sweeps everything for eyeballing.
    """

    #: The frozen state `--fake static` serves, and therefore the ground truth
    #: the screenshot test asserts: cross + R1 + L3 held, d-pad west, right
    #: trigger deep, left stick pushed hard right, one touch left of centre.
    STATIC = dict(buttons=("x", "R1", "L3"), dpad="W", lx=0xF0, ly=0x80,
                  rx=0x80, ry=0x40, l2=0, r2=192, touch=((600, 300),),
                  battery_level=7, gyro=(120, -40, 15), accel=(0, 8100, 0))

    def __init__(self, port: int, serial: str, mode: str = "animate",
                 phase: float = 0.0, hz: float = 60.0):
        self.addr = (TM.LOOPBACK, int(port))
        self.serial = serial
        self.mode = mode
        self.phase = phase
        self.interval = 1.0 / hz
        self._stop = threading.Event()
        self._t: threading.Thread | None = None
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def start(self) -> None:
        self._t = threading.Thread(target=self._loop, name=f"fake-{self.serial}",
                                   daemon=True)
        self._t.start()

    def stop(self) -> None:
        self._stop.set()
        if self._t is not None:
            self._t.join(timeout=2.0)
        try:
            self._sock.close()
        except OSError:
            pass

    def _report(self, t: float) -> bytes:
        if self.mode == "static":
            return TM.build_usb01(**self.STATIC)
        w = t * 2.0 * math.pi / 6.0 + self.phase        # one lap every 6 s
        names = list(TM._BUTTON_BITS)
        return TM.build_usb01(
            lx=int(128 + 90 * math.cos(w)) & 0xFF,
            ly=int(128 + 90 * math.sin(w)) & 0xFF,
            rx=int(128 + 90 * math.cos(-1.7 * w)) & 0xFF,
            ry=int(128 + 90 * math.sin(-1.7 * w)) & 0xFF,
            l2=int(127.5 * (1 + math.sin(w * 2.0))),
            r2=int(127.5 * (1 + math.cos(w * 1.5))),
            dpad=P_DPAD[int(t) % 8],
            buttons=(names[int(t * 2) % len(names)],),
            touch=((int(960 + 700 * math.cos(w)), int(540 + 380 * math.sin(w))),
                   (int(960 + 300 * math.cos(-w)), int(540 + 200 * math.sin(-w)))),
            gyro=(int(3000 * math.sin(w)), int(3000 * math.cos(w)),
                  int(1000 * math.sin(2 * w))),
            accel=(int(2000 * math.sin(w)), 8100, int(2000 * math.cos(w))),
            battery_level=7, seq=int(t * 60) & 0xFF)

    def _loop(self) -> None:
        t0 = time.monotonic()
        while not self._stop.wait(self.interval):
            t = time.monotonic() - t0
            msg = {"v": TM.TELEMETRY_VERSION, "serial": self.serial,
                   "report": self._report(t).hex(), "connected": True,
                   "stale_s": 0.004, "rps": 250.0, "t": time.time()}
            try:
                self._sock.sendto(
                    json.dumps(msg, separators=(",", ":")).encode(), self.addr)
            except OSError:
                pass


P_DPAD = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]


def _fake_snapshot(serials: list[str]):
    """A manager-shaped snapshot for the fake feed, so the page's lifecycle
    chrome (state, port, rate) renders too."""
    def snapshot() -> dict:
        return {
            "master_enabled": True,
            "controllers": {
                s: {"serial": s, "state": "running", "enabled": True,
                    "present": True, "port": 3241 + i, "pid": 4242 + i,
                    "battery_percent": 70, "reports_per_s": 250.0,
                    "uptime_s": 61 + i, "attached": True, "error": None,
                    "hide_bluetooth": False, "last_event": ""}
                for i, s in enumerate(serials)},
            "aggregate": {"present": len(serials), "bridges": len(serials),
                          "running": len(serials), "degraded": 0,
                          "attached": len(serials),
                          "reports_per_s": 250.0 * len(serials), "errors": 0},
        }
    return snapshot


def main(argv=None) -> int:
    """Development entry point:  python -m ds5app.dashboard --fake"""
    ap = argparse.ArgumentParser(
        description="serve the ds5bridge dashboard standalone")
    ap.add_argument("--port", type=int, default=None,
                    help="HTTP port (default: config.dashboard_port)")
    ap.add_argument("--fake", nargs="?", const="animate",
                    choices=("animate", "static"), default=None,
                    help="serve a synthetic controller feed instead of "
                         "expecting live bridges ('static' freezes a known "
                         "state for screenshot tests)")
    ap.add_argument("--pads", type=int, default=1,
                    help="how many fake controllers (default 1)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    hub = TM.TelemetryHub()
    hub.start()
    feeds = []
    snapshot_fn = None
    if args.fake:
        serials = [f"d42f4ba1485{i}" for i in range(max(1, args.pads))]
        snapshot_fn = _fake_snapshot(serials)
        for i, s in enumerate(serials):
            f = FakeFeed(hub.port, s, mode=args.fake, phase=i * 1.5)
            f.start()
            feeds.append(f)
        print(f"fake feed: {len(serials)} controller(s), mode={args.fake}")

    dash = DashboardServer(hub=hub, snapshot_fn=snapshot_fn,
                           port=(args.port if args.port is not None
                                 else K.load().dashboard_port))
    dash.start()
    print(f"dashboard at {dash.url}   (telemetry UDP {hub.port}; Ctrl+C to stop)")
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        pass
    finally:
        for f in feeds:
            f.stop()
        dash.stop()
        hub.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
