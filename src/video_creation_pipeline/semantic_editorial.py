"""Candidate-only editorial projections over existing VKP and MVP contracts.

Timebase identity projection adapts VKP narration_timebase._artifact_identities;
all persistence, approval, rendering routes and preference thresholds are reused.
"""

from __future__ import annotations

from copy import deepcopy
from graphlib import CycleError, TopologicalSorter
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, ValidationError

from .contract_io import (
    artifact_reference,
    atomic_write_json,
    canonical_json_sha256,
    file_sha256,
    payload_sha256,
    read_json_object,
    validate_artifact_reference,
)
from .creative_gates import validate_approval_record
from .errors import ContractError
from .visual_edit_presets import visual_defaults
from .visual_routing import validate_content_visual_routing_plan
from .vkp_bridge import build_preference_candidates

PLAN_SCHEMA = "video_creation_pipeline.semantic_editorial_plan.v1"
BEAT_SCHEMA = "video_creation_pipeline.editorial_visual_beats.v1"
FEEDBACK_SCHEMA = "video_creation_pipeline.editorial_feedback_receipt.v1"
BOUNDARY = {
    "candidate_only": True,
    "review_required": True,
    "execution_mode": "manual_only",
    "execute": False,
    "upload": False,
    "publish": False,
    "automatic_fallback": False,
}
SCHEMAS = Path(__file__).with_name("schemas")


def _validate(value: dict, definition: str) -> None:
    schema = read_json_object(
        SCHEMAS / "semantic_editorial_inputs.v1.schema.json", "schema"
    )
    selected = {"$ref": f"#/$defs/{definition}", "$defs": schema["$defs"]}
    try:
        Draft202012Validator(selected).validate(value)
    except ValidationError as exc:
        raise ContractError(f"{definition}: {exc.message}") from exc


def _seal(value: dict) -> dict:
    value["boundary"] = dict(BOUNDARY)
    value["artifact_sha256"] = payload_sha256(value, "artifact_sha256")
    return value


def load_narration_timebase(path: str | Path, bundle_root: str | Path) -> dict:
    """Verify the published v1 contract and its locally resolved source refs."""
    value = read_json_object(path, "VKP narration timebase")
    schema = read_json_object(
        SCHEMAS / "vkp_narration_timebase.v1.schema.json", "VKP schema"
    )
    try:
        Draft202012Validator(schema).validate(value)
    except ValidationError as exc:
        raise ContractError(f"VKP timebase schema: {exc.message}") from exc
    if value["contract_sha256"] != payload_sha256(value, "contract_sha256"):
        raise ContractError("VKP timebase hash drift")
    if (
        value["status"] != "complete_draft"
        or value["downstream_ready"] is not True
        or value["review_gaps"]
    ):
        raise ContractError("VKP timebase is not ready")
    authority = value["downstream_time_authority"]
    if (
        authority.get("mvp_handoff_must_preserve_absolute_times") is not True
        or authority.get("renderer_or_music_may_rewrite_semantic_boundaries")
        is not False
    ):
        raise ContractError("VKP time authority is unsafe")
    if (
        value["upstream_version_and_invalidation"].get(
            "invalidate_on_any_artifact_hash_change"
        )
        is not True
    ):
        raise ContractError("VKP invalidation policy is missing")
    operator = value["operator_boundary"]
    if (
        operator.get("local_only") is not True
        or operator.get("human_review_required") is not True
    ):
        raise ContractError("VKP operator boundary is missing")
    for key in (
        "provider_called",
        "uploaded",
        "published",
        "timeline_modified",
        "chunk_offsets_applied_by_adapter",
    ):
        if operator.get(key) is not False:
            raise ContractError(f"unsafe VKP operator boundary: {key}")
    root = Path(bundle_root).resolve()
    if not {"manifest", "timeline", "media", "transcript"}.issubset(value["artifacts"]):
        raise ContractError("VKP required source references are missing")
    if any(
        not isinstance(value["artifacts"][key], dict)
        for key in ("manifest", "timeline", "media", "transcript")
    ):
        raise ContractError("VKP required source reference is null or invalid")
    identities = {}
    for key, raw in value["artifacts"].items():
        if raw is None:
            continue
        refs = raw if isinstance(raw, list) else [raw]
        identity_rows = []
        for ref in refs:
            if not isinstance(ref, dict) or ref.get("scope") != "bundle_relative":
                raise ContractError(
                    "external VKP references require a separate explicit resolver"
                )
            relative = Path(str(ref.get("path", "")))
            if relative.is_absolute() or ".." in relative.parts or not relative.parts:
                raise ContractError("unsafe VKP artifact path")
            resolved = (root / relative).resolve()
            if not resolved.is_relative_to(root) or not resolved.is_file():
                raise ContractError("VKP artifact escaped or is missing")
            if resolved.stat().st_size != ref.get("bytes") or file_sha256(
                resolved
            ) != ref.get("sha256"):
                raise ContractError("VKP source artifact hash drift")
            identity_rows.append({"sha256": ref["sha256"], "bytes": ref["bytes"]})
        identities[key] = identity_rows if isinstance(raw, list) else identity_rows[0]
    # Preserve the upstream identity recipe, not its Git/package identity.
    identity = canonical_json_sha256(
        {
            "artifacts": identities,
            "timestamp_policy": {
                "basis": "absolute_media_milliseconds",
                "origin": "media_start",
                "tolerance_ms": value["timestamp_basis"]["tolerance_ms"],
            },
        }
    )
    if identity != value["source_identity_sha256"]:
        raise ContractError("VKP source identity drift")
    for key in ("sentence_segments", "semantic_segments", "chapters"):
        ids = set()
        previous_end = 0
        for row in value[key]:
            if (
                row["segment_id"] in ids
                or not 0 <= row["start_ms"] < row["end_ms"] <= value["duration_ms"]
            ):
                raise ContractError("VKP segment identity or time is invalid")
            if (
                row["start_ms"]
                < previous_end - value["timestamp_basis"]["tolerance_ms"]
            ):
                raise ContractError("VKP segments overlap or are out of order")
            ids.add(row["segment_id"])
            previous_end = row["end_ms"]
    if not value["semantic_segments"]:
        raise ContractError("VKP semantic segments are empty")
    return value


def build_semantic_editorial_plan(
    timebase_path: str | Path, bundle_root: str | Path, spec_path: str | Path
) -> dict:
    source = load_narration_timebase(timebase_path, bundle_root)
    spec = read_json_object(spec_path, "editorial spec")
    _validate(spec, "editorial")
    segments = {row["segment_id"]: row for row in source["semantic_segments"]}
    decisions = {row["segment_id"]: row for row in spec["decisions"]}
    if len(decisions) != len(spec["decisions"]) or set(decisions) != set(segments):
        raise ContractError(
            "editorial decisions must cover every source segment exactly once"
        )
    known_evidence = {f"semantic:{key}" for key in segments}
    known_evidence.update(
        f"sentence:{row['segment_id']}" for row in source["sentence_segments"]
    )
    groups: dict[str, list[dict]] = {}
    for row in decisions.values():
        if row["redundancy_group"]:
            groups.setdefault(row["redundancy_group"], []).append(row)
    for members in groups.values():
        winners = {
            row.get("selected_segment_id")
            for row in members
            if row["action"] == "select_best_example"
        }
        if len(winners) > 1:
            raise ContractError("example group has conflicting selections")
    dependencies = {key: set() for key in segments}
    for row in decisions.values():
        if row["action"] == "move":
            dependencies[row["segment_id"]].add(row.get("after_segment_id", ""))
    try:
        tuple(TopologicalSorter(dependencies).static_order())
    except CycleError as exc:
        raise ContractError("editorial move cycle") from exc
    projected = []
    for segment_id, segment in segments.items():
        row = deepcopy(decisions[segment_id])
        if any(ref not in known_evidence for ref in row["evidence_refs"]):
            raise ContractError("editorial decision references unknown evidence")
        reasons = []
        action = row["action"]
        if action != "keep" and not row["evidence_refs"]:
            reasons.append("missing_evidence")
        if action not in {"keep", "review_required"} and row["confidence"] < 0.7:
            reasons.append("low_confidence")
        if segment.get("human_boundary_confirmed") and action != "keep":
            reasons.append("human_locked_source_boundary")
        if action in {"delete", "compress"} and row["rhetorical_role"] in {
            "claim",
            "evidence",
            "counterpoint",
        }:
            reasons.append("fact_or_qualification_requires_review")
        members = groups.get(row["redundancy_group"], [])
        if (
            action in {"delete", "select_best_example"}
            and len({r["rhetorical_role"] for r in members}) > 1
        ):
            reasons.append("different_rhetorical_roles_are_not_duplicates")
        if action == "select_best_example":
            selected = row.get("selected_segment_id")
            if (
                len(members) < 2
                or selected not in {r["segment_id"] for r in members}
                or any(r["rhetorical_role"] != "example" for r in members)
            ):
                raise ContractError(
                    "select_best_example requires an explicit same-role group member"
                )
            if decisions[selected]["action"] not in {"keep", "select_best_example"}:
                raise ContractError("selected example cannot also be removed or moved")
        if action == "move" and (
            row.get("after_segment_id") not in segments
            or row["after_segment_id"] == segment_id
        ):
            raise ContractError("move requires another known source segment")
        projected.append(
            {
                **row,
                "source_interval_ms": {
                    "start": segment["start_ms"],
                    "end": segment["end_ms"],
                },
                "effective_action": "review_required" if reasons else action,
                "review_reasons": reasons,
                "human_review_required": True,
            }
        )
    return _seal(
        {
            "schema": PLAN_SCHEMA,
            "inputs": {
                "timebase": artifact_reference(timebase_path, "timebase"),
                "spec": artifact_reference(spec_path, "editorial spec"),
            },
            "bundle_root": str(Path(bundle_root).resolve()),
            "source_identity_sha256": source["source_identity_sha256"],
            "source_media_sha256": source["artifacts"]["media"]["sha256"],
            "timestamp_basis": source["timestamp_basis"],
            "original_order": list(segments),
            "segments": projected,
            "technical_cleanup": "existing_spoken_rough_cut_and_videocut_kit",
            "approval_target": "storyboard",
            "source_timeline_mutated": False,
        }
    )


def validate_editorial_artifact(value: dict) -> None:
    if value.get("boundary") != BOUNDARY or value.get(
        "artifact_sha256"
    ) != payload_sha256(value, "artifact_sha256"):
        raise ContractError("editorial artifact integrity or authority drift")
    inputs = value.get("inputs", {})
    paths = {key: validate_artifact_reference(ref, key) for key, ref in inputs.items()}
    schema = value.get("schema")
    if schema == PLAN_SCHEMA:
        expected = build_semantic_editorial_plan(
            paths["timebase"], value["bundle_root"], paths["spec"]
        )
    elif schema == BEAT_SCHEMA:
        expected = build_editorial_visual_beats(
            paths["routing"], paths["spec"], paths.get("screen_profile")
        )
    elif schema == FEEDBACK_SCHEMA:
        expected = build_editorial_feedback(
            paths["plan"], paths["feedback"], paths["approval"]
        )
    elif schema == "video_creation_pipeline.semantic_editorial_handoff.v1":
        expected = editorial_review_handoff(paths["plan"], paths["approval"])
    else:
        raise ContractError("unsupported editorial artifact")
    if expected != value:
        raise ContractError("editorial artifact projection drift")


def editorial_review_handoff(plan_path: str | Path, approval_path: str | Path) -> dict:
    plan = read_json_object(plan_path, "editorial plan")
    validate_editorial_artifact(plan)
    if plan["schema"] != PLAN_SCHEMA:
        raise ContractError("semantic editorial plan required")
    _require_approval(approval_path, plan_path)
    if any(row["effective_action"] == "review_required" for row in plan["segments"]):
        raise ContractError("unresolved editorial decisions cannot enter a handoff")
    return _seal(
        {
            "schema": "video_creation_pipeline.semantic_editorial_handoff.v1",
            "inputs": {
                "plan": artifact_reference(plan_path, "plan"),
                "approval": artifact_reference(approval_path, "approval"),
            },
            "segments": plan["segments"],
            "time_projection": "original_absolute_media_milliseconds",
            "consumer": "existing_spoken_rough_cut_and_visual_routing",
            "execution_status": "manual_edit_projection_required",
            "render_consumer": "existing_single_ffmpeg_outlet",
        }
    )


def build_editorial_packaging_projection(
    plan_path: str | Path, *, width: int = 720, height: int = 1280, fps: float = 30
) -> dict:
    """Feed existing routing without applying proposed deletes or moved times."""
    plan = read_json_object(plan_path, "editorial plan")
    validate_editorial_artifact(plan)
    if plan["schema"] != PLAN_SCHEMA or width <= 0 or height <= 0 or not 0 < fps <= 120:
        raise ContractError("invalid editorial packaging input")
    return {
        "schema": "video_creation_pipeline.video_packaging_plan.v1",
        "status": "candidate_only",
        "source_editorial_plan": artifact_reference(plan_path, "editorial plan"),
        "output": {"width": width, "height": height, "fps": fps},
        "timing_authority": "original_VKP_absolute_media_time_proposal_not_applied",
        "scenes": [
            {
                "scene_id": row["segment_id"],
                "start_s": row["source_interval_ms"]["start"] / 1000,
                "end_s": row["source_interval_ms"]["end"] / 1000,
                "evidence_refs": row["evidence_refs"],
                "material_status": "missing_material",
            }
            for row in plan["segments"]
        ],
    }


def _require_approval(approval_path: str | Path, target_path: str | Path) -> dict:
    approval = read_json_object(approval_path, "approval")
    validate_approval_record(approval)
    if approval["target"] != "storyboard" or approval["artifact"] != artifact_reference(
        target_path, "approved artifact"
    ):
        raise ContractError("approval must bind this exact storyboard artifact")
    return approval


def validate_screen_recording_profile(profile: dict) -> None:
    _validate(profile, "screen")
    if (
        not all(profile["privacy_preflight"].values())
        or profile["speaker_notes_visible"]
    ):
        raise ContractError("screen recording privacy preflight is incomplete")
    if not profile["safe_zone_ids"] or len(set(profile["safe_zone_ids"])) != len(
        profile["safe_zone_ids"]
    ):
        raise ContractError("screen recording safe zones are missing or duplicated")
    previous = -1
    for event in profile["focus_events"]:
        if (
            event["start_ms"] < previous
            or event["end_ms"] <= event["start_ms"]
            or event["safe_zone_id"] not in profile["safe_zone_ids"]
        ):
            raise ContractError("screen focus time or safe zone is invalid")
        previous = event["start_ms"]


def build_editorial_visual_beats(
    routing_path: str | Path,
    spec_path: str | Path,
    screen_profile_path: str | Path | None = None,
) -> dict:
    routing = read_json_object(routing_path, "visual routing")
    validate_content_visual_routing_plan(routing)
    spec = read_json_object(spec_path, "visual beat spec")
    _validate(spec, "visual")
    scenes = {row["scene_id"]: row for row in routing["scene_routes"]}
    rows = {row["scene_id"]: row for row in spec["scenes"]}
    if len(rows) != len(spec["scenes"]) or set(rows) != set(scenes):
        raise ContractError("visual beats must cover every routed scene exactly once")
    packaging = read_json_object(
        validate_artifact_reference(routing["inputs"]["packaging_plan"], "packaging"),
        "packaging",
    )
    if "source_editorial_plan" in packaging:
        editor_path = validate_artifact_reference(
            packaging["source_editorial_plan"], "source editorial plan"
        )
        expected_scenes = build_editorial_packaging_projection(editor_path)["scenes"]
        actual_timing = [
            (s["scene_id"], s["start_s"], s["end_s"], s.get("evidence_refs", []))
            for s in packaging["scenes"]
        ]
        expected_timing = [
            (s["scene_id"], s["start_s"], s["end_s"], s["evidence_refs"])
            for s in expected_scenes
        ]
        if actual_timing != expected_timing:
            raise ContractError("packaging drifted from VKP editorial time or evidence")
    evidence = {r["scene_id"]: r.get("evidence_refs", []) for r in packaging["scenes"]}
    inputs = {
        "routing": artifact_reference(routing_path, "routing"),
        "spec": artifact_reference(spec_path, "visual spec"),
    }
    profile = None
    if screen_profile_path:
        profile = read_json_object(screen_profile_path, "screen profile")
        validate_screen_recording_profile(profile)
        inputs["screen_profile"] = artifact_reference(
            screen_profile_path, "screen profile"
        )
        screen_intervals = [
            (
                round(scenes[key]["start_s"] * 1000),
                round(scenes[key]["end_s"] * 1000),
                row["source_or_evidence_refs"],
            )
            for key, row in rows.items()
            if row["visual_role"] == "screen_demo"
        ]
        for event in profile["focus_events"]:
            if not any(
                start <= event["start_ms"] < event["end_ms"] <= end
                and event["target_ref"] in refs
                for start, end, refs in screen_intervals
            ):
                raise ContractError(
                    "screen focus event is outside its scene time or evidence"
                )
    beats = []
    for scene_id, scene in scenes.items():
        row = deepcopy(rows[scene_id])
        defaults=visual_defaults(row,scene['start_s'],scene['end_s'])
        if any(ref not in evidence[scene_id] for ref in row["source_or_evidence_refs"]):
            raise ContractError("visual beat evidence does not belong to its scene")
        reasons = []
        if (
            not row["information_advance"].strip()
            or not row["semantic_trigger"].strip()
        ):
            reasons.append("decoration_without_information_advance")
        if (
            row["visual_role"] == "source_evidence"
            and not row["source_or_evidence_refs"]
        ):
            reasons.append("source_evidence_missing")
        if (
            row["visual_role"] == "source_evidence"
            and scene["renderer"] != "hyperframes"
        ):
            reasons.append("source_evidence_route_requires_review")
        if (
            row["visual_role"] == "knowledge_explanation"
            and scene["renderer"] != "remotion"
        ):
            reasons.append("knowledge_route_requires_review")
        if row["visual_role"] == "screen_demo":
            if profile is None:
                reasons.append("screen_profile_required")
            elif row["safe_zone"] not in profile["safe_zone_ids"]:
                raise ContractError("screen beat references unknown safe zone")
            if scene["visual_route"] != "existing_media_broll":
                reasons.append("screen_demo_requires_explicit_local_media_route")
        if (
            row["visual_role"] == "human_trust"
            and scene["visual_route"] != "existing_media_broll"
        ):
            reasons.append("human_trust_requires_real_footage")
        beats.append(
            {
                **row,
                **defaults,
                "start_s": scene["start_s"],
                "end_s": scene["end_s"],
                "visual_route": scene["visual_route"],
                "renderer": scene["renderer"],
                "status": "review_required" if reasons else "candidate_only",
                "review_reasons": reasons,
                "layout_validation": "existing_safe_media_and_semantic_motion_gates_required",
            }
        )
    return _seal(
        {
            "schema": BEAT_SCHEMA,
            "inputs": inputs,
            "beats": beats,
            "style": deepcopy(routing["style"]),
            "style_authority": "bound_existing_visual_style_profile",
            "scene_boundaries_must_not_be_rewritten": True,
            "approval_projection": deepcopy(routing["approval_projection"]),
        }
    )


def build_editorial_feedback(
    plan_path: str | Path, feedback_path: str | Path, approval_path: str | Path
) -> dict:
    plan = read_json_object(plan_path, "editorial plan")
    validate_editorial_artifact(plan)
    if plan["schema"] != PLAN_SCHEMA:
        raise ContractError("semantic editorial plan required")
    feedback = read_json_object(feedback_path, "human feedback")
    _validate(feedback, "feedback")
    approval = _require_approval(approval_path, feedback_path)
    if feedback["plan_sha256"] != plan["artifact_sha256"]:
        raise ContractError("feedback belongs to a different plan")
    if len({r["segment_id"] for r in feedback["decisions"]}) != len(
        feedback["decisions"]
    ):
        raise ContractError("duplicate feedback segment")
    decisions = {r["segment_id"]: r for r in plan["segments"]}
    diffs = []
    rows = []
    for row in feedback["decisions"]:
        if row["segment_id"] not in decisions:
            raise ContractError("feedback references unknown segment")
        recommendation = decisions[row["segment_id"]]
        rows.append(
            {
                **row,
                "ai_recommendation": recommendation,
                "human_confirmed": True,
                "confirmed_by": approval["attestation"]["approved_by"],
            }
        )
        diffs.append(
            {
                "human_confirmed": True,
                "source_video_sha256": plan["source_media_sha256"],
                "category": feedback["content_type"],
                "direction": row["outcome"],
                "rule": row["reason"],
                "explicit_user_pin": row["explicit_user_pin"],
            }
        )
    return _seal(
        {
            "schema": FEEDBACK_SCHEMA,
            "inputs": {
                "plan": artifact_reference(plan_path, "plan"),
                "feedback": artifact_reference(feedback_path, "feedback"),
                "approval": artifact_reference(approval_path, "approval"),
            },
            "decisions": rows,
            "content_type": feedback["content_type"],
            "preference_candidates": build_preference_candidates(diffs),
            "learning_scope": "candidate_priority_only",
            "rules_written": False,
        }
    )


def write_editorial_artifact(value: dict[str, Any], output_path: str | Path) -> Path:
    """Use the existing atomic writer; never replace a previous candidate."""
    target = Path(output_path).resolve()
    if target.exists():
        if read_json_object(target, "existing editorial output") == value:
            return target
        raise ContractError("editorial output already exists with different inputs")
    return atomic_write_json(value, target)


def aggregate_editorial_preferences(receipt_paths: list[str | Path]) -> dict:
    diffs = []
    for path in receipt_paths:
        receipt = read_json_object(path, "feedback receipt")
        validate_editorial_artifact(receipt)
        if receipt["schema"] != FEEDBACK_SCHEMA:
            raise ContractError("feedback receipt required for preference aggregation")
        plan_path = validate_artifact_reference(
            receipt["inputs"]["plan"], "feedback plan"
        )
        plan = read_json_object(plan_path, "plan")
        for row in receipt["decisions"]:
            diffs.append(
                {
                    "human_confirmed": True,
                    "source_video_sha256": plan["source_media_sha256"],
                    "category": receipt["content_type"],
                    "direction": row["outcome"],
                    "rule": row["reason"],
                    "explicit_user_pin": row["explicit_user_pin"],
                }
            )
    return build_preference_candidates(diffs)


def editorial_doctor() -> dict:
    return {
        "status": "available_candidate_planning_only",
        "network_required": False,
        "timebase_contract": "video_knowledge_pipeline.narration_timebase.v1",
        "timebase_contract_version": "1.0.0",
        "boundary": dict(BOUNDARY),
        "asr_added": False,
        "renderer_added": False,
        "runtime_visual_qa": "not_assessed_by_this_doctor",
        "semantic_inference": "agent_authored_spec_required",
    }
