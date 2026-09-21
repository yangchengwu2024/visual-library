import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import visual_library_mcp_gateway as gateway


class FakeStdin:
    def __init__(self, owner, output_queue):
        self.owner = owner
        self.output_queue = output_queue
        self.writes = []

    def write(self, value):
        self.writes.append(value)
        request = json.loads(value.decode("utf-8"))
        if str(request.get("id", "")).startswith("__visual_library_gateway_"):
            self.output_queue.put(
                (json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": {}}) + "\n").encode("utf-8")
            )
        return len(value)

    def flush(self):
        return None


class FakeChild:
    def __init__(self, output_queue):
        self.stdout = FakeStdout()
        self.stdin = FakeStdin(self, output_queue)
        self.returncode = None

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = -15

    def wait(self, timeout=None):
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def kill(self):
        self.returncode = -9


class FakeStdout:
    def __iter__(self):
        return iter(())

    def close(self):
        return None


class VisualLibraryGatewayTest(unittest.TestCase):
    def test_marker_change_replaces_worker_and_replays_handshake(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data_marker = root / "installed-commit.txt"
            code_marker = root / "installed-code-revision.txt"
            data_marker.write_text("a" * 40 + "\n", encoding="utf-8")
            code_marker.write_text("b" * 40 + "\n", encoding="utf-8")
            children = []

            def fake_popen(*args, **kwargs):
                child = FakeChild(test_gateway.output_queue)
                children.append(child)
                return child

            test_gateway = gateway.Gateway()
            test_gateway.start()
            with patch.object(gateway, "INSTALL_ROOT", root), \
                    patch.object(gateway, "WORKER", root / "serve_mcp.py"), \
                    patch.object(gateway, "DATA_MARKER", data_marker), \
                    patch.object(gateway, "CODE_MARKER", code_marker), \
                    patch.object(gateway.subprocess, "Popen", side_effect=fake_popen):
                initialize = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
                test_gateway.handle(initialize, (json.dumps(initialize) + "\n").encode("utf-8"))
                self.assertEqual(len(children), 1)
                self.assertEqual(json.loads(children[0].stdin.writes[0])["id"], 1)

                initialized = {"jsonrpc": "2.0", "method": "notifications/initialized"}
                test_gateway.handle(initialized, (json.dumps(initialized) + "\n").encode("utf-8"))
                self.assertEqual(len(children[0].stdin.writes), 2)

                data_marker.write_text("c" * 40 + "\n", encoding="utf-8")
                tool_call = {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "list_catalog", "arguments": {}},
                }
                test_gateway.handle(tool_call, (json.dumps(tool_call) + "\n").encode("utf-8"))

                self.assertEqual(len(children), 2)
                replayed = [json.loads(value) for value in children[1].stdin.writes]
                self.assertTrue(str(replayed[0]["id"]).startswith("__visual_library_gateway_"))
                self.assertEqual(replayed[1]["method"], "notifications/initialized")
                self.assertEqual(replayed[2]["id"], 2)
            test_gateway.stopping = True
            test_gateway._stop_child()
            test_gateway.output_queue.put(None)


if __name__ == "__main__":
    unittest.main()
