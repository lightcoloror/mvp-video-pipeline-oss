from __future__ import annotations

import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError, ValidationError

from .content_video_foundation import (
    DEFAULT_CONTENT_TIME_SENSITIVITY,
    normalize_content_time_sensitivity,
    validate_canonical_artifact,
)
from .contract_io import (
    artifact_reference,
    atomic_write_json,
    atomic_write_text,
    canonical_json_sha256,
    payload_sha256,
    read_json_object,
    validate_artifact_reference,
)
from .errors import ContractError
from .music_pipeline import build_music_intent_curve, write_music_contract
from .reference_remix import REFERENCE_RECIPE_SCHEMA, normalize_reference_recipe
from .tts_pipeline import TTS_CONFIG_SCHEMA, build_tts_job, write_tts_contract

CONTENT_VIDEO_PROJECT_PLAN_SCHEMA = (
    "video_creation_pipeline.content_video_project_plan.v1"
)
CONTENT_SHOT_GENERATION_UNIT_SCHEMA = (
    "video_creation_pipeline.content_shot_generation_unit.v1"
)
CONTENT_VIDEO_PLAN_MANIFEST_SCHEMA = (
    "video_creation_pipeline.content_video_plan_manifest.v1"
)
CONTENT_AUDIO_INTENT_SCHEMA = "video_creation_pipeline.content_audio_intent.v1"
VIDEO_PACKAGING_PLAN_SCHEMA = "video_creation_pipeline.video_packaging_plan.v1"
EDIT_DECISION_LIST_SCHEMA = "video_creation_pipeline.edit_decision_list.v1"
CONTENT_VIDEO_OUTPUT_OWNER_SCHEMA = (
    "video_creation_pipeline.content_video_output_owner.v1"
)


def validate_content_video_inputs(
    brief_path: str | Path,
    brief_schema_path: str | Path,
    research_output_path: str | Path,
    research_schema_path: str | Path,
    *,
    max_age_hours: float,
    time_sensitivity: str = DEFAULT_CONTENT_TIME_SENSITIVITY,
    case_handoff_manifest_path: str | Path | None = None,
    case_handoff_manifest_sha256: str | None = None,
    require_case_handoff: bool = False,
) -> dict[str, Any]:
    sensitivity = normalize_content_time_sensitivity(time_sensitivity)
    brief_validation = validate_canonical_artifact(
        brief_path,
        brief_schema_path,
        max_age_hours=max_age_hours,
        time_sensitivity=sensitivity,
    )
    brief = brief_validation["payload"]
    if brief.get("schema_version") != "content-video-brief.v1":
        raise ContractError("unsupported canonical content video brief version")

    research_path = Path(research_output_path).expanduser().resolve()
    research_schema_file = Path(research_schema_path).expanduser().resolve()
    research = read_json_object(research_path, "research output")
    research_schema = read_json_object(research_schema_file, "research output schema")
    _validate_schema_instance(research, research_schema, "research output")
    research_age = _evaluate_artifact_age(
        research_path,
        research.get("created_at"),
        max_age_hours,
        "research output",
        sensitivity,
    )

    provenance = _object(brief.get("provenance"))
    expected_research_hash = canonical_json_sha256(research)
    if provenance.get("input_sha256") != expected_research_hash:
        raise ContractError("content video brief research input hash is stale")
    if provenance.get("input_schema_id") != research_schema.get("$id"):
        raise ContractError("content video brief research schema identity drifted")
    if provenance.get("research_id") != research.get("research_id"):
        raise ContractError("content video brief research id drifted")
    brief_age = _evaluate_artifact_age(
        Path(brief_path).expanduser().resolve(),
        provenance.get("generated_at"),
        max_age_hours,
        "content video brief",
        sensitivity,
    )

    case_handoff = validate_case_handoff_manifest(
        case_handoff_manifest_path,
        case_handoff_manifest_sha256,
        brief_path=brief_path,
        brief=brief,
        research=research,
    )
    if require_case_handoff and case_handoff is None:
        raise ContractError("case handoff is required by the selected consumer route")

    referenced_source_ids = _validate_evidence_references(brief, research)
    source_freshness = _validate_referenced_sources(
        research,
        referenced_source_ids,
        sensitivity,
    )
    blockers = _input_blockers(brief, research)
    freshness_policy: dict[str, Any] = {
        "schema": "video_creation_pipeline.content_freshness_policy.v1",
        "time_sensitivity": sensitivity,
        "age_gate_applied": sensitivity == "high_temporal",
        "max_age_hours": (
            float(max_age_hours) if sensitivity == "high_temporal" else None
        ),
        "future_timestamp_gate_applied": True,
        "source_freshness_gate_applied": sensitivity == "high_temporal",
    }
    freshness_evaluation = {
        "schema": "video_creation_pipeline.content_freshness_evaluation.v1",
        "policy": dict(freshness_policy),
        "brief_file": dict(brief_validation["freshness_evaluation"]),
        "brief_generation": brief_age,
        "research_output": research_age,
        "referenced_sources": source_freshness,
        "decision": (
            "age_gate_passed" if sensitivity == "high_temporal" else "age_not_required"
        ),
    }
    result = {
        "brief": brief,
        "research": research,
        "brief_artifact": brief_validation["artifact"],
        "brief_schema_artifact": brief_validation["schema_artifact"],
        "research_artifact": artifact_reference(research_path, "research output"),
        "research_schema_artifact": artifact_reference(
            research_schema_file, "research output schema"
        ),
        "schema_version": brief["schema_version"],
        "research_schema_id": str(research_schema.get("$id") or ""),
        "referenced_source_ids": sorted(referenced_source_ids),
        "freshness_policy": freshness_policy,
        "freshness_evaluation": freshness_evaluation,
        "blockers": blockers,
    }
    if case_handoff is not None:
        result["case_handoff_manifest_artifact"] = case_handoff["artifact"]
        result["case_projection_sha256"] = case_handoff["projection_sha256"]
        result["case_script_id"] = case_handoff["script_id"]
    return result


def validate_case_handoff_manifest(
    manifest_path: str | Path | None,
    expected_manifest_sha256: str | None,
    *,
    brief_path: str | Path,
    brief: dict[str, Any],
    research: dict[str, Any],
) -> dict[str, Any] | None:
    """Validate the Content Studio case projection without adding a video state."""

    provenance = _object(brief.get("provenance"))
    adapter_version = str(provenance.get("adapter_version") or "")
    version_marker = "+case-script." in adapter_version
    upstream_rows = [
        _object(row) for row in provenance.get("upstream_input_files") or []
    ]
    script_markers = [
        row
        for row in upstream_rows
        if str(row.get("path") or "").startswith("artifact://case-video-script/")
    ]
    upstream_marker = len(script_markers) == 1
    if version_marker != upstream_marker:
        raise ContractError("case handoff markers disagree")

    supplied = manifest_path is not None or expected_manifest_sha256 is not None
    if (manifest_path is None) != (expected_manifest_sha256 is None):
        raise ContractError("case handoff arguments must be supplied together")
    if version_marker and not supplied:
        raise ContractError("case handoff manifest required for case projection")
    if not version_marker and supplied:
        raise ContractError("case handoff manifest supplied for non-case brief")
    if not version_marker:
        return None

    resolved = Path(str(manifest_path)).expanduser().resolve()
    manifest = read_json_object(resolved, "case handoff manifest")
    artifact = artifact_reference(resolved, "case handoff manifest")
    if artifact.get("canonical_sha256") != expected_manifest_sha256:
        raise ContractError("case handoff manifest hash is stale")
    if manifest.get("schema_version") != "case-video-script-handoff.v1":
        raise ContractError("unsupported case handoff manifest schema")
    expected_manifest_keys = {
        "schema_version",
        "status",
        "adapter",
        "adapter_version",
        "handoff_id",
        "script_id",
        "content_id",
        "brief_id",
        "synthetic_only",
        "inputs",
        "output",
        "brief_projection_sha256",
        "selected_hook_id",
        "selected_hook_binding",
        "interview_slots",
        "experiment",
        "boundaries",
    }
    if set(manifest) != expected_manifest_keys:
        raise ContractError("case handoff manifest shape is invalid")
    if (
        manifest.get("status") != "generated_draft_only"
        or manifest.get("adapter") != "case-video-script-sidecar"
    ):
        raise ContractError("case handoff manifest status or adapter is invalid")
    if manifest.get("synthetic_only") is not True:
        raise ContractError("real case handoff review owner is unavailable")
    constraints = set(_object(research.get("scope")).get("constraints") or [])
    if "synthetic-only" not in constraints:
        raise ContractError("case handoff requires a synthetic-only research artifact")

    boundaries = _object(manifest.get("boundaries"))
    expected_boundaries = {
        "draft_only": True,
        "manual_only": True,
        "candidate_visuals_only": True,
        "external_api_called": False,
        "would_publish": False,
    }
    if set(boundaries) != set(expected_boundaries):
        raise ContractError("case handoff manifest shape is invalid")
    if boundaries != expected_boundaries:
        raise ContractError("case handoff boundary is invalid")
    if manifest.get("content_id") != brief.get("content_id"):
        raise ContractError("case handoff content id drifted")
    if manifest.get("brief_id") != brief.get("brief_id"):
        raise ContractError("case handoff brief id drifted")
    if provenance.get("manifest_ref") != resolved.name:
        raise ContractError("case handoff manifest ref drifted")
    manifest_version = str(manifest.get("adapter_version") or "")
    if not adapter_version.endswith(f"+case-script.{manifest_version}"):
        raise ContractError("case handoff adapter version drifted")

    output = _object(manifest.get("output"))
    if set(output) != {"path", "brief_id", "schema_version", "sha256"}:
        raise ContractError("case handoff manifest shape is invalid")
    if output.get("path") != Path(brief_path).expanduser().resolve().name:
        raise ContractError("case handoff brief artifact path drifted")
    if output.get("brief_id") != brief.get("brief_id") or output.get(
        "schema_version"
    ) != brief.get("schema_version"):
        raise ContractError("case handoff brief artifact identity drifted")
    actual_brief_sha256 = canonical_json_sha256(brief)
    if output.get("sha256") != actual_brief_sha256:
        raise ContractError("case handoff brief hash is stale")

    inputs = _object(manifest.get("inputs"))
    expected_input_keys = {
        "case_video_script",
        "research_output",
        "content_item",
        "fact_card",
        "route_card",
        "review_receipt",
    }
    if set(inputs) != expected_input_keys:
        raise ContractError("case handoff manifest shape is invalid")
    for reference in inputs.values():
        if set(_object(reference)) != {"artifact_id", "schema_version", "sha256"}:
            raise ContractError("case handoff manifest shape is invalid")
    script_ref = _object(inputs.get("case_video_script"))
    content_ref = _object(inputs.get("content_item"))
    if script_ref.get("artifact_id") != manifest.get("script_id"):
        raise ContractError("case handoff script id drifted")
    if content_ref.get("artifact_id") != manifest.get("content_id"):
        raise ContractError("case handoff content artifact drifted")
    research_ref = _object(inputs.get("research_output"))
    if (
        research_ref.get("artifact_id") != research.get("research_id")
        or research_ref.get("schema_version") != research.get("schema_version")
        or research_ref.get("sha256") != canonical_json_sha256(research)
    ):
        raise ContractError("case handoff research hash drifted")
    upstream_by_path: dict[str, list[dict[str, Any]]] = {}
    for row in upstream_rows:
        upstream_by_path.setdefault(str(row.get("path") or ""), []).append(row)
    for input_key, label in (
        ("case_video_script", "case-video-script"),
        ("content_item", "content-item"),
        ("fact_card", "fact-card"),
        ("route_card", "route-card"),
        ("review_receipt", "case-review"),
    ):
        reference = _object(inputs.get(input_key))
        uri = f"artifact://{label}/{reference.get('artifact_id') or ''}"
        matches = upstream_by_path.get(uri, [])
        if len(matches) != 1 or matches[0].get("sha256") != reference.get("sha256"):
            raise ContractError(f"case handoff upstream hash drifted: {input_key}")

    hook_binding = _object(manifest.get("selected_hook_binding"))
    if set(hook_binding) != {"hook_id", "text_sha256", "evidence_refs_sha256"}:
        raise ContractError("case handoff manifest shape is invalid")
    if hook_binding.get("hook_id") != manifest.get("selected_hook_id"):
        raise ContractError("case handoff selected hook id drifted")
    chapters = brief.get("chapters")
    scenes = brief.get("scenes")
    if (
        not isinstance(chapters, list)
        or not chapters
        or not isinstance(scenes, list)
        or not scenes
    ):
        raise ContractError("case handoff selected hook projection is missing")
    chapter = _object(chapters[0])
    scene = _object(scenes[0])
    hook_text = str(chapter.get("narration_draft") or "")
    if (
        chapter.get("purpose") != "hook"
        or chapter.get("title") != "selected-hook"
        or scene.get("purpose") != "selected-hook"
        or scene.get("narration_draft") != hook_text
        or hook_binding.get("text_sha256") != canonical_json_sha256(hook_text)
        or hook_binding.get("evidence_refs_sha256")
        != canonical_json_sha256(chapter.get("evidence_refs") or [])
        or scene.get("evidence_refs") != chapter.get("evidence_refs")
    ):
        raise ContractError("case handoff selected hook projection drifted")

    interview_slots = manifest.get("interview_slots")
    if not isinstance(interview_slots, list):
        raise ContractError("case handoff manifest shape is invalid")
    slot_ids: set[str] = set()
    for slot_value in interview_slots:
        slot = _object(slot_value)
        if set(slot) != {"slot_id", "state", "story_segment_ref", "fallback_edit"}:
            raise ContractError("case handoff manifest shape is invalid")
        if slot.get("state") not in {"planned", "excluded"}:
            raise ContractError(
                "case handoff interview is not authorized for projection"
            )
        slot_id = str(slot.get("slot_id") or "")
        if not slot_id or slot_id in slot_ids:
            raise ContractError("case handoff interview slot identity is invalid")
        slot_ids.add(slot_id)

    experiment = _object(manifest.get("experiment"))
    if set(experiment) != {"variable_to_change", "constants", "no_signal_meaning"}:
        raise ContractError("case handoff manifest shape is invalid")
    if (
        not isinstance(experiment.get("variable_to_change"), str)
        or not experiment.get("variable_to_change")
        or not isinstance(experiment.get("constants"), list)
        or not experiment.get("constants")
        or experiment.get("no_signal_meaning")
        != "content_no_signal_is_observation_only"
    ):
        raise ContractError("case handoff experiment boundary is invalid")

    projection = {
        key: value
        for key, value in manifest.items()
        if key != "brief_projection_sha256"
    }
    projection_sha256 = canonical_json_sha256(projection)
    if manifest.get("brief_projection_sha256") != projection_sha256:
        raise ContractError("case handoff projection hash is stale")
    return {
        "artifact": artifact,
        "projection_sha256": projection_sha256,
        "script_id": str(manifest.get("script_id") or ""),
    }


def build_content_video_project_plan(
    inputs: dict[str, Any],
    *,
    width: int = 1280,
    height: int = 720,
) -> dict[str, Any]:
    brief = _object(inputs.get("brief"))
    scenes = brief.get("scenes")
    if not isinstance(scenes, list) or not scenes:
        raise ContractError("content video brief must contain scenes")
    timings = _allocate_scene_timings(
        scenes,
        float(brief.get("target_duration_seconds") or 0),
    )
    shot_units = []
    packaging_scenes = []
    edl_ranges = []
    for scene, timing in zip(scenes, timings, strict=True):
        scene_id = str(scene["scene_id"])
        visual_leads = [
            dict(row)
            for row in scene.get("visual_leads") or []
            if isinstance(row, dict)
        ]
        evidence_refs = [
            dict(row)
            for row in scene.get("evidence_refs") or []
            if isinstance(row, dict)
        ]
        visual_prompt = (
            "; ".join(
                str(row.get("description") or "").strip()
                for row in visual_leads
                if str(row.get("description") or "").strip()
            )
            or "No confirmed visual material; keep this shot unresolved."
        )
        unit: dict[str, Any] = {
            "schema": CONTENT_SHOT_GENERATION_UNIT_SCHEMA,
            "scene_id": scene_id,
            "chapter_ref": str(scene.get("chapter_ref") or ""),
            "purpose": str(scene.get("purpose") or ""),
            "duration_s": timing["duration_s"],
            "media_type": "video",
            "output": {"width": int(width), "height": int(height)},
            "generator": "manual",
            "required_capability": "manual_media_import",
            "narration": str(scene.get("narration_draft") or ""),
            "visual_prompt": visual_prompt,
            "visual_leads": visual_leads,
            "evidence_refs": evidence_refs,
            "material": {
                "status": "missing_material",
                "reason": (
                    "canonical brief contains candidate visual leads, not a local "
                    "media artifact"
                ),
            },
            "status": "candidate_only",
            "human_confirmed": False,
            "mutates_vkp_timeline": False,
        }
        unit["unit_sha256"] = payload_sha256(unit, "unit_sha256")
        shot_units.append(unit)
        packaging_scenes.append(
            {
                "scene_id": scene_id,
                "start": timing["start_s"],
                "end": timing["end_s"],
                "start_s": timing["start_s"],
                "end_s": timing["end_s"],
                "purpose": unit["purpose"],
                "visual_intent": visual_prompt,
                "material_status": "missing_material",
                "evidence_refs": evidence_refs,
                "subtitle_candidates": list(scene.get("subtitle_candidates") or []),
            }
        )
        edl_ranges.append(
            {
                "scene_id": scene_id,
                "source": None,
                "source_start_s": None,
                "source_end_s": None,
                "output_start_s": timing["start_s"],
                "output_end_s": timing["end_s"],
                "material_status": "missing_material",
                "evidence_refs": evidence_refs,
            }
        )

    plan: dict[str, Any] = {
        "schema": CONTENT_VIDEO_PROJECT_PLAN_SCHEMA,
        "brief_id": str(brief.get("brief_id") or ""),
        "content_id": str(brief.get("content_id") or ""),
        "title": str(brief.get("title") or ""),
        "thesis": str(brief.get("thesis") or ""),
        "audience": str(brief.get("audience") or ""),
        "target_duration_s": float(brief.get("target_duration_seconds") or 0),
        "created_at": str(_object(brief.get("provenance")).get("generated_at") or ""),
        "inputs": {
            "content_video_brief": dict(inputs["brief_artifact"]),
            "content_video_brief_schema": dict(inputs["brief_schema_artifact"]),
            "research_output": dict(inputs["research_artifact"]),
            "research_output_schema": dict(inputs["research_schema_artifact"]),
            "research_schema_id": str(inputs["research_schema_id"]),
            "referenced_source_ids": list(inputs["referenced_source_ids"]),
            "freshness_policy": dict(inputs["freshness_policy"]),
        },
        "shot_units": shot_units,
        "packaging_plan": {
            "schema": VIDEO_PACKAGING_PLAN_SCHEMA,
            "aspect_ratio": "16:9",
            "output": {"width": int(width), "height": int(height), "fps": 30},
            "scenes": packaging_scenes,
            "status": "candidate_only",
        },
        "edit_decision_list": {
            "schema": EDIT_DECISION_LIST_SCHEMA,
            "total_duration_s": float(brief.get("target_duration_seconds") or 0),
            "ranges": edl_ranges,
            "execution_ready": False,
            "blocking_reason": "missing_material",
        },
        "audio_projection": {
            "narration_source": "scene.narration_draft",
            "tts_execution_authorized": False,
            "music_relationship_mode": "neutral_support",
            "synthetic_audio_allowed_for_fixture": True,
        },
        "unknowns": [dict(row) for row in brief.get("unknowns") or []],
        "forbidden_expressions": list(brief.get("forbidden_expressions") or []),
        "review_gate": dict(_object(brief.get("review_gate"))),
        "readiness": {
            "status": "blocked" if inputs["blockers"] else "candidate_plan_ready",
            "blockers": list(inputs["blockers"]),
            "missing_material_scene_ids": [row["scene_id"] for row in shot_units],
            "production_render_eligible": False,
            "synthetic_fixture_render_eligible": not inputs["blockers"],
        },
        "authority_boundary": {
            "candidate_only": True,
            "production_success_claimed": False,
            "synthetic_fixture_is_production_material": False,
            "mutates_vkp": False,
            "external_provider_called": False,
            "upload_performed": False,
            "publish_performed": False,
        },
    }
    if "case_handoff_manifest_artifact" in inputs:
        plan["inputs"]["case_handoff_manifest"] = dict(
            inputs["case_handoff_manifest_artifact"]
        )
        plan["inputs"]["case_projection_sha256"] = str(inputs["case_projection_sha256"])
        plan["inputs"]["case_script_id"] = str(inputs["case_script_id"])
    plan["plan_sha256"] = payload_sha256(plan, "plan_sha256")
    validate_content_video_project_plan(plan)
    return plan


def validate_content_video_project_plan(plan: dict[str, Any]) -> None:
    if plan.get("schema") != CONTENT_VIDEO_PROJECT_PLAN_SCHEMA:
        raise ContractError("unsupported content video project plan schema")
    if plan.get("plan_sha256") != payload_sha256(plan, "plan_sha256"):
        raise ContractError("content video project plan integrity check failed")
    inputs = _object(plan.get("inputs"))
    _validate_content_freshness_policy(_object(inputs.get("freshness_policy")))
    input_paths: dict[str, Path] = {}
    for field in (
        "content_video_brief",
        "content_video_brief_schema",
        "research_output",
        "research_output_schema",
    ):
        artifact_reference_path = _object(inputs.get(field))
        path = Path(str(artifact_reference_path.get("path") or ""))
        if not path.is_file():
            raise ContractError(f"project plan input disappeared: {field}")
        actual = artifact_reference(path, field)
        if actual != artifact_reference_path:
            raise ContractError(f"project plan input changed: {field}")
        input_paths[field] = path

    brief = read_json_object(input_paths["content_video_brief"], "content video brief")
    research = read_json_object(input_paths["research_output"], "research output")
    case_reference = _object(inputs.get("case_handoff_manifest"))
    if case_reference:
        case_path = validate_artifact_reference(
            case_reference,
            "case handoff manifest",
        )
        case_result = validate_case_handoff_manifest(
            case_path,
            str(case_reference.get("canonical_sha256") or ""),
            brief_path=input_paths["content_video_brief"],
            brief=brief,
            research=research,
        )
    else:
        case_result = validate_case_handoff_manifest(
            None,
            None,
            brief_path=input_paths["content_video_brief"],
            brief=brief,
            research=research,
        )
    if case_result is not None:
        if case_reference != case_result["artifact"]:
            raise ContractError("project plan case handoff artifact changed")
        if inputs.get("case_projection_sha256") != case_result["projection_sha256"]:
            raise ContractError("project plan case projection changed")
        if inputs.get("case_script_id") != case_result["script_id"]:
            raise ContractError("project plan case script id changed")
        if (
            plan.get("content_id") != brief.get("content_id")
            or plan.get("brief_id") != brief.get("brief_id")
            or plan.get("title") != brief.get("title")
            or plan.get("thesis") != brief.get("thesis")
            or plan.get("audience") != brief.get("audience")
            or plan.get("target_duration_s") != brief.get("target_duration_seconds")
            or plan.get("unknowns") != brief.get("unknowns")
            or plan.get("forbidden_expressions") != brief.get("forbidden_expressions")
            or plan.get("review_gate") != brief.get("review_gate")
        ):
            raise ContractError("project plan case brief projection changed")
        brief_scenes = {
            str(_object(scene).get("scene_id") or ""): _object(scene)
            for scene in brief.get("scenes") or []
        }
        plan_units = plan.get("shot_units")
        if (
            not isinstance(plan_units, list)
            or len(plan_units) != len(brief_scenes)
            or not brief_scenes
        ):
            raise ContractError("project plan case scene projection changed")
        for unit_value in plan_units:
            unit = _object(unit_value)
            source_scene = brief_scenes.get(str(unit.get("scene_id") or ""))
            if source_scene is None or (
                unit.get("purpose") != source_scene.get("purpose")
                or unit.get("narration") != source_scene.get("narration_draft")
                or unit.get("evidence_refs")
                != [
                    dict(row)
                    for row in source_scene.get("evidence_refs") or []
                    if isinstance(row, dict)
                ]
                or unit.get("visual_leads")
                != [
                    dict(row)
                    for row in source_scene.get("visual_leads") or []
                    if isinstance(row, dict)
                ]
            ):
                raise ContractError("project plan case scene projection changed")
    elif any(
        field in inputs
        for field in (
            "case_handoff_manifest",
            "case_projection_sha256",
            "case_script_id",
        )
    ):
        raise ContractError("project plan case handoff metadata is unexpected")
    shot_units = plan.get("shot_units")
    if not isinstance(shot_units, list) or not shot_units:
        raise ContractError("content video project plan requires shot units")
    for unit in shot_units:
        row = _object(unit)
        if row.get("schema") != CONTENT_SHOT_GENERATION_UNIT_SCHEMA:
            raise ContractError("content shot generation unit schema is invalid")
        if row.get("unit_sha256") != payload_sha256(row, "unit_sha256"):
            raise ContractError("content shot generation unit integrity check failed")
        if _object(row.get("material")).get("status") != "missing_material":
            raise ContractError("brief planning cannot invent a material artifact")
        if (
            row.get("status") != "candidate_only"
            or row.get("human_confirmed") is not False
        ):
            raise ContractError("content shot units must remain unconfirmed candidates")
    boundary = _object(plan.get("authority_boundary"))
    if (
        boundary.get("candidate_only") is not True
        or boundary.get("production_success_claimed") is not False
        or boundary.get("synthetic_fixture_is_production_material") is not False
        or boundary.get("mutates_vkp") is not False
        or boundary.get("external_provider_called") is not False
        or boundary.get("upload_performed") is not False
        or boundary.get("publish_performed") is not False
    ):
        raise ContractError("content project plan authority boundary is invalid")


def write_content_video_plan_bundle(
    plan: dict[str, Any],
    output_root: str | Path,
) -> dict[str, Path]:
    validate_content_video_project_plan(plan)
    root = Path(output_root).expanduser().resolve()
    _prepare_content_video_output_root(root, plan)
    paths: dict[str, Path] = {}
    paths["project_plan"] = atomic_write_json(plan, root / "project-plan.json")
    for unit in plan["shot_units"]:
        paths[f"shot_unit:{unit['scene_id']}"] = atomic_write_json(
            unit,
            root / "shot-units" / f"{unit['scene_id']}.json",
        )
    packaging = dict(plan["packaging_plan"])
    packaging["source_project_plan_sha256"] = plan["plan_sha256"]
    paths["packaging_plan"] = atomic_write_json(
        packaging,
        root / "render" / "video-packaging-plan.json",
    )
    edl = dict(plan["edit_decision_list"])
    edl["source_project_plan_sha256"] = plan["plan_sha256"]
    paths["edl"] = atomic_write_json(edl, root / "edit" / "edl.json")
    paths["subtitles"] = atomic_write_text(
        _build_srt(plan),
        root / "edit" / "subtitles.srt",
    )

    recipe = _reference_recipe(plan)
    normalized_recipe = normalize_reference_recipe(recipe)
    paths["reference_recipe"] = atomic_write_json(
        normalized_recipe,
        root / "audio" / "reference-recipe.json",
    )
    tts_config = _tts_config()
    paths["tts_config"] = atomic_write_json(
        tts_config,
        root / "audio" / "tts-config.json",
    )
    tts_job = build_tts_job(normalized_recipe, tts_config)
    paths["tts_job"] = write_tts_contract(
        tts_job,
        root / "audio" / "tts-job.json",
    )
    music_intent = build_music_intent_curve(
        paths["packaging_plan"],
        _music_segments(plan),
        created_by="content-video-brief-adapter",
    )
    paths["music_intent"] = write_music_contract(
        music_intent,
        root / "audio" / "music-intent-curve.json",
    )
    audio_intent: dict[str, Any] = {
        "schema": CONTENT_AUDIO_INTENT_SCHEMA,
        "tts_job": artifact_reference(paths["tts_job"], "TTS job"),
        "music_intent_curve": artifact_reference(
            paths["music_intent"], "music intent curve"
        ),
        "execution": {
            "tts_executed": False,
            "music_generated": False,
            "synthetic_fixture_audio_only": True,
        },
        "authority_boundary": {
            "external_provider_called": False,
            "automatic_fallback": False,
            "candidate_only": True,
        },
    }
    audio_intent["audio_intent_sha256"] = payload_sha256(
        audio_intent, "audio_intent_sha256"
    )
    paths["audio_intent"] = atomic_write_json(
        audio_intent,
        root / "audio" / "audio-intent.json",
    )
    paths["manifest"] = _write_plan_manifest(plan, paths, root)
    return paths


def build_plan_dry_run(plan: dict[str, Any], output_root: str | Path) -> dict[str, Any]:
    validate_content_video_project_plan(plan)
    root = Path(output_root).expanduser().resolve()
    return {
        "schema": "video_creation_pipeline.content_video_dry_run.v1",
        "status": "candidate_plan_ready"
        if not plan["readiness"]["blockers"]
        else "blocked",
        "source_plan_sha256": plan["plan_sha256"],
        "output_root": str(root),
        "would_write": [
            "project-plan.json",
            "shot-units/<scene_id>.json",
            "render/video-packaging-plan.json",
            "edit/edl.json",
            "edit/subtitles.srt",
            "audio/reference-recipe.json",
            "audio/tts-config.json",
            "audio/tts-job.json",
            "audio/music-intent-curve.json",
            "audio/audio-intent.json",
            "plan-manifest.json",
        ],
        "would_execute_ffmpeg": False,
        "would_call_provider": False,
        "would_upload": False,
        "would_publish": False,
        "blockers": list(plan["readiness"]["blockers"]),
        "case_handoff_verified": "case_handoff_manifest" in plan["inputs"],
    }


def _write_plan_manifest(
    plan: dict[str, Any],
    paths: dict[str, Path],
    root: Path,
) -> Path:
    artifacts = {
        key: artifact_reference(path, key)
        for key, path in sorted(paths.items())
        if key != "manifest"
    }
    manifest: dict[str, Any] = {
        "schema": CONTENT_VIDEO_PLAN_MANIFEST_SCHEMA,
        "source_plan_sha256": plan["plan_sha256"],
        "artifacts": artifacts,
        "status": "candidate_only",
        "production_render_eligible": False,
        "synthetic_fixture_render_eligible": plan["readiness"][
            "synthetic_fixture_render_eligible"
        ],
    }
    manifest["manifest_sha256"] = payload_sha256(manifest, "manifest_sha256")
    return atomic_write_json(manifest, root / "plan-manifest.json")


def _prepare_content_video_output_root(root: Path, plan: dict[str, Any]) -> None:
    marker_path = root / ".content-video-output-owner.json"
    if root.exists() and not root.is_dir():
        raise ContractError("content video output root is not a directory")
    if root.is_dir() and any(root.iterdir()):
        if not marker_path.is_file():
            raise ContractError("content video output root is non-empty and unmanaged")
        marker = read_json_object(marker_path, "content video output owner")
        if marker.get("schema") != CONTENT_VIDEO_OUTPUT_OWNER_SCHEMA or marker.get(
            "source_plan_sha256"
        ) != plan.get("plan_sha256"):
            raise ContractError("content video output root belongs to another plan")
    root.mkdir(parents=True, exist_ok=True)
    marker: dict[str, Any] = {
        "schema": CONTENT_VIDEO_OUTPUT_OWNER_SCHEMA,
        "source_plan_sha256": plan["plan_sha256"],
        "managed_outputs_only": True,
        "user_media_overwrite_allowed": False,
    }
    marker["owner_sha256"] = payload_sha256(marker, "owner_sha256")
    atomic_write_json(marker, marker_path)


def _reference_recipe(plan: dict[str, Any]) -> dict[str, Any]:
    shots = []
    for unit, scene in zip(
        plan["shot_units"],
        plan["packaging_plan"]["scenes"],
        strict=True,
    ):
        shots.append(
            {
                "shot_id": unit["scene_id"],
                "start_s": scene["start_s"],
                "end_s": scene["end_s"],
                "script": unit["narration"],
                "requirements": {
                    "scene": unit["visual_prompt"],
                    "action": unit["purpose"],
                    "shot_size": "unresolved",
                    "avoid": list(plan["forbidden_expressions"]),
                },
            }
        )
    return {
        "schema": REFERENCE_RECIPE_SCHEMA,
        "reference": {
            "kind": "content_video_brief",
            "brief_id": plan["brief_id"],
            "plan_sha256": plan["plan_sha256"],
        },
        "shots": shots,
    }


def _tts_config() -> dict[str, Any]:
    return {
        "schema": TTS_CONFIG_SCHEMA,
        "intended_use": "commercial",
        "provider": {
            "id": "offline-synthetic-fixture",
            "kind": "fixture_only",
            "model": "ffmpeg-tone-not-speech",
            "source_commit": "project-owned-fixture",
            "license_scope": "project-owned-test-fixture",
            "commercial_use_allowed": True,
        },
        "voice_profile": {
            "profile_id": "synthetic-fixture-voice",
            "speaker_embedding_ref": "",
            "seed": 20260805,
            "temperature": 0.0,
            "top_p": 1.0,
            "top_k": 1,
        },
        "chunk_policy": {
            "max_chars": 140,
            "max_tokens": 180,
            "max_estimated_duration_s": 18,
            "chars_per_second": 4.2,
            "retry_limit": 1,
        },
        "term_rules": [],
        "run_registry": {
            "reference": "external-run-registry-not-created-by-adapter",
            "authoritative": True,
        },
    }


def _music_segments(plan: dict[str, Any]) -> list[dict[str, Any]]:
    evidence_ids = sorted(
        {
            f"{row['ref_type']}:{row['ref_id']}"
            for scene in plan["packaging_plan"]["scenes"]
            for row in scene["evidence_refs"]
        }
    )
    return [
        {
            "segment_id": "final-program",
            "start_s": 0.0,
            "end_s": float(plan["target_duration_s"]),
            "relationship_mode": "neutral_support",
            "text_query": "克制、清晰、无人声的中性背景音乐",
            "text_query_en": "restrained clear neutral instrumental background",
            "target": {
                "valence": 0.55,
                "arousal": 0.30,
                "tension": 0.20,
                "intimacy_scale": 0.40,
                "warmth": 0.55,
                "movement": 0.25,
                "engagement": 0.20,
            },
            "vocal_policy": "instrumental_required",
            "narration_density": 0.8,
            "prohibited_tags": ["vocal", "aggressive"],
            "evidence_ids": evidence_ids,
        }
    ]


def _build_srt(plan: dict[str, Any]) -> str:
    blocks = []
    for index, (unit, scene) in enumerate(
        zip(plan["shot_units"], plan["packaging_plan"]["scenes"], strict=True),
        1,
    ):
        candidates = scene.get("subtitle_candidates") or []
        text = str(candidates[0] if candidates else unit["narration"]).strip()
        blocks.append(
            f"{index}\n{_srt_time(scene['start_s'])} --> {_srt_time(scene['end_s'])}\n{text}\n"
        )
    return "\n".join(blocks)


def _allocate_scene_timings(
    scenes: list[dict[str, Any]],
    target_duration_s: float,
) -> list[dict[str, float]]:
    if target_duration_s <= 0 or not math.isfinite(target_duration_s):
        raise ContractError("target video duration must be positive")
    weights = [max(1, len(str(row.get("narration_draft") or ""))) for row in scenes]
    total_weight = sum(weights)
    cursor_ms = 0
    total_ms = int(round(target_duration_s * 1000))
    rows = []
    for index, weight in enumerate(weights):
        end_ms = (
            total_ms
            if index == len(weights) - 1
            else int(round(total_ms * sum(weights[: index + 1]) / total_weight))
        )
        if end_ms <= cursor_ms:
            raise ContractError("target duration is too short for the scene count")
        rows.append(
            {
                "start_s": round(cursor_ms / 1000, 3),
                "end_s": round(end_ms / 1000, 3),
                "duration_s": round((end_ms - cursor_ms) / 1000, 3),
            }
        )
        cursor_ms = end_ms
    return rows


def _validate_schema_instance(
    payload: dict[str, Any],
    schema: dict[str, Any],
    label: str,
) -> None:
    try:
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(
            schema,
            format_checker=FormatChecker(),
        ).validate(payload)
    except (SchemaError, ValidationError) as exc:
        raise ContractError(f"{label} schema validation failed: {exc.message}") from exc


def _validate_content_freshness_policy(policy: dict[str, Any]) -> None:
    if policy.get("schema") != "video_creation_pipeline.content_freshness_policy.v1":
        raise ContractError("content freshness policy schema is invalid")
    sensitivity = normalize_content_time_sensitivity(
        str(policy.get("time_sensitivity") or "")
    )
    expected_gate = sensitivity == "high_temporal"
    if (
        policy.get("age_gate_applied") is not expected_gate
        or policy.get("source_freshness_gate_applied") is not expected_gate
        or policy.get("future_timestamp_gate_applied") is not True
    ):
        raise ContractError("content freshness policy gates are inconsistent")
    max_age_hours = policy.get("max_age_hours")
    if expected_gate:
        if not isinstance(max_age_hours, (int, float)) or max_age_hours <= 0:
            raise ContractError(
                "high-temporal freshness policy requires a positive age"
            )
    elif max_age_hours is not None:
        raise ContractError("non-temporal freshness policy cannot apply an age limit")


def _evaluate_artifact_age(
    path: Path,
    created_at: Any,
    max_age_hours: float,
    label: str,
    time_sensitivity: str,
) -> dict[str, Any]:
    sensitivity = normalize_content_time_sensitivity(time_sensitivity)
    age_gate_applied = sensitivity == "high_temporal"
    if age_gate_applied and max_age_hours <= 0:
        raise ContractError("max_age_hours must be greater than zero")
    now = datetime.now(timezone.utc)
    modified_age = (now.timestamp() - path.stat().st_mtime) / 3600
    created = _parse_datetime(created_at, f"{label} created_at")
    created_age = (now - created).total_seconds() / 3600
    if modified_age < -5 / 60 or created_age < -5 / 60:
        raise ContractError(f"{label} timestamp is in the future")
    if age_gate_applied and (
        modified_age > max_age_hours or created_age > max_age_hours
    ):
        raise ContractError(f"{label} failed the artifact freshness gate")
    return {
        "artifact_age_hours": round(max(0.0, modified_age), 6),
        "created_age_hours": round(max(0.0, created_age), 6),
        "age_gate_applied": age_gate_applied,
        "max_age_hours": float(max_age_hours) if age_gate_applied else None,
        "future_timestamp_check": "passed",
        "decision": "within_age_limit" if age_gate_applied else "age_not_required",
    }


def _validate_evidence_references(
    brief: dict[str, Any],
    research: dict[str, Any],
) -> set[str]:
    ids = {
        "claim": {str(row.get("claim_id")) for row in research.get("claims") or []},
        "evidence": {
            str(row.get("evidence_id")) for row in research.get("evidence") or []
        },
        "source": {str(row.get("source_id")) for row in research.get("sources") or []},
        "unknown": {str(row.get("code")) for row in research.get("unknowns") or []},
    }
    contradictions = [_object(row) for row in research.get("contradictions") or []]
    ids["contradiction"] = {
        value
        for row in contradictions
        for value in (
            str(row.get("claim_id") or ""),
            str(row.get("evidence_id") or ""),
            ":".join(
                str(row.get(key) or "")
                for key in ("claim_id", "evidence_id", "source_id")
            ),
        )
        if value
    }
    referenced_sources: set[str] = set()
    claim_map = {
        str(row.get("claim_id")): _object(row) for row in research.get("claims") or []
    }
    evidence_map = {
        str(row.get("evidence_id")): _object(row)
        for row in research.get("evidence") or []
    }
    for container in [*brief.get("chapters", []), *brief.get("scenes", [])]:
        for reference in _object(container).get("evidence_refs") or []:
            row = _object(reference)
            kind = str(row.get("ref_type") or "")
            ref_id = str(row.get("ref_id") or "")
            if ref_id not in ids.get(kind, set()):
                raise ContractError(
                    f"brief evidence reference is unresolved: {kind}:{ref_id}"
                )
            if kind == "source":
                referenced_sources.add(ref_id)
            elif kind == "claim":
                referenced_sources.update(
                    claim_map.get(ref_id, {}).get("source_ids") or []
                )
            elif kind == "evidence":
                source_id = str(evidence_map.get(ref_id, {}).get("source_id") or "")
                if source_id:
                    referenced_sources.add(source_id)
        for lead in _object(container).get("visual_leads") or []:
            for reference in _object(lead).get("evidence_refs") or []:
                row = _object(reference)
                kind = str(row.get("ref_type") or "")
                ref_id = str(row.get("ref_id") or "")
                if ref_id not in ids.get(kind, set()):
                    raise ContractError(
                        f"brief visual evidence reference is unresolved: {kind}:{ref_id}"
                    )
                if kind == "source":
                    referenced_sources.add(ref_id)
                elif kind == "claim":
                    referenced_sources.update(
                        claim_map.get(ref_id, {}).get("source_ids") or []
                    )
                elif kind == "evidence":
                    source_id = str(evidence_map.get(ref_id, {}).get("source_id") or "")
                    if source_id:
                        referenced_sources.add(source_id)
    return referenced_sources


def _validate_referenced_sources(
    research: dict[str, Any],
    referenced_source_ids: set[str],
    time_sensitivity: str,
) -> list[dict[str, str]]:
    sensitivity = normalize_content_time_sensitivity(time_sensitivity)
    sources = {
        str(row.get("source_id")): _object(row) for row in research.get("sources") or []
    }
    freshness_rows: list[dict[str, str]] = []
    for source_id in sorted(referenced_source_ids):
        source = sources.get(source_id)
        if source is None:
            raise ContractError(f"referenced research source is missing: {source_id}")
        freshness = str(source.get("freshness") or "unknown")
        if sensitivity == "high_temporal" and freshness in {"stale", "unknown"}:
            raise ContractError(
                "high-temporal referenced research source is not current: "
                f"{source_id} ({freshness})"
            )
        source_hash = _object(source.get("hash"))
        if source_hash.get("status") != "verified" or not source_hash.get("value"):
            raise ContractError(
                f"referenced research source hash is not verified: {source_id}"
            )
        freshness_rows.append(
            {
                "source_id": source_id,
                "declared_freshness": freshness,
                "decision": (
                    "current_or_not_applicable"
                    if sensitivity == "high_temporal"
                    else "recorded_not_gated"
                ),
            }
        )
    evidence = {
        str(row.get("evidence_id")): _object(row)
        for row in research.get("evidence") or []
    }
    for item in evidence.values():
        if (
            item.get("source_id") in referenced_source_ids
            and item.get("ai_synthesis") is True
        ):
            raise ContractError("AI synthesis cannot become scene evidence")
    return freshness_rows


def _input_blockers(brief: dict[str, Any], research: dict[str, Any]) -> list[str]:
    blockers = []
    review_gate = _object(brief.get("review_gate"))
    if review_gate.get("status") == "blocked":
        blockers.append("content_video_brief_review_blocked")
    if any(
        _object(row).get("resolution_status") == "unresolved"
        for row in research.get("contradictions") or []
    ):
        blockers.append("research_has_unresolved_contradictions")
    if any(
        _object(row).get("severity") == "blocker"
        for row in research.get("unknowns") or []
    ):
        blockers.append("research_has_blocker_unknowns")
    return sorted(set(blockers))


def _srt_time(seconds: float) -> str:
    millis = int(round(float(seconds) * 1000))
    hours = millis // 3_600_000
    millis %= 3_600_000
    minutes = millis // 60_000
    millis %= 60_000
    whole_seconds = millis // 1000
    remainder = millis % 1000
    return f"{hours:02}:{minutes:02}:{whole_seconds:02},{remainder:03}"


def _parse_datetime(value: Any, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"{label} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ContractError(f"{label} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}
