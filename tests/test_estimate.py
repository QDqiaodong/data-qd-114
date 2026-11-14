import json, unittest, subprocess, sys, tempfile
from pathlib import Path
from fractions import Fraction
from survey_estimate.estimate import estimate
ROOT=Path(__file__).resolve().parents[1]
def val(x):return Fraction(int(x['numerator']),int(x['denominator']))
class EstimateTests(unittest.TestCase):
    def setUp(self):self.data=json.loads((ROOT/'fixtures/survey.json').read_text())
    def test_known_mean_and_finite_population_variance(self):
        r=estimate(self.data);self.assertEqual(val(r['estimated_total']),192);self.assertEqual(val(r['estimated_mean']),Fraction(96,5))
        self.assertEqual(val(r['variance_mean']),Fraction(32,25));self.assertAlmostEqual(r['standard_error_mean'],1.131370849898476)
    def test_unequal_sampling_weights(self):
        self.data['strata'][0]['population']=8
        r=estimate(self.data);self.assertEqual(val(r['estimated_mean']),Fraction(120,7))
        self.assertEqual(val(r['sample_weights'][3]['design_weight']),4)
    def test_census_has_zero_variance_including_single_unit(self):
        r=estimate({'strata':[{'id':'all','population':1}],'sample':[{'unit_id':'U','stratum':'all','value':'2.5'}]})
        self.assertEqual(val(r['estimated_mean']),Fraction(5,2));self.assertEqual(r['standard_error_mean'],0)
    def test_singleton_non_census_retains_point_estimate_with_unknown_variance(self):
        self.data['sample']=self.data['sample'][1:]
        r=estimate(self.data);self.assertIsNone(r['variance_mean']);self.assertEqual(r['variance_unavailable_strata'],['small'])
    def test_missing_layer_and_duplicate_units_rejected(self):
        self.data['sample']=self.data['sample'][2:]
        with self.assertRaises(ValueError):estimate(self.data)
        self.setUp();self.data['sample'].append(self.data['sample'][0])
        with self.assertRaises(ValueError):estimate(self.data)
    def test_nonfinite_boolean_and_missing_values_rejected(self):
        for value in ['NaN','Infinity',None,True,'0.0000001']:
            self.data['sample'][0]['value']=value
            with self.assertRaises(ValueError):estimate(self.data)
    def test_order_and_decimal_precision(self):
        before=estimate(self.data);self.data['sample'].reverse();self.assertEqual(before,estimate(self.data))
        self.data['sample'][0]['value']='-0.1';self.assertIsNotNone(estimate(self.data)['estimated_mean'])
    def test_cli_success_and_failed_update_preserve_report(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td);out=p/'out.json';args=[sys.executable,'-m','survey_estimate.cli','--output',str(out),'--source']
            r=subprocess.run(args+[str(ROOT/'fixtures/survey.json')],capture_output=True)
            self.assertEqual(r.returncode,0,r.stderr);self.assertEqual(json.loads(out.read_text()),estimate(self.data))
            before=out.read_bytes();bad=p/'bad.json';bad.write_text('{}')
            self.assertEqual(subprocess.run(args+[str(bad)],capture_output=True).returncode,2);self.assertEqual(before,out.read_bytes())
if __name__=='__main__':unittest.main()
