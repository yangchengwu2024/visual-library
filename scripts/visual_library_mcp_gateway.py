"""Keep the visual-library MCP connection stable while replacing its worker.

Codex owns this process through stdio.  The gateway owns a short-lived
``serve_mcp.py`` worker and checks deployment markers before each request that
can touch library data.  When code or data changes, the worker is replaced and
the MCP handshake is replayed internally; the outer Codex connection remains
unchanged.

This is deliberately a small protocol-preserving wrapper, similar to the
Blender MCP gateway.  It does not edit snapshots or bypass the verification
performed by ``serve_mcp.py``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
from typing import Any


INSTALL_ROOT = Path(__file__).resolve().parents[1]
WORKER = INSTALL_ROOT / "scripts" / "serve_mcp.py"
DATA_MARKER = INSTALL_ROOT / "installed-commit.txt"
CODE_MARKER = INSTALL_ROOT / "installed-code-revision.txt"
HANDSHAKE_TIMEOUT_SECONDS = float(os.environ.get("VISUAL_LIBRARY_HANDSHAKE_TIMEOUT_SECONDS", "30"))


def _read_marker(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _deployment_key() -> tuple[str, str]:
    return _read_marker(DATA_MARKER), _read_marker(CODE_MARKER)


class Gateway:
    def __init__(self) -> None:
        self.child: subprocess.Popen[bytes] | None = None
        self.child_lock = threading.RLock()
        self.output_queue: queue.Queue[bytes | None] = queue.Queue()
        self.parent_write_lock = threading.Lock()
        self.waiter_lock = threading.Lock()
        self.waiters: dict[Any, tuple[threading.Event, dict[str, Any]]] = {}
        self.sequence = 0
        self.initialize_request: dict[str, Any] | None = None
        self.initialized_notification: dict[str, Any] | None = None
        self.current_deployment = ("", "")
        self.stopping = False

    def log(self, message: str) -> None:
        sys.stderr.write(f"visual-library gateway: {message}\n")
        sys.stderr.flush()

    def start(self) -> None:
        threading.Thread(target=self._dispatch_output, name="visual-library-output", daemon=True).start()

    def _dispatch_output(self) -> None:
        while not self.stopping:
            raw_line = self.output_queue.get()
            if raw_line is None:
                return
            try:
                message = json.loads(raw_line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._write_parent(raw_line)
                continue

            message_id = message.get("id") if isinstance(message, dict) else None
            if message_id is not None:
                with self.waiter_lock:
                    waiter = self.waiters.get(message_id)
                if waiter is not None:
                    event, result = waiter
                    result["message"] = message
                    event.set()
                    continue
            self._write_parent(raw_line)

    def _write_parent(self, raw_line: bytes) -> None:
        with self.parent_write_lock:
            sys.stdout.buffer.write(raw_line)
            if not raw_line.endswith(b"\n"):
                sys.stdout.buffer.write(b"\n")
            sys.stdout.buffer.flush()

    def _read_child(self, child: subprocess.Popen[bytes]) -> None:
        assert child.stdout is not None
        try:
            for raw_line in child.stdout:
                self.output_queue.put(raw_line)
        finally:
            try:
                child.stdout.close()
            except OSError:
                pass

    def _start_child(self) -> None:
        with self.child_lock:
            if self.child is not None and self.child.poll() is None:
                return
            environment = dict(os.environ)
            environment.setdefault("PYTHONDONTWRITEBYTECODE", "1")
            child = subprocess.Popen(
                [sys.executable, "-B", str(WORKER)],
                cwd=str(INSTALL_ROOT),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=None,
                env=environment,
                bufsize=0,
            )
            self.child = child
            self.current_deployment = _deployment_key()
            threading.Thread(target=self._read_child, args=(child,), name="visual-library-worker-output", daemon=True).start()
            self.log(f"worker started for deployment {self.current_deployment}")

    def _stop_child(self) -> None:
        with self.child_lock:
            child = self.child
            self.child = None
            if child is None or child.poll() is not None:
                return
            try:
                child.terminate()
                child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=3)
            except OSError:
                pass

    def _write_child(self, raw_line: bytes) -> None:
        with self.child_lock:
            child = self.child
            if child is None or child.poll() is not None or child.stdin is None:
                raise RuntimeError("visual-library MCP worker is not running")
            try:
                child.stdin.write(raw_line)
                if not raw_line.endswith(b"\n"):
                    child.stdin.write(b"\n")
                child.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                raise RuntimeError("visual-library MCP worker stopped while handling a request") from exc

    def _next_internal_id(self) -> str:
        self.sequence += 1
        return f"__visual_library_gateway_{self.sequence}"

    def _internal_request(self, request: dict[str, Any]) -> dict[str, Any]:
        request_id = request["id"]
        event = threading.Event()
        result: dict[str, Any] = {}
        with self.waiter_lock:
            self.waiters[request_id] = (event, result)
        try:
            self._write_child((json.dumps(request, ensure_ascii=False) + "\n").encode("utf-8"))
            if not event.wait(HANDSHAKE_TIMEOUT_SECONDS):
                raise RuntimeError("visual-library MCP worker did not complete its internal handshake")
            message = result.get("message")
            if not isinstance(message, dict):
                raise RuntimeError("visual-library MCP worker returned an invalid handshake response")
            if "error" in message:
                raise RuntimeError("visual-library MCP worker rejected its internal handshake: " + json.dumps(message["error"], ensure_ascii=False))
            return message
        finally:
            with self.waiter_lock:
                self.waiters.pop(request_id, None)

    def _replay_handshake(self) -> None:
        if self.initialize_request is None:
            return
        replay = dict(self.initialize_request)
        replay["id"] = self._next_internal_id()
        self._internal_request(replay)
        if self.initialized_notification is not None:
            self._write_child((json.dumps(self.initialized_notification, ensure_ascii=False) + "\n").encode("utf-8"))

    def _ensure_worker(self, *, replay: bool = True) -> bool:
        """Ensure a worker for the current deployment; return whether it was replaced."""
        desired = _deployment_key()
        with self.child_lock:
            child = self.child
            alive = child is not None and child.poll() is None
            current = self.current_deployment
        if not alive:
            self._start_child()
            if replay and self.initialize_request is not None:
                self._replay_handshake()
            return True
        if desired != current:
            self.log(f"deployment changed from {current} to {desired}; replacing worker")
            self._stop_child()
            self._start_child()
            if replay and self.initialize_request is not None:
                self._replay_handshake()
            return True
        return False

    def _send_error(self, request: dict[str, Any], message: str) -> None:
        if "id" not in request:
            return
        response = {
            "jsonrpc": "2.0",
            "id": request["id"],
            "error": {"code": -32001, "message": message},
        }
        self._write_parent((json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8"))

    def handle(self, request: dict[str, Any], raw_line: bytes) -> None:
        method = request.get("method")
        try:
            if method == "initialize":
                # The first handshake belongs to the parent.  Do not replay it
                # internally; forward this exact request and its response.
                self.initialize_request = dict(request)
                self._ensure_worker(replay=False)
                self._write_child(raw_line)
                return

            if method == "notifications/initialized":
                self.initialized_notification = dict(request)
                replaced = self._ensure_worker()
                # A replacement already replayed the notification above.
                if not replaced:
                    self._write_child(raw_line)
                return
            self._ensure_worker()
            self._write_child(raw_line)
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            self._send_error(request, str(exc))

    def run(self) -> None:
        self.start()
        try:
            for raw_line in sys.stdin.buffer:
                if not raw_line.strip():
                    continue
                try:
                    request = json.loads(raw_line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    # Preserve protocol behavior for malformed or non-JSON
                    # input; the worker owns the eventual error response.
                    try:
                        self._ensure_worker()
                        self._write_child(raw_line)
                    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
                        self.log(str(exc))
                    continue
                if not isinstance(request, dict):
                    self.log("ignoring non-object JSON-RPC input")
                    continue
                self.handle(request, raw_line)
        finally:
            self.stopping = True
            self._stop_child()
            self.output_queue.put(None)


def main() -> None:
    Gateway().run()


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        sys.stderr.write(f"Visual Library MCP gateway failed: {exc}\n")
        raise SystemExit(1)
