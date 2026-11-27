# Make room for a via cluster where a 5 A path changes layer in a crowded spot:
#   python3 via_room.py ../io-board.kicad_pcb
# At each SPOT: thin tracks/vias of other nets within CLEAR mm are removed (finish_routes.py
# routes them again around the cluster), then up to 12 vias 0.6/0.3 are set within 1.2 mm of the
# layer change, each keeping 0.3 mm clearance, and copper squares join them on both layers
# (bottom only next to a net tie: KiCad keeps pours 0.3 mm away from net-tie copper).
# A via is kept only if copper of its net reaches it on both layers (otherwise it carries nothing).
import sys, math, pcbnew
mm = pcbnew.FromMM
SPOTS = [('OUT1_C', 129.115, 130.17)]   # the only 5 A layer change with too few vias (JP1 -> K1 COM)
CLEAR, VIA_D, VIA_DR, CL = 2.4, mm(0.6), mm(0.3), mm(0.3)
HC = ('V12_AUX', 'SH1_P', 'SH1_N', 'OUT1_C', 'SH2_P', 'SH2_N', 'OUT2_C', 'PD_SUPPLY', 'PD_12V',
      'OUT1_NO', 'OUT1_NC', 'OUT2_NO', 'OUT2_NC', 'V12_AUX_IN')
b = pcbnew.LoadBoard(sys.argv[1])
ties = [f for f in b.GetFootprints() if f.GetReference().startswith('NT')]

def square(c, r):
    sq = pcbnew.SHAPE_LINE_CHAIN()
    for dx, dy in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
        sq.Append(c.x + dx * r, c.y + dy * r)
    sq.SetClosed(True); return sq

for k, (net, x, y) in enumerate(SPOTS):
    c = pcbnew.VECTOR2I(mm(x), mm(y)); ni = b.FindNet(net)
    removed = 0
    for t in list(b.GetTracks()):
        if t.GetNetname() != net and t.GetNetname() not in HC and t.HitTest(c, mm(CLEAR)):
            b.Delete(t); removed += 1
    near_tie = any((f.GetPosition() - c).EuclideanNorm() < mm(3.0) for f in ties)
    layers = (pcbnew.B_Cu,) if near_tie else (pcbnew.F_Cu, pcbnew.B_Cu)
    for L in layers:
        z = pcbnew.ZONE(b); z.SetLayer(L); z.SetNet(ni)
        z.Outline().AddOutline(square(c, mm(1.8)))
        z.SetLocalClearance(CL); z.SetMinThickness(mm(0.25))
        z.SetPadConnection(pcbnew.ZONE_CONNECTION_FULL); z.SetAssignedPriority(80 + k); b.Add(z)
    def own_copper(pos, L):
        if L in layers and abs(pos.x - c.x) <= mm(1.5) and abs(pos.y - c.y) <= mm(1.5): return True
        for t in b.GetTracks():
            if t.GetNetname() == net and t.Type() == pcbnew.PCB_TRACE_T and t.GetLayer() == L and t.HitTest(pos, -VIA_D // 4): return True
        return False
    def free(pos):
        for t in b.GetTracks():
            if t.GetNetname() != net and t.HitTest(pos, VIA_D // 2 + CL + mm(0.02)): return False
            if t.GetNetname() == net and t.Type() == pcbnew.PCB_VIA_T and (t.GetPosition() - pos).EuclideanNorm() < mm(0.95): return False
        for f in b.GetFootprints():
            for p in f.Pads():
                if p.GetNetname() != net and p.HitTest(pos, VIA_D // 2 + CL + mm(0.02)): return False
            for g in f.GraphicalItems():                     # net-tie copper
                if g.GetLayer() in (pcbnew.F_Cu, pcbnew.B_Cu) and g.HitTest(pos, VIA_D // 2 + CL + mm(0.02)): return False
        return True
    added = 0
    for r in (0.0, 0.6, 1.0, 1.2):
        for a in range(0, 360, 30 if r else 360):
            pos = pcbnew.VECTOR2I(c.x + int(mm(r) * math.cos(math.radians(a))), c.y + int(mm(r) * math.sin(math.radians(a))))
            if free(pos) and own_copper(pos, pcbnew.F_Cu) and own_copper(pos, pcbnew.B_Cu):
                v = pcbnew.PCB_VIA(b); v.SetPosition(pos); v.SetWidth(VIA_D); v.SetDrill(VIA_DR)
                v.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu); v.SetNet(ni); b.Add(v); added += 1
    total = sum(1 for t in b.GetTracks() if t.Type() == pcbnew.PCB_VIA_T and t.GetNetname() == net and t.HitTest(c, mm(2.0)))
    print(f'{net} at ({x}, {y}): removed {removed} thin segments, +{added} vias, {total} vias at this layer change',
          '(copper square bottom only, net tie near)' if near_tie else '')
pcbnew.SaveBoard(sys.argv[1], b)
