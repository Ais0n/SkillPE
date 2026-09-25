import json
from pathlib import Path
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from collections import Counter
from unittest.mock import patch

from skillpe.io import DIMS, ROLES, freeze, read, write, library, ROOT, digest
from skillpe.queue import Queue
from skillpe.selection import select, random_library, subset_library
from skillpe.development import (clean_prompt, partition, reservoir, CATEGORIES,
                                complexity_bin, balanced_select, summarize_skill, retrieval_indices)
from skillpe.pipeline import Handler, bridge, publish, scored_rows
from skillpe.pe import rewrite
from skillpe.official import normalized_total


def catalog():
    return [{'candidate_id': r, 'source_skill_id':'s', 'role':r, 'skill':{'shots':[{}]}} for r in ROLES]

def development_manifest():
    return [{'benchmark':'development','source_skill_id':'s','prompt_id':f'p{i}'} for i in range(20)]

def complete_scores():
    return [{'benchmark':'development','candidate_id':r,'prompt_id':f'p{j}',
             'scores':dict(zip(DIMS,[5,i+2,i+2,i+2]))} for j in range(20) for i,r in enumerate(ROLES)]


class CoreTests(unittest.TestCase):
    def test_artifact_scope(self):
        for folder in ('expert_authored','normalized_seed'):
            values=library(ROOT/'artifacts'/folder)
            self.assertEqual(len(values),20)
            self.assertEqual(len({x['source_skill_id'] for x in values}),20)
            for value in values:
                self.assertNotIn('embedding',value['skill'])
                self.assertNotIn('metadata',value)
                self.assertTrue(value['skill']['shots'])

    def test_frozen_input(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'input.json'
            freeze(p,{'x':1});freeze(p,{'x':1})
            with self.assertRaises(ValueError):freeze(p,{'x':2})

    def test_complete_pair_selection(self):
        records=complete_scores()
        libs,report=select(catalog(),records,development_manifest())
        self.assertEqual(libs['top'][0]['role'],'extreme')
        self.assertEqual(len(libs['top2']),2)
        self.assertEqual(report[0]['n_paired_prompts'],20)
        with self.assertRaises(ValueError):select(catalog(),records[:-1],development_manifest())
        records[0]['prompt_id']='outside_manifest'
        with self.assertRaises(ValueError):select(catalog(),records,development_manifest())

    def test_no_test_set_selection(self):
        with self.assertRaises(ValueError):
            select(catalog(),[{'benchmark':'storyeval'}],development_manifest())

    def test_invalid_excluded_not_zero(self):
        records=complete_scores()
        records[0]['scores'][DIMS[0]]=None
        with self.assertRaises(ValueError):select(catalog(),records,development_manifest())

    def test_toxicity(self):
        row={'prompt':'A person opens a large book.'}
        self.assertIsNotNone(clean_prompt(row))
        self.assertIsNone(clean_prompt({**row,'toxicity':.5}))
        self.assertIsNone(clean_prompt({**row,'identity_attack':.3}))
        self.assertIsNotNone(clean_prompt({**row,'threat':'unparseable'}))
        self.assertIsNone(clean_prompt({'prompt':'Too short'}))

    def test_reservoir_deterministic(self):
        rows=[{'prompt':f'A person opens book {category} {b} number {i}.','category':category,
               'narrative_complexity':b/4} for category in CATEGORIES for b in range(4) for i in range(8)]
        self.assertEqual(reservoir(rows,5),reservoir(rows,5))
        self.assertEqual(len(reservoir(rows,5)),100)

    def test_fixed_13000_draw(self):
        rows=[{'prompt_id':f'{c}_{b}_{i}','prompt':f'A person opens book {c} {b} number {i}.',
               'category':c,'narrative_complexity':b/4} for c in CATEGORIES for b in range(4) for i in range(652)]
        sample=reservoir(rows)
        self.assertEqual(len(sample),13000)
        self.assertEqual(set(Counter((r['category'],complexity_bin(r)) for r in sample).values()),{650})
        self.assertEqual(len({r['prompt_id'] for r in sample}),13000)

    def test_no_automatic_sample_expansion(self):
        with self.assertRaises(ValueError):reservoir([])
        with self.assertRaises(KeyError):complexity_bin({'complexity':.5})
        self.assertEqual([complexity_bin({'narrative_complexity':v}) for v in (0,.25,.5,.75,1)], [0,1,2,3,3])

    def test_greedy_balancing_and_order_independence(self):
        rows=[{'prompt_id':f'{c}_{b}_{i}','category':c,'narrative_complexity':b/4}
              for c in CATEGORIES for b in range(4) for i in range(3)]
        selected=balanced_select(rows,20,'skill_001')
        self.assertEqual(selected,balanced_select(list(reversed(rows)),20,'skill_001'))
        self.assertEqual(set(Counter(r['category'] for r in selected).values()),{4})
        self.assertEqual(set(Counter(complexity_bin(r) for r in selected).values()),{5})
        with self.assertRaises(ValueError):balanced_select(rows[:3],20,'skill_001')

    def test_summary_is_not_character_truncated(self):
        class Client:
            def generate(self,*args,**kwargs):
                return ('A solitary archaeologist painstakingly reconstructs fragmented inscriptions '
                        'while traversing interconnected subterranean chambers beneath an abandoned '
                        'metropolitan transportation interchange.'),{}
        text,_=summarize_skill(Client(),{'role':'expert','skill':{}})
        self.assertGreater(len(text),180)
        self.assertLess(len(text.split()),30)
        self.assertTrue(text.endswith('interchange.'))
        with self.assertRaises(ValueError):summarize_skill(Client(),{'role':'seed','skill':{}})

    def test_cross_skill_candidates_are_filled_without_duplicates(self):
        # Each other pool's first ten overlap; selecting only these would fail.
        rankings=[list(range(10))+list(range(10+200*i,200+200*i)) for i in range(20)]
        pools=retrieval_indices(rankings)
        self.assertEqual(pools,retrieval_indices(rankings))
        self.assertTrue(all(len(pool)==len(set(pool))==390 for pool in pools))
        with self.assertRaises(ValueError):retrieval_indices([list(range(200))]*20)

    def test_annotation_uses_qwen_and_single_score(self):
        with tempfile.TemporaryDirectory() as d:
            job={'kind':'annotate','model':'Qwen/Qwen3.6-27B',
                 'row':{'prompt_id':'x','prompt':'A person opens a large book.'}}
            with patch('skillpe.pipeline.TextCompletion') as factory:
                factory.return_value.generate.return_value=({'primary_category':'human_centric',
                    'narrative_complexity':.75,'cinematic_potential':0}, {})
                value=Handler(d,{})(job)
                factory.assert_called_once_with('QWEN','Qwen/Qwen3.6-27B')
            self.assertEqual(value['narrative_complexity'],.75)
            self.assertNotIn('complexity',value)

    def test_development_routes_all_13000_before_selecting(self):
        from skillpe.__main__ import main
        with tempfile.TemporaryDirectory() as d:
            source=Path(d)/'source.json';skills_path=Path(d)/'skills.json';output=Path(d)/'development.json'
            rows=[{'prompt_id':f'{c}_{b}_{i}','prompt':f'A person opens book {c} {b} number {i}.',
                   'category':c,'narrative_complexity':b/4,'test_group':i%20}
                  for c in CATEGORIES for b in range(4) for i in range(650)]
            skills=[{'source_skill_id':f'skill_{i+1:03d}','role':'expert','skill':{}} for i in range(20)]
            write(source,rows);write(skills_path,skills)
            count=[]
            def route(client,row,candidates,temperature):
                self.assertEqual(temperature,.2)
                self.assertTrue(all(s['role']=='expert' for s in candidates))
                self.assertEqual(len(read(Path(str(output)+'.routing')/'sample_13000.json')),13000) if not count else None
                count.append(1)
                return candidates[row['test_group']],{}
            benchmark_path=Path(d)/'benchmark.jsonl'
            benchmark_path.write_text('\n'.join(json.dumps({'benchmark':b,'prompt':'Excluded benchmark '+b}) for b in ('storyeval','vbench')))
            argv=['skillpe','development-set','--input',str(source),'--skills',str(skills_path),'--output',str(output),'--exclude',str(benchmark_path)]
            with patch('sys.argv',argv),patch('skillpe.pe.route',side_effect=route):main()
            self.assertEqual(len(count),13000)
            final=read(output)
            self.assertEqual(len(final),400)
            self.assertEqual(set(Counter(r['source_skill_id'] for r in final).values()),{20})

    def test_reference_partition_disjoint(self):
        rows=[{'reference':{'id':str(i)},'similarity':.9,'alignment':i/50,'inspiration':1-i/100} for i in range(40)]
        groups=partition(rows)
        self.assertEqual([len(groups[k]) for k in ('resonant','divergent','dissonant')],[10,15,5])
        self.assertEqual(len({r['reference']['id'] for values in groups.values() for r in values}),30)

    def test_queue_retries_and_idempotency(self):
        with tempfile.TemporaryDirectory() as d:
            q=Queue(d,'fake');key=q.publish({'x':1});self.assertEqual(key,q.publish({'x':1}))
            calls=[]
            def handler(job):
                calls.append(job)
                if len(calls)<2:raise RuntimeError('temporary')
                return {'value':3}
            q.worker(handler,concurrency=4)
            q.worker(handler)
            self.assertEqual(len(calls),2)
            self.assertEqual(q.result(key),{'value':3})

    def test_stale_running_not_lock(self):
        with tempfile.TemporaryDirectory() as d:
            q=Queue(d,'fake');key=q.publish({'x':2})
            write(q.root/'state'/(key+'.json'),{'status':'running','attempts':1})
            q.worker(lambda job:{'recovered':True})
            self.assertEqual(q.result(key),{'recovered':True})

    def test_failure_does_not_block(self):
        with tempfile.TemporaryDirectory() as d:
            q=Queue(d,'fake');q.publish({'bad':True});good=q.publish({'bad':False})
            def handler(job):
                if job['bad']:raise ValueError('permanent')
                return {'ok':True}
            q.worker(handler,attempts=2)
            self.assertEqual(q.status(),{'failed':1,'done':1})
            self.assertTrue(q.result(good)['ok'])

    def test_budget_retry(self):
        class Client:
            def __init__(self):self.n=0
            def generate(self,*args,**kwargs):
                self.n+=1
                assert '{budget}' not in args[0]
                assert '95 through 105' in args[0]
                return {'pe_prompt':'word '* (10 if self.n==1 else 100)},{}
        client=Client()
        r=rewrite(client,{'prompt':'test','benchmark':'storyeval'},'direct',budget=100)
        self.assertEqual(len(r['pe_prompt'].split()),100)
        self.assertEqual(client.n,2)

    def test_budget_bypasses_adapter(self):
        with tempfile.TemporaryDirectory() as d:
            h=Handler(d,{})
            job={'kind':'pe','row':{'prompt':'test','benchmark':'storyeval'},'arm':'seed',
                 'skills':[catalog()[0]],'budget':100,'development':True,'backbone':'h3','seed':1}
            with patch('skillpe.pipeline.pe.rewrite',return_value={'pe_prompt':'word '*100}),patch('skillpe.pipeline.pe.adapt_h3') as adapt:
                h(job)
                adapt.assert_not_called()

    def test_pair_seed_and_bridge(self):
        with tempfile.TemporaryDirectory() as d:
            config={'backbone':'h3','generation_seed':123}
            manifest=[{'benchmark':'storyeval','prompt_id':'x','prompt':'A bird flies over the trees.','seed':456}]
            self.assertEqual(publish(d,config,manifest,['raw','direct']),2)
            Queue(d,'pe').worker(lambda j:{'pe_prompt':j['row']['prompt']})
            bridge(d,config)
            bridge(d,config)
            generation=Queue(d,'h3')
            jobs=[read(p) for p in (generation.root/'jobs').glob('*.json')]
            self.assertEqual(len(jobs),2)
            self.assertEqual({j['seed'] for j in jobs},{456})
            def generate(job):
                video=Path(d)/(digest(job)+'.mp4');video.touch()
                return {'video':str(video)}
            generation.worker(generate)
            bridge(d,config)
            Queue(d,'gemini').worker(lambda j:{'scores':dict.fromkeys(DIMS,5),'overall':5})
            self.assertEqual(len(scored_rows(d)),2)

    def test_random_library_reproducible(self):
        self.assertEqual(random_library(catalog(),123),random_library(catalog(),123))
        self.assertEqual(len(subset_library(catalog(),1,123)),5)

    def test_no_partial_official_total(self):
        with self.assertRaises(ValueError):normalized_total({'subject_consistency':.9})

    def test_two_workers_claim_once(self):
        with tempfile.TemporaryDirectory() as d:
            q=Queue(d,'fake')
            for n in range(20):q.publish({'n':n})
            calls=[]
            def handler(job):
                calls.append(job['n'])
                return {'ok':True}
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures=[executor.submit(Queue(d,'fake').worker,handler,4) for _ in range(2)]
                for future in futures:future.result()
            self.assertEqual(sorted(calls),list(range(20)))

    def test_dimension_resume_and_invalid_null(self):
        from skillpe.evaluation import score
        class Client:
            model='test-only'
            def __init__(self):self.calls=[]
            def generate(self,system,request,**kwargs):
                dimension=next(d for d in DIMS if '"'+d+'"' in system)
                self.calls.append(dimension)
                valid=dimension!=DIMS[-1]
                return {'dimension':dimension,'valid':valid,'evidence':['Synthetic test evidence'],
                        'score':5 if valid else None,'confidence':'low','rationale':'Test fixture'},{}
        with tempfile.TemporaryDirectory() as d:
            source=Path(d)/'source.mp4';source.touch()
            def compress(source,target):target.touch()
            client=Client()
            with patch('skillpe.evaluation.compress',side_effect=compress):
                first=score(client,{'prompt':'A test prompt'},source,Path(d)/'score')
                second=score(client,{'prompt':'A test prompt'},source,Path(d)/'score')
            self.assertEqual(len(client.calls),4)
            self.assertEqual(first,second)
            self.assertIsNone(first['overall'])
            self.assertIsNone(first['scores']['creativity'])


if __name__ == '__main__':
    unittest.main()
