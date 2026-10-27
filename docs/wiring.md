# Wiring: ESP32-S3 hydrostatic tank sender

```
 Van 12 V aux ──[fuse 1 A]──┬───────────────────────────────┐
                            │                               │ (4-20 mA sensors only)
                    ┌───────┴───────┐                       │
                    │ Buck MP1584 / │                       │
                    │ LM2596 → 5.0 V│                       │
                    └──┬─────────┬──┘                       │
                    5V │     GND │                          │
                       │         │                          │
            ┌──────────┴─────────┴──────────┐               │
            │ 5V/VIN          GND           │               │
            │        ESP32-S3 Dev Board     │               │
            │ 3V3   GPIO4 (ADC1)   GPIO5    │               │
            └──┬──────┬──────────────┬──────┘               │
               │      │              │                      │
               │   (signal, below)   └──── DS18B20 DATA     │
               ├──[4k7]──────────────────┘                  │
               └──────────────────────────── DS18B20 VDD    │
                         GND ─────────────── DS18B20 GND    │
```

Set the buck converter to 5.0 V with a multimeter **before** connecting the
ESP32. Use the ESP32's 5V/VIN pin, not 3V3.

## Option A: voltage-output sensor (0.5-4.5 V, 5 V supply)

```
 sensor +5V  ── ESP32 5V
 sensor GND  ── GND
 sensor OUT ──[R1 10k]──┬──[1k]── GPIO4
                        │         │
                     [R2 20k]  [100nF]
                        │         │
                       GND       GND
```
Divider ratio (R1+R2)/R2 = 1.5, so 4.5 V becomes 3.0 V at the pin.
Firmware: `INPUT_CURRENT_LOOP 0`, `DIVIDER_RATIO 1.5f`,
`SIGNAL_AT_ZERO 0.5f`, `SIGNAL_AT_FULL 4.5f`.

## Option B: 4-20 mA two-wire transmitter (most common, 12-24 V supply)

```
 12 V (fused) ── sensor +      sensor - ──┬──[1k]── GPIO4
                                          │         │
                                     [R 120 Ω]   [100nF]
                                          │         │
                                         GND       GND
```
4 mA → 0.48 V, 20 mA → 2.4 V. Use a 0.1 % or 1 % resistor.
Firmware: `INPUT_CURRENT_LOOP 1`, `SHUNT_OHMS 120.0f`,
`SIGNAL_AT_ZERO 4.0f`, `SIGNAL_AT_FULL 20.0f`,
`SIGNAL_FAULT_LOW 3.0f`, `SIGNAL_FAULT_HIGH 22.0f`.
A broken wire then shows up as a fault in the GUI instead of "empty".

The 1 kΩ + 100 nF give an RC filter and limit current into the pin's
protection diodes if something goes wrong.

## Sensor choice and mounting

* Range: pick one just above the tank height, e.g. 0-1 m H₂O (0-10 kPa) for a
  40 cm tank. A 0-5 m sensor would only use 8 % of its range.
* It must be a **gauge** (vented) sensor. The cable has a small vent tube:
  keep its end open to air and dry (inside the junction box is fine, never
  outside under the van).
* Lay the probe flat on the tank bottom, as close to the **middle of the
  tank** as you can. When parked on a slope, the water level at the centre
  barely changes, while at the edges it changes a lot.
* Drinking water: use a sensor with a stainless body and an FDA/food-safe
  cable, or mount it outside the tank in a T-piece on the drain at the
  lowest point.

## ADC pins

On the ESP32-S3 only GPIO1..GPIO10 are ADC1. ADC2 (GPIO11..20) shares hardware with the radio
and can return errors while it is active, so keep the sensor on ADC1.
