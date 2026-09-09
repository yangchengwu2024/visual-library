import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from archive_image_views import repair_snapshot, check_images, image_references


class ArchiveImageViewsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.snapshot = self.root / 'sources/example/upstream/rev'
        self.snapshot.mkdir(parents=True)
        (self.root / 'docs').mkdir()
        (self.root / 'docs/gallery.md').write_text('# Gallery', encoding='utf-8')
        (self.root / 'README.md').write_text('# Home', encoding='utf-8')
        self.asset = self.root / 'images/own image.png'
        self.asset.parent.mkdir()
        self.asset.write_bytes(b'image fixture')

    def archive(self, raw):
        path = self.snapshot / 'docs/gallery.md'
        path.parent.mkdir()
        path.write_bytes(raw)
        item = {'path': path.relative_to(self.root).as_posix(), 'source_path': 'docs/gallery.md',
                'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw)}
        (self.snapshot / 'manifest.json').write_text(json.dumps({'files': [item]}), encoding='utf-8')
        return path

    def test_repair_preserves_original_and_is_idempotent(self):
        raw = ('[home](../README.md#start)\r\n![one](../missing.png)\r\n'
               '```text\r\n![literal]({image})\r\n```\r\n'
               '`![inline]({other})`\r\n').encode('utf-8')
        path = self.archive(raw)
        seen = []
        def resolve(document, url):
            seen.append((document, url))
            return self.asset
        result = repair_snapshot(self.root, self.snapshot, resolve)
        self.assertEqual(result, {'refs_count': 1, 'images_fixed': 1, 'navigation_fixes': 1})
        self.assertEqual(seen, [(path, '../missing.png')])
        self.assertEqual(path.with_name(path.name + '.original.txt').read_bytes(), raw)
        first = path.read_bytes()
        self.assertIn(b'![literal]({image})', first)
        self.assertIn(b'`![inline]({other})`', first)
        self.assertIn(b'README.md#start', first)
        manifest_first = (self.snapshot / 'manifest.json').read_bytes()
        repair_snapshot(self.root, self.snapshot, resolve)
        self.assertEqual(first, path.read_bytes())
        self.assertEqual(manifest_first, (self.snapshot / 'manifest.json').read_bytes())
        self.assertEqual(check_images(self.root)['errors'], [])
        manifest = json.loads(manifest_first)
        self.assertEqual(manifest['media_files'][0]['sha256'], hashlib.sha256(self.asset.read_bytes()).hexdigest())

    def test_html_reference_and_nested_inline_destinations(self):
        text = ('![one](<a b.png> "title")\n![two](a(b).png)\n'
                '![three][ID]\n![ID][]\n![ID]\n[ID]: https://example.com/x.png\n'
                '<img alt="a" src="https://example.com/x?a=1&amp;b=2">\n'
                '<img src=relative.png>\n<!-- ![ignored](bad.png) -->')
        refs = image_references(text)
        self.assertEqual(len(refs), 7)
        self.assertEqual(refs[0]['url'], 'a b.png')
        self.assertEqual(refs[1]['url'], 'a(b).png')
        self.assertEqual(refs[-2]['url'], 'https://example.com/x?a=1&b=2')
        path = self.archive(text.encode())
        repair_snapshot(self.root, self.snapshot, lambda p, u: self.asset)
        self.assertEqual(check_images(self.root, [path]), {'errors': [], 'count': 7})

    def test_missing_and_outside_resolver_fail_before_writes(self):
        raw = b'![image](old.png)'
        path = self.archive(raw)
        for target, exception in [(self.root / 'missing.png', FileNotFoundError),
                                  (self.root.parent / 'unsafe.png', ValueError)]:
            with self.assertRaises(exception):
                repair_snapshot(self.root, self.snapshot, lambda p, u: target)
            self.assertEqual(path.read_bytes(), raw)
            self.assertFalse(path.with_name(path.name + '.original.txt').exists())

    def test_picture_and_img_srcset_preserve_descriptors_and_code(self):
        text = ('<picture><source type="image/webp" srcset="https://a/x.webp 400w, https://a/y.webp 800w">'
                '<source type="image/png"><img src="https://a/fallback.png" '
                'srcset="https://a/one.png 1x, https://a/two.png 2x"></picture>\n'
                '<video><source src="movie.mp4"></video>\n'
                '`<img srcset="literal.png 2x">`\n'
                '```html\n<img srcset="literal.png 2x">\n```\n')
        refs = image_references(text)
        self.assertEqual(len(refs), 5)
        for ref in refs:
            self.assertEqual(text[ref['start']:ref['end']], ref['url'])
        path = self.archive(text.encode())
        repair_snapshot(self.root, self.snapshot, lambda p, u: self.asset)
        rendered = path.read_text(encoding='utf-8')
        for descriptor in ['400w', '800w', '1x', '2x']:
            self.assertIn(' ' + descriptor, rendered)
        self.assertIn('`<img srcset="literal.png 2x">`', rendered)
        self.assertIn('```html\n<img srcset="literal.png 2x">\n```', rendered)
        self.assertEqual(check_images(self.root, [path]), {'errors': [], 'count': 5})
        with self.assertRaisesRegex(ValueError, 'Data URI srcset'):
            image_references('<img srcset="data:image/png;base64,AAAA 1x">')

    def test_hash_mismatch_fails(self):
        path = self.archive(b'![image](old.png)')
        path.write_bytes(b'tampered')
        with self.assertRaisesRegex(ValueError, 'hash mismatch'):
            repair_snapshot(self.root, self.snapshot, lambda p, u: self.asset)

    def test_malformed_references_and_external_are_reported(self):
        for text in ['![a][missing]', '![a](unclosed', '<img alt="no source">']:
            with self.assertRaises(ValueError):
                image_references(text)
        path = self.archive(b'![remote](https://example.com/x.png)\n![escape](../../../../../../escape.png)')
        result = check_images(self.root, [path])
        self.assertEqual(result['count'], 2)
        self.assertEqual(result['errors'][0]['error'], 'external')
        self.assertIn('Unsafe path', result['errors'][1]['error'])


if __name__ == '__main__':
    unittest.main()
