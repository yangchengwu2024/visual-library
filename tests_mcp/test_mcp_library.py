"""Isolated behavior tests for the read-only MCP snapshot boundary."""
import asyncio
import base64
import copy
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import library
from mcp_library import LibrarySnapshot, create_server
from PIL import Image

COMMIT = "a" * 40
REPOSITORY = "https://github.com/example/visual-library"


class MCPLibraryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "archive"
        self.source = self.base / "source.png"
        Image.new("RGB", (2000, 1000), "steelblue").save(self.source)
        self.record = {"source_id": "one", "title": "Warm portrait", "prompt": "First line\r\n原文。  \n",
                       "category": "Characters", "styles": ["Warm"], "scenes": ["Interior"],
                       "source_url": "https://example.test/one", "model": None, "parameters": None,
                       "assets": [{"path": str(self.source), "source_path": str(self.source), "role": "output"}],
                       "missing_assets": []}
        library.import_records(self.root, [self.record], "fixture", "r1")
        self.case_id = library.stable_case_id("fixture", "one")
        changed = copy.deepcopy(self.record)
        changed["prompt"] = "Second version"
        library.import_records(self.root, [changed], "fixture", "r2")
        (self.root / "metadata").mkdir(exist_ok=True)
        (self.root / "metadata/taxonomy.json").write_text(json.dumps({
            "categories": [{"value": "Characters"}], "aliases": {"人物": "Characters"}}), encoding="utf-8")
        (self.root / "indexes/templates.json").write_text(json.dumps({"templates": [{
            "id": "portrait", "document": "docs/templates.md", "anchor": "portrait", "title": "Portrait"}]}), encoding="utf-8")
        self.snapshot = LibrarySnapshot(self.root, COMMIT, REPOSITORY)

    def files(self):
        return {str(p.relative_to(self.root)): (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
                for p in self.root.rglob("*") if p.is_file()}

    def test_all_reads_leave_files_bytes_names_and_mtimes_unchanged(self):
        before = self.files()
        self.snapshot.search_cases(["Warm"])
        self.snapshot.get_case(self.case_id, "v1")
        self.snapshot.get_case_image(self.case_id, "v1")
        self.snapshot.list_catalog()
        self.assertEqual(before, self.files())

    def test_search_alias_and_version_fixed_links(self):
        result = self.snapshot.search_cases(category="人物")
        self.assertEqual(len(result["cases"]), 1)
        item = result["cases"][0]
        self.assertEqual((item["case_id"], item["version"], item["commit"]), (self.case_id, "v2", COMMIT))
        self.assertIn("/blob/" + COMMIT + "/cases/", item["github_url"])
        self.assertTrue(item["versions"][0]["json_url"].endswith("/v1.json"))
        self.assertFalse(result["visual_match_verified"])
        for limit in (0, -1, 31):
            with self.assertRaises(ValueError):
                self.snapshot.search_cases(limit=limit)

    def test_exact_old_version_missing_version_and_local_paths(self):
        old = self.snapshot.get_case(self.case_id, "v1", COMMIT)["case"]
        self.assertEqual(old["prompt"], self.record["prompt"])
        self.assertEqual(self.snapshot.get_case(self.case_id)["case"]["version"], "v2")
        self.assertNotIn("absolute_path", json.dumps(old))
        self.assertNotIn(str(self.source), json.dumps(old))
        self.assertIn("sha256", old["assets"][0])
        for version in ("v99", "999"):
            with self.assertRaises(KeyError):
                self.snapshot.get_case(self.case_id, version)
            with self.assertRaises(KeyError):
                self.snapshot.get_case_image(self.case_id, version)

    def test_image_has_decodable_mcp_payload_with_hash_and_bounded_dimensions(self):
        result = self.snapshot.get_case_image(self.case_id, "v1", max_size=800)
        image = next(block for block in result.content if block.type == "image")
        data = base64.b64decode(image.data, validate=True)
        metadata = result.structuredContent
        self.assertEqual(image.mimeType, "image/png")
        self.assertEqual(hashlib.sha256(data).hexdigest(), metadata["preview_sha256"])
        self.assertEqual(metadata["sha256"], hashlib.sha256(self.source.read_bytes()).hexdigest())
        self.assertEqual(metadata["original_size"], [2000, 1000])
        with Image.open(io.BytesIO(data)) as preview:
            self.assertEqual(preview.size, (800, 400))
        self.assertIn("/raw/" + COMMIT + "/images/", metadata["original_url"])
        self.assertEqual(json.loads(result.content[0].text), metadata)

    def test_invalid_image_index_size_and_commit_fail(self):
        for index in (-1, 1):
            with self.assertRaises(ValueError):
                self.snapshot.get_case_image(self.case_id, image_index=index)
        for size in (0, 1601):
            with self.assertRaises(ValueError):
                self.snapshot.get_case_image(self.case_id, max_size=size)
        for method in (self.snapshot.get_case, self.snapshot.get_case_image):
            with self.assertRaisesRegex(ValueError, "commit mismatch"):
                method(self.case_id, expected_commit="b" * 40)

    def test_corrupt_image_fails_instead_of_returning_unverified_bytes(self):
        asset = library.show(self.root, self.case_id)["assets"][0]
        (self.root / asset["path"]).write_bytes(b"corrupt")
        with self.assertRaisesRegex(ValueError, "hash, size, or filename mismatch"):
            self.snapshot.get_case_image(self.case_id)

    def test_asset_path_escape_rejected(self):
        version = self.root / "cases" / self.case_id / "v1.json"
        data = json.loads(version.read_text(encoding="utf-8"))
        data["assets"][0]["path"] = "images/../../source.png"
        version.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(ValueError):
            self.snapshot.get_case_image(self.case_id, "v1")

    def test_catalog_and_offline_status(self):
        snapshot = LibrarySnapshot(self.root, COMMIT, REPOSITORY, "cached-offline")
        results = [snapshot.list_catalog(), snapshot.search_cases(), snapshot.get_case(self.case_id),
                   snapshot.get_case_image(self.case_id).structuredContent]
        for result in results:
            self.assertEqual(result["snapshot_status"], "cached-offline")
            self.assertEqual(result["commit"], COMMIT)
        catalog = results[0]
        self.assertEqual(catalog["case_count"], 1)
        self.assertEqual(catalog["taxonomy"]["aliases"]["人物"], "Characters")
        self.assertTrue(catalog["templates"]["templates"][0]["github_url"].endswith("docs/templates.md#portrait"))

    def test_sdk_exposes_only_four_readonly_tools(self):
        server = create_server(self.root, COMMIT, REPOSITORY)
        tools = asyncio.run(server.list_tools())
        self.assertEqual({tool.name for tool in tools}, {"search_cases", "get_case", "get_case_image", "list_catalog"})
        for tool in tools:
            self.assertTrue(tool.annotations.readOnlyHint)
            self.assertFalse(tool.annotations.destructiveHint)
            self.assertTrue(tool.annotations.idempotentHint)
            self.assertFalse(tool.annotations.openWorldHint)
        image_schema = next(tool.inputSchema for tool in tools if tool.name == "get_case_image")
        self.assertIn("expected_commit", image_schema["properties"])


if __name__ == "__main__":
    unittest.main()
