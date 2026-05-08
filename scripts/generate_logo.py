"""Generate the Fat-Pikachu logo at assets/pokeball.png.

Targets the classic Gen 1 chubby chibi pose: round body, lightning-bolt
tail flicked up-right, brown stripes on the back, stubby limbs. We
render at SIZE × SUPERSAMPLE px and LANCZOS-downsample to SIZE px so
the launcher can load it without runtime scaling.

(File is still named pokeball.png so launcher.py's asset path
doesn't need to change.)
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter

OUT = Path(__file__).resolve().parent.parent / "assets" / "pokeball.png"
SIZE = 96
SUPERSAMPLE = 8
S = SIZE * SUPERSAMPLE

YELLOW   = (255, 203, 5, 255)
BROWN    = (165, 95, 35, 255)
BROWN_D  = (110, 65, 25, 255)
INK      = (24, 22, 22, 255)
CHEEK    = (235, 50, 60, 255)
WHITE    = (255, 255, 255, 255)


def _stroke(d: ImageDraw.ImageDraw, pts, w):
    for i in range(len(pts)):
        d.line([pts[i], pts[(i + 1) % len(pts)]], fill=INK, width=w)


def main() -> None:
    canvas = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(canvas)

    out_w = max(2, int(S * 0.013))   # body outline thickness
    fine_w = max(2, int(S * 0.009))  # stripe / inner outline thickness

    # Body sits slightly left of centre to leave room for the tail.
    body_cx = int(S * 0.46)
    body_cy = int(S * 0.58)
    body_w  = int(S * 0.30)         # horizontal radius
    body_h  = int(S * 0.30)         # vertical radius

    # ── Tail — lightning-bolt zigzag, drawn first so the body overlaps
    # its base. Yellow body, brown wedge at the base, black outline.
    tail = [
        (body_cx + int(body_w * 0.55), body_cy - int(body_h * 0.35)),  # 0 attach upper
        (body_cx + int(body_w * 1.30), body_cy - int(body_h * 0.85)),  # 1 first elbow
        (body_cx + int(body_w * 0.95), body_cy - int(body_h * 1.05)),  # 2 inner notch
        (body_cx + int(body_w * 1.55), body_cy - int(body_h * 1.85)),  # 3 tip
        (body_cx + int(body_w * 1.20), body_cy - int(body_h * 1.85)),  # 4 tip-back
        (body_cx + int(body_w * 0.65), body_cy - int(body_h * 1.10)),  # 5 inner notch
        (body_cx + int(body_w * 0.85), body_cy - int(body_h * 0.70)),  # 6 second bend
        (body_cx + int(body_w * 0.40), body_cy - int(body_h * 0.10)),  # 7 attach lower
    ]
    d.polygon(tail, fill=YELLOW)
    _stroke(d, tail, out_w)

    # Brown wedge at the tail base (the "root" patch).
    base_wedge = [tail[0], tail[6], tail[7]]
    d.polygon(base_wedge, fill=BROWN)
    _stroke(d, base_wedge, fine_w)

    # ── Body — chubby ellipse.
    d.ellipse([body_cx - body_w, body_cy - body_h,
               body_cx + body_w, body_cy + body_h],
              fill=YELLOW, outline=INK, width=out_w)

    # ── Brown stripes on the back (right shoulder area, peeking around
    # the body). Two short bands stacked vertically with a thin gap.
    # We build a stripe layer and clip its alpha channel to the body
    # ellipse, then alpha_composite it OVER the canvas — paste-with-mask
    # would wipe the body fill with the layer's transparent pixels.
    stripe_layer = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    sl = ImageDraw.Draw(stripe_layer)
    sx0 = body_cx + int(body_w * 0.30)
    sy0 = body_cy - int(body_h * 0.55)
    bw  = int(body_w * 0.65)
    bh  = int(body_h * 0.10)
    sl.rectangle([sx0, sy0, sx0 + bw, sy0 + bh], fill=BROWN)
    sl.rectangle([sx0 + int(bw * 0.10), sy0 + int(bh * 1.9),
                  sx0 + bw + int(bw * 0.04),
                  sy0 + int(bh * 1.9) + bh], fill=BROWN)
    body_mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(body_mask).ellipse(
        [body_cx - body_w, body_cy - body_h,
         body_cx + body_w, body_cy + body_h], fill=255)
    stripe_alpha = stripe_layer.split()[-1]
    stripe_layer.putalpha(ImageChops.multiply(stripe_alpha, body_mask))
    canvas = Image.alpha_composite(canvas, stripe_layer)

    d = ImageDraw.Draw(canvas)

    # ── Ears — two long pointed shapes with black tips. Slight outward
    # tilt; left ear is flopped a bit to the left, right ear sticks
    # straight up. Drawn AFTER the body so their bases sit on top.
    def _ear(side: int, lean: float):
        # side: -1 left, +1 right.  lean: tip's horizontal offset (px).
        base_cx = body_cx + side * int(body_w * 0.55)
        base_y  = body_cy - int(body_h * 0.92)
        ew = int(body_w * 0.28)
        eh = int(body_h * 1.30)
        bl = (base_cx - ew // 2, base_y + int(ew * 0.10))
        br = (base_cx + ew // 2, base_y - int(ew * 0.05))
        tip = (base_cx + int(lean), base_y - eh)
        poly = [bl, br, tip]
        d.polygon(poly, fill=YELLOW)
        _stroke(d, poly, out_w)
        # Black tip — fill the upper ~45% with INK.
        def lerp(p0, p1, t):
            return (int(p0[0] + (p1[0] - p0[0]) * t),
                    int(p0[1] + (p1[1] - p0[1]) * t))
        t_tip = 0.45
        tip_l = lerp(tip, bl, t_tip)
        tip_r = lerp(tip, br, t_tip)
        d.polygon([tip, tip_l, tip_r], fill=INK)
        _stroke(d, [tip, tip_l, tip_r], fine_w)

    _ear(-1, lean=-int(body_w * 0.25))
    _ear(+1, lean=+int(body_w * 0.05))

    # ── Arms — short stubby ovals tucked against the body's sides.
    arm_w = int(body_w * 0.20)
    arm_h = int(body_h * 0.28)
    arm_y = body_cy + int(body_h * 0.05)
    for side in (-1, +1):
        ax = body_cx + side * (body_w - int(body_w * 0.05))
        d.ellipse([ax - arm_w, arm_y - arm_h // 2,
                   ax + arm_w, arm_y + arm_h // 2],
                  fill=YELLOW, outline=INK, width=fine_w)

    # ── Feet — two small ovals at the bottom with a brown bottom panel.
    foot_w = int(body_w * 0.40)
    foot_h = int(body_h * 0.22)
    foot_y = body_cy + body_h - int(body_h * 0.02)
    for side, dx in ((-1, int(body_w * 0.40)), (+1, int(body_w * 0.40))):
        fx = body_cx + side * dx
        d.ellipse([fx - foot_w // 2, foot_y - foot_h // 2,
                   fx + foot_w // 2, foot_y + foot_h // 2],
                  fill=YELLOW, outline=INK, width=fine_w)
        # Brown bottom of the foot.
        d.chord([fx - foot_w // 2, foot_y - foot_h // 2,
                 fx + foot_w // 2, foot_y + foot_h // 2],
                start=10, end=170, fill=BROWN, outline=INK, width=fine_w)

    # ── Face features ──────────────────────────────────────────────────
    # Eyes — round black with a small white catchlight.
    eye_y = body_cy - int(body_h * 0.40)
    eye_dx = int(body_w * 0.34)
    eye_r = int(body_w * 0.13)
    for side in (-1, +1):
        ex = body_cx + side * eye_dx
        d.ellipse([ex - eye_r, eye_y - eye_r,
                   ex + eye_r, eye_y + eye_r], fill=INK)
        hl_r = max(2, int(eye_r * 0.42))
        hx = ex + int(eye_r * 0.32)
        hy = eye_y - int(eye_r * 0.32)
        d.ellipse([hx - hl_r, hy - hl_r, hx + hl_r, hy + hl_r], fill=WHITE)

    # Red cheeks — sit lower & wider for the chubby look.
    cheek_r = int(body_w * 0.16)
    cheek_y = body_cy - int(body_h * 0.08)
    cheek_dx = int(body_w * 0.62)
    for side in (-1, +1):
        cx2 = body_cx + side * cheek_dx
        d.ellipse([cx2 - cheek_r, cheek_y - cheek_r,
                   cx2 + cheek_r, cheek_y + cheek_r],
                  fill=CHEEK, outline=INK, width=fine_w)

    # Nose — a tiny dot.
    nr = max(2, int(body_w * 0.025))
    d.ellipse([body_cx - nr, body_cy - int(body_h * 0.18) - nr,
               body_cx + nr, body_cy - int(body_h * 0.18) + nr],
              fill=INK)

    # Smile — a small "w" formed by two arcs.
    mouth_w = int(body_w * 0.22)
    mouth_y = body_cy - int(body_h * 0.08)
    mouth_h = int(body_h * 0.16)
    smile_w = max(2, int(S * 0.009))
    d.arc([body_cx - mouth_w, mouth_y - mouth_h // 2,
           body_cx,           mouth_y + mouth_h // 2],
          start=20, end=160, fill=INK, width=smile_w)
    d.arc([body_cx,           mouth_y - mouth_h // 2,
           body_cx + mouth_w, mouth_y + mouth_h // 2],
          start=20, end=160, fill=INK, width=smile_w)

    # ── Soft drop shadow under the feet.
    shadow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    sd.ellipse([body_cx - int(body_w * 0.95),
                body_cy + body_h + int(body_h * 0.02),
                body_cx + int(body_w * 0.95),
                body_cy + body_h + int(body_h * 0.18)],
               fill=(0, 0, 0, 110))
    shadow = shadow.filter(ImageFilter.GaussianBlur(radius=S * 0.014))
    canvas = Image.alpha_composite(shadow, canvas)

    final = canvas.resize((SIZE, SIZE), Image.LANCZOS)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    final.save(OUT, format="PNG")
    print(f"wrote {OUT} ({SIZE}x{SIZE}, internal {S}x{S})")


if __name__ == "__main__":
    main()
