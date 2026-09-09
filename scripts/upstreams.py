"""Source-policy dispatcher: isolated imports, read-only checks, explicit selections."""
import argparse
import hashlib
import importlib
import json
import shutil
import subprocess
from pathlib import Path
import re
import sys
import tempfile
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse

import library
import sync_upstream
import template_catalog
import validate_archive

ADAPTERS = {'jamez': 'source_adapters.jamez', 'pico': 'source_adapters.pico', 'ezagor': 'source_adapters.ezagor'}
POLICIES = {'approved_auto', 'candidate', 'manual_only', 'disabled'}
PUBLISH_DIRS = ('cases', 'images', 'indexes', 'sources', 'metadata', 'docs')


def read(path, default=None):
    return json.loads(path.read_text(encoding='utf-8')) if path.is_file() else default


def put(path, value):
    sync_upstream.put_json(path, value)


def registry(root):
    value = read(root / 'sources/registry.json')
    if not isinstance(value, dict) or value.get('schema_version') != 1:
        raise ValueError('A versioned sources/registry.json is required')
    rows = value.get('sources')
    if not isinstance(rows, list):
        raise ValueError('sources must be a list')
    seen = set()
    for row in rows:
        identifier = row.get('id', '')
        if not re.fullmatch(r'[a-z0-9][a-z0-9-]*', identifier) or identifier in seen:
            raise ValueError('Invalid or duplicate source id: ' + identifier)
        seen.add(identifier)
        if row.get('policy') not in POLICIES:
            raise ValueError('Unknown source policy: ' + identifier)
        if row['policy'] == 'approved_auto':
            if row.get('adapter') not in {'freestylefly', *ADAPTERS}:
                raise ValueError('Approved source has no implemented adapter: ' + identifier)
            if not re.fullmatch(r'[\w.-]+/[\w.-]+', row.get('repository', '')):
                raise ValueError('Invalid GitHub repository: ' + identifier)
            scope = row.get('scope', {})
            if scope.get('kind') not in {'all', 'selected'}:
                raise ValueError('Approved source scope is missing: ' + identifier)
            if scope['kind'] == 'selected' and not scope.get('ids'):
                raise ValueError('Selected scope must name IDs: ' + identifier)
            if scope['kind'] == 'selected':
                ids = scope['ids']
                if not isinstance(ids, list) or any(not isinstance(value, str) or not value for value in ids) or len(ids) != len(set(ids)):
                    raise ValueError('Selected IDs must be unique nonempty strings: ' + identifier)
    return rows


def scope_hash(source):
    value = {key: source.get(key) for key in ('repository', 'adapter', 'scope', 'selection_sha256')}
    if source.get('approved_documents'):
        value['approved_documents'] = source['approved_documents']
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def latest(source):
    branch = source.get('branch', 'main')
    try:
        with sync_upstream.fetch('https://api.github.com/repos/' + source['repository'] + '/commits/' + quote(branch, safe='')) as response:
            revision = json.load(response)['sha']
    except (OSError, URLError):
        result = subprocess.run(['git', 'ls-remote', '--exit-code', 'https://github.com/' + source['repository'] + '.git', 'refs/heads/' + branch],
                                capture_output=True, text=True, timeout=45, check=True)
        revision = result.stdout.split()[0]
    if not re.fullmatch(r'[a-f0-9]{40}', revision):
        raise ValueError('Invalid source commit')
    return revision


class Downloader:
    def __init__(self, source, revision, folder):
        self.source, self.revision, self.folder = source, revision, Path(folder)
        self.repository_folder = self.folder / 'repository'

    def download(self, url, path):
        if path.is_file():
            return path
        path.parent.mkdir(parents=True, exist_ok=True)
        with sync_upstream.fetch(url) as response:
            data = response.read(64 * 1024 * 1024 + 1)
        if len(data) > 64 * 1024 * 1024:
            raise ValueError('Source asset exceeds current archive download limit')
        path.write_bytes(data)
        return path

    def get_file(self, name):
        name = str(name)
        if '\\' in name or Path(name).is_absolute() or '..' in Path(name).parts:
            raise ValueError('Unsafe repository source path')
        path = (self.repository_folder / name).resolve()
        if not path.is_relative_to(self.repository_folder.resolve()):
            raise ValueError('Source path leaves download directory')
        return self.download('https://raw.githubusercontent.com/' + self.source['repository'] + '/' + self.revision + '/' + quote(name, safe='/'), path)

    def get_url(self, url):
        parsed = urlparse(url)
        allowed = {'user-images.githubusercontent.com', 'raw.githubusercontent.com', 'github.com', 'objects.githubusercontent.com', 'private-user-images.githubusercontent.com'}
        if parsed.scheme != 'https' or parsed.hostname not in allowed or parsed.username or parsed.password:
            raise ValueError('External asset host requires explicit adapter review')
        suffix = Path(parsed.path).suffix.lower()
        if suffix not in {'.png', '.jpg', '.jpeg', '.webp', '.gif', '.avif', '.bmp'}:
            suffix = '.img'
        path = self.folder / 'external' / (hashlib.sha256(url.encode()).hexdigest() + suffix)
        return self.download(url, path)


def archive_documents(root, source, revision, documents, downloader):
    target = root / 'sources' / source['id'] / 'upstream' / revision
    entries = []
    for document in sorted(set(Path(p).resolve() for p in documents)):
        relative = document.relative_to(downloader.repository_folder.resolve()).as_posix()
        destination = target / relative
        data = document.read_bytes()
        sync_upstream.put(destination, data)
        entries.append({'path': destination.relative_to(root).as_posix(), 'source_path': relative,
                        'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)})
    put(target / 'manifest.json', {'revision': revision, 'files': entries})
    return target


def validate_images(records):
    from PIL import Image
    for record in records:
        valid = []
        for asset in record.get('assets', []):
            try:
                with Image.open(asset['path']) as picture:
                    picture.verify()
                valid.append(asset)
            except (OSError, ValueError, Image.DecompressionBombError) as exc:
                record.setdefault('missing_assets', []).append({'asset': asset.get('source_path'), 'reason': type(exc).__name__})
        record['assets'] = valid


def prepare(stage, source, revision, download_dir, selected_ids=None):
    if source['adapter'] == 'freestylefly':
        upstream = sync_upstream.download_source(download_dir, revision)
        for name, expected in source.get('approved_documents', {}).items():
            document = (upstream / name).resolve()
            if not document.is_relative_to(upstream.resolve()) or not document.is_file():
                raise ValueError('Approved source notice missing')
            if hashlib.sha256(document.read_bytes()).hexdigest() != expected:
                raise ValueError('Source license or content notice changed; source review required: ' + name)
        records, _ = sync_upstream.prepare(upstream)
        wanted = set(selected_ids or (source['scope']['ids'] if source['scope']['kind'] == 'selected' else []))
        if selected_ids and source['scope']['kind'] == 'selected' and not wanted.issubset(set(source['scope']['ids'])):
            raise ValueError('Requested IDs leave approved source scope')
        if wanted:
            records = [r for r in records if str(r['source_id']) in wanted]
            if {str(r['source_id']) for r in records} != wanted:
                raise ValueError('Selected source IDs were not all found')
        sync_upstream.archive_documents(stage, upstream, revision, include_templates=not bool(wanted))
        return records
    module = importlib.import_module(ADAPTERS[source['adapter']])
    selection_path = stage / 'sources' / source['id'] / 'selection.json'
    selection = read(selection_path)
    if not selection:
        raise ValueError('Curated source selection manifest is missing')
    digest = hashlib.sha256(selection_path.read_bytes()).hexdigest()
    if source.get('selection_sha256') != digest:
        raise ValueError('Curated selection changed without updating approved scope fingerprint')
    wanted = set(selected_ids or source['scope']['ids'])
    if not wanted.issubset(set(source['scope']['ids'])):
        raise ValueError('Requested IDs leave approved source selection')
    downloader = Downloader(source, revision, download_dir)
    records, documents = module.prepare(source, selection, revision, downloader.get_file, downloader.get_url)
    records = [record for record in records if str(record['source_id']) in wanted]
    if {str(r['source_id']) for r in records} != wanted:
        raise ValueError('Adapter did not account for every selected ID')
    target = archive_documents(stage, source, revision, documents, downloader)
    for record in records:
        metadata = record.setdefault('metadata', {})
        if metadata.get('review_status') == 'needs_review':
            labels = {key: metadata.get(key, []) for key in ('artists', 'movements', 'materials', 'techniques')}
            metadata['previous_review_labels'] = labels
            for key in labels:
                metadata[key] = []
            metadata['review_note'] = metadata.get('review_note', '') + ' 当前内容尚待复核，旧细分类只保留为历史说明。'
        details = record.get('source_metadata', {})
        if details:
            metadata['source_details'] = details
        if record.get('attribution'):
            metadata['attribution'] = record['attribution']
        case = details.get('case', {})
        variants = {key: case[key] for key in ('prompt', 'prompt_en') if isinstance(case.get(key), str) and case[key]}
        if variants:
            record['prompt_variants'] = variants
        metadata['archived_source_manifest'] = (target / 'manifest.json').relative_to(stage).as_posix()
        original_category = record.get('category')
        metadata['original_category'] = original_category
        # Curated source labels are kept above; navigation retains the established 13 categories.
        text = str(original_category or '').casefold()
        if text not in {'ui 与界面', '图表与信息可视化', '海报与排版', '商品与电商', '品牌与标志', '建筑与空间', '摄影与写实', '插画与艺术', '人物与角色', '场景与叙事', '历史与古风题材', '文档与出版物', '其他应用场景'}:
            record['category'] = '摄影与写实' if any(word in text for word in ('photo', '摄影')) else '商品与电商' if any(word in text for word in ('product', '商品', '产品')) else '插画与艺术'
    return records


def copy_changes(stage, root):
    # No mirror deletion: old cases, snapshots, pending evidence and docs survive.
    changed = 0
    journal = []
    try:
        for name in (*PUBLISH_DIRS, 'README.md'):
            path = stage / name
            files = path.rglob('*') if path.is_dir() else [path]
            for file in files:
                if not file.is_file():
                    continue
                destination = root / file.relative_to(stage)
                data = file.read_bytes()
                old = destination.read_bytes() if destination.is_file() else None
                if old != data:
                    journal.append((destination, old))
                    sync_upstream.put(destination, data)
                    changed += 1
    except Exception:
        # Recover handled write failures. Remote publication still requires a complete Git commit.
        for destination, old in reversed(journal):
            if not destination.resolve().is_relative_to(root.resolve()):
                raise ValueError('Rollback path leaves library')
            if old is None:
                destination.unlink(missing_ok=True)
            else:
                sync_upstream.put(destination, old)
        raise
    return changed


def run(root, mode='sync', source_ids=None, selected_ids=None, revisions=None, resume=False):
    root = Path(root).resolve()
    rows = registry(root)
    if mode not in {'sync', 'check', 'import'}:
        raise ValueError('Unknown mode')
    if mode == 'import' and (not source_ids or not selected_ids):
        raise ValueError('import requires explicit --source and --ids')
    if selected_ids and len(source_ids or []) != 1:
        raise ValueError('Selected IDs must refer to exactly one source')
    wanted = set(source_ids or [r['id'] for r in rows if r['policy'] == 'approved_auto'])
    if wanted - {r['id'] for r in rows}:
        raise ValueError('Unknown source ID')
    results = []
    for source in rows:
        if source['id'] not in wanted:
            continue
        identifier = source['id']
        runtime_path = root / 'sources' / identifier / 'runtime.json'
        runtime = read(runtime_path, {})
        if source['policy'] == 'disabled' or (mode == 'sync' and source['policy'] != 'approved_auto'):
            results.append({'source': identifier, 'status': 'SKIPPED', 'reason': 'source policy'})
            continue
        if mode == 'sync' and runtime.get('paused') and not resume:
            results.append({'source': identifier, 'status': 'PAUSED', 'reason': runtime.get('error')})
            continue
        revision = None
        try:
            if source.get('adapter') not in {'freestylefly', *ADAPTERS}:
                raise ValueError('This candidate has no reviewed importer yet')
            revision = (revisions or {}).get(identifier) or latest(source)
            fingerprint = scope_hash(source)
            legacy = read(root / 'sources' / identifier / 'state.json', {})
            if (not runtime and source['adapter'] == 'freestylefly' and source['scope']['kind'] == 'all'
                    and mode in {'sync', 'check'} and not resume and not selected_ids
                    and legacy.get('last_success_revision') == revision and not legacy.get('pending_count')):
                valid, archived = library.validate(root), validate_archive.validate(root)
                if not valid['ok'] or archived['status'] != 'PASS':
                    raise ValueError('Existing archive failed migration validation')
                if mode == 'sync':
                    put(runtime_path, {'checked_revision': revision, 'applied_revision': revision,
                                       'scope_fingerprint': fingerprint, 'pending': 0, 'paused': False})
                results.append({'source': identifier, 'status': 'CHECKED' if mode == 'check' else 'UNCHANGED',
                                'revision': revision, 'migration': 'preserved existing full-source mapping'})
                continue
            if mode == 'sync' and not resume and runtime.get('applied_revision') == revision and runtime.get('scope_fingerprint') == fingerprint and not runtime.get('pending'):
                results.append({'source': identifier, 'status': 'UNCHANGED', 'revision': revision})
                continue
            with tempfile.TemporaryDirectory(prefix='vl-source-') as temp:
                base = Path(temp)
                stage = base / 'library'
                downloads = base / 'downloads'
                downloads.mkdir()
                with library.library_lock(root):
                    shutil.copytree(root, stage, ignore=shutil.ignore_patterns('.git', '.library.lock', '__pycache__', '.cache', '.sync-work'))
                    prior = read(stage / 'sources' / identifier / 'state.json', {}).get('mappings', {})
                    records = prepare(stage, source, revision, downloads, set(selected_ids) if selected_ids else None)
                    validate_images(records)
                    import_mode = 'snapshot' if source['scope']['kind'] == 'all' and not selected_ids else 'append'
                    outcome = library.import_records(stage, records, {'name': identifier, 'url': source['url']}, revision, mode=import_mode)
                    template_catalog.rebuild_templates(stage)
                    library.rebuild(stage)
                    valid, archived = library.validate(stage), validate_archive.validate(stage)
                    if not valid['ok'] or archived['status'] != 'PASS':
                        raise ValueError('Staged validation failed: ' + str(valid.get('errors', []) + archived.get('errors', []))[:1600])
                    pending = read(stage / 'sources' / identifier / 'state.json', {}).get('pending_count', 0)
                    changes = [r for r in outcome['records'] if r['status'] == 'pending' or prior.get(r['source_id'], {}).get('version') != r['version']]
                    result = {key: value for key, value in outcome.items() if key != 'records'}
                    result.update(source=identifier, revision=revision, status='PARTIAL' if pending else 'SYNCED', changes=changes)
                    if mode != 'check':
                        # A selected import does not advance the whole approved scope cursor.
                        if mode == 'sync':
                            next_runtime = {'checked_revision': revision, 'scope_fingerprint': fingerprint,
                                            'pending': pending, 'paused': False,
                                            'applied_revision': runtime.get('applied_revision') if pending else revision}
                            put(stage / 'sources' / identifier / 'runtime.json', next_runtime)
                        result['changed_files'] = copy_changes(stage, root)
                    else:
                        result['status'] = 'CHECKED'
                        result['would_be_partial'] = bool(pending)
                    results.append(result)
        except Exception as exc:
            transient = isinstance(exc, (OSError, URLError, TimeoutError, subprocess.SubprocessError))
            error = type(exc).__name__ + ': ' + str(exc)
            status = 'CHECK_FAILED' if mode == 'check' else 'FAILED' if transient else 'PAUSED'
            result = {'source': identifier, 'status': status, 'revision': revision, 'error': error[:2000]}
            if mode == 'check':
                result['suggested_pause'] = not transient
            results.append(result)
            if mode == 'sync':
                runtime.update(paused=not transient, error=error[:2000])
                if revision:
                    runtime['checked_revision'] = revision
                put(runtime_path, runtime)
    failures = sum(r['status'] in {'FAILED', 'PAUSED', 'CHECK_FAILED'} for r in results)
    successes = sum(r['status'] in {'SYNCED', 'UNCHANGED', 'PARTIAL', 'CHECKED'} for r in results)
    status = 'FAILED' if failures and not successes else 'PARTIAL' if failures or any(r.get('pending') for r in results) else 'OK'
    return {'status': status, 'mode': mode, 'sources': results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('sync', 'check', 'import'))
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--source', action='append', dest='source_ids')
    parser.add_argument('--ids', nargs='+')
    parser.add_argument('--revision', help='Pinned commit for exactly one explicit source')
    parser.add_argument('--resume', action='store_true', help='After fixing an adapter, retry runtime-paused approved sources')
    args = parser.parse_args()
    if args.revision and len(args.source_ids or []) != 1:
        parser.error('--revision requires one --source')
    if args.revision and not re.fullmatch(r'[a-f0-9]{40}', args.revision):
        parser.error('revision must be a complete commit')
    result = run(args.root, args.mode, args.source_ids, args.ids,
                 {args.source_ids[0]: args.revision} if args.revision else None, args.resume)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result['status'] == 'FAILED' else 0


if __name__ == '__main__':
    raise SystemExit(main())
