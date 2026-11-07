from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from .contract_io import (
    artifact_reference,
    atomic_copy_promote,
    atomic_write_json,
    atomic_write_text,
    canonical_json_sha256,
    file_sha256,
    payload_sha256,
    read_json_object,
    validate_artifact_reference,
)
from .errors import ContractError
from .execution_receipts import (
    build_ffmpeg_execution_receipt,
    execute_ffmpeg_with_receipt,
    probe_media_metadata,
    resolve_media_tool,
    validate_ffmpeg_execution_receipt,
    write_execution_contract,
)

JOB_SCHEMA = "video_creation_pipeline.privacy_redaction_job.v1"
RECEIPT_SCHEMA = "video_creation_pipeline.privacy_redaction_receipt.v1"
CLOAKFRAME_HANDOFF_SCHEMA = "video_creation_pipeline.cloakframe_gui_handoff.v1"
CLOAKFRAME_ADOPTION_SCHEMA = "video_creation_pipeline.cloakframe_adoption.v1"
CLOAKFRAME_COMMIT = "abdf376799e88d4762d4534cfdf620a91f621c8f"
SENTRYBLUR_COMMIT = "ded52d269b6e8e19dbbaf79f353426da3a228a98"
SENTRYBLUR_VERSION = "0.1.0"
DEFAULT_REDACTION_PARAMETERS = {
    "style": "pixelate",
    "dilation": 112,
    "window": 5,
    "pixel_size": 48,
    "blur_strength": 51,
    "confidence": 0.25,
    "gpu": True,
}
TARGETS = {"face", "plate", "manual_region"}
_AUTOMATIC_TARGETS = {"face", "plate"}
_SENTRYBLUR_COMMAND = {"face": "faces", "plate": "plates"}
_HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_RUNTIME_PROVIDER_MARKER = "VIDEO_CREATION_ORT_RUNTIME="
_SESSION_PROVIDER_MARKER = "VIDEO_CREATION_ORT_SESSION="
_CAPTURE_PATH_ENV = "VIDEO_CREATION_REDACTION_CAPTURE_PATH"
_CAPTURE_TARGET_ENV = "VIDEO_CREATION_REDACTION_CAPTURE_TARGET"
_EXECUTION_POLICY = {
    "candidate_only": True,
    "requires_explicit_execution": True,
    "automatic_fallback": False,
    "rescan_required": True,
    "automatic_publish": False,
    "source_media_modified": False,
    "network_model_download_allowed": False,
}


def build_redaction_job(spec: dict[str, Any]) -> dict[str, Any]:
    mode = str(spec.get("mode") or "").strip()
    if mode not in {"mask_all", "preserve_authorized_subject"}:
        raise ContractError(
            "redaction job must explicitly choose mask_all or preserve_authorized_subject"
        )
    source = Path(str(spec.get("input") or "")).expanduser().resolve()
    output = Path(str(spec.get("output") or "")).expanduser().resolve()
    if not source.is_file():
        raise ContractError(f"redaction input not found: {source}")
    if source == output or output.exists():
        raise ContractError("redaction output must be distinct and absent")

    targets = [str(value).strip() for value in spec.get("targets") or ["face", "plate"]]
    if (
        not targets
        or any(target not in TARGETS for target in targets)
        or len(set(targets)) != len(targets)
    ):
        raise ContractError(
            "redaction targets must be unique face, plate, or manual_region"
        )
    params = _normalize_parameters(dict(spec.get("parameters") or {}), targets)
    cloakframe_required = (
        "manual_region" in targets or mode == "preserve_authorized_subject"
    )
    route = (
        "blocked_unsupported_solid"
        if params["style"] == "solid"
        else "cloakframe_gui"
        if cloakframe_required
        else "sentryblur_cli"
    )
    params.update(
        {
            "cloakframe_required": cloakframe_required and route == "cloakframe_gui",
            "execution_route": route,
        }
    )

    tool = _optional_file(spec.get("sentryblur_executable"))
    model_root = _optional_directory(spec.get("sentryblur_model_root"))
    if route == "sentryblur_cli" and model_root is None:
        raise ContractError("SentryBlur model root is required for automatic redaction")
    model_manifest = _build_model_manifest(spec, targets, route, model_root)
    model_hash = _model_manifest_hash(model_manifest) if model_manifest else None
    model_root_hash = _directory_manifest_hash(model_root) if model_root else None
    supplied_model_hash = str(spec.get("sentryblur_model_hash") or "").strip()
    if supplied_model_hash and supplied_model_hash != model_hash:
        raise ContractError(
            "SentryBlur supplied model hash does not match model manifest"
        )
    if route == "sentryblur_cli" and tool is None:
        raise ContractError("SentryBlur executable is required for automatic redaction")

    cloakframe_tool = _optional_file(spec.get("cloakframe_executable"))
    job: dict[str, Any] = {
        "schema": JOB_SCHEMA,
        "created_at": str(spec.get("created_at") or _now()),
        "mode": mode,
        "input": artifact_reference(source, "redaction input"),
        "output_path": str(output),
        "targets": targets,
        "sentryblur": {
            "executable": str(tool) if tool else None,
            "executable_sha256": file_sha256(tool) if tool else None,
            "source_commit": SENTRYBLUR_COMMIT,
            "tool_version": str(spec.get("sentryblur_version") or SENTRYBLUR_VERSION),
            "models": model_manifest,
            "model_hash": model_hash,
            "model_root": str(model_root) if model_root else None,
            "model_root_sha256": model_root_hash,
        },
        "cloakframe": {
            "source_commit": CLOAKFRAME_COMMIT,
            "executable": str(cloakframe_tool) if cloakframe_tool else None,
            "executable_sha256": file_sha256(cloakframe_tool)
            if cloakframe_tool
            else None,
        },
        "parameters": params,
        "execution_policy": dict(_EXECUTION_POLICY),
    }
    job["job_sha256"] = payload_sha256(job, "job_sha256")
    validate_redaction_job(job)
    return job


def validate_redaction_job(
    job: dict[str, Any], *, allow_output_exists: bool = False
) -> None:
    _validate(job, "privacy_redaction_job.v1.schema.json")
    if job.get("job_sha256") != payload_sha256(job, "job_sha256"):
        raise ContractError("redaction job integrity check failed")
    source = validate_artifact_reference(job["input"], "redaction input")
    output = Path(str(job["output_path"])).expanduser().resolve()
    if output == source or (output.exists() and not allow_output_exists):
        raise ContractError("redaction output must be distinct and absent")

    params = dict(job["parameters"])
    normalized = _normalize_parameters(params, list(job["targets"]))
    expected_route = (
        "blocked_unsupported_solid"
        if normalized["style"] == "solid"
        else "cloakframe_gui"
        if "manual_region" in job["targets"]
        or job["mode"] == "preserve_authorized_subject"
        else "sentryblur_cli"
    )
    normalized.update(
        {
            "cloakframe_required": expected_route == "cloakframe_gui",
            "execution_route": expected_route,
        }
    )
    if params != normalized:
        raise ContractError("redaction parameters are not normalized")
    if job.get("execution_policy") != _EXECUTION_POLICY:
        raise ContractError("redaction execution policy drifted")

    sentryblur = dict(job["sentryblur"])
    models = list(sentryblur.get("models") or [])
    for row in models:
        validate_artifact_reference(
            row["artifact"], f"SentryBlur {row['target']} model"
        )
    actual_model_hash = _model_manifest_hash(models) if models else None
    if sentryblur.get("model_hash") != actual_model_hash:
        raise ContractError("SentryBlur model manifest hash drifted")
    if expected_route == "sentryblur_cli":
        if not set(job["targets"]) <= _AUTOMATIC_TARGETS:
            raise ContractError("SentryBlur accepts only face and plate targets")
        executable = str(sentryblur.get("executable") or "")
        if not executable or not Path(executable).is_file():
            raise ContractError("SentryBlur executable is missing")
        if file_sha256(executable) != sentryblur.get("executable_sha256"):
            raise ContractError("SentryBlur executable hash drifted")
        if not _HEX_SHA256.fullmatch(str(sentryblur.get("model_hash") or "")):
            raise ContractError("SentryBlur model hash is required for execution")
        if {row["target"] for row in models} != set(job["targets"]):
            raise ContractError("SentryBlur model manifest does not cover every target")
        model_root = Path(str(sentryblur.get("model_root") or "")).resolve()
        if not model_root.is_dir():
            raise ContractError("SentryBlur model root is missing")
        for row in models:
            model_path = Path(str(row["artifact"]["path"])).resolve()
            if not model_path.is_relative_to(model_root):
                raise ContractError("SentryBlur model file is outside the model root")
        if _directory_manifest_hash(model_root) != sentryblur.get("model_root_sha256"):
            raise ContractError("SentryBlur model root hash drifted")

    cloakframe = dict(job["cloakframe"])
    cloakframe_executable = cloakframe.get("executable")
    if cloakframe_executable:
        if not Path(str(cloakframe_executable)).is_file():
            raise ContractError("CloakFrame executable is missing")
        if file_sha256(str(cloakframe_executable)) != cloakframe.get(
            "executable_sha256"
        ):
            raise ContractError("CloakFrame executable hash drifted")


def write_redaction_job(job: dict[str, Any], path: str | Path) -> Path:
    validate_redaction_job(job)
    return atomic_write_json(job, path)


def build_cloakframe_handoff(job_path: str | Path, output_path: str | Path) -> Path:
    source_job = Path(job_path).expanduser().resolve()
    job = read_json_object(source_job, "redaction job")
    validate_redaction_job(job)
    if job["parameters"]["execution_route"] != "cloakframe_gui":
        if job["parameters"]["execution_route"] == "blocked_unsupported_solid":
            raise ContractError("solid redaction is unsupported by the selected tools")
        raise ContractError("automatic mask_all jobs use SentryBlur preview")
    payload: dict[str, Any] = {
        "schema": CLOAKFRAME_HANDOFF_SCHEMA,
        "created_at": _now(),
        "job": artifact_reference(source_job, "redaction job"),
        "job_sha256": job["job_sha256"],
        "input": job["input"],
        "candidate_output_path": job["output_path"],
        "targets": job["targets"],
        "manual_regions": job["parameters"]["manual_regions"],
        "source_commit": CLOAKFRAME_COMMIT,
        "requires_bidirectional_track_review": True,
        "requires_false_track_exclusion_review": True,
        "requires_uncovered_span_review": True,
        "adoption_blocked_until": "manual_track_review_completed",
        "status": "manual_review_required",
        "publish_allowed": False,
    }
    payload["handoff_sha256"] = payload_sha256(payload, "handoff_sha256")
    return atomic_write_json(payload, output_path)


def preview_redaction(
    job_path: str | Path,
    output_path: str | Path,
    *,
    timeout_s: float = 300,
) -> dict[str, Any]:
    source_job = Path(job_path).expanduser().resolve()
    job = read_json_object(source_job, "redaction job")
    validate_redaction_job(job)
    route = job["parameters"]["execution_route"]
    if route == "blocked_unsupported_solid":
        raise ContractError("solid redaction is unsupported by the selected tools")
    if route == "cloakframe_gui":
        handoff = build_cloakframe_handoff(source_job, output_path)
        return {
            "status": "manual_review_required",
            "handoff": str(handoff),
            "publish_allowed": False,
        }

    requested = Path(output_path).expanduser().resolve()
    preview_paths = _preview_paths(requested, list(job["targets"]))
    for path in preview_paths:
        _absent(path)
    stages: list[dict[str, Any]] = []
    provider_audits: list[dict[str, Any]] = []
    created: list[Path] = []
    model_root = Path(job["sentryblur"]["model_root"]).resolve()
    model_root_before = _directory_manifest_hash(model_root)
    try:
        for target, output in zip(job["targets"], preview_paths, strict=True):
            command = _command(
                job,
                target,
                Path(job["input"]["path"]),
                output,
                preview=True,
            )
            result = _run(command, timeout_s, job)
            if result.returncode != 0 or not output.is_file():
                raise ContractError(f"SentryBlur preview {target} failed")
            provider_audit = _provider_audit_from_result(
                gpu_requested=job["parameters"]["gpu"], result=result
            )
            provider_audits.append(provider_audit)
            created.append(output)
            stages.append(
                {
                    "target": target,
                    "command": command,
                    "returncode": result.returncode,
                    "output": artifact_reference(output, f"{target} preview"),
                    "stdout_tail": (result.stdout or "")[-2000:],
                    "stderr_tail": (result.stderr or "")[-2000:],
                    "provider_audit": provider_audit,
                }
            )
        model_root_after = _directory_manifest_hash(model_root)
        if model_root_after != model_root_before:
            raise ContractError("SentryBlur changed the pinned model root")
        return {
            "status": "preview_ready",
            "outputs": [stage["output"] for stage in stages],
            "stages": stages,
            "candidate_only": True,
            "publish_allowed": False,
            "model_root_sha256_before": model_root_before,
            "model_root_sha256_after": model_root_after,
            "model_cache_unchanged": True,
            "provider_audit": _merge_provider_audits(provider_audits),
        }
    except Exception:
        for path in created + preview_paths:
            _remove(path)
        raise


def run_redaction(
    job_path: str | Path,
    receipt_path: str | Path,
    *,
    timeout_s: float = 3600,
    capture_zones: bool = False,
) -> dict[str, Any]:
    source_job = Path(job_path).expanduser().resolve()
    job = read_json_object(source_job, "redaction job")
    validate_redaction_job(job)
    route = job["parameters"]["execution_route"]
    if route == "blocked_unsupported_solid":
        raise ContractError("solid redaction is unsupported by the selected tools")
    if route != "sentryblur_cli":
        raise ContractError("this job requires CloakFrame handoff and adoption")
    output = Path(job["output_path"]).resolve()
    _absent(output)
    work: list[Path] = []
    success_sidecars: list[Path] = []
    stages: list[dict[str, Any]] = []
    provider_audits: list[dict[str, Any]] = []
    audio_restore_binding: tuple[dict[str, Any], Path] | None = None
    model_root = Path(job["sentryblur"]["model_root"]).resolve()
    model_root_before = _directory_manifest_hash(model_root)
    try:
        current = Path(job["input"]["path"]).resolve()
        for index, target in enumerate(job["targets"]):
            staged = output.with_name(
                f".{output.stem}.{index + 1}.{target}{output.suffix}"
            )
            _absent(staged)
            work.append(staged)
            command = _command(job, target, current, staged, preview=False)
            capture_path = (
                output.parent
                / ".privacy-redaction-zones"
                / f"{output.stem}.{index + 1:02d}-{target}.jsonl"
                if capture_zones
                else None
            )
            if capture_path is not None:
                _absent(capture_path)
            result = _run(
                command,
                timeout_s,
                job,
                capture_path=capture_path,
                capture_target=target if capture_zones else None,
            )
            logs = _logs(output.parent, output.stem, index + 1, target, result)
            if result.returncode != 0 or not staged.is_file():
                raise ContractError(
                    f"SentryBlur {target} failed; no final output was promoted"
                )
            if capture_path is not None:
                if not capture_path.is_file():
                    raise ContractError(
                        "SentryBlur coordinate capture was not atomically promoted"
                    )
                success_sidecars.append(capture_path)
            provider_audit = _provider_audit_from_result(
                gpu_requested=job["parameters"]["gpu"], result=result
            )
            provider_audits.append(provider_audit)
            stages.append(
                _stage(
                    job,
                    target,
                    command,
                    result,
                    current,
                    staged,
                    logs,
                    provider_audit,
                    capture_path,
                )
            )
            current = staged
        model_root_after = _directory_manifest_hash(model_root)
        if model_root_after != model_root_before:
            raise ContractError("SentryBlur changed the pinned model root")
        source = Path(job["input"]["path"]).resolve()
        if _first_stream(probe_media_metadata(source), "audio") is not None:
            restored = output.with_name(f".{output.stem}.audio-restored{output.suffix}")
            _absent(restored)
            work.append(restored)
            audio_stage, scratch_receipt, final_receipt = _restore_source_audio(
                current,
                source,
                restored,
                output,
                timeout_s=timeout_s,
            )
            work.append(scratch_receipt)
            stages.append(audio_stage)
            audio_restore_binding = (audio_stage, final_receipt)
            current = restored
        atomic_copy_promote(current, output, label="redaction output")
        if audio_restore_binding is not None:
            audio_stage, final_receipt = audio_restore_binding
            _finalize_audio_restore_receipt(
                audio_stage,
                final_receipt,
                source_job,
                source,
                output,
            )
            success_sidecars.append(final_receipt)
        receipt = _build_receipt(
            source_job,
            job,
            output,
            stages,
            multi_encode=len(job["targets"]) > 1,
            engine={
                "name": "SentryBlur",
                "tool_version": job["sentryblur"]["tool_version"],
                "source_commit": job["sentryblur"]["source_commit"],
                "executable_sha256": job["sentryblur"]["executable_sha256"],
                "model_hash": job["sentryblur"]["model_hash"],
                "model_root_sha256_before": model_root_before,
                "model_root_sha256_after": model_root_after,
                "model_cache_unchanged": True,
                "provider_audit": _merge_provider_audits(provider_audits),
                "coordinate_capture": {
                    "enabled": capture_zones,
                    "same_invocation_as_redaction": capture_zones,
                    "format": "jsonl_xyxy_v1" if capture_zones else None,
                    "stage_count": len(job["targets"]) if capture_zones else 0,
                },
            },
        )
        atomic_write_json(receipt, receipt_path)
        return receipt
    except Exception:
        _remove(output)
        for sidecar in success_sidecars:
            _remove(sidecar)
        raise
    finally:
        for staged in work:
            _remove(staged)


def adopt_redaction(
    job_path: str | Path,
    candidate_path: str | Path,
    adoption_spec: dict[str, Any],
    receipt_path: str | Path,
) -> dict[str, Any]:
    source_job = Path(job_path).expanduser().resolve()
    job = read_json_object(source_job, "redaction job")
    validate_redaction_job(job)
    if job["parameters"]["execution_route"] != "cloakframe_gui":
        raise ContractError("only CloakFrame handoff jobs can adopt a GUI candidate")
    normalized_adoption = _validate_adoption_spec(adoption_spec, job)
    candidate = Path(candidate_path).expanduser().resolve()
    source = Path(job["input"]["path"]).resolve()
    output = Path(job["output_path"]).resolve()
    if not candidate.is_file():
        raise ContractError("external redaction candidate not found")
    if candidate in {source, output}:
        raise ContractError(
            "external candidate must be distinct from source and output"
        )
    if file_sha256(candidate) != normalized_adoption["candidate_sha256"]:
        raise ContractError("external candidate hash does not match adoption spec")
    _absent(output)
    atomic_copy_promote(candidate, output, label="adopted redaction output")
    try:
        stage = {
            "target": "manual_gui_review",
            "operation": "cloakframe_adopt",
            "input": _compact_reference(candidate, "CloakFrame candidate"),
            "output": _compact_reference(output, "adopted redaction output"),
            "manual_track_review_completed": True,
            "operator": normalized_adoption["operator"],
        }
        receipt = _build_receipt(
            source_job,
            job,
            output,
            [stage],
            multi_encode=False,
            engine={
                "name": "CloakFrame",
                "tool_version": normalized_adoption["tool_version"],
                "source_commit": CLOAKFRAME_COMMIT,
                "executable_sha256": normalized_adoption.get("executable_sha256"),
                "model_hash": normalized_adoption["model_hash"],
            },
            adoption=normalized_adoption,
        )
        atomic_write_json(receipt, receipt_path)
        return receipt
    except Exception:
        _remove(output)
        raise


def validate_redaction_receipt(receipt: dict[str, Any]) -> None:
    _validate(receipt, "privacy_redaction_receipt.v1.schema.json")
    if receipt.get("receipt_sha256") != payload_sha256(receipt, "receipt_sha256"):
        raise ContractError("redaction receipt integrity check failed")
    job_path = validate_artifact_reference(receipt["job"], "redaction job")
    job = read_json_object(job_path, "redaction job")
    validate_redaction_job(job, allow_output_exists=True)
    if receipt.get("job_sha256") != job["job_sha256"]:
        raise ContractError("redaction receipt is bound to a different job")
    if receipt.get("input") != job["input"]:
        raise ContractError("redaction receipt input drifted from job")
    validate_artifact_reference(receipt["input"], "redaction input")
    output_path = validate_artifact_reference(receipt["output"], "redaction output")
    if output_path != Path(job["output_path"]).resolve():
        raise ContractError("redaction receipt output path drifted from job")
    if receipt.get("execution_policy") != _EXECUTION_POLICY:
        raise ContractError("redaction receipt cannot authorize publish")
    if (
        receipt.get("status") != "candidate_only"
        or receipt.get("publish_allowed") is not False
        or receipt.get("rescan_required") is not True
    ):
        raise ContractError("redaction receipt cannot authorize publish")
    if (
        receipt["full_decode"].get("verified") is not True
        or receipt["full_decode"].get("returncode") != 0
        or receipt["duration"].get("within_one_frame") is not True
        or receipt["audio"].get("preserved") is not True
    ):
        raise ContractError("redaction receipt media QA is not complete")
    engine = dict(receipt["engine"])
    route = job["parameters"]["execution_route"]
    expected_name = "SentryBlur" if route == "sentryblur_cli" else "CloakFrame"
    if engine.get("name") != expected_name:
        raise ContractError("redaction receipt engine does not match job route")
    if not _HEX_SHA256.fullmatch(str(engine.get("model_hash") or "")):
        raise ContractError("redaction receipt model hash is invalid")
    if expected_name == "SentryBlur":
        expected_root_hash = job["sentryblur"]["model_root_sha256"]
        if (
            engine.get("model_root_sha256_before") != expected_root_hash
            or engine.get("model_root_sha256_after") != expected_root_hash
            or engine.get("model_cache_unchanged") is not True
        ):
            raise ContractError("redaction receipt model root provenance is invalid")
        provider_audit = engine.get("provider_audit")
        if provider_audit is None:
            if job["parameters"]["gpu"]:
                raise ContractError("GPU redaction receipt is missing provider audit")
        else:
            _validate_provider_audit(
                dict(provider_audit), gpu_requested=job["parameters"]["gpu"]
            )
        coordinate_capture = dict(engine.get("coordinate_capture") or {})
        capture_enabled = coordinate_capture.get("enabled") is True
        if capture_enabled:
            if (
                coordinate_capture.get("same_invocation_as_redaction") is not True
                or coordinate_capture.get("format") != "jsonl_xyxy_v1"
                or coordinate_capture.get("stage_count") != len(job["targets"])
            ):
                raise ContractError(
                    "redaction coordinate capture provenance is invalid"
                )
        elif coordinate_capture and coordinate_capture != {
            "enabled": False,
            "same_invocation_as_redaction": False,
            "format": None,
            "stage_count": 0,
        }:
            raise ContractError("disabled redaction coordinate capture drifted")
    coordinate_capture_count = 0
    for stage in receipt["stages"]:
        for field in (
            "stdout_log",
            "stderr_log",
            "ffmpeg_log",
            "runtime_executable",
            "adapter_runner",
            "coordinate_capture",
        ):
            if stage.get(field) is not None:
                validate_artifact_reference(stage[field], f"redaction {field}")
        if stage.get("coordinate_capture") is not None:
            coordinate_capture_count += 1
            if (
                stage.get("coordinate_capture_target") != stage.get("target")
                or stage.get("same_invocation_as_redaction") is not True
            ):
                raise ContractError("redaction stage coordinate capture drifted")
        if stage.get("ffmpeg_receipt") is not None:
            ffmpeg_receipt_path = validate_artifact_reference(
                stage["ffmpeg_receipt"], "redaction FFmpeg receipt"
            )
            validate_ffmpeg_execution_receipt(
                read_json_object(ffmpeg_receipt_path, "redaction FFmpeg receipt")
            )
    if expected_name == "SentryBlur":
        expected_capture_count = int(
            (engine.get("coordinate_capture") or {}).get("stage_count") or 0
        )
        if coordinate_capture_count != expected_capture_count:
            raise ContractError("redaction coordinate capture stage count drifted")


def privacy_redaction_doctor(
    *,
    sentryblur_executable: str | Path | None = None,
    sentryblur_model_root: str | Path | None = None,
    cloakframe_executable: str | Path | None = None,
) -> dict[str, Any]:
    sentryblur = _doctor_tool(sentryblur_executable, "sentryblur")
    cloakframe = _doctor_tool(cloakframe_executable, "CloakFrame")
    model_root = _optional_directory(sentryblur_model_root)
    if sentryblur["status"] == "available_unexecuted" and model_root is not None:
        gpu_runtime = _probe_sentryblur_runtime(
            Path(str(sentryblur["path"])), model_root
        )
        sentryblur["status"] = (
            "runtime_ready" if gpu_runtime["status"] == "ready" else "runtime_blocked"
        )
    else:
        gpu_runtime = {
            "status": "not_probed",
            "reason": (
                "SentryBlur executable was not supplied"
                if sentryblur["path"] is None
                else "SentryBlur model root was not supplied"
            ),
            "requested_execution_provider": "CUDAExecutionProvider",
        }
    return {
        "schema": "video_creation_pipeline.privacy_redaction_doctor.v1",
        "status": (
            "runtime_ready"
            if gpu_runtime["status"] == "ready"
            else "contract_ready_runtime_optional"
        ),
        "contract_ready": True,
        "tools": {
            "sentryblur": {
                **sentryblur,
                "source_commit": SENTRYBLUR_COMMIT,
                "runtime_scope": ["face", "plate"],
                "model_download_performed": False,
                "gpu_runtime": gpu_runtime,
            },
            "cloakframe": {
                **cloakframe,
                "source_commit": CLOAKFRAME_COMMIT,
                "runtime_scope": ["face", "plate", "manual_region"],
                "source_reviewed": True,
            },
        },
        "routes": {
            "mask_all": "sentryblur_cli",
            "preserve_authorized_subject": "cloakframe_gui_handoff",
            "manual_region": "cloakframe_gui_handoff",
            "solid": "blocked_unsupported_by_selected_tools",
        },
        "default_parameters": dict(DEFAULT_REDACTION_PARAMETERS),
        "boundaries": {
            "candidate_only": True,
            "explicit_mode_required": True,
            "automatic_fallback": False,
            "network_model_download_allowed": False,
            "privacy_rescan_required": True,
            "automatic_publish": False,
        },
    }


def _build_receipt(
    job_path: Path,
    job: dict[str, Any],
    output: Path,
    stages: list[dict[str, Any]],
    multi_encode: bool,
    engine: dict[str, Any],
    adoption: dict[str, Any] | None = None,
) -> dict[str, Any]:
    input_probe = probe_media_metadata(job["input"]["path"])
    output_probe = probe_media_metadata(output)
    decoded = _full_decode(output)
    input_duration = _duration(input_probe, "redaction input")
    output_duration = _duration(output_probe, "redaction output")
    error = abs(output_duration - input_duration)
    frame = _frame_duration(input_probe)
    audio = _audio_continuity(input_probe, output_probe, frame)
    receipt: dict[str, Any] = {
        "schema": RECEIPT_SCHEMA,
        "created_at": _now(),
        "status": "candidate_only",
        "job": artifact_reference(job_path, "redaction job"),
        "job_sha256": job["job_sha256"],
        "input": job["input"],
        "output": artifact_reference(output, "redaction output"),
        "stages": stages,
        "multi_encode": multi_encode,
        "engine": engine,
        "ffprobe": {"input": input_probe, "output": output_probe},
        "full_decode": decoded,
        "duration": {
            "input_s": input_duration,
            "output_s": output_duration,
            "error_s": error,
            "max_error_s": frame,
            "within_one_frame": error <= frame,
        },
        "audio": audio,
        "execution_policy": dict(_EXECUTION_POLICY),
        "publish_allowed": False,
        "rescan_required": True,
    }
    if adoption is not None:
        receipt["cloakframe_adoption"] = adoption
    if not decoded["verified"] or error > frame or audio["preserved"] is not True:
        raise ContractError("redaction media QA failed")
    receipt["receipt_sha256"] = payload_sha256(receipt, "receipt_sha256")
    _validate(receipt, "privacy_redaction_receipt.v1.schema.json")
    return receipt


def _command(
    job: dict[str, Any],
    target: str,
    source: Path,
    output: Path,
    *,
    preview: bool,
) -> list[str]:
    params = job["parameters"]
    prefix = _sentryblur_command_prefix(job)
    command = [
        *prefix,
        _SENTRYBLUR_COMMAND[target],
        str(source),
        "--output",
        str(output),
        "--blur-mode",
        params["style"],
        "--dilation",
        str(params["dilation"]),
        "--window",
        str(params["window"]),
        "--pixel-size",
        str(params["pixel_size"]),
        "--blur-strength",
        str(params["blur_strength"]),
        "--conf",
        str(params["confidence"]),
    ]
    if params["gpu"]:
        command.append("--gpu")
    if preview:
        command.append("--preview")
    if command[len(prefix)] not in {"faces", "plates"} or "--last" in command:
        raise ContractError("SentryBlur prompt/last path is forbidden")
    return command


def _sentryblur_command_prefix(job: dict[str, Any]) -> list[str]:
    executable = Path(job["sentryblur"]["executable"]).resolve()
    runner = Path(__file__).with_name("_sentryblur_runner.py").resolve()
    python_name = "python.exe" if os.name == "nt" else "python"
    runtime = executable.with_name(python_name)
    if runtime.is_file() and runner.is_file():
        return [str(runtime), str(runner)]
    return [str(executable)]


def _run(
    command: list[str],
    timeout_s: float,
    job: dict[str, Any],
    *,
    capture_path: Path | None = None,
    capture_target: str | None = None,
) -> subprocess.CompletedProcess[str]:
    model_root = Path(job["sentryblur"]["model_root"]).resolve()
    command_output = Path(command[command.index("--output") + 1]).resolve()
    environment = _redaction_environment(
        model_root,
        include_ffmpeg="--preview" not in command,
        temp_root=command_output.parent / ".privacy-redaction-runtime",
    )
    if capture_path is not None or capture_target is not None:
        if capture_path is None or capture_target not in {"face", "plate"}:
            raise ContractError("redaction coordinate capture configuration is invalid")
        environment[_CAPTURE_PATH_ENV] = str(capture_path.resolve())
        environment[_CAPTURE_TARGET_ENV] = capture_target
    try:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout_s,
            check=False,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ContractError(f"third-party redaction execution failed: {exc}") from exc


def _redaction_environment(
    model_root: Path, *, include_ffmpeg: bool, temp_root: Path | None = None
) -> dict[str, str]:
    environment = dict(os.environ)
    cache_root = model_root / ".cache"
    blocked_proxy = "http://127.0.0.1:9"
    environment.update(
        {
            "HOME": str(model_root),
            "USERPROFILE": str(model_root),
            "XDG_CACHE_HOME": str(cache_root),
            "HF_HOME": str(cache_root / "huggingface"),
            "HF_HUB_CACHE": str(cache_root / "huggingface" / "hub"),
            "TRANSFORMERS_CACHE": str(cache_root / "transformers"),
            "HF_HUB_OFFLINE": "1",
            "HF_DATASETS_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "HTTP_PROXY": blocked_proxy,
            "HTTPS_PROXY": blocked_proxy,
            "ALL_PROXY": blocked_proxy,
            "NO_PROXY": "",
            "NO_ALBUMENTATIONS_UPDATE": "1",
        }
    )
    if temp_root is not None:
        temp_root.mkdir(parents=True, exist_ok=True)
        environment.update(
            {
                "TEMP": str(temp_root),
                "TMP": str(temp_root),
                "TMPDIR": str(temp_root),
            }
        )
    if include_ffmpeg:
        ffmpeg_dir = str(Path(resolve_media_tool("ffmpeg")).parent)
        environment["PATH"] = ffmpeg_dir + os.pathsep + environment.get("PATH", "")
    return environment


def _provider_audit_from_result(
    *, gpu_requested: bool, result: subprocess.CompletedProcess[str]
) -> dict[str, Any]:
    combined_output = (result.stdout or "") + "\n" + (result.stderr or "")
    if gpu_requested and (
        "Falling back to ['CPUExecutionProvider']" in combined_output
        or 'Falling back to ["CPUExecutionProvider"]' in combined_output
        or "CUDNN_FE failure" in combined_output
    ):
        raise ContractError(
            "SentryBlur CUDA inference failed and attempted a CPU fallback"
        )
    runtime_rows = _marker_payloads(result.stdout or "", _RUNTIME_PROVIDER_MARKER)
    session_rows = _marker_payloads(result.stdout or "", _SESSION_PROVIDER_MARKER)
    if len(runtime_rows) != 1:
        raise ContractError("SentryBlur runtime provider audit marker is missing")
    if not session_rows:
        raise ContractError("SentryBlur ONNX session provider audit marker is missing")
    runtime = runtime_rows[0]
    session_providers = [
        [str(provider) for provider in row.get("actual_execution_providers") or []]
        for row in session_rows
    ]
    requested = str(runtime.get("requested_execution_provider") or "")
    if any(
        not providers or providers[0] != requested for providers in session_providers
    ):
        raise ContractError(
            "SentryBlur requested execution provider was not session priority"
        )
    actual: list[str] = []
    for providers in session_providers:
        for provider in providers:
            if provider not in actual:
                actual.append(provider)
    audit = {
        "runtime_version": str(runtime.get("runtime_version") or ""),
        "requested_execution_provider": requested,
        "primary_execution_provider": actual[0],
        "available_execution_providers": sorted(
            str(provider)
            for provider in runtime.get("available_execution_providers") or []
        ),
        "actual_execution_providers": actual,
        "cuda_libraries_preloaded": bool(
            runtime.get("cuda_libraries_preloaded") is True
        ),
        "provider_requirement_met": True,
    }
    _validate_provider_audit(audit, gpu_requested=gpu_requested)
    return audit


def _marker_payloads(text: str, prefix: str) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for line in text.splitlines():
        if not line.startswith(prefix):
            continue
        try:
            payload = json.loads(line[len(prefix) :])
        except json.JSONDecodeError as exc:
            raise ContractError("SentryBlur provider audit marker is invalid") from exc
        if not isinstance(payload, dict):
            raise ContractError("SentryBlur provider audit marker must be an object")
        payloads.append(payload)
    return payloads


def _validate_provider_audit(audit: dict[str, Any], *, gpu_requested: bool) -> None:
    requested = "CUDAExecutionProvider" if gpu_requested else "CPUExecutionProvider"
    if audit.get("requested_execution_provider") != requested:
        raise ContractError("SentryBlur requested execution provider drifted")
    available = set(audit.get("available_execution_providers") or [])
    actual = set(audit.get("actual_execution_providers") or [])
    if requested not in available or requested not in actual:
        raise ContractError(
            f"SentryBlur did not use requested execution provider: {requested}"
        )
    if audit.get("primary_execution_provider") != requested:
        raise ContractError("SentryBlur requested provider was not session priority")
    if gpu_requested and audit.get("cuda_libraries_preloaded") is not True:
        raise ContractError("SentryBlur CUDA/cuDNN libraries were not preloaded")
    if audit.get("provider_requirement_met") is not True:
        raise ContractError("SentryBlur provider requirement was not met")
    if not str(audit.get("runtime_version") or "").strip():
        raise ContractError("SentryBlur ONNX runtime version is missing")


def _merge_provider_audits(audits: list[dict[str, Any]]) -> dict[str, Any]:
    if not audits:
        raise ContractError("SentryBlur provider audit is empty")
    requested = audits[0]["requested_execution_provider"]
    runtime_version = audits[0]["runtime_version"]
    for audit in audits:
        if (
            audit["requested_execution_provider"] != requested
            or audit["runtime_version"] != runtime_version
        ):
            raise ContractError("SentryBlur provider audit changed between stages")
    return {
        "runtime_version": runtime_version,
        "requested_execution_provider": requested,
        "primary_execution_provider": requested,
        "available_execution_providers": sorted(
            {
                provider
                for audit in audits
                for provider in audit["available_execution_providers"]
            }
        ),
        "actual_execution_providers": [
            requested,
            *sorted(
                {
                    provider
                    for audit in audits
                    for provider in audit["actual_execution_providers"]
                    if provider != requested
                }
            ),
        ],
        "cuda_libraries_preloaded": all(
            audit["cuda_libraries_preloaded"] for audit in audits
        ),
        "provider_requirement_met": all(
            audit["provider_requirement_met"] for audit in audits
        ),
    }


def _restore_source_audio(
    redacted_video: Path,
    source: Path,
    output: Path,
    final_output: Path,
    *,
    timeout_s: float,
) -> tuple[dict[str, Any], Path, Path]:
    log_root = final_output.parent / ".privacy-redaction-logs"
    log_root.mkdir(parents=True, exist_ok=True)
    scratch_receipt = output.with_suffix(output.suffix + ".ffmpeg-receipt.json")
    final_receipt = log_root / f"{final_output.stem}.audio-restore.ffmpeg-receipt.json"
    log_path = log_root / f"{final_output.stem}.audio-restore.ffmpeg.log"
    command = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-nostats",
        "-i",
        str(redacted_video),
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-map",
        "1:a?",
        "-c:v",
        "copy",
        "-c:a",
        "copy",
        "-shortest",
        "-map_metadata",
        "-1",
        "-movflags",
        "+faststart",
        str(output),
    ]
    ffmpeg_receipt = execute_ffmpeg_with_receipt(
        "render",
        command,
        [
            {"role": "redacted_video_without_audio", "path": redacted_video},
            {"role": "redaction_source_audio", "path": source},
        ],
        [{"role": "redacted_video_with_source_audio", "path": output}],
        scratch_receipt,
        capture_log_path=log_path,
        timeout_s=timeout_s,
    )
    stage = {
        "target": "audio",
        "operation": "restore_source_audio",
        "command": command,
        "ephemeral_video_input": _compact_reference(
            redacted_video, "redacted video without audio"
        ),
        "audio_source": _compact_reference(source, "redaction source audio"),
        "execution_output_before_promotion": _compact_reference(
            output, "redacted video with source audio"
        ),
        "video_codec_mode": "stream_copy",
        "audio_codec_mode": "stream_copy",
        "ffmpeg_outlet_id": ffmpeg_receipt["outlet_id"],
        "ffmpeg_log": artifact_reference(
            log_path, "redaction audio restore FFmpeg log"
        ),
    }
    return stage, scratch_receipt, final_receipt


def _finalize_audio_restore_receipt(
    stage: dict[str, Any],
    receipt_path: Path,
    job_path: Path,
    source: Path,
    output: Path,
) -> None:
    receipt = build_ffmpeg_execution_receipt(
        "render",
        list(stage["command"]),
        [
            {"role": "privacy_redaction_job", "path": job_path},
            {"role": "redaction_source_audio", "path": source},
        ],
        [{"role": "redaction_candidate", "path": output}],
    )
    write_execution_contract(receipt, receipt_path)
    stage["output"] = _compact_reference(output, "redaction candidate")
    stage["ffmpeg_receipt"] = artifact_reference(
        receipt_path, "redaction audio restore FFmpeg receipt"
    )


def _stage(
    job: dict[str, Any],
    target: str,
    command: list[str],
    result: subprocess.CompletedProcess[str],
    input_path: Path,
    output_path: Path,
    logs: dict[str, dict[str, Any]],
    provider_audit: dict[str, Any],
    coordinate_capture: Path | None,
) -> dict[str, Any]:
    stage = {
        "target": target,
        "operation": _SENTRYBLUR_COMMAND[target],
        "command": command,
        "returncode": result.returncode,
        "input": _compact_reference(input_path, f"{target} input"),
        "output": _compact_reference(output_path, f"{target} output"),
        "stdout_log": logs["stdout"],
        "stderr_log": logs["stderr"],
        "tool_version": job["sentryblur"]["tool_version"],
        "source_commit": job["sentryblur"]["source_commit"],
        "model_hash": job["sentryblur"]["model_hash"],
        "provider_audit": provider_audit,
    }
    if coordinate_capture is not None:
        stage["coordinate_capture"] = artifact_reference(
            coordinate_capture, f"{target} coordinate capture"
        )
        stage["coordinate_capture_target"] = target
        stage["same_invocation_as_redaction"] = True
    runner = Path(__file__).with_name("_sentryblur_runner.py").resolve()
    if len(command) > 2 and Path(command[1]).resolve() == runner:
        stage["runtime_executable"] = artifact_reference(
            Path(command[0]), "SentryBlur Python runtime"
        )
        stage["adapter_runner"] = artifact_reference(
            runner, "SentryBlur no-open adapter"
        )
    return stage


def _logs(
    root: Path,
    output_stem: str,
    index: int,
    target: str,
    result: subprocess.CompletedProcess[str],
) -> dict[str, dict[str, Any]]:
    directory = root / ".privacy-redaction-logs"
    directory.mkdir(parents=True, exist_ok=True)
    stdout = directory / f"{output_stem}.{index:02d}-{target}.stdout.log"
    stderr = directory / f"{output_stem}.{index:02d}-{target}.stderr.log"
    atomic_write_text(result.stdout or "", stdout)
    atomic_write_text(result.stderr or "", stderr)
    return {
        "stdout": artifact_reference(stdout, f"{target} stdout log"),
        "stderr": artifact_reference(stderr, f"{target} stderr log"),
    }


def _full_decode(path: Path) -> dict[str, Any]:
    command = [
        resolve_media_tool("ffmpeg"),
        "-v",
        "error",
        "-i",
        str(path),
        "-f",
        "null",
        "-",
    ]
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        errors="replace",
        check=False,
    )
    return {
        "verified": result.returncode == 0 and not (result.stderr or "").strip(),
        "command": command,
        "returncode": result.returncode,
        "stderr_tail": (result.stderr or "")[-2000:],
    }


def _duration(probe: dict[str, Any], label: str) -> float:
    try:
        value = float(probe.get("format", {}).get("duration"))
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{label} duration is missing") from exc
    if not math.isfinite(value) or value <= 0:
        raise ContractError(f"{label} duration must be positive")
    return value


def _frame_duration(probe: dict[str, Any]) -> float:
    for stream in probe.get("streams", []):
        if stream.get("codec_type") != "video":
            continue
        for field in ("avg_frame_rate", "r_frame_rate"):
            try:
                numerator, denominator = str(stream.get(field) or "0/1").split("/", 1)
                rate = float(numerator) / float(denominator)
                if math.isfinite(rate) and rate > 0:
                    return 1 / rate
            except (ValueError, ZeroDivisionError):
                continue
    raise ContractError("redaction input frame rate is missing")


def _audio_continuity(
    input_probe: dict[str, Any], output_probe: dict[str, Any], frame: float
) -> dict[str, Any]:
    input_stream = _first_stream(input_probe, "audio")
    output_stream = _first_stream(output_probe, "audio")
    input_present = input_stream is not None
    output_present = output_stream is not None
    input_duration = (
        _stream_duration(input_stream, input_probe) if input_stream else None
    )
    output_duration = (
        _stream_duration(output_stream, output_probe) if output_stream else None
    )
    duration_error = (
        abs(float(input_duration) - float(output_duration))
        if input_duration is not None and output_duration is not None
        else None
    )
    within_one_frame = duration_error is None or duration_error <= frame
    return {
        "input_present": input_present,
        "output_present": output_present,
        "input_duration_s": input_duration,
        "output_duration_s": output_duration,
        "duration_error_s": duration_error,
        "within_one_frame": within_one_frame,
        "preserved": input_present == output_present and within_one_frame,
    }


def _first_stream(probe: dict[str, Any], codec_type: str) -> dict[str, Any] | None:
    return next(
        (
            row
            for row in probe.get("streams", [])
            if isinstance(row, dict) and row.get("codec_type") == codec_type
        ),
        None,
    )


def _stream_duration(stream: dict[str, Any], probe: dict[str, Any]) -> float:
    raw = stream.get("duration") or probe.get("format", {}).get("duration")
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ContractError("audio duration is missing") from exc
    if not math.isfinite(value) or value <= 0:
        raise ContractError("audio duration must be positive")
    return value


def _normalize_parameters(
    parameters: dict[str, Any], targets: list[str]
) -> dict[str, Any]:
    style = (
        str(parameters.get("style") or DEFAULT_REDACTION_PARAMETERS["style"])
        .strip()
        .lower()
    )
    if style not in {"pixelate", "gaussian", "solid"}:
        raise ContractError("redaction style must be pixelate, gaussian, or solid")
    dilation = _integer(
        parameters.get("dilation", DEFAULT_REDACTION_PARAMETERS["dilation"]),
        "dilation",
        0,
        512,
    )
    window = _integer(
        parameters.get("window", DEFAULT_REDACTION_PARAMETERS["window"]),
        "window",
        0,
        301,
    )
    pixel_size = _integer(
        parameters.get("pixel_size", DEFAULT_REDACTION_PARAMETERS["pixel_size"]),
        "pixel_size",
        2,
        256,
    )
    blur_strength = _integer(
        parameters.get("blur_strength", DEFAULT_REDACTION_PARAMETERS["blur_strength"]),
        "blur_strength",
        3,
        511,
    )
    if blur_strength % 2 == 0:
        blur_strength += 1
    try:
        confidence = float(
            parameters.get("confidence", DEFAULT_REDACTION_PARAMETERS["confidence"])
        )
    except (TypeError, ValueError) as exc:
        raise ContractError("confidence must be a number") from exc
    if not math.isfinite(confidence) or not 0 < confidence <= 1:
        raise ContractError("confidence must be greater than 0 and at most 1")
    gpu = parameters.get("gpu", DEFAULT_REDACTION_PARAMETERS["gpu"])
    if not isinstance(gpu, bool):
        raise ContractError("gpu must be boolean")
    manual_regions = _normalize_manual_regions(parameters.get("manual_regions") or [])
    if "manual_region" in targets and not manual_regions:
        raise ContractError("manual_region target requires at least one manual region")
    if "manual_region" not in targets and manual_regions:
        raise ContractError("manual regions require the manual_region target")
    return {
        "style": style,
        "dilation": dilation,
        "window": window,
        "pixel_size": pixel_size,
        "blur_strength": blur_strength,
        "confidence": confidence,
        "gpu": gpu,
        "manual_regions": manual_regions,
    }


def _normalize_manual_regions(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ContractError("manual_regions must be a list")
    normalized: list[dict[str, Any]] = []
    ids: set[str] = set()
    for row in value:
        if not isinstance(row, dict):
            raise ContractError("manual region must be an object")
        region_id = str(row.get("region_id") or "").strip()
        if not region_id or region_id in ids:
            raise ContractError("manual region_id must be present and unique")
        ids.add(region_id)
        start_s = _number(row.get("start_s"), "manual region start_s", minimum=0)
        end_s = _number(row.get("end_s"), "manual region end_s", minimum=0)
        if end_s <= start_s:
            raise ContractError("manual region end_s must be after start_s")
        rect = row.get("rect")
        if not isinstance(rect, dict) or set(rect) != {"x", "y", "width", "height"}:
            raise ContractError("manual region rect requires x, y, width, and height")
        values = {
            key: _number(rect[key], f"manual region {key}", minimum=0, maximum=1)
            for key in ("x", "y", "width", "height")
        }
        if values["width"] <= 0 or values["height"] <= 0:
            raise ContractError("manual region dimensions must be positive")
        if values["x"] + values["width"] > 1 or values["y"] + values["height"] > 1:
            raise ContractError("manual region must stay inside normalized frame")
        normalized.append(
            {
                "region_id": region_id,
                "start_s": start_s,
                "end_s": end_s,
                "rect": values,
            }
        )
    return normalized


def _build_model_manifest(
    spec: dict[str, Any],
    targets: list[str],
    route: str,
    model_root: Path | None,
) -> list[dict[str, Any]]:
    if route != "sentryblur_cli":
        return []
    supplied = spec.get("sentryblur_models")
    if not isinstance(supplied, dict):
        raise ContractError(
            "automatic redaction requires explicit SentryBlur model files"
        )
    manifest = []
    for target in targets:
        path = _optional_file(supplied.get(target))
        if path is None:
            raise ContractError(f"SentryBlur {target} model file is required")
        if model_root is None or not path.is_relative_to(model_root):
            raise ContractError(
                f"SentryBlur {target} model file must be inside the model root"
            )
        manifest.append(
            {
                "target": target,
                "artifact": artifact_reference(path, f"SentryBlur {target} model"),
            }
        )
    return manifest


def _model_manifest_hash(models: list[dict[str, Any]]) -> str:
    stable = [
        {
            "target": row["target"],
            "bytes": row["artifact"]["bytes"],
            "sha256": row["artifact"]["sha256"],
        }
        for row in models
    ]
    return canonical_json_sha256(stable)


def _validate_adoption_spec(
    value: dict[str, Any], job: dict[str, Any]
) -> dict[str, Any]:
    required = {
        "schema",
        "job_sha256",
        "candidate_sha256",
        "manual_track_review_completed",
        "operator",
        "tool_version",
        "model_hash",
    }
    if set(value) - (required | {"executable_sha256", "review_notes"}):
        raise ContractError("CloakFrame adoption spec has unsupported fields")
    if not required <= set(value):
        raise ContractError("CloakFrame adoption spec is incomplete")
    if value.get("schema") != CLOAKFRAME_ADOPTION_SCHEMA:
        raise ContractError("unsupported CloakFrame adoption schema")
    if value.get("job_sha256") != job["job_sha256"]:
        raise ContractError("CloakFrame adoption job hash drifted")
    if value.get("manual_track_review_completed") is not True:
        raise ContractError("manual_track_review_completed=true is required")
    for field in ("candidate_sha256", "model_hash"):
        if not _HEX_SHA256.fullmatch(str(value.get(field) or "")):
            raise ContractError(f"CloakFrame adoption {field} is invalid")
    if value.get("executable_sha256") is not None and not _HEX_SHA256.fullmatch(
        str(value["executable_sha256"])
    ):
        raise ContractError("CloakFrame adoption executable_sha256 is invalid")
    if not str(value.get("operator") or "").strip():
        raise ContractError("CloakFrame adoption operator is required")
    if not str(value.get("tool_version") or "").strip():
        raise ContractError("CloakFrame adoption tool_version is required")
    return {
        **value,
        "operator": str(value["operator"]).strip(),
        "tool_version": str(value["tool_version"]).strip(),
        "review_notes": str(value.get("review_notes") or "").strip(),
    }


def _preview_paths(requested: Path, targets: list[str]) -> list[Path]:
    if len(targets) == 1:
        return [requested]
    suffix = requested.suffix or ".jpg"
    return [
        requested.with_name(f"{requested.stem}.{target}{suffix}") for target in targets
    ]


def _compact_reference(path: str | Path, label: str) -> dict[str, Any]:
    reference = artifact_reference(path, label)
    return {
        "path": reference["path"],
        "bytes": reference["bytes"],
        "sha256": reference["sha256"],
    }


def _optional_file(value: Any) -> Path | None:
    if value is None or not str(value).strip():
        return None
    path = Path(str(value)).expanduser().resolve()
    if not path.is_file():
        raise ContractError(f"required third-party file not found: {path}")
    return path


def _optional_directory(value: Any) -> Path | None:
    if value is None or not str(value).strip():
        return None
    path = Path(str(value)).expanduser().resolve()
    if not path.is_dir():
        raise ContractError(f"required third-party directory not found: {path}")
    return path


def _directory_manifest_hash(root: Path) -> str:
    files = sorted(path for path in root.rglob("*") if path.is_file())
    manifest = [
        {
            "path": path.relative_to(root).as_posix(),
            "bytes": path.stat().st_size,
            "sha256": file_sha256(path),
        }
        for path in files
    ]
    return canonical_json_sha256(manifest)


def _doctor_tool(explicit: str | Path | None, command: str) -> dict[str, Any]:
    candidate = str(explicit) if explicit is not None else shutil.which(command)
    if not candidate:
        return {"status": "not_supplied", "path": None, "sha256": None}
    path = Path(candidate).expanduser().resolve()
    if not path.is_file():
        return {"status": "missing", "path": str(path), "sha256": None}
    return {
        "status": "available_unexecuted",
        "path": str(path),
        "sha256": file_sha256(path),
    }


def _probe_sentryblur_runtime(
    executable: Path, model_root: Path, *, timeout_s: float = 120.0
) -> dict[str, Any]:
    runner = Path(__file__).with_name("_sentryblur_runner.py").resolve()
    python_name = "python.exe" if os.name == "nt" else "python"
    runtime = executable.with_name(python_name)
    if not runtime.is_file() or not runner.is_file():
        return {
            "status": "blocked",
            "requested_execution_provider": "CUDAExecutionProvider",
            "error": "auditable SentryBlur Python runtime or adapter runner is missing",
        }
    before = _directory_manifest_hash(model_root)
    command = [str(runtime), str(runner), "--provider-doctor"]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout_s,
            check=False,
            env=_redaction_environment(
                model_root,
                include_ffmpeg=False,
                temp_root=model_root.parent / ".sentryblur-runtime-temp",
            ),
        )
        after = _directory_manifest_hash(model_root)
        if after != before:
            return {
                "status": "blocked",
                "requested_execution_provider": "CUDAExecutionProvider",
                "error": "provider doctor changed the pinned model root",
            }
        if result.returncode != 0:
            return {
                "status": "blocked",
                "requested_execution_provider": "CUDAExecutionProvider",
                "returncode": result.returncode,
                "error": (result.stderr or result.stdout or "provider probe failed")[
                    -2000:
                ],
            }
        audit = _provider_audit_from_result(gpu_requested=True, result=result)
        return {
            "status": "ready",
            **audit,
            "runtime_executable": str(runtime),
            "adapter_runner": str(runner),
            "model_root_sha256": before,
        }
    except (ContractError, OSError, subprocess.TimeoutExpired) as exc:
        return {
            "status": "blocked",
            "requested_execution_provider": "CUDAExecutionProvider",
            "error": str(exc),
        }


def _integer(value: Any, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContractError(f"{label} must be an integer")
    if not minimum <= value <= maximum:
        raise ContractError(f"{label} must be between {minimum} and {maximum}")
    return value


def _number(
    value: Any,
    label: str,
    *,
    minimum: float,
    maximum: float | None = None,
) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{label} must be a number") from exc
    if not math.isfinite(number) or number < minimum:
        raise ContractError(f"{label} is out of range")
    if maximum is not None and number > maximum:
        raise ContractError(f"{label} is out of range")
    return number


def _absent(path: Path) -> None:
    if path.exists():
        raise ContractError("redaction output already exists")


def _remove(path: Path) -> None:
    try:
        if path.is_file():
            path.unlink()
    except OSError:
        pass


def _validate(value: dict[str, Any], filename: str) -> None:
    schema = json.loads(
        (Path(__file__).with_name("schemas") / filename).read_text(encoding="utf-8")
    )
    errors = sorted(
        Draft202012Validator(schema).iter_errors(value),
        key=lambda error: list(error.path),
    )
    if errors:
        raise ContractError(f"{filename}: {errors[0].message}")


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
