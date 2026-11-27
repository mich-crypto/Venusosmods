# Routes the last open connections that the autorouter left (grid A* on both layers, 0.1 mm grid,
# vias 0.6/0.3), respecting every other net's copper with its net-class clearance, keep-outs and
# the board edge. Zones are ignored as obstacles (they refill around the new tracks).
#   python3 finish_routes.py ../io-board.kicad_pcb
import sys, math, heapq, pcbnew
mm = pcbnew.FromMM
G = mm(0.1)                       # grid
VIA_D, VIA_DRILL = mm(0.6), mm(0.3)
EDGE = mm(0.5)
path = sys.argv[1]
b = pcbnew.LoadBoard(path)
ds = b.GetDesignSettings()
LAYERS = (pcbnew.F_Cu, pcbnew.B_Cu)

import json, fnmatch, os.path
_pro = json.load(open(os.path.splitext(path)[0] + '.kicad_pro'))['net_settings']
_cls = {c['name']: (mm(c['track_width']), mm(c['clearance'])) for c in _pro['classes']}
def nc(net):            # net class (width, clearance) from the project's classes and name patterns
    name = net.GetNetname()
    for pat in _pro['netclass_patterns']:
        if fnmatch.fnmatchcase(name, pat['pattern']): return _cls[pat['netclass']]
    return _cls['Default']

def copper_items():
    for t in b.GetTracks():
        yield t
    for f in b.GetFootprints():
        for p in f.Pads():
            yield p


def route(net, src_item, dst_item, margin=12):
    w, cl_own = nc(net)
    half = w // 2
    pts = [src_item.GetPosition(), dst_item.GetPosition() if dst_item.Type() != pcbnew.PCB_TRACE_T else dst_item.GetStart()]
    x0 = min(p.x for p in pts) - mm(margin); x1 = max(p.x for p in pts) + mm(margin)
    y0 = min(p.y for p in pts) - mm(margin); y1 = max(p.y for p in pts) + mm(margin)
    bb = b.GetBoardEdgesBoundingBox()
    x0 = max(x0, bb.GetX() + EDGE + half); y0 = max(y0, bb.GetY() + EDGE + half)
    x1 = min(x1, bb.GetRight() - EDGE - half); y1 = min(y1, bb.GetBottom() - EDGE - half)
    NX = (x1 - x0) // G + 1; NY = (y1 - y0) // G + 1
    blocked = {L: bytearray(NX * NY) for L in LAYERS}     # track centre not allowed
    vblock = bytearray(NX * NY)                             # via centre not allowed
    def mark(item, layer, inflate, arr):
        box = item.GetBoundingBox()
        gx0 = max(0, (box.GetX() - inflate - x0) // G); gx1 = min(NX - 1, (box.GetRight() + inflate - x0) // G + 1)
        gy0 = max(0, (box.GetY() - inflate - y0) // G); gy1 = min(NY - 1, (box.GetBottom() + inflate - y0) // G + 1)
        for gy in range(gy0, gy1 + 1):
            for gx in range(gx0, gx1 + 1):
                i = gy * NX + gx
                if arr[i]: continue
                pt = pcbnew.VECTOR2I(x0 + gx * G, y0 + gy * G)
                if item.HitTest(pt, inflate) if item.Type() != pcbnew.PCB_PAD_T else item.HitTest(pt, inflate):
                    arr[i] = 1
    for it in copper_items():
        if it.GetNetCode() == net.GetNetCode() and it.GetNetCode() != 0:
            continue
        other_cl = nc(it.GetNet())[1] if it.GetNetCode() else cl_own
        cl = max(cl_own, other_cl) + mm(0.02)
        for L in LAYERS:
            if it.IsOnLayer(L):
                mark(it, L, cl + half, blocked[L])
                mark(it, L, cl + VIA_D // 2, vblock)
        if it.Type() == pcbnew.PCB_PAD_T and it.GetDrillSize().x > 0:        # holes block both layers
            for L in LAYERS: mark(it, L, mm(0.25) + half, blocked[L])
    for f in b.GetFootprints():                      # footprint copper drawings (net-tie bridges)
        for g in f.GraphicalItems():
            for L in LAYERS:
                if g.GetLayer() == L:
                    mark(g, L, cl_own + half + mm(0.02), blocked[L]); mark(g, L, cl_own + VIA_D // 2 + mm(0.02), vblock)
    for z in b.Zones():                             # board-level keep-outs (SHT40 slot)
        if z.GetIsRuleArea() and z.GetDoNotAllowTracks():
            for L in LAYERS:
                if z.IsOnLayer(L): mark(z, L, half, blocked[L])
            mark(z, pcbnew.F_Cu, VIA_D // 2, vblock)
    for d in b.GetDrawings():                       # board outline and cut-outs: 0.5 mm edge clearance
        if d.GetLayer() == pcbnew.Edge_Cuts:
            for L in LAYERS: mark(d, L, EDGE + half, blocked[L])
            mark(d, pcbnew.F_Cu, EDGE + VIA_D // 2, vblock)
    for f in b.GetFootprints():                      # keep-outs (tracks/vias not allowed)
        for z in f.Zones():
            if z.GetIsRuleArea() and z.GetDoNotAllowTracks():
                for L in LAYERS:
                    if z.IsOnLayer(L):
                        mark(z, L, half, blocked[L])
                mark(z, pcbnew.F_Cu, VIA_D // 2, vblock)
    # same-net targets: cells inside the destination item
    def cells_in(item, layer):
        s = set(); box = item.GetBoundingBox()
        for gy in range(max(0, (box.GetY() - y0) // G), min(NY - 1, (box.GetBottom() - y0) // G + 1) + 1):
            for gx in range(max(0, (box.GetX() - x0) // G), min(NX - 1, (box.GetRight() - x0) // G + 1) + 1):
                pt = pcbnew.VECTOR2I(x0 + gx * G, y0 + gy * G)
                if not item.HitTest(pt, 0): continue
                if item.Type() == pcbnew.PCB_TRACE_T:      # join tracks only at their ends
                    r = item.GetWidth() // 2
                    if min(math.hypot(pt.x - e.x, pt.y - e.y) for e in (item.GetStart(), item.GetEnd())) > r: continue
                s.add((gx, gy, layer))
        return s
    src = set(); dst = set()
    for L in LAYERS:
        if src_item.IsOnLayer(L): src |= cells_in(src_item, L)
        if dst_item.IsOnLayer(L): dst |= cells_in(dst_item, L)
    for (gx, gy, L) in src | dst:           # own copper is always enterable
        blocked[L][gy * NX + gx] = 0
    tx = sum(c[0] for c in dst) / len(dst); ty = sum(c[1] for c in dst) / len(dst)
    h = lambda gx, gy: math.hypot(gx - tx, gy - ty)
    DIRS = [(1,0,1),(-1,0,1),(0,1,1),(0,-1,1),(1,1,1.414),(1,-1,1.414),(-1,1,1.414),(-1,-1,1.414)]
    VIA_COST = 40
    openh = []; g = {}; prev = {}
    for c in src:
        g[c] = 0; heapq.heappush(openh, (h(c[0], c[1]), c))
    end = None
    while openh:
        f_, c = heapq.heappop(openh)
        if c in dst: end = c; break
        gx, gy, L = c; gc = g[c]
        nbrs = []
        for dx, dy, cost in DIRS:
            nx, ny = gx + dx, gy + dy
            if 0 <= nx < NX and 0 <= ny < NY and not blocked[L][ny * NX + nx]:
                if dx and dy and (blocked[L][gy * NX + nx] or blocked[L][ny * NX + gx]): continue
                nbrs.append(((nx, ny, L), cost))
        oL = LAYERS[1] if L == LAYERS[0] else LAYERS[0]
        if not vblock[gy * NX + gx] and not blocked[oL][gy * NX + gx]:
            nbrs.append(((gx, gy, oL), VIA_COST))
        for n, cost in nbrs:
            ng = gc + cost
            if ng < g.get(n, 1e18):
                g[n] = ng; prev[n] = c; heapq.heappush(openh, (ng + h(n[0], n[1]), n))
    if end is None:
        return 0
    cells = [end]
    while cells[-1] in prev: cells.append(prev[cells[-1]])
    cells.reverse()
    # string pulling: on each layer keep only the corners needed for clear straight lines
    def clear(a, c):
        L = a[2]; n = int(max(abs(c[0] - a[0]), abs(c[1] - a[1])) * 2) + 1
        for k in range(n + 1):
            gx = round(a[0] + (c[0] - a[0]) * k / n); gy = round(a[1] + (c[1] - a[1]) * k / n)
            if blocked[L][gy * NX + gx] and (gx, gy, L) not in src and (gx, gy, L) not in dst: return False
            for ox in (0, 1):
                for oy in (0, 1):     # the line passes between cells: check the neighbours too
                    hx = int(a[0] + (c[0] - a[0]) * k / n) + ox; hy = int(a[1] + (c[1] - a[1]) * k / n) + oy
                    if 0 <= hx < NX and 0 <= hy < NY and blocked[L][hy * NX + hx] and (hx, hy, L) not in src and (hx, hy, L) not in dst: return False
        return True
    pulled = [cells[0]]; i = 0
    while i < len(cells) - 1:
        j = i + 1
        for k in range(len(cells) - 1, i, -1):
            if all(cells[m][2] == cells[i][2] for m in range(i, k + 1)) and clear(cells[i], cells[k]): j = k; break
        pulled.append(cells[j]); i = j
    cells = pulled
    # straight runs -> segments, layer changes -> vias
    P = lambda c: pcbnew.VECTOR2I(x0 + c[0] * G, y0 + c[1] * G)
    added = 0
    run = [cells[0]]
    def flush(run):
        nonlocal added
        keep = [run[0]]
        for i in range(1, len(run) - 1):
            a, m, c = keep[-1], run[i], run[i + 1]
            if (m[0] - a[0]) * (c[1] - m[1]) != (m[1] - a[1]) * (c[0] - m[0]): keep.append(m)
        if len(run) > 1: keep.append(run[-1])
        for a, c in zip(keep, keep[1:]):
            t = pcbnew.PCB_TRACK(b); t.SetStart(P(a)); t.SetEnd(P(c)); t.SetWidth(w); t.SetLayer(a[2]); t.SetNet(net); b.Add(t); added += 1
    for c in cells[1:]:
        if c[2] != run[-1][2]:
            flush(run)
            v = pcbnew.PCB_VIA(b); v.SetPosition(P(c)); v.SetWidth(VIA_D); v.SetDrill(VIA_DRILL)
            v.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu); v.SetNet(net); b.Add(v)
            run = [c]
        else:
            run.append(c)
    flush(run)
    # tie grid ends to the item centres (pad centres are off-grid)
    for item, cell in ((src_item, cells[0]), (dst_item, cells[-1])):
        if item.Type() == pcbnew.PCB_PAD_T and P(cell) != item.GetPosition():
            t = pcbnew.PCB_TRACK(b); t.SetStart(item.GetPosition()); t.SetEnd(P(cell)); t.SetWidth(min(w, mm(0.25)))
            t.SetLayer(cell[2]); t.SetNet(net); b.Add(t)
    return len(cells)

# GND must be connected by its tracks alone: empty the GND pours so DRC shows any GND split that
# only the pour bridged (filled again at the end)
for z in b.Zones():
    if z.GetNetname() == 'GND': z.UnFill()
# open connections from KiCad's DRC: pairs of items
import re, tempfile, os
rpt = tempfile.mktemp(suffix='.rpt')
pcbnew.WriteDRCReport(b, rpt, pcbnew.EDA_UNITS_MILLIMETRES, True)
txt = open(rpt).read(); os.unlink(rpt)
blocks = [x for x in re.split(r'\n(?=\[)', txt) if x.startswith('[unconnected_items]')]
def find(desc, x, y):
    pt = pcbnew.VECTOR2I(mm(x), mm(y))
    m = re.match(r'(?:PTH )?[Pp]ad (\S+) \[[^\]]*\] of (\S+)', desc)
    if m:
        for p in b.FindFootprintByReference(m.group(2)).Pads():
            if p.GetNumber() == m.group(1) and p.GetPosition() == pt: return p
        return [p for p in b.FindFootprintByReference(m.group(2)).Pads() if p.GetNumber() == m.group(1)][0]
    netname = re.search(r'\[([^\]]*)\]', desc).group(1)
    for t in b.GetTracks():
        if t.Type() == pcbnew.PCB_TRACE_T and t.GetNetname() == netname and (t.GetStart() == pt or t.GetEnd() == pt or t.HitTest(pt, 0)): return t
def span(blk):      # route short connections first: they have the fewest ways around
    its = re.findall(r'@\(([\d.]+) mm, ([\d.]+) mm\)', blk)
    return math.hypot(float(its[0][0]) - float(its[1][0]), float(its[0][1]) - float(its[1][1])) if len(its) > 1 else 0
blocks.sort(key=span)
for blk in blocks:
    its = re.findall(r'@\(([\d.]+) mm, ([\d.]+) mm\): (.*)', blk)
    a = find(its[0][2], float(its[0][0]), float(its[0][1])); c = find(its[1][2], float(its[1][0]), float(its[1][1]))
    if a is None or c is None: continue          # zone-to-zone items (fill islands) are not routed here
    net = a.GetNet()
    n = route(net, a, c) or route(net, a, c, 25) or route(net, a, c, 40)
    print(f'{net.GetNetname():10} {"routed, %d cells" % n if n else "NO PATH"}')
# remove tracks/vias left dangling (stubs the router went around), but only when that does not
# open a connection; repeat until nothing changes
def n_open():
    b.BuildConnectivity(); return b.GetConnectivity().GetUnconnectedCount(False)
def clone(t):
    if t.Type() == pcbnew.PCB_VIA_T:
        v = pcbnew.PCB_VIA(b); v.SetPosition(t.GetPosition()); v.SetWidth(t.GetWidth()); v.SetDrill(t.GetDrill())
        v.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu); v.SetNet(t.GetNet()); return v
    c = pcbnew.PCB_TRACK(b); c.SetStart(t.GetStart()); c.SetEnd(t.GetEnd()); c.SetWidth(t.GetWidth())
    c.SetLayer(t.GetLayer()); c.SetNet(t.GetNet()); return c
# vias of one net closer than 0.6 mm (the router can flip layers twice in a row): drop one when
# that opens nothing
vs = [(t, t.GetNetCode(), t.GetPosition()) for t in b.GetTracks() if t.Type() == pcbnew.PCB_VIA_T]
gone = set()
for i, (v, vn, vp) in enumerate(vs):
    if i in gone: continue
    for j in range(i + 1, len(vs)):
        w, wn, wp = vs[j]
        if j in gone or vn != wn or (vp - wp).EuclideanNorm() >= mm(0.6): continue
        before = n_open(); c = clone(w); b.Delete(w)
        if n_open() > before: b.Add(c); vs[j] = (c, wn, wp)
        else: gone.add(j); print('removed doubled via', c.GetNetname())
        break
kept = set()
while True:
    rpt = tempfile.mktemp(suffix='.rpt')
    pcbnew.WriteDRCReport(b, rpt, pcbnew.EDA_UNITS_MILLIMETRES, True)
    txt = open(rpt).read(); os.unlink(rpt)
    dang = [x for x in re.split(r'\n(?=\[)', txt) if x.startswith(('[track_dangling]', '[via_dangling]'))]
    changed = False
    for blk in dang:
        x, y, desc = re.findall(r'@\(([\d.]+) mm, ([\d.]+) mm\): (.*)', blk)[0]
        key = (x, y, desc)
        if key in kept: continue
        pt = pcbnew.VECTOR2I(mm(float(x)), mm(float(y)))
        if desc.startswith('Via'):
            t = next((v for v in b.GetTracks() if v.Type() == pcbnew.PCB_VIA_T and v.GetPosition() == pt), None)
        else:
            t = find(desc, float(x), float(y))
        if t is None: kept.add(key); continue
        before = n_open(); c = clone(t); b.Delete(t)
        if n_open() > before: b.Add(c); kept.add(key)
        else: changed = True; print('removed dangling', desc[:50])
        break
    if not changed and all(k in kept for k in [(a, bb, d) for blk in dang for (a, bb, d) in re.findall(r'@\(([\d.]+) mm, ([\d.]+) mm\): (.*)', blk)[:1]]):
        break
pcbnew.ZONE_FILLER(b).Fill(b.Zones())
b.BuildConnectivity()
print('unrouted now', b.GetConnectivity().GetUnconnectedCount(False))
pcbnew.SaveBoard(path, b)
