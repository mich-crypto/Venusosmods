# Short exit tracks from the SHT40 (U91) pads (GND too: the pour cannot enter) past its keep-out (the sensor footprint keeps
# copper out from under the body; Freerouting cannot enter the pad notches, so the pads get a
# 1.2 mm stub outward and the router connects to the stub end).
#   python3 stubs.py ../io-board.kicad_pcb
import sys, pcbnew
mm = pcbnew.FromMM
b = pcbnew.LoadBoard(sys.argv[1])
f = b.FindFootprintByReference('U91')
c = f.GetPosition()
n = 0
for p in f.Pads():
    if any(t.Type() == pcbnew.PCB_TRACE_T and t.GetNetname() == p.GetNetname() and (t.GetStart() == p.GetPosition() or t.GetEnd() == p.GetPosition()) for t in b.GetTracks()):
        continue          # already has its exit track
    pos = p.GetPosition()
    out = 1 if pos.x > c.x else -1          # away from the body, along x
    t = pcbnew.PCB_TRACK(b); t.SetStart(pos); t.SetEnd(pcbnew.VECTOR2I(pos.x + out * mm(1.2), pos.y))
    t.SetWidth(mm(0.25)); t.SetLayer(pcbnew.F_Cu); t.SetNet(p.GetNet()); b.Add(t); n += 1
pcbnew.SaveBoard(sys.argv[1], b); print('stubs', n)
