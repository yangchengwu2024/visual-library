import importlib.util
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('publisher', Path(__file__).resolve().parents[1] / 'scripts/publish_sync.py')
publisher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publisher)


class PublisherTests(unittest.TestCase):
    def fake(self, conflict=False, fail_push=False):
        calls = []
        state = {'clones': 0, 'pushes': 0}
        def command(args, cwd, env, check=True):
            calls.append(args)
            if args[:2] == ['git', 'clone']:
                state['clones'] += 1
            output, error, code = '', '', 0
            if args[:2] == ['git', 'rev-parse']:
                output = 'base-' + str(state['clones'])
            elif args[:2] == ['git', 'status']:
                output = ' M indexes/catalog.json'
            elif args[:2] == ['git', 'push']:
                state['pushes'] += 1
                if (conflict and state['pushes'] == 1) or fail_push:
                    error, code = 'push rejected', 1
            elif args[:2] == ['git', 'ls-remote']:
                output = ('new-base' if conflict else 'base-' + str(state['clones'])) + '\trefs/heads/main'
            return subprocess.CompletedProcess(args, code, output, error)
        return command, calls, state

    def test_conflict_recomputes_in_new_clone_without_force(self):
        fake, calls, state = self.fake(conflict=True)
        with patch.object(publisher, 'command', fake):
            publisher.publish('example/library')
        self.assertEqual(state['clones'], 2)
        self.assertEqual(state['pushes'], 2)
        self.assertEqual(sum('scripts/upstreams.py' in c for c in calls), 2)
        self.assertFalse(any('--force' in c or 'reset' in c or 'rebase' in c for c in calls))

    def test_no_change_does_not_commit(self):
        fake, calls, state = self.fake()
        def no_change(args, cwd, env, check=True):
            result = fake(args, cwd, env, check)
            if args[:2] == ['git', 'status']:
                result.stdout = ''
            return result
        with patch.object(publisher, 'command', no_change):
            publisher.publish('example/library')
        self.assertEqual(state['pushes'], 0)
        self.assertFalse(any(c[:2] == ['git', 'commit'] for c in calls))

    def test_failed_sync_never_pushes(self):
        fake, calls, state = self.fake()
        def failed(args, cwd, env, check=True):
            result = fake(args, cwd, env, check)
            if 'scripts/upstreams.py' in args:
                result.returncode = 1
                result.stdout = 'pending resource'
            return result
        with patch.object(publisher, 'command', failed):
            with self.assertRaisesRegex(RuntimeError, 'remote left unchanged'):
                publisher.publish('example/library')
        self.assertEqual(state['pushes'], 0)

    def test_network_failure_is_not_force_retried(self):
        fake, calls, state = self.fake(fail_push=True)
        with patch.object(publisher, 'command', fake):
            with self.assertRaisesRegex(RuntimeError, 'without a competing commit'):
                publisher.publish('example/library')
        self.assertEqual(state['clones'], 1)


if __name__ == '__main__':
    unittest.main()
