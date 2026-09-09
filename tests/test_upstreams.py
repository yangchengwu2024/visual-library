"""Dispatcher regressions using real archive imports and disposable fixture roots."""
import copy
import importlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import upstreams
import publish_sync
from PIL import Image

REVISION = 'a' * 40


class UpstreamTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / 'library'
        self.root.mkdir()
        self.asset = self.base / 'input.png'
        Image.new('RGB', (2, 2), 'red').save(self.asset)

    def tearDown(self):
        self.temp.cleanup()

    def source(self, identifier='approved', policy='approved_auto', scope=None):
        return {'id': identifier, 'policy': policy, 'adapter': 'freestylefly',
                'repository': 'owner/repository', 'url': 'https://github.com/owner/repository',
                'scope': scope or {'kind': 'all'}}

    def write_registry(self, sources):
        path = self.root / 'sources/registry.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'schema_version': 1, 'sources': sources}), encoding='utf-8')

    def record(self, identifier='1'):
        return {'source_id': identifier, 'title': 'Fixture ' + identifier,
                'prompt': 'Exact fixture prompt ' + identifier, 'category': 'Photography',
                'model': None, 'parameters': None, 'source_url': 'https://example.test/' + identifier,
                'assets': [{'path': str(self.asset), 'role': 'output', 'source_path': 'input.png'}],
                'missing_assets': []}

    def prepare(self, stage, source, revision, downloads, selected_ids=None):
        return [self.record(identifier) for identifier in sorted(selected_ids or {'1'})]

    def snapshot(self):
        return {p.relative_to(self.root).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns)
                for p in self.root.rglob('*') if p.is_file() and p.name != '.library.lock'}

    def test_sync_never_fetches_candidates_and_disabled_never_fetches(self):
        for policy, modes in [('candidate', ['sync']), ('manual_only', ['sync']), ('disabled', ['sync', 'check', 'import'])]:
            for mode in modes:
                with self.subTest(policy=policy, mode=mode):
                    self.write_registry([self.source(policy=policy)])
                    with patch.object(upstreams, 'latest', side_effect=AssertionError('Network must not run')) as network:
                        upstreams.run(self.root, mode, ['approved'], ['1'] if mode == 'import' else None)
                    network.assert_not_called()
        self.write_registry([self.source(policy='candidate')])
        with patch.object(upstreams, 'latest') as network:
            upstreams.run(self.root, 'check')
        network.assert_not_called()

    def test_changed_approved_notice_stops_before_case_import(self):
        source = self.source()
        old_fingerprint = upstreams.scope_hash(source)
        source['approved_documents'] = {'LICENSE': '0' * 64}
        self.assertNotEqual(old_fingerprint, upstreams.scope_hash(source))
        downloaded = self.base / 'upstream'
        downloaded.mkdir()
        (downloaded / 'LICENSE').write_bytes(b'changed notice')
        with patch.object(upstreams.sync_upstream, 'download_source', return_value=downloaded), patch.object(upstreams.sync_upstream, 'prepare') as parse:
            with self.assertRaisesRegex(ValueError, 'license or content notice changed'):
                upstreams.prepare(self.root, source, REVISION, self.base)
        parse.assert_not_called()

    def test_explicit_candidate_check_is_readonly_and_does_not_promote_policy(self):
        self.write_registry([self.source(policy='candidate')])
        before = self.snapshot()
        with patch.object(upstreams, 'latest', return_value=REVISION) as network, patch.object(upstreams, 'prepare', side_effect=self.prepare):
            result = upstreams.run(self.root, 'check', ['approved'])
        network.assert_called_once()
        self.assertEqual(result['sources'][0]['status'], 'CHECKED')
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(upstreams.registry(self.root)[0]['policy'], 'candidate')
        self.assertFalse((self.root / 'sources/approved/runtime.json').exists())

    def test_check_is_read_only_on_success_and_failure_including_runtime(self):
        self.write_registry([self.source()])
        with patch.object(upstreams, 'latest', return_value=REVISION), patch.object(upstreams, 'prepare', side_effect=self.prepare):
            upstreams.run(self.root)
        for failure in [False, True]:
            before = self.snapshot()
            with patch.object(upstreams, 'latest', return_value='b' * 40), patch.object(upstreams, 'prepare', side_effect=ValueError('bad source') if failure else self.prepare):
                result = upstreams.run(self.root, 'check')
            self.assertEqual(self.snapshot(), before)
            self.assertEqual(result['status'], 'FAILED' if failure else 'OK')
            if not failure:
                self.assertEqual(result['sources'][0]['status'], 'CHECKED')

    def test_failed_source_does_not_block_success_and_all_failures_are_failed(self):
        self.write_registry([self.source('bad'), self.source('good')])
        def prepared(stage, source, revision, downloads, selected_ids=None):
            if source['id'] == 'bad':
                raise OSError('unavailable')
            return self.prepare(stage, source, revision, downloads, selected_ids)
        with patch.object(upstreams, 'latest', return_value=REVISION), patch.object(upstreams, 'prepare', side_effect=prepared):
            result = upstreams.run(self.root)
        self.assertEqual(result['status'], 'PARTIAL')
        self.assertEqual([row['status'] for row in result['sources']], ['FAILED', 'SYNCED'])
        catalog = upstreams.library.query(self.root)
        self.assertEqual(len(catalog), 1)
        self.assertEqual(catalog[0]['sources'], ['good'])
        with patch.object(upstreams, 'latest', side_effect=OSError('unavailable')):
            self.assertEqual(upstreams.run(self.root)['status'], 'FAILED')

    def test_same_revision_scope_is_noop_and_changed_scope_runs(self):
        source = self.source()
        self.write_registry([source])
        with patch.object(upstreams, 'latest', return_value=REVISION), patch.object(upstreams, 'prepare', side_effect=self.prepare) as prepare:
            self.assertEqual(upstreams.run(self.root)['sources'][0]['status'], 'SYNCED')
            before = self.snapshot()
            self.assertEqual(upstreams.run(self.root)['sources'][0]['status'], 'UNCHANGED')
            self.assertEqual(prepare.call_count, 1)
            self.assertEqual(self.snapshot(), before)
            source['scope'] = {'kind': 'selected', 'ids': ['1']}
            self.write_registry([source])
            self.assertEqual(upstreams.run(self.root)['sources'][0]['status'], 'SYNCED')
            self.assertEqual(prepare.call_count, 2)

    def test_selected_import_appends_without_marking_omitted_records_absent(self):
        self.write_registry([self.source()])
        with patch.object(upstreams, 'latest', return_value=REVISION), patch.object(upstreams, 'prepare', side_effect=self.prepare):
            first = upstreams.run(self.root, 'import', ['approved'], ['1', '2'])
            self.assertEqual(first['status'], 'OK')
            second = upstreams.run(self.root, 'import', ['approved'], ['3'])
            self.assertEqual(second['sources'][0]['new_cases'], 1)
        state = upstreams.read(self.root / 'sources/approved/state.json')
        self.assertEqual(set(state['mappings']), {'1', '2', '3'})
        self.assertTrue(all(row['upstream_present'] for row in state['mappings'].values()))
        self.assertFalse((self.root / 'sources/approved/runtime.json').exists())
        self.assertEqual(len(upstreams.library.query(self.root)), 3)

    def test_freestyle_selected_scope_filters_actual_prepare(self):
        source = self.source(scope={'kind': 'selected', 'ids': ['2']})
        self.write_registry([source])
        records = [self.record('1'), self.record('2')]
        with patch.object(upstreams, 'latest', return_value=REVISION), patch.object(upstreams.sync_upstream, 'download_source', return_value=self.base), patch.object(upstreams.sync_upstream, 'prepare', return_value=(records, {})), patch.object(upstreams.sync_upstream, 'archive_documents') as archive:
            result = upstreams.run(self.root)
        self.assertFalse(archive.call_args.kwargs['include_templates'])
        self.assertEqual(result['status'], 'OK')
        state = upstreams.read(self.root / 'sources/approved/state.json')
        self.assertEqual(set(state['mappings']), {'2'})

    def test_selected_missing_or_out_of_scope_id_does_not_publish(self):
        source = self.source(scope={'kind': 'selected', 'ids': ['2', '3']})
        self.write_registry([source])
        for requested, expected in [(['2', '3'], 'not all found'), (['1'], 'leave approved')]:
            with self.subTest(requested=requested):
                before = self.snapshot()
                with patch.object(upstreams, 'latest', return_value=REVISION), patch.object(upstreams.sync_upstream, 'download_source', return_value=self.base), patch.object(upstreams.sync_upstream, 'prepare', return_value=([self.record('1'), self.record('2')], {})), patch.object(upstreams.sync_upstream, 'archive_documents') as archive:
                    result = upstreams.run(self.root, 'import', ['approved'], requested)
                self.assertEqual(result['status'], 'FAILED')
                self.assertIn(expected, result['sources'][0]['error'])
                self.assertEqual(self.snapshot(), before)
                archive.assert_not_called()

    def test_stage_failure_keeps_formal_archive_clean(self):
        self.write_registry([self.source()])
        with patch.object(upstreams, 'latest', return_value=REVISION), patch.object(upstreams, 'prepare', side_effect=self.prepare):
            upstreams.run(self.root)
        before = self.snapshot()
        def broken(stage, *args):
            (stage / 'images/staged-only.bin').write_bytes(b'partial artifact')
            raise ValueError('parser failure after staged write')
        with patch.object(upstreams, 'latest', return_value='b' * 40), patch.object(upstreams, 'prepare', side_effect=broken):
            self.assertEqual(upstreams.run(self.root)['status'], 'FAILED')
        after = self.snapshot()
        runtime = 'sources/approved/runtime.json'
        self.assertEqual({k:v for k,v in after.items() if k != runtime}, {k:v for k,v in before.items() if k != runtime})
        self.assertTrue(upstreams.read(self.root / runtime)['paused'])
        self.assertFalse((self.root / 'images/staged-only.bin').exists())


class PublisherTests(unittest.TestCase):
    def test_all_source_failure_is_not_reported_as_success_after_failure_state_push(self):
        calls = []
        def command(args, cwd, env, check=True):
            calls.append(args)
            stdout = ''
            code = 0
            if 'scripts/upstreams.py' in args:
                stdout = json.dumps({'status': 'FAILED', 'sources': [{'source': 'bad', 'status': 'FAILED'}]})
                code = 1
            elif args[1:3] == ['rev-parse', 'HEAD']:
                stdout = REVISION
            elif args[1:3] == ['status', '--porcelain']:
                stdout = ' M sources/bad/runtime.json'
            return subprocess.CompletedProcess(args, code, stdout, '')
        with patch.object(publish_sync, 'command', side_effect=command), patch.dict(publish_sync.os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, 'all selected sources failed'):
                publish_sync.publish('owner/repo')
        self.assertTrue(any(args[:2] == ['git', 'push'] for args in calls))
        self.assertTrue(any('scripts/validate_archive.py' in args for args in calls))
        self.assertFalse(any('--force' in args for args in calls))


class PublicationRollbackTests(unittest.TestCase):
    def test_handled_write_failure_restores_previous_files(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            root, stage = Path(folder) / 'root', Path(folder) / 'stage'
            for directory in (root / 'cases', stage / 'cases', stage / 'images'):
                directory.mkdir(parents=True)
            (root / 'cases/existing.json').write_bytes(b'old')
            (stage / 'cases/existing.json').write_bytes(b'new')
            (stage / 'images/new.png').write_bytes(b'new image')
            original_put = upstreams.sync_upstream.put
            calls = 0
            def fail_once(path, data):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError('simulated write interruption')
                return original_put(path, data)
            with patch.object(upstreams.sync_upstream, 'put', side_effect=fail_once):
                with self.assertRaisesRegex(OSError, 'write interruption'):
                    upstreams.copy_changes(stage, root)
            self.assertEqual((root / 'cases/existing.json').read_bytes(), b'old')
            self.assertFalse((root / 'images/new.png').exists())


if __name__ == '__main__':
    unittest.main()
