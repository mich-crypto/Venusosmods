# Filling chart for the wheel-arch water tank

Calculated from the tank's technical drawing
([tank/tank-drawing.jpg](tank/tank-drawing.jpg)) with
[`tools/tank_wheelarch.py`](../tools/tank_wheelarch.py). **This table is already
the firmware's default**, so after `zero` on the empty tank the ESP32 shows
litres without any filling calibration.

![Water volume by height](tank/tank-filling-chart.svg)

**Capacity: about 106 L** (115 L from the outside dimensions, minus 5 mm
walls). If the tank's label or datasheet gives a capacity, rescale the table
to it (see "Adjusting" below).

## How the tank was modelled

![Tank drawing](tank/tank-drawing.jpg)

| Part | Dimensions used (outside, mm) |
|---|---|
| Total height / length | 695 / 1150 |
| Depth | 260 in the top 230 mm, 230 below |
| Wheel arch | circle centred on the floor at 590 from the leg end; radius 420 on one face and 350 on the other (the arch is slanted across the depth) |
| Leg at the floor | 170 mm long on one face, 240 on the other (590 − 420 and 590 − 350, matching the bottom view) |
| Arch top / box bottom | 275 and 345 mm below the top (end view) |
| Walls | 5 mm (typical for roto-moulded PE) |

The drawing labels the arch curves R394.3 and R333. Those are the curved
wall surfaces, which aren't centred on the floor. The radius used here
(420 / 350 around a centre on the floor) is the one that matches the 170 /
240 mm leg and the 275 / 345 mm box bottom at the same time. The difference
in volume is below 1 L.

The volume is added up in 1 mm height slices, and each slice follows the arch
across the depth. Small details are ignored: the fittings, the V-shaped sump
under the outlet, rounded edges (R5) and the 10 mm chamfers. Together they
change the total by roughly 1–2 %.

## Where the sensor goes

The leg is the only part that reaches the floor, so the sensor must lie
**at the bottom of the leg**. Lower it through the threaded port above the
leg (the S12 port 101 mm from the leg end in the top view). The heights in the table are measured from the leg's inner floor.

Parking on a slope matters more for this tank than for a box. The upper
part is 1150 mm long, so when the van stands nose-up or nose-down, the water
shifts along it. Above about 350 mm (around 20 L) the reading is only exact
on level ground. The ESP32's slosh filter doesn't correct for a slope; park
level when you want a precise number.

## The table

| Height (mm) | Litres | % |
|---:|---:|---:|
| 0 | 0.0 | 0 |
| 20 | 0.9 | 1 |
| 40 | 1.7 | 2 |
| 60 | 2.6 | 2 |
| 80 | 3.5 | 3 |
| 100 | 4.4 | 4 |
| 120 | 5.3 | 5 |
| 140 | 6.3 | 6 |
| 160 | 7.3 | 7 |
| 180 | 8.3 | 8 |
| 200 | 9.4 | 9 |
| 220 | 10.6 | 10 |
| 240 | 11.8 | 11 |
| 260 | 13.1 | 12 |
| 280 | 14.4 | 14 |
| 300 | 15.9 | 15 |
| 320 | 17.4 | 16 |
| 340 | 19.2 | 18 |
| 350 | 20.1 | 19 |
| 360 | 21.1 | 20 |
| 380 | 24.0 | 23 |
| 400 | 27.7 | 26 |
| 420 | 32.4 | 30 |
| 440 | 37.4 | 35 |
| 460 | 42.4 | 40 |
| 465 | 43.7 | 41 |
| 480 | 48.0 | 45 |
| 500 | 53.7 | 50 |
| 520 | 59.4 | 56 |
| 540 | 65.1 | 61 |
| 560 | 70.8 | 67 |
| 580 | 76.5 | 72 |
| 600 | 82.2 | 77 |
| 620 | 87.9 | 83 |
| 640 | 93.6 | 88 |
| 660 | 99.3 | 93 |
| 680 | 105.0 | 99 |
| 685 | 106.4 | 100 |

## Checking it

1. Empty the tank, park level, type `zero` in the Serial Monitor.
2. Pour in 10 L and compare `litres=` in the log with the table. Repeat a few
   times. The bottom part (leg) is the most sensitive to the model, and the
   top part the least.
3. If the reading is consistently a few % off, rescale (below). If one region
   is off (e.g. around the arch), calibrate by filling in steps instead
   ([calibration.md](calibration.md), method A), which measures your real
   tank.

## Adjusting

```sh
python3 tools/tank_wheelarch.py --capacity 100   # scale to the label's capacity
python3 tools/tank_wheelarch.py --wall 6         # different wall thickness
```

Each prints `cal point ...` lines to paste into the Serial Monitor (followed
by `cal save`), plus the C table for `DEFAULT_TABLE` in the sketch.
`--markdown --svg docs/tank/tank-filling-chart.svg` regenerates this table
and the chart.
