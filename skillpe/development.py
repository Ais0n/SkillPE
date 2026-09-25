"""Caption retrieval, audiovisual reference assessment, and frozen candidate evolution."""
import html
import json
import math
import random
import re
import subprocess
from collections import Counter
from pathlib import Path

from .io import digest, freeze, prompt, read, write

TOXICITY = {'toxicity': .5, 'obscene': .5, 'identity_attack': .3,
            'insult': .5, 'threat': .3, 'sexual_explicit': .5}
REFERENCE_DIMS = ('camera_movement', 'spatial_composition', 'lighting_color',
                  'atmosphere', 'shot_logic', 'audio_logic')
CATEGORIES = ('abstract_creative', 'creature', 'environment', 'human_centric', 'object_focus')
SUMMARY_SYSTEM = ('You extract concise visual summaries for cinematic skill templates. '
    'Return only one complete English sentence with fewer than 30 whitespace-separated words. '
    'Describe only subject, core action, and spatial environment. Ignore camera parameters, lighting, and plot.')


def complexity_bin(row):
    # Deliberately do not accept the historical four-score average as a fallback.
    value = row['narrative_complexity']
    if type(value) not in (int,float) or not 0 <= value <= 1:
        raise ValueError('Narrative complexity must be in [0,1]')
    return min(int(value*4),3)


def clean_prompt(row):
    text = html.unescape(row.get('prompt', '')).strip()
    if len(text.split()) < 5 or re.search(r'--ar \d+:\d+|-FPS \d+|Message: \d+ Attachment|PARAMETERS:', text):
        return None
    for key, threshold in TOXICITY.items():
        try:
            if float(row.get(key)) >= threshold:
                return None
        except (ValueError, TypeError):
            pass
    return {**row, 'prompt': text}


def reservoir(rows, per_stratum=650, seed=20260714):
    """One fixed equal-quota draw: 5 categories x 4 bins x 650 = 13,000."""
    if per_stratum < 1:
        raise ValueError('Positive stratum quota required')
    rng = random.Random(seed)
    pools = {(category,b):[] for category in CATEGORIES for b in range(4)}
    counts, seen = {}, set()
    for raw in rows:
        row = clean_prompt(raw)
        identity = ' '.join(row['prompt'].casefold().split()) if row else None
        if not row or identity in seen:
            continue
        seen.add(identity)
        if row['category'] not in CATEGORIES:
            raise ValueError('Unknown prompt category')
        key = (row['category'], complexity_bin(row))
        pool = pools[key]
        counts[key] = counts.get(key, 0)+1
        if len(pool) < per_stratum:
            pool.append(row)
        else:
            j = rng.randrange(counts[key])
            if j < per_stratum:
                pool[j] = row
    if any(len(pool) != per_stratum for pool in pools.values()):
        raise ValueError('Insufficient prompts for the fixed stratified sample; no automatic expansion')
    return [r for k in sorted(pools) for r in pools[k]]


def balanced_select(rows, count, source_skill_id, seed=20260729):
    """Greedy joint-stratum, category and bin balancing with deterministic ties."""
    if len(rows) < count:
        raise ValueError('Insufficient routed prompts for '+source_skill_id)
    categories, bins, strata = Counter(), Counter(), Counter()
    remaining, selected = list(rows), []
    while len(selected) < count:
        def key(row):
            category, b = row['category'], complexity_bin(row)
            return (strata[(category,b)], categories[category], bins[b],
                    digest([seed,source_skill_id,row['prompt_id']]))
        chosen = min(remaining,key=key)
        remaining.remove(chosen)
        selected.append(chosen)
        category,b = chosen['category'],complexity_bin(chosen)
        categories[category] += 1
        bins[b] += 1
        strata[(category,b)] += 1
    return selected


def summarize_skill(client, skill):
    if skill.get('role') != 'expert':
        raise ValueError('Retrieval summaries require original expert-authored skills')
    request = {'expert_skill':skill['skill']}
    for _ in range(3):
        value,audit = client.generate(SUMMARY_SYSTEM,request,json_mode=False)
        text = value.strip().strip('"')
        if (0 < len(text.split()) < 30 and re.search(r'[A-Za-z]',text)
            and not re.search(r'[\u3400-\u9fff]',text) and '\n' not in text
            and len(re.findall(r'[.!?](?:\s|$)',text)) == 1
            and text.endswith(('.', '!', '?'))):
            return text,audit
        request['correction'] = 'Return one complete English sentence, fewer than 30 words, without headings.'
    raise ValueError('Summary does not satisfy the one-sentence / under-30-word contract')


def retrieval_indices(rankings, top_k=200, extra=10, seed=20260709):
    """Select unseen candidates from each other skill's top-200 pool, filling quota."""
    rng = random.Random(seed)
    pools = []
    for i,ranking in enumerate(rankings):
        selected = list(map(int,ranking[:top_k]))
        seen = set(selected)
        if len(seen) != top_k:
            raise ValueError('Invalid or undersized primary ranking')
        for j,other in enumerate(rankings):
            if i == j:
                continue
            candidates = [int(x) for x in other[:top_k] if int(x) not in seen]
            if len(candidates) < extra:
                raise ValueError('Cannot fill cross-skill quota from the other top-200 pool')
            added = rng.sample(candidates,extra)
            selected.extend(added)
            seen.update(added)
        pools.append(selected)
    return pools


def retrieve(skills, references, checkpoint, device='cuda', batch_size=128, summaries=None):
    """Qwen text embeddings of captions, NOT visual embeddings."""
    import numpy as np
    import torch
    import torch.nn.functional as F
    from transformers import AutoModel, AutoTokenizer
    tokenizer = AutoTokenizer.from_pretrained(checkpoint, local_files_only=True, padding_side='left')
    model = AutoModel.from_pretrained(checkpoint, local_files_only=True, torch_dtype=torch.bfloat16).to(device).eval()
    def embed(texts):
        blocks = []
        for start in range(0, len(texts), batch_size):
            batch = tokenizer(texts[start:start+batch_size], padding=True, truncation=True,
                              max_length=8192, return_tensors='pt').to(device)
            with torch.inference_mode():
                last = model(**batch).last_hidden_state[:, -1]
                blocks.append(F.normalize(last.float(), p=2, dim=1).cpu().numpy())
        return np.concatenate(blocks)
    if any(s.get('role') != 'expert' for s in skills):
        raise ValueError('Retrieval requires original expert-authored skills')
    refs = [r for r in references if r.get('caption','').strip() and 5 <= float(r['end'])-float(r['start']) <= 20]
    if len({r['id'] for r in refs}) != len(refs):
        raise ValueError('Reference IDs must be globally unique')
    if len(refs) < 200:
        raise ValueError('At least 200 eligible references required')
    if summaries is None:
        raise ValueError('Frozen visual summaries are required')
    queries = ['Instruct: Given a concise cinematic skill visual summary, retrieve movie clip captions '
               'that contain locally similar visual, spatial, atmospheric, camera, or audio elements.\nQuery:'+ 
               summaries[s['source_skill_id']] for s in skills]
    similarity = embed(queries) @ embed([r['caption'] for r in refs]).T
    rankings = np.argsort(-similarity, axis=1, kind='stable')
    output = []
    for i, (skill,indices) in enumerate(zip(skills,retrieval_indices(rankings))):
        for index in indices:
            output.append({'source_skill_id': skill['source_skill_id'], 'skill': skill['skill'],
                           'reference': refs[index], 'similarity': float(similarity[i, index])})
    return output


def assess_reference(client, job, directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    ref = job['reference']
    av, silent = directory/'reference.mp4', directory/'silent.mp4'
    if not av.exists():
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-ss', str(ref['start']), '-i', ref['video_path'],
            '-t', str(float(ref['end'])-float(ref['start'])), '-vf', 'scale=-2:720', '-c:v', 'libx264',
            '-crf', '23', '-c:a', 'aac', '-threads', '2', str(av)], check=True, capture_output=True)
    if not silent.exists():
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', str(av), '-an', '-c:v', 'copy', str(silent)],
                       check=True, capture_output=True)
    judgments = {}
    for d in REFERENCE_DIMS:
        path = directory/(d+'.json')
        if path.exists():
            judgments[d] = read(path)
            continue
        request = prompt('reference/'+d).replace('{skill_text}', json.dumps(job['skill'], ensure_ascii=False))
        value, audit = client.generate(prompt('reference/evaluation_system'), request,
                                      media=[av if d == 'audio_logic' else silent])
        for key in ('alignment', 'inspiration'):
            if type(value.get(key)) not in (float, int) or not 0 <= value[key] <= 1:
                raise ValueError('Invalid reference score')
        if value.get('evidence_status') == 'not_observable' and (value['alignment'] or value['inspiration']):
            raise ValueError('Unobservable evidence must have zero scores')
        judgments[d] = {'judgment': value, 'audit': audit}
        write(path, judgments[d])
    description, audit = client.generate('Describe transferable cinematic execution in English. Return JSON with a description field.',
                                        ref['caption'], media=[av])
    return {**job, 'alignment': sum(x['judgment']['alignment'] for x in judgments.values()),
            'inspiration': sum(x['judgment']['inspiration'] for x in judgments.values()),
            'judgments': judgments, 'description': description, 'description_audit': audit}


def partition(records):
    def identity(x):
        return str(x['reference']['id'])
    resonant = sorted(records, key=lambda r: (-r['alignment'], -r['inspiration'], identity(r)))[:10]
    used = {identity(r) for r in resonant}
    divergent = sorted((r for r in records if identity(r) not in used),
                       key=lambda r: (-r['inspiration'], -r['alignment'], identity(r)))[:15]
    used.update(identity(r) for r in divergent)
    dissonant = sorted((r for r in records if identity(r) not in used and r['similarity'] > .35),
                      key=lambda r: (r['alignment'], -r['similarity'], identity(r)))[:5]
    if (len(resonant), len(divergent), len(dissonant)) != (10, 15, 5):
        raise ValueError('Not enough valid references for disjoint 10/15/5 quotas')
    return {'resonant': resonant, 'divergent': divergent, 'dissonant': dissonant}


def evolve(client, seed, references, role):
    """Each mutation starts from the same normalized seed; R&D augments guidance."""
    sid = seed['source_skill_id']
    def compact(records):
        return [{'reference_id': r['reference']['id'], 'caption': r['reference']['caption'],
                 'audiovisual_description': r['description'],
                 'dimension_evidence': {d: v['judgment'] for d,v in r['judgments'].items()}}
                for r in records]
    if role == 'r_and_d':
        value, audit = client.generate(prompt('reflection'), {'seed_skill': seed['skill'],
            'positive': compact(references['resonant']),
            'negative': compact(references['dissonant'])}, temperature=.3)
        for field in ('core_invariants','cinematic_patterns','modifiable_dimensions','strengthen_when',
                      'weaken_when','do_not_force_when','rhythm_and_cut_notes','application_notes'):
            if not isinstance(value.get(field),list) or not value[field] or not all(isinstance(s,str) and s.strip() for s in value[field]):
                raise ValueError('Missing R&D guidance: '+field)
        skill = {**seed['skill'], 'usage_guidance': value,
                 'usage_examples': {'positive': compact(references['resonant']),
                                    'negative': compact(references['dissonant'])}}
    else:
        if role not in ('bold', 'wilder', 'extreme'):
            raise ValueError('Unknown mutation level')
        value, audit = client.generate(prompt('divergent'), {'source_skill_id': sid, 'raw_seed_skill': seed['skill'],
            'divergent_references': compact(references['divergent']), 'mutation_level': role}, temperature=.8)
        candidate = value['candidate']
        changed = set()
        for item in candidate['modified_dimensions']:
            match = re.match(r'^(D[1-9])\b', str(item['dimension']))
            if not match:
                raise ValueError('Unknown mutation dimension')
            changed.add(match.group(1))
        if not changed <= {'D'+str(i) for i in range(1, 10)}:
            raise ValueError('Unknown mutation dimension')
        unchanged = set()
        for item in candidate['unchanged_dimensions']:
            match = re.match(r'^(D[1-9])\b', str(item))
            if not match:
                raise ValueError('Unknown unchanged dimension')
            unchanged.add(match.group(1))
        if changed & unchanged or changed | unchanged != {'D'+str(i) for i in range(1,10)}:
            raise ValueError('Mutation dimensions must partition D1 through D9')
        valid = (1 <= len(changed) <= 2 if role == 'bold' else
                 3 <= len(changed) <= 4 and bool(changed & {'D1','D2','D4','D6','D7'}) if role == 'wilder' else
                 len(changed) >= 5 and bool(changed & {'D1','D8'}))
        if not valid:
            raise ValueError('Mutation level constraints not met')
        skill = candidate['skill']
    if not skill.get('shots'):
        raise ValueError('Candidate has no shots')
    return {'source_skill_id': sid, 'candidate_id': sid+'_'+role, 'role': role,
            'skill': skill, 'audit': audit}
