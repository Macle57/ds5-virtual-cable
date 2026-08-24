"""The descriptor tables must equal the ground truth captured in Phase 0.

`docs/usb-ground-truth.md` is the authority (it was read off the physical wired
controller through hub IOCTLs). These tests re-parse the hexdumps out of that
markdown file and compare, so the two can never silently diverge.
"""

import pathlib
import re
import unittest

from ds5emu import descriptors as D

DOC = pathlib.Path(__file__).resolve().parents[2] / "docs" / "usb-ground-truth.md"

_HEX_LINE = re.compile(r"^[0-9a-f]{4}\s+((?:[0-9a-f]{2}\s*)+)$")


def _blocks(text: str):
    """Yield every contiguous run of `NNNN  xx xx ...` hexdump lines."""
    current: list[str] = []
    for line in text.splitlines():
        m = _HEX_LINE.match(line.strip())
        if m:
            current.append(m.group(1).replace(" ", ""))
        elif current:
            yield bytes.fromhex("".join(current))
            current = []
    if current:
        yield bytes.fromhex("".join(current))


class TestAgainstGroundTruth(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.assertTrue(pathlib.Path(DOC).exists(), f"missing {DOC}")
        cls.blobs = list(_blocks(DOC.read_text(encoding="utf-8")))

    def test_ground_truth_doc_yields_three_blobs(self):
        # device (18), configuration (227), HID report (289)
        self.assertEqual([len(b) for b in self.blobs], [18, 227, 289])

    def test_device_descriptor_matches(self):
        self.assertEqual(D.DEVICE_DESCRIPTOR, self.blobs[0])

    def test_config_descriptor_matches(self):
        self.assertEqual(D.CONFIG_DESCRIPTOR, self.blobs[1])

    def test_hid_report_descriptor_matches(self):
        self.assertEqual(D.HID_REPORT_DESCRIPTOR, self.blobs[2])
        # docs/STATUS.md gotcha #4: hidapi reconstructs 467 bytes on Windows.
        # The emulator must replay the real 289.
        self.assertEqual(len(D.HID_REPORT_DESCRIPTOR), 289)
        self.assertNotEqual(len(D.HID_REPORT_DESCRIPTOR), 467)


class TestDescriptorSelfConsistency(unittest.TestCase):
    def test_device_descriptor_fields(self):
        d = D.DEVICE_DESCRIPTOR
        self.assertEqual(d[0], 18)                       # bLength
        self.assertEqual(d[1], 0x01)                     # bDescriptorType DEVICE
        self.assertEqual(int.from_bytes(d[2:4], "little"), D.BCD_USB)
        self.assertEqual(d[7], D.B_MAX_PACKET_SIZE0)
        self.assertEqual(int.from_bytes(d[8:10], "little"), D.ID_VENDOR)
        self.assertEqual(int.from_bytes(d[10:12], "little"), D.ID_PRODUCT)
        self.assertEqual(int.from_bytes(d[12:14], "little"), D.BCD_DEVICE)
        self.assertEqual(d[14], 1)                       # iManufacturer
        self.assertEqual(d[15], 2)                       # iProduct
        self.assertEqual(d[16], 0)                       # iSerialNumber: none
        self.assertEqual(d[17], D.B_NUM_CONFIGURATIONS)

    def test_config_header(self):
        c = D.CONFIG_DESCRIPTOR
        self.assertEqual(c[0], 9)
        self.assertEqual(c[1], 0x02)
        self.assertEqual(int.from_bytes(c[2:4], "little"), len(c))
        self.assertEqual(c[4], D.B_NUM_INTERFACES)
        self.assertEqual(c[5], D.B_CONFIGURATION_VALUE)
        self.assertEqual(c[7], 0xC0)     # self-powered
        self.assertEqual(c[8], 250)      # 500 mA

    def test_descriptor_walk_is_well_formed(self):
        total = sum(len(d) for _, d in D.iter_descriptors(D.CONFIG_DESCRIPTOR))
        self.assertEqual(total, len(D.CONFIG_DESCRIPTOR))

    def test_endpoints_match_ground_truth_table(self):
        eps = {}
        for dtype, d in D.iter_descriptors(D.CONFIG_DESCRIPTOR):
            if dtype == 0x05:  # ENDPOINT
                eps[d[2]] = {
                    "attrs": d[3],
                    "wMaxPacketSize": int.from_bytes(d[4:6], "little"),
                    "bInterval": d[6],
                }
        self.assertEqual(set(eps), {D.EP_ISO_OUT, D.EP_ISO_IN, D.EP_HID_IN, D.EP_HID_OUT})

        iso_out = eps[D.EP_ISO_OUT]
        self.assertEqual(iso_out["attrs"] & 0x03, 0x01)   # isochronous
        self.assertEqual(iso_out["attrs"] & 0x0C, 0x08)   # adaptive
        self.assertEqual(iso_out["wMaxPacketSize"], D.ISO_OUT_MAX_PACKET)
        self.assertEqual(iso_out["bInterval"], 4)

        iso_in = eps[D.EP_ISO_IN]
        self.assertEqual(iso_in["attrs"] & 0x03, 0x01)
        self.assertEqual(iso_in["attrs"] & 0x0C, 0x04)    # asynchronous
        self.assertEqual(iso_in["wMaxPacketSize"], D.ISO_IN_MAX_PACKET)
        self.assertEqual(iso_in["bInterval"], 4)

        for ep in (D.EP_HID_IN, D.EP_HID_OUT):
            self.assertEqual(eps[ep]["attrs"] & 0x03, 0x03)  # interrupt
            self.assertEqual(eps[ep]["wMaxPacketSize"], D.HID_MAX_PACKET)
            self.assertEqual(eps[ep]["bInterval"], 6)        # 4 ms at high speed

    def test_iso_binterval_is_at_least_4(self):
        """The condition nefarius identified for UDE isochronous audio to work.

        See docs/virtualization-options.md §2.3. UDE treats bInterval as
        microframes regardless; anything below 4 makes usbaudio.sys fail the
        URB with USBD_STATUS_INVALID_PARAMETER. The real DualSense already
        declares 4, so we must not "fix" it.
        """
        for dtype, d in D.iter_descriptors(D.CONFIG_DESCRIPTOR):
            if dtype == 0x05 and (d[3] & 0x03) == 0x01:
                self.assertGreaterEqual(d[6], 4)

    def test_hid_descriptor_declares_the_report_length(self):
        for dtype, d in D.iter_descriptors(D.CONFIG_DESCRIPTOR):
            if dtype == 0x21:  # HID
                # 09 21 11 01 00 01 22 21 01
                self.assertEqual(d[0], 9)                                  # bLength
                self.assertEqual(int.from_bytes(d[2:4], "little"), 0x0111)  # bcdHID
                self.assertEqual(d[4], 0)                                  # bCountryCode
                self.assertEqual(d[5], 1)                                  # bNumDescriptors
                self.assertEqual(d[6], 0x22)                               # REPORT
                self.assertEqual(int.from_bytes(d[7:9], "little"),
                                 len(D.HID_REPORT_DESCRIPTOR))
                break
        else:
            self.fail("no HID descriptor found")

    def test_interface_infos(self):
        self.assertEqual(
            D.interface_infos(),
            [(0x01, 0x01, 0x00),   # AudioControl
             (0x01, 0x02, 0x00),   # AudioStreaming OUT
             (0x01, 0x02, 0x00),   # AudioStreaming IN
             (0x03, 0x00, 0x00)],  # HID
        )

    def test_no_bos_and_no_ms_os_string(self):
        """Phase 0 correction #2: the real device has neither."""
        self.assertIsNone(D.string_descriptor(0xEE))
        self.assertIsNone(D.string_descriptor(3))

    def test_string_descriptors(self):
        s0 = D.string_descriptor(0)
        self.assertEqual(s0, bytes.fromhex("04030904"))
        s1 = D.string_descriptor(1)
        self.assertEqual(s1[0], len(s1))
        self.assertEqual(s1[1], 0x03)
        self.assertEqual(s1[2:].decode("utf-16-le"), "Sony Interactive Entertainment")
        s2 = D.string_descriptor(2)
        self.assertEqual(s2[2:].decode("utf-16-le"), "DualSense Wireless Controller")

    def test_audio_bandwidth_matches_the_stated_requirement(self):
        # docs/usb-ground-truth.md: ~588 KB/s of isochronous traffic at 1 ms.
        self.assertEqual(D.ISO_OUT_MAX_PACKET + D.ISO_IN_MAX_PACKET, 588)


if __name__ == "__main__":
    unittest.main()
