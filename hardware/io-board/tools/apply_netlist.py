# Bring a routed board in line with parts.py without re-placing or re-routing it
# (KiCad 7 python, run in hardware/io-board/):  python3 tools/apply_netlist.py
# - regenerates io-board.kicad_sch (gen.py) and links every footprint to its new symbol
# - every pad gets the net parts.py gives it; nets that no longer exist lose their tracks/vias
# - parts that are new in parts.py are placed on free board area near ANCHOR (no copper of other
#   nets under their pads on F.Cu, no courtyard overlap) and then routed by finish_routes.py
# - footprints no longer in parts.py are reported, not deleted
import os, sys, math, pcbnew
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
import parts, gen            # gen writes io-board.kicad_sch into the current folder
mm = pcbnew.FromMM
FPDIR = '/usr/share/kicad/footprints'
ANCHOR = {'R94': (200, 196), 'R95': (200, 196), 'R96': (200, 196),     # near J3 (RJ45), board mm
          'R97': (200, 196), 'R98': (200, 196), 'R99': (200, 196)}
b = pcbnew.LoadBoard('io-board.kicad_pcb')
nets = b.GetNetsByName()
def net(n):
    if nets.has_key(n): return nets[n]
    ni = pcbnew.NETINFO_ITEM(b, n); b.Add(ni); return ni

fps = {f.GetReference(): f for f in b.GetFootprints()}
want = {p[0] for p in parts.P}
print('on board but not in parts.py:', sorted(r for r in fps if r not in want and not r.startswith('H')))

def bynum(sym, conn):
    ps = gen.sym_pins(sym); out = {}
    for k, v in conn.items():
        if k.startswith('@'):
            for p in ps:
                if p['name'] == k[1:]: out[p['num']] = v
        else: out[k] = v
    return out

def free_spot(fp, ax, ay):
    """nearest position to (ax, ay) where the footprint's pads touch no other-net copper on F.Cu
    and its courtyard box overlaps no other footprint"""
    edge = b.GetBoardEdgesBoundingBox(); edge.Inflate(-mm(1.5))
    others = [f.GetBoundingBox(False, False) for f in b.GetFootprints() if f.GetReference() != fp.GetReference()]
    tracks = [t for t in b.GetTracks() if t.IsOnLayer(pcbnew.F_Cu)]
    for r in [x * 0.5 for x in range(0, 60)]:
        for k in range(max(1, int(r * 8))):
            a = 2 * math.pi * k / max(1, int(r * 8))
            fp.SetPosition(pcbnew.VECTOR2I(mm(ax + r * math.cos(a)), mm(ay + r * math.sin(a))))
            bb = fp.GetBoundingBox(False, False)
            if not edge.Contains(bb) or any(bb.Intersects(o) for o in others): continue
            ok = True
            for p in fp.Pads():
                pb = p.GetBoundingBox(); pb.Inflate(mm(0.35))
                if any(t.GetNetname() != p.GetNetname() and t.HitTest(pb, False, 0) for t in tracks): ok = False; break
            if ok: return True
    return False

added = []
for ref, sym, val, fpname, conn, note in parts.P:
    f = fps.get(ref)
    if f is None:
        lib, name = fpname.split(':')
        f = pcbnew.FootprintLoad(f'{FPDIR}/{lib}.pretty', name)
        f.SetReference(ref); f.SetFPID(pcbnew.LIB_ID(lib, name))
        f.Reference().SetTextSize(pcbnew.VECTOR2I(mm(0.8), mm(0.8))); f.Reference().SetTextThickness(mm(0.12))
        f.Value().SetLayer(pcbnew.F_Fab)
        b.Add(f); added.append(ref)
    f.SetValue(val)
    f.SetPath(pcbnew.KIID_PATH('/' + gen.SYMUUID[ref]))
    m = bynum(sym, conn)
    for pad in f.Pads():
        n = m.get(pad.GetNumber())
        if n and n != 'NC': pad.SetNet(net(n))
        elif pad.GetNumber() in m: pad.SetNet(net(''))  # NC
    if ref in added:
        ax, ay = ANCHOR.get(ref, (160, 170))
        print(ref, 'placed' if free_spot(f, ax, ay) else 'NO FREE SPOT', round(f.GetPosition().x / 1e6, 1), round(f.GetPosition().y / 1e6, 1))

# copper of nets that no pad uses any more
used = {p.GetNetname() for f in b.GetFootprints() for p in f.Pads()}
gone = 0
for t in list(b.GetTracks()):
    if t.GetNetname() not in used: b.Delete(t); gone += 1
print('new parts', added, '| tracks/vias of removed nets deleted:', gone)
pcbnew.SaveBoard('io-board.kicad_pcb', b)
