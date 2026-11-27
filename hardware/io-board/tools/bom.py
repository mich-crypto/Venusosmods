# Parts list (io-board-bom.csv) from parts.py: python3 bom.py parts.py ../io-board-bom.csv
import sys,csv,collections,importlib.util
spec=importlib.util.spec_from_file_location('parts',sys.argv[1]); m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
g=collections.OrderedDict()
for ref,sym,val,fp,conn,note in m.P:
    if sym=='Device:NetTie_2': continue  # copper only, nothing to buy
    g.setdefault((val,fp,sym),[]).append(ref)
w=csv.writer(open(sys.argv[2],'w',newline='')); w.writerow(['Qty','References','Value','Footprint','Symbol'])
for (val,fp,sym),refs in g.items(): w.writerow([len(refs),' '.join(refs),val,fp,sym])
