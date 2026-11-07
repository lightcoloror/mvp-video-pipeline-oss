from __future__ import annotations

import json
import math
import shutil
from pathlib import Path
from typing import Any
from uuid import uuid4

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

from .contract_io import (
    artifact_reference,
    atomic_write_json,
    payload_sha256,
    read_json_object,
    validate_artifact_reference,
)
from .errors import ContractError
from .execution_receipts import (
    FFMPEG_OUTLET_ID,
    execute_ffmpeg_with_receipt,
    validate_ffmpeg_execution_receipt,
)
from .production_strategy import validate_audio_mastering_plan

AUDIO_MASTERING_MEASUREMENTS_SCHEMA = (
    "video_creation_pipeline.audio_mastering_measurements.v1"
)
LEGACY_AUDIO_MASTERING_EXECUTION_RECEIPT_SCHEMA = (
    "video_creation_pipeline.audio_mastering_execution_receipt.v1"
)
AUDIO_MASTERING_EXECUTION_RECEIPT_SCHEMA = (
    "video_creation_pipeline.audio_mastering_execution_receipt.v2"
)
AUDIO_MASTERING_OUTPUT_VERIFICATION_SCHEMA = (
    "video_creation_pipeline.audio_mastering_output_verification.v1"
)
OUTPUT_VERIFICATION_TOLERANCES = {
    "integrated_lufs_tolerance_lu": 0.6,
    "true_peak_ceiling_tolerance_db": 0.1,
    "lra_ceiling_tolerance_lu": 1.0,
}
_MEASUREMENT_FIELDS = (
    "input_i",
    "input_tp",
    "input_lra",
    "input_thresh",
    "target_offset",
)


def execute_audio_mastering(
    plan_path: str | Path,
    work_dir: str | Path,
    receipt_path: str | Path,
    *,
    timeout_s: float = 180.0,
) -> dict[str, Any]:
    plan_file = Path(plan_path).expanduser().resolve()
    plan = read_json_object(plan_file, "audio mastering plan")
    validate_audio_mastering_plan(plan)
    source_path = validate_artifact_reference(
        _object(plan.get("source_audio")), "source audio"
    )
    output_path = Path(str(_object(plan.get("output")).get("path"))).resolve()
    root = Path(work_dir).expanduser().resolve()
    final_receipt_path = Path(receipt_path).expanduser().resolve()
    measurement_log_path = root / "loudnorm-measurement.log"
    measurement_path = root / "loudnorm-measurements.json"
    measurement_receipt_path = root / "loudnorm-measurement-receipt.json"
    apply_receipt_path = root / "loudnorm-apply-receipt.json"
    staged_output_path = root / "mastered-audio-staging.wav"
    output_verification_log_path = root / "loudnorm-output-verification.log"
    output_verification_path = root / "loudnorm-output-verification.json"
    output_verification_receipt_path = (
        root / "loudnorm-output-verification-receipt.json"
    )
    _validate_output_paths(
        source_path,
        plan_file,
        output_path,
        final_receipt_path,
        measurement_log_path,
        measurement_path,
        measurement_receipt_path,
        apply_receipt_path,
        staged_output_path,
        output_verification_log_path,
        output_verification_path,
        output_verification_receipt_path,
    )
    root.mkdir(parents=True, exist_ok=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    measurement_command = build_measurement_command(source_path, plan["policy"])
    execute_ffmpeg_with_receipt(
        "audio_mastering",
        measurement_command,
        [
            {"role": "source_audio", "path": source_path},
            {"role": "audio_mastering_plan", "path": plan_file},
        ],
        [{"role": "loudnorm_measurement_log", "path": measurement_log_path}],
        measurement_receipt_path,
        capture_log_path=measurement_log_path,
        timeout_s=timeout_s,
    )
    measurements = build_audio_mastering_measurements(plan_file, measurement_log_path)
    atomic_write_json(measurements, measurement_path)

    apply_command = build_apply_command(
        source_path,
        staged_output_path,
        plan["policy"],
        measurements["measurements"],
        codec=str(plan["output"]["codec"]),
    )
    execute_ffmpeg_with_receipt(
        "audio_mastering",
        apply_command,
        [
            {"role": "source_audio", "path": source_path},
            {"role": "audio_mastering_plan", "path": plan_file},
            {"role": "loudnorm_measurements", "path": measurement_path},
        ],
        [{"role": "mastered_audio_staging", "path": staged_output_path}],
        apply_receipt_path,
        timeout_s=timeout_s,
    )
    output_verification_command = build_output_verification_command(
        staged_output_path, plan["policy"]
    )
    execute_ffmpeg_with_receipt(
        "audio_mastering",
        output_verification_command,
        [
            {"role": "mastered_audio_staging", "path": staged_output_path},
            {"role": "audio_mastering_plan", "path": plan_file},
        ],
        [
            {
                "role": "loudnorm_output_verification_log",
                "path": output_verification_log_path,
            }
        ],
        output_verification_receipt_path,
        capture_log_path=output_verification_log_path,
        timeout_s=timeout_s,
    )
    output_verification = build_audio_mastering_output_verification(
        plan_file,
        staged_output_path,
        output_verification_log_path,
        output_verification_receipt_path,
    )
    atomic_write_json(output_verification, output_verification_path)
    if output_verification["readiness"]["technical_ready"] is not True:
        failures = ", ".join(output_verification["readiness"]["blocking_failures"])
        raise ContractError(
            "mastered audio failed final loudness verification: " + failures
        )
    _atomic_promote(staged_output_path, output_path)

    receipt = build_audio_mastering_execution_receipt(
        plan_file,
        measurement_path,
        measurement_receipt_path,
        apply_receipt_path,
        output_verification_path,
        output_verification_receipt_path,
        output_path,
    )
    atomic_write_json(receipt, final_receipt_path)
    return receipt


def build_audio_mastering_measurements(
    plan_path: str | Path,
    measurement_log_path: str | Path,
) -> dict[str, Any]:
    plan_file = Path(plan_path).expanduser().resolve()
    log_file = Path(measurement_log_path).expanduser().resolve()
    plan = read_json_object(plan_file, "audio mastering plan")
    validate_audio_mastering_plan(plan)
    try:
        log_text = log_file.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise ContractError(f"loudnorm measurement log is unreadable: {exc}") from exc
    payload: dict[str, Any] = {
        "schema": AUDIO_MASTERING_MEASUREMENTS_SCHEMA,
        "source_plan": artifact_reference(plan_file, "audio mastering plan"),
        "source_plan_sha256": plan["plan_sha256"],
        "measurement_log": artifact_reference(log_file, "loudnorm measurement log"),
        "policy": dict(plan["policy"]),
        "measurements": parse_loudnorm_measurement_log(log_text),
        "authority_boundary": {
            "measurement_only": True,
            "source_audio_modified": False,
            "automatic_publish": False,
        },
    }
    payload["measurements_sha256"] = payload_sha256(payload, "measurements_sha256")
    validate_audio_mastering_measurements(payload)
    return payload


def validate_audio_mastering_measurements(payload: dict[str, Any]) -> None:
    if payload.get("schema") != AUDIO_MASTERING_MEASUREMENTS_SCHEMA:
        raise ContractError("unsupported audio mastering measurements schema")
    if payload.get("measurements_sha256") != payload_sha256(
        payload, "measurements_sha256"
    ):
        raise ContractError("audio mastering measurements integrity check failed")
    plan_path = validate_artifact_reference(
        _object(payload.get("source_plan")), "audio mastering plan"
    )
    plan = read_json_object(plan_path, "audio mastering plan")
    validate_audio_mastering_plan(plan)
    if payload.get("source_plan_sha256") != plan.get("plan_sha256"):
        raise ContractError("audio mastering measurements bind a stale plan")
    log_path = validate_artifact_reference(
        _object(payload.get("measurement_log")), "loudnorm measurement log"
    )
    expected = parse_loudnorm_measurement_log(
        log_path.read_text(encoding="utf-8", errors="replace")
    )
    if payload.get("measurements") != expected:
        raise ContractError("audio mastering measurements drifted from the FFmpeg log")
    if payload.get("policy") != plan.get("policy"):
        raise ContractError("audio mastering measurement policy drifted")
    boundary = _object(payload.get("authority_boundary"))
    if boundary != {
        "measurement_only": True,
        "source_audio_modified": False,
        "automatic_publish": False,
    }:
        raise ContractError("audio mastering measurement boundary is invalid")


def build_audio_mastering_output_verification(
    plan_path: str | Path,
    mastered_audio_path: str | Path,
    measurement_log_path: str | Path,
    measurement_receipt_path: str | Path,
) -> dict[str, Any]:
    plan_file = Path(plan_path).expanduser().resolve()
    mastered_audio = Path(mastered_audio_path).expanduser().resolve()
    log_file = Path(measurement_log_path).expanduser().resolve()
    receipt_file = Path(measurement_receipt_path).expanduser().resolve()
    plan = read_json_object(plan_file, "audio mastering plan")
    validate_audio_mastering_plan(plan)
    measurements = parse_loudnorm_measurement_log(
        log_file.read_text(encoding="utf-8", errors="replace")
    )
    evaluation = evaluate_audio_mastering_output_measurements(
        measurements, plan["policy"]
    )
    payload: dict[str, Any] = {
        "schema": AUDIO_MASTERING_OUTPUT_VERIFICATION_SCHEMA,
        "source_plan": artifact_reference(plan_file, "audio mastering plan"),
        "source_plan_sha256": plan["plan_sha256"],
        "mastered_audio": artifact_reference(mastered_audio, "mastered audio staging"),
        "measurement_log": artifact_reference(
            log_file, "loudnorm output verification log"
        ),
        "measurement_pass_receipt": artifact_reference(
            receipt_file, "loudnorm output verification receipt"
        ),
        "policy": dict(plan["policy"]),
        "tolerances": dict(OUTPUT_VERIFICATION_TOLERANCES),
        "measurements": measurements,
        **evaluation,
        "authority_boundary": {
            "measurement_only": True,
            "candidate_only": True,
            "creative_approval_claimed": False,
            "release_eligible": False,
            "automatic_upload": False,
            "automatic_publish": False,
            "single_ffmpeg_outlet": FFMPEG_OUTLET_ID,
        },
    }
    payload["verification_sha256"] = payload_sha256(payload, "verification_sha256")
    validate_audio_mastering_output_verification(payload)
    return payload


def validate_audio_mastering_output_verification(payload: dict[str, Any]) -> None:
    if payload.get("schema") != AUDIO_MASTERING_OUTPUT_VERIFICATION_SCHEMA:
        raise ContractError("unsupported audio mastering output verification schema")
    _validate_schema(
        payload,
        "audio_mastering_output_verification.v1.schema.json",
        "audio mastering output verification",
    )
    if payload.get("verification_sha256") != payload_sha256(
        payload, "verification_sha256"
    ):
        raise ContractError(
            "audio mastering output verification integrity check failed"
        )
    plan_path = validate_artifact_reference(
        _object(payload.get("source_plan")), "audio mastering plan"
    )
    plan = read_json_object(plan_path, "audio mastering plan")
    validate_audio_mastering_plan(plan)
    if payload.get("source_plan_sha256") != plan.get("plan_sha256"):
        raise ContractError("audio mastering output verification binds a stale plan")
    mastered_audio = validate_artifact_reference(
        _object(payload.get("mastered_audio")), "mastered audio staging"
    )
    log_path = validate_artifact_reference(
        _object(payload.get("measurement_log")),
        "loudnorm output verification log",
    )
    receipt_path = validate_artifact_reference(
        _object(payload.get("measurement_pass_receipt")),
        "loudnorm output verification receipt",
    )
    _validate_output_verification_receipt(
        plan_path,
        mastered_audio,
        log_path,
        receipt_path,
        plan["policy"],
    )
    measurements = parse_loudnorm_measurement_log(
        log_path.read_text(encoding="utf-8", errors="replace")
    )
    if payload.get("measurements") != measurements:
        raise ContractError("audio mastering output measurements drifted from the log")
    if payload.get("policy") != plan.get("policy"):
        raise ContractError("audio mastering output verification policy drifted")
    if payload.get("tolerances") != OUTPUT_VERIFICATION_TOLERANCES:
        raise ContractError("audio mastering output verification tolerances drifted")
    evaluation = evaluate_audio_mastering_output_measurements(
        measurements, plan["policy"]
    )
    for field in ("checks", "readiness"):
        if payload.get(field) != evaluation[field]:
            raise ContractError(f"audio mastering output verification {field} drifted")
    if _object(payload.get("authority_boundary")) != {
        "measurement_only": True,
        "candidate_only": True,
        "creative_approval_claimed": False,
        "release_eligible": False,
        "automatic_upload": False,
        "automatic_publish": False,
        "single_ffmpeg_outlet": FFMPEG_OUTLET_ID,
    }:
        raise ContractError("audio mastering output verification boundary is invalid")


def build_audio_mastering_execution_receipt(
    plan_path: str | Path,
    measurements_path: str | Path,
    measurement_receipt_path: str | Path,
    apply_receipt_path: str | Path,
    output_verification_path: str | Path,
    output_verification_receipt_path: str | Path,
    output_audio_path: str | Path,
) -> dict[str, Any]:
    plan_file = Path(plan_path).expanduser().resolve()
    measurements_file = Path(measurements_path).expanduser().resolve()
    measurement_receipt_file = Path(measurement_receipt_path).expanduser().resolve()
    apply_receipt_file = Path(apply_receipt_path).expanduser().resolve()
    output_verification_file = Path(output_verification_path).expanduser().resolve()
    output_verification_receipt_file = (
        Path(output_verification_receipt_path).expanduser().resolve()
    )
    output_path = Path(output_audio_path).expanduser().resolve()
    plan = read_json_object(plan_file, "audio mastering plan")
    validate_audio_mastering_plan(plan)
    measurements = read_json_object(measurements_file, "audio mastering measurements")
    validate_audio_mastering_measurements(measurements)
    output_verification = read_json_object(
        output_verification_file, "audio mastering output verification"
    )
    validate_audio_mastering_output_verification(output_verification)
    if output_verification["readiness"]["technical_ready"] is not True:
        raise ContractError("audio mastering output verification did not pass")
    payload: dict[str, Any] = {
        "schema": AUDIO_MASTERING_EXECUTION_RECEIPT_SCHEMA,
        "status": "completed_technical_candidate",
        "source_plan": artifact_reference(plan_file, "audio mastering plan"),
        "source_plan_sha256": plan["plan_sha256"],
        "measurements": artifact_reference(
            measurements_file, "audio mastering measurements"
        ),
        "measurement_pass_receipt": artifact_reference(
            measurement_receipt_file, "loudnorm measurement receipt"
        ),
        "apply_pass_receipt": artifact_reference(
            apply_receipt_file, "loudnorm apply receipt"
        ),
        "output_verification": artifact_reference(
            output_verification_file, "audio mastering output verification"
        ),
        "output_verification_pass_receipt": artifact_reference(
            output_verification_receipt_file,
            "loudnorm output verification receipt",
        ),
        "output_quality": dict(output_verification["readiness"]),
        "mastered_audio": artifact_reference(output_path, "mastered audio"),
        "policy": dict(plan["policy"]),
        "authority_boundary": {
            "candidate_only": True,
            "technical_processing_completed": True,
            "output_loudness_verified": True,
            "creative_approval_claimed": False,
            "release_eligible": False,
            "automatic_upload": False,
            "automatic_publish": False,
            "single_ffmpeg_outlet": FFMPEG_OUTLET_ID,
        },
    }
    payload["receipt_sha256"] = payload_sha256(payload, "receipt_sha256")
    validate_audio_mastering_execution_receipt(payload)
    return payload


def validate_audio_mastering_execution_receipt(payload: dict[str, Any]) -> None:
    schema = payload.get("schema")
    if schema not in {
        LEGACY_AUDIO_MASTERING_EXECUTION_RECEIPT_SCHEMA,
        AUDIO_MASTERING_EXECUTION_RECEIPT_SCHEMA,
    }:
        raise ContractError("unsupported audio mastering execution receipt schema")
    if schema == AUDIO_MASTERING_EXECUTION_RECEIPT_SCHEMA:
        _validate_schema(
            payload,
            "audio_mastering_execution_receipt.v2.schema.json",
            "audio mastering execution receipt",
        )
    if payload.get("receipt_sha256") != payload_sha256(payload, "receipt_sha256"):
        raise ContractError("audio mastering execution receipt integrity check failed")
    if payload.get("status") != "completed_technical_candidate":
        raise ContractError("audio mastering execution status is invalid")
    plan_path = validate_artifact_reference(
        _object(payload.get("source_plan")), "audio mastering plan"
    )
    plan = read_json_object(plan_path, "audio mastering plan")
    validate_audio_mastering_plan(plan)
    if payload.get("source_plan_sha256") != plan.get("plan_sha256"):
        raise ContractError("audio mastering execution binds a stale plan")
    measurements_path = validate_artifact_reference(
        _object(payload.get("measurements")), "audio mastering measurements"
    )
    measurements = read_json_object(measurements_path, "audio mastering measurements")
    validate_audio_mastering_measurements(measurements)
    if measurements.get("source_plan_sha256") != plan.get("plan_sha256"):
        raise ContractError("audio mastering execution measurements are stale")

    measurement_receipt = _load_ffmpeg_receipt(
        payload.get("measurement_pass_receipt"), "loudnorm measurement receipt"
    )
    apply_receipt = _load_ffmpeg_receipt(
        payload.get("apply_pass_receipt"), "loudnorm apply receipt"
    )
    source_path = validate_artifact_reference(
        _object(plan.get("source_audio")), "source audio"
    )
    output_path = validate_artifact_reference(
        _object(payload.get("mastered_audio")), "mastered audio"
    )
    if output_path != Path(str(plan["output"]["path"])).resolve():
        raise ContractError("mastered audio differs from the plan output")
    measurement_log_path = validate_artifact_reference(
        _object(measurements.get("measurement_log")), "loudnorm measurement log"
    )
    if measurement_receipt.get("command") != build_measurement_command(
        source_path, plan["policy"]
    ):
        raise ContractError("audio mastering measurement command drifted")
    staged_output_path = _receipt_role_path(
        apply_receipt, "outputs", "mastered_audio_staging"
    )
    if apply_receipt.get("command") != build_apply_command(
        source_path,
        staged_output_path,
        plan["policy"],
        measurements["measurements"],
        codec=str(plan["output"]["codec"]),
    ):
        raise ContractError("audio mastering apply command drifted")
    _require_receipt_role(
        measurement_receipt,
        "outputs",
        "loudnorm_measurement_log",
        measurement_log_path,
    )
    _require_receipt_role(
        apply_receipt, "inputs", "loudnorm_measurements", measurements_path
    )
    if artifact_reference(staged_output_path, "mastered audio staging")[
        "sha256"
    ] != _object(payload.get("mastered_audio")).get("sha256"):
        raise ContractError("mastered audio differs from the FFmpeg staging output")
    if payload.get("policy") != plan.get("policy"):
        raise ContractError("audio mastering execution policy drifted")
    expected_boundary = {
        "candidate_only": True,
        "technical_processing_completed": True,
        "creative_approval_claimed": False,
        "release_eligible": False,
        "automatic_upload": False,
        "automatic_publish": False,
        "single_ffmpeg_outlet": FFMPEG_OUTLET_ID,
    }
    if schema == AUDIO_MASTERING_EXECUTION_RECEIPT_SCHEMA:
        verification_path = validate_artifact_reference(
            _object(payload.get("output_verification")),
            "audio mastering output verification",
        )
        verification = read_json_object(
            verification_path, "audio mastering output verification"
        )
        validate_audio_mastering_output_verification(verification)
        if verification["readiness"]["technical_ready"] is not True:
            raise ContractError("audio mastering output verification did not pass")
        verification_receipt_path = validate_artifact_reference(
            _object(payload.get("output_verification_pass_receipt")),
            "loudnorm output verification receipt",
        )
        embedded_receipt_path = validate_artifact_reference(
            _object(verification.get("measurement_pass_receipt")),
            "loudnorm output verification receipt",
        )
        if verification_receipt_path != embedded_receipt_path:
            raise ContractError(
                "audio mastering output verification receipt binding drifted"
            )
        if _object(verification.get("source_plan")).get("sha256") != _object(
            payload.get("source_plan")
        ).get("sha256") or _object(verification.get("mastered_audio")).get(
            "sha256"
        ) != _object(payload.get("mastered_audio")).get("sha256"):
            raise ContractError("audio mastering output verification binding drifted")
        if payload.get("output_quality") != verification.get("readiness"):
            raise ContractError("audio mastering output quality projection drifted")
        expected_boundary["output_loudness_verified"] = True
    if _object(payload.get("authority_boundary")) != expected_boundary:
        raise ContractError("audio mastering execution boundary is invalid")


def parse_loudnorm_measurement_log(text: str) -> dict[str, float]:
    decoder = json.JSONDecoder()
    candidates: list[dict[str, Any]] = []
    for index, character in enumerate(text):
        if character != "{":
            continue
        try:
            value, _end = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and all(
            field in value for field in _MEASUREMENT_FIELDS
        ):
            candidates.append(value)
    if not candidates:
        raise ContractError("FFmpeg loudnorm measurement JSON was not found")
    source = candidates[-1]
    return {
        field: _finite_number(source[field], f"loudnorm {field}")
        for field in _MEASUREMENT_FIELDS
    }


def evaluate_audio_mastering_output_measurements(
    measurements: dict[str, Any], policy: dict[str, Any]
) -> dict[str, Any]:
    normalized = {
        field: _finite_number(measurements.get(field), f"loudnorm {field}")
        for field in _MEASUREMENT_FIELDS
    }
    target_lufs = _finite_number(policy.get("target_lufs"), "target LUFS")
    target_true_peak = _finite_number(
        policy.get("target_true_peak_db"), "target true peak"
    )
    target_lra = _finite_number(policy.get("target_lra_lu"), "target LRA")
    tolerances = OUTPUT_VERIFICATION_TOLERANCES
    checks = [
        {
            "id": "integrated_lufs",
            "status": (
                "passed"
                if abs(normalized["input_i"] - target_lufs)
                <= tolerances["integrated_lufs_tolerance_lu"]
                else "failed"
            ),
            "blocking": True,
            "actual": normalized["input_i"],
            "target": target_lufs,
            "tolerance": tolerances["integrated_lufs_tolerance_lu"],
        },
        {
            "id": "true_peak_ceiling",
            "status": (
                "passed"
                if normalized["input_tp"]
                <= target_true_peak + tolerances["true_peak_ceiling_tolerance_db"]
                else "failed"
            ),
            "blocking": True,
            "actual": normalized["input_tp"],
            "maximum": round(
                target_true_peak + tolerances["true_peak_ceiling_tolerance_db"],
                6,
            ),
        },
        {
            "id": "loudness_range_ceiling",
            "status": (
                "passed"
                if normalized["input_lra"]
                <= target_lra + tolerances["lra_ceiling_tolerance_lu"]
                else "failed"
            ),
            "blocking": True,
            "actual": normalized["input_lra"],
            "maximum": round(target_lra + tolerances["lra_ceiling_tolerance_lu"], 6),
        },
    ]
    failures = [row["id"] for row in checks if row["status"] == "failed"]
    return {
        "checks": checks,
        "readiness": {
            "status": "passed" if not failures else "failed",
            "technical_ready": not failures,
            "release_eligible": False,
            "blocking_failure_count": len(failures),
            "blocking_failures": failures,
            "next_action": (
                "human_audio_review" if not failures else "fix_audio_mastering"
            ),
        },
    }


def build_measurement_command(
    source_path: str | Path, policy: dict[str, Any]
) -> list[str]:
    source = Path(source_path).expanduser().resolve()
    return [
        "ffmpeg",
        "-hide_banner",
        "-nostats",
        "-i",
        str(source),
        "-map",
        "0:a:0",
        "-vn",
        "-af",
        _measurement_filter(policy),
        "-f",
        "null",
        "-",
    ]


def build_output_verification_command(
    source_path: str | Path, policy: dict[str, Any]
) -> list[str]:
    source = Path(source_path).expanduser().resolve()
    return [
        "ffmpeg",
        "-hide_banner",
        "-nostats",
        "-i",
        str(source),
        "-map",
        "0:a:0",
        "-vn",
        "-af",
        _output_verification_filter(policy),
        "-f",
        "null",
        "-",
    ]


def build_apply_command(
    source_path: str | Path,
    output_path: str | Path,
    policy: dict[str, Any],
    measurements: dict[str, Any],
    *,
    codec: str,
) -> list[str]:
    source = Path(source_path).expanduser().resolve()
    output = Path(output_path).expanduser().resolve()
    normalized = {
        field: _finite_number(measurements.get(field), f"loudnorm {field}")
        for field in _MEASUREMENT_FIELDS
    }
    return [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-nostats",
        "-i",
        str(source),
        "-map",
        "0:a:0",
        "-vn",
        "-af",
        _apply_filter(policy, normalized),
        "-ar",
        str(int(policy["sample_rate"])),
        "-c:a",
        codec,
        "-map_metadata",
        "-1",
        "-fflags",
        "+bitexact",
        "-flags:a",
        "+bitexact",
        "-threads",
        "1",
        str(output),
    ]


def _measurement_filter(policy: dict[str, Any]) -> str:
    return (
        f"highpass=f={_format_number(policy['highpass_hz'])},"
        f"alimiter=limit={_format_number(policy['limiter'])}:level=false,"
        "loudnorm="
        f"I={_format_number(policy['target_lufs'])}:"
        f"TP={_format_number(policy['target_true_peak_db'])}:"
        f"LRA={_format_number(policy['target_lra_lu'])}:"
        "print_format=json"
    )


def _output_verification_filter(policy: dict[str, Any]) -> str:
    return (
        "loudnorm="
        f"I={_format_number(policy['target_lufs'])}:"
        f"TP={_format_number(policy['target_true_peak_db'])}:"
        f"LRA={_format_number(policy['target_lra_lu'])}:"
        "print_format=json"
    )


def _apply_filter(policy: dict[str, Any], measurements: dict[str, float]) -> str:
    return (
        f"highpass=f={_format_number(policy['highpass_hz'])},"
        f"alimiter=limit={_format_number(policy['limiter'])}:level=false,"
        "loudnorm="
        f"I={_format_number(policy['target_lufs'])}:"
        f"TP={_format_number(policy['target_true_peak_db'])}:"
        f"LRA={_format_number(policy['target_lra_lu'])}:"
        f"measured_I={_format_number(measurements['input_i'])}:"
        f"measured_TP={_format_number(measurements['input_tp'])}:"
        f"measured_LRA={_format_number(measurements['input_lra'])}:"
        f"measured_thresh={_format_number(measurements['input_thresh'])}:"
        f"offset={_format_number(measurements['target_offset'])}:"
        "linear=true:print_format=summary"
    )


def _validate_output_verification_receipt(
    plan_path: Path,
    mastered_audio_path: Path,
    log_path: Path,
    receipt_path: Path,
    policy: dict[str, Any],
) -> None:
    receipt = read_json_object(receipt_path, "loudnorm output verification receipt")
    validate_ffmpeg_execution_receipt(receipt)
    if receipt.get("operation") != "audio_mastering":
        raise ContractError(
            "loudnorm output verification receipt has the wrong FFmpeg operation"
        )
    if receipt.get("command") != build_output_verification_command(
        mastered_audio_path, policy
    ):
        raise ContractError("audio mastering output verification command drifted")
    _require_receipt_role(
        receipt, "inputs", "mastered_audio_staging", mastered_audio_path
    )
    _require_receipt_role(receipt, "inputs", "audio_mastering_plan", plan_path)
    _require_receipt_role(
        receipt, "outputs", "loudnorm_output_verification_log", log_path
    )


def _load_ffmpeg_receipt(value: Any, label: str) -> dict[str, Any]:
    path = validate_artifact_reference(_object(value), label)
    receipt = read_json_object(path, label)
    validate_ffmpeg_execution_receipt(receipt)
    if receipt.get("operation") != "audio_mastering":
        raise ContractError(f"{label} has the wrong FFmpeg operation")
    return receipt


def _require_receipt_role(
    receipt: dict[str, Any],
    section: str,
    role: str,
    path: Path,
) -> None:
    matches = [
        row
        for row in receipt.get(section) or []
        if isinstance(row, dict) and row.get("role") == role
    ]
    if len(matches) != 1:
        raise ContractError(f"audio mastering receipt requires one {role} artifact")
    actual = validate_artifact_reference(
        _object(matches[0].get("artifact")), f"audio mastering {role}"
    )
    if actual != path:
        raise ContractError(f"audio mastering receipt {role} path drifted")


def _receipt_role_path(receipt: dict[str, Any], section: str, role: str) -> Path:
    matches = [
        row
        for row in receipt.get(section) or []
        if isinstance(row, dict) and row.get("role") == role
    ]
    if len(matches) != 1:
        raise ContractError(f"audio mastering receipt requires one {role} artifact")
    return validate_artifact_reference(
        _object(matches[0].get("artifact")), f"audio mastering {role}"
    )


def _atomic_promote(staged_path: Path, output_path: Path) -> None:
    if not staged_path.is_file():
        raise ContractError("audio mastering staging output was not created")
    temp_path = output_path.with_name(f".{output_path.name}.{uuid4().hex}.tmp")
    try:
        shutil.copyfile(staged_path, temp_path)
        temp_path.replace(output_path)
    except OSError as exc:
        raise ContractError(f"audio mastering output promotion failed: {exc}") from exc
    finally:
        if temp_path.exists():
            temp_path.unlink()


def _validate_output_paths(source: Path, plan: Path, *outputs: Path) -> None:
    protected = {source, plan}
    seen: set[Path] = set()
    for path in outputs:
        if path in protected:
            raise ContractError("audio mastering cannot overwrite a bound input")
        if path in seen:
            raise ContractError("audio mastering output paths must be distinct")
        seen.add(path)


def _finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise ContractError(f"{label} must be a finite number")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{label} must be a finite number") from exc
    if not math.isfinite(result):
        raise ContractError(f"{label} must be a finite number")
    return round(result, 6)


def _format_number(value: Any) -> str:
    return f"{_finite_number(value, 'FFmpeg filter value'):.6f}"


def _validate_schema(payload: dict[str, Any], filename: str, label: str) -> None:
    schema_path = Path(__file__).resolve().parent / "schemas" / filename
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(payload)
    except (OSError, json.JSONDecodeError, SchemaError, ValidationError) as exc:
        raise ContractError(f"{label} schema validation failed: {exc}") from exc


def _object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}
