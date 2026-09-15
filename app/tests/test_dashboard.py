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
        # ... and the built bundle, not a stray file: the mount point is the
        # one static element (the controller SVG is rendered by React).
        self.assertIn('data-app="ds5bridge-dashboard"', text)
        # Zero-network rule: nothing may load from anywhere but this page.
        self.assertNotRegex(text, r'(src|href)="https?://')

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

    def test_macros_round_trip_and_delete_through_the_api(self):
        self.post_config({"input": {"macros": {
            "tm": {"keys": ["ctrl", "shift", "esc"], "label": "Task Manager"},
            "notes": {"run": "notepad.exe"}}}})
        self.assertEqual(set(K.load().input.macros), {"tm", "notes"})
        doc = self.get_json("/api/config")["config"]
        self.assertEqual(doc["input"]["macros"]["tm"]["label"], "Task Manager")
        # A merge cannot express removal, so the page sends a tombstone.
        status, body, _ = self.post_config({"input": {"macros": {"tm": None}}})
        self.assertEqual(status, 200, body)
        self.assertEqual(set(K.load().input.macros), {"notes"})
        doc = self.get_json("/api/config")["config"]
        self.assertNotIn("tm", doc["input"]["macros"])

    def test_unknown_sections_survive_the_round_trip(self):
        """Forward compatibility: the section a newer build writes.

        A section this build has never heard of must be served by GET (so the
        page can render it generically), be editable by POST, and come out of
        `Config.extra` untouched -- not silently dropped by a save that only
        knows today's keys. (This test once used `input` as its example;
        `input` has since become a real, known section -- see the next test.)
        """
        cfg = K.load()
        cfg.extra["someday"] = {"off_timer_minutes": 15, "chords": ["PS+mute"]}
        cfg.save()
        doc = self.get_json("/api/config")
        self.assertEqual(doc["config"]["someday"]["off_timer_minutes"], 15)

        status, _, _ = self.post_config(
            {"someday": {"off_timer_minutes": 30}, "port_base": 3260})
        self.assertEqual(status, 200)
        cfg = K.load()
        self.assertEqual(cfg.extra["someday"]["off_timer_minutes"], 30)
        self.assertEqual(cfg.extra["someday"]["chords"], ["PS+mute"])  # merged,
        self.assertEqual(cfg.port_base, 3260)                          # not replaced

    def test_the_input_section_posts_as_a_known_section(self):
        # The chord engine's section is known now: a POST must land in the
        # parsed `Config.input`, not in `extra`.
        status, _, _ = self.post_config({"input": {"off_timer_minutes": 30}})
        self.assertEqual(status, 200)
        cfg = K.load()
        self.assertNotIn("input", cfg.extra)
        self.assertEqual(cfg.input.off_timer_minutes, 30)

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

    # -- /api/actions ------------------------------------------------------

    def test_api_actions_shape(self):
        """The metadata the settings page builds its pickers from: sourced
        from the live registry and config constants, never hardcoded."""
        doc = self.get_json("/api/actions")
        self.assertTrue(doc["actions"], "empty action list")
        names = [a["name"] for a in doc["actions"]]
        self.assertEqual(names, sorted(names))          # stable, browsable
        for a in doc["actions"]:
            self.assertIsInstance(a["name"], str)
            self.assertTrue(a["doc"], f"{a['name']} has no doc for the picker")
            self.assertIsInstance(a["repeatable"], bool)
        self.assertIn("volume_up", names)
        self.assertNotIn("pad_power_off", names)        # engine-special, the
        # page adds it (and "(none)") itself -- it is not an OS action.
        self.assertEqual(doc["chord_buttons"], list(K.CHORD_BUTTONS))
        self.assertEqual(doc["chord_keys"], list(K.CHORD_BUTTONS))
        self.assertEqual(doc["gesture_keys"], list(K.CHORD_GESTURES))
        self.assertEqual(doc["defaults"]["chords"], dict(K.DEFAULT_CHORDS))
        # The macro editor's key vocabulary is the engine's, in its order.
        from ds5app import actions as ACT
        self.assertEqual(doc["macro_keys"], list(ACT.KEY_NAMES))
        self.assertEqual(doc["macro_keys"][:4], ["ctrl", "shift", "alt", "win"])
        # Every default chord must resolve inside the served vocabulary, or
        # the page would show "(unknown)" rows on a fresh install.
        vocab = set(names) | {e["name"] for e in doc["engine_actions"]}
        for key, action in K.DEFAULT_CHORDS.items():
            self.assertIn(action, vocab, f"default chord {key} -> {action}")
        # The contract the dashboard codes against: the remote table's
        # defaults and the keys it accepts, next to the existing fields.
        self.assertEqual(doc["defaults"]["remote_chords"],
                         dict(K.DEFAULT_REMOTE_CHORDS))
        self.assertEqual(doc["remote_keys"],
                         [b for b in K.CHORD_BUTTONS if b != "ps"]
                         + list(K.CHORD_GESTURES))
        for key in K.DEFAULT_REMOTE_CHORDS:
            self.assertIn(key, doc["remote_keys"])
        for key, action in K.DEFAULT_REMOTE_CHORDS.items():
            self.assertIn(action, vocab, f"default remote {key} -> {action}")
        engine_names = [e["name"] for e in doc["engine_actions"]]
        self.assertIn("keyboard", engine_names)
        for name in ("display_extend", "display_second_only",
                     "display_pc_only", "display_duplicate", "display_cycle",
                     "dictation", "left_click", "right_click", "middle_click"):
            self.assertIn(name, names)
        for a in doc["engine_actions"]:
            self.assertTrue(a["doc"], f"{a['name']} has no doc for the picker")

    def test_api_actions_is_cached_and_guarded(self):
        # Static per process: the exact same object every call ...
        self.assertIs(self.dash.actions_meta(), self.dash.actions_meta())
        # ... and behind the same loopback-Host guard as every other route.
        status, _, _ = self.request("GET", "/api/actions",
                                    headers={"Host": "evil.example"})
        self.assertEqual(status, 403)

    def test_chord_set_to_none_round_trips_as_removed(self):
        # The page writes "(none)" as the string "none". `from_dict` must
        # REMOVE the default binding, and `to_dict` must serve it back as
        # "none" -- an absent key would resurrect the default on the next
        # load (InputConfig.to_dict's contract).
        status, _, _ = self.post_config({"input": {"chords": {"cross": "none"}}})
        self.assertEqual(status, 200)
        self.assertNotIn("cross", K.load().input.chords)
        doc = self.get_json("/api/config")
        self.assertEqual(doc["config"]["input"]["chords"]["cross"], "none")

    def test_chord_rebind_round_trips(self):
        status, _, _ = self.post_config(
            {"input": {"chords": {"circle": "media_next"}}})
        self.assertEqual(status, 200)
        cfg = K.load()
        self.assertEqual(cfg.input.chords["circle"], "media_next")
        # untouched defaults survive the merge
        self.assertEqual(cfg.input.chords["cross"], "media_play_pause")

    def test_page_wires_the_input_section(self):
        _, body, _ = self.request("GET", "/")
        text = body.decode("utf-8")
        self.assertIn("/api/actions", text)      # pickers are fed, not typed
        self.assertIn("Desktop Layout", text)    # the Steam remote-mode warning
        self.assertIn("haptic_strength", text)   # sibling-agent keys rendered
        self.assertIn("stick_mouse_in_chord", text)

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

    # -- /api/action and the host's extra state ----------------------------

    def post_action(self, body, headers=None):
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        status, out, _ = self.request(
            "POST", "/api/action", body=raw,
            headers=headers or {"Content-Type": "application/json"})
        try:
            return status, json.loads(out)
        except ValueError:
            return status, out

    def test_action_without_a_host_is_503(self):
        status, body = self.post_action({"op": "rescan"})
        self.assertEqual(status, 503)
        self.assertFalse(body["ok"])

    def test_action_is_dispatched_to_the_host_with_its_body(self):
        seen = []

        def host(op, body):
            seen.append((op, body))
            return {"ok": True, "text": f"did {op}"}
        self.dash.on_action = host
        status, body = self.post_action({"op": "rescan", "why": "test"})
        self.assertEqual(status, 200)
        self.assertEqual(body, {"ok": True, "text": "did rescan"})
        self.assertEqual(seen, [("rescan", {"op": "rescan", "why": "test"})])

    def test_an_op_the_host_does_not_know_is_404(self):
        self.dash.on_action = lambda op, body: None
        status, body = self.post_action({"op": "make_coffee"})
        self.assertEqual(status, 404)
        self.assertFalse(body["ok"])
        self.assertIn("make_coffee", body["text"])

    def test_a_body_without_an_op_is_400(self):
        self.dash.on_action = lambda op, body: {"ok": True}
        for bad in ({}, {"op": ""}, {"op": 3}):
            status, body = self.post_action(bad)
            self.assertEqual(status, 400, bad)
            self.assertFalse(body["ok"])

    def test_a_host_that_throws_is_a_500_with_its_message(self):
        def host(op, body):
            raise RuntimeError("usbip is on fire")
        self.dash.on_action = host
        status, body = self.post_action({"op": "rescan"})
        self.assertEqual(status, 500)
        self.assertEqual(body, {"ok": False, "text": "usbip is on fire"})

    def test_a_bare_host_answer_is_normalised(self):
        # A host may answer with just a bool, or a dict missing `text`; the
        # page always gets the {ok, text} shape it was promised.
        self.dash.on_action = lambda op, body: True
        self.assertEqual(self.post_action({"op": "x"})[1],
                         {"ok": True, "text": ""})
        self.dash.on_action = lambda op, body: {"ok": False}
        self.assertEqual(self.post_action({"op": "x"})[1],
                         {"ok": False, "text": ""})

    def test_action_has_the_same_guards_as_config(self):
        self.dash.on_action = lambda op, body: {"ok": True}
        status, _ = self.post_action({"op": "rescan"},
                                     headers={"Content-Type": "text/plain"})
        self.assertEqual(status, 415)
        status, _ = self.post_action(b"{not json")
        self.assertEqual(status, 400)
        status, _ = self.post_action({"op": "rescan"},
                                     headers={"Content-Type": "application/json",
                                              "Host": "evil.example"})
        self.assertEqual(status, 403)

    def test_state_carries_the_hosts_extras(self):
        self.dash.state_extras = lambda: {
            "update": {"available": "1.0.1", "url": "u", "checked_at": 1.0,
                       "installing": False},
            "autostart": {"enabled": True, "mode": "task"},
            # never allowed to displace the frame's own fields
            "controllers": "nope", "aggregate": "nope"}
        state = self.get_json("/api/state")
        self.assertEqual(state["update"]["available"], "1.0.1")
        self.assertEqual(state["autostart"], {"enabled": True, "mode": "task"})
        self.assertIn("aabbccddeeff", state["controllers"])
        self.assertEqual(state["aggregate"]["present"], 1)

    def test_extras_that_fail_cost_nothing_but_themselves(self):
        def boom():
            raise RuntimeError("no")
        self.dash.state_extras = boom
        state = self.get_json("/api/state")
        self.assertNotIn("update", state)
        self.assertIn("aabbccddeeff", state["controllers"])

    def test_without_a_host_there_are_no_extras(self):
        state = self.get_json("/api/state")
        self.assertNotIn("update", state)
        self.assertNotIn("autostart", state)


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
