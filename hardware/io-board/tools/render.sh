#!/bin/sh
# Renders of the bare board: pcb-render-top.png and pcb-render-bottom.png in hardware/io-board.
set -e
cd "$(dirname "$0")/.."
D=/tmp/io-board-render/; rm -rf $D; mkdir -p $D
for side in F B; do
  m=""; [ $side = B ] && m="--mirror"
  for L in Cu Mask Silkscreen; do
    kicad-cli pcb export svg -o $D${side}_$L.svg --layers $side.$L,Edge.Cuts --black-and-white \
      --page-size-mode 2 --exclude-drawing-sheet --drill-shape-opt 2 $m io-board.kicad_pcb >/dev/null
    rsvg-convert -w 2400 -b white $D${side}_$L.svg -o $D${side}_$L.png
  done
done
python3 -c "
import pcbnew, json
b=pcbnew.LoadBoard('io-board.kicad_pcb'); h=[]
for f in b.GetFootprints():
    for p in f.Pads():
        d=p.GetDrillSize()
        if d.x>0: h.append([p.GetPosition().x/1e6,p.GetPosition().y/1e6,min(d.x,d.y)/1e6,p.GetAttribute()==pcbnew.PAD_ATTRIB_PTH])
for t in b.GetTracks():
    if t.Type()==pcbnew.PCB_VIA_T: h.append([t.GetPosition().x/1e6,t.GetPosition().y/1e6,t.GetDrillValue()/1e6,False])
json.dump(h,open('${D}holes.json','w'))"
sed "s#^D = .*#D = '$D'#" tools/render_compose.py > ${D}compose.py
python3 ${D}compose.py
cp ${D}render-top.png pcb-render-top.png; cp ${D}render-bottom.png pcb-render-bottom.png
