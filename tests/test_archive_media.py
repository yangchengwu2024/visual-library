import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from archive_media import asset_index


class ArchiveMediaIndexTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.remote = 'https://mmbiz.qpic.cn/example.png'
        self.local = self.root / 'images/local.png'
        self.local.parent.mkdir()
        self.local.write_bytes(b'archived image fixture')
        self.digest = hashlib.sha256(self.local.read_bytes()).hexdigest()
        case = self.root / 'cases/mixed'
        case.mkdir(parents=True)
        header = {'source_aliases': [{
            'source': 'example', 'revision': 'revision-1', 'version': 'v1',
            'asset_sources': [{'source_path': self.remote}, {'source_path': 'upstream/local.png'}],
        }]}
        body = {'assets': [
            {'remote_url': self.remote},
            {'path': 'images/local.png', 'sha256': self.digest},
        ]}
        (case / 'case.json').write_text(json.dumps(header), encoding='utf-8')
        (case / 'v1.json').write_text(json.dumps(body), encoding='utf-8')

    def test_remote_asset_does_not_block_local_media_reuse(self):
        with patch('archive_media.urlopen') as fetch:
            lookup, by_hash = asset_index(self.root)
        fetch.assert_not_called()
        self.assertEqual(lookup, {('example', 'revision-1', 'upstream/local.png'): self.local})
        self.assertEqual(by_hash, {self.digest: self.local})

    def test_local_hash_validation_survives_remote_asset(self):
        self.local.write_bytes(b'corrupted local image')
        with self.assertRaisesRegex(ValueError, 'Existing archive image hash mismatch'):
            asset_index(self.root)


if __name__ == '__main__':
    unittest.main()
