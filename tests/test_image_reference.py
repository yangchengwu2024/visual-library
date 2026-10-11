"""Image-only records retain provenance without weakening prompt-based cases."""

import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]

def load(name):
    spec = importlib.util.spec_from_file_location('image_reference_' + name, ROOT / 'scripts' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

library, gallery = load('library'), load('gallery')

class ImageReferenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'library'
        self.image = Path(self.temp.name) / 'reference.png'
        self.image.write_bytes(b'reference fixture bytes')
        self.record = {
            'source_id': 'one', 'title': 'Face line reference', 'prompt': '',
            'category': 'Characters', 'styles': ['Line drawing'], 'scenes': ['Portrait'],
            'source_url': 'https://example.test/source', 'model': None, 'parameters': None,
            'assets': [{'path': str(self.image), 'role': 'face_reference', 'source_path': 'reference.png'}],
            'missing_assets': [], 'license': 'CC BY 4.0', 'attribution': 'Fixture author',
            'source_metadata': {'source_record': 'source-one', 'method': 'line extraction',
                                'master_sha256': 'a' * 64, 'processing': {'long_edge': 512, 'quality': 85}},
            'metadata': {'record_type': 'image_reference', 'review_status': 'verified'},
        }

    def ingest(self, record=None):
        return library.import_records(self.root, [record or self.record], 'face-reference-fixture', 'r1', mode='append')

    def content(self):
        return library.show(self.root, library.stable_case_id('face-reference-fixture', 'one'), 'v1')

    def test_image_without_prompt_is_complete_readable_and_repeatable(self):
        first = self.ingest()
        self.assertEqual((first['new_cases'], first['pending']), (1, 0))
        content = self.content()
        self.assertEqual(content['prompt'], '')
        self.assertEqual(content['record_type'], 'image_reference')
        self.assertEqual(content['status'], 'complete')
        self.assertEqual(len(content['assets']), 1)
        self.assertTrue(library.validate(self.root)['ok'])
        version_path = self.root / 'cases' / content['case_id'] / 'v1.json'
        before = version_path.read_bytes()
        self.assertEqual(self.ingest()['duplicates'], 1)
        self.assertEqual(version_path.read_bytes(), before)
        self.assertEqual(self.content()['available_versions'], ['v1'])
        matches = library.query(self.root, record_type='image_reference')
        self.assertEqual([row['case_id'] for row in matches], [content['case_id']])
        markdown = version_path.with_suffix('.md').read_text(encoding='utf-8')
        self.assertIn('图片参考', markdown)
        self.assertNotIn('## 完整提示词', markdown)
        self.assertNotIn('```text', markdown)
        catalog = json.loads((self.root / 'indexes/catalog.json').read_text(encoding='utf-8'))
        gallery.build_gallery(self.root, catalog)
        self.assertIn('图片参考 1', (self.root / 'docs/gallery.md').read_text(encoding='utf-8'))

    def test_actual_prompt_and_processing_relationship_are_preserved(self):
        record = copy.deepcopy(self.record)
        record['prompt'] = '原始提线提示\r\nKeep `eyes`.  \n'
        self.ingest(record)
        content = self.content()
        self.assertEqual(content['prompt'], record['prompt'])
        provenance = next(x for x in content['source_aliases'] if x.get('source_metadata'))
        self.assertEqual(provenance['source_metadata'], record['source_metadata'])
        self.assertEqual(provenance['license'], record['license'])
        self.assertEqual(provenance['attribution'], record['attribution'])
        self.assertIn(record['prompt'], (self.root / 'cases' / content['case_id'] / 'v1.md').read_bytes().decode('utf-8'))

    def test_missing_image_stays_pending_and_old_cases_still_need_prompt(self):
        missing_image = copy.deepcopy(self.record)
        missing_image['assets'] = []
        result = self.ingest(missing_image)
        self.assertEqual((result['new_cases'], result['pending']), (0, 1))
        for kind in ('case', 'style_reference', 'keyword_reference'):
            record = copy.deepcopy(self.record)
            record['source_id'] = kind
            record['metadata']['record_type'] = kind
            result = self.ingest(record)
            self.assertEqual((result['new_cases'], result['pending']), (0, 1), kind)
        self.assertEqual(library.query(self.root), [])

    def test_later_incomplete_record_keeps_last_complete_version(self):
        self.ingest()
        content = self.content()
        before = (self.root / 'cases' / content['case_id'] / 'v1.json').read_bytes()
        incomplete = copy.deepcopy(self.record)
        incomplete['assets'] = []
        self.assertEqual(self.ingest(incomplete)['pending'], 1)
        self.assertEqual((self.root / 'cases' / content['case_id'] / 'v1.json').read_bytes(), before)
        self.assertEqual(self.content()['available_versions'], ['v1'])

if __name__ == '__main__':
    unittest.main()
