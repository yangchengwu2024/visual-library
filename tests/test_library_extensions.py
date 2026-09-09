"""Selection imports and version metadata remain separate from original archives."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location("extension_library", Path(__file__).resolve().parents[1] / "scripts/library.py")
library = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(library)


class LibraryExtensionsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "library"
        self.asset = self.base / "image.png"
        self.asset.write_bytes(b"fixture image")
        self.record = {"source_id": "1", "title": "original", "prompt": "exact original",
                       "assets": [{"path": str(self.asset), "role": "example"}],
                       "source_url": "https://example.test/primary-evidence"}
        self.case_id = library.stable_case_id("fixture", "1")

    def ingest(self, records, revision="r1", **kwargs):
        return library.import_records(self.root, records, "fixture", revision, **kwargs)

    def state(self):
        return library._read(self.root / "sources/fixture/state.json")

    def test_append_preserves_unselected_mapping_pending_and_cursors(self):
        second = dict(self.record, source_id="2")
        self.ingest([self.record, second])
        missing = dict(self.record, source_id="3", assets=[])
        self.ingest([self.record, second, missing], "r2")
        state_before = self.state()
        pending_path = next((self.root / "sources/fixture/pending").glob("*.json"))
        pending_before = pending_path.read_bytes()
        self.ingest([dict(self.record, prompt="revised")], "selected-r3", mode="append")
        state = self.state()
        self.assertEqual(state["mappings"]["2"], state_before["mappings"]["2"])
        self.assertTrue(state["mappings"]["2"]["upstream_present"])
        self.assertEqual(state["last_attempt_revision"], "r2")
        self.assertEqual(state["last_success_revision"], "r1")
        self.assertEqual(state["pending_count"], 1)
        self.assertEqual(pending_path.read_bytes(), pending_before)
        self.ingest([dict(self.record, source_id="3")], "selected-r4", mode="append")
        self.assertEqual(self.state()["pending_count"], 0)
        self.assertEqual(self.state()["last_attempt_revision"], "r2")
        self.assertEqual(self.state()["last_success_revision"], "r1")

    def test_snapshot_still_marks_absent_items(self):
        self.ingest([self.record, dict(self.record, source_id="2")])
        self.ingest([self.record], "r2")
        self.assertFalse(self.state()["mappings"]["2"]["upstream_present"])
        self.assertEqual(self.state()["last_success_revision"], "r2")

    def test_sidecar_addition_keeps_old_bytes_and_personal_authority(self):
        self.ingest([self.record])
        version_path = self.root / "cases" / self.case_id / "v1.json"
        original = version_path.read_bytes()
        legacy = library.show(self.root, self.case_id)
        self.assertEqual(legacy["model_family"], "unknown")
        self.assertEqual(legacy["review_status"], "needs_review")
        personal_path = self.root / "personal" / (self.case_id + ".json")
        library._write(personal_path, {"case_id": self.case_id, "corrections": {"artists": ["Personal artist"]}})
        personal_bytes = personal_path.read_bytes()
        with_metadata = dict(self.record, metadata={"model_family": "gpt-image", "model_version": "2",
                                                   "artists": ["Source artist"], "review_status": "verified"})
        result = self.ingest([with_metadata], "r2", mode="append")
        self.assertEqual(result["new_versions"], 0)
        self.assertEqual(version_path.read_bytes(), original)
        self.assertEqual(personal_path.read_bytes(), personal_bytes)
        self.assertEqual(library.show(self.root, self.case_id)["artists"], ["Personal artist"])
        self.assertEqual(library.query(self.root, artist="Personal artist")[0]["case_id"], self.case_id)
        self.assertEqual(library.query(self.root, artist="Source artist"), [])
        self.ingest([dict(self.record, prompt="second original")], "r3")
        self.assertEqual(library.show(self.root, self.case_id, "v1")["model_family"], "gpt-image")
        self.assertEqual(library.show(self.root, self.case_id, "v2")["model_family"], "unknown")

    def test_filters_source_evidence_and_missing_inputs_propagate(self):
        metadata = {"model_family": "midjourney", "model_version": "7", "artists": ["Artist A"],
                    "movements": ["Surrealism"], "materials": ["Paper"], "techniques": ["Collage"],
                    "record_type": "style_reference", "review_status": "missing_inputs",
                    "review_note": "The required input reference is absent", "evidence": ["source caption"],
                    "asset_roles": [{"index": 0, "role": "output", "reason": "source caption"}]}
        self.ingest([dict(self.record, metadata=metadata), dict(self.record, source_id="2")])
        for field, value in {"model_family": "midjourney", "artist": "artist a", "movement": "surrealism",
                             "material": "paper", "record_type": "style_reference", "review_status": "missing_inputs"}.items():
            results = library.query(self.root, **{field: value})
            self.assertEqual([item["case_id"] for item in results], [self.case_id])
            self.assertEqual(library.query(self.root, **{field: "not present"}), [])
        shown = library.show(self.root, self.case_id)
        catalog = next(item for item in library._read(self.root / "indexes/catalog.json")["cases"] if item["case_id"] == self.case_id)
        for item in (shown, catalog, library.query(self.root, review_status="missing_inputs")[0]):
            self.assertEqual(item["status"], "complete")
            self.assertEqual(item["effective_status"], "missing_inputs")
            self.assertEqual(item["record_type"], "style_reference")
            self.assertEqual(item["source_evidence"][0]["source_url"], self.record["source_url"])
        self.assertEqual(shown["assets"][0]["role"], "output")
        self.assertEqual(shown["assets"][0]["source_role"], "example")
        self.assertEqual(len(library.query(self.root, "Collage")), 1)
        self.assertEqual(len(library.query(self.root, "primary-evidence")), 2)
        self.assertIn("not visually assessed", library.query(self.root)[0]["matched_by"])

    def test_legacy_catalog_reads_new_sidecar_without_rebuild(self):
        self.ingest([self.record])
        library._write(library._metadata_path(self.root, self.case_id, "v1"),
                       {"model_family": "nano-banana", "review_status": "missing_inputs"})
        item = library.query(self.root, model_family="nano-banana")[0]
        self.assertEqual(item["effective_status"], "missing_inputs")
        self.assertEqual(item["case_id"], self.case_id)

    def test_standalone_markdown_shows_review_and_roles_without_changing_version(self):
        self.ingest([self.record])
        version_path = self.root / "cases" / self.case_id / "v1.json"
        markdown_path = version_path.with_suffix(".md")
        original = version_path.read_bytes()
        self.assertIn("\u672a\u8865\u5145\u590d\u6838", markdown_path.read_text(encoding="utf-8"))
        metadata = {"record_type": "style_reference", "review_status": "missing_inputs",
                    "review_note": "Required reference image was not provided by the source.",
                    "asset_roles": [{"index": 0, "role": "output", "reason": "Source labels the displayed result"}]}
        self.ingest([dict(self.record, metadata=metadata)], "r2", mode="append")
        markdown = markdown_path.read_text(encoding="utf-8")
        self.assertIn("\u7f3a\u5c11\u5fc5\u8981\u8f93\u5165", markdown)
        self.assertIn(metadata["review_note"], markdown)
        self.assertIn("\u98ce\u683c\u53c2\u8003", markdown)
        self.assertIn("\u8f93\u51fa\u6548\u679c\u56fe", markdown)
        self.assertIn("\u6765\u6e90\u89d2\u8272\uff1a`example`", markdown)
        self.assertIn(metadata["asset_roles"][0]["reason"], markdown)
        self.assertIn(self.record["prompt"], markdown)
        self.assertIn("(../../images/", markdown)
        self.assertEqual(version_path.read_bytes(), original)
        library._write(self.root / "personal" / (self.case_id + ".json"),
                       {"case_id": self.case_id, "corrections": {"review_note": "Personal verified explanation"}})
        library.rebuild(self.root)
        self.assertIn("Personal verified explanation", markdown_path.read_text(encoding="utf-8"))
        self.assertEqual(version_path.read_bytes(), original)

    def test_cli_filters_reach_query(self):
        self.ingest([dict(self.record, metadata={"model_family": "gpt-image", "materials": ["Ink"]})])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(library.main(["--root", str(self.root), "query", "--model-family", "gpt-image", "--material", "ink"]), 0)
        self.assertEqual(json.loads(output.getvalue())[0]["case_id"], self.case_id)

    def test_search_and_catalog_are_light_while_exact_case_keeps_full_evidence(self):
        long_prompt = "Only-in-original-prompt " + "original text " * 500
        long_evidence = "Sidecar searchable marker " + "full evidence " * 500
        long_note = "Detailed review " * 100
        metadata = {"model_family": "midjourney", "artists": ["Artist"],
                    "evidence": [{"excerpt": long_evidence}], "review_note": long_note,
                    "asset_roles": [{"index": 0, "role": "output", "reason": long_evidence}]}
        record = dict(self.record, prompt=long_prompt, metadata=metadata,
                      source_metadata={"original_prompt": long_prompt},
                      prompt_variants={"en": long_prompt + " English variant"})
        self.ingest([record])
        catalog_item = library._read(self.root / "indexes/catalog.json")["cases"][0]
        search_item = library.query(self.root, "Sidecar searchable marker", artist="Artist")[0]
        full_text_item = library.query(self.root, "Only-in-original-prompt", full_text=True)[0]
        self.assertEqual(library.query(self.root, "Only-in-original-prompt"), [])
        for item in (catalog_item, search_item, full_text_item):
            for field in ("evidence", "asset_roles", "review_note", "prompt", "prompt_variants"):
                self.assertNotIn(field, item)
            self.assertLessEqual(len(item["review_summary"]), 240)
            self.assertEqual(item["metadata_path"], "metadata/cases/" + self.case_id + "/v1.json")
            self.assertEqual(set(item["source_evidence"][0]),
                             {"source", "source_id", "source_url", "upstream_url", "revision", "version"})
            self.assertNotIn(long_prompt, json.dumps(item))
            self.assertNotIn(long_evidence, json.dumps(item))
        detail = library.show(self.root, self.case_id)
        self.assertEqual(detail["prompt"], long_prompt)
        self.assertEqual(detail["evidence"], metadata["evidence"])
        self.assertEqual(detail["review_note"], long_note)
        self.assertEqual(detail["asset_roles"], metadata["asset_roles"])
        self.assertEqual(detail["source_evidence"][0]["source_metadata"]["original_prompt"], long_prompt)

    def test_invalid_metadata_rejected_before_writes(self):
        with self.assertRaises(ValueError):
            self.ingest([dict(self.record, metadata={"model_family": "invented"})])
        self.assertFalse(self.root.exists())
        with self.assertRaises(ValueError):
            self.ingest([self.record], mode="partial")

    def test_prompt_variants_version_search_and_group_preserve_originals(self):
        first_variants = {"original": self.record["prompt"], "en": "English archival phrase\r\nKeep ``` formatting.  "}
        first = dict(self.record, prompt_variants=first_variants)
        self.ingest([first])
        original_bytes = (self.root / "cases" / self.case_id / "v1.json").read_bytes()
        second = dict(self.record, prompt_variants={**first_variants, "en": "English revised wording"})
        result = self.ingest([second], "r2")
        self.assertEqual(result["new_versions"], 1)
        old = library.show(self.root, self.case_id, "v1")
        new = library.show(self.root, self.case_id, "v2")
        self.assertEqual(old["prompt"], new["prompt"])
        self.assertEqual(old["prompt_variants"], first_variants)
        self.assertEqual(new["prompt_variants"]["en"], "English revised wording")
        self.assertIn("original prompt variants changed", new["changes"])
        self.assertEqual((self.root / "cases" / self.case_id / "v1.json").read_bytes(), original_bytes)
        self.assertEqual(library.query(self.root, "revised wording"), [])
        self.assertEqual(library.query(self.root, "revised wording", full_text=True)[0]["case_id"], self.case_id)
        markdown = (self.root / "cases" / self.case_id / "v1.md").read_bytes().decode("utf-8")
        self.assertIn(first_variants["en"], markdown)
        self.assertIn("````text", markdown)
        self.assertEqual(markdown.count(self.record["prompt"]), 1)

        legacy = dict(self.record, source_id="legacy", prompt="Legacy unchanged")
        self.ingest([legacy], mode="append")
        legacy_id = library.stable_case_id("fixture", "legacy")
        legacy_path = self.root / "cases" / legacy_id / "v1.json"
        legacy_bytes = legacy_path.read_bytes()
        self.assertEqual(self.ingest([legacy], mode="append")["new_versions"], 0)
        self.assertEqual(legacy_path.read_bytes(), legacy_bytes)
        self.assertNotIn("prompt_variants", library.show(self.root, legacy_id))
        mapping = library.group_variant(self.root, self.case_id, legacy_id)
        for version in ("v1", "v2"):
            resolved = library.show(self.root, self.case_id, version)
            self.assertEqual(resolved["version"], mapping["versions"][version])
            self.assertEqual(resolved["prompt_variants"], first["prompt_variants"] if version == "v1" else second["prompt_variants"])
        self.assertTrue(library.validate(self.root)["ok"])


if __name__ == "__main__":
    unittest.main()
