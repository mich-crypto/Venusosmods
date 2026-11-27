# Fab-style renders of the bare board (pcb-render-top/bottom.png): layers exported by kicad-cli as
# black-and-white SVGs, rasterised with rsvg-convert, coloured here (mask, pads, silkscreen, holes).
# usage: sh render.sh  (in tools/; needs kicad-cli, rsvg-convert, KiCad 7 python with PIL and numpy)
import sys, json, numpy as np
from PIL import Image, ImageDraw, ImageFilter
D = '/tmp/claude-0/render/'
holes = json.load(open(D + 'holes.json'))   # [x_mm, y_mm, d_mm, plated]
BX0, BY0, BW, BH = 100.0, 100.0, 120.0, 140.0

def load(n):
    return np.array(Image.open(D + n).convert('L')) < 128      # True = drawn

def render(side, out):
    cu, mask, silk = load(f'{side}_Cu.png'), load(f'{side}_Mask.png'), load(f'{side}_Silkscreen.png')
    H, W = cu.shape
    # board outline = outermost drawn pixels of the mask layer (only the edge rect reaches that far)
    ys, xs = np.where(mask)
    x0, x1, y0, y1 = xs.min(), xs.max(), ys.min(), ys.max()
    sx = (x1 - x0) / BW; sy = (y1 - y0) / BH
    inside = np.zeros_like(cu); inside[y0 + 3:y1 - 2, x0 + 3:x1 - 2] = True
    # the edge line itself is in every layer: drop it
    edge = np.zeros_like(cu); edge[y0:y1 + 1, x0:x1 + 1] = True; edge[y0 + 4:y1 - 3, x0 + 4:x1 - 3] = False
    cu &= ~edge; mask &= ~edge; silk &= ~edge
    open_ = mask & inside                  # solder mask openings
    img = np.zeros((H, W, 4), np.uint8)
    FR4 = (196, 178, 120); MASK = (22, 94, 52); MASK_CU = (40, 128, 72); PAD = (214, 214, 206); SILK = (245, 245, 240)
    def put(sel, c, a=255): img[sel] = (*c, a)
    put(inside, MASK)
    put(inside & cu, MASK_CU)
    put(open_ & ~cu, FR4)
    put(open_ & cu, PAD)
    put(inside & silk & ~open_, SILK)
    # light mask "sheen": soft shadow along copper edges
    im = Image.fromarray(img, 'RGBA')
    # holes
    d = ImageDraw.Draw(im)
    for x, y, dia, plated in holes:
        px = x0 + (x - BX0) * sx
        if side == 'B': px = x1 - (x - BX0) * sx
        py = y0 + (y - BY0) * sy
        r = dia / 2 * sx
        if plated:
            d.ellipse([px - r * 1.35, py - r * 1.35, px + r * 1.35, py + r * 1.35], fill=PAD + (255,))
        d.ellipse([px - r, py - r, px + r, py + r], fill=(28, 28, 28, 255))
    # crop to the board with a small margin, white background, soft drop shadow
    m = 40
    im = im.crop((x0 - m, y0 - m, x1 + m, y1 + m))
    a = im.split()[3]
    shadow = Image.new('RGBA', im.size, (0, 0, 0, 0)); shadow.putalpha(a.point(lambda v: 90 if v else 0))
    shadow = shadow.filter(ImageFilter.GaussianBlur(14))
    bg = Image.new('RGBA', im.size, (255, 255, 255, 255))
    bg.alpha_composite(shadow, (10, 14)); bg.alpha_composite(im)
    bg = bg.convert('RGB')
    bg.resize((bg.width // 2, bg.height // 2), Image.LANCZOS).save(out)
    print(out, bg.size)

render('F', D + 'render-top.png')
render('B', D + 'render-bottom.png')
