from fractions import Fraction
import math
from .design import parse

def exact(x):
    return None if x is None else {'numerator':str(x.numerator),'denominator':str(x.denominator)}

def domain_row(name,members,strata):
    """Ratio-linearized mean over all sampled units; non-members contribute zero."""
    count=Fraction();total=Fraction()
    for s in strata.values():
        Nh=s['population'];n=len(s['observations']);w=Fraction(Nh,n)
        for uid,v in s['observations']:
            if uid in members:
                count+=w;total+=w*v
    if count==0:
        return {'domain':name,'estimated_count':exact(Fraction()),'estimated_total':exact(Fraction()),
                'estimated_mean':None,'variance_mean':None,'standard_error_mean':None,
                'variance_unavailable_strata':[]}
    mean=total/count
    variance=Fraction();unknown=[]
    for sid,s in sorted(strata.items()):
        Nh=s['population'];n=len(s['observations'])
        z=[(v-mean)/count if uid in members else Fraction() for uid,v in s['observations']]
        if n==Nh:continue
        if n<2:
            unknown.append(sid);continue
        zbar=sum(z,Fraction())/n
        s2=sum(((x-zbar)**2 for x in z),Fraction())/(n-1)
        variance+=Fraction(Nh**2,n)*(1-Fraction(n,Nh))*s2
    var_mean=None if unknown else variance
    return {'domain':name,'estimated_count':exact(count),'estimated_total':exact(total),
            'estimated_mean':exact(mean),'variance_mean':exact(var_mean),
            'standard_error_mean':math.sqrt(float(var_mean)) if var_mean is not None else None,
            'variance_unavailable_strata':unknown}

def estimate(data,domains=None):
    strata=parse(data);N=sum(s['population'] for s in strata.values());total=Fraction();variance=Fraction();rows=[];weights=[];unknown=[]
    for sid,s in sorted(strata.items()):
        Nh=s['population'];obs=s['observations'];n=len(obs);values=[v for _,v in obs]
        mean=sum(values,Fraction())/n;total+=Nh*mean
        s2=sum(((v-mean)**2 for v in values),Fraction())/(n-1) if n>1 else None
        if n==Nh:var_total=Fraction()
        elif s2 is None:var_total=None;unknown.append(sid)
        else:var_total=Nh**2*(1-Fraction(n,Nh))*s2/n
        if var_total is not None:variance+=var_total
        rows.append({'stratum':sid,'population':Nh,'sample_size':n,'sample_mean':exact(mean),
                     'sample_variance':exact(s2),'estimated_total':exact(Nh*mean),'total_variance':exact(var_total)})
        weights.extend({'unit_id':uid,'stratum':sid,'design_weight':exact(Fraction(Nh,n)),'value':exact(v)} for uid,v in obs)
    var_mean=variance/N**2 if not unknown else None
    result={'population':N,'sample_size':len(weights),'estimated_total':exact(total),
            'estimated_mean':exact(total/N),'variance_mean':exact(var_mean),
            'standard_error_mean':math.sqrt(float(var_mean)) if var_mean is not None else None,
            'variance_unavailable_strata':unknown,'strata':rows,'sample_weights':weights}
    if domains is not None:
        result['domains']=[domain_row(name,members,strata) for name,members in sorted(domains.items())]
    return result
