from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import ContractError
from .provider_handoff import validate_provider_request

ADAPTER_PLAN_SCHEMA = "video_creation_pipeline.provider_adapter_plan.v1"


def build_adapter_plan(request_path: str | Path) -> dict[str, Any]:
    source_path = Path(request_path).expanduser().resolve()
    request = _read_object(source_path, "provider request")
    validate_provider_request(request)
    authorization = (
        request.get("authorization")
        if isinstance(request.get("authorization"), dict)
        else {}
    )
    kind = str(authorization.get("kind") or "")
    if kind == "vkp_consent_v2":
        adapter = _vkp_adapter(request, authorization)
    elif kind == "provider_mcp_consent":
        adapter = _provider_mcp_adapter(request, authorization, source_path)
    else:
        raise ContractError(f"unsupported provider authorization kind: {kind}")

    plan = {
        "schema": ADAPTER_PLAN_SCHEMA,
        "created_at": _now(),
        "source_request": {
            "path": str(source_path),
            "sha256": _sha256(source_path),
            "request_id": request["request_id"],
            "request_sha256": request["request_sha256"],
        },
        "provider": request["provider"],
        "task": request["task"],
        "route": dict(request.get("route") or {}),
        "adapter": adapter,
        "execution_boundary": {
            "network_execution_performed": False,
            "upload_execution_performed": False,
            "automatic_publish": False,
            "automatic_local_cloud_fallback": False,
            "unlisted_upload_allowed": False,
            "provider_asr_allowed": False,
            "vkp_reservation_logic_duplicated": False,
            "ffmpeg_orchestration_created": False,
        },
        "receipt_contract": {
            "schema": "video_creation_pipeline.provider_receipt.v1",
            "request_id": request["request_id"],
            "request_sha256": request["request_sha256"],
            "acceptance_status": "candidate_only",
            "requires_local_artifact_hashes": True,
            "requires_human_review": True,
        },
    }
    plan["plan_sha256"] = _payload_sha256(plan, "plan_sha256")
    validate_adapter_plan(plan)
    return plan


def validate_adapter_plan(plan: dict[str, Any]) -> None:
    if plan.get("schema") != ADAPTER_PLAN_SCHEMA:
        raise ContractError("unsupported provider adapter plan schema")
    if plan.get("plan_sha256") != _payload_sha256(plan, "plan_sha256"):
        raise ContractError("provider adapter plan integrity check failed")
    source = (
        plan.get("source_request")
        if isinstance(plan.get("source_request"), dict)
        else {}
    )
    path = Path(str(source.get("path") or "")).expanduser().resolve()
    if not path.is_file() or _sha256(path) != source.get("sha256"):
        raise ContractError("provider request changed after adapter planning")
    request = _read_object(path, "provider request")
    validate_provider_request(request)
    if source.get("request_id") != request.get("request_id") or source.get(
        "request_sha256"
    ) != request.get("request_sha256"):
        raise ContractError("adapter plan does not bind the exact provider request")
    boundary = (
        plan.get("execution_boundary")
        if isinstance(plan.get("execution_boundary"), dict)
        else {}
    )
    required_false = (
        "network_execution_performed",
        "upload_execution_performed",
        "automatic_publish",
        "automatic_local_cloud_fallback",
        "unlisted_upload_allowed",
        "provider_asr_allowed",
        "vkp_reservation_logic_duplicated",
        "ffmpeg_orchestration_created",
    )
    if not all(boundary.get(key) is False for key in required_false):
        raise ContractError("provider adapter plan execution boundary is incomplete")
    adapter = plan.get("adapter") if isinstance(plan.get("adapter"), dict) else {}
    if adapter.get("execution_supported_by_this_repo") is not False:
        raise ContractError("adapter plan cannot claim provider execution")
    authorization = (
        request.get("authorization")
        if isinstance(request.get("authorization"), dict)
        else {}
    )
    kind = str(authorization.get("kind") or "")
    if kind == "vkp_consent_v2":
        expected_adapter = _vkp_adapter(request, authorization)
    elif kind == "provider_mcp_consent":
        expected_adapter = _provider_mcp_adapter(request, authorization, path)
    else:
        raise ContractError(f"unsupported provider authorization kind: {kind}")
    if adapter != expected_adapter:
        raise ContractError("adapter plan differs from the exact provider request")


def write_adapter_plan(plan: dict[str, Any], output_path: str | Path) -> Path:
    validate_adapter_plan(plan)
    output = Path(output_path).expanduser().resolve()
    source_path = Path(str(plan["source_request"]["path"])).resolve()
    if output == source_path:
        raise ContractError("adapter plan must not overwrite the provider request")
    request = _read_object(source_path, "provider request")
    bundle = Path(
        str(request.get("source_edit_job", {}).get("source_bundle_path") or "")
    ).resolve()
    try:
        output.relative_to(bundle)
    except ValueError:
        pass
    else:
        raise ContractError("adapter plan must stay outside the read-only VKP Bundle")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    return output


def _vkp_adapter(
    request: dict[str, Any], authorization: dict[str, Any]
) -> dict[str, Any]:
    route = request.get("route") if isinstance(request.get("route"), dict) else {}
    return {
        "adapter_id": "vkp_trusted_model_connector",
        "interface": "mcp",
        "server": "video_knowledge_pipeline.trusted_model_connector_mcp",
        "tool": "execute_consented_model_task_tool",
        "status": "awaiting_stable_front_door",
        "execution_supported_by_this_repo": False,
        "invocation": {
            "consent_path": str(authorization.get("reference_path") or ""),
            "route_revision": str(route.get("revision") or ""),
            "write": True,
        },
        "owner": "video-knowledge-pipeline",
        "blocker": "VKP stable consent-execute CLI is not registered yet",
        "delegated_controls": [
            "consent_revalidation",
            "call_and_cost_reservation",
            "provider_secret_resolution",
            "execution_audit",
        ],
    }


def _provider_mcp_adapter(
    request: dict[str, Any],
    authorization: dict[str, Any],
    request_path: Path,
) -> dict[str, Any]:
    route = request.get("route") if isinstance(request.get("route"), dict) else {}
    return {
        "adapter_id": "provider_mcp",
        "interface": "provider_plugin_mcp",
        "provider": request["provider"],
        "route_id": str(route.get("id") or ""),
        "route_revision": str(route.get("revision") or ""),
        "status": "awaiting_provider_plugin",
        "execution_supported_by_this_repo": False,
        "invocation": {
            "provider_request_path": str(request_path),
            "provider_request_sha256": request["request_sha256"],
            "authorization_reference": str(authorization.get("reference") or ""),
            "upload_manifest_sha256": str(
                request.get("upload_manifest", {}).get("manifest_sha256") or ""
            ),
            "publish": False,
            "run_provider_asr": False,
            "return_editable_result": True,
        },
        "blocker": "provider MCP tool must be present and selected explicitly",
    }


def _read_object(path: Path, label: str) -> dict[str, Any]:
    if not path.is_file():
        raise ContractError(f"{label} not found: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"cannot read {label}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ContractError(f"{label} must be a JSON object")
    return payload


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _payload_sha256(payload: dict[str, Any], field: str) -> str:
    value = dict(payload)
    value.pop(field, None)
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
