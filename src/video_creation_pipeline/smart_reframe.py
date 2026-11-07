from __future__ import annotations

import math
from collections import defaultdict
from pathlib import Path
from statistics import median
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError, ValidationError

from .contract_io import (
    artifact_reference,
    atomic_copy_promote,
    atomic_write_json,
    payload_sha256,
    read_json_object,
    validate_artifact_reference,
)
from .errors import ContractError
from .execution_receipts import (
    execute_ffmpeg_with_receipt,
    validate_ffmpeg_execution_receipt,
)
from .reframe_detectors import (
    source_media_dimensions,
    validate_smart_reframe_evidence,
)
from .reframe_profiles import get_reframe_profile
from .reframe_tracking import smooth_crop_keyframes

SMART_REFRAME_REQUEST_SCHEMA = "video_creation_pipeline.smart_reframe_request.v1"
SMART_REFRAME_PLAN_SCHEMA = "video_creation_pipeline.smart_reframe_plan.v1"
SMART_REFRAME_RENDER_RECEIPT_SCHEMA = (
    "video_creation_pipeline.smart_reframe_render_receipt.v1"
)

_CROP_STRATEGIES = {"fixed_crop", "tracked_crop", "speaker_cut", "roi_sequence"}
_PAD_STRATEGIES = {"contain", "blur_pad"}
_ALLOWED_STRATEGIES = (
    _CROP_STRATEGIES
    | _PAD_STRATEGIES
    | {
        "split_layout",
        "reject",
    }
)


def build_smart_reframe_request(
    *,
    profile_id: str,
    media_role: str,
    fallback_order: list[str] | None = None,
    deadband: float = 0.035,
    minimum_dwell_s: float = 0.35,
    max_speed_per_s: float = 0.6,
    max_acceleration_per_s2: float = 1.8,
    max_upscale_factor: float = 1.5,
    production_project_type: str | None = None,
    composition_review_path: str | Path | None = None,
) -> dict[str, Any]:
    request: dict[str, Any] = {
        "schema": SMART_REFRAME_REQUEST_SCHEMA,
        "profile_id": str(profile_id),
        "media_role": str(media_role),
        "fallback_order": list(fallback_order or _default_fallbacks(media_role)),
        "motion_policy": {
            "deadband": float(deadband),
            "minimum_dwell_s": float(minimum_dwell_s),
            "max_speed_per_s": float(max_speed_per_s),
            "max_acceleration_per_s2": float(max_acceleration_per_s2),
            "reset_at_shot_boundary": True,
        },
        "quality_policy": {
            "required_region_retention": 1.0,
            "max_upscale_factor": float(max_upscale_factor),
            "safe_zone_violations_allowed": 0,
            "blind_crop_allowed": False,
        },
        "execution_policy": {
            "candidate_only": True,
            "automatic_crop_enabled": False,
            "requires_explicit_candidate_execution": True,
            "automatic_publish": False,
        },
    }
    if production_project_type is not None:
        request["production_project_type"] = production_project_type
    if composition_review_path is not None:
        request["composition_review"] = artifact_reference(composition_review_path, "composition review")
    request["request_sha256"] = payload_sha256(request, "request_sha256")
    validate_smart_reframe_request(request)
    return request


def validate_smart_reframe_request(request: dict[str, Any]) -> None:
    _validate_schema(request, "smart_reframe_request.v1.schema.json", "reframe request")
    if request.get("schema") != SMART_REFRAME_REQUEST_SCHEMA:
        raise ContractError("unsupported smart reframe request schema")
    if request.get("request_sha256") != payload_sha256(request, "request_sha256"):
        raise ContractError("smart reframe request integrity check failed")
    get_reframe_profile(str(request.get("profile_id") or ""))
    if "composition_review" in request or "production_project_type" in request:
        from .production_presets import profile
        profile(request.get("production_project_type"))
        if "composition_review" not in request:
            raise ContractError("project-specific crop requires composition review")
        validate_artifact_reference(request["composition_review"], "composition review")
    if request.get("media_role") not in {
        "talking_head",
        "interview",
        "presentation",
        "screen_recording",
        "document",
        "chart",
        "photo",
        "broll",
    }:
        raise ContractError("unsupported smart reframe media role")
    fallbacks = request.get("fallback_order")
    if not isinstance(fallbacks, list) or not fallbacks:
        raise ContractError("smart reframe request requires fallback strategies")
    if len(set(fallbacks)) != len(fallbacks) or any(
        value not in _ALLOWED_STRATEGIES for value in fallbacks
    ):
        raise ContractError("smart reframe fallback order is invalid")
    motion = _object(request.get("motion_policy"), "motion policy")
    if motion.get("reset_at_shot_boundary") is not True:
        raise ContractError("smart reframe motion must reset at shot boundaries")
    for field in (
        "deadband",
        "minimum_dwell_s",
        "max_speed_per_s",
        "max_acceleration_per_s2",
    ):
        value = _number(motion.get(field), f"motion {field}")
        if value < 0 or (field.startswith("max_") and value <= 0):
            raise ContractError(f"motion {field} is invalid")
    quality = _object(request.get("quality_policy"), "quality policy")
    if (
        float(quality.get("required_region_retention", 0)) != 1.0
        or int(quality.get("safe_zone_violations_allowed", -1)) != 0
        or quality.get("blind_crop_allowed") is not False
        or _number(quality.get("max_upscale_factor"), "max upscale factor") < 1
    ):
        raise ContractError("smart reframe quality policy is unsafe")
    if request.get("execution_policy") != {
        "candidate_only": True,
        "automatic_crop_enabled": False,
        "requires_explicit_candidate_execution": True,
        "automatic_publish": False,
    }:
        raise ContractError("smart reframe execution policy drifted")


def build_smart_reframe_plan(
    source_media_path: str | Path,
    request_path: str | Path,
    *,
    evidence_paths: list[str | Path] | None = None,
) -> dict[str, Any]:
    source = Path(source_media_path).expanduser().resolve()
    request_file = Path(request_path).expanduser().resolve()
    request = read_json_object(request_file, "smart reframe request")
    validate_smart_reframe_request(request)
    source_ref = artifact_reference(source, "smart reframe source media")
    _production_composition(request, source)
    evidence_files: list[Path] = []
    evidence_payloads: list[dict[str, Any]] = []
    for raw in evidence_paths or []:
        path = Path(raw).expanduser().resolve()
        payload = read_json_object(path, "smart reframe evidence")
        validate_smart_reframe_evidence(payload)
        if payload.get("source_media") != source_ref:
            raise ContractError("smart reframe evidence binds a different source media")
        evidence_files.append(path)
        evidence_payloads.append(payload)
    metadata = source_media_dimensions(source)
    profile = get_reframe_profile(str(request["profile_id"]))
    observations = [
        row for payload in evidence_payloads for row in payload.get("observations", [])
    ]
    shot_intervals = _shot_intervals(observations, float(metadata["duration_s"]))
    planned_shots = [
        _plan_shot(
            index,
            start_s,
            end_s,
            observations,
            source_metadata=metadata,
            profile=profile,
            request=request,
        )
        for index, (start_s, end_s) in enumerate(shot_intervals, start=1)
    ]
    blocked = [
        row["shot_id"]
        for row in planned_shots
        if row["strategy"] == "reject"
        or row["quality_estimate"]["quality_gate_passed"] is not True
    ]
    fallback = [
        row["shot_id"] for row in planned_shots if row["strategy"] in _PAD_STRATEGIES
    ]
    plan: dict[str, Any] = {
        "schema": SMART_REFRAME_PLAN_SCHEMA,
        "inputs": {
            "source_media": source_ref,
            "request": artifact_reference(request_file, "smart reframe request"),
            "evidence": [
                artifact_reference(path, "smart reframe evidence")
                for path in evidence_files
            ],
        },
        "source_metadata": metadata,
        "target_profile": profile,
        "shots": planned_shots,
        "summary": {
            "shot_count": len(planned_shots),
            "crop_shot_count": sum(
                row["strategy"] in _CROP_STRATEGIES for row in planned_shots
            ),
            "fallback_shot_ids": fallback,
            "blocked_shot_ids": blocked,
            "evidence_observation_count": len(observations),
            "status": "blocked" if blocked else "candidate_ready",
        },
        "renderer_handoff": {
            "single_ffmpeg_outlet_required": True,
            "mixed_strategy_render_supported": False,
            "static_image_render_supported": True,
            "candidate_execution_requires_explicit_command": True,
            "automatic_render": False,
        },
        "authority_boundary": _plan_boundary(),
    }
    plan["plan_sha256"] = payload_sha256(plan, "plan_sha256")
    validate_smart_reframe_plan(plan)
    return plan


def _production_composition(request, source):
    if "composition_review" in request:
        from .production_presets import validate_composition_review
        path = validate_artifact_reference(request["composition_review"], "composition review")
        validate_composition_review(source, read_json_object(path, "composition review"), request["production_project_type"])


def validate_smart_reframe_plan(plan: dict[str, Any]) -> None:
    _validate_schema(plan, "smart_reframe_plan.v1.schema.json", "reframe plan")
    if plan.get("schema") != SMART_REFRAME_PLAN_SCHEMA:
        raise ContractError("unsupported smart reframe plan schema")
    if plan.get("plan_sha256") != payload_sha256(plan, "plan_sha256"):
        raise ContractError("smart reframe plan integrity check failed")
    inputs = _object(plan.get("inputs"), "reframe inputs")
    source_path = validate_artifact_reference(
        _object(inputs.get("source_media"), "source media reference"),
        "smart reframe source media",
    )
    request_path = validate_artifact_reference(
        _object(inputs.get("request"), "request reference"),
        "smart reframe request",
    )
    request = read_json_object(request_path, "smart reframe request")
    validate_smart_reframe_request(request)
    _production_composition(request, source_path)
    evidence_refs = inputs.get("evidence")
    if not isinstance(evidence_refs, list):
        raise ContractError("smart reframe plan evidence references must be a list")
    source_ref = inputs["source_media"]
    observations: list[dict[str, Any]] = []
    for index, reference in enumerate(evidence_refs, start=1):
        evidence_path = validate_artifact_reference(
            _object(reference, f"evidence reference {index}"),
            f"smart reframe evidence {index}",
        )
        evidence = read_json_object(evidence_path, "smart reframe evidence")
        validate_smart_reframe_evidence(evidence)
        if evidence.get("source_media") != source_ref:
            raise ContractError("smart reframe plan evidence source drifted")
        observations.extend(evidence.get("observations", []))
    metadata = source_media_dimensions(source_path)
    if plan.get("source_metadata") != metadata:
        raise ContractError("smart reframe source metadata drifted")
    profile = get_reframe_profile(str(request["profile_id"]))
    if plan.get("target_profile") != profile:
        raise ContractError("smart reframe profile drifted")
    expected_shots = [
        _plan_shot(
            index,
            start_s,
            end_s,
            observations,
            source_metadata=metadata,
            profile=profile,
            request=request,
        )
        for index, (start_s, end_s) in enumerate(
            _shot_intervals(observations, float(metadata["duration_s"])), start=1
        )
    ]
    if plan.get("shots") != expected_shots:
        raise ContractError("smart reframe shot decisions drifted from their evidence")
    blocked = [
        row["shot_id"]
        for row in expected_shots
        if row["strategy"] == "reject"
        or row["quality_estimate"]["quality_gate_passed"] is not True
    ]
    fallback = [
        row["shot_id"] for row in expected_shots if row["strategy"] in _PAD_STRATEGIES
    ]
    expected_summary = {
        "shot_count": len(expected_shots),
        "crop_shot_count": sum(
            row["strategy"] in _CROP_STRATEGIES for row in expected_shots
        ),
        "fallback_shot_ids": fallback,
        "blocked_shot_ids": blocked,
        "evidence_observation_count": len(observations),
        "status": "blocked" if blocked else "candidate_ready",
    }
    if plan.get("summary") != expected_summary:
        raise ContractError("smart reframe plan summary drifted")
    if plan.get("renderer_handoff") != {
        "single_ffmpeg_outlet_required": True,
        "mixed_strategy_render_supported": False,
        "static_image_render_supported": True,
        "candidate_execution_requires_explicit_command": True,
        "automatic_render": False,
    }:
        raise ContractError("smart reframe renderer handoff drifted")
    if plan.get("authority_boundary") != _plan_boundary():
        raise ContractError("smart reframe authority boundary drifted")


def write_smart_reframe_contract(
    payload: dict[str, Any], output_path: str | Path
) -> Path:
    schema = payload.get("schema")
    if schema == SMART_REFRAME_REQUEST_SCHEMA:
        validate_smart_reframe_request(payload)
    elif schema == SMART_REFRAME_PLAN_SCHEMA:
        validate_smart_reframe_plan(payload)
    elif schema == SMART_REFRAME_RENDER_RECEIPT_SCHEMA:
        validate_smart_reframe_render_receipt(payload)
    else:
        raise ContractError("unsupported smart reframe contract")
    return atomic_write_json(payload, output_path)


def build_smart_reframe_render_command(
    plan: dict[str, Any], source: Path, staged_output: Path
) -> list[str]:
    validate_smart_reframe_plan(plan)
    strategies = {row["strategy"] for row in plan["shots"]}
    width = int(plan["target_profile"]["width"])
    height = int(plan["target_profile"]["height"])
    command = ["ffmpeg", "-y", "-hide_banner"]
    if plan["source_metadata"].get("media_kind") == "image":
        command.extend(["-loop", "1", "-framerate", "30"])
    command.extend(["-i", str(source)])
    if strategies <= _CROP_STRATEGIES:
        keyframes = [frame for shot in plan["shots"] for frame in shot["keyframes"]]
        if not keyframes:
            raise ContractError("crop render requires keyframes")
        source_width = int(plan["source_metadata"]["width"])
        source_height = int(plan["source_metadata"]["height"])
        crop_width = _even(int(round(keyframes[0]["crop"]["width"] * source_width)))
        crop_height = _even(int(round(keyframes[0]["crop"]["height"] * source_height)))
        if any(
            _even(int(round(frame["crop"]["width"] * source_width))) != crop_width
            or _even(int(round(frame["crop"]["height"] * source_height))) != crop_height
            for frame in keyframes
        ):
            raise ContractError("crop renderer requires one crop size across the plan")
        x_points = [
            (
                float(row["time_s"]),
                float(row["crop"]["x"]) * source_width,
                shot["interpolation"] == "hold",
            )
            for shot in plan["shots"]
            for row in shot["keyframes"]
        ]
        y_points = [
            (
                float(row["time_s"]),
                float(row["crop"]["y"]) * source_height,
                shot["interpolation"] == "hold",
            )
            for shot in plan["shots"]
            for row in shot["keyframes"]
        ]
        filter_graph = (
            f"crop={crop_width}:{crop_height}:"
            f"x='{_piecewise_expression(x_points)}':"
            f"y='{_piecewise_expression(y_points)}',"
            f"scale={width}:{height}:flags=lanczos,setsar=1"
        )
        command.extend(["-vf", filter_graph])
    elif strategies == {"contain"}:
        command.extend(
            [
                "-vf",
                f"scale={width}:{height}:force_original_aspect_ratio=decrease:"
                f"flags=lanczos,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:"
                "color=black,setsar=1",
            ]
        )
    elif strategies == {"blur_pad"}:
        foreground_width = _even(int(round(width * 0.97)))
        foreground_height = _even(int(round(height * 0.97)))
        graph = (
            f"[0:v]split=2[bg][fg];[bg]scale={width}:{height}:"
            f"force_original_aspect_ratio=increase:flags=lanczos,crop={width}:{height},"
            "gblur=sigma=30[bg2];"
            f"[fg]scale={foreground_width}:{foreground_height}:"
            "force_original_aspect_ratio=decrease:"
            "flags=lanczos[fg2];"
            "[bg2][fg2]overlay=(W-w)/2:(H-h)/2,setsar=1[v]"
        )
        command.extend(["-filter_complex", graph, "-map", "[v]"])
    else:
        raise ContractError(
            "mixed or split-layout reframe rendering is not verified; "
            "render per shot after plan review"
        )
    if "-map" not in command:
        command.extend(["-map", "0:v:0"])
    command.extend(
        [
            "-map",
            "0:a?",
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
        ]
    )
    if plan["source_metadata"].get("media_kind") == "image":
        command.extend(["-t", "1", "-r", "30"])
    command.append(str(staged_output))
    return command


def build_smart_reframe_debug_overlay_command(
    plan: dict[str, Any], source: Path, staged_output: Path
) -> list[str]:
    """Build a source-space crop overlay through the shared FFmpeg outlet."""

    validate_smart_reframe_plan(plan)
    strategies = {row["strategy"] for row in plan["shots"]}
    if not strategies <= _CROP_STRATEGIES:
        raise ContractError(
            "debug crop overlay requires a crop-only plan; padding and mixed plans "
            "remain visible in the JSON QA report"
        )
    keyframes = [frame for shot in plan["shots"] for frame in shot["keyframes"]]
    if not keyframes:
        raise ContractError("debug crop overlay requires crop keyframes")
    source_width = int(plan["source_metadata"]["width"])
    source_height = int(plan["source_metadata"]["height"])
    crop_width = _even(int(round(keyframes[0]["crop"]["width"] * source_width)))
    crop_height = _even(int(round(keyframes[0]["crop"]["height"] * source_height)))
    if any(
        _even(int(round(frame["crop"]["width"] * source_width))) != crop_width
        or _even(int(round(frame["crop"]["height"] * source_height))) != crop_height
        for frame in keyframes
    ):
        raise ContractError("debug crop overlay requires one crop size across the plan")
    x_points = [
        (
            float(row["time_s"]),
            float(row["crop"]["x"]) * source_width,
            shot["interpolation"] == "hold",
        )
        for shot in plan["shots"]
        for row in shot["keyframes"]
    ]
    y_points = [
        (
            float(row["time_s"]),
            float(row["crop"]["y"]) * source_height,
            shot["interpolation"] == "hold",
        )
        for shot in plan["shots"]
        for row in shot["keyframes"]
    ]
    command = ["ffmpeg", "-y", "-hide_banner"]
    if plan["source_metadata"].get("media_kind") == "image":
        command.extend(["-loop", "1", "-framerate", "30"])
    command.extend(
        [
            "-i",
            str(source),
            "-vf",
            f"drawbox=x='{_piecewise_expression(x_points)}':"
            f"y='{_piecewise_expression(y_points)}':"
            f"w={crop_width}:h={crop_height}:color=lime@0.9:t=4",
            "-map",
            "0:v:0",
            "-map",
            "0:a?",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-crf",
            "20",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-movflags",
            "+faststart",
        ]
    )
    if plan["source_metadata"].get("media_kind") == "image":
        command.extend(["-t", "1", "-r", "30"])
    command.append(str(staged_output))
    return command


def render_smart_reframe_debug_overlay(
    plan_path: str | Path,
    output_video_path: str | Path,
    work_dir: str | Path,
    *,
    timeout_s: float = 300.0,
    annotator: str = "crop",
    font_path: str | Path | None = None,
    privacy_zones_path: str | Path | None = None,
    diagnostic_evidence_path: str | Path | None = None,
) -> dict[str, Any]:
    """Render a candidate-only crop overlay without granting release authority."""

    if annotator == "supervision":
        from .reframe_visual_qa import render_supervision_debug

        return render_supervision_debug(
            plan_path,
            output_video_path,
            work_dir,
            font_path=font_path,
            privacy_zones_path=privacy_zones_path,
            diagnostic_evidence_path=diagnostic_evidence_path,
            timeout_s=timeout_s,
        )
    if (
        annotator != "crop"
        or privacy_zones_path is not None
        or font_path is not None
        or diagnostic_evidence_path is not None
    ):
        raise ContractError(
            "font/privacy overlays require the explicit supervision annotator"
        )
    plan_file = Path(plan_path).expanduser().resolve()
    plan = read_json_object(plan_file, "smart reframe plan")
    validate_smart_reframe_plan(plan)
    source = validate_artifact_reference(
        _object(plan["inputs"]["source_media"], "source media reference"),
        "smart reframe source media",
    )
    root = Path(work_dir).expanduser().resolve()
    output = Path(output_video_path).expanduser().resolve()
    staged = root / "smart-reframe-debug-staging.mp4"
    receipt_path = root / "smart-reframe-debug-ffmpeg-receipt.json"
    protected = {staged, receipt_path, output}
    if len(protected) != 3 or source in protected or plan_file in protected:
        raise ContractError("smart reframe debug paths overlap")
    for path in protected:
        if path.exists():
            raise ContractError(f"smart reframe debug output already exists: {path}")
    root.mkdir(parents=True, exist_ok=True)
    command = build_smart_reframe_debug_overlay_command(plan, source, staged)
    receipt = execute_ffmpeg_with_receipt(
        "render",
        command,
        [
            {"role": "smart_reframe_debug_source", "path": source},
            {"role": "smart_reframe_debug_plan", "path": plan_file},
        ],
        [{"role": "smart_reframe_debug_staging", "path": staged}],
        receipt_path,
        timeout_s=timeout_s,
    )
    atomic_copy_promote(staged, output, label="smart reframe debug overlay")
    return {
        "status": "completed_debug_candidate_only",
        "debug_video": artifact_reference(output, "smart reframe debug overlay"),
        "ffmpeg_receipt": artifact_reference(
            receipt_path, "smart reframe debug FFmpeg receipt"
        ),
        "ffmpeg_receipt_sha256": receipt["receipt_sha256"],
        "candidate_only": True,
        "automatic_publish": False,
    }


def render_smart_reframe_candidate(
    plan_path: str | Path,
    output_video_path: str | Path,
    work_dir: str | Path,
    *,
    timeout_s: float = 300.0,
) -> dict[str, Any]:
    plan_file = Path(plan_path).expanduser().resolve()
    plan = read_json_object(plan_file, "smart reframe plan")
    validate_smart_reframe_plan(plan)
    if plan["summary"]["status"] != "candidate_ready":
        raise ContractError("blocked smart reframe plan cannot be rendered")
    source = validate_artifact_reference(
        _object(plan["inputs"]["source_media"], "source media reference"),
        "smart reframe source media",
    )
    root = Path(work_dir).expanduser().resolve()
    output = Path(output_video_path).expanduser().resolve()
    staged = root / "smart-reframe-staging.mp4"
    ffmpeg_receipt_path = root / "smart-reframe-ffmpeg-receipt.json"
    final_receipt_path = root / "smart-reframe-render-receipt.json"
    protected = {staged, ffmpeg_receipt_path, final_receipt_path, output}
    if len(protected) != 4 or source in protected or plan_file in protected:
        raise ContractError("smart reframe render paths overlap")
    for path in protected:
        if path.exists():
            raise ContractError(f"smart reframe render output already exists: {path}")
    root.mkdir(parents=True, exist_ok=True)
    command = build_smart_reframe_render_command(plan, source, staged)
    execute_ffmpeg_with_receipt(
        "render",
        command,
        [
            {"role": "smart_reframe_source", "path": source},
            {"role": "smart_reframe_plan", "path": plan_file},
        ],
        [{"role": "smart_reframe_staging", "path": staged}],
        ffmpeg_receipt_path,
        timeout_s=timeout_s,
    )
    atomic_copy_promote(staged, output, label="smart reframe candidate")
    receipt: dict[str, Any] = {
        "schema": SMART_REFRAME_RENDER_RECEIPT_SCHEMA,
        "status": "completed_candidate_only",
        "source_plan": artifact_reference(plan_file, "smart reframe plan"),
        "source_plan_sha256": plan["plan_sha256"],
        "ffmpeg_receipt": artifact_reference(
            ffmpeg_receipt_path, "smart reframe FFmpeg receipt"
        ),
        "staged_video": artifact_reference(staged, "smart reframe staged video"),
        "candidate_video": artifact_reference(output, "smart reframe candidate video"),
        "authority_boundary": {
            "candidate_only": True,
            "technical_completion_is_not_release_approval": True,
            "single_ffmpeg_outlet_used": True,
            "source_media_modified": False,
            "automatic_publish": False,
        },
    }
    receipt["receipt_sha256"] = payload_sha256(receipt, "receipt_sha256")
    validate_smart_reframe_render_receipt(receipt)
    atomic_write_json(receipt, final_receipt_path)
    return receipt


def validate_smart_reframe_render_receipt(receipt: dict[str, Any]) -> None:
    if receipt.get("schema") != SMART_REFRAME_RENDER_RECEIPT_SCHEMA:
        raise ContractError("unsupported smart reframe render receipt schema")
    if receipt.get("receipt_sha256") != payload_sha256(receipt, "receipt_sha256"):
        raise ContractError("smart reframe render receipt integrity check failed")
    plan_path = validate_artifact_reference(
        _object(receipt.get("source_plan"), "source plan reference"),
        "smart reframe plan",
    )
    plan = read_json_object(plan_path, "smart reframe plan")
    validate_smart_reframe_plan(plan)
    if receipt.get("source_plan_sha256") != plan.get("plan_sha256"):
        raise ContractError("smart reframe render receipt binds a stale plan")
    ffmpeg_path = validate_artifact_reference(
        _object(receipt.get("ffmpeg_receipt"), "FFmpeg receipt reference"),
        "smart reframe FFmpeg receipt",
    )
    ffmpeg_receipt = read_json_object(ffmpeg_path, "smart reframe FFmpeg receipt")
    validate_ffmpeg_execution_receipt(ffmpeg_receipt)
    if ffmpeg_receipt.get("operation") != "render":
        raise ContractError("smart reframe used the wrong FFmpeg operation")
    staged = _object(receipt.get("staged_video"), "staged video reference")
    candidate = _object(receipt.get("candidate_video"), "candidate video reference")
    validate_artifact_reference(staged, "smart reframe staged video")
    validate_artifact_reference(candidate, "smart reframe candidate video")
    if staged.get("sha256") != candidate.get("sha256"):
        raise ContractError("smart reframe promoted output differs from staging")
    expected_boundary = {
        "candidate_only": True,
        "technical_completion_is_not_release_approval": True,
        "single_ffmpeg_outlet_used": True,
        "source_media_modified": False,
        "automatic_publish": False,
    }
    if (
        receipt.get("status") != "completed_candidate_only"
        or receipt.get("authority_boundary") != expected_boundary
    ):
        raise ContractError("smart reframe render authority boundary drifted")


def _plan_shot(
    index: int,
    start_s: float,
    end_s: float,
    observations: list[dict[str, Any]],
    *,
    source_metadata: dict[str, Any],
    profile: dict[str, Any],
    request: dict[str, Any],
) -> dict[str, Any]:
    spatial = [
        row
        for row in observations
        if row.get("rect") is not None
        and start_s <= float(row["time_s"]) <= end_s + 1e-6
    ]
    selected_track_id = _selected_track_id(spatial)
    ambiguous_subjects = _has_ambiguous_subjects(spatial)
    if selected_track_id:
        spatial = _filter_selected_track(spatial, selected_track_id)
    required = [row for row in spatial if row["required"]]
    preferred = [row for row in spatial if not row["required"]]
    crop_width, crop_height = _maximum_crop_shape(
        int(source_metadata["width"]),
        int(source_metadata["height"]),
        int(profile["width"]) / int(profile["height"]),
    )
    reason_codes: list[str] = []
    raw_keyframes: list[dict[str, Any]] = []
    strategy = "reject"
    if (
        ambiguous_subjects
        and selected_track_id is None
        and request["media_role"] in {"talking_head", "interview"}
    ):
        strategy = _first_fallback(
            request, compatible={"contain", "split_layout", "reject"}
        )
        reason_codes.append("multiple_subjects_require_explicit_track_selection")
    elif not spatial:
        strategy = _first_fallback(
            request, compatible={"contain", "blur_pad", "reject"}
        )
        reason_codes.append("no_spatial_evidence_blind_crop_forbidden")
    else:
        by_time: dict[float, list[dict[str, Any]]] = defaultdict(list)
        for row in spatial:
            by_time[float(row["time_s"])].append(row)
        incompatible_required = False
        for time_s, rows in sorted(by_time.items()):
            required_rows = [row for row in rows if row["required"]]
            window = _crop_window(
                rows,
                required_rows,
                crop_width,
                crop_height,
                face_target_y=float(profile["face_target_y"]),
                face_headroom_ratio=float(profile.get("face_headroom_ratio", 0.0)),
            )
            if window is None:
                incompatible_required = True
                break
            raw_keyframes.append(
                {
                    "time_s": round(min(max(time_s, start_s), end_s), 6),
                    "crop": window,
                    "evidence_ids": sorted(row["evidence_id"] for row in rows),
                    "motion_reason": "evidence_focus",
                }
            )
        if incompatible_required:
            strategy = _first_fallback(
                request, compatible=_role_fallbacks(str(request["media_role"]))
            )
            raw_keyframes = []
            reason_codes.append("required_regions_do_not_fit_target_crop")
        else:
            observed_keyframes = list(raw_keyframes)
            strategy, strategy_reasons = _crop_strategy(
                observed_keyframes, spatial, request
            )
            raw_keyframes = _deduplicate_keyframe_times(raw_keyframes, start_s, end_s)
            reason_codes.extend(strategy_reasons)
            reason_codes.append("spatial_evidence_supports_crop")

    keyframes = raw_keyframes
    if strategy == "tracked_crop":
        motion = request["motion_policy"]
        keyframes = smooth_crop_keyframes(
            raw_keyframes,
            deadband=float(motion["deadband"]),
            minimum_dwell_s=float(motion["minimum_dwell_s"]),
            max_speed_per_s=float(motion["max_speed_per_s"]),
            max_acceleration_per_s2=float(motion["max_acceleration_per_s2"]),
        )
    if keyframes and float(profile.get("face_headroom_ratio", 0.0)) > 0:
        evidence_by_id = {str(row["evidence_id"]): row for row in spatial}
        headroom = float(profile["face_headroom_ratio"])
        for keyframe in keyframes:
            face_rows = [
                evidence_by_id[evidence_id]
                for evidence_id in keyframe.get("evidence_ids", [])
                if evidence_id in evidence_by_id
                and evidence_by_id[evidence_id]["kind"] in {"face", "active_speaker"}
            ]
            if face_rows:
                keyframe["crop"]["y"] = round(
                    min(
                        float(keyframe["crop"]["y"]),
                        min(
                            max(
                                0.0,
                                float(row["rect"]["y"])
                                - float(row["rect"]["height"]) * headroom,
                            )
                            for row in face_rows
                        ),
                    ),
                    8,
                )
        reason_codes.append("face_headroom_protection_applied")
    if strategy == "fixed_crop" and keyframes:
        keyframes = [_robust_fixed_keyframe(keyframes, start_s)]
    if strategy in _PAD_STRATEGIES | {"split_layout", "reject"}:
        keyframes = []
    estimate = _quality_estimate(
        strategy,
        crop_width,
        crop_height,
        source_metadata,
        profile,
        required,
        keyframes,
        max_upscale=float(request["quality_policy"]["max_upscale_factor"]),
    )
    if not estimate["quality_gate_passed"] and strategy in _CROP_STRATEGIES:
        strategy = _first_fallback(
            request, compatible=_role_fallbacks(str(request["media_role"]))
        )
        keyframes = []
        reason_codes.append("crop_quality_gate_failed")
        if strategy == "blur_pad" and _face_near_top(spatial, profile):
            reason_codes.append("top_safe_line_blur_pad_rescue")
        estimate = _quality_estimate(
            strategy,
            crop_width,
            crop_height,
            source_metadata,
            profile,
            required,
            keyframes,
            max_upscale=float(request["quality_policy"]["max_upscale_factor"]),
        )
    return {
        "shot_id": f"shot-{index:04d}",
        "start_s": round(start_s, 6),
        "end_s": round(end_s, 6),
        "strategy": strategy,
        "required_evidence_ids": sorted(row["evidence_id"] for row in required),
        "preferred_evidence_ids": sorted(row["evidence_id"] for row in preferred),
        "keyframes": keyframes,
        "interpolation": "linear" if strategy == "tracked_crop" else "hold",
        "motion_policy": dict(request["motion_policy"]),
        "reason_codes": reason_codes,
        "quality_estimate": estimate,
        "candidate_only": True,
    }


def _crop_window(
    rows: list[dict[str, Any]],
    required: list[dict[str, Any]],
    crop_width: float,
    crop_height: float,
    *,
    face_target_y: float,
    face_headroom_ratio: float = 0.0,
) -> dict[str, float] | None:
    protected_required = [
        dict(row, rect=_expand_face_headroom(row["rect"], face_headroom_ratio))
        if row["kind"] in {"face", "active_speaker"}
        else row
        for row in required
    ]
    union = (
        _union([row["rect"] for row in protected_required])
        if protected_required
        else None
    )
    if union and (
        union["width"] > crop_width + 1e-8 or union["height"] > crop_height + 1e-8
    ):
        return None
    weights = []
    for row in rows:
        weight = (4.0 if row["required"] else 1.0) * max(0.05, float(row["score"]))
        if row["kind"] in {"face", "active_speaker", "user_hint"}:
            weight *= 1.5
        weights.append((_rect_center(row["rect"]), weight))
    total = sum(weight for _, weight in weights)
    center_x = sum(center[0] * weight for center, weight in weights) / total
    center_y = sum(center[1] * weight for center, weight in weights) / total
    face_rows = [row for row in rows if row["kind"] in {"face", "active_speaker"}]
    if face_rows:
        face_center = _rect_center(
            max(face_rows, key=lambda row: float(row["score"]))["rect"]
        )
        y = face_center[1] - face_target_y * crop_height
    else:
        y = center_y - crop_height / 2
    x = center_x - crop_width / 2
    if union:
        x_min = max(0.0, union["x"] + union["width"] - crop_width)
        x_max = min(union["x"], 1.0 - crop_width)
        y_min = max(0.0, union["y"] + union["height"] - crop_height)
        y_max = min(union["y"], 1.0 - crop_height)
        if x_min > x_max + 1e-8 or y_min > y_max + 1e-8:
            return None
        x = min(max(x, x_min), x_max)
        y = min(max(y, y_min), y_max)
    x = min(max(x, 0.0), 1.0 - crop_width)
    y = min(max(y, 0.0), 1.0 - crop_height)
    return {
        "x": round(x, 8),
        "y": round(y, 8),
        "width": round(crop_width, 8),
        "height": round(crop_height, 8),
    }


def _expand_face_headroom(rect: dict[str, float], ratio: float) -> dict[str, float]:
    """Protect inferred headroom in planning without changing detector evidence."""
    if ratio <= 0:
        return dict(rect)
    padding = float(rect["height"]) * ratio
    top = max(0.0, float(rect["y"]) - padding)
    return {
        "x": float(rect["x"]),
        "y": top,
        "width": float(rect["width"]),
        "height": min(1.0 - top, float(rect["height"]) + padding),
    }


def _crop_strategy(
    keyframes: list[dict[str, Any]],
    observations: list[dict[str, Any]],
    request: dict[str, Any],
) -> tuple[str, list[str]]:
    speakers = [
        (str(row.get("speaker_id") or ""), row["rect"])
        for row in observations
        if row["kind"] == "active_speaker" and _speaker_evidence_agrees(row)
    ]
    if len({speaker for speaker, _ in speakers}) > 1:
        centers = [_rect_center(rect) for _, rect in speakers]
        if max(math.dist(left, right) for left in centers for right in centers) > 0.22:
            return "speaker_cut", ["speaker_cut_requires_three_way_agreement"]
    if request["media_role"] == "photo" and len(keyframes) == 1:
        return "fixed_crop", ["single_static_image_fixed_crop"]
    observation_times = {
        float(row["time_s"]) for row in observations if row.get("rect") is not None
    }
    if len(observation_times) < 6:
        return (
            _first_fallback(request, compatible={"contain", "reject"}),
            ["insufficient_observations_for_stable_fixed_or_tracked_crop"],
        )
    fixed = _robust_fixed_keyframe(keyframes, min(observation_times))["crop"]
    centers = [_rect_center(row["crop"]) for row in keyframes]
    median_center = _rect_center(fixed)
    distances = sorted(math.dist(center, median_center) for center in centers)
    p95 = distances[max(0, math.ceil(0.95 * len(distances)) - 1)]
    x_values = [center[0] for center in centers]
    y_values = [center[1] for center in centers]
    deadband = float(request["motion_policy"]["deadband"])
    required_retained = all(
        _contains(fixed, row["rect"])
        for row in observations
        if row.get("rect") is not None and row["required"]
    )
    stable = (
        p95 <= deadband + 1e-8
        and max(x_values) - min(x_values) <= 2 * deadband + 1e-8
        and max(y_values) - min(y_values) <= 2 * deadband + 1e-8
        and required_retained
    )
    if stable:
        return "fixed_crop", [
            "fixed_crop_uses_robust_median_window",
            "fixed_crop_p95_within_deadband",
            "fixed_crop_retains_all_required_regions",
        ]
    return "tracked_crop", ["tracked_crop_requires_observed_motion_beyond_deadband"]


def _robust_fixed_keyframe(
    keyframes: list[dict[str, Any]], start_s: float
) -> dict[str, Any]:
    if not keyframes:
        raise ContractError("fixed crop requires keyframes")
    crops = [row["crop"] for row in keyframes]
    width = float(crops[0]["width"])
    height = float(crops[0]["height"])
    crop = {
        "x": round(float(median(float(row["x"]) for row in crops)), 8),
        "y": round(float(median(float(row["y"]) for row in crops)), 8),
        "width": round(width, 8),
        "height": round(height, 8),
    }
    return {
        "time_s": round(start_s, 6),
        "crop": crop,
        "evidence_ids": sorted(
            {
                evidence_id
                for row in keyframes
                for evidence_id in row.get("evidence_ids", [])
            }
        ),
        "motion_reason": "robust_median_fixed_for_shot",
    }


def _selected_track_id(observations: list[dict[str, Any]]) -> str | None:
    selected = {
        str(row.get("track_id") or "").strip()
        for row in observations
        if row["kind"] == "user_hint" and str(row.get("track_id") or "").strip()
    }
    if len(selected) > 1:
        raise ContractError("smart reframe evidence selects multiple subject tracks")
    return next(iter(selected), None)


def _filter_selected_track(
    observations: list[dict[str, Any]], track_id: str
) -> list[dict[str, Any]]:
    subject_kinds = {"face", "person", "active_speaker", "user_hint"}
    return [
        row
        for row in observations
        if row["kind"] not in subject_kinds
        or str(row.get("track_id") or "") == track_id
    ]


def _has_ambiguous_subjects(observations: list[dict[str, Any]]) -> bool:
    subject_rows = [
        row
        for row in observations
        if row["kind"] in {"face", "person", "active_speaker"}
    ]
    track_ids = {
        str(row.get("track_id") or "").strip()
        for row in subject_rows
        if str(row.get("track_id") or "").strip()
    }
    if len(track_ids) > 1:
        return True
    by_time: dict[float, list[dict[str, Any]]] = defaultdict(list)
    faces = [row for row in subject_rows if row["kind"] == "face"] or subject_rows
    for row in faces:
        by_time[float(row["time_s"])].append(row)
    return any(len(rows) > 1 for rows in by_time.values())


def _speaker_evidence_agrees(row: dict[str, Any]) -> bool:
    speaker = str(row.get("speaker_id") or "")
    compared = {
        str(row.get("asr_speaker_id") or ""),
        str(row.get("visual_speaker_id") or ""),
        str(row.get("role_mapping_speaker_id") or ""),
    }
    return (
        bool(speaker)
        and row.get("role_mapping_confirmed") is True
        and compared == {speaker}
    )


def _face_near_top(observations: list[dict[str, Any]], profile: dict[str, Any]) -> bool:
    safe_top = float(profile.get("safe_zones", {}).get("top", 0.0))
    return any(
        row["kind"] in {"face", "active_speaker"}
        and float(row["rect"]["y"]) <= safe_top + 0.02
        for row in observations
        if row.get("rect") is not None
    )


def _quality_estimate(
    strategy: str,
    crop_width: float,
    crop_height: float,
    source: dict[str, Any],
    profile: dict[str, Any],
    required: list[dict[str, Any]],
    keyframes: list[dict[str, Any]],
    *,
    max_upscale: float,
) -> dict[str, Any]:
    if strategy in _CROP_STRATEGIES:
        crop_px_width = max(1, int(round(crop_width * int(source["width"]))))
        crop_px_height = max(1, int(round(crop_height * int(source["height"]))))
        upscale = max(
            int(profile["width"]) / crop_px_width,
            int(profile["height"]) / crop_px_height,
        )
        retained = all(
            (
                (
                    crop := _crop_at_time(
                        keyframes,
                        float(row["time_s"]),
                        hold=strategy != "tracked_crop",
                    )
                )
                is not None
                and _contains(crop, row["rect"])
            )
            for row in required
        )
    else:
        crop_px_width = int(source["width"])
        crop_px_height = int(source["height"])
        upscale = max(
            int(profile["width"]) / crop_px_width,
            int(profile["height"]) / crop_px_height,
        )
        retained = strategy in _PAD_STRATEGIES | {"split_layout"}
    passed = retained and upscale <= max_upscale + 1e-8 and strategy != "reject"
    return {
        "required_regions_retained": bool(retained),
        "effective_source_width_px": crop_px_width,
        "effective_source_height_px": crop_px_height,
        "upscale_factor": round(upscale, 6),
        "max_upscale_factor": round(max_upscale, 6),
        "quality_gate_passed": bool(passed),
    }


def _shot_intervals(
    observations: list[dict[str, Any]], duration_s: float
) -> list[tuple[float, float]]:
    boundaries = [row for row in observations if row.get("kind") == "shot_boundary"]
    if not boundaries:
        return [(0.0, round(duration_s, 6))]
    intervals = sorted(
        (float(row["start_s"]), float(row["end_s"])) for row in boundaries
    )
    if intervals[0][0] > 0.05 or abs(intervals[-1][1] - duration_s) > 0.2:
        raise ContractError("shot boundary evidence does not cover source duration")
    previous_end = intervals[0][0]
    for start, end in intervals:
        if start < previous_end - 1e-6 or end <= start or end > duration_s + 0.2:
            raise ContractError(
                "shot boundary evidence overlaps or leaves source bounds"
            )
        if start - previous_end > 0.05:
            raise ContractError("shot boundary evidence leaves a timeline gap")
        previous_end = end
    intervals[0] = (0.0, intervals[0][1])
    intervals[-1] = (intervals[-1][0], duration_s)
    return [(round(start, 6), round(end, 6)) for start, end in intervals]


def _deduplicate_keyframe_times(
    keyframes: list[dict[str, Any]], start_s: float, end_s: float
) -> list[dict[str, Any]]:
    by_time: dict[float, dict[str, Any]] = {}
    for row in keyframes:
        by_time[float(row["time_s"])] = row
    ordered = [by_time[key] for key in sorted(by_time)]
    if ordered and ordered[0]["time_s"] > start_s:
        first = dict(ordered[0])
        first["time_s"] = round(start_s, 6)
        first["motion_reason"] = "shot_start_hold"
        ordered.insert(0, first)
    if ordered and ordered[-1]["time_s"] < end_s:
        last = dict(ordered[-1])
        last["time_s"] = round(end_s, 6)
        last["motion_reason"] = "shot_end_hold"
        ordered.append(last)
    return ordered


def _maximum_crop_shape(
    source_width: int, source_height: int, target_ratio: float
) -> tuple[float, float]:
    source_ratio = source_width / source_height
    if source_ratio >= target_ratio:
        return target_ratio / source_ratio, 1.0
    return 1.0, source_ratio / target_ratio


def _role_fallbacks(media_role: str) -> set[str]:
    if media_role in {"presentation", "screen_recording", "document", "chart"}:
        return {"contain", "blur_pad", "roi_sequence", "split_layout", "reject"}
    if media_role == "interview":
        return {
            "blur_pad",
            "contain",
            "split_layout",
            "speaker_cut",
            "reject",
        }
    return {"blur_pad", "contain", "reject"}


def _default_fallbacks(media_role: str) -> list[str]:
    if media_role in {"presentation", "screen_recording", "document", "chart"}:
        return ["contain", "blur_pad", "reject"]
    if media_role == "interview":
        return ["speaker_cut", "split_layout", "blur_pad", "contain", "reject"]
    return ["blur_pad", "contain", "reject"]


def _first_fallback(request: dict[str, Any], *, compatible: set[str]) -> str:
    return next(
        (value for value in request["fallback_order"] if value in compatible),
        "reject",
    )


def _union(rects: list[dict[str, float]]) -> dict[str, float]:
    left = min(row["x"] for row in rects)
    top = min(row["y"] for row in rects)
    right = max(row["x"] + row["width"] for row in rects)
    bottom = max(row["y"] + row["height"] for row in rects)
    return {
        "x": left,
        "y": top,
        "width": right - left,
        "height": bottom - top,
    }


def _rect_center(rect: dict[str, float]) -> tuple[float, float]:
    return rect["x"] + rect["width"] / 2, rect["y"] + rect["height"] / 2


def _contains(outer: dict[str, float], inner: dict[str, float]) -> bool:
    return (
        outer["x"] <= inner["x"] + 1e-8
        and outer["y"] <= inner["y"] + 1e-8
        and outer["x"] + outer["width"] + 1e-8 >= inner["x"] + inner["width"]
        and outer["y"] + outer["height"] + 1e-8 >= inner["y"] + inner["height"]
    )


def _crop_at_time(
    keyframes: list[dict[str, Any]], time_s: float, *, hold: bool
) -> dict[str, float] | None:
    if not keyframes:
        return None
    ordered = sorted(keyframes, key=lambda row: float(row["time_s"]))
    if time_s <= float(ordered[0]["time_s"]):
        return dict(ordered[0]["crop"])
    if time_s >= float(ordered[-1]["time_s"]):
        return dict(ordered[-1]["crop"])
    for left, right in zip(ordered, ordered[1:]):
        start = float(left["time_s"])
        end = float(right["time_s"])
        if start <= time_s <= end:
            ratio = 0.0 if hold else (time_s - start) / max(1e-9, end - start)
            return {
                field: round(
                    float(left["crop"][field])
                    + (float(right["crop"][field]) - float(left["crop"][field]))
                    * ratio,
                    8,
                )
                for field in ("x", "y", "width", "height")
            }
    return None


def _piecewise_expression(points: list[tuple[float, float, bool]]) -> str:
    compact: list[tuple[float, float, bool]] = []
    for time_s, value, hold in sorted(points):
        if compact and abs(time_s - compact[-1][0]) <= 1e-7:
            compact[-1] = (time_s, value, hold)
        else:
            compact.append((time_s, value, hold))
    expression = f"{compact[-1][1]:.6f}"
    pairs = list(zip(compact, compact[1:]))
    for (start_t, start_v, hold), (end_t, end_v, _) in reversed(pairs):
        duration = max(1e-6, end_t - start_t)
        segment = (
            f"{start_v:.6f}"
            if hold
            else f"{start_v:.6f}+({end_v - start_v:.6f})*"
            f"(t-{start_t:.6f})/{duration:.6f}"
        )
        expression = f"if(lt(t,{end_t:.6f}),{segment},{expression})"
    return expression


def _even(value: int) -> int:
    return max(2, value - value % 2)


def _plan_boundary() -> dict[str, Any]:
    return {
        "candidate_only": True,
        "source_media_modified": False,
        "automatic_crop_enabled": False,
        "identity_recognition_performed": False,
        "vkp_evidence_mutated": False,
        "second_ffmpeg_outlet_created": False,
        "automatic_publish": False,
    }


def _validate_schema(payload: dict[str, Any], filename: str, label: str) -> None:
    path = Path(__file__).resolve().parent / "schemas" / filename
    try:
        schema = read_json_object(path, f"{label} schema")
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema, format_checker=FormatChecker()).validate(payload)
    except (SchemaError, ValidationError) as exc:
        raise ContractError(
            f"{label} failed JSON Schema validation: {exc.message}"
        ) from exc


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError(f"{label} must be an object")
    return value


def _number(value: Any, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{label} must be numeric") from exc
    if not math.isfinite(number):
        raise ContractError(f"{label} must be finite")
    return number
