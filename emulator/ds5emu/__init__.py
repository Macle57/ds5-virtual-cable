"""ds5emu — a user-mode USB/IP device emulator for a wired Sony DualSense.

Phase 2 skeleton. It serves the *real* wired controller's descriptors (read off
the physical device in Phase 0, see `docs/usb-ground-truth.md`) over the USB/IP
wire protocol, so that vadimgrn/usbip-win2's signed UDE driver can attach it as
a local USB device.

Nothing here touches the system. No driver is installed and none is required to
run the unit tests.

Layers, bottom-up:

    wire.py         USB/IP wire protocol — pure bytes, no I/O
    descriptors.py  ground-truth descriptor tables
    uac.py          UAC1 mixer control state
    device.py       the emulated device: CMD_SUBMIT -> RET_SUBMIT, pure
    backend.py      where the Phase-1 Bluetooth bridge plugs in
    server.py       asyncio TCP shell
"""

__all__ = ["wire", "descriptors", "uac", "device", "backend", "server"]

__version__ = "0.1.0"
