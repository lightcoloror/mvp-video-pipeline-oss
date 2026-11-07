from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .contract_io import (
    artifact_reference,
    atomic_write_json,
    payload_sha256,
    read_json_object,
    validate_artifact_reference,
)
from .errors import ContractError
from .execution_receipts import (
    FFMPEG_EXECUTION_RECEIPT_SCHEMA,
    validate_ffmpeg_execution_receipt,
)
from .reference_remix import material_candidate_map

FRAME_MANIFEST_SCHEMA = "video_creation_pipeline.frame_manifest.v1"
LOCAL_MULTIMODAL_PLAN_SCHEMA = "video_creation_pipeline.local_multimodal_plan.v1"
LOCAL_MULTIMODAL_RECEIPT_SCHEMA = "video_creation_pipeline.local_multimodal_receipt.v1"
LOCAL_MULTIMODAL_VALIDATION_SCHEMA = (
    "video_creation_pipeline.local_multimodal_validation.v1"
)

LLAMA_CPP_BUILD = "b8644"
LLAMA_CPP_COMMIT = "39b27f0da0271c06986cb31b68bc0fe68e780616"
DEFAULT_LLAMA_ENDPOINT = "http://127.0.0.1:8080/v1/chat/completions"


def build_frame_manifest(
    source_media_path: str | Path,
    ffmpeg_receipt_path: str | Path,
) -> dict[str, Any]:
    source = Path(source_media_path).expanduser().resolve()
    receipt_path = Path(ffmpeg_receipt_path).expanduser().resolve()
    receipt = read_json_object(receipt_path, "FFmpeg execution receipt")
    validate_ffmpeg_execution_receipt(receipt)
    if receipt.get("schema") != FFMPEG_EXECUTION_RECEIPT_SCHEMA:
        raise ContractError("frame manifest requires the current FFmpeg receipt")
    if receipt.get("operation") != "frame_extract":
        raise ContractError("frame manifest requires operation=frame_extract")
    source_reference = artifact_reference(source, "source media")
    input_references = [
        row.get("artifact")
        for row in receipt.get("inputs") or []
        if isinstance(row, dict)
    ]
    if source_reference not in input_references:
        raise ContractError("FFmpeg frame receipt does not bind the selected source")

    frames = [
        {
            "frame_id": f"frame-{index:04d}",
            "media_time_s": float(row["media_time_s"]),
            "artifact": dict(row["artifact"]),
        }
        for index, row in enumerate(receipt["outputs"], 1)
    ]
    manifest: dict[str, Any] = {
        "schema": FRAME_MANIFEST_SCHEMA,
        "source_media": source_reference,
        "ffmpeg_receipt": artifact_reference(receipt_path, "FFmpeg execution receipt"),
        "extraction": {
            "outlet_id": receipt["outlet_id"],
            "command": list(receipt["command"]),
            "execution_profile": dict(receipt["execution_profile"]),
        },
        "frames": frames,
        "authority_boundary": {
            "existing_ffmpeg_outlet_consumed": True,
            "ffmpeg_executed_by_this_adapter": False,
            "source_media_modified": False,
            "candidate_evidence_only": True,
        },
    }
    manifest["manifest_sha256"] = payload_sha256(manifest, "manifest_sha256")
    validate_frame_manifest(manifest)
    return manifest


def validate_frame_manifest(manifest: dict[str, Any]) -> None:
    if manifest.get("schema") != FRAME_MANIFEST_SCHEMA:
        raise ContractError("unsupported frame manifest schema")
    if manifest.get("manifest_sha256") != payload_sha256(manifest, "manifest_sha256"):
        raise ContractError("frame manifest integrity check failed")
    source_path = validate_artifact_reference(
        _object(manifest.get("source_media")), "frame source media"
    )
    receipt_path = validate_artifact_reference(
        _object(manifest.get("ffmpeg_receipt")), "FFmpeg execution receipt"
    )
    receipt = read_json_object(receipt_path, "FFmpeg execution receipt")
    validate_ffmpeg_execution_receipt(receipt)
    if receipt.get("operation") != "frame_extract":
        raise ContractError("frame manifest receipt operation drifted")
    if artifact_reference(source_path, "frame source media") not in [
        row.get("artifact")
        for row in receipt.get("inputs") or []
        if isinstance(row, dict)
    ]:
        raise ContractError("frame manifest source is absent from FFmpeg inputs")
    expected = [
        {
            "frame_id": f"frame-{index:04d}",
            "media_time_s": float(row["media_time_s"]),
            "artifact": dict(row["artifact"]),
        }
        for index, row in enumerate(receipt["outputs"], 1)
    ]
    if manifest.get("frames") != expected:
        raise ContractError("frame manifest drifted from FFmpeg outputs")
    extraction = _object(manifest.get("extraction"))
    if (
        extraction.get("outlet_id") != receipt.get("outlet_id")
        or extraction.get("command") != receipt.get("command")
        or extraction.get("execution_profile") != receipt.get("execution_profile")
    ):
        raise ContractError("frame extraction provenance is inconsistent")
    boundary = _object(manifest.get("authority_boundary"))
    if (
        boundary.get("existing_ffmpeg_outlet_consumed") is not True
        or boundary.get("ffmpeg_executed_by_this_adapter") is not False
        or boundary.get("source_media_modified") is not False
        or boundary.get("candidate_evidence_only") is not True
    ):
        raise ContractError("frame manifest authority boundary is incomplete")


def build_local_multimodal_plan(
    frame_manifest_path: str | Path,
    prompt_path: str | Path,
    model_path: str | Path,
    *,
    prompt_version: str,
    endpoint: str = DEFAULT_LLAMA_ENDPOINT,
) -> dict[str, Any]:
    frame_path = Path(frame_manifest_path).expanduser().resolve()
    prompt_file = Path(prompt_path).expanduser().resolve()
    model_file = Path(model_path).expanduser().resolve()
    manifest = read_json_object(frame_path, "frame manifest")
    validate_frame_manifest(manifest)
    normalized_endpoint = _loopback_endpoint(endpoint)
    version = str(prompt_version).strip()
    if not version:
        raise ContractError("local multimodal plan requires a prompt version")

    plan: dict[str, Any] = {
        "schema": LOCAL_MULTIMODAL_PLAN_SCHEMA,
        "source_frames": artifact_reference(frame_path, "frame manifest"),
        "prompt": artifact_reference(prompt_file, "multimodal prompt"),
        "prompt_version": version,
        "runtime": {
            "name": "llama.cpp",
            "build": LLAMA_CPP_BUILD,
            "source_commit": LLAMA_CPP_COMMIT,
            "model": artifact_reference(model_file, "llama.cpp model"),
            "endpoint": normalized_endpoint,
            "interface": "openai_compatible_chat_completions",
        },
        "request_mapping": {
            "method": "POST",
            "content_types": ["text", "image_url"],
            "image_transport": "data_url",
            "frame_sources": [
                {
                    "frame_id": row["frame_id"],
                    "media_time_s": row["media_time_s"],
                    "type": "image_url",
                    "image_url": {
                        "transport": "data_url",
                        "source_artifact": dict(row["artifact"]),
                    },
                }
                for row in manifest["frames"]
            ],
            "custom_video_message_type": False,
        },
        "result_contract": {
            "schema": "video_creation_pipeline.material_candidates.v1",
            "acceptance_status": "candidate_only",
            "requires_human_review": True,
            "mutates_timeline": False,
        },
        "execution_boundary": {
            "execution_performed": False,
            "loopback_only": True,
            "external_network_allowed": False,
            "model_download_performed": False,
            "server_started_by_this_repo": False,
            "automatic_online_fallback": False,
        },
    }
    plan["plan_sha256"] = payload_sha256(plan, "plan_sha256")
    validate_local_multimodal_plan(plan)
    return plan


def validate_local_multimodal_plan(plan: dict[str, Any]) -> None:
    if plan.get("schema") != LOCAL_MULTIMODAL_PLAN_SCHEMA:
        raise ContractError("unsupported local multimodal plan schema")
    if plan.get("plan_sha256") != payload_sha256(plan, "plan_sha256"):
        raise ContractError("local multimodal plan integrity check failed")
    frames_path = validate_artifact_reference(
        _object(plan.get("source_frames")), "frame manifest"
    )
    manifest = read_json_object(frames_path, "frame manifest")
    validate_frame_manifest(manifest)
    validate_artifact_reference(_object(plan.get("prompt")), "multimodal prompt")
    runtime = _object(plan.get("runtime"))
    if (
        runtime.get("name") != "llama.cpp"
        or runtime.get("build") != LLAMA_CPP_BUILD
        or runtime.get("source_commit") != LLAMA_CPP_COMMIT
        or runtime.get("interface") != "openai_compatible_chat_completions"
    ):
        raise ContractError("local multimodal runtime is not the reviewed llama.cpp")
    validate_artifact_reference(_object(runtime.get("model")), "llama.cpp model")
    _loopback_endpoint(runtime.get("endpoint"))
    mapping = _object(plan.get("request_mapping"))
    if (
        mapping.get("method") != "POST"
        or mapping.get("content_types") != ["text", "image_url"]
        or mapping.get("image_transport") != "data_url"
        or mapping.get("custom_video_message_type") is not False
    ):
        raise ContractError("llama.cpp request mapping is invalid")
    expected_frames = [
        {
            "frame_id": row["frame_id"],
            "media_time_s": row["media_time_s"],
            "type": "image_url",
            "image_url": {
                "transport": "data_url",
                "source_artifact": dict(row["artifact"]),
            },
        }
        for row in manifest["frames"]
    ]
    if mapping.get("frame_sources") != expected_frames:
        raise ContractError("llama.cpp request frames drifted from the manifest")
    result = _object(plan.get("result_contract"))
    if (
        result.get("schema") != "video_creation_pipeline.material_candidates.v1"
        or result.get("acceptance_status") != "candidate_only"
        or result.get("requires_human_review") is not True
        or result.get("mutates_timeline") is not False
    ):
        raise ContractError("local multimodal result contract is incomplete")
    boundary = _object(plan.get("execution_boundary"))
    if boundary.get("loopback_only") is not True or any(
        boundary.get(field) is not False
        for field in (
            "execution_performed",
            "external_network_allowed",
            "model_download_performed",
            "server_started_by_this_repo",
            "automatic_online_fallback",
        )
    ):
        raise ContractError("local multimodal execution boundary is invalid")


def validate_local_multimodal_receipt(
    plan: dict[str, Any], receipt: dict[str, Any]
) -> dict[str, Any]:
    validate_local_multimodal_plan(plan)
    if receipt.get("schema") != LOCAL_MULTIMODAL_RECEIPT_SCHEMA:
        raise ContractError("unsupported local multimodal receipt schema")
    if (
        receipt.get("plan_sha256") != plan.get("plan_sha256")
        or receipt.get("runtime_commit") != LLAMA_CPP_COMMIT
        or receipt.get("model_sha256")
        != plan.get("runtime", {}).get("model", {}).get("sha256")
    ):
        raise ContractError("local multimodal receipt does not bind the exact plan")
    if (
        receipt.get("endpoint") != plan.get("runtime", {}).get("endpoint")
        or receipt.get("external_network_used") is not False
        or receipt.get("fallback_used") is not False
        or receipt.get("custom_video_message_type_used") is not False
    ):
        raise ContractError("local multimodal receipt violates its execution boundary")
    status = str(receipt.get("status") or "")
    if status not in {"completed", "failed", "cancelled"}:
        raise ContractError("local multimodal receipt status is invalid")
    if status != "completed":
        if not str(receipt.get("error") or "").strip():
            raise ContractError("failed local multimodal receipt needs a reason")
        return _receipt_validation(plan, status, 0)
    raw_path = validate_artifact_reference(
        _object(receipt.get("raw_output")), "llama.cpp raw output"
    )
    candidates_path = validate_artifact_reference(
        _object(receipt.get("material_candidates")), "material candidates"
    )
    if raw_path == candidates_path:
        raise ContractError("raw model output and parsed candidates must be separate")
    candidates = read_json_object(candidates_path, "material candidates")
    material_candidate_map(candidates)
    if receipt.get("acceptance_status") != "candidate_only":
        raise ContractError("local multimodal output must remain candidate_only")
    return _receipt_validation(
        plan,
        status,
        sum(len(row) for row in material_candidate_map(candidates).values()),
    )


def write_local_multimodal_contract(
    payload: dict[str, Any], output_path: str | Path
) -> Path:
    if payload.get("schema") == FRAME_MANIFEST_SCHEMA:
        validate_frame_manifest(payload)
    elif payload.get("schema") == LOCAL_MULTIMODAL_PLAN_SCHEMA:
        validate_local_multimodal_plan(payload)
    else:
        raise ContractError("unsupported local multimodal contract")
    return atomic_write_json(payload, output_path)


def _receipt_validation(
    plan: dict[str, Any], status: str, candidate_count: int
) -> dict[str, Any]:
    return {
        "schema": LOCAL_MULTIMODAL_VALIDATION_SCHEMA,
        "valid": True,
        "status": status,
        "plan_sha256": plan["plan_sha256"],
        "candidate_count": candidate_count,
        "acceptance_status": "candidate_only",
        "next_action": "human_material_review"
        if status == "completed"
        else "operator_review",
    }


def _loopback_endpoint(value: Any) -> str:
    endpoint = str(value or "").strip()
    parsed = urlparse(endpoint)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.path not in {"/chat/completions", "/v1/chat/completions"}
    ):
        raise ContractError(
            "llama.cpp endpoint must be a loopback chat-completions endpoint"
        )
    return endpoint


def _object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}
