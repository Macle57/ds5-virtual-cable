"""Assert that `ds5emu` imports nothing outside the standard library.

That property is a design constraint, not a coincidence: it is what lets the
USB/IP protocol, the descriptors, the device state machine and the frame clock
be tested on a bare Python with no driver, no controller and no pip packages.
`bridge.py` is the single deliberate exception and is reached only through a
module-level `__getattr__` in `backend.py`, so importing `backend` must NOT
pull numpy / PyAV / hidapi in.

A violation is invisible on a developer machine with the venv active. It only
shows up later, on someone else's bare Python — so check it explicitly.

    python tools/check_stdlib_only.py      (from emulator/)

Exits non-zero and names the offender if anything leaked. Run by CI.
"""

from __future__ import annotations

import sys
from pathlib import Path

FORBIDDEN = ("numpy", "av", "hid", "sounddevice")

#: Every ds5emu module except `bridge`, which is allowed third-party imports.
MODULES = (
    "ds5emu",
    "ds5emu.wire",
    "ds5emu.descriptors",
    "ds5emu.uac",
    "ds5emu.timing",
    "ds5emu.device",
    "ds5emu.backend",
    "ds5emu.server",
    "ds5emu.translate",
    "ds5emu.__main__",
)


def main() -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

    import importlib

    for name in MODULES:
        importlib.import_module(name)

    leaked = sorted(m for m in FORBIDDEN if m in sys.modules)
    if leaked:
        print(f"FAIL: third-party import leaked into ds5emu: {leaked}")
        print("      Something in ds5emu/ now imports it eagerly. Move that")
        print("      code to bridge.py, or import it lazily inside a function.")
        return 1

    print(f"OK: {len(MODULES)} ds5emu modules import stdlib only")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
