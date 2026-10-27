#!/usr/bin/env python3
"""
Build the height -> litres table for an irregular tank from its dimensions,
for when you cannot calibrate by filling it in steps (see docs/calibration.md).

Measure the inside of the tank at several heights from the bottom: width
(left-right) and depth (front-back). Shapes with steps, slopes, wheel-arch
cut-outs or rounded corners are all fine - just measure more heights where
the shape changes. Between measurements the width and depth are taken as
changing linearly.

  # constant depth 300 mm, width 600 mm up to 150 mm high, then 400 mm (wheel arch)
  python3 tank_table.py --depth 300 --profile "0:600 150:600 151:400 400:400"

  # width and depth both vary: height:width:depth
  python3 tank_table.py --profile "0:500:250 100:550:300 380:550:300"

  # tank that is 15 L less than the box suggests (pump, baffles...): --scale
  python3 tank_table.py --depth 300 --profile "0:600 400:600" --scale 0.95

It prints serial commands for the ESP32 ("cal point ...") and the C table for
the sketch. Litres = mm^3 / 1e6.
"""

import argparse


def parse_profile(text, depth):
    pts = []
    for item in text.replace(',', ' ').split():
        f = [float(x) for x in item.split(':')]
        if len(f) == 2:
            if depth is None:
                raise SystemExit('--depth is needed for height:width points (%s)' % item)
            f.append(depth)
        if len(f) != 3:
            raise SystemExit('bad point %r: use height:width or height:width:depth' % item)
        pts.append(tuple(f))
    pts.sort()
    if len(pts) < 2:
        raise SystemExit('need at least two heights')
    return pts


def area_at(pts, h):
    """Cross-section area (mm^2) at height h, width and depth interpolated."""
    for (h0, w0, d0), (h1, w1, d1) in zip(pts, pts[1:]):
        if h0 <= h <= h1:
            t = 0.0 if h1 == h0 else (h - h0) / (h1 - h0)
            return (w0 + (w1 - w0) * t) * (d0 + (d1 - d0) * t)
    return 0.0


def build_table(pts, step_mm, scale):
    top = pts[-1][0]
    heights = sorted(set([round(h, 1) for h, _, _ in pts] +
                         [round(x * step_mm, 1) for x in range(int(top // step_mm) + 1)] + [top]))
    table, volume, prev = [], 0.0, heights[0]
    # integrate in 1 mm slices for accuracy (Simpson would be overkill)
    for h in heights:
        y = prev
        while y < h:
            dy = min(1.0, h - y)
            volume += area_at(pts, y + dy / 2) * dy
            y += dy
        prev = h
        table.append((h, volume / 1e6 * scale))
    return table


def interp(table, h):
    for (h0, l0), (h1, l1) in zip(table, table[1:]):
        if h0 <= h <= h1:
            return l0 + (l1 - l0) * (h - h0) / (h1 - h0)
    return table[-1][1] if h > table[-1][0] else 0.0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--profile', required=True, help='"height:width[:depth] ..." in mm')
    ap.add_argument('--depth', type=float, help='constant depth (mm) for height:width points')
    ap.add_argument('--step', type=float, default=25.0, help='table step in mm (default 25)')
    ap.add_argument('--scale', type=float, default=1.0,
                    help='multiply volumes, e.g. 0.97 for wall thickness or internals')
    ap.add_argument('--offset', type=float, default=0.0,
                    help='height of the sensor membrane above the tank bottom (mm). The '
                         'sensor cannot see water below it, so heights are shifted down and '
                         'the water below the membrane is left out (usable litres)')
    args = ap.parse_args()

    table = build_table(parse_profile(args.profile, args.depth), args.step, args.scale)
    if args.offset:
        # water below the membrane is invisible to the sensor (and usually below
        # the pump pick-up too): count litres from the membrane upwards
        base = interp(table, args.offset)
        table = [(0.0, 0.0)] + [(h - args.offset, l - base) for h, l in table if h > args.offset]
        print('Not measurable below the sensor: %.1f L\n' % base)

    # the firmware keeps 40 points at most
    while len(table) > 40:
        table = table[::2] if table[-1] in table[::2] else table[::2] + [table[-1]]

    print('Capacity: %.1f L in %d points\n' % (table[-1][1], len(table)))
    print('Serial Monitor commands (after "zero" on the empty tank):')
    print('cal cancel')
    for h, l in table:
        print('cal point %.1f %.1f' % (h, l))
    print('cal save\n')
    print('Or paste into DEFAULT_TABLE in the sketch:')
    for h, l in table:
        print('  { %6.1ff, %6.1ff },' % (h, l))


if __name__ == '__main__':
    main()
