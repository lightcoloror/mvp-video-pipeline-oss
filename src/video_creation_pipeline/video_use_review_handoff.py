from __future__ import annotations

import math
import subprocess
from pathlib import Path
from typing import Any

from .content_video_pipeline import VIDEO_PACKAGING_PLAN_SCHEMA
from .contract_io import (
    artifact_reference,
    atomic_write_json,
    payload_sha256,
    read_json_object,
    validate_artifact_reference,
)
from .errors import ContractError

VIDEO_USE_REVIEW_HANDOFF_SCHEMA = "video_creation_pipeline.video_use_review_handoff.v1"
VIDEO_USE_COMMIT = "92c2b34e44c205cbc2acae7f6ca7c1c219d5dd66"
_HELPER = Path("helpers/timeline_view.py")
_SKILL = Path("SKILL.md")


def video_use_review_doctor(source_root: str | Path | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "schema": "video_creation_pipeline.video_use_review_doctor.v1",
        "status": "not_supplied",
        "source_execution_performed": False,
        "ffmpeg_execution_performed": False,
        "transcription_execution_performed": False,
    }
    if source_root is None:
        return result
    try:
        result["source"] = _source(Path(source_root))
        result["status"] = "ready_for_candidate_review_handoff"
    except ContractError as exc:
        result.update({"status": "blocked", "error": str(exc)})
    return result


def build_video_use_review_handoff(
    source_root: str | Path,
    candidate_video_path: str | Path,
    packaging_plan_path: str | Path,
    *,
    window_radius_s: float = 1.5,
) -> dict[str, Any]:
    source = _source(Path(source_root))
    candidate_path = Path(candidate_video_path).expanduser().resolve()
    packaging_path = Path(packaging_plan_path).expanduser().resolve()
    packaging = read_json_object(packaging_path, "video packaging plan")
    _validate_packaging_plan(packaging)
    radius = _positive_number(window_radius_s, "review window radius")
    if radius > 10:
        raise ContractError("review window radius must not exceed 10 seconds")
    duration = _packaging_duration(packaging)
    payload: dict[str, Any] = {
        "schema": VIDEO_USE_REVIEW_HANDOFF_SCHEMA,
        "video_use": source,
        "candidate_video": artifact_reference(candidate_path, "candidate video"),
        "source_packaging_plan": artifact_reference(
            packaging_path, "video packaging plan"
        ),
        "review_windows": _review_windows(packaging, duration, radius),
        "review_checks": [
            "visual_discontinuity_or_flash",
            "boundary_audio_pop_or_spike",
            "subtitle_occlusion_by_overlay",
            "overlay_timing_or_frame_offset",
            "first_and_last_frame_coherence",
        ],
        "execution_constraints": {
            "external_helper": "helpers/timeline_view.py",
            "max_self_evaluation_passes": 3,
            "review_is_on_demand_not_background_indexing": True,
            "transcription_requested": False,
            "edl_mutation_allowed": False,
            "render_execution_allowed": False,
            "single_ffmpeg_outlet_required_for_any_fix": True,
        },
        "authority_boundary": {
            "candidate_only": True,
            "source_execution_performed": False,
            "ffmpeg_execution_performed": False,
            "transcription_execution_performed": False,
            "source_media_modified": False,
            "automatic_publish": False,
            "second_ffmpeg_orchestrator_created": False,
        },
    }
    payload["handoff_sha256"] = payload_sha256(payload, "handoff_sha256")
    validate_video_use_review_handoff(payload)
    return payload


def validate_video_use_review_handoff(payload: dict[str, Any]) -> None:
    if payload.get("schema") != VIDEO_USE_REVIEW_HANDOFF_SCHEMA:
        raise ContractError("unsupported video-use review handoff schema")
    if payload.get("handoff_sha256") != payload_sha256(payload, "handoff_sha256"):
        raise ContractError("video-use review handoff integrity check failed")
    _validate_source(_object(payload.get("video_use")))
    validate_artifact_reference(
        _object(payload.get("candidate_video")), "candidate video"
    )
    packaging_path = validate_artifact_reference(
        _object(payload.get("source_packaging_plan")), "video packaging plan"
    )
    packaging = read_json_object(packaging_path, "video packaging plan")
    _validate_packaging_plan(packaging)
    duration = _packaging_duration(packaging)
    windows = payload.get("review_windows")
    if not isinstance(windows, list) or not windows:
        raise ContractError("video-use review handoff requires review windows")
    _validate_windows(windows, duration)
    if payload.get("review_checks") != [
        "visual_discontinuity_or_flash",
        "boundary_audio_pop_or_spike",
        "subtitle_occlusion_by_overlay",
        "overlay_timing_or_frame_offset",
        "first_and_last_frame_coherence",
    ]:
        raise ContractError("video-use review checks are invalid")
    if _object(payload.get("execution_constraints")) != {
        "external_helper": "helpers/timeline_view.py",
        "max_self_evaluation_passes": 3,
        "review_is_on_demand_not_background_indexing": True,
        "transcription_requested": False,
        "edl_mutation_allowed": False,
        "render_execution_allowed": False,
        "single_ffmpeg_outlet_required_for_any_fix": True,
    }:
        raise ContractError("video-use review execution constraints are invalid")
    if _object(payload.get("authority_boundary")) != {
        "candidate_only": True,
        "source_execution_performed": False,
        "ffmpeg_execution_performed": False,
        "transcription_execution_performed": False,
        "source_media_modified": False,
        "automatic_publish": False,
        "second_ffmpeg_orchestrator_created": False,
    }:
        raise ContractError("video-use review authority boundary is invalid")


def write_video_use_review_handoff(
    payload: dict[str, Any], output_path: str | Path
) -> Path:
    validate_video_use_review_handoff(payload)
    return atomic_write_json(payload, output_path)


def _review_windows(
    packaging: dict[str, Any], duration: float, radius: float
) -> list[dict[str, Any]]:
    scenes = packaging["scenes"]
    points: list[tuple[str, float]] = [("opening", 0.0)]
    for previous, current in zip(scenes, scenes[1:]):
        points.append(
            (
                f"cut:{previous['scene_id']}->{current['scene_id']}",
                float(current["start_s"]),
            )
        )
    points.append(("ending", duration))
    return [
        {
            "window_id": f"review-{index + 1:03d}",
            "kind": kind,
            "focus_s": round(point, 3),
            "start_s": round(max(0.0, point - radius), 3),
            "end_s": round(min(duration, point + radius), 3),
        }
        for index, (kind, point) in enumerate(points)
    ]


def _validate_windows(windows: list[Any], duration: float) -> None:
    for index, raw in enumerate(windows):
        row = _object(raw)
        if row.get("window_id") != f"review-{index + 1:03d}":
            raise ContractError("video-use review window ids are invalid")
        start = _nonnegative_number(row.get("start_s"), "review window start")
        end = _nonnegative_number(row.get("end_s"), "review window end")
        focus = _nonnegative_number(row.get("focus_s"), "review window focus")
        if start > focus or focus > end or end > duration + 0.001:
            raise ContractError("video-use review window bounds are invalid")
        if not str(row.get("kind") or "").strip():
            raise ContractError("video-use review window kind is invalid")


def _validate_packaging_plan(plan: dict[str, Any]) -> None:
    if plan.get("schema") != VIDEO_PACKAGING_PLAN_SCHEMA:
        raise ContractError("video-use review requires a video packaging plan")
    scenes = plan.get("scenes")
    if not isinstance(scenes, list) or not scenes:
        raise ContractError("video-use review packaging plan requires scenes")
    cursor = 0.0
    for raw in scenes:
        row = _object(raw)
        start = _nonnegative_number(row.get("start_s"), "packaging scene start")
        end = _positive_number(row.get("end_s"), "packaging scene end")
        if end <= start or abs(start - cursor) > 0.001:
            raise ContractError("video-use review packaging scenes must be contiguous")
        if not str(row.get("scene_id") or "").strip():
            raise ContractError("video-use review packaging scene id is invalid")
        cursor = end


def _packaging_duration(plan: dict[str, Any]) -> float:
    return float(plan["scenes"][-1]["end_s"])


def _source(root: Path) -> dict[str, Any]:
    root = root.expanduser().resolve()
    skill = root / _SKILL
    helper = root / _HELPER
    if not skill.is_file() or not helper.is_file():
        raise ContractError("video-use reviewed skill or timeline helper is missing")
    if _git_head(root) != VIDEO_USE_COMMIT:
        raise ContractError("video-use source commit drifted")
    return {
        "source_commit": VIDEO_USE_COMMIT,
        "source_license": "MIT",
        "skill_file": artifact_reference(skill, "video-use skill"),
        "timeline_helper": artifact_reference(helper, "video-use timeline helper"),
        "reuse_mode": "review_handoff_only_no_transcription_or_render_execution",
    }


def _validate_source(source: dict[str, Any]) -> None:
    if (
        source.get("source_commit") != VIDEO_USE_COMMIT
        or source.get("source_license") != "MIT"
        or source.get("reuse_mode")
        != "review_handoff_only_no_transcription_or_render_execution"
    ):
        raise ContractError("video-use source is not the reviewed source")
    helper = validate_artifact_reference(
        _object(source.get("timeline_helper")), "video-use timeline helper"
    )
    validate_artifact_reference(_object(source.get("skill_file")), "video-use skill")
    _source(helper.parents[1])


def _git_head(root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ContractError("video-use source commit could not be read") from exc
    if result.returncode != 0:
        raise ContractError("video-use source commit could not be read")
    return result.stdout.strip().lower()


def _object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError("expected an object")
    return value


def _positive_number(value: Any, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{label} must be a positive number") from exc
    if not math.isfinite(number) or number <= 0:
        raise ContractError(f"{label} must be a positive number")
    return number


def _nonnegative_number(value: Any, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{label} must be a non-negative number") from exc
    if not math.isfinite(number) or number < 0:
        raise ContractError(f"{label} must be a non-negative number")
    return number
