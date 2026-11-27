# Finishing steps on the routed board (run after import_ses.py, with KiCad 7's python):
#   python3 finish.py ../io-board.kicad_pcb
# - footprints get their library names (so KiCad can compare them with the library)
# - JP10 (solder jumper IN_COM-GND, bridged): its pads are not flooded by the GND plane,
#   the plane would otherwise touch the bridge copper; GND reaches it by a track
# - 12 V AUX copper: a pour along every V12_AUX track (3 mm each side, where free), so the
#   trunk from F2 (10 A) to the output fuses, F7 and the relays is far wider than the 2 mm track
# - GND planes refilled
import sys, os, re, pcbnew
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import parts
mm = pcbnew.FromMM
path = sys.argv[1]
b = pcbnew.LoadBoard(path)

fpname = {p[0]: p[3] for p in parts.P}
for f in b.GetFootprints():
    r = f.GetReference()
    lib, name = (fpname[r].split(':') if r in fpname else ('MountingHole', 'MountingHole_3.2mm_M3'))
    f.SetFPID(pcbnew.LIB_ID(lib, name))

for p in b.FindFootprintByReference('JP10').Pads():
    p.SetZoneConnection(pcbnew.ZONE_CONNECTION_NONE)

# remove an earlier V12_AUX pour (re-running is safe)
for z in list(b.Zones()):
    if z.GetNetname() == 'V12_AUX':
        b.Delete(z)
import math
def capsule(a, c, r, n=12):
    """outline of a track segment a-c widened to radius r (round ends, n points per end)"""
    ang = math.atan2(c.y - a.y, c.x - a.x)
    ch = pcbnew.SHAPE_LINE_CHAIN()
    for (p, start) in ((c, ang - math.pi / 2), (a, ang + math.pi / 2)):
        for k in range(n + 1):
            t = start + math.pi * k / n
            ch.Append(int(p.x + r * math.cos(t)), int(p.y + r * math.sin(t)))
    ch.SetClosed(True)
    return ch
aux = b.FindNet('V12_AUX')
for layer in (pcbnew.F_Cu, pcbnew.B_Cu):
    poly = pcbnew.SHAPE_POLY_SET()
    for t in b.GetTracks():
        if t.GetNetCode() == aux.GetNetCode() and t.Type() == pcbnew.PCB_TRACE_T and t.GetLayer() == layer:
            poly.AddOutline(capsule(t.GetStart(), t.GetEnd(), t.GetWidth() // 2 + mm(3.0)))
    if poly.OutlineCount() == 0:
        continue
    poly.Simplify(pcbnew.SHAPE_POLY_SET.PM_STRICTLY_SIMPLE)
    for i in range(poly.OutlineCount()):
        z = pcbnew.ZONE(b)
        z.SetLayer(layer); z.SetNet(aux)
        z.Outline().AddOutline(poly.Outline(i))   # (SetOutline would take ownership: crash)
        z.SetLocalClearance(mm(0.3)); z.SetMinThickness(mm(0.25))
        z.SetPadConnection(pcbnew.ZONE_CONNECTION_FULL)
        z.SetAssignedPriority(1)          # above the GND planes (priority 0)
        b.Add(z)

# silkscreen warning at the RJ45 expansion socket (12 V on pins 1-2)
for d in list(b.GetDrawings()):
    if isinstance(d, pcbnew.PCB_TEXT) and d.GetText().startswith('EXPANSION 12V'):
        b.Delete(d)
j3 = b.FindFootprintByReference('J3').GetBoundingBox(False, False)
t = pcbnew.PCB_TEXT(b); t.SetText('EXPANSION 12V\nNO ETHERNET'); t.SetLayer(pcbnew.F_SilkS)
t.SetTextSize(pcbnew.VECTOR2I(mm(1.0), mm(1.0))); t.SetTextThickness(mm(0.15))
t.SetPosition(pcbnew.VECTOR2I(j3.GetCenter().x, j3.GetBottom() + mm(2.2))); b.Add(t)

# reference texts that overlap other silkscreen: move to the nearest free spot
silk = []
for f in b.GetFootprints():
    for g in f.GraphicalItems():
        if g.GetLayer() == pcbnew.F_SilkS and not isinstance(g, pcbnew.FP_TEXT):
            silk.append((f.GetReference(), g))
for d in b.GetDrawings():
    if d.GetLayer() == pcbnew.F_SilkS:
        silk.append(('', d))
pads = [p.GetBoundingBox() for f in b.GetFootprints() for p in f.Pads()]
edge = b.GetBoardEdgesBoundingBox()
def ref_free(f, box):
    inner = pcbnew.BOX2I(edge.GetPosition(), edge.GetSize()); inner.Inflate(-mm(0.5))
    if not inner.Contains(box): return False
    big = pcbnew.BOX2I(box.GetPosition(), box.GetSize()); big.Inflate(mm(0.15))
    if any(s.HitTest(big, False, 0) for r, s in silk): return False   # actual lines, so a label may sit inside an outline
    if any(box.Intersects(p) for p in pads): return False
    others = [g.Reference().GetBoundingBox() for g in b.GetFootprints() if g.GetReference() != f.GetReference()]
    return not any(box.Intersects(o) for o in others)
moved = []
for f in b.GetFootprints():
    ref = f.Reference()
    if not ref.IsVisible() or ref.GetLayer() != pcbnew.F_SilkS: continue
    if ref_free(f, ref.GetBoundingBox()): continue
    home = ref.GetPosition(); best = None
    for r in [x * 0.25 for x in range(1, 41)]:
        for k in range(16):
            a = 2 * math.pi * k / 16
            ref.SetPosition(pcbnew.VECTOR2I(home.x + int(mm(r) * math.cos(a)), home.y + int(mm(r) * math.sin(a))))
            if ref_free(f, ref.GetBoundingBox()): best = ref.GetPosition(); break
        if best: break
    ref.SetPosition(best or home)
    if best: moved.append(f.GetReference())
print('reference texts moved:', moved)

pcbnew.ZONE_FILLER(b).Fill(b.Zones())
b.BuildConnectivity()
print('V12_AUX pours', sum(z.GetNetname() == 'V12_AUX' for z in b.Zones()),
      'unrouted', b.GetConnectivity().GetUnconnectedCount(False))
pcbnew.SaveBoard(path, b)
