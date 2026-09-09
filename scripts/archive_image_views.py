"""Render archived documents with local images while retaining byte-exact sources."""
import hashlib
import html
import json
import os
import re
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

SUFFIXES = {'.md', '.markdown', '.html', '.htm'}


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _inside(root, path):
    path = Path(path).resolve()
    if not path.is_relative_to(root):
        raise ValueError(f'Unsafe path outside library: {path}')
    return path


def _mask(text):
    """Keep offsets stable, hiding fenced/inline code and HTML comments."""
    chars = list(text)
    fence = None
    offset = 0
    for line in text.splitlines(True):
        match = re.match(r'^ {0,3}(`{3,}|~{3,})(.*)$', line.rstrip('\r\n'))
        hidden = fence is not None
        if match:
            mark, tail = match.groups()
            if fence is None:
                fence = mark
                hidden = True
            elif mark[0] == fence[0] and len(mark) >= len(fence) and not tail.strip():
                fence = None
        if hidden:
            chars[offset:offset + len(line)] = ['\n' if c == '\n' else ' ' for c in line]
        offset += len(line)
    masked = ''.join(chars)
    for match in re.finditer(r'<!--.*?-->|(`+)(?!`)(.*?)(?<!`)\1(?!`)', masked, re.S):
        chars[match.start():match.end()] = ['\n' if c == '\n' else ' ' for c in match.group()]
    return ''.join(chars)


def _closing(text, start, opening, closing):
    depth = 1
    i = start + 1
    while i < len(text):
        if text[i] == '\\':
            i += 2
            continue
        if text[i] == opening:
            depth += 1
        elif text[i] == closing:
            depth -= 1
            if not depth:
                return i
        i += 1
    raise ValueError(f'Unclosed image syntax at offset {start}')


def image_references(text):
    """Return start/end URL offsets, decoded url and kind for real images.

    Reference definitions can occur more than once in this list when reused.
    Unsupported or incomplete image syntax raises instead of disappearing.
    """
    masked = _mask(text)
    definitions = {}
    for match in re.finditer(r'^ {0,3}\[([^\]\n]+)\]:\s*(?:<([^>\n]+)>|([^\s]+))', masked, re.M):
        group = 2 if match.group(2) is not None else 3
        definitions.setdefault(' '.join(match.group(1).lower().split()),
                               (match.start(group), match.end(group), html.unescape(match.group(group))))
    refs = []
    for match in re.finditer(r'(?<!\\)!\[', masked):
        close = _closing(masked, match.start() + 1, '[', ']')
        pos = close + 1
        if masked[pos:pos + 1] == '(':
            end = _closing(masked, pos, '(', ')')
            start = pos + 1
            while start < end and masked[start].isspace():
                start += 1
            if masked[start:start + 1] == '<':
                finish = masked.find('>', start + 1, end)
                if finish < 0:
                    raise ValueError(f'Unclosed image destination at offset {start}')
                start += 1
                tail = masked[finish + 1:end].strip()
            else:
                finish = start
                depth = 0
                while finish < end:
                    c = masked[finish]
                    if c == '\\':
                        finish += 2
                        continue
                    if c.isspace() and depth == 0:
                        break
                    if c == '(':
                        depth += 1
                    elif c == ')':
                        depth -= 1
                    finish += 1
                tail = masked[finish:end].strip()
            if tail and not re.fullmatch(r'".*"|\'.*\'|\(.*\)', tail, re.S):
                raise ValueError(f'Unsupported image title/destination at offset {start}')
            if finish <= start:
                raise ValueError(f'Empty image destination at offset {start}')
            url = re.sub(r'\\([!"#$%&\'()*+,\-./:;<=>?@\[\\\]^_`{|}~])', r'\1', text[start:finish])
            refs.append({'start': start, 'end': finish, 'url': html.unescape(url), 'kind': 'markdown'})
        else:
            label = masked[match.start() + 2:close]
            if masked[pos:pos + 1] == '[':
                end = _closing(masked, pos, '[', ']')
                label = masked[pos + 1:end] or label
            key = ' '.join(label.lower().split())
            if key not in definitions:
                raise ValueError(f'Unresolved image reference {label!r}')
            start, end, url = definitions[key]
            refs.append({'start': start, 'end': end, 'url': url, 'kind': 'reference'})
    picture_depth = 0
    for match in re.finditer(r'<(/?)(picture|img|source)\b[^>]*>', masked, re.I):
        closing, tag = match.group(1), match.group(2).lower()
        if tag == 'picture':
            picture_depth = max(0, picture_depth + (-1 if closing else 1))
            continue
        if closing or (tag == 'source' and not picture_depth):
            continue
        attributes = {}
        for attr in re.finditer(r'\s(src|srcset)\s*=\s*(?:"([^"]*)"|\'([^\']*)\'|([^\s>]+))', match.group(), re.I):
            group = next(i for i in (2, 3, 4) if attr.group(i) is not None)
            attributes.setdefault(attr.group(1).lower(),
                                  (match.start() + attr.start(group), attr.group(group)))
        if tag == 'img' and not attributes:
            raise ValueError(f'Image tag missing src/srcset at offset {match.start()}')
        if tag == 'img' and 'src' in attributes:
            start, value = attributes['src']
            refs.append({'start': start, 'end': start + len(value),
                         'url': html.unescape(value), 'kind': 'html'})
        if 'srcset' in attributes:
            start, value = attributes['srcset']
            if re.search(r'data:', html.unescape(value), re.I):
                raise ValueError(f'Data URI srcset is unsupported at offset {start}')
            if not value.strip():
                raise ValueError(f'Empty image srcset at offset {start}')
            offset = 0
            for candidate in value.split(','):
                parsed = re.fullmatch(r'\s*(\S+?)(?:\s+(?:[1-9]\d*w|(?:\d+(?:\.\d+)?|\.\d+)x))?\s*', candidate)
                if not parsed:
                    raise ValueError(f'Unsupported image srcset candidate at offset {start + offset}')
                refs.append({'start': start + offset + parsed.start(1),
                             'end': start + offset + parsed.end(1),
                             'url': html.unescape(parsed.group(1)), 'kind': 'html-srcset'})
                offset += len(candidate) + 1
    return refs


def _relative(document, target):
    return quote(Path(os.path.relpath(target, document.parent)).as_posix(), safe='/.-_~')


def repair_snapshot(root, snapshot, resolver):
    """Rebuild readable documents from originals; resolver returns a library Path."""
    root = Path(root).resolve()
    snapshot = _inside(root, snapshot)
    manifest_path = snapshot / 'manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    staged = {}
    rendered = []
    media = {}
    summary = {'refs_count': 0, 'images_fixed': 0, 'navigation_fixes': 0}
    for item in manifest['files']:
        source_path = item.get('source_path', item['path'])
        if Path(source_path).suffix.lower() not in SUFFIXES:
            continue
        original = _inside(root, root / item['path'])
        document = _inside(root, snapshot / source_path)
        raw = original.read_bytes()
        if _sha(raw) != item['sha256']:
            raise ValueError(f'Original hash mismatch: {original}')
        raw_path = document.with_name(document.name + '.original.txt')
        if raw_path.exists() and raw_path.read_bytes() != raw:
            raise ValueError(f'Conflicting preserved original: {raw_path}')
        text = raw.decode('utf-8-sig')
        replacements = {}
        refs = image_references(text)
        summary['refs_count'] += len(refs)
        for ref in refs:
            start, end, url = ref['start'], ref['end'], ref['url']
            resolved = Path(resolver(document, url))
            target = _inside(root, resolved if resolved.is_absolute() else root / resolved)
            if not target.is_file():
                raise FileNotFoundError(f'Missing resolved image: {target}')
            data = target.read_bytes()
            key = target.relative_to(root).as_posix()
            media[key] = {'path': key, 'sha256': _sha(data), 'bytes': len(data)}
            replacement = _relative(document, target)
            replacements[(start, end)] = replacement
            summary['images_fixed'] += replacement != text[start:end]
        masked = _mask(text)
        for match in re.finditer(r'(?<!!)\[[^\]\n]+\]\(([^\s)]+)\)', masked):
            start, end = match.span(1)
            if any(a <= start < b for a, b in replacements):
                continue
            url = match.group(1)
            replacement = None
            if url == '@GeminiApp':
                replacement = 'https://x.com/GeminiApp'
            else:
                parsed = urlsplit(url)
                if not parsed.scheme and not parsed.netloc and parsed.path:
                    target = (document.parent / unquote(parsed.path)).resolve()
                    if not target.is_file():
                        name = target.name.lower()
                        if name in ('readme.md', 'gallery.md', 'templates.md'):
                            dest = root / ('README.md' if name == 'readme.md' else 'docs/' + name)
                            replacement = _relative(document, dest)
                            if parsed.fragment:
                                replacement += '#' + parsed.fragment
            if replacement is not None:
                replacements[(start, end)] = replacement
                summary['navigation_fixes'] += 1
        for (start, end), replacement in sorted(replacements.items(), reverse=True):
            text = text[:start] + replacement + text[end:]
        header = f'> [原始文本]({_relative(document, raw_path)}) | [主画廊]({_relative(document, root / "docs/gallery.md")})\n\n'
        result = (header + text).encode('utf-8')
        staged[raw_path] = raw
        staged[document] = result
        item['path'] = raw_path.relative_to(root).as_posix()
        rendered.append({'path': document.relative_to(root).as_posix(), 'sha256': _sha(result),
                         'bytes': len(result), 'original_path': item['path']})
    manifest['rendered_files'] = rendered
    manifest['media_files'] = sorted(media.values(), key=lambda row: row['path'])
    staged[manifest_path] = (json.dumps(manifest, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    for path, data in staged.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists() or path.read_bytes() != data:
            path.write_bytes(data)
    return summary


def check_images(root, paths=None):
    """Check all real image references. External URLs are explicit errors."""
    root = Path(root).resolve()
    paths = paths if paths is not None else (p for p in root.rglob('*') if p.suffix.lower() in SUFFIXES and '.git' not in p.parts)
    errors = []
    count = 0
    for path in paths:
        path = Path(path)
        if not path.is_absolute():
            path = root / path
        try:
            path = _inside(root, path)
            refs = image_references(path.read_text(encoding='utf-8-sig'))
            count += len(refs)
            for ref in refs:
                url = ref['url']
                parsed = urlsplit(url)
                if parsed.scheme == 'data' and re.match(r'^data:image/[^,]+,', url, re.I):
                    continue
                if parsed.scheme or parsed.netloc:
                    errors.append({'path': str(path), 'reference': url, 'error': 'external'})
                    continue
                try:
                    target = _inside(root, path.parent / unquote(parsed.path))
                    if not target.is_file():
                        raise FileNotFoundError(f'Missing image: {target}')
                except (ValueError, FileNotFoundError) as exc:
                    errors.append({'path': str(path), 'reference': url, 'error': str(exc)})
        except (ValueError, OSError) as exc:
            errors.append({'path': str(path), 'error': str(exc)})
    return {'errors': errors, 'count': count}
