from __future__ import annotations

import json
import math
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from .contract_io import (
    artifact_reference,
    atomic_write_json,
    payload_sha256,
    read_json_object,
    validate_artifact_reference,
)
from .errors import ContractError
from .execution_receipts import FFMPEG_OUTLET_ID
from .music_timing_qa import (
    build_speech_priority_windows,
    classify_music_family,
    spoken_bgm_policy,
)

MUSIC_INTENT_CURVE_SCHEMA = "video_creation_pipeline.music_intent_curve.v1"
MUSIC_ASSET_PROFILE_SCHEMA = "video_creation_pipeline.music_asset_profile.v1"
MUSIC_RETRIEVAL_RESULTS_SCHEMA = "video_creation_pipeline.music_retrieval_results.v1"
MUSIC_CANDIDATE_SET_SCHEMA = "video_creation_pipeline.music_candidate_set.v1"
MUSIC_EDIT_PLAN_V1_SCHEMA = "video_creation_pipeline.music_edit_plan.v1"
MUSIC_EDIT_PLAN_SCHEMA = "video_creation_pipeline.music_edit_plan.v2"
PINNED_MSCLAP_PROVIDER_ID = "microsoft-clap-local-pinned"

_SPOKEN_BGM_POLICY_PATH = (
    Path(__file__).with_name("policies") / "spoken-bgm-policy.v1.json"
)
_SCHEMA_ROOT = Path(__file__).with_name("schemas")
_SRT_TIMING_PATTERN = re.compile(
    r"(?P<start>\d{1,2}:\d{2}:\d{2}[,.]\d{3})\s*-->\s*"
    r"(?P<end>\d{1,2}:\d{2}:\d{2}[,.]\d{3})"
)

AFFECT_DIMENSIONS = (
    "valence",
    "arousal",
    "tension",
    "intimacy_scale",
    "warmth",
    "movement",
    "engagement",
)
RELATIONSHIP_MODES = {"match", "counterpoint", "neutral_support"}
VOCAL_POLICIES = {"instrumental_required", "instrumental_preferred", "allowed"}
PURPOSES = {"personal", "commercial"}
DEFAULT_WEIGHTS = {
    "semantic_similarity": 0.30,
    "affect_match": 0.20,
    "energy_arc_match": 0.20,
    "structure_fit": 0.15,
    "speech_compatibility": 0.10,
    "continuity_diversity": 0.05,
}


def build_music_intent_curve(
    source_path: str | Path,
    segments: Iterable[dict[str, Any]],
    *,
    created_by: str = "operator_or_authorized_provider",
) -> dict[str, Any]:
    normalized = _normalize_intent_segments(segments)
    payload: dict[str, Any] = {
        "schema": MUSIC_INTENT_CURVE_SCHEMA,
        "source": artifact_reference(source_path, "music intent source"),
        "created_by": _required_text(created_by, "music intent creator"),
        "segments": normalized,
        "authority_boundary": {
            "source_read_only": True,
            "mutates_vkp_timeline": False,
            "replaces_vkp_evidence": False,
            "automatic_local_cloud_fallback": False,
        },
    }
    payload["intent_curve_sha256"] = payload_sha256(payload, "intent_curve_sha256")
    validate_music_intent_curve(payload)
    return payload


def validate_music_intent_curve(payload: dict[str, Any]) -> None:
    if payload.get("schema") != MUSIC_INTENT_CURVE_SCHEMA:
        raise ContractError("unsupported music intent curve schema")
    if payload.get("intent_curve_sha256") != payload_sha256(
        payload, "intent_curve_sha256"
    ):
        raise ContractError("music intent curve integrity check failed")
    validate_artifact_reference(_object(payload.get("source")), "music intent source")
    if not str(payload.get("created_by") or "").strip():
        raise ContractError("music intent curve requires a creator")
    normalized = _normalize_intent_segments(payload.get("segments") or [])
    if payload.get("segments") != normalized:
        raise ContractError("music intent curve is not normalized")
    boundary = _object(payload.get("authority_boundary"))
    if (
        boundary.get("source_read_only") is not True
        or boundary.get("mutates_vkp_timeline") is not False
        or boundary.get("replaces_vkp_evidence") is not False
        or boundary.get("automatic_local_cloud_fallback") is not False
    ):
        raise ContractError("music intent authority boundary is invalid")


def build_music_asset_profile(
    audio_path: str | Path,
    *,
    asset_id: str,
    duration_s: float,
    windows: Iterable[dict[str, Any]],
    license_info: dict[str, Any],
    analyzer: dict[str, Any],
) -> dict[str, Any]:
    duration = _positive_number(duration_s, "music duration")
    normalized_windows = _normalize_windows(windows, duration)
    payload: dict[str, Any] = {
        "schema": MUSIC_ASSET_PROFILE_SCHEMA,
        "asset_id": _required_text(asset_id, "music asset id"),
        "source_audio": artifact_reference(audio_path, "music source audio"),
        "duration_s": duration,
        "license": _normalize_license(license_info),
        "analyzer": _normalize_analyzer(analyzer),
        "windows": normalized_windows,
        "authority_boundary": {
            "candidate_evidence_only": True,
            "source_audio_modified": False,
            "creates_music_library_authority": False,
        },
    }
    payload["profile_sha256"] = payload_sha256(payload, "profile_sha256")
    validate_music_asset_profile(payload)
    return payload


def validate_music_asset_profile(payload: dict[str, Any]) -> None:
    if payload.get("schema") != MUSIC_ASSET_PROFILE_SCHEMA:
        raise ContractError("unsupported music asset profile schema")
    if payload.get("profile_sha256") != payload_sha256(payload, "profile_sha256"):
        raise ContractError("music asset profile integrity check failed")
    _required_text(payload.get("asset_id"), "music asset id")
    validate_artifact_reference(
        _object(payload.get("source_audio")), "music source audio"
    )
    duration = _positive_number(payload.get("duration_s"), "music duration")
    if payload.get("license") != _normalize_license(_object(payload.get("license"))):
        raise ContractError("music license is not normalized")
    if payload.get("analyzer") != _normalize_analyzer(_object(payload.get("analyzer"))):
        raise ContractError("music analyzer provenance is not normalized")
    windows = _normalize_windows(payload.get("windows") or [], duration)
    if payload.get("windows") != windows:
        raise ContractError("music asset windows are not normalized")
    boundary = _object(payload.get("authority_boundary"))
    if (
        boundary.get("candidate_evidence_only") is not True
        or boundary.get("source_audio_modified") is not False
        or boundary.get("creates_music_library_authority") is not False
    ):
        raise ContractError("music profile authority boundary is invalid")
    if payload.get("licensed_asset_manifest") is not None:
        from .stock_media import validate_music_profile_license_binding

        validate_music_profile_license_binding(payload)


def build_music_retrieval_results(
    intent_path: str | Path,
    matches: Iterable[dict[str, Any]],
    *,
    provider: dict[str, Any],
) -> dict[str, Any]:
    intent_file = Path(intent_path).expanduser().resolve()
    intent = read_json_object(intent_file, "music intent curve")
    validate_music_intent_curve(intent)
    segment_ids = {row["segment_id"] for row in intent["segments"]}
    normalized_matches = _normalize_retrieval_matches(matches, segment_ids)
    payload: dict[str, Any] = {
        "schema": MUSIC_RETRIEVAL_RESULTS_SCHEMA,
        "intent_curve": artifact_reference(intent_file, "music intent curve"),
        "intent_curve_sha256": intent["intent_curve_sha256"],
        "provider": _normalize_retrieval_provider(provider),
        "matches": normalized_matches,
        "execution_boundary": {
            "candidate_only": True,
            "music_assets_modified": False,
            "automatic_provider_fallback": False,
            "publication_performed": False,
        },
    }
    payload["retrieval_results_sha256"] = payload_sha256(
        payload, "retrieval_results_sha256"
    )
    validate_music_retrieval_results(payload)
    return payload


def validate_music_retrieval_results(payload: dict[str, Any]) -> None:
    if payload.get("schema") != MUSIC_RETRIEVAL_RESULTS_SCHEMA:
        raise ContractError("unsupported music retrieval results schema")
    if payload.get("retrieval_results_sha256") != payload_sha256(
        payload, "retrieval_results_sha256"
    ):
        raise ContractError("music retrieval results integrity check failed")
    intent_path = validate_artifact_reference(
        _object(payload.get("intent_curve")), "music intent curve"
    )
    intent = read_json_object(intent_path, "music intent curve")
    validate_music_intent_curve(intent)
    if payload.get("intent_curve_sha256") != intent.get("intent_curve_sha256"):
        raise ContractError("music retrieval results bind a stale intent curve")
    if payload.get("provider") != _normalize_retrieval_provider(
        _object(payload.get("provider"))
    ):
        raise ContractError("music retrieval provider is not normalized")
    _validate_pinned_retrieval_provider(payload)
    normalized = _normalize_retrieval_matches(
        payload.get("matches") or [],
        {row["segment_id"] for row in intent["segments"]},
    )
    if payload.get("matches") != normalized:
        raise ContractError("music retrieval matches are not normalized")
    boundary = _object(payload.get("execution_boundary"))
    if (
        boundary.get("candidate_only") is not True
        or boundary.get("music_assets_modified") is not False
        or boundary.get("automatic_provider_fallback") is not False
        or boundary.get("publication_performed") is not False
    ):
        raise ContractError("music retrieval execution boundary is invalid")


def build_music_candidate_set(
    intent_path: str | Path,
    retrieval_results_path: str | Path,
    profile_paths: Iterable[str | Path],
    *,
    purpose: str = "personal",
    top_k: int = 5,
    weights: dict[str, Any] | None = None,
    retrieval_benchmark_path: str | Path | None = None,
) -> dict[str, Any]:
    intent_file = Path(intent_path).expanduser().resolve()
    retrieval_file = Path(retrieval_results_path).expanduser().resolve()
    intent = read_json_object(intent_file, "music intent curve")
    retrieval = read_json_object(retrieval_file, "music retrieval results")
    validate_music_intent_curve(intent)
    validate_music_retrieval_results(retrieval)
    profiles, references = _load_profiles(profile_paths)
    normalized_purpose = _choice(purpose, PURPOSES, "music usage purpose")
    normalized_top_k = _positive_int(top_k, "music candidate top_k")
    normalized_weights = _normalize_weights(weights or DEFAULT_WEIGHTS)
    benchmark_reference, benchmark_sha256 = _load_retrieval_benchmark(
        retrieval_benchmark_path, retrieval
    )
    segments, rejected = _score_candidates(
        intent,
        retrieval,
        profiles,
        purpose=normalized_purpose,
        top_k=normalized_top_k,
        weights=normalized_weights,
    )
    payload: dict[str, Any] = {
        "schema": MUSIC_CANDIDATE_SET_SCHEMA,
        "intent_curve": artifact_reference(intent_file, "music intent curve"),
        "retrieval_results": artifact_reference(
            retrieval_file, "music retrieval results"
        ),
        "retrieval_benchmark": benchmark_reference,
        "retrieval_benchmark_sha256": benchmark_sha256,
        "profiles": references,
        "purpose": normalized_purpose,
        "top_k": normalized_top_k,
        "weights": normalized_weights,
        "segments": segments,
        "rejected": rejected,
        "authority_boundary": {
            "candidate_only": True,
            "mutates_timeline": False,
            "mutates_music_metadata": False,
            "creates_second_index": False,
            "automatic_publish": False,
        },
    }
    payload["candidate_set_sha256"] = payload_sha256(payload, "candidate_set_sha256")
    validate_music_candidate_set(payload)
    return payload


def validate_music_candidate_set(payload: dict[str, Any]) -> None:
    if payload.get("schema") != MUSIC_CANDIDATE_SET_SCHEMA:
        raise ContractError("unsupported music candidate set schema")
    if payload.get("candidate_set_sha256") != payload_sha256(
        payload, "candidate_set_sha256"
    ):
        raise ContractError("music candidate set integrity check failed")
    intent_path = validate_artifact_reference(
        _object(payload.get("intent_curve")), "music intent curve"
    )
    retrieval_path = validate_artifact_reference(
        _object(payload.get("retrieval_results")), "music retrieval results"
    )
    intent = read_json_object(intent_path, "music intent curve")
    retrieval = read_json_object(retrieval_path, "music retrieval results")
    validate_music_intent_curve(intent)
    validate_music_retrieval_results(retrieval)
    benchmark_reference = payload.get("retrieval_benchmark")
    benchmark_path = (
        str(_object(benchmark_reference).get("path") or "")
        if benchmark_reference is not None
        else None
    )
    expected_benchmark_reference, expected_benchmark_sha256 = _load_retrieval_benchmark(
        benchmark_path, retrieval
    )
    if (
        benchmark_reference != expected_benchmark_reference
        or payload.get("retrieval_benchmark_sha256") != expected_benchmark_sha256
    ):
        raise ContractError("music candidate benchmark binding drifted")
    profile_rows = payload.get("profiles")
    if not isinstance(profile_rows, list) or not profile_rows:
        raise ContractError("music candidate set requires profiles")
    profiles: dict[str, dict[str, Any]] = {}
    expected_references: list[dict[str, Any]] = []
    for row in profile_rows:
        item = _object(row)
        path = validate_artifact_reference(
            _object(item.get("artifact")), "music asset profile"
        )
        profile = read_json_object(path, "music asset profile")
        validate_music_asset_profile(profile)
        asset_id = profile["asset_id"]
        if asset_id in profiles or item.get("asset_id") != asset_id:
            raise ContractError("music candidate profile identity is invalid")
        profiles[asset_id] = profile
        expected_references.append(
            {
                "asset_id": asset_id,
                "artifact": artifact_reference(path, "music asset profile"),
                "profile_sha256": profile["profile_sha256"],
            }
        )
    if payload.get("profiles") != expected_references:
        raise ContractError("music candidate profile references drifted")
    purpose = _choice(payload.get("purpose"), PURPOSES, "music usage purpose")
    top_k = _positive_int(payload.get("top_k"), "music candidate top_k")
    weights = _normalize_weights(_object(payload.get("weights")))
    expected_segments, expected_rejected = _score_candidates(
        intent,
        retrieval,
        profiles,
        purpose=purpose,
        top_k=top_k,
        weights=weights,
    )
    if (
        payload.get("segments") != expected_segments
        or payload.get("rejected") != expected_rejected
    ):
        raise ContractError("music candidate scores are inaccurate")
    boundary = _object(payload.get("authority_boundary"))
    if (
        boundary.get("candidate_only") is not True
        or boundary.get("mutates_timeline") is not False
        or boundary.get("mutates_music_metadata") is not False
        or boundary.get("creates_second_index") is not False
        or boundary.get("automatic_publish") is not False
    ):
        raise ContractError("music candidate authority boundary is invalid")


def build_music_edit_plan(
    candidate_set_path: str | Path,
    *,
    min_score: float = 0.72,
    min_margin: float = 0.05,
    word_timestamps_path: str | Path | None = None,
    subtitles_path: str | Path | None = None,
    contract_version: str = "v2",
) -> dict[str, Any]:
    candidate_file = Path(candidate_set_path).expanduser().resolve()
    candidate_set = read_json_object(candidate_file, "music candidate set")
    validate_music_candidate_set(candidate_set)
    threshold = _ratio(min_score, "minimum music score")
    margin = _ratio(min_margin, "minimum music score margin")
    if contract_version == "v1":
        return _build_music_edit_plan_v1(
            candidate_file,
            candidate_set,
            min_score=threshold,
            min_margin=margin,
        )
    if contract_version != "v2":
        raise ContractError("music edit plan contract_version must be v1 or v2")

    intent_path = validate_artifact_reference(
        _object(candidate_set.get("intent_curve")), "music intent curve"
    )
    intent = read_json_object(intent_path, "music intent curve")
    validate_music_intent_curve(intent)
    word_rows, word_reference = _load_word_timestamps(word_timestamps_path)
    subtitle_rows, subtitle_reference = _load_srt_subtitles(subtitles_path)
    policy = spoken_bgm_policy()
    selections, readiness = _select_music_v2(
        candidate_set,
        intent,
        min_score=threshold,
        min_margin=margin,
        word_timestamps=word_rows,
        subtitle_cues=subtitle_rows,
    )
    payload: dict[str, Any] = {
        "schema": MUSIC_EDIT_PLAN_SCHEMA,
        "candidate_set": artifact_reference(candidate_file, "music candidate set"),
        "mix_policy": {
            "policy_id": policy["policy_id"],
            "artifact": artifact_reference(
                _SPOKEN_BGM_POLICY_PATH, "spoken BGM policy"
            ),
            "three_family_vocabulary": [
                "warm_story",
                "clear_explainer",
                "restrained_risk",
            ],
            "ordinary_music_gain_db": float(
                policy["mix_profiles"]["ordinary"]["music_gain_db"]
            ),
            "speech_priority_music_gain_db": float(
                policy["mix_profiles"]["speech_priority"]["music_gain_db"]
            ),
            "automatic_music_boost_allowed": False,
        },
        "speech_timing_evidence": {
            "precedence": [
                "word_timestamps",
                "srt_subtitles",
                "semantic_segment_fallback",
            ],
            "word_timestamps": word_reference,
            "subtitles": subtitle_reference,
            "asr_executed_by_music_pipeline": False,
        },
        "selection_policy": {
            "mode": "automatic_high_confidence",
            "min_score": threshold,
            "min_margin": margin,
            "risk_family_precedence": True,
            "family_match_required": True,
            "music_change_only_at_semantic_boundary": True,
            "manual_review_required_for_high_confidence": False,
            "creative_review_optional": True,
        },
        "selections": selections,
        "readiness": readiness,
        "transition_policy": {
            "crossfade_s": float(policy["transition"]["crossfade_s"]),
            "phrase_alignment_priority": policy["transition"][
                "phrase_alignment_priority"
            ],
            "may_move_semantic_timeline": False,
        },
        "ffmpeg_handoff": {
            "outlet_id": FFMPEG_OUTLET_ID,
            "operation": "music_mix",
            "command_generated": False,
            "executed": False,
            "second_ffmpeg_orchestrator_created": False,
        },
        "authority_boundary": {
            "source_music_modified": False,
            "timeline_modified": False,
            "automatic_publish": False,
            "automatic_provider_fallback": False,
        },
    }
    payload["music_edit_plan_sha256"] = payload_sha256(
        payload, "music_edit_plan_sha256"
    )
    validate_music_edit_plan(payload)
    return payload


def _build_music_edit_plan_v1(
    candidate_file: Path,
    candidate_set: dict[str, Any],
    *,
    min_score: float,
    min_margin: float,
) -> dict[str, Any]:
    selections, readiness = _select_music_v1(
        candidate_set, min_score=min_score, min_margin=min_margin
    )
    payload: dict[str, Any] = {
        "schema": MUSIC_EDIT_PLAN_V1_SCHEMA,
        "candidate_set": artifact_reference(candidate_file, "music candidate set"),
        "selection_policy": {
            "mode": "automatic_high_confidence",
            "min_score": min_score,
            "min_margin": min_margin,
            "manual_review_required_for_high_confidence": False,
            "creative_review_optional": True,
        },
        "selections": selections,
        "readiness": readiness,
        "ffmpeg_handoff": {
            "outlet_id": FFMPEG_OUTLET_ID,
            "operation": "music_mix",
            "command_generated": False,
            "executed": False,
            "second_ffmpeg_orchestrator_created": False,
        },
        "authority_boundary": {
            "source_music_modified": False,
            "timeline_modified": False,
            "automatic_publish": False,
            "automatic_provider_fallback": False,
        },
    }
    payload["music_edit_plan_sha256"] = payload_sha256(
        payload, "music_edit_plan_sha256"
    )
    validate_music_edit_plan(payload)
    return payload


def build_music_mix_command(
    program_video: str | Path,
    music_path: str | Path,
    output_path: str | Path,
    selection: dict[str, Any],
    *,
    deterministic: bool = False,
) -> list[str]:
    if selection.get("status") == "intentional_none":
        if selection.get("music_mode") != "none" or not str(selection.get("selection_reason", "")).strip():
            raise ContractError("no-music selection needs an explicit editorial choice")
        timeline = _object(selection.get('timeline_interval'))
        if _nonnegative_number(timeline.get('start_s'),'no-music start') != 0:
            raise ContractError('music mix currently requires a timeline-zero selection')
        _positive_number(timeline.get('end_s'),'no-music duration')
        return ["ffmpeg", "-y", "-i", str(Path(program_video).resolve()), "-map", "0:v:0", "-map", "0:a:0?",
                "-c", "copy", "-movflags", "+faststart", str(Path(output_path).resolve())]
    timeline = _object(selection.get("timeline_interval"))
    source = _object(selection.get("source_interval"))
    duration = _positive_number(
        float(timeline.get("end_s") or 0) - float(timeline.get("start_s") or 0),
        "music timeline duration",
    )
    if abs(float(timeline.get("start_s") or 0)) > 0.001:
        raise ContractError("music mix currently requires a timeline-zero selection")
    fade_out = _nonnegative_number(selection.get("fade_out_s"), "music fade out")
    fade_out_start = max(0.0, duration - fade_out)

    program = Path(program_video).expanduser().resolve()
    music = Path(music_path).expanduser().resolve()
    output = Path(output_path).expanduser().resolve()
    command = ["ffmpeg", "-y", "-i", str(program)]
    if _boolean(selection.get("loop"), "music loop"):
        command.extend(["-stream_loop", "-1"])
    command.extend(["-i", str(music)])

    trim = f"atrim=start={float(source.get('start_s') or 0):.6f}"
    if not selection["loop"]:
        trim += f":end={float(source.get('end_s') or 0):.6f}"
    music_filters = [
        trim,
        "asetpts=PTS-STARTPTS",
        f"atrim=duration={duration:.6f}",
        f"afade=t=in:st=0:d={float(selection['fade_in_s']):.3f}",
        f"afade=t=out:st={fade_out_start:.3f}:d={fade_out:.3f}",
        f"volume={float(selection['gain_db']):.3f}dB",
    ]
    gain_envelope = selection.get("gain_envelope")
    envelope_filter = _gain_envelope_filter(
        gain_envelope,
        timeline_start_s=float(timeline.get("start_s") or 0),
        duration_s=duration,
        base_gain_db=float(selection["gain_db"]),
    )
    if envelope_filter:
        music_filters.append(envelope_filter)
    no_music = _no_music_filter(selection.get("no_music_windows", []),float(timeline.get("start_s") or 0),duration)
    if no_music:
        music_filters.append(no_music)
    ducking = _object(selection.get("ducking"))
    filter_parts = [f"[1:a]{','.join(music_filters)}[bgm]"]
    if _boolean(ducking.get("enabled"), "music ducking"):
        filter_parts.append(
            "[bgm][0:a]sidechaincompress="
            f"threshold={float(ducking['threshold']):.6f}:"
            f"ratio={float(ducking['ratio']):.3f}:"
            f"attack={int(ducking['attack_ms'])}:"
            f"release={int(ducking['release_ms'])}[music]"
        )
    else:
        filter_parts.append("[bgm]anull[music]")
    if gain_envelope is None:
        filter_parts.append(
            "[0:a][music]amix=inputs=2:duration=first:normalize=0[aout]"
        )
    else:
        output_policy = _object(spoken_bgm_policy().get("output"))
        limiter_peak_dbfs = _finite_number(
            output_policy.get("limiter_sample_peak_dbfs"),
            "music mix limiter sample peak",
        )
        if (
            limiter_peak_dbfs > -1.0
            or output_policy.get("limiter_may_boost") is not False
        ):
            raise ContractError("music mix output limiter policy is unsafe")
        limiter_linear = 10 ** (limiter_peak_dbfs / 20.0)
        deterministic_quantization = (
            ",aresample=osf=s16:dither_method=none" if deterministic else ""
        )
        filter_parts.append(
            "[0:a][music]amix=inputs=2:duration=first:normalize=0,"
            f"alimiter=limit={limiter_linear:.9f}:level=false"
            f"{deterministic_quantization}[aout]"
        )
    if deterministic:
        command.extend(
            [
                "-map_metadata",
                "-1",
                "-fflags",
                "+bitexact",
                "-flags:a",
                "+bitexact",
                "-filter_complex_threads",
                "1",
                "-threads",
                "1",
            ]
        )
    audio_codec = "alac" if deterministic else "aac"
    command.extend(
        [
            "-filter_complex",
            ";".join(filter_parts),
            "-map",
            "0:v:0",
            "-map",
            "[aout]",
            "-c:v",
            "copy",
            "-c:a",
            audio_codec,
            "-movflags",
            "+faststart",
            str(output),
        ]
    )
    return command


def build_ducked_music_stem_command(
    program_video: str | Path,
    music_path: str | Path,
    output_path: str | Path,
    selection: dict[str, Any],
    *,
    deterministic: bool = False,
) -> list[str]:
    """Render the exact post-envelope, post-ducking music stem used by the mix."""

    output = Path(output_path).expanduser().resolve()
    if selection.get("status") == "intentional_none":
        if selection.get('music_mode') != 'none' or not str(selection.get('selection_reason','')).strip():
            raise ContractError('no-music selection needs an explicit editorial choice')
        timeline=selection["timeline_interval"]
        duration=_positive_number(timeline["end_s"]-timeline["start_s"],"silent music duration")
        return ["ffmpeg","-y","-f","lavfi","-i","anullsrc=r=48000:cl=stereo","-t",str(duration),
                "-c:a","pcm_s24le",str(output)]
    mix_command = build_music_mix_command(
        program_video,
        music_path,
        output.with_suffix(".mp4"),
        selection,
        deterministic=deterministic,
    )
    graph_index = mix_command.index("-filter_complex")
    graph = str(mix_command[graph_index + 1])
    filter_parts = graph.split(";")
    if not filter_parts or not filter_parts[-1].endswith("[aout]"):
        raise ContractError("music mix graph has no auditable output stage")
    return [
        *mix_command[:graph_index],
        "-filter_complex",
        ";".join(filter_parts[:-1]),
        "-map",
        "[music]",
        "-c:a",
        "pcm_s24le",
        "-ar",
        "48000",
        "-ac",
        "2",
        str(output),
    ]


def validate_music_edit_plan(payload: dict[str, Any]) -> None:
    schema = payload.get("schema")
    if schema not in {MUSIC_EDIT_PLAN_V1_SCHEMA, MUSIC_EDIT_PLAN_SCHEMA}:
        raise ContractError("unsupported music edit plan schema")
    if schema == MUSIC_EDIT_PLAN_SCHEMA:
        _validate_music_edit_plan_v2_schema(payload)
    if payload.get("music_edit_plan_sha256") != payload_sha256(
        payload, "music_edit_plan_sha256"
    ):
        raise ContractError("music edit plan integrity check failed")
    candidate_path = validate_artifact_reference(
        _object(payload.get("candidate_set")), "music candidate set"
    )
    candidate_set = read_json_object(candidate_path, "music candidate set")
    validate_music_candidate_set(candidate_set)
    policy = _object(payload.get("selection_policy"))
    if (
        policy.get("mode") != "automatic_high_confidence"
        or policy.get("manual_review_required_for_high_confidence") is not False
        or policy.get("creative_review_optional") is not True
    ):
        raise ContractError("music selection policy is invalid")
    min_score = _ratio(policy.get("min_score"), "minimum music score")
    min_margin = _ratio(policy.get("min_margin"), "minimum music score margin")
    if schema == MUSIC_EDIT_PLAN_V1_SCHEMA:
        expected_selections, expected_readiness = _select_music_v1(
            candidate_set, min_score=min_score, min_margin=min_margin
        )
    else:
        if (
            policy.get("risk_family_precedence") is not True
            or policy.get("family_match_required") is not True
            or policy.get("music_change_only_at_semantic_boundary") is not True
        ):
            raise ContractError("music v2 family selection policy is incomplete")
        mix_policy = _object(payload.get("mix_policy"))
        current_policy = spoken_bgm_policy()
        policy_path = validate_artifact_reference(
            _object(mix_policy.get("artifact")), "spoken BGM policy"
        )
        if policy_path != _SPOKEN_BGM_POLICY_PATH.resolve():
            raise ContractError("music edit plan binds another spoken BGM policy")
        expected_mix_policy = {
            "policy_id": current_policy["policy_id"],
            "artifact": artifact_reference(
                _SPOKEN_BGM_POLICY_PATH, "spoken BGM policy"
            ),
            "three_family_vocabulary": [
                "warm_story",
                "clear_explainer",
                "restrained_risk",
            ],
            "ordinary_music_gain_db": float(
                current_policy["mix_profiles"]["ordinary"]["music_gain_db"]
            ),
            "speech_priority_music_gain_db": float(
                current_policy["mix_profiles"]["speech_priority"]["music_gain_db"]
            ),
            "automatic_music_boost_allowed": False,
        }
        if mix_policy != expected_mix_policy:
            raise ContractError("music edit plan mix policy drifted")
        evidence = _object(payload.get("speech_timing_evidence"))
        if (
            evidence.get("precedence")
            != [
                "word_timestamps",
                "srt_subtitles",
                "semantic_segment_fallback",
            ]
            or evidence.get("asr_executed_by_music_pipeline") is not False
        ):
            raise ContractError("music speech timing evidence policy is invalid")
        word_rows = _validate_word_timestamp_reference(evidence.get("word_timestamps"))
        subtitle_rows = _validate_subtitle_reference(evidence.get("subtitles"))
        intent_path = validate_artifact_reference(
            _object(candidate_set.get("intent_curve")), "music intent curve"
        )
        intent = read_json_object(intent_path, "music intent curve")
        validate_music_intent_curve(intent)
        expected_selections, expected_readiness = _select_music_v2(
            candidate_set,
            intent,
            min_score=min_score,
            min_margin=min_margin,
            word_timestamps=word_rows,
            subtitle_cues=subtitle_rows,
        )
        transition = _object(payload.get("transition_policy"))
        expected_transition = {
            "crossfade_s": float(current_policy["transition"]["crossfade_s"]),
            "phrase_alignment_priority": current_policy["transition"][
                "phrase_alignment_priority"
            ],
            "may_move_semantic_timeline": False,
        }
        if transition != expected_transition:
            raise ContractError("music transition policy drifted")
    if (
        payload.get("selections") != expected_selections
        or payload.get("readiness") != expected_readiness
    ):
        raise ContractError("music edit plan selection is inaccurate")
    handoff = _object(payload.get("ffmpeg_handoff"))
    if (
        handoff.get("outlet_id") != FFMPEG_OUTLET_ID
        or handoff.get("operation") != "music_mix"
        or handoff.get("command_generated") is not False
        or handoff.get("executed") is not False
        or handoff.get("second_ffmpeg_orchestrator_created") is not False
    ):
        raise ContractError("music edit plan FFmpeg handoff is invalid")
    boundary = _object(payload.get("authority_boundary"))
    if (
        boundary.get("source_music_modified") is not False
        or boundary.get("timeline_modified") is not False
        or boundary.get("automatic_publish") is not False
        or boundary.get("automatic_provider_fallback") is not False
    ):
        raise ContractError("music edit plan authority boundary is invalid")


def write_music_contract(payload: dict[str, Any], output_path: str | Path) -> Path:
    validators = {
        MUSIC_INTENT_CURVE_SCHEMA: validate_music_intent_curve,
        MUSIC_ASSET_PROFILE_SCHEMA: validate_music_asset_profile,
        MUSIC_RETRIEVAL_RESULTS_SCHEMA: validate_music_retrieval_results,
        MUSIC_CANDIDATE_SET_SCHEMA: validate_music_candidate_set,
        MUSIC_EDIT_PLAN_V1_SCHEMA: validate_music_edit_plan,
        MUSIC_EDIT_PLAN_SCHEMA: validate_music_edit_plan,
    }
    validator = validators.get(str(payload.get("schema") or ""))
    if validator is None:
        raise ContractError("unsupported music contract")
    validator(payload)
    return atomic_write_json(payload, output_path)


def _normalize_intent_segments(
    segments: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    rows = list(segments)
    if not rows:
        raise ContractError("music intent curve requires segments")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    previous_end = 0.0
    for index, raw in enumerate(rows, 1):
        if not isinstance(raw, dict):
            raise ContractError("music intent segment must be an object")
        segment_id = _required_text(raw.get("segment_id"), "music segment id")
        if segment_id in seen:
            raise ContractError("music segment ids must be unique")
        seen.add(segment_id)
        start = _nonnegative_number(raw.get("start_s"), "music segment start")
        end = _positive_number(raw.get("end_s"), "music segment end")
        if end <= start or (index > 1 and start < previous_end):
            raise ContractError(
                "music intent segments must be ordered and non-overlapping"
            )
        previous_end = end
        evidence_ids = _text_list(raw.get("evidence_ids"), "music evidence ids")
        normalized.append(
            {
                "segment_id": segment_id,
                "start_s": start,
                "end_s": end,
                "relationship_mode": _choice(
                    raw.get("relationship_mode"),
                    RELATIONSHIP_MODES,
                    "music relationship mode",
                ),
                "text_query": _required_text(raw.get("text_query"), "music text query"),
                "text_query_en": str(raw.get("text_query_en") or "").strip(),
                "target": _normalize_descriptors(raw.get("target")),
                "vocal_policy": _choice(
                    raw.get("vocal_policy", "instrumental_preferred"),
                    VOCAL_POLICIES,
                    "music vocal policy",
                ),
                "narration_density": _ratio(
                    raw.get("narration_density", 0.0), "narration density"
                ),
                "prohibited_tags": _text_list(
                    raw.get("prohibited_tags") or [],
                    "prohibited music tags",
                    allow_empty=True,
                ),
                "evidence_ids": evidence_ids,
            }
        )
        if "music_mode" in raw:
            normalized[-1]["music_mode"] = _choice(raw["music_mode"],{"music","none"},"music mode")
            normalized[-1]["music_mode_reason"] = _required_text(raw.get("music_mode_reason"),"music mode reason")
        if "no_music_windows" in raw:
            windows=raw["no_music_windows"]
            _no_music_filter(windows,start,end-start)
            normalized[-1]["no_music_windows"]=windows
    return normalized


def _normalize_windows(
    windows: Iterable[dict[str, Any]], duration_s: float
) -> list[dict[str, Any]]:
    rows = list(windows)
    if not rows:
        raise ContractError("music asset profile requires windows")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in rows:
        if not isinstance(raw, dict):
            raise ContractError("music analysis window must be an object")
        window_id = _required_text(raw.get("window_id"), "music window id")
        if window_id in seen:
            raise ContractError("music window ids must be unique")
        seen.add(window_id)
        start = _nonnegative_number(raw.get("start_s"), "music window start")
        end = _positive_number(raw.get("end_s"), "music window end")
        if end <= start or end > duration_s + 0.001:
            raise ContractError("music analysis window exceeds the audio duration")
        normalized.append(
            {
                "window_id": window_id,
                "start_s": start,
                "end_s": end,
                "structure_label": _required_text(
                    raw.get("structure_label", "unknown"), "music structure label"
                ),
                "structure_confidence": _ratio(
                    raw.get("structure_confidence", 0.5),
                    "music structure confidence",
                ),
                "descriptors": _normalize_descriptors(raw.get("descriptors")),
                "vocal_probability": _ratio(
                    raw.get("vocal_probability", 0.0), "music vocal probability"
                ),
                "tags": _text_list(
                    raw.get("tags") or [], "music window tags", allow_empty=True
                ),
                "loopable": _boolean(raw.get("loopable", False), "music loopable"),
            }
        )
    return normalized


def _normalize_license(value: dict[str, Any]) -> dict[str, Any]:
    status = _choice(
        value.get("status"), {"allowed", "restricted", "unknown"}, "music license"
    )
    result = {
        "status": status,
        "commercial_use_allowed": _boolean(
            value.get("commercial_use_allowed", False), "commercial music use"
        ),
        "license_uri": str(value.get("license_uri") or "").strip(),
        "license_snapshot": _required_text(
            value.get("license_snapshot"), "music license snapshot"
        ),
        "attribution": str(value.get("attribution") or "").strip(),
    }
    if status == "allowed" and not result["license_uri"]:
        raise ContractError("allowed music requires a license or ownership URI")
    return result


def _normalize_analyzer(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": _required_text(value.get("id"), "music analyzer id"),
        "model": _required_text(value.get("model"), "music analyzer model"),
        "version": _required_text(value.get("version"), "music analyzer version"),
        "source_commit": _required_text(
            value.get("source_commit"), "music analyzer source commit"
        ),
        "execution_location": _choice(
            value.get("execution_location"), {"local", "remote"}, "execution location"
        ),
        "model_license": _required_text(
            value.get("model_license"), "music analyzer model license"
        ),
        "automatic_fallback_used": _boolean(
            value.get("automatic_fallback_used", False),
            "music analyzer automatic fallback",
        ),
    }


def _normalize_retrieval_provider(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": _required_text(value.get("id"), "music retrieval provider id"),
        "model": _required_text(value.get("model"), "music retrieval model"),
        "version": _required_text(value.get("version"), "music retrieval version"),
        "source_commit": _required_text(
            value.get("source_commit"), "music retrieval source commit"
        ),
        "execution_location": _choice(
            value.get("execution_location"), {"local", "remote"}, "execution location"
        ),
        "model_license": _required_text(
            value.get("model_license"), "music retrieval model license"
        ),
        "authorization_reference": str(
            value.get("authorization_reference") or ""
        ).strip(),
        "model_sha256": _optional_sha256(
            value.get("model_sha256"), "music retrieval model sha256"
        ),
        "model_license_snapshot_sha256": _optional_sha256(
            value.get("model_license_snapshot_sha256"),
            "music retrieval model license snapshot sha256",
        ),
        "commercial_use_allowed": _boolean(
            value.get("commercial_use_allowed", False),
            "music retrieval commercial model use",
        ),
        "source_sha256": _optional_sha256(
            value.get("source_sha256"), "music retrieval source sha256"
        ),
        "adapter_plan_sha256": _optional_sha256(
            value.get("adapter_plan_sha256"),
            "music retrieval adapter plan sha256",
        ),
        "network_execution_performed": _boolean(
            value.get("network_execution_performed", False),
            "music retrieval network execution",
        ),
        "automatic_model_download": _boolean(
            value.get("automatic_model_download", False),
            "music retrieval automatic model download",
        ),
    }


def _validate_pinned_retrieval_provider(payload: dict[str, Any]) -> None:
    provider = _object(payload.get("provider"))
    if provider.get("id") != PINNED_MSCLAP_PROVIDER_ID:
        return

    from .music_msclap import validate_msclap_retrieval_plan

    plan_path = (
        Path(str(provider.get("authorization_reference") or "")).expanduser().resolve()
    )
    plan = read_json_object(plan_path, "Microsoft CLAP retrieval plan")
    validate_msclap_retrieval_plan(plan)
    runtime = _object(plan.get("runtime"))
    expected = {
        "model": f"msclap-{runtime.get('model_version')}",
        "version": runtime.get("package_version"),
        "source_commit": runtime.get("source_commit"),
        "execution_location": "local",
        "model_license": runtime.get("model_license"),
        "model_sha256": _object(runtime.get("model_weights")).get("sha256"),
        "model_license_snapshot_sha256": _object(
            runtime.get("model_license_snapshot")
        ).get("sha256"),
        "commercial_use_allowed": runtime.get("commercial_use_allowed"),
        "source_sha256": runtime.get("source_manifest_sha256"),
        "adapter_plan_sha256": plan.get("plan_sha256"),
        "network_execution_performed": False,
        "automatic_model_download": False,
    }
    for field, expected_value in expected.items():
        if provider.get(field) != expected_value:
            raise ContractError(
                f"Microsoft CLAP retrieval provider drifted from its plan: {field}"
            )
    if payload.get("intent_curve_sha256") != plan.get("intent_curve_sha256"):
        raise ContractError(
            "Microsoft CLAP retrieval result targets another plan intent"
        )


def _normalize_retrieval_matches(
    matches: Iterable[dict[str, Any]], segment_ids: set[str]
) -> list[dict[str, Any]]:
    rows = list(matches)
    if not rows:
        raise ContractError("music retrieval results require matches")
    normalized: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for raw in rows:
        if not isinstance(raw, dict):
            raise ContractError("music retrieval match must be an object")
        segment_id = _required_text(raw.get("segment_id"), "music segment id")
        asset_id = _required_text(raw.get("asset_id"), "music asset id")
        window_id = _required_text(raw.get("window_id"), "music window id")
        key = (segment_id, asset_id, window_id)
        if segment_id not in segment_ids or key in seen:
            raise ContractError("music retrieval match identity is invalid")
        seen.add(key)
        normalized.append(
            {
                "segment_id": segment_id,
                "asset_id": asset_id,
                "window_id": window_id,
                "semantic_similarity": _ratio(
                    raw.get("semantic_similarity"), "music semantic similarity"
                ),
                "continuity_diversity": _ratio(
                    raw.get("continuity_diversity", 0.5),
                    "music continuity and diversity",
                ),
                "query_text": _required_text(
                    raw.get("query_text"), "music retrieval query"
                ),
            }
        )
    return normalized


def _load_retrieval_benchmark(
    benchmark_path: str | Path | None,
    retrieval: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    provider_id = str(retrieval.get("provider", {}).get("id") or "")
    if benchmark_path is None:
        if provider_id == PINNED_MSCLAP_PROVIDER_ID:
            raise ContractError(
                "pinned Microsoft CLAP retrieval requires a passing gold benchmark"
            )
        return None, None

    from .music_benchmark import (
        music_retrieval_provider_identity,
        validate_music_retrieval_benchmark,
    )

    path = Path(benchmark_path).expanduser().resolve()
    benchmark = read_json_object(path, "music retrieval benchmark")
    validate_music_retrieval_benchmark(benchmark)
    if benchmark.get("provider_identity") != music_retrieval_provider_identity(
        _object(retrieval.get("provider"))
    ):
        raise ContractError(
            "music retrieval benchmark belongs to another provider or model"
        )
    if benchmark.get("readiness", {}).get("passed") is not True:
        raise ContractError("music retrieval benchmark did not pass")
    return (
        artifact_reference(path, "music retrieval benchmark"),
        str(benchmark["benchmark_sha256"]),
    )


def _load_profiles(
    profile_paths: Iterable[str | Path],
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    profiles: dict[str, dict[str, Any]] = {}
    references: list[dict[str, Any]] = []
    for source in profile_paths:
        path = Path(source).expanduser().resolve()
        profile = read_json_object(path, "music asset profile")
        validate_music_asset_profile(profile)
        asset_id = profile["asset_id"]
        if asset_id in profiles:
            raise ContractError("music asset profile ids must be unique")
        profiles[asset_id] = profile
        references.append(
            {
                "asset_id": asset_id,
                "artifact": artifact_reference(path, "music asset profile"),
                "profile_sha256": profile["profile_sha256"],
            }
        )
    if not profiles:
        raise ContractError("music candidate generation requires profiles")
    return profiles, references


def _score_candidates(
    intent: dict[str, Any],
    retrieval: dict[str, Any],
    profiles: dict[str, dict[str, Any]],
    *,
    purpose: str,
    top_k: int,
    weights: dict[str, float],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    expected_intent = _object(retrieval.get("intent_curve"))
    actual_intent = _object(intent.get("source"))
    if not expected_intent or not actual_intent:
        raise ContractError("music intent or retrieval provenance is incomplete")
    if retrieval.get("intent_curve_sha256") != intent.get("intent_curve_sha256"):
        raise ContractError("music retrieval results target another intent curve")
    intent_by_id = {row["segment_id"]: row for row in intent["segments"]}
    windows: dict[tuple[str, str], tuple[dict[str, Any], dict[str, Any]]] = {}
    for asset_id, profile in profiles.items():
        for window in profile["windows"]:
            windows[(asset_id, window["window_id"])] = (profile, window)
    candidates: dict[str, list[dict[str, Any]]] = {
        segment_id: [] for segment_id in intent_by_id
    }
    rejected: list[dict[str, Any]] = []
    for match in retrieval["matches"]:
        segment = intent_by_id[match["segment_id"]]
        pair = windows.get((match["asset_id"], match["window_id"]))
        if pair is None:
            raise ContractError("music retrieval match has no reviewed asset window")
        profile, window = pair
        reasons = _hard_filter_reasons(segment, profile, window, purpose)
        if reasons:
            rejected.append(
                {
                    "segment_id": segment["segment_id"],
                    "asset_id": profile["asset_id"],
                    "window_id": window["window_id"],
                    "reasons": reasons,
                }
            )
            continue
        components = _score_components(segment, window, match)
        total = round(
            sum(components[name] * weights[name] for name in DEFAULT_WEIGHTS), 6
        )
        candidates[segment["segment_id"]].append(
            {
                "asset_id": profile["asset_id"],
                "window_id": window["window_id"],
                "source_audio": dict(profile["source_audio"]),
                "source_interval": {
                    "start_s": window["start_s"],
                    "end_s": window["end_s"],
                    "loopable": window["loopable"],
                },
                "structure_label": window["structure_label"],
                "score": total,
                "score_components": components,
                "license": dict(profile["license"]),
                "retrieval_provider": dict(retrieval["provider"]),
                "query_text": match["query_text"],
            }
        )
    segments: list[dict[str, Any]] = []
    for segment in intent["segments"]:
        ranked = sorted(
            candidates[segment["segment_id"]],
            key=lambda row: (-row["score"], row["asset_id"], row["window_id"]),
        )[:top_k]
        for rank, row in enumerate(ranked, 1):
            row["rank"] = rank
        segments.append(
            {
                "segment_id": segment["segment_id"],
                "timeline_interval": {
                    "start_s": segment["start_s"],
                    "end_s": segment["end_s"],
                },
                "narration_density": segment["narration_density"],
                "candidates": ranked,
            }
        )
    return segments, sorted(
        rejected,
        key=lambda row: (row["segment_id"], row["asset_id"], row["window_id"]),
    )


def _hard_filter_reasons(
    segment: dict[str, Any],
    profile: dict[str, Any],
    window: dict[str, Any],
    purpose: str,
) -> list[str]:
    reasons: list[str] = []
    license_info = profile["license"]
    if license_info["status"] != "allowed":
        reasons.append("license_not_allowed")
    if purpose == "commercial" and not license_info["commercial_use_allowed"]:
        reasons.append("commercial_use_not_allowed")
    if (
        segment["vocal_policy"] == "instrumental_required"
        and window["vocal_probability"] > 0.15
    ):
        reasons.append("vocal_content_forbidden")
    prohibited = set(segment["prohibited_tags"])
    if prohibited.intersection(window["tags"]):
        reasons.append("prohibited_tag_present")
    required_duration = segment["end_s"] - segment["start_s"]
    available_duration = window["end_s"] - window["start_s"]
    if available_duration + 0.001 < required_duration and not window["loopable"]:
        reasons.append("window_too_short_and_not_loopable")
    return reasons


def _score_components(
    segment: dict[str, Any], window: dict[str, Any], match: dict[str, Any]
) -> dict[str, float]:
    target = segment["target"]
    actual = window["descriptors"]
    if segment["relationship_mode"] == "counterpoint":
        affect = (
            sum(abs(target[key] - actual[key]) for key in ("valence", "arousal")) / 2
        )
    elif segment["relationship_mode"] == "neutral_support":
        affect = (
            1
            - (
                abs(target["valence"] - actual["valence"])
                + abs(target["warmth"] - actual["warmth"])
            )
            / 2
        )
    else:
        affect = (
            1
            - sum(abs(target[key] - actual[key]) for key in ("valence", "arousal")) / 2
        )
    energy = (
        1
        - (
            abs(target["arousal"] - actual["arousal"])
            + abs(target["movement"] - actual["movement"])
        )
        / 2
    )
    narration = segment["narration_density"]
    interference = narration * (
        0.7 * window["vocal_probability"] + 0.3 * actual["engagement"]
    )
    return {
        "semantic_similarity": round(match["semantic_similarity"], 6),
        "affect_match": round(_clamp01(affect), 6),
        "energy_arc_match": round(_clamp01(energy), 6),
        "structure_fit": round(window["structure_confidence"], 6),
        "speech_compatibility": round(_clamp01(1 - interference), 6),
        "continuity_diversity": round(match["continuity_diversity"], 6),
    }


def _select_music_v1(
    candidate_set: dict[str, Any], *, min_score: float, min_margin: float
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    selections: list[dict[str, Any]] = []
    unresolved: list[str] = []
    for segment in candidate_set["segments"]:
        rows = segment["candidates"]
        top = rows[0] if rows else None
        second_score = rows[1]["score"] if len(rows) > 1 else 0.0
        margin = round(top["score"] - second_score, 6) if top else 0.0
        selected = bool(top and top["score"] >= min_score and margin >= min_margin)
        if not selected:
            unresolved.append(segment["segment_id"])
            selections.append(
                {
                    "segment_id": segment["segment_id"],
                    "status": "needs_creative_selection",
                    "reason": "no_candidate_meets_score_and_margin",
                    "top_score": top["score"] if top else None,
                    "score_margin": margin,
                }
            )
            continue
        duration = round(
            segment["timeline_interval"]["end_s"]
            - segment["timeline_interval"]["start_s"],
            6,
        )
        source_duration = round(
            top["source_interval"]["end_s"] - top["source_interval"]["start_s"],
            6,
        )
        narration = segment["narration_density"]
        selections.append(
            {
                "segment_id": segment["segment_id"],
                "status": "auto_selected",
                "asset_id": top["asset_id"],
                "window_id": top["window_id"],
                "source_audio": dict(top["source_audio"]),
                "source_interval": dict(top["source_interval"]),
                "timeline_interval": dict(segment["timeline_interval"]),
                "loop": source_duration + 0.001 < duration,
                "gain_db": -18.0 if narration >= 0.5 else -12.0,
                "fade_in_s": round(min(0.6, duration / 4), 3),
                "fade_out_s": round(min(0.8, duration / 4), 3),
                "ducking": {
                    "enabled": narration >= 0.25,
                    "threshold": 0.02,
                    "ratio": 8.0,
                    "attack_ms": 20,
                    "release_ms": 300,
                },
                "score": top["score"],
                "score_margin": margin,
                "score_components": dict(top["score_components"]),
                "selection_reason": "highest licensed candidate passed automatic score and margin gates",
            }
        )
    render_eligible = not unresolved
    readiness = {
        "status": "ready_for_single_ffmpeg_outlet"
        if render_eligible
        else "needs_creative_selection",
        "render_eligible": render_eligible,
        "unresolved_segment_ids": unresolved,
        "automatic_selection_count": sum(
            row["status"] == "auto_selected" for row in selections
        ),
        "manual_review_required": bool(unresolved),
    }
    return selections, readiness


def _select_music_v2(
    candidate_set: dict[str, Any],
    intent: dict[str, Any],
    *,
    min_score: float,
    min_margin: float,
    word_timestamps: list[dict[str, Any]],
    subtitle_cues: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    policy = spoken_bgm_policy()
    ordinary_gain = float(policy["mix_profiles"]["ordinary"]["music_gain_db"])
    ducking_policy = _object(policy.get("ducking"))
    family_lookup = _candidate_family_lookup(candidate_set)
    intent_by_id = {row["segment_id"]: row for row in intent["segments"]}
    selections: list[dict[str, Any]] = []
    unresolved: list[str] = []
    previous_family: str | None = None

    for segment in candidate_set["segments"]:
        intent_segment = intent_by_id.get(segment["segment_id"])
        if intent_segment is None:
            raise ContractError("music candidate segment has no intent segment")
        family = classify_music_family(intent_segment)
        family_id = str(family["family_id"])
        if intent_segment.get("music_mode") == "none":
            selections.append({"segment_id":segment["segment_id"],"status":"intentional_none",
                "music_mode":"none","selection_reason":intent_segment["music_mode_reason"],
                "music_family":family,"timeline_interval":dict(segment["timeline_interval"]),
                "semantic_timeline_moved":False})
            previous_family=None
            continue
        rows = [
            row
            for row in segment["candidates"]
            if family_lookup.get((row["asset_id"], row["window_id"])) == family_id
        ]
        top = rows[0] if rows else None
        second_score = rows[1]["score"] if len(rows) > 1 else 0.0
        margin = round(top["score"] - second_score, 6) if top else 0.0
        selected = bool(top and top["score"] >= min_score and margin >= min_margin)
        if not selected:
            unresolved.append(segment["segment_id"])
            selections.append(
                {
                    "segment_id": segment["segment_id"],
                    "status": "needs_creative_selection",
                    "reason": (
                        "no_candidate_matches_music_family"
                        if not rows
                        else "no_candidate_meets_score_and_margin"
                    ),
                    "music_family": family,
                    "top_score": top["score"] if top else None,
                    "score_margin": margin,
                    "semantic_timeline_moved": False,
                }
            )
            previous_family = family_id
            continue

        duration = round(
            segment["timeline_interval"]["end_s"]
            - segment["timeline_interval"]["start_s"],
            6,
        )
        source_duration = round(
            top["source_interval"]["end_s"] - top["source_interval"]["start_s"],
            6,
        )
        priority = build_speech_priority_windows(
            intent_segment,
            word_timestamps=word_timestamps,
            subtitle_cues=subtitle_cues,
        )
        narration_density = float(intent_segment["narration_density"])
        ducking_enabled = bool(ducking_policy["enabled"]) and narration_density > 0.0
        crossfade_s = min(float(policy["transition"]["crossfade_s"]), duration / 4)
        release_ms, release_reason = _ducking_release_for_segment(
            intent_segment, word_timestamps, ducking_policy
        )
        if not ducking_enabled:
            release_reason = "semantic segment declares no narration"
        selections.append(
            {
                "segment_id": segment["segment_id"],
                "status": "auto_selected",
                "asset_id": top["asset_id"],
                "window_id": top["window_id"],
                "music_family": family,
                "source_audio": dict(top["source_audio"]),
                "source_interval": dict(top["source_interval"]),
                "timeline_interval": dict(segment["timeline_interval"]),
                "loop": source_duration + 0.001 < duration,
                "gain_db": ordinary_gain,
                "gain_envelope": {
                    "base_music_gain_db": ordinary_gain,
                    "speech_priority_music_gain_db": float(
                        policy["mix_profiles"]["speech_priority"]["music_gain_db"]
                    ),
                    "ramp_s": float(policy["speech_priority_window"]["ramp_s"]),
                    "source_mode": priority["source_mode"],
                    "application_mode": priority["application_mode"],
                    "coverage_ratio": priority["coverage_ratio"],
                    "windows": priority["windows"],
                    "semantic_timeline_moved": False,
                },
                **({"no_music_windows": intent_segment["no_music_windows"]} if "no_music_windows" in intent_segment else {}),
                "fade_in_s": round(min(crossfade_s, duration / 4), 3),
                "fade_out_s": round(min(crossfade_s, duration / 4), 3),
                "ducking": {
                    "enabled": ducking_enabled,
                    "threshold": float(ducking_policy["threshold"]),
                    "ratio": float(ducking_policy["ratio"]),
                    "attack_ms": int(ducking_policy["attack_ms"]),
                    "release_ms": release_ms,
                    "release_reason": release_reason,
                },
                "transition": {
                    "previous_family": previous_family,
                    "family_change": previous_family not in {None, family_id},
                    "music_change_allowed_at_boundary": True,
                    "crossfade_s": round(crossfade_s, 3),
                    "phrase_alignment_priority": "background_quality_only",
                    "semantic_timeline_moved": False,
                },
                "score": top["score"],
                "score_margin": margin,
                "score_components": dict(top["score_components"]),
                "selection_reason": (
                    "highest licensed candidate matched the semantic music family "
                    "and passed automatic score and margin gates"
                ),
                "automatic_music_boost_allowed": False,
                "semantic_timeline_moved": False,
            }
        )
        previous_family = family_id

    render_eligible = not unresolved
    readiness = {
        "status": "ready_for_single_ffmpeg_outlet"
        if render_eligible
        else "needs_creative_selection",
        "render_eligible": render_eligible,
        "unresolved_segment_ids": unresolved,
        "automatic_selection_count": sum(
            row["status"] == "auto_selected" for row in selections
        ),
        "manual_review_required": bool(unresolved),
        "candidate_only": True,
    }
    return selections, readiness


def _no_music_filter(windows, timeline_start, duration):
    if not isinstance(windows,list):
        raise ContractError("no_music_windows must be a list")
    expressions=[]
    previous=timeline_start
    for row in windows:
        if not isinstance(row,dict): raise ContractError("invalid no-music window")
        a=_nonnegative_number(row.get("start_s"),"no-music start")
        b=_positive_number(row.get("end_s"),"no-music end")
        _required_text(row.get("reason"),"no-music reason")
        if not timeline_start<=a<b<=timeline_start+duration or a<previous:
            raise ContractError("no-music windows must be ordered and inside the segment")
        previous=b
        a-=timeline_start; b-=timeline_start
        # Silence throughout the protected interval; ramps stay outside it.
        expressions.append(f"if(lt(t,{a:.6f}),clip(({a:.6f}-t)/0.1,0,1),if(lte(t,{b:.6f}),0,clip((t-{b:.6f})/0.1,0,1)))")
    return "volume='"+"*".join(expressions)+"':eval=frame" if expressions else ""


def _candidate_family_lookup(
    candidate_set: dict[str, Any],
) -> dict[tuple[str, str], str]:
    lookup: dict[tuple[str, str], str] = {}
    for reference in candidate_set["profiles"]:
        path = validate_artifact_reference(
            _object(reference.get("artifact")), "music asset profile"
        )
        profile = read_json_object(path, "music asset profile")
        validate_music_asset_profile(profile)
        for window in profile["windows"]:
            lookup[(profile["asset_id"], window["window_id"])] = (
                _music_family_from_tags(window["tags"])
            )
    return lookup


def _music_family_from_tags(tags: Iterable[str]) -> str:
    normalized = {str(tag).strip().lower() for tag in tags}
    for family_id in ("restrained_risk", "warm_story", "clear_explainer"):
        if family_id in normalized:
            return family_id
    if normalized.intersection(
        {"risk", "serious", "restrained", "legal", "policy", "caution"}
    ):
        return "restrained_risk"
    if normalized.intersection(
        {"warm", "story", "human", "intimate", "documentary", "humane"}
    ):
        return "warm_story"
    return "clear_explainer"


def _ducking_release_for_segment(
    intent_segment: dict[str, Any],
    word_timestamps: list[dict[str, Any]],
    ducking_policy: dict[str, Any],
) -> tuple[int, str]:
    start_s = float(intent_segment["start_s"])
    end_s = float(intent_segment["end_s"])
    rows: list[dict[str, float]] = []
    for raw in word_timestamps:
        if not isinstance(raw, dict):
            continue
        row_start = _finite_number(
            raw.get("start_s", raw.get("start")), "word timestamp start"
        )
        row_end = _finite_number(raw.get("end_s", raw.get("end")), "word timestamp end")
        if row_start < 0 or row_end <= row_start:
            raise ContractError("word timestamp interval is invalid")
        if row_end > start_s and row_start < end_s:
            rows.append({"start_s": row_start, "end_s": row_end})
    rows.sort(key=lambda row: (row["start_s"], row["end_s"]))
    gaps = [
        max(0.0, rows[index]["start_s"] - rows[index - 1]["end_s"])
        for index in range(1, len(rows))
    ]
    short_pause_count = sum(0.18 <= gap <= 1.2 for gap in gaps)
    duration_s = max(0.001, end_s - start_s)
    rate = short_pause_count / (duration_s / 60.0)
    threshold = float(ducking_policy["dense_short_pause_rate_per_minute"])
    if rate >= threshold:
        return int(ducking_policy["dense_short_pause_release_ms"]), (
            "word timestamps show dense short pauses"
        )
    return int(ducking_policy["release_ms"]), "ordinary spoken-word release"


def _load_word_timestamps(
    source_path: str | Path | None,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    if source_path is None:
        return [], None
    path = Path(source_path).expanduser().resolve()
    if not path.is_file():
        raise ContractError(f"word timestamp artifact not found: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ContractError(f"word timestamp artifact is unreadable: {path}") from exc
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        rows = payload.get("words") or payload.get("tokens") or []
        if not rows and isinstance(payload.get("segments"), list):
            rows = [
                word
                for segment in payload["segments"]
                if isinstance(segment, dict)
                for word in segment.get("words", [])
                if isinstance(word, dict)
            ]
    else:
        raise ContractError("word timestamp artifact must be an object or array")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ContractError("word timestamp rows must be objects")
    return list(rows), artifact_reference(path, "word timestamps")


def _load_srt_subtitles(
    source_path: str | Path | None,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    if source_path is None:
        return [], None
    path = Path(source_path).expanduser().resolve()
    if not path.is_file():
        raise ContractError(f"subtitle artifact not found: {path}")
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise ContractError(f"subtitle artifact is unreadable: {path}") from exc
    rows: list[dict[str, Any]] = []
    for block in re.split(r"\r?\n\s*\r?\n", text.strip()):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        timing_index = next(
            (
                index
                for index, line in enumerate(lines)
                if _SRT_TIMING_PATTERN.search(line)
            ),
            None,
        )
        if timing_index is None:
            continue
        match = _SRT_TIMING_PATTERN.search(lines[timing_index])
        if match is None:
            continue
        cue_text = " ".join(lines[timing_index + 1 :]).strip()
        if cue_text:
            rows.append(
                {
                    "text": cue_text,
                    "start_s": _srt_seconds(match.group("start")),
                    "end_s": _srt_seconds(match.group("end")),
                }
            )
    if not rows:
        raise ContractError("subtitle artifact contains no valid SRT cues")
    return rows, artifact_reference(path, "SRT subtitles")


def _srt_seconds(value: str) -> float:
    hours, minutes, seconds = value.replace(",", ".").split(":")
    return round(int(hours) * 3600 + int(minutes) * 60 + float(seconds), 6)


def _validate_word_timestamp_reference(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    path = validate_artifact_reference(_object(value), "word timestamps")
    rows, expected = _load_word_timestamps(path)
    if value != expected:
        raise ContractError("word timestamp artifact reference drifted")
    return rows


def _validate_subtitle_reference(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    path = validate_artifact_reference(_object(value), "SRT subtitles")
    rows, expected = _load_srt_subtitles(path)
    if value != expected:
        raise ContractError("subtitle artifact reference drifted")
    return rows


def _gain_envelope_filter(
    value: Any,
    *,
    timeline_start_s: float,
    duration_s: float,
    base_gain_db: float,
) -> str | None:
    if value is None:
        return None
    envelope = _object(value)
    if envelope.get("semantic_timeline_moved") is not False:
        raise ContractError("music gain envelope cannot move the semantic timeline")
    if not math.isclose(
        float(envelope.get("base_music_gain_db")), base_gain_db, abs_tol=1e-6
    ):
        raise ContractError("music gain envelope base gain drifted")
    ramp_s = _nonnegative_number(envelope.get("ramp_s"), "gain envelope ramp")
    windows = envelope.get("windows")
    if not isinstance(windows, list):
        raise ContractError("music gain envelope windows must be a list")
    expressions: list[str] = []
    previous_end = timeline_start_s
    timeline_end = timeline_start_s + duration_s
    for row in windows:
        item = _object(row)
        start_s = _nonnegative_number(item.get("start_s"), "gain window start")
        end_s = _positive_number(item.get("end_s"), "gain window end")
        gain_db = _finite_number(item.get("music_gain_db"), "gain window level")
        if (
            end_s <= start_s
            or start_s < timeline_start_s - 0.001
            or end_s > timeline_end + 0.001
            or start_s < previous_end - 0.001
        ):
            raise ContractError("music gain envelope windows are invalid")
        if gain_db > base_gain_db + 1e-6:
            raise ContractError("music gain envelope cannot automatically boost music")
        previous_end = end_s
        if math.isclose(gain_db, base_gain_db, abs_tol=1e-6):
            continue
        expressions.append(
            _gain_window_expression(
                start_s=start_s - timeline_start_s,
                end_s=end_s - timeline_start_s,
                duration_s=duration_s,
                ramp_s=ramp_s,
                multiplier=10 ** ((gain_db - base_gain_db) / 20.0),
            )
        )
    if not expressions:
        return None
    return f"volume={'*'.join(expressions)}:eval=frame"


def _gain_window_expression(
    *,
    start_s: float,
    end_s: float,
    duration_s: float,
    ramp_s: float,
    multiplier: float,
) -> str:
    ramp_in_start = max(0.0, start_s - ramp_s)
    ramp_out_end = min(duration_s, end_s + ramp_s)
    low = f"{multiplier:.9f}"
    if start_s - ramp_in_start > 1e-6:
        attack = f"1+({low}-1)*(t-{ramp_in_start:.6f})/{start_s - ramp_in_start:.6f}"
        before = (
            f"if(lt(t\\,{ramp_in_start:.6f})\\,1\\,"
            f"if(lt(t\\,{start_s:.6f})\\,{attack}\\,{low}))"
        )
    else:
        before = low
    if ramp_out_end - end_s > 1e-6:
        release = f"{low}+(1-{low})*(t-{end_s:.6f})/{ramp_out_end - end_s:.6f}"
        after = f"if(lt(t\\,{ramp_out_end:.6f})\\,{release}\\,1)"
    else:
        after = "1"
    return f"if(lte(t\\,{end_s:.6f})\\,{before}\\,{after})"


def _normalize_descriptors(value: Any) -> dict[str, float]:
    raw = _object(value)
    return {
        key: _ratio(raw.get(key), f"music descriptor {key}")
        for key in AFFECT_DIMENSIONS
    }


def _normalize_weights(value: dict[str, Any]) -> dict[str, float]:
    if set(value) != set(DEFAULT_WEIGHTS):
        raise ContractError("music score weights must define the reviewed components")
    normalized = {
        key: _ratio(value[key], f"music weight {key}") for key in DEFAULT_WEIGHTS
    }
    if not math.isclose(sum(normalized.values()), 1.0, abs_tol=1e-6):
        raise ContractError("music score weights must sum to one")
    return normalized


def _validate_music_edit_plan_v2_schema(payload: dict[str, Any]) -> None:
    schema_path = _SCHEMA_ROOT / "music_edit_plan.v2.schema.json"
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ContractError(
            f"music edit plan schema is unreadable: {schema_path}"
        ) from exc
    Draft202012Validator.check_schema(schema)
    errors = sorted(
        Draft202012Validator(schema).iter_errors(payload),
        key=lambda error: list(error.path),
    )
    if errors:
        messages = [
            f"{'.'.join(str(part) for part in error.path) or '$'}: {error.message}"
            for error in errors
        ]
        raise ContractError("music edit plan v2 schema failed: " + "; ".join(messages))


def _required_text(value: Any, label: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ContractError(f"{label} is required")
    return text


def _text_list(value: Any, label: str, *, allow_empty: bool = False) -> list[str]:
    if not isinstance(value, list):
        raise ContractError(f"{label} must be a list")
    result: list[str] = []
    seen: set[str] = set()
    for raw in value:
        text = str(raw or "").strip()
        if not text or text in seen:
            raise ContractError(f"{label} must contain unique non-empty values")
        seen.add(text)
        result.append(text)
    if not result and not allow_empty:
        raise ContractError(f"{label} cannot be empty")
    return result


def _choice(value: Any, allowed: set[str], label: str) -> str:
    text = str(value or "").strip()
    if text not in allowed:
        raise ContractError(f"{label} must be one of {sorted(allowed)}")
    return text


def _ratio(value: Any, label: str) -> float:
    number = _finite_number(value, label)
    if number < 0 or number > 1:
        raise ContractError(f"{label} must be between zero and one")
    return round(number, 6)


def _nonnegative_number(value: Any, label: str) -> float:
    number = _finite_number(value, label)
    if number < 0:
        raise ContractError(f"{label} cannot be negative")
    return round(number, 6)


def _positive_number(value: Any, label: str) -> float:
    number = _finite_number(value, label)
    if number <= 0:
        raise ContractError(f"{label} must be positive")
    return round(number, 6)


def _finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise ContractError(f"{label} must be numeric")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{label} must be numeric") from exc
    if not math.isfinite(number):
        raise ContractError(f"{label} must be finite")
    return number


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise ContractError(f"{label} must be a positive integer")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{label} must be a positive integer") from exc
    if number <= 0 or float(number) != float(value):
        raise ContractError(f"{label} must be a positive integer")
    return number


def _boolean(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise ContractError(f"{label} must be a boolean")
    return value


def _optional_sha256(value: Any, label: str) -> str:
    result = str(value or "").strip().lower()
    if not result:
        return ""
    if len(result) != 64 or any(
        character not in "0123456789abcdef" for character in result
    ):
        raise ContractError(f"{label} must be a SHA-256 digest")
    return result


def _object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _clamp01(value: float) -> float:
    return min(1.0, max(0.0, value))
