"""Immutable inputs, atomic outputs, and non-secret provenance."""
import contextlib
import fcntl
import hashlib
import json
import os
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIMS = ('prompt_fidelity', 'cinematic_quality', 'narrative_appeal', 'creativity')
ROLES = ('seed', 'r_and_d', 'bold', 'wilder', 'extreme')


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def rows(path):
    return [json.loads(line) for line in Path(path).read_text(encoding='utf-8').splitlines() if line.strip()]


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.' + secrets.token_hex(8) + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    os.replace(tmp, path)


def freeze(path, value):
    path = Path(path)
    with lock(path.with_suffix(path.suffix + '.lock'), blocking=True):
        if path.exists():
            if read(path) != value:
                raise ValueError('Frozen input changed; use a new output directory')
        else:
            write(path, value)


@contextlib.contextmanager
def lock(path, blocking=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def prompt(name):
    return (ROOT / 'prompts' / (name + '.md')).read_text(encoding='utf-8')


def library(path):
    p = Path(path)
    if p.is_file():
        return read(p)
    return [read(f) for f in sorted(p.glob('*.json'))]


def extract_json(text):
    decoder = json.JSONDecoder()
    for i, char in enumerate(text):
        if char != '{':
            continue
        try:
            obj, _ = decoder.raw_decode(text[i:])
            if isinstance(obj, dict):
                return obj
        except ValueError:
            pass
    raise ValueError('No JSON object in response')
