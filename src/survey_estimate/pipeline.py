from .io import load
from .design import parse
from .estimate import estimate
from .domains import memberships

def run(path,domains_path=None):
    data=load(path)
    if domains_path is None:
        return estimate(data)
    strata=parse(data)
    unit_ids={uid for s in strata.values() for uid,_ in s['observations']}
    return estimate(data,memberships(load(domains_path),unit_ids))
