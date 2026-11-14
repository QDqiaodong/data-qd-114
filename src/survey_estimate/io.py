import json, math, os, tempfile
from pathlib import Path

def pairs(items):
    result={}
    for k,v in items:
        if k in result: raise ValueError('duplicate JSON key: '+k)
        result[k]=v
    return result

def load(path):
    raw=Path(path).read_bytes()
    if len(raw)>8_000_000: raise ValueError('input exceeds 8 MB')
    def invalid(value): raise ValueError('non-finite JSON number: '+value)
    return json.loads(raw,object_pairs_hook=pairs,parse_constant=invalid)

def fields(obj,names):
    if not isinstance(obj,dict) or set(obj)!=set(names.split()):
        raise ValueError('expected fields: '+names)

def text(v):
    if not isinstance(v,str) or not v.strip() or len(v)>256 or any(ord(c)<32 for c in v):
        raise ValueError('expected nonempty text, at most 256 characters')
    return v

def integer(v,low=0,high=10**9):
    if type(v)!=int or not low<=v<=high: raise ValueError('integer outside allowed bounds')
    return v

def array(v):
    if not isinstance(v,list) or len(v)>100000: raise ValueError('expected array of at most 100000 entries')
    return v

def publish(path,result):
    path=Path(path)
    if path.is_symlink(): raise ValueError('output symlink is not allowed')
    path.parent.mkdir(parents=True,exist_ok=True)
    name=None
    try:
        with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8',dir=path.parent,delete=False) as f:
            name=f.name;json.dump(result,f,ensure_ascii=False,sort_keys=True,indent=2,allow_nan=False);f.write('\n')
        os.replace(name,path)
    finally:
        if name and os.path.exists(name): os.unlink(name)
