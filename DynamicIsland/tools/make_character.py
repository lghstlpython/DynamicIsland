"""
Builds the animation frames + mouth shapes for the chat character.
Needs: numpy, opencv-python, pillow.   Usage:  python make_character.py picture.jpg
Output goes to ../character/ (frame_00.png ..., mouth_0.png ..., meta.json).
You do NOT need to run this to build the island: the generated files are already included.
"""
import json
import os
import sys

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFilter

N_FRAMES = 40            # frames in one seamless loop
LOOP_SECONDS = 4.0       # one loop = one slow breath + one hair sway
OUT_W, OUT_H = 328, 584  # frame size (2x of the 164x292 logical card)
MOUTH_SRC = (328, 388)   # mouth centre in the source picture (x, y)
PATCH_W, PATCH_H = 44, 30

HAIR_AMP = 8.0           # source pixels
BREATH_AMP = 7.0


def smoothstep(a, b, v):
    t = np.clip((v - a) / (b - a), 0, 1)
    return t * t * (3 - 2 * t)


def build_fields(src):
    h, w = src.shape
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)

    # ---- hair weight: dark pixels at the left / right hair locks, fading in towards the tips
    dark = (src < 110).astype(np.float32)
    side = ((xx < 262) | (xx > 428)) & (yy > 230) & (yy < 640)
    m = cv2.GaussianBlur(dark * side, (0, 0), 10)
    m = np.clip(m * 2.6, 0, 1)
    ramp = smoothstep(250, 470, yy) * (1 - smoothstep(500, 570, yy))
    hair_w = cv2.GaussianBlur(m * ramp, (0, 0), 6).astype(np.float32)

    # ---- breathing weight: shoulders and upper torso rise a little, fading out at the waist
    body_w = (smoothstep(440, 740, yy) * (1 - smoothstep(900, 1100, yy))).astype(np.float32)
    return xx, yy, hair_w, body_w


def make_frames(src, outdir):
    xx, yy, hair_w, body_w = build_fields(src)
    side_phase = 0.9 * (xx > 342)
    for k in range(N_FRAMES):
        phi = 2 * np.pi * k / N_FRAMES
        ph = phi + side_phase
        wave = np.sin(ph - yy / 90.0) + 0.35 * np.sin(2 * ph - yy / 60.0 + 1.3)
        dx = HAIR_AMP * hair_w * wave
        dy = 0.2 * HAIR_AMP * hair_w * np.sin(ph - yy / 90.0 + 1.0)
        b = np.sin(phi - 0.6)
        dy += -BREATH_AMP * body_w * b
        dx += (xx - 368) * 0.005 * body_w * b
        out = cv2.remap(src, (xx - dx).astype(np.float32), (yy - dy).astype(np.float32),
                        cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)
        out = cv2.resize(out, (OUT_W, OUT_H), interpolation=cv2.INTER_AREA)
        cv2.imwrite(os.path.join(outdir, f"frame_{k:02d}.png"), out)


def make_mouths(src, outdir):
    sx, sy = OUT_W / src.shape[1], OUT_H / src.shape[0]
    cx, cy = round(MOUTH_SRC[0] * sx), round(MOUTH_SRC[1] * sy)

    # paper colour around the mouth (so the cover patch is invisible)
    ring = src[MOUTH_SRC[1] - 40:MOUTH_SRC[1] + 40, MOUTH_SRC[0] - 60:MOUTH_SRC[0] + 60]
    paper = int(np.percentile(ring, 90))

    SS = 8
    W, H = PATCH_W * SS, PATCH_H * SS
    mid = (W / 2, H / 2)
    ink, inside, tongue, teeth = 20, 55, 135, 245

    def px(v):
        return v * SS

    for state in range(5):
        img = Image.new("L", (W, H), paper)
        a = Image.new("L", (W, H), 0)
        # cover patch: soft ellipse over the original mouth
        ImageDraw.Draw(a).ellipse([mid[0] - px(14.5), mid[1] - px(8.5),
                                   mid[0] + px(14.5), mid[1] + px(8.5)], fill=255)
        d = ImageDraw.Draw(img)

        def mouth(w, h, tongue_h=0.0, teeth_h=0.0):
            box = [mid[0] - px(w / 2), mid[1] - px(h / 2),
                   mid[0] + px(w / 2), mid[1] + px(h / 2)]
            d.ellipse(box, fill=ink)
            t = px(1.0)
            inner = [box[0] + t, box[1] + t, box[2] - t, box[3] - t]
            layer = Image.new("L", (W, H), inside)
            ld = ImageDraw.Draw(layer)
            if teeth_h:
                ld.rectangle([0, inner[1], W, inner[1] + px(teeth_h)], fill=teeth)
            if tongue_h:
                ld.ellipse([mid[0] - px(w * 0.3), inner[3] - px(tongue_h),
                            mid[0] + px(w * 0.3), inner[3] + px(tongue_h * 0.8)], fill=tongue)
            clip = Image.new("L", (W, H), 0)
            ImageDraw.Draw(clip).ellipse(inner, fill=255)
            img.paste(layer, (0, 0), clip)

        if state == 0:      # closed: a calm, slightly lowered line
            d.line([(mid[0] - px(8.5), mid[1] - px(0.2)), (mid[0], mid[1] + px(0.9)),
                    (mid[0] + px(8.5), mid[1] - px(0.4))], fill=ink, width=int(px(1.5)), joint="curve")
            d.ellipse([mid[0] - px(9.6), mid[1] - px(1.3), mid[0] - px(7.6), mid[1] + px(0.9)], fill=ink)
        elif state == 1:    # barely open (consonants)
            mouth(17, 3.4)
        elif state == 2:    # wide (e / i)
            mouth(21, 6.0, tongue_h=1.6, teeth_h=0.9)
        elif state == 3:    # round (o / u)
            mouth(11, 10.0, tongue_h=2.8)
        else:               # big open (a)
            mouth(20.5, 9.6, tongue_h=3.2, teeth_h=1.3)

        a = a.filter(ImageFilter.GaussianBlur(SS * 1.0))
        # ink strokes must stay fully opaque: union with a mask of everything darker than paper
        dark = img.point(lambda v: 255 if v < paper - 25 else 0).filter(ImageFilter.GaussianBlur(SS * 0.4))
        alpha = Image.fromarray(np.maximum(np.asarray(a), np.asarray(dark)))
        rgba = Image.merge("LA", (img, alpha)).resize((PATCH_W, PATCH_H), Image.LANCZOS)
        rgba.save(os.path.join(outdir, f"mouth_{state}.png"))
    return cx, cy


def main():
    path = sys.argv[1]
    outdir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "character")
    os.makedirs(outdir, exist_ok=True)
    src = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
    make_frames(src, outdir)
    cx, cy = make_mouths(src, outdir)
    meta = {"frames": N_FRAMES, "loop_seconds": LOOP_SECONDS, "width": OUT_W, "height": OUT_H,
            "mouth_x": cx, "mouth_y": cy, "patch_w": PATCH_W, "patch_h": PATCH_H, "mouth_states": 5}
    with open(os.path.join(outdir, "meta.json"), "w") as f:
        json.dump(meta, f, indent=2)
    print("done", meta)


if __name__ == "__main__":
    main()
