"""Make `prototype/ds5bridge` and `emulator/ds5emu` importable from `app/`.

Three trees are siblings in this repository and none is installed as a package.
`app/` is a thin layer over both, so it reaches them the same way `emulator/`
reaches `prototype/`: by putting the directories on `sys.path`, once, here.

**Frozen builds:** PyInstaller collects `ds5bridge` and `ds5emu` as top-level
packages inside the bundle, so when `sys.frozen` is set the paths below do not
exist and nothing is added -- the imports resolve out of the archive instead.
Nothing is installed and nothing is written; this only touches `sys.path`.
"""

from __future__ import annotations

import sys
from pathlib import Path

FROZEN = bool(getattr(sys, "frozen", False))

#: <repo>/app/ds5app/_bootstrap.py -> <repo>
REPO_ROOT = Path(__file__).resolve().parents[2]
PROTOTYPE_DIR = REPO_ROOT / "prototype"
EMULATOR_DIR = REPO_ROOT / "emulator"


def ensure_on_path() -> None:
    """Idempotently add the sibling source trees to sys.path."""
    if FROZEN:
        return
    for d in (PROTOTYPE_DIR, EMULATOR_DIR):
        p = str(d)
        if d.is_dir() and p not in sys.path:
            sys.path.insert(0, p)


ensure_on_path()
