"""Thin consumer adapter for the external canonical model-provider-gateway."""

from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterable
from urllib.parse import urlparse

from jsonschema import Draft202012Validator, SchemaError, ValidationError

from .contract_io import (
    atomic_write_json,
    canonical_json_sha256,
    file_sha256,
    payload_sha256,
    read_json_object,
)
from .errors import ContractError

GATEWAY_CANONICAL_COMMIT = "c5f3ec49644453e0cddb56350e3b243b49e0f7da"
GATEWAY_CONSUMER_ID = "mvp_video_pipeline"
GATEWAY_RECEIPT_SCHEMA = "video_creation_pipeline.gateway_candidate_receipt.v1"
GATEWAY_DOCTOR_SCHEMA = "video_creation_pipeline.gateway_consumer_doctor.v1"
GATEWAY_SEMANTIC_REVISION = "video_creation_pipeline.gateway_semantic_compat.v1"

_REQUIRED_HTTP_API_SCHEMA = "model_provider_gateway.http.v1"
_REQUIRED_ADAPTER_SCHEMA = "model_provider_gateway.adapter_contract.v2"
_REQUIRED_ADAPTER_REQUEST_SCHEMA = "model_provider_gateway.adapter_request.v2"
_REQUIRED_REQUEST_SCHEMA = "model_provider_gateway.execution_request.v1"
_REQUIRED_RESULT_SCHEMA = "model_provider_gateway.execution_result.v1"
_REQUIRED_CONSENT_SCHEMA = "model_provider_gateway.consent_manifest.v2"
_REQUIRED_FORBIDDEN_FIELDS = {
    "provider_url",
    "api_key",
    "headers",
    "raw_json",
    "fallback",
}
_REQUIRED_GATEWAY_INVARIANTS = {
    "provider_protocol_adapter": "litellm_only",
    "arbitrary_urls_allowed": False,
    "silent_fallback_allowed": False,
    "automatic_retry_allowed": False,
    "saving_profile_is_consent": False,
    "real_provider_called": False,
    "file_upload_requires_exact_manifest_and_user_approval": True,
}

_CONTRACT_FILES = {
    "profile": "gateway-profile.json",
    "route": "gateway-route.json",
    "consent": "gateway-consent.json",
    "request": "gateway-request.json",
    "result": "gateway-result.json",
}
_RECEIPT_FILE = "candidate-receipt.json"

_USE_CASES: dict[str, dict[str, str]] = {
    "script": {
        "gateway_task": "script",
        "capability": "text",
        "candidate_role": "script_candidate",
        "execution_status": "enabled",
    },
    "storyboard": {
        "gateway_task": "script",
        "capability": "text",
        "candidate_role": "storyboard_candidate",
        "execution_status": "enabled",
    },
    "narration": {
        "gateway_task": "script",
        "capability": "text",
        "candidate_role": "narration_candidate",
        "execution_status": "enabled",
    },
    "visual": {
        "gateway_task": "visual",
        "capability": "vision",
        "candidate_role": "visual_candidate",
        "execution_status": "enabled",
    },
    "tts": {
        "gateway_task": "tts",
        "capability": "tts",
        "candidate_role": "audio_candidate",
        "execution_status": "blocked",
        "blocked_reason": "no audited shared Gateway TTS execution preset",
    },
    "asr": {
        "gateway_task": "asr",
        "capability": "asr",
        "candidate_role": "transcript_candidate",
        "execution_status": "blocked",
        "blocked_reason": "ASR remains an explicit owner-controlled local route",
    },
    "ocr": {
        "gateway_task": "ocr",
        "capability": "ocr",
        "candidate_role": "ocr_candidate",
        "execution_status": "blocked",
        "blocked_reason": "no audited shared Gateway OCR execution contract",
    },
    "embedding": {
        "gateway_task": "embedding",
        "capability": "embedding",
        "candidate_role": "embedding_candidate",
        "execution_status": "blocked",
        "blocked_reason": "no audited shared Gateway embedding execution contract",
    },
}


def gateway_capability_matrix() -> list[dict[str, str]]:
    return [
        {"use_case": use_case, **dict(spec)} for use_case, spec in _USE_CASES.items()
    ]


def build_gateway_doctor_report(gateway_root: str | Path) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    try:
        runtime = _load_gateway_runtime(gateway_root)
        gateway_doctor = runtime.doctor()
        adapter = runtime.adapter_contract(GATEWAY_CONSUMER_ID)
        _validate_gateway_truth(gateway_doctor, adapter)
    except (ContractError, OSError, RuntimeError, ValueError) as exc:
        checks.append(
            {
                "id": "gateway_canonical_runtime",
                "status": "fail",
                "blocking": True,
                "detail": _safe_gateway_error(exc),
            }
        )
        return {
            "schema": GATEWAY_DOCTOR_SCHEMA,
            "status": "blocked",
            "consumer_id": GATEWAY_CONSUMER_ID,
            "canonical_commit": GATEWAY_CANONICAL_COMMIT,
            "canonical_commit_role": "historical_provenance_evidence_only",
            "package_commit": None,
            "package_version": None,
            "release_identity_is_compatibility_gate": False,
            "checks": checks,
            "capabilities": gateway_capability_matrix(),
            "real_provider_called": False,
        }

    checks.extend(
        [
            {
                "id": "gateway_semantic_runtime",
                "status": "pass",
                "blocking": True,
                "detail": runtime.semantic_contract["revision"],
            },
            {
                "id": "gateway_adapter_contract",
                "status": "pass",
                "blocking": True,
                "detail": adapter["schema"],
            },
            {
                "id": "gateway_execution_truth",
                "status": "pass",
                "blocking": True,
                "detail": "text/vision enabled; asr/ocr/tts/embedding blocked",
            },
        ]
    )
    return {
        "schema": GATEWAY_DOCTOR_SCHEMA,
        "status": "ready",
        "consumer_id": GATEWAY_CONSUMER_ID,
        "canonical_commit": runtime.canonical_commit,
        "canonical_commit_role": runtime.canonical_commit_role,
        "package_commit": runtime.checkout_commit,
        "package_version": runtime.version,
        "source_tree_dirty": runtime.source_tree_dirty,
        "release_identity_is_compatibility_gate": False,
        "semantic_contract": runtime.semantic_contract,
        "schema_sha256": runtime.schema_sha256,
        "gateway_component_sha256": runtime.gateway_component_sha256,
        "gateway_doctor": gateway_doctor,
        "adapter_contract": adapter,
        "checks": checks,
        "capabilities": gateway_capability_matrix(),
        "real_provider_called": False,
    }


def run_gateway_loopback(
    gateway_root: str | Path,
    *,
    use_case: str,
    artifact_path: str | Path | None,
    instruction: str,
    output_root: str | Path,
    trigger: str = "",
    allowed_evidence_refs: Iterable[str] = (),
    fact_assertions: Iterable[dict[str, Any]] = (),
) -> dict[str, Any]:
    spec = _use_case(use_case)
    runtime = _load_gateway_runtime(gateway_root)
    _validate_gateway_truth(
        runtime.doctor(), runtime.adapter_contract(GATEWAY_CONSUMER_ID)
    )
    root = _prepare_output_root(output_root)
    if spec["execution_status"] != "enabled":
        receipt = _blocked_receipt(use_case, spec, runtime)
        atomic_write_json(receipt, root / _RECEIPT_FILE)
        return receipt
    if artifact_path is None:
        raise ContractError(
            "Gateway text/vision execution requires one explicit artifact"
        )
    if not instruction.strip():
        raise ContractError("Gateway instruction must not be empty")
    if trigger not in {"", "400", "429", "500", "timeout"}:
        raise ContractError("unsupported Gateway loopback trigger")

    capability = spec["capability"]
    preset = "modelscope-glm-5-2" if capability == "text" else "siliconflow-glm-4-5v"
    profile = runtime.build_profile(
        preset,
        "env:MVP_GATEWAY_LOOPBACK_ONLY",
        profile_id=f"mvp-loopback-{capability}",
        capabilities=[capability],
        timeout_seconds=1,
        max_attempts=1,
        max_calls=1,
        max_cost_usd=1.0,
        last_verified="2026-08-05T00:00:00+00:00",
    )
    route = runtime.build_route_plan(
        f"mvp-{use_case}-loopback",
        capability,
        [profile],
        fallback_enabled=False,
        max_calls=1,
        max_cost_usd=1.0,
    )
    consent = runtime.build_consent_plan(
        route,
        [Path(artifact_path).expanduser().resolve()],
        capability=capability,
        purpose=instruction,
        data_type="synthetic_fixture",
        max_calls=1,
        max_cost_usd=1.0,
    )
    input_hash = consent["input_policy"]["input_manifest_sha256"]
    request = runtime.build_execution_request(
        profile,
        route,
        consent,
        consumer_id=GATEWAY_CONSUMER_ID,
        task=spec["gateway_task"],
        max_estimated_cost_usd=0.1,
        request_id=f"mvp-{use_case}-{input_hash[:24]}",
    )
    runtime.validate_adapter_request(
        {
            "schema": "model_provider_gateway.adapter_request.v2",
            "consumer_id": GATEWAY_CONSUMER_ID,
            "task": spec["gateway_task"],
            "required_capabilities": [capability],
            "route_id": route["route_id"],
            "route_revision": route["route_revision"],
            "consent_hash": consent["consent_sha256"],
            "input_manifest_hash": input_hash,
        },
        runtime.adapter_contract(GATEWAY_CONSUMER_ID),
    )

    for role, filename in _CONTRACT_FILES.items():
        if role == "result":
            continue
        payload = {
            "profile": profile,
            "route": route,
            "consent": consent,
            "request": request,
        }[role]
        atomic_write_json(payload, root / filename)

    status = int(trigger) if trigger in {"400", "429", "500"} else 200
    delay = 1.3 if trigger == "timeout" else 0.0
    with runtime.mock_server(status=status, delay_seconds=delay) as base_url:
        transport = runtime.LiteLLMTransport.loopback_stub(base_url)
        _validate_loopback_transport(runtime, transport)
        result = runtime.execute_model_request(
            request,
            profile=profile,
            route=route,
            consent=consent,
            ledger_path=root / "gateway-ledger.json",
            transport=transport,
            secret_resolver=lambda _ref: "synthetic-loopback-key",
        )
    atomic_write_json(result, root / _CONTRACT_FILES["result"])
    return project_gateway_result(
        gateway_root,
        bundle_root=root,
        use_case=use_case,
        allowed_evidence_refs=allowed_evidence_refs,
        fact_assertions=fact_assertions,
    )


def project_gateway_result(
    gateway_root: str | Path,
    *,
    bundle_root: str | Path,
    use_case: str,
    allowed_evidence_refs: Iterable[str] = (),
    fact_assertions: Iterable[dict[str, Any]] = (),
) -> dict[str, Any]:
    spec = _use_case(use_case)
    if spec["execution_status"] != "enabled":
        raise ContractError("blocked Gateway capabilities cannot project candidates")
    root = Path(bundle_root).expanduser().resolve()
    runtime = _load_gateway_runtime(gateway_root)
    contracts = _load_and_validate_contracts(runtime, root, spec)
    receipt = _build_candidate_receipt(
        runtime,
        root,
        use_case,
        spec,
        contracts,
        sorted(set(str(value) for value in allowed_evidence_refs if str(value))),
        [dict(value) for value in fact_assertions],
    )
    atomic_write_json(receipt, root / _RECEIPT_FILE)
    return receipt


def verify_gateway_bundle(
    gateway_root: str | Path,
    bundle_root: str | Path,
) -> dict[str, Any]:
    root = Path(bundle_root).expanduser().resolve()
    receipt = read_json_object(root / _RECEIPT_FILE, "Gateway candidate receipt")
    spec = _use_case(str(receipt.get("use_case") or ""))
    if spec["execution_status"] != "enabled":
        validate_gateway_candidate_receipt(receipt)
        return {
            "schema": "video_creation_pipeline.gateway_bundle_validation.v1",
            "status": "blocked_capability_confirmed",
            "valid": True,
            "candidate_only": True,
            "receipt_sha256": receipt["receipt_sha256"],
        }
    runtime = _load_gateway_runtime(gateway_root)
    contracts = _load_and_validate_contracts(runtime, root, spec)
    _validate_receipt_bindings(receipt, contracts)
    validate_gateway_candidate_receipt(receipt)
    _validate_bundle_file_refs(root, receipt["bundle_files"])
    return {
        "schema": "video_creation_pipeline.gateway_bundle_validation.v1",
        "status": "valid_candidate_only",
        "valid": True,
        "candidate_only": True,
        "receipt_sha256": receipt["receipt_sha256"],
        "candidate_sha256": (
            receipt["candidate"]["candidate_sha256"]
            if receipt.get("candidate")
            else None
        ),
        "gateway_result_status": contracts.result["status"],
    }


def validate_gateway_candidate_receipt(receipt: dict[str, Any]) -> dict[str, Any]:
    if receipt.get("schema") != GATEWAY_RECEIPT_SCHEMA:
        raise ContractError("unsupported Gateway candidate receipt schema")
    if receipt.get("consumer_id") != GATEWAY_CONSUMER_ID:
        raise ContractError("Gateway candidate receipt consumer drifted")
    if receipt.get("candidate_only") is not True:
        raise ContractError("Gateway output must remain candidate_only")
    boundary = receipt.get("execution_boundary") or {}
    required_false = (
        "publication_authorized",
        "published",
        "automatic_retry_used",
        "automatic_fallback_used",
        "technical_qa_authorizes_publication",
    )
    if any(boundary.get(field) is not False for field in required_false):
        raise ContractError("Gateway candidate execution boundary is unsafe")

    use_case = str(receipt.get("use_case") or "")
    spec = _use_case(use_case)
    if receipt.get("capability") != spec["capability"]:
        raise ContractError("Gateway candidate capability drifted")
    if receipt.get("gateway_task") != spec["gateway_task"]:
        raise ContractError("Gateway candidate task drifted")
    if receipt.get("candidate_role") != spec["candidate_role"]:
        raise ContractError("Gateway candidate role drifted")

    _validate_recorded_runtime_provenance(receipt.get("gateway_provenance") or {})
    _validate_fact_policy(receipt.get("fact_policy") or {})
    if spec["execution_status"] != "enabled":
        if receipt.get("status") != "blocked" or receipt.get("candidate") is not None:
            raise ContractError("blocked Gateway capability produced a candidate")
        if receipt.get("gateway_result") is not None:
            raise ContractError(
                "blocked Gateway capability cannot have an execution result"
            )
    else:
        _validate_enabled_receipt(receipt, spec)
    if receipt.get("receipt_sha256") != payload_sha256(receipt, "receipt_sha256"):
        raise ContractError("Gateway candidate receipt integrity check failed")
    return receipt


def _load_and_validate_contracts(
    runtime: SimpleNamespace,
    root: Path,
    spec: dict[str, str],
) -> SimpleNamespace:
    values = {
        role: read_json_object(root / filename, f"Gateway {role}")
        for role, filename in _CONTRACT_FILES.items()
    }
    profile = values["profile"]
    route = values["route"]
    consent = values["consent"]
    request = values["request"]
    result = values["result"]
    runtime.validate_profile(profile)
    retry_policy = profile.get("retry_policy") or {}
    if (
        retry_policy.get("max_attempts") != 1
        or retry_policy.get("retry_statuses") not in ([], None)
        or profile.get("fallback_pool") not in ([], None)
    ):
        raise ContractError("MVP Gateway profiles cannot authorize automatic retry")
    runtime.validate_route_plan(route)
    if (
        route.get("fallback_policy", {}).get("enabled") is not False
        or len(route.get("members") or []) != 1
    ):
        raise ContractError("MVP Gateway routes cannot authorize fallback")
    runtime.validate_consent(consent, route=route)
    if (
        consent.get("schema") != _REQUIRED_CONSENT_SCHEMA
        or consent.get("fallback_authorized") is not False
    ):
        raise ContractError("MVP Gateway requires consent_manifest.v2 without fallback")
    runtime.validate_execution_request(
        request,
        profile=profile,
        route=route,
        consent=consent,
    )
    if request.get("consumer_id") != GATEWAY_CONSUMER_ID:
        raise ContractError("Gateway request consumer drifted")
    if request.get("task") != spec["gateway_task"]:
        raise ContractError("Gateway request task drifted")
    if request.get("schema") != _REQUIRED_REQUEST_SCHEMA:
        raise ContractError("Gateway request schema is incompatible")
    _validate_gateway_result_schema(runtime.execution_result_schema, result)
    _validate_gateway_result_semantics(result)
    expected = {
        "request_id": request["request_id"],
        "provider_id": request["provider_id"],
        "model": request["model"],
        "capability": request["capability"],
        "route_hash": request["route_revision"],
        "consent_hash": request["consent_hash"],
        "input_manifest_hash": request["input_manifest_hash"],
    }
    for field, value in expected.items():
        if result.get(field) != value:
            raise ContractError(f"Gateway result {field} drifted")
    audit = result.get("redacted_audit") or {}
    if audit.get("automatic_fallback_used") is not False:
        raise ContractError("Gateway result reports an unauthorized fallback")
    return SimpleNamespace(**values)


def _build_candidate_receipt(
    runtime: SimpleNamespace,
    root: Path,
    use_case: str,
    spec: dict[str, str],
    contracts: SimpleNamespace,
    allowed_evidence_refs: list[str],
    fact_assertions: list[dict[str, Any]],
) -> dict[str, Any]:
    result = contracts.result
    output_text = (
        str(result["output"]["text"])
        if result.get("status") == "completed" and result.get("output")
        else None
    )
    candidate_core = {
        "use_case": use_case,
        "capability": spec["capability"],
        "candidate_role": spec["candidate_role"],
        "input_manifest_hash": contracts.request["input_manifest_hash"],
        "output_text": output_text,
        "allowed_evidence_refs": allowed_evidence_refs,
        "fact_assertions": fact_assertions,
    }
    candidate = None
    if output_text is not None:
        candidate = {
            "role": spec["candidate_role"],
            "text": output_text,
            "text_sha256": canonical_json_sha256({"text": output_text}),
            "candidate_sha256": canonical_json_sha256(candidate_core),
            "acceptance_status": "candidate_only",
        }
    status_map = {
        "completed": "completed_candidate_only",
        "failed": "failed_candidate_only",
        "blocked": "blocked",
    }
    receipt = {
        "schema": GATEWAY_RECEIPT_SCHEMA,
        "status": status_map[result["status"]],
        "consumer_id": GATEWAY_CONSUMER_ID,
        "use_case": use_case,
        "gateway_task": spec["gateway_task"],
        "capability": spec["capability"],
        "candidate_role": spec["candidate_role"],
        "candidate_only": True,
        "gateway_provenance": {
            **_runtime_provenance(runtime),
            "adapter_schema": _REQUIRED_ADAPTER_SCHEMA,
            "request_schema": contracts.request["schema"],
            "result_schema": result["schema"],
            "profile_id": contracts.profile["profile_id"],
            "profile_revision": contracts.profile["route_revision"],
            "route_id": contracts.route["route_id"],
            "route_revision": contracts.route["route_revision"],
            "consent_hash": contracts.consent["consent_sha256"],
            "input_manifest_hash": contracts.request["input_manifest_hash"],
            "provider_id": contracts.request["provider_id"],
            "model": contracts.request["model"],
            "transport": result["redacted_audit"]["transport"],
        },
        "artifact_manifest": list(result["artifact_refs"]),
        "gateway_result": result,
        "candidate": candidate,
        "fact_policy": {
            "status": "unverified_model_candidate",
            "verified_fact_count": 0,
            "allowed_evidence_refs": allowed_evidence_refs,
            "fact_assertions": fact_assertions,
        },
        "execution_boundary": {
            "loopback_stub": result["redacted_audit"]["transport"] == "loopback_stub",
            "real_provider_called": result["redacted_audit"]["transport"]
            != "loopback_stub",
            "real_secret_read": False,
            "real_media_uploaded": False,
            "publication_authorized": False,
            "published": False,
            "automatic_retry_used": False,
            "automatic_fallback_used": False,
            "technical_qa_authorizes_publication": False,
        },
        "bundle_files": _bundle_file_refs(root),
    }
    receipt["receipt_sha256"] = payload_sha256(receipt, "receipt_sha256")
    validate_gateway_candidate_receipt(receipt)
    return receipt


def _blocked_receipt(
    use_case: str, spec: dict[str, str], runtime: SimpleNamespace
) -> dict[str, Any]:
    receipt = {
        "schema": GATEWAY_RECEIPT_SCHEMA,
        "status": "blocked",
        "consumer_id": GATEWAY_CONSUMER_ID,
        "use_case": use_case,
        "gateway_task": spec["gateway_task"],
        "capability": spec["capability"],
        "candidate_role": spec["candidate_role"],
        "candidate_only": True,
        "blocked_reason": spec["blocked_reason"],
        "gateway_provenance": {
            **_runtime_provenance(runtime),
            "adapter_schema": _REQUIRED_ADAPTER_SCHEMA,
        },
        "artifact_manifest": [],
        "gateway_result": None,
        "candidate": None,
        "fact_policy": {
            "status": "unverified_model_candidate",
            "verified_fact_count": 0,
            "allowed_evidence_refs": [],
            "fact_assertions": [],
        },
        "execution_boundary": {
            "loopback_stub": False,
            "real_provider_called": False,
            "real_secret_read": False,
            "real_media_uploaded": False,
            "publication_authorized": False,
            "published": False,
            "automatic_retry_used": False,
            "automatic_fallback_used": False,
            "technical_qa_authorizes_publication": False,
        },
        "bundle_files": [],
    }
    receipt["receipt_sha256"] = payload_sha256(receipt, "receipt_sha256")
    validate_gateway_candidate_receipt(receipt)
    return receipt


def _validate_enabled_receipt(receipt: dict[str, Any], spec: dict[str, str]) -> None:
    result = receipt.get("gateway_result")
    if not isinstance(result, dict):
        raise ContractError("enabled Gateway candidate requires an execution result")
    provenance = receipt.get("gateway_provenance") or {}
    _validate_recorded_runtime_provenance(provenance)
    expected = {
        "adapter_schema": _REQUIRED_ADAPTER_SCHEMA,
        "request_schema": _REQUIRED_REQUEST_SCHEMA,
        "result_schema": _REQUIRED_RESULT_SCHEMA,
        "route_revision": result.get("route_hash"),
        "consent_hash": result.get("consent_hash"),
        "input_manifest_hash": result.get("input_manifest_hash"),
        "provider_id": result.get("provider_id"),
        "model": result.get("model"),
        "transport": (result.get("redacted_audit") or {}).get("transport"),
    }
    for field, value in expected.items():
        if provenance.get(field) != value:
            raise ContractError(f"Gateway receipt provenance {field} drifted")
    _validate_gateway_result_semantics(result)
    audit = result.get("redacted_audit") or {}
    for field in (
        "input_paths_logged",
        "input_content_logged",
        "secret_logged",
        "automatic_fallback_used",
    ):
        if audit.get(field) is not False:
            raise ContractError("Gateway result redaction or fallback boundary failed")
    status_map = {
        "completed": "completed_candidate_only",
        "failed": "failed_candidate_only",
        "blocked": "blocked",
    }
    if receipt.get("status") != status_map.get(result.get("status")):
        raise ContractError("Gateway receipt status drifted from its result")
    candidate = receipt.get("candidate")
    if result.get("status") == "completed":
        output = result.get("output") or {}
        if not result.get("ok") or not str(output.get("text") or ""):
            raise ContractError("completed Gateway result has no candidate text")
        if not isinstance(candidate, dict) or candidate.get("text") != output["text"]:
            raise ContractError("Gateway candidate text drifted from its result")
        if candidate.get("role") != spec["candidate_role"]:
            raise ContractError("Gateway candidate output role drifted")
        core = {
            "use_case": receipt["use_case"],
            "capability": receipt["capability"],
            "candidate_role": receipt["candidate_role"],
            "input_manifest_hash": provenance["input_manifest_hash"],
            "output_text": candidate["text"],
            "allowed_evidence_refs": receipt["fact_policy"]["allowed_evidence_refs"],
            "fact_assertions": receipt["fact_policy"]["fact_assertions"],
        }
        if candidate.get("candidate_sha256") != canonical_json_sha256(core):
            raise ContractError("Gateway candidate content integrity check failed")
    elif candidate is not None:
        raise ContractError(
            "failed or blocked Gateway result cannot produce a candidate"
        )


def _validate_fact_policy(policy: dict[str, Any]) -> None:
    if policy.get("status") != "unverified_model_candidate":
        raise ContractError("Gateway output cannot be promoted to verified fact")
    if policy.get("verified_fact_count") != 0:
        raise ContractError("Gateway candidate cannot add verified facts")
    allowed = set(policy.get("allowed_evidence_refs") or [])
    assertions = policy.get("fact_assertions")
    if not isinstance(assertions, list):
        raise ContractError("Gateway fact assertions must be a list")
    for assertion in assertions:
        if (
            not isinstance(assertion, dict)
            or assertion.get("status") != "candidate_supported"
        ):
            raise ContractError("Gateway fact assertion status is invalid")
        refs = set(assertion.get("evidence_refs") or [])
        if not str(assertion.get("text") or "").strip() or not refs:
            raise ContractError("Gateway fact assertion requires evidence")
        if not refs <= allowed:
            raise ContractError("Gateway fact assertion adds evidence-free facts")


def _validate_receipt_bindings(
    receipt: dict[str, Any], contracts: SimpleNamespace
) -> None:
    provenance = receipt.get("gateway_provenance") or {}
    expected = {
        "profile_id": contracts.profile["profile_id"],
        "profile_revision": contracts.profile["route_revision"],
        "route_id": contracts.route["route_id"],
        "route_revision": contracts.route["route_revision"],
        "consent_hash": contracts.consent["consent_sha256"],
        "input_manifest_hash": contracts.request["input_manifest_hash"],
        "provider_id": contracts.request["provider_id"],
        "model": contracts.request["model"],
    }
    for field, value in expected.items():
        if provenance.get(field) != value:
            raise ContractError(f"Gateway bundle {field} drifted")
    if receipt.get("gateway_result") != contracts.result:
        raise ContractError("Gateway result changed after candidate projection")


def _bundle_file_refs(root: Path) -> list[dict[str, Any]]:
    refs = []
    for role, filename in _CONTRACT_FILES.items():
        path = root / filename
        payload = read_json_object(path, f"Gateway {role}")
        refs.append(
            {
                "role": role,
                "filename": filename,
                "bytes": path.stat().st_size,
                "sha256": file_sha256(path),
                "canonical_sha256": canonical_json_sha256(payload),
            }
        )
    return refs


def _validate_bundle_file_refs(root: Path, refs: list[dict[str, Any]]) -> None:
    if refs != _bundle_file_refs(root):
        raise ContractError("Gateway bundle file provenance drifted")


def _validate_gateway_result_schema(
    schema: dict[str, Any], result: dict[str, Any]
) -> None:
    try:
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(result)
    except (SchemaError, ValidationError) as exc:
        raise ContractError("Gateway result schema validation failed") from exc


def _validate_gateway_result_semantics(result: dict[str, Any]) -> None:
    status = result.get("status")
    ok = result.get("ok")
    error_class = result.get("error_class")
    error_message = result.get("error_message")
    retryable = result.get("retryable")
    output = result.get("output")
    if status == "completed":
        if (
            ok is not True
            or error_class is not None
            or error_message is not None
            or retryable is not False
            or not isinstance(output, dict)
            or not str(output.get("text") or "")
        ):
            raise ContractError("Gateway completed result semantics are incompatible")
        return
    if status not in {"failed", "blocked"}:
        raise ContractError("Gateway result status semantics are incompatible")
    if (
        ok is not False
        or output is not None
        or not isinstance(error_class, str)
        or not error_class
        or not isinstance(error_message, str)
        or not error_message
        or len(error_message) > 256
        or "\n" in error_message
        or "\r" in error_message
        or not isinstance(retryable, bool)
    ):
        raise ContractError("Gateway failure result semantics are incompatible")
    retryable_classes = {"rate_limited", "provider_server_error", "timeout"}
    if retryable != (error_class in retryable_classes):
        raise ContractError("Gateway retryable error semantics are incompatible")
    if status == "blocked" and retryable is not False:
        raise ContractError("Gateway blocked result cannot be retryable")


def _required_semantic_contract() -> dict[str, Any]:
    return {
        "revision": GATEWAY_SEMANTIC_REVISION,
        "http_api_schema": _REQUIRED_HTTP_API_SCHEMA,
        "adapter_schema": _REQUIRED_ADAPTER_SCHEMA,
        "adapter_request_schema": _REQUIRED_ADAPTER_REQUEST_SCHEMA,
        "request_schema": _REQUIRED_REQUEST_SCHEMA,
        "result_schema": _REQUIRED_RESULT_SCHEMA,
        "consent_schema": _REQUIRED_CONSENT_SCHEMA,
        "required_capabilities": ["text", "vision"],
        "blocked_capabilities": ["asr", "embedding", "ocr", "tts"],
        "error_fields": ["error_class", "error_message", "retryable"],
        "no_silent_fallback": True,
        "no_automatic_retry": True,
        "exact_upload_consent_required": True,
    }


def _validate_gateway_semantics(
    *,
    http_api_schema: Any,
    adapter_request_schema: Any,
    consent_schema: Any,
    request_schema: Any,
    result_schema: Any,
    request_schema_document: dict[str, Any],
    result_schema_document: dict[str, Any],
    matrix: dict[str, Any],
    adapter: dict[str, Any],
) -> dict[str, Any]:
    expected = _required_semantic_contract()
    observed_schemas = {
        "http_api_schema": http_api_schema,
        "adapter_schema": adapter.get("schema"),
        "adapter_request_schema": adapter_request_schema,
        "request_schema": request_schema,
        "result_schema": result_schema,
        "consent_schema": consent_schema,
    }
    for field in (
        "http_api_schema",
        "adapter_schema",
        "adapter_request_schema",
        "request_schema",
        "result_schema",
        "consent_schema",
    ):
        if observed_schemas.get(field) != expected[field]:
            raise ContractError(f"Gateway semantic schema is incompatible: {field}")

    request_properties = request_schema_document.get("properties") or {}
    request_required = set(request_schema_document.get("required") or [])
    if (
        request_schema_document.get("additionalProperties") is not False
        or request_properties.get("schema", {}).get("const") != _REQUIRED_REQUEST_SCHEMA
        or not {
            "consumer_id",
            "task",
            "capability",
            "route_revision",
            "consent_hash",
            "instruction",
            "artifacts",
            "input_manifest_hash",
        }
        <= request_required
        or not {"text", "vision"}
        <= set(request_properties.get("capability", {}).get("enum") or [])
    ):
        raise ContractError("Gateway execution request schema document is incompatible")

    result_properties = result_schema_document.get("properties") or {}
    result_required = set(result_schema_document.get("required") or [])
    result_audit = result_properties.get("redacted_audit", {}).get("properties") or {}
    if (
        result_schema_document.get("additionalProperties") is not False
        or result_properties.get("schema", {}).get("const") != _REQUIRED_RESULT_SCHEMA
        or not {"status", "error_class", "error_message", "retryable", "redacted_audit"}
        <= result_required
        or not {"completed", "failed", "blocked"}
        <= set(result_properties.get("status", {}).get("enum") or [])
        or result_audit.get("automatic_fallback_used", {}).get("const") is not False
    ):
        raise ContractError("Gateway execution result schema document is incompatible")

    if adapter.get("consumer_id") != GATEWAY_CONSUMER_ID:
        raise ContractError("Gateway MVP adapter identity is incompatible")
    tasks = adapter.get("tasks") or {}
    required_tasks = {"script": "text", "visual": "vision"}
    for task, capability in required_tasks.items():
        row = tasks.get(task) or {}
        if (
            row.get("capabilities") != [capability]
            or row.get("execution_status") != "enabled"
            or row.get("control_policy") != "shared_consent"
        ):
            raise ContractError(f"Gateway MVP task capability is incompatible: {task}")
    for task in ("tts", "asr"):
        if (tasks.get(task) or {}).get("execution_status") != "blocked":
            raise ContractError(f"Gateway MVP task must remain blocked: {task}")
    if not _REQUIRED_FORBIDDEN_FIELDS <= set(adapter.get("forbidden_fields") or []):
        raise ContractError("Gateway adapter safety fields are incomplete")

    baseline = matrix.get("semantic_baseline") or {}
    for field in (
        "http_api_schema",
        "adapter_schema",
        "adapter_request_schema",
        "execution_request_schema",
        "execution_result_schema",
        "consent_schema",
    ):
        expected_field = {
            "execution_request_schema": _REQUIRED_REQUEST_SCHEMA,
            "execution_result_schema": _REQUIRED_RESULT_SCHEMA,
        }.get(field, expected.get(field))
        if baseline.get(field) != expected_field:
            raise ContractError(
                f"Gateway capability matrix schema is incompatible: {field}"
            )
    if (
        not set(expected["error_fields"]) <= set(baseline.get("error_fields") or [])
        or baseline.get("silent_fallback_allowed") is not False
        or baseline.get("automatic_retry_allowed") is not False
    ):
        raise ContractError(
            "Gateway capability matrix error semantics are incompatible"
        )

    consumers = matrix.get("consumers") or []
    consumer = next(
        (row for row in consumers if row.get("consumer_id") == GATEWAY_CONSUMER_ID),
        None,
    )
    if not consumer or consumer.get("integration_status") != "real_adapter":
        raise ContractError("Gateway MVP consumer capability is unavailable")
    consumer_capabilities = consumer.get("capabilities") or {}
    for capability in ("text", "vision"):
        if (consumer_capabilities.get(capability) or {}).get(
            "status"
        ) != "real_adapter":
            raise ContractError(
                f"Gateway required capability is unavailable: {capability}"
            )

    policies = {
        row.get("capability"): row for row in matrix.get("capability_policies") or []
    }
    for capability in ("text", "vision"):
        policy = policies.get(capability) or {}
        requirements = set(policy.get("remote_candidate_requires") or [])
        if (
            policy.get("shared_execution") != "enabled"
            or policy.get("default_location") != "explicit_route"
            or not {"profile", "route", "consent_v2", "budget", "timeout"}
            <= requirements
            or (capability == "vision" and "artifact_manifest" not in requirements)
        ):
            raise ContractError(
                f"Gateway capability safety policy is incompatible: {capability}"
            )
    for capability in ("asr", "ocr", "tts", "embedding"):
        if (policies.get(capability) or {}).get("shared_execution") != "blocked":
            raise ContractError(f"Gateway capability must remain blocked: {capability}")

    invariants = matrix.get("invariants") or {}
    for field, required in _REQUIRED_GATEWAY_INVARIANTS.items():
        if invariants.get(field) != required:
            raise ContractError(f"Gateway safety invariant is incompatible: {field}")
    return expected


def _validate_recorded_runtime_provenance(provenance: dict[str, Any]) -> None:
    if provenance.get("release_identity_is_compatibility_gate") is not False:
        raise ContractError("Gateway release identity cannot be a compatibility gate")
    if provenance.get("semantic_contract") != _required_semantic_contract():
        raise ContractError("Gateway receipt semantic contract drifted")
    for field in ("canonical_commit", "package_commit"):
        value = provenance.get(field)
        if value is not None and (
            not isinstance(value, str)
            or len(value) != 40
            or any(character not in "0123456789abcdef" for character in value)
        ):
            raise ContractError("Gateway receipt commit provenance is invalid")
    if not str(provenance.get("package_version") or "").strip():
        raise ContractError("Gateway receipt package version provenance is missing")
    matrix_revision = provenance.get("capability_matrix_revision")
    if (
        not isinstance(matrix_revision, str)
        or len(matrix_revision) != 64
        or any(character not in "0123456789abcdef" for character in matrix_revision)
    ):
        raise ContractError("Gateway capability matrix provenance is invalid")
    if provenance.get("source_tree_dirty") not in {True, False, None}:
        raise ContractError("Gateway source state provenance is invalid")
    for field in ("schema_sha256", "gateway_component_sha256"):
        hashes = provenance.get(field)
        if not isinstance(hashes, dict) or not hashes:
            raise ContractError("Gateway receipt provenance hashes are missing")
        for value in hashes.values():
            if (
                not isinstance(value, str)
                or len(value) != 64
                or any(character not in "0123456789abcdef" for character in value)
            ):
                raise ContractError("Gateway receipt provenance hash is invalid")


def _runtime_provenance(runtime: SimpleNamespace) -> dict[str, Any]:
    return {
        "canonical_commit": runtime.canonical_commit,
        "canonical_commit_role": runtime.canonical_commit_role,
        "package_commit": runtime.checkout_commit,
        "package_version": runtime.version,
        "source_tree_dirty": runtime.source_tree_dirty,
        "release_identity_is_compatibility_gate": False,
        "capability_matrix_revision": runtime.matrix_revision,
        "semantic_contract": dict(runtime.semantic_contract),
        "schema_sha256": dict(runtime.schema_sha256),
        "gateway_component_sha256": dict(runtime.gateway_component_sha256),
    }


def _validate_gateway_truth(doctor: dict[str, Any], adapter: dict[str, Any]) -> None:
    enabled = set(doctor.get("execution", {}).get("enabled_capabilities") or [])
    blocked = set(doctor.get("execution", {}).get("blocked_capabilities") or [])
    if doctor.get("ok") is not True or not {"text", "vision"} <= enabled:
        raise ContractError("Gateway text/vision execution truth is unavailable")
    if not {"asr", "ocr", "tts", "embedding"} <= blocked:
        raise ContractError("Gateway blocked capability truth drifted")
    if adapter.get("schema") != _REQUIRED_ADAPTER_SCHEMA:
        raise ContractError("Gateway MVP adapter schema drifted")
    tasks = adapter.get("tasks") or {}
    if tasks.get("script", {}).get("execution_status") != "enabled":
        raise ContractError("Gateway MVP script task is not enabled")
    if tasks.get("visual", {}).get("execution_status") != "enabled":
        raise ContractError("Gateway MVP visual task is not enabled")
    if tasks.get("tts", {}).get("execution_status") != "blocked":
        raise ContractError("Gateway MVP TTS task must remain blocked")
    if tasks.get("asr", {}).get("execution_status") != "blocked":
        raise ContractError("Gateway MVP ASR task must remain blocked")


def _load_gateway_runtime(gateway_root: str | Path) -> SimpleNamespace:
    root = Path(gateway_root).expanduser().resolve()
    if not root.is_dir():
        raise ContractError("shared Gateway runtime is unavailable")
    source_root = (root / "src").resolve()
    if not source_root.is_dir():
        raise ContractError("shared Gateway Python source is unavailable")
    schema_root = source_root / "model_provider_gateway" / "schemas"
    request_schema_path = schema_root / "execution-request.v1.schema.json"
    result_schema_path = schema_root / "execution-result.v1.schema.json"
    request_schema_document = read_json_object(
        request_schema_path, "Gateway execution request schema"
    )
    result_schema_document = read_json_object(
        result_schema_path, "Gateway execution result schema"
    )
    try:
        Draft202012Validator.check_schema(request_schema_document)
        Draft202012Validator.check_schema(result_schema_document)
    except SchemaError as exc:
        raise ContractError("shared Gateway JSON Schema is invalid") from exc

    if str(source_root) not in sys.path:
        sys.path.insert(0, str(source_root))
    try:
        package = importlib.import_module("model_provider_gateway")
        adapters = importlib.import_module("model_provider_gateway.adapters")
        consent_module = importlib.import_module("model_provider_gateway.consent")
        coverage_module = importlib.import_module("model_provider_gateway.coverage")
        doctor_module = importlib.import_module("model_provider_gateway.doctor")
        execution = importlib.import_module("model_provider_gateway.execution")
        mock_server_module = importlib.import_module(
            "model_provider_gateway.mock_server"
        )
        runtime_module = importlib.import_module("model_provider_gateway.runtime")
    except ImportError as exc:
        raise ContractError("shared Gateway Python package is unavailable") from exc
    loaded_modules = {
        "package": package,
        "adapters": adapters,
        "consent": consent_module,
        "coverage": coverage_module,
        "doctor": doctor_module,
        "execution": execution,
        "mock_server": mock_server_module,
        "runtime": runtime_module,
    }
    if any(
        source_root not in Path(str(module.__file__)).resolve().parents
        for module in loaded_modules.values()
    ):
        raise ContractError(
            "loaded Gateway package does not match the explicit source root"
        )
    matrix = coverage_module.build_consumer_capability_matrix(
        consumer_id=GATEWAY_CONSUMER_ID
    )
    adapter = adapters.adapter_contract(GATEWAY_CONSUMER_ID)
    semantic_contract = _validate_gateway_semantics(
        http_api_schema=getattr(runtime_module, "HTTP_API_SCHEMA", None),
        adapter_request_schema=getattr(adapters, "ADAPTER_REQUEST_SCHEMA", None),
        consent_schema=getattr(consent_module, "CONSENT_SCHEMA", None),
        request_schema=getattr(execution, "EXECUTION_REQUEST_SCHEMA", None),
        result_schema=getattr(execution, "EXECUTION_RESULT_SCHEMA", None),
        request_schema_document=request_schema_document,
        result_schema_document=result_schema_document,
        matrix=matrix,
        adapter=adapter,
    )
    source_provenance = _git_source_provenance(root)
    return SimpleNamespace(
        root=root,
        checkout_commit=source_provenance["commit"],
        source_tree_dirty=source_provenance["dirty"],
        version=str(getattr(package, "__version__", "unknown")),
        canonical_commit=matrix.get("canonical_gateway_commit"),
        canonical_commit_role=matrix.get(
            "canonical_gateway_commit_role", "provenance_evidence_only"
        ),
        matrix_revision=matrix.get("matrix_revision"),
        semantic_contract=semantic_contract,
        schema_sha256={
            "execution_request": file_sha256(request_schema_path),
            "execution_result": file_sha256(result_schema_path),
        },
        gateway_component_sha256={
            name: file_sha256(Path(str(module.__file__)).resolve())
            for name, module in loaded_modules.items()
        },
        execution_result_schema=result_schema_document,
        build_profile=package.build_profile,
        validate_profile=package.validate_profile,
        build_route_plan=package.build_route_plan,
        validate_route_plan=package.validate_route_plan,
        build_consent_plan=package.build_consent_plan,
        validate_consent=package.validate_consent,
        build_execution_request=package.build_execution_request,
        validate_execution_request=package.validate_execution_request,
        execute_model_request=package.execute_model_request,
        adapter_contract=adapters.adapter_contract,
        validate_adapter_request=adapters.validate_adapter_request,
        doctor=doctor_module.doctor,
        LiteLLMTransport=execution.LiteLLMTransport,
        mock_server=mock_server_module.mock_server,
    )


def _git_source_provenance(root: Path) -> dict[str, Any]:
    commit: str | None = None
    dirty: bool | None = None
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        value = completed.stdout.strip().lower()
        if (
            completed.returncode == 0
            and len(value) == 40
            and all(character in "0123456789abcdef" for character in value)
        ):
            commit = value
        status = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain=v1"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if status.returncode == 0:
            dirty = bool(status.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    return {"commit": commit, "dirty": dirty}


def _validate_loopback_transport(runtime: SimpleNamespace, transport: Any) -> None:
    if type(transport) is not runtime.LiteLLMTransport:
        raise ContractError("MVP requires the canonical Gateway HTTP transport")
    parsed = urlparse(str(getattr(transport, "_api_base_override", "") or ""))
    if (
        transport.mode != "loopback_stub"
        or getattr(transport, "_synthetic_key", None) != "synthetic-loopback-only"
        or parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}
        or parsed.username
        or parsed.password
        or parsed.path.rstrip("/") != "/v1"
        or parsed.query
        or parsed.fragment
    ):
        raise ContractError("MVP Gateway loopback must use a clean HTTP v1 endpoint")


def _prepare_output_root(value: str | Path) -> Path:
    root = Path(value).expanduser().resolve()
    if root.exists() and any(root.iterdir()):
        raise ContractError("Gateway output root must be new or empty")
    root.mkdir(parents=True, exist_ok=True)
    return root


def _use_case(value: str) -> dict[str, str]:
    try:
        return dict(_USE_CASES[value])
    except KeyError as exc:
        raise ContractError("unsupported MVP Gateway use case") from exc


def _safe_gateway_error(exc: Exception) -> str:
    message = str(exc)
    allowed = {
        "shared Gateway runtime is unavailable",
        "shared Gateway Python source is unavailable",
        "shared Gateway Python package is unavailable",
        "loaded Gateway package does not match the explicit source root",
        "shared Gateway JSON Schema is invalid",
        "Gateway text/vision execution truth is unavailable",
        "Gateway blocked capability truth drifted",
        "Gateway MVP adapter schema drifted",
        "Gateway MVP script task is not enabled",
        "Gateway MVP visual task is not enabled",
        "Gateway MVP TTS task must remain blocked",
        "Gateway MVP ASR task must remain blocked",
    }
    allowed_prefixes = (
        "Gateway semantic schema is incompatible:",
        "Gateway MVP task capability is incompatible:",
        "Gateway capability matrix schema is incompatible:",
        "Gateway required capability is unavailable:",
        "Gateway capability safety policy is incompatible:",
        "Gateway capability must remain blocked:",
        "Gateway safety invariant is incompatible:",
    )
    return (
        message
        if message in allowed or message.startswith(allowed_prefixes)
        else "shared Gateway semantic validation failed"
    )
