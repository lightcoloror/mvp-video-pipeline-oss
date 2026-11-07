from __future__ import annotations

import hashlib
import json
import math
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .errors import ContractError

REMOTE_POLICY_SCHEMA = "video_creation_pipeline.remote_execution_policy.v2"
LOCAL_UPLOAD_MANIFEST_SCHEMA = "video_creation_pipeline.upload_manifest.v1"
VKP_CONSENT_SCHEMA = "video_knowledge_pipeline.model_connector_consent.v2"
VKP_UPLOAD_MANIFEST_SCHEMA = "video_knowledge_pipeline.model_upload_manifest.v1"


def build_remote_contract(policy: dict[str, Any] | None) -> dict[str, Any]:
    raw = policy if isinstance(policy, dict) else {}
    enabled = _strict_bool(raw.get("enabled"), "enabled")
    if not enabled:
        return _disabled_contract()
    if raw.get("schema") != REMOTE_POLICY_SCHEMA:
        raise ContractError(f"authorized remote policy must use {REMOTE_POLICY_SCHEMA}")

    authorization = raw.get("authorization")
    if not isinstance(authorization, dict):
        raise ContractError(
            "authorized remote execution requires an authorization object"
        )
    kind = str(authorization.get("kind") or "").strip()
    tasks = _tasks(raw.get("tasks"))
    if kind == "vkp_consent_v2":
        return _contract_from_vkp_consent(raw, authorization, tasks)
    if kind == "provider_mcp_consent":
        return _contract_from_provider_mcp(raw, authorization, tasks)
    raise ContractError(
        "authorization.kind must be vkp_consent_v2 or provider_mcp_consent"
    )


def validate_remote_contract(remote: dict[str, Any]) -> None:
    if remote.get("schema") != REMOTE_POLICY_SCHEMA:
        raise ContractError("unsupported remote execution policy schema")
    if remote.get("automatic_fallback") or remote.get("automatic_publish"):
        raise ContractError(
            "remote execution cannot enable fallback or automatic publishing"
        )
    upload = remote.get("upload") if isinstance(remote.get("upload"), dict) else {}
    if upload.get("automatic"):
        raise ContractError("remote uploads must use an explicit artifact manifest")
    if not remote.get("execute"):
        if upload.get("allowed"):
            raise ContractError(
                "upload cannot be authorized while remote execution is disabled"
            )
        return
    if (
        remote.get("status") != "authorized_for_execution"
        or remote.get("online_model_calls_allowed") is not True
    ):
        raise ContractError("remote execution status does not match its authorization")

    if not str(remote.get("provider") or "").strip():
        raise ContractError("authorized remote execution requires a provider")
    route = remote.get("route") if isinstance(remote.get("route"), dict) else {}
    if not route.get("id") or not route.get("revision"):
        raise ContractError(
            "authorized remote execution requires route id and revision"
        )
    if not remote.get("capabilities"):
        raise ContractError("authorized remote execution requires at least one task")
    limits = remote.get("limits") if isinstance(remote.get("limits"), dict) else {}
    _validate_limits(limits)

    authorization = (
        remote.get("authorization")
        if isinstance(remote.get("authorization"), dict)
        else {}
    )
    kind = authorization.get("kind")
    if kind == "vkp_consent_v2":
        current = _validated_vkp_consent(
            Path(str(authorization.get("reference_path") or ""))
        )
        _require_equal(
            authorization,
            current["authorization"],
            "VKP consent authorization snapshot changed",
        )
        _require_equal(
            remote.get("provider"), current["provider"], "VKP consent provider changed"
        )
        _require_equal(route, current["route"], "VKP consent route changed")
        _require_equal(
            remote.get("limits"),
            current["limits"],
            "VKP consent call or cost allowance changed",
        )
        _require_equal(
            remote.get("capabilities"),
            _capability_catalog(current["provider"], [current["task"]], True),
            "VKP consent task capability changed",
        )
        _require_equal(
            upload.get("manifest_sha256"),
            current["upload"]["manifest_sha256"],
            "VKP consent upload manifest changed",
        )
    elif kind == "provider_mcp_consent":
        if (
            not authorization.get("reference")
            or authorization.get("operator_confirmed") is not True
            or _parse_datetime(authorization.get("confirmed_at")) is None
            or authorization.get("authoritative_source")
            != "explicit_provider_mcp_policy"
            or authorization.get("embedded_vkp_consent_copy") is not False
        ):
            raise ContractError(
                "provider MCP execution requires an operator-confirmed authorization reference"
            )
    else:
        raise ContractError("remote authorization reference is missing")
    capabilities = remote.get("capabilities") or []
    if any(
        not isinstance(row, dict)
        or row.get("provider") != remote.get("provider")
        or row.get("status") != "authorized"
        or not row.get("task")
        for row in capabilities
    ):
        raise ContractError(
            "remote capability catalog differs from its authorized provider"
        )
    _validate_upload_snapshot(upload)


def _contract_from_vkp_consent(
    raw: dict[str, Any], authorization: dict[str, Any], tasks: list[str]
) -> dict[str, Any]:
    duplicates = [
        key for key in ("provider", "route", "limits", "upload") if key in raw
    ]
    if duplicates:
        raise ContractError(
            "vkp_consent_v2 policy must derive provider, route, limits, and upload from the consent file; "
            f"remove: {', '.join(duplicates)}"
        )
    consent_path = (
        Path(str(authorization.get("consent_path") or "")).expanduser().resolve()
    )
    derived = _validated_vkp_consent(consent_path)
    if tasks and tasks != [derived["task"]]:
        raise ContractError(
            "remote policy tasks must exactly match the VKP consent task"
        )
    return _assemble_contract(
        provider=derived["provider"],
        tasks=[derived["task"]],
        route=derived["route"],
        authorization=derived["authorization"],
        limits=derived["limits"],
        upload=derived["upload"],
    )


def _contract_from_provider_mcp(
    raw: dict[str, Any], authorization: dict[str, Any], tasks: list[str]
) -> dict[str, Any]:
    provider = str(raw.get("provider") or "").strip()
    if not provider or not tasks:
        raise ContractError(
            "provider MCP policy requires provider and at least one task"
        )
    route_raw = raw.get("route") if isinstance(raw.get("route"), dict) else {}
    route = {
        "id": str(route_raw.get("id") or "").strip(),
        "revision": str(route_raw.get("revision") or "").strip(),
    }
    if not route["id"] or not route["revision"]:
        raise ContractError("provider MCP policy requires route id and revision")
    reference = str(authorization.get("reference") or "").strip()
    confirmed = _strict_bool(
        authorization.get("operator_confirmed"), "authorization.operator_confirmed"
    )
    confirmed_at = str(authorization.get("confirmed_at") or "").strip()
    if not reference or not confirmed or _parse_datetime(confirmed_at) is None:
        raise ContractError(
            "provider MCP execution requires a dated, operator-confirmed authorization reference"
        )
    limits_raw = raw.get("limits") if isinstance(raw.get("limits"), dict) else {}
    limits = {
        "max_calls": _positive_int(limits_raw.get("max_calls"), "limits.max_calls"),
        "max_cost": _nonnegative_money(limits_raw.get("max_cost"), "limits.max_cost"),
        "currency": str(limits_raw.get("currency") or "USD").strip().upper(),
    }
    _validate_limits(limits)
    upload_raw = raw.get("upload") if isinstance(raw.get("upload"), dict) else {}
    upload_allowed = _strict_bool(upload_raw.get("allowed"), "upload.allowed")
    artifacts = (
        _local_upload_artifacts(upload_raw.get("artifacts") or [])
        if upload_allowed
        else []
    )
    upload = _upload_snapshot(
        schema=LOCAL_UPLOAD_MANIFEST_SCHEMA,
        source="provider_mcp_policy",
        artifacts=artifacts,
        allowed=upload_allowed,
    )
    return _assemble_contract(
        provider=provider,
        tasks=tasks,
        route=route,
        authorization={
            "kind": "provider_mcp_consent",
            "reference": reference,
            "operator_confirmed": True,
            "confirmed_at": confirmed_at,
            "authoritative_source": "explicit_provider_mcp_policy",
            "embedded_vkp_consent_copy": False,
        },
        limits=limits,
        upload=upload,
    )


def _validated_vkp_consent(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ContractError(f"VKP consent file not found: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"VKP consent file is unreadable: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema") != VKP_CONSENT_SCHEMA:
        raise ContractError(f"VKP consent must use {VKP_CONSENT_SCHEMA}")
    if (
        payload.get("status") != "active"
        or payload.get("user_confirmed_data_export") is not True
    ):
        raise ContractError(
            "VKP consent is not active or data export was not explicitly confirmed"
        )
    if payload.get("consent_sha256") != _payload_sha256(payload, "consent_sha256"):
        raise ContractError("VKP consent integrity check failed")
    expires_at = _parse_datetime(payload.get("expires_at"))
    if expires_at is None or expires_at <= datetime.now(timezone.utc):
        raise ContractError("VKP consent is expired or has an invalid expiry")

    task = str(payload.get("task") or "").strip()
    provider_raw = (
        payload.get("provider") if isinstance(payload.get("provider"), dict) else {}
    )
    provider = str(provider_raw.get("provider") or "").strip()
    route_raw = payload.get("route") if isinstance(payload.get("route"), dict) else {}
    route = {
        "id": str(route_raw.get("route_id") or "").strip(),
        "revision": str(route_raw.get("route_revision") or "").strip(),
    }
    deployments = payload.get("authorized_deployments")
    if not task or not provider or not route["id"] or not route["revision"]:
        raise ContractError("VKP consent task, provider, or route is incomplete")
    if not isinstance(deployments, list) or not deployments:
        raise ContractError("VKP consent has no authorized deployment")
    if route_raw.get("execution_location") != "remote":
        raise ContractError("VKP consent route must use remote execution")
    if route_raw.get("route_snapshot_sha256") != _route_snapshot_sha256(
        route_raw, deployments
    ):
        raise ContractError("VKP consent route snapshot integrity check failed")

    manifest = (
        payload.get("upload_manifest")
        if isinstance(payload.get("upload_manifest"), dict)
        else {}
    )
    files = manifest.get("files") if isinstance(manifest.get("files"), list) else []
    if manifest.get("schema") != VKP_UPLOAD_MANIFEST_SCHEMA or not files:
        raise ContractError("VKP consent requires a non-empty upload manifest")
    if payload.get("artifacts") != files:
        raise ContractError("VKP consent artifacts differ from its upload manifest")
    manifest_hash = _manifest_sha256(files)
    total_bytes = sum(_artifact_bytes(row) for row in files)
    if (
        manifest.get("file_count") != len(files)
        or manifest.get("total_bytes") != total_bytes
    ):
        raise ContractError("VKP consent upload manifest count or byte total changed")
    if manifest.get("manifest_sha256") != manifest_hash:
        raise ContractError("VKP consent upload manifest integrity check failed")
    paths = [str(row.get("path") or "") for row in files if isinstance(row, dict)]
    if len(paths) != len(files) or len(paths) != len(set(paths)):
        raise ContractError(
            "VKP consent upload manifest contains invalid or duplicate paths"
        )
    for artifact in files:
        _validate_file_record(artifact)

    confirmation = (
        payload.get("operator_confirmation")
        if isinstance(payload.get("operator_confirmation"), dict)
        else {}
    )
    if (
        confirmation.get("confirmed") is not True
        or confirmation.get("confirmation_method") != "visible_operator_shell"
        or _parse_datetime(confirmation.get("confirmed_at")) is None
        or confirmation.get("exact_manifest_sha256") != manifest_hash
    ):
        raise ContractError(
            "VKP consent operator confirmation does not bind the exact upload manifest"
        )

    scope = payload.get("scope") if isinstance(payload.get("scope"), dict) else {}
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    max_calls = _positive_int(scope.get("max_calls"), "scope.max_calls")
    max_cost = _positive_money(
        scope.get("max_estimated_cost_usd"), "scope.max_estimated_cost_usd"
    )
    per_call = _positive_money(
        scope.get("max_cost_per_call_usd"), "scope.max_cost_per_call_usd"
    )
    calls_used = max(
        _safe_int(usage.get("calls_attempted"), 0),
        _safe_int(usage.get("calls_completed"), 0),
    )
    committed = _nonnegative_money(
        usage.get("cost_committed_usd") or 0, "usage.cost_committed_usd"
    )
    if (
        per_call > max_cost
        or calls_used >= max_calls
        or committed > max_cost
        or usage.get("cost_limit_exceeded")
    ):
        raise ContractError(
            "VKP consent call or cost allowance is exhausted or invalid"
        )

    upload = _upload_snapshot(
        schema=VKP_UPLOAD_MANIFEST_SCHEMA,
        source="vkp_consent_v2",
        artifacts=[dict(row) for row in files],
        allowed=True,
    )
    _require_equal(
        upload["manifest_sha256"],
        manifest_hash,
        "VKP consent upload manifest normalization changed",
    )
    return {
        "task": task,
        "provider": provider,
        "route": route,
        "authorization": {
            "kind": "vkp_consent_v2",
            "reference_path": str(path.resolve()),
            "reference_sha256": _sha256(path),
            "consent_id": str(payload.get("consent_id") or ""),
            "consent_sha256": str(payload.get("consent_sha256") or ""),
            "expires_at": str(payload.get("expires_at") or ""),
            "operator_confirmed": True,
            "upload_manifest_sha256": manifest_hash,
            "authoritative_source": "vkp_consent_file",
            "embedded_vkp_consent_copy": False,
        },
        "limits": {
            "max_calls": max_calls - calls_used,
            "max_cost": round(max_cost - committed, 8),
            "currency": "USD",
        },
        "upload": upload,
    }


def _assemble_contract(
    *,
    provider: str,
    tasks: list[str],
    route: dict[str, str],
    authorization: dict[str, Any],
    limits: dict[str, Any],
    upload: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema": REMOTE_POLICY_SCHEMA,
        "status": "authorized_for_execution",
        "execute": True,
        "online_model_calls_allowed": True,
        "automatic_fallback": False,
        "automatic_publish": False,
        "gateway": "VKP Trusted Broker or explicitly selected provider MCP",
        "provider": provider,
        "route": route,
        "authorization": authorization,
        "limits": limits,
        "capabilities": _capability_catalog(provider, tasks, True),
        "upload": upload,
        "required_before_execution": [
            "explicit_remote_route",
            "authorization_reference",
            "exact_upload_manifest",
            "call_and_cost_limit",
            "operator_confirmation",
        ],
        "forbidden": [
            "upload_unlisted_artifacts",
            "automatic_publish",
            "local_cloud_fallback",
        ],
    }


def _disabled_contract() -> dict[str, Any]:
    return {
        "schema": REMOTE_POLICY_SCHEMA,
        "status": "awaiting_explicit_authorization",
        "execute": False,
        "online_model_calls_allowed": False,
        "automatic_fallback": False,
        "automatic_publish": False,
        "gateway": "VKP Trusted Broker or explicitly selected provider MCP",
        "provider": "",
        "route": {"id": "", "revision": ""},
        "authorization": {
            "kind": "none",
            "authoritative_source": "none",
            "embedded_vkp_consent_copy": False,
        },
        "limits": {"max_calls": 0, "max_cost": 0.0, "currency": "USD"},
        "capabilities": _capability_catalog("", [], False),
        "upload": _upload_snapshot(
            schema=LOCAL_UPLOAD_MANIFEST_SCHEMA,
            source="none",
            artifacts=[],
            allowed=False,
        ),
        "required_before_execution": [
            "explicit_remote_route",
            "authorization_reference",
            "exact_upload_manifest",
            "call_and_cost_limit",
            "operator_confirmation",
        ],
        "forbidden": [
            "upload_unlisted_artifacts",
            "automatic_publish",
            "local_cloud_fallback",
        ],
    }


def _capability_catalog(
    provider: str, tasks: list[str], enabled: bool
) -> list[dict[str, Any]]:
    if enabled:
        return [
            {
                "task": task,
                "provider": provider,
                "status": "authorized",
                "result_role": _result_role(task),
            }
            for task in tasks
        ]
    return [
        {
            "task": "scene_segmentation",
            "provider": "mediakit",
            "status": "available_requires_authorization",
            "result_role": "candidate_evidence",
        },
        {
            "task": "storyline",
            "provider": "mediakit",
            "status": "available_requires_authorization",
            "result_role": "candidate_evidence",
        },
        {
            "task": "editable_timeline_finishing",
            "provider": "chatcut",
            "status": "available_requires_authorization",
            "result_role": "candidate_timeline",
        },
    ]


def _result_role(task: str) -> str:
    if task in {
        "scene_segmentation",
        "storyline",
        "script_analysis",
        "visual_analysis",
    }:
        return "candidate_evidence"
    if task == "editable_timeline_finishing":
        return "candidate_timeline"
    return "candidate_artifact"


def _local_upload_artifacts(values: Iterable[Any]) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []
    for raw in values:
        if isinstance(raw, dict):
            raw_path = raw.get("path")
            purpose = str(raw.get("purpose") or "remote_processing").strip()
        else:
            raw_path = raw
            purpose = "remote_processing"
        path = Path(str(raw_path or "")).expanduser().resolve()
        if not path.is_file():
            raise ContractError(f"upload artifact not found or not a file: {path}")
        artifacts.append(
            {
                "path": str(path),
                "data_type": _data_type(path),
                "bytes": path.stat().st_size,
                "sha256": _sha256(path),
                "purpose": purpose,
            }
        )
    if not artifacts:
        raise ContractError(
            "upload authorization requires at least one explicit artifact"
        )
    return artifacts


def _upload_snapshot(
    *, schema: str, source: str, artifacts: list[dict[str, Any]], allowed: bool
) -> dict[str, Any]:
    return {
        "schema": schema,
        "source": source,
        "allowed": allowed,
        "automatic": False,
        "artifact_count": len(artifacts),
        "total_bytes": sum(_artifact_bytes(row) for row in artifacts),
        "manifest_sha256": _snapshot_manifest_sha256(schema, artifacts)
        if artifacts
        else "",
        "artifacts": artifacts,
    }


def _validate_upload_snapshot(upload: dict[str, Any]) -> None:
    artifacts = (
        upload.get("artifacts") if isinstance(upload.get("artifacts"), list) else []
    )
    if upload.get("allowed") and not artifacts:
        raise ContractError("upload authorization requires an artifact manifest")
    if upload.get("artifact_count") != len(artifacts):
        raise ContractError("upload artifact count changed")
    if upload.get("total_bytes") != sum(_artifact_bytes(row) for row in artifacts):
        raise ContractError("upload artifact byte total changed")
    expected_hash = (
        _snapshot_manifest_sha256(str(upload.get("schema") or ""), artifacts)
        if artifacts
        else ""
    )
    if upload.get("manifest_sha256") != expected_hash:
        raise ContractError("upload manifest integrity check failed")
    for artifact in artifacts:
        _validate_file_record(artifact)


def _snapshot_manifest_sha256(schema: str, files: list[dict[str, Any]]) -> str:
    if schema == VKP_UPLOAD_MANIFEST_SCHEMA:
        return _manifest_sha256(files)
    canonical = [
        {
            "path": str(row.get("path") or ""),
            "data_type": str(row.get("data_type") or ""),
            "bytes": _artifact_bytes(row),
            "sha256": str(row.get("sha256") or ""),
            "purpose": str(row.get("purpose") or ""),
        }
        for row in files
    ]
    return hashlib.sha256(
        json.dumps(
            canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


def _validate_file_record(artifact: Any) -> None:
    if not isinstance(artifact, dict):
        raise ContractError("upload artifact manifest is incomplete")
    path = Path(str(artifact.get("path") or "")).expanduser().resolve()
    expected_hash = str(artifact.get("sha256") or "")
    expected_bytes = _artifact_bytes(artifact)
    if not path.is_file() or len(expected_hash) != 64:
        raise ContractError("upload artifact manifest is incomplete")
    if path.stat().st_size != expected_bytes or _sha256(path) != expected_hash:
        raise ContractError(f"upload artifact changed after authorization: {path}")


def _manifest_sha256(files: list[dict[str, Any]]) -> str:
    canonical = [
        {
            "path": str(row.get("path") or ""),
            "data_type": str(row.get("data_type") or ""),
            "bytes": _artifact_bytes(row),
            "sha256": str(row.get("sha256") or ""),
        }
        for row in files
    ]
    return hashlib.sha256(
        json.dumps(
            canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


def _route_snapshot_sha256(route: dict[str, Any], deployments: list[Any]) -> str:
    normalized = [_normalized_deployment(row) for row in deployments]
    canonical = {
        "route_id": str(route.get("route_id") or ""),
        "route_revision": str(route.get("route_revision") or ""),
        "virtual_model": str(route.get("virtual_model") or ""),
        "execution_location": str(route.get("execution_location") or ""),
        "deployments": normalized,
    }
    return hashlib.sha256(
        json.dumps(
            canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


def _normalized_deployment(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError("VKP consent deployment must be an object")
    result = {
        "provider": str(value.get("provider") or ""),
        "model": str(value.get("model") or ""),
        "base_url": _normalized_url(str(value.get("base_url") or "")),
        "interface": str(value.get("interface") or "openai_compatible"),
    }
    if not all(result.values()):
        raise ContractError("VKP consent deployment is incomplete")
    for key in ("id", "adapter_backend"):
        if str(value.get(key) or "").strip():
            result[key] = str(value[key]).strip()
    if value.get("timeout_seconds") not in (None, ""):
        result["timeout_seconds"] = int(value["timeout_seconds"])
    return result


def _normalized_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    if not parsed.scheme or not parsed.netloc:
        return value.rstrip("/")
    return urllib.parse.urlunsplit(
        (parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip("/"), "", "")
    )


def _payload_sha256(payload: dict[str, Any], hash_key: str) -> str:
    value = dict(payload)
    value.pop(hash_key, None)
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


def _tasks(value: Any) -> list[str]:
    if value in (None, []):
        return []
    if not isinstance(value, list):
        raise ContractError("remote policy tasks must be a JSON array")
    return list(dict.fromkeys(str(item).strip() for item in value if str(item).strip()))


def _validate_limits(limits: dict[str, Any]) -> None:
    _positive_int(limits.get("max_calls"), "limits.max_calls")
    _nonnegative_money(limits.get("max_cost"), "limits.max_cost")
    if not str(limits.get("currency") or "").strip():
        raise ContractError("limits.currency is required")


def _artifact_bytes(row: Any) -> int:
    if not isinstance(row, dict):
        raise ContractError("artifact record must be an object")
    value = row.get("bytes")
    if isinstance(value, bool):
        raise ContractError("artifact bytes must be a non-negative integer")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ContractError("artifact bytes must be a non-negative integer") from exc
    if number < 0:
        raise ContractError("artifact bytes must be a non-negative integer")
    return number


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise ContractError(f"{label} must be a positive integer")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{label} must be a positive integer") from exc
    if number <= 0:
        raise ContractError(f"{label} must be a positive integer")
    return number


def _positive_money(value: Any, label: str) -> float:
    amount = _nonnegative_money(value, label)
    if amount <= 0:
        raise ContractError(f"{label} must be positive")
    return amount


def _nonnegative_money(value: Any, label: str) -> float:
    if isinstance(value, bool) or value is None:
        raise ContractError(f"{label} must be a non-negative finite amount")
    try:
        amount = round(float(value), 8)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{label} must be a non-negative finite amount") from exc
    if amount < 0 or not math.isfinite(amount):
        raise ContractError(f"{label} must be a non-negative finite amount")
    return amount


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _strict_bool(value: Any, label: str) -> bool:
    if value is None:
        return False
    if not isinstance(value, bool):
        raise ContractError(f"{label} must be a JSON boolean")
    return value


def _parse_datetime(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _data_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".txt", ".md", ".json", ".jsonl", ".srt", ".vtt", ".csv", ".tsv"}:
        return "text"
    if suffix in {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}:
        return "image"
    if suffix in {".wav", ".mp3", ".m4a", ".flac", ".aac", ".ogg", ".opus"}:
        return "audio"
    if suffix in {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}:
        return "video"
    return "binary"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _require_equal(actual: Any, expected: Any, message: str) -> None:
    if actual != expected:
        raise ContractError(message)
