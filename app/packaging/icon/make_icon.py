"""Regenerate app/packaging/ds5bridge.ico and the PNGs next to this script.

    prototype\\.venv\\Scripts\\python.exe app\\packaging\\icon\\make_icon.py

Needs only Pillow. Writes:

    icon/icon-<n>.png      one RGBA frame per size (the .ico's sources)
    icon/preview.png       every frame on a dark and a light background,
                           nearest-neighbour upscaled, plus a 1:1 strip
    ../ds5bridge.ico       all frames; <= 48 px as 32-bit BMP (with an AND
                           mask, for anything that still reads those), >= 64 px
                           as PNG (Vista+)

The shape is the tray's own (`ds5app/icon_art.draw_pad`), in the tray's
"running" green, without the battery bar or the second-pad pip. From 32 px up
it is that drawing rendered at 8x and downsampled. At 16, 20 and 24 px the
downsampled drawing goes soft (the sticks melt into the body and the dip
between the grips vanishes), so those frames are drawn by hand below: same
silhouette, same colour, pixel-aligned edges, square-ish sticks centred where
the drawing puts them, and the grips stretched one pixel down so the
gamepad's notch still reads.
"""

from __future__ import annotations

import importlib.util
import io
import struct
import sys
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
ICO = HERE.parent / "ds5bridge.ico"
ART = HERE.parents[1] / "ds5app" / "icon_art.py"

SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)
SUPERSAMPLE = 8

# Loaded by path: importing the `ds5app` package would import the service.
_spec = importlib.util.spec_from_file_location("ds5_icon_art", ART)
IA = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(IA)

# Hand-tuned small frames. '#' body, '+' body at half alpha (corner
# smoothing), 'o' stick, 'x' stick blended 65/35 with the body (rounds the
# stick's corners), '.' transparent.
PIXEL_ART = {
    16: """
        ................
        ................
        ................
        ................
        ..+##########+..
        .##############.
        ################
        #####oo##oo#####
        #####oo##oo#####
        ################
        ################
        +#####....#####+
        .+###+....+###+.
        ................
        ................
        ................
    """,
    20: """
        ....................
        ....................
        ....................
        ....................
        ....................
        ..+##############+..
        .+################+.
        +##################+
        ####################
        ######xox##xox######
        ######ooo##ooo######
        ######xox##xox######
        ####################
        +#######....#######+
        .######......######.
        ..+###+......+###+..
        ....................
        ....................
        ....................
        ....................
    """,
    24: """
        ........................
        ........................
        ........................
        ........................
        ........................
        ........................
        ...+################+...
        ..+##################+..
        .+####################+.
        ########################
        #######xoox##xoox#######
        #######oooo##oooo#######
        #######oooo##oooo#######
        #######xoox##xoox#######
        ########################
        +#########....#########+
        .+#######......#######+.
        ..+####+........+####+..
        ........................
        ........................
        ........................
        ........................
        ........................
        ........................
    """,
}


def _from_pixel_art(n: int, art: str, rgb) -> Image.Image:
    rows = [r.strip() for r in art.strip().splitlines()]
    assert len(rows) == n and all(len(r) == n for r in rows), f"{n}px art is not {n}x{n}"
    stick = IA.STICK_RGB
    mix = tuple(round(0.35 * a + 0.65 * b) for a, b in zip(rgb, stick))
    pal = {
        ".": (0, 0, 0, 0),
        "#": rgb + (255,),
        "+": rgb + (128,),
        "o": stick + (255,),
        "x": mix + (255,),
    }
    img = Image.new("RGBA", (n, n))
    img.putdata([pal[c] for r in rows for c in r])
    return img


def _from_drawing(n: int, rgb) -> Image.Image:
    big = n * SUPERSAMPLE
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    IA.draw_pad(ImageDraw.Draw(img), rgb, big / 64.0)
    # Downsample in premultiplied space, so the transparent black around the
    # shape does not darken its anti-aliased edge.
    return img.convert("RGBa").resize((n, n), Image.LANCZOS).convert("RGBA")


def frame(n: int, rgb=IA.APP_ICON_RGB) -> Image.Image:
    if n in PIXEL_ART:
        return _from_pixel_art(n, PIXEL_ART[n], rgb)
    return _from_drawing(n, rgb)


def _bmp_entry(img: Image.Image) -> bytes:
    """A 32-bit BGRA DIB with its AND mask, as an .ico frame stores it."""
    w, h = img.size
    px = img.load()
    header = struct.pack("<IiiHHIIiiII", 40, w, h * 2, 1, 32, 0, 0, 0, 0, 0, 0)
    xor = bytearray()
    for y in range(h - 1, -1, -1):
        for x in range(w):
            r, g, b, a = px[x, y]
            xor += bytes((b, g, r, a))
    stride = ((w + 31) // 32) * 4
    mask = bytearray()
    for y in range(h - 1, -1, -1):
        row = bytearray(stride)
        for x in range(w):
            if px[x, y][3] == 0:
                row[x // 8] |= 0x80 >> (x % 8)
        mask += row
    return header + bytes(xor) + bytes(mask)


def _png_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def write_ico(frames: list[Image.Image], path: Path) -> None:
    blobs = [_png_bytes(f) if f.width >= 64 else _bmp_entry(f) for f in frames]
    out = bytearray(struct.pack("<HHH", 0, 1, len(frames)))
    offset = 6 + 16 * len(frames)
    for f, blob in zip(frames, blobs):
        w = f.width if f.width < 256 else 0
        out += struct.pack("<BBBBHHII", w, w, 0, 0, 1, 32, len(blob), offset)
        offset += len(blob)
    for blob in blobs:
        out += blob
    path.write_bytes(bytes(out))


def preview(frames: list[Image.Image]) -> Image.Image:
    """Each frame upscaled (integer, nearest) to <= 256 px, on dark and light,
    then a strip of every frame at 1:1 on both."""
    cell, pad = 256, 12
    backgrounds = [(32, 32, 36, 255), (240, 240, 240, 255)]
    strip_h = 256 + 2 * pad
    width = len(frames) * (cell + pad) + pad
    sheet = Image.new("RGBA", (width, 2 * (cell + pad) + pad + strip_h), (128, 128, 128, 255))
    for row, bg in enumerate(backgrounds):
        y = pad + row * (cell + pad)
        for i, f in enumerate(frames):
            k = cell // f.width
            tile = Image.new("RGBA", (cell, cell), bg)
            up = f.resize((f.width * k, f.height * k), Image.NEAREST)
            tile.alpha_composite(up, ((cell - up.width) // 2, (cell - up.height) // 2))
            sheet.paste(tile, (pad + i * (cell + pad), y))
    # 1:1 strip: left half dark, right half light
    y0 = 2 * (cell + pad) + pad
    half = width // 2
    sheet.paste(Image.new("RGBA", (half, strip_h), backgrounds[0]), (0, y0))
    sheet.paste(Image.new("RGBA", (width - half, strip_h), backgrounds[1]), (half, y0))
    for start in (0, half):
        x = start + pad
        for f in frames:
            sheet.alpha_composite(f, (x, y0 + pad + (256 - f.height) // 2))
            x += f.width + 8
    return sheet.convert("RGB")


def main() -> int:
    frames = [frame(n) for n in SIZES]
    for n, f in zip(SIZES, frames):
        f.save(HERE / f"icon-{n}.png", optimize=True)
    write_ico(frames, ICO)
    preview(frames).save(HERE / "preview.png", optimize=True)
    print(f"wrote {ICO} ({ICO.stat().st_size} bytes) and {len(SIZES)} PNGs + preview.png in {HERE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
