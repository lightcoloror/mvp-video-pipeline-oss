"""Layered material revision contracts on the existing manual-media handoff.

No editor, importer, renderer or approval authority is implemented here.
"""
from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from .contract_io import artifact_reference, payload_sha256, read_json_object, validate_artifact_reference
from .errors import ContractError
from .generation_contracts import validate_generation_receipt

BRIEF_SCHEMA = "video_creation_pipeline.photocraft_revision_brief.v1"
PLAN_SCHEMA = "video_creation_pipeline.photocraft_revision_plan.v1"
RETURN_SCHEMA = "video_creation_pipeline.photocraft_revision_return.v1"
LOSS_AXES = ("layers", "fonts", "transparency", "crop", "export")
TRIAL_STEPS = ("open", "edit", "save", "reopen", "export")


def _require(ok: bool, message: str) -> None:
    if not ok:
        raise ContractError(message)


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _number(value: Any, low: float, high: float) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and low <= value <= high


def _object(value: Any, label: str) -> dict:
    _require(isinstance(value, dict), f"{label} must be an object")
    return value


def _ref(value: Any, label: str) -> Path:
    return validate_artifact_reference(_object(value, label), label)


def build_revision_plan(handoff_path: str | Path, brief_path: str | Path) -> dict:
    from .creative_tool_handoff import validate_creative_tool_handoff
    handoff = read_json_object(handoff_path, "creative handoff")
    validate_creative_tool_handoff(handoff)
    _require(handoff["tool"] == "photocraft", "revision requires the PhotoCraft image handoff")
    brief = read_json_object(brief_path, "revision brief")
    _require(brief.get("schema") == BRIEF_SCHEMA, "unsupported revision brief schema")
    _require(brief.get("source_kind") in {"synthetic", "public", "user_authorized"}, "source kind required")
    if brief["source_kind"] != "synthetic":
        _ref(brief.get("material_authorization"), "material authorization record")
    _ref(brief.get("editable_source"), "editable source")
    canvas = _object(brief.get("canvas"), "canvas")
    expected = handoff["work_request"]["output"]
    _require(all(type(canvas.get(k)) is int and canvas[k] == expected[k] for k in ("width", "height")),
             "canvas must match the existing shot dimensions")
    _require(_text(canvas.get("color_space")), "canvas color space required")
    layers = brief.get("layers")
    _require(isinstance(layers, list) and 0 < len(layers) <= 100, "bounded layer inventory required")
    ids = []
    for i, layer in enumerate(layers):
        _object(layer, "layer")
        _require(_text(layer.get("id")) and layer["id"] not in ids, "unique layer ID required")
        ids.append(layer["id"])
        _require(type(layer.get("order")) is int and layer["order"] == i, "layers must retain explicit bottom-to-top order")
        _require(layer.get("parent_id") is None or layer["parent_id"] in ids[:-1], "layer parent must precede child")
        _require(layer.get("kind") in {"text", "raster", "vector", "group"}, "layer kind required")
        _require(type(layer.get("visible")) is bool and _number(layer.get("opacity"), 0, 1), "finite opacity and visibility required")
        _require(_text(layer.get("blend_mode")) and _text(layer.get("mask")), "blend and mask inventory required")
        _require(isinstance(layer.get("effects"), list) and all(_text(x) for x in layer["effects"]), "effects inventory required")
        if layer["kind"] == "text":
            font = _object(layer.get("font"), "font")
            _require(_text(layer.get("text")) and _text(font.get("family")) and _text(font.get("style")), "text and expected font required")
            _require(font.get("availability") in {"available", "missing", "unverified"}, "font availability must be explicit")
            _require(font.get("substitution_allowed") is False, "silent font substitution is forbidden")
        color = _object(layer.get("color_role"), "color role")
        _require(color.get("kind") in {"material", "semantic", "brand_annotation", "layout"} and _text(color.get("reason")), "color meaning and reason required")
    crop = _object(brief.get("crop"), "crop")
    _require(crop.get("policy") in {"preserve_canvas", "explicit_crop"}, "crop policy required")
    rect = crop.get("rect_px")
    _require(isinstance(rect, list) and len(rect) == 4 and all(type(v) is int for v in rect), "crop rect requires four integer pixels")
    x,y,w,h = rect
    _require(x >= 0 and y >= 0 and w > 0 and h > 0 and x+w <= canvas['width'] and y+h <= canvas['height'], "crop rectangle outside canvas")
    if crop["policy"] == "preserve_canvas":
        _require(rect == [0,0,canvas['width'],canvas['height']], "preserve_canvas cannot silently crop")
    _require(_text(crop.get("reason")), "crop reason required")
    export = _object(brief.get("export"), "export")
    _require(export.get("preview_format") == "png" and export.get("alpha") in {"preserve", "opaque"}, "explicit PNG alpha policy required")
    _require(export.get("keep_editable_source") is True, "editable source must be returned")
    _require(_text(brief.get("requested_change")), "specific requested change required")
    plan = {
        "schema": PLAN_SCHEMA, "source_handoff": artifact_reference(handoff_path, "creative handoff"),
        "source_brief": artifact_reference(brief_path, "revision brief"),
        "scene_id": handoff["work_request"]["scene_id"], "requirements": brief,
        "return_contract": {"schema": RETURN_SCHEMA, "loss_axes": list(LOSS_AXES),
                            "software_trial_steps": list(TRIAL_STEPS),
                            "manual_media_validation": "generation_contracts.validate_generation_receipt",
                            "next_gate": "existing technical QA and human review"},
        "status": "candidate_only", "application_import_file": False,
        "runtime_verified": False, "executionAuthority": "none_metadata_only",
        "publish_approved": False,
    }
    plan["plan_sha256"] = payload_sha256(plan, "plan_sha256")
    return plan


def validate_revision_plan(plan: dict) -> None:
    _require(plan.get("schema") == PLAN_SCHEMA, "unsupported revision plan schema")
    _require(plan.get("plan_sha256") == payload_sha256(plan, "plan_sha256"), "revision plan integrity failed")
    handoff = _ref(plan.get("source_handoff"), "creative handoff")
    brief = _ref(plan.get("source_brief"), "revision brief")
    _require(plan == build_revision_plan(handoff, brief), "revision plan content or authority drifted")


def validate_revision_return(payload: dict) -> dict:
    """Validate reported evidence; never infer editor fidelity from file hashes."""
    _require(payload.get("schema") == RETURN_SCHEMA, "unsupported revision return schema")
    _require(payload.get("return_sha256") == payload_sha256(payload, "return_sha256"), "revision return integrity failed")
    plan_path = _ref(payload.get("source_plan"), "revision plan")
    plan = read_json_object(plan_path, "revision plan"); validate_revision_plan(plan)
    _require(payload.get("scene_id") == plan["scene_id"], "revision scene identity drifted")
    _require(payload.get("status") == "candidate_only" and payload.get("publish_approved") is False,
             "revision remains an unpublished candidate")
    original = Path(plan['requirements']['editable_source']['path']).resolve()
    editable = _ref(payload.get("editable_result"), "editable result")
    preview = _ref(payload.get("preview"), "flattened preview")
    _require(len({original, editable, preview}) == 3, "original, editable result and preview must be distinct")
    _require(_text(payload.get("change_notes")), "change notes required")
    losses = _object(payload.get("loss_report"), "loss report")
    _require(set(losses) == set(LOSS_AXES), "all five loss axes must be reported")
    pending = []
    for axis in LOSS_AXES:
        row = _object(losses[axis], axis)
        _require(row.get("status") in {"preserved", "changed", "unsupported", "unverified"}, "invalid loss status")
        _require(all(_text(row.get(k)) for k in ("before", "after", "detail")), f"{axis} before/after/detail required")
        refs = row.get("evidence")
        _require(isinstance(refs, list), "loss evidence list required")
        for ref in refs: _ref(ref, f"{axis} evidence")
        if row['status'] != 'unverified': _require(bool(refs), "reported inspection requires evidence")
        if row['status'] != 'preserved': pending.append(axis)
    trial = _object(payload.get("software_trial"), "software trial")
    _require(trial.get("status") in {"blocked", "unverified", "reported_executed"}, "explicit software trial status required")
    _require(_text(trial.get("environment")) and _text(trial.get("limitations")), "software environment and limitations required")
    if trial["status"] == "reported_executed":
        steps = _object(trial.get("steps"), "software trial steps")
        _require(set(steps) == set(TRIAL_STEPS), "open/edit/save/reopen/export evidence required")
        for step in TRIAL_STEPS: _ref(steps[step], f"software {step}")
    else:
        _require(all(row['status'] == 'unverified' for row in losses.values()), "unrun software cannot claim preserved or inspected losses")
    binding = payload.get("manual_media")
    validation = None
    if binding is not None:
        _object(binding, "manual media")
        task = read_json_object(_ref(binding.get("task"), "generation task"), "generation task")
        receipt = read_json_object(_ref(binding.get("receipt"), "generation receipt"), "generation receipt")
        handoff = read_json_object(plan['source_handoff']['path'], 'creative handoff')
        _require(task.get("generator") == "manual" and task.get("media_type") == "image", "existing manual image route required")
        _require(task.get("source_unit") == handoff["source_shot_unit"], "manual task binds a different shot")
        _require(receipt.get("output") == payload["preview"], "manual receipt must bind returned preview")
        validation = validate_generation_receipt(task, receipt)
        _require(validation['status'] == 'completed', "manual media receipt is not completed")
    ready = validation is not None and trial['status'] == 'reported_executed' and not pending
    return {"status": "valid_revision_contract_only", "acceptance_status": "candidate_only",
            "software_trial_status": trial['status'], "runtime_verified": False,
            "evidence_level": "reported_artifacts_not_independent_software_acceptance",
            "manual_media_validation": validation, "loss_review_required": pending,
            "can_enter_existing_qa": ready, "publish_approved": False,
            "next_action": "existing_qa_and_human_review" if ready else "resolve_unverified_losses_or_manual_media"}
