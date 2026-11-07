from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any

from .errors import ContractError

REFERENCE_RECIPE_SCHEMA = "video_creation_pipeline.reference_recipe.v1"
MATERIAL_CANDIDATES_SCHEMA = "video_creation_pipeline.material_candidates.v1"
MATERIAL_MATCH_PLAN_SCHEMA = "video_creation_pipeline.material_match_plan.v1"
VOICE_MEASUREMENTS_SCHEMA = "video_creation_pipeline.voice_measurements.v1"
VOICE_TIMING_PLAN_SCHEMA = "video_creation_pipeline.voice_timing_plan.v1"


def normalize_reference_recipe(source: dict[str, Any] | str | Path) -> dict[str, Any]:
    payload = _read_object(source, "reference recipe")
    if payload.get("schema") != REFERENCE_RECIPE_SCHEMA:
        raise ContractError(f"reference recipe must use {REFERENCE_RECIPE_SCHEMA}")
    reference = (
        payload.get("reference") if isinstance(payload.get("reference"), dict) else {}
    )
    if reference.get("kind") == "vkp_decomposition_report":
        from .decomposition_adoption import (
            validate_decomposition_reference_recipe,
        )

        validate_decomposition_reference_recipe(payload)
    raw_shots = payload.get("shots")
    if not isinstance(raw_shots, list) or not raw_shots:
        raise ContractError("reference recipe must contain at least one shot")

    shots: list[dict[str, Any]] = []
    shot_ids: set[str] = set()
    previous_end = -1.0
    for index, raw in enumerate(raw_shots, 1):
        if not isinstance(raw, dict):
            raise ContractError(f"reference recipe shot {index} must be an object")
        shot_id = str(raw.get("shot_id") or "").strip()
        if not shot_id or shot_id in shot_ids:
            raise ContractError(
                "reference recipe shot ids must be non-empty and unique"
            )
        start = _number(raw.get("start_s"), f"{shot_id}.start_s")
        end = _number(raw.get("end_s"), f"{shot_id}.end_s")
        if end <= start:
            raise ContractError(f"{shot_id} end_s must be greater than start_s")
        if start < previous_end:
            raise ContractError(
                "reference recipe shots must be ordered and not overlap"
            )
        previous_end = end
        shot_ids.add(shot_id)
        requirements = raw.get("requirements")
        if requirements is None:
            requirements = {}
        if not isinstance(requirements, dict):
            raise ContractError(f"{shot_id}.requirements must be an object")
        avoid = requirements.get("avoid") or []
        if not isinstance(avoid, list):
            raise ContractError(f"{shot_id}.requirements.avoid must be a list")
        shots.append(
            {
                "shot_id": shot_id,
                "order": index,
                "start_s": round(start, 3),
                "end_s": round(end, 3),
                "duration_s": round(end - start, 3),
                "script": str(raw.get("script") or "").strip(),
                "requirements": {
                    "scene": str(requirements.get("scene") or "").strip(),
                    "action": str(requirements.get("action") or "").strip(),
                    "shot_size": str(requirements.get("shot_size") or "").strip(),
                    "avoid": [
                        str(value).strip() for value in avoid if str(value).strip()
                    ],
                },
                "source_status": "candidate_only",
                "human_confirmed": False,
            }
        )

    return {
        "schema": REFERENCE_RECIPE_SCHEMA,
        "reference": reference,
        "shots": shots,
        "human_review": {
            "cut_points_confirmed": False,
            "script_confirmed": False,
            "required_before_render": True,
        },
        "creative_boundary": {
            "reference_role": "structure_and_pacing_candidate",
            "copy_reference_media": False,
            "copy_reference_script": False,
        },
    }


def build_material_match_plan(
    recipe_source: dict[str, Any] | str | Path,
    candidates_source: dict[str, Any] | str | Path,
    *,
    min_confidence: float = 0.6,
    max_consecutive_uses: int = 2,
    repeated_context_penalty: float = 0.08,
) -> dict[str, Any]:
    if not 0 <= min_confidence <= 1:
        raise ContractError("min_confidence must be between 0 and 1")
    if max_consecutive_uses < 1:
        raise ContractError("max_consecutive_uses must be at least 1")
    if not 0 <= repeated_context_penalty <= 1:
        raise ContractError("repeated_context_penalty must be between 0 and 1")

    recipe = normalize_reference_recipe(recipe_source)
    candidates_payload = _read_object(candidates_source, "material candidates")
    if candidates_payload.get("schema") != MATERIAL_CANDIDATES_SCHEMA:
        raise ContractError(
            f"material candidates must use {MATERIAL_CANDIDATES_SCHEMA}"
        )
    by_shot = _candidate_map(candidates_payload)

    assignments: list[dict[str, Any]] = []
    previous_context: dict[str, str] = {}
    consecutive_source = ""
    consecutive_count = 0
    missing_count = 0

    for shot in recipe["shots"]:
        ranked: list[dict[str, Any]] = []
        for candidate in by_shot.get(shot["shot_id"], []):
            source_key = candidate["source_media_id"] or candidate["asset_id"]
            same_source_streak = (
                consecutive_count if source_key == consecutive_source else 0
            )
            hard_rejected = same_source_streak >= max_consecutive_uses
            penalties: list[dict[str, Any]] = []
            adjusted = candidate["confidence"]
            for field in ("room_id", "composition_id"):
                value = candidate[field]
                if value and value == previous_context.get(field):
                    adjusted -= repeated_context_penalty
                    penalties.append(
                        {
                            "reason": f"adjacent_{field}_repeat",
                            "amount": round(repeated_context_penalty, 4),
                        }
                    )
            ranked.append(
                {
                    **candidate,
                    "adjusted_confidence": round(max(0.0, adjusted), 4),
                    "penalties": penalties,
                    "hard_rejected": hard_rejected,
                    "rejection_reason": "max_consecutive_source_use"
                    if hard_rejected
                    else "",
                }
            )
        ranked.sort(
            key=lambda row: (
                row["hard_rejected"],
                -row["adjusted_confidence"],
                row["asset_id"],
            )
        )
        selected = next(
            (
                row
                for row in ranked
                if not row["hard_rejected"]
                and row["adjusted_confidence"] >= min_confidence
            ),
            None,
        )
        if selected is None:
            missing_count += 1
            assignments.append(
                {
                    "shot_id": shot["shot_id"],
                    "status": "missing_material",
                    "selected": None,
                    "ranked_candidates": ranked,
                    "human_confirmed": False,
                    "automatic_fallback": False,
                }
            )
            consecutive_source = ""
            consecutive_count = 0
            previous_context = {}
            continue

        source_key = selected["source_media_id"] or selected["asset_id"]
        consecutive_count = (
            consecutive_count + 1 if source_key == consecutive_source else 1
        )
        consecutive_source = source_key
        previous_context = {
            "room_id": selected["room_id"],
            "composition_id": selected["composition_id"],
        }
        assignments.append(
            {
                "shot_id": shot["shot_id"],
                "status": "candidate_selected",
                "selected": selected,
                "ranked_candidates": ranked,
                "copy_instruction": {
                    "operation": "copy",
                    "source": selected["path"],
                    "target_relative": (
                        f"material/{shot['shot_id']}/{Path(selected['path']).name}"
                    ),
                    "executed": False,
                },
                "human_confirmed": False,
                "automatic_fallback": False,
            }
        )

    plan = {
        "schema": MATERIAL_MATCH_PLAN_SCHEMA,
        "inputs": {
            "reference_recipe_sha256": _payload_sha256(recipe),
            "material_candidates_sha256": _payload_sha256(candidates_payload),
        },
        "selection_policy": {
            "min_confidence": min_confidence,
            "max_consecutive_uses": max_consecutive_uses,
            "repeated_context_penalty": repeated_context_penalty,
            "score_owner": "upstream_index_or_model",
            "automatic_fallback": False,
        },
        "assignments": assignments,
        "readiness": {
            "status": "needs_material"
            if missing_count
            else "awaiting_human_confirmation",
            "shot_count": len(assignments),
            "missing_material_count": missing_count,
            "required_before_render": [
                "fill_missing_material",
                "confirm_each_assignment",
            ]
            if missing_count
            else ["confirm_each_assignment"],
        },
        "execution_boundary": {
            "candidate_only": True,
            "files_copied": 0,
            "files_moved": 0,
            "ffmpeg_executed": False,
            "asr_executed": False,
            "online_model_executed": False,
        },
    }
    plan["plan_sha256"] = _payload_sha256(plan)
    return plan


def build_voice_timing_plan(
    recipe_source: dict[str, Any] | str | Path,
    measurements_source: dict[str, Any] | str | Path,
    *,
    tolerance_s: float = 0.25,
    tolerance_ratio: float = 0.08,
) -> dict[str, Any]:
    if tolerance_s < 0 or not 0 <= tolerance_ratio <= 1:
        raise ContractError("voice timing tolerances are invalid")
    recipe = normalize_reference_recipe(recipe_source)
    measurements = _read_object(measurements_source, "voice measurements")
    if measurements.get("schema") != VOICE_MEASUREMENTS_SCHEMA:
        raise ContractError(f"voice measurements must use {VOICE_MEASUREMENTS_SCHEMA}")
    by_shot = _measurement_map(measurements)

    rows: list[dict[str, Any]] = []
    review_count = 0
    missing_count = 0
    for shot in recipe["shots"]:
        measurement = by_shot.get(shot["shot_id"])
        if measurement is None:
            missing_count += 1
            rows.append(
                {
                    "shot_id": shot["shot_id"],
                    "status": "missing_voice_measurement",
                    "reference_duration_s": shot["duration_s"],
                    "timeline_duration_s": shot["duration_s"],
                    "timing_basis": "reference_duration",
                    "human_confirmed": False,
                }
            )
            continue
        measured = measurement["measured_duration_s"]
        difference = round(measured - shot["duration_s"], 3)
        ratio = abs(difference) / shot["duration_s"] if shot["duration_s"] else 0.0
        needs_review = abs(difference) > tolerance_s or ratio > tolerance_ratio
        review_count += int(needs_review)
        rows.append(
            {
                "shot_id": shot["shot_id"],
                "status": "needs_timing_review" if needs_review else "within_tolerance",
                "audio_path": measurement["audio_path"],
                "reference_duration_s": shot["duration_s"],
                "measured_voice_duration_s": measured,
                "duration_difference_s": difference,
                "duration_difference_ratio": round(ratio, 4),
                "timeline_duration_s": measured,
                "timing_basis": "measured_voice_duration",
                "human_confirmed": False,
            }
        )

    plan = {
        "schema": VOICE_TIMING_PLAN_SCHEMA,
        "inputs": {
            "reference_recipe_sha256": _payload_sha256(recipe),
            "voice_measurements_sha256": _payload_sha256(measurements),
        },
        "tolerance": {
            "seconds": tolerance_s,
            "ratio": tolerance_ratio,
        },
        "shots": rows,
        "readiness": {
            "status": "needs_voice_measurements"
            if missing_count
            else (
                "needs_timing_review" if review_count else "awaiting_human_confirmation"
            ),
            "missing_measurement_count": missing_count,
            "timing_review_count": review_count,
        },
        "execution_boundary": {
            "tts_executed": False,
            "audio_concatenated": False,
            "ffmpeg_executed": False,
            "candidate_only": True,
        },
    }
    plan["plan_sha256"] = _payload_sha256(plan)
    return plan


def write_contract(payload: dict[str, Any], output: str | Path) -> Path:
    path = Path(output).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return path


def material_candidate_map(
    payload: dict[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    """Expose the existing candidate normalization for downstream adapters."""

    if payload.get("schema") != MATERIAL_CANDIDATES_SCHEMA:
        raise ContractError(
            f"material candidates must use {MATERIAL_CANDIDATES_SCHEMA}"
        )
    return _candidate_map(payload)


def _candidate_map(payload: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    rows = payload.get("shots")
    if not isinstance(rows, list):
        raise ContractError("material candidates shots must be a list")
    result: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ContractError("material candidate shot must be an object")
        shot_id = str(row.get("shot_id") or "").strip()
        raw_candidates = row.get("candidates")
        if not shot_id or not isinstance(raw_candidates, list):
            raise ContractError(
                "material candidate shot is missing shot_id or candidates"
            )
        if shot_id in result:
            raise ContractError(f"duplicate material candidate shot: {shot_id}")
        result[shot_id] = []
        for candidate in raw_candidates:
            if not isinstance(candidate, dict):
                raise ContractError(f"{shot_id} candidate must be an object")
            asset_id = str(candidate.get("asset_id") or "").strip()
            path = str(candidate.get("path") or "").strip()
            confidence = _number(
                candidate.get("confidence"), f"{shot_id}.{asset_id}.confidence"
            )
            if not asset_id or not path or not 0 <= confidence <= 1:
                raise ContractError(
                    f"{shot_id} candidate identity or confidence is invalid"
                )
            components = candidate.get("score_components") or {}
            if not isinstance(components, dict):
                raise ContractError(
                    f"{shot_id}.{asset_id}.score_components must be an object"
                )
            result[shot_id].append(
                {
                    "asset_id": asset_id,
                    "path": path,
                    "confidence": round(confidence, 4),
                    "score_components": components,
                    "source_media_id": str(
                        candidate.get("source_media_id") or ""
                    ).strip(),
                    "room_id": str(candidate.get("room_id") or "").strip(),
                    "composition_id": str(
                        candidate.get("composition_id") or ""
                    ).strip(),
                    "reason": str(candidate.get("reason") or "").strip(),
                }
            )
    return result


def _measurement_map(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows = payload.get("shots")
    if not isinstance(rows, list):
        raise ContractError("voice measurements shots must be a list")
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ContractError("voice measurement must be an object")
        shot_id = str(row.get("shot_id") or "").strip()
        audio_path = str(row.get("audio_path") or "").strip()
        if not shot_id or not audio_path or shot_id in result:
            raise ContractError("voice measurement shot ids and paths must be valid")
        result[shot_id] = {
            "audio_path": audio_path,
            "measured_duration_s": round(
                _number(
                    row.get("measured_duration_s"), f"{shot_id}.measured_duration_s"
                ),
                3,
            ),
        }
    return result


def _read_object(source: dict[str, Any] | str | Path, label: str) -> dict[str, Any]:
    if isinstance(source, dict):
        return json.loads(json.dumps(source))
    path = Path(source).expanduser().resolve()
    if not path.is_file():
        raise ContractError(f"{label} not found: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"{label} is unreadable: {exc}") from exc
    if not isinstance(payload, dict):
        raise ContractError(f"{label} must be a JSON object")
    return payload


def _number(value: Any, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{label} must be a number") from exc
    if not math.isfinite(number) or number < 0:
        raise ContractError(f"{label} must not be negative")
    return number


def _payload_sha256(payload: dict[str, Any]) -> str:
    value = dict(payload)
    value.pop("plan_sha256", None)
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
