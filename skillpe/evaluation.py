"""Full audiovisual inline evaluation with independently retained dimension calls."""
import re
import subprocess
from pathlib import Path
from statistics import mean

from .io import DIMS, digest, freeze, prompt, read, write
from .storyeval import DESCRIPTION_PROMPT, scoring_prompt


def compress(source, destination):
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', str(source), '-t', '10',
        '-map', '0:v:0', '-map', '0:a:0', '-vf', 'scale=-2:720', '-r', '24',
        '-c:v', 'libx264', '-crf', '23', '-threads', '2', '-c:a', 'aac', '-b:a', '128k',
        '-ac', '2', str(destination)], check=True, timeout=300, capture_output=True)


def score(client, row, video, output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    source = Path(video)
    systems = {d: prompt('evaluation/'+d) for d in DIMS}
    freeze(output/'protocol.json', {'prompts': {d: digest(s) for d, s in systems.items()},
        'model': client.model, 'temperature': 0, 'max_tokens': 2048, 'original_prompt': row['prompt'],
        'video_size': source.stat().st_size, 'video_mtime_ns': source.stat().st_mtime_ns,
        'transport': 'full_av_inline_720p24_h264_crf23_aac128k'})
    proxy = output/'input.mp4'
    if not proxy.exists():
        temp = output/'input.partial.mp4'
        compress(source, temp)
        temp.replace(proxy)
    results = {}
    for d in DIMS:
        path = output/(d+'.json')
        if path.exists():
            results[d] = read(path)
            continue
        value, audit = client.generate(systems[d], 'Original user prompt:\n'+row['prompt'],
                                      temperature=0, max_tokens=2048, media=[proxy])
        if value.get('dimension') != d or type(value.get('valid')) is not bool:
            raise ValueError('Invalid scoring schema')
        if value['valid']:
            if type(value.get('score')) is not int or not 1 <= value['score'] <= 7:
                raise ValueError('Invalid score')
            if (not isinstance(value.get('evidence'), list) or not 1 <= len(value['evidence']) <= 4
                or not all(isinstance(s,str) and s.strip() for s in value['evidence'])):
                raise ValueError('Evidence missing')
            if value.get('confidence') not in ('high','medium','low'):
                raise ValueError('Invalid confidence')
        elif value.get('score') is not None or value.get('confidence') != 'low':
            raise ValueError('Invalid video requires null score and low confidence')
        results[d] = {'judgment': value, 'audit': audit}
        write(path, results[d])
    scores = {d: r['judgment']['score'] if r['judgment']['valid'] else None for d, r in results.items()}
    return {'scores': scores, 'details': results,
            'overall': mean(scores.values()) if all(v is not None for v in scores.values()) else None}


def storyeval(client, row, video):
    if not row.get('events'):
        raise ValueError('Official ordered event annotations are required')
    repeats = []
    for _ in range(3):
        description, first = client.generate('', DESCRIPTION_PROMPT, media=[video], json_mode=False)
        response, second = client.generate('', scoring_prompt(description, row['prompt'], row['events']),
                                           media=[video], json_mode=False)
        matches = re.findall(r'\[COMPLETE_LIST\]\s*:\s*([^\n\r]+)', response)
        values = [int(v) for v in re.findall(r'\b[01]\b', matches[-1])] if matches else []
        if len(values) != len(row['events']):
            raise ValueError('Event score length mismatch')
        repeats.append({'events': values, 'description': description, 'response': response,
                        'calls': [first, second]})
    return {'official': mean(mean(r['events']) for r in repeats), 'repeats': repeats}
