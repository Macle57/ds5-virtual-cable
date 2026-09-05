"""The telemetry channel, end to end, with no hardware and no dashboard.

Three claims are worth money here:

1. `build_usb01` -> `decode_report` round-trips every field through Phase 1's
   own decoder. The fake feed and the unit tests eat exactly the byte layout
   the real bridge publishes, so a dashboard developed against the fake is
   developed against the truth.
2. A publisher fed by a backend-shaped object reaches a hub over REAL UDP on
   loopback -- sockets, JSON, hex and all -- and the hub serves the decoded
   state. This is the whole pipe minus the controller.
3. Garbage datagrams (wrong version, wrong types, non-JSON, bad hex) are
   dropped by structure, because anything on the machine can send to the port.
"""

from __future__ import annotations

import json
import socket
import time
import unittest

from ds5app import telemetry as TM
from ds5bridge import protocol as P


def wait_for(cond, timeout=3.0, tick=0.02):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(tick)
    return cond()


class BuildDecodeRoundTrip(unittest.TestCase):
    def test_neutral_report_is_neutral(self):
        d = TM.decode_report(TM.build_usb01())
        self.assertEqual((d["lx"], d["ly"], d["rx"], d["ry"]), (128,) * 4)
        self.assertEqual((d["l2"], d["r2"]), (0, 0))
        self.assertEqual(d["dpad"], "-")
        self.assertFalse(any(d["buttons"].values()))
        self.assertFalse(d["touch"][0]["active"])
        self.assertFalse(d["touch"][1]["active"])

    def test_every_button_round_trips(self):
        for name in TM._BUTTON_BITS:
            d = TM.decode_report(TM.build_usb01(buttons=(name,)))
            pressed = [k for k, v in d["buttons"].items() if v]
            self.assertEqual(pressed, [name])

    def test_dpad_round_trips(self):
        for dpad in P.DPAD_NAMES:
            d = TM.decode_report(TM.build_usb01(dpad=dpad))
            self.assertEqual(d["dpad"], dpad)

    def test_axes_triggers_and_seq(self):
        d = TM.decode_report(TM.build_usb01(lx=1, ly=254, rx=200, ry=55,
                                            l2=17, r2=255, seq=42))
        self.assertEqual((d["lx"], d["ly"], d["rx"], d["ry"]), (1, 254, 200, 55))
        self.assertEqual((d["l2"], d["r2"]), (17, 255))
        self.assertEqual(d["seq"], 42)

    def test_touch_points_round_trip_at_the_corners(self):
        # The far corner exercises the 12-bit packing across the split byte.
        d = TM.decode_report(TM.build_usb01(touch=((0, 0), (1919, 1079))))
        t0, t1 = d["touch"]
        self.assertTrue(t0["active"] and t1["active"])
        self.assertEqual((t0["x"], t0["y"]), (0, 0))
        self.assertEqual((t1["x"], t1["y"]), (1919, 1079))
        self.assertEqual((t0["id"], t1["id"]), (0, 1))

    def test_motion_round_trips_signed(self):
        d = TM.decode_report(TM.build_usb01(gyro=(-3000, 42, 32767),
                                            accel=(-32768, 8100, -1)))
        self.assertEqual(d["gyro"], [-3000, 42, 32767])
        self.assertEqual(d["accel"], [-32768, 8100, -1])

    def test_battery_and_status_flags(self):
        d = TM.decode_report(TM.build_usb01(battery_level=3,
                                            battery_state="charging",
                                            headphone=True, mic_muted=True))
        self.assertEqual(d["battery_percent"], 30)
        self.assertEqual(d["battery_state"], "charging")
        self.assertTrue(d["headphone"])
        self.assertTrue(d["mic_muted"])
        self.assertFalse(d["mic"])

    def test_decode_is_json_serialisable(self):
        # The whole point of the dict: it goes straight into an SSE frame.
        json.dumps(TM.decode_report(TM.build_usb01(buttons=("PS",),
                                                   touch=((5, 5),))))

    def test_decode_rejects_junk(self):
        self.assertIsNone(TM.decode_report(b""))
        self.assertIsNone(TM.decode_report(b"\x02" + bytes(63)))
        self.assertIsNone(TM.decode_report(b"\x01\x02\x03"))


class FakeBackend:
    """Just enough of `BridgeBackend`'s read surface for a publisher."""

    def __init__(self, report):
        self.report = report
        self.stats = {"input_delivered": 0}

    def latest_input_report(self, max_len):
        return self.report[:max_len]

    def device_status(self):
        return {"connected": True, "serial": "aabbccddeeff", "stale_s": 0.004,
                "battery_percent": 70, "battery_state": "discharging"}


class PublisherToHub(unittest.TestCase):
    """Claim 2: the real pipe, over real loopback UDP."""

    def setUp(self):
        self.hub = TM.TelemetryHub()
        self.hub.start()
        self.addCleanup(self.hub.stop)

    def test_state_arrives_and_decodes(self):
        report = TM.build_usb01(buttons=("x", "R1"), lx=240, r2=192,
                                touch=((600, 300),))
        pub = TM.TelemetryPublisher(FakeBackend(report), self.hub.port,
                                    serial="AABBCCDDEEFF", hz=120)
        pub.start()
        self.addCleanup(pub.stop)
        self.assertTrue(wait_for(lambda: self.hub.latest()))
        entry = self.hub.latest()["aabbccddeeff"]     # serial is lowercased
        self.assertTrue(entry["connected"])
        d = entry["decoded"]
        self.assertTrue(d["buttons"]["x"] and d["buttons"]["R1"])
        self.assertEqual(d["lx"], 240)
        self.assertEqual(d["r2"], 192)
        self.assertEqual(d["touch"][0]["x"], 600)

    def test_unchanged_state_still_keepalives(self):
        # A pad nobody touches must not vanish from the page: the publisher
        # refreshes at KEEPALIVE_S even when the report bytes are identical.
        pub = TM.TelemetryPublisher(FakeBackend(TM.build_usb01()),
                                    self.hub.port, serial="ab", hz=120)
        pub.start()
        self.addCleanup(pub.stop)
        self.assertTrue(wait_for(lambda: pub.sent >= 2,
                                 timeout=3 * TM.KEEPALIVE_S + 1.0))

    def test_garbage_is_dropped_not_fatal(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(s.close)
        addr = (TM.LOOPBACK, self.hub.port)
        s.sendto(b"not json", addr)
        s.sendto(b'{"v": 99, "serial": "zz"}', addr)             # wrong version
        s.sendto(json.dumps({"v": 1, "serial": 7}).encode(), addr)   # bad type
        s.sendto(json.dumps({"v": 1, "serial": "zz",
                             "report": "zz-not-hex"}).encode(), addr)
        self.assertTrue(wait_for(lambda: self.hub.dropped >= 4))
        self.assertEqual(self.hub.latest(), {})
        # ... and a good one still lands afterwards.
        s.sendto(json.dumps({"v": 1, "serial": "ok",
                             "report": TM.build_usb01().hex(),
                             "connected": True}).encode(), addr)
        self.assertTrue(wait_for(lambda: "ok" in self.hub.latest()))

    def test_old_entries_age_out(self):
        self.hub._ingest(json.dumps({"v": 1, "serial": "old",
                                     "connected": True}).encode())
        self.assertIn("old", self.hub.latest())
        time.sleep(0.05)
        self.assertNotIn("old", self.hub.latest(max_age=0.01))


class ChildCommandFlag(unittest.TestCase):
    def test_manager_child_command_carries_the_port(self):
        from ds5app import manager as MG

        cmd = MG.default_child_command("aa", 3241, telemetry_port=45678)
        i = cmd.index("--telemetry-port")
        self.assertEqual(cmd[i + 1], "45678")
        # ... and no flag at all when there is no dashboard.
        self.assertNotIn("--telemetry-port",
                         MG.default_child_command("aa", 3241))


if __name__ == "__main__":
    unittest.main()
