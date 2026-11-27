#!/bin/sh
# Manufacturing files from io-board.kicad_pcb (KiCad 7 kicad-cli): gerbers, drill, pick-and-place, zip.
# usage: cd hardware/io-board && sh tools/fab.sh
set -e
OUT=fab; rm -rf $OUT; mkdir -p $OUT/gerbers
kicad-cli pcb export gerbers -o $OUT/gerbers/ --subtract-soldermask \
  -l F.Cu,B.Cu,F.Paste,B.Paste,F.Silkscreen,B.Silkscreen,F.Mask,B.Mask,Edge.Cuts io-board.kicad_pcb
kicad-cli pcb export drill -o $OUT/gerbers/ --format excellon --excellon-separate-th \
  --generate-map --map-format gerberx2 io-board.kicad_pcb
kicad-cli pcb export pos -o $OUT/io-board-pos.csv --format csv --units mm --side both io-board.kicad_pcb
cp io-board-bom.csv $OUT/
(cd $OUT/gerbers && zip -q ../io-board-gerbers.zip *)
ls -l $OUT $OUT/gerbers
