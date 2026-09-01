"""The dashboard server: page, state, stream, and -- above all -- the config API.

The config API is the part that can do damage, so it gets the scrutiny: writes
must go through `config.save` (atomic, coercing, quarantine-safe), a section
this build has never heard of must survive a round trip untouched (other
agents add sections in parallel; the settings page renders them generically),
and the two cheap hardenings -- loopback-only Host names and JSON-only writes
-- must actually refuse.

Every test runs against a real `DashboardServer` on an ephemeral port with
`DS5_CONFIG` pointed into a temp directory, so nothing here can touch the
developer's settings.
"""

from __future__ import annotations

import http.client
import json
import os
import tempfile
import time
import unittest

from ds5app import config as K
from ds5app import dashboard as DB
from ds5app import telemetry as TM


class DashboardCase(unittest.TestCase):
    """A running server + a scratch config dir, per test."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._old_env = os.environ.get("DS5_CONFIG")
        os.environ["DS5_CONFIG"] = self._tmp.name

        def restore():
            if self._old_env is None:
                os.environ.pop("DS5_CONFIG", None)
            else:
                os.environ["DS5_CONFIG"] = self._old_env
        self.addCleanup(restore)

        self.hub = TM.TelemetryHub()
        self.hub.start()
        self.addCleanup(self.hub.stop)
        self.saved = []
        self.dash = DB.DashboardServer(
            hub=self.hub, snapshot_fn=self._snapshot, port=0,
            on_config_saved=self.saved.append)
        self.dash.start()
        self.addCleanup(self.dash.stop)

    @staticmethod
    def _snapshot() -> dict:
        return {
            "master_enabled": True,
            "controllers": {"aabbccddeeff": {
                "serial": "aabbccddeeff", "state": "running", "enabled": True,
                "present": True, "port": 3241, "battery_percent": 70,
                "reports_per_s": 250.0, "uptime_s": 61, "attached": True,
                "error": None, "hide_bluetooth": False}},
            "aggregate": {"present": 1, "running": 1, "degraded": 0,
                          "attached": 1, "reports_per_s": 250.0, "errors": 0},
        }

    # -- plumbing ----------------------------------------------------------

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.dash.port, timeout=5)
        self.addCleanup(conn.close)
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        return resp.status, resp.read(), dict(resp.getheaders())

    def get_json(self, path):
        status, body, _ = self.request("GET", path)
        self.assertEqual(status, 200, body)
        return json.loads(body)

    def post_config(self, updates, headers=None):
        return self.request(
            "POST", "/api/config", body=json.dumps(updates).encode(),
            headers=headers or {"Content-Type": "application/json"})

    # -- the page ----------------------------------------------------------

    def test_page_serves(self):
        status, body, headers = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers.get("Content-Type", ""))
        text = body.decode("utf-8")
        self.assertIn("ds5bridge", text)
        self.assertIn("api/stream", text)       # the page really is this page
        self.assertIn('id="pad"', text)         # ... with the controller SVG

    def test_unknown_path_is_404(self):
        status, _, _ = self.request("GET", "/nope")
        self.assertEqual(status, 404)

    # -- state -------------------------------------------------------------

    def test_state_merges_manager_and_telemetry(self):
        self.hub._ingest(json.dumps({
            "v": 1, "serial": "AABBCCDDEEFF", "connected": True, "rps": 250.0,
            "report": TM.build_usb01(buttons=("tri",), l2=99).hex()}).encode())
        state = self.get_json("/api/state")
        c = state["controllers"]["aabbccddeeff"]
        self.assertEqual(c["state"], "running")            # from the manager
        self.assertEqual(c["port"], 3241)
        d = c["telemetry"]["decoded"]                      # from telemetry
        self.assertTrue(d["buttons"]["tri"])
        self.assertEqual(d["l2"], 99)

    def test_state_serves_telemetry_only_controllers(self):
        # A pad publishing telemetry that the manager does not know (a bare
        # `ds5bridge run --telemetry-port`) still appears.
        self.hub._ingest(json.dumps({
            "v": 1, "serial": "0011223344aa", "connected": True,
            "report": TM.build_usb01().hex()}).encode())
        state = self.get_json("/api/state")
        self.assertIn("0011223344aa", state["controllers"])

    def test_stream_sends_json_frames(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.dash.port, timeout=5)
        self.addCleanup(conn.close)
        conn.request("GET", "/api/stream")
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200)
        self.assertIn("text/event-stream", resp.getheader("Content-Type", ""))
        deadline = time.monotonic() + 5.0
        frame = None
        while time.monotonic() < deadline:
            line = resp.fp.readline()
            if line.startswith(b"data: "):
                frame = json.loads(line[6:])
                break
        self.assertIsNotNone(frame, "no SSE frame within 5 s")
        self.assertIn("controllers", frame)
        self.assertIn("aabbccddeeff", frame["controllers"])

    # -- config API --------------------------------------------------------

    def test_config_get_serves_defaults(self):
        doc = self.get_json("/api/config")
        self.assertEqual(doc["config"]["dashboard_port"],
                         K.DEFAULT_DASHBOARD_PORT)
        self.assertTrue(doc["path"].startswith(self._tmp.name))

    def test_config_post_saves_through_the_config_module(self):
        status, body, _ = self.post_config(
            {"port_base": 3300,
             "controllers": {"aabbccddeeff": {"label": "living room"}}})
        self.assertEqual(status, 200, body)
        # The FILE is the truth: reload through the config module.
        cfg = K.load()
        self.assertEqual(cfg.port_base, 3300)
        self.assertEqual(cfg.get("aabbccddeeff").label, "living room")
        # ... and the host was told, with the saved object.
        self.assertEqual(len(self.saved), 1)
        self.assertEqual(self.saved[0].port_base, 3300)

    def test_config_post_merges_rather_than_replaces(self):
        # Two saves touching different things must both survive.
        self.post_config({"controllers": {"aabbccddeeff": {"label": "sofa"}}})
        self.post_config({"controllers": {"aabbccddeeff": {"enabled": False}}})
        cc = K.load().get("aabbccddeeff")
        self.assertEqual(cc.label, "sofa")
        self.assertFalse(cc.enabled)

    def test_unknown_sections_survive_the_round_trip(self):
        """Forward compatibility: the section another agent's build writes.

        An `input` section this build has never heard of must be served by
        GET (so the page can render it generically), be editable by POST, and
        come out of `Config.extra` untouched -- not silently dropped by a
        save that only knows today's keys.
        """
        cfg = K.load()
        cfg.extra["input"] = {"off_timer_minutes": 15, "chords": ["PS+mute"]}
        cfg.save()
        doc = self.get_json("/api/config")
        self.assertEqual(doc["config"]["input"]["off_timer_minutes"], 15)

        status, _, _ = self.post_config(
            {"input": {"off_timer_minutes": 30}, "port_base": 3260})
        self.assertEqual(status, 200)
        cfg = K.load()
        self.assertEqual(cfg.extra["input"]["off_timer_minutes"], 30)
        self.assertEqual(cfg.extra["input"]["chords"], ["PS+mute"])  # merged,
        self.assertEqual(cfg.port_base, 3260)                        # not replaced

    def test_config_post_coerces_garbage_instead_of_saving_it(self):
        # `Config.from_dict` is the gate: nonsense degrades to defaults, so a
        # buggy page cannot poison the startup path.
        status, _, _ = self.post_config({"port_base": "not a port"})
        self.assertEqual(status, 200)
        self.assertEqual(K.load().port_base, K.DEFAULT_PORT_BASE)

    def test_config_post_refuses_non_json_content_type(self):
        status, _, _ = self.post_config(
            {"enabled": False},
            headers={"Content-Type": "text/plain"})
        self.assertEqual(status, 415)
        self.assertTrue(K.load().enabled)      # nothing was written

    def test_config_post_refuses_malformed_bodies(self):
        status, _, _ = self.request(
            "POST", "/api/config", body=b"{not json",
            headers={"Content-Type": "application/json"})
        self.assertEqual(status, 400)
        status, _, _ = self.request(
            "POST", "/api/config", body=b'["a", "list"]',
            headers={"Content-Type": "application/json"})
        self.assertEqual(status, 400)

    def test_foreign_host_header_is_refused(self):
        # DNS rebinding: evil.example resolves to 127.0.0.1 and the browser
        # happily connects -- but the Host header betrays it.
        status, _, _ = self.request("GET", "/api/config",
                                    headers={"Host": "evil.example:8765"})
        self.assertEqual(status, 403)
        status, _, _ = self.post_config(
            {"enabled": False},
            headers={"Content-Type": "application/json",
                     "Host": "evil.example"})
        self.assertEqual(status, 403)
        self.assertTrue(K.load().enabled)


class PureHelpers(unittest.TestCase):
    def test_host_ok(self):
        for good in ("127.0.0.1", "127.0.0.1:8765", "localhost",
                     "LOCALHOST:80", "[::1]", "[::1]:8765"):
            self.assertTrue(DB._host_ok(good), good)
        for bad in (None, "", "evil.example", "evil.example:8765",
                    "127.0.0.1.evil.example", "192.168.1.4:8765"):
            self.assertFalse(DB._host_ok(bad), bad)

    def test_deep_merge(self):
        base = {"a": 1, "b": {"x": 1, "y": 2}, "c": [1, 2]}
        out = DB.deep_merge(base, {"b": {"y": 3, "z": 4}, "c": [9], "d": True})
        self.assertEqual(out, {"a": 1, "b": {"x": 1, "y": 3, "z": 4},
                               "c": [9], "d": True})
        self.assertEqual(base["b"], {"x": 1, "y": 2})      # base untouched

    def test_config_dashboard_port_field(self):
        # The additive Config field: default, round trip, and coercion.
        self.assertEqual(K.Config().dashboard_port, K.DEFAULT_DASHBOARD_PORT)
        cfg = K.Config.from_dict({"dashboard_port": 9001})
        self.assertEqual(cfg.dashboard_port, 9001)
        self.assertEqual(cfg.to_dict()["dashboard_port"], 9001)
        for junk in ("nope", -1, 70000, 3240, True):
            self.assertEqual(
                K.Config.from_dict({"dashboard_port": junk}).dashboard_port,
                K.DEFAULT_DASHBOARD_PORT, junk)


class FakeFeedTests(unittest.TestCase):
    def test_static_feed_reaches_a_hub(self):
        """`--fake static` publishes the documented ground-truth state --
        the same state the screenshot test asserts against pixels."""
        hub = TM.TelemetryHub()
        hub.start()
        self.addCleanup(hub.stop)
        feed = DB.FakeFeed(hub.port, "feedaa", mode="static", hz=120)
        feed.start()
        self.addCleanup(feed.stop)
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and "feedaa" not in hub.latest():
            time.sleep(0.02)
        d = hub.latest()["feedaa"]["decoded"]
        self.assertTrue(d["buttons"]["x"] and d["buttons"]["R1"]
                        and d["buttons"]["L3"])
        self.assertEqual(d["dpad"], "W")
        self.assertEqual(d["r2"], 192)
        self.assertEqual(d["lx"], 0xF0)
        self.assertEqual(d["touch"][0]["x"], 600)


if __name__ == "__main__":
    unittest.main()
