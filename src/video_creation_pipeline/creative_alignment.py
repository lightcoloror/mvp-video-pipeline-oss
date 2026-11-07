from __future__ import annotations

from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

from .content_video_pipeline import validate_content_video_project_plan
from .contract_io import (
    artifact_reference,
    atomic_write_json,
    payload_sha256,
    read_json_object,
    validate_artifact_reference,
)
from .errors import ContractError
from .previs_contracts import validate_previs_scene
from .semantic_motion import validate_semantic_motion_run_receipt

CREATIVE_ALIGNMENT_PLAN_SCHEMA = "video_creation_pipeline.creative_alignment_plan.v1"
CREATIVE_ALIGNMENT_SPEC_SCHEMA = "video_creation_pipeline.creative_alignment_spec.v1"

PRODUCTION_MODE_POLICIES = {
    "live_action_primary": {
        "human_capture_required": True,
        "ai_visual_scope": "internal_reference_only",
        "ai_voice_scope": "internal_reference_only",
        "identity_clone_allowed": False,
    },
    "ai_assisted": {
        "human_capture_required": True,
        "ai_visual_scope": "reference_or_bounded_effect_candidate",
        "ai_voice_scope": "internal_reference_only",
        "identity_clone_allowed": False,
    },
    "ai_led_sandbox": {
        "human_capture_required": False,
        "ai_visual_scope": "sandbox_candidate_only",
        "ai_voice_scope": "candidate_only",
        "identity_clone_allowed": False,
    },
}

REFERENCE_ASSET_ROLES = {
    "storyboard_candidate",
    "previs_scene",
    "semantic_motion_receipt",
    "ai_reference_video",
    "temporary_voice",
}


def build_creative_alignment_plan(
    project_plan_path: str | Path,
    cover_candidate_path: str | Path,
    spec_path: str | Path,
) -> dict[str, Any]:
    project_path = Path(project_plan_path).expanduser().resolve()
    cover_path = Path(cover_candidate_path).expanduser().resolve()
    source_spec_path = Path(spec_path).expanduser().resolve()
    project = read_json_object(project_path, "content video project plan")
    spec = read_json_object(source_spec_path, "creative alignment spec")
    validate_content_video_project_plan(project)
    normalized_spec = _validate_spec(spec, project)

    scene_ids = {
        str(row.get("scene_id") or "")
        for row in project["packaging_plan"]["scenes"]
        if isinstance(row, dict)
    }
    assets = _reference_assets(normalized_spec["reference_assets"], scene_ids)
    temporary_voice_assets = [
        row["artifact"] for row in assets if row["role"] == "temporary_voice"
    ]
    mode = normalized_spec["production_mode"]
    policy = dict(PRODUCTION_MODE_POLICIES[mode])
    plan: dict[str, Any] = {
        "schema": CREATIVE_ALIGNMENT_PLAN_SCHEMA,
        "source_project_plan": artifact_reference(
            project_path, "content video project plan"
        ),
        "source_spec": artifact_reference(source_spec_path, "creative alignment spec"),
        "project_plan_sha256": project["plan_sha256"],
        "premise": {
            "selected_title": normalized_spec["selected_title"],
            "audience": project["audience"],
            "thesis": project["thesis"],
            "cover_candidate": artifact_reference(cover_path, "cover candidate"),
            "cover_hypothesis": normalized_spec["cover_hypothesis"],
            "expensive_production_may_start": True,
        },
        "production_mode": {
            "mode": mode,
            **policy,
        },
        "reference_video": {
            "purpose": "internal_alignment_only",
            "render_status": "not_rendered",
            "render_route": "existing_renderer_then_single_ffmpeg_outlet",
            "assets": assets,
            "scene_projection": _scene_projection(project, assets, policy),
            "may_be_used_as_final_program": False,
        },
        "shooting_handoff": {
            "required": policy["human_capture_required"],
            "source_of_truth": "source_project_plan.shot_units",
            "scene_count": len(project["shot_units"]),
            "reference_video_is_acceptance_standard": False,
            "creative_intent_may_be_reinterpreted": True,
        },
        "release_policy": {
            "temporary_voice_assets": temporary_voice_assets,
            "temporary_voice_replacement_required": bool(temporary_voice_assets),
            "approved_final_voice_required": True,
            "final_voice_must_differ_from_temporary_voice": bool(
                temporary_voice_assets
            ),
            "title_cover_alignment_review_required": True,
            "reference_video_cannot_grant_release": True,
        },
        "authority_boundary": {
            "candidate_only": True,
            "creative_approval_claimed": False,
            "render_execution_performed": False,
            "external_provider_called": False,
            "upload_performed": False,
            "publish_performed": False,
            "second_pipeline_created": False,
            "second_state_machine_created": False,
            "second_ffmpeg_outlet_created": False,
        },
    }
    title_cover_binding = normalized_spec.get("title_cover_binding")
    if isinstance(title_cover_binding, dict):
        plan["source_title_cover_plan"] = title_cover_binding["artifact"]
        plan["premise"]["title_cover_candidate_id"] = title_cover_binding[
            "candidate_id"
        ]
        plan["premise"]["title_cover_platform_profile_ids"] = title_cover_binding[
            "profile_ids"
        ]
    plan["plan_sha256"] = payload_sha256(plan, "plan_sha256")
    validate_creative_alignment_plan(plan)
    return plan


def validate_creative_alignment_plan(plan: dict[str, Any]) -> None:
    schema = _load_schema()
    try:
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(plan)
    except (SchemaError, ValidationError) as exc:
        raise ContractError(
            f"creative alignment schema validation failed: {exc.message}"
        ) from exc
    if plan.get("plan_sha256") != payload_sha256(plan, "plan_sha256"):
        raise ContractError("creative alignment plan integrity check failed")

    project_path = validate_artifact_reference(
        _object(plan.get("source_project_plan")), "content video project plan"
    )
    spec_path = validate_artifact_reference(
        _object(plan.get("source_spec")), "creative alignment spec"
    )
    project = read_json_object(project_path, "content video project plan")
    spec = read_json_object(spec_path, "creative alignment spec")
    validate_content_video_project_plan(project)
    normalized_spec = _validate_spec(spec, project)
    if plan.get("project_plan_sha256") != project.get("plan_sha256"):
        raise ContractError("creative alignment plan drifted from its project plan")

    premise = _object(plan.get("premise"))
    validate_artifact_reference(
        _object(premise.get("cover_candidate")), "cover candidate"
    )
    expected_premise: dict[str, Any] = {
        "selected_title": normalized_spec["selected_title"],
        "audience": project["audience"],
        "thesis": project["thesis"],
        "cover_candidate": premise.get("cover_candidate"),
        "cover_hypothesis": normalized_spec["cover_hypothesis"],
        "expensive_production_may_start": True,
    }
    title_cover_binding = normalized_spec.get("title_cover_binding")
    if isinstance(title_cover_binding, dict):
        expected_premise["title_cover_candidate_id"] = title_cover_binding[
            "candidate_id"
        ]
        expected_premise["title_cover_platform_profile_ids"] = title_cover_binding[
            "profile_ids"
        ]
        if plan.get("source_title_cover_plan") != title_cover_binding["artifact"]:
            raise ContractError("creative alignment title cover source drifted")
        validate_artifact_reference(
            _object(plan.get("source_title_cover_plan")), "title cover candidate plan"
        )
    elif "source_title_cover_plan" in plan:
        raise ContractError("legacy creative alignment cannot bind a title cover plan")
    if premise != expected_premise:
        raise ContractError("creative premise drifted from its source inputs")

    mode = normalized_spec["production_mode"]
    if _object(plan.get("production_mode")) != {
        "mode": mode,
        **PRODUCTION_MODE_POLICIES[mode],
    }:
        raise ContractError("creative alignment production mode policy drifted")

    reference = _object(plan.get("reference_video"))
    scene_ids = {
        str(row.get("scene_id") or "")
        for row in project["packaging_plan"]["scenes"]
        if isinstance(row, dict)
    }
    assets = _reference_assets_from_plan(reference.get("assets"), scene_ids)
    expected_projection = _scene_projection(
        project, assets, PRODUCTION_MODE_POLICIES[mode]
    )
    if (
        reference.get("purpose") != "internal_alignment_only"
        or reference.get("render_status") != "not_rendered"
        or reference.get("render_route")
        != "existing_renderer_then_single_ffmpeg_outlet"
        or reference.get("scene_projection") != expected_projection
        or reference.get("may_be_used_as_final_program") is not False
    ):
        raise ContractError("creative alignment reference video contract drifted")

    shooting = _object(plan.get("shooting_handoff"))
    policy = PRODUCTION_MODE_POLICIES[mode]
    if shooting != {
        "required": policy["human_capture_required"],
        "source_of_truth": "source_project_plan.shot_units",
        "scene_count": len(project["shot_units"]),
        "reference_video_is_acceptance_standard": False,
        "creative_intent_may_be_reinterpreted": True,
    }:
        raise ContractError("creative alignment shooting handoff drifted")

    temporary = [row["artifact"] for row in assets if row["role"] == "temporary_voice"]
    release = _object(plan.get("release_policy"))
    if release != {
        "temporary_voice_assets": temporary,
        "temporary_voice_replacement_required": bool(temporary),
        "approved_final_voice_required": True,
        "final_voice_must_differ_from_temporary_voice": bool(temporary),
        "title_cover_alignment_review_required": True,
        "reference_video_cannot_grant_release": True,
    }:
        raise ContractError("creative alignment release policy drifted")

    boundary = _object(plan.get("authority_boundary"))
    if boundary != {
        "candidate_only": True,
        "creative_approval_claimed": False,
        "render_execution_performed": False,
        "external_provider_called": False,
        "upload_performed": False,
        "publish_performed": False,
        "second_pipeline_created": False,
        "second_state_machine_created": False,
        "second_ffmpeg_outlet_created": False,
    }:
        raise ContractError("creative alignment authority boundary is invalid")


def write_creative_alignment_plan(
    plan: dict[str, Any], output_path: str | Path
) -> Path:
    validate_creative_alignment_plan(plan)
    return atomic_write_json(plan, output_path)


def temporary_voice_sha256s(plan: dict[str, Any]) -> set[str]:
    validate_creative_alignment_plan(plan)
    return {
        str(row.get("sha256") or "")
        for row in _object(plan.get("release_policy")).get("temporary_voice_assets", [])
        if isinstance(row, dict)
    }


def _validate_spec(spec: dict[str, Any], project: dict[str, Any]) -> dict[str, Any]:
    if spec.get("schema") != CREATIVE_ALIGNMENT_SPEC_SCHEMA:
        raise ContractError("unsupported creative alignment spec schema")
    selected_title = _required_text(spec.get("selected_title"), "selected title")
    mode = str(spec.get("production_mode") or "")
    if mode not in PRODUCTION_MODE_POLICIES:
        raise ContractError(f"unsupported production mode: {mode}")
    cover = _object(spec.get("cover_hypothesis"))
    normalized_cover = {
        "visual_focus": _required_text(cover.get("visual_focus"), "cover visual focus"),
        "audience_promise": _required_text(
            cover.get("audience_promise"), "cover audience promise"
        ),
        "click_reason": _required_text(cover.get("click_reason"), "cover click reason"),
        "overlay_text": str(cover.get("overlay_text") or "").strip(),
    }
    if len(normalized_cover["overlay_text"]) > 24:
        raise ContractError("cover overlay text must not exceed 24 characters")
    assets = spec.get("reference_assets")
    if not isinstance(assets, list) or not assets:
        raise ContractError("creative alignment spec requires reference assets")
    result: dict[str, Any] = {
        "selected_title": selected_title,
        "production_mode": mode,
        "cover_hypothesis": normalized_cover,
        "reference_assets": assets,
    }
    title_cover_path = str(spec.get("title_cover_candidate_plan_path") or "").strip()
    title_cover_candidate_id = str(
        spec.get("selected_title_cover_candidate_id") or ""
    ).strip()
    if title_cover_path or title_cover_candidate_id:
        if not title_cover_path or not title_cover_candidate_id:
            raise ContractError(
                "title cover plan path and selected candidate id must be supplied together"
            )
        from .title_cover import (
            selected_title_cover_candidate,
            validate_title_cover_candidate_plan,
        )

        title_cover_plan = read_json_object(
            title_cover_path, "title cover candidate plan"
        )
        validate_title_cover_candidate_plan(title_cover_plan)
        candidate = selected_title_cover_candidate(title_cover_plan)
        if candidate.get("candidate_id") != title_cover_candidate_id:
            raise ContractError("selected title cover candidate drifted")
        if selected_title != candidate.get("title"):
            raise ContractError("selected title must match the title cover candidate")
        if normalized_cover != candidate.get("cover_hypothesis"):
            raise ContractError("cover hypothesis must match the title cover candidate")
        result["title_cover_binding"] = {
            "artifact": artifact_reference(
                title_cover_path, "title cover candidate plan"
            ),
            "candidate_id": title_cover_candidate_id,
            "profile_ids": list(candidate.get("profile_ids") or []),
        }
    elif selected_title != project.get("title"):
        raise ContractError(
            "selected title must match the content project plan unless a title cover plan is bound"
        )
    return result


def _reference_assets(values: list[Any], scene_ids: set[str]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    identities: set[tuple[str, str]] = set()
    for index, value in enumerate(values, 1):
        row = _object(value)
        role = str(row.get("role") or "")
        if role not in REFERENCE_ASSET_ROLES:
            raise ContractError(f"unsupported reference asset role: {role}")
        path = Path(str(row.get("path") or "")).expanduser().resolve()
        identity = (role, str(path))
        if identity in identities:
            raise ContractError("reference asset role/path pairs must be unique")
        identities.add(identity)
        bound_scenes = _scene_ids(row.get("scene_ids"), scene_ids)
        _validate_role_source(role, path, index)
        result.append(
            {
                "asset_id": f"reference-{index:03d}",
                "role": role,
                "artifact": artifact_reference(path, f"reference asset {index}"),
                "scene_ids": bound_scenes,
                "intended_use": "internal_alignment_only",
                "final_material": False,
                "evidence_claimed": False,
            }
        )
    return result


def _reference_assets_from_plan(
    values: Any, scene_ids: set[str]
) -> list[dict[str, Any]]:
    if not isinstance(values, list) or not values:
        raise ContractError("creative alignment plan requires reference assets")
    result: list[dict[str, Any]] = []
    identities: set[tuple[str, str]] = set()
    for index, value in enumerate(values, 1):
        row = _object(value)
        role = str(row.get("role") or "")
        if role not in REFERENCE_ASSET_ROLES:
            raise ContractError(f"unsupported reference asset role: {role}")
        path = validate_artifact_reference(
            _object(row.get("artifact")), f"reference asset {index}"
        )
        identity = (role, str(path))
        if identity in identities:
            raise ContractError("reference asset role/path pairs must be unique")
        identities.add(identity)
        bound_scenes = _scene_ids(row.get("scene_ids"), scene_ids)
        _validate_role_source(role, path, index)
        expected = {
            "asset_id": f"reference-{index:03d}",
            "role": role,
            "artifact": row["artifact"],
            "scene_ids": bound_scenes,
            "intended_use": "internal_alignment_only",
            "final_material": False,
            "evidence_claimed": False,
        }
        if row != expected:
            raise ContractError("reference asset policy drifted")
        result.append(expected)
    return result


def _validate_role_source(role: str, path: Path, index: int) -> None:
    if role == "previs_scene":
        validate_previs_scene(read_json_object(path, f"previs scene {index}"))
    elif role == "semantic_motion_receipt":
        validate_semantic_motion_run_receipt(
            read_json_object(path, f"semantic motion receipt {index}")
        )
    elif role == "ai_reference_video" and path.suffix.lower() not in {
        ".mp4",
        ".mov",
        ".mkv",
        ".webm",
    }:
        raise ContractError("AI reference video must be a supported video file")
    elif role == "temporary_voice" and path.suffix.lower() not in {
        ".wav",
        ".mp3",
        ".m4a",
        ".flac",
    }:
        raise ContractError("temporary voice must be a supported audio file")


def _scene_projection(
    project: dict[str, Any],
    assets: list[dict[str, Any]],
    mode_policy: dict[str, Any],
) -> list[dict[str, Any]]:
    units = {
        str(row.get("scene_id") or ""): row
        for row in project["shot_units"]
        if isinstance(row, dict)
    }
    result = []
    for raw in project["packaging_plan"]["scenes"]:
        scene = _object(raw)
        scene_id = str(scene.get("scene_id") or "")
        unit = _object(units.get(scene_id))
        result.append(
            {
                "scene_id": scene_id,
                "start_s": scene.get("start_s"),
                "end_s": scene.get("end_s"),
                "purpose": scene.get("purpose"),
                "narration": unit.get("narration"),
                "visual_intent": scene.get("visual_intent"),
                "evidence_refs": scene.get("evidence_refs"),
                "material_status": scene.get("material_status"),
                "human_capture_required": mode_policy["human_capture_required"],
                "reference_asset_ids": [
                    row["asset_id"] for row in assets if scene_id in row["scene_ids"]
                ],
            }
        )
    return result


def _scene_ids(value: Any, available: set[str]) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ContractError("reference assets require at least one scene id")
    result: list[str] = []
    for raw in value:
        scene_id = str(raw or "").strip()
        if scene_id not in available:
            raise ContractError(f"reference asset uses unknown scene id: {scene_id}")
        if scene_id in result:
            raise ContractError("reference asset scene ids must be unique")
        result.append(scene_id)
    return result


def _required_text(value: Any, label: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise ContractError(f"{label} is required")
    if len(result) > 240:
        raise ContractError(f"{label} is too long")
    return result


def _load_schema() -> dict[str, Any]:
    return read_json_object(
        Path(__file__).with_name("schemas") / "creative_alignment_plan.v1.schema.json",
        "creative alignment plan schema",
    )


def _object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}
