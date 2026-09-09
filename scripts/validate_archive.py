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
    manifests = list((root / 'sources').glob('*/upstream/*/manifest.json'))
    if (root / 'notices/manifest.json').is_file():
        manifests.append(root / 'notices/manifest.json')
    for manifest in manifests:
        data = json.loads(manifest.read_text(encoding='utf-8'))
        for entry in data['files']:
            check(entry['path'], entry['sha256'])
        for entry in data.get('rendered_files', []) + data.get('media_files', []):
            check(entry['path'], entry['sha256'])
    templates_file = root / 'indexes/templates.json'
    if not templates_file.is_file():
        errors.append('Template index missing')
    else:
        data = json.loads(templates_file.read_text(encoding='utf-8'))
        aggregate = 'sources' in data
        sources = data.get('sources', [data])
        seen = set()
        for source in sources:
            source_id = source.get('source', '')
            if not source_id or source_id in seen or '/' in source_id or '\\' in source_id or '..' in source_id:
                errors.append('Invalid or duplicate template source')
                continue
            seen.add(source_id)
            if source.get('document'):
                check(source['document'])
            state_file = root / 'sources' / source_id / 'state.json'
            if not state_file.exists():
                errors.append('Template source state missing: ' + source_id)
            # A failed or case-only later sync must not invalidate preserved templates.
            if aggregate:
                sidecar = root / 'sources' / source_id / 'templates.json'
                if not sidecar.is_file():
                    errors.append('Template source sidecar missing: ' + source_id)
                else:
                    local = json.loads(sidecar.read_text(encoding='utf-8'))
                    selected = [row for row in data['templates'] if row.get('source') == source_id]
                    if local.get('source') != source_id or local.get('revision') != source.get('revision') or local.get('templates') != selected or len(selected) != source.get('count'):
                        errors.append('Template sidecar and aggregate disagree: ' + source_id)
        stable_ids = set()
        for template in data['templates']:
            if aggregate:
                stable_id = str(template.get('source')) + ':' + str(template.get('id'))
                if template.get('stable_id') != stable_id or stable_id in stable_ids or template.get('source') not in seen:
                    errors.append('Invalid or duplicate stable template ID: ' + stable_id)
                stable_ids.add(stable_id)
            document = template.get('document') or data.get('document')
            if document:
                check(document)
            else:
                errors.append('Template document missing: ' + str(template.get('id')))
            if template.get('cover'):
                check(template['cover'], template.get('cover_sha256'))
    from archive_image_views import check_images
    image_check = check_images(root)
    errors.extend('Broken image reference: ' + str(item) for item in image_check['errors'])
    return {'status': 'PASS' if not errors else 'FAIL', 'files_checked': files,
            'image_references_checked': image_check['count'], 'errors': errors}


if __name__ == '__main__':
    result = validate(Path(__file__).resolve().parents[1])
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(bool(result['errors']))
