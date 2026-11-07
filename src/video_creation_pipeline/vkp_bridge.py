from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .errors import ContractError
from .remote_authorization import (
    build_remote_contract,
    validate_remote_contract,
)

EDIT_JOB_SCHEMA = "video_creation_pipeline.vkp_edit_job.v3"
PREFERENCE_SCHEMA = "video_creation_pipeline.preference_candidates.v1"
VKP_REGISTRY_SCHEMA = "video_knowledge_pipeline.run_artifact_registry.v1"

TRANSCRIPT_KEYS = (
    "human_corrected_transcript_json",
    "llm_readable_transcript_json",
    "agent_readable_transcript_json",
    "readable_transcript_json",
    "llm_corrected_transcript_json",
    "corrected_transcript_json",
    "source_arbitrated_transcript_json",
    "normalized_transcript_json",
)


def build_edit_job(
    bundle_dir: str | Path,
    *,
    confirmed_diffs: Iterable[dict[str, Any]] | None = None,
    remote_policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    root = Path(bundle_dir).expanduser().resolve()
    manifest_path = root / "manifest.json"
    timeline_path = root / "timeline.json"
    manifest = _read_object(manifest_path, "manifest.json")
    timeline = _read_list(timeline_path, "timeline.json")

    registry_path = _artifact_path(
        root,
        manifest.get("run_artifact_registry_json"),
        "run-artifact-registry.json",
    )
    registry = _read_object(registry_path, "run-artifact-registry.json")
    if registry.get("schema") != VKP_REGISTRY_SCHEMA:
        raise ContractError(
            "run-artifact-registry.json must use "
            f"{VKP_REGISTRY_SCHEMA}; video creation does not create a replacement status source"
        )

    arbitration, arbitration_path = _optional_object(
        root,
        manifest.get("transcript_source_arbitration_json"),
        "transcript-source-arbitration.json",
    )
    smart_input, smart_input_path = _optional_object(
        root,
        manifest.get("smart_summary_input_pack"),
        "exports/smart-summary-input-pack.json",
    )
    chapters, chapters_path = _optional_object(
        root,
        manifest.get("smart_summary_chapters"),
        "exports/smart-summary-chapters.json",
    )
    transcript_path, transcript_source = _select_transcript_source(root, manifest)

    workbench_path = _first_existing_path(
        root,
        (
            manifest.get("video_workbench_json"),
            manifest.get("video_workbench_html"),
            "video-workbench.json",
            "video-workbench.html",
        ),
    )
    candidates = _timeline_candidates(timeline)
    storyboard = _storyboard_candidates(chapters, candidates)
    preference_candidates = build_preference_candidates(list(confirmed_diffs or []))
    temporal_count = sum(
        bool(row.get("temporal_visual_understanding"))
        or bool(row.get("human_corrected_temporal_visual_understanding"))
        for row in timeline
        if isinstance(row, dict)
    )

    blockers: list[str] = []
    if not candidates:
        blockers.append("timeline_has_no_candidates")
    if not workbench_path:
        blockers.append("vkp_workbench_missing")
    if temporal_count == 0:
        blockers.append("temporal_visual_evidence_missing")
    if not transcript_path and not any(
        _timeline_text(row) for row in timeline if isinstance(row, dict)
    ):
        blockers.append("transcript_evidence_missing")

    remote_contract = build_remote_contract(remote_policy)
    job = {
        "schema": EDIT_JOB_SCHEMA,
        "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "source_bundle": {
            "path": str(root),
            "schema": str(manifest.get("schema") or ""),
            "title": str(manifest.get("title") or root.name),
            "manifest_path": str(manifest_path),
            "manifest_sha256": _sha256(manifest_path),
            "timeline_path": str(timeline_path),
            "timeline_sha256": _sha256(timeline_path),
            "timeline_count": len(timeline),
        },
        "status_source": {
            "kind": "vkp_run_artifact_registry",
            "path": str(registry_path),
            "sha256": _sha256(registry_path),
            "schema": registry.get("schema"),
            "run_count": int(
                registry.get("run_count") or len(registry.get("runs") or [])
            ),
            "status_counts": registry.get("status_counts")
            if isinstance(registry.get("status_counts"), dict)
            else {},
            "runs": _compact_runs(registry.get("runs")),
            "authoritative": True,
            "local_status_machine_created": False,
        },
        "readiness": {
            "status": "ready_for_human_selection"
            if not blockers
            else "needs_vkp_evidence",
            "blockers": blockers,
            "derived_from_vkp_only": True,
        },
        "transcript_contract": {
            "selected_source": transcript_source,
            "selected_path": str(transcript_path)
            if transcript_path
            else "timeline.json",
            "selected_sha256": _sha256(transcript_path)
            if transcript_path
            else _sha256(timeline_path),
            "arbitration_path": str(arbitration_path) if arbitration else "",
            "arbitration_status": _arbitration_status(arbitration),
            "quality_summary": arbitration.get("quality_summary")
            if isinstance(arbitration.get("quality_summary"), dict)
            else {},
            "term_evidence": _term_evidence(smart_input),
            "raw_evidence_mutated": False,
        },
        "candidate_clips": candidates,
        "boundary_refinement": {
            "provider": "videocut-kit",
            "algorithms": ["snap_boundaries", "reclaim_silence"],
            "execution_location": "local",
            "execute": False,
            "status": "awaiting_human_confirmation",
            "input_contract": "confirmed Timeline intervals + VKP transcript tokens + local audio envelope",
            "requests": [
                {
                    "candidate_id": row["candidate_id"],
                    "timeline_indexes": [row["timeline_index"]],
                    "requested_interval": row["source_interval"],
                    "status": "blocked_until_human_confirmation",
                    "execute": False,
                }
                for row in candidates
            ],
        },
        "seam_review": {
            "surface": "vkp_video_workbench",
            "workbench_path": str(workbench_path) if workbench_path else "",
            "new_review_server_created": False,
            "items": [
                {
                    "candidate_id": row["candidate_id"],
                    "timeline_index": row["timeline_index"],
                    "review_window_seconds": 1.5,
                    "temporal_evidence": row["evidence"][
                        "temporal_visual_understanding"
                    ],
                    "evidence_paths": row["evidence"]["paths"],
                    "status": "awaiting_workbench_review",
                }
                for row in candidates
            ],
        },
        "storyboard_scenes": storyboard,
        "post_cut_transcript": {
            "task": "local_asr_service",
            "gateway": "video_knowledge_pipeline.model_task_gateway",
            "status": "deferred_until_rendered_clip",
            "execute": False,
            "requires_rendered_artifact_sha256": True,
            "uses_vkp_arbitration": True,
            "overwrites_source_transcript": False,
        },
        "remote_capabilities": remote_contract,
        "preference_learning": preference_candidates,
        "deferred": {
            "fcpxml_adapter": {
                "status": "deferred",
                "reason": "No stable cross-editor round-trip fixture is available; do not ship an unverified placeholder.",
            }
        },
        "operator_boundary": {
            "execution_mode": "hybrid_local_remote",
            "read_only_bundle": True,
            "online_model_calls": "allowed_with_explicit_authorization",
            "online_model_execution_authorized": remote_contract["execute"],
            "uploads": "allowed_from_explicit_manifest_with_authorization_reference",
            "upload_execution_authorized": remote_contract["upload"]["allowed"],
            "automatic_publish": False,
            "bridge_executes_ffmpeg": False,
            "bridge_executes_asr": False,
            "no_index_build": True,
            "no_review_server": True,
            "no_automatic_local_cloud_fallback": True,
        },
        "source_artifacts": {
            "smart_summary_input_pack": str(smart_input_path) if smart_input else "",
            "smart_summary_chapters": str(chapters_path) if chapters else "",
        },
    }
    validate_edit_job(job)
    return job


def build_preference_candidates(diffs: Iterable[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
    ignored = 0
    for raw in diffs:
        if not isinstance(raw, dict) or not raw.get("human_confirmed"):
            ignored += 1
            continue
        source_hash = str(raw.get("source_video_sha256") or "").lower()
        if not re.fullmatch(r"[0-9a-f]{64}", source_hash):
            ignored += 1
            continue
        category = str(raw.get("category") or "edit").strip()
        direction = str(raw.get("direction") or "").strip()
        rule = str(raw.get("rule") or raw.get("reason") or "").strip()
        if not direction or not rule:
            ignored += 1
            continue
        key = (category, direction, rule)
        row = grouped.setdefault(
            key,
            {
                "candidate_id": f"pref-{len(grouped) + 1:04d}",
                "category": category,
                "direction": direction,
                "rule": rule,
                "evidence_video_sha256": [],
                "explicit_user_pin": False,
            },
        )
        if source_hash not in row["evidence_video_sha256"]:
            row["evidence_video_sha256"].append(source_hash)
        row["explicit_user_pin"] = bool(
            row["explicit_user_pin"] or raw.get("explicit_user_pin")
        )

    candidates = []
    for row in grouped.values():
        evidence_count = len(row["evidence_video_sha256"])
        eligible = evidence_count >= 3 or row["explicit_user_pin"]
        candidates.append(
            {
                **row,
                "evidence_video_count": evidence_count,
                "status": "eligible_for_human_rule_review"
                if eligible
                else "insufficient_cross_video_evidence",
                "eligible": eligible,
                "rule_written": False,
                "requires_human_confirmation": True,
            }
        )
    return {
        "schema": PREFERENCE_SCHEMA,
        "minimum_distinct_videos": 3,
        "accepted_evidence": "human_confirmed_final_diff_only",
        "candidate_count": len(candidates),
        "ignored_diff_count": ignored,
        "candidates": candidates,
        "automatic_rule_write": False,
    }


def validate_edit_job(job: dict[str, Any]) -> None:
    if job.get("schema") != EDIT_JOB_SCHEMA:
        raise ContractError("unsupported edit job schema")
    status = (
        job.get("status_source") if isinstance(job.get("status_source"), dict) else {}
    )
    if status.get("schema") != VKP_REGISTRY_SCHEMA or not status.get("authoritative"):
        raise ContractError(
            "VKP run artifact registry must remain the authoritative status source"
        )
    for row in job.get("candidate_clips") or []:
        interval = row.get("source_interval") if isinstance(row, dict) else {}
        start = float(interval.get("start") or 0)
        end = float(interval.get("end") or 0)
        if end <= start:
            raise ContractError(f"invalid candidate interval: {start}..{end}")
        if row.get("human_confirmed"):
            raise ContractError("the bridge must not auto-confirm Timeline candidates")
    boundary = job.get("boundary_refinement") or {}
    if boundary.get("execute"):
        raise ContractError(
            "boundary refinement execution is owned by the confirmed VKP path"
        )
    remote = job.get("remote_capabilities") or {}
    if remote.get("automatic_fallback"):
        raise ContractError("automatic local/cloud fallback is forbidden")
    if remote.get("automatic_publish"):
        raise ContractError("remote execution must not automatically publish")
    upload = remote.get("upload") if isinstance(remote.get("upload"), dict) else {}
    if upload.get("automatic"):
        raise ContractError("remote uploads must use an explicit artifact manifest")
    if remote.get("execute"):
        validate_remote_contract(remote)
    elif upload.get("allowed"):
        raise ContractError(
            "upload cannot be authorized while remote execution is disabled"
        )
    operator = job.get("operator_boundary") or {}
    if operator.get("execution_mode") != "hybrid_local_remote":
        raise ContractError(
            "operator boundary must declare hybrid local/remote execution"
        )
    if operator.get("online_model_execution_authorized") is not bool(
        remote.get("execute")
    ):
        raise ContractError(
            "operator boundary does not match remote execution authorization"
        )
    if operator.get("upload_execution_authorized") is not bool(upload.get("allowed")):
        raise ContractError("operator boundary does not match upload authorization")
    required_false = (
        "automatic_publish",
        "bridge_executes_ffmpeg",
        "bridge_executes_asr",
    )
    if not all(operator.get(key) is False for key in required_false):
        raise ContractError("operator boundary is incomplete")


def write_edit_job(job: dict[str, Any], output_path: str | Path) -> Path:
    validate_edit_job(job)
    output = Path(output_path).expanduser().resolve()
    bundle = Path(str(job["source_bundle"]["path"])).resolve()
    try:
        output.relative_to(bundle)
    except ValueError:
        pass
    else:
        raise ContractError(
            "video creation output must stay outside the read-only VKP Bundle"
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
    return output


def _timeline_candidates(timeline: list[Any]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    seen: set[int] = set()
    for position, raw in enumerate(timeline, start=1):
        if not isinstance(raw, dict):
            continue
        index = _safe_int(raw.get("index"), position)
        if index in seen:
            raise ContractError(f"timeline index is duplicated: {index}")
        seen.add(index)
        start = _seconds(
            raw.get("review_start")
            if raw.get("review_start") is not None
            else raw.get("start")
        )
        end = _seconds(raw.get("end"), start)
        if end <= start:
            raise ContractError(f"timeline row {index} has an invalid interval")
        temporal = raw.get("human_corrected_temporal_visual_understanding")
        if not isinstance(temporal, dict):
            temporal = raw.get("temporal_visual_understanding")
        if not isinstance(temporal, dict):
            temporal = {}
        candidates.append(
            {
                "candidate_id": f"timeline-{index:04d}",
                "timeline_index": index,
                "source_interval": {"start": start, "end": end},
                "transcript": _timeline_text(raw),
                "visual_text": _first_text(
                    raw.get("human_corrected_visual_text"),
                    raw.get("visual_text"),
                    raw.get("ocr_text"),
                ),
                "visual_route": str(
                    raw.get("visual_route") or raw.get("route") or "unknown"
                ),
                "selection_status": "candidate_only",
                "human_confirmed": False,
                "needs_human_review": bool(
                    raw.get("needs_human_review") or raw.get("quality_issues")
                ),
                "quality_issues": [
                    str(value)
                    for value in raw.get("quality_issues") or []
                    if str(value)
                ],
                "evidence": {
                    "temporal_visual_understanding": temporal,
                    "visual_understanding": raw.get("visual_understanding")
                    if isinstance(raw.get("visual_understanding"), dict)
                    else {},
                    "paths": _evidence_paths(raw),
                    "provenance": "vkp_timeline",
                },
            }
        )
    return candidates


def _storyboard_candidates(
    chapters: dict[str, Any], candidates: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    known = {row["timeline_index"] for row in candidates}
    scenes = []
    for position, raw in enumerate(chapters.get("chapters") or [], start=1):
        if not isinstance(raw, dict):
            continue
        trace = (
            raw.get("evidence_trace")
            if isinstance(raw.get("evidence_trace"), dict)
            else {}
        )
        indexes = [_safe_int(value, 0) for value in trace.get("timeline_indexes") or []]
        indexes = [value for value in indexes if value in known]
        citations = [
            row for row in raw.get("citation_digest") or [] if isinstance(row, dict)
        ]
        if not indexes:
            for citation in citations:
                indexes.extend(
                    value
                    for value in (
                        _safe_int(item, 0)
                        for item in citation.get("timeline_indexes") or []
                    )
                    if value in known
                )
        indexes = list(dict.fromkeys(indexes))
        scenes.append(
            {
                "scene_id": f"storyboard-{position:04d}",
                "source_chapter_index": _safe_int(raw.get("index"), position),
                "title": str(raw.get("title") or f"场景 {position}"),
                "start": _seconds(raw.get("start")),
                "end": _seconds(raw.get("end")),
                "timeline_indexes": indexes,
                "summary_sentences": [
                    str(value)
                    for value in raw.get("summary_sentences") or []
                    if str(value)
                ],
                "visual_notes": [
                    row
                    for row in raw.get("visual_notes") or []
                    if isinstance(row, dict)
                ],
                "citation_digest": citations,
                "status": "candidate_only",
                "human_confirmed": False,
                "decoration_contract": {
                    "status": "blocked_until_scene_confirmation",
                    "items": [],
                },
                "mutates_vkp_timeline": False,
            }
        )
    return scenes


def _select_transcript_source(
    root: Path, manifest: dict[str, Any]
) -> tuple[Path | None, str]:
    for key in TRANSCRIPT_KEYS:
        value = manifest.get(key)
        if not value:
            continue
        path = _artifact_path(root, value, "")
        if path.is_file():
            return path, key
    for name, label in (
        ("human-corrected-transcript.json", "human_corrected_transcript_json"),
        ("agent-readable-transcript.json", "agent_readable_transcript_json"),
        ("corrected-transcript.json", "corrected_transcript_json"),
        ("source-arbitrated-transcript.json", "source_arbitrated_transcript_json"),
        ("normalized-transcript.json", "normalized_transcript_json"),
    ):
        path = root / name
        if path.is_file():
            return path.resolve(), label
    return None, "timeline_fallback"


def _term_evidence(smart_input: dict[str, Any]) -> dict[str, Any]:
    summary = (
        smart_input.get("term_summary")
        if isinstance(smart_input.get("term_summary"), dict)
        else {}
    )
    return {
        "high_confidence_terms": summary.get("high_confidence_terms")
        if isinstance(summary.get("high_confidence_terms"), list)
        else [],
        "needs_review_terms": summary.get("needs_review_terms")
        if isinstance(summary.get("needs_review_terms"), list)
        else [],
        "timeline_replacements": summary.get("timeline_replacements")
        if isinstance(summary.get("timeline_replacements"), list)
        else [],
        "automatic_replacement": False,
    }


def _arbitration_status(payload: dict[str, Any]) -> str:
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    quality = (
        payload.get("quality_summary")
        if isinstance(payload.get("quality_summary"), dict)
        else {}
    )
    value = str(
        payload.get("status")
        or summary.get("quality_status")
        or quality.get("status")
        or "missing"
    )
    return value if value else "missing"


def _compact_runs(value: Any) -> list[dict[str, Any]]:
    result = []
    for row in value or []:
        if not isinstance(row, dict):
            continue
        result.append(
            {
                "run_id": str(row.get("run_id") or ""),
                "run_type": str(row.get("run_type") or ""),
                "status": str(row.get("status") or "unknown"),
                "artifact_count": _safe_int(row.get("artifact_count"), 0),
                "failed_count": _safe_int(row.get("failed_count"), 0),
                "run_json": str(row.get("run_json") or ""),
            }
        )
    return result


def _evidence_paths(row: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for key in ("frame_path", "crop_path", "source_frame_path"):
        value = row.get(key)
        if value:
            values.append(str(value))
    for key in ("frame_paths", "temporal_frame_paths", "evidence_paths"):
        raw = row.get(key)
        if isinstance(raw, list):
            values.extend(str(value) for value in raw if str(value))
    for target in row.get("tile_review_targets") or []:
        if isinstance(target, dict):
            for key in ("path", "crop_path", "frame_path"):
                if target.get(key):
                    values.append(str(target[key]))
    return list(dict.fromkeys(values))[:24]


def _first_existing_path(root: Path, values: Iterable[Any]) -> Path | None:
    for value in values:
        if not value:
            continue
        try:
            path = _artifact_path(root, value, "")
        except ContractError:
            continue
        if path.exists():
            return path
    return None


def _optional_object(
    root: Path, value: Any, fallback: str
) -> tuple[dict[str, Any], Path]:
    path = _artifact_path(root, value, fallback)
    if not path.is_file():
        return {}, path
    return _read_object(path, path.name), path


def _artifact_path(root: Path, value: Any, fallback: str) -> Path:
    raw = str(value or fallback).strip()
    if not raw:
        return root
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    candidate = candidate.resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ContractError(f"Bundle artifact escapes selected root: {raw}") from exc
    return candidate


def _read_object(path: Path, label: str) -> dict[str, Any]:
    if not path.is_file():
        raise ContractError(f"{label} not found: {path}")
    value = _read_json(path, label)
    if not isinstance(value, dict):
        raise ContractError(f"{label} must be a JSON object")
    return value


def _read_list(path: Path, label: str) -> list[Any]:
    if not path.is_file():
        raise ContractError(f"{label} not found: {path}")
    value = _read_json(path, label)
    if not isinstance(value, list):
        raise ContractError(f"{label} must be a JSON array")
    return value


def _read_json(path: Path, label: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"{label} is unreadable: {exc}") from exc


def _sha256(path: Path | None) -> str:
    if not path or not path.is_file():
        return ""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _timeline_text(row: dict[str, Any]) -> str:
    return _first_text(
        row.get("human_corrected_transcript"),
        row.get("corrected_transcript"),
        row.get("transcript"),
        row.get("asr_text"),
        row.get("text"),
    )


def _first_text(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _seconds(value: Any, default: float = 0.0) -> float:
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _strict_bool(value: Any, label: str) -> bool:
    if value is None:
        return False
    if not isinstance(value, bool):
        raise ContractError(f"{label} must be a JSON boolean")
    return value
