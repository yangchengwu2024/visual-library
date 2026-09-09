"""Multi-source preservation, archive validation, and gallery review semantics."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


def module(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parents[1] / 'scripts' / (name + '.py'))
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


templates = module('template_catalog')
archive = module('validate_archive')
gallery = module('gallery')


class MultisourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def write(self, name, payload):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload) if isinstance(payload, dict) else payload, encoding='utf-8')
        return path

    def payload(self, source, count=1):
        document = 'sources/' + source + '/upstream/r1/templates.md'
        self.write(document, '<a id="first"></a>\nOriginal full text')
        self.write('sources/' + source + '/state.json', {'last_attempt_revision': 'r1'})
        return {'source': source, 'revision': 'r1', 'document': document,
                'templates': [{'id': 'shared-' + str(i), 'title': 'Original', 'anchor': 'first'} for i in range(count)]}

    def snapshot(self):
        return {str(p.relative_to(self.root)): (p.read_bytes(), p.stat().st_mtime_ns)
                for p in self.root.rglob('*') if p.is_file()}

    def test_legacy_migration_keeps_twenty_two_and_same_local_id_is_namespaced(self):
        original = self.payload('original', 22)
        self.write('indexes/templates.json', original)
        templates.write_source_templates(self.root, 'new', self.payload('new'))
        result = templates.rebuild_templates(self.root)
        self.assertEqual(len(result['templates']), 23)
        self.assertEqual(len({t['stable_id'] for t in result['templates']}), 23)
        self.assertEqual(sum(t['id'] == 'shared-0' for t in result['templates']), 2)
        self.assertEqual(archive.validate(self.root)['status'], 'PASS')
        empty = self.payload('empty', 0)
        templates.write_source_templates(self.root, 'empty', empty)
        self.assertEqual(len(templates.rebuild_templates(self.root)['templates']), 23)
        before = self.snapshot()
        self.assertFalse(templates.write_source_templates(self.root, 'empty', empty)['changed'])
        self.assertEqual(self.snapshot(), before)

    def test_legacy_validation_and_failed_later_revision_preserve_archive(self):
        payload = self.payload('original')
        self.write('indexes/templates.json', payload)
        self.assertEqual(archive.validate(self.root)['status'], 'PASS')
        templates.rebuild_templates(self.root)
        self.write('sources/original/state.json', {'last_attempt_revision': 'r2', 'status': 'failed'})
        self.assertEqual(archive.validate(self.root)['status'], 'PASS')
        path = self.root / 'sources/original/templates.json'
        sidecar = json.loads(path.read_text())
        sidecar['templates'][0]['document'] = 'nonexistent.md'
        self.write('sources/original/templates.json', sidecar)
        self.assertEqual(archive.validate(self.root)['status'], 'FAIL')

    def test_invalid_source_and_duplicate_ids_do_not_mutate(self):
        payload = self.payload('original')
        self.write('indexes/templates.json', payload)
        before = self.snapshot()
        with self.assertRaises(ValueError):
            templates.write_source_templates(self.root, '../escape', payload)
        payload['templates'].append(dict(payload['templates'][0]))
        with self.assertRaises(ValueError):
            templates.write_source_templates(self.root, 'original', payload)
        self.assertEqual(self.snapshot(), before)

    def test_gallery_review_labels_facets_and_source_templates(self):
        for source in ['a', 'b']:
            templates.write_source_templates(self.root, source, self.payload(source))
        self.write('README.md', gallery.README_START + '\n' + gallery.README_END)
        entries = []
        for number, kind in enumerate(['case', 'style_reference', 'keyword_reference']):
            entry = {'case_id': 'fixture-' + str(number), 'version': 'v1', 'title': 'Fixture',
                     'record_type': kind, 'model_family': 'model-' + str(number),
                     'artists': ['Artist'], 'materials': ['Stone']}
            if number == 0:
                entry.update(review_status='missing_inputs', effective_status='missing_inputs', review_note='Missing portrait reference')
            entries.append(entry)
            self.write('cases/' + entry['case_id'] + '/v1.json', {'prompt': 'Original prompt', 'assets': []})
            self.write('cases/' + entry['case_id'] + '/v1.md', '# Details')
        gallery.build_gallery(self.root, {'cases': entries})
        overview = (self.root / 'docs/gallery.md').read_text(encoding='utf-8')
        self.assertIn('\u539f\u56fe\u6587\u6848\u4f8b 1', overview)
        self.assertIn('\u98ce\u683c\u53c2\u8003 1', overview)
        self.assertIn('\u5173\u952e\u8bcd\u8d44\u6599 1', overview)
        self.assertIn('\u5f85\u590d\u6838 2', overview)
        self.assertIn('\u7f3a\u5c11\u5fc5\u8981\u8f93\u5165\u56fe 1', overview)
        part = (self.root / 'docs/gallery-part-1.md').read_text(encoding='utf-8')
        self.assertIn('Missing portrait reference', part)
        self.assertIn('\u5c1a\u65e0\u8865\u5145\u6838\u67e5', part)
        self.assertIn('model-0', (self.root / 'docs/models/index.md').read_text(encoding='utf-8'))
        self.assertIn('Stone', (self.root / 'docs/topics/index.md').read_text(encoding='utf-8'))
        template_page = (self.root / 'docs/templates.md').read_text(encoding='utf-8')
        self.assertIn('../sources/a/upstream/r1/templates.md#first', template_page)
        self.assertIn('../sources/b/upstream/r1/templates.md#first', template_page)


if __name__ == '__main__':
    unittest.main()
