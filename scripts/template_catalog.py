"""Merge source-owned template sidecars without overwriting other sources.

Call under the library writer lock. Archived documents, IDs and covers remain
authoritative; this module only normalizes and indexes their references.
"""
from copy import deepcopy
import json
import os
from pathlib import Path
import re
import tempfile


def _write(path, payload):
    data = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if path.is_file() and json.loads(path.read_text(encoding="utf-8")) == payload:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".templates-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return True


def _normalize(source_id, payload):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", source_id):
        raise ValueError("Invalid template source ID")
    if payload.get("source", source_id) != source_id:
        raise ValueError("Template source mismatch")
    result = deepcopy(payload)
    result.pop("sources", None)
    result.pop("schema_version", None)
    result["source"] = source_id
    rows, seen = [], set()
    for template in result.get("templates", []):
        entry = deepcopy(template)
        original = str(entry.get("id", ""))
        if not original or original in seen:
            raise ValueError("Missing or duplicate template ID: " + original)
        seen.add(original)
        entry.update(stable_id=source_id + ":" + original, source=source_id,
                     revision=payload.get("revision"),
                     document=entry.get("document") or payload.get("document"))
        if not entry["document"] or not entry["revision"]:
            raise ValueError("Template requires document and revision")
        rows.append(entry)
    result["templates"] = rows
    return result


def _migrate(root):
    path = root / "indexes/templates.json"
    if not path.is_file():
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    # Recover every source from an aggregate too, if a sidecar is absent.
    sources = data.get("sources") or ([{key: data.get(key) for key in
                                           ("source", "revision", "document")}]
                                      if data.get("source") else [])
    for source in sources:
        source_id = source["source"]
        sidecar = root / "sources" / source_id / "templates.json"
        if sidecar.is_file():
            continue
        payload = dict(source)
        payload.pop("count", None)
        payload["templates"] = [row for row in data.get("templates", [])
                                if row.get("source", data.get("source")) == source_id]
        _write(sidecar, _normalize(source_id, payload))


def rebuild_templates(root):
    """Migrate legacy single-source data and return the deterministic aggregate."""
    root = Path(root)
    _migrate(root)
    sources, templates = [], []
    for sidecar in sorted((root / "sources").glob("*/templates.json")):
        data = _normalize(sidecar.parent.name, json.loads(sidecar.read_text(encoding="utf-8")))
        sources.append({key: data.get(key) for key in ("source", "revision", "document")}
                       | {"count": len(data["templates"])})
        templates.extend(data["templates"])
    aggregate = {"schema_version": 2, "sources": sources, "templates": templates}
    if len(sources) == 1:
        aggregate.update({key: sources[0][key] for key in ("source", "revision", "document")})
    _write(root / "indexes/templates.json", aggregate)
    return aggregate


def write_source_templates(root, source_id, payload):
    """Replace this source's templates only; an empty list cannot erase others."""
    root = Path(root)
    normalized = _normalize(source_id, payload)
    _migrate(root)
    changed = _write(root / "sources" / source_id / "templates.json", normalized)
    aggregate = rebuild_templates(root)
    return {"changed": changed, "source": source_id,
            "source_templates": len(normalized["templates"]), "templates": len(aggregate["templates"])}
