from __future__ import annotations

import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .contract_io import (
    artifact_reference,
    atomic_write_json,
    canonical_json_sha256,
    payload_sha256,
    read_json_object,
    validate_artifact_reference,
)
from .errors import ContractError
from .provider_handoff import resolve_human_selection
from .vkp_bridge import validate_edit_job

NLE_MEDIA_MAP_SCHEMA = "video_creation_pipeline.nle_media_map.v1"
PREMIERE_IMPORT_JOB_SCHEMA = "video_creation_pipeline.premiere_import_job.v1"
PREMIERE_IMPORT_RECEIPT_SCHEMA = "video_creation_pipeline.premiere_import_receipt.v1"
PREMIERE_IMPORT_VALIDATION_SCHEMA = (
    "video_creation_pipeline.premiere_import_validation.v1"
)
PREMIERE_PLUGIN_STATUS_SCHEMA = "video_creation_pipeline.premiere_plugin_status.v1"


def build_premiere_import_job(
    edit_job_path: str | Path,
    selection_path: str | Path,
    media_map_path: str | Path,
    *,
    playhead_s: float = 0.0,
    project_id: str = "",
    sequence_id: str = "",
) -> dict[str, Any]:
    confirmed = load_confirmed_nle_segments(
        edit_job_path, selection_path, media_map_path
    )
    return _build_premiere_task(confirmed, playhead_s=playhead_s,
                                project_id=project_id, sequence_id=sequence_id)


def build_spoken_premiere_import_job(
    spoken_edit_plan_path: str | Path,
    delivery_request_path: str | Path,
    *,
    playhead_s: float = 0.0,
    project_id: str = "",
    sequence_id: str = "",
) -> dict[str, Any]:
    from .spoken_delivery import require_spoken_route
    plan, _ = require_spoken_route(spoken_edit_plan_path, delivery_request_path, "premiere")
    confirmed = {
        "source_spoken_edit_plan": artifact_reference(spoken_edit_plan_path, "spoken edit plan"),
        "source_delivery_request": artifact_reference(delivery_request_path, "delivery request"),
        "segments": plan["nle_segments"],
    }
    return _build_premiere_task(confirmed, playhead_s=playhead_s,
                                project_id=project_id, sequence_id=sequence_id)


def _premiere_source_hashes(confirmed: dict[str, Any]) -> dict[str, str]:
    if "source_spoken_edit_plan" in confirmed:
        return {"spoken_edit_plan_sha256": confirmed["source_spoken_edit_plan"]["sha256"],
                "delivery_request_sha256": confirmed["source_delivery_request"]["sha256"]}
    return {"edit_job_sha256": confirmed["source_edit_job"]["sha256"],
            "selection_sha256": confirmed["source_selection"]["sha256"],
            "media_map_sha256": confirmed["source_media_map"]["sha256"]}


def _build_premiere_task(confirmed, *, playhead_s, project_id, sequence_id):
    segments = confirmed["segments"]
    start_at = _nonnegative_number(playhead_s, "playhead_s")
    project_identity = str(project_id).strip()
    sequence_identity = str(sequence_id).strip()
    if bool(project_identity) != bool(sequence_identity):
        raise ContractError(
            "Premiere project_id and sequence_id must be supplied together"
        )
    idempotency_payload = {
        **_premiere_source_hashes(confirmed),
        "mode": "append_at_playhead_new_track",
        "playhead_s": start_at,
        "target_identity": {
            "project_id": project_identity,
            "sequence_id": sequence_identity,
        },
        "segments": segments,
    }
    task: dict[str, Any] = {
        "schema": PREMIERE_IMPORT_JOB_SCHEMA,
        "task_id": str(uuid4()),
        "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "idempotency_key": canonical_json_sha256(idempotency_payload),
        "mode": "append_at_playhead_new_track",
        "playhead_s": start_at,
        "target_identity": {
            "project_id": project_identity,
            "sequence_id": sequence_identity,
        },
        **confirmed,
        "plugin_requirements": {
            "exact_media_path_match": True,
            "basename_fallback_allowed": False,
            "idempotency_receipt_required": True,
            "partial_commit_must_be_reported": True,
            "automatic_retry_after_partial_commit": False,
            "project_locked_access_required": True,
            "compound_transaction_required": True,
            "project_identity_required": True,
            "sequence_identity_required": True,
        },
        "transport": {
            "kind": "atomic_file_queue",
            "queue": "queue/{task_id}.json",
            "done": "done/{task_id}.json",
            "failed": "failed/{task_id}.json",
            "heartbeat": "plugin_status.json",
            "heartbeat_interval_s": 15,
            "online_max_age_s": 60,
        },
        "execution_boundary": {
            "premiere_execution_performed": False,
            "source_media_modified": False,
            "automatic_publish": False,
            "ffmpeg_orchestration_created": False,
            "status_machine_created": False,
        },
        "status": "ready_for_premiere_import"
        if project_identity and sequence_identity
        else "awaiting_target_identity",
    }
    task["task_sha256"] = payload_sha256(task, "task_sha256")
    validate_premiere_import_job(task)
    return task


def load_confirmed_nle_segments(
    edit_job_path: str | Path,
    selection_path: str | Path,
    media_map_path: str | Path,
) -> dict[str, Any]:
    """Resolve one confirmed selection into the shared exact-path NLE segment list."""

    job_path = Path(edit_job_path).expanduser().resolve()
    selection_file = Path(selection_path).expanduser().resolve()
    mapping_path = Path(media_map_path).expanduser().resolve()
    job = read_json_object(job_path, "edit job")
    selection = read_json_object(selection_file, "human selection")
    media_map = read_json_object(mapping_path, "NLE media map")
    validate_edit_job(job)
    edit_reference = artifact_reference(job_path, "edit job")
    selected = resolve_human_selection(selection, job, edit_reference["sha256"])
    segments = _segments(selected, media_map)
    source_selection = artifact_reference(selection_file, "human selection")
    source_media_map = artifact_reference(mapping_path, "NLE media map")
    return {
        "source_edit_job": edit_reference,
        "source_selection": source_selection,
        "source_media_map": source_media_map,
        "segments": segments,
    }


def validate_premiere_import_job(task: dict[str, Any]) -> None:
    if task.get("schema") != PREMIERE_IMPORT_JOB_SCHEMA:
        raise ContractError("unsupported Premiere import job schema")
    if task.get("task_sha256") != payload_sha256(task, "task_sha256"):
        raise ContractError("Premiere import job integrity check failed")
    if "source_spoken_edit_plan" in task:
        if any(key in task for key in ("source_edit_job", "source_selection", "source_media_map")):
            raise ContractError("Premiere source contracts must not be mixed")
        from .spoken_delivery import require_spoken_route
        pp = validate_artifact_reference(task["source_spoken_edit_plan"], "spoken edit plan")
        rp = validate_artifact_reference(task.get("source_delivery_request", {}), "delivery request")
        plan, _ = require_spoken_route(pp, rp, "premiere")
        expected_segments = plan["nle_segments"]
    else:
        if "source_delivery_request" in task:
            raise ContractError("Premiere spoken delivery request needs its spoken plan")
        edit_path = validate_artifact_reference(task.get("source_edit_job", {}), "edit job")
        selection_path = validate_artifact_reference(task.get("source_selection", {}), "human selection")
        media_map_path = validate_artifact_reference(task.get("source_media_map", {}), "NLE media map")
        expected_segments = load_confirmed_nle_segments(edit_path, selection_path, media_map_path)["segments"]
    if task.get("segments") != expected_segments:
        raise ContractError(
            "Premiere import segments differ from the confirmed selection"
        )
    if task.get("mode") != "append_at_playhead_new_track":
        raise ContractError("Premiere import mode is unsupported")
    target = (
        task.get("target_identity")
        if isinstance(task.get("target_identity"), dict)
        else {}
    )
    project_identity = str(target.get("project_id") or "").strip()
    sequence_identity = str(target.get("sequence_id") or "").strip()
    if bool(project_identity) != bool(sequence_identity):
        raise ContractError("Premiere target identity is incomplete")
    expected_status = (
        "ready_for_premiere_import"
        if project_identity and sequence_identity
        else "awaiting_target_identity"
    )
    if task.get("status") != expected_status:
        raise ContractError("Premiere import readiness does not match target identity")
    playhead = _nonnegative_number(task.get("playhead_s"), "playhead_s")
    expected_idempotency = canonical_json_sha256(
        {
            **_premiere_source_hashes(task),
            "mode": task["mode"],
            "playhead_s": playhead,
            "target_identity": {
                "project_id": project_identity,
                "sequence_id": sequence_identity,
            },
            "segments": expected_segments,
        }
    )
    if task.get("idempotency_key") != expected_idempotency:
        raise ContractError("Premiere import idempotency key is invalid")
    requirements = (
        task.get("plugin_requirements")
        if isinstance(task.get("plugin_requirements"), dict)
        else {}
    )
    if (
        requirements.get("exact_media_path_match") is not True
        or requirements.get("basename_fallback_allowed") is not False
        or requirements.get("idempotency_receipt_required") is not True
        or requirements.get("partial_commit_must_be_reported") is not True
        or requirements.get("automatic_retry_after_partial_commit") is not False
        or requirements.get("project_locked_access_required") is not True
        or requirements.get("compound_transaction_required") is not True
        or requirements.get("project_identity_required") is not True
        or requirements.get("sequence_identity_required") is not True
    ):
        raise ContractError("Premiere plugin requirements are incomplete")
    boundary = (
        task.get("execution_boundary")
        if isinstance(task.get("execution_boundary"), dict)
        else {}
    )
    required_false = (
        "premiere_execution_performed",
        "source_media_modified",
        "automatic_publish",
        "ffmpeg_orchestration_created",
        "status_machine_created",
    )
    if not all(boundary.get(field) is False for field in required_false):
        raise ContractError("Premiere import execution boundary is invalid")


def validate_premiere_import_receipt(
    task: dict[str, Any], receipt: dict[str, Any]
) -> dict[str, Any]:
    validate_premiere_import_job(task)
    if receipt.get("schema") != PREMIERE_IMPORT_RECEIPT_SCHEMA:
        raise ContractError("unsupported Premiere import receipt schema")
    if any(
        receipt.get(field) != task.get(field)
        for field in ("task_id", "task_sha256", "idempotency_key")
    ):
        raise ContractError("Premiere receipt does not bind the exact task")
    target = (
        task.get("target_identity")
        if isinstance(task.get("target_identity"), dict)
        else {}
    )
    if (
        not str(target.get("project_id") or "").strip()
        or not str(target.get("sequence_id") or "").strip()
    ):
        raise ContractError(
            "Premiere task requires stable project and sequence ids before execution"
        )
    if receipt.get("target_identity") != target:
        raise ContractError("Premiere receipt target identity mismatch")
    if (
        receipt.get("project_locked_access_used") is not True
        or receipt.get("compound_transaction_used") is not True
    ):
        raise ContractError(
            "Premiere receipt must prove locked access and transaction use"
        )
    if (
        receipt.get("published") is not False
        or receipt.get("source_media_modified") is not False
    ):
        raise ContractError(
            "Premiere receipt violates the publication or media boundary"
        )
    if receipt.get("basename_fallback_used") is not False:
        raise ContractError("Premiere receipt cannot use basename fallback")
    if receipt.get("automatic_retry_performed") is not False:
        raise ContractError(
            "Premiere receipt cannot retry a partial task automatically"
        )
    status = str(receipt.get("status") or "")
    if status not in {"completed", "failed", "cancelled"}:
        raise ContractError("Premiere receipt status is invalid")
    rows = receipt.get("segments")
    if not isinstance(rows, list):
        raise ContractError("Premiere receipt segments must be a list")
    expected = {row["segment_id"]: row for row in task["segments"]}
    if len(rows) != len(
        {str(row.get("segment_id") or "") for row in rows if isinstance(row, dict)}
    ):
        raise ContractError("Premiere receipt contains duplicate segment results")
    applied_count = 0
    cursor = float(task["playhead_s"])
    for row in rows:
        if not isinstance(row, dict):
            raise ContractError("Premiere segment receipt must be an object")
        segment = expected.get(str(row.get("segment_id") or ""))
        if segment is None:
            raise ContractError("Premiere receipt references an unknown segment")
        if (
            row.get("source_sha256") != segment["source_sha256"]
            or row.get("exact_media_path_match") is not True
        ):
            raise ContractError("Premiere segment receipt source binding is invalid")
        applied = row.get("inserted") is True or row.get("already_applied") is True
        if applied:
            applied_count += 1
            if not str(row.get("project_item_id") or "") or not str(
                row.get("sequence_item_id") or ""
            ):
                raise ContractError("applied Premiere segment lacks stable item ids")
            start = _nonnegative_number(row.get("insert_start_s"), "insert_start_s")
            end = _nonnegative_number(row.get("insert_end_s"), "insert_end_s")
            if (
                abs(start - cursor) > 0.001
                or abs(end - (cursor + float(segment["duration_s"]))) > 0.001
            ):
                raise ContractError("Premiere segment insert interval is inconsistent")
            cursor = end
    partial = 0 < applied_count < len(task["segments"])
    if receipt.get("partial_commit_detected") is not partial:
        raise ContractError("Premiere receipt partial commit flag is inaccurate")
    if status == "completed":
        if applied_count != len(task["segments"]) or len(rows) != len(task["segments"]):
            raise ContractError("completed Premiere receipt must apply every segment")
        if receipt.get("duplicate_insertion_detected") is not False:
            raise ContractError(
                "completed Premiere receipt reports a duplicate insertion"
            )
        next_action = "human_timeline_review"
    else:
        if not str(receipt.get("error") or "").strip():
            raise ContractError("failed or cancelled Premiere receipt needs a reason")
        next_action = "manual_partial_commit_recovery" if partial else "operator_review"
    return {
        "schema": PREMIERE_IMPORT_VALIDATION_SCHEMA,
        "valid": True,
        "task_id": task["task_id"],
        "task_sha256": task["task_sha256"],
        "status": status,
        "applied_segment_count": applied_count,
        "partial_commit_detected": partial,
        "next_action": next_action,
    }


def validate_premiere_plugin_status(
    status: dict[str, Any], *, now: datetime | None = None
) -> None:
    if status.get("schema") != PREMIERE_PLUGIN_STATUS_SCHEMA:
        raise ContractError("unsupported Premiere plugin status schema")
    if status.get("ready") is not True:
        raise ContractError("Premiere plugin is not ready")
    updated_at = _parse_datetime(status.get("updated_at"))
    if updated_at is None:
        raise ContractError("Premiere plugin status lacks a valid heartbeat time")
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    if (current.astimezone(timezone.utc) - updated_at).total_seconds() > 60:
        raise ContractError("Premiere plugin heartbeat is stale")
    if PREMIERE_IMPORT_JOB_SCHEMA not in (status.get("supported_schemas") or []):
        raise ContractError("Premiere plugin does not support the import job schema")
    if (
        status.get("exact_media_path_match") is not True
        or status.get("idempotency_supported") is not True
        or status.get("partial_commit_reporting") is not True
        or status.get("project_locked_access_required") is not True
        or status.get("compound_transaction_required") is not True
        or status.get("project_identity_supported") is not True
        or status.get("sequence_identity_supported") is not True
    ):
        raise ContractError("Premiere plugin lacks required safety capabilities")


def verify_premiere_import_files(
    task_path: str | Path, receipt_path: str | Path
) -> dict[str, Any]:
    task = read_json_object(task_path, "Premiere import job")
    receipt = read_json_object(receipt_path, "Premiere import receipt")
    return validate_premiere_import_receipt(task, receipt)


def write_premiere_import_job(task: dict[str, Any], output_path: str | Path) -> Path:
    validate_premiere_import_job(task)
    return atomic_write_json(task, output_path)


def _segments(
    selected: list[dict[str, Any]], media_map: dict[str, Any]
) -> list[dict[str, Any]]:
    if media_map.get("schema") != NLE_MEDIA_MAP_SCHEMA:
        raise ContractError(f"NLE media map must use {NLE_MEDIA_MAP_SCHEMA}")
    raw_assets = media_map.get("assets")
    if not isinstance(raw_assets, list):
        raise ContractError("NLE media map assets must be a list")
    assets: dict[str, dict[str, Any]] = {}
    for row in raw_assets:
        if not isinstance(row, dict):
            raise ContractError("NLE media map asset must be an object")
        candidate_id = str(row.get("candidate_id") or "")
        if not candidate_id or candidate_id in assets:
            raise ContractError(
                "NLE media map candidate ids must be non-empty and unique"
            )
        assets[candidate_id] = row
    segments: list[dict[str, Any]] = []
    for index, item in enumerate(selected, 1):
        candidate_id = str(item["candidate_id"])
        mapped = assets.get(candidate_id)
        if mapped is None:
            raise ContractError(f"NLE media map is missing candidate: {candidate_id}")
        source = artifact_reference(str(mapped.get("path") or ""), "source media")
        interval = item["confirmed_interval"]
        start = float(interval["start"])
        end = float(interval["end"])
        segments.append(
            {
                "segment_id": f"segment-{index:04d}",
                "candidate_id": candidate_id,
                "timeline_index": item["timeline_index"],
                "source_path": source["path"],
                "source_bytes": source["bytes"],
                "source_sha256": source["sha256"],
                "in_s": start,
                "out_s": end,
                "duration_s": round(end - start, 3),
                "label": str(mapped.get("label") or candidate_id),
            }
        )
    return segments


def _nonnegative_number(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise ContractError(f"{label} must be a non-negative finite number")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{label} must be a non-negative finite number") from exc
    if result < 0 or not math.isfinite(result):
        raise ContractError(f"{label} must be a non-negative finite number")
    return round(result, 3)


def _parse_datetime(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)
