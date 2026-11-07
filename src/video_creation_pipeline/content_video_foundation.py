from __future__ import annotations

import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError, ValidationError

from .contract_io import artifact_reference, read_json_object
from .errors import ContractError
from .gateway_consumer import (
    GATEWAY_CANONICAL_COMMIT,
    GATEWAY_DOCTOR_SCHEMA,
    build_gateway_doctor_report,
    gateway_capability_matrix,
)

CONTENT_TIME_SENSITIVITIES = ("high_temporal", "standard", "evergreen")
DEFAULT_CONTENT_TIME_SENSITIVITY = "standard"


def normalize_content_time_sensitivity(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if normalized not in CONTENT_TIME_SENSITIVITIES:
        allowed = ", ".join(CONTENT_TIME_SENSITIVITIES)
        raise ContractError(f"time_sensitivity must be one of: {allowed}")
    return normalized


def validate_canonical_artifact(
    artifact_path: str | Path,
    schema_path: str | Path,
    *,
    max_age_hours: float,
    time_sensitivity: str = DEFAULT_CONTENT_TIME_SENSITIVITY,
) -> dict[str, Any]:
    artifact = Path(artifact_path).expanduser().resolve()
    schema_file = Path(schema_path).expanduser().resolve()
    payload = read_json_object(artifact, "content video brief")
    schema = read_json_object(schema_file, "content video brief schema")
    try:
        validator = Draft202012Validator(schema, format_checker=FormatChecker())
        validator.check_schema(schema)
        validator.validate(payload)
    except (SchemaError, ValidationError) as exc:
        raise ContractError(
            f"content video brief schema validation failed: {exc.message}"
        ) from exc

    sensitivity = normalize_content_time_sensitivity(time_sensitivity)
    age_gate_applied = sensitivity == "high_temporal"
    if age_gate_applied and max_age_hours <= 0:
        raise ContractError("max_age_hours must be greater than zero")
    raw_age_hours = (
        datetime.now(timezone.utc).timestamp() - artifact.stat().st_mtime
    ) / 3600
    if raw_age_hours < -5 / 60:
        raise ContractError("content video brief file timestamp is in the future")
    age_hours = max(0.0, raw_age_hours)
    if age_gate_applied and age_hours > max_age_hours:
        raise ContractError(
            "content video brief is stale: "
            f"{age_hours:.3f}h exceeds {max_age_hours:.3f}h"
        )

    freshness_evaluation = {
        "time_sensitivity": sensitivity,
        "age_gate_applied": age_gate_applied,
        "max_age_hours": float(max_age_hours) if age_gate_applied else None,
        "artifact_age_hours": round(age_hours, 6),
        "future_timestamp_check": "passed",
        "decision": "within_age_limit" if age_gate_applied else "age_not_required",
    }

    return {
        "artifact": artifact_reference(artifact, "content video brief"),
        "schema_artifact": artifact_reference(
            schema_file, "content video brief schema"
        ),
        "schema_id": str(schema.get("$id") or ""),
        "schema_version": _schema_version(payload, schema),
        "artifact_age_hours": round(age_hours, 6),
        "freshness_limit_hours": float(max_age_hours) if age_gate_applied else None,
        "fresh": True if age_gate_applied else None,
        "freshness_evaluation": freshness_evaluation,
        "payload": payload,
    }


def build_doctor_report(
    *,
    artifact_path: str | Path | None = None,
    schema_path: str | Path | None = None,
    output_root: str | Path | None = None,
    research_output_path: str | Path | None = None,
    research_schema_path: str | Path | None = None,
    gateway_root: str | Path | None = None,
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    for executable in ("ffmpeg", "ffprobe"):
        resolved = shutil.which(executable)
        checks.append(
            {
                "id": executable,
                "status": "pass" if resolved else "fail",
                "blocking": True,
                "detail": _version_line(resolved) if resolved else "not found",
            }
        )

    checks.append(
        {
            "id": "python",
            "status": "pass" if sys.version_info >= (3, 11) else "fail",
            "blocking": True,
            "detail": sys.version.split()[0],
        }
    )
    checks.append(_path_check("canonical_brief", artifact_path, required=False))
    checks.append(_path_check("canonical_schema", schema_path, required=False))
    checks.append(_path_check("research_output", research_output_path, required=False))
    checks.append(_path_check("research_schema", research_schema_path, required=False))
    checks.append(_output_check(output_root))
    checks.append(
        {
            "id": "execution_boundary",
            "status": "pass",
            "blocking": True,
            "detail": "offline; no provider, upload, publish, or VKP mutation",
        }
    )
    gateway_report = (
        build_gateway_doctor_report(gateway_root)
        if gateway_root is not None
        else {
            "schema": GATEWAY_DOCTOR_SCHEMA,
            "status": "not_configured",
            "consumer_id": "mvp_video_pipeline",
            "canonical_commit": GATEWAY_CANONICAL_COMMIT,
            "capabilities": gateway_capability_matrix(),
            "real_provider_called": False,
        }
    )
    if gateway_root is not None:
        checks.append(
            {
                "id": "model_provider_gateway",
                "status": "pass" if gateway_report["status"] == "ready" else "fail",
                "blocking": True,
                "detail": (
                    "canonical text/vision ready; asr/ocr/tts/embedding blocked"
                    if gateway_report["status"] == "ready"
                    else "canonical Gateway unavailable or drifted"
                ),
            }
        )
    blocking_failures = sum(
        row["status"] == "fail" and row["blocking"] for row in checks
    )
    return {
        "schema": "video_creation_pipeline.content_video_doctor.v1",
        "status": "ready" if blocking_failures == 0 else "blocked",
        "blocking_failure_count": blocking_failures,
        "checks": checks,
        "model_provider_gateway": gateway_report,
    }


def read_artifact_summary(source: str | Path) -> dict[str, Any]:
    path = Path(source).expanduser().resolve()
    payload = read_json_object(path, "artifact")
    return {
        "schema": "video_creation_pipeline.artifact_summary.v1",
        "artifact": artifact_reference(path, "artifact"),
        "artifact_schema": str(payload.get("schema") or ""),
        "top_level_keys": sorted(str(key) for key in payload),
        "candidate_only": _candidate_only(payload),
        "status": str(payload.get("status") or ""),
    }


def _schema_version(payload: dict[str, Any], schema: dict[str, Any]) -> str:
    for key in ("schema_version", "version", "schema"):
        value = payload.get(key)
        if isinstance(value, (str, int, float)) and str(value).strip():
            return str(value)
    schema_id = str(schema.get("$id") or "")
    if schema_id:
        return schema_id
    raise ContractError("content video brief does not expose a schema version")


def _candidate_only(payload: dict[str, Any]) -> bool:
    if payload.get("candidate_only") is True:
        return True
    if payload.get("status") == "candidate_only":
        return True
    authority = payload.get("authority_boundary")
    return isinstance(authority, dict) and authority.get("release_eligible") is False


def _path_check(
    check_id: str,
    value: str | Path | None,
    *,
    required: bool,
) -> dict[str, Any]:
    if value is None:
        return {
            "id": check_id,
            "status": "fail" if required else "warning",
            "blocking": required,
            "detail": "not supplied",
        }
    path = Path(value).expanduser().resolve()
    return {
        "id": check_id,
        "status": "pass" if path.is_file() else "fail",
        "blocking": True,
        "detail": str(path),
    }


def _output_check(value: str | Path | None) -> dict[str, Any]:
    if value is None:
        return {
            "id": "output_root",
            "status": "warning",
            "blocking": False,
            "detail": "not supplied",
        }
    path = Path(value).expanduser().resolve()
    probe = path if path.exists() else path.parent
    writable = probe.exists() and os.access(probe, os.W_OK)
    return {
        "id": "output_root",
        "status": "pass" if writable else "fail",
        "blocking": True,
        "detail": str(path),
    }


def _version_line(executable: str | None) -> str:
    if not executable:
        return "not found"
    result = subprocess.run(
        [executable, "-version"],
        capture_output=True,
        text=True,
        errors="replace",
        timeout=10,
        check=False,
    )
    output = result.stdout.strip() or result.stderr.strip()
    return output.splitlines()[0] if output else executable
