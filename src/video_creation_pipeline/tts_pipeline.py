from __future__ import annotations

import hashlib
import json
import math
import re
import wave
from array import array
from pathlib import Path
from typing import Any

from .errors import ContractError
from .reference_remix import (
    VOICE_MEASUREMENTS_SCHEMA,
    normalize_reference_recipe,
)

TTS_CONFIG_SCHEMA = "video_creation_pipeline.tts_config.v1"
TTS_JOB_SCHEMA = "video_creation_pipeline.tts_job.v1"
TTS_RECEIPTS_SCHEMA = "video_creation_pipeline.tts_chunk_receipts.v1"
TTS_RESUME_PLAN_SCHEMA = "video_creation_pipeline.tts_resume_plan.v1"
AUDIO_STITCH_QA_SCHEMA = "video_creation_pipeline.audio_stitch_qa.v1"
STITCHED_VOICE_OUTPUTS_SCHEMA = "video_creation_pipeline.stitched_voice_outputs.v1"

_PAUSE_DEFAULTS = {
    "paragraph": 520,
    "sentence": 360,
    "clause": 180,
    "hard_split": 80,
    "end": 0,
}
_CHINESE_DIGITS = "零一二三四五六七八九"


def normalize_tts_text(
    text: str, term_rules: list[dict[str, Any]] | None = None
) -> tuple[str, list[dict[str, str]]]:
    """Return deterministic spoken text and an auditable replacement list."""
    value = str(text)
    replacements: list[dict[str, str]] = []

    def replace_literal(source: str, target: str, kind: str) -> None:
        nonlocal value
        if source and source in value:
            replacements.append({"kind": kind, "source": source, "target": target})
            value = value.replace(source, target)

    rules = term_rules or []
    for rule in sorted(
        rules, key=lambda row: len(str(row.get("find") or "")), reverse=True
    ):
        if not isinstance(rule, dict):
            raise ContractError("term rule must be an object")
        source = str(rule.get("find") or "")
        target = str(rule.get("replace") or "")
        if not source or not target:
            raise ContractError("term rule find and replace must be non-empty")
        replace_literal(source, target, "term")

    def substitute(pattern: str, kind: str, convert: Any) -> None:
        nonlocal value

        def callback(match: re.Match[str]) -> str:
            target = str(convert(match))
            replacements.append(
                {"kind": kind, "source": match.group(0), "target": target}
            )
            return target

        value = re.sub(pattern, callback, value)

    substitute(
        r"(?<!\d)(\d{4})[-/](\d{1,2})[-/](\d{1,2})(?!\d)",
        "date",
        lambda m: (
            "".join(_CHINESE_DIGITS[int(ch)] for ch in m.group(1))
            + "年"
            + _integer_to_chinese(int(m.group(2)))
            + "月"
            + _integer_to_chinese(int(m.group(3)))
            + "日"
        ),
    )
    substitute(
        r"(?<!\d)(\d+(?:\.\d+)?)%(?!\d)",
        "percent",
        lambda m: "百分之" + _decimal_to_chinese(m.group(1)),
    )
    substitute(
        r"(?<!\d)1\d{10}(?!\d)",
        "phone",
        lambda m: " ".join(_CHINESE_DIGITS[int(ch)] for ch in m.group(0)),
    )
    substitute(
        r"(?<![\d.])\d+\.\d+(?![\d.])",
        "decimal",
        lambda m: _decimal_to_chinese(m.group(0)),
    )
    substitute(
        r"(?<![\dA-Za-z])\d+(?![\dA-Za-z])",
        "integer",
        lambda m: _integer_to_chinese(int(m.group(0))),
    )
    substitute(
        r"(?<![A-Za-z])[A-Z]{2,}(?![A-Za-z])",
        "acronym",
        lambda m: " ".join(m.group(0)),
    )
    value = re.sub(r"[ \t]+", " ", value).strip()
    return value, replacements


def build_tts_job(
    recipe_source: dict[str, Any] | str | Path,
    config_source: dict[str, Any] | str | Path,
) -> dict[str, Any]:
    recipe = normalize_reference_recipe(recipe_source)
    config = _read_object(config_source, "TTS config")
    if config.get("schema") != TTS_CONFIG_SCHEMA:
        raise ContractError(f"TTS config must use {TTS_CONFIG_SCHEMA}")

    intended_use = str(config.get("intended_use") or "").strip()
    if intended_use not in {"research_noncommercial", "commercial"}:
        raise ContractError("intended_use must be research_noncommercial or commercial")
    provider = _provider_config(config.get("provider"))
    is_chattts = "chattts" in (provider["id"] + " " + provider["model"]).lower()
    if intended_use == "commercial" and (
        is_chattts or not provider["commercial_use_allowed"]
    ):
        raise ContractError(
            f"provider {provider['id']} is not authorized for commercial use"
        )
    voice_profile = _voice_profile(config.get("voice_profile"))
    voice_sha256 = _payload_sha256(voice_profile)
    policy = _chunk_policy(config.get("chunk_policy"))
    term_rules = config.get("term_rules") or []
    if not isinstance(term_rules, list):
        raise ContractError("term_rules must be a list")

    chunks: list[dict[str, Any]] = []
    for shot in recipe["shots"]:
        shot_chunks = _split_script(
            shot["script"],
            term_rules=term_rules,
            policy=policy,
        )
        for index, row in enumerate(shot_chunks, 1):
            chunk_id = f"{shot['shot_id']}-chunk-{index:03d}"
            chunk_input = {
                "provider_id": provider["id"],
                "provider_model": provider["model"],
                "voice_profile_sha256": voice_sha256,
                "normalized_text": row["normalized_text"],
            }
            input_sha256 = _payload_sha256(chunk_input)
            identity = {"chunk_id": chunk_id, "input_sha256": input_sha256}
            chunks.append(
                {
                    "chunk_id": chunk_id,
                    "shot_id": shot["shot_id"],
                    "order_in_shot": index,
                    **row,
                    "voice_profile_sha256": voice_sha256,
                    "input_sha256": input_sha256,
                    "idempotency_key": _payload_sha256(identity),
                    "retry_limit": policy["retry_limit"],
                }
            )

    registry = config.get("run_registry") or {}
    if registry and not isinstance(registry, dict):
        raise ContractError("run_registry must be an object")
    job: dict[str, Any] = {
        "schema": TTS_JOB_SCHEMA,
        "inputs": {
            "reference_recipe_sha256": _payload_sha256(recipe),
            "tts_config_sha256": _payload_sha256(config),
        },
        "intended_use": intended_use,
        "provider": provider,
        "voice_profile": voice_profile,
        "voice_profile_sha256": voice_sha256,
        "chunk_policy": policy,
        "chunks": chunks,
        "run_registry": {
            "reference": str(registry.get("reference") or ""),
            "authoritative": bool(registry.get("authoritative", True)),
            "local_status_machine_created": False,
        },
        "result_contract": {
            "receipt_schema": TTS_RECEIPTS_SCHEMA,
            "one_receipt_per_attempt": True,
            "completed_audio_must_be_local_and_hashed": True,
            "resume_is_derived_from_job_and_receipts": True,
        },
        "execution_boundary": {
            "candidate_only": True,
            "tts_executed": False,
            "network_execution_performed": False,
            "upload_execution_performed": False,
            "ffmpeg_executed": False,
            "ffmpeg_orchestration_created": False,
            "local_status_machine_created": False,
            "automatic_publish": False,
            "automatic_local_cloud_fallback": False,
        },
    }
    job["job_sha256"] = _payload_sha256(job, "job_sha256")
    job["run_id"] = f"tts-{job['job_sha256'][:16]}"
    job["job_sha256"] = _payload_sha256(job, "job_sha256")
    return job


def build_resume_plan(
    job_source: dict[str, Any] | str | Path,
    receipts_source: dict[str, Any] | str | Path,
) -> dict[str, Any]:
    job = _validated_job(job_source)
    receipts = _validated_receipts(job, receipts_source, inspect_audio=True)
    latest = _latest_receipts(receipts["receipts"])
    completed: list[str] = []
    retryable: list[str] = []
    pending: list[str] = []
    blocked: list[str] = []
    next_attempts: list[dict[str, Any]] = []

    for chunk in job["chunks"]:
        receipt = latest.get(chunk["chunk_id"])
        if receipt is None:
            pending.append(chunk["chunk_id"])
            next_attempts.append(_next_attempt(chunk, 1, "not_started"))
        elif receipt["status"] == "completed":
            completed.append(chunk["chunk_id"])
        elif receipt["attempt"] < chunk["retry_limit"]:
            retryable.append(chunk["chunk_id"])
            next_attempts.append(
                _next_attempt(chunk, receipt["attempt"] + 1, receipt["status"])
            )
        else:
            blocked.append(chunk["chunk_id"])

    plan: dict[str, Any] = {
        "schema": TTS_RESUME_PLAN_SCHEMA,
        "inputs": {
            "tts_job_sha256": job["job_sha256"],
            "receipts_sha256": _payload_sha256(receipts),
        },
        "run_id": job["run_id"],
        "summary": {
            "status": "blocked"
            if blocked
            else ("complete" if len(completed) == len(job["chunks"]) else "ready"),
            "chunk_count": len(job["chunks"]),
            "completed_count": len(completed),
            "pending_count": len(pending),
            "retryable_count": len(retryable),
            "blocked_count": len(blocked),
        },
        "completed_chunk_ids": completed,
        "pending_chunk_ids": pending,
        "retryable_chunk_ids": retryable,
        "blocked_chunk_ids": blocked,
        "next_attempts": next_attempts,
        "derivation_boundary": {
            "stored_as_runtime_state": False,
            "job_and_receipts_remain_authoritative": True,
            "network_execution_performed": False,
        },
    }
    plan["plan_sha256"] = _payload_sha256(plan, "plan_sha256")
    return plan


def build_audio_stitch_qa(
    job_source: dict[str, Any] | str | Path,
    receipts_source: dict[str, Any] | str | Path,
    *,
    max_loudness_delta_db: float = 4.0,
    max_seam_delta: float = 0.35,
    max_clipping_ratio: float = 0.001,
    max_edge_silence_ms: float = 1500.0,
) -> dict[str, Any]:
    job = _validated_job(job_source)
    receipts = _validated_receipts(job, receipts_source, inspect_audio=True)
    latest = _latest_receipts(receipts["receipts"])
    ordered: list[dict[str, Any]] = []
    issues: list[dict[str, str]] = []
    previous: dict[str, Any] | None = None

    for chunk in job["chunks"]:
        receipt = latest.get(chunk["chunk_id"])
        if receipt is None or receipt["status"] != "completed":
            issues.append(
                {"kind": "missing_completed_audio", "chunk_id": chunk["chunk_id"]}
            )
            continue
        audio = _inspect_wav(Path(receipt["output"]["path"]))
        segment = {
            "chunk_id": chunk["chunk_id"],
            "shot_id": chunk["shot_id"],
            "path": receipt["output"]["path"],
            "sha256": receipt["output"]["sha256"],
            "pause_after_ms": chunk["pause_after_ms"],
            "audio": {
                key: value for key, value in audio.items() if not key.startswith("_")
            },
        }
        if audio["clipping_ratio"] > max_clipping_ratio:
            issues.append({"kind": "clipping_detected", "chunk_id": chunk["chunk_id"]})
        if (
            audio["leading_silence_ms"] > max_edge_silence_ms
            or audio["trailing_silence_ms"] > max_edge_silence_ms
        ):
            issues.append(
                {"kind": "abnormal_edge_silence", "chunk_id": chunk["chunk_id"]}
            )
        if previous is not None:
            if audio["format_key"] != previous["audio"]["format_key"]:
                issues.append(
                    {"kind": "format_mismatch", "chunk_id": chunk["chunk_id"]}
                )
            loudness_delta = abs(audio["rms_dbfs"] - previous["audio"]["rms_dbfs"])
            seam_delta = abs(audio["_first_sample"] - previous["audio"]["_last_sample"])
            segment["seam_from_previous"] = {
                "loudness_delta_db": round(loudness_delta, 3),
                "sample_delta": round(seam_delta, 5),
                "planned_pause_ms": previous["pause_after_ms"],
            }
            if loudness_delta > max_loudness_delta_db:
                issues.append({"kind": "loudness_jump", "chunk_id": chunk["chunk_id"]})
            if seam_delta > max_seam_delta:
                issues.append(
                    {"kind": "sample_discontinuity", "chunk_id": chunk["chunk_id"]}
                )
        ordered.append(segment)
        previous = {"audio": audio, "pause_after_ms": chunk["pause_after_ms"]}

    ready = len(ordered) == len(job["chunks"]) and not issues
    qa: dict[str, Any] = {
        "schema": AUDIO_STITCH_QA_SCHEMA,
        "inputs": {
            "tts_job_sha256": job["job_sha256"],
            "receipts_sha256": _payload_sha256(receipts),
        },
        "thresholds": {
            "max_loudness_delta_db": max_loudness_delta_db,
            "max_seam_delta": max_seam_delta,
            "max_clipping_ratio": max_clipping_ratio,
            "max_edge_silence_ms": max_edge_silence_ms,
        },
        "segments": ordered,
        "issues": issues,
        "readiness": {
            "status": "ready_for_ffmpeg_stitch" if ready else "blocked_or_needs_review",
            "ready": ready,
            "completed_segment_count": len(ordered),
            "required_segment_count": len(job["chunks"]),
        },
        "ffmpeg_handoff": {
            "outlet": "existing_project_ffmpeg_orchestration",
            "ordered_segments": [
                {
                    "path": row["path"],
                    "sha256": row["sha256"],
                    "pause_after_ms": row["pause_after_ms"],
                }
                for row in ordered
            ],
            "command_generated": False,
            "executed": False,
        },
        "execution_boundary": {
            "ffmpeg_executed": False,
            "ffmpeg_orchestration_created": False,
            "audio_modified": False,
        },
    }
    qa["qa_sha256"] = _payload_sha256(qa, "qa_sha256")
    return qa


def build_voice_measurements(
    job_source: dict[str, Any] | str | Path,
    stitched_outputs_source: dict[str, Any] | str | Path,
) -> dict[str, Any]:
    job = _validated_job(job_source)
    outputs = _read_object(stitched_outputs_source, "stitched voice outputs")
    if outputs.get("schema") != STITCHED_VOICE_OUTPUTS_SCHEMA:
        raise ContractError(
            f"stitched voice outputs must use {STITCHED_VOICE_OUTPUTS_SCHEMA}"
        )
    rows = outputs.get("shots")
    if not isinstance(rows, list):
        raise ContractError("stitched voice outputs shots must be a list")
    expected = {chunk["shot_id"] for chunk in job["chunks"]}
    seen: set[str] = set()
    measurements: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            raise ContractError("stitched voice output must be an object")
        shot_id = str(row.get("shot_id") or "").strip()
        path = Path(str(row.get("path") or "")).expanduser().resolve()
        if shot_id not in expected or shot_id in seen:
            raise ContractError(f"invalid or duplicate stitched shot: {shot_id}")
        audio = _inspect_wav(path)
        expected_sha = str(row.get("sha256") or "").strip()
        actual_sha = _sha256(path)
        if expected_sha and expected_sha != actual_sha:
            raise ContractError(f"stitched audio hash mismatch: {shot_id}")
        seen.add(shot_id)
        measurements.append(
            {
                "shot_id": shot_id,
                "audio_path": str(path),
                "audio_sha256": actual_sha,
                "measured_duration_s": audio["duration_s"],
                "sample_rate": audio["sample_rate"],
                "channels": audio["channels"],
                "sample_width_bits": audio["sample_width_bits"],
                "peak_dbfs": audio["peak_dbfs"],
                "rms_dbfs": audio["rms_dbfs"],
                "chunk_count": sum(
                    1 for chunk in job["chunks"] if chunk["shot_id"] == shot_id
                ),
            }
        )
    result: dict[str, Any] = {
        "schema": VOICE_MEASUREMENTS_SCHEMA,
        "source_tts_job_sha256": job["job_sha256"],
        "shots": measurements,
        "readiness": {
            "status": "ready_for_voice_timing_plan"
            if seen == expected
            else "missing_stitched_voice_outputs",
            "expected_shot_count": len(expected),
            "measured_shot_count": len(seen),
            "missing_shot_ids": sorted(expected - seen),
        },
    }
    result["measurements_sha256"] = _payload_sha256(result, "measurements_sha256")
    return result


def write_tts_contract(payload: dict[str, Any], output: str | Path) -> Path:
    hash_fields = ("job_sha256", "plan_sha256", "qa_sha256", "measurements_sha256")
    present = [field for field in hash_fields if field in payload]
    if len(present) != 1 or payload[present[0]] != _payload_sha256(payload, present[0]):
        raise ContractError("TTS contract integrity check failed")
    path = Path(output).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return path


def _split_script(
    text: str,
    *,
    term_rules: list[dict[str, Any]],
    policy: dict[str, Any],
) -> list[dict[str, Any]]:
    if not text.strip():
        raise ContractError("each TTS shot must contain non-empty script text")
    hard_char_limit = min(
        policy["max_chars"],
        max(
            1,
            math.floor(policy["chars_per_second"] * policy["max_estimated_duration_s"]),
        ),
    )
    rows: list[dict[str, Any]] = []
    cursor = 0
    while cursor < len(text):
        while cursor < len(text) and text[cursor].isspace():
            cursor += 1
        if cursor >= len(text):
            break
        end = min(len(text), cursor + hard_char_limit)
        if end < len(text):
            boundary = _best_boundary(text, cursor, end)
            end = boundary if boundary > cursor else end
        while end > cursor:
            original = text[cursor:end]
            normalized, replacements = normalize_tts_text(original, term_rules)
            if (
                len(normalized) <= policy["max_chars"]
                and _estimate_tokens(normalized) <= policy["max_tokens"]
                and len(normalized) / policy["chars_per_second"]
                <= policy["max_estimated_duration_s"]
            ):
                break
            end -= 1
        if end <= cursor:
            raise ContractError("chunk policy cannot fit one normalized character")
        original_end = end
        original = text[cursor:original_end]
        normalized, replacements = normalize_tts_text(original, term_rules)
        pause_type = _pause_type(text, original_end)
        if original_end >= len(text):
            pause_type = "end"
        rows.append(
            {
                "source_range": {"start": cursor, "end": original_end},
                "original_text": original,
                "normalized_text": normalized,
                "normalization_replacements": replacements,
                "normalized_char_count": len(normalized),
                "estimated_token_count": _estimate_tokens(normalized),
                "estimated_duration_s": round(
                    len(normalized) / policy["chars_per_second"], 3
                ),
                "pause_type": pause_type,
                "pause_after_ms": policy["pause_ms"][pause_type],
            }
        )
        cursor = original_end
    return rows


def _best_boundary(text: str, start: int, end: int) -> int:
    window = text[start:end]
    for pattern in (r"[。！？!?\n]", r"[；;]", r"[，,、：:]"):
        matches = list(re.finditer(pattern, window))
        if matches:
            return start + matches[-1].end()
    return end


def _pause_type(text: str, end: int) -> str:
    fragment = text[:end]
    if fragment.endswith("\n\n"):
        return "paragraph"
    tail = fragment.rstrip()[-1:] if fragment.rstrip() else ""
    if tail in "。！？!?":
        return "sentence"
    if tail in "，,、；;：:":
        return "clause"
    return "hard_split"


def _provider_config(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ContractError("provider must be an object")
    provider = {
        "id": str(raw.get("id") or "").strip(),
        "kind": str(raw.get("kind") or "").strip(),
        "model": str(raw.get("model") or "").strip(),
        "source_commit": str(raw.get("source_commit") or "").strip(),
        "license_scope": str(raw.get("license_scope") or "").strip(),
        "commercial_use_allowed": raw.get("commercial_use_allowed") is True,
    }
    if not provider["id"] or not provider["kind"] or not provider["model"]:
        raise ContractError("provider id, kind and model are required")
    return provider


def _voice_profile(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ContractError("voice_profile must be an object")
    embedding = str(raw.get("speaker_embedding_ref") or "").strip()
    seed = raw.get("seed")
    if not embedding and not isinstance(seed, int):
        raise ContractError(
            "voice profile requires speaker_embedding_ref or integer seed"
        )
    result = {
        "profile_id": str(raw.get("profile_id") or "").strip(),
        "speaker_embedding_ref": embedding,
        "seed": seed if isinstance(seed, int) else None,
        "temperature": _bounded_number(
            raw.get("temperature", 0.3), "temperature", 0, 2
        ),
        "top_p": _bounded_number(raw.get("top_p", 0.7), "top_p", 0, 1),
        "top_k": int(_bounded_number(raw.get("top_k", 20), "top_k", 1, 1000)),
    }
    if not result["profile_id"]:
        raise ContractError("voice profile_id is required")
    return result


def _chunk_policy(raw: Any) -> dict[str, Any]:
    raw = raw or {}
    if not isinstance(raw, dict):
        raise ContractError("chunk_policy must be an object")
    pause = dict(_PAUSE_DEFAULTS)
    custom_pause = raw.get("pause_ms") or {}
    if not isinstance(custom_pause, dict):
        raise ContractError("chunk_policy.pause_ms must be an object")
    for key, value in custom_pause.items():
        if key not in pause or not isinstance(value, int) or value < 0:
            raise ContractError(f"invalid pause setting: {key}")
        pause[key] = value
    result = {
        "max_chars": int(
            _bounded_number(raw.get("max_chars", 140), "max_chars", 1, 2000)
        ),
        "max_tokens": int(
            _bounded_number(raw.get("max_tokens", 180), "max_tokens", 1, 4000)
        ),
        "max_estimated_duration_s": _bounded_number(
            raw.get("max_estimated_duration_s", 18),
            "max_estimated_duration_s",
            0.1,
            300,
        ),
        "chars_per_second": _bounded_number(
            raw.get("chars_per_second", 4.2), "chars_per_second", 0.1, 30
        ),
        "retry_limit": int(
            _bounded_number(raw.get("retry_limit", 3), "retry_limit", 1, 20)
        ),
        "pause_ms": pause,
    }
    return result


def _validated_job(source: dict[str, Any] | str | Path) -> dict[str, Any]:
    job = _read_object(source, "TTS job")
    if job.get("schema") != TTS_JOB_SCHEMA:
        raise ContractError(f"TTS job must use {TTS_JOB_SCHEMA}")
    if job.get("job_sha256") != _payload_sha256(job, "job_sha256"):
        raise ContractError("TTS job integrity check failed")
    if not isinstance(job.get("chunks"), list) or not job["chunks"]:
        raise ContractError("TTS job must contain chunks")
    return job


def _validated_receipts(
    job: dict[str, Any],
    source: dict[str, Any] | str | Path,
    *,
    inspect_audio: bool,
) -> dict[str, Any]:
    payload = _read_object(source, "TTS receipts")
    if payload.get("schema") != TTS_RECEIPTS_SCHEMA:
        raise ContractError(f"TTS receipts must use {TTS_RECEIPTS_SCHEMA}")
    if payload.get("tts_job_sha256") != job["job_sha256"]:
        raise ContractError("TTS receipts bind a different job")
    rows = payload.get("receipts")
    if not isinstance(rows, list):
        raise ContractError("TTS receipts must be a list")
    chunks = {row["chunk_id"]: row for row in job["chunks"]}
    seen_attempts: set[tuple[str, int]] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ContractError("TTS receipt must be an object")
        chunk_id = str(row.get("chunk_id") or "")
        chunk = chunks.get(chunk_id)
        attempt = row.get("attempt")
        if chunk is None or not isinstance(attempt, int) or attempt < 1:
            raise ContractError("TTS receipt chunk or attempt is invalid")
        if (chunk_id, attempt) in seen_attempts:
            raise ContractError(f"duplicate TTS receipt attempt: {chunk_id}/{attempt}")
        seen_attempts.add((chunk_id, attempt))
        if row.get("idempotency_key") != chunk["idempotency_key"]:
            raise ContractError(f"TTS receipt input mismatch: {chunk_id}")
        if row.get("input_sha256") != chunk["input_sha256"]:
            raise ContractError(f"TTS receipt input hash mismatch: {chunk_id}")
        if row.get("voice_profile_sha256") != job["voice_profile_sha256"]:
            raise ContractError(f"TTS receipt voice profile mismatch: {chunk_id}")
        if row.get("provider_id") != job["provider"]["id"]:
            raise ContractError(f"TTS receipt provider mismatch: {chunk_id}")
        if row.get("status") not in {"completed", "failed", "cancelled"}:
            raise ContractError(f"invalid TTS receipt status: {chunk_id}")
        if row["status"] == "completed":
            output = row.get("output")
            if not isinstance(output, dict):
                raise ContractError(f"completed TTS receipt has no output: {chunk_id}")
            path = Path(str(output.get("path") or "")).expanduser().resolve()
            if not path.is_file() or output.get("sha256") != _sha256(path):
                raise ContractError(
                    f"completed TTS output is missing or changed: {chunk_id}"
                )
            if inspect_audio:
                audio = _inspect_wav(path)
                if round(float(output.get("duration_s", -1)), 3) != audio["duration_s"]:
                    raise ContractError(f"TTS output duration mismatch: {chunk_id}")
    return payload


def _latest_receipts(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for row in rows:
        current = latest.get(row["chunk_id"])
        if current is None or row["attempt"] > current["attempt"]:
            latest[row["chunk_id"]] = row
    return latest


def _next_attempt(chunk: dict[str, Any], attempt: int, reason: str) -> dict[str, Any]:
    return {
        "chunk_id": chunk["chunk_id"],
        "attempt": attempt,
        "reason": reason,
        "idempotency_key": chunk["idempotency_key"],
        "normalized_text": chunk["normalized_text"],
        "voice_profile_sha256": chunk["voice_profile_sha256"],
    }


def _inspect_wav(path: Path) -> dict[str, Any]:
    path = path.expanduser().resolve()
    if not path.is_file():
        raise ContractError(f"WAV file not found: {path}")
    try:
        with wave.open(str(path), "rb") as handle:
            channels = handle.getnchannels()
            sample_width = handle.getsampwidth()
            sample_rate = handle.getframerate()
            frames = handle.getnframes()
            raw = handle.readframes(frames)
    except (wave.Error, OSError) as exc:
        raise ContractError(f"invalid WAV file {path}: {exc}") from exc
    if sample_width != 2:
        raise ContractError(f"only 16-bit PCM WAV is supported for seam QA: {path}")
    samples = array("h")
    samples.frombytes(raw)
    if not samples:
        raise ContractError(f"WAV file has no samples: {path}")
    maximum = 32767.0
    peak = max(abs(value) for value in samples) / maximum
    rms = math.sqrt(sum(value * value for value in samples) / len(samples)) / maximum
    threshold = maximum * (10 ** (-45 / 20))
    leading = _silence_samples(samples, threshold, forward=True)
    trailing = _silence_samples(samples, threshold, forward=False)
    return {
        "sample_rate": sample_rate,
        "channels": channels,
        "sample_width_bits": sample_width * 8,
        "duration_s": round(frames / sample_rate, 3),
        "peak_dbfs": round(_dbfs(peak), 3),
        "rms_dbfs": round(_dbfs(rms), 3),
        "clipping_ratio": round(
            sum(1 for value in samples if abs(value) >= 32760) / len(samples), 6
        ),
        "leading_silence_ms": round(leading / channels / sample_rate * 1000, 1),
        "trailing_silence_ms": round(trailing / channels / sample_rate * 1000, 1),
        "format_key": f"pcm_s16le/{sample_rate}/{channels}",
        "_first_sample": samples[0] / maximum,
        "_last_sample": samples[-1] / maximum,
    }


def _silence_samples(samples: array[int], threshold: float, *, forward: bool) -> int:
    iterable = samples if forward else reversed(samples)
    count = 0
    for value in iterable:
        if abs(value) > threshold:
            break
        count += 1
    return count


def _dbfs(value: float) -> float:
    return 20 * math.log10(max(value, 1e-12))


def _estimate_tokens(text: str) -> int:
    cjk = len(re.findall(r"[\u3400-\u9fff]", text))
    words = len(re.findall(r"[A-Za-z0-9]+", text))
    punctuation = len(re.findall(r"[^\w\s\u3400-\u9fff]", text))
    return max(1, cjk + words + math.ceil(punctuation / 2))


def _decimal_to_chinese(value: str) -> str:
    integer, dot, fraction = value.partition(".")
    result = _integer_to_chinese(int(integer))
    if dot:
        result += "点" + "".join(_CHINESE_DIGITS[int(ch)] for ch in fraction)
    return result


def _integer_to_chinese(value: int) -> str:
    if value == 0:
        return _CHINESE_DIGITS[0]
    if value >= 10000:
        high, low = divmod(value, 10000)
        bridge = "零" if 0 < low < 1000 else ""
        return (
            _integer_to_chinese(high)
            + "万"
            + bridge
            + (_integer_to_chinese(low) if low else "")
        )
    units = ((1000, "千"), (100, "百"), (10, "十"), (1, ""))
    result = ""
    pending_zero = False
    remaining = value
    for divisor, unit in units:
        digit, remaining = divmod(remaining, divisor)
        if digit:
            if pending_zero and result:
                result += "零"
            if not (divisor == 10 and digit == 1 and not result):
                result += _CHINESE_DIGITS[digit]
            result += unit
            pending_zero = False
        elif result and remaining:
            pending_zero = True
    return result


def _bounded_number(value: Any, label: str, minimum: float, maximum: float) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{label} must be a number") from exc
    if not math.isfinite(result) or not minimum <= result <= maximum:
        raise ContractError(f"{label} must be between {minimum} and {maximum}")
    return result


def _read_object(source: dict[str, Any] | str | Path, label: str) -> dict[str, Any]:
    if isinstance(source, dict):
        return json.loads(json.dumps(source))
    path = Path(source).expanduser().resolve()
    if not path.is_file():
        raise ContractError(f"{label} not found: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"{label} is unreadable: {exc}") from exc
    if not isinstance(payload, dict):
        raise ContractError(f"{label} must be a JSON object")
    return payload


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _payload_sha256(payload: dict[str, Any], excluded: str = "") -> str:
    value = dict(payload)
    if excluded:
        value.pop(excluded, None)
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
