"""Behavioral archive tests; fixtures are tiny files, not generated visual claims."""

import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "library.py"
SPEC = importlib.util.spec_from_file_location("visual_library", SCRIPT)
library = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(library)


class LibraryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / "library"
        self.asset = self.base / "original.png"
        self.asset.write_bytes(b"original image bytes\x00\xff")
        self.other_asset = self.base / "other.png"
        self.other_asset.write_bytes(b"changed image bytes\x00\xff")
        self.record = {
            "source_id": "7", "title": "A portrait", "prompt": "line one\r\n\u4eba\u50cf\n\nKeep `details`.  ",
            "category": "Characters", "styles": ["Warm"], "scenes": ["Interior"],
            "source_url": "https://example.test/post/7", "upstream_url": "https://example.test/gallery",
            "model": None, "parameters": None,
            "assets": [{"path": str(self.asset), "role": "output", "source_path": "images/original.png"}],
            "missing_assets": [],
        }

    def tearDown(self):
        self.temp.cleanup()

    def ingest(self, record=None, source="upstream", revision="r1"):
        return library.import_records(self.root, [record or self.record], source, revision)

    def get_id(self):
        return library.stable_case_id("upstream", "7")

    def snapshot(self):
        return {p.relative_to(self.root).as_posix(): (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
                for p in self.root.rglob("*") if p.is_file()}

    def test_exact_original_versions_and_repeat_noop(self):
        first = self.ingest()
        self.assertEqual(first["new_cases"], 1)
        original = library.show(self.root, self.get_id(), "v1")
        self.assertEqual(original["prompt"], self.record["prompt"])
        before = self.snapshot()
        result = self.ingest()
        self.assertEqual(result["duplicates"], 1)
        self.assertEqual(self.snapshot(), before)
        changed = dict(self.record, prompt="completely revised\n\u4e2d\u6587")
        result = self.ingest(changed, revision="r2")
        self.assertEqual(result["new_versions"], 1)
        self.assertEqual(library.show(self.root, self.get_id(), "v1")["prompt"], self.record["prompt"])
        self.assertEqual(library.show(self.root, self.get_id(), "v2")["prompt"], changed["prompt"])
        self.assertEqual(len(list((self.root / "images").iterdir())), 1)
        self.assertTrue(library.validate(self.root)["ok"])

    def test_same_prompt_different_image_creates_version(self):
        self.ingest()
        changed = copy.deepcopy(self.record)
        changed["assets"][0]["path"] = str(self.other_asset)
        self.assertEqual(self.ingest(changed, revision="r2")["new_versions"], 1)
        one = library.show(self.root, self.get_id(), "v1")
        two = library.show(self.root, self.get_id(), "v2")
        self.assertEqual(one["prompt"], two["prompt"])
        self.assertNotEqual(one["assets"][0]["sha256"], two["assets"][0]["sha256"])
        self.assertEqual(len(list((self.root / "images").iterdir())), 2)

    def test_cross_source_unknown_keeps_cases_and_reuses_bytes(self):
        self.ingest()
        result = self.ingest(source="another")
        self.assertEqual(result["new_cases"], 1)
        self.assertEqual(result["shared_assets"], 1)
        self.assertEqual(len(library.query(self.root)), 2)
        self.assertEqual(len(list((self.root / "images").iterdir())), 1)
        candidates = library.similar_candidates(self.root)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["shared_assets"], 1)

    def test_cross_source_complete_identity_retains_aliases(self):
        known = dict(self.record, model="explicit-model", parameters={"seed": 2})
        self.ingest(known)
        copied = dict(known, title="Another title", source_url="https://second.test/7")
        result = self.ingest(copied, source="another")
        self.assertEqual(result["duplicates"], 1)
        self.assertEqual(len(library.query(self.root)), 1)
        retrieved = library.show(self.root, self.get_id())
        self.assertEqual({x["source"] for x in retrieved["source_aliases"]}, {"upstream", "another"})
        self.assertEqual(retrieved["available_versions"], ["v1"])

    def test_metadata_and_personal_edits_do_not_create_version(self):
        self.ingest()
        library.personal_note(self.root, self.get_id(), "my observation", "v1")
        library.personal_favorite(self.root, self.get_id(), "v1")
        personal_path = self.root / "personal" / (self.get_id() + ".json")
        personal_before = personal_path.read_bytes()
        changed = dict(self.record, title="Better heading", category="Photography", source_url="https://example.test/renamed")
        result = self.ingest(changed, revision="r2")
        self.assertEqual(result["duplicates"], 1)
        self.assertEqual(personal_path.read_bytes(), personal_before)
        self.assertEqual(library.query(self.root)[0]["title"], "Better heading")
        self.assertEqual(library.show(self.root, self.get_id())["available_versions"], ["v1"])
        self.ingest(dict(changed, prompt="new content"), revision="r3")
        self.assertEqual(library.query(self.root, favorites=True)[0]["favorite_versions"], ["v1"])

    def test_missing_assets_never_replace_complete_and_stable_retry(self):
        self.ingest()
        changed = dict(self.record, prompt="new incomplete content", missing_assets=["reference.png"])
        result = self.ingest(changed, revision="r2")
        self.assertEqual(result["pending"], 1)
        self.assertEqual(library.show(self.root, self.get_id())["version"], "v1")
        state = library._read(self.root / "sources" / "upstream" / "state.json")
        self.assertEqual(state["last_success_revision"], "r1")
        self.assertEqual(state["last_attempt_revision"], "r2")
        before = self.snapshot()
        self.ingest(changed, revision="r2")
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(library.validate(self.root)["pending"], 1)
        completed = dict(changed, missing_assets=[])
        self.ingest(completed, revision="r3")
        self.assertEqual(library.show(self.root, self.get_id())["version"], "v2")
        self.assertEqual(library.validate(self.root)["pending"], 0)

    def test_missing_input_file_and_prompt_pending(self):
        broken = copy.deepcopy(self.record)
        broken["assets"][0]["path"] = str(self.base / "not-found.png")
        broken["prompt"] = ""
        result = self.ingest(broken)
        self.assertEqual(result["pending"], 1)
        self.assertEqual(library.query(self.root), [])
        self.assertIsNone(library._read(self.root / "sources" / "upstream" / "state.json")["last_success_revision"])

    def test_source_empty_snapshot_preserves_offline_archive(self):
        self.ingest()
        library.import_records(self.root, [], "upstream", "r2")
        state = library._read(self.root / "sources" / "upstream" / "state.json")
        self.assertFalse(state["mappings"]["7"]["upstream_present"])
        self.asset.unlink()
        content = library.show(self.root, self.get_id(), "v1")
        self.assertTrue(Path(content["assets"][0]["absolute_path"]).is_file())
        self.assertEqual(len(library.query(self.root)), 1)
        self.assertTrue(library.validate(self.root)["ok"])

    def test_read_operations_are_byte_and_mtime_readonly(self):
        self.ingest()
        before = self.snapshot()
        library.query(self.root, keywords="portrait")
        library.query(self.root, keywords="details", full_text=True)
        library.show(self.root, self.get_id(), "v1")
        library.similar_candidates(self.root)
        self.assertTrue(library.validate(self.root)["ok"])
        self.assertEqual(before, self.snapshot())
        catalog = (self.root / "indexes" / "catalog.json").read_text(encoding="utf-8")
        self.assertNotIn("line one", catalog)
        self.assertNotIn('"prompt"', catalog)

    def test_default_keyword_search_includes_personal_notes_without_writing(self):
        self.ingest()
        self.assertEqual(library.query(self.root, "unique-observation"), [])
        library.personal_note(self.root, self.get_id(), "unique-observation for future use", "v1")
        before = self.snapshot()
        self.assertEqual(len(library.query(self.root, "unique-observation")), 1)
        self.assertEqual(before, self.snapshot())

    def test_alias_classification_and_fulltext_are_explicit(self):
        path = self.root / "metadata" / "taxonomy.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"aliases": {"Characters": "Character", "portrait-tag": "Character"}}), encoding="utf-8")
        self.ingest()
        self.assertEqual(len(library.query(self.root, category="Characters", tags=["portrait-tag"])), 1)
        self.assertEqual(library.query(self.root, keywords="details"), [])
        self.assertEqual(len(library.query(self.root, keywords="details", full_text=True)), 1)
        self.assertEqual(library.query(self.root)[0]["source_category"], "Characters")

    def test_corrupt_old_asset_detected_even_with_new_good_version(self):
        self.ingest()
        old = library.show(self.root, self.get_id(), "v1")
        changed = copy.deepcopy(self.record)
        changed["assets"][0]["path"] = str(self.other_asset)
        self.ingest(changed, revision="r2")
        Path(old["assets"][0]["absolute_path"]).write_bytes(b"corrupt")
        report = library.validate(self.root)
        self.assertFalse(report["ok"])
        self.assertTrue(any("mismatch" in e for e in report["errors"]))

    def test_chinese_alias_searches_english_only_prompt_with_grouped_terms(self):
        path = self.root / "metadata" / "taxonomy.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"aliases": {"warm": "\u6696\u8272", "golden": "\u6696\u8272", "portrait": "\u4eba\u50cf"}}), encoding="utf-8")
        record = dict(self.record, title="Example", styles=[], scenes=[], category="Example",
                      prompt="A golden portrait with natural sunlight.")
        self.ingest(record)
        self.assertEqual(library.query(self.root, keywords="\u6696\u8272 \u4eba\u50cf"), [])
        self.assertEqual(len(library.query(self.root, keywords="\u6696\u8272 \u4eba\u50cf", full_text=True)), 1)
        self.assertEqual(len(library.query(self.root, keywords="warm portrait", full_text=True)), 1)
        self.assertEqual(library.query(self.root, keywords="\u6696\u8272 ocean", full_text=True), [])

    def test_cross_process_versions_do_not_collide(self):
        self.ingest()
        processes = []
        for i in range(4):
            record = dict(self.record, prompt="parallel independent change " + str(i))
            path = self.base / ("batch" + str(i) + ".json")
            path.write_text(json.dumps([record]), encoding="utf-8")
            processes.append(subprocess.Popen([sys.executable, "-B", str(SCRIPT), "--root", str(self.root), "import", "--records", str(path), "--source", "upstream", "--revision", "r" + str(i + 2)], stdout=subprocess.PIPE, stderr=subprocess.PIPE))
        for process in processes:
            out, err = process.communicate(timeout=40)
            self.assertEqual(process.returncode, 0, (out, err))
        current = library.show(self.root, self.get_id())
        self.assertEqual(current["available_versions"], ["v1", "v2", "v3", "v4", "v5"])
        actual = {library.show(self.root, self.get_id(), version)["prompt"] for version in current["available_versions"]}
        self.assertEqual(actual, {self.record["prompt"], *("parallel independent change " + str(i) for i in range(4))})
        self.assertTrue(library.validate(self.root)["ok"])
        self.assertFalse((self.root / ".library.lock").exists())

    def test_explicit_group_preserves_old_id_versions_and_personal_files(self):
        self.ingest()
        alternative = dict(self.record, prompt="a deliberate variation")
        second = self.ingest(alternative, source="another")["records"][0]["case_id"]
        library.personal_note(self.root, second, "note on other version", "v1")
        personal_path = self.root / "personal" / (second + ".json")
        personal_bytes = personal_path.read_bytes()
        result = library.group_variant(self.root, second, self.get_id())
        self.assertEqual(result["versions"], {"v1": "v2"})
        self.assertEqual(personal_path.read_bytes(), personal_bytes)
        old_ref = library.show(self.root, second, "v1")
        self.assertEqual(old_ref["version"], "v2")
        self.assertEqual(old_ref["prompt"], alternative["prompt"])
        self.assertEqual(old_ref["personal"]["notes"][0]["version"], "v2")
        self.assertEqual(len(library.query(self.root)), 1)
        self.assertTrue((self.root / "cases" / second / "v1.json").is_file())
        self.assertTrue(library.validate(self.root)["ok"])
        followup = dict(alternative, prompt="new upstream change after grouping")
        self.ingest(followup, source="another", revision="r2")
        self.assertEqual(library.show(self.root, self.get_id())["version"], "v3")
        self.assertEqual(library.show(self.root, second, "v1")["version"], "v2")

    def test_unknown_equal_content_stays_distinct_when_explicitly_grouped(self):
        self.ingest()
        second = self.ingest(source="another")["records"][0]["case_id"]
        result = library.group_variant(self.root, second, self.get_id())
        self.assertEqual(result["versions"], {"v1": "v2"})
        self.assertEqual(library.show(self.root, self.get_id())["available_versions"], ["v1", "v2"])

    def test_unfavorite_after_grouping_overrides_preserved_old_personal_record(self):
        self.ingest()
        second = self.ingest(dict(self.record, prompt="variant"), source="another")["records"][0]["case_id"]
        library.personal_favorite(self.root, second, "v1")
        path = self.root / "personal" / (second + ".json")
        before = path.read_bytes()
        library.group_variant(self.root, second, self.get_id())
        self.assertEqual(library.query(self.root, favorites=True)[0]["favorite_versions"], ["v2"])
        library.personal_favorite(self.root, second, "v1", value=False)
        self.assertEqual(library.query(self.root, favorites=True), [])
        self.assertEqual(path.read_bytes(), before)

    def test_reference_path_and_immutable_no_clobber(self):
        self.ingest()
        with self.assertRaises(ValueError):
            library.show(self.root, "../../escape", "v1")
        with self.assertRaises(ValueError):
            library._safe_asset(self.root, "images/../../escape.png")
        path = self.root / "cases" / self.get_id() / "v1.json"
        before = path.read_bytes()
        with self.assertRaises(FileExistsError):
            library._write(path, b"replacement", immutable=True)
        self.assertEqual(path.read_bytes(), before)

    def test_validate_detects_dropped_catalog_case_and_rebuild_repairs(self):
        self.ingest()
        path = self.root / "indexes" / "catalog.json"
        library._write(path, {"cases": []})
        self.assertFalse(library.validate(self.root)["ok"])
        library.rebuild(self.root)
        self.assertTrue(library.validate(self.root)["ok"])

    def test_outer_adapter_lock_can_cover_import_and_source_documents(self):
        with library.library_lock(self.root):
            result = library.import_records(self.root, [self.record], "upstream", "r1", lock=False)
            self.assertEqual(result["new_cases"], 1)
            self.assertTrue((self.root / ".library.lock").exists())
        self.assertFalse((self.root / ".library.lock").exists())
        self.assertTrue(library.validate(self.root)["ok"])


if __name__ == "__main__":
    unittest.main()
