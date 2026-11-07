"""Source-first production presets; inspection never renders a media derivative."""

from __future__ import annotations

import copy
import json
from fractions import Fraction
from pathlib import Path

from .contract_io import (
    artifact_reference as ref,
    atomic_write_json as write,
    payload_sha256,
    read_json_object as read,
    validate_artifact_reference as bound,
)
from .editing_presets import interval, number, reviewed
from .errors import ContractError
from .execution_receipts import probe_media_metadata, probe_packet_timestamps

POLICY = json.loads(
    (Path(__file__).parent / "policies/production-presets.v1.json").read_text("utf-8")
)
AUDIT_SCHEMA = "video_creation_pipeline.source_production_audit.v1"
CHECKS = {
    "technical": "核对运动是否卡顿、旋转方向和原声同步；不凭平均帧率差直接判断可变帧率",
    "color": "核对源色彩解释、曝光、白平衡、肤色、白色物体、高光及相邻镜头一致性",
    "composition": "核对眼睛、头顶、手部、操作区域、关键物件和主体稳定性",
    "restoration": "判断是否确需防抖或增强；原片已足够时保持原片",
    "split_edit": "判断声画错开是否有叙事价值、是否保留原对话时序和真实回应",
    "audio": "核对左右声道、单声道兼容、现场底声与切口连续性",
    "delivery": "按目标画幅、正常速度、手机大小和静音分别核对实际成片",
}


def profile(name="spoken"):
    if name not in POLICY["profiles"]:
        raise ContractError("unknown production project type")
    return copy.deepcopy(POLICY["profiles"][name])


def seal(value):
    value["sha256"] = payload_sha256(value, "sha256")
    return value


def checked_payload(path, label):
    value = read(path, label)
    if value.get("sha256") != payload_sha256(value, "sha256"):
        raise ContractError(label + " integrity mismatch")
    return value


def rate(value):
    try:
        parsed = float(Fraction(str(value)))
        return parsed if 0 < parsed < 1000 else None
    except (ValueError, ZeroDivisionError, TypeError, OverflowError):
        return None


def media_facts(metadata):
    videos = [s for s in metadata.get("streams", []) if s.get("codec_type") == "video"]
    if not videos:
        raise ContractError("source needs a video stream")
    v = videos[0]
    duration = number(
        metadata.get("format", {}).get("duration") or v.get("duration"), "duration"
    )
    if duration <= 0:
        raise ContractError("source duration must be positive")
    avg, nominal = rate(v.get("avg_frame_rate")), rate(v.get("r_frame_rate"))
    rotation = next(
        (s["rotation"] for s in v.get("side_data_list", []) if "rotation" in s),
        v.get("tags", {}).get("rotate", 0),
    )
    return {
        "duration_s": duration,
        "width": int(v.get("width") or 0),
        "height": int(v.get("height") or 0),
        "avg_fps": avg,
        "nominal_fps": nominal,
        "frame_cadence": "needs_timestamp_review"
        if avg and nominal and abs(avg - nominal) > 0.01
        else "not_measured",
        "vfr_confirmed": False,
        "rotation_degrees": number(rotation, "rotation"),
        "pixel_format": v.get("pix_fmt"),
        "color": {
            k: v.get(k)
            for k in ("color_primaries", "color_transfer", "color_space", "color_range")
        },
        "audio_streams": [
            {
                k: s.get(k)
                for k in (
                    "index",
                    "codec_name",
                    "sample_rate",
                    "channels",
                    "channel_layout",
                )
            }
            for s in metadata.get("streams", [])
            if s.get("codec_type") == "audio"
        ],
    }


def color_disposition(facts, input_encoding=None):
    color = facts["color"]
    encoding = str(input_encoding or color.get("color_transfer") or "unknown").lower()
    if (
        encoding
        in {
            "smpte2084",
            "arib-std-b67",
            "pq",
            "hlg",
            "raw",
            "linear",
            "bt2020-10",
            "bt2020-12",
        }
        or "log" in encoding
        or color.get("color_primaries") == "bt2020"
    ):
        return {
            "status": "conversion_required",
            "encoding": encoding,
            "automatic_relabel_allowed": False,
        }
    if encoding not in {
        "bt709",
        "gamma22",
        "gamma28",
        "smpte170m",
        "smpte240m",
        "iec61966-2-1",
        "bt1361e",
        "iec61966-2-4",
    }:
        return {
            "status": "input_interpretation_pending",
            "encoding": encoding,
            "automatic_relabel_allowed": False,
        }
    return {
        "status": "known_sdr",
        "encoding": encoding,
        "automatic_relabel_allowed": False,
    }


def assert_sdr_processing_safe(metadata, input_encoding=None):
    disposition = color_disposition(media_facts(metadata), input_encoding)
    if disposition["status"] == "conversion_required":
        raise ContractError(
            "HDR/Log/wide-gamut source requires a verified color conversion; this SDR correction/export must not relabel or truncate it"
        )
    return disposition


def sample_cadence(source, ranges):
    samples = []
    for row in ranges[:3]:
        packets = probe_packet_timestamps(source, row["start"])
        # Packet storage order can differ from presentation order with B frames.
        points = sorted(
            {
                number(p["pts_time"], "packet PTS")
                for p in packets
                if p.get("pts_time") not in (None, "N/A")
            }
        )
        deltas = [b - a for a, b in zip(points, points[1:]) if b > a]
        varied = len(deltas) >= 3 and max(deltas) - min(deltas) > max(
            0.0001, min(deltas) * 0.05
        )
        samples.append(
            {
                "requested_start_s": row["start"],
                "actual_first_pts": points[0] if points else None,
                "packet_count": len(packets),
                "delta_min_s": min(deltas) if deltas else None,
                "delta_max_s": max(deltas) if deltas else None,
                "varying_pts_observed": varied,
            }
        )
    return {
        "samples": samples,
        "status": "varying_pts_requires_review"
        if any(s["varying_pts_observed"] for s in samples)
        else "no_variation_in_samples"
        if any(s["packet_count"] >= 4 for s in samples)
        else "insufficient_samples",
        "whole_source_cfr_proven": False,
        "media_copies_created": 0,
        "seek_may_begin_at_previous_keyframe": True,
    }


def inspect_source(
    source, *, project_type="spoken", checks=None, sample_timestamps=False
):
    defaults = profile(project_type)
    source_ref = ref(source, "production source")
    metadata = probe_media_metadata(source)
    facts = media_facts(metadata)
    duration = facts["duration_s"]
    if checks is None:
        width = min(POLICY["review"]["sample_window_s"], duration)
        starts = sorted(
            set([0.0, max(0.0, (duration - width) / 2), max(0.0, duration - width)])
        )
        checks = [
            {"start": a, "end": a + width, "checks": list(CHECKS)[:-1]} for a in starts
        ]
        coverage = "representative_only_not_full_review"
    else:
        coverage = "explicit_ranges_not_full_review"
    if not isinstance(checks, list) or not checks:
        raise ContractError("non-empty source review ranges required")
    rows = []
    for row in checks:
        a, b = interval(row)
        kinds = row.get("checks")
        if (
            b > duration + 1e-6
            or not isinstance(kinds, list)
            or not kinds
            or any(k not in CHECKS for k in kinds)
        ):
            raise ContractError("invalid source review bounds or check names")
        rows.append({"start": a, "end": b, "checks": list(dict.fromkeys(kinds))})
    if source_ref != ref(source, "production source"):
        raise ContractError("source changed during inspection")
    cadence = (
        sample_cadence(source, rows)
        if sample_timestamps
        else {"status": "not_measured", "whole_source_cfr_proven": False}
    )
    if source_ref != ref(source, "production source"):
        raise ContractError("source changed during cadence inspection")
    return seal(
        {
            "schema": AUDIT_SCHEMA,
            "source": source_ref,
            "metadata": metadata,
            "facts": facts,
            "cadence": cadence,
            "project_type": project_type,
            "profile": defaults,
            "color_disposition": color_disposition(facts),
            "review_ranges": sorted(rows, key=lambda r: r["start"]),
            "coverage": coverage,
            "media_copies_created": 0,
            "transcode_performed": False,
            "review_status": "pending",
            "policy_version": POLICY["version"],
        }
    )


def validate_audit(value):
    if value.get("schema") != AUDIT_SCHEMA or value.get("sha256") != payload_sha256(
        value, "sha256"
    ):
        raise ContractError("source audit integrity mismatch")
    bound(value["source"], "production source")
    if value.get("facts") != media_facts(value["metadata"]) or value.get(
        "profile"
    ) != profile(value["project_type"]):
        raise ContractError("source audit facts/profile drift")
    if (
        value.get("color_disposition") != color_disposition(value["facts"])
        or value.get("policy_version") != POLICY["version"]
    ):
        raise ContractError("source audit policy/color interpretation drift")
    if (
        value.get("media_copies_created") != 0
        or value.get("transcode_performed") is not False
    ):
        raise ContractError("source audit must not produce media")


def prepare_source_review(audit_path, output_dir):
    from .spoken_delivery import _new_directory, _self_media_module

    audit = checked_payload(audit_path, "production audit")
    validate_audit(audit)
    reviewer = _self_media_module("video_review_delivery")
    requests = []
    for i, row in enumerate(audit["review_ranges"], 1):
        a, b = interval(row)
        if b > audit["facts"]["duration_s"] + 1e-6 or any(
            k not in CHECKS for k in row["checks"]
        ):
            raise ContractError("invalid source review range")
        requests.append(
            {
                "review_id": f"production-{i:03d}",
                "source_path": audit["source"]["path"],
                "artifact_sha256": audit["source"]["sha256"],
                "review_lane": "mixed",
                "playback_mode": "normal",
                "review_access_mode": "source_time_range",
                "review_start_seconds": a,
                "review_end_seconds": b,
                "review_purpose": "；".join(CHECKS[k] for k in row["checks"]),
                "review_question": "逐项记录通过、需修改或不确定；未检查不得标为通过。项目重点："
                + ", ".join(audit["profile"]["priorities"]),
                "status": "pending",
            }
        )
        if "delivery" in row["checks"]:
            requests[-1]["review_question"] += (
                "；按手机实际显示大小检查字号、裁切和平台遮挡。"
            )
            requests.append(
                {
                    **requests[-1],
                    "review_id": f"production-{i:03d}-muted",
                    "playback_mode": "muted",
                    "review_question": "静音核对字幕、说话角色和真实声音事件是否表达充分；不得补造原声不存在的信息。",
                }
            )
    out = _new_directory(output_dir)
    req = write(
        {
            "schema_version": "self-media-human-review-request-pack.v1",
            "scope": "production_presets",
            "requests": requests,
        },
        out / "source-review-request.json",
    )
    receipt = reviewer.prepare(req, out / "review", "material_requirement")
    result = {
        "audit": ref(audit_path, "production audit"),
        "review_page": str(out / "review/targeted-review.html"),
        "review": receipt,
        "media_copies_created": 0,
        "human_review_status": "pending",
    }
    write(result, out / "source-review-receipt.json")
    return result


def assess_working_copy(source, duration_s, assessment):
    source_ref = ref(source, "working-copy source")
    if assessment.get("source") != source_ref:
        raise ContractError("working-copy assessment must bind exact source")
    reviewed(
        assessment,
        ["full_source_checked", "useful_content_accounted"],
        "working-copy exception",
    )
    duration = number(duration_s, "source duration")
    if duration <= 0:
        raise ContractError("source duration must be positive")
    ranges = assessment.get("useful_ranges")
    if not isinstance(ranges, list) or not ranges:
        raise ContractError("working-copy useful ranges required")
    previous, useful = 0.0, 0.0
    for row in ranges:
        a, b = interval(row)
        if a < previous or b > duration + 1e-6:
            raise ContractError(
                "working-copy ranges must be ordered, non-overlapping and within source"
            )
        previous, useful = b, useful + b - a
    estimate = assessment.get("estimated_output_bytes")
    if isinstance(estimate, bool) or not isinstance(estimate, int) or estimate <= 0:
        raise ContractError("positive working-copy output estimate required")
    size = Path(source).stat().st_size
    reduction = size - estimate
    policy = POLICY["working_copy"]
    eligible = (
        useful / duration <= policy["max_useful_fraction"]
        and reduction >= policy["min_estimated_reduction_bytes"]
        and size > 0
        and reduction / size >= policy["min_estimated_reduction_fraction"]
    )
    return {
        "eligible": eligible,
        "route": "explicit_working_copy_candidate" if eligible else "source_time_range",
        "useful_fraction": useful / duration,
        "estimated_archive_reduction_bytes": reduction,
        "temporary_additional_bytes_with_original_retained": estimate,
        "immediate_reclaimed_bytes": 0,
        "original_deletion_allowed": False,
        "reason": "low_useful_fraction_and_large_estimated_reduction"
        if eligible
        else "working_copy_thresholds_not_met",
    }


def restoration_decision(source, candidate, review):
    if review.get("source") != ref(source, "restoration source") or review.get(
        "candidate"
    ) != ref(candidate, "restoration candidate"):
        raise ContractError("restoration review must bind source and candidate")
    operation = review.get("operation")
    if operation not in {"stabilization", "enhancement"}:
        raise ContractError("unsupported restoration operation")
    reviewed(
        review,
        [
            "same_interval_compared",
            "normal_speed_checked",
            "eyes_checked",
            "teeth_checked",
            "text_checked",
            "temporal_consistency_checked",
        ],
        "restoration comparison",
    )
    interval(review)
    flags = review.get("degradation_flags", [])
    if not isinstance(flags, list) or any(
        f not in POLICY["restoration"]["degradation_flags"] for f in flags
    ):
        raise ContractError("unknown restoration degradation flag")
    flags = list(flags)
    if operation == "stabilization":
        scale = number(review.get("max_scale"), "stabilization scale")
        if scale < 1:
            raise ContractError("stabilization scale must be at least one")
        if scale > POLICY["restoration"]["max_stabilization_scale"]:
            flags.append("excessive_crop")
        reviewed(
            review,
            ["intentional_camera_motion_preserved", "critical_content_preserved"],
            "stabilization",
        )
    return {
        "accepted": not flags,
        "selected_path": str(Path(source if flags else candidate).resolve()),
        "fallback": "original" if flags else None,
        "reasons": sorted(set(flags)),
        "comparison_is_human_report": True,
        "reviewed_range": {"start": review["start"], "end": review["end"]},
        "whole_candidate_acceptance_claimed": False,
    }


def validate_composition_review(source, review, project_type):
    if review.get("source") != ref(source, "composition source"):
        raise ContractError("composition review source mismatch")
    interval(review)
    required = [k + "_checked" for k in profile(project_type)["composition_checks"]]
    reviewed(review, required, "composition")
    if review.get("unresolved_issues"):
        raise ContractError("composition has unresolved issues")
    return {
        "status": "reviewed",
        "required_checks": required,
        "human_report_not_machine_vision": True,
    }
