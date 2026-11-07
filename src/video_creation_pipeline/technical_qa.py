from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .contract_io import (
    artifact_reference,
    atomic_write_json,
    payload_sha256,
    read_json_object,
    validate_artifact_reference,
)
from .errors import ContractError
from .execution_receipts import validate_ffmpeg_execution_receipt

TECHNICAL_QA_REPORT_SCHEMA = "video_creation_pipeline.technical_qa_report.v1"

DEFAULT_TECHNICAL_QA_POLICY = {
    "target_width": 1280,
    "target_height": 720,
    "target_fps": 30.0,
    "fps_tolerance": 0.1,
    "duration_tolerance_s": 0.35,
    "max_black_ratio": 0.05,
    "max_silence_ratio": 0.25,
    "freeze_warning_ratio": 0.5,
    "max_subtitle_line_chars": 80,
    "require_audio": True,
    "require_subtitles": True,
    "require_packaging_plan": True,
}


def build_technical_qa_report(
    media_path: str | Path,
    probe_path: str | Path,
    ffmpeg_receipt_path: str | Path,
    *,
    subtitles_path: str | Path | None = None,
    packaging_plan_path: str | Path | None = None,
    expected_duration_s: float | None = None,
    policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    media = Path(media_path).expanduser().resolve()
    probe = Path(probe_path).expanduser().resolve()
    receipt = Path(ffmpeg_receipt_path).expanduser().resolve()
    normalized_policy = _normalize_policy(policy)
    expected_duration = _optional_nonnegative_number(
        expected_duration_s, "expected duration"
    )
    inputs = {
        "media": artifact_reference(media, "technical QA media"),
        "probe": artifact_reference(probe, "ffprobe JSON"),
        "ffmpeg_receipt": artifact_reference(receipt, "technical QA FFmpeg receipt"),
        "subtitles": _optional_artifact(subtitles_path, "subtitles"),
        "packaging_plan": _optional_artifact(packaging_plan_path, "packaging plan"),
    }
    detector_path = _validate_receipt_binding(receipt, inputs["media"])
    inputs["detector_log"] = artifact_reference(detector_path, "FFmpeg detector log")
    evaluation = _evaluate(inputs, expected_duration, normalized_policy)
    report: dict[str, Any] = {
        "schema": TECHNICAL_QA_REPORT_SCHEMA,
        "created_at": _now(),
        "inputs": inputs,
        "expected_duration_s": expected_duration,
        "policy": normalized_policy,
        **evaluation,
        "authority_boundary": {
            "technical_checks_automatic": True,
            "creative_approval_claimed": False,
            "release_eligible": False,
            "automatic_publish": False,
            "source_media_modified": False,
            "second_ffmpeg_orchestrator_created": False,
        },
    }
    report["report_sha256"] = payload_sha256(report, "report_sha256")
    validate_technical_qa_report(report)
    return report


def validate_technical_qa_report(report: dict[str, Any]) -> None:
    if report.get("schema") != TECHNICAL_QA_REPORT_SCHEMA:
        raise ContractError("unsupported technical QA report schema")
    if report.get("report_sha256") != payload_sha256(report, "report_sha256"):
        raise ContractError("technical QA report integrity check failed")
    _parse_datetime(report.get("created_at"), "technical QA created_at")
    inputs = _object(report.get("inputs"))
    media_path = validate_artifact_reference(
        _object(inputs.get("media")), "technical QA media"
    )
    validate_artifact_reference(_object(inputs.get("probe")), "ffprobe JSON")
    receipt_path = validate_artifact_reference(
        _object(inputs.get("ffmpeg_receipt")),
        "technical QA FFmpeg receipt",
    )
    detector_path = validate_artifact_reference(
        _object(inputs.get("detector_log")), "FFmpeg detector log"
    )
    expected_detector = _validate_receipt_binding(
        receipt_path, _object(inputs.get("media"))
    )
    if detector_path != expected_detector:
        raise ContractError("technical QA detector log is not receipt-bound")
    for field, label in (
        ("subtitles", "subtitles"),
        ("packaging_plan", "packaging plan"),
    ):
        _validate_optional_artifact(inputs.get(field), label)

    policy = _normalize_policy(_object(report.get("policy")))
    if report.get("policy") != policy:
        raise ContractError("technical QA policy snapshot is invalid")
    expected_duration = _optional_nonnegative_number(
        report.get("expected_duration_s"), "expected duration"
    )
    evaluation = _evaluate(inputs, expected_duration, policy)
    for field in ("media_metrics", "detector_metrics", "checks", "readiness"):
        if report.get(field) != evaluation[field]:
            raise ContractError(f"technical QA {field} drifted from its inputs")

    boundary = _object(report.get("authority_boundary"))
    if (
        boundary.get("technical_checks_automatic") is not True
        or boundary.get("creative_approval_claimed") is not False
        or boundary.get("release_eligible") is not False
        or boundary.get("automatic_publish") is not False
        or boundary.get("source_media_modified") is not False
        or boundary.get("second_ffmpeg_orchestrator_created") is not False
    ):
        raise ContractError("technical QA authority boundary is invalid")
    if not media_path.is_file():
        raise ContractError("technical QA media disappeared")


def write_technical_qa_report(report: dict[str, Any], output_path: str | Path) -> Path:
    validate_technical_qa_report(report)
    return atomic_write_json(report, output_path)


def parse_ffmpeg_detector_log(text: str, duration_s: float) -> dict[str, Any]:
    duration = max(0.0, float(duration_s))
    values = {}
    for kind in ("black", "silence", "freeze"):
        seconds = sum(
            float(value)
            for value in re.findall(
                rf"{kind}_duration\s*:?\s*([0-9]+(?:\.[0-9]+)?)",
                text,
                flags=re.IGNORECASE,
            )
        )
        seconds = min(duration, max(0.0, seconds))
        values[f"{kind}_seconds"] = round(seconds, 4)
        values[f"{kind}_ratio"] = round(seconds / duration, 6) if duration > 0 else 0.0
    values["detector_events"] = sum(
        len(re.findall(rf"{kind}_duration", text, flags=re.IGNORECASE))
        for kind in ("black", "silence", "freeze")
    )
    return values


def _evaluate(
    inputs: dict[str, Any],
    expected_duration_s: float | None,
    policy: dict[str, Any],
) -> dict[str, Any]:
    probe_path = Path(str(_object(inputs.get("probe")).get("path"))).resolve()
    detector_path = Path(str(_object(inputs.get("detector_log")).get("path"))).resolve()
    probe = read_json_object(probe_path, "ffprobe JSON")
    streams = probe.get("streams") if isinstance(probe.get("streams"), list) else []
    video = next(
        (
            row
            for row in streams
            if isinstance(row, dict) and row.get("codec_type") == "video"
        ),
        None,
    )
    audio = next(
        (
            row
            for row in streams
            if isinstance(row, dict) and row.get("codec_type") == "audio"
        ),
        None,
    )
    format_payload = _object(probe.get("format"))
    duration = _nonnegative_number(format_payload.get("duration"), "media duration")
    fps = _frame_rate(video)
    media_metrics = {
        "duration_s": round(duration, 4),
        "width": _safe_int(video.get("width")) if video else 0,
        "height": _safe_int(video.get("height")) if video else 0,
        "fps": round(fps, 4),
        "video_codec": str(video.get("codec_name") or "") if video else "",
        "pixel_format": str(video.get("pix_fmt") or "") if video else "",
        "audio_codec": str(audio.get("codec_name") or "") if audio else "",
        "audio_sample_rate": _safe_int(audio.get("sample_rate")) if audio else 0,
        "audio_channels": _safe_int(audio.get("channels")) if audio else 0,
    }
    detector_text = detector_path.read_text(encoding="utf-8", errors="replace")
    detector_metrics = parse_ffmpeg_detector_log(detector_text, duration)
    subtitle_metrics = _subtitle_metrics(
        _optional_path(inputs.get("subtitles")), duration
    )
    packaging_metrics = _packaging_metrics(_optional_path(inputs.get("packaging_plan")))

    checks: list[dict[str, Any]] = []
    _check(checks, "has_video", video is not None, bool(video), True)
    _check(
        checks,
        "has_audio",
        audio is not None or not policy["require_audio"],
        bool(audio),
        policy["require_audio"],
    )
    _check(
        checks,
        "resolution",
        media_metrics["width"] == policy["target_width"]
        and media_metrics["height"] == policy["target_height"],
        f"{media_metrics['width']}x{media_metrics['height']}",
        f"{policy['target_width']}x{policy['target_height']}",
    )
    _check(
        checks,
        "frame_rate",
        abs(fps - policy["target_fps"]) <= policy["fps_tolerance"],
        round(fps, 4),
        {
            "target": policy["target_fps"],
            "tolerance": policy["fps_tolerance"],
        },
    )
    if expected_duration_s is not None:
        _check(
            checks,
            "duration",
            abs(duration - expected_duration_s) <= policy["duration_tolerance_s"],
            round(duration, 4),
            {
                "target": expected_duration_s,
                "tolerance_s": policy["duration_tolerance_s"],
            },
        )
    _check(
        checks,
        "black_ratio",
        detector_metrics["black_ratio"] <= policy["max_black_ratio"],
        detector_metrics["black_ratio"],
        {"maximum": policy["max_black_ratio"]},
    )
    _check(
        checks,
        "silence_ratio",
        detector_metrics["silence_ratio"] <= policy["max_silence_ratio"],
        detector_metrics["silence_ratio"],
        {"maximum": policy["max_silence_ratio"]},
    )
    _warning_check(
        checks,
        "freeze_ratio",
        detector_metrics["freeze_ratio"] <= policy["freeze_warning_ratio"],
        detector_metrics["freeze_ratio"],
        {"warning_above": policy["freeze_warning_ratio"]},
    )
    subtitles_valid = (
        subtitle_metrics["exists"]
        and subtitle_metrics["cue_count"] > 0
        and subtitle_metrics["invalid_cue_count"] == 0
        and subtitle_metrics["overlap_count"] == 0
        and subtitle_metrics["out_of_bounds_count"] == 0
        and subtitle_metrics["longest_line_chars"] <= policy["max_subtitle_line_chars"]
    )
    _check(
        checks,
        "subtitles",
        subtitles_valid or not policy["require_subtitles"],
        subtitle_metrics,
        {
            "required": policy["require_subtitles"],
            "max_line_chars": policy["max_subtitle_line_chars"],
        },
    )
    packaging_valid = (
        packaging_metrics["exists"]
        and packaging_metrics["valid_json"]
        and packaging_metrics["scene_count"] > 0
        and packaging_metrics["invalid_scene_count"] == 0
    )
    _check(
        checks,
        "packaging_plan",
        packaging_valid or not policy["require_packaging_plan"],
        packaging_metrics,
        {"required": policy["require_packaging_plan"]},
    )

    blocking_failures = [
        row["id"] for row in checks if row["blocking"] and row["status"] == "failed"
    ]
    warnings = [row["id"] for row in checks if row["status"] == "warning"]
    technical_ready = not blocking_failures
    return {
        "media_metrics": media_metrics,
        "detector_metrics": detector_metrics,
        "checks": checks,
        "readiness": {
            "status": "passed" if technical_ready else "failed",
            "technical_ready": technical_ready,
            "release_eligible": False,
            "blocking_failure_count": len(blocking_failures),
            "blocking_failures": blocking_failures,
            "warning_count": len(warnings),
            "warnings": warnings,
            "next_action": (
                "human_release_review" if technical_ready else "fix_technical_failures"
            ),
        },
    }


def _validate_receipt_binding(
    receipt_path: Path, media_reference: dict[str, Any]
) -> Path:
    receipt = read_json_object(receipt_path, "technical QA FFmpeg receipt")
    validate_ffmpeg_execution_receipt(receipt)
    if receipt.get("operation") != "technical_qa":
        raise ContractError("technical QA requires operation=technical_qa")
    media_rows = [
        row
        for row in receipt.get("inputs") or []
        if isinstance(row, dict) and row.get("role") == "media"
    ]
    log_rows = [
        row
        for row in receipt.get("outputs") or []
        if isinstance(row, dict) and row.get("role") == "detector_log"
    ]
    if len(media_rows) != 1 or len(log_rows) != 1:
        raise ContractError(
            "technical QA receipt requires one media input and one detector log"
        )
    if media_rows[0].get("artifact") != media_reference:
        raise ContractError("technical QA receipt is bound to different media")
    return validate_artifact_reference(
        _object(log_rows[0].get("artifact")), "FFmpeg detector log"
    )


def _subtitle_metrics(path: Path | None, duration_s: float) -> dict[str, Any]:
    metrics = {
        "exists": bool(path and path.is_file()),
        "cue_count": 0,
        "invalid_cue_count": 0,
        "overlap_count": 0,
        "out_of_bounds_count": 0,
        "longest_line_chars": 0,
    }
    if not path or not path.is_file():
        return metrics
    blocks = re.split(r"\r?\n\s*\r?\n", path.read_text(encoding="utf-8-sig"))
    cues: list[tuple[float, float]] = []
    for block in (row.strip() for row in blocks if row.strip()):
        lines = block.splitlines()
        timeline_index = next(
            (index for index, line in enumerate(lines) if "-->" in line), None
        )
        if timeline_index is None:
            metrics["invalid_cue_count"] += 1
            continue
        match = re.fullmatch(
            r"\s*(\d{2}:\d{2}:\d{2}[,.]\d{3})\s*-->\s*"
            r"(\d{2}:\d{2}:\d{2}[,.]\d{3})\s*",
            lines[timeline_index],
        )
        if not match:
            metrics["invalid_cue_count"] += 1
            continue
        start = _srt_seconds(match.group(1))
        end = _srt_seconds(match.group(2))
        if end <= start:
            metrics["invalid_cue_count"] += 1
            continue
        text_lines = [
            line.strip() for line in lines[timeline_index + 1 :] if line.strip()
        ]
        metrics["longest_line_chars"] = max(
            metrics["longest_line_chars"],
            max((len(line) for line in text_lines), default=0),
        )
        if end > duration_s + 0.1:
            metrics["out_of_bounds_count"] += 1
        cues.append((start, end))
    cues.sort()
    metrics["cue_count"] = len(cues)
    metrics["overlap_count"] = sum(
        1 for previous, current in zip(cues, cues[1:]) if current[0] < previous[1]
    )
    return metrics


def _packaging_metrics(path: Path | None) -> dict[str, Any]:
    metrics = {
        "exists": bool(path and path.is_file()),
        "valid_json": False,
        "scene_count": 0,
        "invalid_scene_count": 0,
    }
    if not path or not path.is_file():
        return metrics
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return metrics
    if not isinstance(payload, dict):
        return metrics
    scenes = payload.get("scenes") if isinstance(payload.get("scenes"), list) else []
    metrics["valid_json"] = True
    metrics["scene_count"] = len(scenes)
    metrics["invalid_scene_count"] = sum(
        1
        for row in scenes
        if not isinstance(row, dict)
        or _safe_float(row.get("end", row.get("end_s")))
        <= _safe_float(row.get("start", row.get("start_s")))
    )
    return metrics


def _normalize_policy(policy: dict[str, Any] | None) -> dict[str, Any]:
    raw = dict(DEFAULT_TECHNICAL_QA_POLICY)
    raw.update(policy or {})
    return {
        "target_width": _positive_int(raw["target_width"], "target width"),
        "target_height": _positive_int(raw["target_height"], "target height"),
        "target_fps": _positive_number(raw["target_fps"], "target fps"),
        "fps_tolerance": _nonnegative_number(raw["fps_tolerance"], "fps tolerance"),
        "duration_tolerance_s": _nonnegative_number(
            raw["duration_tolerance_s"], "duration tolerance"
        ),
        "max_black_ratio": _ratio(raw["max_black_ratio"], "max black ratio"),
        "max_silence_ratio": _ratio(raw["max_silence_ratio"], "max silence ratio"),
        "freeze_warning_ratio": _ratio(
            raw["freeze_warning_ratio"], "freeze warning ratio"
        ),
        "max_subtitle_line_chars": _positive_int(
            raw["max_subtitle_line_chars"], "max subtitle line chars"
        ),
        "require_audio": _boolean(raw["require_audio"], "require audio"),
        "require_subtitles": _boolean(raw["require_subtitles"], "require subtitles"),
        "require_packaging_plan": _boolean(
            raw["require_packaging_plan"], "require packaging plan"
        ),
    }


def _optional_artifact(source: str | Path | None, label: str) -> dict[str, Any]:
    if source is None:
        return {"path": "", "artifact": None}
    path = Path(source).expanduser().resolve()
    return {
        "path": str(path),
        "artifact": artifact_reference(path, label) if path.is_file() else None,
    }


def _validate_optional_artifact(value: Any, label: str) -> None:
    row = _object(value)
    path = Path(str(row.get("path") or "")).expanduser()
    reference = row.get("artifact")
    if reference is None:
        if path.is_absolute() and path.is_file():
            raise ContractError(f"{label} appeared after the report was created")
        return
    actual = validate_artifact_reference(_object(reference), label)
    if not path.is_absolute() or actual != path.resolve():
        raise ContractError(f"{label} path and artifact reference disagree")


def _optional_path(value: Any) -> Path | None:
    raw = str(_object(value).get("path") or "").strip()
    return Path(raw).expanduser().resolve() if raw else None


def _frame_rate(stream: dict[str, Any] | None) -> float:
    if not stream:
        return 0.0
    raw = str(stream.get("avg_frame_rate") or stream.get("r_frame_rate") or "0")
    if "/" in raw:
        numerator, denominator = raw.split("/", 1)
        bottom = _safe_float(denominator)
        return _safe_float(numerator) / bottom if bottom else 0.0
    return _safe_float(raw)


def _srt_seconds(value: str) -> float:
    hours, minutes, rest = value.replace(",", ".").split(":")
    return int(hours) * 3600 + int(minutes) * 60 + float(rest)


def _check(
    rows: list[dict[str, Any]],
    check_id: str,
    passed: bool,
    actual: Any,
    expected: Any,
) -> None:
    rows.append(
        {
            "id": check_id,
            "status": "passed" if passed else "failed",
            "blocking": True,
            "actual": actual,
            "expected": expected,
        }
    )


def _warning_check(
    rows: list[dict[str, Any]],
    check_id: str,
    passed: bool,
    actual: Any,
    expected: Any,
) -> None:
    rows.append(
        {
            "id": check_id,
            "status": "passed" if passed else "warning",
            "blocking": False,
            "actual": actual,
            "expected": expected,
        }
    )


def _object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _boolean(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise ContractError(f"{label} must be a JSON boolean")
    return value


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise ContractError(f"{label} must be a positive integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{label} must be a positive integer") from exc
    if result <= 0 or _safe_float(value) != result:
        raise ContractError(f"{label} must be a positive integer")
    return result


def _positive_number(value: Any, label: str) -> float:
    result = _nonnegative_number(value, label)
    if result <= 0:
        raise ContractError(f"{label} must be positive")
    return result


def _nonnegative_number(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise ContractError(f"{label} must be a non-negative finite number")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{label} must be a non-negative finite number") from exc
    if result < 0 or not math.isfinite(result):
        raise ContractError(f"{label} must be a non-negative finite number")
    return round(result, 6)


def _optional_nonnegative_number(value: Any, label: str) -> float | None:
    if value is None:
        return None
    return _nonnegative_number(value, label)


def _ratio(value: Any, label: str) -> float:
    result = _nonnegative_number(value, label)
    if result > 1:
        raise ContractError(f"{label} must be between 0 and 1")
    return result


def _safe_float(value: Any) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return 0.0
    return result if math.isfinite(result) else 0.0


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _parse_datetime(value: Any, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"{label} must be timezone-aware") from exc
    if parsed.tzinfo is None:
        raise ContractError(f"{label} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
