# Move parts on a routed board to the nearest free spot next to an IC pin (their tracks are removed;
# finish_routes.py connects them again). Free = outline clear of other footprints (ESP32: its pads
# + 0.5 mm) and pads clear of other nets' copper.
#   python3 move_near.py ../io-board.kicad_pcb C5:U3:2 C7:U3:3 ...
import sys, math, pcbnew
mm = pcbnew.FromMM
b = pcbnew.LoadBoard(sys.argv[1])
def box(f):
    if f.GetReference() == 'U3':
        bbs = [p.GetBoundingBox() for p in f.Pads()]
        x0 = min(q.GetX() for q in bbs); y0 = min(q.GetY() for q in bbs)
        x1 = max(q.GetRight() for q in bbs); y1 = max(q.GetBottom() for q in bbs)
        return pcbnew.BOX2I(pcbnew.VECTOR2I(x0 - mm(0.5), y0 - mm(0.5)), pcbnew.VECTOR2I(x1 - x0 + mm(1), y1 - y0 + mm(1)))
    return f.GetBoundingBox(False, False)
edge = b.GetBoardEdgesBoundingBox(); edge.Inflate(-mm(1.5))
for arg in sys.argv[2:]:
    ref, ic, pin = arg.split(':')
    f = b.FindFootprintByReference(ref)
    pads = [p.GetPosition() for p in f.Pads()]
    for t in list(b.GetTracks()):
        if t.Type() == pcbnew.PCB_TRACE_T and any(t.GetStart() == q or t.GetEnd() == q for q in pads): b.Delete(t)
    tgt = [p for p in b.FindFootprintByReference(ic).Pads() if p.GetNumber() == pin][0].GetPosition()
    others = [box(g) for g in b.GetFootprints() if g.GetReference() != ref]
    others += [z.GetBoundingBox() for g in b.GetFootprints() for z in g.Zones() if z.GetIsRuleArea()]   # antenna / sensor keep-outs
    tracks = list(b.GetTracks())
    found = False
    for r in [x * 0.25 for x in range(4, 80)]:
        for k in range(36):
            a = 2 * math.pi * k / 36
            for rot in (0, 90):
                f.SetOrientationDegrees(rot)
                f.SetPosition(pcbnew.VECTOR2I(int(tgt.x + mm(r) * math.cos(a)), int(tgt.y + mm(r) * math.sin(a))))
                bb = f.GetBoundingBox(False, False); bb2 = pcbnew.BOX2I(bb.GetPosition(), bb.GetSize()); bb2.Inflate(mm(0.2))
                if not edge.Contains(bb) or any(bb2.Intersects(o) for o in others): continue
                if any(t.GetNetname() != p.GetNetname() and t.HitTest(p.GetPosition(), max(p.GetSize().x, p.GetSize().y) // 2 + mm(0.35))
                       for p in f.Pads() for t in tracks): continue
                found = True; break
            if found: break
        if found: break
    d = min((p.GetPosition() - tgt).EuclideanNorm() for p in f.Pads()) / 1e6
    print(ref, 'moved, pad to pin', round(d, 1), 'mm' if found else 'NO SPOT')
pcbnew.SaveBoard(sys.argv[1], b)
