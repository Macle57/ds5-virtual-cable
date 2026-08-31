"""The black-box flight recorder (`ds5emu.capture`) and its bridge wiring.

`BlackBox` itself is stdlib-only and is tested everywhere; the
`BridgeBackend` integration needs numpy / PyAV / hidapi and is skipped where
they are missing, same as the rest of the bridge tests.

What is worth asserting here is the recorder's contract, not its formatting:
recording is bounded, dumping never raises, and every host->device seam of
the backend leaves a trace — because the whole point of the tool is that a
single reproduction of a Bluetooth link death preserves the bytes that
caused it (docs/wired-gap-findings.md, the TLOU power-off investigation).
"""

from __future__ import annotations

import os
import tempfile
import unittest

from ds5emu.capture import BlackBox, _hex

try:
    from ds5emu import bridge as B
    HAVE_DEPS = True
except Exception:  # noqa: BLE001
    HAVE_DEPS = False


class BlackBoxTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.bb = BlackBox(os.path.join(self.dir.name, "bb"), capacity=100)

    def _read(self, path: str) -> str:
        with open(path, encoding="utf-8") as fh:
            return fh.read()

    def test_record_and_dump_roundtrip(self):
        self.bb.record("bt.write", b"\x31\x40\x10", note="seq=4 crc=ok")
        self.bb.record("usb.out02", None, note="flags=03/00/00")
        path = self.bb.dump("link-death")
        self.assertIsNotNone(path)
        text = self._read(path)
        self.assertIn("bt.write", text)
        self.assertIn("31 40 10", text)
        self.assertIn("seq=4 crc=ok", text)
        self.assertIn("flags=03/00/00", text)
        self.assertIn("link-death", text)

    def test_ring_is_bounded_and_keeps_the_newest(self):
        for i in range(250):
            self.bb.record("e", note=f"n={i}")
        text = self._read(self.bb.dump("full"))
        self.assertNotIn("n=149", text)   # oldest gone
        self.assertIn("n=150", text)      # exactly the last 100 kept
        self.assertIn("n=249", text)

    def test_data_is_copied_not_referenced(self):
        buf = bytearray(b"\x02\xff")
        self.bb.record("usb.out02", buf)
        buf[1] = 0
        text = self._read(self.bb.dump("copy"))
        self.assertIn("02 ff", text)

    def test_long_data_is_elided_in_the_middle(self):
        s = _hex(bytes(range(200)))
        self.assertIn("more]..", s)
        self.assertIn("00 01", s)                 # head survives
        self.assertIn("c6 c7", s)                 # tail survives (198, 199)

    def test_dumps_are_numbered_and_reason_is_slugged(self):
        p1 = self.bb.dump("link death!")
        p2 = self.bb.dump("link death!")
        self.assertNotEqual(p1, p2)
        self.assertIn("-01-link-death-", p1)
        self.assertIn("-02-link-death-", p2)

    def test_dump_never_raises_even_when_the_path_is_unwritable(self):
        bb = BlackBox(os.path.join(self.dir.name, "no-such-dir", "x", "bb"))
        bb.record("e")
        self.assertIsNone(bb.dump("doomed"))     # returned None, did not raise

    def test_context_fn_lines_appear_and_its_failure_is_contained(self):
        self.bb.context_fn = lambda: ["stats: {'x': 1}"]
        self.assertIn("stats: {'x': 1}", self._read(self.bb.dump("ctx")))

        def boom():
            raise RuntimeError("nope")

        self.bb.context_fn = boom
        self.assertIn("context_fn failed", self._read(self.bb.dump("ctx2")))


@unittest.skipUnless(HAVE_DEPS, "numpy / PyAV / hidapi not available")
class BridgeCaptureTests(unittest.TestCase):
    """Every host->device seam leaves a trace, and a link death dumps."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.be = B.BridgeBackend()
        self.be.blackbox = BlackBox(os.path.join(self.dir.name, "bb"))
        self.be.blackbox.context_fn = self.be._bb_context

    def _kinds(self):
        return [e[2] for e in self.be.blackbox._ring]

    def test_output_report_is_recorded_with_its_flags(self):
        self.be.write_output_report(b"\x02" + b"\x03\x14" + bytes(45))
        (entry,) = list(self.be.blackbox._ring)
        self.assertEqual(entry[2], "usb.out02")
        self.assertIn("flags=03/14/00", entry[4])
        self.assertEqual(entry[3][0], 0x02)

    def test_feature_miss_and_hit_are_both_recorded(self):
        self.be._features[0x20] = b"\x20" + bytes(63)
        self.assertIsNone(self.be.get_feature_report(0xE0, 64))
        self.assertIsNotNone(self.be.get_feature_report(0x20, 64))
        notes = [e[4] for e in self.be.blackbox._ring]
        self.assertTrue(any("0xe0" in n and "MISS" in n for n in notes))
        self.assertTrue(any("0x20" in n and "64B" in n for n in notes))

    def test_feature_write_and_test_command_verdicts_are_recorded(self):
        self.be.set_feature_report(0x09, b"\x09\x01\x02")        # blocked write
        self.be.set_feature_report(0x80, bytes([0x7F, 0x01]))    # not allowlisted
        kinds = self._kinds()
        self.assertIn("usb.set_feature", kinds)
        self.assertIn("usb.test_cmd", kinds)
        notes = [e[4] for e in self.be.blackbox._ring if e[2] == "usb.test_cmd"]
        self.assertIn("dev=0x7f act=0x01 BLOCKED", notes)

    def test_set_interface_and_audio_out_summaries_are_recorded(self):
        self.be.set_alt_setting(1, 1)
        self.be.write_audio_out(bytes(384))          # silent 1 ms packet
        self.be.write_audio_out(b"\x01" * 384)       # non-silent
        kinds = self._kinds()
        self.assertIn("usb.set_interface", kinds)
        notes = [e[4] for e in self.be.blackbox._ring if e[2] == "usb.audio_out"]
        self.assertEqual(notes, ["384B silent", "384B"])

    def test_link_death_dumps_automatically_with_context(self):
        self.be.connected.set()
        self.be._bb("bt.write", b"\x31\x40\x10", note="the suspect")
        self.be._on_io_error()
        self.assertEqual(self.be.blackbox.dumps, 1)
        files = os.listdir(self.dir.name)
        self.assertEqual(len(files), 1)
        with open(os.path.join(self.dir.name, files[0]), encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("link-death", files[0])
        self.assertIn("the suspect", text)
        self.assertIn("link.death", text)
        self.assertIn("stats:", text)
        self.assertIn("device_status", text)

    def test_no_dump_when_already_disconnected(self):
        self.be.connected.clear()
        self.be._on_io_error()
        self.assertEqual(self.be.blackbox.dumps, 0)

    def test_backend_without_a_blackbox_is_unaffected(self):
        be = B.BridgeBackend()
        self.assertIsNone(be.blackbox)
        be.write_output_report(b"\x02" + bytes(47))
        be.write_audio_out(bytes(384))
        self.assertIsNone(be.get_feature_report(0xE0, 64))   # no AttributeError


if __name__ == "__main__":
    unittest.main()
