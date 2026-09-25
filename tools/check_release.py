"""Offline release lint. Prints relative filenames only, never matched secrets."""
import ast
import hashlib
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]


def check():
    errors = []
    hashes = {}
    patterns = {
        'absolute_local_path': re.compile(r'(?<![\w])/(?:home|root|tmp|mnt|media|workspace|data|opt|srv|Users)/'),
        'private_network': re.compile(r'\b(?:10\.\d+\.\d+\.\d+|192\.168\.\d+\.\d+|172\.(?:1[6-9]|2\d|3[01])\.\d+\.\d+)\b'),
        'secret_token': re.compile(r'\b(?:sk[-_]|hf_|AIza)[A-Za-z0-9_-]{20,}\b'),
        'private_key': re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
    }
    for path in sorted(ROOT.rglob('*')):
        relative = str(path.relative_to(ROOT))
        if path.is_symlink():
            errors.append((relative,'symlink'))
            continue
        if not path.is_file():
            continue
        if '__pycache__' in path.parts or path.suffix == '.pyc':
            errors.append((relative,'generated_bytecode'))
            continue
        raw=path.read_bytes()
        try:
            text=raw.decode('utf-8')
        except UnicodeDecodeError:
            errors.append((relative,'binary_file'))
            continue
        for name, pattern in patterns.items():
            if pattern.search(text):
                errors.append((relative,name))
        if path.suffix == '.py':
            ast.parse(text,filename=relative)
        if path.suffix == '.json':
            json.loads(text)
        hashes[relative]=hashlib.sha256(raw).hexdigest()
    artifact_files=sorted((ROOT/'artifacts').rglob('*'))
    files=[p for p in artifact_files if p.is_file()]
    if len(files)!=40:
        errors.append(('artifacts','expected_exactly_40_files'))
    for p in files:
        if p.parent.name not in ('expert_authored','normalized_seed') or p.suffix!='.json':
            errors.append((str(p.relative_to(ROOT)),'artifact_not_allowed'))
    print(json.dumps({'files_checked':len(hashes),'skill_artifacts':len(files),'errors':errors},indent=2))
    return bool(errors)


if __name__ == '__main__':
    sys.exit(check())
