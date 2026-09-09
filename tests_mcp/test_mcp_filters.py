"""MCP filter schema and archive retrieval agree on metadata and review state."""
import asyncio
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import library
from mcp_library import LibrarySnapshot, create_server


class MCPFiltersTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "archive"
        image = Path(self.temp.name) / "image.png"
        image.write_bytes(b"image fixture")
        record = {"source_id": "1", "title": "Reference", "prompt": "Original prompt",
                  "assets": [{"path": str(image)}], "metadata": {
                      "model_family": "nano-banana", "artists": ["Artist"], "movements": ["Modernism"],
                      "materials": ["Glass"], "record_type": "case", "review_status": "missing_inputs"}}
        library.import_records(self.root, [record], "fixture", "r1")
        self.case_id = library.stable_case_id("fixture", "1")
        self.snapshot = LibrarySnapshot(self.root, "a" * 40, "https://github.com/example/library")

    def test_filters_return_retrievable_missing_input_case(self):
        filters = {"model_family": "nano-banana", "artist": "Artist", "movement": "Modernism",
                   "material": "Glass", "record_type": "case", "review_status": "missing_inputs"}
        result = self.snapshot.search_cases(**filters)
        self.assertEqual(result["returned"], 1)
        self.assertEqual(result["cases"][0]["case_id"], self.case_id)
        self.assertEqual(result["cases"][0]["effective_status"], "missing_inputs")
        shown = self.snapshot.get_case(self.case_id)["case"]
        self.assertEqual(shown["effective_status"], "missing_inputs")
        self.assertFalse(result["visual_match_verified"])
        self.assertEqual(self.snapshot.search_cases(material="Bronze")["returned"], 0)

    def test_registered_tool_exposes_and_forwards_filters(self):
        server = create_server(self.root, "a" * 40, "https://github.com/example/library")
        definitions = asyncio.run(server.list_tools())
        search = next(item for item in definitions if item.name == "search_cases")
        for field in ("model_family", "artist", "movement", "material", "record_type", "review_status"):
            self.assertIn(field, search.inputSchema["properties"])
        response = asyncio.run(server.call_tool("search_cases", {"material": "Bronze"}))
        self.assertEqual(json.loads(response[0].text)["returned"], 0)


if __name__ == "__main__":
    unittest.main()
