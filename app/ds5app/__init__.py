"""ds5app -- the product layer over the Phase 1-3 bridge.

`emulator/ds5emu` is the USB/IP device and the Bluetooth backend; this package
is everything that turns those into something a person can run: finding
usbip.exe, finding the right controller, ordering the bring-up and the
tear-down, a system-tray icon, and the packaged exe.

Deliberately thin. The tray and the CLI are two front ends over one
`BridgeService`; nothing about the protocol lives here.
"""

from .service import BridgeService, cleanup  # noqa: F401

__version__ = "0.5.0"
