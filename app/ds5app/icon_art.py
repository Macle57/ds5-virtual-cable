"""The ds5bridge pad drawing: one shape for the tray icon and the file icon.

The tray (`tray._icon_image`) paints it in the live state colour with a
battery bar on top; `app/packaging/icon/make_icon.py` paints it in
`APP_ICON_RGB` with nothing on top and writes `ds5bridge.ico`, which the
exes, the installer and the shortcuts carry. Keep this module free of
imports: the generator loads it by file path, without importing `ds5app`
(whose `__init__` pulls in the whole service).
"""

from __future__ import annotations

#: The file icon's colour: the tray's "running" green (`tray.COLORS[RUNNING]`,
#: pinned by a test), i.e. what the tray looks like when all is well.
APP_ICON_RGB = (60, 170, 90)

#: The thumbsticks.
STICK_RGB = (20, 20, 24)


def draw_pad(d, rgb, s: float) -> None:
    """A DualSense-ish silhouette on ImageDraw `d`, on a 64-unit grid * `s`.

    Two grips joined by a bar, two sticks. Spans x 2..62, y 20..50 of 64.
    """
    d.rounded_rectangle([6 * s, 20 * s, 58 * s, 46 * s], radius=12 * s, fill=rgb + (255,))
    d.ellipse([2 * s, 22 * s, 26 * s, 50 * s], fill=rgb + (255,))
    d.ellipse([38 * s, 22 * s, 62 * s, 50 * s], fill=rgb + (255,))
    for cx in (24, 40):
        d.ellipse([(cx - 5) * s, 30 * s, (cx + 5) * s, 40 * s], fill=STICK_RGB + (255,))
