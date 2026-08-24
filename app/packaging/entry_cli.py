"""Frozen entry point for ds5bridge.exe (console)."""

import multiprocessing
import sys

from ds5app.cli import main

if __name__ == "__main__":
    # Harmless when nothing spawns a process, and mandatory if anything ever
    # does: without it a frozen child re-runs the whole program instead of the
    # worker, which on this app would mean a second bridge fighting the first.
    multiprocessing.freeze_support()
    sys.exit(main())
