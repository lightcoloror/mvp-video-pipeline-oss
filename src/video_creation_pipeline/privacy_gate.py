from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
import subprocess
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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
    _resolve_windows_tool_wrapper,
    execute_ffmpeg_frame_series_with_receipt,
)

PRIVACY_TERMS_SCHEMA = "video_creation_pipeline.privacy_terms.v1"
PRIVACY_REVIEW_PLAN_SCHEMA = "video_creation_pipeline.privacy_review_plan.v1"
PRIVACY_SCAN_REPORT_SCHEMA = "video_creation_pipeline.privacy_scan_report.v1"
PRIVACY_REVIEW_DECISION_SCHEMA = "video_creation_pipeline.privacy_review_decision.v1"

PRESIDIO_SOURCE_COMMIT = "e069216c17ad3712a54c0ce3c5578e2ccee8f45f"
OPENCV_QR_SOURCE_COMMIT = "77dfa297d08fdecdc509fc01ad92a2e9ec776a57"

LANES = {"text", "audio", "visual", "metadata"}
EVIDENCE_ROLES = {
    "transcript",
    "subtitle",
    "script",
    "ocr_text",
    "document_text",
    "frame_report",
}
IMAGE_SUFFIXES = {".bmp", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"}
VIDEO_SUFFIXES = {".avi", ".m4v", ".mkv", ".mov", ".mp4", ".webm"}
AUDIO_SUFFIXES = {".aac", ".flac", ".m4a", ".mp3", ".ogg", ".wav"}
TEXT_SUFFIXES = {".ass", ".csv", ".json", ".md", ".srt", ".txt", ".vtt"}

BUILTIN_PATTERNS: tuple[tuple[str, str, str, re.Pattern[str]], ...] = (
    (
        "CN_MOBILE",
        "手机号码候选",
        "builtin_regex",
        re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
    ),
    (
        "CN_ID_NUMBER",
        "身份证号码候选",
        "builtin_regex",
        re.compile(r"(?<!\d)\d{17}[0-9Xx](?!\d)"),
    ),
    (
        "EMAIL_ADDRESS",
        "电子邮箱候选",
        "builtin_regex",
        re.compile(r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}(?![\w.-])"),
    ),
    (
        "LONG_ACCOUNT_NUMBER",
        "长编号／账户号码候选",
        "builtin_regex",
        re.compile(r"(?<!\d)\d{13,24}(?!\d)"),
    ),
)


def build_privacy_review_plan(
    final_artifact_path: str | Path,
    terms_path: str | Path,
    *,
    authorization_evidence_path: str | Path | None,
    evidence: list[dict[str, Any]],
    subject_policy: dict[str, Any],
    required_lanes: Iterable[str] | None = None,
    visual_sample_interval_s: float = 1.0,
    max_visual_frames: int = 300,
    ocr_language: str = "chi_sim",
) -> dict[str, Any]:
    final_path = Path(final_artifact_path).expanduser().resolve()
    terms = Path(terms_path).expanduser().resolve()
    _validate_terms(read_json_object(terms, "privacy terms"))
    normalized_evidence = []
    for row in evidence:
        role = str(row.get("role") or "").strip()
        if role not in EVIDENCE_ROLES:
            raise ContractError(
                f"unsupported privacy evidence role: {role or 'missing'}"
            )
        normalized_evidence.append(
            {
                "role": role,
                "artifact": artifact_reference(row.get("path"), f"privacy {role}"),
            }
        )
    normalized_policy = _normalize_subject_policy(subject_policy)
    authorization = (
        artifact_reference(authorization_evidence_path, "authorization evidence")
        if authorization_evidence_path
        else None
    )
    if (
        normalized_policy["mode"] == "authorized_identifiable"
        or normalized_policy["face"] == "allowed"
        or normalized_policy["voice"] == "allowed"
    ) and authorization is None:
        raise ContractError(
            "identifiable face or voice requires bound authorization evidence"
        )
    lanes = sorted(set(required_lanes or _default_lanes(final_path, evidence)))
    if not lanes or not set(lanes) <= LANES:
        raise ContractError("privacy review plan has invalid required lanes")
    interval = _positive_number(visual_sample_interval_s, "visual sample interval")
    if not isinstance(max_visual_frames, int) or not 1 <= max_visual_frames <= 5000:
        raise ContractError("max visual frames must be between 1 and 5000")
    if not str(ocr_language).strip():
        raise ContractError("OCR language is required")
    plan: dict[str, Any] = {
        "schema": PRIVACY_REVIEW_PLAN_SCHEMA,
        "created_at": _now(),
        "final_artifact": artifact_reference(final_path, "final privacy artifact"),
        "privacy_terms": artifact_reference(terms, "privacy terms"),
        "authorization_evidence": authorization,
        "evidence": normalized_evidence,
        "subject_policy": normalized_policy,
        "scan_policy": {
            "required_lanes": lanes,
            "visual_sample_interval_s": round(interval, 4),
            "max_visual_frames": max_visual_frames,
            "ocr_language": str(ocr_language).strip(),
            "scan_actual_final_artifact": True,
            "store_raw_sensitive_matches": False,
            "human_full_media_review_required": True,
        },
        "provider_contracts": {
            "text_candidate_model": {
                "provider": "presidio_pattern_contract_adaptation",
                "source_commit": PRESIDIO_SOURCE_COMMIT,
                "full_presidio_runtime_required": False,
            },
            "qr_candidate_model": {
                "provider": "opencv_qrcode_detector_payload_free",
                "source_commit": OPENCV_QR_SOURCE_COMMIT,
            },
            "asr_owner": "video-knowledge-pipeline",
            "ocr_preferred_owner": "video-knowledge-pipeline_to_ebook_markdown_pipeline",
            "final_visual_ocr_fallback": "local_tesseract",
            "frame_extraction_owner": "video_creation_pipeline.single_ffmpeg_outlet",
        },
        "authority_boundary": {
            "candidate_detection_only": True,
            "automatic_redaction": False,
            "automatic_publish": False,
            "human_review_required": True,
            "source_media_modified": False,
            "second_asr_or_ocr_truth_created": False,
            "second_ffmpeg_outlet_created": False,
        },
    }
    plan["plan_sha256"] = payload_sha256(plan, "plan_sha256")
    validate_privacy_review_plan(plan)
    return plan


def validate_privacy_review_plan(plan: dict[str, Any]) -> None:
    _validate_schema(plan, "privacy_review_plan.v1.schema.json", "privacy plan")
    if plan.get("plan_sha256") != payload_sha256(plan, "plan_sha256"):
        raise ContractError("privacy review plan integrity check failed")
    validate_artifact_reference(
        _object(plan.get("final_artifact")), "final privacy artifact"
    )
    terms_path = validate_artifact_reference(
        _object(plan.get("privacy_terms")), "privacy terms"
    )
    _validate_terms(read_json_object(terms_path, "privacy terms"))
    authorization = plan.get("authorization_evidence")
    if authorization is not None:
        validate_artifact_reference(_object(authorization), "authorization evidence")
    for row in plan.get("evidence") or []:
        item = _object(row)
        validate_artifact_reference(
            _object(item.get("artifact")), f"privacy {item.get('role')}"
        )
    policy = _normalize_subject_policy(_object(plan.get("subject_policy")))
    if plan.get("subject_policy") != policy:
        raise ContractError("privacy subject policy is not normalized")
    if (
        policy["mode"] == "authorized_identifiable"
        or policy["face"] == "allowed"
        or policy["voice"] == "allowed"
    ) and authorization is None:
        raise ContractError("identifiable subject policy lost authorization evidence")
    boundary = _object(plan.get("authority_boundary"))
    if boundary != {
        "candidate_detection_only": True,
        "automatic_redaction": False,
        "automatic_publish": False,
        "human_review_required": True,
        "source_media_modified": False,
        "second_asr_or_ocr_truth_created": False,
        "second_ffmpeg_outlet_created": False,
    }:
        raise ContractError("privacy plan authority boundary is invalid")


def write_privacy_review_plan(plan: dict[str, Any], output_path: str | Path) -> Path:
    validate_privacy_review_plan(plan)
    return atomic_write_json(plan, output_path)


def run_privacy_scan(
    plan_path: str | Path,
    output_dir: str | Path,
    *,
    report_path: str | Path | None = None,
    timeout_s: float = 300.0,
) -> dict[str, Any]:
    source_plan_path = Path(plan_path).expanduser().resolve()
    plan = read_json_object(source_plan_path, "privacy review plan")
    validate_privacy_review_plan(plan)
    root = Path(output_dir).expanduser().resolve()
    owner_path = root / ".privacy-scan-owner.json"
    if root.is_dir() and any(root.iterdir()) and not owner_path.is_file():
        raise ContractError("privacy scan output directory is non-empty and unmanaged")
    root.mkdir(parents=True, exist_ok=True)
    atomic_write_json(
        {
            "schema": "video_creation_pipeline.privacy_scan_output_owner.v1",
            "source_plan": artifact_reference(source_plan_path, "privacy review plan"),
        },
        owner_path,
    )
    final_path = Path(str(plan["final_artifact"]["path"])).resolve()
    terms = _term_entries(
        read_json_object(plan["privacy_terms"]["path"], "privacy terms")
    )
    accumulator: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    lanes: dict[str, dict[str, Any]] = {}
    providers: dict[str, Any] = {}

    _scan_evidence_lanes(plan, terms, accumulator, lanes)
    _scan_metadata_lane(plan, final_path, terms, accumulator, lanes, providers)
    if "visual" in plan["scan_policy"]["required_lanes"]:
        _scan_visual_lane(
            plan,
            final_path,
            root,
            terms,
            accumulator,
            lanes,
            providers,
            timeout_s=timeout_s,
        )
    else:
        lanes["visual"] = _lane("not_applicable", [], "not required by plan")
    for lane_name in LANES:
        lanes.setdefault(
            lane_name,
            _lane(
                "not_applicable",
                [],
                "not required by plan",
            ),
        )

    findings = _finalize_findings(accumulator)
    required = set(plan["scan_policy"]["required_lanes"])
    incomplete = sorted(
        lane_name
        for lane_name in required
        if lanes.get(lane_name, {}).get("status") != "complete"
    )
    report: dict[str, Any] = {
        "schema": PRIVACY_SCAN_REPORT_SCHEMA,
        "created_at": _now(),
        "source_plan": artifact_reference(source_plan_path, "privacy review plan"),
        "scanned_final_artifact": artifact_reference(
            final_path, "scanned final artifact"
        ),
        "lanes": lanes,
        "providers": providers,
        "findings": findings,
        "limitations": [
            "OCR, ASR, regex, QR and face detection can miss content.",
            "Video visual checks sample frames; a human must still watch the final file from start to finish.",
            "No candidate finding is permission to disclose information.",
        ],
        "readiness": {
            "machine_scan_complete": not incomplete,
            "incomplete_required_lanes": incomplete,
            "candidate_count": len(findings),
            "human_review_required": True,
            "privacy_gate_passed": False,
            "release_blocked": True,
        },
        "authority_boundary": {
            "candidate_detection_only": True,
            "raw_sensitive_matches_stored": False,
            "human_decision_claimed": False,
            "automatic_publish": False,
            "publish_performed": False,
            "source_media_modified": False,
        },
    }
    report["report_sha256"] = payload_sha256(report, "report_sha256")
    validate_privacy_scan_report(report)
    destination = (
        Path(report_path).expanduser().resolve()
        if report_path
        else root / "privacy-scan-report.json"
    )
    atomic_write_json(report, destination)
    return report


def validate_privacy_scan_report(report: dict[str, Any]) -> None:
    _validate_schema(report, "privacy_scan_report.v1.schema.json", "privacy report")
    if report.get("report_sha256") != payload_sha256(report, "report_sha256"):
        raise ContractError("privacy scan report integrity check failed")
    plan_path = validate_artifact_reference(
        _object(report.get("source_plan")), "privacy review plan"
    )
    plan = read_json_object(plan_path, "privacy review plan")
    validate_privacy_review_plan(plan)
    final_path = validate_artifact_reference(
        _object(report.get("scanned_final_artifact")), "scanned final artifact"
    )
    if final_path != Path(str(plan["final_artifact"]["path"])).resolve():
        raise ContractError("privacy report is not bound to the planned final artifact")
    if report.get("scanned_final_artifact") != plan.get("final_artifact"):
        raise ContractError("privacy report final artifact hash drifted")
    for finding in report.get("findings") or []:
        row = _object(finding)
        if any(key in row for key in ("raw_text", "matched_text", "raw_value")):
            raise ContractError("privacy findings cannot store raw sensitive matches")
    boundary = _object(report.get("authority_boundary"))
    if boundary != {
        "candidate_detection_only": True,
        "raw_sensitive_matches_stored": False,
        "human_decision_claimed": False,
        "automatic_publish": False,
        "publish_performed": False,
        "source_media_modified": False,
    }:
        raise ContractError("privacy report authority boundary is invalid")


def build_privacy_review_decision(
    report_path: str | Path,
    review_spec: dict[str, Any],
) -> dict[str, Any]:
    source_report_path = Path(report_path).expanduser().resolve()
    report = read_json_object(source_report_path, "privacy scan report")
    validate_privacy_scan_report(report)
    reviewer = str(review_spec.get("reviewer") or "").strip()
    if not reviewer:
        raise ContractError("privacy review requires a reviewer")
    full_review = review_spec.get("human_full_media_review_completed") is True
    expected = {str(row["finding_id"]) for row in report["findings"]}
    normalized = []
    seen: set[str] = set()
    for raw in review_spec.get("decisions") or []:
        row = _object(raw)
        finding_id = str(row.get("finding_id") or "").strip()
        if finding_id not in expected or finding_id in seen:
            raise ContractError(
                "privacy review decision has an unknown or duplicate finding"
            )
        seen.add(finding_id)
        disposition = str(row.get("disposition") or "").strip()
        if disposition not in {"redact", "authorized_keep", "false_positive"}:
            raise ContractError("privacy finding disposition is invalid")
        reason = str(row.get("reason") or "").strip()
        scope = str(row.get("authorization_scope") or "").strip()
        if not reason:
            raise ContractError("every privacy finding decision requires a reason")
        if disposition == "authorized_keep" and not scope:
            raise ContractError("authorized_keep requires an authorization scope")
        normalized.append(
            {
                "finding_id": finding_id,
                "disposition": disposition,
                "reason": reason,
                "authorization_scope": scope,
            }
        )
    missing = sorted(expected - seen)
    incomplete_lanes = list(report["readiness"]["incomplete_required_lanes"])
    redact_count = sum(row["disposition"] == "redact" for row in normalized)
    gate_passed = bool(
        full_review and not missing and not incomplete_lanes and redact_count == 0
    )
    decision: dict[str, Any] = {
        "schema": PRIVACY_REVIEW_DECISION_SCHEMA,
        "reviewed_at": _now(),
        "reviewer": reviewer,
        "source_report": artifact_reference(source_report_path, "privacy scan report"),
        "reviewed_final_artifact": dict(report["scanned_final_artifact"]),
        "human_full_media_review_completed": full_review,
        "decisions": sorted(normalized, key=lambda row: row["finding_id"]),
        "outcome": {
            "missing_finding_decisions": missing,
            "incomplete_required_lanes": incomplete_lanes,
            "redact_required_count": redact_count,
            "privacy_gate_passed": gate_passed,
            "release_blocked": not gate_passed,
            "status": "privacy_review_passed" if gate_passed else "blocked",
        },
        "authority_boundary": {
            "human_privacy_review_recorded": True,
            "technical_or_privacy_pass_is_publish_permission": False,
            "automatic_publish": False,
            "publish_performed": False,
            "new_final_artifact_requires_rescan": True,
        },
    }
    decision["decision_sha256"] = payload_sha256(decision, "decision_sha256")
    validate_privacy_review_decision(decision)
    return decision


def validate_privacy_review_decision(decision: dict[str, Any]) -> None:
    _validate_schema(
        decision, "privacy_review_decision.v1.schema.json", "privacy decision"
    )
    if decision.get("decision_sha256") != payload_sha256(decision, "decision_sha256"):
        raise ContractError("privacy review decision integrity check failed")
    report_path = validate_artifact_reference(
        _object(decision.get("source_report")), "privacy scan report"
    )
    report = read_json_object(report_path, "privacy scan report")
    validate_privacy_scan_report(report)
    final_path = validate_artifact_reference(
        _object(decision.get("reviewed_final_artifact")), "reviewed final artifact"
    )
    if final_path != Path(str(report["scanned_final_artifact"]["path"])).resolve():
        raise ContractError("privacy decision is not bound to the scanned artifact")
    if decision.get("reviewed_final_artifact") != report.get("scanned_final_artifact"):
        raise ContractError("privacy decision final artifact hash drifted")
    expected = {str(row["finding_id"]) for row in report["findings"]}
    rows = decision.get("decisions") or []
    for row in rows:
        item = _object(row)
        disposition = str(item.get("disposition") or "")
        if disposition not in {"redact", "authorized_keep", "false_positive"}:
            raise ContractError("privacy finding disposition is invalid")
        if not str(item.get("reason") or "").strip():
            raise ContractError("every privacy finding decision requires a reason")
        if (
            disposition == "authorized_keep"
            and not str(item.get("authorization_scope") or "").strip()
        ):
            raise ContractError("authorized_keep requires an authorization scope")
    seen = {str(row["finding_id"]) for row in rows}
    if len(seen) != len(rows) or not seen <= expected:
        raise ContractError("privacy review has duplicate or unknown findings")
    missing = sorted(expected - seen)
    redact_count = sum(row.get("disposition") == "redact" for row in rows)
    incomplete = list(report["readiness"]["incomplete_required_lanes"])
    passed = bool(
        decision.get("human_full_media_review_completed") is True
        and not missing
        and not incomplete
        and redact_count == 0
    )
    expected_outcome = {
        "missing_finding_decisions": missing,
        "incomplete_required_lanes": incomplete,
        "redact_required_count": redact_count,
        "privacy_gate_passed": passed,
        "release_blocked": not passed,
        "status": "privacy_review_passed" if passed else "blocked",
    }
    if decision.get("outcome") != expected_outcome:
        raise ContractError("privacy review outcome drifted from its evidence")
    boundary = _object(decision.get("authority_boundary"))
    if boundary != {
        "human_privacy_review_recorded": True,
        "technical_or_privacy_pass_is_publish_permission": False,
        "automatic_publish": False,
        "publish_performed": False,
        "new_final_artifact_requires_rescan": True,
    }:
        raise ContractError("privacy review decision authority boundary is invalid")


def write_privacy_review_decision(
    decision: dict[str, Any], output_path: str | Path
) -> Path:
    validate_privacy_review_decision(decision)
    return atomic_write_json(decision, output_path)


def assert_privacy_gate_passed(decision: dict[str, Any]) -> None:
    validate_privacy_review_decision(decision)
    if decision["outcome"]["privacy_gate_passed"] is not True:
        raise ContractError("privacy gate is blocked")


def privacy_gate_doctor(
    *,
    sentryblur_executable: str | Path | None = None,
    sentryblur_model_root: str | Path | None = None,
    cloakframe_executable: str | Path | None = None,
) -> dict[str, Any]:
    from .privacy_redaction import privacy_redaction_doctor

    ffmpeg = _tool_status("ffmpeg")
    ffprobe = _tool_status("ffprobe")
    tesseract = _tool_status("tesseract")
    try:
        import cv2  # type: ignore

        opencv = {"status": "ready", "version": str(cv2.__version__)}
    except (ImportError, AttributeError) as exc:
        opencv = {"status": "missing", "error": str(exc)}
    redaction_sidecars = privacy_redaction_doctor(
        sentryblur_executable=sentryblur_executable,
        sentryblur_model_root=sentryblur_model_root,
        cloakframe_executable=cloakframe_executable,
    )
    schema_root = Path(__file__).with_name("schemas")
    contract_files = (
        schema_root / "privacy_exclusion_zones.v1.schema.json",
        schema_root / "privacy_layout_collision_report.v1.schema.json",
    )
    contracts_ready = all(path.is_file() for path in contract_files)
    try:
        import norfair  # type: ignore

        norfair_status = {
            "status": "ready",
            "version": str(getattr(norfair, "__version__", "unknown")),
        }
    except (ImportError, AttributeError) as exc:
        norfair_status = {"status": "missing", "error": str(exc)}
    dynamic_layout = {
        "status": (
            "runtime_ready"
            if contracts_ready and norfair_status["status"] == "ready"
            else "blocked_missing_tracking_dependency"
            if contracts_ready
            else "blocked_missing_contract"
        ),
        "contract_ready": contracts_ready,
        "coordinate_capture": "same_invocation_contract_ready",
        "tracker": {
            "name": "norfair",
            "reviewed_version": "2.3.0",
            **norfair_status,
        },
        "consumers": {
            "semantic_motion": "contract_ready",
            "hyperframes": "contract_ready_qa_debug_only",
            "remotion": "contract_ready_metadata_projection",
        },
        "synthetic_verified": False,
        "real_media_verified": False,
        "runtime_layout_reposition_allowed": False,
    }
    redaction_runtime_ready = (
        sentryblur_executable is None
        or sentryblur_model_root is None
        or redaction_sidecars["tools"]["sentryblur"]["status"] == "runtime_ready"
    )
    return {
        "schema": "video_creation_pipeline.privacy_gate_doctor.v1",
        "status": (
            "ready"
            if all(row["status"] == "ready" for row in (ffmpeg, ffprobe, tesseract))
            and opencv["status"] == "ready"
            and redaction_runtime_ready
            else "blocked"
        ),
        "tools": {
            "ffmpeg": ffmpeg,
            "ffprobe": ffprobe,
            "tesseract": tesseract,
            "opencv": opencv,
            "presidio": {
                "status": "source_reviewed_contract_adaptation",
                "source_commit": PRESIDIO_SOURCE_COMMIT,
                "full_runtime_loaded": False,
            },
        },
        "redaction_sidecars": redaction_sidecars,
        "dynamic_layout_avoidance": dynamic_layout,
        "boundaries": {
            "asr_owned_by_vkp": True,
            "candidate_detection_only": True,
            "human_review_required": True,
            "automatic_publish": False,
        },
    }


def _scan_evidence_lanes(
    plan: dict[str, Any],
    terms: list[dict[str, Any]],
    accumulator: dict[tuple[str, str, str, str], dict[str, Any]],
    lanes: dict[str, dict[str, Any]],
) -> None:
    required = set(plan["scan_policy"]["required_lanes"])
    evidence = list(plan.get("evidence") or [])
    lane_roles = {
        "text": {"subtitle", "script", "ocr_text", "document_text"},
        "audio": {"transcript"},
    }
    for lane_name, roles in lane_roles.items():
        if lane_name not in required:
            lanes[lane_name] = _lane("not_applicable", [], "not required by plan")
            continue
        sources = [row for row in evidence if row.get("role") in roles]
        if not sources:
            lanes[lane_name] = _lane(
                "incomplete", [], f"required {lane_name} evidence is missing"
            )
            continue
        errors = []
        scanned = []
        for row in sources:
            role = str(row["role"])
            artifact = _object(row["artifact"])
            path = Path(str(artifact["path"])).resolve()
            try:
                count = 0
                for text, location in _iter_artifact_text(path):
                    count += _scan_text(
                        text,
                        lane_name,
                        role,
                        artifact,
                        location,
                        terms,
                        accumulator,
                    )
                scanned.append(
                    {"role": role, "sha256": artifact["sha256"], "hits": count}
                )
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                errors.append(f"{role}: {exc}")
        lanes[lane_name] = _lane(
            "complete" if not errors else "incomplete",
            scanned,
            "; ".join(errors)
            if errors
            else "existing reviewed evidence consumed read-only",
        )


def _scan_metadata_lane(
    plan: dict[str, Any],
    final_path: Path,
    terms: list[dict[str, Any]],
    accumulator: dict[tuple[str, str, str, str], dict[str, Any]],
    lanes: dict[str, dict[str, Any]],
    providers: dict[str, Any],
) -> None:
    if "metadata" not in plan["scan_policy"]["required_lanes"]:
        lanes["metadata"] = _lane("not_applicable", [], "not required by plan")
        return
    artifact = _object(plan["final_artifact"])
    scanned = []
    errors = []
    _scan_text(
        final_path.name,
        "metadata",
        "filename",
        artifact,
        {"kind": "filename"},
        terms,
        accumulator,
    )
    suffix = final_path.suffix.casefold()
    if suffix in VIDEO_SUFFIXES or suffix in AUDIO_SUFFIXES:
        try:
            probe = _ffprobe(final_path)
            providers["ffprobe"] = {"status": "ready", "mode": "local_metadata"}
            for key_path, value in _iter_json_strings(probe):
                lowered = key_path.casefold()
                if ".tags." not in lowered:
                    continue
                _scan_text(
                    value,
                    "metadata",
                    "ffprobe_tag",
                    artifact,
                    {"kind": "metadata", "key_path": key_path},
                    terms,
                    accumulator,
                )
                if any(
                    marker in lowered
                    for marker in ("location", "title", "artist", "comment", "author")
                ):
                    _record_candidate(
                        accumulator,
                        lane="metadata",
                        entity_type="EMBEDDED_METADATA",
                        label="可能含身份或位置的媒体元数据",
                        detector="ffprobe_metadata_key",
                        internal_match=key_path,
                        source_role="ffprobe_tag",
                        source_artifact=artifact,
                        location={"kind": "metadata", "key_path": key_path},
                    )
            scanned.append({"role": "ffprobe_tags", "sha256": artifact["sha256"]})
        except (ContractError, OSError, json.JSONDecodeError) as exc:
            errors.append(str(exc))
            providers["ffprobe"] = {"status": "unavailable", "error": str(exc)}
    elif suffix in IMAGE_SUFFIXES:
        try:
            from PIL import Image  # type: ignore

            with Image.open(final_path) as image:
                exif = image.getexif()
                for key, value in exif.items():
                    rendered = str(value)
                    _scan_text(
                        rendered,
                        "metadata",
                        "image_exif",
                        artifact,
                        {"kind": "metadata", "key_path": f"exif.{key}"},
                        terms,
                        accumulator,
                    )
                if exif:
                    _record_candidate(
                        accumulator,
                        lane="metadata",
                        entity_type="IMAGE_EXIF",
                        label="图片包含 EXIF 元数据",
                        detector="pillow_exif_inventory",
                        internal_match="exif_present",
                        source_role="image_exif",
                        source_artifact=artifact,
                        location={"kind": "metadata", "key_path": "exif"},
                    )
            providers["pillow_exif"] = {"status": "ready"}
            scanned.append({"role": "image_exif", "sha256": artifact["sha256"]})
        except (ImportError, OSError, ValueError) as exc:
            errors.append(str(exc))
            providers["pillow_exif"] = {"status": "unavailable", "error": str(exc)}
    else:
        scanned.append({"role": "filename_only", "sha256": artifact["sha256"]})
    lanes["metadata"] = _lane(
        "complete" if not errors else "incomplete",
        scanned,
        "; ".join(errors) if errors else "final artifact metadata inspected",
    )


def _scan_visual_lane(
    plan: dict[str, Any],
    final_path: Path,
    root: Path,
    terms: list[dict[str, Any]],
    accumulator: dict[tuple[str, str, str, str], dict[str, Any]],
    lanes: dict[str, dict[str, Any]],
    providers: dict[str, Any],
    *,
    timeout_s: float,
) -> None:
    suffix = final_path.suffix.casefold()
    frames: list[tuple[Path, float]] = []
    errors = []
    scanned = []
    if suffix in IMAGE_SUFFIXES:
        frames = [(final_path, 0.0)]
    elif suffix in VIDEO_SUFFIXES:
        try:
            probe = _ffprobe(final_path)
            duration = _probe_duration(probe)
            requested = float(plan["scan_policy"]["visual_sample_interval_s"])
            maximum = int(plan["scan_policy"]["max_visual_frames"])
            effective = (
                max(requested, duration / maximum) if duration > 0 else requested
            )
            frames_dir = root / "visual-frames"
            if frames_dir.exists() and any(frames_dir.iterdir()):
                raise ContractError("privacy visual frame directory is not empty")
            frames_dir.mkdir(parents=True, exist_ok=True)
            pattern = frames_dir / "frame-%06d.jpg"
            receipt_path = root / "receipts" / "privacy-frame-extract.json"
            log_path = root / "logs" / "privacy-frame-extract.log"
            command = [
                "ffmpeg",
                "-y",
                "-i",
                str(final_path),
                "-vf",
                f"fps=1/{effective:.6f},scale=w='min(1280,iw)':h=-2",
                "-fps_mode",
                "vfr",
                "-q:v",
                "2",
                "-map_metadata",
                "-1",
                str(pattern),
            ]
            _receipt, paths = execute_ffmpeg_frame_series_with_receipt(
                command,
                [{"role": "final_candidate", "path": final_path}],
                pattern,
                receipt_path,
                media_time_step_s=effective,
                capture_log_path=log_path,
                timeout_s=timeout_s,
            )
            frames = [(path, index * effective) for index, path in enumerate(paths)]
            providers["ffmpeg_frame_sampler"] = {
                "status": "ready",
                "outlet": "video_creation_pipeline.single_ffmpeg_outlet",
                "requested_interval_s": requested,
                "effective_interval_s": round(effective, 4),
                "frame_count": len(frames),
                "receipt": artifact_reference(receipt_path, "privacy frame receipt"),
            }
        except (ContractError, OSError, ValueError) as exc:
            errors.append(f"frame extraction: {exc}")
    else:
        errors.append("final artifact is not a supported image or video")

    cv2_module = None
    try:
        import cv2  # type: ignore

        cv2_module = cv2
        providers["opencv_visual_candidates"] = {
            "status": "ready",
            "version": str(cv2.__version__),
            "qr_source_commit": OPENCV_QR_SOURCE_COMMIT,
            "qr_payload_persisted": False,
        }
    except ImportError as exc:
        errors.append(f"OpenCV: {exc}")
        providers["opencv_visual_candidates"] = {
            "status": "unavailable",
            "error": str(exc),
        }

    tesseract_path = _resolve_tool("tesseract")
    if tesseract_path:
        providers["tesseract_visual_ocr"] = {
            "status": "ready",
            "language": plan["scan_policy"]["ocr_language"],
            "role": "final_pixel_rescan_not_canonical_document_ocr",
        }
    else:
        errors.append("Tesseract executable was not found")
        providers["tesseract_visual_ocr"] = {"status": "unavailable"}

    face_locations = []
    qr_locations = []
    for index, (frame_path, media_time_s) in enumerate(frames, 1):
        location = {
            "kind": "video_frame" if suffix in VIDEO_SUFFIXES else "image",
            "frame_index": index,
            "media_time_s": round(media_time_s, 3),
        }
        hits = 0
        if tesseract_path:
            try:
                text = _run_tesseract(
                    tesseract_path,
                    frame_path,
                    str(plan["scan_policy"]["ocr_language"]),
                    timeout_s=min(timeout_s, 60.0),
                )
                hits += _scan_text(
                    text,
                    "visual",
                    "final_frame_ocr",
                    _object(plan["final_artifact"]),
                    location,
                    terms,
                    accumulator,
                )
            except (ContractError, OSError, subprocess.TimeoutExpired) as exc:
                errors.append(f"OCR frame {index}: {exc}")
        if cv2_module is not None:
            image = _read_cv_image(cv2_module, frame_path)
            if image is None:
                errors.append(f"OpenCV could not decode frame {index}")
            else:
                if _detect_qr_code(cv2_module, image):
                    qr_locations.append(location)
                if _detect_faces(cv2_module, image):
                    face_locations.append(location)
        scanned.append(
            {
                "frame_index": index,
                "media_time_s": round(media_time_s, 3),
                "ocr_candidate_hits": hits,
            }
        )
    if qr_locations:
        _record_group_candidate(
            accumulator,
            lane="visual",
            entity_type="QR_CODE",
            label="二维码候选",
            detector="opencv_qrcode_detector_payload_free",
            internal_match="qr_present",
            source_role="final_frame",
            source_artifact=_object(plan["final_artifact"]),
            locations=qr_locations,
        )
    if face_locations:
        _record_group_candidate(
            accumulator,
            lane="visual",
            entity_type="FACE",
            label="人物正脸候选",
            detector="opencv_haar_face_candidate",
            internal_match="face_present",
            source_role="final_frame",
            source_artifact=_object(plan["final_artifact"]),
            locations=face_locations,
        )
    lanes["visual"] = _lane(
        "complete" if frames and not errors else "incomplete",
        scanned,
        "; ".join(errors)
        if errors
        else "sampled final pixels; human full playback remains mandatory",
    )


def _scan_text(
    text: str,
    lane: str,
    source_role: str,
    source_artifact: dict[str, Any],
    base_location: dict[str, Any],
    terms: list[dict[str, Any]],
    accumulator: dict[tuple[str, str, str, str], dict[str, Any]],
) -> int:
    if not text:
        return 0
    hits = 0
    for entity_type, label, detector, pattern in BUILTIN_PATTERNS:
        for match in pattern.finditer(text):
            location = _match_location(text, match.start(), base_location)
            _record_candidate(
                accumulator,
                lane=lane,
                entity_type=entity_type,
                label=label,
                detector=detector,
                internal_match=match.group(0),
                source_role=source_role,
                source_artifact=source_artifact,
                location=location,
            )
            hits += 1
    for term in terms:
        if lane not in term["lanes"]:
            continue
        needle = term["value"]
        start = 0
        while True:
            index = text.find(needle, start)
            if index < 0:
                break
            _record_candidate(
                accumulator,
                lane=lane,
                entity_type=term["entity_type"],
                label=term["label"],
                detector="presidio_denylist_contract_adaptation",
                internal_match=needle,
                source_role=source_role,
                source_artifact=source_artifact,
                location=_match_location(text, index, base_location),
            )
            hits += 1
            start = index + max(1, len(needle))
    return hits


def _record_candidate(
    accumulator: dict[tuple[str, str, str, str], dict[str, Any]],
    *,
    lane: str,
    entity_type: str,
    label: str,
    detector: str,
    internal_match: str,
    source_role: str,
    source_artifact: dict[str, Any],
    location: dict[str, Any],
) -> None:
    key = (lane, entity_type, detector, internal_match)
    row = accumulator.setdefault(
        key,
        {
            "lane": lane,
            "entity_type": entity_type,
            "label": label,
            "detector": detector,
            "matched_length": len(internal_match),
            "source_roles": set(),
            "source_sha256": set(),
            "locations": [],
        },
    )
    row["source_roles"].add(source_role)
    row["source_sha256"].add(str(source_artifact["sha256"]))
    if len(row["locations"]) < 100:
        row["locations"].append(dict(location))
    row["occurrence_count"] = int(row.get("occurrence_count") or 0) + 1


def _record_group_candidate(
    accumulator: dict[tuple[str, str, str, str], dict[str, Any]],
    *,
    lane: str,
    entity_type: str,
    label: str,
    detector: str,
    internal_match: str,
    source_role: str,
    source_artifact: dict[str, Any],
    locations: list[dict[str, Any]],
) -> None:
    for location in locations:
        _record_candidate(
            accumulator,
            lane=lane,
            entity_type=entity_type,
            label=label,
            detector=detector,
            internal_match=internal_match,
            source_role=source_role,
            source_artifact=source_artifact,
            location=location,
        )


def _finalize_findings(
    accumulator: dict[tuple[str, str, str, str], dict[str, Any]],
) -> list[dict[str, Any]]:
    findings = []
    for row in accumulator.values():
        value = {
            "lane": row["lane"],
            "entity_type": row["entity_type"],
            "label": row["label"],
            "detector": row["detector"],
            "matched_length": row["matched_length"],
            "source_roles": sorted(row["source_roles"]),
            "source_sha256": sorted(row["source_sha256"]),
            "locations": row["locations"],
            "occurrence_count": row["occurrence_count"],
            "raw_match_stored": False,
            "human_disposition_required": True,
        }
        identity = json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")
        value["finding_id"] = "privacy-" + hashlib.sha256(identity).hexdigest()[:20]
        findings.append(value)
    return sorted(findings, key=lambda row: row["finding_id"])


def _iter_artifact_text(path: Path) -> Iterable[tuple[str, dict[str, Any]]]:
    if path.suffix.casefold() not in TEXT_SUFFIXES:
        raise ContractError(
            f"privacy text evidence is not a supported text file: {path}"
        )
    if path.suffix.casefold() == ".json":
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        yield from (
            (value, {"kind": "json", "key_path": key_path})
            for key_path, value in _iter_json_strings(payload)
        )
        return
    text = path.read_text(encoding="utf-8-sig")
    yield text, {"kind": "text"}


def _iter_json_strings(value: Any, prefix: str = "$") -> Iterable[tuple[str, str]]:
    if isinstance(value, str):
        yield prefix, value
    elif isinstance(value, dict):
        for key, child in value.items():
            yield from _iter_json_strings(child, f"{prefix}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _iter_json_strings(child, f"{prefix}[{index}]")


def _match_location(
    text: str, start: int, base_location: dict[str, Any]
) -> dict[str, Any]:
    location = dict(base_location)
    if location.get("kind") in {"text", "json", "metadata", "filename"}:
        line = text.count("\n", 0, start) + 1
        line_start = text.rfind("\n", 0, start) + 1
        location["line"] = line
        location["column"] = start - line_start + 1
    return location


def _term_entries(payload: dict[str, Any]) -> list[dict[str, Any]]:
    _validate_terms(payload)
    return [
        {
            "label": str(row["label"]),
            "entity_type": str(row["entity_type"]),
            "value": str(row["value"]),
            "lanes": list(row["lanes"]),
        }
        for row in payload.get("terms") or []
    ]


def _validate_terms(payload: dict[str, Any]) -> None:
    if payload.get("schema") != PRIVACY_TERMS_SCHEMA:
        raise ContractError("unsupported privacy terms schema")
    rows = payload.get("terms")
    if not isinstance(rows, list):
        raise ContractError("privacy terms must be a list")
    for row in rows:
        item = _object(row)
        if not str(item.get("label") or "").strip():
            raise ContractError("privacy term label is required")
        if not str(item.get("entity_type") or "").strip():
            raise ContractError("privacy term entity_type is required")
        if not str(item.get("value") or ""):
            raise ContractError("privacy term value is required")
        lanes = item.get("lanes")
        if not isinstance(lanes, list) or not lanes or not set(lanes) <= LANES:
            raise ContractError("privacy term lanes are invalid")


def _normalize_subject_policy(value: dict[str, Any]) -> dict[str, str]:
    policy = {
        "mode": str(value.get("mode") or "").strip(),
        "face": str(value.get("face") or "").strip(),
        "voice": str(value.get("voice") or "").strip(),
    }
    if policy["mode"] not in {
        "authorized_identifiable",
        "deidentified",
        "synthetic",
    }:
        raise ContractError("privacy subject mode is invalid")
    if policy["face"] not in {"allowed", "must_mask", "not_applicable"}:
        raise ContractError("privacy face policy is invalid")
    if policy["voice"] not in {"allowed", "must_mask", "not_applicable"}:
        raise ContractError("privacy voice policy is invalid")
    return policy


def _default_lanes(final_path: Path, evidence: list[dict[str, Any]]) -> list[str]:
    suffix = final_path.suffix.casefold()
    lanes = {"metadata"}
    if suffix in VIDEO_SUFFIXES:
        lanes.update({"audio", "visual"})
    elif suffix in AUDIO_SUFFIXES:
        lanes.add("audio")
    elif suffix in IMAGE_SUFFIXES:
        lanes.add("visual")
    if any(str(row.get("role") or "") != "transcript" for row in evidence):
        lanes.add("text")
    return sorted(lanes)


def _ffprobe(path: Path) -> dict[str, Any]:
    executable = _resolve_tool("ffprobe")
    if not executable:
        raise ContractError("ffprobe executable was not found")
    result = subprocess.run(
        [
            executable,
            "-v",
            "error",
            "-show_format",
            "-show_streams",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        errors="replace",
        check=False,
        timeout=60,
    )
    if result.returncode != 0:
        raise ContractError(f"ffprobe failed: {(result.stderr or '')[-1000:]}")
    payload = json.loads(result.stdout)
    if not isinstance(payload, dict):
        raise ContractError("ffprobe did not return a JSON object")
    return payload


def _probe_duration(probe: dict[str, Any]) -> float:
    raw = _object(probe.get("format")).get("duration")
    try:
        duration = float(raw)
    except (TypeError, ValueError) as exc:
        raise ContractError("video duration is unavailable") from exc
    if duration <= 0 or not math.isfinite(duration):
        raise ContractError("video duration is invalid")
    return duration


def _run_tesseract(
    executable: str,
    image_path: Path,
    language: str,
    *,
    timeout_s: float,
) -> str:
    result = subprocess.run(
        [executable, str(image_path), "stdout", "-l", language, "--psm", "6"],
        capture_output=True,
        text=True,
        errors="replace",
        check=False,
        timeout=timeout_s,
    )
    if result.returncode != 0:
        raise ContractError(f"Tesseract failed: {(result.stderr or '')[-1000:]}")
    return result.stdout or ""


def _read_cv_image(cv2_module: Any, path: Path) -> Any | None:
    try:
        import numpy as np  # type: ignore

        data = np.fromfile(str(path), dtype=np.uint8)
        return cv2_module.imdecode(data, cv2_module.IMREAD_COLOR)
    except (ImportError, OSError, ValueError):
        return None


def _detect_qr_code(cv2_module: Any, image: Any) -> bool:
    """Adapt the reviewed payload-free OpenCV QR detector used by wechat-tools."""

    if image is None or getattr(image, "size", 0) == 0:
        return False
    detector = cv2_module.QRCodeDetector()
    try:
        found, points = detector.detect(image)
        if bool(found) and points is not None:
            return True
    except cv2_module.error:
        pass
    try:
        found, points = detector.detectMulti(image)
        return bool(found) and points is not None
    except (AttributeError, cv2_module.error, ValueError):
        return False


def detect_face_boxes(cv2_module: Any, image: Any) -> list[dict[str, int]]:
    """Return conservative Haar face candidates without identifying anyone."""
    try:
        cascade_path = (
            Path(cv2_module.data.haarcascades) / "haarcascade_frontalface_default.xml"
        )
        classifier = cv2_module.CascadeClassifier(str(cascade_path))
        if classifier.empty():
            return []
        gray = cv2_module.cvtColor(image, cv2_module.COLOR_BGR2GRAY)
        faces = classifier.detectMultiScale(
            gray,
            scaleFactor=1.1,
            minNeighbors=5,
            minSize=(40, 40),
        )
        return [
            {
                "x": int(x),
                "y": int(y),
                "width": int(width),
                "height": int(height),
            }
            for x, y, width, height in faces
        ]
    except (AttributeError, cv2_module.error, ValueError):
        return []


def _detect_faces(cv2_module: Any, image: Any) -> bool:
    return bool(detect_face_boxes(cv2_module, image))


def _resolve_tool(name: str) -> str | None:
    return _resolve_windows_tool_wrapper(shutil.which(name), name)


def _tool_status(name: str) -> dict[str, Any]:
    try:
        path = _resolve_tool(name)
    except ContractError as exc:
        return {"status": "blocked", "error": str(exc)}
    return {"status": "ready", "path": path} if path else {"status": "missing"}


def _lane(status: str, scanned: list[dict[str, Any]], note: str) -> dict[str, Any]:
    return {"status": status, "scanned": scanned, "note": note}


def _validate_schema(payload: dict[str, Any], filename: str, label: str) -> None:
    schema = read_json_object(Path(__file__).with_name("schemas") / filename, label)
    try:
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(payload)
    except (SchemaError, ValidationError) as exc:
        raise ContractError(f"{label} schema validation failed: {exc.message}") from exc


def _object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError("expected a JSON object")
    return value


def _positive_number(value: Any, label: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{label} must be a positive number") from exc
    if result <= 0 or not math.isfinite(result):
        raise ContractError(f"{label} must be a positive number")
    return result


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()
