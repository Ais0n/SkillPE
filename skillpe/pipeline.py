"""One data model for development, main experiments, and ablations."""
import time
from pathlib import Path

from .client import Gemini, TextCompletion
from .io import ROOT, DIMS, digest, freeze, library, prompt, read, write
from .queue import Queue
from . import development, evaluation, pe


def protocol(config):
    return {'config': config,
            'implementation': {str(p.relative_to(ROOT)): digest(p.read_text(encoding='utf-8'))
                               for p in sorted((ROOT/'skillpe').glob('*.py'))},
            'prompts': {str(p.relative_to(ROOT)): digest(p.read_text(encoding='utf-8'))
                                        for p in sorted((ROOT/'prompts').rglob('*.md'))}}


def publish(root, config, manifest, arms, libraries=None, catalog=None, budget=None, benchmark_rows=None):
    if catalog is not None:
        if not benchmark_rows or {r['benchmark'] for r in benchmark_rows} != {'storyeval','vbench'}:
            raise ValueError('Development publishing requires the StoryEval/VBench benchmark union')
        excluded = {' '.join(r['prompt'].casefold().split()) for r in benchmark_rows}
        if any(' '.join(r['prompt'].casefold().split()) in excluded for r in manifest):
            raise ValueError('Development and benchmark prompts overlap')
        freeze(Path(root)/'benchmark_exclusion.json', {'count':len(benchmark_rows),'sha256':digest(benchmark_rows)})
    frozen = protocol(config)
    freeze(Path(root)/'protocol.json', frozen)
    freeze(Path(root)/'manifest.json', manifest)
    libraries = libraries or {}
    count = 0
    seen = set()
    for row in manifest:
        identity = (row['benchmark'], str(row['prompt_id']))
        if identity in seen:
            raise ValueError('Duplicate benchmark/prompt ID')
        seen.add(identity)
        if catalog is not None:
            if row['benchmark'] != 'development':
                raise ValueError('Candidate selection uses development prompts only')
            choices = [(c['candidate_id'], [c]) for c in catalog if c['source_skill_id'] == row['source_skill_id']]
            if len(choices) != 5:
                raise ValueError('Expected five paired candidates')
        else:
            choices = [(arm, libraries.get(arm)) for arm in arms]
        for arm, skills in choices:
            if skills is None and arm not in ('raw','direct','vpo','prompt_a_video','mora','rapo'):
                raise ValueError('Missing arm library: '+arm)
            job = {'kind': 'pe', 'row': row, 'arm': arm, 'skills': skills, 'budget': budget,
                   'development': catalog is not None, 'seed': row.get('seed', config['generation_seed']),
                   'protocol': digest(frozen), 'backbone': config['backbone']}
            channel = arm if arm in ('vpo','prompt_a_video','mora','rapo') else 'pe'
            Queue(root, channel).publish(job)
            count += 1
    return count


class Handler:
    def __init__(self, root, config):
        self.root, self.config = Path(root), config
        self.client = Gemini(config.get('judge_model', 'gemini-3.1-pro-preview'))
        self.engines = {}

    def __call__(self, job):
        key = digest(job)[:32]
        directory = self.root/'records'/key
        directory.mkdir(parents=True, exist_ok=True)
        kind = job['kind']
        if kind == 'annotate':
            row = development.clean_prompt(job['row'])
            if row is None:
                return {'rejected': True}
            client=TextCompletion('QWEN',job['model'])
            value, audit = client.generate(prompt('annotation_system'),
                                          prompt('annotation').format(prompt=row['prompt']))
            complexity=value.get('narrative_complexity')
            if type(complexity) not in (int,float) or not 0 <= complexity <= 1:
                raise ValueError('Invalid source annotation')
            if value.get('primary_category') not in ('human_centric','creature','environment','object_focus','abstract_creative'):
                raise ValueError('Invalid source category')
            return {**row,'category':value['primary_category'],
                    'narrative_complexity':complexity,'annotation':value,'audit':audit}
        if kind == 'reference':
            return development.assess_reference(self.client, job, directory)
        if kind == 'evolve':
            return development.evolve(self.client, job['seed_skill'], job['references'], job['role'])
        if kind == 'normalize':
            value, audit = self.client.generate(prompt('normalize'), {'skill': job['skill']['skill']}, temperature=.4)
            skill = value.get('skill')
            if not isinstance(skill, dict) or not skill.get('shots'):
                raise ValueError('Invalid normalized skill')
            sid = job['skill']['source_skill_id']
            return {'source_skill_id': sid, 'candidate_id': sid+'_seed', 'role': 'seed', 'skill': skill, 'audit': audit}
        row = job['row']
        if kind == 'pe':
            arm, skills = job['arm'], job['skills']
            routing = None
            if skills:
                selected_path = directory/'route.json'
                if selected_path.exists():
                    selected = read(selected_path)
                else:
                    skill, routing = (skills[0], None) if job['development'] else pe.route(self.client, row, skills)
                    selected = {'skill': skill, 'audit': routing}
                    freeze(selected_path, selected)
                value = pe.rewrite(self.client, row, arm, selected['skill'], job['budget'], job['development'])
                value['routing'] = selected
            elif arm in ('raw', 'direct'):
                value = pe.rewrite(self.client, row, arm, budget=job['budget'])
            else:
                from .baselines import ExternalRewriter, Mora, rapo
                cfg = self.config['baselines'][arm]
                if arm == 'rapo':
                    value = rapo(self.client, cfg, row, directory/'rapo')
                else:
                    if arm not in self.engines:
                        self.engines[arm] = Mora(cfg, self.client) if arm == 'mora' else ExternalRewriter(arm, cfg)
                    value = self.engines[arm](row, job['seed'], directory/'first_frame.png') if arm == 'mora' else self.engines[arm](row)
            value['before_adapter'] = value['pe_prompt']
            if job['backbone'] == 'h3' and job['budget'] is None:
                value['pe_prompt'], value['adapter_audit'] = pe.adapt_h3(self.client, value['pe_prompt'], row['prompt'])
            return value
        if kind == 'generate':
            from .generation import H3, LTX
            if 'generator' not in self.engines:
                self.engines['generator'] = (H3 if job['backbone'] == 'h3' else LTX)(self.config['generation'])
            path = directory/'video.mp4'
            value = self.engines['generator'](job, path)
            return {**value, 'video': str(path)}
        if kind == 'evaluate':
            return evaluation.score(self.client, row, job['video'], directory)
        if kind == 'storyeval':
            return evaluation.storyeval(self.client, row, job['video'])
        raise ValueError('Unknown task kind')


def bridge(root, config, watch=False, score=True, official=False):
    """Publish downstream jobs immediately; failures never block unrelated jobs."""
    while True:
        for channel in ('pe', 'vpo', 'prompt_a_video', 'mora', 'rapo', config['backbone']):
            q = Queue(root, channel)
            for path in sorted((q.root/'results').glob('*.json')):
                source = read(q.root/'jobs'/path.name)
                result = read(path)
                if source['kind'] == 'pe':
                    job = {k: source[k] for k in ('row','arm','seed','backbone','protocol','development')}
                    job.update(kind='generate', pe_prompt=result['pe_prompt'], pe_task=path.stem,
                               candidate_id=result.get('candidate_id'))
                    if result.get('image_path'):
                        job['image_path'] = result['image_path']
                    Queue(root, config['backbone']).publish(job)
                elif source['kind'] == 'generate':
                    if not Path(result['video']).is_file():
                        raise FileNotFoundError('Completed video missing; restore or explicitly invalidate its result')
                    job = {k: source[k] for k in ('row','arm','seed','backbone','protocol','development','candidate_id')}
                    job.update(video=result['video'], generation_task=path.stem)
                    if score:
                        Queue(root, 'gemini').publish({**job, 'kind': 'evaluate'})
                    if official and source['row']['benchmark'] == 'storyeval':
                        Queue(root, 'storyeval').publish({**job, 'kind': 'storyeval'})
        if not watch:
            return
        time.sleep(10)


def scored_rows(root, channel='gemini'):
    q = Queue(root, channel)
    results = []
    for path in sorted((q.root/'results').glob('*.json')):
        job, result = read(q.root/'jobs'/path.name), read(path)
        results.append({**job['row'], 'arm': job['arm'], 'candidate_id': job.get('candidate_id'),
                        'backbone': job['backbone'], 'seed': job['seed'], **result})
    return results
