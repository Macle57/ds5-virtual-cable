"""Make `prototype/ds5bridge` importable from inside `emulator/ds5emu`.

The two trees are siblings in this repository and neither is installed as a
package, so `emulator/` reaches Phase 1 code the same way `prototype/tools/*`
does: by putting `prototype/` on `sys.path`. Doing it here — once, in one
place — is what lets `translate.py` and `bridge.py` *import* the Phase-1
protocol code instead of copy-pasting offsets that would then silently drift.

Nothing is installed and nothing is written; this only touches `sys.path`.
"""

from __future__ import annotations

import sys
from pathlib import Path

#: <repo>/emulator/ds5emu/_bootstrap.py -> <repo>
REPO_ROOT = Path(__file__).resolve().parents[2]
PROTOTYPE_DIR = REPO_ROOT / "prototype"


def ensure_ds5bridge_on_path() -> Path:
    """Idempotently add `<repo>/prototype` to sys.path. Returns the directory."""
    p = str(PROTOTYPE_DIR)
    if p not in sys.path:
        sys.path.insert(0, p)
    return PROTOTYPE_DIR


ensure_ds5bridge_on_path()
