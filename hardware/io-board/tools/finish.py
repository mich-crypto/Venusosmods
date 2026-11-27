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

import math, json, fnmatch
_pro = json.load(open(os.path.splitext(path)[0] + '.kicad_pro'))['net_settings']
def netclass(name):
    for pat in _pro['netclass_patterns']:
        if fnmatch.fnmatchcase(name, pat['pattern']): return pat['netclass']
    return 'Default'
HC = sorted({n for n in b.GetNetsByName().keys() if n and netclass(str(n)) == 'HighCurrent'}, key=str)
HC = [str(n) for n in HC]
POUR = {n: 3.0 for n in HC if not n.startswith('SH')}   # 5-10 A nets: up to 3 mm extra copper each side
# (not the short shunt nets SHn_P/SHn_N: a pour there would run into the Kelvin net-tie copper)
POUR.update({'TRUMA_12V': 1.5, 'EXP_12V': 1.5})   # 1 A PTC (trips at about 2 A)

# remove pours/patches from an earlier run (re-running is safe)
for z in list(b.Zones()):
    if not z.GetIsRuleArea() and z.GetNetname() in POUR:
        b.Delete(z)
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
def add_zone(netinfo, layer, outline, prio):
    z = pcbnew.ZONE(b)
    z.SetLayer(layer); z.SetNet(netinfo)
    z.Outline().AddOutline(outline)          # (SetOutline would take ownership: crash)
    z.SetLocalClearance(mm(0.3)); z.SetMinThickness(mm(0.25))
    z.SetPadConnection(pcbnew.ZONE_CONNECTION_FULL)
    z.SetAssignedPriority(prio)              # above the GND planes (priority 0)
    b.Add(z)
for k, name in enumerate(sorted(POUR)):
    ni = b.FindNet(name)
    if ni is None: continue
    for layer in (pcbnew.F_Cu, pcbnew.B_Cu):
        poly = pcbnew.SHAPE_POLY_SET()
        for t in b.GetTracks():
            if t.GetNetCode() == ni.GetNetCode() and t.Type() == pcbnew.PCB_TRACE_T and t.GetLayer() == layer:
                poly.AddOutline(capsule(t.GetStart(), t.GetEnd(), t.GetWidth() // 2 + mm(POUR[name])))
        if poly.OutlineCount() == 0: continue
        poly.Simplify(pcbnew.SHAPE_POLY_SET.PM_STRICTLY_SIMPLE)
        for i in range(poly.OutlineCount()):
            add_zone(ni, layer, poly.Outline(i), 1 + k)

# 5 A layer changes: one 0.3 mm via carries only about 1-1.5 A. Around every via of a HighCurrent
# net: a 3.6 mm copper square on both layers and up to 8 extra vias, where the clearances allow.
VIA_D, VIA_DR, CL = mm(0.6), mm(0.3), mm(0.3)
edge = b.GetBoardEdgesBoundingBox(); edge.Inflate(-mm(1.0))
keep = [z for f in b.GetFootprints() for z in f.Zones() if z.GetIsRuleArea()]
def via_free(net, pos):
    if not edge.Contains(pos): return False
    box = pcbnew.BOX2I(pcbnew.VECTOR2I(pos.x - VIA_D // 2 - CL, pos.y - VIA_D // 2 - CL), pcbnew.VECTOR2I(VIA_D + 2 * CL, VIA_D + 2 * CL))
    for t in b.GetTracks():         # point test: distance to the centre line minus half the width
        if t.GetNetname() != net and t.HitTest(pos, VIA_D // 2 + CL + mm(0.02)): return False
    for f in b.GetFootprints():
        for p in f.Pads():
            pb = p.GetBoundingBox()
            if p.GetNetname() != net and pb.Intersects(box): return False
            if p.GetDrillSize().x and pb.Intersects(box) and p.GetNetname() == net: return False
    if any(z.Outline().Contains(pos) for z in keep): return False
    return True
cluster = []
extra = []
hcv = [t for t in b.GetTracks() if t.Type() == pcbnew.PCB_VIA_T and t.GetNetname() in HC]
done = []
for v in hcv:
    net = v.GetNetname(); c = v.GetPosition(); added = 0
    near = [w for w in hcv if w.GetNetname() == net and (w.GetPosition() - c).EuclideanNorm() < mm(1.7)]
    if len(near) > 2 or any((c - d).EuclideanNorm() < mm(2.5) for d in done):   # cluster exists already
        continue
    if any(f.GetReference().startswith('NT') and (f.GetPosition() - c).EuclideanNorm() < mm(3.0) for f in b.GetFootprints()):
        continue      # next to a Kelvin net tie: sense branch, no current (and pours must keep off the tie)
    done.append(c)
    sq = pcbnew.SHAPE_LINE_CHAIN()
    for dx, dy in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
        sq.Append(c.x + dx * mm(1.8), c.y + dy * mm(1.8))
    sq.SetClosed(True)
    for layer in (pcbnew.F_Cu, pcbnew.B_Cu):
        add_zone(v.GetNet(), layer, sq, 50 + len(done))
    for dx in (-1.1, 0, 1.1):
        for dy in (-1.1, 0, 1.1):
            if dx == 0 and dy == 0: continue
            pos = pcbnew.VECTOR2I(c.x + mm(dx), c.y + mm(dy))
            if via_free(net, pos):
                nv = pcbnew.PCB_VIA(b); nv.SetPosition(pos); nv.SetWidth(VIA_D); nv.SetDrill(VIA_DR)
                nv.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu); nv.SetNet(v.GetNet()); b.Add(nv); added += 1
                extra.append(nv)
    cluster.append((net, added + 1))
# let KiCad's DRC judge the extra vias against the filled pours: drop any it flags, repeat
import tempfile
while True:
    pcbnew.ZONE_FILLER(b).Fill(b.Zones())
    rpt = tempfile.mktemp(suffix='.rpt'); pcbnew.WriteDRCReport(b, rpt, pcbnew.EDA_UNITS_MILLIMETRES, True)
    txt = open(rpt).read(); os.unlink(rpt)
    bad = set()
    for blk in re.split(r'\n(?=\[)', txt):
        if not blk.startswith(('[clearance', '[hole_clearance', '[hole_to_hole', '[copper_sliver', '[shorting')): continue
        for x, y, desc in re.findall(r'@\(([-\d.]+) mm, ([-\d.]+) mm\): (.*)', blk):
            if desc.startswith('Via'):
                pt = pcbnew.VECTOR2I(mm(float(x)), mm(float(y)))
                for nv in extra:
                    if nv.GetPosition() == pt: bad.add(nv)
    if not bad: break
    for nv in bad:
        extra.remove(nv); b.Delete(nv)
    print('extra vias removed by DRC:', len(bad))
count = {}
for nv in extra: count[nv.GetNetname()] = count.get(nv.GetNetname(), 0) + 1
print('HighCurrent layer changes:', [(n, k) for n, k in cluster], '| extra vias kept per net:', count)

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

seen = set()                        # exact duplicate vias (same net, same spot)
for v in [t for t in b.GetTracks() if t.Type() == pcbnew.PCB_VIA_T]:
    k = (v.GetPosition().x, v.GetPosition().y, v.GetNetCode())
    if k in seen: b.Delete(v)
    else: seen.add(k)
for z in b.Zones():                 # no copper islands (unconnected fill pieces)
    if not z.GetIsRuleArea(): z.SetIslandRemovalMode(pcbnew.ISLAND_REMOVAL_MODE_ALWAYS)
pcbnew.ZONE_FILLER(b).Fill(b.Zones())
b.BuildConnectivity()
print('pours', sum(z.GetNetname() in POUR for z in b.Zones()),
      'unrouted', b.GetConnectivity().GetUnconnectedCount(False))
pcbnew.SaveBoard(path, b)
