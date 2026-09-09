"""Bounded PicoTrex README adapter. Reads only reviewed IDs via caller downloaders."""
from __future__ import annotations

import copy
import hashlib
from pathlib import Path, PurePosixPath
import re


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def prepare(source, selection, revision, get_file, get_url):
    """Return selected records and downloaded source documents without importing.

    get_file is scoped to the caller's fixed commit. No live URL or branch reads.
    Unknown Markdown/image-role changes fail rather than assuming completeness.
    """
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("Pico requires a fixed full commit SHA")
    entries = selection.get("entries")
    if not isinstance(entries, list) or not entries:
        raise ValueError("Pico selection must contain reviewed entries")
    if selection.get("source_id") != source.get("id"):
        raise ValueError("Pico selection belongs to a different source")
    ids = [entry.get("source_id") for entry in entries]
    if len(ids) != len(set(ids)) or any(not re.fullmatch(r"pro_case[1-9][0-9]*", str(i)) for i in ids):
        raise ValueError("Pico selection IDs must be unique Pro case IDs")
    docs = {name: Path(get_file(name)) for name in ("README.md", "README_en.md", "LICENSE")}
    if _sha(docs["LICENSE"]) != selection.get("license_sha256"):
        raise ValueError("Pico license changed; scope must be reviewed")
    readme = docs["README.md"].read_text(encoding="utf-8")
    if "[![License: CC BY 4.0]" not in readme:
        raise ValueError("Pico README license declaration changed")
    if re.search(r"all rights reserved|rights remain with", readme, re.I):
        raise ValueError("Pico README now contains a restrictive rights declaration")
    sections = re.split(r"(?=^### 例 )", readme, flags=re.M)[1:]
    repo_url = source.get("url") or "https://github.com/" + source["repository"]
    repo_url = repo_url.rstrip("/")
    records = []
    for entry in entries:
        sid = entry["source_id"]
        candidates = [block for block in sections if f'images/{sid}/' in block]
        if len(candidates) != 1:
            raise ValueError("Pico section missing or ambiguous: " + sid)
        block = candidates[0]
        before = readme[:readme.index(block)]
        headers = re.findall(r"^## (.+)$", before, flags=re.M)
        if not headers or "Nano Banana Pro" not in headers[-1]:
            raise ValueError("Pico case moved out of its reviewed model section: " + sid)
        heading = re.match(r"### 例 \d+: \[(.*?)\]\((https://[^\s)]+)\)（by \[([^\]]+)\]\((https://[^\s)]+)\)）", block)
        if not heading:
            raise ValueError("Pico title/source/attribution structure changed: " + sid)
        title, upstream, creator, creator_url = heading.groups()
        prompts = re.findall(r"^```[^\n]*\n([\s\S]*?)\n```", block, flags=re.M)
        if len(prompts) != 1 or not prompts[0].strip():
            raise ValueError("Pico prompt structure changed: " + sid)
        prompt = prompts[0]
        image_tags = re.findall(r"<img\b[^>]*>", block)
        observed = []
        for tag in image_tags:
            attributes = dict(re.findall(r'(src|alt)="([^"]*)"', tag))
            relative = attributes.get("src", "")
            alt = attributes.get("alt", "")
            if not relative.startswith("images/" + sid + "/") or ".." in PurePosixPath(relative).parts or "\\" in relative:
                raise ValueError("Pico image path changed unexpectedly: " + sid)
            if alt == "输入图片":
                role = "input"
            elif alt == "输出结果":
                role = "output"
            else:
                raise ValueError("Pico image role is no longer explicit: " + sid)
            observed.append({"source_path": relative, "role": role})
        expected = [{k: a[k] for k in ("source_path", "role")} for a in entry["assets"]]
        if observed != expected:
            raise ValueError("Pico image count/order/role changed: " + sid)
        if "**输入:**" in block and not any(a["role"] == "input" for a in observed):
            raise ValueError("Pico required input absent: " + sid)
        assets = []
        image_hashes = []
        missing = []
        for asset in observed:
            try:
                path = Path(get_file(asset["source_path"]))
                if not path.is_file() or not path.stat().st_size:
                    raise ValueError("Pico image download absent")
                # The upstream names PNG binaries *.jpg; extensions are not format proof.
                signature = path.read_bytes()[:12]
                if not (signature.startswith(b"\x89PNG\r\n\x1a\n") or signature.startswith(b"\xff\xd8\xff") or
                        (signature.startswith(b"RIFF") and signature[8:12] == b"WEBP")):
                    raise ValueError("Pico response is not an expected image")
                assets.append(dict(asset, path=str(path.resolve())))
                image_hashes.append(_sha(path))
            except (OSError, ValueError) as exc:
                missing.append(dict(asset, reason=type(exc).__name__ + ": " + str(exc)))
        unchanged = (not missing and hashlib.sha256(prompt.encode("utf-8")).hexdigest() == entry["prompt_sha256"] and
                     image_hashes == [a["sha256"] for a in entry["assets"]])
        record = copy.deepcopy(entry["record_fields"])
        record.update(source_id=sid, title=title, prompt=prompt, assets=assets,
                      upstream_url=upstream, source_url=repo_url + "/blob/" + revision + "/README.md")
        record["attribution"].update(creator_as_credited=creator, creator_url=creator_url, original_source=upstream)
        metadata = record["metadata"]
        for ev in metadata.get("evidence", []):
            if ev.get("kind") == "source_section":
                ev["url"] = record["source_url"]
            elif ev.get("kind") == "original_source":
                ev["url"] = upstream
        if not unchanged:
            metadata["review_status"] = "needs_review"
            metadata["review_note"] = "所选案例的 prompt 或原图已变化；输入输出结构齐全，当前版本尚未人工看图复核。"
            metadata["evidence"] = [ev for ev in metadata.get("evidence", []) if ev.get("kind") != "visual_review"]
            metadata["evidence"].append({"kind": "review_reset", "revision": revision,
                                         "note": "Prompt/image hash differs from reviewed selection"})
        if missing:
            record["missing_assets"] = missing
            metadata["review_status"] = "missing_inputs" if any(a["role"] == "input" for a in missing) else "needs_review"
            metadata["review_note"] = "当前所选案例有下载失败的原图；保留取得的 prompt 和图片，由导入器记录 pending，不替换原完整版本。"
            metadata["asset_roles"] = [{"index": i, "role": a["role"], "reason": "当前 README 明确标注；原图下载未齐，尚未人工复核"} for i, a in enumerate(assets)]
        records.append(record)
    return records, list(docs.values())
