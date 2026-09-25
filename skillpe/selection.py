"""Paired five-candidate selection; benchmark scores are never selection inputs."""
from collections import defaultdict
from statistics import mean, median
import random

from .io import DIMS, ROLES


def select(catalog, scores, manifest):
    expected = defaultdict(set)
    all_prompts = set()
    for row in manifest:
        if row['benchmark'] != 'development' or row['prompt_id'] in all_prompts:
            raise ValueError('Development manifest must contain unique development prompt IDs')
        all_prompts.add(row['prompt_id'])
        expected[row['source_skill_id']].add(row['prompt_id'])
    by_id = {r['candidate_id']: r for r in catalog}
    families = defaultdict(list)
    for row in catalog:
        families[row['source_skill_id']].append(row)
    if not families or set(expected) != set(families) or any(len(ids) != 20 for ids in expected.values()):
        raise ValueError('Exactly 20 frozen development prompts per source family are required')
    if len(by_id) != len(catalog):
        raise ValueError('Duplicate candidate ID')
    seen = set()
    pairs = defaultdict(dict)
    for row in scores:
        if row['benchmark'] != 'development':
            raise ValueError('Only development scores may select skills')
        identity = (row['candidate_id'], row['prompt_id'])
        if identity in seen:
            raise ValueError('Duplicate development score')
        seen.add(identity)
        candidate = by_id[row['candidate_id']]
        if row['prompt_id'] not in expected[candidate['source_skill_id']]:
            raise ValueError('Score not in the frozen candidate-family development manifest')
        values = row.get('scores', {})
        if all(type(values.get(d)) in (int, float) and 1 <= values[d] <= 7 for d in DIMS):
            pairs[(candidate['source_skill_id'], row['prompt_id'])][row['candidate_id']] = values
    libraries = {'top': [], 'top2': [], 'overall': []}
    report = []
    for family, candidates in sorted(families.items()):
        if len(candidates) != 5 or {c['role'] for c in candidates} != set(ROLES):
            raise ValueError('Each family must have exactly five candidate roles')
        ids = {c['candidate_id'] for c in candidates}
        common = [v for (sid, _), v in pairs.items() if sid == family and set(v) == ids]
        if len(common) != 20:
            raise ValueError('All 20 complete five-way paired development prompts are required for ' + family)
        records = []
        for c in candidates:
            values = {d: mean(p[c['candidate_id']][d] for p in common) for d in DIMS}
            records.append({**c, 'means': values, 'creative_mean': mean(values[d] for d in DIMS[1:]),
                            'overall_mean': mean(values.values()), 'n_paired_prompts': len(common)})
        floor = median(r['means'][DIMS[0]] for r in records)
        eligible = sorted((r for r in records if r['means'][DIMS[0]] >= floor),
            key=lambda r: (-r['creative_mean'], -r['means'][DIMS[0]], -r['overall_mean'], r['candidate_id']))
        overall = sorted(records, key=lambda r: (-r['overall_mean'], -r['means'][DIMS[0]], -r['creative_mean'], r['candidate_id']))
        libraries['top'].append(eligible[0])
        libraries['top2'].extend(eligible[:2])
        libraries['overall'].append(overall[0])
        report.append({'source_skill_id': family, 'fidelity_floor': floor, 'n_paired_prompts': len(common), 'candidates': records})
    return libraries, report


def random_library(catalog, seed):
    groups = defaultdict(list)
    for row in catalog:
        groups[row['source_skill_id']].append(row)
    rng = random.Random(seed)
    return [rng.choice(sorted(v, key=lambda r: r['candidate_id'])) for _, v in sorted(groups.items())]


def subset_library(library, n, seed):
    families = sorted({r['source_skill_id'] for r in library})
    selected = set(random.Random(seed).sample(families, n))
    return [r for r in library if r['source_skill_id'] in selected]
