"""Optional editorial treatments compiled into the existing spoken FFmpeg outlet."""

from __future__ import annotations

import copy
import re
from pathlib import Path

from .contract_io import (
    artifact_reference as ref,
    read_json_object as read,
    validate_artifact_reference as bound,
)
from .editing_presets import interval, number, reviewed
from .errors import ContractError
from .production_presets import (
    POLICY,
    assert_sdr_processing_safe,
    assess_working_copy,
    media_facts,
    profile,
)


def settings(request):
    value = copy.deepcopy(request.get("production", {}))
    allowed = {
        "project_type",
        "output_purpose",
        "export_profile",
        "working_copy_assessment",
        "audio_edit",
        "restoration_review",
    }
    if not isinstance(value, dict) or set(value) - allowed:
        raise ContractError("unsupported production settings")
    value.setdefault("project_type", "spoken")
    value.setdefault("output_purpose", "final_candidate")
    value.setdefault("export_profile", "source_sdr_h264")
    profile(value["project_type"])
    if (
        value["output_purpose"] not in {"final_candidate", "working_copy"}
        or value["export_profile"] not in POLICY["export_profiles"]
    ):
        raise ContractError("unsupported production purpose/export profile")
    if (
        value["output_purpose"] == "working_copy"
        and "working_copy_assessment" not in value
    ):
        raise ContractError("working-copy exception requires source-bound assessment")
    for key in ("working_copy_assessment", "audio_edit", "restoration_review"):
        if key in value:
            bound(value[key], key)
    if request["deliverable"] != "video" and any(
        k in value for k in ("working_copy_assessment", "audio_edit")
    ):
        raise ContractError(
            "media treatments cannot turn review/editor preparation into rendering"
        )
    return value


def prepare_render_options(plan, request, metadata):
    options = settings(request)
    facts = media_facts(metadata)
    color = assert_sdr_processing_safe(metadata)
    if options["export_profile"] == "web_sdr_1080" and any(
        facts["color"].get(k) != "bt709"
        for k in ("color_primaries", "color_transfer", "color_space")
    ):
        raise ContractError(
            "web SDR profile requires verified BT.709 input; metadata relabel is not conversion"
        )
    options.update(
        facts=facts, color_disposition=color, profile=profile(options["project_type"])
    )
    if "restoration_review" in options:
        from .production_presets import restoration_decision

        review = read(
            bound(options["restoration_review"], "restoration review"),
            "restoration review",
        )
        if review["candidate"] != ref(
            plan["source_video"]["path"], "restored candidate"
        ):
            raise ContractError(
                "restoration review candidate differs from actual timeline source"
            )
        result = restoration_decision(
            bound(review["source"], "original source"),
            bound(review["candidate"], "restored candidate"),
            review,
        )
        if not result["accepted"]:
            raise ContractError(
                "restored candidate rejected; return to original source review: "
                + ", ".join(result["reasons"])
            )
        a, b = interval(review)
        if any(s["in_s"] < a or s["out_s"] > b for s in plan["nle_segments"]):
            raise ContractError(
                "restoration comparison does not cover actual selected source ranges"
            )
        options["restoration_decision"] = result
    if options["output_purpose"] == "working_copy":
        assessment = read(
            bound(options["working_copy_assessment"], "working copy"), "working copy"
        )
        expected = [
            {"start": s["in_s"], "end": s["out_s"]} for s in plan["nle_segments"]
        ]
        if assessment.get("useful_ranges") != expected:
            raise ContractError(
                "working-copy assessment must match actual selected timeline"
            )
        decision = assess_working_copy(
            plan["source_video"]["path"], facts["duration_s"], assessment
        )
        if not decision["eligible"]:
            raise ContractError(
                "working copy not justified; use original source-time-range review"
            )
        options["working_copy_decision"] = decision
    edit = (
        read(bound(options["audio_edit"], "audio edit"), "audio edit")
        if "audio_edit" in options
        else {}
    )
    options["audio_treatment"] = validate_audio_edit(plan, facts, edit)
    return options


def validate_audio_edit(plan, facts, edit):
    allowed = {
        "source",
        "source_plan_sha256",
        "audio_stream_ordinal",
        "audio_stream_review",
        "split_edits",
        "room_tone",
    }
    if not isinstance(edit, dict) or set(edit) - allowed:
        raise ContractError("unsupported audio treatment field")
    if edit and (
        edit.get("source") != ref(plan["source_video"]["path"], "source")
        or edit.get("source_plan_sha256") != plan.get("plan_sha256")
    ):
        raise ContractError("audio treatment must bind exact source and edit plan")
    streams = facts["audio_streams"]
    ordinal = edit.get("audio_stream_ordinal", 0)
    if (
        isinstance(ordinal, bool)
        or not isinstance(ordinal, int)
        or ordinal < 0
        or (streams and ordinal >= len(streams))
    ):
        raise ContractError("invalid audio stream ordinal")
    if len(streams) > 1:
        reviewed(
            edit.get("audio_stream_review"),
            ["all_speakers_accounted", "channel_meaning_checked"],
            "audio stream selection",
        )
    video = [
        {"start": number(s["in_s"]), "end": number(s["out_s"])}
        for s in plan["nle_segments"]
    ]
    split = edit.get("split_edits", [])
    rooms = edit.get("room_tone", [])
    if not isinstance(split, list) or not isinstance(rooms, list):
        raise ContractError("audio treatments must be lists")
    if (split or rooms) and not streams:
        raise ContractError("audio treatments require original audio")
    seen = set()
    for row in split:
        index = row.get("boundary_after_segment")
        if (
            isinstance(index, bool)
            or not isinstance(index, int)
            or index in seen
            or not 0 <= index < len(video) - 1
        ):
            raise ContractError("split edit needs unique interior boundary index")
        seen.add(index)
        reviewed(
            row,
            [
                "context_checked",
                "chronology_preserved",
                "reaction_not_fabricated",
                "visual_handles_checked",
            ],
            "J/L edit",
        )
        offset = number(row.get("video_cut_offset_s"), "video cut offset")
        if not offset:
            raise ContractError("zero offset is a straight cut, not a J/L edit")
        video[index]["end"] += offset
        video[index + 1]["start"] += offset
    for row in video:
        a, b = interval(row)
        if b > facts["duration_s"] + 1e-6:
            raise ContractError("split edit exceeds source handles")
    duration = sum(s["duration_s"] for s in plan["nle_segments"])
    if abs(sum(r["end"] - r["start"] for r in video) - duration) > 1e-5:
        raise ContractError("split edit changed timeline length")
    previous = 0.0
    for row in rooms:
        a, b = interval(row)
        sa, sb = interval(row.get("source_range"))
        if (
            a < previous
            or b > duration + 1e-6
            or sb > facts["duration_s"]
            or sb - sa < b - a
        ):
            raise ContractError(
                "room tone ranges overlap/exceed timeline or available source"
            )
        previous = b
        gain = number(row.get("gain_db"), "room tone gain")
        if not -60 <= gain <= 0:
            raise ContractError("room tone gain must be -60..0 dB")
        reviewed(
            row,
            [
                "same_scene",
                "contains_no_speech_or_reaction",
                "target_non_dialogue_checked",
                "auditioned",
            ],
            "room tone",
        )
    return {
        "audio_stream_ordinal": ordinal,
        "video_ranges": video,
        "split_edits": copy.deepcopy(split),
        "room_tone": copy.deepcopy(rooms),
        "duration_s": duration,
    }


def treatment_filters(plan, options, has_audio):
    """Audio stays on the original edit clock; only video boundaries move for J/L."""
    from .editing_presets import fade_filter

    treatment = options["audio_treatment"]
    filters = []
    for i, (segment, video) in enumerate(
        zip(plan["nle_segments"], treatment["video_ranges"], strict=True)
    ):
        filters.append(
            f"[0:v]trim=start={video['start']}:end={video['end']},setpts=PTS-STARTPTS[v{i}]"
        )
        if has_audio:
            splices = plan.get("audio_splices", [])
            fade = fade_filter(splices[i]) if splices else ""
            gains = plan.get("clip_gains", [])
            gain = f",volume={gains[i]['gain_db']}dB" if gains else ""
            filters.append(
                f"[0:a:{treatment['audio_stream_ordinal']}]atrim=start={segment['in_s']}:end={segment['out_s']},asetpts=PTS-STARTPTS,aresample=48000"
                + gain
                + ("," + fade if fade else "")
                + f"[a{i}]"
            )
    n = len(plan["nle_segments"])
    filters.append(
        "".join(f"[v{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=0[vbase]"
    )
    if options["export_profile"] == "web_sdr_1080":
        facts = options["facts"]
        portrait = facts["height"] > facts["width"]
        if abs(facts["rotation_degrees"]) % 180 == 90:
            portrait = not portrait
        w, h = (1080, 1920) if portrait else (1920, 1080)
        filters.append(
            f"[vbase]scale=w='min(iw,{w})':h='min(ih,{h})':force_original_aspect_ratio=decrease:force_divisible_by=2[v]"
        )
    else:
        filters.append("[vbase]null[v]")
    if has_audio:
        filters.append(
            "".join(f"[a{i}]" for i in range(n)) + f"concat=n={n}:v=0:a=1[abase]"
        )
        rooms = treatment["room_tone"]
        for i, row in enumerate(rooms):
            a, b = interval(row)
            start = row["source_range"]["start"]
            fade = min(0.05, (b - a) / 4)
            filters.append(
                f"[0:a:{treatment['audio_stream_ordinal']}]atrim=start={start}:duration={b - a},asetpts=PTS-STARTPTS,aresample=48000,volume={row['gain_db']}dB,afade=t=in:d={fade},afade=t=out:st={b - a - fade}:d={fade},adelay={a * 1000}:all=1[room{i}]"
            )
        if rooms:
            filters.append(
                "[abase]"
                + "".join(f"[room{i}]" for i in range(len(rooms)))
                + f"amix=inputs={len(rooms) + 1}:duration=first:normalize=0[a]"
            )
        else:
            filters.append("[abase]anull[a]")
    return filters


def export_arguments(options):
    # No -r, interpolation, automatic downmix or upscaling.
    args = ["-fps_mode", "vfr"]
    for key, flag in (
        ("color_primaries", "-color_primaries"),
        ("color_transfer", "-color_trc"),
        ("color_space", "-colorspace"),
        ("color_range", "-color_range"),
    ):
        value = options["facts"]["color"].get(key)
        if value and value not in {"unknown", "unspecified", "reserved"}:
            args += [flag, str(value)]
    if all(
        options["facts"]["color"].get(k) == "bt709"
        for k in ("color_primaries", "color_transfer", "color_space")
    ):
        # Preserve verified source interpretation in H.264 VUI as well as container
        # fields. This branch must never be used as an HDR/Log conversion.
        args += [
            "-bsf:v",
            "h264_metadata=colour_primaries=1:transfer_characteristics=1:matrix_coefficients=1",
        ]
    return args


def final_metadata_check(metadata, expected_duration, options):
    facts = media_facts(metadata)
    video = next(s for s in metadata["streams"] if s.get("codec_type") == "video")
    export = POLICY["export_profiles"][options["export_profile"]]
    errors, unverified = [], []
    for field, expected in (
        ("codec_name", export["video_codec"]),
        ("pix_fmt", export["pixel_format"]),
    ):
        if not video.get(field):
            unverified.append(field)
        elif video[field] != expected:
            errors.append(field)
    tolerance = max(0.15, 2 / (options["facts"]["avg_fps"] or 25))
    if abs(facts["duration_s"] - expected_duration) > tolerance:
        errors.append("duration")
    if len(facts["audio_streams"]) != int(bool(options["facts"]["audio_streams"])):
        errors.append("audio_presence")
    for field, expected in options["facts"]["color"].items():
        if expected and expected not in {"unknown", "unspecified", "reserved"}:
            actual = facts["color"].get(field)
            if not actual:
                unverified.append(field)
            elif actual != expected:
                errors.append(field)
    source_audio = options["facts"]["audio_streams"]
    ordinal = options["audio_treatment"]["audio_stream_ordinal"]
    if source_audio and facts["audio_streams"]:
        expected_channels = source_audio[ordinal].get("channels")
        actual_channels = facts["audio_streams"][0].get("channels")
        if (
            expected_channels
            and actual_channels
            and expected_channels != actual_channels
        ):
            errors.append("audio_channels")
        elif not actual_channels:
            unverified.append("audio_channels")
    for audio in facts["audio_streams"]:
        for field, expected in (("codec_name", "aac"), ("sample_rate", "48000")):
            if audio.get(field) is None:
                unverified.append("audio_" + field)
            elif str(audio[field]) != expected:
                errors.append("audio_" + field)
    if options["export_profile"] == "web_sdr_1080":
        if (
            max(facts["width"], facts["height"]) > 1920
            or min(facts["width"], facts["height"]) > 1080
        ):
            errors.append("dimensions")
        if any(
            facts["color"].get(k) != "bt709"
            for k in ("color_primaries", "color_transfer", "color_space")
        ):
            errors.append("color")
    return {
        "status": "failed" if errors else "unverified" if unverified else "passed",
        "errors": errors,
        "unverified": unverified,
        "normal_speed_review": "pending",
        "mobile_size_review": "pending",
        "muted_review": "pending",
        "sync_review": "pending",
    }


def verify_working_copy_result(plan, options, candidate):
    if options["output_purpose"] != "working_copy":
        return {"applicable": False}
    assessment = read(
        bound(options["working_copy_assessment"], "working-copy assessment"),
        "working-copy assessment",
    )
    assessment["estimated_output_bytes"] = Path(candidate).stat().st_size
    result = assess_working_copy(
        plan["source_video"]["path"], options["facts"]["duration_s"], assessment
    )
    if not result["eligible"]:
        raise ContractError(
            "actual working-copy size does not meet storage exception; preserve originals and mark candidate unsuccessful"
        )
    return {
        **result,
        "applicable": True,
        "size_basis": "actual_output_bytes",
        "actual_archive_reduction_bytes": result["estimated_archive_reduction_bytes"],
    }


def detection_findings(log_path):
    text = Path(log_path).read_text(encoding="utf-8", errors="replace")
    events = []
    for match in re.finditer(
        r"(black_start|black_end|freeze_start|freeze_end|silence_start|silence_end):\s*([0-9.]+)",
        text,
    ):
        events.append(
            {
                "kind": match[1],
                "time_s": float(match[2]),
                "disposition": "pending_not_automatic_error",
            }
        )
    return {
        "events": events,
        "automatic_deletion": False,
        "human_disposition_required": bool(events),
    }
