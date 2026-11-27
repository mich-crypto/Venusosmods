import pcbnew,re,parts,gen
FPDIR='/usr/share/kicad/footprints'
OX,OY=100,100   # board top-left (mm); front edge = OY+140
mm=pcbnew.FromMM
b=pcbnew.BOARD()
def edge(x1,y1,x2,y2):
    s=pcbnew.PCB_SHAPE(b); s.SetShape(pcbnew.SHAPE_T_RECT); s.SetStart(pcbnew.VECTOR2I(mm(x1),mm(y1))); s.SetEnd(pcbnew.VECTOR2I(mm(x2),mm(y2))); s.SetLayer(pcbnew.Edge_Cuts); s.SetWidth(mm(0.1)); b.Add(s)
edge(OX,OY,OX+120,OY+140)
# fixed placement (placement-plan coords: x 0..120 left->right, y 0..140 back->front), rotation
FIX={'U3':(60,12.75,0),'K1':(24,52,0),'K2':(24,76,0),'U1':(30,97,0),'U2':(42,97,0),'C1':(48,104,0),'F2':(30,110,0),
 'J5':(5,66,270),'J10':(115,37,90),'J8':(112,67,90),'J9':(112,84,90),'J3':(111,103,90),
 'J4':(40,131,180),'J6':(64,131,180),'J7':(88,131,180),'J1':(16,134,0),'J2':(26,126,0),
 'SW3':(52,116,0),'SW4':(70,116,0),'D9':(106,128,0),'D5':(113,128,0),
 'U60':(90,40,0),'U92':(92,80,0),'U90':(64,74,0),'U91':(110,118,0),
 'U5':(56,48,0),'U6':(56,56,0),'U7':(69,48,0),'U8':(69,56,0),'U71':(102,28,0),'U72':(102,36,0),'U73':(102,44,0),
 'R50':(42,58,90),'R51':(42,82,90),'F4':(42,48,90),'F5':(42,72,90),'U11':(49,62,0),'U12':(49,67,0),'U21':(49,84,0),'U22':(49,89,0),
 'Q1':(36,60,0),'Q2':(36,84,0),'Q80':(20,120,0),'Q81':(28,120,0),'F7':(13,120,90),'D4':(16,104,0),'D1':(16,96,0),'D2':(16,110,0),'F1':(16,90,0),
 'SW1':(80,7,0),'SW2':(91,7,0),'U4':(16,127,0)}
# remaining parts grouped near their circuit: region (x0,y0,x1) to pack into
REG=[('^(R1|R2|C2|C3|C4|D3)$',(52,94,64)),('^(C5|C6|C7|R3|R4|R5|R6|C8)$',(76,16,104)),
     ('^(R1[0-6]|D7|D8)$',(50,32,80)),('^(J6|R2[0-4]|F3|D2[01]|C20)$',(66,94,84)),('^(R30|R3[1-4]|C3[1-4]|R4[1-4]|D3[1-4])$',(84,92,101)),
     ('^(R58|R59|R[12]0[5-8]|D1[12]|D4[12]|JP[12])$',(14,26,46)),('^(D60|D61|C60|R6[1-3]|F6|JP10|D7[1-3]|R7[1-6])$',(78,48,102)),
     ('^(R8[0-2])$',(34,116,46)),('^(R9[0-3]|C9[0-3]|F8)$',(72,68,100))]
lib_of=lambda fp: fp.split(':')
nets={}
def net(n):
    if n not in nets:
        ni=pcbnew.NETINFO_ITEM(b,n); b.Add(ni); nets[n]=ni
    return nets[n]
cursor={i:[r[1][0],r[1][1],0] for i,r in enumerate(REG)}
placed=0
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
# mounting holes M3
for i,(x,y) in enumerate([(4,4),(116,4),(4,136),(116,136)]):
    fp=pcbnew.FootprintLoad(f'{FPDIR}/MountingHole.pretty','MountingHole_3.2mm_M3'); fp.SetReference(f'H{i+1}'); fp.SetValue('M3')
    fp.SetPosition(pcbnew.VECTOR2I(mm(OX+x),mm(OY+y))); b.Add(fp)
# notes on the user layer
t=pcbnew.PCB_TEXT(b); t.SetText('FRONT: ST1 / ST2 / ST3 / USB-C - faces the LT 100 opening'); t.SetPosition(pcbnew.VECTOR2I(mm(OX+60),mm(OY+146))); t.SetLayer(pcbnew.Cmts_User); t.SetTextSize(pcbnew.VECTOR2I(mm(2),mm(2))); b.Add(t)
t=pcbnew.PCB_TEXT(b); t.SetText('ESP32 antenna: no copper under it, board edge'); t.SetPosition(pcbnew.VECTOR2I(mm(OX+60),mm(OY-4))); t.SetLayer(pcbnew.Cmts_User); t.SetTextSize(pcbnew.VECTOR2I(mm(1.5),mm(1.5))); b.Add(t)
pcbnew.SaveBoard('io-board.kicad_pcb',b)
fps=list(b.GetFootprints())
def box(f):
    if f.GetReference()=='U3':
        r=pcbnew.BOX2I(pcbnew.VECTOR2I(mm(OX+51),mm(OY)),pcbnew.VECTOR2I(mm(18),mm(25.5))); return r
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
