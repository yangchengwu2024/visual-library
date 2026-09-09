"""Portable visual-library storage and read-only retrieval.

Version JSON is authoritative and immutable. case.json stores mutable provenance
and classification; personal/ stores user edits. Markdown and catalog are generated.
Only explicit import, rebuild, group-variant, note, and favorite commands write.
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from datetime import datetime, timezone


SCHEMA_VERSION = 1


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _read(path, default=None):
    path = Path(path)
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else copy.deepcopy(default)


def _write(path, value, immutable=False):
    """Atomic replacement, or atomic no-clobber publication for versions/assets."""
    path = Path(path)
    data = value if isinstance(value, bytes) else _json_bytes(value)
    if path.exists():
        if path.read_bytes() == data:
            return False
        if immutable:
            raise FileExistsError("Refusing to overwrite immutable file: " + str(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        if immutable:
            try:
                os.link(temp, path)
            except FileExistsError:
                if path.read_bytes() != data:
                    raise
        else:
            os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
    return True


@contextlib.contextmanager
def library_lock(root, timeout=20):
    """Bounded cross-process local lock; never steal a possibly live writer's lock."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    lock = root / ".library.lock"
    deadline = time.monotonic() + timeout
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise TimeoutError("Library writer lock is busy; inspect .library.lock before retrying")
            time.sleep(0.05)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump({"pid": os.getpid(), "created_at": _now()}, out)
        yield
    finally:
        lock.unlink(missing_ok=True)


def source_slug(source):
    name = str(source.get("name", "") if isinstance(source, dict) else source)
    if not name:
        raise ValueError("Source name must not be empty")
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", name) and name.lower() not in {"con", "prn", "aux", "nul"}:
        return name
    stem = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")[:48] or "source"
    return stem + "-" + hashlib.sha256(name.encode("utf-8")).hexdigest()[:10]


def stable_case_id(source, source_id):
    name = str(source.get("name") if isinstance(source, dict) else source)
    stem = re.sub(r"[^a-zA-Z0-9-]+", "-", str(source_id)).strip("-")[:35] or "item"
    digest = hashlib.sha256((name + "\0" + str(source_id)).encode("utf-8")).hexdigest()[:12]
    return "case-" + source_slug(source) + "-" + stem + "-" + digest


def _case_dir(root, case_id):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", str(case_id)) or ".." in case_id:
        raise ValueError("Invalid case ID")
    return Path(root) / "cases" / case_id


def _version_name(version):
    version = str(version)
    if version.isdigit():
        version = "v" + version
    if not re.fullmatch(r"v[1-9][0-9]*", version):
        raise ValueError("Version must be v1, v2, ...")
    return version


def _versions(directory):
    return sorted((p for p in Path(directory).glob("v*.json") if re.fullmatch(r"v[1-9][0-9]*", p.stem)), key=lambda p: int(p.stem[1:]))


def _resolve(root, case_id, version=None):
    aliases = _read(Path(root) / "metadata" / "case-aliases.json", {})
    seen = set()
    if version is not None:
        version = _version_name(version)
    while case_id in aliases:
        if case_id in seen:
            raise ValueError("Cyclic case alias")
        seen.add(case_id)
        alias = aliases[case_id]
        if version is not None:
            if version not in alias["versions"]:
                raise KeyError("Unknown version for old case ID: " + version)
            version = alias["versions"][version]
        case_id = alias["case_id"]
    directory = _case_dir(root, case_id)
    if not (directory / "case.json").is_file():
        raise KeyError("Unknown case: " + case_id)
    return case_id, version


def _alias_map(root):
    taxonomy = _read(Path(root) / "metadata" / "taxonomy.json", {})
    result = {}
    for key, value in taxonomy.get("aliases", {}).items():
        if isinstance(value, str):
            result[key.casefold()] = value
        elif isinstance(value, list):
            for alias in value:
                result[str(alias).casefold()] = key
    return result


def _normalize(value, aliases):
    return aliases.get(str(value).casefold(), str(value))


def _keyword_groups(terms, aliases):
    """OR within a synonym group, AND across requested concepts."""
    groups = []
    for term in terms:
        canonical = _normalize(term, aliases).casefold()
        group = {str(term).casefold(), canonical}
        group.update(alias.casefold() for alias, target in aliases.items() if target.casefold() == canonical)
        groups.append(group)
    return groups


def _matches_keywords(haystack, groups):
    return all(any(term in haystack for term in group) for group in groups)


def _tags(value):
    if value is None:
        return []
    return [value] if isinstance(value, str) else list(value)


def _classification(record, aliases):
    categories = _tags(record.get("category"))
    styles = _tags(record.get("styles"))
    scenes = _tags(record.get("scenes"))
    return {
        "source_category": record.get("category"),
        "source_tags": {"styles": styles, "scenes": scenes},
        "categories": list(dict.fromkeys(_normalize(x, aliases) for x in categories)),
        "styles": list(dict.fromkeys(_normalize(x, aliases) for x in styles)),
        "scenes": list(dict.fromkeys(_normalize(x, aliases) for x in scenes)),
        "tag_evidence": "source-provided; no independent visual verification",
    }


def _store_asset(root, asset):
    path = Path(asset["path"])
    data = path.read_bytes()
    if not data:
        raise ValueError("Empty asset: " + str(path))
    digest = hashlib.sha256(data).hexdigest()
    folder = Path(root) / "images"
    existing = sorted(folder.glob(digest + ".*"))
    shared = bool(existing)
    if existing:
        destination = existing[0]
        if hashlib.sha256(destination.read_bytes()).hexdigest() != digest:
            raise ValueError("Existing content-addressed asset is corrupt: " + str(destination))
    else:
        ext = path.suffix.lower()
        if not re.fullmatch(r"\.[a-z0-9]{1,8}", ext):
            ext = ".bin"
        destination = folder / (digest + ext)
        _write(destination, data, immutable=True)
    return {
        "path": destination.relative_to(root).as_posix(),
        "sha256": digest,
        "bytes": len(data),
        "role": asset.get("role", "example"),
        "source_path": asset.get("source_path"),
    }, shared


def _fingerprint(content):
    identity = {
        "prompt": content["prompt"],
        "model": content.get("model"),
        "parameters": content.get("parameters"),
        "assets": [{"sha256": a["sha256"], "role": a.get("role", "example")} for a in content["assets"]],
    }
    return hashlib.sha256(_json_bytes(identity)).hexdigest()


def _known_parameters(content):
    return bool(content.get("model")) and isinstance(content.get("parameters"), dict)


def _provenance(record, source, revision):
    result = {"source": source.get("name") if isinstance(source, dict) else source,
              "source_id": str(record["source_id"]), "revision": revision,
              "title": record.get("title", ""),
              "source_url": record.get("source_url"),
              "upstream_url": record.get("upstream_url"),
              "asset_sources": [{"source_path": a.get("source_path"), "role": a.get("role", "example")} for a in record.get("assets", [])],
              "source_category": record.get("category"),
              "source_tags": {"styles": _tags(record.get("styles")), "scenes": _tags(record.get("scenes"))}}
    for field in ("license", "license_status", "attribution", "rights", "source_metadata"):
        if field in record:
            result[field] = record[field]
    return result


def _append_provenance(header, provenance, version):
    entry = dict(provenance, version=version)
    if entry not in header["source_aliases"]:
        header["source_aliases"].append(entry)
    title = provenance.get("title")
    if title and title not in header["title_aliases"]:
        header["title_aliases"].append(title)


def _difference(previous, current):
    if previous is None:
        return ["initial archive"]
    result = []
    if previous["prompt"] != current["prompt"]:
        result.append("original prompt changed")
    if [(a["sha256"], a["role"]) for a in previous["assets"]] != [(a["sha256"], a["role"]) for a in current["assets"]]:
        result.append("asset bytes, order, or roles changed")
    if previous.get("model") != current.get("model"):
        result.append("known model changed")
    if previous.get("parameters") != current.get("parameters"):
        result.append("generation parameters changed")
    return result


def _render_version(root, content):
    directory = _case_dir(root, content["case_id"])
    prompt = content["prompt"]
    fence = "`" * max(3, max((len(m.group(0)) + 1 for m in re.finditer(r"`+", prompt)), default=3))
    lines = ["<!-- GENERATED from version JSON. Edit personal records, never this file. -->",
             "# " + str(content.get("title", content["case_id"])), "",
             "Case: `" + content["case_id"] + "` | Version: `" + content["version"] + "`", "",
             "Changes: " + "; ".join(content["changes"]), "", "## Original prompt", "", fence + "text", prompt, fence, "", "## Local assets", ""]
    for asset in content["assets"]:
        lines += ["- " + str(asset["role"]) + ": [" + asset["path"] + "](../../" + asset["path"] + ")"]
    lines += ["", "## Model and parameters", "", "```json", json.dumps({"model": content.get("model"), "parameters": content.get("parameters")}, ensure_ascii=False, indent=2), "```", "", "Provenance and source aliases: [case.json](case.json)", ""]
    _write(directory / (content["version"] + ".md"), "\n".join(lines).encode("utf-8"))


def _new_version(root, case_id, content, provenance, origin=None, allow_reuse=True):
    directory = _case_dir(root, case_id)
    old_paths = _versions(directory)
    for path in old_paths:
        previous = _read(path)
        if allow_reuse and previous["fingerprint"] == content["fingerprint"]:
            return previous["version"], False
    previous = _read(old_paths[-1]) if old_paths else None
    version = "v" + str(int(old_paths[-1].stem[1:]) + 1 if old_paths else 1)
    saved = dict(content, schema_version=SCHEMA_VERSION, case_id=case_id,
                 version=version, previous_version=previous["version"] if previous else None,
                 changes=_difference(previous, content), archived_at=_now(),
                 provenance=provenance, status="complete")
    if origin:
        saved["grouped_from"] = origin
        if not saved["changes"]:
            saved["changes"] = ["explicitly grouped variant; unknown parameters do not establish identical content"]
    _write(directory / (version + ".json"), saved, immutable=True)
    _render_version(root, saved)
    return version, True


def _rebuild(root):
    root = Path(root)
    aliases = _read(root / "metadata" / "case-aliases.json", {})
    taxonomy_aliases = _alias_map(root)
    entries = []
    for header_path in sorted((root / "cases").glob("*/case.json")):
        header = _read(header_path)
        if header["case_id"] in aliases:
            continue
        versions = [_read(p) for p in _versions(header_path.parent)]
        if not versions:
            continue
        current = versions[-1]
        original = header["classification"]
        classification = _classification({"category": original["source_category"], **original["source_tags"]}, taxonomy_aliases)
        personal = _personal_for(root, header["case_id"])
        corrections = personal.get("corrections", {})
        categories = corrections.get("categories", classification["categories"])
        styles = corrections.get("styles", classification["styles"])
        scenes = corrections.get("scenes", classification["scenes"])
        entries.append({"case_id": header["case_id"], "title": header["title"],
                        "title_aliases": header["title_aliases"], "version": current["version"],
                        "versions": [{"version": v["version"], "changes": v["changes"], "path": "cases/" + header["case_id"] + "/" + v["version"] + ".json"} for v in versions],
                        "categories": categories, "styles": styles, "scenes": scenes,
                        "source_category": classification["source_category"], "source_tags": classification["source_tags"],
                        "model": current.get("model"), "image_count": len(current["assets"]),
                        "sources": sorted(set(p["source"] for p in header["source_aliases"])),
                        "archived_at": current["archived_at"], "status": "complete"})
    catalog = {"schema_version": SCHEMA_VERSION, "case_count": len(entries), "cases": entries}
    _write(root / "indexes" / "catalog.json", catalog)
    return catalog


def rebuild(root):
    """Explicit maintenance operation: rebuild generated Markdown and catalog."""
    with library_lock(root):
        for path in (Path(root) / "cases").glob("*/v*.json"):
            if re.fullmatch(r"v[1-9][0-9]*", path.stem):
                _render_version(root, _read(path))
        return _rebuild(root)


def import_records(root, records, source, revision, *, lock=True):
    """Import one full source snapshot, retaining absent items and all old versions.

    records: source_id, title, exact prompt, category, styles, scenes, source_url,
    upstream_url, model (null=unknown), parameters (null=unknown, {}=known empty),
    assets [{path: local file, role, source_path}], missing_assets. Optional license,
    license_status, attribution, rights and source_metadata are preserved.
    Missing records go to sources/<source>/pending. Their partial files survive;
    the last complete source mapping and last_success_revision stay usable.
    Internal adapters already holding library_lock may pass lock=False to cover
    their own source-document writes and this import with one shared lock.
    """
    root = Path(root).resolve()
    records = list(records)
    ids = [str(r["source_id"]) for r in records]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate source_id within source snapshot")
    if not all(isinstance(r.get("prompt", ""), str) for r in records):
        raise TypeError("prompt must be the exact original string")
    _json_bytes(records and [{k: v for k, v in r.items() if k != "assets"} for r in records])
    slug = source_slug(source)
    summary = {"new_cases": 0, "new_versions": 0, "duplicates": 0,
               "pending": 0, "shared_assets": 0, "records": []}
    with library_lock(root) if lock else contextlib.nullcontext():
        state_path = root / "sources" / slug / "state.json"
        state = _read(state_path, {"schema_version": SCHEMA_VERSION, "source": source, "mappings": {}, "last_success_revision": None})
        mappings = state["mappings"]
        aliases = _alias_map(root)
        # Cross-source auto-reuse requires explicitly known model AND parameters.
        known_fingerprints = {}
        for version_path in (root / "cases").glob("*/v*.json"):
            old = _read(version_path)
            if _known_parameters(old):
                resolved = _resolve(root, old["case_id"], old["version"])
                known_fingerprints.setdefault(old["fingerprint"], resolved)
        for record in records:
            source_id = str(record["source_id"])
            existing = mappings.get(source_id)
            case_id = existing["case_id"] if existing else stable_case_id(source, source_id)
            if existing and (_case_dir(root, case_id) / "case.json").exists():
                case_id, _ = _resolve(root, case_id)
            assets, missing = [], list(record.get("missing_assets") or [])
            for asset in record.get("assets", []):
                try:
                    stored, shared = _store_asset(root, asset)
                    assets.append(stored)
                    summary["shared_assets"] += int(shared)
                except (OSError, ValueError, KeyError) as exc:
                    missing.append({"source_path": asset.get("source_path"), "reason": type(exc).__name__ + ": " + str(exc)})
            if not record.get("prompt", "").strip():
                missing.append({"field": "prompt", "reason": "missing original prompt"})
            if not assets:
                missing.append({"field": "assets", "reason": "no locally available images"})
            content = {"title": record.get("title", ""), "prompt": record.get("prompt", ""),
                       "model": record.get("model"), "parameters": record.get("parameters"), "assets": assets}
            content["fingerprint"] = _fingerprint(content)
            provenance = _provenance(record, source, revision)
            if missing:
                pending = dict(content, source_id=source_id, case_id=case_id,
                               status="pending", missing_assets=missing, provenance=provenance)
                pending_id = hashlib.sha256(source_id.encode("utf-8")).hexdigest()[:20]
                pending_path = root / "sources" / slug / "pending" / (pending_id + ".json")
                # Stable pending path/content: no timestamp-only churn on retries.
                _write(pending_path, pending)
                summary["pending"] += 1
                summary["records"].append({"case_id": case_id, "source_id": source_id, "version": existing.get("version") if existing else None, "status": "pending", "path": pending_path.relative_to(root).as_posix()})
                if existing:
                    existing["upstream_present"] = True
                continue
            if not existing and _known_parameters(content) and content["fingerprint"] in known_fingerprints:
                case_id, _ = known_fingerprints[content["fingerprint"]]
            directory = _case_dir(root, case_id)
            is_new_case = not (directory / "case.json").exists()
            header = _read(directory / "case.json", {"schema_version": SCHEMA_VERSION,
                           "case_id": case_id, "title": record.get("title", ""),
                           "title_aliases": [], "source_aliases": [],
                           "classification": _classification(record, aliases)})
            version, added = _new_version(root, case_id, content, provenance)
            if is_new_case:
                summary["new_cases"] += 1
            elif added:
                summary["new_versions"] += 1
            else:
                summary["duplicates"] += 1
            _append_provenance(header, provenance, version)
            # Classifications/titles are source metadata, not a new content version.
            header["title"] = record.get("title", header["title"])
            header["classification"] = _classification(record, aliases)
            _write(directory / "case.json", header)
            mappings[source_id] = {"case_id": case_id, "version": version, "upstream_present": True}
            if _known_parameters(content):
                known_fingerprints[content["fingerprint"]] = (case_id, version)
            pending_id = hashlib.sha256(source_id.encode("utf-8")).hexdigest()[:20]
            pending_path = root / "sources" / slug / "pending" / (pending_id + ".json")
            if pending_path.exists():
                pending = _read(pending_path)
                if pending.get("status") != "resolved":
                    pending.update(status="resolved", resolved_case_id=case_id, resolved_version=version)
                    _write(pending_path, pending)
            summary["records"].append({"case_id": case_id, "source_id": source_id, "version": version, "status": "complete"})
        for source_id, mapping in mappings.items():
            if source_id not in ids:
                mapping["upstream_present"] = False
        state["last_attempt_revision"] = revision
        state["pending_count"] = summary["pending"]
        if not summary["pending"]:
            state["last_success_revision"] = revision
        _write(state_path, state)
        _rebuild(root)
    return summary


def _personal_for(root, case_id, records=None):
    """Read personal records through preserved old IDs, without rewriting them."""
    combined = {"notes": [], "favorites": [], "rewrites": [], "corrections": {}}
    if records is None:
        records = [(path.stem, _read(path)) for path in (Path(root) / "personal").glob("*.json")]
    for _, data in sorted(records, key=lambda item: (item[0] == case_id, item[0])):
        try:
            resolved, _ = _resolve(root, data["case_id"])
        except (KeyError, ValueError):
            continue
        if resolved != case_id:
            continue
        for key in ("notes", "favorites", "rewrites"):
            for item in data.get(key, []):
                entry = dict(item, original_case_id=data["case_id"])
                if item.get("version"):
                    _, entry["version"] = _resolve(root, data["case_id"], item["version"])
                combined[key].append(entry)
        combined["corrections"].update(data.get("corrections", {}))
    # A later explicit favorite/unfavorite on the canonical case overrides an
    # inherited old-ID favorite, without modifying the preserved personal file.
    combined["favorites"] = list({item["version"]: item for item in combined["favorites"]}.values())
    return combined


def _personal_index(root):
    """Read each personal file once for a whole-catalog query."""
    result = {}
    for path in sorted((Path(root) / "personal").glob("*.json")):
        data = _read(path)
        try:
            canonical, _ = _resolve(root, data["case_id"])
        except (KeyError, ValueError):
            continue
        result.setdefault(canonical, []).append((path.stem, data))
    return result


def show(root, case_id, version=None):
    """Read exactly the requested version; no writes, network, or cache creation."""
    root = Path(root).resolve()
    requested_id, requested_version = case_id, version
    case_id, version = _resolve(root, case_id, version)
    directory = _case_dir(root, case_id)
    paths = _versions(directory)
    if version is None:
        if not paths:
            raise KeyError("Case has no complete versions")
        version = paths[-1].stem
    content = _read(directory / (version + ".json"))
    if content is None:
        raise KeyError("Unknown case version: " + case_id + " " + version)
    content["source_aliases"] = _read(directory / "case.json")["source_aliases"]
    content["available_versions"] = [p.stem for p in paths]
    content["personal"] = _personal_for(root, case_id)
    content["requested_case_id"] = requested_id
    content["requested_version"] = requested_version
    for asset in content["assets"]:
        asset["absolute_path"] = str(_safe_asset(root, asset["path"]))
    return content


def query(root, keywords=None, category=None, tags=None, limit=20, full_text=False, favorites=False):
    """Search light metadata by default; --full-text explicitly reads prompts.

    All keyword terms must match; category is exact after alias normalization.
    All requested tags must occur in category/style/scene tags. No visual inference.
    """
    root = Path(root)
    catalog = _read(root / "indexes" / "catalog.json", {"cases": []})
    aliases = _alias_map(root)
    terms = keywords.split() if isinstance(keywords, str) else (keywords or [])
    groups = _keyword_groups(terms, aliases)
    required_tags = {_normalize(t, aliases).casefold() for t in _tags(tags)}
    personal_records = _personal_index(root)
    result = []
    for entry in catalog["cases"]:
        values = entry["categories"] + entry["styles"] + entry["scenes"]
        if category and _normalize(category, aliases).casefold() not in {x.casefold() for x in entry["categories"]}:
            continue
        if not required_tags.issubset({x.casefold() for x in values}):
            continue
        personal = _personal_for(root, entry["case_id"], personal_records.get(entry["case_id"], []))
        if favorites and not any(x.get("value", True) for x in personal["favorites"]):
            continue
        note_texts = [str(note.get("text", "")) for note in personal["notes"]]
        haystack = " ".join([entry["case_id"], entry["title"], *entry["title_aliases"], *values, *entry["sources"], str(entry.get("model") or ""), *note_texts]).casefold()
        if not _matches_keywords(haystack, groups):
            if not full_text:
                continue
            content = _read(_case_dir(root, entry["case_id"]) / (entry["version"] + ".json"))
            if not _matches_keywords(haystack + " " + content["prompt"].casefold(), groups):
                continue
        item = copy.deepcopy(entry)
        item["matched_by"] = "text and source metadata; images not visually assessed"
        if favorites:
            item["favorite_versions"] = sorted(set(x["version"] for x in personal["favorites"] if x.get("value", True)))
        result.append(item)
    return result[:max(0, limit)]


def personal_note(root, case_id, text, version=None):
    with library_lock(root):
        content = show(root, case_id, version)
        case_id, version = content["case_id"], content["version"]
        path = Path(root) / "personal" / (case_id + ".json")
        data = _read(path, {"case_id": case_id, "notes": [], "favorites": [], "rewrites": [], "corrections": {}})
        note = {"version": version, "text": text}
        if not any(x["version"] == version and x["text"] == text for x in data["notes"]):
            data["notes"].append(dict(note, created_at=_now()))
        _write(path, data)
        return data


def personal_favorite(root, case_id, version=None, value=True):
    with library_lock(root):
        content = show(root, case_id, version)
        case_id, version = content["case_id"], content["version"]
        path = Path(root) / "personal" / (case_id + ".json")
        data = _read(path, {"case_id": case_id, "notes": [], "favorites": [], "rewrites": [], "corrections": {}})
        data["favorites"] = [x for x in data["favorites"] if x["version"] != version]
        data["favorites"].append({"version": version, "value": bool(value)})
        _write(path, data)
        return data


def _safe_asset(root, relative):
    root = Path(root).resolve()
    if not isinstance(relative, str) or "\\" in relative:
        raise ValueError("Asset references must be relative POSIX paths")
    path = root / relative
    if Path(relative).is_absolute() or not relative.startswith("images/") or ".." in Path(relative).parts:
        raise ValueError("Asset reference leaves library images: " + relative)
    if not path.resolve().is_relative_to((root / "images").resolve()):
        raise ValueError("Asset resolves outside library images: " + relative)
    return path


def validate(root):
    """Check all complete versions, shared bytes, source/alias/personal references.

    This checks archival integrity, not visual quality, semantics, or licensing.
    Pending originals may remain incomplete and are reported separately.
    """
    root = Path(root).resolve()
    errors, warnings, checked = [], [], {}
    version_count, pending_count = 0, 0
    for path in sorted((root / "cases").glob("*/v*.json")):
        try:
            content = _read(path)
            version_count += 1
            if content["case_id"] != path.parent.name or content["version"] != path.stem:
                errors.append(str(path.relative_to(root)) + ": identity mismatch")
            if content.get("status") != "complete" or not content["prompt"].strip() or not content["assets"]:
                errors.append(str(path.relative_to(root)) + ": invalid complete record")
            if content["fingerprint"] != _fingerprint(content):
                errors.append(str(path.relative_to(root)) + ": content fingerprint mismatch")
            previous = content.get("previous_version")
            if previous and not (path.parent / (_version_name(previous) + ".json")).exists():
                errors.append(str(path.relative_to(root)) + ": missing previous version")
            for asset in content["assets"]:
                actual = _safe_asset(root, asset["path"])
                if asset["path"] not in checked:
                    payload = actual.read_bytes()
                    checked[asset["path"]] = (hashlib.sha256(payload).hexdigest(), len(payload))
                digest, size = checked[asset["path"]]
                if digest != asset["sha256"] or size != asset["bytes"] or actual.stem != digest:
                    errors.append(asset["path"] + ": hash/size/name mismatch")
        except (OSError, KeyError, ValueError, TypeError) as exc:
            errors.append(str(path.relative_to(root)) + ": " + str(exc))
    seen_hashes = {}
    for path in sorted((root / "images").glob("*")):
        if not path.is_file():
            continue
        try:
            rel = path.relative_to(root).as_posix()
            if rel not in checked:
                payload = path.read_bytes()
                checked[rel] = (hashlib.sha256(payload).hexdigest(), len(payload))
            digest, _ = checked[rel]
            if path.stem != digest:
                errors.append(rel + ": asset filename is not its SHA-256")
            if digest in seen_hashes:
                warnings.append("duplicate asset bytes: " + rel + " and " + seen_hashes[digest])
            seen_hashes[digest] = rel
        except OSError as exc:
            errors.append(str(path) + ": " + str(exc))
    for path in (root / "sources").glob("*/pending/*.json"):
        pending = _read(path)
        if pending.get("status") == "pending":
            pending_count += 1
        for asset in pending.get("assets", []):
            try:
                local = _safe_asset(root, asset["path"])
                actual = checked.get(asset["path"])
                if not local.is_file() or actual != (asset["sha256"], asset["bytes"]):
                    errors.append(str(path.relative_to(root)) + ": invalid partial asset")
            except (ValueError, KeyError) as exc:
                errors.append(str(path.relative_to(root)) + ": " + str(exc))
    for path in (root / "sources").glob("*/state.json"):
        for mapping in _read(path).get("mappings", {}).values():
            try:
                case_id, version = _resolve(root, mapping["case_id"], mapping["version"])
                if not (_case_dir(root, case_id) / (version + ".json")).is_file():
                    raise KeyError("Source mapping points to absent version")
            except (KeyError, ValueError) as exc:
                errors.append(str(path.relative_to(root)) + ": " + str(exc))
    for old_id, alias in _read(root / "metadata" / "case-aliases.json", {}).items():
        for old_version in alias.get("versions", {}):
            try:
                case_id, version = _resolve(root, old_id, old_version)
                if not (_case_dir(root, case_id) / (version + ".json")).is_file():
                    raise KeyError("Alias points to absent version")
            except (KeyError, ValueError) as exc:
                errors.append("alias " + old_id + ": " + str(exc))
    for path in (root / "personal").glob("*.json"):
        data = _read(path)
        for item in data.get("notes", []) + data.get("favorites", []) + data.get("rewrites", []):
            try:
                case_id, version = _resolve(root, data["case_id"], item["version"])
                if not (_case_dir(root, case_id) / (version + ".json")).exists():
                    raise KeyError("Personal record points to absent version")
            except (KeyError, ValueError) as exc:
                errors.append(str(path.relative_to(root)) + ": " + str(exc))
    alias_ids = set(_read(root / "metadata" / "case-aliases.json", {}))
    canonical_ids = set()
    for path in (root / "cases").glob("*/case.json"):
        header = _read(path)
        if header.get("case_id") != path.parent.name:
            errors.append(str(path.relative_to(root)) + ": header identity mismatch")
        if path.parent.name not in alias_ids:
            canonical_ids.add(path.parent.name)
        for entry in header.get("source_aliases", []):
            if not (path.parent / (_version_name(entry["version"]) + ".json")).is_file():
                errors.append(str(path.relative_to(root)) + ": provenance points to absent version")
    catalog_path = root / "indexes" / "catalog.json"
    catalog_entries = _read(catalog_path, {"cases": []}).get("cases", [])
    catalog_ids = [entry["case_id"] for entry in catalog_entries]
    if set(catalog_ids) != canonical_ids or len(catalog_ids) != len(set(catalog_ids)):
        errors.append("catalog does not contain exactly the canonical cases; run rebuild")
    for entry in catalog_entries:
        try:
            paths = _versions(_case_dir(root, entry["case_id"]))
            if not paths or entry["version"] != paths[-1].stem or [v["version"] for v in entry["versions"]] != [p.stem for p in paths]:
                errors.append("catalog out of date: " + entry["case_id"])
            for item in entry["versions"]:
                expected_path = "cases/" + entry["case_id"] + "/" + item["version"] + ".json"
                if item.get("path") != expected_path:
                    errors.append("catalog has invalid version reference: " + entry["case_id"])
        except (ValueError, KeyError) as exc:
            errors.append("catalog: " + str(exc))
    return {"ok": not errors, "versions": version_count, "assets": len(checked),
            "pending": pending_count, "errors": errors, "warnings": warnings}


def similar_candidates(root, case_id=None, threshold=0.86, limit=30):
    """Explicit candidate discovery; never merges, deletes, or writes anything."""
    root = Path(root)
    entries = _read(root / "indexes" / "catalog.json", {"cases": []})["cases"]
    if case_id:
        case_id, _ = _resolve(root, case_id)
    versions = [_read(_case_dir(root, e["case_id"]) / (e["version"] + ".json")) for e in entries]
    candidates = []
    for i, left in enumerate(versions):
        for right in versions[i + 1:]:
            if case_id and case_id not in (left["case_id"], right["case_id"]):
                continue
            shared = {a["sha256"] for a in left["assets"]} & {a["sha256"] for a in right["assets"]}
            # Length bound avoids expensive comparisons when similarity is impossible.
            a, b = left["prompt"], right["prompt"]
            ratio = 0.0
            if 2 * min(len(a), len(b)) / max(1, len(a) + len(b)) >= threshold:
                ratio = difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()
            if shared or ratio >= threshold:
                candidates.append({"left": {"case_id": left["case_id"], "version": left["version"]},
                                   "right": {"case_id": right["case_id"], "version": right["version"]},
                                   "shared_assets": len(shared), "prompt_similarity": round(ratio, 4),
                                   "decision": "candidate only; requires explicit human/Codex judgment"})
    candidates.sort(key=lambda c: (c["shared_assets"], c["prompt_similarity"]), reverse=True)
    return candidates[:max(0, limit)]


def group_variant(root, source_case_id, target_case_id):
    """Explicitly group an existing case into another; preserve old IDs and files.

    All original versions and personal files remain intact. The alias version map
    resolves each old ID/version to its retained or newly appended target version.
    An append-only operation history preserves the pre-group mapping for reversal.
    No visual/semantic equivalence is inferred by this command.
    """
    root = Path(root)
    with library_lock(root):
        source_case_id, _ = _resolve(root, source_case_id)
        target_case_id, _ = _resolve(root, target_case_id)
        if source_case_id == target_case_id:
            raise ValueError("Cases already resolve to the same case")
        source_dir, target_dir = _case_dir(root, source_case_id), _case_dir(root, target_case_id)
        header = _read(target_dir / "case.json")
        source_header = _read(source_dir / "case.json")
        mapping = {}
        for path in _versions(source_dir):
            old = _read(path)
            content = {k: old[k] for k in ("prompt", "title", "model", "parameters", "assets", "fingerprint")}
            version, _ = _new_version(root, target_case_id, content, old["provenance"], {"case_id": source_case_id, "version": old["version"]}, allow_reuse=_known_parameters(content))
            mapping[old["version"]] = version
        for provenance in source_header["source_aliases"]:
            item = {k: v for k, v in provenance.items() if k != "version"}
            _append_provenance(header, item, mapping[provenance["version"]])
        _write(target_dir / "case.json", header)
        alias_path = root / "metadata" / "case-aliases.json"
        aliases = _read(alias_path, {})
        aliases[source_case_id] = {"case_id": target_case_id, "versions": mapping}
        old_states = {}
        for state_path in (root / "sources").glob("*/state.json"):
            state = _read(state_path)
            changed = False
            for source_id, entry in state.get("mappings", {}).items():
                if entry["case_id"] == source_case_id:
                    old_states.setdefault(state_path.relative_to(root).as_posix(), {})[source_id] = copy.deepcopy(entry)
                    entry.update(case_id=target_case_id, version=mapping[entry["version"]])
                    changed = True
            if changed:
                _write(state_path, state)
        _write(alias_path, aliases)
        operations_path = root / "metadata" / "group-operations.json"
        operations = _read(operations_path, [])
        operations.append({"source_case_id": source_case_id, "target_case_id": target_case_id,
                           "versions": mapping, "prior_source_mappings": old_states,
                           "created_at": _now(), "decision": "explicit group-variant command"})
        _write(operations_path, operations)
        _rebuild(root)
        return aliases[source_case_id]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    commands = parser.add_subparsers(dest="command", required=True)
    q = commands.add_parser("query", help="Read metadata only unless --full-text is specified")
    q.add_argument("keywords", nargs="*")
    q.add_argument("--category")
    q.add_argument("--tag", action="append", default=[])
    q.add_argument("--limit", type=int, default=20)
    q.add_argument("--full-text", action="store_true")
    q.add_argument("--favorites", action="store_true")
    s = commands.add_parser("show")
    s.add_argument("case_id")
    s.add_argument("--version")
    commands.add_parser("validate")
    commands.add_parser("rebuild", help="Explicitly regenerate Markdown and lightweight catalog")
    imp = commands.add_parser("import", help="Import a complete source snapshot from prepared local JSON")
    imp.add_argument("--records", type=Path, required=True)
    imp.add_argument("--source", required=True)
    imp.add_argument("--revision", required=True)
    note = commands.add_parser("note", help="Explicitly save a personal note fixed to a version")
    note.add_argument("case_id")
    note.add_argument("text")
    note.add_argument("--version")
    favorite = commands.add_parser("favorite", help="Explicitly save a version-specific favorite")
    favorite.add_argument("case_id")
    favorite.add_argument("--version")
    favorite.add_argument("--remove", action="store_true")
    similar = commands.add_parser("similar", help="Read-only candidates, no automatic semantic merging")
    similar.add_argument("--case")
    similar.add_argument("--threshold", type=float, default=0.86)
    similar.add_argument("--limit", type=int, default=30)
    group = commands.add_parser("group-variant", help="Explicitly group two cases, preserving old IDs and versions")
    group.add_argument("source_case_id")
    group.add_argument("target_case_id")
    args = parser.parse_args(argv)
    if args.command == "query":
        result = query(args.root, args.keywords, args.category, args.tag, args.limit, args.full_text, args.favorites)
    elif args.command == "show":
        result = show(args.root, args.case_id, args.version)
    elif args.command == "validate":
        result = validate(args.root)
    elif args.command == "rebuild":
        result = rebuild(args.root)
    elif args.command == "import":
        result = import_records(args.root, _read(args.records), args.source, args.revision)
    elif args.command == "note":
        result = personal_note(args.root, args.case_id, args.text, args.version)
    elif args.command == "favorite":
        result = personal_favorite(args.root, args.case_id, args.version, not args.remove)
    elif args.command == "similar":
        result = similar_candidates(args.root, args.case, args.threshold, args.limit)
    elif args.command == "group-variant":
        result = group_variant(args.root, args.source_case_id, args.target_case_id)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if args.command == "validate" and not result["ok"] else 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, TypeError, TimeoutError) as exc:
        print(type(exc).__name__ + ": " + str(exc), file=sys.stderr)
        raise SystemExit(2)
