from decimal import Decimal, InvalidOperation
from fractions import Fraction
from .io import fields, array, text, integer

def outcome(v):
    if not isinstance(v,str) or len(v)>40: raise ValueError('outcome must be decimal text')
    try:d=Decimal(v)
    except InvalidOperation:raise ValueError('invalid decimal outcome')
    if not d.is_finite() or abs(d)>10**9 or d.as_tuple().exponent < -6:
        raise ValueError('outcome outside precision or magnitude bounds')
    return Fraction(d)

def parse(data):
    fields(data,'strata sample');strata={};seen=set()
    for s in array(data['strata']):
        fields(s,'id population');sid=text(s['id'])
        if sid in strata:raise ValueError('duplicate stratum')
        strata[sid]={'population':integer(s['population'],1,10**9),'observations':[]}
    if not strata:raise ValueError('at least one stratum required')
    for r in array(data['sample']):
        fields(r,'unit_id stratum value');uid=text(r['unit_id']);sid=text(r['stratum'])
        if uid in seen or sid not in strata:raise ValueError('duplicate sample unit or unknown stratum')
        seen.add(uid);strata[sid]['observations'].append((uid,outcome(r['value'])))
    for s in strata.values():
        if not 1<=len(s['observations'])<=s['population']:raise ValueError('stratum must have 1..population sampled units')
        s['observations'].sort()
    return strata
