#!/usr/bin/env python3
"""
Filling chart (height -> litres) for the L-shaped wheel-arch water tank in
docs/tank/tank-drawing.jpg, computed from the drawing.

Model (outer dimensions from the drawing, mm):
  * upper box over the full length L = 1150, from the top down to the arch
  * depth 260 in the top 230 mm (Z_STEP = 695 - 230 above the floor),
    230 below that
  * vertical leg at one end, down to the floor (total height H = 695)
  * wheel arch: a circular cut-out centred on the floor at XC = 590 from the
    leg end. Its radius changes across the depth from 420 to 350 because the
    arch is slanted: the leg's floor is 170 mm long on one face and 240 mm on
    the other (bottom view), and the box bottom is 275 / 345 mm below the top
    (end view). 590 - 420 = 170 and 590 - 350 = 240, so all three views agree.
  * walls: --wall mm thick (roto-moulded PE is usually 4-6 mm)

The volume is integrated in 1 mm height slices. Heights in the output are
measured from the inner floor of the leg, where the sensor lies, so they match
the ESP32's height after "zero" on the empty tank.

  python3 tools/tank_wheelarch.py                      # serial commands + C table (100 L)
  python3 tools/tank_wheelarch.py --capacity 0         # pure geometry, no scaling
  python3 tools/tank_wheelarch.py --markdown --svg docs/tank/tank-filling-chart.svg
"""

import argparse
import math

H, L, XC = 695.0, 1150.0, 590.0
D_LOW, D_TOP = 230.0, 260.0
Z_STEP = H - 230.0
R_A, R_B = 420.0, 350.0
NOMINAL_L = 100.0       # the tank is sold as 100 L; the geometry alone gives ~106 L


def slice_area(z, t):
    """Inner horizontal cross-section (mm^2) at height z above the outer floor."""
    if z < t or z > H - t:
        return 0.0
    area = 0.0
    depth = D_TOP - 2 * t if z >= Z_STEP + t else D_LOW - 2 * t
    for i in range(int(depth)):
        y = t + i + 0.5                      # position across the depth
        if y < D_LOW - t:
            r = R_A + (R_B - R_A) * min(1.0, y / D_LOW) + t   # inner arch surface
            length = XC - math.sqrt(r * r - z * z) - t if z < r else L - 2 * t
        else:
            length = L - 2 * t               # overhang of the 260 mm deep top part
        area += max(0.0, length)
    return area


def volume_curve(t):
    """[(height above inner floor, litres)] in 1 mm steps."""
    curve, vol = [(0.0, 0.0)], 0.0
    z = t
    while z < H - t:
        vol += slice_area(z + 0.5, t)
        z += 1.0
        curve.append((z - t, vol / 1e6))
    return curve


def pick_points(curve, step, max_points=40):
    top = curve[-1][0]
    # breakpoints where the shape changes, plus a regular grid
    marks = {round(b) for b in (R_B, R_A, Z_STEP)} | set(range(0, int(top), int(step)))
    marks.add(int(top))
    pts = [curve[min(int(m), len(curve) - 1)] for m in sorted(marks)]
    while len(pts) > max_points:
        pts = pts[:-1:2] + [pts[-1]]
    return pts


def svg_chart(curve, pts, capacity, path):
    """Single-series line chart, height on x, litres on y."""
    W, Hh, ml, mr, mt, mb = 720, 390, 64, 24, 52, 56
    pw, ph = W - ml - mr, Hh - mt - mb
    xmax = math.ceil(curve[-1][0] / 100) * 100
    ymax = math.ceil(capacity * 1.1 / 20) * 20   # headroom for the end label
    sx = lambda h: ml + h / xmax * pw
    sy = lambda v: mt + ph - v / ymax * ph
    ink, ink2, grid, blue, surf = '#0b0b0b', '#52514e', '#e1e0d9', '#2a78d6', '#fcfcfb'
    out = ['<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" '
           'font-family="Inter, DejaVu Sans, sans-serif" font-size="12">' % (W, Hh),
           '<rect width="100%%" height="100%%" fill="%s"/>' % surf,
           '<text x="%d" y="22" font-size="14" font-weight="600" fill="%s">'
           'Water volume by height above the tank floor</text>' % (ml, ink)]
    # shape zones as recessive bands with labels
    zones = [(0, R_B - 5, 'leg only'), (R_B - 5, Z_STEP - 5, 'wheel arch / box'),
             (Z_STEP - 5, curve[-1][0], 'deep top part')]
    for i, (a, b, label) in enumerate(zones):
        if i % 2:
            out.append('<rect x="%.1f" y="%d" width="%.1f" height="%d" fill="#f1f0ec"/>'
                       % (sx(a), mt, sx(b) - sx(a), ph))
        out.append('<text x="%.1f" y="%d" text-anchor="middle" fill="%s" font-size="11">%s</text>'
                   % ((sx(a) + sx(b)) / 2, mt - 6, ink2, label))
    for v in range(0, ymax + 1, 20):
        out.append('<line x1="%d" x2="%d" y1="%.1f" y2="%.1f" stroke="%s"/>'
                   % (ml, ml + pw, sy(v), sy(v), grid))
        out.append('<text x="%d" y="%.1f" text-anchor="end" fill="%s">%d</text>'
                   % (ml - 8, sy(v) + 4, ink2, v))
    for h in range(0, xmax + 1, 100):
        out.append('<text x="%.1f" y="%d" text-anchor="middle" fill="%s">%d</text>'
                   % (sx(h), mt + ph + 18, ink2, h))
    out.append('<text x="%.1f" y="%d" text-anchor="middle" fill="%s">water height (mm)</text>'
               % (ml + pw / 2, Hh - 14, ink2))
    out.append('<text transform="translate(16 %.1f) rotate(-90)" text-anchor="middle" '
               'fill="%s">litres</text>' % (mt + ph / 2, ink2))
    line = ' '.join('%.1f,%.1f' % (sx(h), sy(v)) for h, v in curve[::5] + [curve[-1]])
    out.append('<polyline points="%s" fill="none" stroke="%s" stroke-width="2" '
               'stroke-linejoin="round"/>' % (line, blue))
    h, v = curve[-1]
    out.append('<circle cx="%.1f" cy="%.1f" r="4" fill="%s" stroke="%s" stroke-width="2"/>'
               % (sx(h), sy(v), blue, surf))
    out.append('<text x="%.1f" y="%.1f" text-anchor="end" fill="%s" font-weight="600">'
               '%.0f L at %.0f mm</text>' % (sx(h) - 14, sy(v) - 6, ink, v, h))
    out.append('</svg>')
    with open(path, 'w') as f:
        f.write('\n'.join(out) + '\n')


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--wall', type=float, default=5.0, help='wall thickness in mm (default 5)')
    ap.add_argument('--capacity', type=float, default=NOMINAL_L,
                    help='scale to this total in litres (default %(default)s, the '
                         'nominal volume of the tank; 0 = pure geometry)')
    ap.add_argument('--step', type=float, default=20.0, help='table step in mm (default 20)')
    ap.add_argument('--markdown', action='store_true', help='print a Markdown table')
    ap.add_argument('--svg', help='write the chart to this SVG file')
    args = ap.parse_args()

    curve = volume_curve(args.wall)
    if args.capacity:
        k = args.capacity / curve[-1][1]
        curve = [(h, v * k) for h, v in curve]
    pts = pick_points(curve, args.step)
    total = curve[-1][1]

    if args.svg:
        svg_chart(curve, pts, total, args.svg)
    if args.markdown:
        print('| Height (mm) | Litres | % |')
        print('|---:|---:|---:|')
        for h, v in pts:
            print('| %.0f | %.1f | %.0f |' % (h, v, 100 * v / total))
        return

    print('Capacity: %.1f L (wall %.1f mm), inner height %.0f mm, %d points\n'
          % (total, args.wall, curve[-1][0], len(pts)))
    print('Serial Monitor commands (after "zero" on the empty tank):')
    print('cal cancel')
    for h, v in pts:
        print('cal point %.1f %.1f' % (h, v))
    print('cal save\n')
    print('Or paste into DEFAULT_TABLE in the sketch:')
    for h, v in pts:
        print('  { %6.1ff, %6.1ff },' % (h, v))


if __name__ == '__main__':
    main()
