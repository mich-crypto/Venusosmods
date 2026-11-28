# JLCPCB assembly files from parts.py, lcsc.py and the KiCad position file:
#   fab/jlc/io-board-bom-jlc.csv  (Comment, Designator, Footprint, LCSC Part #)
#   fab/jlc/io-board-cpl-jlc.csv  (Designator, Mid X, Mid Y, Layer, Rotation)
# Run after tools/fab.sh, in hardware/io-board:  python3 tools/jlc.py
# Left out: copper-only items (net ties, the JP10 solder jumper, fiducials) and parts.DNP.
# Rotations are KiCad's; check JLCPCB's placement preview (polarity of diodes, ICs, connectors).
import csv, os, sys, collections
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import parts
from lcsc import LCSC
os.makedirs('fab/jlc', exist_ok=True)
skip = lambda sym, fp: sym == 'Device:NetTie_2' or fp.startswith('Jumper:SolderJumper')
groups = collections.OrderedDict(); missing = []
for ref, sym, val, fp, conn, note in parts.P:
    if skip(sym, fp) or ref in parts.DNP: continue
    if val not in LCSC: missing.append((ref, val)); continue
    groups.setdefault((val, fp), []).append(ref)
if missing: sys.exit(f'no LCSC number for: {missing}')
with open('fab/jlc/io-board-bom-jlc.csv', 'w', newline='') as f:
    w = csv.writer(f); w.writerow(['Comment', 'Designator', 'Footprint', 'LCSC Part #'])
    for (val, fp), refs in groups.items():
        w.writerow([val, ','.join(refs), fp.split(':')[1], LCSC[val]])
placed = {r for refs in groups.values() for r in refs}
n = 0
with open('fab/io-board-pos.csv') as fi, open('fab/jlc/io-board-cpl-jlc.csv', 'w', newline='') as fo:
    w = csv.writer(fo); w.writerow(['Designator', 'Mid X', 'Mid Y', 'Layer', 'Rotation'])
    for r in csv.DictReader(fi):
        if r['Ref'] not in placed: continue
        w.writerow([r['Ref'], r['PosX'] + 'mm', r['PosY'] + 'mm', 'Top' if r['Side'] == 'top' else 'Bottom', r['Rot']])
        n += 1
print('BOM lines', len(groups), 'parts', len(placed), '| CPL entries', n, '| not fitted:', sorted(parts.DNP))
if n != len(placed): sys.exit('CPL and BOM differ')
