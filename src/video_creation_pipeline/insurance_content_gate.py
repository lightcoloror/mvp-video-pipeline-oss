from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import datetime, timezone
from importlib.resources import files
from pathlib import Path
from typing import Any, Iterable, Iterator

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

POLICY_PACK_SCHEMA = "video_creation_pipeline.insurance_content_policy_pack.v1"
SCAN_REQUEST_SCHEMA = "video_creation_pipeline.insurance_content_scan_request.v1"
SCAN_REPORT_SCHEMA = "video_creation_pipeline.insurance_content_scan_report.v1"

CONTENT_ROLES = {
    "title",
    "hook",
    "script",
    "subtitle",
    "overlay_text",
    "cover_text",
    "disclaimer",
    "customer_case",
    "product_explanation",
    "medical_explanation",
    "sales_call_to_action",
}
SEVERITIES = {"block", "compliance_review", "fact_check"}
DETECTION_MODES = {"literal_any", "regex_any"}
STATUS_PRIORITY = {
    "review_ready_candidate": 0,
    "needs_fact_check": 1,
    "needs_compliance_review": 2,
    "blocked": 3,
}


def default_insurance_policy_path() -> Path:
    return Path(
        str(
            files("video_creation_pipeline")
            .joinpath("policies")
            .joinpath("insurance-content-cn-2026-08.v1.json")
        )
    ).resolve()


def build_insurance_content_scan_request(
    policy_pack_path: str | Path,
    *,
    content: Iterable[dict[str, Any]],
    evidence: Iterable[dict[str, Any]] = (),
    created_at: str | None = None,
    distribution_channel: str = "unspecified",
) -> dict[str, Any]:
    timestamp = _timestamp(created_at)
    policy_path = Path(policy_pack_path).expanduser().resolve()
    policy = read_json_object(policy_path, "insurance content policy pack")
    validate_insurance_content_policy_pack(policy, at=timestamp)

    normalized_content = []
    seen_content_ids: set[str] = set()
    for raw in content:
        content_id = str(raw.get("content_id") or "").strip()
        role = str(raw.get("role") or "").strip()
        if not content_id or content_id in seen_content_ids:
            raise ContractError("insurance content ids must be non-empty and unique")
        if role not in CONTENT_ROLES:
            raise ContractError(f"unsupported insurance content role: {role}")
        seen_content_ids.add(content_id)
        normalized_content.append(
            {
                "content_id": content_id,
                "role": role,
                "artifact": artifact_reference(
                    raw.get("path"), f"insurance content {content_id}"
                ),
            }
        )
    if not normalized_content:
        raise ContractError("insurance content scan requires at least one artifact")

    normalized_evidence = []
    seen_evidence_ids: set[str] = set()
    for raw in evidence:
        evidence_id = str(raw.get("evidence_id") or "").strip()
        evidence_type = str(raw.get("evidence_type") or "").strip()
        supports = sorted(
            {str(value).strip() for value in raw.get("supports_rule_ids") or []}
        )
        if not evidence_id or evidence_id in seen_evidence_ids:
            raise ContractError("insurance evidence ids must be non-empty and unique")
        if not evidence_type:
            raise ContractError("insurance evidence type is required")
        if not supports:
            raise ContractError(
                "insurance evidence must name supported rule ids or '*'"
            )
        seen_evidence_ids.add(evidence_id)
        normalized_evidence.append(
            {
                "evidence_id": evidence_id,
                "evidence_type": evidence_type,
                "supports_rule_ids": supports,
                "valid_from": _optional_timestamp(raw.get("valid_from")),
                "valid_until": _optional_timestamp(raw.get("valid_until")),
                "artifact": artifact_reference(
                    raw.get("path"), f"insurance evidence {evidence_id}"
                ),
            }
        )

    request: dict[str, Any] = {
        "schema": SCAN_REQUEST_SCHEMA,
        "created_at": timestamp.isoformat(),
        "policy_pack": artifact_reference(policy_path, "insurance content policy pack"),
        "distribution_channel": str(distribution_channel).strip() or "unspecified",
        "content": sorted(normalized_content, key=lambda row: row["content_id"]),
        "evidence": sorted(normalized_evidence, key=lambda row: row["evidence_id"]),
        "authority_boundary": _request_boundary(),
    }
    request["request_sha256"] = payload_sha256(request, "request_sha256")
    validate_insurance_content_scan_request(request)
    return request


def validate_insurance_content_policy_pack(
    policy: dict[str, Any], *, at: str | datetime | None = None
) -> None:
    _validate_schema(
        policy, "insurance_content_policy_pack.v1.schema.json", "policy pack"
    )
    if policy.get("policy_pack_sha256") != payload_sha256(policy, "policy_pack_sha256"):
        raise ContractError("insurance content policy pack integrity check failed")
    if policy.get("authority_boundary") != _policy_boundary():
        raise ContractError("insurance content policy authority boundary is invalid")

    check_time = _timestamp(at)
    reviewed_at = _timestamp(policy.get("reviewed_at"))
    review_due_at = _timestamp(policy.get("review_due_at"))
    if reviewed_at > check_time:
        raise ContractError("insurance content policy review time is in the future")
    if check_time > review_due_at:
        raise ContractError("insurance content policy pack requires current review")

    source_ids: set[str] = set()
    for source in policy.get("official_sources") or []:
        source_id = str(source.get("source_id") or "")
        if not source_id or source_id in source_ids:
            raise ContractError("insurance policy source ids must be unique")
        source_ids.add(source_id)
        effective_from = _timestamp(source.get("effective_from"))
        lifecycle = source.get("lifecycle")
        expected = "effective" if effective_from <= check_time else "future"
        if lifecycle != expected:
            raise ContractError(
                f"insurance policy source lifecycle drifted: {source_id}"
            )

    rule_ids: set[str] = set()
    categories = set(policy.get("categories") or [])
    evidence_types = set(policy.get("evidence_types") or [])
    for rule in policy.get("rules") or []:
        rule_id = str(rule.get("rule_id") or "")
        if not rule_id or rule_id in rule_ids:
            raise ContractError("insurance policy rule ids must be unique")
        rule_ids.add(rule_id)
        if rule.get("category") not in categories:
            raise ContractError(f"unknown insurance policy category: {rule_id}")
        if rule.get("severity") not in SEVERITIES:
            raise ContractError(f"invalid insurance policy severity: {rule_id}")
        if rule.get("detection") not in DETECTION_MODES:
            raise ContractError(f"invalid insurance policy detector: {rule_id}")
        if not set(rule.get("applies_to_roles") or []) <= CONTENT_ROLES:
            raise ContractError(f"invalid insurance policy content role: {rule_id}")
        if not set(rule.get("required_evidence_types") or []) <= evidence_types:
            raise ContractError(f"unknown insurance evidence type: {rule_id}")
        if not set(rule.get("source_ids") or []) <= source_ids:
            raise ContractError(f"unknown insurance policy source reference: {rule_id}")
        if rule.get("detection") == "regex_any":
            for pattern in rule.get("patterns") or []:
                try:
                    re.compile(str(pattern), re.IGNORECASE)
                except re.error as exc:
                    raise ContractError(
                        f"invalid insurance policy regex for {rule_id}: {exc}"
                    ) from exc

    for requirement in policy.get("role_requirements") or []:
        if requirement.get("role") not in CONTENT_ROLES:
            raise ContractError("insurance role requirement has an invalid role")
        if requirement.get("severity") not in SEVERITIES:
            raise ContractError("insurance role requirement has invalid severity")
        if not set(requirement.get("required_evidence_types") or []) <= evidence_types:
            raise ContractError("insurance role requirement uses unknown evidence")


def validate_insurance_content_scan_request(request: dict[str, Any]) -> None:
    _validate_schema(
        request, "insurance_content_scan_request.v1.schema.json", "scan request"
    )
    if request.get("request_sha256") != payload_sha256(request, "request_sha256"):
        raise ContractError("insurance content scan request integrity check failed")
    if request.get("authority_boundary") != _request_boundary():
        raise ContractError("insurance content scan request boundary is invalid")
    created_at = _timestamp(request.get("created_at"))
    policy_path = validate_artifact_reference(
        _object(request.get("policy_pack")), "insurance content policy pack"
    )
    policy = read_json_object(policy_path, "insurance content policy pack")
    validate_insurance_content_policy_pack(policy, at=created_at)

    content_ids: set[str] = set()
    for row in request.get("content") or []:
        content_id = str(row.get("content_id") or "")
        if content_id in content_ids:
            raise ContractError("insurance content ids must be unique")
        content_ids.add(content_id)
        if row.get("role") not in CONTENT_ROLES:
            raise ContractError("insurance content role is invalid")
        validate_artifact_reference(
            _object(row.get("artifact")), f"insurance content {content_id}"
        )

    rule_ids = {str(row.get("rule_id")) for row in policy.get("rules") or []}
    evidence_types = set(policy.get("evidence_types") or [])
    evidence_ids: set[str] = set()
    for row in request.get("evidence") or []:
        evidence_id = str(row.get("evidence_id") or "")
        if evidence_id in evidence_ids:
            raise ContractError("insurance evidence ids must be unique")
        evidence_ids.add(evidence_id)
        if row.get("evidence_type") not in evidence_types:
            raise ContractError(f"unknown insurance evidence type: {evidence_id}")
        supports = set(row.get("supports_rule_ids") or [])
        if not supports or not supports <= (rule_ids | {"*"}):
            raise ContractError(
                f"insurance evidence support scope is invalid: {evidence_id}"
            )
        valid_from = _optional_datetime(row.get("valid_from"))
        valid_until = _optional_datetime(row.get("valid_until"))
        if valid_from and valid_until and valid_from > valid_until:
            raise ContractError(
                f"insurance evidence validity is inverted: {evidence_id}"
            )
        validate_artifact_reference(
            _object(row.get("artifact")), f"insurance evidence {evidence_id}"
        )


def run_insurance_content_scan(
    request_path: str | Path,
    output_path: str | Path,
) -> dict[str, Any]:
    request_file = Path(request_path).expanduser().resolve()
    request = read_json_object(request_file, "insurance content scan request")
    validate_insurance_content_scan_request(request)
    policy_path = validate_artifact_reference(
        _object(request.get("policy_pack")), "insurance content policy pack"
    )
    policy = read_json_object(policy_path, "insurance content policy pack")
    findings, gaps, status = _evaluate(policy, request)
    report: dict[str, Any] = {
        "schema": SCAN_REPORT_SCHEMA,
        "created_at": request["created_at"],
        "source_request": artifact_reference(
            request_file, "insurance content scan request"
        ),
        "policy_pack": request["policy_pack"],
        "content": request["content"],
        "evidence": request["evidence"],
        "findings": findings,
        "evidence_gaps": gaps,
        "readiness": _readiness(findings, gaps, status),
        "limitations": [
            "规则命中是工程化风险候选，不是法律意见或最终违法认定。",
            "机器检查不能证明条款解释、产品状态或医学结论真实，只能验证已绑定证据。",
            "通过本门仅允许继续生成候选，不授权上传、发布、投放或销售。",
        ],
        "authority_boundary": _report_boundary(),
    }
    report["report_sha256"] = payload_sha256(report, "report_sha256")
    validate_insurance_content_scan_report(report)
    atomic_write_json(report, output_path)
    return report


def validate_insurance_content_scan_report(report: dict[str, Any]) -> None:
    _validate_schema(
        report, "insurance_content_scan_report.v1.schema.json", "scan report"
    )
    if report.get("report_sha256") != payload_sha256(report, "report_sha256"):
        raise ContractError("insurance content scan report integrity check failed")
    if report.get("authority_boundary") != _report_boundary():
        raise ContractError("insurance content scan report boundary is invalid")
    request_path = validate_artifact_reference(
        _object(report.get("source_request")), "insurance content scan request"
    )
    request = read_json_object(request_path, "insurance content scan request")
    validate_insurance_content_scan_request(request)
    if report.get("created_at") != request.get("created_at"):
        raise ContractError("insurance content scan report creation time drifted")
    if report.get("policy_pack") != request.get("policy_pack"):
        raise ContractError("insurance content scan report policy binding drifted")
    if report.get("content") != request.get("content") or report.get(
        "evidence"
    ) != request.get("evidence"):
        raise ContractError("insurance content scan report input binding drifted")
    policy_path = validate_artifact_reference(
        _object(request.get("policy_pack")), "insurance content policy pack"
    )
    policy = read_json_object(policy_path, "insurance content policy pack")
    findings, gaps, status = _evaluate(policy, request)
    if report.get("findings") != findings or report.get("evidence_gaps") != gaps:
        raise ContractError(
            "insurance content scan report no longer matches its inputs"
        )
    if report.get("readiness") != _readiness(findings, gaps, status):
        raise ContractError("insurance content scan readiness is inconsistent")


def write_insurance_content_scan_request(
    request: dict[str, Any], output_path: str | Path
) -> Path:
    validate_insurance_content_scan_request(request)
    return atomic_write_json(request, output_path)


def _evaluate(
    policy: dict[str, Any], request: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
    at = _timestamp(request.get("created_at"))
    validate_insurance_content_policy_pack(policy, at=at)
    available = _available_evidence(request.get("evidence") or [], at)
    findings: list[dict[str, Any]] = []
    roles_present = {str(row.get("role")) for row in request.get("content") or []}

    for content in request.get("content") or []:
        role = str(content.get("role"))
        artifact = _object(content.get("artifact"))
        path = validate_artifact_reference(
            artifact, f"insurance content {content.get('content_id')}"
        )
        for pointer, text in _text_segments(path):
            normalized = unicodedata.normalize("NFKC", text).lower()
            for rule in policy.get("rules") or []:
                if role not in set(rule.get("applies_to_roles") or []):
                    continue
                matches = _matches(rule, normalized)
                if not matches:
                    continue
                missing = _missing_rule_evidence(rule, available)
                for start, end, matched in matches:
                    finding_key = "|".join(
                        [
                            str(rule["rule_id"]),
                            str(content["content_id"]),
                            pointer,
                            str(start),
                            hashlib.sha256(matched.encode("utf-8")).hexdigest(),
                        ]
                    )
                    findings.append(
                        {
                            "finding_id": "insurance-"
                            + hashlib.sha256(finding_key.encode("utf-8")).hexdigest()[
                                :20
                            ],
                            "rule_id": rule["rule_id"],
                            "category": rule["category"],
                            "severity": rule["severity"],
                            "content_id": content["content_id"],
                            "content_role": role,
                            "json_pointer_or_line": pointer,
                            "span_start": start,
                            "span_end": end,
                            "matched_length": max(1, end - start),
                            "match_sha256": hashlib.sha256(
                                matched.encode("utf-8")
                            ).hexdigest(),
                            "missing_evidence_types": missing,
                            "disposition": _finding_disposition(
                                str(rule["severity"]), missing
                            ),
                            "source_ids": sorted(rule.get("source_ids") or []),
                            "raw_match_stored": False,
                        }
                    )

    findings.sort(key=lambda row: row["finding_id"])
    gaps = []
    for requirement in policy.get("role_requirements") or []:
        role = str(requirement.get("role"))
        if role not in roles_present:
            continue
        missing = sorted(
            evidence_type
            for evidence_type in requirement.get("required_evidence_types") or []
            if not _has_evidence(available, evidence_type, "*")
        )
        if missing:
            gaps.append(
                {
                    "gap_id": "insurance-gap-"
                    + hashlib.sha256(
                        (role + "|" + "|".join(missing)).encode("utf-8")
                    ).hexdigest()[:20],
                    "content_role": role,
                    "severity": requirement["severity"],
                    "missing_evidence_types": missing,
                    "source_ids": sorted(requirement.get("source_ids") or []),
                }
            )
    gaps.sort(key=lambda row: row["gap_id"])

    status = "review_ready_candidate"
    effective_severities = [
        row["severity"]
        for row in findings
        if row["disposition"] != "evidence_bound_signal"
    ] + [row["severity"] for row in gaps]
    for severity in effective_severities:
        candidate = {
            "block": "blocked",
            "compliance_review": "needs_compliance_review",
            "fact_check": "needs_fact_check",
        }[severity]
        if STATUS_PRIORITY[candidate] > STATUS_PRIORITY[status]:
            status = candidate
    return findings, gaps, status


def _finding_disposition(severity: str, missing: list[str]) -> str:
    if severity == "block":
        return "blocked_expression"
    if severity == "compliance_review":
        return "manual_compliance_review"
    return "missing_evidence" if missing else "evidence_bound_signal"


def _matches(rule: dict[str, Any], normalized: str) -> list[tuple[int, int, str]]:
    matches: list[tuple[int, int, str]] = []
    for raw_pattern in rule.get("patterns") or []:
        pattern = str(raw_pattern)
        regex = (
            pattern
            if rule.get("detection") == "regex_any"
            else re.escape(unicodedata.normalize("NFKC", pattern).lower())
        )
        for match in re.finditer(regex, normalized, re.IGNORECASE):
            matches.append((match.start(), match.end(), match.group(0)))
    return sorted(set(matches), key=lambda row: (row[0], row[1], row[2]))


def _available_evidence(
    rows: list[dict[str, Any]], at: datetime
) -> list[dict[str, Any]]:
    available = []
    for row in rows:
        valid_from = _optional_datetime(row.get("valid_from"))
        valid_until = _optional_datetime(row.get("valid_until"))
        if valid_from and at < valid_from:
            continue
        if valid_until and at > valid_until:
            continue
        available.append(row)
    return available


def _missing_rule_evidence(
    rule: dict[str, Any], evidence: list[dict[str, Any]]
) -> list[str]:
    return sorted(
        evidence_type
        for evidence_type in rule.get("required_evidence_types") or []
        if not _has_evidence(evidence, evidence_type, str(rule.get("rule_id")))
    )


def _has_evidence(
    evidence: list[dict[str, Any]], evidence_type: str, rule_id: str
) -> bool:
    return any(
        row.get("evidence_type") == evidence_type
        and (
            "*" in set(row.get("supports_rule_ids") or [])
            or rule_id in set(row.get("supports_rule_ids") or [])
        )
        for row in evidence
    )


def _text_segments(path: Path) -> Iterator[tuple[str, str]]:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise ContractError(
            f"insurance content must be UTF-8 text or JSON: {path}"
        ) from exc
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        for line_number, line in enumerate(text.splitlines(), start=1):
            if line.strip():
                yield f"line:{line_number}", line
        return
    yield from _json_strings(payload)


def _json_strings(value: Any, pointer: str = "") -> Iterator[tuple[str, str]]:
    if isinstance(value, str):
        if value.strip():
            yield pointer or "/", value
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            yield from _json_strings(child, f"{pointer}/{index}")
        return
    if isinstance(value, dict):
        for key in sorted(value):
            escaped = str(key).replace("~", "~0").replace("/", "~1")
            yield from _json_strings(value[key], f"{pointer}/{escaped}")


def _readiness(
    findings: list[dict[str, Any]], gaps: list[dict[str, Any]], status: str
) -> dict[str, Any]:
    blocking = sum(row["severity"] == "block" for row in findings + gaps)
    eligible = status == "review_ready_candidate"
    return {
        "status": status,
        "machine_scan_complete": True,
        "finding_count": len(findings),
        "evidence_gap_count": len(gaps),
        "blocking_count": blocking,
        "machine_gate_passed": eligible,
        "render_candidate_eligible": eligible,
        "human_release_review_required": True,
        "publish_authorized": False,
    }


def _policy_boundary() -> dict[str, Any]:
    return {
        "candidate_policy_only": True,
        "legal_opinion": False,
        "automatic_rewrite": False,
        "automatic_publish": False,
    }


def _request_boundary() -> dict[str, Any]:
    return {
        "local_artifacts_only": True,
        "external_provider_called": False,
        "automatic_upload": False,
        "automatic_publish": False,
    }


def _report_boundary() -> dict[str, Any]:
    return {
        "candidate_detection_only": True,
        "legal_conclusion": False,
        "automatic_rewrite": False,
        "automatic_upload": False,
        "automatic_publish": False,
    }


def _validate_schema(payload: dict[str, Any], filename: str, label: str) -> None:
    schema = read_json_object(
        Path(__file__).with_name("schemas") / filename, f"{label} schema"
    )
    try:
        validator = Draft202012Validator(schema)
        validator.check_schema(schema)
        validator.validate(payload)
    except (SchemaError, ValidationError) as exc:
        raise ContractError(f"{label} schema validation failed: {exc.message}") from exc


def _timestamp(value: str | datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc).replace(microsecond=0)
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError as exc:
            raise ContractError("timestamp must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ContractError("timestamp must include a timezone")
    return parsed.astimezone(timezone.utc).replace(microsecond=0)


def _optional_datetime(value: Any) -> datetime | None:
    return None if value in {None, ""} else _timestamp(value)


def _optional_timestamp(value: Any) -> str | None:
    parsed = _optional_datetime(value)
    return parsed.isoformat() if parsed else None


def _object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}
