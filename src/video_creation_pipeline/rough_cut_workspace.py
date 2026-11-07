from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from uuid import uuid4

from .contract_io import (
    artifact_reference,
    atomic_write_json,
    canonical_json_sha256,
    payload_sha256,
    read_json_object,
    validate_artifact_reference,
)
from .errors import ContractError
from .reference_remix import (
    material_candidate_map,
    normalize_reference_recipe,
)

LEGACY_ROUGH_CUT_WORKSPACE_SCHEMA = "video_creation_pipeline.rough_cut_workspace.v1"
ROUGH_CUT_WORKSPACE_SCHEMA = "video_creation_pipeline.rough_cut_workspace.v2"
ROUGH_CUT_SELECTION_SCHEMA = "video_creation_pipeline.rough_cut_selection.v1"
MATERIAL_RETRY_PLAN_SCHEMA = "video_creation_pipeline.material_retry_plan.v1"
ROUGH_CUT_FINALIZE_RECEIPT_SCHEMA = (
    "video_creation_pipeline.rough_cut_finalize_receipt.v1"
)

SCRIPT_ALIGNED_REVIEW_POLICY = {
    "default_delivery_mode": "script_aligned_independent_clips",
    "alignment_key": "storyboard_shot_id",
    "independent_clip_per_script_segment_required": True,
    "material_index_required": True,
    "cross_segment_batch_browse_allowed": False,
    "continuous_review_reel_generated_by_default": False,
    "unresolved_mapping_route": "upstream_material_discovery",
}


def build_rough_cut_workspace(
    storyboard_path: str | Path,
    candidates_path: str | Path,
    *,
    allowed_material_ids: Iterable[str] = (),
) -> dict[str, Any]:
    storyboard_file = Path(storyboard_path).expanduser().resolve()
    candidates_file = Path(candidates_path).expanduser().resolve()
    storyboard = normalize_reference_recipe(
        read_json_object(storyboard_file, "storyboard")
    )
    candidates = read_json_object(candidates_file, "material candidates")
    candidate_map = material_candidate_map(candidates)
    shot_ids = [str(row["shot_id"]) for row in storyboard["shots"]]
    if set(candidate_map) != set(shot_ids):
        missing = sorted(set(shot_ids) - set(candidate_map))
        extra = sorted(set(candidate_map) - set(shot_ids))
        raise ContractError(
            "rough-cut candidates must match storyboard shots exactly"
            f" (missing={missing}, extra={extra})"
        )

    candidate_ids = {
        _candidate_id(candidate)
        for rows in candidate_map.values()
        for candidate in rows
    }
    declared_ids = {
        str(value).strip() for value in allowed_material_ids if str(value).strip()
    }
    if not declared_ids:
        declared_ids = {
            str(value).strip()
            for value in candidates.get("allowed_material_ids") or []
            if str(value).strip()
        }
    if not declared_ids:
        declared_ids = set(candidate_ids)
    unknown = sorted(candidate_ids - declared_ids)
    if unknown:
        raise ContractError(
            "round-one candidates fall outside allowed material ids: "
            + ", ".join(unknown)
        )

    workspace: dict[str, Any] = {
        "schema": ROUGH_CUT_WORKSPACE_SCHEMA,
        "workspace_id": str(uuid4()),
        "created_at": _now(),
        "source_storyboard": artifact_reference(storyboard_file, "storyboard"),
        "round_one_candidates": artifact_reference(
            candidates_file, "material candidates"
        ),
        "shot_ids": shot_ids,
        "candidate_inventory": {
            shot_id: [_candidate_id(row) for row in candidate_map[shot_id]]
            for shot_id in shot_ids
        },
        "review_delivery_policy": dict(SCRIPT_ALIGNED_REVIEW_POLICY),
        "review_units": [
            {
                "script_segment_id": shot_id,
                "candidate_ids": [_candidate_id(row) for row in candidate_map[shot_id]],
                "review_mode": (
                    "single_mapped_clip"
                    if len(candidate_map[shot_id]) == 1
                    else "compare_candidates_within_segment"
                ),
            }
            for shot_id in shot_ids
        ],
        "allowed_material_ids": sorted(declared_ids),
        "round_policy": {
            "maximum_rounds": 2,
            "round_two_only_for_rejected_shots": True,
            "confirmed_round_one_shots_locked": True,
            "silent_drop_allowed": False,
        },
        "authority_boundary": {
            "candidate_workspace_only": True,
            "project_truth_created": False,
            "status_machine_created": False,
            "database_created": False,
            "ffmpeg_executed": False,
            "asr_executed": False,
            "online_model_executed": False,
        },
    }
    workspace["workspace_sha256"] = payload_sha256(workspace, "workspace_sha256")
    validate_rough_cut_workspace(workspace)
    return workspace


def validate_rough_cut_workspace(workspace: dict[str, Any]) -> None:
    schema = workspace.get("schema")
    if schema not in {
        LEGACY_ROUGH_CUT_WORKSPACE_SCHEMA,
        ROUGH_CUT_WORKSPACE_SCHEMA,
    }:
        raise ContractError("unsupported rough-cut workspace schema")
    if workspace.get("workspace_sha256") != payload_sha256(
        workspace, "workspace_sha256"
    ):
        raise ContractError("rough-cut workspace integrity check failed")
    storyboard_path = validate_artifact_reference(
        _object(workspace.get("source_storyboard")),
        "rough-cut storyboard",
    )
    candidates_path = validate_artifact_reference(
        _object(workspace.get("round_one_candidates")),
        "round-one material candidates",
    )
    storyboard = normalize_reference_recipe(
        read_json_object(storyboard_path, "rough-cut storyboard")
    )
    candidates = read_json_object(candidates_path, "round-one material candidates")
    candidate_map = material_candidate_map(candidates)
    expected_shots = [str(row["shot_id"]) for row in storyboard["shots"]]
    if workspace.get("shot_ids") != expected_shots:
        raise ContractError("rough-cut workspace storyboard shots drifted")
    expected_inventory = {
        shot_id: [_candidate_id(row) for row in candidate_map.get(shot_id, [])]
        for shot_id in expected_shots
    }
    if workspace.get("candidate_inventory") != expected_inventory:
        raise ContractError("rough-cut workspace candidate inventory drifted")
    if schema == ROUGH_CUT_WORKSPACE_SCHEMA:
        if workspace.get("review_delivery_policy") != SCRIPT_ALIGNED_REVIEW_POLICY:
            raise ContractError(
                "rough-cut review delivery must use script-aligned independent clips"
            )
        expected_review_units = [
            {
                "script_segment_id": shot_id,
                "candidate_ids": expected_inventory[shot_id],
                "review_mode": (
                    "single_mapped_clip"
                    if len(expected_inventory[shot_id]) == 1
                    else "compare_candidates_within_segment"
                ),
            }
            for shot_id in expected_shots
        ]
        if workspace.get("review_units") != expected_review_units:
            raise ContractError(
                "rough-cut review units must match storyboard shots and candidate clips"
            )
    allowed = _unique_strings(
        workspace.get("allowed_material_ids"), "allowed material ids"
    )
    inventory = {
        candidate_id
        for values in expected_inventory.values()
        for candidate_id in values
    }
    if not inventory.issubset(set(allowed)):
        raise ContractError(
            "rough-cut candidate inventory exceeds the material allowlist"
        )
    policy = _object(workspace.get("round_policy"))
    if (
        policy.get("maximum_rounds") != 2
        or policy.get("round_two_only_for_rejected_shots") is not True
        or policy.get("confirmed_round_one_shots_locked") is not True
        or policy.get("silent_drop_allowed") is not False
    ):
        raise ContractError("rough-cut round policy is incomplete")
    _validate_nonexecuting_boundary(
        workspace.get("authority_boundary"), "rough-cut workspace"
    )


def validate_rough_cut_selection(
    selection: dict[str, Any],
    workspace: dict[str, Any],
    *,
    round_number: int,
    candidate_map: dict[str, list[dict[str, Any]]] | None = None,
    expected_shot_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    validate_rough_cut_workspace(workspace)
    if selection.get("schema") != ROUGH_CUT_SELECTION_SCHEMA:
        raise ContractError("unsupported rough-cut selection schema")
    if selection.get("workspace_sha256") != workspace.get("workspace_sha256"):
        raise ContractError("rough-cut selection does not bind the workspace")
    if selection.get("round") != round_number:
        raise ContractError("rough-cut selection round is invalid")
    confirmation = _object(selection.get("operator_confirmation"))
    if (
        confirmation.get("confirmed") is not True
        or confirmation.get("method") != "visible_operator_selection"
        or not str(confirmation.get("confirmed_by") or "").strip()
        or _parse_datetime(confirmation.get("confirmed_at")) is None
    ):
        raise ContractError(
            "rough-cut selection requires visible operator confirmation"
        )

    rows = selection.get("decisions")
    if not isinstance(rows, list) or not rows:
        raise ContractError("rough-cut selection requires decisions")
    expected = expected_shot_ids or set(workspace["shot_ids"])
    if len(rows) != len(expected):
        raise ContractError("rough-cut selection must decide every expected shot")
    available = candidate_map or _workspace_candidate_map(workspace)
    decisions: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in rows:
        if not isinstance(raw, dict):
            raise ContractError("rough-cut decision must be an object")
        shot_id = str(raw.get("shot_id") or "").strip()
        if not shot_id or shot_id in seen or shot_id not in expected:
            raise ContractError("rough-cut decision shot id is invalid or duplicated")
        seen.add(shot_id)
        status = str(raw.get("status") or "").strip().lower()
        if status not in {"accepted", "rejected"}:
            raise ContractError(
                "rough-cut decision status must be accepted or rejected"
            )
        candidate_id = str(raw.get("candidate_id") or "").strip()
        reason = str(raw.get("reason") or "").strip()
        known = {_candidate_id(candidate) for candidate in available.get(shot_id, [])}
        if status == "accepted" and candidate_id not in known:
            raise ContractError(
                f"accepted rough-cut candidate is unavailable: {shot_id}/{candidate_id}"
            )
        if status == "rejected" and not reason:
            raise ContractError("rejected rough-cut decision requires a visible reason")
        if candidate_id and candidate_id not in known:
            raise ContractError(
                f"rough-cut decision references an unavailable candidate: "
                f"{shot_id}/{candidate_id}"
            )
        decisions.append(
            {
                "shot_id": shot_id,
                "status": status,
                "candidate_id": candidate_id,
                "reason": reason,
                "human_confirmed": True,
                "source_round": round_number,
            }
        )
    if seen != expected:
        raise ContractError("rough-cut selection shot set is incomplete")
    decisions.sort(key=lambda row: workspace["shot_ids"].index(row["shot_id"]))
    return decisions


def build_material_retry_plan(
    workspace_path: str | Path,
    round_one_selection_path: str | Path,
) -> dict[str, Any]:
    workspace_file = Path(workspace_path).expanduser().resolve()
    selection_file = Path(round_one_selection_path).expanduser().resolve()
    workspace = read_json_object(workspace_file, "rough-cut workspace")
    selection = read_json_object(selection_file, "round-one selection")
    validate_rough_cut_workspace(workspace)
    decisions = validate_rough_cut_selection(selection, workspace, round_number=1)
    rejected = [row for row in decisions if row["status"] == "rejected"]
    if not rejected:
        raise ContractError(
            "round one has no rejected shots; retry plan is unnecessary"
        )

    plan: dict[str, Any] = {
        "schema": MATERIAL_RETRY_PLAN_SCHEMA,
        "created_at": _now(),
        "source_workspace": artifact_reference(workspace_file, "rough-cut workspace"),
        "round_one_selection": artifact_reference(
            selection_file, "round-one selection"
        ),
        "workspace_sha256": workspace["workspace_sha256"],
        "retry_round": 2,
        "retry_required": True,
        "retry_shots": [
            {
                "shot_id": row["shot_id"],
                "rejected_candidate_id": row["candidate_id"],
                "rejection_reason": row["reason"],
                "allowed_material_ids": list(workspace["allowed_material_ids"]),
                "must_not_modify_locked_shots": True,
            }
            for row in rejected
        ],
        "locked_round_one_decisions": [
            row for row in decisions if row["status"] == "accepted"
        ],
        "execution_boundary": {
            "candidate_query_only": True,
            "index_provider_selected": False,
            "automatic_fallback": False,
            "database_created": False,
            "ffmpeg_executed": False,
            "asr_executed": False,
            "online_model_executed": False,
        },
    }
    plan["retry_plan_sha256"] = payload_sha256(plan, "retry_plan_sha256")
    validate_material_retry_plan(plan)
    return plan


def validate_material_retry_plan(plan: dict[str, Any]) -> None:
    if plan.get("schema") != MATERIAL_RETRY_PLAN_SCHEMA:
        raise ContractError("unsupported material retry plan schema")
    if plan.get("retry_plan_sha256") != payload_sha256(plan, "retry_plan_sha256"):
        raise ContractError("material retry plan integrity check failed")
    workspace_path = validate_artifact_reference(
        _object(plan.get("source_workspace")), "rough-cut workspace"
    )
    selection_path = validate_artifact_reference(
        _object(plan.get("round_one_selection")), "round-one selection"
    )
    workspace = read_json_object(workspace_path, "rough-cut workspace")
    selection = read_json_object(selection_path, "round-one selection")
    validate_rough_cut_workspace(workspace)
    if plan.get("workspace_sha256") != workspace.get("workspace_sha256"):
        raise ContractError("material retry plan workspace binding is stale")
    decisions = validate_rough_cut_selection(selection, workspace, round_number=1)
    expected_rejected = [row for row in decisions if row["status"] == "rejected"]
    expected_locked = [row for row in decisions if row["status"] == "accepted"]
    expected_retry = [
        {
            "shot_id": row["shot_id"],
            "rejected_candidate_id": row["candidate_id"],
            "rejection_reason": row["reason"],
            "allowed_material_ids": list(workspace["allowed_material_ids"]),
            "must_not_modify_locked_shots": True,
        }
        for row in expected_rejected
    ]
    if (
        not expected_retry
        or plan.get("retry_round") != 2
        or plan.get("retry_required") is not True
        or plan.get("retry_shots") != expected_retry
        or plan.get("locked_round_one_decisions") != expected_locked
    ):
        raise ContractError("material retry plan differs from round-one decisions")
    boundary = _object(plan.get("execution_boundary"))
    required_false = (
        "index_provider_selected",
        "automatic_fallback",
        "database_created",
        "ffmpeg_executed",
        "asr_executed",
        "online_model_executed",
    )
    if boundary.get("candidate_query_only") is not True or not all(
        boundary.get(field) is False for field in required_false
    ):
        raise ContractError("material retry plan execution boundary is incomplete")


def build_rough_cut_finalize_receipt(
    workspace_path: str | Path,
    round_one_selection_path: str | Path,
    *,
    confirmed_by: str,
    confirmed_at: str,
    retry_plan_path: str | Path | None = None,
    round_two_candidates_path: str | Path | None = None,
    round_two_selection_path: str | Path | None = None,
) -> dict[str, Any]:
    workspace_file = Path(workspace_path).expanduser().resolve()
    round_one_file = Path(round_one_selection_path).expanduser().resolve()
    workspace = read_json_object(workspace_file, "rough-cut workspace")
    round_one = read_json_object(round_one_file, "round-one selection")
    validate_rough_cut_workspace(workspace)
    first_decisions = validate_rough_cut_selection(round_one, workspace, round_number=1)
    actor = str(confirmed_by).strip()
    confirmation_time = _parse_datetime(confirmed_at)
    if not actor or confirmation_time is None:
        raise ContractError(
            "rough-cut finalization requires a dated operator confirmation"
        )

    rejected_ids = {
        row["shot_id"] for row in first_decisions if row["status"] == "rejected"
    }
    second_refs: dict[str, Any] | None = None
    second_decisions: list[dict[str, Any]] = []
    supplied = (
        retry_plan_path,
        round_two_candidates_path,
        round_two_selection_path,
    )
    if rejected_ids:
        if any(value is None for value in supplied):
            raise ContractError(
                "rejected round-one shots require retry plan, round-two candidates, "
                "and round-two selection"
            )
        retry_file = Path(str(retry_plan_path)).expanduser().resolve()
        candidates_file = Path(str(round_two_candidates_path)).expanduser().resolve()
        selection_file = Path(str(round_two_selection_path)).expanduser().resolve()
        retry_plan = read_json_object(retry_file, "material retry plan")
        validate_material_retry_plan(retry_plan)
        if retry_plan["source_workspace"] != artifact_reference(
            workspace_file, "rough-cut workspace"
        ):
            raise ContractError("retry plan does not bind the selected workspace")
        if retry_plan["round_one_selection"] != artifact_reference(
            round_one_file, "round-one selection"
        ):
            raise ContractError(
                "retry plan does not bind the selected round-one selection"
            )
        candidates = read_json_object(candidates_file, "round-two material candidates")
        second_map = material_candidate_map(candidates)
        if set(second_map) != rejected_ids:
            raise ContractError(
                "round-two candidates must contain exactly the rejected shots"
            )
        allowed = set(workspace["allowed_material_ids"])
        if any(
            _candidate_id(candidate) not in allowed
            for rows in second_map.values()
            for candidate in rows
        ):
            raise ContractError("round-two candidates exceed the material allowlist")
        round_two = read_json_object(selection_file, "round-two selection")
        second_decisions = validate_rough_cut_selection(
            round_two,
            workspace,
            round_number=2,
            candidate_map=second_map,
            expected_shot_ids=rejected_ids,
        )
        second_refs = {
            "retry_plan": artifact_reference(retry_file, "material retry plan"),
            "candidates": artifact_reference(
                candidates_file, "round-two material candidates"
            ),
            "selection": artifact_reference(selection_file, "round-two selection"),
        }
    elif any(value is not None for value in supplied):
        raise ContractError("round-two artifacts are forbidden when round one passed")

    final_rows = _merge_final_decisions(
        workspace["shot_ids"], first_decisions, second_decisions
    )
    unresolved = [
        {
            "shot_id": row["shot_id"],
            "reason": row["reason"],
            "last_candidate_id": row["candidate_id"],
            "source_round": row["source_round"],
        }
        for row in final_rows
        if row["status"] == "rejected"
    ]
    receipt: dict[str, Any] = {
        "schema": ROUGH_CUT_FINALIZE_RECEIPT_SCHEMA,
        "created_at": _now(),
        "source_workspace": artifact_reference(workspace_file, "rough-cut workspace"),
        "round_one_selection": artifact_reference(
            round_one_file, "round-one selection"
        ),
        "round_two": second_refs,
        "workspace_sha256": workspace["workspace_sha256"],
        "final_decisions": final_rows,
        "unresolved_shots": unresolved,
        "final_timeline_sha256": canonical_json_sha256(
            [row for row in final_rows if row["status"] == "accepted"]
        ),
        "operator_confirmation": {
            "confirmed": True,
            "method": "visible_operator_finalization",
            "confirmed_by": actor,
            "confirmed_at": confirmation_time.isoformat(),
        },
        "readiness": {
            "status": "ready_for_boundary_refinement"
            if not unresolved
            else "unresolved_material",
            "render_eligible": not unresolved,
            "next_action": "videocut_kit_boundary_refinement"
            if not unresolved
            else "human_material_review",
        },
        "execution_boundary": {
            "candidate_only": True,
            "timeline_truth_created": False,
            "ffmpeg_executed": False,
            "boundary_refinement_executed": False,
            "automatic_fallback": False,
            "automatic_publish": False,
        },
    }
    receipt["finalize_receipt_sha256"] = payload_sha256(
        receipt, "finalize_receipt_sha256"
    )
    validate_rough_cut_finalize_receipt(receipt)
    return receipt


def validate_rough_cut_finalize_receipt(receipt: dict[str, Any]) -> None:
    if receipt.get("schema") != ROUGH_CUT_FINALIZE_RECEIPT_SCHEMA:
        raise ContractError("unsupported rough-cut finalize receipt schema")
    if receipt.get("finalize_receipt_sha256") != payload_sha256(
        receipt, "finalize_receipt_sha256"
    ):
        raise ContractError("rough-cut finalize receipt integrity check failed")
    workspace_path = validate_artifact_reference(
        _object(receipt.get("source_workspace")), "rough-cut workspace"
    )
    round_one_path = validate_artifact_reference(
        _object(receipt.get("round_one_selection")), "round-one selection"
    )
    workspace = read_json_object(workspace_path, "rough-cut workspace")
    round_one = read_json_object(round_one_path, "round-one selection")
    validate_rough_cut_workspace(workspace)
    if receipt.get("workspace_sha256") != workspace.get("workspace_sha256"):
        raise ContractError("rough-cut finalize workspace binding is stale")
    first_decisions = validate_rough_cut_selection(round_one, workspace, round_number=1)
    rejected_ids = {
        row["shot_id"] for row in first_decisions if row["status"] == "rejected"
    }
    second_decisions: list[dict[str, Any]] = []
    second = receipt.get("round_two")
    if rejected_ids:
        if not isinstance(second, dict):
            raise ContractError("rough-cut finalize receipt lacks round-two artifacts")
        retry_path = validate_artifact_reference(
            _object(second.get("retry_plan")), "material retry plan"
        )
        candidates_path = validate_artifact_reference(
            _object(second.get("candidates")), "round-two material candidates"
        )
        selection_path = validate_artifact_reference(
            _object(second.get("selection")), "round-two selection"
        )
        retry_plan = read_json_object(retry_path, "material retry plan")
        validate_material_retry_plan(retry_plan)
        if (
            retry_plan["source_workspace"] != receipt["source_workspace"]
            or retry_plan["round_one_selection"] != receipt["round_one_selection"]
        ):
            raise ContractError("rough-cut retry plan source binding is inconsistent")
        candidates = read_json_object(candidates_path, "round-two material candidates")
        second_map = material_candidate_map(candidates)
        if set(second_map) != rejected_ids:
            raise ContractError("rough-cut round-two shot set drifted")
        allowed = set(workspace["allowed_material_ids"])
        if any(
            _candidate_id(candidate) not in allowed
            for rows in second_map.values()
            for candidate in rows
        ):
            raise ContractError("rough-cut round-two candidates exceed the allowlist")
        round_two = read_json_object(selection_path, "round-two selection")
        second_decisions = validate_rough_cut_selection(
            round_two,
            workspace,
            round_number=2,
            candidate_map=second_map,
            expected_shot_ids=rejected_ids,
        )
    elif second is not None:
        raise ContractError("rough-cut finalize receipt contains unnecessary round two")

    expected_rows = _merge_final_decisions(
        workspace["shot_ids"], first_decisions, second_decisions
    )
    if receipt.get("final_decisions") != expected_rows:
        raise ContractError(
            "rough-cut final decisions drifted from confirmed selections"
        )
    expected_unresolved = [
        {
            "shot_id": row["shot_id"],
            "reason": row["reason"],
            "last_candidate_id": row["candidate_id"],
            "source_round": row["source_round"],
        }
        for row in expected_rows
        if row["status"] == "rejected"
    ]
    if receipt.get("unresolved_shots") != expected_unresolved:
        raise ContractError("rough-cut unresolved shot list is inaccurate")
    expected_timeline_sha = canonical_json_sha256(
        [row for row in expected_rows if row["status"] == "accepted"]
    )
    if receipt.get("final_timeline_sha256") != expected_timeline_sha:
        raise ContractError("rough-cut final timeline digest is invalid")
    confirmation = _object(receipt.get("operator_confirmation"))
    if (
        confirmation.get("confirmed") is not True
        or confirmation.get("method") != "visible_operator_finalization"
        or not str(confirmation.get("confirmed_by") or "").strip()
        or _parse_datetime(confirmation.get("confirmed_at")) is None
    ):
        raise ContractError("rough-cut finalize receipt lacks operator confirmation")
    readiness = _object(receipt.get("readiness"))
    expected_ready = not expected_unresolved
    if (
        readiness.get("render_eligible") is not expected_ready
        or readiness.get("status")
        != (
            "ready_for_boundary_refinement" if expected_ready else "unresolved_material"
        )
        or readiness.get("next_action")
        != (
            "videocut_kit_boundary_refinement"
            if expected_ready
            else "human_material_review"
        )
    ):
        raise ContractError("rough-cut finalize readiness is inaccurate")
    boundary = _object(receipt.get("execution_boundary"))
    if boundary.get("candidate_only") is not True or any(
        boundary.get(field) is not False
        for field in (
            "timeline_truth_created",
            "ffmpeg_executed",
            "boundary_refinement_executed",
            "automatic_fallback",
            "automatic_publish",
        )
    ):
        raise ContractError("rough-cut finalize execution boundary is incomplete")


def write_rough_cut_contract(payload: dict[str, Any], output_path: str | Path) -> Path:
    schema = payload.get("schema")
    if schema == ROUGH_CUT_WORKSPACE_SCHEMA:
        validate_rough_cut_workspace(payload)
    elif schema == MATERIAL_RETRY_PLAN_SCHEMA:
        validate_material_retry_plan(payload)
    elif schema == ROUGH_CUT_FINALIZE_RECEIPT_SCHEMA:
        validate_rough_cut_finalize_receipt(payload)
    else:
        raise ContractError("unsupported rough-cut contract")
    return atomic_write_json(payload, output_path)


def _workspace_candidate_map(
    workspace: dict[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    source = _object(workspace.get("round_one_candidates"))
    path = validate_artifact_reference(source, "round-one material candidates")
    return material_candidate_map(
        read_json_object(path, "round-one material candidates")
    )


def _merge_final_decisions(
    shot_ids: list[str],
    first: list[dict[str, Any]],
    second: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    first_map = {row["shot_id"]: row for row in first}
    second_map = {row["shot_id"]: row for row in second}
    result: list[dict[str, Any]] = []
    for shot_id in shot_ids:
        first_row = first_map[shot_id]
        row = (
            first_row
            if first_row["status"] == "accepted"
            else second_map.get(shot_id, first_row)
        )
        result.append(dict(row))
    return result


def _candidate_id(candidate: dict[str, Any]) -> str:
    value = str(
        candidate.get("candidate_id") or candidate.get("asset_id") or ""
    ).strip()
    if not value:
        raise ContractError("material candidate requires asset_id or candidate_id")
    return value


def _object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _unique_strings(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ContractError(f"{label} must be a non-empty list")
    result = [str(row).strip() for row in value]
    if any(not row for row in result) or len(result) != len(set(result)):
        raise ContractError(f"{label} must contain unique non-empty values")
    return result


def _validate_nonexecuting_boundary(value: Any, label: str) -> None:
    boundary = _object(value)
    if boundary.get("candidate_workspace_only") is not True or any(
        boundary.get(field) is not False
        for field in (
            "project_truth_created",
            "status_machine_created",
            "database_created",
            "ffmpeg_executed",
            "asr_executed",
            "online_model_executed",
        )
    ):
        raise ContractError(f"{label} authority boundary is incomplete")


def _parse_datetime(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
