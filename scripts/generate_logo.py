"""Generate the Pokéball logo at assets/pokeball.png.

Renders at 4x then downsamples for clean anti-aliased edges. The
launcher subsamples 3x at runtime, so a 128x128 source becomes a
crisp ~43px header glyph. Run once, then commit the PNG.
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

OUT = Path(__file__).resolve().parent.parent / "assets" / "pokeball.png"
SIZE = 128
SUPERSAMPLE = 4
S = SIZE * SUPERSAMPLE

WHITE = (248, 248, 250, 255)
RED   = (220, 30, 35, 255)
INK   = (22, 22, 24, 255)


def main() -> None:
    canvas = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(canvas)

    pad = int(S * 0.06)
    a, b = pad, S - pad
    cx, cy = S // 2, S // 2
    r = (b - a) // 2

    band_h    = max(2, int(S * 0.085))
    btn_outer = int(r * 0.30)
    btn_mid   = int(r * 0.225)
    btn_inner = int(r * 0.115)
    outline_w = max(2, int(S * 0.014))

    # White full ball.
    d.ellipse([a, a, b, b], fill=WHITE)
    # Red top half. PIL pieslice angles measure clockwise from 3 o'clock,
    # so 180→360 sweeps left → top → right = the upper hemisphere.
    d.pieslice([a, a, b, b], start=180, end=360, fill=RED)

    # Equator band — paste solid INK using a mask of (ball ∩ horizontal stripe)
    # so only the band pixels are replaced.
    band_mask = Image.new("L", (S, S), 0)
    md = ImageDraw.Draw(band_mask)
    md.ellipse([a, a, b, b], fill=255)
    md.rectangle([0, 0, S, cy - band_h // 2 - 1], fill=0)
    md.rectangle([0, cy + band_h // 2 + 1, S, S], fill=0)
    black_layer = Image.new("RGBA", (S, S), INK)
    canvas.paste(black_layer, (0, 0), band_mask)

    d = ImageDraw.Draw(canvas)
    # Outer outline.
    d.ellipse([a, a, b, b], outline=INK, width=outline_w)

    # Center button — three concentric circles.
    d.ellipse([cx - btn_outer, cy - btn_outer,
               cx + btn_outer, cy + btn_outer], fill=INK)
    d.ellipse([cx - btn_mid, cy - btn_mid,
               cx + btn_mid, cy + btn_mid], fill=WHITE)
    d.ellipse([cx - btn_inner, cy - btn_inner,
               cx + btn_inner, cy + btn_inner],
              fill=(220, 220, 224, 255))

    # Subtle highlight on the upper-left of the red half. Compose via
    # alpha_composite so the existing pixels are preserved (paste-with-mask
    # would replace them with the highlight layer's transparent zones).
    hl_w = int(r * 0.55)
    hl_h = int(r * 0.22)
    hl_x = cx - int(r * 0.42)
    hl_y = cy - int(r * 0.62)
    highlight = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(highlight).ellipse(
        [hl_x, hl_y, hl_x + hl_w, hl_y + hl_h],
        fill=(255, 255, 255, 110))
    highlight = highlight.filter(ImageFilter.GaussianBlur(radius=S * 0.012))
    canvas = Image.alpha_composite(canvas, highlight)

    final = canvas.resize((SIZE, SIZE), Image.LANCZOS)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    final.save(OUT, format="PNG")
    print(f"wrote {OUT} ({SIZE}x{SIZE})")


if __name__ == "__main__":
    main()
