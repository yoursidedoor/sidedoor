"""Rebuild the Your Sidedoor brand files in assets/brand/ (variant C, "Bold tile", owner 2026-10-24).

    <venv with Pillow and cairosvg>/bin/python tools/make_brand.py [--out assets/brand]

A developer tool, macOS only (the wordmark uses the system San Francisco font); the product only reads
the PNGs it writes. The mark is an outline-free dimensional cube: a closed side door with a red
no-entry sign on the left face, the right face's door open with a warm glow, an orange arrow running
under the cube to it. All geometry is in a 512-unit mark space. SVG is rendered with cairosvg; the soft
shadows (which cairosvg cannot blur) are separate layers blurred and composited with Pillow.

Files (every size is drawn for its own pixels, nothing is upscaled):
    yoursidedoor-icon.svg           the macOS app icon as vector source (shadows as SVG filters)
    yoursidedoor-mark.svg           the mark alone, no tile
    yoursidedoor-icon-1024.png      macOS app icon master: Apple's grid, 824 px tile in 1024, soft shadow
    yoursidedoor-icon-16.png, -32   macOS simplified art (bigger sign, thicker arrow, closed doors)
    yoursidedoor-icon-win-256.png   Windows master: the tile fills 984 of 1024 (no macOS shadow margin)
    yoursidedoor-icon-win-16/20/24/32.png   Windows simplified art (also the dashboard favicons)
    yoursidedoor-apple-touch-180.png        a full-bleed square for iOS/iPadOS home screens (it masks it)
    yoursidedoor-tray-template.png  44 px black-and-alpha menu-bar glyph (macOS template image, 22 pt)
    yoursidedoor-logo.png           the tile as a badge next to "Your Sidedoor", dark text (light pages)
    yoursidedoor-logo-on-dark.png   the same with light text (dark pages)
"""

import argparse
import io
import math
import os
from pathlib import Path

import cairosvg
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

REPO = Path(__file__).resolve().parent.parent
FONT = "/System/Library/Fonts/SFNS.ttf"

# ---------------------------------------------------------------- geometry (mark space)
A, B, H = 160.0, 80.0, 190.0
T = (256, 25); L = (96, 105); C = (256, 185); R = (416, 105)
Lb = (96, 295); Cb = (256, 375); Rb = (416, 295); Bk = (256, 215)
P = (A, B)      # along the left face (and the right face's outward normal)
Q = (A, -B)     # along the right face


def lf(u, v):  # left face local -> mark space
    return (96 + A * u, 105 + B * u + H * v)


def rf(u, v):  # right face local -> mark space
    return (256 + A * u, 185 - B * u + H * v)


def add(a, b, k=1.0):
    return (a[0] + k * b[0], a[1] + k * b[1])


def pts(*ps):
    return " ".join(f"{x:.2f},{y:.2f}" for x, y in ps)


def poly(ps, **attrs):
    a = " ".join(f'{k.replace("_", "-")}="{v}"' for k, v in attrs.items())
    return f'<polygon points="{pts(*ps)}" {a}/>'


LD = dict(u0=0.25, u1=0.70, v0=0.40)   # left (closed) door
RD = dict(u0=0.30, u1=0.72, v0=0.42)   # right (open) door
SIGN = lf(0.475, 0.585)
THETA = math.radians(108)


def door_poly(face, d):
    return [face(d["u0"], d["v0"]), face(d["u1"], d["v0"]), face(d["u1"], 1), face(d["u0"], 1)]


def leaf_poly():
    w = RD["u1"] - RD["u0"]
    ht, hb = rf(RD["u1"], RD["v0"]), rf(RD["u1"], 1)
    d = (w * (-math.cos(THETA) * Q[0] + math.sin(THETA) * P[0]),
         w * (-math.cos(THETA) * Q[1] + math.sin(THETA) * P[1]))
    return [ht, add(ht, d), add(hb, d), hb]


ARROW_P0, ARROW_C1, ARROW_C2, ARROW_P3 = (92, 398), (158, 448), (268, 456), (336, 386)


def arrow_svg(fill, width, head=1.0):
    tx, ty = ARROW_P3[0] - ARROW_C2[0], ARROW_P3[1] - ARROW_C2[1]
    n = math.hypot(tx, ty); tx, ty = tx / n, ty / n
    nx, ny = -ty, tx
    base = add(ARROW_P3, (tx, ty), -4)
    tip = add(base, (tx, ty), 40 * head)
    b1 = add(base, (nx, ny), 33 * head); b2 = add(base, (nx, ny), -33 * head)
    back = add(base, (tx, ty), -2)
    d = (f"M{ARROW_P0[0]},{ARROW_P0[1]} C{ARROW_C1[0]},{ARROW_C1[1]} {ARROW_C2[0]},{ARROW_C2[1]} "
         f"{ARROW_P3[0]},{ARROW_P3[1]}")
    shaft = f'<path d="{d}" fill="none" stroke="{fill}" stroke-width="{width}" stroke-linecap="round"/>'
    headp = (f'<polygon points="{pts(tip, b1, back, b2)}" fill="{fill}" stroke="{fill}" '
             f'stroke-width="{max(6, width * 0.42):.1f}" stroke-linejoin="round"/>')
    return shaft + headp


# ---------------------------------------------------------------- colours (variant C)
TOP = ("#FFFFFF", "#F7F2EA"); LEFT = ("#F1E9DC", "#DCCFBC"); RIGHT = ("#CDBDA6", "#AC9A82")
LDOOR = ("#D7C9B4", "#C2B39B"); ORANGE = ("#DA7039", "#A2461D"); ARROW = ("#F08A4B", "#C25526")
RED = ("#F24A3E", "#B91C22"); INTERIOR = ("#FFE6B5", "#F5A95C")
TILE_COLOURS = ("#3D3631", "#171412")


def lin(id_, c0, c1, p0, p1):
    return (f'<linearGradient id="{id_}" gradientUnits="userSpaceOnUse" x1="{p0[0]}" y1="{p0[1]}" '
            f'x2="{p1[0]}" y2="{p1[1]}"><stop offset="0" stop-color="{c0}"/>'
            f'<stop offset="1" stop-color="{c1}"/></linearGradient>')


def mark_body(small=False, pfx="m"):
    """(defs, body) SVG in mark space, no shadows. `small` is the simplified art for 16 and 32 px."""
    g = lambda n: f"{pfx}{n}"
    di = door_poly(rf, RD)
    defs = [
        lin(g("top"), *TOP, (150, 60), (330, 170)),
        lin(g("left"), *LEFT, (96, 120), (230, 360)),
        lin(g("right"), *RIGHT, (270, 150), (420, 330)),
        lin(g("ld"), *LDOOR, lf(LD["u0"], LD["v0"]), lf(LD["u1"], 1)),
        lin(g("or"), *ORANGE, (380, 210), (400, 370)),
        lin(g("ar"), *ARROW, (90, 400), (340, 400)),
        lin(g("in"), INTERIOR[1], INTERIOR[0], di[0], di[3]),
        f'<radialGradient id="{g("red")}" cx="{SIGN[0] - 9}" cy="{SIGN[1] - 11}" r="40" '
        f'gradientUnits="userSpaceOnUse"><stop offset="0" stop-color="{RED[0]}"/>'
        f'<stop offset="1" stop-color="{RED[1]}"/></radialGradient>',
        f'<linearGradient id="{g("gloss")}" gradientUnits="userSpaceOnUse" x1="0" y1="{SIGN[1] - 30}" '
        f'x2="0" y2="{SIGN[1] + 2}"><stop offset="0" stop-color="#FFFFFF" stop-opacity="0.75"/>'
        f'<stop offset="1" stop-color="#FFFFFF" stop-opacity="0"/></linearGradient>',
        f'<linearGradient id="{g("ao")}" gradientUnits="userSpaceOnUse" x1="0" y1="250" x2="0" y2="380">'
        f'<stop offset="0" stop-color="#3A2A1A" stop-opacity="0"/>'
        f'<stop offset="1" stop-color="#3A2A1A" stop-opacity="0.22"/></linearGradient>',
    ]
    body = [
        poly([T, R, C, L], fill=f"url(#{g('top')})"),
        poly([L, C, Cb, Lb], fill=f"url(#{g('left')})"),
        poly([C, R, Rb, Cb], fill=f"url(#{g('right')})"),
        poly([L, C, Cb, Lb], fill=f"url(#{g('ao')})"),
        poly([C, R, Rb, Cb], fill=f"url(#{g('ao')})", opacity="0.8"),
        # bevel highlights on the lit edges (no outlines)
        f'<polyline points="{pts(L, C, R)}" fill="none" stroke="#FFFFFF" stroke-width="3" '
        f'stroke-linejoin="round" opacity="0.95"/>',
        f'<polyline points="{pts(C, Cb)}" fill="none" stroke="#FFFFFF" stroke-width="2.2" opacity="0.55"/>',
        f'<polyline points="{pts(L, T, R)}" fill="none" stroke="#FFFFFF" stroke-width="2.4" '
        f'stroke-linejoin="round" opacity="0.9"/>',
    ]
    ld = door_poly(lf, LD)
    body.append(poly(ld, fill=f"url(#{g('ld')})"))
    body.append(poly([ld[0], ld[1], add(ld[1], (0, 9)), add(ld[0], (0, 9))], fill="#3A2A1A", opacity="0.22"))
    body.append(poly([ld[0], add(ld[0], (8, 4)), add(ld[3], (8, 4)), ld[3]], fill="#3A2A1A", opacity="0.16"))
    body.append(f'<line x1="{ld[1][0]}" y1="{ld[1][1]}" x2="{ld[2][0]}" y2="{ld[2][1]}" '
                f'stroke="#FFFFFF" stroke-width="2.4" opacity="0.8"/>')
    if small:
        body.append(poly(di, fill=f"url(#{g('or')})"))
    else:
        body.append(poly(di, fill=f"url(#{g('in')})"))
        body.append(poly([di[0], add(di[0], (10, 8)), add(di[3], (10, 2)), di[3]], fill="#C9782F", opacity="0.55"))
        leaf = leaf_poly()
        body.append(poly(leaf, fill=f"url(#{g('or')})"))
        body.append(f'<line x1="{leaf[1][0]:.1f}" y1="{leaf[1][1]:.1f}" x2="{leaf[2][0]:.1f}" '
                    f'y2="{leaf[2][1]:.1f}" stroke="#7E3112" stroke-width="3" opacity="0.7"/>')
        hx, hy = add(leaf[1], (leaf[0][0] - leaf[1][0], leaf[0][1] - leaf[1][1]), 0.22)
        body.append(f'<circle cx="{hx:.1f}" cy="{hy + 62:.1f}" r="3.6" fill="#FBE3C8" opacity="0.9"/>')
    rr = 42 if small else 31
    sx, sy = SIGN
    body.append(f'<circle cx="{sx}" cy="{sy}" r="{rr}" fill="url(#{g("red")})"/>')
    body.append(f'<circle cx="{sx}" cy="{sy}" r="{rr - 1}" fill="none" stroke="#7A0E12" '
                f'stroke-width="2" opacity="0.35"/>')
    bw, bh = rr * 1.25, rr * 0.36
    body.append(f'<rect x="{sx - bw / 2}" y="{sy - bh / 2 + 2}" width="{bw}" height="{bh}" rx="{bh / 2}" '
                f'fill="#5A0A0D" opacity="0.30"/>')
    body.append(f'<rect x="{sx - bw / 2}" y="{sy - bh / 2}" width="{bw}" height="{bh}" rx="{bh / 2}" fill="#FFFFFF"/>')
    if not small:
        body.append(f'<ellipse cx="{sx}" cy="{sy - rr * 0.48}" rx="{rr * 0.72}" ry="{rr * 0.42}" '
                    f'fill="url(#{g("gloss")})" opacity="0.65"/>')
    body.append(arrow_svg(f"url(#{g('ar')})", 30 if small else 21, head=1.25 if small else 1.0))
    if not small:
        d = (f"M{ARROW_P0[0] + 6},{ARROW_P0[1] - 3} C{ARROW_C1[0]},{ARROW_C1[1] - 6} "
             f"{ARROW_C2[0]},{ARROW_C2[1] - 7} {ARROW_P3[0] - 6},{ARROW_P3[1] - 2}")
        body.append(f'<path d="{d}" fill="none" stroke="#FFFFFF" stroke-width="3" '
                    f'stroke-linecap="round" opacity="0.28"/>')
    return "\n".join(defs), "\n".join(body)


def shadow_layers(small=False):
    """Shadow shapes in mark space: (svg body, blur in mark units, opacity)."""
    base = [Lb, Cb, Rb, Bk]
    return [
        (poly([add(p, (20, 18)) for p in base], fill="#000"), 22, 0.20),
        (poly([add(p, (6, 6)) for p in base], fill="#000"), 6, 0.30),
        (f'<g transform="translate(3,7)">{arrow_svg("#000", 30 if small else 21, 1.25 if small else 1.0)}</g>', 6, 0.22),
    ]


def leaf_spill():
    """Warm light from the open door onto the ground."""
    rd = door_poly(rf, RD)
    a, b = rd[3], rd[2]
    return poly([a, b, add(b, P, 0.32), add(a, P, 0.32)], fill="#FFB25E")


# ---------------------------------------------------------------- rendering
def svg_doc(inner, w, h, vb=None, defs=""):
    vb = vb or f"0 0 {w} {h}"
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="{vb}">'
            f'<defs>{defs}</defs>{inner}</svg>')


def render(svg, w, h):
    png = cairosvg.svg2png(bytestring=svg.encode(), output_width=w, output_height=h)
    return Image.open(io.BytesIO(png)).convert("RGBA")


def blurred(svg_inner, transform, size, blur_px, opacity, color=(0, 0, 0)):
    img = render(svg_doc(f'<g transform="{transform}">{svg_inner}</g>', size, size), size, size)
    a = img.getchannel("A").filter(ImageFilter.GaussianBlur(blur_px)).point(lambda v: int(v * opacity))
    layer = Image.new("RGBA", (size, size), color + (0,))
    layer.putalpha(a)
    return layer


def squircle_path(cx, cy, half, n=5.0, steps=720):
    """Apple's continuous-corner tile (a superellipse)."""
    out = []
    for i in range(steps):
        t = 2 * math.pi * i / steps
        c, s = math.cos(t), math.sin(t)
        out.append(f"{cx + half * math.copysign(abs(c) ** (2 / n), c):.2f},"
                   f"{cy + half * math.copysign(abs(s) ** (2 / n), s):.2f}")
    return "M" + " L".join(out) + " Z"


MARK_SCALE = 1.40               # mark scale for an 824 tile in 1024
MARK_CX, MARK_CY = 272, 240     # centre of the mark's box in mark space
MAC_HALF = 412                  # macOS: 824 tile in 1024 (Apple's grid)
WIN_HALF = 492                  # Windows: 984 tile in 1024, no shadow margin
SQUARE = "M0,0 L1024,0 L1024,1024 L0,1024 Z"


def mark_transform(size, scale, cx=512, cy=516):
    k = size / 1024.0
    s = scale * k
    return f"translate({cx * k - MARK_CX * s:.3f},{cy * k - MARK_CY * s:.3f}) scale({s:.4f})"


def tile_svg(path, size):
    k = size / 1024.0
    defs = (lin("tg", *TILE_COLOURS, (512, 100), (512, 924)) +
            '<linearGradient id="rim" x1="0" y1="0" x2="0" y2="1">'
            '<stop offset="0" stop-color="#FFFFFF" stop-opacity="0.22"/>'
            '<stop offset="0.25" stop-color="#FFFFFF" stop-opacity="0"/></linearGradient>'
            '<radialGradient id="sheen" cx="0.32" cy="0.18" r="0.75">'
            '<stop offset="0" stop-color="#FFFFFF" stop-opacity="0.10"/>'
            '<stop offset="1" stop-color="#FFFFFF" stop-opacity="0"/></radialGradient>')
    inner = (f'<g transform="scale({k})"><path d="{path}" fill="url(#tg)"/>'
             f'<path d="{path}" fill="url(#sheen)"/>'
             f'<path d="{path}" fill="none" stroke="url(#rim)" stroke-width="4"/>'
             f'<path d="{path}" fill="none" stroke="#000000" stroke-opacity="0.5" stroke-width="1.5"/></g>')
    return svg_doc(inner, size, size, defs=defs)


def compose_icon(size=1024, small=False, half=MAC_HALF, square=False, tile_shadow=True):
    """The tile with the mark. `half` is the tile's half-width in a 1024 canvas; `square` makes a
    full-bleed square (apple-touch); `small` draws the simplified art."""
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    k = size / 1024.0
    path = SQUARE if square else squircle_path(512, 512, half)
    if tile_shadow and not square:
        shape = f'<g transform="scale({k})"><path d="{path}" fill="#000"/></g>'
        canvas.alpha_composite(blurred(shape, f"translate(0,{12 * k})", size, 14 * k, 0.32))
        canvas.alpha_composite(blurred(shape, f"translate(0,{3 * k})", size, 3 * k, 0.18))
    canvas.alpha_composite(render(tile_svg(path, size), size, size))
    grow = (512 if square else half) / MAC_HALF
    scale = (1.62 if small else MARK_SCALE) * grow
    tf = mark_transform(size, scale)
    msc = scale * k
    for body, blur, op in shadow_layers(small):
        canvas.alpha_composite(blurred(body, tf, size, blur * msc, min(0.85, op * 1.6)))
    if not small:
        canvas.alpha_composite(blurred(leaf_spill(), tf, size, 7 * msc, 0.75, (255, 196, 120)))
    defs, body = mark_body(small=small)
    canvas.alpha_composite(render(svg_doc(f'<g transform="{tf}">{body}</g>', size, size, defs=defs), size, size))
    return canvas


def at(px, **kwargs):
    """The icon at `px` pixels, always drawn larger and reduced (never enlarged)."""
    small = px <= 32
    return compose_icon(512 if small else 1024, small=small, **kwargs).resize((px, px), Image.LANCZOS)


# ---------------------------------------------------------------- tray glyph (macOS template image)
def tray_template(px=44):
    """A one-colour cube with its open side door: black where drawn, alpha elsewhere. The faces are
    cut apart by thin gaps and the right face's door is a hole with the door leaf swung out."""
    gap = 20
    rd = door_poly(rf, RD)
    x0, y0, w = 72, 2, 392    # the cube and leaf span x 91..447, y 20..380: a square with a margin
    big = px * 4

    def alpha(inner):
        return render(svg_doc(inner, px, px, vb=f"{x0} {y0} {w} {w}"), big, big).getchannel("A")

    cube = alpha(poly([T, R, Rb, Cb, Lb, L], fill="#000", stroke="#000", stroke_width="10",
                      stroke_linejoin="round"))
    cuts = alpha(f'<polyline points="{pts(L, C, R)}" fill="none" stroke="#000" stroke-width="{gap}" '
                 f'stroke-linejoin="round"/>'
                 f'<line x1="{C[0]}" y1="{C[1]}" x2="{Cb[0]}" y2="{Cb[1] + 10}" stroke="#000" stroke-width="{gap}"/>'
                 + poly(rd, fill="#000"))
    leaf = alpha(poly(leaf_poly(), fill="#000"))
    shape = ImageChops.lighter(ImageChops.subtract(cube, cuts), leaf).resize((px, px), Image.LANCZOS)
    out = Image.new("RGBA", (px, px), (0, 0, 0, 0))
    out.putalpha(shape)
    return out


# ---------------------------------------------------------------- logo
def font(px, weight=700):
    f = ImageFont.truetype(FONT, px)
    try:
        f.set_variation_by_axes([100, min(96, max(17, px * 0.75)), 400, weight])
    except Exception:
        pass
    return f


def compose_logo(text_color, W=1004, Hh=240):
    """The dark tile as a badge, then "Your Sidedoor"."""
    canvas = Image.new("RGBA", (W, Hh), (0, 0, 0, 0))
    badge = compose_icon(1024).resize((300, 300), Image.LANCZOS).crop((30, 30, 270, 270))
    canvas.alpha_composite(badge.resize((224, 224), Image.LANCZOS), (8, 8))
    tx = 252
    f = font(118, 720)
    d = ImageDraw.Draw(canvas)
    text = "Your Sidedoor"
    bbox = d.textbbox((0, 0), text, font=f)
    avail = W - tx - 14
    if bbox[2] - bbox[0] > avail:
        f = font(int(118 * avail / (bbox[2] - bbox[0])), 720)
        bbox = d.textbbox((0, 0), text, font=f)
    ty = (Hh - (bbox[3] - bbox[1])) // 2 - bbox[1] + 2
    d.text((tx, ty), text, font=f, fill=text_color)
    return canvas


# ---------------------------------------------------------------- SVG sources
MARK_BOX = (68, 8, 470, 472)


def write_svgs(out):
    defs, body = mark_body()
    x0, y0, x1, y1 = MARK_BOX
    (out / "yoursidedoor-mark.svg").write_text(
        svg_doc(body, x1 - x0, y1 - y0, vb=f"{x0} {y0} {x1 - x0} {y1 - y0}", defs=defs))
    tile = squircle_path(512, 512, MAC_HALF)
    tf = mark_transform(1024, MARK_SCALE)
    layers = shadow_layers()
    shadows = "".join(f'<g filter="url(#b{i})" opacity="{op}" transform="{tf}">{b}</g>'
                      for i, (b, blur, op) in enumerate(layers))
    fdefs = "".join(f'<filter id="b{i}" x="-50%" y="-50%" width="200%" height="200%">'
                    f'<feGaussianBlur stdDeviation="{blur}"/></filter>' for i, (b, blur, op) in enumerate(layers))
    fdefs += '<filter id="ts" x="-20%" y="-20%" width="140%" height="140%"><feGaussianBlur stdDeviation="14"/></filter>'
    inner = (f'<path d="{tile}" fill="#000" opacity="0.32" transform="translate(0,12)" filter="url(#ts)"/>'
             f'<path d="{tile}" fill="url(#tg)"/>{shadows}<g transform="{tf}">{body}</g>')
    (out / "yoursidedoor-icon.svg").write_text(
        svg_doc(inner, 1024, 1024, defs=defs + lin("tg", *TILE_COLOURS, (512, 100), (512, 924)) + fdefs))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=REPO / "assets" / "brand")
    out = parser.parse_args(argv).out
    out.mkdir(parents=True, exist_ok=True)
    write_svgs(out)
    compose_icon(1024).save(out / "yoursidedoor-icon-1024.png")
    for px in (16, 32):
        at(px).save(out / f"yoursidedoor-icon-{px}.png")
    compose_icon(1024, half=WIN_HALF, tile_shadow=False).resize((256, 256), Image.LANCZOS).save(
        out / "yoursidedoor-icon-win-256.png")
    for px in (16, 20, 24, 32):
        at(px, half=WIN_HALF, tile_shadow=False).save(out / f"yoursidedoor-icon-win-{px}.png")
    compose_icon(1024, square=True).convert("RGB").resize((180, 180), Image.LANCZOS).save(
        out / "yoursidedoor-apple-touch-180.png")
    tray_template().save(out / "yoursidedoor-tray-template.png")
    compose_logo((26, 25, 22, 255)).save(out / "yoursidedoor-logo.png")
    compose_logo((244, 238, 228, 255)).save(out / "yoursidedoor-logo-on-dark.png")
    for path in sorted(out.iterdir()):
        print(path.name, os.path.getsize(path))


if __name__ == "__main__":
    main()
