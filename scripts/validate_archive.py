"""Verify archived source documents and template resources without network access."""
import hashlib
import json
from pathlib import Path
import sys


def validate(root):
    root = Path(root).resolve()
    errors, files = [], 0
    def check(name, digest=None):
        nonlocal files
        path = (root / name).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            errors.append('Missing or unsafe archived file: ' + name)
            return
        files += 1
        if digest and hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            errors.append('Hash mismatch: ' + name)
    for manifest in (root / 'sources').glob('*/upstream/*/manifest.json'):
        data = json.loads(manifest.read_text(encoding='utf-8'))
        for entry in data['files']:
            check(entry['path'], entry['sha256'])
    templates_file = root / 'indexes/templates.json'
    if not templates_file.is_file():
        errors.append('Template index missing')
    else:
        data = json.loads(templates_file.read_text(encoding='utf-8'))
        check(data['document'])
        state_file = root / 'sources' / data['source'] / 'state.json'
        if state_file.exists():
            state = json.loads(state_file.read_text(encoding='utf-8'))
            if state.get('last_attempt_revision') != data['revision']:
                errors.append('Template index and last attempted source revision disagree')
        else:
            errors.append('Template source state missing')
        for template in data['templates']:
            if template.get('cover'):
                check(template['cover'], template.get('cover_sha256'))
    return {'status': 'PASS' if not errors else 'FAIL', 'files_checked': files, 'errors': errors}


if __name__ == '__main__':
    result = validate(Path(__file__).resolve().parents[1])
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(bool(result['errors']))
