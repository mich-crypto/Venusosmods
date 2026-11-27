import uuid,re,symlib,parts
U=lambda: str(uuid.uuid4())
ROOT=U()
CUSTOM={'io_board:PCA9615':'''(symbol "io_board:PCA9615" (pin_names (offset 1.016)) (in_bom yes) (on_board yes)
  (property "Reference" "U" (at 0 11.43 0) (effects (font (size 1.27 1.27))))
  (property "Value" "PCA9615" (at 0 -11.43 0) (effects (font (size 1.27 1.27))))
  (property "Footprint" "Package_SO:TSSOP-10_3x3mm_P0.5mm" (at 0 0 0) (effects (font (size 1.27 1.27)) hide))
  (property "Datasheet" "https://www.nxp.com/docs/en/data-sheet/PCA9615.pdf" (at 0 0 0) (effects (font (size 1.27 1.27)) hide))
  (property "ki_description" "Dual bidirectional differential I2C-bus buffer" (at 0 0 0) (effects (font (size 1.27 1.27)) hide))
  (symbol "PCA9615_0_1" (rectangle (start -7.62 10.16) (end 7.62 -10.16) (stroke (width 0.254) (type default)) (fill (type background))))
  (symbol "PCA9615_1_1"
    (pin power_in line (at -10.16 7.62 0) (length 2.54) (name "VDDA" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
    (pin bidirectional line (at -10.16 2.54 0) (length 2.54) (name "SCL" (effects (font (size 1.27 1.27)))) (number "2" (effects (font (size 1.27 1.27)))))
    (pin bidirectional line (at -10.16 0 0) (length 2.54) (name "SDA" (effects (font (size 1.27 1.27)))) (number "3" (effects (font (size 1.27 1.27)))))
    (pin power_in line (at -10.16 -7.62 0) (length 2.54) (name "GND" (effects (font (size 1.27 1.27)))) (number "4" (effects (font (size 1.27 1.27)))))
    (pin input line (at -10.16 -2.54 0) (length 2.54) (name "EN" (effects (font (size 1.27 1.27)))) (number "5" (effects (font (size 1.27 1.27)))))
    (pin bidirectional line (at 10.16 2.54 180) (length 2.54) (name "DSDAM" (effects (font (size 1.27 1.27)))) (number "6" (effects (font (size 1.27 1.27)))))
    (pin bidirectional line (at 10.16 5.08 180) (length 2.54) (name "DSDAP" (effects (font (size 1.27 1.27)))) (number "7" (effects (font (size 1.27 1.27)))))
    (pin bidirectional line (at 10.16 0 180) (length 2.54) (name "DSCLP" (effects (font (size 1.27 1.27)))) (number "8" (effects (font (size 1.27 1.27)))))
    (pin bidirectional line (at 10.16 -2.54 180) (length 2.54) (name "DSCLM" (effects (font (size 1.27 1.27)))) (number "9" (effects (font (size 1.27 1.27)))))
    (pin power_in line (at 10.16 7.62 180) (length 2.54) (name "VDDB" (effects (font (size 1.27 1.27)))) (number "10" (effects (font (size 1.27 1.27)))))
  )
)'''}
def sym_text(lib_id):
    if lib_id in CUSTOM: return CUSTOM[lib_id]
    lib,name=lib_id.split(':')
    t=symlib.resolved(lib,name)
    return t.replace(f'(symbol "{name}"',f'(symbol "{lib_id}"',1)
def sym_pins(lib_id):
    if lib_id in CUSTOM:
        out=[]
        for m in re.finditer(r'\(pin (\w+) \w+ \(at ([-\d.]+) ([-\d.]+) (\d+)\).*?\(name "([^"]*)".*?\(number "([^"]*)"',CUSTOM[lib_id]):
            out.append(dict(type=m.group(1),x=float(m.group(2)),y=float(m.group(3)),rot=int(m.group(4)),name=m.group(5),num=m.group(6)))
        return out
    lib,name=lib_id.split(':'); return symlib.pins(lib,name)
def q(s): return s.replace('\\','\\\\').replace('"','\\"')
items=[]; libs={}; SYMUUID={}
def snap(v,g=2.54): return round(round(v/g)*g,2)
def place(ref,lib_id,val,fp,conn,note,X,Y):
    libs[lib_id]=1
    ps=sym_pins(lib_id)
    # resolve pin keys: number or '@name'
    bynum={}
    for k,v in conn.items():
        if k.startswith('@'):
            m=[p for p in ps if p['name']==k[1:]]
            assert m,(ref,k)
            for p in m: bynum[p['num']]=v
        else: bynum[k]=v
    nums={p['num'] for p in ps}
    for k in bynum: assert k in nums,(ref,k,sorted(nums))
    pinuu=''.join(f'(pin "{p["num"]}" (uuid {U()}))' for p in ps)
    ymax=max([p['y'] for p in ps]+[0]); ymin=min([p['y'] for p in ps]+[0])
    props=(f'(property "Reference" "{ref}" (at {X} {round(Y-ymax-2.54,2)} 0) (effects (font (size 1.27 1.27))))'
           f'(property "Value" "{q(val)}" (at {X} {round(Y-ymin+2.54,2)} 0) (effects (font (size 1.27 1.27))))'
           f'(property "Footprint" "{q(fp)}" (at {X} {Y} 0) (effects (font (size 1.27 1.27)) hide))'
           f'(property "Datasheet" "~" (at {X} {Y} 0) (effects (font (size 1.27 1.27)) hide))')
    if note: props+=f'(property "Note" "{q(note)}" (at {X} {Y} 0) (effects (font (size 1.27 1.27)) hide))'
    su=U(); SYMUUID[ref]=su
    items.append(f'(symbol (lib_id "{lib_id}") (at {X} {Y} 0) (unit 1) (in_bom yes) (on_board yes) (dnp no) (uuid {su}) {props} {pinuu} '
                 f'(instances (project "io-board" (path "/{ROOT}" (reference "{ref}") (unit 1)))))')
    seen=set()
    for p in ps:
        ex,ey=round(X+p['x'],2),round(Y-p['y'],2)
        net=bynum.get(p['num'],'NC')
        key=(ex,ey,net)
        if key in seen: continue
        seen.add(key)
        if net=='NC' or p['type']=='no_connect':
            items.append(f'(no_connect (at {ex} {ey}) (uuid {U()}))'); continue
        ang={0:180,180:0,90:270,270:90}[p['rot']]
        just='left' if ang in (0,90) else 'right'
        items.append(f'(label "{q(net)}" (at {ex} {ey} {ang}) (effects (font (size 1.27 1.27)) (justify {just} bottom)) (uuid {U()}))')
    xs=[p['x'] for p in ps] or [0]; 
    return max(xs)-min(xs), ymax-ymin, bynum
def text(s,X,Y,size=3.81):
    items.append(f'(text "{q(s)}" (at {X} {Y} 0) (effects (font (size {size} {size}) bold) (justify left bottom)) (uuid {U()}))')
# group by section using ref order in parts.P (sections in file order)
sections=[('1 · Power',lambda r:r in ('J4','F1','D1','J5','F2','D2','D4','C1','U1','C2','U2','C3','D3','R1','R2','C4')),
          ('2 · ESP32-S3, USB-C programming, status LED',lambda r:r in ('U3','C5','C6','R3','C7','SW1','SW2','J1','R4','R5','U4','D5','R6','C8')),
          ('3 · EBL 119 (ST1) and front plate buttons',lambda r:r in ('R10','R11','U5','U6','R12','D7','U7','R15','R13','D8','U8','R16','R14','D9','SW3','SW4')),
          ('4 · Tanks: ST2 fresh water, ST3 grey water',lambda r:r in ('J6','R20','F3','D20','R21','C20','R22','D21','R23','R24','J7','R30') or re.match(r'^(R3[1-4]|C3[1-4]|R4[1-4]|D3[1-4])$',r)),
          ('5 · Output 1 and Output 2 (relays with current sense)',lambda r:r in ('R58','R59','F4','F5','R50','R51','JP1','JP2','K1','K2','Q1','Q2','D11','D12','D41','D42','U11','U12','U21','U22') or re.match(r'^R[12]0[5-8]$',r)),
          ('6 · Truma LIN, CP plus 12 V, inputs',lambda r:r in ('J8','J9','D60','D61','C60','U60','R61','R62','R63','F6','J10','JP10') or re.match(r'^(D7[1-3]|R7[1-6]|U7[1-3])$',r)),
          ('7 · USB PD module, tilt + humidity sensors, RJ45 expansion',lambda r:r in ('F7','Q80','R80','Q81','R81','R82','J2','R90','R91','R93','U90','C90','U91','C91','U92','C92','C93','F8','R92','J3'))]
used=set(); PAGEW=760
y=30
allnets={}
for title,f in sections:
    grp=[p for p in parts.P if f(p[0])]
    text(title,15,y); y+=12
    x=20; rowh=0
    for ref,lib_id,val,fp,conn,note in grp:
        used.add(ref)
        ps=sym_pins(lib_id)
        w=(max(p['x'] for p in ps)-min(p['x'] for p in ps)) if ps else 5
        h=(max(p['y'] for p in ps)-min(p['y'] for p in ps)) if ps else 5
        maxlab=max([len(v) for v in conn.values()]+[4])*1.27+4
        cellw=w+2*maxlab+8
        if x+cellw>PAGEW: x=20; y+=rowh+18; rowh=0
        X=snap(x+maxlab-min(p['x'] for p in ps)+2); Y=snap(y+max(p['y'] for p in ps)+6)
        _,_,bn=place(ref,lib_id,val,fp,conn,note,X,Y)
        for n in bn.values(): allnets[n]=allnets.get(n,0)+1
        x+=cellw; rowh=max(rowh,h+12)
    y+=rowh+28
missing=[p[0] for p in parts.P if p[0] not in used]; assert not missing,missing
# power flags
text('Power flags',15,y); y+=12; x=30
for i,n in enumerate(parts.FLAGS):
    libs['power:PWR_FLAG']=1
    X,Y=snap(x+i*40),snap(y+10)
    items.append(f'(symbol (lib_id "power:PWR_FLAG") (at {X} {Y} 0) (unit 1) (in_bom yes) (on_board yes) (dnp no) (uuid {U()})'
                 f'(property "Reference" "#FLG0{i+1:02d}" (at {X} {Y-5} 0) (effects (font (size 1.27 1.27)) hide))'
                 f'(property "Value" "PWR_FLAG" (at {X} {Y-3} 0) (effects (font (size 1.27 1.27))))'
                 f'(property "Footprint" "" (at {X} {Y} 0) (effects (font (size 1.27 1.27)) hide))(property "Datasheet" "~" (at {X} {Y} 0) (effects (font (size 1.27 1.27)) hide))'
                 f'(pin "1" (uuid {U()})) (instances (project "io-board" (path "/{ROOT}" (reference "#FLG0{i+1:02d}") (unit 1)))))')
    items.append(f'(label "{q(n)}" (at {X} {Y} 270) (effects (font (size 1.27 1.27)) (justify right bottom)) (uuid {U()}))')
H=snap(y+40)
single=[n for n,c in allnets.items() if c==1 and n!='NC']
libtxt='\n'.join(sym_text(l) for l in libs)
out=f'''(kicad_sch (version 20230121) (generator eeschema)
  (uuid {ROOT})
  (paper "User" {PAGEW+20} {H})
  (title_block (title "Venus OS mods I/O board") (date "2026-11-27") (rev "0.1 planning") (company "") (comment 1 "Replaces the Schaudt LT 100 panel; see docs/io-board.md") (comment 2 "Net labels connect the parts; check pinouts marked in the Note fields"))
  (lib_symbols
{libtxt}
  )
{chr(10).join(items)}
  (sheet_instances (path "/" (page "1")))
)
'''
open('io-board.kicad_sch','w').write(out)
print('parts',len(used),'nets',len(allnets),'height',H)
print('single-pin nets:',single)
