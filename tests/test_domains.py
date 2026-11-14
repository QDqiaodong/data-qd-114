import json, unittest, subprocess, sys, tempfile
from pathlib import Path
from fractions import Fraction
from survey_estimate.estimate import estimate
from survey_estimate.domains import memberships
ROOT=Path(__file__).resolve().parents[1]
def val(x):return Fraction(int(x['numerator']),int(x['denominator']))

def source(rows,populations):
    return {'strata':[{'id':sid,'population':populations[sid]} for sid in sorted(populations)],
            'sample':[{'unit_id':uid,'stratum':sid,'value':value} for uid,sid,value in rows]}

A=source([('u1','h1','2'),('u2','h1','6'),
          ('v1','h2','10'),('v2','h2','20'),('v3','h2','30')],{'h1':8,'h2':6})
A_DOMAINS={'overlap':['u1','v1','v3'],'other':['v1','v2'],'empty':[]}

B=source([('c1','c','5'),('t1','t','2'),('t2','t','4')],{'c':1,'t':4})
B_DOMAINS={'census_and_t':['c1','t1','t2'],'only_census':['c1']}

C=source([('s1','s','7'),('t1','t','2'),('t2','t','4')],{'s':5,'t':4})
C_DOMAINS={'mix':['s1','t1']}

def domains_of(result):return {d['domain']:d for d in result['domains']}

class DomainTests(unittest.TestCase):
    def test_unequal_rates_overlap_empty_domain(self):
        r=estimate(A,{k:set(v) for k,v in A_DOMAINS.items()})
        got=domains_of(r)
        self.assertEqual([d['domain'] for d in r['domains']],['empty','other','overlap'])
        o=got['overlap']
        self.assertEqual(val(o['estimated_count']),8);self.assertEqual(val(o['estimated_total']),88)
        self.assertEqual(val(o['estimated_mean']),11)
        self.assertEqual(val(o['variance_mean']),Fraction(867,32))
        self.assertAlmostEqual(o['standard_error_mean'],5.205165703414253)
        self.assertEqual(o['variance_unavailable_strata'],[])
        t=got['other']
        self.assertEqual(val(t['estimated_count']),4);self.assertEqual(val(t['estimated_total']),60)
        self.assertEqual(val(t['estimated_mean']),15)
        self.assertEqual(val(t['variance_mean']),Fraction(75,8))
        self.assertAlmostEqual(t['standard_error_mean'],3.0618621784789726)
        e=got['empty']
        self.assertEqual(val(e['estimated_count']),0);self.assertEqual(val(e['estimated_total']),0)
        for k in ('estimated_mean','variance_mean','standard_error_mean'):self.assertIsNone(e[k])
        self.assertEqual(e['variance_unavailable_strata'],[])

    def test_domain_rows_keep_exact_fractions_and_expected_fields(self):
        d=domains_of(estimate(A,{k:set(v) for k,v in A_DOMAINS.items()}))['overlap']
        self.assertEqual(set(d),{'domain','estimated_count','estimated_total','estimated_mean',
                                 'variance_mean','standard_error_mean','variance_unavailable_strata'})
        for k in ('estimated_count','estimated_total','estimated_mean','variance_mean'):
            self.assertEqual(set(d[k]),{'numerator','denominator'})
        self.assertEqual(d['estimated_count'],{'numerator':'8','denominator':'1'})

    def test_census_stratum_contributes_zero_variance(self):
        got=domains_of(estimate(B,{k:set(v) for k,v in B_DOMAINS.items()}))
        d=got['census_and_t']
        self.assertEqual(val(d['estimated_count']),5);self.assertEqual(val(d['estimated_total']),17)
        self.assertEqual(val(d['estimated_mean']),Fraction(17,5))
        self.assertEqual(val(d['variance_mean']),Fraction(8,25))
        self.assertAlmostEqual(d['standard_error_mean'],0.565685424949238)
        o=got['only_census']
        self.assertEqual(val(o['estimated_mean']),5);self.assertEqual(val(o['variance_mean']),0)
        self.assertEqual(o['standard_error_mean'],0)

    def test_singleton_non_census_domain_variance_is_unavailable(self):
        got=domains_of(estimate(C,{k:set(v) for k,v in C_DOMAINS.items()}))
        d=got['mix']
        self.assertEqual(val(d['estimated_count']),7);self.assertEqual(val(d['estimated_total']),39)
        self.assertEqual(val(d['estimated_mean']),Fraction(39,7))
        self.assertIsNone(d['variance_mean']);self.assertIsNone(d['standard_error_mean'])
        self.assertEqual(d['variance_unavailable_strata'],['s'])

    def test_domain_does_not_change_existing_report(self):
        plain=estimate(A);withd=estimate(A,{k:set(v) for k,v in A_DOMAINS.items()})
        self.assertNotIn('domains',plain)
        self.assertEqual(plain,{k:v for k,v in withd.items() if k!='domains'})

    def test_full_membership_domain_reproduces_population_mean_variance(self):
        all_units={r['unit_id'] for r in A['sample']}
        r=estimate(A,{'everyone':all_units})
        d=r['domains'][0]
        self.assertEqual(val(d['estimated_count']),r['population'])
        self.assertEqual(val(d['estimated_total']),val(r['estimated_total']))
        self.assertEqual(d['estimated_mean'],r['estimated_mean'])
        self.assertEqual(d['variance_mean'],r['variance_mean'])
        self.assertEqual(d['standard_error_mean'],r['standard_error_mean'])

    def test_empty_membership_on_census_and_singleton_layers_stays_null(self):
        r=estimate(C,{'nobody':set()})
        d=r['domains'][0]
        self.assertEqual(val(d['estimated_count']),0)
        for k in ('estimated_mean','variance_mean','standard_error_mean'):self.assertIsNone(d[k])

    def test_domain_wider_than_sample_is_still_zero_variance(self):
        r=estimate(B,{'census':{'c1'}})
        d=r['domains'][0]
        self.assertEqual(val(d['estimated_count']),1)
        self.assertEqual(val(d['variance_mean']),0)
        self.assertEqual(d['standard_error_mean'],0)

    def test_input_order_does_not_matter(self):
        first=estimate(A,{k:set(v) for k,v in A_DOMAINS.items()})
        shuffled=source([('v3','h2','30'),('u2','h1','6'),('v1','h2','10'),
                         ('u1','h1','2'),('v2','h2','20')],{'h2':6,'h1':8})
        second=estimate(shuffled,{k:set(reversed(v)) for k,v in reversed(list(A_DOMAINS.items()))})
        self.assertEqual(first,second)

    def test_domain_validation_rejects_duplicate_and_unknown_units(self):
        good=memberships({'d':['a']},{'a','b'})
        self.assertEqual(good,{'d':{'a'}})
        with self.assertRaises(ValueError):memberships({'d':['a','a']},{'a','b'})
        with self.assertRaises(ValueError):memberships({'d':['z']},{'a','b'})
        with self.assertRaises(ValueError):memberships({'d':['a'],'e':['z']},{'a','b'})
        with self.assertRaises(ValueError):memberships({'d':'a'},{'a','b'})
        with self.assertRaises(ValueError):memberships({'d':[1]},{'a','b'})
        with self.assertRaises(ValueError):memberships({'':['a']},{'a','b'})
        with self.assertRaises(ValueError):memberships(['a'],{'a','b'})
        self.assertEqual(memberships({'d':[],'e':['a','b']},{'a','b'}),{'d':set(),'e':{'a','b'}})

class DomainCliTests(unittest.TestCase):
    def cli(self,out,source,domains=None):
        args=[sys.executable,'-m','survey_estimate.cli','--output',str(out),'--source',str(source)]
        if domains is not None:args+=['--domains',str(domains)]
        return subprocess.run(args,capture_output=True)

    def test_cli_without_domains_matches_baseline_byte_for_byte(self):
        with tempfile.TemporaryDirectory() as td:
            out=Path(td)/'out.json'
            r=self.cli(out,ROOT/'fixtures/survey.json')
            self.assertEqual(r.returncode,0,r.stderr)
            self.assertEqual(out.read_bytes(),(ROOT/'tests/baseline_report.json').read_bytes())

    def test_cli_with_domains(self):
        with tempfile.TemporaryDirectory() as td:
            out=Path(td)/'out.json'
            r=self.cli(out,ROOT/'fixtures/survey.json',ROOT/'fixtures/domains.json')
            self.assertEqual(r.returncode,0,r.stderr)
            report=json.loads(out.read_bytes())
            self.assertEqual([d['domain'] for d in report['domains']],['all','none','youth'])
            youth=next(d for d in report['domains'] if d['domain']=='youth')
            self.assertEqual(val(youth['estimated_count']),4)
            self.assertEqual(val(youth['estimated_total']),60)
            self.assertEqual(val(youth['estimated_mean']),15)
            self.assertEqual(val(youth['variance_mean']),Fraction(25,4))
            self.assertAlmostEqual(youth['standard_error_mean'],2.5)
            self.assertEqual(report['domains'][1]['estimated_mean'],None)

    def test_failed_domain_publication_preserves_report(self):
        bad_files={
            'unknown_unit.json':{'d':['missing']},
            'duplicate_member.json':{'d':['S1','S1']},
            'not_array.json':{'d':'S1'},
            'array_root.json':['S1'],
            'empty_name.json':{'':['S1']},
            'duplicate_key.json':None,
        }
        with tempfile.TemporaryDirectory() as td:
            p=Path(td);out=p/'out.json'
            self.assertEqual(self.cli(out,ROOT/'fixtures/survey.json').returncode,0)
            before=out.read_bytes()
            for name,payload in bad_files.items():
                path=p/name
                path.write_text('{"d":["S1"],"d":["S2"]}' if payload is None else json.dumps(payload))
                r=self.cli(out,ROOT/'fixtures/survey.json',path)
                self.assertEqual(r.returncode,2,name)
                self.assertEqual(out.read_bytes(),before,name)
            missing=p/'nope.json'
            self.assertEqual(self.cli(out,ROOT/'fixtures/survey.json',missing).returncode,2)
            self.assertEqual(out.read_bytes(),before)

    def test_bad_source_in_domain_mode_exits_2(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td);out=p/'out.json';bad=p/'bad.json';bad.write_text('{}')
            self.assertEqual(self.cli(out,bad,ROOT/'fixtures/domains.json').returncode,2)
            self.assertFalse(out.exists())

if __name__=='__main__':unittest.main()
