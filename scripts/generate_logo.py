"""Generate the Pikachu-face logo at assets/pokeball.png.

We render at SIZE × SUPERSAMPLE pixels then LANCZOS-downsample to
SIZE px. tkinter's `PhotoImage.subsample()` is nearest-neighbour,
which destroys anti-aliasing — so we pre-render the asset at the
exact display size and let the launcher load it without scaling.

(File is still named pokeball.png so launcher.py's asset path
doesn't need to update.)
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

OUT = Path(__file__).resolve().parent.parent / "assets" / "pokeball.png"
SIZE = 72                      # display size in the launcher header
SUPERSAMPLE = 8                # 576px internal → LANCZOS to 72px
S = SIZE * SUPERSAMPLE

YELLOW    = (255, 203, 5, 255)     # canonical Pikachu yellow
INK       = (24, 22, 22, 255)      # near-black outline / eyes
CHEEK     = (235, 50, 60, 255)     # red cheek
NOSE      = (40, 35, 35, 255)
MOUTH     = (40, 35, 35, 255)
WHITE     = (255, 255, 255, 255)


def main() -> None:
    canvas = Image.new("RGBA", (S, S), (0, 0, 0, 0))

    cx, cy = S // 2, S // 2
    face_r = int(S * 0.34)
    ear_w  = int(face_r * 0.55)
    ear_h  = int(face_r * 1.05)

    outline_w = max(2, int(S * 0.012))

    # ── Ears ────────────────────────────────────────────────────────────────
    ear_offset_x = int(face_r * 0.55)
    ear_base_y   = cy - int(face_r * 0.55)

    def _draw_ear(side: int):
        base_cx = cx + side * ear_offset_x
        tilt    = side * int(ear_w * 0.55)
        base_left  = (base_cx - ear_w // 2, ear_base_y)
        base_right = (base_cx + ear_w // 2, ear_base_y - int(ear_w * 0.15))
        tip        = (base_cx + tilt, ear_base_y - ear_h)
        ear_poly = [base_left, base_right, tip]
        d = ImageDraw.Draw(canvas)
        d.polygon(ear_poly, fill=YELLOW, outline=INK)
        for i in range(3):
            p1 = ear_poly[i]
            p2 = ear_poly[(i + 1) % 3]
            d.line([p1, p2], fill=INK, width=outline_w)

        def lerp(p0, p1, t):
            return (int(p0[0] + (p1[0] - p0[0]) * t),
                    int(p0[1] + (p1[1] - p0[1]) * t))
        t_tip = 0.42
        tip_left  = lerp(tip, base_left,  t_tip)
        tip_right = lerp(tip, base_right, t_tip)
        d.polygon([tip, tip_left, tip_right], fill=INK, outline=INK)

    _draw_ear(-1)
    _draw_ear(+1)

    # ── Face ────────────────────────────────────────────────────────────────
    d = ImageDraw.Draw(canvas)
    d.ellipse([cx - face_r, cy - face_r, cx + face_r, cy + face_r],
              fill=YELLOW, outline=INK, width=outline_w)
    d.ellipse([cx - face_r, cy - face_r, cx + face_r, cy + face_r],
              outline=INK, width=outline_w)

    # ── Cheeks ─────────────────────────────────────────────────────────────
    cheek_r = int(face_r * 0.20)
    cheek_y = cy + int(face_r * 0.12)
    cheek_dx = int(face_r * 0.62)
    for sign in (-1, +1):
        x = cx + sign * cheek_dx
        d.ellipse([x - cheek_r, cheek_y - cheek_r,
                   x + cheek_r, cheek_y + cheek_r],
                  fill=CHEEK, outline=INK, width=max(1, outline_w // 2))

    # ── Eyes ──────────────────────────────────────────────────────────────
    eye_r = int(face_r * 0.105)
    eye_y = cy - int(face_r * 0.20)
    eye_dx = int(face_r * 0.36)
    for sign in (-1, +1):
        x = cx + sign * eye_dx
        d.ellipse([x - eye_r, eye_y - eye_r, x + eye_r, eye_y + eye_r],
                  fill=INK)
        hl_r = max(2, int(eye_r * 0.40))
        hx = x + int(eye_r * 0.30)
        hy = eye_y - int(eye_r * 0.30)
        d.ellipse([hx - hl_r, hy - hl_r, hx + hl_r, hy + hl_r],
                  fill=WHITE)

    # ── Nose ──────────────────────────────────────────────────────────────
    nose_w = int(face_r * 0.06)
    nose_h = int(face_r * 0.05)
    nose_y = cy - int(face_r * 0.04)
    d.ellipse([cx - nose_w, nose_y - nose_h,
               cx + nose_w, nose_y + nose_h],
              fill=NOSE)

    # ── Smile ─────────────────────────────────────────────────────────────
    mouth_w = int(face_r * 0.32)
    mouth_y = cy + int(face_r * 0.05)
    mouth_h = int(face_r * 0.18)
    stroke = max(2, int(S * 0.010))
    d.arc([cx - mouth_w, mouth_y - mouth_h // 2,
           cx,           mouth_y + mouth_h],
          start=20, end=160, fill=MOUTH, width=stroke)
    d.arc([cx,           mouth_y - mouth_h // 2,
           cx + mouth_w, mouth_y + mouth_h],
          start=20, end=160, fill=MOUTH, width=stroke)

    # ── Soft drop shadow ──────────────────────────────────────────────────
    shadow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    sh_off = int(S * 0.015)
    sd.ellipse([cx - face_r + sh_off, cy - face_r + sh_off * 2,
                cx + face_r + sh_off, cy + face_r + sh_off * 2],
               fill=(0, 0, 0, 80))
    shadow = shadow.filter(ImageFilter.GaussianBlur(radius=S * 0.014))
    canvas = Image.alpha_composite(shadow, canvas)

    final = canvas.resize((SIZE, SIZE), Image.LANCZOS)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    final.save(OUT, format="PNG")
    print(f"wrote {OUT} ({SIZE}x{SIZE}, internal {S}x{S})")


if __name__ == "__main__":
    main()
