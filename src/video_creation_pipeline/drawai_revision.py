"""Read-only DrawAI revision checks for the existing manual shot handoff.

No scene code is executed. The result is an offline mapping/check report for the
existing generation and QA route, not a new project or approval format.
"""
from __future__ import annotations

import copy
import json
from fractions import Fraction
from pathlib import Path

from .contract_io import artifact_reference, file_sha256, validate_artifact_reference
from .errors import ContractError
from .generation_contracts import validate_shot_generation_unit

SOURCE_COMMIT = "f26770ca91934e89d39e025433467ebc325e82b8"
MANIFEST_SHA256 = "8765626eccf5a39e9599f470fd1d2fb664043912a77e7c0e5d972f7a559127e2"
TEXT_BUDGET = 2 * 1024 * 1024
MUTABLE_FIELDS = {"label", "x", "y", "w", "h", "style"}


def _require(condition, message):
    if not condition:
        raise ContractError(message)


def _path(value):
    _require(isinstance(value, str) and bool(value.strip()), "local path required")
    _require("://" not in value and not value.startswith(("\\\\", "//")), "local disk path required")
    return Path(value).expanduser().resolve()


def _json(path):
    _require(path.is_file() and path.stat().st_size <= TEXT_BUDGET, "bounded JSON file required")
    try:
        value = json.loads(path.read_bytes().decode("utf-8-sig"), parse_constant=_reject_constant)
    except (UnicodeError, ValueError) as exc:
        raise ContractError("invalid JSON input") from exc
    _require(isinstance(value, dict), "JSON object required")
    return value


def _reject_constant(value):
    raise ValueError(value)


def _bounded_ref(path, label):
    _require(path.is_file() and path.stat().st_size <= TEXT_BUDGET, "bounded input file required")
    return artifact_reference(path, label)


def _review(root):
    manifest = root / "source-manifest.json"
    _require(manifest.is_file() and file_sha256(manifest) == MANIFEST_SHA256,
             "DrawAI reviewed manifest drifted")
    data = _json(manifest)
    _require(data.get("commit") == SOURCE_COMMIT and len(data["files"]) == 28,
             "DrawAI selected source identity drifted")
    source = (root / "source").resolve()
    for row in data["files"]:
        path = (source / row["path"]).resolve()
        _require(path.is_relative_to(source) and path.is_file(), "review source escaped or missing")
        _require(file_sha256(path) == row["sha256"], "DrawAI reviewed source hash drifted")
    return {"repo": data["repo"], "commit": SOURCE_COMMIT,
            "manifest": artifact_reference(manifest, "DrawAI source manifest"),
            "files_verified": len(data["files"]), "runtime_verified": False}


def _canvas(path):
    _require(path.suffix.lower() == ".drawai", "native .drawai JSON snapshot required")
    data = _json(path)
    _require(data.get("format") == "dsh-drawai" and data.get("schemaVersion") == 1,
             "unsupported DrawAI native format")
    pages = data.get("pages")
    _require(isinstance(data.get("id"), str) and isinstance(pages, list) and 0 < len(pages) <= 20,
             "bounded canvas pages and project identity required")
    page_ids = set()
    for page in pages:
        _require(isinstance(page, dict) and isinstance(page.get("id"), str)
                 and page["id"] not in page_ids, "unique page IDs required")
        page_ids.add(page["id"])
        doc = page.get("doc", {})
        _require(isinstance(doc, dict), "canvas page document required")
        for kind in ("nodes", "edges"):
            rows = doc.get(kind)
            _require(isinstance(rows, list) and len(rows) <= 1000, "bounded node/edge inventory required")
            ids = [row.get("id") for row in rows if isinstance(row, dict)]
            _require(len(ids) == len(rows) and all(isinstance(i, str) and i for i in ids)
                     and len(set(ids)) == len(ids), "unique node/edge IDs required")
    return data


def _check_canvas(before, after, brief):
    allowed = brief.get("mutable_node_fields")
    _require(isinstance(allowed, dict) and bool(allowed), "explicit mutable node fields required")
    page_id = brief.get("page_id")
    old_page = next((p for p in before["pages"] if p["id"] == page_id), None)
    new_page = next((p for p in after["pages"] if p["id"] == page_id), None)
    _require(old_page is not None and new_page is not None, "bound page missing")
    old_nodes = {n["id"]: n for n in old_page["doc"]["nodes"]}
    new_nodes = {n["id"]: n for n in new_page["doc"]["nodes"]}
    _require(brief.get("video_node_id") in old_nodes and brief["video_node_id"] in new_nodes,
             "bound video node missing")
    masked_before, masked_after = copy.deepcopy(before), copy.deepcopy(after)
    for data in (masked_before, masked_after):
        page = next(p for p in data["pages"] if p["id"] == page_id)
        for node_id, fields in allowed.items():
            _require(node_id in old_nodes and node_id in new_nodes, "mutable node identity changed")
            _require(isinstance(fields, list) and fields
                     and all(isinstance(f, str) and f in MUTABLE_FIELDS for f in fields)
                     and len(set(fields)) == len(fields),
                     "unsupported mutable field")
            node = next((n for n in page["doc"]["nodes"] if n["id"] == node_id), None)
            _require(node is not None, "mutable node missing")
            for field in fields:
                node.pop(field, None)
    _require(masked_before == masked_after, "confirmed canvas content changed outside the declared fields")
    return {node_id: [field for field in fields if old_nodes[node_id].get(field) != new_nodes[node_id].get(field)]
            for node_id, fields in allowed.items()}


def _project(root):
    _require(root.is_dir(), "project directory required")
    files = {}
    total = 0
    for path in root.rglob("*"):
        _require(not path.is_symlink() and path.resolve().is_relative_to(root), "project link escaped")
        if path.is_file():
            total += path.stat().st_size
            _require(len(files) < 100 and total <= 16 * 1024 * 1024, "offline project budget exceeded")
            files[path.relative_to(root).as_posix()] = artifact_reference(path, "project file")
    _require("timeline.json" in files and "asset-sources.json" in files, "timeline and asset provenance required")
    return files


def _timeline(data, unit):
    try:
        rate = Fraction(str(data["fps"]))
    except (ValueError, TypeError, KeyError, ZeroDivisionError) as exc:
        raise ContractError("valid DrawAI frame rate required") from exc
    _require(0 < rate <= 120 and data.get("width") == unit["output"]["width"]
             and data.get("height") == unit["output"]["height"], "DrawAI output dimensions/frame rate mismatch")
    total = data.get("durationInFrames")
    _require(type(total) is int and total > 0, "integer total frame count required")
    _require(abs(float(Fraction(total, 1) / rate) - unit["duration_s"]) <= 0.001,
             "DrawAI duration differs from canonical shot clock")
    shots = data.get("shots")
    _require(isinstance(shots, list) and 0 < len(shots) <= 100, "bounded DrawAI shots required")
    cursor, ids, names = 0, set(), set()
    for shot in shots:
        _require(isinstance(shot, dict) and isinstance(shot.get("id"), str)
                 and shot["id"] not in ids, "unique DrawAI shot IDs required")
        ids.add(shot["id"])
        duration = shot.get("durationFrames")
        _require(type(shot.get("startFrame")) is int and shot["startFrame"] == cursor
                 and type(duration) is int and duration > 0, "DrawAI timeline gap/overlap detected")
        cursor += duration
        name = shot.get("file")
        _require(isinstance(name, str) and Path(name).name == name and name.endswith(".tsx")
                 and name not in names, "unique flat scene TSX filenames required")
        names.add(name)
    _require(cursor == total, "DrawAI timeline does not fill canonical duration")
    return {"fps_numerator": rate.numerator, "fps_denominator": rate.denominator,
            "duration_frames": total, "canonical_duration_s": unit["duration_s"]}


def check_drawai_revision(shot_unit_path, brief_path, review_root):
    """Check declared local snapshots. This never writes the canvas/project."""
    unit_path, brief_path = _path(str(shot_unit_path)), _path(str(brief_path))
    unit_ref = _bounded_ref(unit_path, "shot unit")
    brief_ref = _bounded_ref(brief_path, "revision brief")
    unit, brief = _json(unit_path), _json(brief_path)
    _require(all(key in brief for key in ("before_canvas", "candidate_canvas", "before_project",
                                        "candidate_project", "page_id", "video_node_id")),
             "DrawAI revision brief is missing bindings")
    validate_shot_generation_unit(unit)
    _require(unit["generator"] == "manual" and unit["media_type"] == "video",
             "existing manual video shot route required")
    source = _review(_path(str(review_root)))
    before_path, after_path = _path(brief["before_canvas"]), _path(brief["candidate_canvas"])
    before_ref = _bounded_ref(before_path, "human-edited canvas snapshot")
    after_ref = _bounded_ref(after_path, "candidate canvas snapshot")
    expected = brief.get("expected_revision")
    _require(isinstance(expected, str) and expected == before_ref["sha256"][:12],
             "missing/stale DrawAI expected_revision")
    before, after = _canvas(before_path), _canvas(after_path)
    changed_fields = _check_canvas(before, after, brief)
    old_root, new_root = _path(brief["before_project"]), _path(brief["candidate_project"])
    _require(old_root != new_root, "separate candidate project required")
    old_files, new_files = _project(old_root), _project(new_root)
    _require(old_files.keys() == new_files.keys(), "project file inventory changed")
    timeline = _json(old_root / "timeline.json")
    clock = _timeline(timeline, unit)
    allowed_ids = brief.get("mutable_shot_ids")
    _require(isinstance(allowed_ids, list) and allowed_ids
             and all(isinstance(i, str) for i in allowed_ids) and len(set(allowed_ids)) == len(allowed_ids),
             "explicit mutable DrawAI shot IDs required")
    shots = {s["id"]: s for s in timeline["shots"]}
    _require(set(allowed_ids) <= shots.keys(), "mutable DrawAI shot missing")
    mutable_files = {shots[shot_id]["file"] for shot_id in allowed_ids}
    _require(all(s["file"] in old_files for s in timeline["shots"]), "scene source file missing")
    changed_files = [name for name in old_files if old_files[name]["sha256"] != new_files[name]["sha256"]]
    _require(set(changed_files) <= mutable_files, "confirmed project file or canonical timeline changed")
    assets = _json(old_root / "asset-sources.json")
    entries = assets.get("entries")
    _require(isinstance(entries, list) and all(isinstance(e, dict) and isinstance(e.get("path"), str)
             and e["path"] in old_files
             and e.get("sha256") == old_files[e["path"]]["sha256"] and e.get("source")
             and e.get("license") for e in entries), "asset provenance/hash missing")
    _require({name for name in old_files if name.startswith("assets/")} <= {e["path"] for e in entries},
             "project asset omitted from provenance")
    warnings = brief.get("upstream_warnings", [])
    _require(isinstance(warnings, list) and all(isinstance(w, str) for w in warnings), "warning list required")
    preview_rows = brief.get("preview_frames", [])
    _require(isinstance(preview_rows, list) and len(preview_rows) <= 30, "bounded preview references required")
    previews = []
    for row in preview_rows:
        _require(isinstance(row, dict) and type(row.get("frame")) is int
                 and 0 <= row["frame"] < clock["duration_frames"] and "path" in row,
                 "preview frame outside canonical clock")
        previews.append({"frame": row["frame"], "artifact": _bounded_ref(_path(row["path"]), "reported preview frame"),
                         "visual_review": "pending"})
    # Bind the actual read snapshots for the next existing handoff/QA consumer.
    refs = [unit_ref, brief_ref, before_ref, after_ref,
            *old_files.values(), *new_files.values()]
    for ref in refs:
        validate_artifact_reference(ref, "checked revision input")
    return {"status": "candidate_only", "tool": "drawai", "check": "offline_revision_mapping",
            "scene_id": unit["scene_id"], "source_shot_unit": unit_ref,
            "source_edit_job": unit["source_edit_job"], "reviewed_source": source,
            "source_brief": brief_ref,
            "canvas": {"before": before_ref, "candidate": after_ref,
                       "expected_revision": expected, "candidate_revision": after_ref["sha256"][:12],
                       "page_id": brief["page_id"], "video_node_id": brief["video_node_id"],
                       "changed_fields": changed_fields},
            "project_mapping": {"clock": clock, "changed_files": changed_files,
                                "before_files": old_files, "candidate_files": new_files,
                                "canonical_scene_id": unit["scene_id"], "drawai_shots": timeline["shots"]},
            "upstream_warnings": warnings, "preview_frames": previews,
            "return_kind": "editable_project_candidate", "candidate_mp4": None,
            "next_gate": "existing generation_contracts manual-media receipt, technical QA and human review",
            "runtime_verified": False, "executionAuthority": "none_metadata_only",
            "source_media_modified": False, "publish_approved": False}
