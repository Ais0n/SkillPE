"""Persistent model backends. GPU workers run one job at a time per process."""
import json
import os
import subprocess
import time
import urllib.request
from pathlib import Path
from fractions import Fraction

from .io import write


def probe(path):
    result = subprocess.run(['ffprobe', '-v', 'error', '-count_frames', '-show_streams', '-show_format',
                             '-of', 'json', str(path)], check=True, capture_output=True, text=True)
    value = json.loads(result.stdout)
    video = next(s for s in value['streams'] if s['codec_type'] == 'video')
    audio = next(s for s in value['streams'] if s['codec_type'] == 'audio')
    if (abs(float(value['format']['duration'])-10) > .2
        or (video['width'],video['height']) != (1344,768)
        or Fraction(video['r_frame_rate']) != 24 or Fraction(video['avg_frame_rate']) != 24
        or int(video['nb_read_frames']) != 240 or abs(float(video['duration'])-10) > .05
        or int(audio['sample_rate']) != 32000 or audio['channels'] != 2
        or audio.get('channel_layout') != 'stereo' or audio['codec_name'] != 'aac'
        or abs(float(audio['duration'])-10) > .2):
        raise ValueError('Output violates 1344x768 / 240 frames / 24 FPS / 10s / 32-kHz stereo AAC contract')
    return {'width': video['width'], 'height': video['height'], 'duration': value['format']['duration'],
            'fps': video['r_frame_rate'], 'frames':int(video['nb_read_frames']),
            'audio_sample_rate': audio['sample_rate'], 'audio_channels':audio['channels'],
            'audio_codec':audio['codec_name']}


class H3:
    def __init__(self, config):
        self.base = os.environ['H3_BASE_URL'].rstrip('/')
        self.model = config['model']
        self.timeout = config.get('job_timeout', 3600)

    def request(self, suffix, value=None):
        req = urllib.request.Request(self.base+suffix,
            data=json.dumps(value).encode() if value is not None else None,
            headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=120) as response:
            return json.load(response)

    def __call__(self, job, output):
        payload = {'model': self.model, 'prompt': job['pe_prompt'], 'seconds': 10,
            'task': 'fl2va' if job.get('image_path') else 't2va',
            'conditions': [{'type': 'image', 'uri': job['image_path'], 'role': 'keyframe', 'frame_index': 0}]
                          if job.get('image_path') else [],
            'target': {'short_edge': 768, 'aspect_ratio': '16:9', 'duration_seconds': 10.0},
            'num_outputs_per_prompt': 1, 'num_inference_steps': 50, 'flow_shift': 12.0,
            'audio_flow_shift': 3.0, 'quality': 'lossless', 'seed': job['seed']}
        identity = self.request('/v1/videos', payload)['id']
        deadline = time.monotonic()+self.timeout
        while time.monotonic() < deadline:
            state = self.request('/v1/videos/'+identity)
            if state['status'] == 'completed':
                break
            if state['status'] == 'failed':
                raise RuntimeError('H3 generation failed')
            time.sleep(3)
        else:
            raise TimeoutError('H3 generation timed out')
        output = Path(output)
        partial = output.with_suffix('.partial.mp4')
        with urllib.request.urlopen(self.base+'/v1/videos/'+identity+'/content', timeout=300) as response:
            with partial.open('wb') as handle:
                while block := response.read(1024*1024):
                    handle.write(block)
        media = probe(partial)
        partial.replace(output)
        return {'media': media, 'generation_parameters': payload}


class LTX:
    def __init__(self, config):
        import torch
        from ltx_core.model.video_vae.transformer import DiffVAEMode
        from ltx_pipelines.distilled import DistilledPipeline
        from ltx_pipelines.utils.model_paths import ModelPaths
        from ltx_pipelines.utils.types import OffloadMode
        from ltx_pipelines.utils.constants import DISTILLED_SIGMA_VALUES, STAGE_2_DISTILLED_SIGMA_VALUES
        if list(DISTILLED_SIGMA_VALUES) != config['distilled_sigmas'] or list(STAGE_2_DISTILLED_SIGMA_VALUES) != config['stage2_sigmas']:
            raise ValueError('LTX runtime uses a different distilled schedule')
        resident = torch.cuda.get_device_properties(0).total_memory > 80*1024**3
        if resident:
            from .residency import install
            install()
        w = config['weights']
        model = ModelPaths.from_split(transformer_path=w['transformer'], text_encoder_path=w['text_encoder'],
                                    video_vae_path=w['video_vae'], audio_vae_path=w['audio_vae'])
        options = {'prompt_enhancer_gemma_root': w['prompt_enhancer']} if config.get('enhance_prompt') else {}
        self.runner = DistilledPipeline(model_paths=model, spatial_upsampler_path=w['spatial_upsampler'],
            loras=[], quantization=None, offload_mode=OffloadMode.NONE if resident else OffloadMode.CPU,
            diffvae_optimization=DiffVAEMode.CHUNKED_EAGER, **options)
        self.enhance = config.get('enhance_prompt', False)

    def __call__(self, job, output):
        import torch
        from ltx_pipelines.utils import blocks
        from ltx_pipelines.utils.args import ImageConditioningInput
        from ltx_pipelines.utils.media_io import encode_video
        output = Path(output)
        raw = output.with_suffix('.raw.mp4')
        partial = output.with_suffix('.partial.mp4')
        images = [ImageConditioningInput(job['image_path'], 0, 1.0)] if job.get('image_path') else []
        original = blocks.generate_enhanced_prompt
        enhancements = []
        def capture(model, text, **kwargs):
            result = original(model, text, **kwargs)
            enhancements.append({'input': text, 'output': result, 'seed': kwargs.get('seed', 42)})
            return result
        if self.enhance:
            blocks.generate_enhanced_prompt = capture
        try:
            with torch.inference_mode():
                video, audio, n, _ = self.runner(prompt=job['pe_prompt'], seed=job['seed'], height=768,
                    width=1344, num_frames=241, frame_rate=24, images=images, enhance_prompt=self.enhance)
                if n != 241:
                    raise ValueError('Unexpected model frame count')
                def frames():
                    remaining = 240
                    for chunk in video:
                        if remaining == 0:
                            break
                        chunk = chunk[:remaining]
                        remaining -= len(chunk)
                        yield chunk
                    if remaining:
                        raise ValueError('Short decoded video')
                w = audio.waveform
                if w.ndim == 3 and w.shape[0] == 1:
                    w = w[0]
                if w.ndim != 2 or w.shape[0] != 2 or w.shape[1] < 10*audio.sampling_rate:
                    raise ValueError('Invalid audio')
                audio = type(audio)(waveform=w[:, :10*audio.sampling_rate], sampling_rate=audio.sampling_rate)
                encode_video(video=frames(), fps=24, audio=audio, output_path=str(raw),
                             video_chunks_number=16, crf=0, preset='veryfast', thread_count=2)
            subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', str(raw), '-map', '0:v:0', '-map', '0:a:0',
                '-c:v', 'copy', '-c:a', 'aac', '-ar', '32000', '-ac', '2', '-t', '10', str(partial)],
                check=True, timeout=180, capture_output=True)
            media = probe(partial)
            if self.enhance and len(enhancements) != 1:
                raise ValueError('Enhancement was not executed exactly once')
            partial.replace(output)
            return {'media': media, 'enhancement': enhancements}
        finally:
            blocks.generate_enhanced_prompt = original
            raw.unlink(missing_ok=True)
            partial.unlink(missing_ok=True)
