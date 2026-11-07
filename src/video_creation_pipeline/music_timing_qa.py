from __future__ import annotations

import json
import math
import re
from collections.abc import Iterable
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from .contract_io import (
    artifact_reference,
    payload_sha256,
    validate_artifact_reference,
)
from .errors import ContractError

MUSIC_STRUCTURE_ANALYSIS_SCHEMA = "video_creation_pipeline.music_structure_analysis.v1"
MUSIC_TIMING_QA_SCHEMA = "video_creation_pipeline.music_timing_qa.v1"

_SCHEMA_ROOT = Path(__file__).with_name("schemas")
_POLICY_PATH = Path(__file__).with_name("policies") / "spoken-bgm-policy.v1.json"

ALLIN1_SOURCE_COMMIT = "18e78903c0365147a2c5d4e5e57ebf88cb7d800e"
PYMUSICLOOPER_SOURCE_COMMIT = "78313f8f3a2031f6eaa1c4f9b748088a5123398b"

_NUMBER_PATTERN = re.compile(
    r"(?:\d+(?:[.,]\d+)?\s*(?:%|％|元|万元|万|年|岁|次)?)"
    r"|(?:[零一二三四五六七八九十百千万亿两]+(?:元|万元|万|年|岁|次|成))"
)


class _SubtitleTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _semantic_timing_text(value: object) -> str:
    """Return visible subtitle text without presentation-only metadata."""

    source = str(value or "")
    parser = _SubtitleTextExtractor()
    try:
        parser.feed(source)
        parser.close()
    except ValueError:
        visible = source
    else:
        visible = "".join(parser.parts)
    visible = re.sub(r"\{\\[^{}]*\}", "", visible)
    return unescape(visible).strip()


def spoken_bgm_policy() -> dict[str, Any]:
    try:
        policy = json.loads(_POLICY_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ContractError(f"spoken BGM policy is unreadable: {_POLICY_PATH}") from exc
    if policy.get("schema") != "video_creation_pipeline.spoken_bgm_policy.v1":
        raise ContractError("unsupported spoken BGM policy")
    families = _object(policy.get("families"))
    if set(families) != {"warm_story", "clear_explainer", "restrained_risk"}:
        raise ContractError("spoken BGM policy must define exactly three families")
    if sum(bool(_object(row).get("default")) for row in families.values()) != 1:
        raise ContractError("spoken BGM policy requires exactly one default family")
    profiles = _object(policy.get("mix_profiles"))
    if set(profiles) != {"ordinary", "speech_priority"}:
        raise ContractError("spoken BGM policy mix profiles are incomplete")
    ordinary = _object(profiles["ordinary"])
    priority = _object(profiles["speech_priority"])
    if float(ordinary.get("music_gain_db")) != -3.0:
        raise ContractError("ordinary spoken BGM gain must remain -3 dB")
    if float(priority.get("music_gain_db")) != -6.0:
        raise ContractError("speech-priority BGM gain must remain -6 dB")
    return policy


def build_music_structure_analysis(
    source_path: str | Path,
    *,
    provider: dict[str, Any],
    bpm: float,
    beats: Iterable[float],
    downbeats: Iterable[float],
    beat_positions: Iterable[int],
    segments: Iterable[dict[str, Any]],
    beats_per_bar: int = 4,
    phrase_bars: int = 4,
    confidence: float | None = None,
) -> dict[str, Any]:
    source = Path(source_path).expanduser().resolve()
    duration_s = _media_duration(provider, source)
    normalized_beats = _times(beats, "beats", duration_s)
    normalized_downbeats = _times(downbeats, "downbeats", duration_s)
    normalized_positions = [int(value) for value in beat_positions]
    if len(normalized_positions) != len(normalized_beats):
        raise ContractError("beat_positions must match beats")
    if beats_per_bar not in {3, 4}:
        raise ContractError("beats_per_bar must be 3 or 4")
    if phrase_bars < 1:
        raise ContractError("phrase_bars must be positive")
    if any(value < 1 or value > beats_per_bar for value in normalized_positions):
        raise ContractError("beat_positions are outside the time signature")
    if not math.isfinite(float(bpm)) or float(bpm) <= 0:
        raise ContractError("BPM must be positive and finite")

    normalized_segments = _segments(segments, duration_s)
    phrase_candidates = _phrase_candidates(
        normalized_downbeats,
        normalized_segments,
        duration_s,
        phrase_bars,
    )
    payload: dict[str, Any] = {
        "schema": MUSIC_STRUCTURE_ANALYSIS_SCHEMA,
        "status": "completed",
        "source": artifact_reference(source, "music structure source"),
        "duration_s": duration_s,
        "provider": _provider(provider),
        "tempo": {
            "bpm": round(float(bpm), 6),
            "beats_per_bar": beats_per_bar,
            "phrase_bars": phrase_bars,
            "confidence": None
            if confidence is None
            else _unit(confidence, "confidence"),
        },
        "beats": normalized_beats,
        "downbeats": normalized_downbeats,
        "beat_positions": normalized_positions,
        "segments": normalized_segments,
        "phrase_boundary_candidates": phrase_candidates,
        "consumption_policy": {
            "candidate_only": True,
            "may_snap_source_windows": True,
            "may_move_semantic_boundary": False,
            "automatic_provider_fallback": False,
            "source_is_read_only": True,
        },
    }
    payload["analysis_sha256"] = payload_sha256(payload, "analysis_sha256")
    validate_music_structure_analysis(payload)
    return payload


def adapt_allin1_result(
    source_path: str | Path,
    result: dict[str, Any],
    *,
    source_commit: str = ALLIN1_SOURCE_COMMIT,
) -> dict[str, Any]:
    required = {"bpm", "beats", "downbeats", "beat_positions", "segments"}
    missing = sorted(required - set(result))
    if missing:
        raise ContractError(f"All-In-One result is missing: {', '.join(missing)}")
    if source_commit != ALLIN1_SOURCE_COMMIT:
        raise ContractError("All-In-One source commit is not the reviewed revision")
    return build_music_structure_analysis(
        source_path,
        provider={
            "id": "allin1",
            "version": "1.1.0",
            "source_commit": source_commit,
            "method": "harmonix beat/downbeat/functional structure analysis",
            "media_duration_s": _source_result_duration(result),
            "runtime_status": "external_provider_completed",
        },
        bpm=float(result["bpm"]),
        beats=result["beats"],
        downbeats=result["downbeats"],
        beat_positions=result["beat_positions"],
        segments=result["segments"],
    )


def adapt_librosa_bar_grid_result(
    source_path: str | Path,
    result: dict[str, Any],
    *,
    adapted_source_commit: str = PYMUSICLOOPER_SOURCE_COMMIT,
) -> dict[str, Any]:
    required = {
        "duration_s",
        "bpm",
        "beats",
        "downbeats",
        "beat_positions",
        "segments",
        "librosa_version",
    }
    missing = sorted(required - set(result))
    if missing:
        raise ContractError(f"librosa bar-grid result is missing: {', '.join(missing)}")
    if adapted_source_commit != PYMUSICLOOPER_SOURCE_COMMIT:
        raise ContractError("PyMusicLooper source commit is not the reviewed revision")
    return build_music_structure_analysis(
        source_path,
        provider={
            "id": "librosa_bar_grid",
            "version": f"librosa-{result['librosa_version']}",
            "source_commit": adapted_source_commit,
            "adapted_source_commit": adapted_source_commit,
            "method": (
                "librosa onset/chroma bar grid with PyMusicLooper fixed-source "
                "candidate scoring and zero-crossing adaptation"
            ),
            "media_duration_s": result["duration_s"],
            "runtime_status": "external_provider_completed",
            "tempo_evidence": dict(result.get("tempo_evidence") or {}),
        },
        bpm=float(result["bpm"]),
        beats=result["beats"],
        downbeats=result["downbeats"],
        beat_positions=result["beat_positions"],
        segments=result["segments"],
        beats_per_bar=int(result.get("beats_per_bar", 4)),
        phrase_bars=int(result.get("phrase_bars", 4)),
        confidence=result.get("confidence"),
    )


def validate_music_structure_analysis(payload: dict[str, Any]) -> None:
    _validate_schema(payload, "music_structure_analysis.v1.schema.json")
    if payload.get("schema") != MUSIC_STRUCTURE_ANALYSIS_SCHEMA:
        raise ContractError("unsupported music structure analysis schema")
    if payload.get("status") != "completed":
        raise ContractError("music structure analysis must be completed")
    if payload.get("analysis_sha256") != payload_sha256(payload, "analysis_sha256"):
        raise ContractError("music structure analysis integrity check failed")
    source_path = validate_artifact_reference(
        _object(payload.get("source")), "music structure source"
    )
    duration_s = _positive(payload.get("duration_s"), "duration_s")
    provider = _provider(_object(payload.get("provider")))
    if provider["id"] == "allin1" and provider["source_commit"] != ALLIN1_SOURCE_COMMIT:
        raise ContractError("All-In-One provenance drift")
    if provider["id"] == "librosa_bar_grid" and (
        provider.get("adapted_source_commit") != PYMUSICLOOPER_SOURCE_COMMIT
    ):
        raise ContractError("PyMusicLooper adaptation provenance drift")
    tempo = _object(payload.get("tempo"))
    bpm = _positive(tempo.get("bpm"), "BPM")
    if not 30 <= bpm <= 300:
        raise ContractError("BPM is outside the supported review range")
    beats_per_bar = int(tempo.get("beats_per_bar") or 0)
    if beats_per_bar not in {3, 4}:
        raise ContractError("beats_per_bar must be 3 or 4")
    phrase_bars = int(tempo.get("phrase_bars") or 0)
    if phrase_bars < 1:
        raise ContractError("phrase_bars must be positive")
    beats = _times(payload.get("beats"), "beats", duration_s)
    downbeats = _times(payload.get("downbeats"), "downbeats", duration_s)
    positions = payload.get("beat_positions")
    if not isinstance(positions, list) or len(positions) != len(beats):
        raise ContractError("beat_positions must match beats")
    if any(int(value) < 1 or int(value) > beats_per_bar for value in positions):
        raise ContractError("beat_positions are outside the time signature")
    if any(not _contains_close(beats, value, 0.05) for value in downbeats):
        raise ContractError("downbeats must also be beats")
    _segments(payload.get("segments"), duration_s)
    _times(
        payload.get("phrase_boundary_candidates"),
        "phrase_boundary_candidates",
        duration_s,
    )
    policy = _object(payload.get("consumption_policy"))
    if (
        policy.get("candidate_only") is not True
        or policy.get("may_snap_source_windows") is not True
        or policy.get("may_move_semantic_boundary") is not False
        or policy.get("automatic_provider_fallback") is not False
        or policy.get("source_is_read_only") is not True
    ):
        raise ContractError("music structure consumption policy is incomplete")
    if source_path.stat().st_size != payload["source"]["bytes"]:
        raise ContractError("music structure source size drift")


def snap_source_window(
    analysis: dict[str, Any],
    *,
    desired_start_s: float,
    duration_s: float,
    mode: str = "phrase_preferred",
    tolerance_s: float | None = None,
) -> dict[str, Any]:
    validate_music_structure_analysis(analysis)
    desired = _nonnegative(desired_start_s, "desired_start_s")
    duration = _positive(duration_s, "duration_s")
    source_duration = float(analysis["duration_s"])
    maximum_start = source_duration - duration
    if maximum_start < -0.001:
        raise ContractError("requested source window is longer than the music asset")
    desired = min(desired, max(0.0, maximum_start))
    bpm = float(analysis["tempo"]["bpm"])
    default_tolerance = min(1.8, max(0.35, 120.0 / bpm))
    tolerance = (
        default_tolerance
        if tolerance_s is None
        else _positive(tolerance_s, "tolerance_s")
    )
    if mode not in {"phrase_preferred", "downbeat", "beat"}:
        raise ContractError("unsupported music boundary snap mode")

    candidate_groups: list[tuple[str, list[float]]] = []
    if mode == "phrase_preferred":
        candidate_groups.append(
            ("phrase", [float(v) for v in analysis["phrase_boundary_candidates"]])
        )
    if mode in {"phrase_preferred", "downbeat"}:
        candidate_groups.append(("downbeat", [float(v) for v in analysis["downbeats"]]))
    candidate_groups.append(("beat", [float(v) for v in analysis["beats"]]))

    selected: tuple[str, float] | None = None
    for boundary_type, values in candidate_groups:
        eligible = [value for value in values if 0 <= value <= maximum_start + 0.001]
        if not eligible:
            continue
        nearest = min(eligible, key=lambda value: (abs(value - desired), value))
        if abs(nearest - desired) <= tolerance:
            selected = boundary_type, nearest
            break
    snapped = desired if selected is None else selected[1]
    return {
        "desired_start_s": round(desired, 6),
        "snapped_start_s": round(snapped, 6),
        "snapped_end_s": round(snapped + duration, 6),
        "duration_s": round(duration, 6),
        "boundary_type": "unsnapped" if selected is None else selected[0],
        "offset_s": round(snapped - desired, 6),
        "tolerance_s": round(tolerance, 6),
        "status": "kept_original" if selected is None else "snapped",
        "semantic_timeline_moved": False,
    }


def classify_music_family(intent_segment: dict[str, Any]) -> dict[str, Any]:
    """Classify one semantic segment into the reviewed three-family vocabulary."""
    text = _intent_text(intent_segment)
    families = _object(_SPOKEN_BGM_POLICY.get("families"))
    ranked = sorted(
        families.items(),
        key=lambda item: (-int(_object(item[1]).get("priority") or 0), item[0]),
    )
    default_family = next(
        family_id
        for family_id, config in ranked
        if _object(config).get("default") is True
    )
    for family_id, config in ranked:
        terms = sorted(
            {
                str(term).strip()
                for term in _object(config).get("terms", [])
                if str(term).strip()
            }
        )
        matched = [term for term in terms if term in text]
        if matched:
            return {
                "family_id": family_id,
                "matched_terms": matched,
                "reason": "highest-priority semantic family term matched",
            }
    return {
        "family_id": default_family,
        "matched_terms": [],
        "reason": "no story or risk term matched; use the explainer default",
    }


def build_speech_priority_windows(
    intent_segment: dict[str, Any],
    *,
    word_timestamps: Iterable[dict[str, Any]] | None = None,
    subtitle_cues: Iterable[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build deterministic -6 dB speech-priority windows without moving speech."""
    start_s = _nonnegative(intent_segment.get("start_s"), "segment start_s")
    end_s = _positive(intent_segment.get("end_s"), "segment end_s")
    if end_s <= start_s:
        raise ContractError("speech-priority segment end must follow start")
    duration_s = end_s - start_s
    density = _unit(intent_segment.get("narration_density", 0.0), "narration_density")
    segment_terms = _priority_matches(_intent_text(intent_segment))
    family = classify_music_family(intent_segment)
    ordinary_gain = float(DEFAULT_MIX_PROFILE["music_gain_db"])
    priority_gain = float(HIGH_INFORMATION_MIX_PROFILE["music_gain_db"])

    word_rows = _normalize_timing_rows(word_timestamps, start_s, end_s)
    subtitle_rows = _normalize_timing_rows(subtitle_cues, start_s, end_s)
    word_matches = _matched_timing_rows(word_rows)
    subtitle_matches = _matched_timing_rows(subtitle_rows)
    rows: list[dict[str, Any]] = []
    source_mode = ""
    if word_matches:
        rows = word_matches
        source_mode = "word_timestamps"
    elif subtitle_matches:
        rows = subtitle_matches
        source_mode = "srt_subtitles"
    high_information = density >= 0.985 or bool(segment_terms) or bool(rows)

    if not high_information:
        return {
            "music_family": family,
            "source_mode": "none",
            "application_mode": "none",
            "base_music_gain_db": ordinary_gain,
            "speech_priority_music_gain_db": priority_gain,
            "windows": [],
            "coverage_ratio": 0.0,
            "semantic_timeline_moved": False,
        }

    if not rows:
        return {
            "music_family": family,
            "source_mode": "semantic_segment_fallback",
            "application_mode": "full_segment_coverage",
            "base_music_gain_db": ordinary_gain,
            "speech_priority_music_gain_db": priority_gain,
            "windows": [
                {
                    "start_s": round(start_s, 6),
                    "end_s": round(end_s, 6),
                    "music_gain_db": priority_gain,
                    "additional_attenuation_db": round(
                        priority_gain - ordinary_gain, 3
                    ),
                    "matched_terms": segment_terms,
                    "evidence_source": "semantic_segment",
                }
            ],
            "coverage_ratio": 1.0,
            "semantic_timeline_moved": False,
        }

    window_policy = _object(_SPOKEN_BGM_POLICY.get("speech_priority_window"))
    pre_roll = _nonnegative(window_policy.get("pre_roll_s"), "priority pre-roll")
    post_roll = _nonnegative(window_policy.get("post_roll_s"), "priority post-roll")
    merge_gap = _nonnegative(window_policy.get("merge_gap_s"), "priority merge gap")
    expanded = [
        {
            "start_s": max(start_s, float(row["start_s"]) - pre_roll),
            "end_s": min(end_s, float(row["end_s"]) + post_roll),
            "matched_terms": list(row["matched_terms"]),
        }
        for row in rows
    ]
    merged = _merge_priority_windows(expanded, merge_gap)
    coverage = sum(row["end_s"] - row["start_s"] for row in merged) / duration_s
    application_mode = "windowed_attenuation"
    full_threshold = _unit(
        window_policy.get("full_segment_coverage_ratio"),
        "full-segment coverage ratio",
    )
    if coverage >= full_threshold:
        merged = [
            {
                "start_s": start_s,
                "end_s": end_s,
                "matched_terms": sorted(
                    {term for row in merged for term in row["matched_terms"]}
                ),
            }
        ]
        application_mode = "full_segment_coverage"
        coverage = 1.0

    return {
        "music_family": family,
        "source_mode": source_mode,
        "application_mode": application_mode,
        "base_music_gain_db": ordinary_gain,
        "speech_priority_music_gain_db": priority_gain,
        "windows": [
            {
                "start_s": round(float(row["start_s"]), 6),
                "end_s": round(float(row["end_s"]), 6),
                "music_gain_db": priority_gain,
                "additional_attenuation_db": round(priority_gain - ordinary_gain, 3),
                "matched_terms": list(row["matched_terms"]),
                "evidence_source": source_mode,
            }
            for row in merged
        ],
        "coverage_ratio": round(coverage, 6),
        "semantic_timeline_moved": False,
    }


def recommend_mix_profile(
    intent_segment: dict[str, Any],
    *,
    functional_role: str,
) -> dict[str, Any]:
    density = _unit(intent_segment.get("narration_density"), "narration_density")
    text = _intent_text(intent_segment)
    is_body = functional_role not in {
        "opening_warm",
        "opening_clear",
        "lift_warm",
        "lift_clear",
        "ending_warm",
        "ending_clear",
    }
    matched_terms = _priority_matches(text)
    high_information = is_body and (density >= 0.985 or bool(matched_terms))
    selected = HIGH_INFORMATION_MIX_PROFILE if high_information else DEFAULT_MIX_PROFILE
    reason = (
        "body segment has high narration density or explicit numeric/risk terms"
        if high_information
        else "opening/lift/ending or ordinary-density narration keeps the user-approved default"
    )
    return {
        **selected,
        "music_family": classify_music_family(intent_segment),
        "base_music_gain_db": float(DEFAULT_MIX_PROFILE["music_gain_db"]),
        "speech_priority_music_gain_db": float(
            HIGH_INFORMATION_MIX_PROFILE["music_gain_db"]
        ),
        "priority_attenuation_db": round(
            float(HIGH_INFORMATION_MIX_PROFILE["music_gain_db"])
            - float(DEFAULT_MIX_PROFILE["music_gain_db"]),
            3,
        ),
        "functional_role": functional_role,
        "narration_density": density,
        "matched_high_information_terms": matched_terms,
        "reason": reason,
        "automatic_music_boost_allowed": False,
    }


def evaluate_delta_lu(
    *,
    dialogue_lufs: float,
    music_lufs: float,
    profile: dict[str, Any],
) -> dict[str, Any]:
    dialogue = _finite(dialogue_lufs, "dialogue_lufs")
    music = _finite(music_lufs, "music_lufs")
    target = _object(profile.get("target_delta_lu"))
    minimum = _positive(target.get("minimum"), "target delta minimum")
    maximum = _positive(target.get("maximum"), "target delta maximum")
    if maximum <= minimum:
        raise ContractError("target delta range is invalid")
    delta = dialogue - music
    if delta < minimum:
        status = "music_too_prominent"
        safe_gain_correction_db = -(minimum - delta)
    elif delta > maximum:
        status = "music_subtle_user_review"
        safe_gain_correction_db = 0.0
    else:
        status = "within_target"
        safe_gain_correction_db = 0.0
    return {
        "dialogue_lufs": round(dialogue, 3),
        "music_lufs": round(music, 3),
        "delta_lu": round(delta, 3),
        "target_delta_lu": {"minimum": minimum, "maximum": maximum},
        "status": status,
        "safe_gain_correction_db": round(max(-9.0, safe_gain_correction_db), 3),
        "automatic_music_boost_applied": False,
    }


def evaluate_ducking_pump_risk(
    silence_gaps: Iterable[dict[str, Any]],
    *,
    programme_duration_s: float,
    release_ms: int,
) -> dict[str, Any]:
    duration = _positive(programme_duration_s, "programme_duration_s")
    release = int(release_ms)
    if release < 50 or release > 2000:
        raise ContractError("ducking release must be between 50 and 2000 ms")
    normalized = []
    for gap in silence_gaps:
        gap_duration = _positive(gap.get("duration_s"), "silence gap duration")
        normalized.append(gap_duration)
    rebound_gaps = [value for value in normalized if 0.18 <= value <= 1.2]
    rate_per_minute = len(rebound_gaps) / (duration / 60.0)
    ducking_policy = _object(_SPOKEN_BGM_POLICY.get("ducking"))
    dense_rate = _positive(
        ducking_policy.get("dense_short_pause_rate_per_minute"),
        "dense short-pause rate",
    )
    ordinary_release = int(ducking_policy.get("release_ms") or 0)
    dense_release = int(ducking_policy.get("dense_short_pause_release_ms") or 0)
    risk = rate_per_minute >= dense_rate and release <= ordinary_release + 50
    recommended_release = dense_release if risk else release
    return {
        "short_pause_count": len(rebound_gaps),
        "short_pause_rate_per_minute": round(rate_per_minute, 3),
        "release_ms": release,
        "status": "review_release_for_pumping" if risk else "passed",
        "recommended_release_ms": recommended_release,
        "automatic_change_allowed": risk,
    }


def _intent_text(intent_segment: dict[str, Any]) -> str:
    return " ".join(
        str(intent_segment.get(field) or "").strip()
        for field in (
            "text_query",
            "text_query_en",
            "title",
            "purpose",
            "narration",
            "narration_draft",
        )
        if str(intent_segment.get(field) or "").strip()
    )


def _priority_matches(text: str) -> list[str]:
    matched = {term for term in _HIGH_INFORMATION_TERMS if term in text}
    if _NUMBER_PATTERN.search(text):
        matched.add("numeric_expression")
    return sorted(matched)


def _normalize_timing_rows(
    values: Iterable[dict[str, Any]] | None,
    segment_start_s: float,
    segment_end_s: float,
) -> list[dict[str, Any]]:
    if values is None:
        return []
    normalized: list[dict[str, Any]] = []
    for raw in values:
        if not isinstance(raw, dict):
            raise ContractError("speech timing row must be an object")
        text = _semantic_timing_text(
            raw.get("word") or raw.get("text") or raw.get("token") or ""
        )
        if not text:
            continue
        start = _nonnegative(
            raw.get("start_s", raw.get("start")), "speech timing start"
        )
        end = _positive(raw.get("end_s", raw.get("end")), "speech timing end")
        if end <= start:
            raise ContractError("speech timing row end must follow start")
        if end <= segment_start_s or start >= segment_end_s:
            continue
        normalized.append(
            {
                "text": text,
                "start_s": max(segment_start_s, start),
                "end_s": min(segment_end_s, end),
            }
        )
    return sorted(
        normalized, key=lambda row: (row["start_s"], row["end_s"], row["text"])
    )


def _matched_timing_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    combined = ""
    character_ranges: list[tuple[int, int, dict[str, Any]]] = []
    for row in rows:
        start = len(combined)
        combined += str(row["text"])
        character_ranges.append((start, len(combined), row))

    matches: list[tuple[int, int, str]] = []
    for term in sorted(_HIGH_INFORMATION_TERMS):
        offset = 0
        while True:
            index = combined.find(term, offset)
            if index < 0:
                break
            matches.append((index, index + len(term), term))
            offset = index + 1
    matches.extend(
        (match.start(), match.end(), "numeric_expression")
        for match in _NUMBER_PATTERN.finditer(combined)
    )

    selected: dict[int, dict[str, Any]] = {}
    for match_start, match_end, label in matches:
        for index, (row_start, row_end, row) in enumerate(character_ranges):
            if row_end <= match_start or row_start >= match_end:
                continue
            current = selected.setdefault(
                index,
                {
                    "start_s": row["start_s"],
                    "end_s": row["end_s"],
                    "matched_terms": set(),
                },
            )
            current["matched_terms"].add(label)
    return [
        {
            "start_s": row["start_s"],
            "end_s": row["end_s"],
            "matched_terms": sorted(row["matched_terms"]),
        }
        for _, row in sorted(selected.items())
    ]


def _merge_priority_windows(
    windows: list[dict[str, Any]], merge_gap_s: float
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    for row in sorted(windows, key=lambda item: (item["start_s"], item["end_s"])):
        if not merged or row["start_s"] - merged[-1]["end_s"] > merge_gap_s:
            merged.append(
                {
                    "start_s": float(row["start_s"]),
                    "end_s": float(row["end_s"]),
                    "matched_terms": sorted(set(row["matched_terms"])),
                }
            )
            continue
        merged[-1]["end_s"] = max(merged[-1]["end_s"], float(row["end_s"]))
        merged[-1]["matched_terms"] = sorted(
            set(merged[-1]["matched_terms"]).union(row["matched_terms"])
        )
    return merged


def build_music_timing_qa_report(
    *,
    source_reference: dict[str, Any],
    cue_plan_reference: dict[str, Any],
    segment_rows: list[dict[str, Any]],
    ducking: dict[str, Any],
) -> dict[str, Any]:
    if not segment_rows:
        raise ContractError("music timing QA requires segment rows")
    statuses = {str(row.get("delta_lu", {}).get("status")) for row in segment_rows}
    hard_fail = "music_too_prominent" in statuses
    payload: dict[str, Any] = {
        "schema": MUSIC_TIMING_QA_SCHEMA,
        "status": "blocked" if hard_fail else "candidate_only_passed",
        "source": dict(source_reference),
        "cue_plan": dict(cue_plan_reference),
        "segments": segment_rows,
        "ducking": dict(ducking),
        "release_gate": {
            "technical_qa_is_publish_approval": False,
            "music_too_prominent_blocks_candidate": True,
            "subtle_music_requires_only_creative_review": True,
            "candidate_only": True,
        },
    }
    payload["report_sha256"] = payload_sha256(payload, "report_sha256")
    validate_music_timing_qa_report(payload)
    return payload


def validate_music_timing_qa_report(payload: dict[str, Any]) -> None:
    _validate_schema(payload, "music_timing_qa.v1.schema.json")
    if payload.get("schema") != MUSIC_TIMING_QA_SCHEMA:
        raise ContractError("unsupported music timing QA schema")
    if payload.get("report_sha256") != payload_sha256(payload, "report_sha256"):
        raise ContractError("music timing QA integrity check failed")
    validate_artifact_reference(_object(payload.get("source")), "QA source")
    validate_artifact_reference(_object(payload.get("cue_plan")), "QA cue plan")
    statuses = {
        str(_object(row.get("delta_lu")).get("status"))
        for row in payload.get("segments", [])
        if isinstance(row, dict)
    }
    expected_status = (
        "blocked" if "music_too_prominent" in statuses else "candidate_only_passed"
    )
    if payload.get("status") != expected_status:
        raise ContractError("music timing QA status does not match segment evidence")
    gate = _object(payload.get("release_gate"))
    if (
        gate.get("technical_qa_is_publish_approval") is not False
        or gate.get("music_too_prominent_blocks_candidate") is not True
        or gate.get("subtle_music_requires_only_creative_review") is not True
        or gate.get("candidate_only") is not True
    ):
        raise ContractError("music timing QA release boundary is incomplete")


def _provider(value: dict[str, Any]) -> dict[str, Any]:
    result = dict(value)
    for field in ("id", "version", "source_commit", "method", "runtime_status"):
        if not str(result.get(field) or "").strip():
            raise ContractError(f"music structure provider {field} is required")
    if result.get("automatic_fallback") not in {None, False}:
        raise ContractError("music structure provider cannot silently fallback")
    result["automatic_fallback"] = False
    return result


def _media_duration(provider: dict[str, Any], source: Path) -> float:
    if not source.is_file():
        raise ContractError(f"music structure source not found: {source}")
    return _positive(provider.get("media_duration_s"), "provider media_duration_s")


def _source_result_duration(result: dict[str, Any]) -> float:
    segments = result.get("segments")
    if not isinstance(segments, list) or not segments:
        raise ContractError("All-In-One result requires segments")
    return _positive(segments[-1].get("end"), "All-In-One result duration")


def _segments(values: Any, duration_s: float) -> list[dict[str, Any]]:
    if not isinstance(values, list) or not values:
        raise ContractError("music structure segments must be a non-empty list")
    result = []
    previous_end = 0.0
    for index, raw in enumerate(values, 1):
        if not isinstance(raw, dict):
            raise ContractError("music structure segment must be an object")
        start = _nonnegative(raw.get("start"), f"segment {index} start")
        end = _positive(raw.get("end"), f"segment {index} end")
        label = str(raw.get("label") or "").strip()
        if not label or end <= start:
            raise ContractError("music structure segment is invalid")
        if abs(start - previous_end) > 0.051:
            raise ContractError("music structure segments must be contiguous")
        if end > duration_s + 0.051:
            raise ContractError("music structure segment exceeds the source")
        result.append({"start": round(start, 6), "end": round(end, 6), "label": label})
        previous_end = end
    if abs(previous_end - duration_s) > 0.051:
        raise ContractError("music structure segments must cover the source")
    return result


def _phrase_candidates(
    downbeats: list[float],
    segments: list[dict[str, Any]],
    duration_s: float,
    phrase_bars: int,
) -> list[float]:
    values = {0.0, round(duration_s, 6)}
    values.update(round(float(row["start"]), 6) for row in segments)
    values.update(round(float(row["end"]), 6) for row in segments)
    values.update(round(value, 6) for value in downbeats[::phrase_bars])
    return sorted(values)


def _times(values: Any, label: str, duration_s: float) -> list[float]:
    if not isinstance(values, (list, tuple)):
        raise ContractError(f"{label} must be an array")
    result = [round(_nonnegative(value, label), 6) for value in values]
    if result != sorted(result) or len(result) != len(set(result)):
        raise ContractError(f"{label} must be strictly increasing")
    if any(value > duration_s + 0.051 for value in result):
        raise ContractError(f"{label} exceed the source duration")
    return result


def _contains_close(values: list[float], target: float, tolerance: float) -> bool:
    return any(abs(value - target) <= tolerance for value in values)


def _object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _finite(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise ContractError(f"{label} must be finite")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{label} must be finite") from exc
    if not math.isfinite(result):
        raise ContractError(f"{label} must be finite")
    return result


def _nonnegative(value: Any, label: str) -> float:
    result = _finite(value, label)
    if result < 0:
        raise ContractError(f"{label} must be non-negative")
    return result


def _positive(value: Any, label: str) -> float:
    result = _finite(value, label)
    if result <= 0:
        raise ContractError(f"{label} must be positive")
    return result


def _unit(value: Any, label: str) -> float:
    result = _finite(value, label)
    if not 0 <= result <= 1:
        raise ContractError(f"{label} must be in [0, 1]")
    return result


def _validate_schema(payload: dict[str, Any], filename: str) -> None:
    schema_path = _SCHEMA_ROOT / filename
    try:
        import json

        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ContractError(
            f"music timing schema is unreadable: {schema_path}"
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
        raise ContractError("music timing schema failed: " + "; ".join(messages))


_SPOKEN_BGM_POLICY = spoken_bgm_policy()
DEFAULT_MIX_PROFILE = dict(_SPOKEN_BGM_POLICY["mix_profiles"]["ordinary"])
HIGH_INFORMATION_MIX_PROFILE = dict(
    _SPOKEN_BGM_POLICY["mix_profiles"]["speech_priority"]
)
_HIGH_INFORMATION_TERMS = set(_SPOKEN_BGM_POLICY["speech_priority_terms"])
