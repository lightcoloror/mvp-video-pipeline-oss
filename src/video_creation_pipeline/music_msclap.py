from __future__ import annotations

import importlib.util
import math
import os
import random
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Protocol

from .contract_io import (
    artifact_reference,
    atomic_write_json,
    canonical_json_sha256,
    payload_sha256,
    read_json_object,
    validate_artifact_reference,
)
from .errors import ContractError
from .music_pipeline import (
    PINNED_MSCLAP_PROVIDER_ID,
    build_music_retrieval_results,
    validate_music_asset_profile,
    validate_music_intent_curve,
)

MSCLAP_RETRIEVAL_PLAN_SCHEMA = "video_creation_pipeline.msclap_retrieval_plan.v1"
MUSIC_ANALYSIS_AUDIO_MAP_SCHEMA = "video_creation_pipeline.music_analysis_audio_map.v1"
MSCLAP_SOURCE_COMMIT = "e8a6467b87cd85716e20c6a008126150d9740be0"
MSCLAP_PACKAGE_VERSION = "1.3.3"
MSCLAP_MODEL_VERSION = "2023"
MSCLAP_MAX_AUDIO_SECONDS = 7.0

REVIEWED_SOURCE_SHA256 = {
    "msclap/CLAPWrapper.py": (
        "f44f172f375eadb61fde6f06bbb927f56373bbce1e9687e9b1188defc9d35430"
    ),
    "msclap/__init__.py": (
        "21484bc8aa59d12a6b39f548eff9279fafd1fbae5cbbf517a3aaee0087691cec"
    ),
    "msclap/configs/config_2023.yml": (
        "41fbc893997f75fe144d93bf2ec44d5ca27fe61beb8870dd815a53c064cb030f"
    ),
}


class SimilarityBackend(Protocol):
    def similarities(
        self, audio_paths: list[Path], queries: list[str]
    ) -> list[list[float]]: ...


def build_msclap_retrieval_plan(
    intent_path: str | Path,
    profile_paths: list[str | Path],
    audio_map_path: str | Path,
    source_root: str | Path,
    model_path: str | Path,
    license_snapshot_path: str | Path,
    *,
    model_license: str,
    commercial_use_allowed: bool,
    purpose: str = "personal",
    hf_cache_dir: str | Path,
    device: str = "cpu",
    random_seed: int = 20260803,
) -> dict[str, Any]:
    intent_file = Path(intent_path).expanduser().resolve()
    intent = read_json_object(intent_file, "music intent curve")
    validate_music_intent_curve(intent)
    profiles, profile_references = _load_profiles(profile_paths)
    audio_map_file = Path(audio_map_path).expanduser().resolve()
    audio_map = read_json_object(audio_map_file, "music analysis audio map")
    normalized_windows = _normalize_audio_map(audio_map, profiles)
    source = Path(source_root).expanduser().resolve()
    source_files = _reviewed_source_files(source)
    model = artifact_reference(model_path, "Microsoft CLAP model weights")
    license_snapshot = artifact_reference(
        license_snapshot_path, "Microsoft CLAP model license snapshot"
    )
    normalized_purpose = _choice(purpose, {"personal", "commercial"}, "purpose")
    commercial = _boolean(commercial_use_allowed, "commercial model use")
    if normalized_purpose == "commercial" and not commercial:
        raise ContractError(
            "commercial Microsoft CLAP use requires an affirmative model license"
        )
    cache = Path(hf_cache_dir).expanduser().resolve()
    if not cache.is_dir():
        raise ContractError("Microsoft CLAP requires an existing offline HF cache")
    normalized_device = _choice(device, {"cpu", "cuda"}, "Microsoft CLAP device")
    if isinstance(random_seed, bool) or not isinstance(random_seed, int):
        raise ContractError("Microsoft CLAP random seed must be an integer")

    plan: dict[str, Any] = {
        "schema": MSCLAP_RETRIEVAL_PLAN_SCHEMA,
        "intent_curve": artifact_reference(intent_file, "music intent curve"),
        "intent_curve_sha256": intent["intent_curve_sha256"],
        "profiles": profile_references,
        "analysis_audio_map": artifact_reference(
            audio_map_file, "music analysis audio map"
        ),
        "analysis_windows": normalized_windows,
        "runtime": {
            "provider_id": PINNED_MSCLAP_PROVIDER_ID,
            "package": "msclap",
            "package_version": MSCLAP_PACKAGE_VERSION,
            "model_version": MSCLAP_MODEL_VERSION,
            "source_commit": MSCLAP_SOURCE_COMMIT,
            "source_root": str(source),
            "source_files": source_files,
            "source_manifest_sha256": canonical_json_sha256(source_files),
            "model_weights": model,
            "model_license": _required_text(model_license, "model license"),
            "model_license_snapshot": license_snapshot,
            "commercial_use_allowed": commercial,
            "purpose": normalized_purpose,
            "hf_cache_dir": str(cache),
            "device": normalized_device,
            "random_seed": random_seed,
        },
        "query_mapping": {
            "primary": "text_query_en",
            "fallback": "text_query",
            "similarity": "cosine_mapped_from_minus_one_plus_one_to_zero_one",
            "window_aggregation": "mean_of_explicit_audio_clips",
        },
        "execution_boundary": {
            "execution_performed": False,
            "external_network_allowed": False,
            "automatic_model_download": False,
            "automatic_local_cloud_fallback": False,
            "ffmpeg_executed_by_adapter": False,
            "source_music_modified": False,
            "automatic_publish": False,
        },
    }
    plan["plan_sha256"] = payload_sha256(plan, "plan_sha256")
    validate_msclap_retrieval_plan(plan)
    return plan


def validate_msclap_retrieval_plan(plan: dict[str, Any]) -> None:
    if plan.get("schema") != MSCLAP_RETRIEVAL_PLAN_SCHEMA:
        raise ContractError("unsupported Microsoft CLAP retrieval plan schema")
    if plan.get("plan_sha256") != payload_sha256(plan, "plan_sha256"):
        raise ContractError("Microsoft CLAP retrieval plan integrity check failed")
    intent_path = validate_artifact_reference(
        _object(plan.get("intent_curve")), "music intent curve"
    )
    intent = read_json_object(intent_path, "music intent curve")
    validate_music_intent_curve(intent)
    if plan.get("intent_curve_sha256") != intent.get("intent_curve_sha256"):
        raise ContractError("Microsoft CLAP plan targets another intent curve")
    profiles = _validate_profile_references(plan.get("profiles"))
    audio_map_path = validate_artifact_reference(
        _object(plan.get("analysis_audio_map")), "music analysis audio map"
    )
    audio_map = read_json_object(audio_map_path, "music analysis audio map")
    expected_windows = _normalize_audio_map(audio_map, profiles)
    if plan.get("analysis_windows") != expected_windows:
        raise ContractError(
            "Microsoft CLAP analysis windows drifted from the audio map"
        )

    runtime = _object(plan.get("runtime"))
    if (
        runtime.get("provider_id") != PINNED_MSCLAP_PROVIDER_ID
        or runtime.get("package") != "msclap"
        or runtime.get("package_version") != MSCLAP_PACKAGE_VERSION
        or runtime.get("model_version") != MSCLAP_MODEL_VERSION
        or runtime.get("source_commit") != MSCLAP_SOURCE_COMMIT
    ):
        raise ContractError("Microsoft CLAP runtime is not the reviewed source")
    source = Path(str(runtime.get("source_root") or "")).expanduser().resolve()
    expected_source = _reviewed_source_files(source)
    if runtime.get("source_files") != expected_source or runtime.get(
        "source_manifest_sha256"
    ) != canonical_json_sha256(expected_source):
        raise ContractError("Microsoft CLAP reviewed source changed after planning")
    validate_artifact_reference(
        _object(runtime.get("model_weights")), "Microsoft CLAP model weights"
    )
    validate_artifact_reference(
        _object(runtime.get("model_license_snapshot")),
        "Microsoft CLAP model license snapshot",
    )
    _required_text(runtime.get("model_license"), "model license")
    commercial = _boolean(runtime.get("commercial_use_allowed"), "commercial model use")
    purpose = _choice(runtime.get("purpose"), {"personal", "commercial"}, "purpose")
    if purpose == "commercial" and not commercial:
        raise ContractError("Microsoft CLAP model is not cleared for commercial use")
    cache = Path(str(runtime.get("hf_cache_dir") or "")).expanduser().resolve()
    if not cache.is_dir():
        raise ContractError("Microsoft CLAP offline HF cache is missing")
    _choice(runtime.get("device"), {"cpu", "cuda"}, "Microsoft CLAP device")
    seed = runtime.get("random_seed")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ContractError("Microsoft CLAP random seed is invalid")
    mapping = _object(plan.get("query_mapping"))
    if mapping != {
        "primary": "text_query_en",
        "fallback": "text_query",
        "similarity": "cosine_mapped_from_minus_one_plus_one_to_zero_one",
        "window_aggregation": "mean_of_explicit_audio_clips",
    }:
        raise ContractError("Microsoft CLAP query mapping is invalid")
    boundary = _object(plan.get("execution_boundary"))
    required_false = (
        "execution_performed",
        "external_network_allowed",
        "automatic_model_download",
        "automatic_local_cloud_fallback",
        "ffmpeg_executed_by_adapter",
        "source_music_modified",
        "automatic_publish",
    )
    if not all(boundary.get(field) is False for field in required_false):
        raise ContractError("Microsoft CLAP execution boundary is invalid")


def run_msclap_retrieval(
    plan_path: str | Path,
    *,
    backend: SimilarityBackend | None = None,
) -> dict[str, Any]:
    source = Path(plan_path).expanduser().resolve()
    plan = read_json_object(source, "Microsoft CLAP retrieval plan")
    validate_msclap_retrieval_plan(plan)
    intent_path = validate_artifact_reference(
        _object(plan.get("intent_curve")), "music intent curve"
    )
    intent = read_json_object(intent_path, "music intent curve")
    windows = plan["analysis_windows"]
    clips = [clip for window in windows for clip in window["clips"]]
    queries = [
        str(segment.get("text_query_en") or segment["text_query"])
        for segment in intent["segments"]
    ]
    engine = backend or _MsClapBackend(plan)
    matrix = engine.similarities(
        [Path(clip["artifact"]["path"]).resolve() for clip in clips], queries
    )
    normalized = _normalize_similarity_matrix(matrix, len(queries), len(clips))
    matches: list[dict[str, Any]] = []
    offsets: list[tuple[dict[str, Any], int, int]] = []
    cursor = 0
    for window in windows:
        end = cursor + len(window["clips"])
        offsets.append((window, cursor, end))
        cursor = end
    for query_index, segment in enumerate(intent["segments"]):
        query_text = queries[query_index]
        for window, start, end in offsets:
            score = round(sum(normalized[query_index][start:end]) / (end - start), 6)
            matches.append(
                {
                    "segment_id": segment["segment_id"],
                    "asset_id": window["asset_id"],
                    "window_id": window["window_id"],
                    "semantic_similarity": score,
                    "continuity_diversity": 0.5,
                    "query_text": query_text,
                }
            )
    runtime = plan["runtime"]
    return build_music_retrieval_results(
        intent_path,
        matches,
        provider={
            "id": PINNED_MSCLAP_PROVIDER_ID,
            "model": f"msclap-{runtime['model_version']}",
            "version": runtime["package_version"],
            "source_commit": runtime["source_commit"],
            "execution_location": "local",
            "model_license": runtime["model_license"],
            "authorization_reference": str(source),
            "model_sha256": runtime["model_weights"]["sha256"],
            "model_license_snapshot_sha256": runtime["model_license_snapshot"][
                "sha256"
            ],
            "commercial_use_allowed": runtime["commercial_use_allowed"],
            "source_sha256": runtime["source_manifest_sha256"],
            "adapter_plan_sha256": plan["plan_sha256"],
            "network_execution_performed": False,
            "automatic_model_download": False,
        },
    )


def write_msclap_contract(payload: dict[str, Any], output_path: str | Path) -> Path:
    validate_msclap_retrieval_plan(payload)
    return atomic_write_json(payload, output_path)


class _MsClapBackend:
    def __init__(self, plan: dict[str, Any]) -> None:
        runtime = plan["runtime"]
        source_root = Path(runtime["source_root"])
        init_path = source_root / "msclap" / "__init__.py"
        module_name = f"_video_creation_msclap_{plan['plan_sha256'][:12]}"
        with _offline_huggingface_environment(Path(runtime["hf_cache_dir"])):
            try:
                spec = importlib.util.spec_from_file_location(
                    module_name,
                    init_path,
                    submodule_search_locations=[str(init_path.parent)],
                )
                if spec is None or spec.loader is None:
                    raise ImportError("cannot create the reviewed msclap module spec")
                module = importlib.util.module_from_spec(spec)
                sys.modules[module_name] = module
                spec.loader.exec_module(module)
                import torch

                if runtime["device"] == "cuda" and not torch.cuda.is_available():
                    raise ContractError(
                        "CUDA was requested for Microsoft CLAP but is unavailable"
                    )
                self._torch = torch
                self._model = module.CLAP(
                    model_fp=runtime["model_weights"]["path"],
                    version=runtime["model_version"],
                    use_cuda=runtime["device"] == "cuda",
                )
                self._seed = runtime["random_seed"]
            except ContractError:
                raise
            except Exception as exc:
                raise ContractError(
                    "Microsoft CLAP could not initialize from explicit offline assets: "
                    f"{exc}"
                ) from exc

    def similarities(
        self, audio_paths: list[Path], queries: list[str]
    ) -> list[list[float]]:
        random.seed(self._seed)
        try:
            import numpy as np

            np.random.seed(self._seed % (2**32 - 1))
            self._torch.manual_seed(self._seed)
            audio = self._model.get_audio_embeddings(
                [str(path) for path in audio_paths]
            )
            text = self._model.get_text_embeddings(queries)
            audio = self._torch.nn.functional.normalize(audio, dim=-1)
            text = self._torch.nn.functional.normalize(text, dim=-1)
            cosine = text @ audio.T
            return [
                [max(0.0, min(1.0, (float(value) + 1.0) / 2.0)) for value in row]
                for row in cosine.detach().cpu().tolist()
            ]
        except Exception as exc:
            raise ContractError(
                f"Microsoft CLAP offline inference failed: {exc}"
            ) from exc


def _load_profiles(
    paths: list[str | Path],
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    profiles: dict[str, dict[str, Any]] = {}
    references: list[dict[str, Any]] = []
    for raw in paths:
        path = Path(raw).expanduser().resolve()
        profile = read_json_object(path, "music asset profile")
        validate_music_asset_profile(profile)
        asset_id = profile["asset_id"]
        if asset_id in profiles:
            raise ContractError("Microsoft CLAP profile ids must be unique")
        profiles[asset_id] = profile
        references.append(
            {
                "asset_id": asset_id,
                "artifact": artifact_reference(path, "music asset profile"),
                "profile_sha256": profile["profile_sha256"],
            }
        )
    if not profiles:
        raise ContractError("Microsoft CLAP retrieval requires profiles")
    return profiles, references


def _validate_profile_references(value: Any) -> dict[str, dict[str, Any]]:
    rows = value if isinstance(value, list) else []
    profiles: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ContractError("Microsoft CLAP profile reference must be an object")
        path = validate_artifact_reference(
            _object(row.get("artifact")), "music asset profile"
        )
        profile = read_json_object(path, "music asset profile")
        validate_music_asset_profile(profile)
        asset_id = profile["asset_id"]
        if (
            row.get("asset_id") != asset_id
            or row.get("profile_sha256") != profile.get("profile_sha256")
            or asset_id in profiles
        ):
            raise ContractError("Microsoft CLAP profile reference drifted")
        profiles[asset_id] = profile
    if not profiles:
        raise ContractError("Microsoft CLAP retrieval requires profile references")
    return profiles


def _normalize_audio_map(
    payload: dict[str, Any], profiles: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    if payload.get("schema") != MUSIC_ANALYSIS_AUDIO_MAP_SCHEMA:
        raise ContractError("unsupported music analysis audio map schema")
    expected: dict[tuple[str, str], dict[str, Any]] = {}
    for asset_id, profile in profiles.items():
        for window in profile["windows"]:
            expected[(asset_id, window["window_id"])] = window
    rows = payload.get("windows")
    if not isinstance(rows, list) or not rows:
        raise ContractError("music analysis audio map requires windows")
    normalized: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for raw in rows:
        if not isinstance(raw, dict):
            raise ContractError("music analysis audio window must be an object")
        asset_id = _required_text(raw.get("asset_id"), "music asset id")
        window_id = _required_text(raw.get("window_id"), "music window id")
        key = (asset_id, window_id)
        profile_window = expected.get(key)
        if profile_window is None or key in seen:
            raise ContractError("music analysis audio window identity is invalid")
        seen.add(key)
        clips = raw.get("clips")
        if not isinstance(clips, list) or not clips:
            raise ContractError("music analysis window requires explicit audio clips")
        normalized_clips: list[dict[str, Any]] = []
        for clip in clips:
            if not isinstance(clip, dict):
                raise ContractError("music analysis clip must be an object")
            start = _number(clip.get("source_start_s"), "analysis clip start")
            end = _number(clip.get("source_end_s"), "analysis clip end")
            if (
                start < profile_window["start_s"] - 0.001
                or end > profile_window["end_s"] + 0.001
                or end <= start
                or end - start > MSCLAP_MAX_AUDIO_SECONDS + 0.001
            ):
                raise ContractError(
                    "Microsoft CLAP analysis clips must be within their window and at most 7 seconds"
                )
            normalized_clips.append(
                {
                    "source_start_s": start,
                    "source_end_s": end,
                    "artifact": artifact_reference(
                        clip.get("path"), "Microsoft CLAP analysis audio clip"
                    ),
                }
            )
        normalized.append(
            {
                "asset_id": asset_id,
                "window_id": window_id,
                "clips": normalized_clips,
            }
        )
    if seen != set(expected):
        raise ContractError("music analysis audio map must cover every profile window")
    return sorted(normalized, key=lambda row: (row["asset_id"], row["window_id"]))


def _reviewed_source_files(source_root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for relative, expected_sha256 in REVIEWED_SOURCE_SHA256.items():
        path = source_root / Path(relative)
        reference = artifact_reference(path, f"reviewed Microsoft CLAP {relative}")
        if reference["sha256"] != expected_sha256:
            raise ContractError(
                f"Microsoft CLAP reviewed source hash mismatch: {relative}"
            )
        rows.append({"relative_path": relative, "artifact": reference})
    return rows


def _normalize_similarity_matrix(
    value: Any, query_count: int, clip_count: int
) -> list[list[float]]:
    if not isinstance(value, list) or len(value) != query_count:
        raise ContractError("Microsoft CLAP similarity row count is invalid")
    normalized: list[list[float]] = []
    for row in value:
        if not isinstance(row, list) or len(row) != clip_count:
            raise ContractError("Microsoft CLAP similarity column count is invalid")
        normalized.append([_ratio(item, "Microsoft CLAP similarity") for item in row])
    return normalized


@contextmanager
def _offline_huggingface_environment(cache_dir: Path):
    keys = {
        "HF_HOME": str(cache_dir),
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
    }
    previous = {key: os.environ.get(key) for key in keys}
    os.environ.update(keys)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


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


def _ratio(value: Any, label: str) -> float:
    result = _number(value, label)
    if result < 0 or result > 1:
        raise ContractError(f"{label} must be between zero and one")
    return result
