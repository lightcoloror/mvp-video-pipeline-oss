from __future__ import annotations

import math
import subprocess
from pathlib import Path
from typing import Any

from .contract_io import (
    artifact_reference,
    atomic_write_json,
    payload_sha256,
    read_json_object,
    validate_artifact_reference,
)
from .errors import ContractError
from .generation_contracts import validate_shot_generation_unit
from .visual_routing import (
    validate_content_visual_routing_plan,
    validate_visual_style_profile,
)

SEEDANCE_PROMPT_HANDOFF_SCHEMA = "video_creation_pipeline.seedance_prompt_handoff.v1"
SEEDANCE_PROMPT_SKILL_COMMIT = "57d1e2f273747c238dd892698a05137ab2f10d4a"
_SKILL_FILE = Path(".claude/skills/seedance/SKILL.md")


def seedance_prompt_doctor(skill_root: str | Path | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "schema": "video_creation_pipeline.seedance_prompt_doctor.v1",
        "status": "not_supplied",
        "network_execution_performed": False,
        "provider_execution_performed": False,
        "upload_performed": False,
    }
    if skill_root is None:
        return result
    try:
        result["source"] = _skill_source(Path(skill_root))
        result["status"] = "ready_for_candidate_handoff"
    except ContractError as exc:
        result.update({"status": "blocked", "error": str(exc)})
    return result


def build_seedance_prompt_handoff(
    skill_root: str | Path,
    shot_unit_path: str | Path,
    *,
    segment_limit_s: float = 15.0,
    content_visual_routing_plan_path: str | Path | None = None,
    visual_style_profile_path: str | Path | None = None,
) -> dict[str, Any]:
    source = _skill_source(Path(skill_root))
    unit_path = Path(shot_unit_path).expanduser().resolve()
    unit = read_json_object(unit_path, "shot generation unit")
    validate_shot_generation_unit(unit)
    limit = _positive_number(segment_limit_s, "segment limit")
    if limit > 60:
        raise ContractError("segment limit must not exceed 60 seconds")

    visual_binding = _optional_visual_binding(
        unit,
        content_visual_routing_plan_path,
        visual_style_profile_path,
    )
    visual_prompt = str(unit["visual_prompt"])
    negative_constraints = list(unit["negative_constraints"])
    if visual_binding is not None:
        style_prompt = visual_binding["style_prompt"]
        visual_prompt = f"{style_prompt}. {visual_prompt}"
        negative_constraints = list(
            dict.fromkeys(
                [*negative_constraints, *visual_binding["negative_constraints"]]
            )
        )
    segments = _segments(unit, limit, visual_prompt=visual_prompt)
    payload: dict[str, Any] = {
        "schema": SEEDANCE_PROMPT_HANDOFF_SCHEMA,
        "seedance_prompt_skill": source,
        "source_shot_unit": artifact_reference(unit_path, "shot generation unit"),
        "source_shot_unit_sha256": unit["unit_sha256"],
        "prompt_contract": {
            "language": "zh-CN",
            "media_type": unit["media_type"],
            "output": dict(unit["output"]),
            "requested_duration_s": unit["duration_s"],
            "visual_prompt": visual_prompt,
            "timeline_segments": segments,
            "continuity": dict(unit["continuity"]),
            "reference_assets": list(unit["reference_assets"]),
            "negative_constraints": negative_constraints,
            "prompt_rules": {
                "timestamped_segments": True,
                "continuity_anchor_required_after_first_segment": len(segments) > 1,
                "reference_usage_must_be_explicit": True,
                "unsupported_platform_claims_allowed": False,
            },
        },
        "execution_route": {
            "status": "route_unconfigured",
            "provider_execution_authorized": False,
            "upload_authorized": False,
            "reference_media_upload_authorized": False,
            "silent_provider_fallback_allowed": False,
            "provider_capability_verification_required": True,
        },
        "authority_boundary": {
            "candidate_only": True,
            "prompt_generation_only": True,
            "network_execution_performed": False,
            "provider_execution_performed": False,
            "upload_performed": False,
            "source_media_modified": False,
            "automatic_publish": False,
            "second_ffmpeg_outlet_created": False,
        },
    }
    if visual_binding is not None:
        payload["source_content_visual_routing_plan"] = visual_binding[
            "routing_reference"
        ]
        payload["source_visual_style_profile"] = visual_binding["style_reference"]
        payload["visual_route"] = "provider_generated_broll"
    payload["handoff_sha256"] = payload_sha256(payload, "handoff_sha256")
    validate_seedance_prompt_handoff(payload)
    return payload


def validate_seedance_prompt_handoff(payload: dict[str, Any]) -> None:
    if payload.get("schema") != SEEDANCE_PROMPT_HANDOFF_SCHEMA:
        raise ContractError("unsupported Seedance prompt handoff schema")
    if payload.get("handoff_sha256") != payload_sha256(payload, "handoff_sha256"):
        raise ContractError("Seedance prompt handoff integrity check failed")
    _validate_source(_object(payload.get("seedance_prompt_skill")))
    unit_path = validate_artifact_reference(
        _object(payload.get("source_shot_unit")), "shot generation unit"
    )
    unit = read_json_object(unit_path, "shot generation unit")
    validate_shot_generation_unit(unit)
    if payload.get("source_shot_unit_sha256") != unit.get("unit_sha256"):
        raise ContractError("Seedance prompt handoff shot unit drifted")

    contract = _object(payload.get("prompt_contract"))
    if (
        contract.get("media_type") != unit.get("media_type")
        or contract.get("output") != unit.get("output")
        or contract.get("requested_duration_s") != unit.get("duration_s")
        or contract.get("continuity") != unit.get("continuity")
        or contract.get("reference_assets") != unit.get("reference_assets")
    ):
        raise ContractError("Seedance prompt contract drifted from shot unit")
    expected_prompt, expected_negative = _validated_visual_prompt_binding(payload, unit)
    if (
        contract.get("visual_prompt") != expected_prompt
        or contract.get("negative_constraints") != expected_negative
    ):
        raise ContractError("Seedance prompt visual route or style binding drifted")
    if contract.get("language") != "zh-CN":
        raise ContractError("Seedance prompt handoff language is invalid")
    segments = contract.get("timeline_segments")
    if not isinstance(segments, list) or not segments:
        raise ContractError("Seedance prompt handoff requires timeline segments")
    _validate_segments(segments, float(unit["duration_s"]))
    rules = _object(contract.get("prompt_rules"))
    expected_rules = {
        "timestamped_segments": True,
        "continuity_anchor_required_after_first_segment": len(segments) > 1,
        "reference_usage_must_be_explicit": True,
        "unsupported_platform_claims_allowed": False,
    }
    if rules != expected_rules:
        raise ContractError("Seedance prompt rules are invalid")
    route = _object(payload.get("execution_route"))
    if route != {
        "status": "route_unconfigured",
        "provider_execution_authorized": False,
        "upload_authorized": False,
        "reference_media_upload_authorized": False,
        "silent_provider_fallback_allowed": False,
        "provider_capability_verification_required": True,
    }:
        raise ContractError("Seedance prompt execution route is invalid")
    if _object(payload.get("authority_boundary")) != {
        "candidate_only": True,
        "prompt_generation_only": True,
        "network_execution_performed": False,
        "provider_execution_performed": False,
        "upload_performed": False,
        "source_media_modified": False,
        "automatic_publish": False,
        "second_ffmpeg_outlet_created": False,
    }:
        raise ContractError("Seedance prompt authority boundary is invalid")


def write_seedance_prompt_handoff(
    payload: dict[str, Any], output_path: str | Path
) -> Path:
    validate_seedance_prompt_handoff(payload)
    return atomic_write_json(payload, output_path)


def _optional_visual_binding(
    unit: dict[str, Any],
    routing_plan_path: str | Path | None,
    style_profile_path: str | Path | None,
) -> dict[str, Any] | None:
    if routing_plan_path is None and style_profile_path is None:
        return None
    if routing_plan_path is None or style_profile_path is None:
        raise ContractError(
            "generated B-roll visual routing plan and style profile must be supplied together"
        )
    route_path = Path(routing_plan_path).expanduser().resolve()
    style_path = Path(style_profile_path).expanduser().resolve()
    binding = _resolve_visual_binding(unit, route_path, style_path)
    binding["routing_reference"] = artifact_reference(
        route_path, "content visual routing plan"
    )
    binding["style_reference"] = artifact_reference(style_path, "visual style profile")
    return binding


def _validated_visual_prompt_binding(
    payload: dict[str, Any], unit: dict[str, Any]
) -> tuple[str, list[str]]:
    route_ref = payload.get("source_content_visual_routing_plan")
    style_ref = payload.get("source_visual_style_profile")
    if route_ref is None and style_ref is None:
        if "visual_route" in payload:
            raise ContractError("Seedance prompt has an unbound visual route")
        return str(unit["visual_prompt"]), list(unit["negative_constraints"])
    if not isinstance(route_ref, dict) or not isinstance(style_ref, dict):
        raise ContractError(
            "generated B-roll visual routing plan and style profile must be bound together"
        )
    route_path = validate_artifact_reference(
        _object(route_ref), "content visual routing plan"
    )
    style_path = validate_artifact_reference(_object(style_ref), "visual style profile")
    binding = _resolve_visual_binding(unit, route_path, style_path)
    if payload.get("visual_route") != "provider_generated_broll":
        raise ContractError("Seedance prompt visual route is invalid")
    prompt = f"{binding['style_prompt']}. {unit['visual_prompt']}"
    negative = list(
        dict.fromkeys(
            [
                *list(unit["negative_constraints"]),
                *binding["negative_constraints"],
            ]
        )
    )
    return prompt, negative


def _resolve_visual_binding(
    unit: dict[str, Any], route_path: Path, style_path: Path
) -> dict[str, Any]:
    route = read_json_object(route_path, "content visual routing plan")
    style = read_json_object(style_path, "visual style profile")
    validate_content_visual_routing_plan(route)
    validate_visual_style_profile(style)
    route_inputs = _object(route.get("inputs"))
    if _object(route_inputs.get("visual_style_profile")) != artifact_reference(
        style_path, "visual style profile"
    ):
        raise ContractError("generated B-roll visual style binding drifted")
    scene_id = str(unit.get("scene_id") or "")
    scene = next(
        (
            _object(row)
            for row in route.get("scene_routes") or []
            if _object(row).get("scene_id") == scene_id
        ),
        None,
    )
    if scene is None or scene.get("visual_route") != "provider_generated_broll":
        raise ContractError(
            "shot generation unit is not routed to provider-generated B-roll"
        )
    provider = _object(_object(style["renderer_bindings"])["provider_prompt"])
    constraints = provider.get("negative_constraints")
    if not isinstance(constraints, list):
        raise ContractError("visual style provider constraints are invalid")
    return {
        "style_prompt": str(provider["style_prefix"]),
        "negative_constraints": [str(value) for value in constraints],
    }


def _segments(
    unit: dict[str, Any], limit: float, *, visual_prompt: str
) -> list[dict[str, Any]]:
    duration = float(unit["duration_s"])
    count = max(1, math.ceil(duration / limit))
    segments: list[dict[str, Any]] = []
    for index in range(count):
        start = round(index * limit, 3)
        end = round(min(duration, (index + 1) * limit), 3)
        segment: dict[str, Any] = {
            "segment_id": f"segment-{index + 1:02d}",
            "start_s": start,
            "end_s": end,
            "duration_s": round(end - start, 3),
            "prompt": visual_prompt,
            "continuity_anchor": None,
        }
        if index:
            segment["continuity_anchor"] = {
                "previous_segment_id": segments[-1]["segment_id"],
                "preserve": "previous_segment_final_visual_state",
            }
        segments.append(segment)
    return segments


def _validate_segments(segments: list[Any], expected_duration: float) -> None:
    cursor = 0.0
    for index, raw in enumerate(segments):
        row = _object(raw)
        expected_id = f"segment-{index + 1:02d}"
        if row.get("segment_id") != expected_id:
            raise ContractError("Seedance prompt segment ids are invalid")
        start = _nonnegative_number(row.get("start_s"), "segment start")
        end = _positive_number(row.get("end_s"), "segment end")
        if abs(start - cursor) > 0.001 or end <= start:
            raise ContractError("Seedance prompt segments are not contiguous")
        if abs(float(row.get("duration_s") or 0) - (end - start)) > 0.001:
            raise ContractError("Seedance prompt segment duration is invalid")
        if not str(row.get("prompt") or "").strip():
            raise ContractError("Seedance prompt segment is empty")
        anchor = row.get("continuity_anchor")
        if index == 0 and anchor is not None:
            raise ContractError("first Seedance segment cannot have continuity anchor")
        if index and anchor != {
            "previous_segment_id": f"segment-{index:02d}",
            "preserve": "previous_segment_final_visual_state",
        }:
            raise ContractError("Seedance prompt continuity anchor is invalid")
        cursor = end
    if abs(cursor - expected_duration) > 0.001:
        raise ContractError("Seedance prompt segments do not cover shot duration")


def _skill_source(root: Path) -> dict[str, Any]:
    root = root.expanduser().resolve()
    skill = root / _SKILL_FILE
    if not skill.is_file():
        raise ContractError("Seedance prompt skill file is missing")
    if _git_head(root) != SEEDANCE_PROMPT_SKILL_COMMIT:
        raise ContractError("Seedance prompt skill source commit drifted")
    return {
        "source_commit": SEEDANCE_PROMPT_SKILL_COMMIT,
        "source_license": "MIT",
        "skill_file": artifact_reference(skill, "Seedance prompt skill"),
        "reuse_mode": "prompt_structure_adaptation_no_provider_execution",
    }


def _validate_source(source: dict[str, Any]) -> None:
    if (
        source.get("source_commit") != SEEDANCE_PROMPT_SKILL_COMMIT
        or source.get("source_license") != "MIT"
        or source.get("reuse_mode")
        != "prompt_structure_adaptation_no_provider_execution"
    ):
        raise ContractError("Seedance prompt source is not the reviewed source")
    skill = validate_artifact_reference(
        _object(source.get("skill_file")), "Seedance prompt skill"
    )
    _skill_source(skill.parents[3])


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
        raise ContractError("Seedance prompt source commit could not be read") from exc
    if result.returncode != 0:
        raise ContractError("Seedance prompt source commit could not be read")
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
