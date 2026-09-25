import argparse
from collections import defaultdict
import json
from pathlib import Path
from statistics import mean, stdev

from .io import DIMS, ROOT, digest, freeze, library, read, rows, write
from .queue import Queue
from .pipeline import Handler, bridge, protocol, publish, scored_rows
from .selection import select, random_library, subset_library


def main():
    parser = argparse.ArgumentParser(description='Portable SkillPE research pipeline')
    sub = parser.add_subparsers(dest='command', required=True)
    def command(name, options):
        p = sub.add_parser(name)
        for key, kwargs in options:
            p.add_argument('--'+key, **kwargs)
        return p
    required = {'required': True}
    command('normalize', [('root', required), ('skills', {'default':'artifacts/expert_authored'})])
    command('annotate', [('root',required), ('input',required)])
    command('collect', [('root',required), ('channel',required), ('output',required)])
    command('visual-summaries', [('skills',{'default':'artifacts/expert_authored'}), ('output',required)])
    command('retrieve', [('skills', {'default':'artifacts/expert_authored'}), ('references',required),
        ('checkpoint',required), ('output',required), ('summaries',required), ('device',{'default':'cuda'}),
        ('batch-size',{'type':int,'default':128})])
    command('reference-publish', [('input',required), ('root',required)])
    command('evolve', [('root',required), ('skills',{'default':'artifacts/normalized_seed'})])
    command('catalog', [('root',required), ('skills',{'default':'artifacts/normalized_seed'}), ('output',required)])
    command('development-set', [('input',required), ('skills',{'default':'artifacts/expert_authored'}),
        ('output',required), ('exclude',{'required':True,'help':'Complete JSONL benchmark prompt union; excluded by normalized text'})])
    command('publish', [('root',required), ('config',required), ('manifest',required),
        ('arms',{'nargs':'+','default':['raw','direct','vpo','prompt_a_video','mora','seed','top','top2','overall']}),
        ('libraries',{'help':'JSON object mapping arm names to library file/directory paths'}),
        ('catalog',{}), ('exclude',{'help':'Required with --catalog: complete StoryEval/VBench JSONL union'}),
        ('word-budget',{'type':int,'choices':[100,200,400,800]})])
    command('worker', [('root',required), ('config',required), ('channel',required),
        ('concurrency',{'type':int,'default':1}), ('attempts',{'type':int,'default':3}), ('watch',{'action':'store_true'})])
    command('bridge', [('root',required), ('config',required), ('watch',{'action':'store_true'}),
        ('no-score',{'action':'store_true'}), ('storyeval',{'action':'store_true'})])
    command('select', [('root',required), ('catalog',required), ('output',required)])
    command('summarize', [('root',required), ('output',required)])
    command('status', [('root',required)])
    command('ablation-libraries', [('catalog',required), ('selected',required), ('output',required)])
    args = parser.parse_args()
    if args.command == 'normalize':
        q = Queue(args.root, 'normalize')
        for skill in library(args.skills):
            q.publish({'kind':'normalize', 'skill':skill, 'protocol':protocol({})['prompts']})
    elif args.command == 'annotate':
        for row in rows(args.input):
            Queue(args.root,'annotate').publish({'kind':'annotate','row':row,'model':'Qwen/Qwen3.6-27B',
                                               'protocol':protocol({})['prompts']})
    elif args.command == 'collect':
        q=Queue(args.root,args.channel)
        freeze(args.output,[read(p) for p in sorted((q.root/'results').glob('*.json')) if not read(p).get('rejected')])
    elif args.command == 'visual-summaries':
        from .client import TextCompletion
        from .development import summarize_skill, SUMMARY_SYSTEM
        client=TextCompletion('DEEPSEEK','deepseek-v4-pro')
        skills=library(args.skills)
        directory=Path(str(args.output)+'.summaries')
        freeze(directory/'protocol.json',{'model':client.model,'skills':skills,'system':SUMMARY_SYSTEM})
        values={}
        for skill in skills:
            path=directory/(digest(skill)+'.json')
            if not path.exists():
                value,audit=summarize_skill(client,skill)
                freeze(path,{'summary':value,'audit':audit})
            values[skill['source_skill_id']]=read(path)['summary']
        freeze(args.output,values)
    elif args.command == 'retrieve':
        from .development import retrieve
        freeze(args.output, retrieve(library(args.skills), rows(args.references), args.checkpoint, args.device,
                                    batch_size=args.batch_size,summaries=read(args.summaries)))
    elif args.command == 'reference-publish':
        q = Queue(args.root, 'reference')
        for r in read(args.input):
            q.publish({**r, 'kind':'reference', 'protocol':protocol({})['prompts']})
    elif args.command == 'evolve':
        from .development import partition
        reference_q = Queue(args.root, 'reference')
        groups = defaultdict(list)
        for p in sorted((reference_q.root/'results').glob('*.json')):
            r = read(p)
            groups[r['source_skill_id']].append(r)
        for seed in library(args.skills):
            refs = partition(groups[seed['source_skill_id']])
            for role in ('r_and_d','bold','wilder','extreme'):
                Queue(args.root, 'evolve').publish({'kind':'evolve','seed_skill':seed,'references':refs,'role':role,
                                                   'protocol':protocol({})['prompts']})
    elif args.command == 'catalog':
        candidates = library(args.skills)
        q = Queue(args.root, 'evolve')
        candidates += [read(p) for p in sorted((q.root/'results').glob('*.json'))]
        families = defaultdict(set)
        for c in candidates:
            families[c['source_skill_id']].add(c['role'])
        if not all(roles == {'seed','r_and_d','bold','wilder','extreme'} for roles in families.values()):
            raise ValueError('Incomplete family: finish or explicitly repair failed evolution before selection')
        freeze(args.output, candidates)
    elif args.command == 'development-set':
        from .client import Gemini
        from .development import clean_prompt, reservoir, balanced_select
        from .pe import route
        candidates = rows(args.input) if args.input.endswith('.jsonl') else read(args.input)
        skills = library(args.skills)
        if len(skills) != 20 or len({s['source_skill_id'] for s in skills}) != 20 or any(s.get('role') != 'expert' for s in skills):
            raise ValueError('Development matching requires 20 original expert-authored skills')
        benchmark_rows=rows(args.exclude)
        if not benchmark_rows or {r['benchmark'] for r in benchmark_rows} != {'storyeval','vbench'}:
            raise ValueError('Supply nonempty StoryEval and VBench benchmark manifests together')
        excluded = {' '.join(r['prompt'].casefold().split()) for r in benchmark_rows}
        directory = Path(str(args.output)+'.routing')
        routing_protocol={'input_sha256':digest(candidates),'excluded':sorted(excluded),
            'skills':skills,'sample_size':13000,'per_stratum':650,'sampling_seed':20260714,
            'routing_temperature':.2,'routing_model':'gemini-3.1-pro-preview',
            'routing_prompt':protocol({})['prompts']['prompts/route.md'],'selection_seed':20260729}
        freeze(directory/'protocol.json',routing_protocol)
        routing_hash=digest(routing_protocol)
        eligible=[r for raw in candidates if (r := clean_prompt(raw)) is not None
                  and ' '.join(r['prompt'].casefold().split()) not in excluded]
        sample=reservoir(eligible)
        if len({r['prompt_id'] for r in sample}) != 13000:
            raise ValueError('Sample requires 13,000 unique prompt IDs')
        freeze(directory/'sample_13000.json',sample)
        groups=defaultdict(list)
        client = Gemini()
        # Route the entire frozen sample before any per-skill balancing.
        for r in sample:
            cache = directory/(digest([r,routing_hash])+'.json')
            if cache.exists():
                record = read(cache)
            else:
                selected, audit = route(client, r, skills, temperature=.2)
                record = {'source_skill_id':selected['source_skill_id'],'audit':audit}
                write(cache,record)
            sid = record['source_skill_id']
            groups[sid].append(r)
        chosen=[]
        for skill in sorted(skills,key=lambda s:s['source_skill_id']):
            sid=skill['source_skill_id']
            chosen.extend({**r,'benchmark':'development','source_skill_id':sid}
                          for r in balanced_select(groups[sid],20,sid))
        freeze(args.output, chosen)
    elif args.command == 'publish':
        libs = {k:library(v) for k,v in read(args.libraries).items()} if args.libraries else {}
        manifest = rows(args.manifest) if args.manifest.endswith('.jsonl') else read(args.manifest)
        print(publish(args.root,read(args.config),manifest,args.arms,libs,
                      read(args.catalog) if args.catalog else None,args.word_budget,
                      rows(args.exclude) if args.exclude else None))
    elif args.command == 'worker':
        config = read(args.config)
        frozen = Path(args.root)/'protocol.json'
        if frozen.exists() and read(frozen) != protocol(config):
            raise ValueError('Frozen run configuration or prompts changed')
        if args.channel in ('h3','ltx','vpo','prompt_a_video','mora','rapo') and args.concurrency != 1:
            raise ValueError('GPU/service worker concurrency must be one; start separate GPU processes')
        Queue(args.root,args.channel).worker(Handler(args.root,config),args.concurrency,args.attempts,args.watch)
    elif args.command == 'bridge':
        bridge(args.root,read(args.config),args.watch,not args.no_score,args.storyeval)
    elif args.command == 'select':
        catalog=read(args.catalog)
        manifest=read(Path(args.root)/'manifest.json')
        if len(catalog)!=100 or len({r['source_skill_id'] for r in catalog})!=20 or len(manifest)!=400:
            raise ValueError('Paper selection requires 20 families, 100 candidates and 400 development prompts')
        libs, report = select(catalog,scored_rows(args.root),manifest)
        for name, values in libs.items():
            freeze(Path(args.output)/(name+'.json'),values)
        write(Path(args.output)/'selection_report.json',report)
    elif args.command == 'status':
        print(json.dumps({p.name:Queue(args.root,p.name).status() for p in sorted((Path(args.root)/'queues').iterdir())},indent=2))
    elif args.command == 'summarize':
        groups = defaultdict(list)
        for row in scored_rows(args.root):
            groups[(row['backbone'],row['benchmark'],row['arm'])].append(row)
        table=[]
        for key, group in sorted(groups.items()):
            valid=[r for r in group if all(r['scores'].get(d) is not None for d in DIMS)]
            values={d:[r['scores'][d] for r in valid] for d in DIMS}
            values['overall']=[mean(r['scores'][d] for d in DIMS) for r in valid]
            table.append({'backbone':key[0],'benchmark':key[1],'arm':key[2],'n_valid':len(valid),
                'n_completed':len(group),'statistics':{d:{'mean':mean(v) if v else None,
                'std':stdev(v) if len(v)>1 else None} for d,v in values.items()}})
        write(args.output,table)
        print(json.dumps(table,indent=2))
    elif args.command == 'ablation-libraries':
        catalog=read(args.catalog)
        selected=read(Path(args.selected)/'overall.json')
        out=Path(args.output)
        for role in ('seed','r_and_d','bold','wilder','extreme'):
            freeze(out/'A'/(role+'.json'),[c for c in catalog if c['role']==role])
        freeze(out/'A/expert.json',library('artifacts/expert_authored'))
        for n in (4,8,12,16):
            for k in range(3):
                freeze(out/'C'/f'N{n}_K{k+1}.json',subset_library(selected,n,20260911+1000*n+k))
        freeze(out/'C/N20.json',selected)
        for k in range(3):
            freeze(out/'D'/f'random_K{k+1}.json',random_library(catalog,20260911+k))


if __name__ == '__main__':
    main()
