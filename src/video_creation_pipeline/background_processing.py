from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

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
    FFMPEG_OUTLET_ID,
    execute_ffmpeg_frame_series_with_receipt,
    execute_ffmpeg_with_receipt,
    probe_media_metadata,
    resolve_media_tool,
    validate_ffmpeg_execution_receipt,
)

BACKGROUND_PROCESSING_PLAN_SCHEMA = (
    "video_creation_pipeline.background_processing_plan.v1"
)
BACKGROUND_PROCESSING_RECEIPT_SCHEMA = (
    "video_creation_pipeline.background_processing_receipt.v1"
)
BACKGROUND_MATTING_MANIFEST_SCHEMA = (
    "video_creation_pipeline.background_matting_manifest.v1"
)
BACKGROUND_MATTING_PROVIDER_RECEIPT_SCHEMA = (
    "video_creation_pipeline.background_matting_provider_receipt.v1"
)
BACKGROUND_MATTING_PROVIDER_FAILURE_SCHEMA = (
    "video_creation_pipeline.background_matting_provider_failure.v1"
)

RVM_REPO_URL = "https://github.com/PeterL1n/RobustVideoMatting.git"
RVM_SOURCE_COMMIT = "53d74c6826735f01f4406b5ca9075eee27bec094"
RVM_MODEL_NAME = "rvm_mobilenetv3.pth"
RVM_MODEL_SHA256 = "3c7c1d92033f7c38d6577c481d13a195d7d80a159b960f4f3119ac7b534cf4f8"
RVM_SOURCE_FILES = {
    "inference.py": "caa06ab9eb6cf6a5497295de957230a3078b2d5b35f1ce80d9775e9eaf32c347",
    "inference_utils.py": "08dbf9306d2889c0f6be53d392d51777f44050b33f9bae9d88fc7fd4f2a63ab8",
    "model/model.py": "7797db24f3d2750602cebffe0063259efe541085778be6812fa8765a164eb4cb",
    "LICENSE": "81cbae84a29ce7e770bf2bc7b178e50bda0ce8de6067aba661b0bc7b05b562f8",
}

BACKGROUND_MODES = {"blur", "replace_color", "replace_image", "transparent"}
MAX_V1_FRAME_COUNT = 9999


def background_processing_doctor(
    provider_source_root: str | Path,
    model_path: str | Path,
    runtime_dependencies_path: str | Path,
    *,
    device: str,
) -> dict[str, Any]:
    source_root = Path(provider_source_root).expanduser().resolve()
    model = Path(model_path).expanduser().resolve()
    runtime_dependencies = Path(runtime_dependencies_path).expanduser().resolve()
    normalized_device = _device(device)
    checks: dict[str, Any] = {
        "source_root_exists": source_root.is_dir(),
        "runtime_dependencies_exist": runtime_dependencies.is_dir(),
        "model_exists": model.is_file(),
        "ffmpeg_available": _media_tool_available("ffmpeg"),
        "ffprobe_available": _media_tool_available("ffprobe"),
    }
    source_head = ""
    source_files: dict[str, dict[str, Any]] = {}
    tracked_source_clean = False
    if source_root.is_dir():
        source_head = _git_output(source_root, ["rev-parse", "HEAD"])
        tracked_source_clean = _git_clean(source_root)
        for relative, expected_sha256 in RVM_SOURCE_FILES.items():
            path = source_root / Path(relative)
            actual_sha256 = _sha256(path) if path.is_file() else ""
            source_files[relative] = {
                "exists": path.is_file(),
                "sha256": actual_sha256,
                "expected_sha256": expected_sha256,
                "matches": actual_sha256 == expected_sha256,
            }
    checks["source_commit_matches"] = source_head == RVM_SOURCE_COMMIT
    checks["tracked_source_clean"] = tracked_source_clean
    checks["source_files_match"] = bool(source_files) and all(
        row["matches"] for row in source_files.values()
    )
    model_sha256 = _sha256(model) if model.is_file() else ""
    checks["model_sha256_matches"] = model_sha256 == RVM_MODEL_SHA256
    runtime = _probe_rvm_runtime(source_root, runtime_dependencies)
    checks["runtime_imports_ready"] = runtime.get("status") == "ready"
    checks["device_ready"] = (
        normalized_device == "cpu" or runtime.get("cuda_available") is True
    )
    status = "ready" if all(checks.values()) else "blocked_runtime_unavailable"
    return {
        "schema": "video_creation_pipeline.background_processing_doctor.v1",
        "status": status,
        "provider": {
            "name": "RobustVideoMatting",
            "repo_url": RVM_REPO_URL,
            "license": "GPL-3.0-external-provider",
            "source_root": str(source_root),
            "source_commit": source_head,
            "required_source_commit": RVM_SOURCE_COMMIT,
            "tracked_source_clean": tracked_source_clean,
            "source_files": source_files,
            "model_path": str(model),
            "model_name": RVM_MODEL_NAME,
            "model_sha256": model_sha256,
            "required_model_sha256": RVM_MODEL_SHA256,
            "runtime_dependencies_path": str(runtime_dependencies),
            "runtime": runtime,
            "device": normalized_device,
        },
        "checks": checks,
        "supported_modes": sorted(BACKGROUND_MODES),
        "authority_boundary": {
            "local_execution_only": True,
            "source_and_weights_external_to_distribution": True,
            "automatic_model_download": False,
            "automatic_provider_fallback": False,
            "candidate_only": True,
            "human_visual_review_required": True,
            "automatic_upload": False,
            "automatic_publish": False,
            "single_ffmpeg_outlet": FFMPEG_OUTLET_ID,
        },
    }


def build_background_processing_plan(
    source_video_path: str | Path,
    output_video_path: str | Path,
    provider_source_root: str | Path,
    model_path: str | Path,
    runtime_dependencies_path: str | Path,
    *,
    mode: str,
    device: str,
    background_image_path: str | Path | None = None,
    background_color: str = "#183153",
    blur_sigma: float = 18.0,
    blur_steps: int = 2,
    seq_chunk: int = 4,
    downsample_ratio: float | None = None,
) -> dict[str, Any]:
    source = Path(source_video_path).expanduser().resolve()
    output = Path(output_video_path).expanduser().resolve()
    source_root = Path(provider_source_root).expanduser().resolve()
    model = Path(model_path).expanduser().resolve()
    runtime_dependencies = Path(runtime_dependencies_path).expanduser().resolve()
    normalized_mode = _mode(mode)
    normalized_device = _device(device)
    _validate_output_extension(output, normalized_mode)
    if source == output:
        raise ContractError("background processing cannot overwrite its source")
    source_summary = _video_summary(probe_media_metadata(source))
    if source_summary["width"] % 2 or source_summary["height"] % 2:
        raise ContractError("background processing v1 requires even video dimensions")
    expected_frames = int(
        math.ceil(source_summary["duration_s"] * source_summary["fps"])
    )
    if expected_frames > MAX_V1_FRAME_COUNT:
        raise ContractError("background processing v1 supports at most 9999 frames")
    ratio = (
        round(
            min(512.0 / max(source_summary["width"], source_summary["height"]), 1.0), 6
        )
        if downsample_ratio is None
        else _ratio(downsample_ratio)
    )
    chunk = _bounded_int(seq_chunk, "RVM sequence chunk", 1, 16)
    doctor = background_processing_doctor(
        source_root,
        model,
        runtime_dependencies,
        device=normalized_device,
    )
    if doctor.get("status") != "ready":
        raise ContractError("background processing runtime is not ready")
    background = _background_spec(
        normalized_mode,
        background_image_path=background_image_path,
        background_color=background_color,
        blur_sigma=blur_sigma,
        blur_steps=blur_steps,
    )
    output_spec = _output_spec(output, normalized_mode)
    plan: dict[str, Any] = {
        "schema": BACKGROUND_PROCESSING_PLAN_SCHEMA,
        "source_video": artifact_reference(source, "background source video"),
        "source_video_summary": source_summary,
        "output": output_spec,
        "mode": normalized_mode,
        "background": background,
        "frame_contract": {
            "frame_rate": source_summary["fps"],
            "media_time_step_s": round(1.0 / source_summary["fps"], 9),
            "width": source_summary["width"],
            "height": source_summary["height"],
            "source_pixel_format": "rgb24",
            "source_pattern": "%06d.png",
            "alpha_pattern": "%04d.png",
            "maximum_frame_count": MAX_V1_FRAME_COUNT,
            "subject_presence_policy": "required_all_frames",
        },
        "provider": _provider_snapshot(
            doctor,
            seq_chunk=chunk,
            downsample_ratio=ratio,
        ),
        "execution_handoff": {
            "frame_extract_outlet": FFMPEG_OUTLET_ID,
            "matting_provider": "rvm_external_local_provider",
            "composite_outlet": FFMPEG_OUTLET_ID,
            "automatic_provider_fallback": False,
        },
        "authority_boundary": _plan_boundary(),
    }
    plan["plan_sha256"] = payload_sha256(plan, "plan_sha256")
    validate_background_processing_plan(plan)
    return plan


def validate_background_processing_plan(plan: dict[str, Any]) -> None:
    _validate_schema(
        plan,
        "background_processing_plan.v1.schema.json",
        "background processing plan",
    )
    if plan.get("plan_sha256") != payload_sha256(plan, "plan_sha256"):
        raise ContractError("background processing plan integrity check failed")
    source = validate_artifact_reference(
        _object(plan.get("source_video")), "background source video"
    )
    summary = _video_summary(probe_media_metadata(source))
    if plan.get("source_video_summary") != summary:
        raise ContractError("background source video summary drifted")
    mode = _mode(plan.get("mode"))
    output = _object(plan.get("output"))
    output_path = Path(str(output.get("path") or "")).expanduser().resolve()
    _validate_output_extension(output_path, mode)
    if output_path == source or output != _output_spec(output_path, mode):
        raise ContractError("background output specification drifted")
    expected_background = _background_spec_from_plan(mode, plan.get("background"))
    if plan.get("background") != expected_background:
        raise ContractError("background replacement specification drifted")
    frame_contract = _object(plan.get("frame_contract"))
    expected_frame_contract = {
        "frame_rate": summary["fps"],
        "media_time_step_s": round(1.0 / summary["fps"], 9),
        "width": summary["width"],
        "height": summary["height"],
        "source_pixel_format": "rgb24",
        "source_pattern": "%06d.png",
        "alpha_pattern": "%04d.png",
        "maximum_frame_count": MAX_V1_FRAME_COUNT,
        "subject_presence_policy": "required_all_frames",
    }
    if frame_contract != expected_frame_contract:
        raise ContractError("background frame contract drifted")
    provider = _object(plan.get("provider"))
    source_root = Path(str(provider.get("source_root") or "")).expanduser().resolve()
    model_path = validate_artifact_reference(
        _object(provider.get("model")), "RVM model"
    )
    runtime_dependencies = (
        Path(str(provider.get("runtime_dependencies_path") or ""))
        .expanduser()
        .resolve()
    )
    doctor = background_processing_doctor(
        source_root,
        model_path,
        runtime_dependencies,
        device=str(provider.get("device") or ""),
    )
    expected_provider = _provider_snapshot(
        doctor,
        seq_chunk=_bounded_int(provider.get("seq_chunk"), "RVM sequence chunk", 1, 16),
        downsample_ratio=_ratio(provider.get("downsample_ratio")),
    )
    if doctor.get("status") != "ready" or provider != expected_provider:
        raise ContractError("background processing Provider snapshot drifted")
    if plan.get("execution_handoff") != {
        "frame_extract_outlet": FFMPEG_OUTLET_ID,
        "matting_provider": "rvm_external_local_provider",
        "composite_outlet": FFMPEG_OUTLET_ID,
        "automatic_provider_fallback": False,
    }:
        raise ContractError("background processing execution handoff drifted")
    if plan.get("authority_boundary") != _plan_boundary():
        raise ContractError("background processing plan boundary is invalid")


def execute_background_processing(
    plan_path: str | Path,
    work_dir: str | Path,
    receipt_path: str | Path,
    *,
    timeout_s: float = 900.0,
) -> dict[str, Any]:
    plan_file = Path(plan_path).expanduser().resolve()
    plan = read_json_object(plan_file, "background processing plan")
    validate_background_processing_plan(plan)
    source = validate_artifact_reference(
        _object(plan.get("source_video")), "background source video"
    )
    output = Path(str(_object(plan.get("output")).get("path"))).resolve()
    root = Path(work_dir).expanduser().resolve()
    final_receipt = Path(receipt_path).expanduser().resolve()
    source_frames = root / "source-frames"
    alpha_frames = root / "alpha-frames"
    frame_receipt_path = root / "frame-extract-ffmpeg-receipt.json"
    provider_log_path = root / "rvm-provider.log"
    provider_failure_path = root / "rvm-provider-failure.json"
    manifest_path = root / "background-matting-manifest.json"
    provider_receipt_path = root / "background-matting-provider-receipt.json"
    alpha_qa_log_path = root / "alpha-statistics.log"
    alpha_qa_receipt_path = root / "alpha-qa-ffmpeg-receipt.json"
    staged = root / f"background-staging{output.suffix.casefold()}"
    composite_receipt_path = root / "background-composite-ffmpeg-receipt.json"
    protected_paths = [
        output,
        final_receipt,
        frame_receipt_path,
        provider_log_path,
        provider_failure_path,
        manifest_path,
        provider_receipt_path,
        alpha_qa_log_path,
        alpha_qa_receipt_path,
        staged,
    ]
    _validate_output_paths(source, plan_file, *protected_paths)
    _require_fresh_work_directory(root, source_frames, alpha_frames, protected_paths)
    root.mkdir(parents=True, exist_ok=True)
    source_frames.mkdir(parents=True, exist_ok=True)
    output.parent.mkdir(parents=True, exist_ok=True)

    frame_pattern = source_frames / str(plan["frame_contract"]["source_pattern"])
    frame_command = build_background_frame_extract_command(plan, source, frame_pattern)
    _frame_receipt, extracted_frames = execute_ffmpeg_frame_series_with_receipt(
        frame_command,
        [
            {"role": "source_video", "path": source},
            {"role": "background_processing_plan", "path": plan_file},
        ],
        frame_pattern,
        frame_receipt_path,
        media_time_step_s=float(plan["frame_contract"]["media_time_step_s"]),
        timeout_s=timeout_s,
    )
    if len(extracted_frames) > MAX_V1_FRAME_COUNT:
        raise ContractError("background frame extraction exceeded the v1 frame limit")

    alpha_frames.mkdir(parents=True, exist_ok=False)
    provider_command = build_rvm_provider_command(
        plan,
        source_frames,
        alpha_frames,
    )
    _execute_rvm_provider(
        plan_file,
        plan,
        provider_command,
        provider_log_path,
        provider_failure_path,
        timeout_s=timeout_s,
    )
    manifest = build_background_matting_manifest(
        plan_file,
        frame_receipt_path,
        extracted_frames,
        alpha_frames,
    )
    atomic_write_json(manifest, manifest_path)
    provider_receipt = build_background_matting_provider_receipt(
        plan_file,
        frame_receipt_path,
        manifest_path,
        provider_log_path,
        provider_command,
    )
    atomic_write_json(provider_receipt, provider_receipt_path)

    alpha_qa_command = build_background_alpha_qa_command(plan, alpha_frames)
    execute_ffmpeg_with_receipt(
        "technical_qa",
        alpha_qa_command,
        [{"role": "background_matting_manifest", "path": manifest_path}],
        [{"role": "alpha_statistics_log", "path": alpha_qa_log_path}],
        alpha_qa_receipt_path,
        capture_log_path=alpha_qa_log_path,
        timeout_s=timeout_s,
    )
    alpha_qa = _alpha_qa(alpha_qa_log_path, len(manifest["frames"]))
    if not alpha_qa["technically_valid"]:
        raise ContractError("background alpha sequence failed technical QA")

    composite_command = build_background_composite_command(
        plan,
        source,
        alpha_frames,
        staged,
        frame_count=len(manifest["frames"]),
    )
    composite_inputs = [
        {"role": "source_video", "path": source},
        {"role": "background_processing_plan", "path": plan_file},
        {"role": "background_matting_manifest", "path": manifest_path},
    ]
    if plan["mode"] == "replace_image":
        composite_inputs.append(
            {
                "role": "background_image",
                "path": validate_artifact_reference(
                    _object(plan["background"]["image"]), "replacement background"
                ),
            }
        )
    execute_ffmpeg_with_receipt(
        "render",
        composite_command,
        composite_inputs,
        [{"role": "background_processed_staging", "path": staged}],
        composite_receipt_path,
        timeout_s=timeout_s,
    )
    technical_qa = _technical_qa(plan, source, staged, alpha_qa)
    if not technical_qa["technically_valid"]:
        raise ContractError("background processed candidate failed technical QA")
    _atomic_promote(staged, output)
    receipt = build_background_processing_receipt(
        plan_file,
        frame_receipt_path,
        provider_receipt_path,
        manifest_path,
        alpha_qa_receipt_path,
        alpha_qa_log_path,
        composite_receipt_path,
        staged,
        output,
    )
    atomic_write_json(receipt, final_receipt)
    return receipt


def build_background_frame_extract_command(
    plan: dict[str, Any], source_path: str | Path, output_pattern: str | Path
) -> list[str]:
    validate_background_processing_plan(plan)
    source = Path(source_path).expanduser().resolve()
    pattern = Path(output_pattern).expanduser().resolve()
    fps = _format_number(plan["frame_contract"]["frame_rate"])
    return [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-nostats",
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-vf",
        f"fps={fps},format=rgb24",
        "-start_number",
        "0",
        str(pattern),
    ]


def build_rvm_provider_command(
    plan: dict[str, Any], source_frames_dir: str | Path, alpha_frames_dir: str | Path
) -> list[str]:
    validate_background_processing_plan(plan)
    provider = _object(plan.get("provider"))
    source_root = Path(str(provider["source_root"])).resolve()
    model = validate_artifact_reference(_object(provider["model"]), "RVM model")
    return [
        sys.executable,
        str(source_root / "inference.py"),
        "--variant",
        "mobilenetv3",
        "--checkpoint",
        str(model),
        "--device",
        str(provider["device"]),
        "--input-source",
        str(Path(source_frames_dir).expanduser().resolve()),
        "--output-type",
        "png_sequence",
        "--output-alpha",
        str(Path(alpha_frames_dir).expanduser().resolve()),
        "--downsample-ratio",
        _format_number(provider["downsample_ratio"]),
        "--seq-chunk",
        str(int(provider["seq_chunk"])),
        "--disable-progress",
    ]


def build_background_matting_manifest(
    plan_path: str | Path,
    frame_receipt_path: str | Path,
    source_frames: list[Path],
    alpha_frames_dir: str | Path,
) -> dict[str, Any]:
    plan_file = Path(plan_path).expanduser().resolve()
    plan = read_json_object(plan_file, "background processing plan")
    validate_background_processing_plan(plan)
    frame_receipt_file = Path(frame_receipt_path).expanduser().resolve()
    frame_receipt = read_json_object(frame_receipt_file, "frame extraction receipt")
    validate_ffmpeg_execution_receipt(frame_receipt)
    if frame_receipt.get("operation") != "frame_extract":
        raise ContractError("background matting requires a frame_extract receipt")
    alpha_paths = _numbered_png_files(Path(alpha_frames_dir).expanduser().resolve())
    if len(source_frames) != len(alpha_paths) or not source_frames:
        raise ContractError(
            "RVM alpha frame count differs from extracted source frames"
        )
    step = float(plan["frame_contract"]["media_time_step_s"])
    rows = [
        {
            "frame_id": f"frame-{index:06d}",
            "media_time_s": round(index * step, 9),
            "source_frame": artifact_reference(source_frame, "RVM source frame"),
            "alpha_frame": artifact_reference(alpha_frame, "RVM alpha frame"),
        }
        for index, (source_frame, alpha_frame) in enumerate(
            zip(source_frames, alpha_paths, strict=True)
        )
    ]
    manifest: dict[str, Any] = {
        "schema": BACKGROUND_MATTING_MANIFEST_SCHEMA,
        "source_plan": artifact_reference(plan_file, "background processing plan"),
        "source_plan_sha256": plan["plan_sha256"],
        "frame_extract_receipt": artifact_reference(
            frame_receipt_file, "background frame extraction receipt"
        ),
        "frame_count": len(rows),
        "frames": rows,
        "authority_boundary": {
            "candidate_evidence_only": True,
            "source_media_modified": False,
            "automatic_upload": False,
            "automatic_publish": False,
        },
    }
    manifest["manifest_sha256"] = payload_sha256(manifest, "manifest_sha256")
    validate_background_matting_manifest(manifest)
    return manifest


def validate_background_matting_manifest(manifest: dict[str, Any]) -> None:
    if manifest.get("schema") != BACKGROUND_MATTING_MANIFEST_SCHEMA:
        raise ContractError("unsupported background matting manifest schema")
    if manifest.get("manifest_sha256") != payload_sha256(manifest, "manifest_sha256"):
        raise ContractError("background matting manifest integrity check failed")
    plan_path = validate_artifact_reference(
        _object(manifest.get("source_plan")), "background processing plan"
    )
    plan = read_json_object(plan_path, "background processing plan")
    validate_background_processing_plan(plan)
    if manifest.get("source_plan_sha256") != plan.get("plan_sha256"):
        raise ContractError("background matting manifest binds a stale plan")
    receipt_path = validate_artifact_reference(
        _object(manifest.get("frame_extract_receipt")),
        "background frame extraction receipt",
    )
    receipt = read_json_object(receipt_path, "background frame extraction receipt")
    validate_ffmpeg_execution_receipt(receipt)
    if receipt.get("operation") != "frame_extract":
        raise ContractError("background frame receipt operation drifted")
    receipt_frames = [
        row
        for row in receipt.get("outputs") or []
        if isinstance(row, dict) and row.get("role") == "frame"
    ]
    rows = manifest.get("frames") if isinstance(manifest.get("frames"), list) else []
    if manifest.get("frame_count") != len(rows) or len(rows) != len(receipt_frames):
        raise ContractError("background matting frame count drifted")
    step = float(plan["frame_contract"]["media_time_step_s"])
    for index, row in enumerate(rows):
        value = _object(row)
        if value.get("frame_id") != f"frame-{index:06d}" or value.get(
            "media_time_s"
        ) != round(index * step, 9):
            raise ContractError("background matting frame identity drifted")
        source_frame = validate_artifact_reference(
            _object(value.get("source_frame")), "RVM source frame"
        )
        alpha_frame = validate_artifact_reference(
            _object(value.get("alpha_frame")), "RVM alpha frame"
        )
        receipt_source = validate_artifact_reference(
            _object(receipt_frames[index].get("artifact")), "FFmpeg source frame"
        )
        if source_frame != receipt_source or alpha_frame.suffix.casefold() != ".png":
            raise ContractError("background matting frame provenance drifted")
    if manifest.get("authority_boundary") != {
        "candidate_evidence_only": True,
        "source_media_modified": False,
        "automatic_upload": False,
        "automatic_publish": False,
    }:
        raise ContractError("background matting manifest boundary is invalid")


def build_background_matting_provider_receipt(
    plan_path: str | Path,
    frame_receipt_path: str | Path,
    manifest_path: str | Path,
    provider_log_path: str | Path,
    command: list[str],
) -> dict[str, Any]:
    plan_file = Path(plan_path).expanduser().resolve()
    plan = read_json_object(plan_file, "background processing plan")
    validate_background_processing_plan(plan)
    receipt: dict[str, Any] = {
        "schema": BACKGROUND_MATTING_PROVIDER_RECEIPT_SCHEMA,
        "status": "completed",
        "source_plan": artifact_reference(plan_file, "background processing plan"),
        "source_plan_sha256": plan["plan_sha256"],
        "provider": dict(plan["provider"]),
        "command": [str(value) for value in command],
        "frame_extract_receipt": artifact_reference(
            frame_receipt_path, "background frame extraction receipt"
        ),
        "matting_manifest": artifact_reference(
            manifest_path, "background matting manifest"
        ),
        "provider_log": artifact_reference(provider_log_path, "RVM provider log"),
        "execution_boundary": _provider_boundary(),
    }
    receipt["receipt_sha256"] = payload_sha256(receipt, "receipt_sha256")
    validate_background_matting_provider_receipt(receipt)
    return receipt


def validate_background_matting_provider_receipt(receipt: dict[str, Any]) -> None:
    if receipt.get("schema") != BACKGROUND_MATTING_PROVIDER_RECEIPT_SCHEMA:
        raise ContractError("unsupported background matting Provider receipt schema")
    if receipt.get("status") != "completed" or receipt.get(
        "receipt_sha256"
    ) != payload_sha256(receipt, "receipt_sha256"):
        raise ContractError("background matting Provider receipt integrity failed")
    plan_path = validate_artifact_reference(
        _object(receipt.get("source_plan")), "background processing plan"
    )
    plan = read_json_object(plan_path, "background processing plan")
    validate_background_processing_plan(plan)
    if receipt.get("source_plan_sha256") != plan.get("plan_sha256") or receipt.get(
        "provider"
    ) != plan.get("provider"):
        raise ContractError("background matting Provider receipt binds a stale plan")
    validate_artifact_reference(
        _object(receipt.get("frame_extract_receipt")),
        "background frame extraction receipt",
    )
    manifest_path = validate_artifact_reference(
        _object(receipt.get("matting_manifest")), "background matting manifest"
    )
    validate_background_matting_manifest(
        read_json_object(manifest_path, "background matting manifest")
    )
    validate_artifact_reference(
        _object(receipt.get("provider_log")), "RVM provider log"
    )
    manifest = read_json_object(manifest_path, "background matting manifest")
    alpha_parent = Path(str(manifest["frames"][0]["alpha_frame"]["path"])).parent
    source_parent = Path(str(manifest["frames"][0]["source_frame"]["path"])).parent
    if receipt.get("command") != build_rvm_provider_command(
        plan, source_parent, alpha_parent
    ):
        raise ContractError("background matting Provider command drifted")
    if receipt.get("execution_boundary") != _provider_boundary():
        raise ContractError("background matting Provider boundary is invalid")


def build_background_alpha_qa_command(
    plan: dict[str, Any], alpha_frames_dir: str | Path
) -> list[str]:
    validate_background_processing_plan(plan)
    alpha_pattern = Path(alpha_frames_dir).resolve() / str(
        plan["frame_contract"]["alpha_pattern"]
    )
    return [
        "ffmpeg",
        "-hide_banner",
        "-nostats",
        "-v",
        "info",
        "-framerate",
        _format_number(plan["frame_contract"]["frame_rate"]),
        "-start_number",
        "0",
        "-i",
        str(alpha_pattern),
        "-vf",
        "signalstats,metadata=print:file=-",
        "-f",
        "null",
        "-",
    ]


def build_background_composite_command(
    plan: dict[str, Any],
    source_path: str | Path,
    alpha_frames_dir: str | Path,
    output_path: str | Path,
    *,
    frame_count: int,
) -> list[str]:
    validate_background_processing_plan(plan)
    source = Path(source_path).expanduser().resolve()
    output = Path(output_path).expanduser().resolve()
    alpha_pattern = Path(alpha_frames_dir).resolve() / str(
        plan["frame_contract"]["alpha_pattern"]
    )
    count = _bounded_int(frame_count, "background frame count", 1, MAX_V1_FRAME_COUNT)
    fps = _format_number(plan["frame_contract"]["frame_rate"])
    width = int(plan["frame_contract"]["width"])
    height = int(plan["frame_contract"]["height"])
    command = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-nostats",
        "-i",
        str(source),
        "-framerate",
        fps,
        "-start_number",
        "0",
        "-i",
        str(alpha_pattern),
    ]
    mode = str(plan["mode"])
    if mode == "replace_image":
        image = validate_artifact_reference(
            _object(plan["background"]["image"]), "replacement background"
        )
        command.extend(["-loop", "1", "-framerate", fps, "-i", str(image)])
    filter_graph = _composite_filter_graph(plan, width=width, height=height, fps=fps)
    command.extend(["-filter_complex", filter_graph, "-map", "[outv]", "-map", "0:a?"])
    if mode == "transparent":
        command.extend(
            [
                "-c:v",
                "prores_ks",
                "-profile:v",
                "4",
                "-pix_fmt",
                "yuva444p10le",
            ]
        )
    else:
        command.extend(
            [
                "-c:v",
                "libx264",
                "-preset",
                "medium",
                "-crf",
                "18",
                "-pix_fmt",
                "yuv420p",
            ]
        )
    command.extend(
        [
            "-c:a",
            "copy",
            "-frames:v",
            str(count),
            "-map_metadata",
            "-1",
            "-fflags",
            "+bitexact",
            "-flags:v",
            "+bitexact",
            "-threads",
            "1",
            str(output),
        ]
    )
    return command


def build_background_processing_receipt(
    plan_path: str | Path,
    frame_receipt_path: str | Path,
    provider_receipt_path: str | Path,
    manifest_path: str | Path,
    alpha_qa_receipt_path: str | Path,
    alpha_qa_log_path: str | Path,
    composite_receipt_path: str | Path,
    staged_video_path: str | Path,
    output_video_path: str | Path,
) -> dict[str, Any]:
    plan_file = Path(plan_path).expanduser().resolve()
    plan = read_json_object(plan_file, "background processing plan")
    validate_background_processing_plan(plan)
    source = validate_artifact_reference(
        _object(plan.get("source_video")), "background source video"
    )
    manifest_file = Path(manifest_path).expanduser().resolve()
    manifest = read_json_object(manifest_file, "background matting manifest")
    validate_background_matting_manifest(manifest)
    alpha_log = Path(alpha_qa_log_path).expanduser().resolve()
    alpha_qa = _alpha_qa(alpha_log, int(manifest["frame_count"]))
    staged = Path(staged_video_path).expanduser().resolve()
    output = Path(output_video_path).expanduser().resolve()
    receipt: dict[str, Any] = {
        "schema": BACKGROUND_PROCESSING_RECEIPT_SCHEMA,
        "status": "completed_technical_candidate_needs_visual_review",
        "source_plan": artifact_reference(plan_file, "background processing plan"),
        "source_plan_sha256": plan["plan_sha256"],
        "frame_extract_receipt": artifact_reference(
            frame_receipt_path, "background frame extraction receipt"
        ),
        "provider_receipt": artifact_reference(
            provider_receipt_path, "background matting Provider receipt"
        ),
        "matting_manifest": artifact_reference(
            manifest_file, "background matting manifest"
        ),
        "alpha_qa_receipt": artifact_reference(
            alpha_qa_receipt_path, "background alpha QA receipt"
        ),
        "alpha_qa_log": artifact_reference(alpha_log, "background alpha QA log"),
        "composite_ffmpeg_receipt": artifact_reference(
            composite_receipt_path, "background composite FFmpeg receipt"
        ),
        "staged_video": artifact_reference(staged, "background staged video"),
        "processed_video": artifact_reference(output, "background processed video"),
        "mode": plan["mode"],
        "technical_qa": _technical_qa(plan, source, output, alpha_qa),
        "review": _review_boundary(),
        "authority_boundary": _receipt_boundary(),
    }
    receipt["receipt_sha256"] = payload_sha256(receipt, "receipt_sha256")
    validate_background_processing_receipt(receipt)
    return receipt


def validate_background_processing_receipt(receipt: dict[str, Any]) -> None:
    _validate_schema(
        receipt,
        "background_processing_receipt.v1.schema.json",
        "background processing receipt",
    )
    if receipt.get("receipt_sha256") != payload_sha256(receipt, "receipt_sha256"):
        raise ContractError("background processing receipt integrity check failed")
    plan_path = validate_artifact_reference(
        _object(receipt.get("source_plan")), "background processing plan"
    )
    plan = read_json_object(plan_path, "background processing plan")
    validate_background_processing_plan(plan)
    if receipt.get("source_plan_sha256") != plan.get("plan_sha256"):
        raise ContractError("background processing receipt binds a stale plan")
    frame_receipt_path = validate_artifact_reference(
        _object(receipt.get("frame_extract_receipt")),
        "background frame extraction receipt",
    )
    frame_receipt = read_json_object(frame_receipt_path, "frame extraction receipt")
    validate_ffmpeg_execution_receipt(frame_receipt)
    if frame_receipt.get("operation") != "frame_extract":
        raise ContractError("background processing used the wrong frame operation")
    provider_receipt_path = validate_artifact_reference(
        _object(receipt.get("provider_receipt")),
        "background matting Provider receipt",
    )
    validate_background_matting_provider_receipt(
        read_json_object(provider_receipt_path, "background Provider receipt")
    )
    manifest_path = validate_artifact_reference(
        _object(receipt.get("matting_manifest")), "background matting manifest"
    )
    manifest = read_json_object(manifest_path, "background matting manifest")
    validate_background_matting_manifest(manifest)
    alpha_qa_receipt_path = validate_artifact_reference(
        _object(receipt.get("alpha_qa_receipt")), "background alpha QA receipt"
    )
    alpha_qa_receipt = read_json_object(alpha_qa_receipt_path, "alpha QA receipt")
    validate_ffmpeg_execution_receipt(alpha_qa_receipt)
    alpha_log = validate_artifact_reference(
        _object(receipt.get("alpha_qa_log")), "background alpha QA log"
    )
    if alpha_qa_receipt.get("operation") != "technical_qa":
        raise ContractError("background alpha QA operation drifted")
    composite_receipt_path = validate_artifact_reference(
        _object(receipt.get("composite_ffmpeg_receipt")),
        "background composite FFmpeg receipt",
    )
    composite_receipt = read_json_object(
        composite_receipt_path, "background composite FFmpeg receipt"
    )
    validate_ffmpeg_execution_receipt(composite_receipt)
    if composite_receipt.get("operation") != "render":
        raise ContractError("background composite FFmpeg operation drifted")
    staged = validate_artifact_reference(
        _object(receipt.get("staged_video")), "background staged video"
    )
    output = validate_artifact_reference(
        _object(receipt.get("processed_video")), "background processed video"
    )
    if output != Path(str(plan["output"]["path"])).resolve() or _object(
        receipt.get("staged_video")
    ).get("sha256") != _object(receipt.get("processed_video")).get("sha256"):
        raise ContractError("background output differs from its staged candidate")
    source = validate_artifact_reference(
        _object(plan.get("source_video")), "background source video"
    )
    alpha_parent = Path(str(manifest["frames"][0]["alpha_frame"]["path"])).parent
    if composite_receipt.get("command") != build_background_composite_command(
        plan,
        source,
        alpha_parent,
        staged,
        frame_count=int(manifest["frame_count"]),
    ):
        raise ContractError("background composite FFmpeg command drifted")
    alpha_qa = _alpha_qa(alpha_log, int(manifest["frame_count"]))
    if receipt.get("technical_qa") != _technical_qa(plan, source, output, alpha_qa):
        raise ContractError("background processing technical QA drifted")
    if receipt.get("mode") != plan.get("mode"):
        raise ContractError("background processing mode drifted")
    if receipt.get("review") != _review_boundary():
        raise ContractError("background processing review boundary drifted")
    if receipt.get("authority_boundary") != _receipt_boundary():
        raise ContractError("background processing receipt boundary is invalid")


def write_background_processing_contract(
    payload: dict[str, Any], output_path: str | Path
) -> Path:
    schema = payload.get("schema")
    if schema == BACKGROUND_PROCESSING_PLAN_SCHEMA:
        validate_background_processing_plan(payload)
    elif schema == BACKGROUND_PROCESSING_RECEIPT_SCHEMA:
        validate_background_processing_receipt(payload)
    else:
        raise ContractError("unsupported background processing contract")
    return atomic_write_json(payload, output_path)


def _execute_rvm_provider(
    plan_path: Path,
    plan: dict[str, Any],
    command: list[str],
    log_path: Path,
    failure_path: Path,
    *,
    timeout_s: float,
) -> None:
    provider = _object(plan.get("provider"))
    source_root = Path(str(provider["source_root"])).resolve()
    runtime_dependencies = Path(str(provider["runtime_dependencies_path"])).resolve()
    env = os.environ.copy()
    entries = [str(runtime_dependencies), str(source_root)]
    if env.get("PYTHONPATH"):
        entries.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(entries)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        result = subprocess.run(
            command,
            cwd=source_root,
            env=env,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=float(timeout_s),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        _write_provider_failure(plan_path, plan, command, failure_path, str(exc))
        raise ContractError(f"RVM Provider execution failed: {exc}") from exc
    log_path.write_text((result.stderr or "") + (result.stdout or ""), encoding="utf-8")
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()[-2000:]
        _write_provider_failure(plan_path, plan, command, failure_path, detail)
        raise ContractError(
            f"RVM Provider returned exit code {result.returncode}: {detail}"
        )


def _write_provider_failure(
    plan_path: Path,
    plan: dict[str, Any],
    command: list[str],
    output_path: Path,
    error: str,
) -> None:
    failure: dict[str, Any] = {
        "schema": BACKGROUND_MATTING_PROVIDER_FAILURE_SCHEMA,
        "status": "failed_visible_operator_review_required",
        "source_plan": artifact_reference(plan_path, "background processing plan"),
        "source_plan_sha256": plan["plan_sha256"],
        "provider": dict(plan["provider"]),
        "command": [str(value) for value in command],
        "error": str(error)[-2000:],
        "execution_boundary": _provider_boundary(),
    }
    failure["failure_sha256"] = payload_sha256(failure, "failure_sha256")
    atomic_write_json(failure, output_path)


def _alpha_qa(log_path: Path, expected_frame_count: int) -> dict[str, Any]:
    text = log_path.read_text(encoding="utf-8", errors="replace")
    minima = [float(value) for value in re.findall(r"YMIN=([0-9.]+)", text)]
    averages = [float(value) for value in re.findall(r"YAVG=([0-9.]+)", text)]
    maxima = [float(value) for value in re.findall(r"YMAX=([0-9.]+)", text)]
    count = min(len(minima), len(averages), len(maxima))
    minima = minima[:count]
    averages = averages[:count]
    maxima = maxima[:count]
    empty_frames = sum(1 for value in maxima if value <= 1.0)
    full_frames = sum(1 for value in minima if value >= 254.0)
    area_proxy = [value / 255.0 for value in averages]
    deltas = [abs(right - left) for left, right in zip(area_proxy, area_proxy[1:])]
    result = {
        "frame_count": count,
        "expected_frame_count": expected_frame_count,
        "frame_count_matches": count == expected_frame_count,
        "empty_frame_count": empty_frames,
        "full_frame_count": full_frames,
        "subject_present_all_frames": count > 0 and empty_frames == 0,
        "alpha_min": min(minima, default=0.0),
        "alpha_max": max(maxima, default=0.0),
        "alpha_mean_average": round(sum(averages) / count, 6) if count else 0.0,
        "mean_area_delta": round(sum(deltas) / len(deltas), 6) if deltas else 0.0,
        "max_area_delta": round(max(deltas), 6) if deltas else 0.0,
        "temporal_edge_quality_claimed": False,
        "technically_valid": count == expected_frame_count
        and count > 0
        and empty_frames == 0
        and max(maxima, default=0.0) > 1.0,
    }
    return result


def _technical_qa(
    plan: dict[str, Any], source_path: Path, output_path: Path, alpha_qa: dict[str, Any]
) -> dict[str, Any]:
    source = _video_summary(probe_media_metadata(source_path))
    output = _video_summary(probe_media_metadata(output_path))
    duration_delta = round(abs(output["duration_s"] - source["duration_s"]), 6)
    fps_matches = abs(output["fps"] - source["fps"]) <= 0.1
    dimensions_match = (
        output["width"] == source["width"] and output["height"] == source["height"]
    )
    audio_preserved = output["has_audio"] == source["has_audio"]
    duration_tolerance = round(max(0.2, 1.0 / source["fps"] + 0.05), 6)
    transparent_alpha = plan["mode"] != "transparent" or output[
        "pixel_format"
    ].startswith("yuva")
    codec_matches = (
        output["video_codec"] == "prores"
        if plan["mode"] == "transparent"
        else output["video_codec"] == "h264"
    )
    technically_valid = all(
        (
            alpha_qa["technically_valid"],
            dimensions_match,
            fps_matches,
            duration_delta <= duration_tolerance,
            audio_preserved,
            transparent_alpha,
            codec_matches,
        )
    )
    return {
        "source_video": source,
        "processed_video": output,
        "alpha_sequence": alpha_qa,
        "dimensions_match": dimensions_match,
        "frame_rate_matches": fps_matches,
        "duration_delta_s": duration_delta,
        "duration_tolerance_s": duration_tolerance,
        "duration_within_tolerance": duration_delta <= duration_tolerance,
        "audio_presence_preserved": audio_preserved,
        "transparent_alpha_present": transparent_alpha,
        "codec_matches_mode": codec_matches,
        "visual_quality_claimed": False,
        "technically_valid": technically_valid,
    }


def _video_summary(metadata: dict[str, Any]) -> dict[str, Any]:
    streams = [row for row in metadata.get("streams") or [] if isinstance(row, dict)]
    video = next((row for row in streams if row.get("codec_type") == "video"), None)
    if video is None:
        raise ContractError("background processing source has no video stream")
    audio = next((row for row in streams if row.get("codec_type") == "audio"), None)
    format_payload = _object(metadata.get("format"))
    duration = _positive_number(
        video.get("duration") or format_payload.get("duration"), "video duration"
    )
    fps = _frame_rate(video)
    width = int(_positive_number(video.get("width"), "video width"))
    height = int(_positive_number(video.get("height"), "video height"))
    return {
        "duration_s": round(duration, 6),
        "width": width,
        "height": height,
        "fps": round(fps, 6),
        "video_codec": str(video.get("codec_name") or ""),
        "pixel_format": str(video.get("pix_fmt") or ""),
        "has_audio": audio is not None,
        "audio_codec": str(audio.get("codec_name") or "") if audio else "",
        "audio_sample_rate": int(audio.get("sample_rate") or 0) if audio else 0,
        "audio_channels": int(audio.get("channels") or 0) if audio else 0,
    }


def _frame_rate(video: dict[str, Any]) -> float:
    for field in ("avg_frame_rate", "r_frame_rate"):
        value = str(video.get(field) or "")
        if "/" in value:
            numerator, denominator = value.split("/", 1)
            denominator_value = float(denominator)
            if denominator_value:
                result = float(numerator) / denominator_value
                if math.isfinite(result) and result > 0:
                    return result
    raise ContractError("background processing video frame rate is invalid")


def _provider_snapshot(
    doctor: dict[str, Any], *, seq_chunk: int, downsample_ratio: float
) -> dict[str, Any]:
    provider = _object(doctor.get("provider"))
    return {
        "name": "RobustVideoMatting",
        "repo_url": RVM_REPO_URL,
        "license": "GPL-3.0-external-provider",
        "source_root": str(provider.get("source_root") or ""),
        "source_commit": RVM_SOURCE_COMMIT,
        "source_files": dict(provider.get("source_files") or {}),
        "model": artifact_reference(provider.get("model_path"), "RVM model"),
        "runtime_dependencies_path": str(
            provider.get("runtime_dependencies_path") or ""
        ),
        "runtime": dict(provider.get("runtime") or {}),
        "device": str(provider.get("device") or ""),
        "variant": "mobilenetv3",
        "seq_chunk": seq_chunk,
        "downsample_ratio": downsample_ratio,
        "interface": "upstream_inference_png_sequence",
    }


def _probe_rvm_runtime(source_root: Path, runtime_dependencies: Path) -> dict[str, Any]:
    if not source_root.is_dir() or not runtime_dependencies.is_dir():
        return {"status": "blocked_missing_runtime_path"}
    code = (
        "import json, torch, torchvision, pims, slicerator, imageio; "
        "print(json.dumps({'torch': torch.__version__, "
        "'torchvision': torchvision.__version__, "
        "'pims': pims.__version__, 'slicerator': slicerator.__version__, "
        "'imageio': imageio.__version__, "
        "'cuda_available': torch.cuda.is_available(), "
        "'cuda_runtime': torch.version.cuda}))"
    )
    env = os.environ.copy()
    entries = [str(runtime_dependencies), str(source_root)]
    if env.get("PYTHONPATH"):
        entries.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(entries)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=source_root,
            env=env,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=45.0,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"status": "blocked_import_failure", "error": str(exc)}
    if result.returncode != 0:
        return {
            "status": "blocked_import_failure",
            "error": (result.stderr or result.stdout or "").strip()[-2000:],
        }
    try:
        payload = json.loads((result.stdout or "").strip())
    except json.JSONDecodeError as exc:
        return {"status": "blocked_import_failure", "error": str(exc)}
    return {"status": "ready", **payload}


def _background_spec(
    mode: str,
    *,
    background_image_path: str | Path | None,
    background_color: str,
    blur_sigma: float,
    blur_steps: int,
) -> dict[str, Any]:
    if mode == "blur":
        return {
            "kind": "blur",
            "sigma": _bounded_float(blur_sigma, "background blur sigma", 1.0, 100.0),
            "steps": _bounded_int(blur_steps, "background blur steps", 1, 6),
        }
    if mode == "replace_color":
        return {"kind": "color", "color": _hex_color(background_color)}
    if mode == "replace_image":
        if background_image_path is None:
            raise ContractError("replace_image mode requires a background image")
        image = Path(background_image_path).expanduser().resolve()
        return {
            "kind": "image",
            "image": artifact_reference(image, "replacement background"),
            "fit": "cover_center",
        }
    if background_image_path is not None:
        raise ContractError("transparent mode does not accept a background image")
    return {"kind": "transparent"}


def _background_spec_from_plan(mode: str, value: Any) -> dict[str, Any]:
    background = _object(value)
    if mode == "blur":
        return _background_spec(
            mode,
            background_image_path=None,
            background_color="#183153",
            blur_sigma=background.get("sigma"),
            blur_steps=background.get("steps"),
        )
    if mode == "replace_color":
        return _background_spec(
            mode,
            background_image_path=None,
            background_color=str(background.get("color") or ""),
            blur_sigma=18.0,
            blur_steps=2,
        )
    if mode == "replace_image":
        image_path = validate_artifact_reference(
            _object(background.get("image")), "replacement background"
        )
        return _background_spec(
            mode,
            background_image_path=image_path,
            background_color="#183153",
            blur_sigma=18.0,
            blur_steps=2,
        )
    return _background_spec(
        mode,
        background_image_path=None,
        background_color="#183153",
        blur_sigma=18.0,
        blur_steps=2,
    )


def _output_spec(output: Path, mode: str) -> dict[str, Any]:
    if mode == "transparent":
        return {
            "path": str(output),
            "container": "mov",
            "video_codec": "prores_ks",
            "pixel_format": "yuva444p10le",
            "audio_mode": "copy_if_present",
        }
    return {
        "path": str(output),
        "container": "mp4",
        "video_codec": "libx264",
        "pixel_format": "yuv420p",
        "audio_mode": "copy_if_present",
    }


def _composite_filter_graph(
    plan: dict[str, Any], *, width: int, height: int, fps: str
) -> str:
    mode = str(plan["mode"])
    if mode == "blur":
        sigma = _format_number(plan["background"]["sigma"])
        steps = int(plan["background"]["steps"])
        return (
            "[0:v]split=2[fgsrc][bg];"
            f"[bg]gblur=sigma={sigma}:steps={steps}[background];"
            "[1:v]format=gray[mask];"
            "[fgsrc][mask]alphamerge[foreground];"
            "[background][foreground]overlay=format=auto,format=yuv420p[outv]"
        )
    if mode == "replace_color":
        color = str(plan["background"]["color"]).replace("#", "0x")
        return (
            f"color=c={color}:s={width}x{height}:r={fps}[background];"
            "[1:v]format=gray[mask];"
            "[0:v][mask]alphamerge[foreground];"
            "[background][foreground]overlay=format=auto,format=yuv420p[outv]"
        )
    if mode == "replace_image":
        return (
            f"[2:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height}[background];"
            "[1:v]format=gray[mask];"
            "[0:v][mask]alphamerge[foreground];"
            "[background][foreground]overlay=format=auto,format=yuv420p[outv]"
        )
    return "[1:v]format=gray[mask];[0:v][mask]alphamerge,format=yuva444p10le[outv]"


def _plan_boundary() -> dict[str, Any]:
    return {
        "candidate_only": True,
        "source_video_modified": False,
        "provider_source_and_weights_external": True,
        "automatic_model_download": False,
        "automatic_provider_fallback": False,
        "real_person_quality_claimed": False,
        "human_visual_review_required": True,
        "release_eligible": False,
        "automatic_upload": False,
        "automatic_publish": False,
        "single_ffmpeg_outlet": FFMPEG_OUTLET_ID,
    }


def _provider_boundary() -> dict[str, Any]:
    return {
        "local_execution_only": True,
        "external_network_used": False,
        "model_downloaded_during_execution": False,
        "fallback_used": False,
        "automatic_provider_fallback": False,
        "source_media_modified": False,
        "video_encoded_by_provider": False,
        "automatic_upload": False,
        "automatic_publish": False,
    }


def _review_boundary() -> dict[str, Any]:
    return {
        "status": "needs_visual_review",
        "required_checks": [
            "hair_and_fine_edges",
            "hands_and_fast_motion",
            "temporal_flicker",
            "foreground_halo",
            "background_leakage",
            "replacement_background_rights_and_privacy",
        ],
        "automatic_release": False,
    }


def _receipt_boundary() -> dict[str, Any]:
    return {
        "candidate_only": True,
        "technical_processing_completed": True,
        "human_visual_review_required": True,
        "creative_approval_claimed": False,
        "real_person_gold_claimed": False,
        "release_eligible": False,
        "automatic_upload": False,
        "automatic_publish": False,
        "single_ffmpeg_outlet": FFMPEG_OUTLET_ID,
        "second_video_encoder_added": False,
        "second_state_machine_added": False,
    }


def _require_fresh_work_directory(
    root: Path,
    source_frames: Path,
    alpha_frames: Path,
    output_paths: list[Path],
) -> None:
    if source_frames.exists() or alpha_frames.exists():
        raise ContractError(
            "background processing work directory contains frame output"
        )
    if any(path.exists() for path in output_paths):
        raise ContractError(
            "background processing work directory contains stale output"
        )
    if root.exists() and not root.is_dir():
        raise ContractError("background processing work directory is not a directory")


def _validate_output_paths(source: Path, plan: Path, *outputs: Path) -> None:
    protected = {source, plan}
    seen: set[Path] = set()
    for path in outputs:
        if path in protected:
            raise ContractError("background processing cannot overwrite a bound input")
        if path in seen:
            raise ContractError("background processing output paths must be distinct")
        seen.add(path)


def _atomic_promote(staged_path: Path, output_path: Path) -> None:
    if not staged_path.is_file():
        raise ContractError("background processing staging output was not created")
    temporary = output_path.with_name(f".{output_path.name}.{uuid4().hex}.tmp")
    try:
        shutil.copyfile(staged_path, temporary)
        temporary.replace(output_path)
    except OSError as exc:
        raise ContractError(f"background output promotion failed: {exc}") from exc
    finally:
        if temporary.exists():
            temporary.unlink()


def _numbered_png_files(directory: Path) -> list[Path]:
    if not directory.is_dir():
        raise ContractError("RVM alpha output directory is missing")
    rows: list[tuple[int, Path]] = []
    for path in directory.iterdir():
        if path.is_file() and path.suffix.casefold() == ".png" and path.stem.isdigit():
            rows.append((int(path.stem), path.resolve()))
    rows.sort(key=lambda row: row[0])
    if [index for index, _path in rows] != list(range(len(rows))):
        raise ContractError("RVM alpha frames are not a contiguous zero-based sequence")
    return [path for _index, path in rows]


def _git_output(root: Path, arguments: list[str]) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *arguments],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=20.0,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return (result.stdout or "").strip() if result.returncode == 0 else ""


def _git_clean(root: Path) -> bool:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "diff", "--quiet", "--exit-code"],
            capture_output=True,
            timeout=20.0,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _media_tool_available(name: str) -> bool:
    try:
        return bool(resolve_media_tool(name))
    except (ContractError, OSError, ValueError):
        return False


def _sha256(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_schema(payload: dict[str, Any], filename: str, label: str) -> None:
    schema = read_json_object(Path(__file__).with_name("schemas") / filename, label)
    try:
        validator = Draft202012Validator(schema)
        validator.check_schema(schema)
        validator.validate(payload)
    except (SchemaError, ValidationError) as exc:
        raise ContractError(f"{label} schema validation failed: {exc.message}") from exc


def _mode(value: Any) -> str:
    mode = str(value or "").strip()
    if mode not in BACKGROUND_MODES:
        raise ContractError("unsupported background processing mode")
    return mode


def _device(value: Any) -> str:
    device = str(value or "").strip().casefold()
    if device not in {"cpu", "cuda"}:
        raise ContractError("background processing device must be cpu or cuda")
    return device


def _hex_color(value: Any) -> str:
    color = str(value or "").strip().upper()
    if not re.fullmatch(r"#[0-9A-F]{6}", color):
        raise ContractError("background replacement color must be #RRGGBB")
    return color


def _validate_output_extension(output: Path, mode: str) -> None:
    expected = ".mov" if mode == "transparent" else ".mp4"
    if output.suffix.casefold() != expected:
        raise ContractError(f"background {mode} output must use {expected}")


def _ratio(value: Any) -> float:
    return round(_bounded_float(value, "RVM downsample ratio", 0.01, 1.0), 6)


def _bounded_float(value: Any, label: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool):
        raise ContractError(f"{label} must be a finite number")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{label} must be a finite number") from exc
    if not math.isfinite(number) or number < minimum or number > maximum:
        raise ContractError(f"{label} must be between {minimum} and {maximum}")
    return round(number, 6)


def _bounded_int(value: Any, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise ContractError(f"{label} must be an integer")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{label} must be an integer") from exc
    if number < minimum or number > maximum:
        raise ContractError(f"{label} must be between {minimum} and {maximum}")
    return number


def _positive_number(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise ContractError(f"{label} must be a positive finite number")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{label} must be a positive finite number") from exc
    if not math.isfinite(number) or number <= 0:
        raise ContractError(f"{label} must be a positive finite number")
    return number


def _format_number(value: Any) -> str:
    number = _positive_number(value, "FFmpeg numeric value")
    return f"{number:.6f}"


def _object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}
