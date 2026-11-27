# I/O board – KiCad project (rev 0.1, planning)

KiCad 7 project for the board described in [docs/io-board.md](../../docs/io-board.md) and
[docs/io-board-schematic.md](../../docs/io-board-schematic.md). Open `io-board.kicad_pro` in
KiCad 7 or newer.

| File | What |
|---|---|
| `io-board.kicad_sch` | Schematic: 146 parts, every part with a real library symbol and footprint, connected by net labels, grouped in 7 sections (power, ESP32, EBL, tanks, outputs, Truma + inputs, USB PD + sensors + expansion) |
| `io-board.kicad_pcb` | Board 120 × 140 mm with all footprints placed as in the placement plan, nets assigned (ratsnest shown), 4 × M3 holes. **Not routed yet** |
| `io-board-schematic.pdf` | The schematic as PDF |
| `io-board-bom.csv` | Parts list grouped by value and footprint |
| `io-board.net` | Netlist exported by KiCad |
| `pcb-placement.png` | Preview of the placement |
| `io_board.kicad_sym`, `sym-lib-table` | Project library with the PCA9615 symbol (not in the KiCad library) |
| `tools/` | The scripts that generate the schematic and the board from `parts.py` (the netlist in plain Python) |

![placement](pcb-placement.png)

## Checked

* KiCad reads the schematic and exports the netlist: 146 components, 111 named nets; the only
  one-pin nets are pins left open on purpose (spare ESP32 pins, sensor pins not used, SBU, LED DOUT).
* Electrical rules checked on KiCad's netlist: no two outputs on one net, every power input fed
  (power flags on GND, V12_SYS, V12_AUX, LIN_VBAT and the supplies).
* All 146 footprints are linked to their symbols (Tools → Update PCB from Schematic works).
* No footprints overlap; all inside the outline (the ESP32 antenna keep-out sits at the back edge).
* Silkscreen: only small reference designators (placed so none overlaps a pad or another text),
  pin names next to every push-in terminal (J5: AUX+, GND, O1 C/NO/NC, O2 C/NO/NC, CP+, GND;
  J10: COM, IN1–IN3); part values are on the Fab layer only.
* KiCad 7's command line has no ERC/DRC: run **Inspect → Electrical Rules Checker** and, after
  routing, the DRC in the KiCad GUI.

## Before ordering

1. **Truma RJ12:** confirm pin 3 = LIN, pin 5 = GND with a meter; use a 1:1 cable.
2. **PCA9615 pinout** (custom symbol): check against the NXP datasheet.
3. **Connector orientation:** Micro-Fit (ST1–ST3), USB-C, RJ12/RJ45 and the push-in terminals
   must open towards their board edge; turn them in the layout if needed.
4. **Front plate parts:** the 12 V ON/OFF buttons (12 mm), the "12 V on" LED and the status LED
   are on the board's front edge here; if they sit in the front plate instead, put them on a
   small cable or a second board.
5. **USB-C tablet charging:** a ready-made 12 V → USB-C PD 30 W module on J2 (its socket in the
   front plate), switched by Q80; no PD chip on this board.
6. **Tilt sensor:** symbol and footprint are LIS3DH (LGA-16 3 × 3); LIS2DH12 is the pin-compatible
   current part.
7. **Routing:** 2 layers, ground pour on both sides; wide tracks (≥ 2 mm, or copper pours) for
   12 V AUX, the outputs and the relay contacts (5 A); keep the antenna end free of copper.
8. Then DRC, gerbers, one prototype.

## Regenerating

The schematic and the board are generated from `tools/parts.py`: change the netlist there and
run, with KiCad 7's Python (`pcbnew` module):

```
cd hardware/io-board/tools && python3 pcb.py
```

It writes `io-board.kicad_sch` and `io-board.kicad_pcb` into the current folder. Once routing
has started in KiCad, edit the KiCad files directly instead.
