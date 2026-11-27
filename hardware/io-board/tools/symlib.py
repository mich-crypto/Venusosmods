import re,os
LIBDIR='/usr/share/kicad/symbols'
_cache={}
def _sexpr_block(text,start):
    depth=0;i=start;instr=False
    while i<len(text):
        c=text[i]
        if instr:
            if c=='\\': i+=2; continue
            if c=='"': instr=False
        else:
            if c=='"': instr=True
            elif c=='(': depth+=1
            elif c==')':
                depth-=1
                if depth==0: return text[start:i+1]
        i+=1
    raise ValueError
def lib_text(lib):
    if lib not in _cache: _cache[lib]=open(f'{LIBDIR}/{lib}.kicad_sym').read()
    return _cache[lib]
def raw_symbol(lib,name):
    t=lib_text(lib); m=re.search(r'\n\s*\(symbol "'+re.escape(name)+r'"[\s\n]',t)
    if not m: raise KeyError(f'{lib}:{name}')
    return _sexpr_block(t,t.index('(',m.start()))
def resolved(lib,name):
    """symbol text with extends resolved: returns (text_of_symbol_named_name, base_text)"""
    s=raw_symbol(lib,name)
    m=re.search(r'\(extends "([^"]+)"\)',s)
    if not m: return s
    base=resolved(lib,m.group(1))
    # take base, rename to name, replace properties with derived ones
    b=base.replace(f'(symbol "{m.group(1)}"',f'(symbol "{name}"',1)
    b=re.sub(r'\(symbol "'+re.escape(m.group(1))+r'_(\d+)_(\d+)"',lambda mm:f'(symbol "{name}_{mm.group(1)}_{mm.group(2)}"',b)
    # derived properties
    props=re.findall(r'\(property "[^"]+".*?\n\s*\)',s,re.S)
    for p in props:
        key=re.match(r'\(property "([^"]+)"',p).group(1)
        b=re.sub(r'\(property "'+re.escape(key)+r'".*?\n\s*\)',lambda _:p,b,count=1,flags=re.S)
    return b
def pins(lib,name):
    s=resolved(lib,name)
    out=[]
    for m in re.finditer(r'\(pin (\w+) (\w+)\s*\(at ([-\d.]+) ([-\d.]+) (\d+)\)\s*\(length ([-\d.]+)\)(?:\s*hide)?\s*\(name "([^"]*)".*?\(number "([^"]*)"',s,re.S):
        out.append(dict(type=m.group(1),x=float(m.group(3)),y=float(m.group(4)),rot=int(m.group(5)),name=m.group(7),num=m.group(8)))
    return out
if __name__=='__main__':
    import sys
    for spec in sys.argv[1:]:
        lib,name=spec.split(':')
        try:
            ps=pins(lib,name)
            print(spec, len(ps), ' | '.join(f"{p['num']}={p['name']}({p['type'][:3]})" for p in ps))
        except Exception as e: print(spec,'ERR',e)
