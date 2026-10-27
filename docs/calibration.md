# Calibrating the water tank (non-square tanks)

A hydrostatic sensor measures **pressure**, which only depends on the **height**
of the water above it, not on the tank's shape. In a square box, height and
volume are proportional. In a van tank with a wheel-arch cut-out, a slope or
rounded corners they are not: the first 5 cm may hold 12 L, the top 5 cm only
4 L. The ESP32 fixes this with a **height → litres table** that it
interpolates between.

```
 height (mm)
 400 ┤                          ●  57 L = 100 %
     │                     ●
 300 ┤                ●          narrow part (above the wheel arch):
     │           ●               each mm holds less water
 150 ┤      ●  ← shape changes here
     │   ●                       wide part: each mm holds more
   0 ●──────────────────────────── litres
```

You do this once. The table is stored in the ESP32's flash and survives power
cuts and firmware updates. There are two ways to build it:

* **A. Fill it in steps** (most accurate, ~30 minutes)
* **B. Calculate it from the tank's dimensions** (when you can't easily fill
  it in measured steps)

Both use the Arduino IDE **Serial Monitor** (115200 baud, "Both NL & CR")
with the ESP32 connected to your laptop over USB-C. Only keep the van's 12 V
connected at the same time if you fitted the Schottky diode from
[wiring.md](wiring.md); otherwise unplug the buck converter's 5 V first.

## 0. Before you start

1. Mount the sensor where it will stay, lying flat on the bottom, as close to
   the middle of the tank as possible.
2. Park the van **level** (use a spirit level on the floor, or an app).
3. Type `log` to stop the status lines scrolling, or leave them on to watch
   the height settle.
4. Type `status`: you should see a plausible `signal=` (0.5 V or 4 mA at
   zero). `FAULT` means a wiring problem; see [wiring.md](wiring.md).

## 1. Zero point (both methods)

With the tank **completely empty** (or emptied down to the drain):

```
zero
```

The ESP32 averages the reading for 5 s and stores an offset so that the
height now reads 0 mm. This removes the sensor's offset and the height of its
membrane above the tank floor.

## 2A. Calibrate by filling in steps

You need a container of known size: a 10 L bucket, or a 5 L jerrycan. Use
the same one every time, and measure what it really holds once, on a kitchen
scale (1 L of water = 1 kg).

```
cal start
```
records 0 L at the current (empty) height. Then repeat:

1. Pour in one container (for example 10 L).
2. Wait **30 seconds** for the water to settle. If `SLOSH` is shown, wait longer.
3. Type the amount you added **in this step**:
   ```
   cal add 10
   ```
   The ESP32 measures for 5 s and stores the point with the running total
   (10, 20, 30 L...).

Use smaller steps (5 L) where the shape changes, e.g. around the top of the
wheel arch. A wrong point can be removed with `cal undo`, and `cal show`
prints the table so far.

When the tank is full:

```
cal save
```

The table is now active and stored. The last point is 100 %. `cal save` also
prints the table as C code: copy it into `DEFAULT_TABLE` in the sketch as a
backup (otherwise `reset`, or an ESP32 full flash erase, loses it).

Tip: fill through the normal filler neck, so the tank's breather and any
air pockets behave like they will on the road.

## 2B. Calculate it from the tank's dimensions

Measure the **inside** of the tank (outside dimensions minus the wall, often
3-5 mm per wall for roto-moulded tanks) at every height where the shape
changes:

* width (left–right) and depth (front–back) at that height
* if one of them is the same everywhere, you only need the other one

Example: a 300 mm deep tank that is 600 mm wide for the first 150 mm, then
400 mm wide (wheel arch) up to 400 mm:

```sh
python3 tools/tank_table.py --depth 300 --profile "0:600 150:600 151:400 400:400"
```

Width and depth both change? Use `height:width:depth`:

```sh
python3 tools/tank_table.py --profile "0:500:250 100:550:300 380:550:300"
```

Options:
* `--offset 15`: the sensor's membrane sits 15 mm above the floor, for
  example because of its housing. Water below it can't be measured, so the
  table starts counting litres from there.
* `--scale 0.97`: correct for internals (baffles, pump, fittings), or to match
  the volume the tank's datasheet gives.

The script prints `cal point ...` lines. Paste them into the Serial Monitor
(after `zero`), followed by `cal save`.

**Check:** add a known amount (one bucket) and compare. If the reading is
consistently off by a few %, re-run with `--scale`.

## 3. Check and fine-tune

* After calibration, fill and empty a few litres and watch `litres=` in the
  log. The level should change by what you added, within about 1-2 %.
* Driving: `SLOSH` appears and the reading becomes very slow (~25 s time
  constant). That is intended; it settles again within seconds when parked.
* Parked on a slope, the reading changes a little if the sensor isn't in the
  middle of the tank. That is the water level really changing at the sensor;
  only moving the sensor fixes it.
* Freezing: a hydrostatic sensor can be damaged when the water around it
  freezes. Drain the tank in frost, or use the van's tank heater.

## Command reference

| Command | Does |
|---|---|
| `help` | list commands |
| `status` | signal, raw and filtered height, litres, active table |
| `log` | turn the periodic status line on/off |
| `zero` | empty tank: current height becomes 0 mm (stored) |
| `zero <mm>` | set the offset by hand |
| `cal start` | new table, 0 L at the current height |
| `cal add <L>` | record a point after adding `<L>` litres |
| `cal point <mm> <L>` | add a point by hand (total litres) |
| `cal undo` / `cal show` / `cal cancel` | edit the table being measured |
| `cal save` | activate + store the table, print it as code |
| `table` | print the active table as code |
| `reset` | erase stored calibration, use `DEFAULT_TABLE` from the sketch |

The Venus OS side needs no changes after calibration: the ESP32 sends level %,
litres and capacity, and the tank's capacity on the GX updates automatically.
