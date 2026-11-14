DESCRIPTION='Stratified finite-population estimation with design-based uncertainty'
import argparse, json, sys
from pathlib import Path
from .io import publish
from .pipeline import run

def main():
    p=argparse.ArgumentParser(description=DESCRIPTION)
    p.add_argument('--source',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--domains',type=Path,default=None)
    a=p.parse_args()
    try:
        if a.source.resolve()==a.output.resolve(): raise ValueError('source and output must differ')
        if a.domains is not None and a.domains.resolve() in (a.source.resolve(),a.output.resolve()):
            raise ValueError('domains, source and output must differ')
        result=run(a.source,a.domains);publish(a.output,result)
    except (ValueError,OSError,KeyError,TypeError,OverflowError,RecursionError) as e:
        print(str(e),file=sys.stderr);return 2
    print(json.dumps(result,ensure_ascii=False,sort_keys=True,allow_nan=False));return 0

if __name__=='__main__': raise SystemExit(main())
