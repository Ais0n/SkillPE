"""Gemini generateContent transport. Credentials come only from the environment."""
import base64
import json
import mimetypes
import os
import time
from pathlib import Path
import urllib.error
import urllib.request

from .io import extract_json


class Gemini:
    def __init__(self, model='gemini-3.1-pro-preview', attempts=3, timeout=240):
        self.model, self.attempts, self.timeout = model, attempts, timeout

    def generate(self, system, request, temperature=0, max_tokens=8192, media=None, json_mode=True):
        endpoint = os.environ['GEMINI_API_ENDPOINT'].format(model=self.model)
        key = os.environ['GEMINI_API_KEY']
        parts = []
        for path in media or []:
            path = Path(path)
            mime = mimetypes.guess_type(path.name)[0] or 'application/octet-stream'
            parts.append({'inlineData': {'mimeType': mime, 'data': base64.b64encode(path.read_bytes()).decode('ascii')}})
        parts.append({'text': request if isinstance(request, str) else json.dumps(request, ensure_ascii=False)})
        config = {'temperature': temperature, 'maxOutputTokens': max_tokens}
        if json_mode:
            config['responseMimeType'] = 'application/json'
        payload = {'systemInstruction': {'parts': [{'text': system}]},
                   'contents': [{'role': 'user', 'parts': parts}], 'generationConfig': config}
        headers = {'Content-Type': 'application/json'}
        if os.environ.get('GEMINI_AUTH_MODE', 'api-key') == 'bearer':
            headers['Authorization'] = 'Bearer ' + key
        else:
            headers['x-goog-api-key'] = key
        started = time.monotonic()
        last_type = 'unknown'
        for attempt in range(self.attempts):
            try:
                req = urllib.request.Request(endpoint, data=json.dumps(payload).encode(), headers=headers, method='POST')
                with urllib.request.urlopen(req, timeout=self.timeout) as response:
                    raw = json.load(response)
                text = ''.join(p.get('text', '') for c in raw.get('candidates', [])
                               for p in c.get('content', {}).get('parts', []) if not p.get('thought', False)).strip()
                if not text:
                    raise ValueError('Empty or blocked completion')
                value = extract_json(text) if json_mode else text
                return value, {'text': text, 'usage': raw.get('usageMetadata', {}),
                               'seconds': time.monotonic()-started, 'attempts': attempt+1,
                               'model': self.model, 'temperature': temperature}
            except (urllib.error.URLError, OSError, ValueError, KeyError) as exc:
                # Never serialize HTTP bodies, authorization headers, or endpoint URLs.
                last_type = type(exc).__name__
                if attempt+1 < self.attempts:
                    time.sleep(min(5 * 2**attempt, 30))
        raise RuntimeError('Generation failed: ' + last_type)


class TextCompletion:
    """Text-only chat-completions transport for separately hosted paper models.

    The endpoint is the full chat-completions URL, not a base URL. There is no
    fallback to Gemini if the requested backend is unavailable.
    """
    def __init__(self, provider, model, attempts=3, timeout=240):
        if provider not in ('DEEPSEEK', 'QWEN'):
            raise ValueError('Unsupported text provider')
        self.provider, self.model = provider, model
        self.attempts, self.timeout = attempts, timeout

    def generate(self, system, request, temperature=0, max_tokens=2048, json_mode=True):
        endpoint = os.environ[self.provider+'_API_ENDPOINT']
        key = os.environ.get(self.provider+'_API_KEY', '')
        headers = {'Content-Type': 'application/json'}
        if key:
            headers['Authorization'] = 'Bearer '+key
        payload = {'model': self.model, 'temperature': temperature, 'max_tokens': max_tokens,
            'messages': [{'role': 'system', 'content': system}, {'role': 'user', 'content':
                request if isinstance(request,str) else json.dumps(request,ensure_ascii=False)}]}
        if json_mode:
            payload['response_format'] = {'type': 'json_object'}
        started = time.monotonic()
        last_type = 'unknown'
        for attempt in range(self.attempts):
            try:
                req = urllib.request.Request(endpoint, data=json.dumps(payload).encode(),
                                             headers=headers, method='POST')
                with urllib.request.urlopen(req, timeout=self.timeout) as response:
                    raw = json.load(response)
                text = raw['choices'][0]['message']['content'].strip()
                if not text:
                    raise ValueError('Empty completion')
                return (extract_json(text) if json_mode else text), {
                    'text': text, 'usage': raw.get('usage', {}), 'model': self.model,
                    'temperature': temperature, 'attempts': attempt+1,
                    'seconds': time.monotonic()-started}
            except (urllib.error.URLError, OSError, ValueError, KeyError, IndexError, TypeError, AttributeError) as exc:
                last_type = type(exc).__name__
                if attempt+1 < self.attempts:
                    time.sleep(min(5*2**attempt,30))
        raise RuntimeError('Text generation failed: '+last_type)
