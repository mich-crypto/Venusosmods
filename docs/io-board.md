# I/O board: specification (planning)

One printed circuit board that replaces the hand-wired I/O extension (ESP32-S3 dev board,
buck converter, resistors). It reads the tanks, talks to the Truma CP plus, switches the
EBL 119 on and off, and has relays, inputs and a tilt sensor. Status: **planning**, all
connections below are agreed and the van wiring is measured; the schematic is next.

## Overview

| Block | What | Connection |
|---|---|---|
| ESP32-S3 | Module **ESP32-S3-WROOM-1-N8** (antenna on the module; the box sits in the living area, plastic surroundings) | – |
| Power | From the EBL ↔ LT 100 cable (ST1, blue + / brown −), optional second input **12 V AUX** for relays, outputs and display; two diodes pick the input with power | ST1 + push-in |
| EBL 119 on/off | Pass-through on ST1: short pulse to ground on ON (green) or OFF (yellow); reads "12 V on" (white) and mains (pink) | 2 × Micro-Fit 6-pin, 2 rows |
| Fresh water | Pressure sensor QDY30A 0–3.3 V + water temperature probe | Micro-Fit 6-pin, 1 row, label **PRESSURE** |
| Grey water | Rod sender (common + 4 rods), same plug as on the LT 100 | Micro-Fit 5-pin, 1 row |
| Truma CP plus | LIN bus, TJA1021 transceiver | RJ12 |
| 2 relays | 10 A at 14 V DC, changeover contacts (C / NO / NC), LED each | push-in, ≥ 10 A |
| 2 silent outputs | Low-side MOSFET, about 3 A, flyback diode, LED each | push-in |
| 4 inputs | Isolated (optocoupler), 5–30 V; jumper per input: "12 V on alarm" or "switches to ground" | push-in |
| Tilt | Accelerometer LIS2DW12 (levelling, water tank slope correction, movement) | on the board |
| Temperature probe 2 | DS18B20 | push-in |
| Display power | 12 V out, fused 1 A, for the wall display (its own ESP32, data over Wi-Fi) | push-in |
| Expansion | I²C + 3.3 V + GND (e.g. humidity sensor) | 4-pin header |
| Service | USB-C (flashing, native USB), Boot and Reset buttons, RGB status LED | – |
| Not on the board | Buzzer, RS485, LT 100 tank LED drive | – |

Enclosure: ABS box about 120 × 80 mm with cable glands, screwed to the wall; the board
screwed down firmly (the tilt sensor needs it).

## EBL 119 ↔ LT 100 cable (ST1) – measured

![ST1 pinout](io-board/ebl-lt100-st1.png)

| Pin* | Wire | Function | Idle / off | Active / on | On the board |
|---|---|---|---|---|---|
| 6 | blue | +12 V permanent | 12 V | 12 V | supply + |
| 4 | brown | ground | 0 V | 0 V | supply − |
| 2 | green | ON button | 12 V | pulled to 0 V | pulse to ground |
| 1 | yellow | OFF button | 12 V | pulled to 0 V | pulse to ground |
| 3 | white | "12 V on" indicator | ≈ 2 V | 12 V | input, threshold ≈ 6 V |
| 5 | pink | mains (230 V) indicator | 0 V | ≈ 8 V | input, threshold ≈ 4 V |

\* Pin numbers from the LT 100 wiring diagram; colours and functions are measured in the van.
Check the moulded pin-1 mark before crimping the extension cable.

* The board sits between the EBL and the LT 100 (plug in, plug out); the LT 100 buttons keep working.
* The ON/OFF switches are optocoupler outputs to ground, pulse about 300 ms; the white wire confirms.
* The blue/brown lines also feed the LT 100's battery voltage display. The electronics (≈ 50 mA)
  are fine on them; relays, outputs and the display should use **12 V AUX** (EBL "Spare 4",
  unswitched, or the battery with its own fuse) so the LT 100 keeps reading the right voltage.
* EBL notes (manual 811.0421): heater, frost valve, fridge, step light and Spare 4 stay on when
  the main switch is off; below 10.5 V everything except the frost valve is cut.

## Fresh water tank

![Fresh water rods to the LT 100](io-board/fresh-water-rods-lt100.png)

The rod sender stays on the LT 100 (ST2: grey Full, yellow ¾, green ½, brown ¼, white Base
with the panel's 12 V, 6 empty). The board measures the fresh tank with the pressure sensor on
its own 6-pin socket:

| Pin | Signal |
|---|---|
| 1 | +5 V sensor supply |
| 2 | GND |
| 3 | sensor signal (10 kΩ + RC to GPIO4) |
| 4 | DS18B20 data (water temperature) |
| 5 | +3.3 V for the DS18B20 |
| 6 | shield / not connected |

The LT 100 rod cable uses the same plug type: label the board socket **PRESSURE** so they
can't be swapped.

## Grey water tank

Micro-Fit 5-pin, same plug as the LT 100's grey socket (ST3), moved to the board; the LT 100's
grey LEDs stay dark. The board drives the common rod itself (short pulses, no constant voltage,
see [wiring.md](wiring.md#grey-water-tank-rod-sender-5-wire-plug)).

## Inputs

| Input | Use | Signal |
|---|---|---|
| 1 | Gas/CO alarm Karman 3Gas+ (white wire "Alarm out") | **12 V on alarm** (measured) |
| 2 | Smoke alarm (separate 12 V detector with alarm contact; the 3Gas+ does not detect smoke) | to choose |
| 3, 4 | spare | – |

Mains present and "12 V on" come from ST1 and don't use these inputs.

## Pin plan (ESP32-S3)

| Function | GPIO |
|---|---|
| Pressure sensor | 4 |
| Temperature probe 1 / 2 | 5 / 6 |
| Grey tank: common / rods (lowest first) | 9 / 1, 2, 7, 8 |
| Supply voltage measurement | 10 |
| Inputs 1–4 | 11, 12, 13, 14 |
| I²C (tilt sensor, expansion) | 15, 16 |
| Tilt sensor interrupt | 47 |
| Truma LIN RX / TX / SLP | 17 / 18 / 21 |
| Relay 1 / 2 | 38 / 39 |
| Silent output 1 / 2 | 40 / 41 |
| EBL ON / OFF / "12 V on" | 35 / 36 / 37 |
| Mains indicator | 42 |
| RGB status LED | 48 |
| USB-C | 19, 20 |
| Kept free | 45, 46 (strapping), 0, 3 |

GPIO35–37 are free on the N8 module (no octal PSRAM). Only GPIO1–10 have the ADC that works
together with Wi-Fi, so all analog signals are there.

## Protection

* 12 V inputs: fuse (polyfuse), reverse-polarity protection, TVS diode (van transients).
* Sensor, rod and probe lines: series resistors and ESD protection.
* Relay coils and outputs: flyback diodes.
* Power use about 0.5 W (≈ 1 Ah/day from the leisure battery).

## Firmware and pages (after the board)

* EBL: "12 V system" switch in Venus OS, on the mods page and on the display; mains present as a status.
* Inputs: gas/CO and smoke as Venus OS alarms and notifications.
* Relays and outputs: switches in Venus OS (switch pane) and on the display.
* Tilt: levelling page (pitch, roll, how much to raise which wheel), water volume corrected for
  slope, "set level" calibration, movement alarm.

## Still open

* Smoke detector model (12 V with alarm contact).
* Confirm the pin-1 marks of ST1 and ST2.
* Schematic, parts list with order numbers, layout (KiCad), then one prototype before more.
