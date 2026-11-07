from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .contract_io import (
    artifact_reference,
    atomic_write_json,
    payload_sha256,
    read_json_object,
    validate_artifact_reference,
)
from .creative_gates import APPROVAL_RECORD_SCHEMA, validate_approval_record
from .errors import ContractError
from .vkp_bridge import EDIT_JOB_SCHEMA, validate_edit_job

SHOT_GENERATION_UNIT_SCHEMA = "video_creation_pipeline.shot_generation_unit.v1"
CAPABILITY_PREFLIGHT_SCHEMA = "video_creation_pipeline.capability_preflight.v1"
GENERATION_TASK_SCHEMA = "video_creation_pipeline.generation_task.v1"
GENERATION_RECEIPT_SCHEMA = "video_creation_pipeline.generation_receipt.v1"
GENERATION_VALIDATION_SCHEMA = "video_creation_pipeline.generation_validation.v1"

GENERATOR_CAPABILITIES = {
    "manual": "manual_media_import",
    "image-gen": "image_generation",
    "hyperframes": "hyperframes_render",
    "remotion": "remotion_render",
    "storyai-director": "storyai_3d_previs",
}


def build_shot_generation_unit(
    edit_job_path: str | Path,
    scene_id: str,
    *,
    generator: str,
    media_type: str = "video",
    width: int = 1080,
    height: int = 1920,
    visual_prompt: str = "",
) -> dict[str, Any]:
    job_path = Path(edit_job_path).expanduser().resolve()
    job = read_json_object(job_path, "edit job")
    validate_edit_job(job)
    if job.get("schema") != EDIT_JOB_SCHEMA:
        raise ContractError("shot generation requires the current edit job schema")
    selected_generator = _generator(generator)
    selected_media_type = str(media_type).strip().lower()
    if selected_media_type not in {"image", "video"}:
        raise ContractError("shot generation media_type must be image or video")
    output_width = _positive_int(width, "width")
    output_height = _positive_int(height, "height")
    scene = next(
        (
            row
            for row in job.get("storyboard_scenes") or []
            if isinstance(row, dict) and row.get("scene_id") == scene_id
        ),
        None,
    )
    if scene is None:
        raise ContractError(f"storyboard scene not found: {scene_id}")
    duration = _positive_number(
        float(scene.get("end") or 0) - float(scene.get("start") or 0),
        "scene duration",
    )
    prompt = str(visual_prompt).strip() or _scene_prompt(scene)
    if not prompt:
        raise ContractError("shot generation unit requires a visual prompt")

    unit: dict[str, Any] = {
        "schema": SHOT_GENERATION_UNIT_SCHEMA,
        "source_edit_job": artifact_reference(job_path, "edit job"),
        "scene_id": str(scene["scene_id"]),
        "title": str(scene.get("title") or scene_id),
        "timeline_indexes": list(scene.get("timeline_indexes") or []),
        "duration_s": round(duration, 3),
        "media_type": selected_media_type,
        "output": {"width": output_width, "height": output_height},
        "generator": selected_generator,
        "required_capability": GENERATOR_CAPABILITIES[selected_generator],
        "narration": "\n".join(
            str(value).strip()
            for value in scene.get("summary_sentences") or []
            if str(value).strip()
        ),
        "visual_prompt": prompt,
        "visual_notes": [
            dict(row)
            for row in scene.get("visual_notes") or []
            if isinstance(row, dict)
        ],
        "continuity": {
            "characters": [],
            "props": [],
            "environment": [],
            "must_preserve_across_shots": True,
        },
        "reference_assets": [],
        "negative_constraints": [],
        "status": "candidate_only",
        "human_confirmed": False,
        "mutates_vkp_timeline": False,
    }
    unit["unit_sha256"] = payload_sha256(unit, "unit_sha256")
    validate_shot_generation_unit(unit)
    return unit


def validate_shot_generation_unit(unit: dict[str, Any]) -> None:
    if unit.get("schema") != SHOT_GENERATION_UNIT_SCHEMA:
        raise ContractError("unsupported shot generation unit schema")
    if unit.get("unit_sha256") != payload_sha256(unit, "unit_sha256"):
        raise ContractError("shot generation unit integrity check failed")
    validate_artifact_reference(
        unit.get("source_edit_job")
        if isinstance(unit.get("source_edit_job"), dict)
        else {},
        "source edit job",
    )
    _generator(unit.get("generator"))
    if unit.get("required_capability") != GENERATOR_CAPABILITIES[unit["generator"]]:
        raise ContractError("shot generation capability does not match its generator")
    if unit.get("media_type") not in {"image", "video"}:
        raise ContractError("shot generation unit media_type is invalid")
    output = unit.get("output") if isinstance(unit.get("output"), dict) else {}
    _positive_int(output.get("width"), "shot width")
    _positive_int(output.get("height"), "shot height")
    _positive_number(unit.get("duration_s"), "shot duration")
    if not str(unit.get("visual_prompt") or "").strip():
        raise ContractError("shot generation unit visual prompt is empty")
    if (
        unit.get("status") != "candidate_only"
        or unit.get("human_confirmed") is not False
    ):
        raise ContractError(
            "new shot generation units must remain unconfirmed candidates"
        )
    if unit.get("mutates_vkp_timeline") is not False:
        raise ContractError("shot generation unit cannot mutate the VKP Timeline")


def build_generation_task(
    unit_path: str | Path,
    storyboard_approval_path: str | Path,
    capability_preflight_path: str | Path,
    *,
    output_dir: str | Path,
    design_path: str | Path | None = None,
    visual_strategy_path: str | Path | None = None,
) -> dict[str, Any]:
    source_unit = Path(unit_path).expanduser().resolve()
    unit = read_json_object(source_unit, "shot generation unit")
    validate_shot_generation_unit(unit)
    approval_path = Path(storyboard_approval_path).expanduser().resolve()
    approval = read_json_object(approval_path, "storyboard approval")
    validate_approval_record(approval)
    if (
        approval.get("schema") != APPROVAL_RECORD_SCHEMA
        or approval.get("target") != "storyboard"
    ):
        raise ContractError("generation task requires a storyboard approval")
    unit_reference = artifact_reference(source_unit, "shot generation unit")
    if approval.get("artifact") != unit_reference:
        raise ContractError(
            "storyboard approval does not bind the shot generation unit"
        )

    preflight_path = Path(capability_preflight_path).expanduser().resolve()
    preflight = read_json_object(preflight_path, "capability preflight")
    capability = str(unit["required_capability"])
    validate_capability_preflight(preflight, capability)
    destination = Path(output_dir).expanduser().resolve()
    design_reference = (
        artifact_reference(design_path, "project design") if design_path else None
    )
    visual_strategy_reference = _visual_strategy_reference(
        visual_strategy_path,
        unit_reference=unit_reference,
        unit_sha256=unit["unit_sha256"],
    )
    task: dict[str, Any] = {
        "schema": GENERATION_TASK_SCHEMA,
        "task_id": str(uuid4()),
        "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "source_unit": unit_reference,
        "source_unit_sha256": unit["unit_sha256"],
        "storyboard_approval": artifact_reference(approval_path, "storyboard approval"),
        "approval_record_sha256": approval["approval_record_sha256"],
        "capability_preflight": artifact_reference(
            preflight_path, "capability preflight"
        ),
        "required_capability": capability,
        "generator": unit["generator"],
        "media_type": unit["media_type"],
        "duration_s": unit["duration_s"],
        "output": {
            "directory": str(destination),
            "width": unit["output"]["width"],
            "height": unit["output"]["height"],
            "duration_tolerance_s": round(max(0.5, float(unit["duration_s"]) * 0.1), 3),
        },
        "design": design_reference,
        "visual_strategy": visual_strategy_reference,
        "visual_prompt": unit["visual_prompt"],
        "status": "ready_for_external_execution",
        "execution_policy": {
            "claim_after_preflight": True,
            "claim_owner": "existing_run_registry_or_provider",
            "implicit_install_allowed": False,
            "silent_generator_fallback": False,
            "network_execution_performed": False,
            "render_execution_performed": False,
        },
        "completion_requirements": {
            "technical_probe_required": True,
            "representative_frame_review_required": True,
            "blank_or_black_output_allowed": False,
            "wrong_composition_allowed": False,
            "acceptance_status": "candidate_only",
        },
    }
    task["task_sha256"] = payload_sha256(task, "task_sha256")
    validate_generation_task(task)
    return task


def validate_capability_preflight(
    preflight: dict[str, Any], required_capability: str
) -> None:
    if preflight.get("schema") != CAPABILITY_PREFLIGHT_SCHEMA:
        raise ContractError("unsupported capability preflight schema")
    if _parse_datetime(preflight.get("checked_at")) is None:
        raise ContractError("capability preflight requires a dated check")
    capabilities = preflight.get("capabilities")
    if not isinstance(capabilities, list):
        raise ContractError("capability preflight capabilities must be a list")
    matches = [
        row
        for row in capabilities
        if isinstance(row, dict) and row.get("id") == required_capability
    ]
    if len(matches) != 1:
        raise ContractError(
            f"capability preflight must report exactly one {required_capability} result"
        )
    result = matches[0]
    if result.get("ready") is not True:
        raise ContractError(
            f"required capability is unavailable: {required_capability}"
        )
    if result.get("installation_performed") is not False:
        raise ContractError(
            "capability preflight cannot install dependencies implicitly"
        )
    if str(result.get("probe_method") or "") not in {
        "existing_runtime",
        "registered_tool_health_check",
        "provider_capability_receipt",
        "manual_operator_verification",
    }:
        raise ContractError("capability preflight probe method is not auditable")


def validate_generation_task(task: dict[str, Any]) -> None:
    if task.get("schema") != GENERATION_TASK_SCHEMA:
        raise ContractError("unsupported generation task schema")
    if task.get("task_sha256") != payload_sha256(task, "task_sha256"):
        raise ContractError("generation task integrity check failed")
    unit_path = validate_artifact_reference(
        task.get("source_unit") if isinstance(task.get("source_unit"), dict) else {},
        "shot generation unit",
    )
    unit = read_json_object(unit_path, "shot generation unit")
    validate_shot_generation_unit(unit)
    if task.get("source_unit_sha256") != unit.get("unit_sha256"):
        raise ContractError("generation task does not bind the exact shot unit")
    approval_path = validate_artifact_reference(
        task.get("storyboard_approval")
        if isinstance(task.get("storyboard_approval"), dict)
        else {},
        "storyboard approval",
    )
    approval = read_json_object(approval_path, "storyboard approval")
    validate_approval_record(approval)
    if (
        approval.get("target") != "storyboard"
        or approval.get("artifact") != task.get("source_unit")
        or approval.get("approval_record_sha256") != task.get("approval_record_sha256")
    ):
        raise ContractError("generation task storyboard approval is stale")
    preflight_path = validate_artifact_reference(
        task.get("capability_preflight")
        if isinstance(task.get("capability_preflight"), dict)
        else {},
        "capability preflight",
    )
    preflight = read_json_object(preflight_path, "capability preflight")
    validate_capability_preflight(preflight, str(task.get("required_capability") or ""))
    generator = _generator(task.get("generator"))
    if task.get("required_capability") != GENERATOR_CAPABILITIES[generator]:
        raise ContractError("generation task capability and generator differ")
    if task.get("media_type") != unit.get("media_type"):
        raise ContractError("generation task media type differs from the shot unit")
    output = task.get("output") if isinstance(task.get("output"), dict) else {}
    _positive_int(output.get("width"), "generation width")
    _positive_int(output.get("height"), "generation height")
    _positive_number(output.get("duration_tolerance_s"), "duration tolerance")
    design = task.get("design")
    if design is not None:
        if not isinstance(design, dict):
            raise ContractError("generation design reference must be an object")
        validate_artifact_reference(design, "project design")
    _validate_task_visual_strategy(
        task.get("visual_strategy"),
        unit_reference=task["source_unit"],
        unit_sha256=task["source_unit_sha256"],
    )
    policy = (
        task.get("execution_policy")
        if isinstance(task.get("execution_policy"), dict)
        else {}
    )
    if (
        policy.get("claim_after_preflight") is not True
        or policy.get("implicit_install_allowed") is not False
        or policy.get("silent_generator_fallback") is not False
        or policy.get("network_execution_performed") is not False
        or policy.get("render_execution_performed") is not False
    ):
        raise ContractError("generation task execution policy is invalid")


def validate_generation_receipt(
    task: dict[str, Any], receipt: dict[str, Any]
) -> dict[str, Any]:
    validate_generation_task(task)
    if receipt.get("schema") != GENERATION_RECEIPT_SCHEMA:
        raise ContractError("unsupported generation receipt schema")
    if (
        receipt.get("task_id") != task.get("task_id")
        or receipt.get("task_sha256") != task.get("task_sha256")
        or receipt.get("generator") != task.get("generator")
    ):
        raise ContractError("generation receipt does not bind the exact task")
    if receipt.get("published") is not False:
        raise ContractError("generation receipt cannot report publishing")
    if receipt.get("fallback_used") is not False:
        raise ContractError("generation receipt cannot use a silent fallback")
    status = str(receipt.get("status") or "")
    if status not in {"completed", "failed", "cancelled"}:
        raise ContractError("generation receipt status is invalid")
    if status != "completed":
        if not str(receipt.get("error") or "").strip():
            raise ContractError(
                "failed or cancelled generation requires a visible reason"
            )
        return _generation_validation(task, status, 0)

    output = receipt.get("output") if isinstance(receipt.get("output"), dict) else {}
    output_path = validate_artifact_reference(output, "generated output")
    destination = Path(str(task["output"]["directory"])).resolve()
    try:
        output_path.relative_to(destination)
    except ValueError as exc:
        raise ContractError(
            "generated output must stay inside the task output directory"
        ) from exc
    technical = (
        receipt.get("technical_verification")
        if isinstance(receipt.get("technical_verification"), dict)
        else {}
    )
    if (
        technical.get("width") != task["output"]["width"]
        or technical.get("height") != task["output"]["height"]
    ):
        raise ContractError("generated output dimensions do not match the task")
    if task.get("media_type") == "video":
        if technical.get("has_video_stream") is not True:
            raise ContractError("generated video lacks a valid video stream")
        duration = _positive_number(technical.get("duration_s"), "output duration")
        if abs(duration - float(task["duration_s"])) > float(
            task["output"]["duration_tolerance_s"]
        ):
            raise ContractError("generated video duration is outside tolerance")
    elif technical.get("decodable_image") is not True:
        raise ContractError("generated image is not decodable")
    if not str(technical.get("probe_method") or "").strip():
        raise ContractError("generation receipt lacks technical probe evidence")

    visual = (
        receipt.get("visual_verification")
        if isinstance(receipt.get("visual_verification"), dict)
        else {}
    )
    if visual.get("inspected") is not True:
        raise ContractError("generated output requires visual inspection")
    if any(
        visual.get(field) is not False
        for field in ("blank", "black", "wrong_composition")
    ):
        raise ContractError("generated output failed visual inspection")
    frames = visual.get("representative_frames")
    if not isinstance(frames, list) or not frames:
        raise ContractError("visual inspection requires representative frame evidence")
    for index, frame in enumerate(frames, 1):
        if not isinstance(frame, dict):
            raise ContractError("representative frame reference must be an object")
        validate_artifact_reference(frame, f"representative frame {index}")
    return _generation_validation(task, status, len(frames))


def verify_generation_receipt_files(
    task_path: str | Path, receipt_path: str | Path
) -> dict[str, Any]:
    task = read_json_object(task_path, "generation task")
    receipt = read_json_object(receipt_path, "generation receipt")
    return validate_generation_receipt(task, receipt)


def write_generation_contract(payload: dict[str, Any], output_path: str | Path) -> Path:
    schema = payload.get("schema")
    if schema == SHOT_GENERATION_UNIT_SCHEMA:
        validate_shot_generation_unit(payload)
    elif schema == GENERATION_TASK_SCHEMA:
        validate_generation_task(payload)
    else:
        raise ContractError("unsupported generation contract")
    return atomic_write_json(payload, output_path)


def _generation_validation(
    task: dict[str, Any], status: str, frame_count: int
) -> dict[str, Any]:
    return {
        "schema": GENERATION_VALIDATION_SCHEMA,
        "valid": True,
        "task_id": task["task_id"],
        "task_sha256": task["task_sha256"],
        "status": status,
        "representative_frame_count": frame_count,
        "acceptance_status": "candidate_only",
        "next_action": "human_review" if status == "completed" else "operator_review",
    }


def _scene_prompt(scene: dict[str, Any]) -> str:
    parts = [str(scene.get("title") or "").strip()]
    parts.extend(
        str(value).strip()
        for value in scene.get("summary_sentences") or []
        if str(value).strip()
    )
    parts.extend(
        json.dumps(row, ensure_ascii=False, sort_keys=True)
        for row in scene.get("visual_notes") or []
        if isinstance(row, dict)
    )
    return "\n".join(part for part in parts if part)


def _visual_strategy_reference(
    strategy_path: str | Path | None,
    *,
    unit_reference: dict[str, Any],
    unit_sha256: str,
) -> dict[str, Any] | None:
    if strategy_path is None:
        return None
    from .production_strategy import validate_visual_production_plan

    path = Path(strategy_path).expanduser().resolve()
    strategy = read_json_object(path, "visual production plan")
    validate_visual_production_plan(strategy)
    if (
        strategy.get("source_unit") != unit_reference
        or strategy.get("source_unit_sha256") != unit_sha256
    ):
        raise ContractError("visual production plan does not bind the shot unit")
    return artifact_reference(path, "visual production plan")


def _validate_task_visual_strategy(
    value: Any,
    *,
    unit_reference: dict[str, Any],
    unit_sha256: str,
) -> None:
    if value is None:
        return
    if not isinstance(value, dict):
        raise ContractError("generation task visual strategy must be an artifact")
    from .production_strategy import validate_visual_production_plan

    path = validate_artifact_reference(value, "visual production plan")
    strategy = read_json_object(path, "visual production plan")
    validate_visual_production_plan(strategy)
    if (
        strategy.get("source_unit") != unit_reference
        or strategy.get("source_unit_sha256") != unit_sha256
    ):
        raise ContractError("generation task visual strategy is for another shot unit")


def _generator(value: Any) -> str:
    generator = str(value or "").strip().lower()
    if generator not in GENERATOR_CAPABILITIES:
        raise ContractError(f"unsupported generator: {value}")
    return generator


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise ContractError(f"{label} must be a positive integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{label} must be a positive integer") from exc
    if result <= 0 or float(result) != float(value):
        raise ContractError(f"{label} must be a positive integer")
    return result


def _positive_number(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise ContractError(f"{label} must be a positive finite number")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{label} must be a positive finite number") from exc
    if result <= 0 or not math.isfinite(result):
        raise ContractError(f"{label} must be a positive finite number")
    return result


def _parse_datetime(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)
