"""Routing and rewriting: selection is frozen separately from realization."""
import math

from .io import prompt


def route(client, row, skills, temperature=.1):
    candidates = []
    for i, c in enumerate(skills, 1):
        s = c['skill']
        candidates.append({'index': i, 'name': s.get('name'), 'type': s.get('type'),
                           'applicable_scenarios': s.get('applicable_scenarios', [])[:8],
                           'shots': s.get('shots'), 'shot_logic': s.get('shot_logic'),
                           'music_logic': s.get('music_logic'),
                           'usage_guidance': s.get('usage_guidance', {})})
    value, audit = client.generate(prompt('route'), {'original_prompt': row['prompt'],
        'candidate_skills': candidates}, temperature=temperature)
    index = value.get('selected_index')
    if type(index) is not int or not 1 <= index <= len(skills):
        raise ValueError('Invalid selected_index')
    return skills[index-1], audit


def rewrite(client, row, method, skill=None, budget=None, development=False):
    if method == 'raw':
        return {'pe_prompt': row['prompt'], 'audit': []}
    system = prompt('rewrite' if skill else 'direct')
    request = {'original_prompt': row['prompt'],
               'target_duration_seconds': 10, 'benchmark': row['benchmark']}
    if skill:
        request['selected_seed_skill'] = skill['skill']
    if budget is not None:
        system += '\n\n' + prompt('word_budget').format(budget=budget,
            lower=math.ceil(.95*budget), upper=math.floor(1.05*budget))
        request.update(word_budget=budget, minimum_words=math.ceil(.95*budget),
                       maximum_words=math.floor(1.05*budget))
    audit = []
    for _ in range(3):
        value, a = client.generate(system, request, temperature=.35 if development else (0 if skill else .25))
        audit.append(a)
        text = value.get('pe_prompt', '')
        if text and (budget is None or math.ceil(.95*budget) <= len(text.split()) <= math.floor(1.05*budget)):
            return {'pe_prompt': text, 'audit': audit, 'candidate_id': skill['candidate_id'] if skill else None}
        request['previous_response'] = value
        request['correction'] = 'Return a nonempty pe_prompt satisfying the stated word budget.'
    raise ValueError('Rewrite failed validation')


def adapt_h3(client, text, original):
    value, audit = client.generate(prompt('h3_adapter'),
        {'original_prompt': original, 'skill_conditioned_rewrite': text}, temperature=0)
    result = value.get('h3_prompt', '')
    if not isinstance(result, str) or not result.strip():
        raise ValueError('Adapter returned no prompt')
    return result, audit
