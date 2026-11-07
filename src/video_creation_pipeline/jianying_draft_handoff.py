from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .contract_io import (
    artifact_reference,
    atomic_write_json,
    canonical_json_sha256,
    file_sha256,
    payload_sha256,
    read_json_object,
    validate_artifact_reference,
)
from .errors import ContractError
from .nle_bridge import load_confirmed_nle_segments
from .spoken_rough_cut import validate_spoken_edit_plan

JIANYING_DRAFT_JOB_SCHEMA = "video_creation_pipeline.jianying_draft_export_job.v1"
JIANYING_DRAFT_RECEIPT_SCHEMA = (
    "video_creation_pipeline.jianying_draft_export_receipt.v1"
)
JIANYING_SPOKEN_DRAFT_JOB_SCHEMA = (
    "video_creation_pipeline.jianying_spoken_draft_export_job.v1"
)
JIANYING_PROJECT_FOLDER_RECEIPT_SCHEMA = (
    "video_creation_pipeline.jianying_project_folder_receipt.v1"
)
JIANYING_DESKTOP_VALIDATION_SCHEMA = (
    "video_creation_pipeline.jianying_desktop_validation_receipt.v1"
)
VECTCUT_REPOSITORY = "Jiang59991/VectCutAPI"
VECTCUT_COMMIT = "f0b9b1ef89a73cd50af157e04a8088d68e5dd5f6"
VECTCUT_PROFILE = "jianying_pro_10"
VECTCUT_FILES = {
    "license": "LICENSE",
    "package_metadata": "pyproject.toml",
    "serializer": "pyJianYingDraft/script_file.py",
    "profiles": "draft_profiles.py",
    "video_material": "pyJianYingDraft/local_materials.py",
    "video_segment": "pyJianYingDraft/video_segment.py",
}
VECTCUT_PROJECT_FILES = {
    "profile_writer": "draft_profiles.py",
    "template_content": "template_jianying_10_2/draft_content.json",
    "template_project": "template_jianying_10_2/Timelines/project.json",
    "template_layout": "template_jianying_10_2/timeline_layout.json",
}


def vectcut_doctor(source_root: str | Path | None = None) -> dict[str, Any]:
    report: dict[str, Any] = {
        "schema": "video_creation_pipeline.vectcut_doctor.v1",
        "repository": VECTCUT_REPOSITORY,
        "required_commit": VECTCUT_COMMIT,
        "profile": VECTCUT_PROFILE,
        "execution_authority": "explicit_local_candidate_export_only",
        "download_allowed": False,
        "upload_allowed": False,
        "publish_allowed": False,
    }
    if source_root is None:
        report.update(
            {
                "status": "blocked_missing_source",
                "source_ready": False,
                "license_observation": "not_checked",
            }
        )
        return report
    try:
        source = _vectcut_source_manifest(source_root)
    except ContractError as exc:
        report.update(
            {
                "status": "blocked_source_drift",
                "source_ready": False,
                "error": str(exc),
                "license_observation": "not_trusted",
            }
        )
        return report
    report.update(
        {
            "status": "ready_for_candidate_export",
            "source_ready": True,
            "source": source,
            "license_observation": (
                "root Apache-2.0 license is authoritative; package classifier says MIT"
            ),
        }
    )
    return report


def build_jianying_draft_job(
    edit_job_path: str | Path,
    selection_path: str | Path,
    media_map_path: str | Path,
    vectcut_source_root: str | Path,
    *,
    width: int = 1080,
    height: int = 1920,
    fps: int = 30,
) -> dict[str, Any]:
    confirmed = load_confirmed_nle_segments(
        edit_job_path, selection_path, media_map_path
    )
    canvas = {
        "width": _positive_int(width, "Jianying draft width"),
        "height": _positive_int(height, "Jianying draft height"),
        "fps": _positive_int(fps, "Jianying draft fps"),
    }
    source = _vectcut_source_manifest(vectcut_source_root)
    idempotency_payload = {
        "source_edit_job_sha256": confirmed["source_edit_job"]["sha256"],
        "source_selection_sha256": confirmed["source_selection"]["sha256"],
        "source_media_map_sha256": confirmed["source_media_map"]["sha256"],
        "segments": confirmed["segments"],
        "canvas": canvas,
        "profile": VECTCUT_PROFILE,
        "vectcut_commit": source["commit"],
    }
    payload: dict[str, Any] = {
        "schema": JIANYING_DRAFT_JOB_SCHEMA,
        "idempotency_key": canonical_json_sha256(idempotency_payload),
        "source_edit_job": confirmed["source_edit_job"],
        "source_selection": confirmed["source_selection"],
        "source_media_map": confirmed["source_media_map"],
        "segments": confirmed["segments"],
        "canvas": canvas,
        "profile": VECTCUT_PROFILE,
        "vectcut_source": source,
        "output_contract": {
            "kind": "jianying_draft_content_json",
            "candidate_only": True,
            "full_project_folder_claimed": False,
            "jianying_runtime_validation_required": True,
        },
        "execution_boundary": {
            "source_media_modified": False,
            "network_download_performed": False,
            "upload_performed": False,
            "nle_import_performed": False,
            "automatic_publish": False,
            "ffmpeg_orchestration_created": False,
            "state_machine_created": False,
            "upstream_downloader_allowed": False,
            "upstream_uploader_allowed": False,
            "upstream_cache_allowed": False,
            "recursive_delete_allowed": False,
        },
        "status": "ready_for_candidate_materialization",
    }
    payload["job_sha256"] = payload_sha256(payload, "job_sha256")
    validate_jianying_draft_job(payload)
    return payload


def build_jianying_spoken_draft_job(
    spoken_edit_plan_path: str | Path,
    vectcut_source_root: str | Path,
    *,
    delivery_request_path: str | Path | None = None,
    width: int = 1080,
    height: int = 1920,
    fps: int = 30,
) -> dict[str, Any]:
    plan_path = Path(spoken_edit_plan_path).expanduser().resolve()
    plan = read_json_object(plan_path, "spoken edit plan")
    validate_spoken_edit_plan(plan)
    from .spoken_delivery import require_spoken_route
    require_spoken_route(plan_path, delivery_request_path, "jianying")
    segments = [dict(row) for row in plan.get("nle_segments") or []]
    if not segments:
        raise ContractError("spoken edit plan contains no Jianying segments")
    canvas = {
        "width": _positive_int(width, "Jianying draft width"),
        "height": _positive_int(height, "Jianying draft height"),
        "fps": _positive_int(fps, "Jianying draft fps"),
    }
    source = _vectcut_source_manifest(vectcut_source_root)
    plan_reference = artifact_reference(plan_path, "spoken edit plan")
    idempotency_payload = {
        "spoken_edit_plan_sha256": plan_reference["sha256"],
        "delivery_request_sha256": artifact_reference(delivery_request_path, "delivery request")["sha256"],
        "segments": segments,
        "canvas": canvas,
        "profile": VECTCUT_PROFILE,
        "vectcut_commit": source["commit"],
    }
    payload: dict[str, Any] = {
        "schema": JIANYING_SPOKEN_DRAFT_JOB_SCHEMA,
        "idempotency_key": canonical_json_sha256(idempotency_payload),
        "source_spoken_edit_plan": plan_reference,
        "source_delivery_request": artifact_reference(delivery_request_path, "delivery request"),
        "segments": segments,
        "canvas": canvas,
        "profile": VECTCUT_PROFILE,
        "vectcut_source": source,
        "output_contract": {
            "kind": "jianying_draft_content_json",
            "candidate_only": True,
            "full_project_folder_claimed": False,
            "jianying_runtime_validation_required": True,
        },
        "execution_boundary": {
            "source_media_modified": False,
            "network_download_performed": False,
            "upload_performed": False,
            "nle_import_performed": False,
            "automatic_publish": False,
            "ffmpeg_orchestration_created": False,
            "state_machine_created": False,
            "upstream_downloader_allowed": False,
            "upstream_uploader_allowed": False,
            "upstream_cache_allowed": False,
            "recursive_delete_allowed": False,
        },
        "status": "ready_for_candidate_materialization",
    }
    payload["job_sha256"] = payload_sha256(payload, "job_sha256")
    validate_jianying_spoken_draft_job(payload)
    return payload


def validate_jianying_spoken_draft_job(payload: dict[str, Any]) -> None:
    if payload.get("schema") != JIANYING_SPOKEN_DRAFT_JOB_SCHEMA:
        raise ContractError("unsupported Jianying spoken draft job schema")
    if payload.get("job_sha256") != payload_sha256(payload, "job_sha256"):
        raise ContractError("Jianying spoken draft job integrity check failed")
    plan_path = validate_artifact_reference(
        _object(payload.get("source_spoken_edit_plan")), "spoken edit plan"
    )
    plan = read_json_object(plan_path, "spoken edit plan")
    validate_spoken_edit_plan(plan)
    from .spoken_delivery import require_spoken_route
    request_path = validate_artifact_reference(_object(payload.get("source_delivery_request")), "delivery request")
    require_spoken_route(plan_path, request_path, "jianying")
    expected_segments = [dict(row) for row in plan.get("nle_segments") or []]
    if payload.get("segments") != expected_segments:
        raise ContractError("Jianying segments differ from the spoken edit plan")
    _validate_common_draft_job(payload, expected_segments)


def validate_jianying_draft_job(payload: dict[str, Any]) -> None:
    if payload.get("schema") != JIANYING_DRAFT_JOB_SCHEMA:
        raise ContractError("unsupported Jianying draft job schema")
    if payload.get("job_sha256") != payload_sha256(payload, "job_sha256"):
        raise ContractError("Jianying draft job integrity check failed")
    edit_job = validate_artifact_reference(
        _object(payload.get("source_edit_job")), "edit job"
    )
    selection = validate_artifact_reference(
        _object(payload.get("source_selection")), "human selection"
    )
    media_map = validate_artifact_reference(
        _object(payload.get("source_media_map")), "NLE media map"
    )
    confirmed = load_confirmed_nle_segments(edit_job, selection, media_map)
    if payload.get("segments") != confirmed["segments"]:
        raise ContractError(
            "Jianying draft segments differ from the confirmed NLE selection"
        )
    _validate_common_draft_job(payload, confirmed["segments"])


def _validate_common_draft_job(
    payload: dict[str, Any], expected_segments: list[dict[str, Any]]
) -> None:
    canvas = _object(payload.get("canvas"))
    normalized_canvas = {
        "width": _positive_int(canvas.get("width"), "Jianying draft width"),
        "height": _positive_int(canvas.get("height"), "Jianying draft height"),
        "fps": _positive_int(canvas.get("fps"), "Jianying draft fps"),
    }
    if canvas != normalized_canvas or payload.get("profile") != VECTCUT_PROFILE:
        raise ContractError("Jianying draft canvas or profile is invalid")
    source = _validate_vectcut_source_manifest(_object(payload.get("vectcut_source")))
    if payload.get("schema") == JIANYING_SPOKEN_DRAFT_JOB_SCHEMA:
        expected_idempotency_payload = {
            "spoken_edit_plan_sha256": payload["source_spoken_edit_plan"]["sha256"],
            "delivery_request_sha256": payload["source_delivery_request"]["sha256"],
            "segments": expected_segments,
            "canvas": normalized_canvas,
            "profile": VECTCUT_PROFILE,
            "vectcut_commit": source["commit"],
        }
    else:
        expected_idempotency_payload = {
            "source_edit_job_sha256": payload["source_edit_job"]["sha256"],
            "source_selection_sha256": payload["source_selection"]["sha256"],
            "source_media_map_sha256": payload["source_media_map"]["sha256"],
            "segments": expected_segments,
            "canvas": normalized_canvas,
            "profile": VECTCUT_PROFILE,
            "vectcut_commit": source["commit"],
        }
    expected_idempotency = canonical_json_sha256(expected_idempotency_payload)
    if payload.get("idempotency_key") != expected_idempotency:
        raise ContractError("Jianying draft idempotency key is invalid")
    if payload.get("output_contract") != {
        "kind": "jianying_draft_content_json",
        "candidate_only": True,
        "full_project_folder_claimed": False,
        "jianying_runtime_validation_required": True,
    }:
        raise ContractError("Jianying draft output contract is invalid")
    expected_boundary = {
        "source_media_modified": False,
        "network_download_performed": False,
        "upload_performed": False,
        "nle_import_performed": False,
        "automatic_publish": False,
        "ffmpeg_orchestration_created": False,
        "state_machine_created": False,
        "upstream_downloader_allowed": False,
        "upstream_uploader_allowed": False,
        "upstream_cache_allowed": False,
        "recursive_delete_allowed": False,
    }
    if payload.get("execution_boundary") != expected_boundary:
        raise ContractError("Jianying draft execution boundary is invalid")
    if payload.get("status") != "ready_for_candidate_materialization":
        raise ContractError("Jianying draft job status is invalid")


def _validate_any_jianying_draft_job(payload: dict[str, Any]) -> None:
    if payload.get("schema") == JIANYING_SPOKEN_DRAFT_JOB_SCHEMA:
        validate_jianying_spoken_draft_job(payload)
    else:
        validate_jianying_draft_job(payload)


def materialize_jianying_draft(
    job_path: str | Path,
    output_dir: str | Path,
    *,
    python_executable: str | Path | None = None,
    timeout_s: float = 120.0,
) -> dict[str, Any]:
    source_job = Path(job_path).expanduser().resolve()
    job = read_json_object(source_job, "Jianying draft job")
    _validate_any_jianying_draft_job(job)
    output = Path(output_dir).expanduser().resolve()
    receipt_path = output / "jianying-draft-receipt.json"
    if receipt_path.is_file():
        receipt = read_json_object(receipt_path, "Jianying draft receipt")
        validate_jianying_draft_receipt(receipt)
        if (
            receipt["source_job"]["sha256"]
            != artifact_reference(source_job, "Jianying draft job")["sha256"]
        ):
            raise ContractError("existing Jianying draft output belongs to another job")
        return receipt
    if output.exists() and any(output.iterdir()):
        raise ContractError(
            "Jianying draft output directory is non-empty and unmanaged"
        )
    output.mkdir(parents=True, exist_ok=True)
    interpreter = _python_executable(python_executable)
    worker = Path(__file__).with_name("_vectcut_worker.py").resolve()
    temp_output = output / ".vectcut-worker-output.json"
    command = [
        str(interpreter),
        str(worker),
        "--job",
        str(source_job),
        "--source-root",
        str(job["vectcut_source"]["root"]),
        "--output",
        str(temp_output),
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=float(timeout_s),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        if temp_output.exists():
            temp_output.unlink()
        raise ContractError(f"VectCut candidate worker failed to start: {exc}") from exc
    if result.returncode != 0:
        if temp_output.exists():
            temp_output.unlink()
        raise ContractError(
            "VectCut candidate worker failed: "
            + (result.stderr.strip() or result.stdout.strip() or "unknown error")
        )
    try:
        draft = json.loads(temp_output.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"VectCut worker output is invalid: {exc}") from exc
    finally:
        if temp_output.exists():
            temp_output.unlink()
    _validate_draft_content(draft, job)
    draft_path = atomic_write_json(draft, output / "draft_content.json")
    receipt: dict[str, Any] = {
        "schema": JIANYING_DRAFT_RECEIPT_SCHEMA,
        "source_job": artifact_reference(source_job, "Jianying draft job"),
        "job_sha256": job["job_sha256"],
        "idempotency_key": job["idempotency_key"],
        "draft_content": artifact_reference(draft_path, "Jianying draft content"),
        "profile": job["profile"],
        "vectcut_commit": job["vectcut_source"]["commit"],
        "segment_count": len(job["segments"]),
        "status": "candidate_draft_payload_generated",
        "candidate_only": True,
        "full_project_folder_generated": False,
        "jianying_runtime_validated": False,
        "next_action": "materialize_project_folder_then_validate_in_jianying",
        "execution_boundary": dict(job["execution_boundary"]),
    }
    receipt["receipt_sha256"] = payload_sha256(receipt, "receipt_sha256")
    validate_jianying_draft_receipt(receipt)
    atomic_write_json(receipt, receipt_path)
    return receipt


def validate_jianying_draft_receipt(payload: dict[str, Any]) -> None:
    if payload.get("schema") != JIANYING_DRAFT_RECEIPT_SCHEMA:
        raise ContractError("unsupported Jianying draft receipt schema")
    if payload.get("receipt_sha256") != payload_sha256(payload, "receipt_sha256"):
        raise ContractError("Jianying draft receipt integrity check failed")
    job_path = validate_artifact_reference(
        _object(payload.get("source_job")), "Jianying draft job"
    )
    job = read_json_object(job_path, "Jianying draft job")
    _validate_any_jianying_draft_job(job)
    draft_path = validate_artifact_reference(
        _object(payload.get("draft_content")), "Jianying draft content"
    )
    draft = read_json_object(draft_path, "Jianying draft content")
    _validate_draft_content(draft, job)
    expected = {
        "job_sha256": job["job_sha256"],
        "idempotency_key": job["idempotency_key"],
        "profile": job["profile"],
        "vectcut_commit": job["vectcut_source"]["commit"],
        "segment_count": len(job["segments"]),
        "status": "candidate_draft_payload_generated",
        "candidate_only": True,
        "full_project_folder_generated": False,
        "jianying_runtime_validated": False,
        "next_action": "materialize_project_folder_then_validate_in_jianying",
        "execution_boundary": job["execution_boundary"],
    }
    for field, value in expected.items():
        if payload.get(field) != value:
            raise ContractError(f"Jianying draft receipt field drifted: {field}")


def materialize_jianying_project_folder(
    draft_receipt_path: str | Path,
    project_dir: str | Path,
    *,
    python_executable: str | Path | None = None,
    timeout_s: float = 120.0,
) -> dict[str, Any]:
    candidate_receipt_path = Path(draft_receipt_path).expanduser().resolve()
    candidate_receipt = read_json_object(
        candidate_receipt_path, "Jianying draft receipt"
    )
    validate_jianying_draft_receipt(candidate_receipt)
    job_path = validate_artifact_reference(
        _object(candidate_receipt["source_job"]), "Jianying draft job"
    )
    job = read_json_object(job_path, "Jianying draft job")
    _validate_any_jianying_draft_job(job)
    source = _vectcut_project_source_manifest(job["vectcut_source"]["root"])
    output = Path(project_dir).expanduser().resolve()
    receipt_path = output.parent / f"{output.name}.project-receipt.json"
    if output.is_dir() and receipt_path.is_file():
        receipt = read_json_object(receipt_path, "Jianying project folder receipt")
        validate_jianying_project_folder_receipt(receipt)
        if receipt.get("source_draft_receipt") != artifact_reference(
            candidate_receipt_path, "Jianying draft receipt"
        ):
            raise ContractError("existing Jianying project belongs to another draft")
        return receipt
    if output.exists():
        raise ContractError("Jianying project output already exists and is unmanaged")
    output.parent.mkdir(parents=True, exist_ok=True)
    interpreter = _python_executable(python_executable)
    worker = Path(__file__).with_name("_vectcut_project_worker.py").resolve()
    draft_content_path = validate_artifact_reference(
        _object(candidate_receipt["draft_content"]), "Jianying draft content"
    )
    with tempfile.TemporaryDirectory(
        prefix=f".{output.name}.staging-", dir=output.parent
    ) as temp:
        staged = Path(temp) / "project"
        command = [
            str(interpreter),
            str(worker),
            "--source-root",
            str(source["root"]),
            "--draft-content",
            str(draft_content_path),
            "--output-dir",
            str(staged),
            "--profile",
            VECTCUT_PROFILE,
        ]
        result = _run_worker(command, timeout_s, "VectCut project folder worker")
        _validate_project_folder(staged, candidate_receipt)
        staged.replace(output)

    files = _directory_manifest(output)
    receipt: dict[str, Any] = {
        "schema": JIANYING_PROJECT_FOLDER_RECEIPT_SCHEMA,
        "source_draft_receipt": artifact_reference(
            candidate_receipt_path, "Jianying draft receipt"
        ),
        "source_job_schema": job["schema"],
        "project_dir": str(output),
        "project_files": files,
        "project_manifest_sha256": canonical_json_sha256(files),
        "profile": VECTCUT_PROFILE,
        "vectcut_project_source": source,
        "segment_count": candidate_receipt["segment_count"],
        "status": "candidate_project_folder_generated",
        "candidate_only": True,
        "full_project_folder_generated": True,
        "jianying_runtime_validated": False,
        "next_action": "open_in_jianying_and_record_desktop_validation",
        "execution_boundary": {
            "source_media_copied": False,
            "source_media_modified": False,
            "network_download_performed": False,
            "upload_performed": False,
            "jianying_opened": False,
            "automatic_publish": False,
            "recursive_delete_performed": False,
        },
        "worker": {
            "returncode": result.returncode,
            "stderr_tail": result.stderr.strip()[-1000:],
        },
    }
    receipt["receipt_sha256"] = payload_sha256(receipt, "receipt_sha256")
    validate_jianying_project_folder_receipt(receipt)
    atomic_write_json(receipt, receipt_path)
    return receipt


def validate_jianying_project_folder_receipt(
    payload: dict[str, Any], *, validate_current_files: bool = True
) -> None:
    if payload.get("schema") != JIANYING_PROJECT_FOLDER_RECEIPT_SCHEMA:
        raise ContractError("unsupported Jianying project folder receipt schema")
    if payload.get("receipt_sha256") != payload_sha256(payload, "receipt_sha256"):
        raise ContractError("Jianying project folder receipt integrity check failed")
    candidate_path = validate_artifact_reference(
        _object(payload.get("source_draft_receipt")), "Jianying draft receipt"
    )
    candidate = read_json_object(candidate_path, "Jianying draft receipt")
    validate_jianying_draft_receipt(candidate)
    project = Path(str(payload.get("project_dir") or "")).expanduser().resolve()
    if not project.is_dir():
        raise ContractError("Jianying project folder does not exist")
    files = payload.get("project_files")
    if not isinstance(files, list) or not files:
        raise ContractError("Jianying project folder manifest is empty")
    if payload.get("project_manifest_sha256") != canonical_json_sha256(files):
        raise ContractError("Jianying project folder manifest hash is invalid")
    if validate_current_files and files != _directory_manifest(project):
        raise ContractError("Jianying project folder changed after generation")
    source = _object(payload.get("vectcut_project_source"))
    if source != _vectcut_project_source_manifest(str(source.get("root") or "")):
        raise ContractError("VectCut project source manifest drifted")
    expected = {
        "source_job_schema": read_json_object(
            candidate["source_job"]["path"], "Jianying draft job"
        )["schema"],
        "profile": VECTCUT_PROFILE,
        "segment_count": candidate["segment_count"],
        "status": "candidate_project_folder_generated",
        "candidate_only": True,
        "full_project_folder_generated": True,
        "jianying_runtime_validated": False,
        "next_action": "open_in_jianying_and_record_desktop_validation",
    }
    for field, value in expected.items():
        if payload.get(field) != value:
            raise ContractError(f"Jianying project folder field drifted: {field}")
    if _object(payload.get("execution_boundary")) != {
        "source_media_copied": False,
        "source_media_modified": False,
        "network_download_performed": False,
        "upload_performed": False,
        "jianying_opened": False,
        "automatic_publish": False,
        "recursive_delete_performed": False,
    }:
        raise ContractError("Jianying project folder execution boundary is invalid")


def record_jianying_desktop_validation(
    project_receipt_path: str | Path,
    output_path: str | Path,
    *,
    jianying_version: str,
    validated_by: str,
    observed_segment_count: int,
    project_opened: bool,
    editable_timeline_confirmed: bool,
    original_audio_present: bool,
    audio_sync_confirmed: bool,
    project_saved_by_jianying: bool,
    evidence_paths: list[str | Path] | None = None,
) -> dict[str, Any]:
    source_receipt_path = Path(project_receipt_path).expanduser().resolve()
    project_receipt = read_json_object(
        source_receipt_path, "Jianying project folder receipt"
    )
    validate_jianying_project_folder_receipt(
        project_receipt, validate_current_files=False
    )
    actor = str(validated_by).strip()
    version = str(jianying_version).strip()
    expected_segments = int(project_receipt["segment_count"])
    checks = {
        "project_opened": bool(project_opened),
        "editable_timeline_confirmed": bool(editable_timeline_confirmed),
        "original_audio_present": bool(original_audio_present),
        "audio_sync_confirmed": bool(audio_sync_confirmed),
        "project_saved_by_jianying": bool(project_saved_by_jianying),
        "segment_count_matches": int(observed_segment_count) == expected_segments,
    }
    if not actor or not version or not all(checks.values()):
        failed = sorted(field for field, passed in checks.items() if not passed)
        raise ContractError(
            "Jianying desktop validation is incomplete: " + ", ".join(failed)
        )
    project_dir = Path(project_receipt["project_dir"]).resolve()
    post_save_files = _directory_manifest(project_dir)
    evidence = [
        artifact_reference(path, "Jianying desktop validation evidence")
        for path in evidence_paths or []
    ]
    payload: dict[str, Any] = {
        "schema": JIANYING_DESKTOP_VALIDATION_SCHEMA,
        "source_project_receipt": artifact_reference(
            source_receipt_path, "Jianying project folder receipt"
        ),
        "validated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "validated_by": actor,
        "jianying_version": version,
        "observed_segment_count": int(observed_segment_count),
        "checks": checks,
        "post_save_project_files": post_save_files,
        "post_save_manifest_sha256": canonical_json_sha256(post_save_files),
        "evidence": evidence,
        "status": "desktop_validated_editable_candidate",
        "candidate_only": True,
        "release_approval_claimed": False,
        "source_media_modified": False,
        "effects_added": False,
        "automatic_publish": False,
    }
    payload["receipt_sha256"] = payload_sha256(payload, "receipt_sha256")
    validate_jianying_desktop_validation_receipt(payload)
    atomic_write_json(payload, output_path)
    return payload


def validate_jianying_desktop_validation_receipt(payload: dict[str, Any]) -> None:
    if payload.get("schema") != JIANYING_DESKTOP_VALIDATION_SCHEMA:
        raise ContractError("unsupported Jianying desktop validation schema")
    if payload.get("receipt_sha256") != payload_sha256(payload, "receipt_sha256"):
        raise ContractError("Jianying desktop validation integrity check failed")
    source_path = validate_artifact_reference(
        _object(payload.get("source_project_receipt")),
        "Jianying project folder receipt",
    )
    project_receipt = read_json_object(source_path, "Jianying project folder receipt")
    validate_jianying_project_folder_receipt(
        project_receipt, validate_current_files=False
    )
    checks = _object(payload.get("checks"))
    required_checks = {
        "project_opened",
        "editable_timeline_confirmed",
        "original_audio_present",
        "audio_sync_confirmed",
        "project_saved_by_jianying",
        "segment_count_matches",
    }
    if set(checks) != required_checks or not all(checks.values()):
        raise ContractError("Jianying desktop validation checks are incomplete")
    if payload.get("observed_segment_count") != project_receipt["segment_count"]:
        raise ContractError("Jianying desktop segment count drifted")
    if (
        not str(payload.get("validated_by") or "").strip()
        or not str(payload.get("jianying_version") or "").strip()
    ):
        raise ContractError("Jianying desktop validator and version are required")
    try:
        datetime.fromisoformat(str(payload.get("validated_at") or ""))
    except ValueError as exc:
        raise ContractError("Jianying desktop validation time is invalid") from exc
    post_save_files = payload.get("post_save_project_files")
    if not isinstance(post_save_files, list) or not post_save_files:
        raise ContractError("Jianying post-save project manifest is empty")
    if payload.get("post_save_manifest_sha256") != canonical_json_sha256(
        post_save_files
    ):
        raise ContractError("Jianying post-save project manifest hash is invalid")
    project_dir = Path(project_receipt["project_dir"]).resolve()
    if post_save_files != _directory_manifest(project_dir):
        raise ContractError("Jianying project changed after desktop validation")
    for evidence in payload.get("evidence") or []:
        validate_artifact_reference(
            _object(evidence), "Jianying desktop validation evidence"
        )
    expected = {
        "status": "desktop_validated_editable_candidate",
        "candidate_only": True,
        "release_approval_claimed": False,
        "source_media_modified": False,
        "effects_added": False,
        "automatic_publish": False,
    }
    for field, value in expected.items():
        if payload.get(field) != value:
            raise ContractError(f"Jianying desktop validation field drifted: {field}")


def write_jianying_draft_job(payload: dict[str, Any], output_path: str | Path) -> Path:
    _validate_any_jianying_draft_job(payload)
    return atomic_write_json(payload, output_path)


def _vectcut_source_manifest(source_root: str | Path) -> dict[str, Any]:
    root = Path(source_root).expanduser().resolve()
    if not root.is_dir():
        raise ContractError("VectCut source root was not found")
    head = _git_head(root)
    if head != VECTCUT_COMMIT:
        raise ContractError("VectCut source commit drifted from the reviewed source")
    files = {
        name: artifact_reference(root / relative, f"VectCut {name}")
        for name, relative in VECTCUT_FILES.items()
    }
    license_text = (root / VECTCUT_FILES["license"]).read_text(
        encoding="utf-8", errors="replace"
    )
    if "Apache License" not in license_text or "Version 2.0" not in license_text:
        raise ContractError("VectCut root license is not the reviewed Apache-2.0 text")
    return {
        "repository": VECTCUT_REPOSITORY,
        "commit": head,
        "root": str(root),
        "files": files,
        "root_license": "Apache-2.0",
        "package_classifier_conflict": "MIT",
        "source_policy": "root_license_authoritative_no_code_vendoring",
    }


def _vectcut_project_source_manifest(source_root: str | Path) -> dict[str, Any]:
    base = _vectcut_source_manifest(source_root)
    root = Path(base["root"])
    project_files = {
        name: artifact_reference(root / relative, f"VectCut project {name}")
        for name, relative in VECTCUT_PROJECT_FILES.items()
    }
    template_dir = root / "template_jianying_10_2"
    if not template_dir.is_dir():
        raise ContractError("VectCut Jianying 10.2 template directory was not found")
    return {
        **base,
        "project_files": project_files,
        "template_dir": str(template_dir),
        "project_adapter_policy": (
            "copy_fresh_template_then_call_upstream_write_profile_content"
        ),
    }


def _validate_vectcut_source_manifest(payload: dict[str, Any]) -> dict[str, Any]:
    if (
        payload.get("repository") != VECTCUT_REPOSITORY
        or payload.get("commit") != VECTCUT_COMMIT
        or payload.get("root_license") != "Apache-2.0"
        or payload.get("package_classifier_conflict") != "MIT"
        or payload.get("source_policy")
        != "root_license_authoritative_no_code_vendoring"
    ):
        raise ContractError("VectCut source manifest is invalid")
    expected = _vectcut_source_manifest(str(payload.get("root") or ""))
    if payload != expected:
        raise ContractError("VectCut source manifest drifted")
    return expected


def _validate_draft_content(draft: dict[str, Any], job: dict[str, Any]) -> None:
    canvas = _object(draft.get("canvas_config"))
    if (
        canvas.get("width") != job["canvas"]["width"]
        or canvas.get("height") != job["canvas"]["height"]
        or draft.get("fps") != job["canvas"]["fps"]
    ):
        raise ContractError("Jianying draft canvas differs from the export job")
    tracks = draft.get("tracks")
    if not isinstance(tracks, list) or not tracks:
        raise ContractError("Jianying draft has no tracks")
    segment_count = sum(
        len(track.get("segments") or []) for track in tracks if isinstance(track, dict)
    )
    if segment_count != len(job["segments"]):
        raise ContractError("Jianying draft segment count differs from the export job")


def _validate_project_folder(
    project_dir: Path, candidate_receipt: dict[str, Any]
) -> None:
    required = (
        "draft_content.json",
        "draft_content.json.bak",
        "template-2.tmp",
        "Timelines/project.json",
        "Timelines/project.json.bak",
    )
    for relative in required:
        if not (project_dir / relative).is_file():
            raise ContractError(f"Jianying project folder lacks {relative}")
    root_draft = read_json_object(
        project_dir / "draft_content.json", "Jianying project draft content"
    )
    job_path = validate_artifact_reference(
        _object(candidate_receipt["source_job"]), "Jianying draft job"
    )
    job = read_json_object(job_path, "Jianying draft job")
    _validate_any_jianying_draft_job(job)
    _validate_draft_content(root_draft, job)
    timeline_id = str(root_draft.get("id") or "").strip()
    if not timeline_id:
        raise ContractError("Jianying project draft lacks a timeline id")
    timeline_dir = project_dir / "Timelines" / timeline_id
    timeline_draft = timeline_dir / "draft_content.json"
    if not timeline_draft.is_file():
        raise ContractError("Jianying project timeline directory is inconsistent")
    if file_sha256(timeline_draft) != file_sha256(project_dir / "draft_content.json"):
        raise ContractError("Jianying project root and timeline content differ")
    project = read_json_object(
        project_dir / "Timelines" / "project.json", "Jianying timeline project"
    )
    if (
        project.get("id") != timeline_id
        or project.get("main_timeline_id") != timeline_id
        or not any(
            isinstance(row, dict) and row.get("id") == timeline_id
            for row in project.get("timelines") or []
        )
    ):
        raise ContractError("Jianying project timeline identity is inconsistent")


def _directory_manifest(root: Path) -> list[dict[str, Any]]:
    if not root.is_dir():
        raise ContractError(f"project folder not found: {root}")
    rows = []
    for path in sorted(
        (item for item in root.rglob("*") if item.is_file()),
        key=lambda item: item.relative_to(root).as_posix(),
    ):
        rows.append(
            {
                "path": path.relative_to(root).as_posix(),
                "bytes": path.stat().st_size,
                "sha256": file_sha256(path),
            }
        )
    if not rows:
        raise ContractError("Jianying project folder is empty")
    return rows


def _run_worker(
    command: list[str], timeout_s: float, label: str
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=float(timeout_s),
            check=False,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ContractError(f"{label} failed to start: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "unknown error").strip()[-2000:]
        raise ContractError(f"{label} failed: {detail}")
    return result


def _python_executable(value: str | Path | None) -> Path:
    path = Path(value or sys.executable).expanduser().resolve()
    if not path.is_file():
        raise ContractError("VectCut worker Python executable was not found")
    return path


def _git_head(root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ContractError(f"VectCut source commit could not be read: {exc}") from exc
    head = result.stdout.strip()
    if result.returncode != 0 or len(head) != 40:
        raise ContractError("VectCut source commit could not be read")
    return head


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise ContractError(f"{label} must be a positive integer")
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{label} must be a positive integer") from exc
    if normalized <= 0 or normalized != value:
        raise ContractError(f"{label} must be a positive integer")
    return normalized


def _object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError("expected a JSON object")
    return value
