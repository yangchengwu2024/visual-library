import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import serve_mcp


class CacheTests(unittest.TestCase):
    def test_offline_requires_verified_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, 'No verified'):
                serve_mcp.prepare_snapshot(directory, offline=True)

    def test_network_failure_serves_only_previous_verified_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            url = serve_mcp.DEFAULT_REPOSITORY
            key = serve_mcp.hashlib.sha256(url.encode()).hexdigest()[:16]
            cache = Path(directory) / key
            commit = 'a' * 40
            snapshot = cache / 'snapshots' / commit
            snapshot.mkdir(parents=True)
            (cache / 'mirror.git').mkdir()
            (cache / 'last-good.json').write_text(json.dumps({'repository_url': url, 'commit': commit}))
            def fake_git(*args, **kwargs):
                if 'get-url' in args:
                    return url
                raise subprocess.CalledProcessError(1, 'git')
            with patch.object(serve_mcp, 'git', side_effect=fake_git), patch.object(serve_mcp, 'verify_snapshot') as verify:
                actual, actual_commit, status = serve_mcp.prepare_snapshot(directory)
                self.assertEqual((actual, actual_commit, status), (snapshot, commit, 'cached-offline'))
                verify.assert_called_once_with(snapshot, commit)

    def test_corrupt_snapshot_cannot_be_served_offline(self):
        with tempfile.TemporaryDirectory() as directory:
            url = serve_mcp.DEFAULT_REPOSITORY
            cache = Path(directory) / serve_mcp.hashlib.sha256(url.encode()).hexdigest()[:16]
            commit = 'b' * 40
            (cache / 'snapshots' / commit).mkdir(parents=True)
            (cache / 'last-good.json').write_text(json.dumps({'repository_url': url, 'commit': commit}))
            with patch.object(serve_mcp, 'verify_snapshot', side_effect=ValueError('corrupt')):
                with self.assertRaisesRegex(ValueError, 'corrupt'):
                    serve_mcp.prepare_snapshot(directory, offline=True)


if __name__ == '__main__':
    unittest.main()
