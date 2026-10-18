"""
Generate ``assets/logo.png`` (256x256 RGBA) and ``assets/logo.ico``
(multi-resolution Windows icon) from the design in ``assets/logo.svg``:
a Poke Ball whose centre button is a bot visor with two eyes. Run from
the repo root:

    python scripts/gen_logo.py

Drawn at 4x and downscaled with LANCZOS for clean edges. The shapes are
deliberately few and thick so the mark still reads at 16 px.
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"

# Colours: the launcher palette (launcher.py)
INK = (15, 16, 18, 255)        # outline, band, visor
RED = (229, 72, 77, 255)       # top half and eyes
WHITE = (236, 237, 239, 255)   # bottom half
VISOR = (27, 29, 33, 255)      # visor face, a step above the ink
GLINT = (255, 255, 255, 64)

SS = 4                          # supersample factor
BASE = 256


def _draw() -> Image.Image:
    s = BASE * SS
    k = s / BASE                # 256-unit design grid -> pixels

    def box(x0, y0, x1, y1):
        return [round(x0 * k), round(y0 * k), round(x1 * k), round(y1 * k)]

    im = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.ellipse(box(10, 10, 246, 246), fill=INK)                 # outline
    d.pieslice(box(24, 24, 232, 232), 180, 360, fill=RED)      # top half
    d.pieslice(box(24, 24, 232, 232), 0, 180, fill=WHITE)      # bottom half

    glint = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    ImageDraw.Draw(glint).ellipse(box(56, 44, 112, 76), fill=GLINT)
    im = Image.alpha_composite(im, glint)
    d = ImageDraw.Draw(im)

    d.rectangle(box(16, 116, 240, 140), fill=INK)              # band
    d.rounded_rectangle(box(76, 96, 180, 160), radius=round(26 * k),
                        fill=INK)                              # visor rim
    d.rounded_rectangle(box(88, 108, 168, 148), radius=round(16 * k),
                        fill=VISOR)                            # visor face
    for cx in (110, 146):                                      # eyes
        d.ellipse(box(cx - 9, 119, cx + 9, 137), fill=RED)
    return im


def main() -> None:
    final = _draw().resize((BASE, BASE), Image.LANCZOS)
    png_path = ASSETS / "logo.png"
    ico_path = ASSETS / "logo.ico"
    final.save(png_path, "PNG")
    print(f"wrote {png_path}")
    ico_sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64),
                 (128, 128), (256, 256)]
    final.save(ico_path, format="ICO", sizes=ico_sizes)
    print(f"wrote {ico_path}  (sizes: "
          f"{', '.join(f'{w}x{h}' for w, h in ico_sizes)})")


if __name__ == "__main__":
    main()
