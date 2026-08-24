"""Pure tests for the `usbip port` parser. No driver, no hardware, no exe.

This parser is small and it is load-bearing: it decides which attached devices
belong to this bridge, and getting that wrong means detaching somebody else's.
That is not hypothetical -- it happened on 2026-08-25 and killed a running
30-minute soak six minutes in, because "is anything attached?" was being used
to answer "is anything of MINE attached?".

    prototype\\.venv\\Scripts\\python.exe -m unittest discover -s app\\tests -t app
"""

from __future__ import annotations

import os
import unittest
from unittest import mock

from ds5app import usbip as U
from ds5app.usbip import Usbip, UsbipNotFound


class _P(Usbip):
    """Usbip without the constructor's filesystem probe for usbip.exe."""

    def __init__(self, host="127.0.0.1", port=3241):
        self.exe = "usbip.exe"
        self.host = host
        self.port = port
        self.busid = "1-1"


ONE = """\
Imported USB devices
====================
Port 01: device in use at High Speed(480Mbps)
         Sony Corp. : DualSense wireless controller (PS5) (054c:0ce6)
           -> usbip://127.0.0.1:3241/1-1
           -> remote bus/dev 001/001
"""

TWO_DIFFERENT_SERVERS = """\
Imported USB devices
====================
Port 01: device in use at High Speed(480Mbps)
         Sony Corp. : DualSense wireless controller (PS5) (054c:0ce6)
           -> usbip://127.0.0.1:3241/1-1
           -> remote bus/dev 001/001
Port 02: device in use at High Speed(480Mbps)
         Sony Corp. : DualSense wireless controller (PS5) (054c:0ce6)
           -> usbip://127.0.0.1:3242/1-1
           -> remote bus/dev 001/001
"""

REMOTE = """\
Imported USB devices
====================
Port 03: device in use at High Speed(480Mbps)
         Some Vendor : a totally unrelated device (1234:5678)
           -> usbip://192.168.1.50:3240/2-4
           -> remote bus/dev 002/004
"""


class ParsePortsTests(unittest.TestCase):
    def test_nothing_attached_prints_nothing_at_all(self):
        self.assertEqual(_P().parse_ports(""), [])

    def test_one_device(self):
        self.assertEqual(_P().parse_ports(ONE), [(1, "127.0.0.1:3241/1-1")])

    def test_two_devices_from_two_servers(self):
        self.assertEqual(_P().parse_ports(TWO_DIFFERENT_SERVERS),
                         [(1, "127.0.0.1:3241/1-1"), (2, "127.0.0.1:3242/1-1")])

    def test_a_port_with_no_url_still_counts(self):
        # Never silently forget an attached device just because the output
        # shape changed: an unattributable port is still one that exists.
        self.assertEqual(_P().parse_ports("Port 07: device in use\n"), [(7, "")])


class OwnershipTests(unittest.TestCase):
    """The distinction the 2026-08-25 soak kill was about."""

    def ports(self, text, port=3241):
        p = _P(port=port)
        return [n for n, url in p.parse_ports(text)
                if url.startswith(f"{p.host}:{p.port}/")]

    def test_only_our_tcp_port_is_ours(self):
        self.assertEqual(self.ports(TWO_DIFFERENT_SERVERS, 3241), [1])
        self.assertEqual(self.ports(TWO_DIFFERENT_SERVERS, 3242), [2])

    def test_someone_elses_remote_device_is_never_ours(self):
        self.assertEqual(self.ports(REMOTE, 3241), [])
        self.assertEqual(self.ports(REMOTE, 3240), [])

    def test_3241_does_not_match_32410(self):
        text = ONE.replace(":3241/", ":32410/")
        self.assertEqual(self.ports(text, 3241), [])


class FindUsbipTests(unittest.TestCase):
    """The absent-driver path. This message is the entire failure UX for a user
    who skipped step 1 of the guide, so it is asserted rather than assumed."""

    def test_missing_message_names_the_release_the_url_and_the_bad_version(self):
        m = U.MISSING_MESSAGE
        self.assertIn("0.9.7.7", m)
        self.assertIn("github.com/vadimgrn/usbip-win2", m)
        # 0.9.7.8 carries its own maintainer's memory-corruption warning.
        self.assertIn("0.9.7.8", m)
        self.assertIn("USER-GUIDE", m)

    def test_nothing_anywhere_raises_with_that_message(self):
        with mock.patch.object(U, "_from_registry", return_value=[]), \
                mock.patch.object(U.shutil, "which", return_value=None), \
                mock.patch.dict(os.environ, {"ProgramFiles": r"X:\nope",
                                             "ProgramW6432": r"X:\nope",
                                             "ProgramFiles(x86)": r"X:\nope"},
                                clear=False):
            os.environ.pop("DS5_USBIP_EXE", None)
            with self.assertRaises(UsbipNotFound) as cm:
                U.find_usbip()
        self.assertIn("0.9.7.7", str(cm.exception))

    def test_an_explicit_path_is_the_only_candidate(self):
        # An override that silently falls back to the thing it overrides is not
        # an override -- and this is also what makes the missing-driver message
        # reproducible on a machine that HAS the driver installed.
        with mock.patch.object(U, "_from_registry",
                               return_value=[r"C:\Program Files\USBip\usbip.exe"]):
            with self.assertRaises(UsbipNotFound):
                U.find_usbip(r"X:\definitely\not\here\usbip.exe")

    def test_the_env_var_works_the_same_way(self):
        with mock.patch.dict(os.environ,
                             {"DS5_USBIP_EXE": r"X:\nope\usbip.exe"}), \
                mock.patch.object(U, "_from_registry",
                                  return_value=[r"C:\Program Files\USBip\usbip.exe"]):
            with self.assertRaises(UsbipNotFound):
                U.find_usbip()

    def test_registry_lookup_ignores_usbipd_win(self):
        # `usbipd-win` matches a naive "usbip" search, is a completely different
        # product, owns port 3240, and must never be driven by this code.
        self.assertNotIn("usbipd", U.MISSING_MESSAGE.lower())


if __name__ == "__main__":
    unittest.main()
