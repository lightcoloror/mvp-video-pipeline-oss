from __future__ import annotations

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
from .photo_layout import validate_safe_media_layout_plan

SAFE_MEDIA_SCENE_PLAN_SPEC_SCHEMA = (
    "video_creation_pipeline.safe_media_scene_plan_spec.v1"
)
SAFE_MEDIA_SCENE_PLAN_SCHEMA = "video_creation_pipeline.safe_media_scene_plan.v1"


def build_safe_media_scene_plan(
    packaging_plan_path: str | Path,
    spec_path: str | Path,
) -> dict[str, Any]:
    packaging_path = Path(packaging_plan_path).expanduser().resolve()
    source_spec_path = Path(spec_path).expanduser().resolve()
    packaging = read_json_object(packaging_path, "video packaging plan")
    spec = read_json_object(source_spec_path, "safe media scene plan spec")
    packaging_scenes, output = _packaging_projection(packaging)
    normalized = _normalize_spec(spec, packaging_scenes, output)
    plan: dict[str, Any] = {
        "schema": SAFE_MEDIA_SCENE_PLAN_SCHEMA,
        "source_packaging_plan": artifact_reference(
            packaging_path, "video packaging plan"
        ),
        "source_spec": artifact_reference(
            source_spec_path, "safe media scene plan spec"
        ),
        "output": output,
        "scenes": normalized,
        "summary": {
            "scene_count": len(normalized),
            "layout_ready_count": sum(
                row["assignment_status"] == "layout_ready" for row in normalized
            ),
            "missing_material_count": sum(
                row["assignment_status"] == "missing_material" for row in normalized
            ),
        },
        "renderer_handoff": {
            "allowed_generators": ["hyperframes", "remotion"],
            "requires_existing_single_ffmpeg_outlet": True,
            "renderer_execution_performed": False,
            "candidate_only": True,
            "automatic_publish": False,
        },
        "authority_boundary": {
            "packaging_timeline_modified": False,
            "source_media_modified": False,
            "source_media_copied": False,
            "automatic_crop": False,
            "automatic_render": False,
            "network_execution_performed": False,
            "automatic_publish": False,
        },
    }
    plan["plan_sha256"] = payload_sha256(plan, "plan_sha256")
    validate_safe_media_scene_plan(plan)
    return plan


def validate_safe_media_scene_plan(plan: dict[str, Any]) -> None:
    if plan.get("schema") != SAFE_MEDIA_SCENE_PLAN_SCHEMA:
        raise ContractError("unsupported safe media scene plan schema")
    if plan.get("plan_sha256") != payload_sha256(plan, "plan_sha256"):
        raise ContractError("safe media scene plan integrity check failed")
    packaging_path = validate_artifact_reference(
        _object(plan.get("source_packaging_plan")), "video packaging plan"
    )
    spec_path = validate_artifact_reference(
        _object(plan.get("source_spec")), "safe media scene plan spec"
    )
    packaging = read_json_object(packaging_path, "video packaging plan")
    spec = read_json_object(spec_path, "safe media scene plan spec")
    packaging_scenes, output = _packaging_projection(packaging)
    expected = _normalize_spec(spec, packaging_scenes, output)
    if plan.get("output") != output or plan.get("scenes") != expected:
        raise ContractError("safe media scene plan drifted from its inputs")
    summary = _object(plan.get("summary"))
    expected_summary = {
        "scene_count": len(expected),
        "layout_ready_count": sum(
            row["assignment_status"] == "layout_ready" for row in expected
        ),
        "missing_material_count": sum(
            row["assignment_status"] == "missing_material" for row in expected
        ),
    }
    if summary != expected_summary:
        raise ContractError("safe media scene plan summary drifted")
    handoff = _object(plan.get("renderer_handoff"))
    if handoff != {
        "allowed_generators": ["hyperframes", "remotion"],
        "requires_existing_single_ffmpeg_outlet": True,
        "renderer_execution_performed": False,
        "candidate_only": True,
        "automatic_publish": False,
    }:
        raise ContractError("safe media scene renderer handoff is invalid")
    boundary = _object(plan.get("authority_boundary"))
    if boundary != {
        "packaging_timeline_modified": False,
        "source_media_modified": False,
        "source_media_copied": False,
        "automatic_crop": False,
        "automatic_render": False,
        "network_execution_performed": False,
        "automatic_publish": False,
    }:
        raise ContractError("safe media scene authority boundary is invalid")


def write_safe_media_scene_plan(plan: dict[str, Any], output_path: str | Path) -> Path:
    validate_safe_media_scene_plan(plan)
    return atomic_write_json(plan, output_path)


def _normalize_spec(
    spec: dict[str, Any],
    packaging_scenes: list[dict[str, Any]],
    output: dict[str, int],
) -> list[dict[str, Any]]:
    if spec.get("schema") != SAFE_MEDIA_SCENE_PLAN_SPEC_SCHEMA:
        raise ContractError("unsupported safe media scene plan spec schema")
    values = spec.get("scenes")
    if not isinstance(values, list) or not values:
        raise ContractError("safe media scene plan spec requires scenes")
    available = {row["scene_id"] for row in packaging_scenes}
    by_scene: dict[str, dict[str, Any]] = {}
    for raw in values:
        row = _object(raw)
        scene_id = str(row.get("scene_id") or "").strip()
        if scene_id not in available:
            raise ContractError(f"safe media assignment uses unknown scene: {scene_id}")
        if scene_id in by_scene:
            raise ContractError("safe media scene assignments must be unique")
        by_scene[scene_id] = row
    if set(by_scene) != available:
        raise ContractError(
            "every packaging scene requires an explicit media assignment"
        )

    result: list[dict[str, Any]] = []
    layout_count = 0
    for scene in packaging_scenes:
        assignment = by_scene[scene["scene_id"]]
        layout_value = str(assignment.get("safe_media_layout_path") or "").strip()
        missing_reason = str(assignment.get("missing_material_reason") or "").strip()
        if bool(layout_value) == bool(missing_reason):
            raise ContractError(
                "each scene requires exactly one layout path or missing-material reason"
            )
        if layout_value:
            layout_path = Path(layout_value).expanduser().resolve()
            layout = read_json_object(layout_path, "safe media layout plan")
            validate_safe_media_layout_plan(layout)
            layout_output = _object(layout.get("output"))
            if (
                int(layout_output.get("width") or 0) != output["width"]
                or int(layout_output.get("height") or 0) != output["height"]
            ):
                raise ContractError(
                    "safe media scene layout dimensions must match packaging output"
                )
            placements = layout.get("placements")
            if not isinstance(placements, list) or not placements:
                raise ContractError("safe media scene layout has no placements")
            layout_count += 1
            result.append(
                {
                    **scene,
                    "assignment_status": "layout_ready",
                    "safe_media_layout_plan": artifact_reference(
                        layout_path, "safe media layout plan"
                    ),
                    "asset_ids": [
                        str(_object(row).get("asset_id") or "") for row in placements
                    ],
                    "missing_material_reason": None,
                }
            )
        else:
            result.append(
                {
                    **scene,
                    "assignment_status": "missing_material",
                    "safe_media_layout_plan": None,
                    "asset_ids": [],
                    "missing_material_reason": missing_reason,
                }
            )
    if layout_count == 0:
        raise ContractError("safe media scene plan requires at least one ready layout")
    return result


def _packaging_projection(
    packaging: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    if packaging.get("schema") != VIDEO_PACKAGING_PLAN_SCHEMA:
        raise ContractError("safe media scene plan requires a video packaging plan")
    raw_output = _object(packaging.get("output"))
    output = {
        "width": _positive_int(raw_output.get("width"), "packaging width"),
        "height": _positive_int(raw_output.get("height"), "packaging height"),
        "fps": _positive_int(raw_output.get("fps"), "packaging fps"),
    }
    values = packaging.get("scenes")
    if not isinstance(values, list) or not values:
        raise ContractError("video packaging plan requires scenes")
    scenes: list[dict[str, Any]] = []
    seen: set[str] = set()
    previous_end = 0.0
    for raw in values:
        row = _object(raw)
        scene_id = str(row.get("scene_id") or "").strip()
        if not scene_id or scene_id in seen:
            raise ContractError("video packaging scene IDs must be unique")
        seen.add(scene_id)
        start_s = _nonnegative_number(
            row.get("start_s", row.get("start")), "packaging scene start"
        )
        end_s = _positive_number(
            row.get("end_s", row.get("end")), "packaging scene end"
        )
        if end_s <= start_s or start_s < previous_end:
            raise ContractError("video packaging scene timing is invalid")
        previous_end = end_s
        scenes.append(
            {
                "scene_id": scene_id,
                "start_s": start_s,
                "end_s": end_s,
            }
        )
    return scenes, output


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise ContractError(f"{label} must be a positive integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{label} must be a positive integer") from exc
    if result <= 0:
        raise ContractError(f"{label} must be a positive integer")
    return result


def _nonnegative_number(value: Any, label: str) -> float:
    result = _number(value, label)
    if result < 0:
        raise ContractError(f"{label} must be nonnegative")
    return result


def _positive_number(value: Any, label: str) -> float:
    result = _number(value, label)
    if result <= 0:
        raise ContractError(f"{label} must be positive")
    return result


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise ContractError(f"{label} must be finite")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{label} must be finite") from exc
    if result != result or result in {float("inf"), float("-inf")}:
        raise ContractError(f"{label} must be finite")
    return result


def _object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError("expected an object")
    return value
