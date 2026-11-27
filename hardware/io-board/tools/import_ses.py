# Import a Freerouting session (.ses) into the board, refill the GND planes and write a DRC report.
# KiCad 7 can only import .ses in the GUI; this does it with the pcbnew API.
# usage (KiCad 7 python): python3 import_ses.py ../io-board.kicad_pcb board.ses drc.rpt
import pcbnew, sys, re
pcb, ses = sys.argv[1], sys.argv[2]
def parse(s):
    toks = re.findall(r'\(|\)|"[^"]*"|[^\s()]+', s); i = 0
    def rd():
        nonlocal i
        t = toks[i]; i += 1
        if t == '(':
            l = []
            while toks[i] != ')': l.append(rd())
            i += 1; return l
        return t.strip('"')
    return rd()
tree = parse(open(ses).read())
def find(node, key):
    for c in node:
        if isinstance(c, list) and c and c[0] == key: yield c
routes = next(find(tree, 'routes'))
res = next(find(routes, 'resolution'))
scale = {'um': 1000, 'mil': 25400, 'mm': 1e6}[res[1]] / int(res[2])   # -> nm
P = lambda x, y: pcbnew.VECTOR2I(int(round(float(x)*scale)), int(round(-float(y)*scale)))
b = pcbnew.LoadBoard(pcb)
for t in list(b.GetTracks()): b.Delete(t)
nets = b.GetNetsByName()
nt = nv = 0; missing = set()
for net in find(next(find(routes, 'network_out')), 'net'):
    ni = nets[net[1]] if nets.has_key(net[1]) else None
    if ni is None: missing.add(net[1]); continue
    for w in find(net, 'wire'):
        p = next(find(w, 'path')); layer = b.GetLayerID(p[1]); width = float(p[2])*scale
        pts = p[3:]; pts = [P(pts[k], pts[k+1]) for k in range(0, len(pts)-1, 2)]
        for a, c in zip(pts, pts[1:]):
            t = pcbnew.PCB_TRACK(b); t.SetStart(a); t.SetEnd(c); t.SetWidth(int(width))
            t.SetLayer(layer); t.SetNet(ni); b.Add(t); nt += 1
    for v in find(net, 'via'):
        m = re.search(r'_(\d+):(\d+)_um', v[1])
        via = pcbnew.PCB_VIA(b); via.SetPosition(P(v[2], v[3]))
        via.SetWidth(int(m.group(1))*1000); via.SetDrill(int(m.group(2))*1000)
        via.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu); via.SetNet(ni); b.Add(via); nv += 1
print("segments", nt, "vias", nv, "missing nets", missing)
pcbnew.ZONE_FILLER(b).Fill(b.Zones())
b.BuildConnectivity()
print("unrouted connections", b.GetConnectivity().GetUnconnectedCount(False))
pcbnew.SaveBoard(pcb, b)
print("drc", pcbnew.WriteDRCReport(b, sys.argv[3], pcbnew.EDA_UNITS_MILLIMETRES, True))
