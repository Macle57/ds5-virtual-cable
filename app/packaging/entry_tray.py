"""Frozen entry point for ds5bridge-tray.exe (windowed -- no console).

A windowed build has no stdout, so anything that prints goes nowhere and
anything that reads stdin raises. `sys.stdout`/`stderr` are None under
`console=False`, and a bare `print()` then raises AttributeError deep inside a
callback, so they are replaced with a sink before anything else runs.
"""

import multiprocessing
import os
import sys


class _Sink:
    def write(self, _s):
        return 0

    def flush(self):
        pass

    def isatty(self):
        return False


def _harden_streams() -> None:
    for name in ("stdout", "stderr"):
        if getattr(sys, name, None) is None:
            setattr(sys, name, _Sink())


if __name__ == "__main__":
    _harden_streams()
    multiprocessing.freeze_support()
    from ds5app.cli import build_parser, cmd_tray

    argv = sys.argv[1:]
    if not argv or argv[0].startswith("-"):
        argv = ["tray"] + argv
    args = build_parser().parse_args(argv)
    sys.exit(cmd_tray(args) if getattr(args, "func", None) is cmd_tray
             else args.func(args))
