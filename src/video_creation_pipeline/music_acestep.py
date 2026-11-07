from __future__ import annotations

import importlib
import math
import os
import re
import sys
import wave
from contextlib import contextmanager
from pathlib import Path
from types import MethodType
from typing import Any, Protocol

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
from .music_pipeline import (
    build_music_asset_profile,
    validate_music_intent_curve,
    write_music_contract,
)

ACESTEP_CHECKPOINT_MANIFEST_SCHEMA = (
    "video_creation_pipeline.acestep_checkpoint_manifest.v1"
)
ACESTEP_GENERATION_PLAN_SCHEMA = "video_creation_pipeline.acestep_generation_plan.v1"
MUSIC_GENERATION_RECEIPT_SCHEMA = "video_creation_pipeline.music_generation_receipt.v1"

ACESTEP_PROVIDER_ID = "ace-step-1.5-local-pinned"
ACESTEP_PACKAGE_VERSION = "1.5.0"
ACESTEP_SOURCE_COMMIT = "6d467e4b5081ccb0abf1ec1bf4fdf9051a2d34b0"
ACESTEP_MODEL_NAME = "acestep-v15-turbo"
ACESTEP_MODEL_URI = "https://huggingface.co/ACE-Step/Ace-Step1.5"
ACESTEP_MIN_DURATION_S = 10.0
ACESTEP_MAX_DURATION_S = 600.0
ACESTEP_MAX_BATCH_SIZE = 8

REQUIRED_CHECKPOINT_COMPONENTS = (
    "acestep-v15-turbo",
    "vae",
    "Qwen3-Embedding-0.6B",
    "acestep-5Hz-lm-1.7B",
)

REVIEWED_SOURCE_SHA256 = {
    "LICENSE": "4bd5442a24866511d97157b946cf16ca3370dac25632cedee94ed30b13bc404d",
    "pyproject.toml": (
        "902cd5e9dbb4f87ec9cd0c8dd2e232e89d4e8be07f7d0f48eee94c036fec1006"
    ),
    "acestep/model_downloader.py": (
        "7dbfb69c0154dd235c3f5cf8ebeb8b76ccbf958fa7391a67284028d0066887c4"
    ),
    "acestep/inference.py": (
        "234cd6f32b7dfd62a5792918b5bc5a0e0e8cc4b190bdf9f5fcef9ea57485b0c5"
    ),
    "acestep/handler.py": (
        "63e2dd7d5c769b1135cbfacd930f887be8207a744e808bd9aacf483113143e25"
    ),
    "acestep/llm_inference.py": (
        "f1f046461e42340e9cd27658a6f4cf1222c10cd34a77c8f5fdefa8d5fad50dc7"
    ),
    "acestep/core/generation/handler/init_service_orchestrator.py": (
        "b10a69f5a3729db4dc81e3a2a7cfbdb8d9bf6af3ed154fca75359d51fd9253aa"
    ),
    "acestep/core/generation/handler/init_service_downloads.py": (
        "d6e7eb73c4a73d9e6ba265f09227905d05eb83ca1e746fc4cd7d414eb63bbbc4"
    ),
}


class AceStepGenerationBackend(Protocol):
    def generate(
        self, request: dict[str, Any], output_dir: Path
    ) -> list[dict[str, Any]]: ...


def build_acestep_checkpoint_manifest(
    checkpoint_root: str | Path,
    license_snapshot_path: str | Path,
    *,
    model_license: str = "MIT",
    commercial_use_allowed: bool,
) -> dict[str, Any]:
    root = Path(checkpoint_root).expanduser().resolve()
    files = _snapshot_checkpoint_files(root)
    _require_checkpoint_components(files)
    payload: dict[str, Any] = {
        "schema": ACESTEP_CHECKPOINT_MANIFEST_SCHEMA,
        "checkpoint_root": str(root),
        "required_components": list(REQUIRED_CHECKPOINT_COMPONENTS),
        "files": files,
        "files_sha256": canonical_json_sha256(files),
        "model_license": _required_text(model_license, "ACE-Step model license"),
        "model_license_snapshot": artifact_reference(
            license_snapshot_path, "ACE-Step model license snapshot"
        ),
        "commercial_use_allowed": _boolean(
            commercial_use_allowed, "ACE-Step commercial model use"
        ),
        "source": {
            "repository": "https://github.com/ace-step/ACE-Step-1.5",
            "commit": ACESTEP_SOURCE_COMMIT,
            "package_version": ACESTEP_PACKAGE_VERSION,
        },
    }
    payload["manifest_sha256"] = payload_sha256(payload, "manifest_sha256")
    validate_acestep_checkpoint_manifest(payload)
    return payload


def validate_acestep_checkpoint_manifest(payload: dict[str, Any]) -> None:
    if payload.get("schema") != ACESTEP_CHECKPOINT_MANIFEST_SCHEMA:
        raise ContractError("unsupported ACE-Step checkpoint manifest schema")
    if payload.get("manifest_sha256") != payload_sha256(payload, "manifest_sha256"):
        raise ContractError("ACE-Step checkpoint manifest integrity check failed")
    if payload.get("required_components") != list(REQUIRED_CHECKPOINT_COMPONENTS):
        raise ContractError("ACE-Step checkpoint component set is unsupported")
    root = Path(str(payload.get("checkpoint_root") or "")).expanduser().resolve()
    files = _snapshot_checkpoint_files(root)
    _require_checkpoint_components(files)
    if payload.get("files") != files or payload.get(
        "files_sha256"
    ) != canonical_json_sha256(files):
        raise ContractError("ACE-Step checkpoints changed after manifest creation")
    _required_text(payload.get("model_license"), "ACE-Step model license")
    validate_artifact_reference(
        _object(payload.get("model_license_snapshot")),
        "ACE-Step model license snapshot",
    )
    _boolean(payload.get("commercial_use_allowed"), "ACE-Step commercial model use")
    source = _object(payload.get("source"))
    if source != {
        "repository": "https://github.com/ace-step/ACE-Step-1.5",
        "commit": ACESTEP_SOURCE_COMMIT,
        "package_version": ACESTEP_PACKAGE_VERSION,
    }:
        raise ContractError("ACE-Step checkpoint source metadata is unsupported")


def build_acestep_generation_plan(
    intent_path: str | Path,
    checkpoint_manifest_path: str | Path,
    source_root: str | Path,
    output_root: str | Path,
    *,
    purpose: str = "personal",
    device: str = "cuda",
    candidates_per_segment: int = 3,
    random_seed: int = 20260805,
    inference_steps: int = 8,
    guidance_scale: float = 7.0,
    segment_overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    intent_file = Path(intent_path).expanduser().resolve()
    intent = read_json_object(intent_file, "music intent curve")
    validate_music_intent_curve(intent)
    manifest_file = Path(checkpoint_manifest_path).expanduser().resolve()
    manifest = read_json_object(manifest_file, "ACE-Step checkpoint manifest")
    validate_acestep_checkpoint_manifest(manifest)
    normalized_purpose = _choice(purpose, {"personal", "commercial"}, "purpose")
    if normalized_purpose == "commercial" and not manifest["commercial_use_allowed"]:
        raise ContractError("ACE-Step checkpoints are not cleared for commercial use")
    source = Path(source_root).expanduser().resolve()
    source_files = _reviewed_source_files(source)
    destination = Path(output_root).expanduser().resolve()
    _validate_output_root(destination, source, Path(manifest["checkpoint_root"]))
    defaults = _normalize_generation_defaults(
        {
            "model": ACESTEP_MODEL_NAME,
            "device": device,
            "audio_format": "wav",
            "candidates_per_segment": candidates_per_segment,
            "random_seed": random_seed,
            "inference_steps": inference_steps,
            "guidance_scale": guidance_scale,
            "thinking": False,
            "use_cot_metas": False,
            "use_cot_caption": False,
            "use_cot_lyrics": False,
            "use_cot_language": False,
        }
    )
    overrides = _normalize_segment_overrides(
        segment_overrides or {}, {row["segment_id"] for row in intent["segments"]}
    )
    payload: dict[str, Any] = {
        "schema": ACESTEP_GENERATION_PLAN_SCHEMA,
        "intent_curve": artifact_reference(intent_file, "music intent curve"),
        "intent_curve_sha256": intent["intent_curve_sha256"],
        "checkpoint_manifest": artifact_reference(
            manifest_file, "ACE-Step checkpoint manifest"
        ),
        "checkpoint_manifest_sha256": manifest["manifest_sha256"],
        "runtime": {
            "provider_id": ACESTEP_PROVIDER_ID,
            "package": "ace-step",
            "package_version": ACESTEP_PACKAGE_VERSION,
            "source_commit": ACESTEP_SOURCE_COMMIT,
            "source_root": str(source),
            "source_files": source_files,
            "source_manifest_sha256": canonical_json_sha256(source_files),
            "checkpoint_root": manifest["checkpoint_root"],
            "model_license": manifest["model_license"],
            "model_license_snapshot": dict(manifest["model_license_snapshot"]),
            "commercial_use_allowed": manifest["commercial_use_allowed"],
            "purpose": normalized_purpose,
            "device": defaults["device"],
            "model": defaults["model"],
        },
        "output_root": str(destination),
        "generation_defaults": defaults,
        "segment_overrides": overrides,
        "requests": _build_generation_requests(intent["segments"], defaults, overrides),
        "execution_boundary": {
            "execution_performed": False,
            "external_network_allowed": False,
            "automatic_model_download": False,
            "automatic_device_fallback": False,
            "automatic_provider_fallback": False,
            "ffmpeg_executed_by_adapter": False,
            "automatic_publish": False,
        },
    }
    payload["plan_sha256"] = payload_sha256(payload, "plan_sha256")
    validate_acestep_generation_plan(payload)
    return payload


def validate_acestep_generation_plan(payload: dict[str, Any]) -> None:
    if payload.get("schema") != ACESTEP_GENERATION_PLAN_SCHEMA:
        raise ContractError("unsupported ACE-Step generation plan schema")
    if payload.get("plan_sha256") != payload_sha256(payload, "plan_sha256"):
        raise ContractError("ACE-Step generation plan integrity check failed")
    intent_path = validate_artifact_reference(
        _object(payload.get("intent_curve")), "music intent curve"
    )
    intent = read_json_object(intent_path, "music intent curve")
    validate_music_intent_curve(intent)
    if payload.get("intent_curve_sha256") != intent.get("intent_curve_sha256"):
        raise ContractError("ACE-Step plan targets another music intent curve")
    manifest_path = validate_artifact_reference(
        _object(payload.get("checkpoint_manifest")),
        "ACE-Step checkpoint manifest",
    )
    manifest = read_json_object(manifest_path, "ACE-Step checkpoint manifest")
    validate_acestep_checkpoint_manifest(manifest)
    if payload.get("checkpoint_manifest_sha256") != manifest.get("manifest_sha256"):
        raise ContractError("ACE-Step plan targets another checkpoint manifest")
    runtime = _object(payload.get("runtime"))
    if (
        runtime.get("provider_id") != ACESTEP_PROVIDER_ID
        or runtime.get("package") != "ace-step"
        or runtime.get("package_version") != ACESTEP_PACKAGE_VERSION
        or runtime.get("source_commit") != ACESTEP_SOURCE_COMMIT
        or runtime.get("model") != ACESTEP_MODEL_NAME
    ):
        raise ContractError("ACE-Step plan does not use the reviewed runtime")
    source = Path(str(runtime.get("source_root") or "")).expanduser().resolve()
    source_files = _reviewed_source_files(source)
    if runtime.get("source_files") != source_files or runtime.get(
        "source_manifest_sha256"
    ) != canonical_json_sha256(source_files):
        raise ContractError("ACE-Step reviewed source changed after planning")
    if runtime.get("checkpoint_root") != manifest.get("checkpoint_root"):
        raise ContractError("ACE-Step runtime checkpoint root drifted")
    for field in (
        "model_license",
        "model_license_snapshot",
        "commercial_use_allowed",
    ):
        if runtime.get(field) != manifest.get(field):
            raise ContractError(f"ACE-Step runtime {field} drifted from manifest")
    purpose = _choice(runtime.get("purpose"), {"personal", "commercial"}, "purpose")
    if purpose == "commercial" and runtime.get("commercial_use_allowed") is not True:
        raise ContractError("ACE-Step commercial plan lacks model clearance")
    defaults = _normalize_generation_defaults(
        _object(payload.get("generation_defaults"))
    )
    if runtime.get("device") != defaults["device"]:
        raise ContractError("ACE-Step runtime device drifted from defaults")
    overrides = _normalize_segment_overrides(
        _object(payload.get("segment_overrides")),
        {row["segment_id"] for row in intent["segments"]},
    )
    expected_requests = _build_generation_requests(
        intent["segments"], defaults, overrides
    )
    if payload.get("requests") != expected_requests:
        raise ContractError("ACE-Step generation requests drifted from their intent")
    output_root = Path(str(payload.get("output_root") or "")).expanduser().resolve()
    _validate_output_root(output_root, source, Path(manifest["checkpoint_root"]))
    boundary = _object(payload.get("execution_boundary"))
    required_false = (
        "execution_performed",
        "external_network_allowed",
        "automatic_model_download",
        "automatic_device_fallback",
        "automatic_provider_fallback",
        "ffmpeg_executed_by_adapter",
        "automatic_publish",
    )
    if not all(boundary.get(field) is False for field in required_false):
        raise ContractError("ACE-Step generation execution boundary is invalid")


def run_acestep_generation(
    plan_path: str | Path,
    *,
    backend: AceStepGenerationBackend | None = None,
) -> dict[str, Any]:
    source = Path(plan_path).expanduser().resolve()
    plan = read_json_object(source, "ACE-Step generation plan")
    validate_acestep_generation_plan(plan)
    attempts: list[dict[str, Any]] = []
    error: dict[str, str] | None = None
    try:
        engine = backend or _AceStepBackend(plan)
        output_root = Path(plan["output_root"])
        for request in plan["requests"]:
            segment_dir = output_root / request["output_subdir"]
            segment_dir.mkdir(parents=True, exist_ok=True)
            try:
                raw_outputs = engine.generate(request, segment_dir)
                outputs = _normalize_generated_outputs(
                    raw_outputs, request, segment_dir
                )
                attempts.append(
                    {
                        "segment_id": request["segment_id"],
                        "status": "completed",
                        "requested_seeds": list(request["generation"]["seeds"]),
                        "outputs": outputs,
                        "error": None,
                    }
                )
            except Exception as exc:
                message = _safe_error(exc)
                attempts.append(
                    {
                        "segment_id": request["segment_id"],
                        "status": "failed",
                        "requested_seeds": list(request["generation"]["seeds"]),
                        "outputs": [],
                        "error": message,
                    }
                )
                error = {"stage": request["segment_id"], "message": message}
                break
    except Exception as exc:
        error = {"stage": "runtime_initialization", "message": _safe_error(exc)}
    completed = error is None and len(attempts) == len(plan["requests"])
    receipt: dict[str, Any] = {
        "schema": MUSIC_GENERATION_RECEIPT_SCHEMA,
        "status": "completed" if completed else "failed",
        "plan": artifact_reference(source, "ACE-Step generation plan"),
        "plan_sha256": plan["plan_sha256"],
        "provider": {
            "id": ACESTEP_PROVIDER_ID,
            "model": plan["runtime"]["model"],
            "package_version": ACESTEP_PACKAGE_VERSION,
            "source_commit": ACESTEP_SOURCE_COMMIT,
            "source_manifest_sha256": plan["runtime"]["source_manifest_sha256"],
            "checkpoint_manifest_sha256": plan["checkpoint_manifest_sha256"],
            "model_license": plan["runtime"]["model_license"],
            "commercial_use_allowed": plan["runtime"]["commercial_use_allowed"],
            "execution_location": "local",
            "requested_device": plan["runtime"]["device"],
        },
        "attempts": attempts,
        "error": error,
        "readiness": {
            "generation_complete": completed,
            "eligible_for_music_profiling": completed,
            "eligible_for_clap_ranking": completed,
        },
        "execution_boundary": {
            "external_network_used": False,
            "automatic_model_download": False,
            "automatic_device_fallback": False,
            "automatic_provider_fallback": False,
            "automatic_retry": False,
            "ffmpeg_executed_by_adapter": False,
            "automatic_publish": False,
        },
    }
    receipt["receipt_sha256"] = payload_sha256(receipt, "receipt_sha256")
    validate_music_generation_receipt(receipt)
    return receipt


def validate_music_generation_receipt(receipt: dict[str, Any]) -> None:
    if receipt.get("schema") != MUSIC_GENERATION_RECEIPT_SCHEMA:
        raise ContractError("unsupported music generation receipt schema")
    if receipt.get("receipt_sha256") != payload_sha256(receipt, "receipt_sha256"):
        raise ContractError("music generation receipt integrity check failed")
    plan_path = validate_artifact_reference(
        _object(receipt.get("plan")), "ACE-Step generation plan"
    )
    plan = read_json_object(plan_path, "ACE-Step generation plan")
    validate_acestep_generation_plan(plan)
    if receipt.get("plan_sha256") != plan.get("plan_sha256"):
        raise ContractError("music generation receipt targets another plan")
    provider = _object(receipt.get("provider"))
    expected_provider = {
        "id": ACESTEP_PROVIDER_ID,
        "model": plan["runtime"]["model"],
        "package_version": ACESTEP_PACKAGE_VERSION,
        "source_commit": ACESTEP_SOURCE_COMMIT,
        "source_manifest_sha256": plan["runtime"]["source_manifest_sha256"],
        "checkpoint_manifest_sha256": plan["checkpoint_manifest_sha256"],
        "model_license": plan["runtime"]["model_license"],
        "commercial_use_allowed": plan["runtime"]["commercial_use_allowed"],
        "execution_location": "local",
        "requested_device": plan["runtime"]["device"],
    }
    if provider != expected_provider:
        raise ContractError("music generation provider drifted from its plan")
    status = _choice(receipt.get("status"), {"completed", "failed"}, "receipt status")
    attempts = receipt.get("attempts")
    if not isinstance(attempts, list):
        raise ContractError("music generation receipt attempts must be a list")
    if len(attempts) > len(plan["requests"]):
        raise ContractError("music generation receipt has unexpected attempts")
    failed_count = 0
    for index, attempt in enumerate(attempts):
        if not isinstance(attempt, dict):
            raise ContractError("music generation attempt must be an object")
        request = plan["requests"][index]
        if (
            attempt.get("segment_id") != request["segment_id"]
            or attempt.get("requested_seeds") != request["generation"]["seeds"]
        ):
            raise ContractError("music generation attempt drifted from its request")
        attempt_status = _choice(
            attempt.get("status"), {"completed", "failed"}, "attempt status"
        )
        outputs = attempt.get("outputs")
        if not isinstance(outputs, list):
            raise ContractError("music generation outputs must be a list")
        if attempt_status == "completed":
            _validate_generated_outputs(outputs, request, Path(plan["output_root"]))
            if attempt.get("error") is not None:
                raise ContractError("completed music generation attempt has an error")
        else:
            failed_count += 1
            if outputs or not str(attempt.get("error") or "").strip():
                raise ContractError("failed music generation attempt is incomplete")
            if index != len(attempts) - 1:
                raise ContractError("music generation continued after a failed attempt")
    readiness = _object(receipt.get("readiness"))
    if status == "completed":
        if (
            len(attempts) != len(plan["requests"])
            or failed_count
            or receipt.get("error") is not None
            or readiness
            != {
                "generation_complete": True,
                "eligible_for_music_profiling": True,
                "eligible_for_clap_ranking": True,
            }
        ):
            raise ContractError("completed music generation receipt is incomplete")
    else:
        error = _object(receipt.get("error"))
        if not attempts:
            expected_error = {
                "stage": "runtime_initialization",
                "message": error.get("message"),
            }
        elif failed_count == 1 and attempts[-1]["status"] == "failed":
            expected_error = {
                "stage": attempts[-1]["segment_id"],
                "message": attempts[-1]["error"],
            }
        else:
            expected_error = None
        if (
            expected_error is None
            or error != expected_error
            or not str(error.get("message") or "").strip()
            or readiness
            != {
                "generation_complete": False,
                "eligible_for_music_profiling": False,
                "eligible_for_clap_ranking": False,
            }
        ):
            raise ContractError("failed music generation receipt is incomplete")
    boundary = _object(receipt.get("execution_boundary"))
    required_false = (
        "external_network_used",
        "automatic_model_download",
        "automatic_device_fallback",
        "automatic_provider_fallback",
        "automatic_retry",
        "ffmpeg_executed_by_adapter",
        "automatic_publish",
    )
    if not all(boundary.get(field) is False for field in required_false):
        raise ContractError("music generation receipt boundary is invalid")


def write_acestep_contract(payload: dict[str, Any], output_path: str | Path) -> Path:
    schema = payload.get("schema")
    if schema == ACESTEP_CHECKPOINT_MANIFEST_SCHEMA:
        validate_acestep_checkpoint_manifest(payload)
    elif schema == ACESTEP_GENERATION_PLAN_SCHEMA:
        validate_acestep_generation_plan(payload)
    elif schema == MUSIC_GENERATION_RECEIPT_SCHEMA:
        validate_music_generation_receipt(payload)
    else:
        raise ContractError("unsupported ACE-Step contract")
    return atomic_write_json(payload, output_path)


def materialize_acestep_music_profiles(
    receipt_path: str | Path, output_dir: str | Path
) -> list[Path]:
    source = Path(receipt_path).expanduser().resolve()
    receipt = read_json_object(source, "music generation receipt")
    validate_music_generation_receipt(receipt)
    if receipt["status"] != "completed":
        raise ContractError("failed music generation cannot create asset profiles")
    plan_path = validate_artifact_reference(
        _object(receipt["plan"]), "ACE-Step generation plan"
    )
    plan = read_json_object(plan_path, "ACE-Step generation plan")
    manifest_path = validate_artifact_reference(
        _object(plan["checkpoint_manifest"]), "ACE-Step checkpoint manifest"
    )
    manifest = read_json_object(manifest_path, "ACE-Step checkpoint manifest")
    destination = Path(output_dir).expanduser().resolve()
    payloads: list[tuple[Path, dict[str, Any]]] = []
    requests = {row["segment_id"]: row for row in plan["requests"]}
    for attempt in receipt["attempts"]:
        request = requests[attempt["segment_id"]]
        for output in attempt["outputs"]:
            profile = build_music_asset_profile(
                output["audio"]["path"],
                asset_id=output["asset_id"],
                duration_s=output["duration_s"],
                windows=[
                    {
                        "window_id": f"{output['asset_id']}-full",
                        "start_s": 0.0,
                        "end_s": output["duration_s"],
                        "structure_label": "generated-segment",
                        "structure_confidence": 0.5,
                        "descriptors": dict(request["target"]),
                        "vocal_probability": 0.1,
                        "tags": [
                            "instrumental",
                            "generated",
                            "background",
                            request["relationship_mode"],
                        ],
                        "loopable": False,
                    }
                ],
                license_info={
                    "status": "allowed",
                    "commercial_use_allowed": manifest["commercial_use_allowed"],
                    "license_uri": ACESTEP_MODEL_URI,
                    "license_snapshot": (
                        "ACE-Step 1.5 model license snapshot sha256 "
                        f"{manifest['model_license_snapshot']['sha256']}"
                    ),
                    "attribution": "Generated with ACE-Step 1.5",
                },
                analyzer={
                    "id": "ace-step-generation-conditioning",
                    "model": plan["runtime"]["model"],
                    "version": ACESTEP_PACKAGE_VERSION,
                    "source_commit": ACESTEP_SOURCE_COMMIT,
                    "execution_location": "local",
                    "model_license": manifest["model_license"],
                    "automatic_fallback_used": False,
                },
            )
            payloads.append(
                (destination / f"{output['asset_id']}-profile.json", profile)
            )
    if not payloads:
        raise ContractError("music generation receipt has no profileable outputs")
    return [write_music_contract(payload, path) for path, payload in payloads]


class _AceStepBackend:
    def __init__(self, plan: dict[str, Any]) -> None:
        runtime = plan["runtime"]
        source_root = Path(runtime["source_root"])
        checkpoint_root = Path(runtime["checkpoint_root"])
        with _acestep_environment(checkpoint_root):
            _ensure_reviewed_package_import(source_root)
            try:
                import torch

                _validate_requested_device(torch, runtime["device"])
                downloader = importlib.import_module("acestep.model_downloader")
                if not downloader.check_main_model_exists(checkpoint_root):
                    raise ContractError(
                        "ACE-Step main checkpoint is incomplete; automatic download is disabled"
                    )
                if not downloader.check_model_exists(runtime["model"], checkpoint_root):
                    raise ContractError(
                        "ACE-Step DiT checkpoint is incomplete; automatic download is disabled"
                    )
                mismatched_code = downloader._check_code_mismatch(
                    runtime["model"], checkpoint_root
                )
                if mismatched_code:
                    names = ", ".join(sorted(mismatched_code))
                    raise ContractError(
                        "ACE-Step checkpoint code differs from reviewed source; "
                        f"automatic code sync is disabled: {names}"
                    )

                handler_module = importlib.import_module("acestep.handler")
                inference_module = importlib.import_module("acestep.inference")
                self._params_type = inference_module.GenerationParams
                self._config_type = inference_module.GenerationConfig
                self._generate_music = inference_module.generate_music
                self._handler = handler_module.AceStepHandler()
                with _disable_acestep_model_mutation(self._handler):
                    status, success = self._handler.initialize_service(
                        project_root=str(checkpoint_root.parent),
                        config_path=runtime["model"],
                        device=runtime["device"],
                        use_flash_attention=False,
                        compile_model=False,
                        offload_to_cpu=False,
                        offload_dit_to_cpu=False,
                        quantization=None,
                        prefer_source=None,
                        use_mlx_dit=False,
                        vae_checkpoint="official",
                    )
                if not success:
                    raise ContractError(f"ACE-Step initialization failed: {status}")
                self._checkpoint_root = checkpoint_root
            except ContractError:
                raise
            except Exception as exc:
                raise ContractError(
                    f"ACE-Step runtime initialization failed: {exc}"
                ) from exc

    def generate(
        self, request: dict[str, Any], output_dir: Path
    ) -> list[dict[str, Any]]:
        generation = request["generation"]
        with _acestep_environment(self._checkpoint_root):
            try:
                params = self._params_type(
                    task_type="text2music",
                    caption=generation["caption"],
                    lyrics="[Instrumental]",
                    instrumental=True,
                    vocal_language="unknown",
                    bpm=generation["bpm"],
                    keyscale=generation["keyscale"],
                    timesignature=generation["timesignature"],
                    duration=generation["duration_s"],
                    inference_steps=generation["inference_steps"],
                    seed=generation["seeds"][0],
                    guidance_scale=generation["guidance_scale"],
                    thinking=False,
                    use_cot_metas=False,
                    use_cot_caption=False,
                    use_cot_lyrics=False,
                    use_cot_language=False,
                )
                config = self._config_type(
                    batch_size=generation["batch_size"],
                    allow_lm_batch=False,
                    use_random_seed=False,
                    seeds=list(generation["seeds"]),
                    audio_format="wav",
                )
                result = self._generate_music(
                    self._handler,
                    None,
                    params,
                    config,
                    save_dir=str(output_dir),
                )
                if not result.success:
                    raise ContractError(
                        f"ACE-Step generation failed: {result.error or result.status_message}"
                    )
                outputs = []
                for audio in result.audios:
                    params_used = _object(audio.get("params"))
                    outputs.append(
                        {
                            "path": audio.get("path"),
                            "seed": params_used.get("seed"),
                            "key": str(audio.get("key") or ""),
                            "bpm": params_used.get("bpm"),
                        }
                    )
                return outputs
            except ContractError:
                raise
            except Exception as exc:
                raise ContractError(f"ACE-Step generation failed: {exc}") from exc


def _snapshot_checkpoint_files(root: Path) -> list[dict[str, Any]]:
    if not root.is_dir():
        raise ContractError(f"ACE-Step checkpoint root not found: {root}")
    rows: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix().lower()):
        if path.is_symlink():
            raise ContractError("ACE-Step checkpoint manifest does not allow symlinks")
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if _ignored_checkpoint_path(relative):
            continue
        rows.append(
            {
                "relative_path": relative.as_posix(),
                "bytes": path.stat().st_size,
                "sha256": file_sha256(path),
            }
        )
    if not rows:
        raise ContractError("ACE-Step checkpoint root contains no model files")
    return rows


def _ignored_checkpoint_path(relative: Path) -> bool:
    ignored_parts = {".cache", ".git", ".locks", "__pycache__"}
    if any(part in ignored_parts for part in relative.parts):
        return True
    return relative.name.endswith((".lock", ".tmp", ".partial"))


def _require_checkpoint_components(files: list[dict[str, Any]]) -> None:
    paths = [str(row.get("relative_path") or "") for row in files]
    for component in REQUIRED_CHECKPOINT_COMPONENTS:
        prefix = f"{component}/"
        if not any(path.startswith(prefix) for path in paths):
            raise ContractError(
                f"ACE-Step checkpoint component is missing: {component}"
            )


def _reviewed_source_files(source_root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for relative, expected_sha256 in REVIEWED_SOURCE_SHA256.items():
        path = source_root / Path(relative)
        reference = artifact_reference(path, f"reviewed ACE-Step {relative}")
        if reference["sha256"] != expected_sha256:
            raise ContractError(f"ACE-Step reviewed source hash mismatch: {relative}")
        rows.append({"relative_path": relative, "artifact": reference})
    return rows


def _normalize_generation_defaults(value: dict[str, Any]) -> dict[str, Any]:
    model = _choice(value.get("model"), {ACESTEP_MODEL_NAME}, "ACE-Step model")
    device = _choice(value.get("device"), {"cpu", "cuda"}, "ACE-Step device")
    audio_format = _choice(value.get("audio_format"), {"wav"}, "ACE-Step audio format")
    candidates = _integer(
        value.get("candidates_per_segment"), "ACE-Step candidate count"
    )
    if candidates < 1 or candidates > ACESTEP_MAX_BATCH_SIZE:
        raise ContractError("ACE-Step candidate count must be between 1 and 8")
    seed = _integer(value.get("random_seed"), "ACE-Step random seed")
    if seed < 0:
        raise ContractError("ACE-Step random seed must be non-negative")
    steps = _integer(value.get("inference_steps"), "ACE-Step inference steps")
    if steps < 1 or steps > 200:
        raise ContractError("ACE-Step inference steps must be between 1 and 200")
    guidance = _number(value.get("guidance_scale"), "ACE-Step guidance scale")
    if guidance < 0 or guidance > 30:
        raise ContractError("ACE-Step guidance scale must be between 0 and 30")
    result = {
        "model": model,
        "device": device,
        "audio_format": audio_format,
        "candidates_per_segment": candidates,
        "random_seed": seed,
        "inference_steps": steps,
        "guidance_scale": guidance,
        "thinking": _boolean(value.get("thinking"), "ACE-Step thinking"),
        "use_cot_metas": _boolean(value.get("use_cot_metas"), "ACE-Step CoT metas"),
        "use_cot_caption": _boolean(
            value.get("use_cot_caption"), "ACE-Step CoT caption"
        ),
        "use_cot_lyrics": _boolean(value.get("use_cot_lyrics"), "ACE-Step CoT lyrics"),
        "use_cot_language": _boolean(
            value.get("use_cot_language"), "ACE-Step CoT language"
        ),
    }
    if any(
        result[field]
        for field in (
            "thinking",
            "use_cot_metas",
            "use_cot_caption",
            "use_cot_lyrics",
            "use_cot_language",
        )
    ):
        raise ContractError("ACE-Step P0 adapter requires deterministic DiT-only mode")
    return result


def _normalize_segment_overrides(
    value: dict[str, Any], segment_ids: set[str]
) -> dict[str, dict[str, Any]]:
    normalized: dict[str, dict[str, Any]] = {}
    for segment_id, raw in sorted(value.items()):
        if segment_id not in segment_ids or not isinstance(raw, dict):
            raise ContractError("ACE-Step segment override identity is invalid")
        extra = set(raw) - {"bpm", "keyscale", "timesignature"}
        if extra:
            raise ContractError("ACE-Step segment override contains unsupported fields")
        row: dict[str, Any] = {}
        if raw.get("bpm") is not None:
            bpm = _integer(raw.get("bpm"), "ACE-Step BPM")
            if bpm < 30 or bpm > 300:
                raise ContractError("ACE-Step BPM must be between 30 and 300")
            row["bpm"] = bpm
        if raw.get("keyscale") is not None:
            row["keyscale"] = str(raw.get("keyscale") or "").strip()
        if raw.get("timesignature") is not None:
            row["timesignature"] = _choice(
                raw.get("timesignature"),
                {"2", "3", "4", "6"},
                "ACE-Step time signature",
            )
        normalized[segment_id] = row
    return normalized


def _build_generation_requests(
    segments: list[dict[str, Any]],
    defaults: dict[str, Any],
    overrides: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    requests: list[dict[str, Any]] = []
    used_seeds: set[int] = set()
    for index, segment in enumerate(segments):
        duration = round(segment["end_s"] - segment["start_s"], 6)
        if duration > ACESTEP_MAX_DURATION_S:
            raise ContractError(
                "ACE-Step music intent segments must not exceed 600 seconds"
            )
        generated_duration = max(ACESTEP_MIN_DURATION_S, duration)
        override = overrides.get(segment["segment_id"], {})
        seeds = []
        for candidate_index in range(defaults["candidates_per_segment"]):
            seed = (
                defaults["random_seed"] + index * 1009 + candidate_index * 9176
            ) % 2_147_483_647
            if seed in used_seeds:
                raise ContractError("ACE-Step generated duplicate deterministic seeds")
            used_seeds.add(seed)
            seeds.append(seed)
        requests.append(
            {
                "segment_id": segment["segment_id"],
                "timeline": {
                    "start_s": segment["start_s"],
                    "end_s": segment["end_s"],
                    "target_duration_s": duration,
                },
                "relationship_mode": segment["relationship_mode"],
                "target": dict(segment["target"]),
                "narration_density": segment["narration_density"],
                "evidence_ids": list(segment["evidence_ids"]),
                "output_subdir": _segment_output_subdir(
                    index + 1, segment["segment_id"]
                ),
                "generation": {
                    "task_type": "text2music",
                    "caption": _build_instrumental_caption(segment),
                    "lyrics": "[Instrumental]",
                    "instrumental": True,
                    "bpm": override.get("bpm", _suggest_bpm(segment["target"])),
                    "keyscale": override.get("keyscale", ""),
                    "timesignature": override.get("timesignature", "4"),
                    "duration_s": generated_duration,
                    "inference_steps": defaults["inference_steps"],
                    "guidance_scale": defaults["guidance_scale"],
                    "thinking": False,
                    "use_cot_metas": False,
                    "use_cot_caption": False,
                    "use_cot_lyrics": False,
                    "use_cot_language": False,
                    "audio_format": "wav",
                    "batch_size": defaults["candidates_per_segment"],
                    "use_random_seed": False,
                    "seeds": seeds,
                },
            }
        )
    return requests


def _build_instrumental_caption(segment: dict[str, Any]) -> str:
    query = str(segment.get("text_query_en") or segment["text_query"]).strip()
    target = segment["target"]
    mood = []
    mood.append(
        "bright"
        if target["valence"] >= 0.67
        else "somber"
        if target["valence"] <= 0.33
        else "balanced"
    )
    mood.append(
        "high energy"
        if target["arousal"] >= 0.67
        else "low energy"
        if target["arousal"] <= 0.33
        else "moderate energy"
    )
    mood.append(
        "tense"
        if target["tension"] >= 0.67
        else "relaxed"
        if target["tension"] <= 0.33
        else "light tension"
    )
    narration = (
        "sparse arrangement, restrained midrange, supports spoken narration"
        if segment["narration_density"] >= 0.6
        else "background score that leaves space for dialogue"
    )
    return (
        f"{query}. Instrumental background score, no vocals. "
        f"{', '.join(mood)}; {narration}."
    )


def _suggest_bpm(target: dict[str, Any]) -> int:
    activity = 0.6 * float(target["arousal"]) + 0.4 * float(target["movement"])
    return max(55, min(150, round(55 + 95 * activity)))


def _segment_output_subdir(index: int, segment_id: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", segment_id).strip("-._")
    if not slug:
        slug = "segment"
    return f"{index:03d}-{slug[:48]}"


def _normalize_generated_outputs(
    rows: Any, request: dict[str, Any], output_dir: Path
) -> list[dict[str, Any]]:
    if not isinstance(rows, list):
        raise ContractError("ACE-Step backend must return a list of outputs")
    expected_seeds = request["generation"]["seeds"]
    if len(rows) != len(expected_seeds):
        raise ContractError("ACE-Step output count does not match requested candidates")
    normalized: list[dict[str, Any]] = []
    seen_seeds: set[int] = set()
    for raw in rows:
        if not isinstance(raw, dict):
            raise ContractError("ACE-Step output must be an object")
        path = Path(str(raw.get("path") or "")).expanduser().resolve()
        if path.suffix.lower() != ".wav" or not _is_relative_to(path, output_dir):
            raise ContractError(
                "ACE-Step output must be a WAV inside its segment directory"
            )
        seed = _integer(raw.get("seed"), "ACE-Step output seed")
        if seed not in expected_seeds or seed in seen_seeds:
            raise ContractError("ACE-Step output seed does not match its request")
        seen_seeds.add(seed)
        duration = _wav_duration(path)
        requested_duration = request["generation"]["duration_s"]
        tolerance = max(1.0, requested_duration * 0.05)
        if abs(duration - requested_duration) > tolerance:
            raise ContractError("ACE-Step output duration is outside tolerance")
        segment_hash = canonical_json_sha256(request["segment_id"])[:10]
        normalized.append(
            {
                "asset_id": f"acestep-{segment_hash}-{seed}",
                "seed": seed,
                "duration_s": duration,
                "audio": artifact_reference(path, "ACE-Step generated WAV"),
                "upstream": {
                    "key": str(raw.get("key") or ""),
                    "bpm": _optional_integer(raw.get("bpm"), "ACE-Step output BPM"),
                },
            }
        )
    if seen_seeds != set(expected_seeds):
        raise ContractError("ACE-Step outputs do not cover every requested seed")
    return sorted(normalized, key=lambda row: expected_seeds.index(row["seed"]))


def _validate_generated_outputs(
    outputs: list[dict[str, Any]], request: dict[str, Any], output_root: Path
) -> None:
    expected_dir = output_root / request["output_subdir"]
    if len(outputs) != request["generation"]["batch_size"]:
        raise ContractError("music generation receipt output count is invalid")
    seeds: list[int] = []
    for output in outputs:
        if not isinstance(output, dict):
            raise ContractError("music generation receipt output must be an object")
        seed = _integer(output.get("seed"), "music generation output seed")
        path = validate_artifact_reference(
            _object(output.get("audio")), "ACE-Step generated WAV"
        )
        if path.suffix.lower() != ".wav" or not _is_relative_to(path, expected_dir):
            raise ContractError("music generation output path is outside its request")
        duration = _positive_number(
            output.get("duration_s"), "music generation output duration"
        )
        if duration != _wav_duration(path):
            raise ContractError("music generation output duration drifted")
        expected_asset_id = (
            f"acestep-{canonical_json_sha256(request['segment_id'])[:10]}-{seed}"
        )
        if output.get("asset_id") != expected_asset_id:
            raise ContractError("music generation asset id is invalid")
        upstream = _object(output.get("upstream"))
        if set(upstream) != {"key", "bpm"}:
            raise ContractError("music generation upstream metadata is invalid")
        _optional_integer(upstream.get("bpm"), "music generation upstream BPM")
        seeds.append(seed)
    if seeds != request["generation"]["seeds"]:
        raise ContractError("music generation receipt seeds are out of order")


def _wav_duration(path: Path) -> float:
    try:
        with wave.open(str(path), "rb") as handle:
            frame_rate = handle.getframerate()
            frames = handle.getnframes()
    except (OSError, EOFError, wave.Error) as exc:
        raise ContractError(
            f"ACE-Step output is not a readable PCM WAV: {path}"
        ) from exc
    if frame_rate <= 0 or frames <= 0:
        raise ContractError("ACE-Step output WAV has no measurable audio")
    return round(frames / frame_rate, 6)


def _validate_output_root(output: Path, source: Path, checkpoints: Path) -> None:
    for protected in (source.resolve(), checkpoints.resolve()):
        if _is_relative_to(output, protected) or _is_relative_to(protected, output):
            raise ContractError(
                "ACE-Step output root must not overlap source or checkpoints"
            )


def _ensure_reviewed_package_import(source_root: Path) -> None:
    existing = sys.modules.get("acestep")
    if existing is not None:
        module_file = Path(str(getattr(existing, "__file__", ""))).resolve()
        if not _is_relative_to(module_file, source_root):
            raise ContractError("another ACE-Step package is already loaded")
    source_text = str(source_root)
    if source_text not in sys.path:
        sys.path.insert(0, source_text)
    importlib.invalidate_caches()
    module = importlib.import_module("acestep")
    module_file = Path(str(getattr(module, "__file__", ""))).resolve()
    if not _is_relative_to(module_file, source_root):
        raise ContractError("ACE-Step import did not resolve to the reviewed source")


def _validate_requested_device(torch: Any, device: str) -> None:
    if device == "cuda" and not torch.cuda.is_available():
        raise ContractError("CUDA was requested for ACE-Step but is unavailable")
    if device == "cpu":
        return


@contextmanager
def _disable_acestep_model_mutation(handler: Any):
    def local_models_only(_handler: Any, **_kwargs: Any) -> None:
        return None

    def reviewed_code_only(_handler: Any, *_args: Any, **_kwargs: Any) -> None:
        return None

    original_precheck = handler._ensure_models_present
    original_sync = handler._sync_model_code_if_needed
    handler._ensure_models_present = MethodType(local_models_only, handler)
    handler._sync_model_code_if_needed = MethodType(reviewed_code_only, handler)
    try:
        yield
    finally:
        handler._ensure_models_present = original_precheck
        handler._sync_model_code_if_needed = original_sync


@contextmanager
def _acestep_environment(checkpoint_root: Path):
    values = {
        "ACESTEP_CHECKPOINTS_DIR": str(checkpoint_root),
        "HF_HOME": str(checkpoint_root / ".cache" / "huggingface"),
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
    }
    previous = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _safe_error(exc: Exception) -> str:
    message = str(exc).strip() or exc.__class__.__name__
    return message[:1000]


def _object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _required_text(value: Any, label: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise ContractError(f"{label} is required")
    return result


def _choice(value: Any, allowed: set[str], label: str) -> str:
    result = str(value or "").strip()
    if result not in allowed:
        raise ContractError(f"{label} must be one of: {', '.join(sorted(allowed))}")
    return result


def _boolean(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise ContractError(f"{label} must be a boolean")
    return value


def _integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContractError(f"{label} must be an integer")
    return value


def _optional_integer(value: Any, label: str) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ContractError(f"{label} must be an integer")
    try:
        result = int(value)
        numeric = float(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{label} must be an integer") from exc
    if not math.isfinite(numeric) or numeric != result:
        raise ContractError(f"{label} must be an integer")
    return result


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise ContractError(f"{label} must be a finite number")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{label} must be a finite number") from exc
    if not math.isfinite(result):
        raise ContractError(f"{label} must be a finite number")
    return round(result, 6)


def _positive_number(value: Any, label: str) -> float:
    result = _number(value, label)
    if result <= 0:
        raise ContractError(f"{label} must be positive")
    return result
