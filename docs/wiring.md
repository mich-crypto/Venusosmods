# Wiring: I/O extension (ESP32-S3)

## Parts

| Part | Notes |
|---|---|
| ESP32-S3 dev board (DevKitC-1 or clone) | any S3 board with USB-C |
| Buck converter 12 V → 5 V, ≥1 A | MP1584 or LM2596 module, adjustable |
| Fuse holder + 1 A fuse | at the 12 V tap |
| Schottky diode 1N5819 / SS14 | lets USB and 12 V be connected together |
| Hydrostatic level transmitter | 0–3.3 V, 0.5–4.5 V or 4–20 mA output (see below) |
| Resistors | 0–3.3 V version: 10 kΩ + 1 MΩ; 0.5–4.5 V version: 10 kΩ + 20 kΩ; 4–20 mA version: 120 Ω 0.1 %/1 % |
| 1 kΩ resistor + 100 nF capacitor | input filter/protection (options A and B) |
| DS18B20 waterproof probes (up to 2) + 4.7 kΩ per data pin | optional temperatures, each with a name |
| IP65 junction box, cable glands, ferrules | |
| Wire | 0.75 mm² for 12 V, any thin wire for signals |

## Power

```
 Van 12 V aux ──[fuse 1 A]──┬──────────────────────────── to 4-20 mA sensor (option B)
                            │
                    ┌───────┴───────┐
                    │ Buck → 5.0 V  │   set to 5.0 V with a multimeter
                    └──┬─────────┬──┘   BEFORE connecting the ESP32
                    5V ├─────────│───── 5 V sensor supply (option C, before the diode)
                       ▼ 1N5819  │      (stripe towards the ESP32)
                       │         │
               ESP32 5V/VIN     GND ─── van ground / sensor ground
```

* Use the board's **5V / VIN** pin, never 3V3.
* The diode drops ~0.3 V (ESP32 sees ~4.7 V, fine) and stops the buck
  converter and the laptop's USB from feeding into each other while you
  calibrate. Without it: unplug the 5 V wire before plugging in USB.
* Take 12 V from a circuit that is on when the Victron system is on (e.g.
  the 12 V distribution after the main switch), not from the starter battery.
  The ESP32 draws about 0.5 W.

## Pressure sensor, option C: 0–3.3 V output (e.g. QDY30A 0–3.3 V)

This is the firmware's default. The output can go straight to the ESP32,
with no divider and no shunt resistor. The QDY30A datasheet is saved in
[datasheets/](datasheets/QDY30A-water-level-transmitter.png).

QDY30A 3-wire voltage version, **5 V supply, 0–3.3 V output**, colours per
the datasheet:

```
 Buck 5.0 V out ──┬──|>|── ESP32 5V        (1N5819, stripe towards the ESP32)
                  │
 red    (V+)  ────┘                         sensor gets the full 5.0 V
 green  (V-)  ────────────── GND            (common GND: buck, ESP32, sensor)
 yellow (out) ──[10k]──┬───────┬── GPIO4
                       │       │
                    [100nF]  [1M]
                       │       │
                      GND     GND
```
Firmware: `INPUT_CURRENT_LOOP 0`, `DIVIDER_RATIO 1.0f`,
`SIGNAL_AT_ZERO 0.0f`, `SIGNAL_AT_FULL 3.3f`, `SENSOR_RANGE_MM 2000.0f`
(for the 2 m version), `SIGNAL_FAULT_LOW -1.0f`, `SIGNAL_FAULT_HIGH 3.05f`.
These are the sketch defaults and what the ready-made firmware in
`firmware/bin/` uses; only change `SENSOR_RANGE_MM` if yours is not 2 m.

* **Supply:** take the sensor's 5 V from the buck output **before** the diode,
  not from the ESP32's 5V pin: after the diode it only gets ~4.7 V, which can
  be below a 5 V sensor's minimum. Never use the ESP32's **3V3** pin for it,
  and never 12 V for a 5 V sensor.
* **On the bench with USB only** (buck not powered) the sensor has no supply
  and reads 0. Either switch the 12 V on as well, or for a quick test put the
  red wire on the ESP32's 5V pin temporarily.
* **Test before connecting the ESP32:** power the sensor alone (5 V) and measure
  yellow to green with a multimeter: about 0 V in air, rising by about 0.165 V
  per 10 cm when you lower it into a bucket or a pipe filled with water.
* **Why 10 kΩ:** a fault in the sensor could push the output towards its 5 V
  supply, above what the pin takes. The 10 kΩ limits the current into the pin's protection diode to under
  1 mA. With the 1 MΩ pull-down it reads about 1 % low, which the
  calibration cancels out.
* **Resolution:** the ESP32 reads up to about 3.1 V, i.e. about 1.88 m of
  water with the 2 m sensor. A van tank of 30–50 cm uses a quarter of the
  range: about 0.6 mm of water per mV, which the firmware's median filter
  handles well. The datasheet's ±2 % F.S. zero drift (±40 mm) is long-term
  drift: run `zero` on an empty tank once or twice a year.
* **No broken-wire detection:** 0 V means "empty" and also "wire broken". The
  1 MΩ pull-down makes a broken signal wire read as empty instead of random
  values. Option B (4–20 mA) is the only one that can tell the two apart.
* **Vent tube:** the cable has a breather capillary (the datasheet's
  "breather capillary"). Keep the cable end with the tube inside the dry
  junction box, open to air.

## Pressure sensor, option A: voltage output (0.5–4.5 V, 5 V supply)

```
 sensor red   (+5V) ── ESP32 5V
 sensor black (GND) ── GND
 sensor yellow(OUT) ──[R1 10k]──┬──[1k]──┬── GPIO4
                                │        │
                             [R2 20k] [100nF]
                                │        │
                               GND      GND
```
Divider ratio (R1+R2)/R2 = 1.5, so 4.5 V becomes 3.0 V at the pin.
Firmware: `INPUT_CURRENT_LOOP 0`, `DIVIDER_RATIO 1.5f`,
`SIGNAL_AT_ZERO 0.5f`, `SIGNAL_AT_FULL 4.5f`,
`SIGNAL_FAULT_LOW 0.2f`, `SIGNAL_FAULT_HIGH 4.9f`.

Wire colours differ between makers: check the sensor's label or datasheet.

## Pressure sensor, option B: 4–20 mA two-wire transmitter (12–24 V supply)

```
 12 V (fused) ── sensor +      sensor - ──┬──[1k]──┬── GPIO4
                                          │        │
                                     [R 120 Ω] [100nF]
                                          │        │
                                         GND      GND
```
4 mA → 0.48 V, 20 mA → 2.4 V. Use a 0.1 % or 1 % resistor.
Firmware: `INPUT_CURRENT_LOOP 1`, `SHUNT_OHMS 120.0f`,
`SIGNAL_AT_ZERO 4.0f`, `SIGNAL_AT_FULL 20.0f`,
`SIGNAL_FAULT_LOW 3.0f`, `SIGNAL_FAULT_HIGH 22.0f`.
A broken wire then shows up as a fault in the GUI instead of "empty".
This type copes best with long cables and electrical noise in a vehicle.

The 1 kΩ + 100 nF form an RC filter and limit current into the pin's
protection diodes if something goes wrong.

## DS18B20 temperature probes (up to two)

Default: **each probe on its own pin**, probe 1 on **GPIO5**, probe 2 on
**GPIO6**. Each data pin gets its own 4.7 kΩ to 3V3.

```
              probe 1                       probe 2
 3V3 ──┬────── red              3V3 ──┬────── red
    [4.7 kΩ]                       [4.7 kΩ]
       │                              │
 GPIO5 ┴────── yellow (data)    GPIO6 ┴────── yellow (data)
 GND ───────── black            GND ───────── black
```
* The pin decides the number: GPIO5 is always probe 1, GPIO6 always probe 2.
* Both on GPIO5 in parallel (one shared 4.7 kΩ) works too: they are then
  numbered by their address. Set `PIN_ONEWIRE2 -1` if you don't use GPIO6 at all.
* Power them from **3V3**, not 5 V: the data line goes straight to the ESP32.
* **Without the 4.7 kΩ** the firmware switches on the ESP32's own pull-up
  (~45 kΩ). That is often enough for short cables (1–2 m) and two probes, but
  not reliable: fit the resistor for the van. Probes on a small breakout
  board (pins VCC/DAT/GND) usually have it on board already.
* **Names:** press **Rename** on the I/O extension's page, or type `name 1 Inside`
  in the Serial Monitor (`temps` lists them with their pin). The name stays
  with the probe.
* For cables over ~5 m use 2.2 kΩ instead of 4.7 kΩ.
* Set `USE_DS18B20 0` in the sketch if you don't fit any.
* Drinking water: the common "waterproof stainless" probes are not certified
  for potable water. Either buy one with a documented food-grade sleeve and
  cable, or put the probe in a dry pocket on the **outside** of the tank wall
  (clamped with thermal paste and insulated). That measures the water
  through the wall, a little slower, without touching it.

## Grey water tank (rod sender, 5-wire plug)

Most grey tanks have a rod sender: a common rod plus up to 4 level rods
(e.g. ¼, ½, ¾, full), on a 5-pin plug. The van's tank display puts 12 V on
the common rod; water between the common and a level rod carries it over to
that rod's wire. The ESP32 reads the same rods with its own 3.3 V, **without
any resistors**.

**The van's tank display must be unplugged from the I/O extension for good.** Its
12 V must never reach these wires: it destroys the ESP32. Keep the cable away
from other 12 V wiring.

```
 sender plug                         ESP32-S3
 common (the pin with 12 V) ──────── GPIO9    the ESP32 powers the rods
 level 1 (lowest)           ──────── GPIO1
 level 2                    ──────── GPIO2
 level 3                    ──────── GPIO7
 level 4 (full)             ──────── GPIO8
```

* **Finding the pins:** with the display still connected and on, measure each
  pin to van ground: the one with 12 V all the time is the common. Then unplug
  the display and measure resistance (Ω) from the common to each other pin with
  the tank about half full: the wet rods show a value (a few kΩ up to ~50 kΩ),
  the dry ones nothing (OL). The lowest rod is wet first.
* **Fewer rods:** list only the ones you have in `GREY_PINS` (lowest first),
  e.g. `{ 1, 2, 7 }` for three.
* **How it reads:** every 5 s the common pin goes to 3.3 V for about 3 ms (at
  its weakest drive, so a short circuit only draws a few mA). Each level pin
  reads its voltage against the ESP32's own pull-down: wet about 1–3 V, dry
  about 0 V. Then the current runs the other way for the same time, so the
  rods don't corrode the way they do with the display's constant 12 V. In
  between no current flows. A rod changes only after 3 equal readings in a
  row (15 s), so sloshing while driving doesn't make it jump.
* **Check it:** type `grey` in the sensor's terminal (its own page or the
  Serial Monitor): it lists each rod's voltage and wet or dry. The sensor's
  page also has a *Grey water tank* card. If a rod in the water reads below
  the threshold, lower it: `grey threshold 250` (default 400 mV, kept after a
  restart).
* **Venus OS:** *Water → Settings → Grey water tank* on the Mods page (or
  `grey_tank = yes` in config.ini). The tank shows up as *waste water* with the
  level of the highest wet rod (equal steps, or your own with `grey_levels`),
  litres if you give the tank size, and an alarm when it is full
  (`grey_alarm_pct`, default 100 = the top rod is wet).
* A wet rod above a dry one shows as a fault: a dirty rod (grease, hair) or a
  broken wire. The level then follows the highest wet rod.
* Firmware without the grey tank: `USE_GREY 0`. Needs sensor firmware 4.17+.

## Choosing and mounting the pressure sensor

* **Range:** pick one just above the tank height, e.g. 0–1 m H₂O (0–10 kPa)
  for a 40 cm tank. A 0–5 m sensor would only use 8 % of its range, so you
  lose resolution. A 2 m sensor still works well (see option C).
* **Vented (gauge) sensor:** the cable has a small vent tube. Keep its end
  open to air and dry: inside the junction box is fine, outside under the
  van is not. Don't seal it with the cable gland.
* **Position:** flat on the bottom, as close to the **middle** of the tank
  as you can. On a slope the level at the centre hardly changes.
* **Drinking water:** stainless (316) body and a food-safe (e.g. FDA/KTW
  certified) cable, or mount the sensor outside the tank in a T-piece on the
  drain at the lowest point.
* **Through the lid:** most installs lower the probe through a cable gland
  (PG9/PG11) in the tank's inspection lid, so no new hole below the water
  line.

## ADC pins

On the ESP32-S3 only GPIO1..GPIO10 are ADC1. ADC2 (GPIO11..20) shares hardware
with the radio and can return errors while it is active, so keep the sensor
on ADC1. GPIO4 and GPIO5 are the defaults in the sketch (`PIN_PRESSURE`,
`PIN_ONEWIRE`). The grey tank's level rods use GPIO1, 2, 7 and 8 (ADC1 too),
its common rod GPIO9. GPIO3 is left free: it is a strapping pin.

## Junction box layout

```
 ┌──────────── IP65 box ─────────────┐
 │  [fuse]→[buck]→|diode|→[ESP32-S3] │──gland── 12 V in
 │                       │  │  │     │
 │      [R divider/shunt]┘  │  └─────│──gland── sensor cable (+ vent tube end inside)
 │                [4k7]─────┘        │──gland── DS18B20
 └───────────────────────────────────┘
```
Mount the box above the tank's maximum water level, with the glands pointing
down so condensation drains away from the board. Plastic boxes let Bluetooth
through; a metal box or a box inside a metal locker will cut the range.
