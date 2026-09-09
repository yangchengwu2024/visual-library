"""Markdown navigation coverage, safe linking, and no-op generation tests."""

import hashlib
import importlib.util
import json
from pathlib import Path
import re
import tempfile
import unittest
from urllib.parse import unquote


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "gallery.py"
SPEC = importlib.util.spec_from_file_location("visual_gallery", SCRIPT)
gallery = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gallery)


class GalleryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.write("README.md", "# Fixture repository")

    def tearDown(self):
        self.temp.cleanup()

    def write(self, relative, data):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, dict):
            data = json.dumps(data, ensure_ascii=False)
        path.write_text(data, encoding="utf-8")
        return path

    def fixture(self, count=205):
        categories = [{"id": "cat-" + str(i), "value": "Category " + str(i),
                       "title": {"zh": "\u5206\u7c7b " + str(i)}} for i in range(13)]
        self.write("metadata/taxonomy.json", {"categories": categories})
        entries = []
        for number in range(1, count + 1):
            digest = hashlib.sha256(("source\0" + str(number)).encode()).hexdigest()[:12]
            case_id = "case-source-" + str(number) + "-" + digest
            version = "v" + str(number % 3 + 1)
            entry = {"case_id": case_id, "version": version, "title": "\u4e2d\u6587\u6807\u9898 " + str(number),
                     "categories": ["\u5206\u7c7b " + str(number % 4)], "sources": ["source"],
                     "archived_at": "2026-09-09T03:00:00+00:00"}
            entries.append(entry)
            self.write("cases/" + case_id + "/" + version + ".md", "# Fixture")
        return {"case_count": count, "cases": list(reversed(entries))}

    def links(self, path):
        return re.findall(r"\]\(([^\n)]+)\)", path.read_text(encoding="utf-8"))

    def assert_all_links_exist(self):
        for path in (self.root / "docs").rglob("*.md"):
            for link in self.links(path):
                target, _, anchor = link.partition("#")
                self.assertFalse(target.startswith(("/", "http:", "https:")), (path, link))
                local = (path.parent / unquote(target)).resolve()
                self.assertTrue(local.is_relative_to(self.root), (path, link))
                self.assertTrue(local.is_file(), (path, link))
                if anchor:
                    text = local.read_text(encoding="utf-8")
                    self.assertRegex(text, r'(?:id|name)=["\x27]' + re.escape(unquote(anchor)) + r'["\x27]')

    def snapshot(self):
        return {p.relative_to(self.root).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns)
                for p in self.root.rglob("*") if p.is_file()}

    def test_all_cases_are_linked_exactly_once_across_bounded_parts(self):
        catalog = self.fixture()
        result = gallery.build_gallery(self.root, catalog)
        self.assertEqual((result["cases"], result["parts"], result["categories"]), (205, 3, 13))
        parts = sorted((self.root / "docs").glob("gallery-part-*.md"))
        counts, targets = [], []
        for path in parts:
            case_links = [link for link in self.links(path) if link.startswith("../cases/")]
            counts.append(len(case_links))
            targets.extend(case_links)
        self.assertEqual(counts, [100, 100, 5])
        self.assertEqual(len(targets), len(set(targets)))
        expected = {"../cases/" + e["case_id"] + "/" + e["version"] + ".md" for e in catalog["cases"]}
        self.assertEqual(set(targets), expected)
        overview = self.root / "docs" / "gallery.md"
        self.assertLessEqual(sum(link.startswith("../cases/") for link in self.links(overview)), 12)
        self.assertNotIn("![", overview.read_text(encoding="utf-8"))
        self.assert_all_links_exist()

    def test_each_category_covers_its_cases_and_keeps_empty_taxonomy_categories(self):
        catalog = self.fixture(17)
        gallery.build_gallery(self.root, catalog)
        for i in range(13):
            path = self.root / "docs" / "categories" / ("cat-" + str(i) + ".md")
            actual = {link for link in self.links(path) if link.startswith("../../cases/")}
            expected = {"../../cases/" + e["case_id"] + "/" + e["version"] + ".md" for e in catalog["cases"] if "\u5206\u7c7b " + str(i) in e["categories"]}
            self.assertEqual(actual, expected)

    def test_templates_link_local_cover_and_exact_archived_anchor(self):
        catalog = self.fixture(1)
        document = "sources/example/a folder/templates (original).md"
        self.write(document, '<a name="tpl-test"></a>\n# Full original template\n')
        self.write("images/cover (example).png", "fixture")
        self.write("indexes/templates.json", {"document": document, "templates": [{
            "id": "test", "title": {"zh": "\u4e2d\u6587 <script> [title] (demo)", "en": "English title"},
            "description": {"zh": "\u4e2d\u6587\u7b80\u4ecb & \u7ed3\u6784"}, "cover": "images/cover (example).png", "anchor": "tpl-test"}]})
        gallery.build_gallery(self.root, catalog)
        text = (self.root / "docs" / "templates.md").read_text(encoding="utf-8")
        self.assertIn("\u4e2d\u6587", text)
        self.assertNotIn("<script>", text)
        self.assertIn("&lt;script&gt;", text)
        self.assertIn("%20", text)
        self.assertIn("%28", text)
        self.assertIn("#tpl-test", text)
        self.assertNotIn("Full original template", text)
        self.assert_all_links_exist()

    def test_repeat_is_byte_and_mtime_noop_and_leaves_unrelated_docs_intact(self):
        catalog = self.fixture(5)
        personal = self.write("docs/personal-notes.md", "Do not edit this document.")
        original = personal.read_bytes()
        gallery.build_gallery(self.root, catalog)
        before = self.snapshot()
        result = gallery.build_gallery(self.root, catalog)
        self.assertEqual(result["written"], [])
        self.assertEqual(before, self.snapshot())
        self.assertEqual(personal.read_bytes(), original)

    def test_empty_library_without_templates_builds(self):
        result = gallery.build_gallery(self.root, {"cases": []})
        self.assertEqual((result["cases"], result["parts"], result["templates"]), (0, 0, 0))
        self.assert_all_links_exist()

    def test_non_generated_collision_is_refused_before_other_pages_are_written(self):
        catalog = self.fixture(2)
        path = self.write("docs/gallery.md", "A handwritten overview.")
        before = self.snapshot()
        with self.assertRaises(FileExistsError):
            gallery.build_gallery(self.root, catalog)
        self.assertEqual(path.read_text(encoding="utf-8"), "A handwritten overview.")
        self.assertEqual(before, self.snapshot())

    def test_case_id_fallback_does_not_invent_source_numbers(self):
        entry = {"case_id": "case-source-12-000000000000", "version": "v1", "title": "Title", "sources": ["source"]}
        link = gallery._case_link(entry, "docs/gallery.md")
        self.assertIn(entry["case_id"], link)
        self.assertNotIn("source \\#12", link)

    def test_recent_same_day_uses_source_number_before_import_seconds(self):
        catalog = self.fixture(15)
        catalog["cases"][-1]["archived_at"] = "2026-09-09T23:00:00+00:00"
        gallery.build_gallery(self.root, catalog)
        overview = self.root / "docs" / "gallery.md"
        links = [link for link in self.links(overview) if link.startswith("../cases/")]
        self.assertIn("case-source-15-", links[0])
        self.assertEqual(len(links), 12)


if __name__ == "__main__":
    unittest.main()
