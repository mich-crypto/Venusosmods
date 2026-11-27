# I/O board rev 0.2 – independent design audit

Scope: the whole project as committed at `2b359ed` (requirements in `docs/io-board.md`,
`docs/io-board-schematic.md`, `docs/wiring.md`, KiCad files, BOM, fab outputs, scripts).
Method: five passes (understanding, components, electrical, layout/manufacturing, final
failure sweep). **No design file was changed during this audit.** Every finding below is
either measured on the KiCad files with the KiCad 7 Python API, read from a manufacturer
datasheet (page/table given), or marked as an assumption.

**Verdict: NOT APPROVED** – three HIGH findings must be resolved before ordering (section G).

---

## A. Project understanding

The board replaces the LT 100 panel of a camper's EBL 119 power unit and adds I/O for a Venus OS
system. Functional blocks:

| Block | Parts | Function |
|---|---|---|
| Power | J4 (ST1), J5 AUX+, F1, F2, D1, D2, D4, U1 TSR 1-2450, U2 AP7361C, D3 | ST1 (EBL, 2.5 A PTC inside the EBL) and 12 V AUX (EBL Spare 4, 10 A) OR-ed into V12_SYS → 5 V → 3.3 V; USB VBUS as a third 5 V source |
| Controller | U3 ESP32-S3-WROOM-1-N8, J1 USB-C, U4, SW1/SW2 | Wi-Fi/BLE, native USB programming |
| EBL 119 | U5/U6 AQY212EH, U7/U8 PC817, SW3/SW4, D9 | ON/OFF pulses to ground, "12 V on" and mains sense |
| Tanks | J6 (ST2), J7 (ST3), R/C networks, ESD diodes | pressure sensor (ADC), DS18B20, 4-rod grey sender |
| Outputs | K1/K2 G5LE, Q1/Q2, F4/F5, R50/R51 + U11/U21 INA180A3 + U12/U22 LMV331, JP1/JP2 | 2 × 5 A changeover relays fed from 12 V AUX, "output on" current sense |
| Truma | U60 TJA1021T/20, J8/J9 RJ12, D60, F6 | LIN slave (iNet box emulation), CP plus 12 V supply |
| Inputs | J10, D71–73, U71–73, JP10 | 3 opto inputs, common to GND by default |
| USB PD | F7, Q80, Q81, J2 | switched 12 V to an external 30 W USB-C PD module |
| Sensors / expansion | U90 LIS3DH, U91 SHT40, U92 PCA9615, J3 RJ45, F8, R94–R99 | tilt, humidity, differential I²C + 12 V to expansion boards |

## B. Documentation audit

Reviewed: `docs/io-board.md`, `docs/io-board-schematic.md`, `docs/wiring.md`, `docs/truma.md`,
`hardware/io-board/README.md`, `tools/parts.py` (source netlist), `io-board.kicad_sch`, `.kicad_pcb`,
`.kicad_pro`, `io-board.net`, `io-board-bom.csv`, `fab/*`, DRC report (KiCad 7.0.11).

Datasheets read (text extracted from the manufacturer PDF): NXP PCA9615 Rev. 2 (2021), NXP TJA1021
Rev. 9 (2023), ST LIS3DH Doc 17530 Rev 1, Espressif ESP32-S3-WROOM-1 v1.8, TI LMV331 SLCS136V (2025),
TI INA180 SBOS741H, Diodes AP7361C, AOS AO3401A, Omron G5LE, SK6812MINI Rev. 01, SK6812MINI-E Rev. 02.

**Could not be obtained** (403/404/503 from the manufacturer sites) – these parts are
*not* datasheet-verified in this audit: Panasonic AQY212EH, Traco TSR 1-2450, Littelfuse SMBJ18A,
Sharp PC817, Nexperia PESD1LIN, AO3400A, SHT40, USBLC6-2SC6 (pinouts of the last three only through
KiCad's library). The GCT USB4105 drawing downloaded but is image-only; its footprint is KiCad's
library footprint for that part.

Contradictions between documents → findings M3, L5.

## C. Component verification

| Part | Pinout vs datasheet | Ratings in this circuit | Status |
|---|---|---|---|
| U3 ESP32-S3-WROOM-1-N8 | pins 1–41 match Table "Pin Definitions" (e.g. 13 IO19 USB_D−, 14 IO20 USB_D+, 33 IO40); strapping IO0/3/45/46 not driven | EN RC 10 k/1 µF = the module datasheet's recommendation ("R = 10 kΩ and C = 1 µF") | OK; **N8 only** (R8 variants use IO35–37) |
| U92 PCA9615 | Table 3 (fixed in rev 0.2) | VDD(A) 3.3 V (2.3–5.5), VDD(B) 5 V (3.0–5.5); termination per Fig. 5/7 (added rev 0.2) | OK |
| U60 TJA1021T/20 | Table 3: 1 RXD (open drain), 2 SLP_N, 3 WAKE_N, 4 TXD, 5 GND, 6 LIN, 7 VBAT, 8 INH | VIH(TXD, SLP_N) 2 V min → 3.3 V OK; VBAT limit 40 V | OK |
| U90 LIS3DH | pin description table: 1 VDD_IO, 7 SA0→GND (0x18), 8 CS→3.3 V (I²C), 10 RES→GND, 11 INT1 | INT1 push-pull only (CTRL_REG6 has no open-drain bit) – own GPIO since rev 0.2 | OK |
| U11/U21 INA180A3 | Table 6-1 pinout A: 1 OUT, 2 GND, 3 IN+, 4 IN−, 5 VS | IN± common mode −0.3…26 V abs. max → **see H2** | pins OK |
| U12/U22 LMV331 | Table 4-1 DBV: 1 IN+, 2 GND, 3 IN−, 4 OUT, 5 VCC | VICR −0.1…2 V at VCC 2.7 V, −0.1…4.2 V at 5 V → **see M1** | pins OK |
| U2 AP7361C-33E | SOT223 (-E): 1 IN, 2 GND/tab, 3 OUT | input ≤ 6 V (5 V OK); CIN ≥ 1 µF, COUT ≥ 2.2 µF "as close as possible" → **see H1** | pins OK |
| Q80 AO3401A | 1 G, 2 S, 3 D | VGS ±12 V: −V/2 ≈ −7 V (rev 0.2); VDS −30 V → **see H2** | OK |
| Q1/Q2 AO3400A | 1 G, 2 S, 3 D (KiCad) | VDS 30 V → **see H2** | not DS-verified |
| K1/K2 G5LE-1 12 V | coil 2–5, COM 1, NO 3, NC 4 (terminal drawing p. 3) | 8 A at 30 VDC resistive vs 5 A fuse | OK (inductive loads: I2) |
| D5 SK6812MINI | 1 DOUT, 2 VSS, 3 DIN, 4 VDD | VIH 3.4 V at VDD 5 V; fed 4.3 V via D6 → ≈ 2.9 V | OK, small margin (I4) |
| U5/U6 AQY212EH | KiCad TLP3123 symbol (1 A, 2 K, 3/4 out) | LED 6 mA, 60 V output (from memory) | **not verified** |
| U7/U8/U71–73 PC817 | KiCad symbol (1 A, 2 K, 3 E, 4 C), SMDIP-4 W9.53 footprint | CTR 50 % min assumed | **not verified** |
| U1 TSR 1-2450 | KiCad symbol 1 Vin, 2 GND, 3 Vout | 4.75–36 V in, 1 A (from memory) | **not verified** |
| D4 SMBJ18A, D60 PESD1LIN, U4, U91 | KiCad symbols | – | **not verified** |

## D. Electrical design audit – recalculated values

| Quantity | Calculation | Result |
|---|---|---|
| Battery sense | 15 V × 22 k / 122 k | 2.70 V (ADC1_CH9) ✓ |
| Pressure sensor | 2 m sensor, ≈ 0.4 m tank: 3.3 V × 0.4 / 2 | ≈ 0.66 V max, no clipping ✓ (20 % of range, I1) |
| "Output on" threshold | VREF = 3.3 V × 4.7 k / 104.7 k = 0.148 V; I = 0.148 / (100 × 0.05 Ω) | 30 mA ✓ |
| INA180 output saturates | (3.3 V − 0.03) / 100 / 0.05 Ω | above ≈ 0.65 A load (2.5 V reached at 0.5 A) → M1 |
| Shunt dissipation | 5² × 0.05 | 1.25 W in a 2 W 2512 ✓ |
| Relay coil | G5LE 12 V ≈ 360 Ω: 14.6 V / 360 | 41 mA in AO3400A ✓ |
| PhotoMOS LED | (3.3 − 1.3) / 330 | 6 mA ✓ (GPIO 20 mA default drive) |
| "12 V on" opto | (12 − 4.7 − 1.2) / 4.7 k | 1.3 mA; threshold ≈ 6 V ✓ (spec ≈ 6 V) |
| Mains opto | (8 − 2.4 − 1.2) / 2.2 k | 2.0 mA; threshold ≈ 3.6 V ✓ (spec ≈ 4 V) |
| Inputs at spec max 30 V | (30 − 0.7 − 1.2) / 4.7 k = 6.0 mA; P = 6.0² × 4.7 k | **169 mW in a 100 mW 0603** → M2 |
| Inputs at spec min 5 V | (5 − 1.9) / 4.7 k = 0.66 mA × CTR 50 % | 0.33 mA vs 0.33 mA needed by 10 k pull-up → **no margin** → M2 |
| Q80 gate | 12.8 V × 100 k / 200 k | VGS −6.4 V (≤ ±12 V) ✓ |
| 5 V budget | ESP32 peak 0.5 A + LED 36 mA + PCA9615/termination ≈ 15 mA + sensors | ≈ 0.6 A peak < 1 A TSR-1 ✓ |
| LDO dissipation | (5 − 3.3) × 0.5 A peak / ≈ 0.15 A average | 0.85 W peak, 0.26 W average (SOT223 110 °C/W → +29 °C avg) ✓ |
| dI²C termination | 5 V / (620 + 120 + 620) | 3.7 mA per pair, differential idle 0.44 V ✓ |
| 12 V AUX worst case | 5 + 5 + 2.5 + 1 + 1 | 14.5 A > F2 10 A → I5 |

## E. PCB layout audit

Measured on `io-board.kicad_pcb`: DRC 0 errors, 0 unconnected, 18 warnings (silkscreen of
overhanging parts and button pads, ESP32 footprint vias 0.3 mm). Track/pour/via table for all
power nets is in the README. Additional layout measurements in this audit:

**Decoupling distance (supply pin → nearest capacitor of that net, centre to centre):**

| IC pin | Cap | Distance | Requirement |
|---|---|---|---|
| U3 ESP32 3V3 (pin 2) | C5 10 µF / C6 100 nF | **27.8 mm** | next to the pin (Wi-Fi TX bursts ≈ 0.5 A) |
| U2 AP7361C OUT | C3 22 µF | **19.9 mm** | "as close as possible to OUT and GND" (AP7361C datasheet, Application Information) |
| U2 AP7361C IN | C2 22 µF | **14.7 mm** | "as close as possible to the device" |
| U1 TSR-1 out / in | C2 / C1 | 21.1 / 19.7 mm | close (module) |
| U90 LIS3DH VDD | C90 | 22.7 mm | 100 nF at the pin |
| U91 SHT40 VDD | (C91 is placed elsewhere; nearest +3V3 cap) | **51 mm** | 100 nF at the pin |
| U92 PCA9615 VDD(B) | C93 | 21.7 mm | at the pin |
| D5 SK6812 VDD | C8 | **109 mm** | at the LED |
| U11/U21 INA180, U12/U22 LMV331 | none on the board | – | 0.1 µF CBYPASS (INA180 Fig. "Typical Application") |

Cause: the generator (`tools/pcb.py`) packs small parts into fixed regions instead of next to
the IC they serve. → **H1**.

Other layout observations: USB D+/D− 160/176 mm long with 2–4 vias (M4); 5 A paths 2 mm with
5–9 vias per layer change (OK); antenna keep-out free of copper (OK); GND routed as a net plus
pours on both layers (OK); no fiducials (L1).

## F. Cross-document consistency audit

| # | Document says | Board / BOM has |
|---|---|---|
| 1 | io-board.md: tilt sensor **LIS2DW12** | LIS3DH |
| 2 | io-board.md: USB-C tablet with **PD chip SW3516 / IP6557** on the board; io-board-schematic.md lists **U80 SW3516H** | external PD module on J2 (2-pin header), no U80 |
| 3 | io-board-schematic.md: **2 × USB-C** | one USB-C (J1); the tablet socket is on the external module |
| 4 | io-board-schematic.md: relays **Song Chuan 833H-1C-C** | footprint and BOM Omron G5LE-1 |
| 5 | io-board-schematic.md: optos **U70–U72** | U71–U73 |
| 6 | io-board.md: inputs have a **jumper per input**, "12 V = on" or "contact to ground = on" | one jumper JP10 (common to GND) for all three; "contact to ground" needs +12 V brought to COM externally → M3 |
| 7 | io-board.md: RJ45 pin 7 interrupt **shared with the tilt sensor** | separate since rev 0.2 (EXP_INT_MCU, GPIO40) |
| 8 | io-board.md, Protection: "12 V inputs: fuse, **reverse-polarity protection, TVS**" | only V12_SYS has them; V12_AUX (relays, INA180, Q80, CP plus, expansion) has neither → H2 |
| 9 | io-board.md: SHT40 "cut off from the warm parts by a **slot in the board**" | board outline is a plain rectangle, no slot → L6 |
| 10 | io-board.md / schematic.md: ON/OFF buttons, "12 V on" LED and status LED **on the front plate** | SW3/SW4 at 26 mm, D9/D5 at 12 mm behind the board's front edge, behind ST1–ST3 → H3 |
| 11 | io-board.md header: "Status: planning, the schematic is next" | routed board |

Schematic ↔ PCB: all 500 pads carry the net `parts.py` gives them; every footprint is linked
to its symbol (checked in this audit). BOM ↔ pick-and-place: 153 = 153 parts.

## G. Risk assessment – findings (most severe first)

| ID | Sev. | Where | Problem | Why | Evidence | Correction | Mandatory |
|---|---|---|---|---|---|---|---|
| H1 | HIGH | U2, U3, U1, U90–U92, D5, U11/U12/U21/U22 | Decoupling and regulator capacitors 15–109 mm from the pins; INA180/LMV331 have no bypass capacitor | LDO may oscillate; ESP32 brown-outs / resets on Wi-Fi TX; sensor/I²C errors | AP7361C datasheet (CIN/COUT "as close as possible"); INA180 typical application (0.1 µF CBYPASS); measurements in E | Move C2/C3 to U2 (≤ 3 mm), C5/C6 to U3 pin 2, C90/C91/C92/C93/C8 to their ICs; add 4 × 100 nF at U11/U12/U21/U22 VS/VCC; re-route those nets | **yes** |
| H2 | HIGH | 12 V AUX input (J5-1, F2, V12_AUX net) | No reverse-polarity and no transient protection on V12_AUX | Reversed AUX wiring: Q1/Q2 body diode + D11/D12 (1N4148W, 150 mA) short the supply until F2 (10 A) opens – diodes fail; INA180 inputs see −12 V (abs. max −0.3 V). Transients above 26 V exceed INA180 (26 V), above 30 V Q1/Q2/Q80 (30 V) | INA180 Abs. Max. table; AO3401A VDS −30 V; requirement in io-board.md "Protection" | TVS (SMBJ18A) from V12_AUX to GND after F2, and reverse protection on V12_AUX (P-MOSFET ideal diode, ≥ 15 A, or at minimum a series Schottky on the INA/Q80 branch); re-place/re-route around F2 | **yes** |
| H3 | HIGH | SW3, SW4, D9, D5 | Front-plate controls are 12–26 mm behind the board edge, behind the ST1–ST3 plugs | The spec has them operable/visible through the front plate; as placed they are not reachable | io-board.md "Size and casing", schematic.md 3D view; README open point 4 | Decide: move to the front edge, or put them on a small front-plate board with a cable (connector on this board) | **yes (decision)** |
| M1 | MEDIUM | U12/U22 LMV331 | IN+ (INA180 output) rises to ≈ 3.27 V; LMV331 at 3.3 V has VICR up to ≈ 2.5 V | Comparator output state not guaranteed for loads above ≈ 0.5 A – "output on" could read wrong at high load | LMV331 VICR table (−0.1…2 V at 2.7 V, −0.1…4.2 V at 5 V) | Supply U12/U22 from +5 V (inputs ≤ VCC, output open drain still pulled up to 3.3 V) | recommended |
| M2 | MEDIUM | R71–R73 (inputs) | 4.7 k 0603 dissipates 169 mW at the specified 30 V; at 5 V the opto has no margin | Resistor overload at 24–30 V systems; unreliable input at low voltage | calculation in D; 0603 = 100 mW | Specify inputs 9–30 V and use 1206 (250 mW) or 2 × 0805 in series; or a constant-current input | recommended |
| M3 | MEDIUM | J10, JP10 | One common jumper instead of per-input jumpers; "contact to ground" mode needs external +12 V on COM | Wiring an input "to ground" as documented does nothing | io-board.md "3 inputs" | Either update the spec (12 V = on only, COM to GND) or add +12 V option per input | yes (doc or design) |
| M4 | MEDIUM | USB_DP/USB_DM | 160–176 mm, 2–4 vias, not routed as a pair, length mismatch 16 mm | Works at 12 Mbit/s but EMI/ESD exposure; flashing failures possible with long cables | layout measurement | Optional: move J1 nearer U3 or route as a pair; keep programming cable short | no |
| M5 | MEDIUM | J3 (RJ45), J8/J9 (RJ12) | No ESD protection on DSDA±/DSCL±, EXP_INT, TR1/2/4/6 | Cables of several metres to other boards; PCA9615 only 2 kV HBM | PCA9615 Features (ESD > 2000 V HBM) | TVS array (e.g. 4-line, ≤ 6 V) at J3; INT line clamp | recommended |
| M6 | MEDIUM | U90 LGA-16, U91 DFN-4, U92/J1 0.5 mm | HASL finish is uneven on fine-pitch/LGA pads | Open/short joints on the sensors | general DFM | Order **ENIG** | recommended |
| L1 | LOW | board | No fiducials | Assembly houses can align on pads/holes, but 3 fiducials are standard | DFM | Add 3 × 1 mm fiducials | no |
| L2 | LOW | J1 USB4105 | NPTH pegs 0.194 mm from GND pads (rule lowered to 0.19 mm) | Some fabs require 0.2 mm | DRC rule | Accept (same net) or ask the fab | no |
| L3 | LOW | U1 TSR-1 | USB-only operation back-feeds 4.6 V into the TSR-1 output | Behaviour not stated (datasheet not obtained) | – | Check Traco datasheet; add a Schottky at TSR-1 output if needed | check |
| L4 | LOW | GND return | PD module, CP plus and expansion return ≈ 4.5 A via J5 GND; if J5 GND is missing it flows through ST1 brown (2.5 A PTC in the EBL) | EBL PTC trips, battery reading offset | io-board.md ST1 notes | Document: J5 GND must be wired; optional sense line | no |
| L5 | LOW | docs | Contradictions F1–F5, F7, F11 | Wrong parts ordered / confusion | section F | Update io-board.md and io-board-schematic.md | yes (docs) |
| L6 | LOW | U91 SHT40 | No thermal slot in the board as specified | Humidity/temperature read high near warm parts (D5 LED, J3) | io-board.md | Add a routed slot around U91 or accept | no |
| I1 | INFO | pressure | 2 m sensor uses ≈ 20 % of ADC range on a 0.4 m tank | resolution ≈ 0.6 mm/mV, adequate | wiring.md | 0–1 m sensor would be better | no |
| I2 | INFO | K1/K2 | G5LE 8 A at 30 VDC is a resistive rating | Pumps/motors draw inrush and arc | G5LE ratings table | Keep inductive loads ≤ 3 A or use a contactor | no |
| I3 | INFO | U60 | TXD has an internal pull-down; SLP_N held low by R63 until firmware enables | Bus stays recessive during ESP32 boot ✓ | TJA1021 Table 3 notes | – | no |
| I4 | INFO | D5 | 3.3 V data vs VIH ≈ 2.9 V | 0.4 V margin | SK6812MINI table | Level shifter if flicker is seen | no |
| I5 | INFO | F2 | 14.5 A possible vs 10 A fuse | F2 opens at full simultaneous load | calculation | Documented (decision 3) | no |

**FMEA summary (severity × likelihood, detectability):** H2 reverse AUX: severity high, likelihood
medium (installation error), detectable only after damage. H1 LDO/ESP32 supply: severity high,
likelihood medium-high, detectable at bring-up. H3: severity medium (usability), certain.
M1: severity low-medium (wrong status), likely at high loads, detectable in test.

## H. Recommended corrections (order of work)

1. **H1** – re-place C2, C3, C5, C6, C8, C90–C93 at their pins; add C94–C97 (100 nF) at
   U11/U12/U21/U22; fix the generator so decoupling capacitors are placed next to their IC.
2. **H2** – add TVS on V12_AUX and a reverse-polarity element on 12 V AUX.
3. **H3** – decide front-plate controls (front edge vs separate board with cable).
4. **M1** – LMV331 VCC to +5 V (two pad net changes).
5. **M2/M3** – input resistors 1206, decide input modes, update the spec.
6. **M5/M6** – ESD array at J3; order ENIG.
7. **L5** – bring the docs in line with the board.
8. Re-route the affected areas, DRC, regenerate fab files, repeat this audit's measurements.

## I. Final engineering verdict

**NOT APPROVED** – H1, H2 and H3 must be resolved before manufacturing.

**Verified in this audit:** pinouts of the 11 parts whose datasheets were read; diode/LED
polarity; schematic ↔ PCB net agreement (500 pads); BOM ↔ pick-and-place; DRC; all calculations
in section D; current paths, vias and pours of the 5–10 A nets; decoupling distances.

**Not verified:** AQY212EH, PC817, TSR 1-2450, SMBJ18A, PESD1LIN, AO3400A, SHT40, USBLC6 against
their datasheets (sites refused the download); the USB4105 footprint against GCT's drawing (image
PDF); the Truma RJ12 pinout and the van's wiring (needs a meter); mechanical fit in the casing;
thermal behaviour (estimates only, no simulation); EMC; firmware; a schematic ERC in the KiCad GUI
(KiCad 7's command line has no ERC).
