"""
Generate ``assets/logo.png`` (256×256 RGBA) and ``assets/logo.ico``
(multi-resolution Windows icon) from the same Pokéball + circuit-board
design as ``assets/logo.svg``. Run from repo root:

    python scripts/gen_logo.py

Rendered at 4× supersample and downscaled LANCZOS for clean edges.
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"

# Colors — match logo.svg exactly
RED        = (220, 60, 60, 255)
RED_TRACE  = (126, 30, 30, 255)
WHITE      = (240, 240, 245, 255)
SLATE      = (70, 80, 112, 255)
DARK       = (30, 30, 40, 255)


def _draw(size: int) -> Image.Image:
    """Render the logo at the supersampled size; caller downscales."""
    s = size
    cx = cy = s / 2.0
    r = s * 0.453        # 116/256
    eq_h = s * 0.0625    # 16/256
    ring_w = s * 0.03125 # 8/256
    trace_w = max(1, int(round(s * 0.0117)))   # 3/256
    pad_r = s * 0.0156   # 4/256

    canvas = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(canvas)

    # Fill top half red, bottom half white over the whole canvas first,
    # then mask to a circle below — same approach as the clipPath in
    # the SVG, just done by alpha-compositing.
    body = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    bd = ImageDraw.Draw(body)
    bd.rectangle([0, 0, s, s / 2], fill=RED)
    bd.rectangle([0, s / 2, s, s], fill=WHITE)

    def _trace_set(stroke_rgba, ys_top, ys_bot, color_for_pads):
        """Draw the vertical + horizontal traces and their endpoint
        pads for ONE half. ys_top/ys_bot define the y-band the lines
        live in."""
        sw = trace_w
        # 3 vertical traces near columns 76,128,180 (scaled)
        for col_frac in (76, 128, 180):
            x = col_frac / 256.0 * s
            bd.line([(x, ys_top), (x, ys_bot)], fill=stroke_rgba, width=sw)
        # 4 horizontal traces with central gaps (so they don't cross
        # the centre button), at two y rows
        y_rows = (ys_top + (ys_bot - ys_top) * 0.30,
                  ys_top + (ys_bot - ys_top) * 0.65)
        for y in y_rows:
            bd.line([(s * 0.188, y), (s * 0.406, y)],
                    fill=stroke_rgba, width=sw)
            bd.line([(s * 0.594, y), (s * 0.812, y)],
                    fill=stroke_rgba, width=sw)
        # Endpoint pads (slightly bigger than the line so they read as nodes)
        pads = [(s * 0.188, y_rows[0]), (s * 0.812, y_rows[0]),
                (s * 0.188, y_rows[1]), (s * 0.812, y_rows[1])]
        # Plus column-end pads (top or bottom of vertical traces)
        if ys_top < s / 2:                       # upper half
            cap_y = ys_top
        else:
            cap_y = ys_bot
        for col_frac in (76, 128, 180):
            pads.append((col_frac / 256.0 * s, cap_y))
        for px, py in pads:
            bd.ellipse([px - pad_r, py - pad_r, px + pad_r, py + pad_r],
                       fill=color_for_pads)

    # Top traces
    _trace_set(RED_TRACE,
               ys_top=s * (36 / 256.0), ys_bot=s * (100 / 256.0),
               color_for_pads=RED_TRACE)
    # Bottom traces
    _trace_set(SLATE,
               ys_top=s * (156 / 256.0), ys_bot=s * (220 / 256.0),
               color_for_pads=SLATE)

    # Equator band (dark)
    bd.rectangle([0, cy - eq_h / 2, s, cy + eq_h / 2], fill=DARK)

    # Mask to a circle, then composite onto canvas
    mask = Image.new("L", (s, s), 0)
    ImageDraw.Draw(mask).ellipse([cx - r, cy - r, cx + r, cy + r], fill=255)
    canvas.paste(body, (0, 0), mask=mask)

    # Outer ring
    d.ellipse([cx - r, cy - r, cx + r, cy + r],
              outline=DARK, width=max(1, int(round(ring_w))))

    # Center button: dark outer ring, white inner with thin dark stroke
    btn_outer_r = s * 0.086    # 22/256
    btn_inner_r = s * 0.0508   # 13/256
    d.ellipse([cx - btn_outer_r, cy - btn_outer_r,
               cx + btn_outer_r, cy + btn_outer_r], fill=DARK)
    d.ellipse([cx - btn_inner_r, cy - btn_inner_r,
               cx + btn_inner_r, cy + btn_inner_r],
              fill=WHITE, outline=DARK,
              width=max(1, int(round(s * 0.0078))))

    return canvas


def main() -> None:
    SUPER = 1024              # supersample factor for AA
    OUT_PNG = 256
    big = _draw(SUPER)
    final = big.resize((OUT_PNG, OUT_PNG), Image.LANCZOS)

    ASSETS.mkdir(parents=True, exist_ok=True)
    png_path = ASSETS / "logo.png"
    ico_path = ASSETS / "logo.ico"

    final.save(png_path, "PNG")
    print(f"wrote {png_path}  ({OUT_PNG}×{OUT_PNG})")

    # .ico needs multiple resolutions embedded; let Pillow downscale.
    ico_sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64),
                 (128, 128), (256, 256)]
    final.save(ico_path, format="ICO", sizes=ico_sizes)
    print(f"wrote {ico_path}  (sizes: "
          f"{', '.join(f'{w}x{h}' for w, h in ico_sizes)})")


if __name__ == "__main__":
    main()
