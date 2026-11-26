from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from .contract_io import atomic_write_json
from .errors import ContractError
from .smart_reframe import build_smart_reframe_request


EDIT_JOB_V4_SCHEMA = "video_creation_pipeline.vkp_edit_job.v4"
CONSUMER_PLAN_SCHEMA = "video_creation_pipeline.personal_vlog_consumer_plan.v1"


def validate_edit_job_v4(job: Mapping[str, Any]) -> None:
    """Validate the multi-source VKP contract without resolving private paths."""

    if job.get("schema") != EDIT_JOB_V4_SCHEMA:
        raise ContractError("unsupported VKP Vlog edit job schema")
    if job.get("contract_mode") != "multi_source_personal_vlog":
        raise ContractError("VKP Vlog v4 contract mode is invalid")
    source_project = _object(job.get("source_project"), "source project")
    if source_project.get("source_paths_exported") is not False:
        raise ContractError("VKP Vlog v4 must not export source paths")
    if source_project.get("private_project_manifest_required_for_resolution") is not True:
        raise ContractError("VKP Vlog v4 must require a private source resolver")
    recipe = _object(job.get("recipe"), "recipe")
    if recipe.get("preset") not in {"highlight", "standard", "long"}:
        raise ContractError("VKP Vlog v4 preset is invalid")
    if not _sha256_string(recipe.get("recipe_sha256")):
        raise ContractError("VKP Vlog v4 recipe hash is invalid")

    assets = job.get("source_assets")
    if not isinstance(assets, list) or not assets:
        raise ContractError("VKP Vlog v4 requires source assets")
    known: dict[str, Mapping[str, Any]] = {}
    for raw in assets:
        asset = _object(raw, "source asset")
        asset_id = str(asset.get("source_asset_id") or "")
        if not asset_id or asset_id in known:
            raise ContractError("VKP Vlog v4 source asset IDs must be unique")
        if asset.get("media_type") not in {"image", "video", "audio"}:
            raise ContractError("VKP Vlog v4 media type is invalid")
        if not _sha256_string(asset.get("source_sha256")):
            raise ContractError("VKP Vlog v4 source hash is invalid")
        resolver = _object(asset.get("private_resolver"), "private resolver")
        if resolver.get("kind") != "personal_vlog_project_manifest":
            raise ContractError("VKP Vlog v4 resolver kind is invalid")
        if resolver.get("source_path_exported") is not False:
            raise ContractError("VKP Vlog v4 private resolver leaked a source path")
        known[asset_id] = asset

    clips = job.get("candidate_clips")
    if not isinstance(clips, list) or not clips:
        raise ContractError("VKP Vlog v4 requires candidate clips")
    for raw in clips:
        clip = _object(raw, "candidate clip")
        asset_id = str(clip.get("source_asset_id") or "")
        if asset_id not in known:
            raise ContractError("VKP Vlog v4 candidate references an unknown asset")
        if clip.get("status") != "candidate_only" or clip.get("review_required") is not True:
            raise ContractError("VKP Vlog v4 candidates cannot be auto-confirmed")
        duration = _positive_number(
            clip.get("timeline_duration_seconds"), "timeline duration"
        )
        media_type = str(clip.get("media_type") or "")
        if media_type != str(known[asset_id].get("media_type") or ""):
            raise ContractError("VKP Vlog v4 candidate media type drifted")
        if media_type == "image":
            hold = _positive_number(clip.get("hold_seconds"), "photo hold")
            if not 2.0 <= hold <= 4.5 or abs(hold - duration) > 0.001:
                raise ContractError("VKP Vlog v4 photo hold is unsafe")
        else:
            source_range = _object(clip.get("source_range"), "source range")
            start = _nonnegative_number(source_range.get("start_seconds"), "source start")
            end = _positive_number(source_range.get("end_seconds"), "source end")
            if end <= start:
                raise ContractError("VKP Vlog v4 source range is invalid")

    readiness = _object(job.get("readiness"), "readiness")
    if readiness.get("candidate_only") is not True or readiness.get("status") != "review_required":
        raise ContractError("VKP Vlog v4 readiness must remain review-required")
    operator = _object(job.get("operator_boundary"), "operator boundary")
    for key in (
        "bridge_executes_ffmpeg",
        "bridge_executes_asr",
        "bridge_copies_sources",
        "automatic_render",
        "automatic_upload",
        "automatic_publish",
        "source_mutation",
    ):
        if operator.get(key) is not False:
            raise ContractError(f"VKP Vlog v4 unsafe operator setting: {key}")
    if operator.get("human_approval_required") is not True:
        raise ContractError("VKP Vlog v4 human approval is mandatory")
    _reject_private_fields(job)


def build_consumer_plan(job: Mapping[str, Any]) -> dict[str, Any]:
    """Build a non-executing MVP plan that delegates to existing media modules."""

    validate_edit_job_v4(job)
    recipe = _object(job.get("recipe"), "recipe")
    aspect_ratio = str(recipe.get("aspect_ratio") or "")
    assets = {
        str(row["source_asset_id"]): row
        for row in job.get("source_assets") or []
        if isinstance(row, Mapping)
    }
    clips = [dict(row) for row in job.get("candidate_clips") or [] if isinstance(row, Mapping)]

    photo_ids = [
        str(row["source_asset_id"])
        for row in clips
        if row.get("media_type") == "image"
    ]
    layout_requests = [
        {
            "source_asset_id": asset_id,
            "consumer": "video_creation_pipeline.photo_layout",
            "status": "awaiting_private_source_resolution_and_human_approval",
            "crop_allowed": False,
            "automatic_execution": False,
        }
        for asset_id in dict.fromkeys(photo_ids)
    ]
    reframe_requests = []
    if aspect_ratio == "9:16":
        for row in clips:
            if row.get("media_type") not in {"image", "video"}:
                continue
            media_role = "photo" if row.get("media_type") == "image" else "broll"
            request = build_smart_reframe_request(
                profile_id="generic_vertical_9_16",
                media_role=media_role,
            )
            reframe_requests.append(
                {
                    "source_asset_id": row["source_asset_id"],
                    "consumer": "video_creation_pipeline.smart_reframe",
                    "request": request,
                    "status": "awaiting_private_source_resolution_and_human_approval",
                }
            )
    else:
        reframe_requests = [
            {
                "source_asset_id": row["source_asset_id"],
                "consumer": "video_creation_pipeline.smart_reframe",
                "status": "preserve_source_geometry_until_human_review",
                "automatic_execution": False,
            }
            for row in clips
            if row.get("media_type") in {"image", "video"}
        ]

    audio_requests = [
        {
            "source_asset_id": row["source_asset_id"],
            "consumer": "video_creation_pipeline.music_pipeline"
            if row.get("media_type") == "audio"
            else "video_creation_pipeline.audio_mastering",
            "policy": row.get("audio_policy") or {},
            "status": "candidate_only_waiting_human_approval",
            "background_music_must_duck": bool(
                (row.get("audio_policy") or {}).get("background_music_must_duck")
            ),
            "automatic_execution": False,
        }
        for row in clips
        if row.get("media_type") in {"audio", "video"}
    ]
    plan: dict[str, Any] = {
        "schema": CONSUMER_PLAN_SCHEMA,
        "source_job": {
            "schema": job.get("schema"),
            "project_id": _object(job.get("source_project"), "source project").get(
                "project_id"
            ),
            "project_identity_sha256": _object(
                job.get("source_project"), "source project"
            ).get("project_identity_sha256"),
            "recipe_sha256": recipe.get("recipe_sha256"),
            "handoff_sha256": job.get("handoff_sha256"),
        },
        "status": "ready_for_human_review",
        "candidate_only": True,
        "source_asset_count": len(assets),
        "candidate_clip_count": len(clips),
        "timeline": clips,
        "photo_layout_requests": layout_requests,
        "smart_reframe_requests": reframe_requests,
        "audio_requests": audio_requests,
        "render_handoff": {
            "consumer": "existing_single_ffmpeg_outlet",
            "status": "blocked_until_human_approval",
            "execute": False,
        },
        "operator_boundary": {
            "private_source_resolution_required": True,
            "source_paths_exported": False,
            "source_mutation": False,
            "automatic_render": False,
            "automatic_upload": False,
            "automatic_publish": False,
            "new_state_machine_created": False,
        },
    }
    plan["consumer_plan_sha256"] = _sha256_json(plan)
    return plan


def write_consumer_plan(job: Mapping[str, Any], output_path: str | Path) -> dict[str, Any]:
    plan = build_consumer_plan(job)
    atomic_write_json(plan, Path(output_path).expanduser().resolve())
    return plan


def _reject_private_fields(value: Any) -> None:
    forbidden = {
        "source_path",
        "event_root",
        "project_dir",
        "latitude",
        "longitude",
        "gps",
        "appearance_vector",
        "face_embedding",
    }
    if isinstance(value, Mapping):
        for key, child in value.items():
            if str(key).casefold() in forbidden:
                raise ContractError(f"VKP Vlog v4 contains private field: {key}")
            _reject_private_fields(child)
    elif isinstance(value, list):
        for child in value:
            _reject_private_fields(child)


def _object(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractError(f"{label} must be an object")
    return value


def _sha256_string(value: Any) -> bool:
    text = str(value or "")
    return len(text) == 64 and all(char in "0123456789abcdef" for char in text)


def _positive_number(value: Any, label: str) -> float:
    number = _nonnegative_number(value, label)
    if number <= 0:
        raise ContractError(f"{label} must be positive")
    return number


def _nonnegative_number(value: Any, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{label} must be numeric") from exc
    if number < 0:
        raise ContractError(f"{label} must be non-negative")
    return number


def _sha256_json(value: Any) -> str:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()
