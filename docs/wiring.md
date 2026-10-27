# Wiring: ESP32-S3 hydrostatic tank sender

## Parts

| Part | Notes |
|---|---|
| ESP32-S3 dev board (DevKitC-1 or clone) | any S3 board with USB-C |
| Buck converter 12 V → 5 V, ≥1 A | MP1584 or LM2596 module, adjustable |
| Fuse holder + 1 A fuse | at the 12 V tap |
| Schottky diode 1N5819 / SS14 | lets USB and 12 V be connected together |
| Hydrostatic level transmitter | 0.5–4.5 V or 4–20 mA output, range ≈ tank height (see below) |
| Resistors | voltage version: 10 kΩ + 20 kΩ; 4–20 mA version: 120 Ω 0.1 %/1 % |
| 1 kΩ resistor + 100 nF capacitor | input filter/protection |
| DS18B20 waterproof probe + 4.7 kΩ | optional water temperature |
| IP65 junction box, cable glands, ferrules | |
| Wire | 0.75 mm² for 12 V, any thin wire for signals |

## Power

```
 Van 12 V aux ──[fuse 1 A]──┬──────────────────────────── to 4-20 mA sensor (option B)
                            │
                    ┌───────┴───────┐
                    │ Buck → 5.0 V  │   set to 5.0 V with a multimeter
                    └──┬─────────┬──┘   BEFORE connecting the ESP32
                    5V │     GND │
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

## DS18B20 temperature probe (optional)

```
 probe red    (VDD)  ── ESP32 3V3
 probe black  (GND)  ── GND
 probe yellow (DATA) ──┬── GPIO5
                       │
                   [4.7 kΩ]
                       │
                      3V3
```
* Power it from **3V3**, not 5 V: the data line goes straight to the ESP32.
* The 4.7 kΩ pull-up is required. For cables over ~5 m use 2.2 kΩ.
* Set `USE_DS18B20 0` in the sketch if you don't fit one.
* Drinking water: the common "waterproof stainless" probes are not certified
  for potable water. Either buy one with a documented food-grade sleeve and
  cable, or put the probe in a dry pocket on the **outside** of the tank wall
  (clamped with thermal paste and insulated). That measures the water
  through the wall, a little slower, without touching it.

## Choosing and mounting the pressure sensor

* **Range:** pick one just above the tank height, e.g. 0–1 m H₂O (0–10 kPa)
  for a 40 cm tank. A 0–5 m sensor would only use 8 % of its range, so you
  lose resolution.
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
`PIN_ONEWIRE`).

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
