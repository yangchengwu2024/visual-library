"""Bounded jamez YAML adapter. All I/O is supplied by the snapshot runner."""

from __future__ import annotations

import copy
import hashlib
from pathlib import Path, PurePosixPath
import re

import yaml


REPOSITORY = "jamez-bondos/awesome-gpt4o-images"


def _sha(value):
    return hashlib.sha256(value).hexdigest()


def _mapping(path):
    value = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Case and attribution YAML must be mappings")
    return value


def prepare(source, selection, revision, get_file, get_url):
    """Fetch only approved IDs, preserve exact prompt, invalidate stale reviews."""
    repository = source.get("repository") or source.get("repo")
    if repository != REPOSITORY or not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("Expected the approved repository and a fixed commit SHA")
    entries = selection.get("entries")
    if not isinstance(entries, list) or not entries:
        raise ValueError("An explicit nonempty selection is required")
    if selection.get("source_id") != source.get("id", source.get("name")):
        raise ValueError("Selection belongs to another source")
    ids = [str(entry.get("source_id", "")) for entry in entries]
    if len(ids) != len(set(ids)) or not all(re.fullmatch(r"[1-9][0-9]*", sid) for sid in ids):
        raise ValueError("Selected case IDs must be unique positive integers")
    documents = []
    for relative in ("LICENSE", "gpt-image-1/NOTICE-openai.md"):
        path = Path(get_file(relative))
        documents.append(path)
        if _sha(path.read_bytes()) != selection.get("reviewed_document_sha256", {}).get(relative):
            raise ValueError("License or exception notice changed; rights review required: " + relative)
    base = "https://github.com/" + REPOSITORY
    records = []
    for entry, sid in zip(entries, ids):
        case = None
        try:
            relative = "cases/" + sid + "/case.yml"
            attr_relative = "cases/" + sid + "/ATTRIBUTION.yml"
            case_path = Path(get_file(relative))
            documents.append(case_path)
            case = _mapping(case_path)
            attr_path = Path(get_file(attr_relative))
            documents.append(attr_path)
            attr = _mapping(attr_path)
            if attr.get("license") != "CC-BY-4.0":
                raise ValueError("Case license changed: " + sid)
            field = entry.get("prompt_field")
            if field not in {"prompt", "prompt_en"} or not isinstance(case.get(field), str) or not case[field].strip():
                raise ValueError("Missing or changed exact prompt field: " + sid)
            if not isinstance(case.get("title"), str) or not case["title"].strip():
                raise ValueError("Missing case title: " + sid)
            # This batch was reviewed as text-only. Any reference schema change fails closed.
            for key in ("reference_note", "reference_note_en"):
                if key not in case or case[key] != "":
                    raise ValueError("Required input declaration changed: " + sid)
            for key in case:
                if re.search(r"reference|input|image", key, re.I) and key not in {"image", "reference_note", "reference_note_en"}:
                    raise ValueError("Unexpected image/input schema: " + sid + "/" + key)
            image = case.get("image")
            if not isinstance(image, str) or not image or "\\" in image or PurePosixPath(image).name != image or image in {".", ".."} or not re.fullmatch(r"[A-Za-z0-9_.-]+\.(?:png|jpe?g|webp)", image, re.I):
                raise ValueError("Expected one local image filename: " + sid)
            asset_relative = "cases/" + sid + "/" + image
            asset = Path(get_file(asset_relative))
            if not asset.is_file() or not asset.stat().st_size:
                raise ValueError("Required output image missing: " + sid)
            asset_sha = _sha(asset.read_bytes())
            prompt_sha = _sha(case[field].encode("utf-8"))
            unchanged = (prompt_sha == entry.get("reviewed_prompt_sha256") and
                         asset_sha == entry.get("reviewed_asset_sha256") and
                         _sha(attr_path.read_bytes()) == entry.get("reviewed_attribution_sha256") and
                         _sha(case_path.read_bytes()) == entry.get("reviewed_case_sha256"))
            record = copy.deepcopy(entry["record"])
            record.update(source_id=sid, title=case["title"], prompt=case[field], source_url=base + "/blob/" + revision + "/" + relative)
            links = case.get("source_links")
            if not isinstance(links, list) or any(not isinstance(x, dict) or not isinstance(x.get("url"), str) for x in links):
                raise ValueError("Changed source link schema: " + sid)
            record["upstream_url"] = links[0]["url"] if links else record["source_url"]
            record["model"] = "GPT-4o" if attr.get("creation_tool") == "GPT-4o" else None
            record["parameters"] = None
            record["assets"] = [{"path": str(asset.resolve()), "role": "output", "source_path": asset_relative}]
            attr["date"] = str(attr.get("date", ""))
            record["attribution"] = attr
            metadata = record["metadata"]
            metadata["model_version"] = record["model"]
            if not unchanged:
                metadata["review_status"] = "needs_review"
                metadata["review_note"] = "Upstream case, prompt, attribution or image changed after the saved visual review. Previous review: " + metadata.get("review_note", "")
            metadata.setdefault("evidence", []).append({"type": "snapshot_check", "revision": revision, "reviewed_content_unchanged": unchanged, "prompt_sha256": prompt_sha, "image_sha256": asset_sha})
            record["source_metadata"] = {"case": case, "prompt_field": field, "model_claim": attr.get("creation_tool"), "model_snapshot": None, "prompt_origin": "exact upstream YAML field", "license_evidence": base + "/blob/" + revision + "/" + attr_relative}
            records.append(record)
        except OSError as exc:
            # Preserve the failed ID without claiming cached prompts as newly fetched.
            failed = copy.deepcopy(entry["record"])
            field = entry.get("prompt_field")
            failed.update(source_id=sid, prompt=case.get(field, "") if isinstance(case, dict) else "", assets=[],
                          missing_assets=[{"source_path": "cases/" + sid, "reason": str(exc)}],
                          source_url=base + "/blob/" + revision + "/cases/" + sid + "/case.yml")
            failed["metadata"]["review_status"] = "needs_review"
            failed["metadata"]["review_note"] = "Current snapshot could not be fully fetched: " + str(exc)
            failed["source_metadata"] = {"fetch_error": str(exc), "revision": revision, "partial_case": case}
            records.append(failed)
    return records, documents
