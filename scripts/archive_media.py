"""Self-contained rendered views of archived source documents, sharing image bytes."""
import hashlib
import io
import json
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
import posixpath
from urllib.parse import unquote, urlsplit, urlunsplit, quote
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

from archive_image_views import image_references, repair_snapshot


def read(path, default=None):
    return json.loads(path.read_text(encoding='utf-8')) if path.is_file() else default


def put(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() or path.read_bytes() != data:
        temporary = path.with_suffix(path.suffix + '.tmp')
        temporary.write_bytes(data)
        temporary.replace(path)


def save_json(path, value):
    put(path, (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)+'\n').encode())


def asset_index(root):
    """Use revision-specific provenance, never infer history from a filename alone."""
    lookup, by_hash = {}, {}
    for path in (root/'cases').glob('*/case.json'):
        header = read(path)
        for alias in header.get('source_aliases', []):
            version = alias.get('version')
            body = read(path.parent/(str(version)+'.json')) if version else None
            if not body:
                continue
            sources = alias.get('asset_sources', [])
            if len(sources) != len(body['assets']):
                continue
            for source, asset in zip(sources, body['assets']):
                file = root/asset['path']
                if not file.is_file():
                    continue
                if hashlib.sha256(file.read_bytes()).hexdigest() != asset['sha256']:
                    raise ValueError('Existing archive image hash mismatch')
                key = (alias['source'], alias['revision'], source.get('source_path'))
                lookup[key] = file
                if str(source.get('source_path','')).startswith('https://'):
                    lookup[source['source_path']] = file
                by_hash[asset['sha256']] = file
    for path in (root/'sources/_media').glob('*'):
        if path.is_file() and path.suffix != '.json':
            by_hash[hashlib.sha256(path.read_bytes()).hexdigest()] = path
    return lookup, by_hash


def image_suffix(data):
    if data.lstrip().startswith((b'<svg', b'<?xml', b'<!--')):
        try:
            tree = ET.fromstring(data)
            if tree.tag.rsplit('}',1)[-1] == 'svg':
                return '.svg'
        except ET.ParseError:
            pass
    from PIL import Image
    with Image.open(io.BytesIO(data)) as picture:
        picture.verify()
        return {"JPEG":'.jpg',"PNG":'.png',"WEBP":'.webp',"GIF":'.gif',"BMP":'.bmp',"AVIF":'.avif'}[picture.format]


class MediaResolver:
    def __init__(self, root, snapshot, source, *, source_dir=None, get_file=None, lookup=None):
        self.root, self.snapshot = Path(root).resolve(), Path(snapshot).resolve()
        self.source = source
        self.source_dir = Path(source_dir).resolve() if source_dir else None
        self.get_file = get_file
        self.lookup, self.by_hash = lookup if lookup else asset_index(self.root)
        self.index_path = self.root/'sources/_media/index.json'
        self.index = read(self.index_path, {'schema_version':1,'resources':{}})
        self.acquired = 0
        self.lock = threading.RLock()

    def __call__(self, document, reference):
        parts = urlsplit(reference)
        if parts.scheme:
            if parts.scheme != 'https':
                raise ValueError('Unsupported image URL scheme')
            key = reference
            source_path = None
            if key in self.lookup:
                return self.lookup[key]
            url = reference
        else:
            existing = (Path(document).parent/unquote(parts.path)).resolve()
            if existing.is_file() and existing.is_relative_to(self.root):
                if '.git' in existing.relative_to(self.root).parts:
                    raise ValueError('Git internals cannot be image resources')
                image_suffix(existing.read_bytes())
                return existing
            relative_doc = Path(document).relative_to(self.snapshot).as_posix()
            source_path = posixpath.normpath(posixpath.join(posixpath.dirname(relative_doc),unquote(parts.path)))
            if source_path.startswith('../') or source_path.startswith('/'):
                raise ValueError('Source image path leaves its snapshot')
            key = self.source['repository']+'@'+self.snapshot.name+':'+source_path
            saved = self.lookup.get((self.source['id'],self.snapshot.name,source_path))
            if saved:
                return saved
            url = 'https://raw.githubusercontent.com/'+self.source['repository']+'/'+self.snapshot.name+'/'+quote(source_path,safe='/')
        cached = self.index['resources'].get(key)
        if cached:
            file = self.root/cached['path']
            if file.is_file() and hashlib.sha256(file.read_bytes()).hexdigest() == cached['sha256']:
                return file
        if source_path and self.source_dir:
            file = (self.source_dir/source_path).resolve()
            if not file.is_relative_to(self.source_dir):
                raise ValueError('Source image leaves source checkout')
            data = file.read_bytes()
        elif source_path and self.get_file:
            data = Path(self.get_file(source_path)).read_bytes()
        else:
            parsed=urlsplit(url)
            allowed={'raw.githubusercontent.com','user-images.githubusercontent.com','github.com','img.shields.io','api.star-history.com'}
            if parsed.hostname not in allowed or parsed.username or parsed.password:
                raise ValueError('Unreviewed source image host: '+str(parsed.hostname))
            encoded=urlunsplit((parsed.scheme,parsed.netloc,quote(parsed.path,safe='/%:@'),quote(parsed.query,safe='=&/:,+%'),''))
            with urlopen(Request(encoded,headers={'User-Agent':'visual-library-archive/1.0'}),timeout=45) as response:
                data=response.read(64*1024*1024+1)
        if len(data)>64*1024*1024:
            raise ValueError('Source image exceeds 64 MiB')
        suffix=image_suffix(data)
        digest=hashlib.sha256(data).hexdigest()
        with self.lock:
            destination=self.by_hash.get(digest) or self.root/'sources/_media'/(digest+suffix)
            put(destination,data)
            self.by_hash[digest]=destination
            self.index['resources'][key]={'path':destination.relative_to(self.root).as_posix(),'sha256':digest,'bytes':len(data),'source_url':url,'archived_at':datetime.now(timezone.utc).isoformat()}
            save_json(self.index_path,self.index)
            self.acquired+=1
        return destination


def repair_source(root, snapshot, source, source_dir=None, get_file=None):
    resolver=MediaResolver(root,snapshot,source,source_dir=source_dir,get_file=get_file)
    prefetch(snapshot,resolver)
    result=repair_snapshot(root,snapshot,resolver)
    result['downloaded_resources']=resolver.acquired
    return result


def prefetch(snapshot,resolver):
    snapshot=Path(snapshot)
    manifest=read(snapshot/'manifest.json')
    pending={}
    for item in manifest['files']:
        original=resolver.root/item['path']
        logical=item.get('source_path',item['path'])
        if Path(logical).suffix.lower() not in {'.md','.markdown','.html','.htm'}:
            continue
        document=snapshot/logical
        for ref in image_references(original.read_text(encoding='utf-8-sig')):
            key=ref['url'] if urlsplit(ref['url']).scheme else str(document.parent/ref['url'])
            pending[key]=(document,ref['url'])
    errors=[]
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures={pool.submit(resolver,*args):args for args in pending.values()}
        for future in as_completed(futures):
            try:future.result()
            except Exception as exc:
                errors.append({'document':str(futures[future][0]),'reference':futures[future][1],'error':str(exc)})
    if errors:
        raise RuntimeError('Source image fetch failed: '+json.dumps(errors,ensure_ascii=False))
