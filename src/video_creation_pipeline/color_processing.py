from __future__ import annotations

import math
import re
import subprocess
from pathlib import Path
from statistics import fmean, median
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

from .contract_io import (
    artifact_reference,
    atomic_copy_promote,
    atomic_write_json,
    file_sha256,
    payload_sha256,
    read_json_object,
    validate_artifact_reference,
)
from .errors import ContractError
from .execution_receipts import (
    FFMPEG_OUTLET_ID,
    execute_ffmpeg_with_receipt,
    probe_media_metadata,
    resolve_media_tool,
    validate_ffmpeg_execution_receipt,
)

COLOR_ANALYSIS_REPORT_SCHEMA = "video_creation_pipeline.color_analysis_report.v1"
COLOR_PROCESSING_PLAN_SCHEMA = "video_creation_pipeline.color_processing_plan.v1"
COLOR_PROCESSING_RECEIPT_SCHEMA = "video_creation_pipeline.color_processing_receipt.v1"

FFMPEG_REPO_URL = "https://github.com/FFmpeg/FFmpeg.git"
FFMPEG_REVIEWED_SOURCE_COMMIT = "44d082edc87381d978e8588b148116b99fefdb43"
COLOR_MODES = {"cautious_auto", "explicit_basic"}
COLOR_SOURCE_ROLES = {
    "camera_plate",
    "talking_head_plate",
    "interview_plate",
    "synthetic_camera_plate",
}
ANALYSIS_KEYS = (
    "YMIN",
    "YLOW",
    "YAVG",
    "YHIGH",
    "YMAX",
    "UAVG",
    "VAVG",
    "SATAVG",
    "BRNG",
)
REQUIRED_ANALYSIS_KEYS = {"YLOW", "YAVG", "YHIGH", "SATAVG", "BRNG"}


def color_processing_doctor() -> dict[str, Any]:
    checks = {
        "ffmpeg_available": False,
        "ffprobe_available": False,
        "eq_filter_available": False,
        "signalstats_filter_available": False,
        "metadata_filter_available": False,
        "colortemperature_filter_available": False,
    }
    runtime: dict[str, Any] = {
        "ffmpeg_path": "",
        "ffmpeg_sha256": "",
        "version_line": "",
    }
    try:
        ffmpeg = Path(resolve_media_tool("ffmpeg")).resolve()
        checks["ffmpeg_available"] = ffmpeg.is_file()
        runtime["ffmpeg_path"] = str(ffmpeg)
        runtime["ffmpeg_sha256"] = file_sha256(ffmpeg)
        filters = _run_tool([str(ffmpeg), "-hide_banner", "-filters"])
        version = _run_tool([str(ffmpeg), "-version"])
        runtime["version_line"] = version.splitlines()[0].strip() if version else ""
        checks["eq_filter_available"] = _filter_list_contains(filters, "eq")
        checks["signalstats_filter_available"] = _filter_list_contains(
            filters, "signalstats"
        )
        checks["metadata_filter_available"] = _filter_list_contains(filters, "metadata")
        checks["colortemperature_filter_available"] = _filter_list_contains(
            filters, "colortemperature"
        )
    except (ContractError, OSError, subprocess.SubprocessError):
        pass
    try:
        ffprobe = Path(resolve_media_tool("ffprobe")).resolve()
        checks["ffprobe_available"] = ffprobe.is_file()
    except (ContractError, OSError):
        pass
    status = "ready" if all(checks.values()) else "blocked_runtime_unavailable"
    return {
        "schema": "video_creation_pipeline.color_processing_doctor.v1",
        "status": status,
        "runtime": runtime,
        "checks": checks,
        "reviewed_source": {
            "repo_url": FFMPEG_REPO_URL,
            "source_commit": FFMPEG_REVIEWED_SOURCE_COMMIT,
            "filters": ["eq", "signalstats", "metadata", "colortemperature"],
        },
        "supported_modes": sorted(COLOR_MODES),
        "authority_boundary": {
            "candidate_only": True,
            "creative_grade_claimed": False,
            "automatic_white_balance": False,
            "human_visual_review_required": True,
            "automatic_upload": False,
            "automatic_publish": False,
            "single_ffmpeg_outlet": FFMPEG_OUTLET_ID,
        },
    }


def analyze_color_source(
    source_video_path: str | Path,
    work_dir: str | Path,
    report_path: str | Path,
    *,
    sample_fps: float = 2.0,
    timeout_s: float = 300.0,
) -> dict[str, Any]:
    source = Path(source_video_path).expanduser().resolve()
    root = Path(work_dir).expanduser().resolve()
    report_file = Path(report_path).expanduser().resolve()
    rate = _bounded_float(sample_fps, "color analysis sample fps", 0.25, 8.0)
    if not source.is_file():
        raise ContractError(f"color analysis source not found: {source}")
    if source == report_file:
        raise ContractError("color analysis report cannot overwrite its source")
    doctor = color_processing_doctor()
    if doctor["status"] != "ready":
        raise ContractError("color processing runtime is not ready")
    log_path = root / "signalstats.log"
    receipt_path = root / "signalstats-ffmpeg-receipt.json"
    _require_absent(log_path, receipt_path, report_file)
    root.mkdir(parents=True, exist_ok=True)
    report_file.parent.mkdir(parents=True, exist_ok=True)
    command = build_color_analysis_command(source, sample_fps=rate)
    execute_ffmpeg_with_receipt(
        "technical_qa",
        command,
        [{"role": "color_analysis_source", "path": source}],
        [{"role": "color_signalstats_log", "path": log_path}],
        receipt_path,
        capture_log_path=log_path,
        timeout_s=timeout_s,
    )
    metrics = parse_color_signalstats(log_path)
    report: dict[str, Any] = {
        "schema": COLOR_ANALYSIS_REPORT_SCHEMA,
        "source_video": artifact_reference(source, "color analysis source"),
        "source_video_summary": _video_summary(probe_media_metadata(source)),
        "analysis_policy": {
            "sample_fps": rate,
            "sampled_full_frame": True,
            "signalstats_stat": "brng",
            "automatic_face_or_skin_detection": False,
            "automatic_white_balance": False,
        },
        "engine": _engine_snapshot(doctor),
        "ffmpeg_receipt": artifact_reference(
            receipt_path, "color analysis FFmpeg receipt"
        ),
        "signalstats_log": artifact_reference(log_path, "color signalstats log"),
        "metrics": metrics,
        "interpretation_boundary": {
            "technical_measurement_only": True,
            "scene_intent_inferred": False,
            "skin_tone_quality_claimed": False,
            "creative_grade_claimed": False,
        },
    }
    report["report_sha256"] = payload_sha256(report, "report_sha256")
    validate_color_analysis_report(report)
    atomic_write_json(report, report_file)
    return report


def build_color_analysis_command(
    source_video_path: str | Path, *, sample_fps: float
) -> list[str]:
    source = Path(source_video_path).expanduser().resolve()
    rate = _bounded_float(sample_fps, "color analysis sample fps", 0.25, 8.0)
    return [
        "ffmpeg",
        "-hide_banner",
        "-nostats",
        "-v",
        "info",
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-vf",
        f"fps={_format_number(rate)},signalstats=stat=brng,metadata=print:file=-",
        "-an",
        "-f",
        "null",
        "-",
    ]


def parse_color_signalstats(log_path: str | Path) -> dict[str, Any]:
    path = Path(log_path).expanduser().resolve()
    if not path.is_file():
        raise ContractError("color signalstats log is missing")
    text = path.read_text(encoding="utf-8", errors="replace")
    series: dict[str, list[float]] = {}
    for key in ANALYSIS_KEYS:
        values = [
            float(value)
            for value in re.findall(
                rf"lavfi\.signalstats\.{key}=(-?[0-9]+(?:\.[0-9]+)?)", text
            )
        ]
        if values:
            series[key] = values
    missing = sorted(REQUIRED_ANALYSIS_KEYS - set(series))
    if missing:
        raise ContractError(
            "color signalstats log is missing metrics: " + ", ".join(missing)
        )
    frame_count = min(len(values) for values in series.values())
    if frame_count <= 0:
        raise ContractError("color signalstats did not measure any frames")
    trimmed = {key: values[:frame_count] for key, values in series.items()}
    y_ranges = [
        high - low for low, high in zip(trimmed["YLOW"], trimmed["YHIGH"], strict=True)
    ]
    summaries = {key: _series_summary(values) for key, values in trimmed.items()}
    summaries["YRANGE"] = _series_summary(y_ranges)
    return {
        "sampled_frame_count": frame_count,
        "series_summary": summaries,
        "near_black_mean_frame_count": sum(value <= 3.0 for value in trimmed["YAVG"]),
        "near_white_mean_frame_count": sum(value >= 252.0 for value in trimmed["YAVG"]),
        "measurement_scale": {
            "luma_and_chroma_nominal_range": "0..255",
            "saturation_nominal_max": 181.02,
            "broadcast_range_ratio": "0..1",
        },
    }


def validate_color_analysis_report(report: dict[str, Any]) -> None:
    _validate_schema(
        report, "color_analysis_report.v1.schema.json", "color analysis report"
    )
    if report.get("report_sha256") != payload_sha256(report, "report_sha256"):
        raise ContractError("color analysis report integrity check failed")
    _validate_engine_snapshot(_object(report.get("engine")))
    source = validate_artifact_reference(
        _object(report.get("source_video")), "color analysis source"
    )
    receipt_path = validate_artifact_reference(
        _object(report.get("ffmpeg_receipt")), "color analysis FFmpeg receipt"
    )
    receipt = read_json_object(receipt_path, "color analysis FFmpeg receipt")
    validate_ffmpeg_execution_receipt(receipt)
    if receipt.get("operation") != "technical_qa":
        raise ContractError("color analysis used the wrong FFmpeg operation")
    log_path = validate_artifact_reference(
        _object(report.get("signalstats_log")), "color signalstats log"
    )
    policy = _object(report.get("analysis_policy"))
    if receipt.get("command") != build_color_analysis_command(
        source, sample_fps=float(policy["sample_fps"])
    ):
        raise ContractError("color analysis FFmpeg command drifted")
    if report.get("metrics") != parse_color_signalstats(log_path):
        raise ContractError("color analysis metrics drifted from the source log")
    if report.get("source_video_summary") != _video_summary(
        probe_media_metadata(source)
    ):
        raise ContractError("color analysis source metadata drifted")


def build_color_processing_plan(
    source_video_path: str | Path,
    output_video_path: str | Path,
    analysis_report_path: str | Path,
    *,
    source_role: str,
    mode: str,
    brightness: float = 0.0,
    contrast: float = 1.0,
    gamma: float = 1.0,
    saturation: float = 1.0,
    temperature_kelvin: int = 6500,
    temperature_mix: float = 0.0,
    preserve_lightness: float = 1.0,
) -> dict[str, Any]:
    source = Path(source_video_path).expanduser().resolve()
    output = Path(output_video_path).expanduser().resolve()
    analysis_file = Path(analysis_report_path).expanduser().resolve()
    normalized_mode = _mode(mode)
    normalized_source_role = _source_role(source_role)
    if output.suffix.casefold() != ".mp4":
        raise ContractError("color processing v1 output must use .mp4")
    if source == output:
        raise ContractError("color processing cannot overwrite its source")
    report = read_json_object(analysis_file, "color analysis report")
    validate_color_analysis_report(report)
    report_source = validate_artifact_reference(
        _object(report.get("source_video")), "color analysis source"
    )
    if report_source != source:
        raise ContractError("color analysis report belongs to a different source")
    source_metadata = probe_media_metadata(source)
    from .production_presets import assert_sdr_processing_safe
    assert_sdr_processing_safe(source_metadata)
    source_summary = _video_summary(source_metadata)
    if source_summary["has_audio"] and source_summary["audio_codec"] != "aac":
        raise ContractError(
            "color processing v1 requires AAC or no audio for MP4 stream copy"
        )
    if normalized_mode == "cautious_auto":
        adjustments = _derive_cautious_adjustments(_object(report.get("metrics")))
    else:
        adjustments = _explicit_adjustments(
            brightness=brightness,
            contrast=contrast,
            gamma=gamma,
            saturation=saturation,
            temperature_kelvin=temperature_kelvin,
            temperature_mix=temperature_mix,
            preserve_lightness=preserve_lightness,
        )
    doctor = color_processing_doctor()
    if doctor["status"] != "ready":
        raise ContractError("color processing runtime is not ready")
    plan: dict[str, Any] = {
        "schema": COLOR_PROCESSING_PLAN_SCHEMA,
        "source_video": artifact_reference(source, "color processing source"),
        "source_video_summary": source_summary,
        "source_role": normalized_source_role,
        "analysis_report": artifact_reference(analysis_file, "color analysis report"),
        "analysis_report_sha256": report["report_sha256"],
        "output": {
            "path": str(output),
            "container": "mp4",
            "video_codec": "h264",
            "pixel_format": "yuv420p",
            "audio_policy": "stream_copy",
        },
        "mode": normalized_mode,
        "adjustments": adjustments,
        "filter_graph": _filter_graph(adjustments),
        "engine": _engine_snapshot(doctor),
        "execution_policy": {
            "render_outlet": FFMPEG_OUTLET_ID,
            "staging_then_atomic_promotion": True,
            "audio_packet_hash_must_match": source_summary["has_audio"],
            "automatic_fallback": False,
        },
        "review": _review_boundary(),
        "authority_boundary": _plan_boundary(),
    }
    plan["plan_sha256"] = payload_sha256(plan, "plan_sha256")
    validate_color_processing_plan(plan)
    return plan


def validate_color_processing_plan(plan: dict[str, Any]) -> None:
    _validate_schema(plan, "color_processing_plan.v1.schema.json", "color plan")
    if plan.get("plan_sha256") != payload_sha256(plan, "plan_sha256"):
        raise ContractError("color processing plan integrity check failed")
    source = validate_artifact_reference(
        _object(plan.get("source_video")), "color processing source"
    )
    from .production_presets import assert_sdr_processing_safe
    assert_sdr_processing_safe(probe_media_metadata(source))
    report_path = validate_artifact_reference(
        _object(plan.get("analysis_report")), "color analysis report"
    )
    report = read_json_object(report_path, "color analysis report")
    validate_color_analysis_report(report)
    if plan.get("analysis_report_sha256") != report.get("report_sha256") or _object(
        report.get("source_video")
    ).get("sha256") != _object(plan.get("source_video")).get("sha256"):
        raise ContractError("color processing plan binds a stale analysis report")
    _validate_engine_snapshot(_object(plan.get("engine")))
    if plan.get("engine") != report.get("engine"):
        raise ContractError("color processing engine drifted from source analysis")
    if plan.get("source_video_summary") != _video_summary(probe_media_metadata(source)):
        raise ContractError("color processing source metadata drifted")
    mode = _mode(plan.get("mode"))
    _source_role(plan.get("source_role"))
    adjustments = _object(plan.get("adjustments"))
    _validate_adjustments(adjustments, mode=mode)
    if plan.get("filter_graph") != _filter_graph(adjustments):
        raise ContractError("color processing filter graph drifted")
    output = _object(plan.get("output"))
    output_path = Path(str(output.get("path") or "")).expanduser().resolve()
    if output_path == source or output_path.suffix.casefold() != ".mp4":
        raise ContractError("color processing output path is invalid")
    if plan.get("execution_policy") != {
        "render_outlet": FFMPEG_OUTLET_ID,
        "staging_then_atomic_promotion": True,
        "audio_packet_hash_must_match": bool(plan["source_video_summary"]["has_audio"]),
        "automatic_fallback": False,
    }:
        raise ContractError("color processing execution policy drifted")
    if plan.get("review") != _review_boundary():
        raise ContractError("color processing review boundary drifted")
    if plan.get("authority_boundary") != _plan_boundary():
        raise ContractError("color processing plan authority boundary drifted")


def build_color_render_command(
    plan: dict[str, Any], source_path: str | Path, output_path: str | Path
) -> list[str]:
    validate_color_processing_plan(plan)
    source = Path(source_path).expanduser().resolve()
    output = Path(output_path).expanduser().resolve()
    command = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-nostats",
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-vf",
        str(plan["filter_graph"]),
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "18",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "copy",
        "-map_metadata",
        "-1",
        "-fflags",
        "+bitexact",
        "-flags:v",
        "+bitexact",
        "-threads",
        "1",
    ]
    _append_color_metadata(command, _object(plan.get("source_video_summary")))
    command.extend(["-movflags", "+faststart", str(output)])
    return command


def build_audio_packet_hash_command(source_path: str | Path) -> list[str]:
    source = Path(source_path).expanduser().resolve()
    return [
        "ffmpeg",
        "-hide_banner",
        "-nostats",
        "-v",
        "error",
        "-i",
        str(source),
        "-map",
        "0:a:0",
        "-vn",
        "-c",
        "copy",
        "-f",
        "hash",
        "-hash",
        "sha256",
        "-",
    ]


def execute_color_processing(
    plan_path: str | Path,
    work_dir: str | Path,
    receipt_path: str | Path,
    *,
    timeout_s: float = 900.0,
) -> dict[str, Any]:
    plan_file = Path(plan_path).expanduser().resolve()
    plan = read_json_object(plan_file, "color processing plan")
    validate_color_processing_plan(plan)
    source = validate_artifact_reference(
        _object(plan.get("source_video")), "color processing source"
    )
    root = Path(work_dir).expanduser().resolve()
    final_receipt = Path(receipt_path).expanduser().resolve()
    output = Path(str(_object(plan.get("output"))["path"])).resolve()
    staged = root / "color-staging.mp4"
    render_receipt = root / "color-render-ffmpeg-receipt.json"
    post_report_path = root / "post-analysis-report.json"
    source_audio_log = root / "source-audio-packet-hash.log"
    source_audio_receipt = root / "source-audio-packet-hash-receipt.json"
    output_audio_log = root / "output-audio-packet-hash.log"
    output_audio_receipt = root / "output-audio-packet-hash-receipt.json"
    protected = [
        staged,
        render_receipt,
        post_report_path,
        source_audio_log,
        source_audio_receipt,
        output_audio_log,
        output_audio_receipt,
        final_receipt,
        output,
    ]
    if len({path for path in protected}) != len(protected):
        raise ContractError("color processing output paths must be distinct")
    if source in protected or plan_file in protected:
        raise ContractError("color processing cannot overwrite a bound input")
    _require_absent(*protected)
    root.mkdir(parents=True, exist_ok=True)
    output.parent.mkdir(parents=True, exist_ok=True)

    source_audio_qa: dict[str, Any] | None = None
    if bool(plan["source_video_summary"]["has_audio"]):
        source_audio_qa = _execute_audio_hash(
            source,
            source_audio_log,
            source_audio_receipt,
            role="source_color_audio",
            timeout_s=timeout_s,
        )
    render_command = build_color_render_command(plan, source, staged)
    execute_ffmpeg_with_receipt(
        "render",
        render_command,
        [
            {"role": "color_processing_source", "path": source},
            {"role": "color_processing_plan", "path": plan_file},
        ],
        [{"role": "color_processed_staging", "path": staged}],
        render_receipt,
        timeout_s=timeout_s,
    )
    post_report = analyze_color_source(
        staged,
        root / "post-analysis",
        post_report_path,
        sample_fps=float(
            _object(
                read_json_object(
                    validate_artifact_reference(
                        _object(plan["analysis_report"]), "color analysis report"
                    ),
                    "color analysis report",
                )["analysis_policy"]
            )["sample_fps"]
        ),
        timeout_s=timeout_s,
    )
    output_audio_qa: dict[str, Any] | None = None
    if bool(plan["source_video_summary"]["has_audio"]):
        output_audio_qa = _execute_audio_hash(
            staged,
            output_audio_log,
            output_audio_receipt,
            role="processed_color_audio",
            timeout_s=timeout_s,
        )
    qa = _technical_qa(
        plan,
        source,
        staged,
        post_report,
        source_audio_qa,
        output_audio_qa,
    )
    if not qa["technically_valid"]:
        raise ContractError("color processed candidate failed technical QA")
    atomic_copy_promote(staged, output, label="color processed video")
    receipt = build_color_processing_receipt(
        plan_file,
        render_receipt,
        post_report_path,
        staged,
        output,
        source_audio_log if source_audio_qa else None,
        source_audio_receipt if source_audio_qa else None,
        output_audio_log if output_audio_qa else None,
        output_audio_receipt if output_audio_qa else None,
    )
    atomic_write_json(receipt, final_receipt)
    return receipt


def build_color_processing_receipt(
    plan_path: str | Path,
    render_receipt_path: str | Path,
    post_analysis_report_path: str | Path,
    staged_video_path: str | Path,
    output_video_path: str | Path,
    source_audio_log_path: str | Path | None,
    source_audio_receipt_path: str | Path | None,
    output_audio_log_path: str | Path | None,
    output_audio_receipt_path: str | Path | None,
) -> dict[str, Any]:
    plan_file = Path(plan_path).expanduser().resolve()
    plan = read_json_object(plan_file, "color processing plan")
    validate_color_processing_plan(plan)
    source = validate_artifact_reference(
        _object(plan.get("source_video")), "color processing source"
    )
    post_report_file = Path(post_analysis_report_path).expanduser().resolve()
    post_report = read_json_object(post_report_file, "post color analysis report")
    validate_color_analysis_report(post_report)
    staged = Path(staged_video_path).expanduser().resolve()
    output = Path(output_video_path).expanduser().resolve()
    source_audio_qa = _audio_qa_from_paths(
        source_audio_log_path, source_audio_receipt_path, "source_color_audio"
    )
    output_audio_qa = _audio_qa_from_paths(
        output_audio_log_path, output_audio_receipt_path, "processed_color_audio"
    )
    receipt: dict[str, Any] = {
        "schema": COLOR_PROCESSING_RECEIPT_SCHEMA,
        "status": "completed_technical_candidate_needs_visual_review",
        "source_plan": artifact_reference(plan_file, "color processing plan"),
        "source_plan_sha256": plan["plan_sha256"],
        "render_ffmpeg_receipt": artifact_reference(
            render_receipt_path, "color render FFmpeg receipt"
        ),
        "post_analysis_report": artifact_reference(
            post_report_file, "post color analysis report"
        ),
        "staged_video": artifact_reference(staged, "color staged video"),
        "processed_video": artifact_reference(output, "color processed video"),
        "audio_packet_qa": {
            "source": source_audio_qa,
            "processed": output_audio_qa,
        },
        "technical_qa": _technical_qa(
            plan,
            source,
            output,
            post_report,
            source_audio_qa,
            output_audio_qa,
        ),
        "review": _review_boundary(),
        "authority_boundary": _receipt_boundary(),
    }
    receipt["receipt_sha256"] = payload_sha256(receipt, "receipt_sha256")
    validate_color_processing_receipt(receipt)
    return receipt


def validate_color_processing_receipt(receipt: dict[str, Any]) -> None:
    _validate_schema(
        receipt, "color_processing_receipt.v1.schema.json", "color receipt"
    )
    if receipt.get("receipt_sha256") != payload_sha256(receipt, "receipt_sha256"):
        raise ContractError("color processing receipt integrity check failed")
    plan_path = validate_artifact_reference(
        _object(receipt.get("source_plan")), "color processing plan"
    )
    plan = read_json_object(plan_path, "color processing plan")
    validate_color_processing_plan(plan)
    if receipt.get("source_plan_sha256") != plan.get("plan_sha256"):
        raise ContractError("color processing receipt binds a stale plan")
    source = validate_artifact_reference(
        _object(plan.get("source_video")), "color processing source"
    )
    render_path = validate_artifact_reference(
        _object(receipt.get("render_ffmpeg_receipt")), "color render receipt"
    )
    render = read_json_object(render_path, "color render receipt")
    validate_ffmpeg_execution_receipt(render)
    if render.get("operation") != "render":
        raise ContractError("color processing used the wrong render operation")
    post_path = validate_artifact_reference(
        _object(receipt.get("post_analysis_report")), "post color analysis report"
    )
    post_report = read_json_object(post_path, "post color analysis report")
    validate_color_analysis_report(post_report)
    staged = validate_artifact_reference(
        _object(receipt.get("staged_video")), "color staged video"
    )
    output = validate_artifact_reference(
        _object(receipt.get("processed_video")), "color processed video"
    )
    if (
        output != Path(str(plan["output"]["path"])).resolve()
        or _object(receipt["staged_video"])["sha256"]
        != _object(receipt["processed_video"])["sha256"]
    ):
        raise ContractError("color output differs from its staged candidate")
    if render.get("command") != build_color_render_command(plan, source, staged):
        raise ContractError("color render FFmpeg command drifted")
    audio = _object(receipt.get("audio_packet_qa"))
    source_audio = _validate_audio_qa_entry(audio.get("source"), "source_color_audio")
    output_audio = _validate_audio_qa_entry(
        audio.get("processed"), "processed_color_audio"
    )
    expected_qa = _technical_qa(
        plan,
        source,
        output,
        post_report,
        source_audio,
        output_audio,
    )
    if receipt.get("technical_qa") != expected_qa:
        raise ContractError("color processing technical QA drifted")
    if receipt.get("review") != _review_boundary():
        raise ContractError("color processing receipt review boundary drifted")
    if receipt.get("authority_boundary") != _receipt_boundary():
        raise ContractError("color processing receipt authority boundary drifted")


def write_color_processing_contract(
    payload: dict[str, Any], output_path: str | Path
) -> Path:
    schema = payload.get("schema")
    if schema == COLOR_ANALYSIS_REPORT_SCHEMA:
        validate_color_analysis_report(payload)
    elif schema == COLOR_PROCESSING_PLAN_SCHEMA:
        validate_color_processing_plan(payload)
    elif schema == COLOR_PROCESSING_RECEIPT_SCHEMA:
        validate_color_processing_receipt(payload)
    else:
        raise ContractError("unsupported color processing contract")
    return atomic_write_json(payload, output_path)


def _execute_audio_hash(
    media: Path,
    log_path: Path,
    receipt_path: Path,
    *,
    role: str,
    timeout_s: float,
) -> dict[str, Any]:
    command = build_audio_packet_hash_command(media)
    execute_ffmpeg_with_receipt(
        "technical_qa",
        command,
        [{"role": role, "path": media}],
        [{"role": f"{role}_packet_hash_log", "path": log_path}],
        receipt_path,
        capture_log_path=log_path,
        timeout_s=timeout_s,
    )
    return {
        "packet_sha256": _parse_audio_packet_hash(log_path),
        "log": artifact_reference(log_path, f"{role} packet hash log"),
        "ffmpeg_receipt": artifact_reference(
            receipt_path, f"{role} packet hash receipt"
        ),
    }


def _audio_qa_from_paths(
    log_path: str | Path | None,
    receipt_path: str | Path | None,
    role: str,
) -> dict[str, Any] | None:
    if log_path is None and receipt_path is None:
        return None
    if log_path is None or receipt_path is None:
        raise ContractError("color audio QA paths must be supplied together")
    log = Path(log_path).expanduser().resolve()
    receipt = Path(receipt_path).expanduser().resolve()
    return {
        "packet_sha256": _parse_audio_packet_hash(log),
        "log": artifact_reference(log, f"{role} packet hash log"),
        "ffmpeg_receipt": artifact_reference(receipt, f"{role} packet hash receipt"),
    }


def _validate_audio_qa_entry(value: Any, role: str) -> dict[str, Any] | None:
    if value is None:
        return None
    row = _object(value)
    log_path = validate_artifact_reference(
        _object(row.get("log")), f"{role} packet hash log"
    )
    receipt_path = validate_artifact_reference(
        _object(row.get("ffmpeg_receipt")), f"{role} packet hash receipt"
    )
    receipt = read_json_object(receipt_path, f"{role} packet hash receipt")
    validate_ffmpeg_execution_receipt(receipt)
    if receipt.get("operation") != "technical_qa":
        raise ContractError("color audio hash used the wrong FFmpeg operation")
    media_path = validate_artifact_reference(
        _object(receipt["inputs"][0]["artifact"]), role
    )
    if receipt.get("command") != build_audio_packet_hash_command(media_path):
        raise ContractError("color audio hash FFmpeg command drifted")
    if row.get("packet_sha256") != _parse_audio_packet_hash(log_path):
        raise ContractError("color audio packet hash drifted")
    return row


def _parse_audio_packet_hash(log_path: Path) -> str:
    text = log_path.read_text(encoding="utf-8", errors="replace")
    match = re.search(r"SHA256=([0-9a-fA-F]{64})", text)
    if match is None:
        raise ContractError("audio packet hash log is invalid")
    return match.group(1).lower()


def _technical_qa(
    plan: dict[str, Any],
    source_path: Path,
    output_path: Path,
    post_report: dict[str, Any],
    source_audio_qa: dict[str, Any] | None,
    output_audio_qa: dict[str, Any] | None,
) -> dict[str, Any]:
    source = _video_summary(probe_media_metadata(source_path))
    output = _video_summary(probe_media_metadata(output_path))
    duration_delta = round(abs(output["duration_s"] - source["duration_s"]), 6)
    duration_tolerance = round(max(0.2, 1.0 / source["fps"] + 0.05), 6)
    fps_match = abs(output["fps"] - source["fps"]) <= 0.1
    dimensions_match = (
        output["width"] == source["width"] and output["height"] == source["height"]
    )
    audio_presence_match = output["has_audio"] == source["has_audio"]
    if source["has_audio"]:
        audio_hash_match = bool(
            source_audio_qa
            and output_audio_qa
            and source_audio_qa["packet_sha256"] == output_audio_qa["packet_sha256"]
        )
    else:
        audio_hash_match = source_audio_qa is None and output_audio_qa is None
    metrics = _object(post_report.get("metrics"))
    frames = int(metrics.get("sampled_frame_count") or 0)
    yavg = float(_object(metrics.get("series_summary"))["YAVG"]["median"])
    noncollapsed = frames > 0 and 3.0 < yavg < 252.0
    technically_valid = all(
        (
            dimensions_match,
            fps_match,
            duration_delta <= duration_tolerance,
            audio_presence_match,
            audio_hash_match,
            output["video_codec"] == "h264",
            output["pixel_format"] == "yuv420p",
            noncollapsed,
        )
    )
    return {
        "source_video": source,
        "processed_video": output,
        "dimensions_match": dimensions_match,
        "frame_rate_matches": fps_match,
        "duration_delta_s": duration_delta,
        "duration_tolerance_s": duration_tolerance,
        "duration_within_tolerance": duration_delta <= duration_tolerance,
        "audio_presence_preserved": audio_presence_match,
        "audio_packet_sha256_matches": audio_hash_match,
        "sampled_frame_count": frames,
        "median_yavg": yavg,
        "mean_luminance_not_collapsed": noncollapsed,
        "expected_codec_and_pixel_format": output["video_codec"] == "h264"
        and output["pixel_format"] == "yuv420p",
        "skin_tone_quality_claimed": False,
        "creative_grade_quality_claimed": False,
        "technically_valid": technically_valid,
    }


def _derive_cautious_adjustments(metrics: dict[str, Any]) -> dict[str, Any]:
    summary = _object(metrics.get("series_summary"))
    yavg = float(_object(summary.get("YAVG"))["median"])
    yrange = float(_object(summary.get("YRANGE"))["median"])
    satavg = float(_object(summary.get("SATAVG"))["median"])
    if yavg < 75.0:
        brightness = min((95.0 - yavg) / 255.0, 0.06)
    elif yavg > 180.0:
        brightness = max((160.0 - yavg) / 255.0, -0.06)
    else:
        brightness = 0.0
    if abs(brightness) < 0.01:
        brightness = 0.0
    contrast = 1.05 if yrange < 70.0 else 0.97 if yrange > 200.0 else 1.0
    saturation = 1.04 if satavg < 18.0 else 0.96 if satavg > 80.0 else 1.0
    return {
        "policy": "cautious_signalstats_v1",
        "eq": {
            "brightness": round(brightness, 6),
            "contrast": contrast,
            "gamma": 1.0,
            "saturation": saturation,
        },
        "temperature": {
            "enabled": False,
            "kelvin": 6500,
            "mix": 0.0,
            "preserve_lightness": 1.0,
        },
        "decision_basis": {
            "median_yavg": yavg,
            "median_yrange": yrange,
            "median_satavg": satavg,
            "global_statistics_are_not_white_balance_evidence": True,
        },
    }


def _explicit_adjustments(
    *,
    brightness: float,
    contrast: float,
    gamma: float,
    saturation: float,
    temperature_kelvin: int,
    temperature_mix: float,
    preserve_lightness: float,
) -> dict[str, Any]:
    return {
        "policy": "explicit_basic_v1",
        "eq": {
            "brightness": _bounded_float(brightness, "brightness", -0.15, 0.15),
            "contrast": _bounded_float(contrast, "contrast", 0.8, 1.25),
            "gamma": _bounded_float(gamma, "gamma", 0.8, 1.25),
            "saturation": _bounded_float(saturation, "saturation", 0.7, 1.4),
        },
        "temperature": {
            "enabled": float(temperature_mix) > 0,
            "kelvin": _bounded_int(
                temperature_kelvin, "temperature kelvin", 1000, 40000
            ),
            "mix": _bounded_float(temperature_mix, "temperature mix", 0.0, 0.5),
            "preserve_lightness": _bounded_float(
                preserve_lightness, "preserve lightness", 0.0, 1.0
            ),
        },
        "decision_basis": {
            "operator_supplied": True,
            "global_statistics_are_not_white_balance_evidence": True,
        },
    }


def _validate_adjustments(adjustments: dict[str, Any], *, mode: str) -> None:
    eq = _object(adjustments.get("eq"))
    _bounded_float(eq.get("brightness"), "brightness", -0.15, 0.15)
    _bounded_float(eq.get("contrast"), "contrast", 0.8, 1.25)
    _bounded_float(eq.get("gamma"), "gamma", 0.8, 1.25)
    _bounded_float(eq.get("saturation"), "saturation", 0.7, 1.4)
    temperature = _object(adjustments.get("temperature"))
    _bounded_int(temperature.get("kelvin"), "temperature kelvin", 1000, 40000)
    mix = _bounded_float(temperature.get("mix"), "temperature mix", 0.0, 0.5)
    _bounded_float(
        temperature.get("preserve_lightness"), "preserve lightness", 0.0, 1.0
    )
    if temperature.get("enabled") is not (mix > 0):
        raise ContractError("color temperature enabled flag drifted")
    expected_policy = (
        "cautious_signalstats_v1" if mode == "cautious_auto" else "explicit_basic_v1"
    )
    if adjustments.get("policy") != expected_policy:
        raise ContractError("color adjustment policy drifted")
    basis = _object(adjustments.get("decision_basis"))
    if basis.get("global_statistics_are_not_white_balance_evidence") is not True:
        raise ContractError("color adjustment evidence boundary drifted")
    if mode == "cautious_auto" and (
        temperature.get("enabled") is not False
        or float(eq["brightness"]) < -0.06
        or float(eq["brightness"]) > 0.06
        or float(eq["contrast"]) < 0.97
        or float(eq["contrast"]) > 1.05
        or float(eq["saturation"]) < 0.96
        or float(eq["saturation"]) > 1.04
    ):
        raise ContractError("cautious color policy exceeded its safety bounds")


def _filter_graph(adjustments: dict[str, Any]) -> str:
    eq = _object(adjustments.get("eq"))
    filters = [
        "eq="
        + ":".join(
            (
                f"brightness={_format_number(eq['brightness'])}",
                f"contrast={_format_number(eq['contrast'])}",
                f"gamma={_format_number(eq['gamma'])}",
                f"saturation={_format_number(eq['saturation'])}",
                "gamma_weight=0.85",
                "eval=init",
            )
        )
    ]
    temperature = _object(adjustments.get("temperature"))
    if temperature.get("enabled") is True:
        filters.append(
            "colortemperature="
            + ":".join(
                (
                    f"temperature={int(temperature['kelvin'])}",
                    f"mix={_format_number(temperature['mix'])}",
                    f"pl={_format_number(temperature['preserve_lightness'])}",
                )
            )
        )
    filters.append("format=yuv420p")
    return ",".join(filters)


def _append_color_metadata(command: list[str], summary: dict[str, Any]) -> None:
    mappings = (
        ("color_range", "-color_range"),
        ("color_space", "-colorspace"),
        ("color_transfer", "-color_trc"),
        ("color_primaries", "-color_primaries"),
    )
    for field, option in mappings:
        value = str(summary.get(field) or "").strip()
        if value and value not in {"unknown", "unspecified", "reserved"}:
            command.extend([option, value])


def _video_summary(metadata: dict[str, Any]) -> dict[str, Any]:
    streams = [row for row in metadata.get("streams") or [] if isinstance(row, dict)]
    video = next((row for row in streams if row.get("codec_type") == "video"), None)
    if video is None:
        raise ContractError("color processing source has no video stream")
    audio = next((row for row in streams if row.get("codec_type") == "audio"), None)
    format_payload = _object(metadata.get("format"))
    duration = _positive_number(
        video.get("duration") or format_payload.get("duration"), "video duration"
    )
    return {
        "duration_s": round(duration, 6),
        "width": int(_positive_number(video.get("width"), "video width")),
        "height": int(_positive_number(video.get("height"), "video height")),
        "fps": round(_frame_rate(video), 6),
        "video_codec": str(video.get("codec_name") or ""),
        "pixel_format": str(video.get("pix_fmt") or ""),
        "color_range": str(video.get("color_range") or ""),
        "color_space": str(video.get("color_space") or ""),
        "color_transfer": str(video.get("color_transfer") or ""),
        "color_primaries": str(video.get("color_primaries") or ""),
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
            divisor = float(denominator)
            if divisor:
                result = float(numerator) / divisor
                if math.isfinite(result) and result > 0:
                    return result
    raise ContractError("color processing video frame rate is invalid")


def _engine_snapshot(doctor: dict[str, Any]) -> dict[str, Any]:
    runtime = _object(doctor.get("runtime"))
    reviewed = _object(doctor.get("reviewed_source"))
    return {
        "name": "FFmpeg",
        "runtime_path": str(runtime.get("ffmpeg_path") or ""),
        "runtime_sha256": str(runtime.get("ffmpeg_sha256") or ""),
        "runtime_version": str(runtime.get("version_line") or ""),
        "reviewed_source_repo": str(reviewed.get("repo_url") or ""),
        "reviewed_source_commit": str(reviewed.get("source_commit") or ""),
        "filters": list(reviewed.get("filters") or []),
        "outlet_id": FFMPEG_OUTLET_ID,
    }


def _validate_engine_snapshot(engine: dict[str, Any]) -> None:
    runtime = Path(str(engine.get("runtime_path") or "")).expanduser().resolve()
    current = Path(resolve_media_tool("ffmpeg")).expanduser().resolve()
    expected = {
        "name": "FFmpeg",
        "runtime_path": str(current),
        "runtime_sha256": file_sha256(current),
        "reviewed_source_repo": FFMPEG_REPO_URL,
        "reviewed_source_commit": FFMPEG_REVIEWED_SOURCE_COMMIT,
        "filters": ["eq", "signalstats", "metadata", "colortemperature"],
        "outlet_id": FFMPEG_OUTLET_ID,
    }
    if runtime != current or any(
        engine.get(field) != value for field, value in expected.items()
    ):
        raise ContractError("color processing FFmpeg runtime provenance drifted")
    if not str(engine.get("runtime_version") or "").startswith("ffmpeg version "):
        raise ContractError("color processing FFmpeg runtime version is invalid")


def _review_boundary() -> dict[str, Any]:
    return {
        "status": "needs_visual_review",
        "required_checks": [
            "skin_tone_and_face_consistency",
            "highlight_and_shadow_detail",
            "shot_to_shot_consistency",
            "color_cast_and_white_balance",
            "banding_flicker_and_compression",
        ],
        "technical_qa_is_creative_approval": False,
        "automatic_release": False,
    }


def _plan_boundary() -> dict[str, Any]:
    return {
        "candidate_only": True,
        "source_media_modified": False,
        "creative_grade_claimed": False,
        "skin_tone_quality_claimed": False,
        "automatic_white_balance": False,
        "flattened_graphics_input_allowed": False,
        "human_visual_review_required": True,
        "automatic_upload": False,
        "automatic_publish": False,
        "single_ffmpeg_outlet": FFMPEG_OUTLET_ID,
        "second_video_encoder_added": False,
        "second_state_machine_added": False,
    }


def _receipt_boundary() -> dict[str, Any]:
    return {
        **_plan_boundary(),
        "technical_processing_completed": True,
        "release_eligible": False,
    }


def _series_summary(values: list[float]) -> dict[str, Any]:
    ordered = sorted(values)
    return {
        "count": len(values),
        "minimum": round(ordered[0], 6),
        "median": round(float(median(ordered)), 6),
        "mean": round(float(fmean(ordered)), 6),
        "maximum": round(ordered[-1], 6),
    }


def _run_tool(command: list[str]) -> str:
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=30.0,
        check=False,
    )
    if result.returncode != 0:
        raise ContractError("color processing runtime probe failed")
    return (result.stdout or "") + (result.stderr or "")


def _filter_list_contains(text: str, name: str) -> bool:
    return bool(re.search(rf"(?m)^\s*[TSC\.]{{2,4}}\s+{re.escape(name)}\s", text))


def _require_absent(*paths: Path) -> None:
    if any(path.exists() for path in paths):
        raise ContractError("color processing output already exists")


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
    if mode not in COLOR_MODES:
        raise ContractError("unsupported color processing mode")
    return mode


def _source_role(value: Any) -> str:
    role = str(value or "").strip()
    if role not in COLOR_SOURCE_ROLES:
        raise ContractError(
            "color processing source must be an unflattened camera, talking-head, or interview plate"
        )
    return role


def _object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError("expected an object")
    return value


def _bounded_float(value: Any, label: str, minimum: float, maximum: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{label} must be numeric") from exc
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
    if number < minimum or number > maximum or float(value) != number:
        raise ContractError(f"{label} must be between {minimum} and {maximum}")
    return number


def _positive_number(value: Any, label: str) -> float:
    number = _bounded_float(value, label, 0.0, float("inf"))
    if number <= 0:
        raise ContractError(f"{label} must be positive")
    return number


def _format_number(value: Any) -> str:
    return f"{float(value):.6f}".rstrip("0").rstrip(".")
