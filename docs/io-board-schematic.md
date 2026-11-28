# I/O board: schematic, parts and design review

Planning schematic for the board in [io-board.md](io-board.md), drawn sheet by sheet with every
part and value. The sheet images below are the original planning drawings; the **current design
is the KiCad project** (rev 0.3) – where they differ, the KiCad files and the parts table below
are right.

**KiCad project:** [hardware/io-board](../hardware/io-board/README.md) (schematic, placed board,
parts list).

## 3D view

| Board | Casing, lid off | Casing closed (front plate) |
|---|---|---|
| ![board](io-board/3d-board.png) | ![open](io-board/3d-case-open.png) | ![closed](io-board/3d-case-closed.png) |

The casing slides through the LT 100 opening; the front plate (135 × 60 mm) closes it and carries
the USB-C tablet socket, the 12 V ON and OFF buttons, the "12 V on" and status LEDs and the vent slots for the
humidity sensor. The programming USB-C sits behind the plate.

## Schematic sheets

1. [Power](io-board/schematic/sheet1.png): ST1 and 12 V AUX in, OR-ing, TVS, 12→5 V module, 3.3 V LDO, battery voltage
2. [ESP32-S3](io-board/schematic/sheet2.png): module, USB-C programming, reset/boot, all pin assignments
3. [EBL 119 and front plate](io-board/schematic/sheet3.png): ON/OFF photoMOS, "12 V on" and mains inputs, front LED and button
4. [Tanks](io-board/schematic/sheet4.png): ST2 fresh water (pressure sensor + probe), ST3 grey rods
5. [Outputs 1 and 2](io-board/schematic/sheet5.png): relays with current sense
6. [Truma, CP plus 12 V, inputs](io-board/schematic/sheet6.png): LIN transceiver, RJ12 in/out, fused 12 V for the CP plus, 3 inputs
7. [USB PD, sensors, expansion](io-board/schematic/sheet7.png): 30 W tablet charging, tilt + humidity, RJ45 expansion

## Main parts

| Ref | Part | What for |
|---|---|---|
| U3 | ESP32-S3-WROOM-1-N8 | controller, Wi-Fi, Bluetooth |
| U1 | Traco TSR 1-2450 | 12 → 5 V, 1 A |
| U2 | AP7361C-33E (SOT223) | 5 → 3.3 V, 1 A |
| D1, D2 | SS34 | supply OR-ing, reverse polarity of the electronics supply |
| Q3, D13, R7 | AOD4185 P-MOSFET, BZT52C12, 10 k | reverse-polarity protection of 12 V AUX (0.4 W at 5 A) |
| D14 | SMBJ15A | 12 V AUX transient clamp (24.4 V, below the INA180's 26 V) |
| D4 | SMBJ18A | 12 V transient clamp (clamps 29.2 V, below the 35 V parts) |
| F1 / F2 | 0.5 A 30 V PTC / 10 A Littelfuse Nano2 SMD fuse | ST1 supply / 12 V AUX |
| U5, U6 | AQY212EH photoMOS | EBL ON / OFF pulses from the ESP32 |
| SW3, SW4 | Alps SKHHLQA010 right-angle buttons on the front edge | EBL ON / OFF by hand (direct) |
| U7, U8, U71–U73 | PC817 | "12 V on", mains, inputs 1–3 (input resistors 4.7 k 1206 for 30 V) |
| K1, K2 | Songle SRD-12VDC-SL-C, changeover, 10 A | Output 1, Output 2 (5 A fuse each) |
| Q1, Q2 | AO3400 | relay drivers |
| U11, U21 / U12, U22 | INA180A3 / LMV331 (LMV331 on 5 V) | output current sense ("output on"), Kelvin taps NT11–NT22 |
| F4, F5 | 5 A Littelfuse Nano2 SMD fuse | Output 1, Output 2 |
| U60 | TJA1021T/20 | Truma LIN bus |
| D60 | PESD1LIN | LIN ESD protection |
| F6 | 1 A PTC | 12 V for the CP plus |
| J2, F7 | 2-pin header, 4 A fuse | ready-made 12 V → USB-C PD 30 W module (no PD chip on the board) |
| Q80, Q81 | AO3401A, 2N7002 | USB PD on/off (R80/R83 halve the gate voltage, VGS max ±12 V) |
| U90 | LIS3DH | tilt (LGA-16 3 × 3), INT1 push-pull on its own GPIO47 |
| U91 | SHT40 | humidity + temperature |
| U92 | PCA9615 | differential I²C to the RJ45; R94–R99 620/120/620 Ω termination + idle bias on both pairs (needed at each cable end) |
| U4 | USBLC6-2SC6 | USB ESD |
| U93 | ESDA6V1-5SC6 | ESD on the RJ45 expansion lines |
| J | Molex Micro-Fit 3.0: 43045-0600 (ST1, 2 rows), 43650-0600 (ST2), 43650-0500 (ST3); 2 × RJ12 6P6C Connfly DS1133; RJ45 Connfly DS1128; pluggable terminal blocks KF2EDGR (J5 power 4-way, J11/J12 outputs 3-way, J10 inputs 3.5 mm 4-way); 1 × USB-C (programming; the tablet socket is on the PD module) | connectors |

## Design review

Checked against the van measurements, the LT 100 / EBL manuals and the Truma community
projects. What is right, what to check, and what I recommend changing.

**Checked and correct**

* ST1 (EBL) colours, functions and plug position: measured in the van and matching the LT 100
  manual (6-5-4 over 3-2-1, wire side, clip on top).
* ST2 (fresh water): white carries the probe's 3.3 V, so a wrong plug into an LT 100 (12 V on
  white) never reaches the pressure sensor; the tank plug cable half is confirmed.
* The board can't be powered backwards (D1/D2 for the electronics, Q3 for 12 V AUX), transients
  are clamped (D4 on the 12 V system rail, D14 on 12 V AUX), every cable input has a series
  resistor and ESD protection.
* Heavy loads only on 12 V AUX (EBL Spare 4), so the LT 100's old battery sense line (ST1 blue)
  only feeds the electronics and stays an accurate voltage measurement.

**Truma LIN: to check before the layout**

* RJ12 **pin 3 = LIN, pin 5 = GND** comes from the inetbox.py project (Combi 4 + CP plus), not
  from Truma. Check with a meter: pin 3 idles near 12 V and flickers when the CP plus polls,
  pin 5 is 0 V to the van ground.
* **Use a 1:1 RJ12 cable.** Telephone-style RJ12 cables are often crossed: pin 3 then arrives on
  pin 4 and the board hears nothing. Simplest: use Truma's own TIN cable, or check with the meter
  at the board's socket.
* The CP plus must be "iNet ready" (the label on yours says it is).
* The board is a LIN **slave** (like the iNet box): no 1 kΩ master pull-up, the CP plus stays the
  master. That is how it is drawn.
* The CP plus has its **own 12 V supply** (red and red/black wires on its back), separate from
  the bus: the new fused **CP plus 12 V** terminal feeds it from Spare 4, so heating, frost guard
  and timer keep working with the EBL main switch off.

**Decisions**

1. **12 V on/off without the ESP32: done.** Two front buttons (ON, OFF) wired straight to ST1
   green and yellow, like the LT 100's (sheet 3). The ESP32 still switches the EBL itself through
   the photoMOS outputs and sees every change on the "12 V on" wire.
2. **Relays: normal (non-latching), decided.** They drop for a moment when the ESP32 restarts.
3. **EBL Spare 4 fuse: to check in the van.** 12 V AUX can draw up to about 10 A (both outputs,
   30 W charging, CP plus, expansion); the board's F2 is 10 A.
4. **Grey water sensor:** a passive 5-rod probe on about 5 m of cable. Its wire colours from the
   datasheet are uncertain (two of ge/gr): find the common and each level with the beep test.
   The rod inputs use 10 kΩ series, 100 nF and 100 kΩ pull-down (sheet 4) for the long cable.
5. **RJ45 stays**, labelled "EXPANSION – NOT ETHERNET" on the casing.

**Next steps**

* Measure the Truma RJ12 (pin 3 and 5) before connecting the board.
* KiCad schematic, layout and design audit are done (rev 0.3); next is one prototype.
