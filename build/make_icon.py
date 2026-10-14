"""Draw the app icon: "A&R" in the app's own type and colours.

    ~/.venvs/anonymizer-redactor/bin/python build/make_icon.py

Writes build/icon.png (1024 px), build/icon.icns for the Mac bundle and
build/icon.ico for Windows. The outputs are committed, so a build never needs
the fonts or this script; run it again only to change the design.

The design follows the interface: the maroon of the header bar, EB Garamond
for the letters, and the macOS icon grid - an 824-point rounded square on a
1024 canvas, so it sits at the same size as every other app in the Dock.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

HERE = Path(__file__).resolve().parent
FONTS = HERE.parent / "webapp" / "static" / "fonts"

SIZE = 1024
TILE = 824                      # the macOS grid's icon body
RADIUS = 185
TOP, BOTTOM = (0x96, 0x1C, 0x20), (0x5F, 0x0E, 0x11)   # the header's maroon, lit from above
CREAM = (0xFB, 0xF4, 0xEC)


def _tile_mask(scale: int = 4) -> Image.Image:
    """The rounded square, drawn large and scaled down for a clean edge."""
    big = Image.new("L", (SIZE * scale, SIZE * scale), 0)
    inset = (SIZE - TILE) // 2 * scale
    ImageDraw.Draw(big).rounded_rectangle(
        (inset, inset, SIZE * scale - inset, SIZE * scale - inset),
        radius=RADIUS * scale, fill=255)
    return big.resize((SIZE, SIZE), Image.LANCZOS)


def draw() -> Image.Image:
    mask = _tile_mask()
    canvas = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))

    # a soft shadow under the tile, as the Dock draws its own icons
    shadow = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    shadow.putalpha(mask.point(lambda v: v * 0.35))
    shadow = shadow.transform(shadow.size, Image.AFFINE, (1, 0, 0, 0, 1, -12))
    canvas.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(18)))

    gradient = Image.new("RGBA", (SIZE, SIZE))
    pixels = gradient.load()
    for y in range(SIZE):
        t = y / (SIZE - 1)
        colour = tuple(round(a + (b - a) * t) for a, b in zip(TOP, BOTTOM)) + (255,)
        for x in range(SIZE):
            pixels[x, y] = colour
    tile = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    tile.paste(gradient, (0, 0), mask)
    canvas.alpha_composite(tile)

    layer = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    pen = ImageDraw.Draw(layer)
    # a fine inner rule, a seal's border
    inset = (SIZE - TILE) // 2 + 46
    pen.rounded_rectangle((inset, inset, SIZE - inset, SIZE - inset),
                          radius=RADIUS - 46, outline=CREAM + (90,), width=5)

    text = "A&R"
    # as large as fits inside the inner rule with room to spare
    for size in range(400, 100, -10):
        serif = ImageFont.truetype(str(FONTS / "EBGaramond.woff2"), size)
        serif.set_variation_by_name("SemiBold")
        box = pen.textbbox((0, 0), text, font=serif)
        if box[2] - box[0] <= TILE * 0.68:
            break
    width, height = box[2] - box[0], box[3] - box[1]
    x = (SIZE - width) / 2 - box[0]
    y = (SIZE - height) / 2 - box[1] - 30
    pen.text((x, y), text, font=serif, fill=CREAM + (255,))

    # a short rule under the letters, and the app's purpose in small capitals
    rule_y = y + box[3] + 52
    pen.line((SIZE / 2 - 120, rule_y, SIZE / 2 + 120, rule_y), fill=CREAM + (170,), width=5)
    sans = ImageFont.truetype(str(FONTS / "Montserrat.woff2"), 50)
    sans.set_variation_by_name("SemiBold")
    caption = "REDACT · ANONYMIZE"
    cbox = pen.textbbox((0, 0), caption, font=sans)
    pen.text(((SIZE - (cbox[2] - cbox[0])) / 2 - cbox[0], rule_y + 34), caption,
             font=sans, fill=CREAM + (200,))

    canvas.alpha_composite(layer)
    return canvas


def main() -> int:
    image = draw()
    image.save(HERE / "icon.png")
    image.save(HERE / "icon.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48),
                                         (64, 64), (128, 128), (256, 256)])
    if shutil.which("iconutil"):
        with tempfile.TemporaryDirectory() as tmp:
            iconset = Path(tmp) / "icon.iconset"
            iconset.mkdir()
            for points in (16, 32, 128, 256, 512):
                for scale in (1, 2):
                    pixels = points * scale
                    name = f"icon_{points}x{points}{'@2x' if scale == 2 else ''}.png"
                    image.resize((pixels, pixels), Image.LANCZOS).save(iconset / name)
            subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(HERE / "icon.icns")],
                           check=True)
    else:
        print("iconutil not found (not a Mac): icon.icns not rewritten", file=sys.stderr)
    print("wrote", ", ".join(p.name for p in (HERE / "icon.png", HERE / "icon.ico",
                                              HERE / "icon.icns") if p.exists()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
