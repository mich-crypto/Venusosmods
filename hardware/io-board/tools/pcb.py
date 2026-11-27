import pcbnew,re,parts,gen
FPDIR='/usr/share/kicad/footprints'
OX,OY=100,100   # board top-left (mm); front edge = OY+140
mm=pcbnew.FromMM
b=pcbnew.BOARD()
def edge(x1,y1,x2,y2):
    s=pcbnew.PCB_SHAPE(b); s.SetShape(pcbnew.SHAPE_T_RECT); s.SetStart(pcbnew.VECTOR2I(mm(x1),mm(y1))); s.SetEnd(pcbnew.VECTOR2I(mm(x2),mm(y2))); s.SetLayer(pcbnew.Edge_Cuts); s.SetWidth(mm(0.1)); b.Add(s)
edge(OX,OY,OX+120,OY+140)
# fixed placement (placement-plan coords: x 0..120 left->right, y 0..140 back->front), rotation
FIX={'U3':(60,12.75,0),'K1':(24,52,0),'K2':(24,76,0),'U1':(30,97,0),'U2':(42,97,0),'F2':(30,110,0),'Q3':(47,113,0),'D14':(57,113,0),
 'J5':(5,66,270),'J10':(115,37,90),'J8':(112,67,90),'J9':(112,84,90),'J3':(111,103,90),
 'J4':(27.3,131,180),'J6':(46.8,131,180),'J7':(69.0,131,180),'J1':(13.8,134,0),'J2':(8,125.5,0),'D5':(109.6,137.3,0),
 'U60':(85,40,0),'U92':(92,80,0),'U90':(64,74,0),'U91':(110,118,0),
 'U5':(56,48,0),'U6':(56,56,0),'U7':(69,48,0),'U8':(69,56,0),'U71':(97,27,0),'U72':(97,35,0),'U73':(97,43,0),
 'R50':(42,58,90),'R51':(42,82,90),'F4':(42,48,90),'F5':(42,72,90),'U11':(49,62,0),'U12':(49,67,0),'U21':(49,84,0),'U22':(49,89,0),
 'Q1':(36,60,0),'Q2':(36,84,0),'Q80':(20,120,0),'Q81':(28,120,0),'F7':(13,120,90),'D4':(16,104,0),'D1':(16,96,0),'D2':(16,110,0),'F1':(20,90,0),
 'SW1':(92,7,0),'SW2':(103,7,0),'NT11':(45.5,54.5,90),'NT12':(45.5,61.5,90),'NT21':(45.5,78.5,90),'NT22':(45.5,85.5,90),'U4':(14,127,0)}
# front-plate parts on the front edge, placed by pad 1 (x, y, rotation): actuators / LED lens past the edge
PAD1={'SW3':(87.4,137.2,180),'SW4':(97.9,137.2,180),'D9':(102.55,136.5,0)}
# decoupling and support parts: placed after everything else, as close as possible to (IC, pin)
NEAR={'C1':('U1','1'),'C9':('U1','3'),'C2':('U2','1'),'C3':('U2','3'),'C5':('U3','2'),'C6':('U3','2'),'C7':('U3','3'),'R3':('U3','3'),
      'C4':('U3','18'),'C60':('U60','7'),'C90':('U90','14'),'C91':('U91','3'),'C92':('U92','1'),'C93':('U92','10'),
      'C8':('D5','4'),'D6':('D5','4'),'R6':('D5','3'),'C94':('U11','5'),'C95':('U12','5'),'C96':('U21','5'),'C97':('U22','5'),
      'R7':('Q3','1'),'D13':('Q3','1'),'U93':('J3','4'),'R108':('U12','4'),'R208':('U22','4')}
# SHT40 thermal slot: 1 mm wide U-shaped cut-out around a 7 x 7 mm island, open to the left
SLOT_C=(110,118); SLOT_IN=3.5; SLOT_W=1.0
# remaining parts grouped near their circuit: region (x0,y0,x1) to pack into
REG=[('^(R1|R2|C2|C3|C4|D3)$',(52,94,64)),('^(C5|C6|C7|R3|R4|R5|R6|C8|D6)$',(76,16,104)),
     ('^(R1[0-6]|D7|D8)$',(50,32,80)),('^(J6|R2[0-4]|F3|D2[01]|C20)$',(66,94,84)),('^(R30|R3[1-4]|C3[1-4]|R4[1-4]|D3[1-4])$',(84,92,101)),
     ('^(R58|R59|R[12]0[5-8]|D1[12]|D4[12]|JP[12])$',(14,26,46)),('^(D60|D61|C60|R6[1-3]|F6|JP10|D7[1-3]|R7[1-6])$',(78,48,102)),
     ('^(R8[0-3])$',(34,116,46)),('^(R9[0-9]|C9[0-3]|F8)$',(72,68,100))]
lib_of=lambda fp: fp.split(':')
nets={}
def net(n):
    if n not in nets:
        ni=pcbnew.NETINFO_ITEM(b,n); b.Add(ni); nets[n]=ni
    return nets[n]
cursor={i:[r[1][0],r[1][1],0] for i,r in enumerate(REG)}
placed=0
deferred=[]
for ref,sym,val,fpname,conn,note in parts.P:
    lib,name=lib_of(fpname)
    fp=pcbnew.FootprintLoad(f'{FPDIR}/{lib}.pretty',name)
    assert fp,(ref,fpname)
    fp.SetReference(ref); fp.SetValue(val); fp.SetPath(pcbnew.KIID_PATH('/'+gen.SYMUUID[ref]))
    # pin number map (resolve @names through the symbol)
    ps=gen.sym_pins(sym); bynum={}
    for k,v in conn.items():
        if k.startswith('@'):
            for p in ps:
                if p['name']==k[1:]: bynum[p['num']]=v
        else: bynum[k]=v
    for pad in fp.Pads():
        n=bynum.get(pad.GetNumber())
        if n and n!='NC': pad.SetNet(net(n))
        if ref=='U3' and pad.GetDrillSize().x==mm(0.2): pad.SetDrillSize(pcbnew.VECTOR2I(mm(0.3),mm(0.3)))  # thermal vias: 0.3 mm min drill
    if ref in ('U5','U6'): fp.SetLocalClearance(0)  # library 0.2 mm override; 12 V parts, board rule applies
    if ref in NEAR:
        deferred.append((ref,fp)); continue
    if ref in PAD1:
        x,y,r=PAD1[ref]; fp.SetOrientationDegrees(r); fp.SetPosition(pcbnew.VECTOR2I(0,0))
        p1=[p for p in fp.Pads() if p.GetNumber()=='1'][0].GetPosition()
        fp.SetPosition(pcbnew.VECTOR2I(mm(OX+x)-p1.x,mm(OY+y)-p1.y)); b.Add(fp); placed+=1; continue
    if ref in FIX:
        x,y,r=FIX[ref]
    else:
        for i,(pat,(x0,y0,x1)) in enumerate(REG):
            if re.match(pat,ref):
                c=cursor[i]; bb=fp.GetBoundingBox(False,False); w=pcbnew.ToMM(bb.GetWidth())+1.5; h=pcbnew.ToMM(bb.GetHeight())+1.5
                if c[0]+w>x1: c[0]=x0; c[1]+=c[2]; c[2]=0
                x,y,r=c[0]+w/2,c[1]+h/2,0; c[0]+=w; c[2]=max(c[2],h); break
        else: raise SystemExit('no place for '+ref)
    fp.SetOrientationDegrees(r); fp.SetPosition(pcbnew.VECTOR2I(mm(OX+x),mm(OY+y)))
    if ref!='U3':
        bb=fp.GetBoundingBox(False,False); c=bb.GetCenter(); pos=fp.GetPosition()
        fp.SetPosition(pcbnew.VECTOR2I(pos.x+(mm(OX+x)-c.x),pos.y+(mm(OY+y)-c.y)))
    b.Add(fp); placed+=1
# SHT40 slot (Edge.Cuts) + keep-out so nothing is routed into it
cx,cy=SLOT_C; a,w=SLOT_IN,SLOT_W
pts=[(cx-a,cy-a-w),(cx+a+w,cy-a-w),(cx+a+w,cy+a+w),(cx-a,cy+a+w),(cx-a,cy+a),(cx+a,cy+a),(cx+a,cy-a),(cx-a,cy-a)]
sl=pcbnew.PCB_SHAPE(b); sl.SetShape(pcbnew.SHAPE_T_POLY); sl.SetLayer(pcbnew.Edge_Cuts); sl.SetWidth(mm(0.1))
sl.SetPolyPoints([pcbnew.VECTOR2I(mm(OX+x),mm(OY+y)) for x,y in pts]); b.Add(sl)
slot_boxes=[]
for (x0,y0,x1,y1) in [(cx-a,cy-a-w,cx+a+w,cy-a),(cx+a,cy-a-w,cx+a+w,cy+a+w),(cx-a,cy+a,cx+a+w,cy+a+w)]:
    z=pcbnew.ZONE(b); z.SetIsRuleArea(True); z.SetDoNotAllowTracks(True); z.SetDoNotAllowVias(True)
    z.SetDoNotAllowCopperPour(True); z.SetDoNotAllowPads(False); z.SetDoNotAllowFootprints(False)
    ls=pcbnew.LSET(); ls.AddLayer(pcbnew.F_Cu); ls.AddLayer(pcbnew.B_Cu); z.SetLayerSet(ls)
    o=z.Outline(); o.NewOutline(); m=0.5
    for (x,y) in [(x0-m,y0-m),(x1+m,y0-m),(x1+m,y1+m),(x0-m,y1+m)]: o.Append(mm(OX+x),mm(OY+y))
    b.Add(z); slot_boxes.append(pcbnew.BOX2I(pcbnew.VECTOR2I(mm(OX+x0-m),mm(OY+y0-m)),pcbnew.VECTOR2I(mm(x1-x0+2*m),mm(y1-y0+2*m))))
# deferred parts next to their IC pin
edge_in=pcbnew.BOX2I(pcbnew.VECTOR2I(mm(OX+1.5),mm(OY+1.5)),pcbnew.VECTOR2I(mm(117),mm(137)))
def cbox(f):
    c=f.GetCourtyard(pcbnew.F_CrtYd)
    return c.BBox() if c.OutlineCount() else f.GetBoundingBox(False,False)
def obstacles():      # same boxes as the overlap check below: module body for U3, full outline otherwise
    obs=[pcbnew.BOX2I(pcbnew.VECTOR2I(mm(OX+50),mm(OY)),pcbnew.VECTOR2I(mm(20),mm(26.5))) if f.GetReference()=='U3'
         else f.GetBoundingBox(False,False) for f in b.GetFootprints()]+slot_boxes
    obs+=[pcbnew.BOX2I(pcbnew.VECTOR2I(mm(OX+x-3.6),mm(OY+y-3.6)),pcbnew.VECTOR2I(mm(7.2),mm(7.2))) for x,y in [(4,4),(116,4),(4,136),(116,136)]]  # M3 holes (added later)
    for f in b.GetFootprints():
        for z in f.Zones():
            if z.GetIsRuleArea(): obs.append(z.GetBoundingBox())
    return obs
import math as _m
near_dist={}
for ref,fp in sorted(deferred,key=lambda t:list(NEAR).index(t[0])):
    ic,pin=NEAR[ref]; t=[p for p in b.FindFootprintByReference(ic).Pads() if p.GetNumber()==pin][0].GetPosition()
    obs=obstacles(); done=False
    for r in [x*0.25 for x in range(4,60)]:
        for k in range(32):
            ang=2*_m.pi*k/32
            for rot in (0,90):
                fp.SetOrientationDegrees(rot); fp.SetPosition(pcbnew.VECTOR2I(int(t.x+mm(r)*_m.cos(ang)),int(t.y+mm(r)*_m.sin(ang))))
                bb=fp.GetBoundingBox(False,False); bb2=pcbnew.BOX2I(bb.GetPosition(),bb.GetSize()); bb2.Inflate(mm(0.2))
                if edge_in.Contains(bb) and not any(bb2.Intersects(o) for o in obs): done=True; break
            if done: break
        if done: break
    b.Add(fp); placed+=1
    d=min((p.GetPosition()-t).EuclideanNorm() for p in fp.Pads())/1e6
    near_dist[ref]=round(d,1) if done else 'NO SPOT'
print('near placement (pad to pin, mm):',near_dist)
# fiducials for assembly
for i,(x,y) in enumerate([(3,22),(117,22),(3,104)]):
    fd=pcbnew.FootprintLoad(f'{FPDIR}/Fiducial.pretty','Fiducial_1mm_Mask2mm'); fd.SetReference(f'FID{i+1}'); fd.SetValue('Fiducial')
    fd.SetPosition(pcbnew.VECTOR2I(mm(OX+x),mm(OY+y))); b.Add(fd)
# mounting holes M3
for i,(x,y) in enumerate([(4,4),(116,4),(4,136),(116,136)]):
    fp=pcbnew.FootprintLoad(f'{FPDIR}/MountingHole.pretty','MountingHole_3.2mm_M3'); fp.SetReference(f'H{i+1}'); fp.SetValue('M3')
    fp.SetPosition(pcbnew.VECTOR2I(mm(OX+x),mm(OY+y))); b.Add(fp)
# notes on the user layer
t=pcbnew.PCB_TEXT(b); t.SetText('FRONT: ST1 / ST2 / ST3 / USB-C - faces the LT 100 opening'); t.SetPosition(pcbnew.VECTOR2I(mm(OX+60),mm(OY+146))); t.SetLayer(pcbnew.Cmts_User); t.SetTextSize(pcbnew.VECTOR2I(mm(2),mm(2))); b.Add(t)
t=pcbnew.PCB_TEXT(b); t.SetText('ESP32 antenna: no copper under it, board edge'); t.SetPosition(pcbnew.VECTOR2I(mm(OX+60),mm(OY-4))); t.SetLayer(pcbnew.Cmts_User); t.SetTextSize(pcbnew.VECTOR2I(mm(1.5),mm(1.5))); b.Add(t)
# ---- tidy text: values only on Fab, small references placed where they hit nothing
for f in b.GetFootprints():
    f.Value().SetLayer(pcbnew.F_Fab); f.Value().SetVisible(True)
    f.Value().SetTextSize(pcbnew.VECTOR2I(mm(0.6),mm(0.6))); f.Value().SetTextThickness(mm(0.1))
    for g in f.GraphicalItems():                      # stray footprint texts (e.g. "KEEP-OUT ZONE") -> Fab
        if isinstance(g,pcbnew.PCB_TEXT) and g.GetLayer()==pcbnew.F_SilkS: g.SetLayer(pcbnew.F_Fab)
def boxes_of(f):
    out=[]
    for p in f.Pads(): out.append(p.GetBoundingBox())
    return out
obst=[]
for f in b.GetFootprints(): obst+=boxes_of(f)
# ---- pin names next to the push-in terminals
PINLBL={'J5':{'1':'AUX+','2':'GND','3':'O1 C','4':'O1 NO','5':'O1 NC','6':'O2 C','7':'O2 NO','8':'O2 NC','9':'CP+','10':'GND'},
        'J10':{'1':'COM','2':'IN1','3':'IN2','4':'IN3'}}
for f in b.GetFootprints():
    lab=PINLBL.get(f.GetReference())
    if not lab: continue
    fb=f.GetBoundingBox(False,False)
    for p in f.Pads():
        t=pcbnew.PCB_TEXT(b); t.SetText(lab[p.GetNumber()]); t.SetLayer(pcbnew.F_SilkS)
        t.SetTextSize(pcbnew.VECTOR2I(mm(0.9),mm(0.9))); t.SetTextThickness(mm(0.14))
        y=p.GetPosition().y
        if f.GetReference()=='J5':
            t.SetHorizJustify(pcbnew.GR_TEXT_H_ALIGN_LEFT); t.SetPosition(pcbnew.VECTOR2I(fb.GetRight()+mm(0.8),y))
        else:
            t.SetHorizJustify(pcbnew.GR_TEXT_H_ALIGN_RIGHT); t.SetPosition(pcbnew.VECTOR2I(fb.GetLeft()-mm(0.8),y))
        b.Add(t); obst.append(t.GetBoundingBox())
placed_txt=[]
def free(bb):
    if pcbnew.ToMM(bb.GetLeft())<OX+0.3 or pcbnew.ToMM(bb.GetRight())>OX+119.7 or pcbnew.ToMM(bb.GetTop())<OY+0.3 or pcbnew.ToMM(bb.GetBottom())>OY+139.7: return False
    return not any(bb.Intersects(o) for o in obst) and not any(bb.Intersects(t) for t in placed_txt)
order=sorted(b.GetFootprints(),key=lambda f:-pcbnew.ToMM(f.GetBoundingBox(False,False).GetArea())/1e6)
moved=0; bad=[]
for f in order:
    r=f.Reference(); big=pcbnew.ToMM(f.GetBoundingBox(False,False).GetWidth())>8
    sz=1.0 if big else 0.8
    r.SetTextSize(pcbnew.VECTOR2I(mm(sz),mm(sz))); r.SetTextThickness(mm(0.12 if not big else 0.15)); r.SetLayer(pcbnew.F_SilkS); r.SetTextAngle(pcbnew.EDA_ANGLE(0,pcbnew.DEGREES_T))
    fb=f.GetBoundingBox(False,False); c=fb.GetCenter()
    cands=[(c.x, fb.GetTop()-mm(0.8)),(c.x, fb.GetBottom()+mm(0.8)),(fb.GetLeft()-mm(2.2),c.y),(fb.GetRight()+mm(2.2),c.y),(c.x,c.y)]
    for dx in (0,-2,2,-4,4):
        for dy in (0,-1.5,1.5,-3,3):
            cands.append((c.x+mm(dx),fb.GetTop()-mm(0.8)+mm(dy)))
    ok=False
    for (x,y) in cands:
        r.SetPosition(pcbnew.VECTOR2I(int(x),int(y)))
        bb=r.GetBoundingBox()
        if free(bb): placed_txt.append(bb); ok=True; break
    if not ok:
        r.SetPosition(pcbnew.VECTOR2I(int(cands[0][0]),int(cands[0][1]))); placed_txt.append(r.GetBoundingBox()); bad.append(f.GetReference())
print('reference texts not placed freely:',bad)
pcbnew.SaveBoard('io-board.kicad_pcb',b)
fps=list(b.GetFootprints())
def box(f):
    if f.GetReference()=='U3':
        r=pcbnew.BOX2I(pcbnew.VECTOR2I(mm(OX+50),mm(OY)),pcbnew.VECTOR2I(mm(20),mm(26.5))); return r
    return f.GetBoundingBox(False,False)
bbs=[(f.GetReference(),box(f)) for f in fps]
ov=[]
for i in range(len(bbs)):
    for j in range(i+1,len(bbs)):
        a,b1=bbs[i][1],bbs[j][1]
        if a.Intersects(b1):
            ix=min(a.GetRight(),b1.GetRight())-max(a.GetLeft(),b1.GetLeft()); iy=min(a.GetBottom(),b1.GetBottom())-max(a.GetTop(),b1.GetTop())
            if pcbnew.ToMM(ix)>0.3 and pcbnew.ToMM(iy)>0.3: ov.append((bbs[i][0],bbs[j][0],round(pcbnew.ToMM(ix),1),round(pcbnew.ToMM(iy),1)))
out=[(f.GetReference()) for f in fps if f.GetReference()!='U3' and not (pcbnew.ToMM(f.GetBoundingBox(False,False).GetLeft())>=OX-2 and pcbnew.ToMM(f.GetBoundingBox(False,False).GetRight())<=OX+122 and pcbnew.ToMM(f.GetBoundingBox(False,False).GetTop())>=OY-1 and pcbnew.ToMM(f.GetBoundingBox(False,False).GetBottom())<=OY+142)]
print('overlaps',len(ov),ov[:40]); print('outside board',out)
print('placed',placed,'nets',len(nets))
