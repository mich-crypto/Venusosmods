# I/O board – handover for a new session

State on 2026-11-28, branch `claude/inspiring-einstein-00bds5`, last commit `60d8dc2`
(rev 0.4). Read this first, then `README.md` (ordering, checks, routing steps) and
`DESIGN-REVIEW.md` (audit findings, what is and is not verified).

## Where it stands

* **Rev 0.4 is complete and ready for a prototype order at JLCPCB** (full assembly):
  DRC 0 errors / 0 unconnected (38 accepted warnings, listed in README "Checked"),
  525 pads = `tools/parts.py`, every footprint linked to the schematic, decoupling 2.1–4.5 mm
  from the pins, 10 A AUX input 2 mm wide, 4–8 vias per 5 A layer change.
* Order files: `fab/io-board-gerbers.zip` (PCB, ENIG), `fab/jlc/io-board-bom-jlc.csv` +
  `fab/jlc/io-board-cpl-jlc.csv` (assembly, 164 parts). U92 (PCA9615) is **not fitted**
  (`parts.DNP`). Plugs for the pluggable terminals are ordered separately (README "Before ordering" 8).
* The user has **not ordered yet**.

## Open items (in priority order)

1. **J8/J9 RJ12 footprint** (`io_board.pretty/RJ12_Connfly_DS1133-S60_Horizontal`) is derived
   from KiCad's RJ14 DS1133 footprint. The Connfly drawing (LCSC C77859) shows the contact
   stagger mirrored – assumed to be a bottom view. If wrong, the part does not fit and LIN lands
   on pin 4. Verify against the drawing or a real socket before ordering.
2. Datasheets **not read** (download refused or no link): TSR 1-2450 (back-feed from USB when 12 V
   is off, finding L3), SMBJ15A/18A, BZT52C12, ESDA6V1-5SC6, USBLC6, SHT40, Connfly DS1128 RJ45
   (footprint from KiCad's library). Pinouts rest on KiCad's library.
3. Truma RJ12 pinout (pin 3 LIN, pin 5 GND) is from inetbox.py – measure in the van.
4. The planning images in `docs/io-board/` (3D views, schematic sheets, placement) are old;
   the docs say the KiCad files take precedence.
5. Firmware does not yet use the board's pin plan (`docs/io-board.md`, "Pin plan"): tilt INT on
   GPIO47, expansion INT on GPIO40, LMV331 outputs on GPIO14/6, etc.
6. After the prototype arrives: bring-up step by step (README), then rev 0.5 from what is found.

## How the design is built (source of truth: `tools/parts.py`)

* `parts.py` – netlist in Python (`add`, `R`, `C`, `D`), `DNP` set. `lcsc.py` – LCSC number per value.
* `pcb.py` + `gen.py` – generate `io-board.kicad_pcb` and `io-board.kicad_sch` (placement: `FIX`,
  `PAD1` (by pad 1, front-edge parts), `NEAR` (decoupling next to an IC pin), `REG` (packed regions));
  `rules.py` net classes; `zones.py` GND pours. Run in a scratch folder with `io-board.kicad_pro`,
  `io_board.kicad_sym`, `sym-lib-table`, `fp-lib-table` copied in.
* Routing: Freerouting 1.9.0, then `import_ses.py`, `stubs.py`, `finish_routes.py` (own grid A*
  router for what is left: wide nets first, net-class clearances, keep-outs, slot, net-tie copper,
  pad clearances), `via_room.py` / `move_near.py` for local fixes, `finish.py` (pours, via
  clusters, labels, refill). Exact sequence: README "Layout and routing".
* Outputs: `fab.sh` (Gerbers, drill, pos), `bom.py`, `jlc.py`, `render.sh`;
  `kicad-cli sch export netlist|pdf` for `io-board.net` and the PDF.
* Small changes to the routed board: edit `parts.py`, then `apply_netlist.py` (regenerates the
  schematic, re-links footprints, sets pad nets, places new parts), then `finish_routes.py` and
  `finish.py`. A full regenerate (`pcb.py`) discards the routing.

## Environment (cloud container had none of this preinstalled)

```
apt-get install -y kicad kicad-footprints kicad-symbols poppler-utils librsvg2-bin   # KiCad 7.0.11
```

* Freerouting: `https://github.com/freerouting/freerouting/releases/download/v1.9.0/freerouting-1.9.0.jar`;
  needs a display: `xvfb-run -a java -jar freerouting-1.9.0.jar -de in.dsn -do out.ses -mp 100`
  (≈ 20–45 min). Before routing, strip `(plane GND …)` (GND is routed as a net) and the
  `(keepout "" (polygon signal …))` KiCad writes for the SHT40 slot from the DSN. Routing a DSN
  exported from an already routed board can hang ("normalization of net … failed").
* DRC from Python: `pcbnew.WriteDRCReport(board, file, pcbnew.EDA_UNITS_MILLIMETRES, True)`.
  Set `KICAD7_FOOTPRINT_DIR=/usr/share/kicad/footprints` and copy
  `/usr/share/kicad/template/fp-lib-table` to `~/.config/kicad/7.0/` or every footprint shows as
  a library warning. KiCad 7's CLI has no DRC/ERC (KiCad 8+ has both).
* JLCPCB part search (stock, LCSC number, datasheet link):
  `POST https://jlcpcb.com/api/overseas-pcb-order/v1/shoppingCart/smtGood/selectSmtComponentList`
  with JSON `{"keyword": "...", "currentPage": 1, "pageSize": 5, "searchSource": "search"}`;
  `dataManualFileAccessIdUrl` in the result downloads the datasheet PDF. Manufacturer sites
  (TI, NXP, Diodes, AOS work; ST, Littelfuse, Traco, Nexperia often refuse) – extract with `pdftotext`.

## KiCad 7 Python pitfalls met in this project

* `board.Remove(item)` breaks later iteration – use `board.Delete(item)`.
* `ZONE.SetOutline(polyset)` takes ownership → crash; use `zone.Outline().AddOutline(chain)`.
* `ERROR_LOC` enum and `NETINFO_ITEM.GetNetClass()` are not usable from Python – read net classes
  from the `.kicad_pro` JSON (see `finish_routes.py`).
* `BOX2I.Merge` can crash – compute extents by hand. `g is f` compares wrappers, not objects –
  compare references.
* `PCB_TRACK.HitTest(BOX2I, …)` tests the centre line only; use the point test with
  `width/2 + clearance`.
* `import gen` **regenerates `io-board.kicad_sch` with new UUIDs** in the current folder – import it
  only in a scratch folder, or run `apply_netlist.py` afterwards to re-link the board.
* `WriteDRCReport` does not refill zones – refill first (`ZONE_FILLER(b).Fill(b.Zones())`).

## Working agreements with the user

* Develop on `claude/inspiring-einstein-00bds5`; commit and push after each step (a stop hook
  asks for it). No pull request unless asked.
* For reviews: verify against manufacturer datasheets, report what is and is not verified,
  present findings before changing design files (user's audit prompt, see `DESIGN-REVIEW.md`).
* The user writes in English; keep docs in the same plain style.
