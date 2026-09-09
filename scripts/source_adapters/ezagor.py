"""Bounded Ezagor README adapter: only explicitly selected, reviewed labels."""
from __future__ import annotations

import copy
import hashlib
import re
from pathlib import Path
from urllib.parse import urlsplit


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def prepare(source, selection, revision, get_file, get_url):
    if source.get('repository') != 'Ezagor-dev/awesome-midjourney-prompts':
        raise ValueError('Ezagor adapter repository mismatch')
    readme = Path(get_file('README.md'))
    license_file = Path(get_file('LICENSE'))
    if _sha(license_file.read_bytes()) != selection['approved_license_sha256']:
        raise ValueError('Ezagor license changed; review required')
    text = readme.read_text(encoding='utf-8')
    section = re.search(r'^## Midjourney Showcase\s*\n(.*?)(?=^## |\Z)', text, re.M | re.S)
    if not section:
        raise ValueError('Ezagor showcase section not found')
    pattern = re.compile(r'^\* ([^\n]+)\s*\n```\s*\n(/imagine prompt:[^\n]+)\n```\s*!\[[^\]]*\]\((https://[^)\s]+)\)', re.M)
    parsed = {}
    for match in pattern.finditer(section.group(1)):
        label, command, url = match.groups()
        if label in parsed:
            raise ValueError('Duplicate Ezagor label: ' + label)
        parsed[label] = (command, url)
    entries = selection.get('entries', [])
    if not entries or len({e['source_id'] for e in entries}) != len(entries):
        raise ValueError('Selection must contain unique explicit IDs')
    records = []
    for entry in entries:
        label = entry['label']
        if label not in parsed:
            raise ValueError('Selected Ezagor label missing or structure changed: ' + label)
        command, image_url = parsed[label]
        url = urlsplit(image_url)
        if url.scheme != 'https' or url.hostname != 'user-images.githubusercontent.com' or not url.path.startswith('/45847677/'):
            raise ValueError('Selected Ezagor image changed origin')
        assets, missing_assets = [], []
        image_hash = None
        try:
            image = Path(get_url(image_url))
            image_data = image.read_bytes()
            if not image_data.startswith(b'\x89PNG\r\n\x1a\n'):
                raise OSError('Selected Ezagor image is not a PNG')
            image_hash = _sha(image_data)
            assets.append({'path': str(image.resolve()), 'role': 'output', 'source_path': image_url})
        except OSError as exc:
            missing_assets.append({'source_path': image_url, 'role': 'output', 'reason': type(exc).__name__})
        prompt = command.removeprefix('/imagine prompt:')
        prompt_hash = _sha(prompt.encode('utf-8'))
        unchanged = (prompt_hash == entry['approved_prompt_sha256'] and
                     image_hash == entry['approved_asset_sha256'] and
                     image_url == entry['approved_image_url'])
        metadata = copy.deepcopy(entry['metadata'])
        if not unchanged:
            metadata['review_status'] = 'needs_review'
            metadata['review_note'] = '上游prompt、图片URL或图片内容已变更，旧人工核验不适用于本次版本。' + metadata.get('review_note', '')
        if missing_assets:
            metadata['review_note'] = '本次输出图获取失败，已保留原prompt并隔离该例，后续重试。'
            metadata['asset_roles'] = []
        metadata.setdefault('evidence', []).append({
            'kind': 'bounded-source-sync', 'revision': revision,
            'locator': {'path': 'README.md', 'section': 'Midjourney Showcase', 'label': label},
            'original_command': command, 'prompt_sha256': prompt_hash,
            'asset_sha256': image_hash, 'approved_content_unchanged': unchanged})
        record = {k: copy.deepcopy(entry[k]) for k in ('source_id', 'title', 'category', 'styles', 'scenes') if k in entry}
        record.update({'prompt': prompt, 'model': 'Midjourney', 'parameters': None,
                       'source_url': f"https://github.com/{source['repository']}/blob/{revision}/README.md#midjourney-showcase",
                       'upstream_url': image_url,
                       'assets': assets,
                       'missing_assets': missing_assets, 'metadata': metadata})
        records.append(record)
    return records, [readme, license_file]
