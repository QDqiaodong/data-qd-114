import itertools
import json
import random
import subprocess
import sys
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

from survey_estimate.allocate import plan, MAX_BUDGET

ROOT = Path(__file__).resolve().parents[1]


def val(x):
    return Fraction(int(x['numerator']), int(x['denominator']))


def source(strata, samples):
    """strata: list of (id, population); samples: list of (unit_id, stratum, value)."""
    return {'strata': [{'id': sid, 'population': pop} for sid, pop in strata],
            'sample': [{'unit_id': uid, 'stratum': sid, 'value': value}
                       for uid, sid, value in samples]}


def constant_units(sid, n, value):
    return [(f'{sid}{i}', sid, value) for i in range(1, n + 1)]


def variances_of(result):
    return {row['stratum']: None if row['pilot_variance'] is None else val(row['pilot_variance'])
            for row in result['strata']}


def brute_force(data, budget):
    """Enumerate every feasible quota vector and return the exact minimum."""
    strata = sorted((s['id'], s['population']) for s in data['strata'])
    best = None
    ranges = []
    for sid, pop in strata:
        if pop == 1:
            ranges.append((sid, [0]))
        else:
            ranges.append((sid, list(range(2, pop + 1))))
    for combo in itertools.product(*(r for _, r in ranges)):
        if sum(combo) != budget:
            continue
        vector = list(combo)
        objective = Fraction()
        for (sid, pop), quota in zip(strata, vector):
            if quota == 0:
                continue
            s2 = pilot_variance(data, sid)
            objective += Fraction(pop ** 2) * (1 - Fraction(quota, pop)) * s2 / quota
        if best is None or objective < best[0] or (objective == best[0] and vector > best[1]):
            best = (objective, vector)
    if best is None:
        raise AssertionError('no feasible vector in brute force')
    return best[1], best[0]


def pilot_variance(data, sid):
    values = [r['value'] for r in data['sample'] if r['stratum'] == sid]
    nums = [Fraction(v) for v in values]
    n = len(nums)
    mean = sum(nums, Fraction()) / n
    return sum(((v - mean) ** 2 for v in nums), Fraction()) / (n - 1)


def random_case(seed):
    rng = random.Random(seed)
    strata, samples = [], []
    count = rng.randint(2, 5)
    for h in range(count):
        sid = f'h{h}'
        kind = rng.choice(['singleton', 'zero', 'positive', 'positive'])
        if kind == 'singleton':
            pop = 1
            strata.append((sid, pop))
            samples.append((f'{sid}u', sid, str(rng.randint(-5, 5))))
        else:
            pop = rng.randint(2, 6)
            strata.append((sid, pop))
            n = rng.randint(2, pop)
            if kind == 'zero':
                samples.extend(constant_units(sid, n, str(rng.randint(-9, 9))))
            else:
                samples.extend((f'{sid}u{i}', sid, str(rng.randint(-9, 9)))
                               for i in range(1, n + 1))
    return source(strata, samples)


class AllocateTests(unittest.TestCase):
    def test_fixture_hand_calculated_plan_and_fractions(self):
        data = json.loads((ROOT / 'fixtures/allocate_pilot.json').read_text())
        result = plan(data, 8)
        by_id = {row['stratum']: row for row in result['strata']}
        self.assertEqual([row['stratum'] for row in result['strata']],
                         ['noisy', 'quiet', 'singleton', 'stable'])
        self.assertEqual(by_id['singleton']['quota'], 0)
        self.assertIsNone(by_id['singleton']['pilot_variance'])
        self.assertIsNone(by_id['singleton']['planned_total_variance'])
        self.assertEqual(by_id['stable']['quota'], 2)
        self.assertEqual(val(by_id['stable']['pilot_variance']), Fraction(2))
        self.assertEqual(by_id['quiet']['quota'], 2)
        self.assertEqual(val(by_id['quiet']['pilot_variance']), Fraction(1))
        self.assertEqual(by_id['noisy']['quota'], 4)
        self.assertEqual(val(by_id['noisy']['pilot_variance']), Fraction(18))
        self.assertEqual(sum(row['quota'] for row in result['strata']), 8)
        # noisy is a census (n=N=4) and contributes zero; remaining contributions are
        # stable A*(N-n)/(N*n) = 32*2/8 = 8 and quiet 16*2/8 = 4, total 12.
        self.assertEqual(val(result['predicted_variance_total']), Fraction(12))
        self.assertEqual(val(result['predicted_variance_mean']), Fraction(12, 169))
        self.assertEqual(result['population'], 13)
        self.assertEqual(result['planned_sample_size'], 8)
        self.assertEqual(result['budget'], 8)
        # Only ints, exact-fraction text objects, None and strings occur: no floats.
        def walk(node):
            if isinstance(node, dict):
                for child in node.values():
                    walk(child)
            elif isinstance(node, list):
                for child in node:
                    walk(child)
            else:
                self.assertTrue(node is None or isinstance(node, (int, str)), node)
        walk(result)
        for row in result['strata']:
            if row['planned_total_variance'] is not None:
                self.assertEqual(set(row['planned_total_variance']), {'numerator', 'denominator'})

    def test_matches_exhaustive_optimum_on_random_small_cases(self):
        for seed in range(60):
            data = random_case(seed)
            lower = sum(2 for s in data['strata'] if s['population'] > 1)
            upper = sum(s['population'] for s in data['strata']
                        if s['population'] > 1)  # population-one strata are fixed at zero
            for budget in range(lower, upper + 1):
                result = plan(data, budget)
                expected_vector, expected_objective = brute_force(data, budget)
                got_vector = [next(r for r in result['strata'] if r['stratum'] == sid)['quota']
                              for sid, _ in sorted((s['id'], s['population']) for s in data['strata'])]
                self.assertEqual(got_vector, expected_vector, (seed, budget))
                rebuilt = sum((val(r['planned_total_variance'])
                               for r in result['strata']
                               if r['planned_total_variance'] is not None), Fraction())
                self.assertEqual(rebuilt, expected_objective, (seed, budget))

    def test_zero_variance_ties_fill_earliest_stratum_first(self):
        data = source([('a', 4), ('b', 4), ('c', 3)],
                      constant_units('a', 2, '5') + constant_units('b', 2, '5')
                      + [('c1', 'c', '1'), ('c2', 'c', '9')])
        r6 = plan(data, 6)
        # Budget equals the lower bound: every participating stratum gets exactly two.
        self.assertEqual([row['quota'] for row in r6['strata']], [2, 2, 2])
        r7 = plan(data, 7)
        # The positive-variance stratum c has the only strictly positive marginal at
        # n=2 (a and b have zero pilot variance), so it wins the extra unit first.
        self.assertEqual([row['quota'] for row in r7['strata']], [2, 2, 3])
        expected, objective = brute_force(data, 7)
        self.assertEqual([row['quota'] for row in r7['strata']], expected)
        self.assertEqual(objective, 0)
        r9 = plan(data, 9)
        # c is saturated; remaining units all have equal (zero) marginal in a and b,
        # so the lexicographically largest vector fills the earlier stratum id first.
        self.assertEqual([row['quota'] for row in r9['strata']], [4, 2, 3])
        expected, objective = brute_force(data, 9)
        self.assertEqual([row['quota'] for row in r9['strata']], expected)
        self.assertEqual(val(r9['predicted_variance_total']), 0)
        r11 = plan(data, 11)  # census across all three strata
        self.assertEqual([row['quota'] for row in r11['strata']], [4, 4, 3])
        self.assertEqual(val(r11['predicted_variance_total']), 0)

    def test_all_zero_variance_strata_fill_in_id_order(self):
        # Every participating stratum has zero pilot variance: every marginal is
        # zero, so the optimum is any feasible vector and the lexicographically
        # largest one fills earlier ids first at every budget.
        strata, samples = [], []
        for h, pop in enumerate((6, 3, 4)):
            sid = f'z{h}'
            strata.append((sid, pop))
            samples.extend(constant_units(sid, 2, '8'))
        data = source(strata, samples)
        lower, upper = 6, 13
        for budget in range(lower, upper + 1):
            result = plan(data, budget)
            expected, objective = brute_force(data, budget)
            self.assertEqual([row['quota'] for row in result['strata']], expected, budget)
            self.assertEqual(objective, 0)
        self.assertEqual([row['quota'] for row in plan(data, 10)['strata']], [6, 2, 2])
        self.assertEqual([row['quota'] for row in plan(data, 13)['strata']], [6, 3, 4])

    def test_single_unit_strata_get_zero_and_keep_plan_feasible(self):
        data = source([('one', 1), ('two', 1), ('work', 5)],
                      [('g1', 'one', '2'), ('g2', 'two', '4'),
                       ('w1', 'work', '1'), ('w2', 'work', '3')])
        result = plan(data, 4)
        by_id = {row['stratum']: row for row in result['strata']}
        self.assertEqual(by_id['one']['quota'], 0)
        self.assertEqual(by_id['two']['quota'], 0)
        self.assertEqual(by_id['work']['quota'], 4)
        self.assertIsNone(by_id['one']['pilot_variance'])
        expected, _ = brute_force(data, 4)
        self.assertEqual([by_id['one']['quota'], by_id['two']['quota'], by_id['work']['quota']],
                         expected)

    def test_all_singleton_population_rejects_every_positive_budget(self):
        data = source([('a', 1), ('b', 1)],
                      [('a1', 'a', '2'), ('b1', 'b', '7')])
        for budget in (1, 2, MAX_BUDGET):
            with self.assertRaises(ValueError):
                plan(data, budget)

    def test_non_singleton_stratum_with_one_pilot_unit_is_rejected(self):
        good = source([('h', 4)], [('u1', 'h', '1'), ('u2', 'h', '2')])
        plan(good, 2)
        bad = source([('h', 4), ('k', 3)],
                     [('u1', 'h', '1'), ('k1', 'k', '2'), ('k2', 'k', '4')])
        with self.assertRaises(ValueError):
            plan(bad, 4)
        # Even an infeasible-looking budget cannot bypass the pilot-size validation.
        with self.assertRaises(ValueError):
            plan(bad, 100)

    def test_infeasible_budgets_are_rejected(self):
        data = source([('a', 4), ('b', 3)],
                      [('a1', 'a', '1'), ('a2', 'a', '2'),
                       ('b1', 'b', '1'), ('b2', 'b', '2')])
        for bad in (0, -1, MAX_BUDGET + 1, 10 ** 6):
            with self.assertRaises(ValueError):
                plan(data, bad)
        with self.assertRaises(ValueError):
            plan(data, 3)   # below lower bound 2+2
        with self.assertRaises(ValueError):
            plan(data, 8)   # above upper bound 4+3

    def test_population_cap_of_one_billion_is_accepted(self):
        data = source([('big', 10 ** 9)], [('u1', 'big', '3'), ('u2', 'big', '5')])
        result = plan(data, 100)
        row = result['strata'][0]
        self.assertEqual(row['quota'], 100)
        self.assertEqual(row['population'], 10 ** 9)
        self.assertEqual(val(row['pilot_variance']), Fraction(2))
        too_big = source([('big', 10 ** 9 + 1)], [('u1', 'big', '3'), ('u2', 'big', '5')])
        with self.assertRaises(ValueError):
            plan(too_big, 2)

    def test_hundred_thousand_budget_many_strata(self):
        rng = random.Random(2026)
        strata, samples = [], []
        for h in range(300):
            sid = f's{h:03d}'
            kind = rng.choice(['singleton', 'zero', 'positive', 'positive', 'wide'])
            if kind == 'singleton':
                pop = 1
                strata.append((sid, pop))
                samples.append((f'{sid}u0', sid, str(rng.randint(-3, 3))))
                continue
            if kind == 'wide':
                pop = rng.randint(10_000, 10 ** 7)
            else:
                pop = rng.randint(2, 400)
            strata.append((sid, pop))
            n = rng.randint(2, min(pop, 12))
            if kind == 'zero':
                samples.extend(constant_units(sid, n, '7'))
            else:
                samples.extend((f'{sid}u{i}', sid, str(rng.randint(-99, 99)))
                               for i in range(n))
        data = source(strata, samples)
        result = plan(data, MAX_BUDGET)
        rows = result['strata']
        self.assertEqual(sum(row['quota'] for row in rows), MAX_BUDGET)
        by_id = {row['stratum']: row for row in rows}
        s2 = variances_of(result)
        # Feasibility bounds per stratum.
        for sid, pop in strata:
            quota = by_id[sid]['quota']
            lower_h = 0 if pop == 1 else 2
            self.assertLessEqual(lower_h, quota)
            self.assertLessEqual(quota, pop)
        # KKT-style optimality check from the final quotas and exact marginals.
        positive = [r for r in rows if r['quota'] > 0]
        saturated = [r for r in positive if r['quota'] == r['population']]
        slack = [r for r in positive if r['quota'] < r['population']]
        def last_marginal(row):
            pop, quota = row['population'], row['quota']
            a = Fraction(pop ** 2) * s2[row['stratum']]
            return a / (quota * (quota - 1)) if quota > 2 else None
        def next_marginal(row):
            pop, quota = row['population'], row['quota']
            a = Fraction(pop ** 2) * s2[row['stratum']]
            return a / (quota * (quota + 1))
        for sat in saturated:
            if sat['quota'] > 2:
                lm = last_marginal(sat)
                for sl in slack:
                    self.assertGreaterEqual(lm, next_marginal(sl))
        for left, right in zip(positive, positive[1:]):
            if s2[left['stratum']] == 0 and s2[right['stratum']] == 0 \
                    and right['quota'] < right['population'] and left['quota'] < left['population']:
                self.assertGreaterEqual(left['quota'], right['quota'])
        # The singleton strata remain excluded even at the largest budget.
        self.assertTrue(all(by_id[sid]['quota'] == 0 for sid, pop in strata if pop == 1))

    def test_result_does_not_depend_on_input_order(self):
        data = json.loads((ROOT / 'fixtures/allocate_pilot.json').read_text())
        first = plan(data, 9)
        shuffled = json.loads(json.dumps(data))
        shuffled['strata'] = list(reversed(shuffled['strata']))
        shuffled['sample'] = list(reversed(shuffled['sample']))
        self.assertEqual(plan(shuffled, 9), first)

    def test_source_validation_is_reused(self):
        # Unknown stratum reference and out-of-range sample counts still fail.
        bad = source([('h', 2)], [('u1', 'missing', '1'), ('u2', 'missing', '2')])
        with self.assertRaises(ValueError):
            plan(bad, 2)
        thin = source([('h', 4)], [('u1', 'h', '1'), ('u2', 'h', '2'),
                                   ('u3', 'h', '3'), ('u4', 'h', '4'),
                                   ('u5', 'h', '5')])
        with self.assertRaises(ValueError):
            plan(thin, 2)


class AllocateCliTests(unittest.TestCase):
    def cli(self, source_path, size, output):
        return subprocess.run([sys.executable, '-m', 'survey_estimate.allocate',
                               '--source', str(source_path), '--size', str(size),
                               '--output', str(output)], capture_output=True)

    def test_cli_publishes_atomically_and_failure_preserves_output(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td)
            out = p / 'quota.json'
            r = self.cli(ROOT / 'fixtures/allocate_pilot.json', 8, out)
            self.assertEqual(r.returncode, 0, r.stderr)
            data = json.loads((ROOT / 'fixtures/allocate_pilot.json').read_text())
            self.assertEqual(json.loads(out.read_text()), plan(data, 8))
            before = out.read_bytes()
            r = self.cli(ROOT / 'fixtures/allocate_pilot.json', 5, out)
            self.assertEqual(r.returncode, 2)
            self.assertIn(b'infeasible budget', r.stderr)
            self.assertEqual(out.read_bytes(), before)
            bad = p / 'bad.json'
            bad.write_text('{}')
            self.assertEqual(self.cli(bad, 8, out).returncode, 2)
            self.assertEqual(out.read_bytes(), before)
            missing = p / 'missing.json'
            self.assertEqual(self.cli(missing, 8, out).returncode, 2)
            self.assertEqual(out.read_bytes(), before)
            self.assertFalse(list(p.glob('tmp*')))

    def test_cli_rejects_bad_size_and_same_input_output(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / 'quota.json'
            for size in ('0', '-3', '1.5', '100001', 'abc', ''):
                r = self.cli(ROOT / 'fixtures/allocate_pilot.json', size, out)
                self.assertEqual(r.returncode, 2, size)
                self.assertFalse(out.exists())
            same = ROOT / 'fixtures/allocate_pilot.json'
            r = self.cli(same, 8, same)
            self.assertEqual(r.returncode, 2)
            self.assertIn(b'source and output must differ', r.stderr)


if __name__ == '__main__':
    unittest.main()
