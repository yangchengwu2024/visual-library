"""Import a pinned upstream archive without executing upstream code."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import uuid
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen
import zipfile

from library import import_records, library_lock

UPSTREAM = 'freestylefly/awesome-gpt-image-2'
SOURCE = 'awesome-gpt-image-2'


def fetch(url):
    headers = {'User-Agent': 'personal-visual-library/1.0', 'Accept': 'application/vnd.github+json'}
    token = os.environ.get('GITHUB_TOKEN')
    if token and urlparse(url).netloc == 'api.github.com':
        headers['Authorization'] = 'Bearer ' + token
    return urlopen(Request(url, headers=headers), timeout=60)


def latest_revision():
    with fetch('https://api.github.com/repos/' + UPSTREAM + '/commits/main') as response:
        revision = json.load(response)['sha']
    if not re.fullmatch(r'[a-f0-9]{40}', revision):
        raise ValueError('Invalid upstream revision')
    return revision


def download_source(target, revision):
    archive = target / 'source.zip'
    with fetch('https://codeload.github.com/' + UPSTREAM + '/zip/' + revision) as response, archive.open('wb') as out:
        shutil.copyfileobj(response, out)
    unpacked = (target / 'source').resolve()
    unpacked.mkdir()
    with zipfile.ZipFile(archive) as z:
        for entry in z.infolist():
            parts = Path(entry.filename).parts
            if len(parts) < 2:
                continue
            dest = unpacked.joinpath(*parts[1:]).resolve()
            if not dest.is_relative_to(unpacked):
                raise ValueError('Unsafe archive path')
            if entry.is_dir():
                dest.mkdir(parents=True, exist_ok=True)
            else:
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(z.read(entry))
    return unpacked


def put(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    data = content if isinstance(content, bytes) else content.encode('utf-8')
    if not path.exists() or path.read_bytes() != data:
        temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
        temp.write_bytes(data)
        temp.replace(path)


def put_json(path, data):
    put(path, json.dumps(data, ensure_ascii=False, indent=2) + '\n')


def prepare(source_dir):
    source_dir = source_dir.resolve()
    payload = json.loads((source_dir / 'data/cases.json').read_text(encoding='utf-8'))
    source_cases = payload['cases']
    if not source_cases or len({item['id'] for item in source_cases}) != len(source_cases):
        raise ValueError('Empty or duplicate upstream case list')
    blocks = {}
    for part in (1, 2):
        name = f'docs/gallery-part-{part}.md'
        text = (source_dir / name).read_text(encoding='utf-8')
        chunks = re.split(r'<a name="case-(\d+)"></a>', text)
        for index in range(1, len(chunks), 2):
            blocks[int(chunks[index])] = (name, chunks[index + 1])
    if set(blocks) != {item['id'] for item in source_cases}:
        raise ValueError('Gallery and case index disagree; refusing incomplete sync')
    records = []
    for item in source_cases:
        document, block = blocks[item['id']]
        refs = re.findall(r'!\[(?:\\.|[^\]])*\]\(([^)]+)\)', block, re.S)
        assets, missing, seen = [], [], set()
        if not refs:
            refs = ['../data' + item['image']]
        for ref in refs:
            raw_ref = unquote(ref.strip())
            if urlparse(raw_ref).scheme or raw_ref.startswith('//'):
                missing.append('Unarchived external image: ' + raw_ref)
                continue
            path = (source_dir / document).parent.joinpath(raw_ref).resolve()
            if not path.is_relative_to(source_dir) or not path.is_file():
                missing.append(raw_ref)
                continue
            rel = path.relative_to(source_dir).as_posix()
            if rel not in seen:
                seen.add(rel)
                assets.append({'path': str(path), 'role': 'source_example', 'source_path': rel})
        records.append({'source_id': str(item['id']), 'title': item['title'], 'prompt': item['prompt'],
                        'category': item.get('category'), 'styles': item.get('styles', []),
                        'scenes': item.get('scenes', []), 'source_url': item.get('sourceUrl') or item['githubUrl'],
                        'upstream_url': item['githubUrl'], 'model': None, 'parameters': None,
                        'assets': assets, 'missing_assets': missing})
    return records, payload


def archive_documents(root, source_dir, revision):
    target = root / 'sources' / SOURCE / 'upstream' / revision
    paths = ['LICENSE', 'docs/disclaimer.md', 'data/cases.json', 'data/style-library.json',
             'docs/gallery-part-1.md', 'docs/gallery-part-2.md', 'docs/templates.md']
    manifest = []
    for name in paths:
        data = (source_dir / name).read_bytes()
        put(target / name, data)
        manifest.append({'path': (target / name).relative_to(root).as_posix(),
                         'source_path': name, 'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)})
    put_json(target / 'manifest.json', {'revision': revision, 'files': manifest})
    # Provide a small template index, with source-local full text and all example case IDs.
    taxonomy = json.loads((source_dir / 'data/style-library.json').read_text(encoding='utf-8'))
    templates = []
    for template in taxonomy.get('templates', []):
        entry = dict(template)
        entry['source_cover'] = entry.get('cover')
        cover = entry.get('cover')
        if cover:
            file = (source_dir / ('data' + cover)).resolve()
            if not file.is_relative_to(source_dir.resolve()) or not file.is_file():
                raise ValueError('Template cover is missing: ' + str(cover))
            content = file.read_bytes()
            digest = hashlib.sha256(content).hexdigest()
            existing = list((root / 'images').glob(digest + '.*'))
            saved = existing[0] if existing else root / 'images' / (digest + file.suffix.lower())
            put(saved, content)
            entry['cover'] = saved.relative_to(root).as_posix()
            entry['cover_sha256'] = digest
        entry['document'] = (target / 'docs/templates.md').relative_to(root).as_posix()
        templates.append(entry)
    put_json(root / 'indexes/templates.json', {'source': SOURCE, 'revision': revision,
        'document': (target / 'docs/templates.md').relative_to(root).as_posix(), 'templates': templates})


def sync(root, source_dir=None, revision=None, force=False):
    root = Path(root).resolve()
    revision = revision or latest_revision()
    if not re.fullmatch(r'[a-f0-9]{40}', revision):
        raise ValueError('Use a pinned 40-character source commit')
    state_file = root / 'sources' / SOURCE / 'state.json'
    if state_file.exists() and not force:
        state = json.loads(state_file.read_text(encoding='utf-8'))
        if state.get('last_success_revision') == revision:
            return {'status': 'UNCHANGED', 'revision': revision}
    if source_dir is None:
        with tempfile.TemporaryDirectory(prefix='visual-library-source-') as directory:
            source_dir = download_source(Path(directory), revision)
            return sync(root, source_dir, revision, force=True)
    source_dir = Path(source_dir)
    records, payload = prepare(source_dir)
    with library_lock(root):
        archive_documents(root, source_dir, revision)
        result = import_records(root, records, {'name': SOURCE, 'url': 'https://github.com/' + UPSTREAM}, revision, lock=False)
    return dict(result, status='PARTIAL' if result.get('pending') else 'SYNCED', revision=revision, upstream_cases=len(payload['cases']))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--source-dir', type=Path)
    parser.add_argument('--revision')
    parser.add_argument('--force', action='store_true')
    args = parser.parse_args()
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    try:
        result = sync(args.root, args.source_dir, args.revision, args.force)
        print(json.dumps({key: value for key, value in result.items() if key != "records"}, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({'status': 'FAILED', 'error': str(exc)}, ensure_ascii=False))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
