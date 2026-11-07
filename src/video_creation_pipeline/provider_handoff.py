from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .errors import ContractError
from .vkp_bridge import EDIT_JOB_SCHEMA, validate_edit_job

HUMAN_SELECTION_SCHEMA = "video_creation_pipeline.human_selection.v1"
PROVIDER_REQUEST_SCHEMA = "video_creation_pipeline.provider_request.v1"
PROVIDER_RECEIPT_SCHEMA = "video_creation_pipeline.provider_receipt.v1"
RECEIPT_VALIDATION_SCHEMA = "video_creation_pipeline.provider_receipt_validation.v1"


def build_provider_request(
    edit_job_path: str | Path,
    selection_path: str | Path,
    *,
    task: str = "",
) -> dict[str, Any]:
    job_path = Path(edit_job_path).expanduser().resolve()
    selected_path = Path(selection_path).expanduser().resolve()
    job = _read_object(job_path, "edit job")
    selection = _read_object(selected_path, "human selection")
    validate_edit_job(job)
    if job.get("schema") != EDIT_JOB_SCHEMA:
        raise ContractError("provider handoff requires the current edit job schema")

    remote = (
        job.get("remote_capabilities")
        if isinstance(job.get("remote_capabilities"), dict)
        else {}
    )
    if not remote.get("execute"):
        raise ContractError(
            "provider request requires an explicitly authorized remote edit job"
        )
    capability = _select_capability(remote, task)
    selected_candidates = resolve_human_selection(selection, job, _sha256(job_path))
    upload = remote.get("upload") if isinstance(remote.get("upload"), dict) else {}
    if not upload.get("allowed"):
        raise ContractError(
            "provider request requires an explicitly authorized upload manifest"
        )

    provider = str(remote.get("provider") or "")
    request: dict[str, Any] = {
        "schema": PROVIDER_REQUEST_SCHEMA,
        "request_id": str(uuid4()),
        "created_at": _now(),
        "source_edit_job": {
            "path": str(job_path),
            "sha256": _sha256(job_path),
            "schema": job.get("schema"),
            "source_bundle_path": str(job.get("source_bundle", {}).get("path") or ""),
            "vkp_remains_authoritative": True,
        },
        "human_selection": {
            "path": str(selected_path),
            "sha256": _sha256(selected_path),
            "schema": selection.get("schema"),
            "operator_confirmed": True,
        },
        "provider": provider,
        "route": dict(remote.get("route") or {}),
        "task": capability["task"],
        "result_role": capability["result_role"],
        "authorization": dict(remote.get("authorization") or {}),
        "limits": dict(remote.get("limits") or {}),
        "upload_manifest": {
            "schema": upload.get("schema"),
            "source": upload.get("source"),
            "artifact_count": upload.get("artifact_count"),
            "total_bytes": upload.get("total_bytes"),
            "manifest_sha256": upload.get("manifest_sha256"),
            "artifacts": [dict(row) for row in upload.get("artifacts") or []],
        },
        "timeline_items": selected_candidates,
        "provider_instructions": _provider_instructions(provider, capability["task"]),
        "result_contract": {
            "acceptance_status": "candidate_only",
            "must_bind_request_sha256": True,
            "must_return_local_hashed_artifacts": True,
            "overwrites_vkp_timeline": False,
            "requires_human_review": True,
        },
        "operator_boundary": {
            "network_execution_performed": False,
            "upload_execution_performed": False,
            "automatic_publish": False,
            "automatic_local_cloud_fallback": False,
            "unlisted_upload_allowed": False,
            "provider_asr_allowed": False,
            "vkp_status_machine_created": False,
            "ffmpeg_orchestration_created": False,
        },
    }
    request["request_sha256"] = _payload_sha256(request, "request_sha256")
    validate_provider_request(request)
    return request


def validate_provider_request(request: dict[str, Any]) -> None:
    if request.get("schema") != PROVIDER_REQUEST_SCHEMA:
        raise ContractError("unsupported provider request schema")
    if request.get("request_sha256") != _payload_sha256(request, "request_sha256"):
        raise ContractError("provider request integrity check failed")
    source = (
        request.get("source_edit_job")
        if isinstance(request.get("source_edit_job"), dict)
        else {}
    )
    selection = (
        request.get("human_selection")
        if isinstance(request.get("human_selection"), dict)
        else {}
    )
    _validate_referenced_file(source, "source edit job")
    _validate_referenced_file(selection, "human selection")
    job = _read_object(Path(str(source.get("path") or "")), "source edit job")
    selection_payload = _read_object(
        Path(str(selection.get("path") or "")), "human selection"
    )
    validate_edit_job(job)
    expected_items = resolve_human_selection(
        selection_payload, job, str(source.get("sha256") or "")
    )
    if request.get("timeline_items") != expected_items:
        raise ContractError(
            "provider request timeline differs from the bound human selection"
        )
    if not expected_items:
        raise ContractError(
            "provider request requires at least one human-selected timeline item"
        )
    remote = (
        job.get("remote_capabilities")
        if isinstance(job.get("remote_capabilities"), dict)
        else {}
    )
    capability = _select_capability(remote, str(request.get("task") or ""))
    expected_values = {
        "provider": remote.get("provider"),
        "route": remote.get("route"),
        "authorization": remote.get("authorization"),
        "limits": remote.get("limits"),
        "result_role": capability.get("result_role"),
        "upload_manifest": _request_upload_manifest(remote.get("upload") or {}),
    }
    for key, expected in expected_values.items():
        if request.get(key) != expected:
            raise ContractError(
                f"provider request {key} differs from the authorized edit job"
            )
    boundary = (
        request.get("operator_boundary")
        if isinstance(request.get("operator_boundary"), dict)
        else {}
    )
    required_false = (
        "network_execution_performed",
        "upload_execution_performed",
        "automatic_publish",
        "automatic_local_cloud_fallback",
        "unlisted_upload_allowed",
        "provider_asr_allowed",
        "vkp_status_machine_created",
        "ffmpeg_orchestration_created",
    )
    if not all(boundary.get(key) is False for key in required_false):
        raise ContractError("provider request operator boundary is incomplete")


def write_provider_request(request: dict[str, Any], output_path: str | Path) -> Path:
    validate_provider_request(request)
    output = Path(output_path).expanduser().resolve()
    bundle = Path(str(request["source_edit_job"]["source_bundle_path"])).resolve()
    try:
        output.relative_to(bundle)
    except ValueError:
        pass
    else:
        raise ContractError(
            "provider request must stay outside the read-only VKP Bundle"
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(request, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return output


def validate_provider_receipt(
    request: dict[str, Any], receipt: dict[str, Any]
) -> dict[str, Any]:
    validate_provider_request(request)
    if receipt.get("schema") != PROVIDER_RECEIPT_SCHEMA:
        raise ContractError("unsupported provider receipt schema")
    if receipt.get("request_id") != request.get("request_id"):
        raise ContractError("provider receipt request id mismatch")
    if receipt.get("request_sha256") != request.get("request_sha256"):
        raise ContractError("provider receipt does not bind the exact request")
    for key in ("provider", "task"):
        if receipt.get(key) != request.get(key):
            raise ContractError(f"provider receipt {key} mismatch")
    if receipt.get("route") != request.get("route"):
        raise ContractError("provider receipt route mismatch")
    if receipt.get("published") is not False:
        raise ContractError(
            "provider receipt must explicitly confirm that nothing was published"
        )
    if receipt.get("fallback_used") is not False:
        raise ContractError("provider receipt cannot report an automatic fallback")

    status = str(receipt.get("status") or "")
    if status not in {"completed", "failed", "cancelled"}:
        raise ContractError("provider receipt status is invalid")
    _validate_usage(request, receipt)
    outputs = receipt.get("outputs") if isinstance(receipt.get("outputs"), list) else []
    if status == "completed" and not outputs:
        raise ContractError(
            "completed provider receipt requires at least one output artifact"
        )
    for output in outputs:
        _validate_output(request, output)
    return {
        "schema": RECEIPT_VALIDATION_SCHEMA,
        "valid": True,
        "status": status,
        "request_id": request["request_id"],
        "request_sha256": request["request_sha256"],
        "output_count": len(outputs),
        "acceptance_status": "candidate_only",
        "next_action": "human_review" if status == "completed" else "operator_review",
        "validated_at": _now(),
    }


def verify_provider_receipt_files(
    request_path: str | Path, receipt_path: str | Path
) -> dict[str, Any]:
    request = _read_object(
        Path(request_path).expanduser().resolve(), "provider request"
    )
    receipt = _read_object(
        Path(receipt_path).expanduser().resolve(), "provider receipt"
    )
    return validate_provider_receipt(request, receipt)


def resolve_human_selection(
    selection: dict[str, Any], job: dict[str, Any], edit_job_sha256: str
) -> list[dict[str, Any]]:
    if selection.get("schema") != HUMAN_SELECTION_SCHEMA:
        raise ContractError(f"human selection must use {HUMAN_SELECTION_SCHEMA}")
    if selection.get("edit_job_sha256") != edit_job_sha256:
        raise ContractError("human selection does not bind the exact edit job")
    confirmation = (
        selection.get("operator_confirmation")
        if isinstance(selection.get("operator_confirmation"), dict)
        else {}
    )
    if (
        confirmation.get("confirmed") is not True
        or confirmation.get("method") != "visible_operator_selection"
        or _parse_datetime(confirmation.get("confirmed_at")) is None
    ):
        raise ContractError(
            "human selection requires a dated visible operator confirmation"
        )
    values = selection.get("selections")
    if not isinstance(values, list) or not values:
        raise ContractError("human selection requires at least one selected candidate")
    boundary_run = selection.get("boundary_refinement_run")
    if boundary_run is not None:
        if not isinstance(boundary_run, dict):
            raise ContractError("generated boundary refinement run must be an object")
        if (
            boundary_run.get("schema")
            != "video_creation_pipeline.boundary_refinement_run.v1"
            or boundary_run.get("artifact_validation") != "passed"
            or boundary_run.get("run_sha256")
            != _payload_sha256(boundary_run, "run_sha256")
            or boundary_run.get("refined_selections_sha256")
            != _canonical_sha256(values)
        ):
            raise ContractError(
                "generated boundary refinement does not bind the exact selections"
            )
    candidates = {row["candidate_id"]: row for row in job.get("candidate_clips") or []}
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for position, raw in enumerate(values, start=1):
        if not isinstance(raw, dict):
            raise ContractError("each human selection must be an object")
        candidate_id = str(raw.get("candidate_id") or "")
        if candidate_id in seen:
            raise ContractError(f"human selection duplicates candidate: {candidate_id}")
        candidate = candidates.get(candidate_id)
        if candidate is None:
            raise ContractError(
                f"human selection references an unknown candidate: {candidate_id}"
            )
        seen.add(candidate_id)
        interval = (
            raw.get("confirmed_interval")
            if isinstance(raw.get("confirmed_interval"), dict)
            else candidate["source_interval"]
        )
        start = _seconds(interval.get("start"))
        end = _seconds(interval.get("end"))
        if end <= start:
            raise ContractError(
                f"human selection has an invalid interval: {candidate_id}"
            )
        refinement = (
            raw.get("boundary_refinement")
            if isinstance(raw.get("boundary_refinement"), dict)
            else {"status": "not_run"}
        )
        artifact_validation = refinement.get(
            "boundary_artifact_validation",
            refinement.get("artifact_validation"),
        )
        if raw.get("confirmed_interval") and (
            refinement.get("status") != "completed" or artifact_validation != "passed"
        ):
            raise ContractError(
                "confirmed interval overrides require completed boundary refinement and artifact validation"
            )
        result.append(
            {
                "order": position,
                "candidate_id": candidate_id,
                "timeline_index": candidate["timeline_index"],
                "confirmed_interval": {"start": start, "end": end},
                "interval_source": "operator_confirmed_override"
                if raw.get("confirmed_interval")
                else "vkp_candidate_interval",
                "transcript": candidate.get("transcript") or "",
                "visual_text": candidate.get("visual_text") or "",
                "evidence": candidate.get("evidence") or {},
                "boundary_refinement": refinement,
                "selection_status": "human_confirmed",
            }
        )
    return result


def _select_capability(remote: dict[str, Any], task: str) -> dict[str, Any]:
    capabilities = [
        row
        for row in remote.get("capabilities") or []
        if isinstance(row, dict) and row.get("status") == "authorized"
    ]
    if task:
        capabilities = [row for row in capabilities if row.get("task") == task]
    if len(capabilities) != 1:
        raise ContractError("select exactly one authorized provider task")
    return capabilities[0]


def _provider_instructions(provider: str, task: str) -> dict[str, Any]:
    common = {
        "action": task,
        "use_only_confirmed_timeline_items": True,
        "preserve_original_media": True,
        "return_editable_result": True,
        "publish": False,
    }
    if provider.lower() == "chatcut" and task == "editable_timeline_finishing":
        common.update(
            {
                "action": "create_editable_timeline_from_confirmed_intervals",
                "flatten_output": False,
                "run_provider_asr": False,
                "provider_asr_requires_separate_authorization": True,
            }
        )
    return common


def _validate_usage(request: dict[str, Any], receipt: dict[str, Any]) -> None:
    usage = receipt.get("usage") if isinstance(receipt.get("usage"), dict) else {}
    limits = request.get("limits") if isinstance(request.get("limits"), dict) else {}
    calls = _nonnegative_int(usage.get("calls"), "receipt usage.calls")
    if calls > int(limits.get("max_calls") or 0):
        raise ContractError("provider receipt exceeds the authorized call limit")
    cost = _nonnegative_money(usage.get("cost"), "receipt usage.cost")
    if cost > float(limits.get("max_cost") or 0):
        raise ContractError("provider receipt exceeds the authorized cost limit")
    if (
        str(usage.get("currency") or "").upper()
        != str(limits.get("currency") or "").upper()
    ):
        raise ContractError("provider receipt currency mismatch")


def _validate_output(request: dict[str, Any], output: Any) -> None:
    if not isinstance(output, dict):
        raise ContractError("provider output record must be an object")
    if output.get("role") != request.get("result_role"):
        raise ContractError("provider output role mismatch")
    if output.get("acceptance_status") != "candidate_only":
        raise ContractError("provider output must remain candidate_only")
    path = Path(str(output.get("path") or "")).expanduser().resolve()
    if not path.is_file():
        raise ContractError(f"provider output artifact not found: {path}")
    bundle = Path(
        str(request.get("source_edit_job", {}).get("source_bundle_path") or "")
    ).resolve()
    try:
        path.relative_to(bundle)
    except ValueError:
        pass
    else:
        raise ContractError(
            "provider output must stay outside the read-only VKP Bundle"
        )
    upload_paths = {
        str(Path(str(row.get("path") or "")).expanduser().resolve())
        for row in request.get("upload_manifest", {}).get("artifacts") or []
        if isinstance(row, dict)
    }
    if str(path) in upload_paths:
        raise ContractError(
            "provider output cannot overwrite an uploaded source artifact"
        )
    expected_bytes = _nonnegative_int(output.get("bytes"), "provider output bytes")
    expected_sha = str(output.get("sha256") or "")
    if path.stat().st_size != expected_bytes or _sha256(path) != expected_sha:
        raise ContractError(f"provider output artifact integrity check failed: {path}")


def _request_upload_manifest(upload: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": upload.get("schema"),
        "source": upload.get("source"),
        "artifact_count": upload.get("artifact_count"),
        "total_bytes": upload.get("total_bytes"),
        "manifest_sha256": upload.get("manifest_sha256"),
        "artifacts": [dict(row) for row in upload.get("artifacts") or []],
    }


def _validate_referenced_file(reference: dict[str, Any], label: str) -> None:
    path = Path(str(reference.get("path") or "")).expanduser().resolve()
    expected = str(reference.get("sha256") or "")
    if not path.is_file() or _sha256(path) != expected:
        raise ContractError(f"{label} changed after provider request creation")


def _read_object(path: Path, label: str) -> dict[str, Any]:
    if not path.is_file():
        raise ContractError(f"{label} not found: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"{label} is unreadable: {exc}") from exc
    if not isinstance(payload, dict):
        raise ContractError(f"{label} must be a JSON object")
    return payload


def _payload_sha256(payload: dict[str, Any], hash_key: str) -> str:
    value = dict(payload)
    value.pop(hash_key, None)
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _seconds(value: Any) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ContractError("timeline interval must contain numeric seconds") from exc
    if result < 0 or not math.isfinite(result):
        raise ContractError(
            "timeline interval must contain finite non-negative seconds"
        )
    return result


def _nonnegative_int(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise ContractError(f"{label} must be a non-negative integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{label} must be a non-negative integer") from exc
    if result < 0:
        raise ContractError(f"{label} must be a non-negative integer")
    return result


def _nonnegative_money(value: Any, label: str) -> float:
    if isinstance(value, bool) or value is None:
        raise ContractError(f"{label} must be a non-negative finite amount")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{label} must be a non-negative finite amount") from exc
    if result < 0 or not math.isfinite(result):
        raise ContractError(f"{label} must be a non-negative finite amount")
    return result


def _parse_datetime(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
