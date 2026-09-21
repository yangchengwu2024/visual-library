"""Atomically deploy this repository's verified MCP code to its local install."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INSTALL_ROOT = Path.home() / ".codex" / "tools" / "visual-library-mcp"
INSTALLED_FILES = (
    "visual_library_mcp_gateway.py",
    "serve_mcp.py",
    "mcp_library.py",
    "library.py",
    "archive_image_views.py",
    "validate_archive.py",
)


def git_revision(repository_root: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(repository_root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    revision = result.stdout.strip().lower()
    if len(revision) != 40 or any(character not in "0123456789abcdef" for character in revision):
        raise RuntimeError("Git returned an invalid full commit SHA")
    return revision


def require_clean_repository(repository_root: Path) -> None:
    result = subprocess.run(
        ["git", "-C", str(repository_root), "status", "--porcelain", "--untracked-files=all"],
        check=True,
        capture_output=True,
        text=True,
    )
    if result.stdout.strip():
        raise RuntimeError("Refusing to deploy a repository with uncommitted changes")


def atomic_copy(source: Path, destination: Path) -> None:
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_text(destination: Path, value: str) -> None:
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(value, encoding="utf-8")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def prepare_remote_snapshot(install_root: Path, revision: str) -> dict:
    python = install_root / ".venv" / "Scripts" / "python.exe"
    launcher = install_root / "scripts" / "serve_mcp.py"
    if not python.is_file() or not launcher.is_file():
        raise RuntimeError("The installed MCP Python or serve_mcp.py is missing")
    result = subprocess.run(
        [str(python), "-B", str(launcher), "--prepare-only"],
        cwd=str(install_root),
        check=True,
        capture_output=True,
        text=True,
    )
    try:
        payload = json.loads(result.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError) as exc:
        raise RuntimeError("serve_mcp.py --prepare-only did not return JSON") from exc
    if payload.get("commit", "").lower() != revision:
        raise RuntimeError(
            "The configured remote main does not contain the local HEAD: "
            + str(payload.get("commit", "unknown"))
        )
    return payload


def deploy(repository_root: Path, install_root: Path, *, prepare: bool = True) -> dict:
    repository_root = repository_root.resolve(strict=True)
    install_root = install_root.resolve(strict=True)
    require_clean_repository(repository_root)
    revision = git_revision(repository_root)
    source_root = repository_root / "scripts"
    destination_root = install_root / "scripts"
    destination_root.mkdir(parents=True, exist_ok=True)
    for name in INSTALLED_FILES:
        source = source_root / name
        if not source.is_file():
            raise RuntimeError(f"Missing repository MCP file: {source}")

    snapshot = prepare_remote_snapshot(install_root, revision) if prepare else None
    for name in INSTALLED_FILES:
        atomic_copy(source_root / name, destination_root / name)
    manifest = {
        name: hashlib.sha256((destination_root / name).read_bytes()).hexdigest()
        for name in INSTALLED_FILES
    }
    atomic_text(install_root / "installed-files.json", json.dumps(manifest, indent=2) + "\n")
    # Markers are written last. A running gateway notices them only after every
    # installed file and the manifest are ready.
    atomic_text(install_root / "installed-commit.txt", revision + "\n")
    atomic_text(install_root / "installed-code-revision.txt", revision + "\n")
    return {"revision": revision, "install_root": str(install_root), "snapshot": snapshot}


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository-root", type=Path, default=REPOSITORY_ROOT)
    parser.add_argument("--install-root", type=Path, default=DEFAULT_INSTALL_ROOT)
    parser.add_argument("--skip-prepare", action="store_true", help="Do not verify the remote main snapshot before deployment")
    args = parser.parse_args(argv)
    print(json.dumps(deploy(args.repository_root, args.install_root, prepare=not args.skip_prepare), ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"Visual Library MCP deployment failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
