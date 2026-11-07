from __future__ import annotations

import math
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
from .audio_edit_quality import resolve_loudness
from .execution_receipts import FFMPEG_OUTLET_ID
from .generation_contracts import (
    SHOT_GENERATION_UNIT_SCHEMA,
    validate_shot_generation_unit,
)

VISUAL_PRODUCTION_PLAN_SCHEMA = "video_creation_pipeline.visual_production_plan.v1"
AUDIO_MASTERING_PLAN_SCHEMA = "video_creation_pipeline.audio_mastering_plan.v1"

FULL_VIDEO_PRODUCTION_REPO = "waker240/FullVideoProductionSkill"
FULL_VIDEO_PRODUCTION_COMMIT = "f3b13fb590c775f66b7d7a43834033545502d302"

ARGUMENT_ROLES = {"neutral", "system", "tension", "insight"}
EFFORT_MODES = {"efficient", "apex"}
LAYER_IDS = ("canvas", "artifact", "cinema")
INFORMATION_OVERLAY_ROLES = (
    "structure",
    "relationship",
    "sequence",
    "comparison",
    "number",
    "evidence",
)
PEER_LAYOUT_CANDIDATES = ("vertical_list", "horizontal_row", "grid")
PREFERRED_PERSON_WINDOW_CORNERS = ("top_left", "top_right")
EXCEPTION_PERSON_WINDOW_CORNERS = ("bottom_left", "bottom_right")
HUMAN_COMPOSITION_REVIEW_DIMENSIONS = (
    "primary_information",
    "space_yield",
    "visual_balance",
)


def build_visual_production_plan(
    unit_path: str | Path,
    *,
    argument_role: str = "neutral",
    effort_mode: str = "efficient",
    artifact_layer: bool = False,
    cinema_layer: bool = True,
    emphasis_phrases: list[str] | None = None,
    decomposition_adoption_path: str | Path | None = None,
) -> dict[str, Any]:
    source_path = Path(unit_path).expanduser().resolve()
    unit = read_json_object(source_path, "shot generation unit")
    validate_shot_generation_unit(unit)
    if unit.get("schema") != SHOT_GENERATION_UNIT_SCHEMA:
        raise ContractError("visual production plan requires a current shot unit")

    role = _choice(argument_role, ARGUMENT_ROLES, "argument role")
    mode = _choice(effort_mode, EFFORT_MODES, "effort mode")
    phrases = _phrases(emphasis_phrases or [])
    plan: dict[str, Any] = {
        "schema": VISUAL_PRODUCTION_PLAN_SCHEMA,
        "source_unit": artifact_reference(source_path, "shot generation unit"),
        "source_unit_sha256": unit["unit_sha256"],
        "scene_id": unit["scene_id"],
        "decomposition_adoption": _decomposition_adoption_reference(
            decomposition_adoption_path
        ),
        "provenance": _reuse_provenance(),
        "argument_structure": {
            "role": role,
            "subtitle": {
                "mode": "argument_coded",
                "emphasis_phrases": phrases,
                "phrase_level_data_required": True,
                "frame_alignment_required": True,
                "runtime_srt_fetch": False,
                "verbatim_narration_as_visual_default": False,
            },
        },
        "effort_protocol": {
            "mode": mode,
            "divergent_concepts_required": 3 if mode == "apex" else 1,
            "apex_operation_limit": 1 if mode == "apex" else 0,
            "critic_pass_required": True,
        },
        "layers": [
            {
                "id": "canvas",
                "enabled": True,
                "owner": "remotion_or_hyperframes",
                "intent": "code_first_primary_composition",
            },
            {
                "id": "artifact",
                "enabled": _boolean(artifact_layer, "artifact layer"),
                "owner": "existing_provider_gateway",
                "intent": "separately_generated_inspectable_assets",
            },
            {
                "id": "cinema",
                "enabled": _boolean(cinema_layer, "cinema layer"),
                "owner": "remotion_or_hyperframes",
                "intent": "restrained_post_composition_effects",
            },
        ],
        "complementarity_gate": {
            "silent_test_required": True,
            "audio_only_test_required": True,
            "visual_must_advance_argument": True,
            "narration_restatement_allowed": False,
        },
        "reading_priority_contract": {
            "max_simultaneous_primary_targets": 1,
            "subtitle_role": "complete_spoken_content",
            "information_overlay_roles": list(INFORMATION_OVERLAY_ROLES),
            "information_overlay_full_sentence_restatement_allowed": False,
            "max_highlights_per_subtitle": 1,
            "highlight_ratio_min": 0.05,
            "highlight_ratio_max": 0.15,
        },
        "vertical_layout_contract": {
            "profile": "vertical_social_1080x1920_v1",
            "canvas": {"width": 1080, "height": 1920},
            "zones": {
                "main_information": {
                    "x": 0.0889,
                    "y": 0.1146,
                    "width": 0.6889,
                    "height": 0.526,
                },
                "subtitle": {
                    "x": 0.1111,
                    "y": 0.6458,
                    "width": 0.6481,
                    "height": 0.0896,
                },
                "disclaimer": {
                    "x": 0.0889,
                    "y": 0.7474,
                    "width": 0.6889,
                    "height": 0.0443,
                },
                "platform_controls": {
                    "x": 0.7778,
                    "y": 0.0,
                    "width": 0.2222,
                    "height": 1.0,
                },
            },
            "person_region_required_when_visible": True,
            "information_region_required_when_visible": True,
            "protected_region_collision_allowed": False,
        },
        "composition_layout_policy": {
            "semantic_purpose_required_per_cue": True,
            "peer_item_comparison_threshold": 3,
            "required_peer_layout_candidates": list(PEER_LAYOUT_CANDIDATES),
            "person_window_is_secondary_when_information_dominates": True,
            "person_window_center_default_allowed": False,
            "preferred_person_window_corner_candidates": list(
                PREFERRED_PERSON_WINDOW_CORNERS
            ),
            "exception_person_window_corner_candidates": list(
                EXCEPTION_PERSON_WINDOW_CORNERS
            ),
            "lower_corner_use": "exception_only_when_both_top_corners_unavailable",
            "lower_corner_exception_reason_required": True,
            "person_window_placement_reason_required": True,
            "automatic_collision_check_is_sufficient": False,
            "human_composition_review_required": True,
            "human_review_dimensions": list(HUMAN_COMPOSITION_REVIEW_DIMENSIONS),
        },
        "execution_boundary": {
            "status": "candidate_only",
            "network_execution_performed": False,
            "render_execution_performed": False,
            "provider_fallback_performed": False,
            "published": False,
            "source_code_copied_from_upstream": False,
        },
    }
    plan["plan_sha256"] = payload_sha256(plan, "plan_sha256")
    validate_visual_production_plan(plan)
    return plan


def validate_visual_production_plan(plan: dict[str, Any]) -> None:
    if plan.get("schema") != VISUAL_PRODUCTION_PLAN_SCHEMA:
        raise ContractError("unsupported visual production plan schema")
    if plan.get("plan_sha256") != payload_sha256(plan, "plan_sha256"):
        raise ContractError("visual production plan integrity check failed")

    unit_path = validate_artifact_reference(
        _object(plan.get("source_unit")), "shot generation unit"
    )
    unit = read_json_object(unit_path, "shot generation unit")
    validate_shot_generation_unit(unit)
    if plan.get("source_unit_sha256") != unit.get("unit_sha256") or plan.get(
        "scene_id"
    ) != unit.get("scene_id"):
        raise ContractError("visual production plan drifted from its shot unit")
    _validate_decomposition_adoption_reference(plan.get("decomposition_adoption"))
    _validate_reuse_provenance(plan.get("provenance"))

    argument = _object(plan.get("argument_structure"))
    _choice(argument.get("role"), ARGUMENT_ROLES, "argument role")
    subtitle = _object(argument.get("subtitle"))
    if (
        subtitle.get("mode") != "argument_coded"
        or subtitle.get("phrase_level_data_required") is not True
        or subtitle.get("frame_alignment_required") is not True
        or subtitle.get("runtime_srt_fetch") is not False
        or subtitle.get("verbatim_narration_as_visual_default") is not False
    ):
        raise ContractError("visual production subtitle contract is invalid")
    _phrases(subtitle.get("emphasis_phrases"))

    effort = _object(plan.get("effort_protocol"))
    mode = _choice(effort.get("mode"), EFFORT_MODES, "effort mode")
    if (
        effort.get("divergent_concepts_required") != (3 if mode == "apex" else 1)
        or effort.get("apex_operation_limit") != (1 if mode == "apex" else 0)
        or effort.get("critic_pass_required") is not True
    ):
        raise ContractError("visual production effort protocol is invalid")

    layers = plan.get("layers")
    if not isinstance(layers, list) or [
        row.get("id") if isinstance(row, dict) else None for row in layers
    ] != list(LAYER_IDS):
        raise ContractError("visual production layers must use the locked layer order")
    expected_owners = {
        "canvas": "remotion_or_hyperframes",
        "artifact": "existing_provider_gateway",
        "cinema": "remotion_or_hyperframes",
    }
    for layer in layers:
        layer_id = str(layer["id"])
        if not isinstance(layer.get("enabled"), bool):
            raise ContractError("visual production layer enabled flag must be boolean")
        if layer.get("owner") != expected_owners[layer_id]:
            raise ContractError("visual production layer owner is invalid")
        if not str(layer.get("intent") or "").strip():
            raise ContractError("visual production layer intent is required")
    if layers[0]["enabled"] is not True:
        raise ContractError("visual production canvas layer is mandatory")

    gate = _object(plan.get("complementarity_gate"))
    if (
        gate.get("silent_test_required") is not True
        or gate.get("audio_only_test_required") is not True
        or gate.get("visual_must_advance_argument") is not True
        or gate.get("narration_restatement_allowed") is not False
    ):
        raise ContractError("visual production complementarity gate is invalid")
    reading = _object(plan.get("reading_priority_contract"))
    if reading != {
        "max_simultaneous_primary_targets": 1,
        "subtitle_role": "complete_spoken_content",
        "information_overlay_roles": list(INFORMATION_OVERLAY_ROLES),
        "information_overlay_full_sentence_restatement_allowed": False,
        "max_highlights_per_subtitle": 1,
        "highlight_ratio_min": 0.05,
        "highlight_ratio_max": 0.15,
    }:
        raise ContractError("visual production reading priority contract is invalid")
    layout = _object(plan.get("vertical_layout_contract"))
    expected_layout = {
        "profile": "vertical_social_1080x1920_v1",
        "canvas": {"width": 1080, "height": 1920},
        "zones": {
            "main_information": {
                "x": 0.0889,
                "y": 0.1146,
                "width": 0.6889,
                "height": 0.526,
            },
            "subtitle": {
                "x": 0.1111,
                "y": 0.6458,
                "width": 0.6481,
                "height": 0.0896,
            },
            "disclaimer": {
                "x": 0.0889,
                "y": 0.7474,
                "width": 0.6889,
                "height": 0.0443,
            },
            "platform_controls": {
                "x": 0.7778,
                "y": 0.0,
                "width": 0.2222,
                "height": 1.0,
            },
        },
        "person_region_required_when_visible": True,
        "information_region_required_when_visible": True,
        "protected_region_collision_allowed": False,
    }
    if layout != expected_layout:
        raise ContractError("visual production vertical layout contract is invalid")
    composition_policy = _object(plan.get("composition_layout_policy"))
    if composition_policy != {
        "semantic_purpose_required_per_cue": True,
        "peer_item_comparison_threshold": 3,
        "required_peer_layout_candidates": list(PEER_LAYOUT_CANDIDATES),
        "person_window_is_secondary_when_information_dominates": True,
        "person_window_center_default_allowed": False,
        "preferred_person_window_corner_candidates": list(
            PREFERRED_PERSON_WINDOW_CORNERS
        ),
        "exception_person_window_corner_candidates": list(
            EXCEPTION_PERSON_WINDOW_CORNERS
        ),
        "lower_corner_use": "exception_only_when_both_top_corners_unavailable",
        "lower_corner_exception_reason_required": True,
        "person_window_placement_reason_required": True,
        "automatic_collision_check_is_sufficient": False,
        "human_composition_review_required": True,
        "human_review_dimensions": list(HUMAN_COMPOSITION_REVIEW_DIMENSIONS),
    }:
        raise ContractError("visual production composition layout policy is invalid")
    boundary = _object(plan.get("execution_boundary"))
    if (
        boundary.get("status") != "candidate_only"
        or boundary.get("network_execution_performed") is not False
        or boundary.get("render_execution_performed") is not False
        or boundary.get("provider_fallback_performed") is not False
        or boundary.get("published") is not False
        or boundary.get("source_code_copied_from_upstream") is not False
    ):
        raise ContractError("visual production execution boundary is invalid")


def build_audio_mastering_plan(
    source_audio_path: str | Path,
    output_audio_path: str | Path,
    *,
    target_lufs: float | None = None,
    delivery_profile: str | None = None,
    target_true_peak_db: float = -1.0,
    target_lra_lu: float = 5.0,
    highpass_hz: float = 80.0,
    limiter: float = 0.7,
    sample_rate: int = 48000,
    sample_width_bits: int = 24,
) -> dict[str, Any]:
    source_path = Path(source_audio_path).expanduser().resolve()
    output_path = Path(output_audio_path).expanduser().resolve()
    if source_path == output_path:
        raise ContractError("audio mastering output cannot overwrite its source")
    if output_path.suffix.lower() != ".wav":
        raise ContractError("audio mastering output must be a WAV file")

    delivery_profile, target_lufs = resolve_loudness(delivery_profile, target_lufs)
    policy = _audio_policy(
        target_lufs=target_lufs,
        target_true_peak_db=target_true_peak_db,
        target_lra_lu=target_lra_lu,
        highpass_hz=highpass_hz,
        limiter=limiter,
        sample_rate=sample_rate,
        sample_width_bits=sample_width_bits,
    )
    plan: dict[str, Any] = {
        "schema": AUDIO_MASTERING_PLAN_SCHEMA,
        "delivery_profile": delivery_profile,
        "source_audio": artifact_reference(source_path, "source audio"),
        "output": {
            "path": str(output_path),
            "container": "wav",
            "codec": _pcm_codec(policy["sample_width_bits"]),
            "sample_rate": policy["sample_rate"],
            "sample_width_bits": policy["sample_width_bits"],
        },
        "provenance": _reuse_provenance(),
        "policy": policy,
        "passes": [
            {
                "id": "measure",
                "order": 1,
                "filter_chain": ["highpass", "alimiter", "loudnorm"],
                "loudnorm_mode": "measure_json",
                "modifies_audio": False,
            },
            {
                "id": "apply",
                "order": 2,
                "filter_chain": ["highpass", "alimiter", "loudnorm"],
                "loudnorm_mode": "linear_with_measured_values",
                "required_measurements": [
                    "input_i",
                    "input_tp",
                    "input_lra",
                    "input_thresh",
                    "target_offset",
                ],
                "modifies_audio": True,
            },
        ],
        "ffmpeg_handoff": {
            "outlet_id": FFMPEG_OUTLET_ID,
            "operation": "audio_mastering",
            "command_generated": False,
            "executed": False,
            "second_ffmpeg_orchestrator_created": False,
        },
        "execution_boundary": {
            "status": "ready_for_single_ffmpeg_outlet",
            "network_execution_performed": False,
            "source_audio_modified": False,
            "automatic_publish": False,
        },
    }
    plan["plan_sha256"] = payload_sha256(plan, "plan_sha256")
    validate_audio_mastering_plan(plan)
    return plan


def validate_audio_mastering_plan(plan: dict[str, Any]) -> None:
    if plan.get("schema") != AUDIO_MASTERING_PLAN_SCHEMA:
        raise ContractError("unsupported audio mastering plan schema")
    if plan.get("plan_sha256") != payload_sha256(plan, "plan_sha256"):
        raise ContractError("audio mastering plan integrity check failed")
    source_path = validate_artifact_reference(
        _object(plan.get("source_audio")), "source audio"
    )
    output = _object(plan.get("output"))
    output_path = Path(str(output.get("path") or "")).expanduser().resolve()
    if source_path == output_path or output_path.suffix.lower() != ".wav":
        raise ContractError("audio mastering output path is invalid")

    policy = _audio_policy(**_policy_kwargs(plan.get("policy")))
    if "delivery_profile" in plan:
        resolve_loudness(plan["delivery_profile"],policy["target_lufs"])
    if (
        output.get("container") != "wav"
        or output.get("codec") != _pcm_codec(policy["sample_width_bits"])
        or output.get("sample_rate") != policy["sample_rate"]
        or output.get("sample_width_bits") != policy["sample_width_bits"]
    ):
        raise ContractError("audio mastering output differs from its policy")
    _validate_reuse_provenance(plan.get("provenance"))

    passes = plan.get("passes")
    if not isinstance(passes, list) or len(passes) != 2:
        raise ContractError("audio mastering requires exactly two passes")
    measure, apply = passes
    if (
        not isinstance(measure, dict)
        or measure.get("id") != "measure"
        or measure.get("order") != 1
        or measure.get("filter_chain") != ["highpass", "alimiter", "loudnorm"]
        or measure.get("loudnorm_mode") != "measure_json"
        or measure.get("modifies_audio") is not False
    ):
        raise ContractError("audio mastering measurement pass is invalid")
    required_measurements = [
        "input_i",
        "input_tp",
        "input_lra",
        "input_thresh",
        "target_offset",
    ]
    if (
        not isinstance(apply, dict)
        or apply.get("id") != "apply"
        or apply.get("order") != 2
        or apply.get("filter_chain") != ["highpass", "alimiter", "loudnorm"]
        or apply.get("loudnorm_mode") != "linear_with_measured_values"
        or apply.get("required_measurements") != required_measurements
        or apply.get("modifies_audio") is not True
    ):
        raise ContractError("audio mastering application pass is invalid")

    handoff = _object(plan.get("ffmpeg_handoff"))
    if (
        handoff.get("outlet_id") != FFMPEG_OUTLET_ID
        or handoff.get("operation") != "audio_mastering"
        or handoff.get("command_generated") is not False
        or handoff.get("executed") is not False
        or handoff.get("second_ffmpeg_orchestrator_created") is not False
    ):
        raise ContractError("audio mastering FFmpeg handoff is invalid")
    boundary = _object(plan.get("execution_boundary"))
    if (
        boundary.get("status") != "ready_for_single_ffmpeg_outlet"
        or boundary.get("network_execution_performed") is not False
        or boundary.get("source_audio_modified") is not False
        or boundary.get("automatic_publish") is not False
    ):
        raise ContractError("audio mastering execution boundary is invalid")


def write_production_strategy_contract(
    payload: dict[str, Any], output_path: str | Path
) -> Path:
    schema = payload.get("schema")
    if schema == VISUAL_PRODUCTION_PLAN_SCHEMA:
        validate_visual_production_plan(payload)
    elif schema == AUDIO_MASTERING_PLAN_SCHEMA:
        validate_audio_mastering_plan(payload)
    else:
        raise ContractError("unsupported production strategy contract")
    return atomic_write_json(payload, output_path)


def _audio_policy(
    *,
    target_lufs: Any,
    target_true_peak_db: Any,
    target_lra_lu: Any,
    highpass_hz: Any,
    limiter: Any,
    sample_rate: Any,
    sample_width_bits: Any,
) -> dict[str, Any]:
    integrated = _finite_number(target_lufs, "target LUFS")
    true_peak = _finite_number(target_true_peak_db, "target true peak")
    lra = _finite_number(target_lra_lu, "target LRA")
    highpass = _finite_number(highpass_hz, "highpass")
    limit = _finite_number(limiter, "limiter")
    rate = _integer(sample_rate, "sample rate")
    width = _integer(sample_width_bits, "sample width")
    if not -70.0 <= integrated <= -5.0:
        raise ContractError("target LUFS must be between -70 and -5")
    if not -9.0 <= true_peak <= 0.0:
        raise ContractError("target true peak must be between -9 and 0 dB")
    if not 1.0 <= lra <= 50.0:
        raise ContractError("target LRA must be between 1 and 50 LU")
    if not 20.0 <= highpass <= 300.0:
        raise ContractError("highpass must be between 20 and 300 Hz")
    if not 0.0 < limit <= 1.0:
        raise ContractError("limiter must be greater than 0 and at most 1")
    if rate not in {44100, 48000, 96000}:
        raise ContractError("sample rate must be 44100, 48000, or 96000")
    if width not in {16, 24, 32}:
        raise ContractError("sample width must be 16, 24, or 32 bits")
    return {
        "target_lufs": integrated,
        "target_true_peak_db": true_peak,
        "target_lra_lu": lra,
        "highpass_hz": highpass,
        "limiter": limit,
        "sample_rate": rate,
        "sample_width_bits": width,
        "compression_enabled": False,
        "denoise_enabled": False,
    }


def _policy_kwargs(value: Any) -> dict[str, Any]:
    policy = _object(value)
    if policy.get("compression_enabled") is not False:
        raise ContractError("audio mastering cannot silently enable compression")
    if policy.get("denoise_enabled") is not False:
        raise ContractError("audio mastering cannot silently enable denoise")
    return {
        "target_lufs": policy.get("target_lufs"),
        "target_true_peak_db": policy.get("target_true_peak_db"),
        "target_lra_lu": policy.get("target_lra_lu"),
        "highpass_hz": policy.get("highpass_hz"),
        "limiter": policy.get("limiter"),
        "sample_rate": policy.get("sample_rate"),
        "sample_width_bits": policy.get("sample_width_bits"),
    }


def _reuse_provenance() -> dict[str, Any]:
    return {
        "source_repo": FULL_VIDEO_PRODUCTION_REPO,
        "source_commit": FULL_VIDEO_PRODUCTION_COMMIT,
        "reuse_mode": "conceptual_adaptation_no_source_copy",
        "upstream_license_status": "repository_level_license_not_found",
    }


def _validate_reuse_provenance(value: Any) -> None:
    if _object(value) != _reuse_provenance():
        raise ContractError("production strategy reuse provenance is invalid")


def _decomposition_adoption_reference(
    source: str | Path | None,
) -> dict[str, Any] | None:
    if source is None:
        return None
    from .decomposition_adoption import validate_decomposition_adoption

    path = Path(source).expanduser().resolve()
    payload = read_json_object(path, "decomposition adoption")
    validate_decomposition_adoption(payload)
    return artifact_reference(path, "decomposition adoption")


def _validate_decomposition_adoption_reference(value: Any) -> None:
    if value is None:
        return
    if not isinstance(value, dict):
        raise ContractError(
            "visual production decomposition adoption must be an artifact"
        )
    from .decomposition_adoption import validate_decomposition_adoption

    path = validate_artifact_reference(value, "decomposition adoption")
    payload = read_json_object(path, "decomposition adoption")
    validate_decomposition_adoption(payload)


def _phrases(value: Any) -> list[str]:
    if not isinstance(value, list):
        raise ContractError("emphasis phrases must be a list")
    result: list[str] = []
    seen: set[str] = set()
    for raw in value:
        phrase = str(raw or "").strip()
        if not phrase:
            raise ContractError("emphasis phrase cannot be empty")
        if len(phrase) > 120:
            raise ContractError("emphasis phrase is too long")
        if phrase in seen:
            raise ContractError("emphasis phrase is duplicated")
        seen.add(phrase)
        result.append(phrase)
    return result


def _choice(value: Any, allowed: set[str], label: str) -> str:
    normalized = str(value or "").strip().lower()
    if normalized not in allowed:
        raise ContractError(f"unsupported {label}: {value}")
    return normalized


def _finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise ContractError(f"{label} must be a finite number")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{label} must be a finite number") from exc
    if not math.isfinite(result):
        raise ContractError(f"{label} must be a finite number")
    return result


def _integer(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise ContractError(f"{label} must be an integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{label} must be an integer") from exc
    if float(result) != float(value):
        raise ContractError(f"{label} must be an integer")
    return result


def _boolean(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise ContractError(f"{label} must be boolean")
    return value


def _pcm_codec(sample_width_bits: int) -> str:
    return {16: "pcm_s16le", 24: "pcm_s24le", 32: "pcm_s32le"}[sample_width_bits]


def _object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}
