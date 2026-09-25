"""Learned baselines retain their own checkpoints; Mora/RAPO are adaptations."""
import importlib.util
import ast
import gc
import secrets
import sys
from pathlib import Path

from .io import freeze, prompt, read, write


class ExternalRewriter:
    def __init__(self, name, config):
        module_name, class_name = ('generate_vpo_prompt.py', 'VPOPromptGenerator') if name == 'vpo' else (
            'generate_prompt_a_video.py', 'PromptAVideoRefiner')
        repo = Path(config['repo'])
        sys.path.insert(0, str(repo))
        spec = importlib.util.spec_from_file_location('external_'+name, repo/module_name)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        self.engine = getattr(module, class_name).load(config['checkpoint'], dtype='bfloat16', device_map='auto')
        self.name = name

    def __call__(self, row):
        function = self.engine.generate if self.name == 'vpo' else self.engine.refine
        text = function(row['prompt'], max_new_tokens=512 if self.name == 'vpo' else 256,
                        temperature=0, top_p=.9 if self.name == 'vpo' else 1.)
        if not str(text).strip():
            raise ValueError('Empty baseline rewrite')
        return {'pe_prompt': str(text)}


class Mora:
    def __init__(self, config, client):
        import torch
        from diffusers import StableDiffusionXLPipeline
        self.client = client
        self.pipeline = StableDiffusionXLPipeline.from_pretrained(config['checkpoint'],
            torch_dtype=torch.float16, local_files_only=True).to('cuda')

    def __call__(self, row, seed, output):
        import torch
        value, audit = self.client.generate(prompt('mora'), {'original_prompt': row['prompt'],
            'duration_seconds': 10,
            'required_output_schema': {'first_frame_prompt':'Opening-frame image prompt',
                                       'i2v_prompt':'Complete video continuation prompt'}}, temperature=.25)
        if not value.get('first_frame_prompt') or not value.get('i2v_prompt'):
            raise ValueError('Invalid Mora plan')
        image = self.pipeline(value['first_frame_prompt'], width=1024, height=576, num_inference_steps=24,
            guidance_scale=7, generator=torch.Generator('cuda').manual_seed(seed)).images[0]
        image.save(output)
        return {'pe_prompt': value['i2v_prompt'], 'image_path': str(output), 'plan': value, 'audit': audit}


def llama_refactor(checkpoint, request):
    """Run the author-provided refactoring checkpoint with greedy BF16 inference."""
    import torch
    from transformers import AutoTokenizer, AutoModelForCausalLM
    tokenizer = AutoTokenizer.from_pretrained(checkpoint, local_files_only=True)
    model = AutoModelForCausalLM.from_pretrained(checkpoint, torch_dtype=torch.bfloat16,
        device_map='auto', local_files_only=True).eval()
    inputs = output = suffix = None
    try:
        text = tokenizer.apply_chat_template([
            {'role':'system','content':'You are a caption refiner.'},
            {'role':'user','content':request}], tokenize=False, add_generation_prompt=True)
        inputs = tokenizer(text, return_tensors='pt').to(model.device)
        with torch.inference_mode():
            output = model.generate(**inputs, max_new_tokens=512, do_sample=False,
                                    pad_token_id=tokenizer.eos_token_id)
        suffix = output[0, inputs.input_ids.shape[1]:]
        stops = model.generation_config.eos_token_id
        stops = stops if isinstance(stops, (list, tuple)) else [stops]
        if len(suffix) >= 512 and int(suffix[-1]) not in stops:
            raise ValueError('Truncated RAPO refactoring output')
        text = tokenizer.decode(suffix, skip_special_tokens=True).strip()
        if text.lower().startswith('final output:'):
            text = text[len('final output:'):].strip()
        if not text:
            raise ValueError('Empty RAPO refactoring output')
        return text, {'model':checkpoint, 'dtype':'bfloat16', 'do_sample':False,
                      'max_new_tokens':512, 'text':text}
    finally:
        del suffix, output, inputs, model
        gc.collect()
        torch.cuda.empty_cache()


def rapo(client, config, row, directory):
    """Gemini rewrite/selection and author Llama refactoring with graph retrieval."""
    from .rapo import Retriever, upstream_templates
    from .io import digest
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    assets = Path(config['assets'])
    refactor_checkpoint = str(assets/'author/llama3_1_instruct_lora_rewrite')
    render, merge, direct, refactor = upstream_templates(config['repo'])
    freeze(directory/'protocol.json', {
        'backend': 'gemini_rewrite_selection_llama_refactoring', 'model': client.model, 'temperature': 0,
        'refactoring': {'checkpoint':refactor_checkpoint, 'dtype':'bfloat16',
                        'do_sample':False, 'max_new_tokens':512},
        'original_prompt': row['prompt'], 'selector_sha256': digest(prompt('rapo_selector')),
        'merge_template': ast.dump(merge), 'direct_template': ast.dump(direct), 'refactor_template': refactor,
    })
    plan_path = directory/'randomization.json'
    if not plan_path.exists():
        freeze(plan_path, {'retrieval_seed': secrets.randbits(63),
               'order': ['refactored', 'direct'] if secrets.randbelow(2) else ['direct', 'refactored']})
    plan = read(plan_path)
    retrieved_path = directory/'retrieved.json'
    if not retrieved_path.exists():
        write(retrieved_path, Retriever(assets)({'original_prompt': row['prompt']}, plan['retrieval_seed']))
    def rewrite_text(request):
        text, audit = client.generate(
            'You rewrite text-to-video prompts. Follow the supplied rewrite instructions. Return only the rewritten prompt.',
            request, temperature=0, max_tokens=8192, json_mode=False)
        if not isinstance(text,str) or not text.strip():
            raise ValueError('Empty RAPO text output')
        return text.strip(), audit
    rewritten_path = directory/'rewritten.json'
    if not rewritten_path.exists():
        current, history = row['prompt'], []
        for modifier in read(retrieved_path)['selected_modifiers']:
            current, audit = rewrite_text(render(merge, {'current_description': current, 'modifier': modifier}))
            history.append({'modifier': modifier, 'output': current, 'audit': audit})
        other, audit = rewrite_text(render(direct, {'The_current_input': row['prompt']}))
        write(rewritten_path, {'merged': current, 'direct': other, 'history': history, 'direct_audit': audit})
    refined_path = directory/'refined.json'
    if not refined_path.exists():
        prior = read(rewritten_path)
        refined, audit = llama_refactor(refactor_checkpoint, refactor.format(prior['merged']))
        write(refined_path, {'refactored': refined, 'direct': prior['direct'], 'audit': audit})
    candidates = read(refined_path)
    value, audit = client.generate(prompt('rapo_selector'), {'original_prompt': row['prompt'],
        'A': candidates[plan['order'][0]], 'B': candidates[plan['order'][1]]}, temperature=0, max_tokens=32768)
    if value.get('choice') not in ('A', 'B'):
        raise ValueError('Invalid RAPO selector choice')
    selected = plan['order'][0 if value['choice'] == 'A' else 1]
    return {'pe_prompt': candidates[selected], 'selected_branch': selected, 'selection': value, 'audit': audit}
