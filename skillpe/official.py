"""Export current outputs and run the separately installed official VBench code."""
import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

from .io import read, write
from .queue import Queue
from .vbench_weights import TASK_INFO, DIM_WEIGHT, NORMALIZE_DIC, QUALITY_LIST, SEMANTIC_LIST


def normalized_total(raw):
    values = {key.replace('_',' '): value for key,value in raw.items()}
    if set(values) != set(TASK_INFO):
        raise ValueError('All 16 official dimensions required; no partial total')
    weighted = {k: (values[k]-NORMALIZE_DIC[k]['Min']) /
                (NORMALIZE_DIC[k]['Max']-NORMALIZE_DIC[k]['Min'])*DIM_WEIGHT[k] for k in TASK_INFO}
    quality = sum(weighted[k] for k in QUALITY_LIST)/sum(DIM_WEIGHT[k] for k in QUALITY_LIST)
    semantic = sum(weighted[k] for k in SEMANTIC_LIST)/sum(DIM_WEIGHT[k] for k in SEMANTIC_LIST)
    return {'quality': quality, 'semantic': semantic, 'total': .8*quality+.2*semantic}


def export(root, backbone, arm, output, full_info):
    q = Queue(root,backbone)
    out = Path(output)
    videos = out/'videos'
    videos.mkdir(parents=True,exist_ok=True)
    lookup = {r['prompt']:r for r in read(full_info)}
    matched = {}
    for path in sorted((q.root/'results').glob('*.json')):
        job,result = read(q.root/'jobs'/path.name),read(path)
        if job['arm'] != arm or job['row']['benchmark'] != 'vbench':
            continue
        text = job['row']['prompt']
        if text not in lookup:
            raise ValueError('Prompt absent from official annotation file')
        name = text+'-0.mp4'
        if '/' in name or '\\' in name or len(name.encode()) > 255:
            raise ValueError('Official filename is not portable; resolve in the official exporter')
        if text in matched:
            raise ValueError('Multiple seeds per prompt; export separate runs')
        target = videos/name
        if not target.exists():
            try:
                os.link(result['video'],target)
            except OSError:
                shutil.copyfile(result['video'],target)
        matched[text] = lookup[text]
    if not matched:
        raise ValueError('No completed VBench videos')
    write(out/'full_info.json',list(matched.values()))
    return out


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--root',required=True)
    p.add_argument('--backbone',choices=['h3','ltx'],required=True)
    p.add_argument('--arm',required=True)
    p.add_argument('--output',required=True)
    p.add_argument('--full-info',required=True)
    p.add_argument('--repo',required=True)
    p.add_argument('--dimension',nargs='+',default=[x.replace(' ','_') for x in TASK_INFO])
    a=p.parse_args()
    out=export(a.root,a.backbone,a.arm,a.output,a.full_info).resolve()
    for dimension in a.dimension:
        subprocess.run([sys.executable,'evaluate.py','--videos_path',str(out/'videos'),
            '--full_json_dir',str(out/'full_info.json'),'--dimension',dimension,
            '--output_path',str(out/'scores'/dimension),'--load_ckpt_from_local','True'],
            cwd=a.repo,check=True)


if __name__ == '__main__':
    main()
