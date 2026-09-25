import ast
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from skillpe.io import DIMS, prompt
from skillpe.development import REFERENCE_DIMS, assess_reference, evolve, partition
from skillpe.pe import route, rewrite
from skillpe.pipeline import Handler, publish
from skillpe.generation import probe
from skillpe.baselines import Mora, rapo
from skillpe.evaluation import storyeval


class PaperTests(unittest.TestCase):
    def test_reference_assessment_uses_seven_calls_and_direct_audio(self):
        client=Mock()
        client.generate.side_effect=[({'alignment':.5,'inspiration':.25,
                                      'evidence_status':'observed'}, {}) for _ in REFERENCE_DIMS] + [
                                      ({'description':'Cinematic execution.'},{})]
        job={'skill':{'name':'Test skill'},'reference':{'caption':'A person opens a book.',
             'start':0,'end':10,'video_path':'unused.mp4'}}
        with tempfile.TemporaryDirectory() as d:
            directory=Path(d)
            with patch('skillpe.development.subprocess.run'):
                result=assess_reference(client,job,directory)
            self.assertEqual(client.generate.call_count,7)
            self.assertEqual(set(result['judgments']),set(REFERENCE_DIMS))
            self.assertEqual(result['alignment'],3)
            self.assertEqual(result['inspiration'],1.5)
            self.assertFalse((directory/'audio.json').exists())
            for dimension,call in zip(REFERENCE_DIMS,client.generate.call_args_list):
                expected='reference.mp4' if dimension=='audio_logic' else 'silent.mp4'
                self.assertEqual(call.kwargs['media'],[directory/expected])
                self.assertNotIn('Grounded audio inventory',call.args[1])
            self.assertEqual(client.generate.call_args_list[-1].kwargs['media'],
                             [directory/'reference.mp4'])

    def test_pe_requests_are_independent_of_event_annotations(self):
        row={'prompt':'A person opens a book.', 'benchmark':'storyeval'}
        annotated={**row,'events':['EVALUATION_ONLY_EVENT']}
        skill={'candidate_id':'s_seed','skill':{'shots':[{}]}}
        client=Mock()
        client.generate.return_value=({'selected_index':1,'pe_prompt':'A person opens a book.'},{})
        operations=[lambda r:route(client,r,[skill]),
                    lambda r:rewrite(client,r,'direct'),
                    lambda r:rewrite(client,r,'seed',skill),
                    lambda r:rewrite(client,r,'seed',skill,development=True)]
        for operation in operations:
            client.reset_mock()
            operation(row)
            expected=client.generate.call_args
            operation(annotated)
            self.assertEqual(client.generate.call_args,expected)
            self.assertNotIn('EVALUATION_ONLY_EVENT',str(client.generate.call_args))

    def test_mora_plan_is_independent_of_event_annotations(self):
        row={'prompt':'A person opens a book.', 'benchmark':'storyeval'}
        mora=Mora.__new__(Mora)
        mora.client=Mock()
        mora.client.generate.return_value=({'first_frame_prompt':'A closed book.',
                                           'i2v_prompt':'A person opens the book.'},{})
        mora.pipeline=Mock(return_value=SimpleNamespace(images=[Mock()]))
        with patch.dict('sys.modules',{'torch':Mock()}):
            mora(row,1,'unused.png')
            expected=mora.client.generate.call_args
            mora({**row,'events':['EVALUATION_ONLY_EVENT']},1,'unused.png')
        self.assertEqual(mora.client.generate.call_args,expected)
        self.assertNotIn('EVALUATION_ONLY_EVENT',str(expected))

    def test_storyeval_retains_event_annotations_for_scoring(self):
        client=Mock()
        client.generate.side_effect=[('A description.',{}),
                                     ('Finally we have [COMPLETE_LIST]: 1',{})]*3
        result=storyeval(client,{'prompt':'A person opens a book.',
                                'events':['EVALUATION_ONLY_EVENT']},'unused.mp4')
        self.assertEqual(result['official'],1)
        for call in client.generate.call_args_list[1::2]:
            self.assertIn('EVALUATION_ONLY_EVENT',call.args[1])

    def test_dissonants_alignment_first_and_zero_evidence_eligible(self):
        rows=[{'reference':{'id':str(i)},'alignment':10 if i<10 else 5,'inspiration':10,
               'similarity':.9} for i in range(25)]
        rows += [{'reference':{'id':'zero'+str(i)},'alignment':0,'inspiration':0,'similarity':.36+i*.01} for i in range(5)]
        rows += [{'reference':{'id':'high_similarity'},'alignment':1,'inspiration':0,'similarity':.99}]
        chosen=partition(rows)['dissonant']
        self.assertEqual([r['reference']['id'] for r in chosen],['zero4','zero3','zero2','zero1','zero0'])

    def test_rd_negative_guidance_reaches_router(self):
        guidance={k:['Grounded test guidance'] for k in ('core_invariants','cinematic_patterns','modifiable_dimensions',
                  'strengthen_when','weaken_when','do_not_force_when','rhythm_and_cut_notes','application_notes')}
        guidance['batch_summary']='Test summary'
        class Client:
            def generate(self,system,request,**kwargs):
                self.request=request
                return guidance,{}
        client=Client()
        reference={'reference':{'id':'r','caption':'test'},'description':{'description':'test'},'judgments':{}}
        seed={'source_skill_id':'s','skill':{'shots':[{'description':'Preserved'}]}}
        result=evolve(client,seed,{'resonant':[reference],'dissonant':[reference]},'r_and_d')
        self.assertIn('negative',client.request)
        self.assertEqual(result['skill']['shots'],seed['skill']['shots'])
        with patch.object(client,'generate',return_value=({'selected_index':1},{})) as generate:
            route(client,{'prompt':'Test'},[result])
            self.assertEqual(generate.call_args.kwargs['temperature'],.1)
            self.assertEqual(generate.call_args.args[1]['candidate_skills'][0]['usage_guidance'],guidance)

    def test_normalization_temperature(self):
        with tempfile.TemporaryDirectory() as d:
            h=Handler(d,{})
            with patch.object(h.client,'generate',return_value=({'skill':{'shots':[{}]}},{})) as call:
                h({'kind':'normalize','skill':{'source_skill_id':'s','skill':{}}})
                self.assertEqual(call.call_args.kwargs['temperature'],.4)

    def test_all_baseline_methods_use_h3_adapter(self):
        for arm in ('raw','direct','vpo','prompt_a_video','mora','rapo'):
            with self.subTest(arm=arm),tempfile.TemporaryDirectory() as d:
                h=Handler(d,{'baselines':{arm:{}}})
                h.engines[arm]=lambda *args:{'pe_prompt':'before','image_path':'test.png'}
                job={'kind':'pe','row':{'prompt':'original','benchmark':'storyeval'},'arm':arm,
                     'skills':None,'budget':None,'development':False,'backbone':'h3','seed':1}
                with patch('skillpe.pipeline.pe.rewrite',return_value={'pe_prompt':'before'}),\
                     patch('skillpe.baselines.rapo',return_value={'pe_prompt':'before'}),\
                     patch('skillpe.pipeline.pe.adapt_h3',return_value=('adapted',{})) as adapter:
                    result=h(job)
                    adapter.assert_called_once_with(h.client,'before','original')
                    self.assertEqual(result['pe_prompt'],'adapted')
                    if arm=='mora':self.assertEqual(result['image_path'],'test.png')

    def test_dev_publish_requires_benchmark_exclusion(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):publish(d,{},[],[],catalog=[])
            with self.assertRaises(ValueError):publish(d,{},[{'prompt':'Repeated prompt'}],[],catalog=[],
                benchmark_rows=[{'benchmark':'storyeval','prompt':'repeated   prompt'},
                                {'benchmark':'vbench','prompt':'Different'}])

    def test_paper_audio_rubrics_and_no_numeric_example(self):
        for dim in DIMS:
            text=prompt('evaluation/'+dim)
            self.assertIn('"score": <your score>',text)
            self.assertNotIn('"score": 5',text)
        self.assertIn('audiovisual emphasis',prompt('evaluation/cinematic_quality'))
        self.assertIn('audio narration',prompt('evaluation/narrative_appeal'))
        self.assertIn('visual, audio, or temporal',prompt('evaluation/creativity'))

    def test_rapo_uses_llama_refactoring_and_resumes(self):
        class Client:
            model='gemini-3.1-pro-preview'
            def __init__(self):self.calls=[]
            def generate(self,system,request,**kwargs):
                self.calls.append(kwargs)
                return ({'choice':'A'} if kwargs.get('json_mode',True) else 'A complete rewritten prompt.'),{}
        client=Client()
        template=ast.parse('f"{x}"',mode='eval').body
        with tempfile.TemporaryDirectory() as d,\
             patch('skillpe.rapo.Retriever') as retriever,\
             patch('skillpe.baselines.llama_refactor',return_value=('Llama refined prompt.',{})) as refiner,\
             patch('skillpe.rapo.upstream_templates',return_value=(lambda node,values: str(values),template,template,'Refine {}')):
            retriever.return_value.return_value={'selected_modifiers':['light','movement']}
            result=rapo(client,{'repo':'unused','assets':'unused'},{'prompt':'Original'},d)
            self.assertEqual(len(client.calls),4)
            refiner.assert_called_once_with('unused/author/llama3_1_instruct_lora_rewrite',
                                          'Refine A complete rewritten prompt.')
            self.assertTrue(all(c['temperature']==0 for c in client.calls))
            self.assertTrue(result['pe_prompt'])
            rapo(client,{'repo':'unused','assets':'unused'},{'prompt':'Original'},d)
            refiner.assert_called_once()
            self.assertEqual(len(client.calls),5)

    def test_strict_media_contract(self):
        video={'codec_type':'video','width':1344,'height':768,'r_frame_rate':'24/1',
               'avg_frame_rate':'24/1','nb_read_frames':'240','duration':'10'}
        audio={'codec_type':'audio','sample_rate':'32000','channels':2,'channel_layout':'stereo','codec_name':'aac','duration':'10'}
        def run(v,a):
            with patch('skillpe.generation.subprocess.run',return_value=SimpleNamespace(stdout=json.dumps(
                {'streams':[v,a],'format':{'duration':'10'}}))):return probe('test.mp4')
        self.assertEqual(run(video,audio)['frames'],240)
        for field,value in [('width',1024),('r_frame_rate','30/1'),('nb_read_frames','239')]:
            with self.assertRaises(ValueError):run({**video,field:value},audio)
        for field,value in [('sample_rate','48000'),('channels',1),('codec_name','pcm_s16le')]:
            with self.assertRaises(ValueError):run(video,{**audio,field:value})


if __name__=='__main__':unittest.main()
