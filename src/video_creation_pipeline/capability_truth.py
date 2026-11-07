from __future__ import annotations

import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError, ValidationError

from .background_processing import (
    BACKGROUND_PROCESSING_RECEIPT_SCHEMA,
    background_processing_doctor,
    validate_background_processing_receipt,
)
from .color_processing import (
    COLOR_PROCESSING_RECEIPT_SCHEMA,
    color_processing_doctor,
    validate_color_processing_receipt,
)
from .content_video_foundation import build_doctor_report
from .content_video_pipeline import validate_content_video_project_plan
from .content_video_synthetic import validate_content_video_atomic_output_manifest
from .contract_io import (
    artifact_reference,
    atomic_write_json,
    canonical_json_sha256,
    payload_sha256,
    read_json_object,
    validate_artifact_reference,
)
from .errors import ContractError
from .execution_receipts import FFMPEG_OUTLET_ID, validate_ffmpeg_execution_receipt
from .music_pipeline import validate_music_edit_plan
from .privacy_gate import (
    PRIVACY_SCAN_REPORT_SCHEMA,
    privacy_gate_doctor,
    validate_privacy_scan_report,
)
from .reframe_detectors import smart_reframe_doctor
from .remotion_project import (
    REMOTION_RUN_RECEIPT_SCHEMA,
    remotion_project_doctor,
    validate_remotion_run_receipt,
)
from .semantic_motion import (
    HYPERFRAMES_CLI_RELATIVE,
    HYPERFRAMES_SOURCE_COMMIT,
    HYPERFRAMES_VERSION,
    SEMANTIC_MOTION_RUN_RECEIPT_SCHEMA,
    validate_semantic_motion_run_receipt,
)
from .speech_cleanup import (
    SPEECH_CLEANUP_RECEIPT_SCHEMA,
    speech_cleanup_doctor,
    validate_speech_cleanup_receipt,
)
from .technical_qa import validate_technical_qa_report

CAPABILITY_DOCTOR_SCHEMA = "video_creation_pipeline.capability_doctor.v1"
CAPABILITY_TRUTH_SCHEMA = "video_creation_pipeline.capability_truth.v1"
CONTENT_SYNTHETIC_RUN_RECEIPT_SCHEMA = (
    "video_creation_pipeline.content_video_synthetic_run_receipt.v2"
)
REMOTION_SCENE_COMPLETION_RECEIPT_SCHEMA = (
    "video_creation_pipeline.remotion_scene_media_completion_receipt.v1"
)
SPEECH_CLEANUP_COMPLETION_RECEIPT_SCHEMA = (
    "video_creation_pipeline.speech_cleanup_completion_receipt.v1"
)
BACKGROUND_PROCESSING_COMPLETION_RECEIPT_SCHEMA = (
    "video_creation_pipeline.background_processing_completion_receipt.v1"
)
COLOR_PROCESSING_COMPLETION_RECEIPT_SCHEMA = (
    "video_creation_pipeline.color_processing_completion_receipt.v1"
)
WINDOWS_TEST_RECEIPT_SCHEMA = "windows-test-runtime-command-receipt.v3"
DEFAULT_TEST_FRESHNESS_HOURS = 168.0
DEFAULT_DOCTOR_FRESHNESS_HOURS = 1.0

CAPABILITY_IDS = (
    "canonical_candidate_pipeline",
    "hyperframes_semantic_motion",
    "remotion_scene_media_render",
    "licensed_audio_mix",
    "speech_noise_cleanup",
    "privacy_candidate_scan",
    "privacy_dynamic_layout_avoidance",
    "nle_import",
    "real_person_gold_e2e",
    "person_background_processing",
    "basic_color_processing",
    "smart_reframe_candidate",
    "automatic_publish",
)
PRE_REFRAME_CAPABILITY_IDS = tuple(
    capability_id
    for capability_id in CAPABILITY_IDS
    if capability_id != "smart_reframe_candidate"
)
PRE_COLOR_CAPABILITY_IDS = tuple(
    capability_id
    for capability_id in PRE_REFRAME_CAPABILITY_IDS
    if capability_id != "basic_color_processing"
)
PRE_SPEECH_CAPABILITY_IDS = tuple(
    capability_id
    for capability_id in CAPABILITY_IDS
    if capability_id != "speech_noise_cleanup"
)
PRE_REFRAME_PRE_SPEECH_CAPABILITY_IDS = tuple(
    capability_id
    for capability_id in PRE_REFRAME_CAPABILITY_IDS
    if capability_id != "speech_noise_cleanup"
)
LEGACY_CAPABILITY_IDS = tuple(
    capability_id
    for capability_id in PRE_COLOR_CAPABILITY_IDS
    if capability_id != "speech_noise_cleanup"
)


def build_capability_doctor(
    *,
    observed_at: datetime | str | None = None,
    repository_root: str | Path | None = None,
    hyperframes_source_root: str | Path | None = None,
    hyperframes_browser_path: str | Path | None = None,
    hyperframes_ffmpeg_path: str | Path | None = None,
    hyperframes_ffprobe_path: str | Path | None = None,
    remotion_runtime_source: str | Path | None = None,
    remotion_project_dir: str | Path | None = None,
    remotion_browser_path: str | Path | None = None,
    background_provider_source_root: str | Path | None = None,
    background_model_path: str | Path | None = None,
    background_runtime_dependencies_path: str | Path | None = None,
    background_device: str = "cpu",
) -> dict[str, Any]:
    root = (
        Path(repository_root).expanduser().resolve()
        if repository_root is not None
        else Path(__file__).resolve().parents[2]
    )
    timestamp = _as_datetime(observed_at or datetime.now(UTC), "doctor observed_at")
    canonical = build_doctor_report()
    hyperframes = _hyperframes_doctor(
        source_root=hyperframes_source_root,
        browser_path=hyperframes_browser_path,
        ffmpeg_path=hyperframes_ffmpeg_path,
        ffprobe_path=hyperframes_ffprobe_path,
    )
    remotion = remotion_project_doctor(
        remotion_runtime_source,
        remotion_project_dir,
        remotion_browser_path,
    )
    privacy = privacy_gate_doctor()
    speech_cleanup = _speech_cleanup_probe()
    background_processing = _background_processing_probe(
        provider_source_root=background_provider_source_root,
        model_path=background_model_path,
        runtime_dependencies_path=background_runtime_dependencies_path,
        device=background_device,
    )
    color_processing = _color_processing_probe()
    smart_reframe = smart_reframe_doctor()
    privacy_layout_value = privacy.get("dynamic_layout_avoidance")
    privacy_layout = (
        dict(privacy_layout_value) if isinstance(privacy_layout_value, dict) else {}
    )
    canonical_ready = canonical.get("status") == "ready"
    capabilities = {
        "canonical_candidate_pipeline": _doctor_row(
            "canonical_candidate_pipeline",
            "executable",
            True,
            canonical_ready,
            str(canonical.get("status") or "blocked"),
            "Canonical brief planning and synthetic candidate rendering use the existing FFmpeg outlet.",
        ),
        "hyperframes_semantic_motion": _doctor_row(
            "hyperframes_semantic_motion",
            "executable",
            True,
            hyperframes.get("status") == "ready_for_local_candidate_render",
            str(hyperframes.get("status") or "blocked"),
            "HyperFrames must be supplied explicitly and match the reviewed source/runtime contract.",
        ),
        "remotion_scene_media_render": _doctor_row(
            "remotion_scene_media_render",
            "executable",
            True,
            remotion.get("render_ready") is True,
            str(remotion.get("status") or "blocked"),
            "Remotion requires an explicit reviewed runtime, materialized project, and local browser.",
        ),
        "licensed_audio_mix": _doctor_row(
            "licensed_audio_mix",
            "executable",
            True,
            canonical_ready,
            str(canonical.get("status") or "blocked"),
            "Music and sound-effect mixing reuse the canonical local FFmpeg runtime.",
        ),
        "speech_noise_cleanup": _doctor_row(
            "speech_noise_cleanup",
            "executable",
            True,
            speech_cleanup.get("status") == "ready",
            str(speech_cleanup.get("status") or "blocked"),
            "Speech cleanup reuses the reviewed local FFmpeg filters and remains audition-only.",
        ),
        "privacy_candidate_scan": _doctor_row(
            "privacy_candidate_scan",
            "executable",
            True,
            privacy.get("status") == "ready",
            str(privacy.get("status") or "blocked"),
            "Machine detection remains candidate-only and always requires human disposition.",
        ),
        "privacy_dynamic_layout_avoidance": _doctor_row(
            "privacy_dynamic_layout_avoidance",
            "executable",
            privacy_layout.get("contract_ready") is True,
            privacy_layout.get("status") == "runtime_ready",
            str(privacy_layout.get("status") or "blocked_legacy_doctor"),
            "Dynamic exclusion zones remain candidate-only and require final privacy rescan.",
        ),
        "nle_import": _doctor_row(
            "nle_import",
            "handoff",
            True,
            False,
            "handoff_available",
            "NLE jobs and candidate drafts exist, but editor-side import is not verified here.",
        ),
        "real_person_gold_e2e": _doctor_row(
            "real_person_gold_e2e",
            "blocked",
            False,
            False,
            "missing_authorized_gold_receipt",
            "No authorized real-person gold E2E receipt is registered.",
        ),
        "person_background_processing": _doctor_row(
            "person_background_processing",
            "executable",
            True,
            background_processing.get("status") == "ready",
            str(background_processing.get("status") or "blocked"),
            "Person matting uses the reviewed local RVM Provider; compositing and encoding remain in the single FFmpeg outlet.",
        ),
        "basic_color_processing": _doctor_row(
            "basic_color_processing",
            "executable",
            True,
            color_processing.get("status") == "ready",
            str(color_processing.get("status") or "blocked"),
            "Basic color candidates reuse reviewed FFmpeg filters, hash-bound signalstats analysis, atomic promotion, and audio packet preservation checks.",
        ),
        "smart_reframe_candidate": _doctor_row(
            "smart_reframe_candidate",
            "executable",
            True,
            smart_reframe.get("status") == "ready_for_evidence_driven_candidate_plan",
            str(smart_reframe.get("status") or "blocked"),
            "Evidence-driven plans and candidate rendering reuse reviewed adapters and the single FFmpeg outlet; optional detectors remain separately visible.",
        ),
        "automatic_publish": _doctor_row(
            "automatic_publish",
            "blocked",
            False,
            False,
            "policy_blocked",
            "Publication remains outside pipeline execution authority.",
        ),
    }
    report: dict[str, Any] = {
        "schema": CAPABILITY_DOCTOR_SCHEMA,
        "observed_at": timestamp.isoformat(),
        "repository": _repository_provenance(root),
        "probes": {
            "canonical": canonical,
            "hyperframes": hyperframes,
            "remotion": remotion,
            "privacy": privacy,
            "speech_cleanup": speech_cleanup,
            "background_processing": background_processing,
            "color_processing": color_processing,
            "smart_reframe": smart_reframe,
        },
        "capabilities": capabilities,
        "authority_boundary": _authority_boundary(),
    }
    report["doctor_sha256"] = payload_sha256(report, "doctor_sha256")
    validate_capability_doctor(report)
    return report


def validate_capability_doctor(report: dict[str, Any]) -> None:
    if report.get("schema") != CAPABILITY_DOCTOR_SCHEMA:
        raise ContractError("unsupported capability doctor schema")
    if report.get("doctor_sha256") != payload_sha256(report, "doctor_sha256"):
        raise ContractError("capability doctor integrity check failed")
    _validate_schema(report, "capability_doctor.v1.schema.json", "capability doctor")
    capability_ids = set(report["capabilities"])
    compatible_sets = (
        CAPABILITY_IDS,
        PRE_REFRAME_CAPABILITY_IDS,
        PRE_REFRAME_PRE_SPEECH_CAPABILITY_IDS,
        PRE_COLOR_CAPABILITY_IDS,
        PRE_SPEECH_CAPABILITY_IDS,
        LEGACY_CAPABILITY_IDS,
    )
    if not any(
        capability_ids == set(expected)
        or capability_ids
        == {value for value in expected if value != "privacy_dynamic_layout_avoidance"}
        for expected in compatible_sets
    ):
        raise ContractError("capability doctor catalog drifted")
    for capability_id, row in report["capabilities"].items():
        value = _object(row, "capability doctor row")
        if value.get("capability_id") != capability_id:
            raise ContractError("capability doctor row identity drifted")
        if (
            value.get("runtime_ready") is True
            and value.get("contract_ready") is not True
        ):
            raise ContractError("capability runtime cannot be ready without a contract")
        if (
            value.get("contract_mode") in {"handoff", "blocked"}
            and value.get("runtime_ready") is not False
        ):
            raise ContractError("non-executable capability cannot be runtime-ready")
    if report.get("authority_boundary") != _authority_boundary():
        raise ContractError("capability doctor authority boundary drifted")


def write_capability_doctor(report: dict[str, Any], output_path: str | Path) -> Path:
    validate_capability_doctor(report)
    return atomic_write_json(report, output_path)


def build_capability_truth(
    doctor_path: str | Path,
    *,
    evidence_paths: list[str | Path] | None = None,
    test_receipt_path: str | Path | None = None,
    observed_at: datetime | str | None = None,
    test_freshness_hours: float = DEFAULT_TEST_FRESHNESS_HOURS,
    doctor_freshness_hours: float = DEFAULT_DOCTOR_FRESHNESS_HOURS,
) -> dict[str, Any]:
    timestamp = _as_datetime(observed_at or datetime.now(UTC), "truth observed_at")
    doctor_file = Path(doctor_path).expanduser().resolve()
    doctor = read_json_object(doctor_file, "capability doctor")
    validate_capability_doctor(doctor)
    doctor_projection = _doctor_projection(
        doctor,
        doctor_file,
        timestamp,
        doctor_freshness_hours,
    )
    test_projection = (
        _test_receipt_projection(
            Path(test_receipt_path).expanduser().resolve(),
            expected_repository=Path(doctor["repository"]["path"]),
            observed_at=timestamp,
            freshness_hours=test_freshness_hours,
        )
        if test_receipt_path is not None
        else None
    )
    source_receipts = [
        artifact_reference(path, f"capability evidence {index}")
        for index, path in enumerate(evidence_paths or [], 1)
    ]
    source_receipts.sort(key=lambda row: (str(row["sha256"]), str(row["path"])))
    evidence = _derive_evidence(source_receipts, timestamp)
    capabilities = _derive_capabilities(
        doctor,
        doctor_projection,
        test_projection,
        evidence,
    )
    truth: dict[str, Any] = {
        "schema": CAPABILITY_TRUTH_SCHEMA,
        "observed_at": timestamp.isoformat(),
        "policy": {
            "test_freshness_hours": float(test_freshness_hours),
            "doctor_freshness_hours": float(doctor_freshness_hours),
            "doctor_or_tests_alone_can_promote": False,
            "unknown_receipt_schema_allowed": False,
            "real_media_requires_explicit_scope_evidence": True,
        },
        "repository_provenance": dict(doctor["repository"]),
        "doctor_evidence": doctor_projection,
        "test_evidence": test_projection,
        "source_receipts": source_receipts,
        "evidence": evidence,
        "capabilities": capabilities,
        "summary": _status_summary(capabilities),
        "authority_boundary": _authority_boundary(),
    }
    truth["matrix_sha256"] = payload_sha256(truth, "matrix_sha256")
    validate_capability_truth(truth)
    return truth


def validate_capability_truth(truth: dict[str, Any]) -> None:
    if truth.get("schema") != CAPABILITY_TRUTH_SCHEMA:
        raise ContractError("unsupported capability truth schema")
    if truth.get("matrix_sha256") != payload_sha256(truth, "matrix_sha256"):
        raise ContractError("capability truth integrity check failed")
    _validate_schema(truth, "capability_truth.v1.schema.json", "capability truth")
    observed_at = _as_datetime(truth["observed_at"], "truth observed_at")
    policy = _object(truth.get("policy"), "capability truth policy")
    doctor_ref = _object(
        _object(truth.get("doctor_evidence"), "doctor evidence").get("artifact"),
        "doctor artifact reference",
    )
    doctor_path = validate_artifact_reference(doctor_ref, "capability doctor")
    doctor = read_json_object(doctor_path, "capability doctor")
    validate_capability_doctor(doctor)
    expected_doctor = _doctor_projection(
        doctor,
        doctor_path,
        observed_at,
        float(policy["doctor_freshness_hours"]),
    )
    if truth.get("doctor_evidence") != expected_doctor:
        raise ContractError("capability doctor projection drifted")
    if truth.get("repository_provenance") != doctor.get("repository"):
        raise ContractError("capability repository provenance drifted")

    test_value = truth.get("test_evidence")
    if test_value is None:
        expected_test = None
    else:
        test_ref = _object(
            _object(test_value, "test evidence").get("artifact"),
            "test receipt reference",
        )
        test_path = validate_artifact_reference(test_ref, "managed test receipt")
        expected_test = _test_receipt_projection(
            test_path,
            expected_repository=Path(doctor["repository"]["path"]),
            observed_at=observed_at,
            freshness_hours=float(policy["test_freshness_hours"]),
        )
    if test_value != expected_test:
        raise ContractError("managed test receipt projection drifted")

    source_receipts = truth.get("source_receipts")
    if not isinstance(source_receipts, list):
        raise ContractError("capability truth source receipts must be a list")
    for index, reference in enumerate(source_receipts, 1):
        validate_artifact_reference(
            _object(reference, "source receipt reference"),
            f"capability evidence {index}",
        )
    expected_evidence = _derive_evidence(source_receipts, observed_at)
    if truth.get("evidence") != expected_evidence:
        raise ContractError("capability execution evidence drifted")
    expected_capabilities = _derive_capabilities(
        doctor,
        expected_doctor,
        expected_test,
        expected_evidence,
    )
    if truth.get("capabilities") != expected_capabilities:
        raise ContractError("capability status projection drifted")
    if truth.get("summary") != _status_summary(expected_capabilities):
        raise ContractError("capability truth summary drifted")
    if truth.get("authority_boundary") != _authority_boundary():
        raise ContractError("capability truth authority boundary drifted")


def write_capability_truth(truth: dict[str, Any], output_path: str | Path) -> Path:
    validate_capability_truth(truth)
    return atomic_write_json(truth, output_path)


def read_capability_truth_summary(source: str | Path) -> dict[str, Any]:
    path = Path(source).expanduser().resolve()
    truth = read_json_object(path, "capability truth")
    validate_capability_truth(truth)
    return {
        "schema": "video_creation_pipeline.capability_truth_summary.v1",
        "artifact": artifact_reference(path, "capability truth"),
        "observed_at": truth["observed_at"],
        "summary": dict(truth["summary"]),
        "capabilities": {
            capability_id: {
                "status": row["status"],
                "reason": row["reason"],
            }
            for capability_id, row in truth["capabilities"].items()
        },
        "publish_authorized": False,
    }


def validate_content_synthetic_run_receipt(receipt: dict[str, Any]) -> None:
    if receipt.get("schema") != CONTENT_SYNTHETIC_RUN_RECEIPT_SCHEMA:
        raise ContractError("unsupported content synthetic run receipt schema")
    if receipt.get("receipt_sha256") != payload_sha256(receipt, "receipt_sha256"):
        raise ContractError("content synthetic run receipt integrity check failed")
    if receipt.get("status") != "completed_candidate_only":
        raise ContractError("content synthetic run must remain candidate-only")
    for field in (
        "synthetic_material_only",
        "production_success_claimed",
        "upload_performed",
        "publish_performed",
    ):
        expected = field == "synthetic_material_only"
        if receipt.get(field) is not expected:
            raise ContractError(f"content synthetic boundary drifted: {field}")
    source_plan_ref = _object(receipt.get("source_plan"), "source plan reference")
    manifest_ref = _object(receipt.get("render_manifest"), "render manifest reference")
    atomic_output_ref = _object(
        receipt.get("atomic_output_manifest"), "atomic output manifest reference"
    )
    candidate_ref = _object(receipt.get("candidate_video"), "candidate reference")
    qa_ref = _object(receipt.get("qa_report"), "QA reference")
    source_plan_path = validate_artifact_reference(
        source_plan_ref, "content video project plan"
    )
    manifest_path = validate_artifact_reference(
        manifest_ref, "content video render manifest"
    )
    atomic_output_path = validate_artifact_reference(
        atomic_output_ref, "content video atomic output manifest"
    )
    candidate_path = validate_artifact_reference(
        candidate_ref, "content video candidate"
    )
    qa_path = validate_artifact_reference(qa_ref, "content video QA report")
    plan = read_json_object(source_plan_path, "content video project plan")
    validate_content_video_project_plan(plan)
    manifest = read_json_object(manifest_path, "content video render manifest")
    atomic_output_manifest = read_json_object(
        atomic_output_path, "content video atomic output manifest"
    )
    validate_content_video_atomic_output_manifest(atomic_output_manifest)
    if (
        manifest.get("schema")
        != "video_creation_pipeline.content_video_render_manifest.v1"
    ):
        raise ContractError("unsupported content video render manifest schema")
    if manifest.get("render_manifest_sha256") != payload_sha256(
        manifest, "render_manifest_sha256"
    ):
        raise ContractError("content video render manifest integrity check failed")
    if (
        manifest.get("status") != "candidate_only"
        or manifest.get("synthetic_material_only") is not True
        or manifest.get("production_success_claimed") is not False
        or manifest.get("external_provider_called") is not False
        or manifest.get("upload_performed") is not False
        or manifest.get("publish_performed") is not False
    ):
        raise ContractError("content video render manifest authority boundary drifted")
    if manifest.get("source_project_plan") != source_plan_ref:
        raise ContractError("content video manifest source plan drifted")
    if manifest.get("candidate_video") != candidate_ref:
        raise ContractError("content video manifest candidate drifted")
    if manifest.get("technical_qa") != qa_ref:
        raise ContractError("content video manifest QA binding drifted")
    if manifest.get("atomic_output_manifest") != atomic_output_ref:
        raise ContractError("content video manifest atomic output binding drifted")
    if atomic_output_manifest.get("source_project_plan") != source_plan_ref:
        raise ContractError("content video atomic output source plan drifted")
    for field, label in (
        ("plan_manifest", "plan manifest"),
        ("subtitles", "subtitles"),
        ("cover", "cover"),
        ("packaging_plan", "packaging plan"),
        ("music_edit_plan", "music edit plan"),
    ):
        validate_artifact_reference(
            _object(manifest.get(field), f"{label} reference"), label
        )
    music_path = Path(
        str(_object(manifest["music_edit_plan"], "music edit plan reference")["path"])
    )
    validate_music_edit_plan(read_json_object(music_path, "music edit plan"))
    qa = read_json_object(qa_path, "technical QA report")
    validate_technical_qa_report(qa)
    readiness = _object(qa.get("readiness"), "technical QA readiness")
    qa_media = _object(
        _object(qa.get("inputs"), "technical QA inputs").get("media"),
        "technical QA media reference",
    )
    if (
        readiness.get("technical_ready") is not True
        or readiness.get("release_eligible") is not False
        or qa_media.get("sha256") != candidate_ref.get("sha256")
        or candidate_path != Path(str(candidate_ref["path"])).resolve()
    ):
        raise ContractError("content synthetic candidate is not bound to passing QA")
    ffmpeg_rows = manifest.get("ffmpeg_receipts")
    if not isinstance(ffmpeg_rows, list) or not ffmpeg_rows:
        raise ContractError("content video render manifest requires FFmpeg receipts")
    if receipt.get("ffmpeg_receipt_count") != len(ffmpeg_rows):
        raise ContractError("content synthetic FFmpeg receipt count drifted")
    atomic_receipt_paths = {
        str(_object(row.get("ffmpeg_receipt"), "atomic FFmpeg receipt").get("path"))
        for row in atomic_output_manifest["outputs"]
    }
    render_receipt_paths = {
        str(_object(reference, "FFmpeg receipt reference").get("path"))
        for reference in ffmpeg_rows
    }
    if atomic_receipt_paths != render_receipt_paths:
        raise ContractError("content synthetic atomic FFmpeg coverage drifted")
    for index, reference in enumerate(ffmpeg_rows, 1):
        ffmpeg_path = validate_artifact_reference(
            _object(reference, "FFmpeg receipt reference"),
            f"content video FFmpeg receipt {index}",
        )
        ffmpeg_receipt = read_json_object(ffmpeg_path, "FFmpeg receipt")
        validate_ffmpeg_execution_receipt(ffmpeg_receipt)
        if ffmpeg_receipt.get("outlet_id") != FFMPEG_OUTLET_ID:
            raise ContractError("content video used a non-canonical FFmpeg outlet")


def _doctor_row(
    capability_id: str,
    contract_mode: str,
    contract_ready: bool,
    runtime_ready: bool,
    doctor_status: str,
    reason: str,
) -> dict[str, Any]:
    return {
        "capability_id": capability_id,
        "contract_mode": contract_mode,
        "contract_ready": contract_ready,
        "runtime_ready": runtime_ready,
        "doctor_status": doctor_status,
        "reason": reason,
    }


def _doctor_projection(
    doctor: dict[str, Any],
    path: Path,
    observed_at: datetime,
    freshness_hours: float,
) -> dict[str, Any]:
    if freshness_hours <= 0:
        raise ContractError("doctor freshness hours must be positive")
    doctor_time = _as_datetime(doctor["observed_at"], "doctor observed_at")
    age_hours = (observed_at - doctor_time).total_seconds() / 3600
    fresh = -5 / 60 <= age_hours <= freshness_hours
    return {
        "artifact": artifact_reference(path, "capability doctor"),
        "status": "fresh" if fresh else "stale",
        "age_hours": round(max(0.0, age_hours), 6),
        "fresh": fresh,
    }


def _test_receipt_projection(
    path: Path,
    *,
    expected_repository: Path,
    observed_at: datetime,
    freshness_hours: float,
) -> dict[str, Any]:
    if freshness_hours <= 0:
        raise ContractError("test freshness hours must be positive")
    receipt = read_json_object(path, "managed test receipt")
    if receipt.get("schema_version") != WINDOWS_TEST_RECEIPT_SCHEMA:
        raise ContractError("unsupported managed test receipt schema")
    if receipt.get("receipt_sha256") != payload_sha256(receipt, "receipt_sha256"):
        raise ContractError("managed test receipt integrity check failed")
    details = _object(receipt.get("details"), "managed test receipt details")
    completed_at = _as_datetime(receipt.get("completed_at"), "test completed_at")
    age_hours = (observed_at - completed_at).total_seconds() / 3600
    fresh = -5 / 60 <= age_hours <= freshness_hours
    repository_matches = _same_path(details.get("cwd"), expected_repository)
    passed = (
        receipt.get("status") == "passed"
        and details.get("adapter") in {"pytest", "unittest"}
        and details.get("returncode") == 0
        and details.get("timed_out") is False
        and _object(receipt.get("cleanup"), "managed test cleanup").get("status")
        == "cleaned"
    )
    if passed and fresh and repository_matches:
        status = "fresh_pass"
    elif passed and not fresh:
        status = "stale_pass"
    elif passed and not repository_matches:
        status = "repository_mismatch"
    else:
        status = "failed"
    return {
        "artifact": artifact_reference(path, "managed test receipt"),
        "schema_version": WINDOWS_TEST_RECEIPT_SCHEMA,
        "status": status,
        "passed": passed,
        "fresh": fresh,
        "repository_matches": repository_matches,
        "completed_at": completed_at.isoformat(),
        "age_hours": round(max(0.0, age_hours), 6),
        "purpose": str(receipt.get("purpose") or ""),
        "adapter": str(details.get("adapter") or ""),
    }


def _derive_evidence(
    source_receipts: list[dict[str, Any]], observed_at: datetime
) -> list[dict[str, Any]]:
    loaded: list[tuple[dict[str, Any], dict[str, Any], Path]] = []
    for reference in source_receipts:
        ref = _object(reference, "capability source receipt")
        path = validate_artifact_reference(ref, "capability source receipt")
        payload = read_json_object(path, "capability source receipt")
        loaded.append((ref, payload, path))

    remotion_attestations: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    speech_cleanup_attestations: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    background_processing_attestations: dict[
        str, tuple[dict[str, Any], dict[str, Any]]
    ] = {}
    color_processing_attestations: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    privacy_scopes: dict[str, tuple[str, dict[str, Any]]] = {}
    privacy_scope_by_receipt: dict[str, str] = {}
    for ref, payload, _ in loaded:
        schema = payload.get("schema")
        if schema == REMOTION_SCENE_COMPLETION_RECEIPT_SCHEMA:
            _validate_remotion_completion_attestation(payload)
            receipt_hash = str(
                _object(payload.get("synthetic_e2e"), "Remotion synthetic E2E").get(
                    "latest_run_receipt_sha256"
                )
                or ""
            )
            if receipt_hash in remotion_attestations:
                raise ContractError("duplicate Remotion scope attestation")
            remotion_attestations[receipt_hash] = (ref, payload)
        elif schema == SPEECH_CLEANUP_COMPLETION_RECEIPT_SCHEMA:
            _validate_speech_cleanup_completion_attestation(payload)
            receipt_hash = str(
                _object(
                    _object(
                        payload.get("verification"), "speech cleanup verification"
                    ).get("synthetic_e2e"),
                    "speech cleanup synthetic E2E",
                ).get("execution_receipt_payload_sha256")
                or ""
            )
            if receipt_hash in speech_cleanup_attestations:
                raise ContractError("duplicate speech cleanup scope attestation")
            speech_cleanup_attestations[receipt_hash] = (ref, payload)
        elif schema == BACKGROUND_PROCESSING_COMPLETION_RECEIPT_SCHEMA:
            _validate_background_processing_completion_attestation(payload)
            verification = _object(
                payload.get("verification"), "background processing verification"
            )
            synthetic = _object(
                verification.get("synthetic_e2e"),
                "background processing synthetic E2E",
            )
            for execution in synthetic["executions"]:
                receipt_hash = str(
                    _object(execution, "background processing execution").get(
                        "execution_receipt_payload_sha256"
                    )
                    or ""
                )
                if receipt_hash in background_processing_attestations:
                    raise ContractError(
                        "duplicate background processing scope attestation"
                    )
                background_processing_attestations[receipt_hash] = (ref, payload)
        elif schema == COLOR_PROCESSING_COMPLETION_RECEIPT_SCHEMA:
            _validate_color_processing_completion_attestation(payload)
            synthetic = _object(
                _object(
                    payload.get("verification"), "color processing verification"
                ).get("synthetic_e2e"),
                "color processing synthetic E2E",
            )
            receipt_hash = str(synthetic.get("execution_receipt_payload_sha256") or "")
            if receipt_hash in color_processing_attestations:
                raise ContractError("duplicate color processing scope attestation")
            color_processing_attestations[receipt_hash] = (ref, payload)
        elif schema == PRIVACY_SCAN_REPORT_SCHEMA:
            scope, final_sha256 = _privacy_scope_evidence(payload)
            if final_sha256 in privacy_scopes:
                raise ContractError(
                    "duplicate privacy scope evidence for the same final artifact"
                )
            privacy_scopes[final_sha256] = (scope, ref)
            privacy_scope_by_receipt[str(ref["sha256"])] = scope

    rows: list[dict[str, Any]] = []
    seen_primary: set[str] = set()
    consumed_remotion_attestations: set[str] = set()
    consumed_speech_cleanup_attestations: set[str] = set()
    consumed_background_processing_attestations: set[str] = set()
    consumed_color_processing_attestations: set[str] = set()
    for ref, payload, _ in loaded:
        schema = str(payload.get("schema") or "")
        if schema in {
            REMOTION_SCENE_COMPLETION_RECEIPT_SCHEMA,
            SPEECH_CLEANUP_COMPLETION_RECEIPT_SCHEMA,
            BACKGROUND_PROCESSING_COMPLETION_RECEIPT_SCHEMA,
            COLOR_PROCESSING_COMPLETION_RECEIPT_SCHEMA,
        }:
            continue
        if ref["sha256"] in seen_primary:
            raise ContractError("duplicate capability execution receipt")
        seen_primary.add(str(ref["sha256"]))
        supporting: list[dict[str, Any]] = []
        if schema == CONTENT_SYNTHETIC_RUN_RECEIPT_SCHEMA:
            validate_content_synthetic_run_receipt(payload)
            capabilities = ["canonical_candidate_pipeline", "licensed_audio_mix"]
            scope = "synthetic_fixture"
        elif schema == REMOTION_RUN_RECEIPT_SCHEMA:
            validate_remotion_run_receipt(payload)
            capabilities = ["remotion_scene_media_render"]
            attestation = remotion_attestations.get(
                str(payload.get("receipt_sha256") or "")
            )
            if attestation is None:
                scope = "unclassified_local_candidate"
            else:
                supporting = [attestation[0]]
                scope = "synthetic_fixture"
                consumed_remotion_attestations.add(
                    str(payload.get("receipt_sha256") or "")
                )
        elif schema == SEMANTIC_MOTION_RUN_RECEIPT_SCHEMA:
            validate_semantic_motion_run_receipt(payload)
            capabilities = ["hyperframes_semantic_motion"]
            candidate = _object(
                payload.get("candidate_video"), "semantic motion candidate"
            )
            privacy_scope = privacy_scopes.get(str(candidate.get("sha256") or ""))
            if privacy_scope is None:
                scope = "unclassified_local_candidate"
            else:
                scope, privacy_ref = privacy_scope
                supporting = [privacy_ref]
        elif schema == SPEECH_CLEANUP_RECEIPT_SCHEMA:
            validate_speech_cleanup_receipt(payload)
            capabilities = ["speech_noise_cleanup"]
            receipt_hash = str(payload.get("receipt_sha256") or "")
            attestation = speech_cleanup_attestations.get(receipt_hash)
            if attestation is None:
                scope = "unclassified_local_candidate"
            else:
                supporting = [attestation[0]]
                scope = "synthetic_fixture"
                consumed_speech_cleanup_attestations.add(receipt_hash)
        elif schema == BACKGROUND_PROCESSING_RECEIPT_SCHEMA:
            validate_background_processing_receipt(payload)
            capabilities = ["person_background_processing"]
            receipt_hash = str(payload.get("receipt_sha256") or "")
            attestation = background_processing_attestations.get(receipt_hash)
            if attestation is None:
                scope = "unclassified_local_candidate"
            else:
                supporting = [attestation[0]]
                scope = "synthetic_fixture"
                consumed_background_processing_attestations.add(receipt_hash)
        elif schema == COLOR_PROCESSING_RECEIPT_SCHEMA:
            validate_color_processing_receipt(payload)
            capabilities = ["basic_color_processing"]
            receipt_hash = str(payload.get("receipt_sha256") or "")
            attestation = color_processing_attestations.get(receipt_hash)
            if attestation is None:
                scope = "unclassified_local_candidate"
            else:
                supporting = [attestation[0]]
                scope = "synthetic_fixture"
                consumed_color_processing_attestations.add(receipt_hash)
        elif schema == PRIVACY_SCAN_REPORT_SCHEMA:
            capabilities = ["privacy_candidate_scan"]
            scope = privacy_scope_by_receipt[str(ref["sha256"])]
        else:
            raise ContractError(
                f"unsupported capability execution receipt schema: {schema}"
            )
        evidence_id = (
            "evidence-"
            + canonical_json_sha256({"schema": schema, "sha256": ref["sha256"]})[:16]
        )
        rows.append(
            {
                "evidence_id": evidence_id,
                "receipt": ref,
                "receipt_schema": schema,
                "supporting_receipts": supporting,
                "capability_ids": capabilities,
                "validation_status": "valid",
                "evidence_scope": scope,
                "revalidated_at": observed_at.isoformat(),
                "candidate_only": True,
                "real_media_proof": scope == "authorized_real_media",
                "provider_called": False,
                "automatic_fallback_used": False,
                "upload_performed": False,
                "publish_performed": False,
            }
        )
    rows.sort(key=lambda row: row["evidence_id"])
    orphaned_remotion = set(remotion_attestations) - consumed_remotion_attestations
    if orphaned_remotion:
        raise ContractError(
            "Remotion scope attestation is not bound to a supplied run receipt"
        )
    orphaned_speech_cleanup = (
        set(speech_cleanup_attestations) - consumed_speech_cleanup_attestations
    )
    if orphaned_speech_cleanup:
        raise ContractError(
            "speech cleanup scope attestation is not bound to a supplied run receipt"
        )
    orphaned_background_processing = (
        set(background_processing_attestations)
        - consumed_background_processing_attestations
    )
    if orphaned_background_processing:
        raise ContractError(
            "background processing scope attestation is not bound to a supplied run receipt"
        )
    orphaned_color_processing = (
        set(color_processing_attestations) - consumed_color_processing_attestations
    )
    if orphaned_color_processing:
        raise ContractError(
            "color processing scope attestation is not bound to a supplied run receipt"
        )
    return rows


def _privacy_scope_evidence(report: dict[str, Any]) -> tuple[str, str]:
    validate_privacy_scan_report(report)
    plan_ref = _object(report.get("source_plan"), "privacy plan reference")
    plan_path = validate_artifact_reference(plan_ref, "privacy review plan")
    plan = read_json_object(plan_path, "privacy review plan")
    subject_mode = str(
        _object(plan.get("subject_policy"), "privacy subject policy").get("mode") or ""
    )
    if subject_mode == "synthetic":
        scope = "synthetic_fixture"
    elif (
        subject_mode == "authorized_identifiable"
        and plan.get("authorization_evidence") is not None
    ):
        scope = "authorized_real_media"
    else:
        scope = "unclassified_local_candidate"
    final_artifact = _object(
        report.get("scanned_final_artifact"), "privacy scanned final artifact"
    )
    final_sha256 = str(final_artifact.get("sha256") or "")
    if len(final_sha256) != 64:
        raise ContractError("privacy scanned final artifact hash is invalid")
    return scope, final_sha256


def _derive_capabilities(
    doctor: dict[str, Any],
    doctor_projection: dict[str, Any],
    test_projection: dict[str, Any] | None,
    evidence: list[dict[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    test_ready = bool(test_projection and test_projection.get("status") == "fresh_pass")
    doctor_fresh = doctor_projection.get("fresh") is True
    capability_ids = tuple(
        capability_id
        for capability_id in CAPABILITY_IDS
        if capability_id in doctor["capabilities"]
    )
    for capability_id in capability_ids:
        probe = _object(doctor["capabilities"][capability_id], "capability doctor row")
        matching = [
            row for row in evidence if capability_id in row.get("capability_ids", [])
        ]
        evidence_ids = [str(row["evidence_id"]) for row in matching]
        mode = str(probe["contract_mode"])
        if mode == "handoff":
            status = "handoff_only"
            reason = "A versioned handoff exists, but no editor-side execution proof is accepted."
            scope = "none"
            fresh_e2e = False
        elif mode == "blocked":
            status = "blocked"
            reason = str(probe["reason"])
            scope = "none"
            fresh_e2e = False
        elif probe.get("contract_ready") is not True:
            status = "blocked"
            reason = "The capability contract is unavailable."
            scope = "none"
            fresh_e2e = False
        elif not doctor_fresh:
            status = "blocked"
            reason = "The capability doctor artifact is stale."
            scope = "none"
            fresh_e2e = False
        elif probe.get("runtime_ready") is not True:
            status = "blocked"
            reason = f"Runtime doctor is not ready: {probe['doctor_status']}."
            scope = "none"
            fresh_e2e = False
        elif not test_ready:
            status = "blocked"
            reason = (
                "No fresh passing managed test receipt is bound to this repository."
            )
            scope = "none"
            fresh_e2e = False
        else:
            real = [
                row
                for row in matching
                if row["evidence_scope"] == "authorized_real_media"
            ]
            synthetic = [
                row for row in matching if row["evidence_scope"] == "synthetic_fixture"
            ]
            if real:
                status = "verified_real_media"
                reason = "Doctor, managed tests, and explicit authorized real-media execution evidence all pass."
                scope = "authorized_real_media"
                fresh_e2e = True
            elif synthetic:
                status = "verified_synthetic"
                reason = "Doctor, managed tests, and revalidated synthetic execution evidence all pass."
                scope = "synthetic_fixture"
                fresh_e2e = True
            elif matching:
                status = "blocked"
                reason = "Execution receipt is valid, but its media scope is not explicitly classified."
                scope = "unclassified_local_candidate"
                fresh_e2e = False
            else:
                status = "blocked"
                reason = "No accepted execution receipt was supplied."
                scope = "none"
                fresh_e2e = False
        result[capability_id] = {
            "capability_id": capability_id,
            "status": status,
            "contract_mode": mode,
            "contract_ready": bool(probe["contract_ready"]),
            "runtime_ready": bool(probe["runtime_ready"]),
            "doctor_status": str(probe["doctor_status"]),
            "doctor_fresh": doctor_fresh,
            "managed_tests_fresh_pass": test_ready,
            "execution_evidence_present": bool(matching),
            "execution_scope": scope,
            "fresh_e2e_pass": fresh_e2e,
            "real_media_proof": status == "verified_real_media",
            "candidate_only": status in {"verified_real_media", "verified_synthetic"},
            "evidence_ids": evidence_ids,
            "reason": reason,
            "current_online": False,
            "provider_called": False,
            "automatic_fallback_used": False,
            "publish_authorized": False,
        }
    return result


def _status_summary(capabilities: dict[str, Any]) -> dict[str, int]:
    statuses = ("verified_real_media", "verified_synthetic", "handoff_only", "blocked")
    return {
        status: sum(row["status"] == status for row in capabilities.values())
        for status in statuses
    }


def _validate_remotion_completion_attestation(payload: dict[str, Any]) -> None:
    if payload.get("status") != "complete":
        raise ContractError("Remotion scope attestation is not complete")
    synthetic = _object(payload.get("synthetic_e2e"), "Remotion synthetic E2E")
    receipt_hash = str(synthetic.get("latest_run_receipt_sha256") or "")
    if len(receipt_hash) != 64 or any(
        char not in "0123456789abcdef" for char in receipt_hash
    ):
        raise ContractError("Remotion scope attestation receipt hash is invalid")
    boundary = _object(payload.get("boundary_check"), "Remotion completion boundary")
    required_false = (
        "real_person_or_customer_media_used",
        "online_provider_called",
        "remote_asset_fetched",
        "cookie_or_secret_read",
        "media_uploaded",
        "video_published",
        "second_ffmpeg_or_audio_pipeline_created",
        "vkp_modified",
    )
    if boundary.get("candidate_only") is not True or any(
        boundary.get(field) is not False for field in required_false
    ):
        raise ContractError("Remotion scope attestation authority boundary drifted")
    _as_datetime(payload.get("completed_at"), "Remotion completion timestamp")


def _validate_speech_cleanup_completion_attestation(
    payload: dict[str, Any],
) -> None:
    if payload.get("schema") != SPEECH_CLEANUP_COMPLETION_RECEIPT_SCHEMA:
        raise ContractError("unsupported speech cleanup scope attestation schema")
    if payload.get("status") != "complete":
        raise ContractError("speech cleanup scope attestation is not complete")
    verification = _object(payload.get("verification"), "speech cleanup verification")
    synthetic = _object(
        verification.get("synthetic_e2e"), "speech cleanup synthetic E2E"
    )
    receipt_hash = str(synthetic.get("execution_receipt_payload_sha256") or "")
    if len(receipt_hash) != 64 or any(
        char not in "0123456789abcdef" for char in receipt_hash
    ):
        raise ContractError("speech cleanup scope attestation receipt hash is invalid")
    if (
        synthetic.get("scope") != "synthetic_noisy_video_only"
        or synthetic.get("double_run_byte_identical") is not True
        or synthetic.get("audition_status") != "needs_audition"
        or synthetic.get("publish_authorized") is not False
    ):
        raise ContractError("speech cleanup synthetic scope evidence drifted")
    boundary = _object(payload.get("boundary_check"), "speech cleanup boundary")
    required_false = (
        "external_provider_called",
        "real_media_used",
        "media_uploaded",
        "source_media_modified",
        "automatic_profile_selection",
        "voice_quality_claimed",
        "release_eligible",
        "video_published",
        "automatic_publish_enabled",
        "second_ffmpeg_orchestration_added",
        "second_audio_engine_added",
        "second_state_machine_added",
    )
    if boundary.get("human_audition_required") is not True or any(
        boundary.get(field) is not False for field in required_false
    ):
        raise ContractError(
            "speech cleanup scope attestation authority boundary drifted"
        )
    _as_datetime(payload.get("completed_at"), "speech cleanup completion timestamp")


def _validate_background_processing_completion_attestation(
    payload: dict[str, Any],
) -> None:
    if payload.get("schema") != BACKGROUND_PROCESSING_COMPLETION_RECEIPT_SCHEMA:
        raise ContractError(
            "unsupported background processing scope attestation schema"
        )
    if payload.get("status") != "complete":
        raise ContractError("background processing scope attestation is not complete")
    verification = _object(
        payload.get("verification"), "background processing verification"
    )
    synthetic = _object(
        verification.get("synthetic_e2e"), "background processing synthetic E2E"
    )
    executions = synthetic.get("executions")
    if not isinstance(executions, list) or not executions:
        raise ContractError("background processing scope requires execution receipts")
    modes: set[str] = set()
    hashes: set[str] = set()
    for execution in executions:
        row = _object(execution, "background processing execution")
        mode = str(row.get("mode") or "")
        receipt_hash = str(row.get("execution_receipt_payload_sha256") or "")
        if mode not in {"blur", "replace_color", "replace_image", "transparent"}:
            raise ContractError("background processing scope mode is invalid")
        if len(receipt_hash) != 64 or any(
            char not in "0123456789abcdef" for char in receipt_hash
        ):
            raise ContractError(
                "background processing scope attestation receipt hash is invalid"
            )
        if mode in modes or receipt_hash in hashes:
            raise ContractError("background processing scope executions are duplicated")
        modes.add(mode)
        hashes.add(receipt_hash)
    if (
        synthetic.get("scope") != "synthetic_illustration_person_only"
        or synthetic.get("visual_review_status") != "needs_visual_review"
        or synthetic.get("publish_authorized") is not False
        or synthetic.get("real_person_quality_claimed") is not False
    ):
        raise ContractError("background processing synthetic scope evidence drifted")
    boundary = _object(payload.get("boundary_check"), "background processing boundary")
    required_false = (
        "real_person_or_customer_media_used",
        "online_provider_called",
        "media_uploaded",
        "source_media_modified",
        "automatic_provider_fallback_used",
        "visual_quality_claimed",
        "real_person_gold_claimed",
        "release_eligible",
        "video_published",
        "automatic_publish_enabled",
        "second_ffmpeg_orchestration_added",
        "second_state_machine_added",
    )
    if (
        boundary.get("external_local_provider_used") is not True
        or boundary.get("human_visual_review_required") is not True
        or any(boundary.get(field) is not False for field in required_false)
    ):
        raise ContractError(
            "background processing scope attestation authority boundary drifted"
        )
    _as_datetime(
        payload.get("completed_at"), "background processing completion timestamp"
    )


def _validate_color_processing_completion_attestation(
    payload: dict[str, Any],
) -> None:
    if payload.get("schema") != COLOR_PROCESSING_COMPLETION_RECEIPT_SCHEMA:
        raise ContractError("unsupported color processing scope attestation schema")
    if payload.get("status") != "complete":
        raise ContractError("color processing scope attestation is not complete")
    verification = _object(payload.get("verification"), "color processing verification")
    synthetic = _object(
        verification.get("synthetic_e2e"), "color processing synthetic E2E"
    )
    receipt_hash = str(synthetic.get("execution_receipt_payload_sha256") or "")
    if len(receipt_hash) != 64 or any(
        char not in "0123456789abcdef" for char in receipt_hash
    ):
        raise ContractError(
            "color processing scope attestation receipt hash is invalid"
        )
    if (
        synthetic.get("scope") != "synthetic_color_fixture_only"
        or synthetic.get("double_run_byte_identical") is not True
        or synthetic.get("audio_packet_sha256_matches") is not True
        or synthetic.get("visual_review_status") != "needs_visual_review"
        or synthetic.get("publish_authorized") is not False
    ):
        raise ContractError("color processing synthetic scope evidence drifted")
    boundary = _object(payload.get("boundary_check"), "color processing boundary")
    required_false = (
        "real_person_or_customer_media_used",
        "online_provider_called",
        "media_uploaded",
        "source_media_modified",
        "automatic_white_balance_used",
        "creative_grade_quality_claimed",
        "skin_tone_quality_claimed",
        "release_eligible",
        "video_published",
        "automatic_publish_enabled",
        "second_ffmpeg_orchestration_added",
        "second_video_encoder_added",
        "second_state_machine_added",
    )
    if boundary.get("human_visual_review_required") is not True or any(
        boundary.get(field) is not False for field in required_false
    ):
        raise ContractError("color processing scope attestation boundary drifted")
    _as_datetime(payload.get("completed_at"), "color processing completion timestamp")


def _color_processing_probe() -> dict[str, Any]:
    try:
        return color_processing_doctor()
    except (ContractError, OSError, subprocess.SubprocessError) as exc:
        return {
            "schema": "video_creation_pipeline.color_processing_doctor.v1",
            "status": "blocked_runtime_unavailable",
            "checks": {
                "ffmpeg_available": False,
                "ffprobe_available": False,
                "eq_filter_available": False,
                "signalstats_filter_available": False,
                "metadata_filter_available": False,
                "colortemperature_filter_available": False,
            },
            "error": str(exc),
        }


def _speech_cleanup_probe() -> dict[str, Any]:
    try:
        return speech_cleanup_doctor()
    except (ContractError, OSError, subprocess.SubprocessError) as exc:
        return {
            "schema": "video_creation_pipeline.speech_cleanup_doctor.v1",
            "status": "blocked_runtime_unavailable",
            "filters": {
                "afftdn": False,
                "bandreject": False,
                "highpass": False,
            },
            "error": str(exc),
        }


def _background_processing_probe(
    *,
    provider_source_root: str | Path | None,
    model_path: str | Path | None,
    runtime_dependencies_path: str | Path | None,
    device: str,
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "schema": "video_creation_pipeline.background_processing_doctor.v1",
        "status": "not_supplied",
        "execution_performed": False,
    }
    supplied = (
        provider_source_root,
        model_path,
        runtime_dependencies_path,
    )
    if all(value is None for value in supplied):
        return report
    if any(value is None for value in supplied):
        report["status"] = "blocked_incomplete_configuration"
        return report
    try:
        return background_processing_doctor(
            provider_source_root,
            model_path,
            runtime_dependencies_path,
            device=device,
        )
    except (ContractError, OSError, subprocess.SubprocessError, ValueError) as exc:
        report.update({"status": "blocked_runtime_unavailable", "error": str(exc)})
        return report


def _hyperframes_doctor(
    *,
    source_root: str | Path | None,
    browser_path: str | Path | None,
    ffmpeg_path: str | Path | None,
    ffprobe_path: str | Path | None,
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "schema": "video_creation_pipeline.hyperframes_runtime_doctor.v1",
        "status": "not_supplied",
        "render_ready": False,
        "execution_performed": False,
    }
    if source_root is None:
        return report
    try:
        source = Path(source_root).expanduser().resolve()
        package = read_json_object(
            source / "packages" / "cli" / "package.json",
            "HyperFrames CLI package",
        )
        cli = source / HYPERFRAMES_CLI_RELATIVE
        if package.get("version") != HYPERFRAMES_VERSION or not cli.is_file():
            raise ContractError("HyperFrames source package drifted")
        if _git_head(source) != HYPERFRAMES_SOURCE_COMMIT:
            raise ContractError("HyperFrames source commit drifted")
        browser = Path(browser_path).expanduser().resolve() if browser_path else None
        ffmpeg = _resolve_executable(ffmpeg_path, "ffmpeg")
        ffprobe = _resolve_executable(ffprobe_path, "ffprobe")
        node = shutil.which("node")
        if browser is None or not browser.is_file():
            raise ContractError("HyperFrames browser was not supplied")
        if not node:
            raise ContractError("Node.js was not found")
        report.update(
            {
                "status": "ready_for_local_candidate_render",
                "render_ready": True,
                "source_commit": HYPERFRAMES_SOURCE_COMMIT,
                "source_version": HYPERFRAMES_VERSION,
                "cli": str(cli),
                "browser": str(browser),
                "node": str(Path(node).resolve()),
                "ffmpeg": str(ffmpeg),
                "ffprobe": str(ffprobe),
            }
        )
    except (ContractError, OSError, subprocess.SubprocessError) as exc:
        report.update({"status": "blocked", "error": str(exc)})
    return report


def _repository_provenance(root: Path) -> dict[str, Any]:
    head = _git_head(root, required=False)
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=root,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=20,
        check=False,
    )
    return {
        "path": str(root),
        "git_head": head,
        "worktree_dirty": status.returncode != 0 or bool(status.stdout.strip()),
        "git_identity_is_semantic_gate": False,
    }


def _git_head(root: Path, *, required: bool = True) -> str | None:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=20,
        check=False,
    )
    head = result.stdout.strip().lower()
    if result.returncode == 0 and len(head) == 40:
        return head
    if required:
        raise ContractError(f"Git HEAD unavailable for {root}")
    return None


def _resolve_executable(value: str | Path | None, name: str) -> Path:
    if value is None:
        resolved = shutil.which(name)
        if not resolved:
            raise ContractError(f"{name} executable was not found")
        return Path(resolved).resolve()
    path = Path(value).expanduser().resolve()
    if not path.is_file():
        raise ContractError(f"{name} executable was not found: {path}")
    return path


def _authority_boundary() -> dict[str, bool]:
    return {
        "current_online": False,
        "provider_called": False,
        "automatic_fallback_used": False,
        "upload_performed": False,
        "publish_authorized": False,
        "truth_is_release_authorization": False,
    }


def _validate_schema(payload: dict[str, Any], filename: str, label: str) -> None:
    schema = read_json_object(
        Path(__file__).with_name("schemas") / filename, f"{label} schema"
    )
    try:
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema, format_checker=FormatChecker()).validate(payload)
    except (SchemaError, ValidationError) as exc:
        raise ContractError(f"{label} schema validation failed: {exc.message}") from exc


def _as_datetime(value: datetime | str | Any, label: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ContractError(f"{label} is invalid") from exc
    if parsed.tzinfo is None:
        raise ContractError(f"{label} must include a timezone")
    return parsed.astimezone(UTC).replace(microsecond=0)


def _same_path(left: Any, right: Path) -> bool:
    try:
        return (
            str(Path(str(left)).expanduser().resolve()).casefold()
            == str(right.resolve()).casefold()
        )
    except (OSError, ValueError, TypeError):
        return False


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError(f"{label} must be an object")
    return value
