#!/usr/bin/env python3
"""Store / social image (assets/store.png, 1280x640): the dark end of a grey
ramp as the panel shows it inside the bad band, and as OLED Tint shows it.
The 'before' row is modelled on the measured pedestal at backlight 177
(code 2 gives 28% G, 12% B, 9% R of code 32's light), drawn a little
stronger than reality so it reads on a normal monitor."""
import os
from PIL import Image, ImageDraw, ImageFont

W, H = 1280, 640
BG = (11, 11, 13)
FONTS = "/usr/share/fonts/google-noto"
bold = ImageFont.truetype(f"{FONTS}/NotoSans-Bold.ttf", 76)
reg = ImageFont.truetype(f"{FONTS}/NotoSans-Regular.ttf", 30)
small = ImageFont.truetype(f"{FONTS}/NotoSans-Medium.ttf", 24)

LEVELS = [0, 2, 4, 6, 8, 12, 16, 24, 32, 48, 64]  # 8-bit codes, dark end
PED = (0.09, 0.28, 0.12)  # R, G, B pedestal as a share of code 32's light


def lin(v):  # gamma 2.2, 0..1
    return (v / 255) ** 2.2


def enc(x):
    return round(255 * min(1.0, max(0.0, x)) ** (1 / 2.2))


def after(v):
    return (enc(lin(v) * 2.2),) * 3  # brightened for the image, neutral


def before(v):
    if v == 0:
        return (0, 0, 0)
    ref = lin(32) * 2.2
    # Pedestal plus a flattened rise: the darks glow and lean green.
    return tuple(enc(p * ref * 1.6 + lin(v) * 2.2 * 0.75) for p in PED)


img = Image.new("RGB", (W, H), BG)
d = ImageDraw.Draw(img)
d.text((80, 62), "OLED Tint", font=bold, fill=(240, 240, 240))
d.text((84, 160), "Clean dark greys at low brightness on the AYN Odin 2 Portal",
       font=reg, fill=(170, 170, 176))

x0, pw, gap, ph = 80, 92, 10, 120
for row, (label, fn, y) in enumerate(
        [("Panel at ~20% brightness", before, 270),
         ("With OLED Tint", after, 450)]):
    d.text((x0, y - 42), label, font=small,
           fill=(150, 150, 156) if row == 0 else (220, 220, 224))
    for i, v in enumerate(LEVELS):
        x = x0 + i * (pw + gap)
        d.rounded_rectangle((x, y, x + pw, y + ph), 10, fill=fn(v),
                            outline=(38, 38, 42), width=2)

out = os.path.join(os.path.dirname(__file__), "..", "assets", "store.png")
img.save(out, optimize=True)
print(os.path.abspath(out))
