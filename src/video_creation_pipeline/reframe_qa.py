from __future__ import annotations

from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError, ValidationError

from .contract_io import (
    artifact_reference,
    atomic_write_json,
    payload_sha256,
    read_json_object,
    validate_artifact_reference,
)
from .errors import ContractError
from .reframe_detectors import source_media_dimensions, validate_smart_reframe_evidence
from .smart_reframe import (
    SMART_REFRAME_RENDER_RECEIPT_SCHEMA,
    validate_smart_reframe_plan,
    validate_smart_reframe_render_receipt,
)

SMART_REFRAME_QA_SCHEMA = "video_creation_pipeline.smart_reframe_qa.v1"
_CROP_STRATEGIES = {"fixed_crop", "tracked_crop", "speaker_cut", "roi_sequence"}
_PAD_STRATEGIES = {"contain", "blur_pad"}


def build_smart_reframe_qa(
    plan_path: str | Path,
    *,
    render_receipt_path: str | Path | None = None,
) -> dict[str, Any]:
    plan_file = Path(plan_path).expanduser().resolve()
    plan = read_json_object(plan_file, "smart reframe plan")
    validate_smart_reframe_plan(plan)
    observations = _load_observations(plan)
    inputs: dict[str, Any] = {
        "plan": artifact_reference(plan_file, "smart reframe plan"),
        "render_receipt": None,
        "candidate_video": None,
    }
    media_checks = {
        "rendered": False,
        "dimensions_match": None,
        "duration_match": None,
        "single_ffmpeg_outlet_verified": None,
    }
    if render_receipt_path is not None:
        receipt_path = Path(render_receipt_path).expanduser().resolve()
        receipt = read_json_object(receipt_path, "smart reframe render receipt")
        validate_smart_reframe_render_receipt(receipt)
        if receipt.get("source_plan") != inputs["plan"]:
            raise ContractError("smart reframe QA render receipt binds another plan")
        candidate_path = validate_artifact_reference(
            _object(receipt.get("candidate_video"), "candidate video reference"),
            "smart reframe candidate video",
        )
        metadata = source_media_dimensions(candidate_path)
        target = plan["target_profile"]
        media_checks = {
            "rendered": True,
            "dimensions_match": metadata["width"] == target["width"]
            and metadata["height"] == target["height"],
            "duration_match": abs(
                float(metadata["duration_s"])
                - float(plan["source_metadata"]["duration_s"])
            )
            <= 0.2,
            "single_ffmpeg_outlet_verified": True,
        }
        inputs["render_receipt"] = artifact_reference(
            receipt_path, "smart reframe render receipt"
        )
        inputs["candidate_video"] = artifact_reference(
            candidate_path, "smart reframe candidate video"
        )

    checks = [
        _evaluate_shot(shot, observations, plan) for shot in plan.get("shots", [])
    ]
    required_total = sum(row["required_region_count"] for row in checks)
    retained_total = sum(row["retained_required_region_count"] for row in checks)
    safe_violations = sum(row["safe_zone_violation_count"] for row in checks)
    truncations = sum(row["truncation_count"] for row in checks)
    planning_passed = all(row["passed"] for row in checks)
    rendered_passed = not media_checks["rendered"] or all(
        media_checks[field] is True
        for field in (
            "dimensions_match",
            "duration_match",
            "single_ffmpeg_outlet_verified",
        )
    )
    qa: dict[str, Any] = {
        "schema": SMART_REFRAME_QA_SCHEMA,
        "inputs": inputs,
        "shot_checks": checks,
        "media_checks": media_checks,
        "summary": {
            "required_region_count": required_total,
            "retained_required_region_count": retained_total,
            "required_region_retention": round(retained_total / required_total, 6)
            if required_total
            else 1.0,
            "safe_zone_violation_count": safe_violations,
            "truncation_count": truncations,
            "planning_gate_passed": planning_passed,
            "render_gate_passed": rendered_passed,
            "status": "passed_candidate_only"
            if planning_passed and rendered_passed
            else "failed",
        },
        "authority_boundary": {
            "automatic_technical_qa": True,
            "creative_quality_claimed": False,
            "release_eligible": False,
            "candidate_only": True,
            "automatic_publish": False,
        },
    }
    qa["qa_sha256"] = payload_sha256(qa, "qa_sha256")
    validate_smart_reframe_qa(qa)
    return qa


def validate_smart_reframe_qa(qa: dict[str, Any]) -> None:
    _validate_schema(qa, "smart_reframe_qa.v1.schema.json", "reframe QA")
    if qa.get("schema") != SMART_REFRAME_QA_SCHEMA:
        raise ContractError("unsupported smart reframe QA schema")
    if qa.get("qa_sha256") != payload_sha256(qa, "qa_sha256"):
        raise ContractError("smart reframe QA integrity check failed")
    inputs = _object(qa.get("inputs"), "reframe QA inputs")
    plan_path = validate_artifact_reference(
        _object(inputs.get("plan"), "plan reference"), "smart reframe plan"
    )
    plan = read_json_object(plan_path, "smart reframe plan")
    validate_smart_reframe_plan(plan)
    render_ref = inputs.get("render_receipt")
    expected_media_checks = {
        "rendered": False,
        "dimensions_match": None,
        "duration_match": None,
        "single_ffmpeg_outlet_verified": None,
    }
    if render_ref is not None:
        render_path = validate_artifact_reference(
            _object(render_ref, "render receipt reference"),
            "smart reframe render receipt",
        )
        render = read_json_object(render_path, "smart reframe render receipt")
        if render.get("schema") != SMART_REFRAME_RENDER_RECEIPT_SCHEMA:
            raise ContractError("smart reframe QA render receipt schema drifted")
        validate_smart_reframe_render_receipt(render)
        candidate_path = validate_artifact_reference(
            _object(inputs.get("candidate_video"), "candidate video reference"),
            "smart reframe candidate video",
        )
        if render.get("candidate_video") != inputs.get("candidate_video"):
            raise ContractError("smart reframe QA candidate binding drifted")
        if not candidate_path.is_file():
            raise ContractError("smart reframe QA candidate disappeared")
        metadata = source_media_dimensions(candidate_path)
        target = plan["target_profile"]
        expected_media_checks = {
            "rendered": True,
            "dimensions_match": metadata["width"] == target["width"]
            and metadata["height"] == target["height"],
            "duration_match": abs(
                float(metadata["duration_s"])
                - float(plan["source_metadata"]["duration_s"])
            )
            <= 0.2,
            "single_ffmpeg_outlet_verified": True,
        }
    elif inputs.get("candidate_video") is not None:
        raise ContractError("smart reframe QA candidate requires a render receipt")
    if qa.get("media_checks") != expected_media_checks:
        raise ContractError("smart reframe QA media checks drifted")
    observations = _load_observations(plan)
    expected_checks = [
        _evaluate_shot(shot, observations, plan) for shot in plan.get("shots", [])
    ]
    if qa.get("shot_checks") != expected_checks:
        raise ContractError("smart reframe QA shot checks drifted")
    required_total = sum(row["required_region_count"] for row in expected_checks)
    retained_total = sum(
        row["retained_required_region_count"] for row in expected_checks
    )
    safe_violations = sum(row["safe_zone_violation_count"] for row in expected_checks)
    truncations = sum(row["truncation_count"] for row in expected_checks)
    planning_passed = all(row["passed"] for row in expected_checks)
    rendered_passed = not expected_media_checks["rendered"] or all(
        expected_media_checks[field] is True
        for field in (
            "dimensions_match",
            "duration_match",
            "single_ffmpeg_outlet_verified",
        )
    )
    expected_summary = {
        "required_region_count": required_total,
        "retained_required_region_count": retained_total,
        "required_region_retention": round(retained_total / required_total, 6)
        if required_total
        else 1.0,
        "safe_zone_violation_count": safe_violations,
        "truncation_count": truncations,
        "planning_gate_passed": planning_passed,
        "render_gate_passed": rendered_passed,
        "status": "passed_candidate_only"
        if planning_passed and rendered_passed
        else "failed",
    }
    if qa.get("summary") != expected_summary:
        raise ContractError("smart reframe QA summary drifted")
    if qa.get("authority_boundary") != {
        "automatic_technical_qa": True,
        "creative_quality_claimed": False,
        "release_eligible": False,
        "candidate_only": True,
        "automatic_publish": False,
    }:
        raise ContractError("smart reframe QA authority boundary drifted")


def write_smart_reframe_qa(qa: dict[str, Any], output_path: str | Path) -> Path:
    validate_smart_reframe_qa(qa)
    return atomic_write_json(qa, output_path)


def _evaluate_shot(
    shot: dict[str, Any], observations: list[dict[str, Any]], plan: dict[str, Any]
) -> dict[str, Any]:
    required = [
        row
        for row in observations
        if row.get("required") is True
        and row.get("rect") is not None
        and shot["start_s"] <= float(row["time_s"]) <= shot["end_s"] + 1e-6
    ]
    retained = 0
    truncations = 0
    safe_violations = 0
    details = []
    for row in required:
        output_rect = _project_rect(row, shot, plan)
        is_retained = output_rect is not None
        if is_retained:
            retained += 1
            safe = _safe_zone_pass(row["kind"], output_rect, plan["target_profile"])
            if not safe:
                safe_violations += 1
        else:
            truncations += 1
            safe = False
        details.append(
            {
                "evidence_id": row["evidence_id"],
                "kind": row["kind"],
                "retained": is_retained,
                "safe_zone_passed": safe,
                "output_rect": output_rect,
            }
        )
    quality = _object(shot.get("quality_estimate"), "shot quality estimate")
    passed = (
        shot.get("strategy") != "reject"
        and retained == len(required)
        and safe_violations == 0
        and truncations == 0
        and quality.get("quality_gate_passed") is True
    )
    return {
        "shot_id": shot["shot_id"],
        "strategy": shot["strategy"],
        "required_region_count": len(required),
        "retained_required_region_count": retained,
        "truncation_count": truncations,
        "safe_zone_violation_count": safe_violations,
        "details": details,
        "passed": passed,
    }


def _project_rect(
    observation: dict[str, Any], shot: dict[str, Any], plan: dict[str, Any]
) -> dict[str, float] | None:
    rect = observation["rect"]
    strategy = shot["strategy"]
    if strategy in _CROP_STRATEGIES:
        crop = _crop_at_time(
            shot["keyframes"],
            float(observation["time_s"]),
            hold=shot.get("interpolation") == "hold",
        )
        if crop is None or not _contains(crop, rect):
            return None
        return {
            "x": round((rect["x"] - crop["x"]) / crop["width"], 8),
            "y": round((rect["y"] - crop["y"]) / crop["height"], 8),
            "width": round(rect["width"] / crop["width"], 8),
            "height": round(rect["height"] / crop["height"], 8),
        }
    if strategy in _PAD_STRATEGIES:
        source = plan["source_metadata"]
        target = plan["target_profile"]
        scale = min(
            target["width"] / source["width"], target["height"] / source["height"]
        )
        content_width = source["width"] * scale / target["width"]
        content_height = source["height"] * scale / target["height"]
        offset_x = (1 - content_width) / 2
        offset_y = (1 - content_height) / 2
        return {
            "x": round(offset_x + rect["x"] * content_width, 8),
            "y": round(offset_y + rect["y"] * content_height, 8),
            "width": round(rect["width"] * content_width, 8),
            "height": round(rect["height"] * content_height, 8),
        }
    return None


def _safe_zone_pass(kind: str, rect: dict[str, float], profile: dict[str, Any]) -> bool:
    safe = profile["safe_zones"]
    center_x = rect["x"] + rect["width"] / 2
    center_y = rect["y"] + rect["height"] / 2
    if kind in {"face", "person", "active_speaker"}:
        return (
            safe["left"] <= center_x <= 1 - safe["right"]
            and safe["top"] <= center_y <= 1 - safe["bottom"]
        )
    return (
        rect["x"] >= safe["left"]
        and rect["y"] >= safe["top"]
        and rect["x"] + rect["width"] <= 1 - safe["right"]
        and rect["y"] + rect["height"] <= 1 - safe["bottom"]
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


def _load_observations(plan: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for reference in plan["inputs"]["evidence"]:
        path = validate_artifact_reference(reference, "smart reframe QA evidence")
        evidence = read_json_object(path, "smart reframe evidence")
        validate_smart_reframe_evidence(evidence)
        rows.extend(evidence.get("observations", []))
    return rows


def _contains(outer: dict[str, float], inner: dict[str, float]) -> bool:
    return (
        outer["x"] <= inner["x"] + 1e-8
        and outer["y"] <= inner["y"] + 1e-8
        and outer["x"] + outer["width"] + 1e-8 >= inner["x"] + inner["width"]
        and outer["y"] + outer["height"] + 1e-8 >= inner["y"] + inner["height"]
    )


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
