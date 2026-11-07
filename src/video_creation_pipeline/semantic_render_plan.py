"""Conservative editorial-to-renderer glue, with no execution entry point."""

from __future__ import annotations

from pathlib import Path

from .contract_io import (
    artifact_reference,
    payload_sha256,
    read_json_object,
    validate_artifact_reference,
)
from .creative_gates import validate_approval_record
from .errors import ContractError
from .remotion_project import build_remotion_timeline_preview
from .semantic_editorial import (
    PLAN_SCHEMA,
    build_editorial_packaging_projection,
    validate_editorial_artifact,
    write_editorial_artifact,
)

SCHEMA = "video_creation_pipeline.semantic_render_plan.v1"
BOUNDARY = {
    "candidate_only": True,
    "execution_mode": "plan_only",
    "render_execution_performed": False,
    "source_media_copied": False,
    "source_timeline_modified": False,
    "edit_proposals_applied": False,
    "avatar_enabled": False,
    "provider_called": False,
    "upload": False,
    "publish": False,
    "automatic_fallback": False,
}


def _approval(path: str | Path, target_path: str | Path, target: str) -> dict:
    value = read_json_object(path, "approval")
    validate_approval_record(value)
    if value["target"] != target or value["artifact"] != artifact_reference(
        target_path, target
    ):
        raise ContractError("approval must bind the exact target artifact")
    return artifact_reference(path, "approval")


def build_semantic_render_plan(
    plan_path: str | Path,
    packaging_path: str | Path,
    *,
    composition_id: str = "EditorialCandidate",
    approval_path: str | Path | None = None,
    safe_media_scene_plan_path: str | Path | None = None,
    materials_approval_path: str | Path | None = None,
    visual_beats_path: str | Path | None = None,
) -> dict:
    plan = read_json_object(plan_path, "editorial plan")
    validate_editorial_artifact(plan)
    if plan["schema"] != PLAN_SCHEMA:
        raise ContractError("semantic editorial plan required")
    packaging = read_json_object(packaging_path, "packaging")
    output = packaging.get("output", {})
    expected = build_editorial_packaging_projection(
        plan_path,
        width=output.get("width", 0),
        height=output.get("height", 0),
        fps=output.get("fps", 0),
    )
    if packaging != expected:
        raise ContractError("packaging drift from original semantic timebase")
    inputs = {
        "plan": artifact_reference(plan_path, "editorial plan"),
        "packaging": artifact_reference(packaging_path, "packaging"),
    }
    if approval_path is not None:
        inputs["approval"] = _approval(approval_path, plan_path, "storyboard")
    if safe_media_scene_plan_path is not None:
        inputs["safe_media_scene_plan"] = artifact_reference(
            safe_media_scene_plan_path, "scene plan"
        )
    if materials_approval_path is not None:
        if safe_media_scene_plan_path is None:
            raise ContractError("materials approval requires the scene plan")
        inputs["materials_approval"] = _approval(
            materials_approval_path, safe_media_scene_plan_path, "materials"
        )

    # This is the same converter used by materialize_remotion_project, not a mock.
    renderer_data = build_remotion_timeline_preview(
        packaging_path,
        composition_id=composition_id,
        safe_media_scene_plan_path=safe_media_scene_plan_path,
    )
    reviews = []
    visual_blockers = []
    if visual_beats_path is not None:
        beats = read_json_object(visual_beats_path, 'visual beats')
        validate_editorial_artifact(beats)
        if beats.get('schema') != 'video_creation_pipeline.editorial_visual_beats.v1':
            raise ContractError('visual beats artifact required')
        expected_times = {s['scene_id']:(s['start_s'],s['end_s']) for s in packaging['scenes']}
        if {b['scene_id']:(b['start_s'],b['end_s']) for b in beats['beats']} != expected_times:
            raise ContractError('visual beats belong to another timeline')
        routing_path = validate_artifact_reference(beats['inputs']['routing'], 'visual routing')
        routing = read_json_object(routing_path, 'visual routing')
        if routing['inputs']['packaging_plan'] != artifact_reference(packaging_path, 'packaging'):
            raise ContractError('visual beats packaging binding drift')
        inputs['visual_beats'] = artifact_reference(visual_beats_path, 'visual beats')
        if any(b['status'] == 'review_required' for b in beats['beats']):
            visual_blockers.append('visual_semantics_review_required')
        # This converter only performs straight cuts. Never claim an unsupported
        # transition or emphasis animation was rendered from metadata alone.
        if any(b['transition']['kind'] != 'cut' or b['emphasis_events'] for b in beats['beats']):
            visual_blockers.append('visual_effect_renderer_binding_required')
    timeline = []
    last_end = 0
    for decision, scene in zip(plan["segments"], renderer_data["scenes"], strict=True):
        interval = decision["source_interval_ms"]
        if scene["sceneId"] != decision["segment_id"]:
            raise ContractError("renderer scene identity drift")
        if scene["startFrame"] < last_end:
            raise ContractError("renderer frame quantization overlaps scenes")
        last_end = scene["startFrame"] + scene["durationInFrames"]
        pending = decision["effective_action"] != "keep"
        if pending:
            reviews.append(
                {
                    "segment_id": decision["segment_id"],
                    "proposed_action": decision["action"],
                    "effective_action": decision["effective_action"],
                    "reasons": decision["review_reasons"]
                    or ["edit_proposal_requires_explicit_resolved_timebase"],
                }
            )
        timeline.append(
            {
                "segment_id": decision["segment_id"],
                "source_interval_ms": dict(interval),
                "output_interval_ms": dict(interval),
                "evidence_refs": list(decision["evidence_refs"]),
                "action_applied": "keep",
                "review_pending": pending,
                "source_media_sha256": plan["source_media_sha256"],
                "renderer_start_frame": scene["startFrame"],
                "renderer_duration_frames": scene["durationInFrames"],
            }
        )
    missing = [
        scene["sceneId"]
        for scene in renderer_data["scenes"]
        if scene.get("mediaLayout") is None
    ]
    blockers = list(visual_blockers)
    if reviews:
        blockers.append("editorial_resolution_required")
    if approval_path is None:
        blockers.append("storyboard_approval_required")
    if missing:
        blockers.append("missing_material")
    if safe_media_scene_plan_path is not None and materials_approval_path is None:
        blockers.append("materials_approval_required")
    result = {
        "schema": SCHEMA,
        "inputs": inputs,
        "composition_id": composition_id,
        "source_identity_sha256": plan["source_identity_sha256"],
        "timestamp_basis": plan["timestamp_basis"],
        "adapter_offset_applied_ms": 0,
        "timeline": timeline,
        "review_queue": reviews,
        "missing_material_scene_ids": missing,
        "renderer": {
            "entrypoint": "video_creation_pipeline.remotion_project.build_remotion_timeline_preview",
            "production_converter": "video_creation_pipeline.remotion_project._project_data",
            "data": renderer_data,
            "data_sha256": payload_sha256(renderer_data, "artifact_sha256"),
            "mode": "contract_preview_without_source_media",
        },
        "readiness": {
            "status": "pending_resolution_or_approval"
            if blockers
            else "approved_plan_only",
            "blockers": blockers,
            "execution_admission_required": True,
            "render_eligible": False,
            "real_video_produced": False,
            "technical_qa_passed": False,
        },
        "boundary": dict(BOUNDARY),
    }
    result["artifact_sha256"] = payload_sha256(result, "artifact_sha256")
    return result


def validate_semantic_render_plan(value: dict) -> None:
    if value.get("schema") != SCHEMA or value.get("artifact_sha256") != payload_sha256(
        value, "artifact_sha256"
    ):
        raise ContractError("semantic render plan integrity drift")
    inputs = value.get("inputs", {})
    if not {"plan", "packaging"}.issubset(inputs) or set(inputs) - {
        "plan",
        "packaging",
        "approval",
        "safe_media_scene_plan",
        "materials_approval",
        "visual_beats",
    }:
        raise ContractError("semantic render input contract drift")
    paths = {key: validate_artifact_reference(ref, key) for key, ref in inputs.items()}
    expected = build_semantic_render_plan(
        paths["plan"],
        paths["packaging"],
        composition_id=value["composition_id"],
        approval_path=paths.get("approval"),
        safe_media_scene_plan_path=paths.get("safe_media_scene_plan"),
        materials_approval_path=paths.get("materials_approval"),
        visual_beats_path=paths.get("visual_beats"),
    )
    if value != expected:
        raise ContractError("semantic render projection or authority drift")


def write_semantic_render_plan(value: dict, output_path: str | Path) -> Path:
    validate_semantic_render_plan(value)
    return write_editorial_artifact(value, output_path)
