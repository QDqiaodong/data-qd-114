"""Plan integer stratified SRSWOR quotas from a pilot sample.

The pilot document is validated with the same parser as ``survey-estimate``.
The pilot unbiased within-stratum variances s2_h are treated as fixed
planning parameters. A stratum with population one cannot estimate a
variance, so its quota is fixed at zero; every other stratum needs at least
two pilot units and is constrained to ``2 <= n_h <= N_h``.

The predicted total variance ``sum N_h**2 * (1 - n_h/N_h) * s2_h / n_h`` is
minimized exactly. Writing ``A_h = N_h**2 * s2_h`` the objective is
``sum A_h/n_h`` up to a constant; the marginal reduction of moving from n
to n+1 units is ``A_h/(n*(n+1))``, a strictly decreasing sequence whenever
A_h is positive. A binary heap always serves the largest marginal, which
yields the global integer optimum without enumerating allocations or
scanning every stratum per added unit. All comparisons use Fraction, never
floats. Ties are served in stratum-id order, producing the lexicographically
largest quota vector among all global minimizers. When A_h is zero the
successor marginal equals the current one, so a stratum may hold several
zero-key entries at once; stale entries are skipped lazily.
"""
import argparse
import heapq
import json
import sys
from fractions import Fraction
from pathlib import Path

from .design import parse
from .io import load, publish

MAX_BUDGET = 100_000


def _pilot_variance(values):
    """Unbiased sample variance of exact Fraction observations."""
    n = len(values)
    mean = sum(values, Fraction()) / n
    return sum(((v - mean) ** 2 for v in values), Fraction()) / (n - 1)


def _fraction(value):
    if value is None:
        return None
    return {'numerator': str(value.numerator), 'denominator': str(value.denominator)}


def plan(data, budget):
    """Return the optimal integer quota plan for ``budget`` total units."""
    if type(budget) is not int or not 1 <= budget <= MAX_BUDGET:
        raise ValueError('budget must be an integer in 1..%d' % MAX_BUDGET)
    strata = parse(data)
    ids = sorted(strata)
    entries = []
    lower = upper = 0
    heap = []
    for index, sid in enumerate(ids):
        s = strata[sid]
        population = s['population']
        values = [value for _, value in s['observations']]
        pilot = len(values)
        if population == 1:
            # Variance cannot be estimated: the stratum is excluded from planning.
            entries.append({'stratum': sid, 'population': 1, 'pilot_sample_size': pilot,
                            'pilot_variance': None, 'quota': 0,
                            'coefficient': None, 'planned_total_variance': None})
            continue
        if pilot < 2:
            raise ValueError('stratum with population >= 2 needs at least 2 pilot units: ' + sid)
        variance = _pilot_variance(values)
        coefficient = Fraction(population ** 2) * variance
        quota = 2
        lower += 2
        upper += population
        entries.append({'stratum': sid, 'population': population, 'pilot_sample_size': pilot,
                        'pilot_variance': variance, 'quota': quota,
                        'coefficient': coefficient, 'planned_total_variance': None})
        marginal = coefficient / (quota * (quota + 1))
        heapq.heappush(heap, (-marginal, index, quota))
    if not lower <= budget <= upper:
        raise ValueError('infeasible budget %d: feasible quota total is %d..%d' % (budget, lower, upper))
    # Greedy marginal allocation; equal marginals go to the earlier stratum id.
    # A zero-variance stratum can leave several equal zero-key entries behind,
    # so entries whose quota is stale (or saturated) are skipped lazily.
    for _ in range(budget - lower):
        while True:
            _, index, at = heapq.heappop(heap)
            entry = entries[index]
            if at == entry['quota'] and entry['quota'] < entry['population']:
                break
        entry['quota'] += 1
        quota = entry['quota']
        if quota < entry['population']:
            marginal = entry['coefficient'] / (quota * (quota + 1))
            heapq.heappush(heap, (-marginal, index, quota))
    total_population = sum(entry['population'] for entry in entries)
    predicted_total = Fraction()
    rows = []
    for entry in entries:
        contribution = None
        if entry['coefficient'] is not None:
            # N**2 * (1 - n/N) * s2 / n = A * (N - n) / (N * n)
            contribution = (entry['coefficient'] * (entry['population'] - entry['quota'])
                            / (entry['population'] * entry['quota']))
            predicted_total += contribution
        rows.append({'stratum': entry['stratum'], 'population': entry['population'],
                     'pilot_sample_size': entry['pilot_sample_size'],
                     'pilot_variance': _fraction(entry['pilot_variance']),
                     'quota': entry['quota'],
                     'planned_total_variance': _fraction(contribution)})
    return {'budget': budget, 'population': total_population, 'planned_sample_size': budget,
            'predicted_variance_total': _fraction(predicted_total),
            'predicted_variance_mean': _fraction(predicted_total / total_population ** 2),
            'strata': rows}


def _budget(raw):
    if not raw.isascii() or not raw.isdigit() or not 1 <= int(raw) <= MAX_BUDGET:
        raise argparse.ArgumentTypeError('size must be an integer between 1 and %d' % MAX_BUDGET)
    return int(raw)


def main():
    parser = argparse.ArgumentParser(
        description='Plan integer stratified SRSWOR quotas from a pilot sample')
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--size', type=_budget, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.source.resolve() == args.output.resolve():
            raise ValueError('source and output must differ')
        result = plan(load(args.source), args.size)
        publish(args.output, result)
    except (ValueError, OSError, KeyError, TypeError, OverflowError, RecursionError) as e:
        print(str(e), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
