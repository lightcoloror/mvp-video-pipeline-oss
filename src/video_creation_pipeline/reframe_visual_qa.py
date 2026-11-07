from __future__ import annotations

import importlib
import math
import os
from pathlib import Path
from typing import Any

from .contract_io import (
    artifact_reference,
    atomic_copy_promote,
    atomic_write_json,
    atomic_write_text,
    file_sha256,
    payload_sha256,
    read_json_object,
    validate_artifact_reference,
)
from .errors import ContractError
from .execution_receipts import execute_ffmpeg_with_receipt, probe_media_metadata

SUPERVISION_VERSION = "0.30.1"
SUPERVISION_COMMIT = "5f25aa0ee6dc22891415b6e3d2e1689ce7a32952"
VISUAL_QA_SCHEMA = "video_creation_pipeline.smart_reframe_visual_qa_receipt.v1"


def _font(path: str | Path | None) -> Path | None:
    if path is not None:
        candidate = Path(path).expanduser().resolve()
        return candidate if candidate.is_file() else None
    candidate = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts/msyh.ttc"
    return candidate if candidate.is_file() else None


def supervision_doctor(font_path: str | Path | None = None) -> dict[str, Any]:
    font = _font(font_path)
    if font:
        try:
            from PIL import ImageFont

            ImageFont.truetype(str(font), 17)
        except (ImportError, OSError):
            font = None
    try:
        sv = importlib.import_module("supervision")
        version = str(sv.__version__)
        ready = version == SUPERVISION_VERSION and all(
            hasattr(sv, name)
            for name in (
                "Detections",
                "BoxAnnotator",
                "RichLabelAnnotator",
                "TraceAnnotator",
            )
        )
        error = "" if ready else "reviewed Supervision version/API unavailable"
    except (ImportError, OSError, AttributeError) as exc:
        ready, version, error = False, "", str(exc)
    return {
        "status": "ready" if ready and font else "blocked_optional_dependency_or_font",
        "version": version,
        "reviewed_version": SUPERVISION_VERSION,
        "reviewed_source_commit": SUPERVISION_COMMIT,
        "font_status": "ready" if font else "not_supplied_or_missing",
        "font_path": str(font) if font else "",
        "error": error,
        "validation_level": "runtime_import_only_not_real_video_acceptance",
    }


def build_sampled_debug_schedule(
    plan: dict[str, Any], diagnostic_evidence_path: str | Path | None = None
) -> list[dict[str, Any]]:
    from .reframe_detectors import validate_smart_reframe_evidence
    from .smart_reframe import validate_smart_reframe_plan

    validate_smart_reframe_plan(plan)
    duration = float(plan["source_metadata"]["duration_s"])
    if not 0 < duration <= 120:
        raise ContractError(
            "sampled debug requires an explicit clip of at most 120 seconds"
        )
    grouped: dict[float, dict[str, dict[str, Any]]] = {0.0: {}}
    references = list(plan["inputs"]["evidence"])
    if diagnostic_evidence_path is not None:
        diagnostic = read_json_object(diagnostic_evidence_path, "diagnostic evidence")
        validate_smart_reframe_evidence(diagnostic)
        for reference in references:
            selected = read_json_object(
                validate_artifact_reference(reference, "selected evidence"),
                "selected evidence",
            )
            provider = selected["provider"]
            if (
                provider.get("selected_track_id")
                and provider.get("source_evidence_sha256")
                != diagnostic["evidence_sha256"]
            ):
                raise ContractError(
                    "diagnostic evidence is not the selected track source"
                )
        references.insert(
            0, artifact_reference(diagnostic_evidence_path, "diagnostic evidence")
        )
    for reference in references:
        path = validate_artifact_reference(reference, "debug evidence")
        evidence = read_json_object(path, "debug evidence")
        validate_smart_reframe_evidence(evidence)
        if (
            evidence["source_media"]["sha256"]
            != plan["inputs"]["source_media"]["sha256"]
        ):
            raise ContractError("debug evidence source hash mismatch")
        for time in evidence["provider"].get("sample_times_s", []):
            if time < duration:
                grouped.setdefault(float(time), {})
        for row in evidence["observations"]:
            if row["kind"] not in {"face", "person", "user_hint"}:
                continue
            time = float(row["time_s"])
            if time > duration + 1e-6:
                raise ContractError("debug observation exceeds media duration")
            if time >= duration:
                continue  # An endpoint keyframe has no decoded source frame.
            rows = grouped.setdefault(time, {})
            old = rows.get(row["evidence_id"])
            if old is not None and any(
                old.get(key) != row.get(key)
                for key in ("kind", "time_s", "rect", "track_id", "score")
            ):
                raise ContractError("conflicting debug evidence IDs")
            rows[row["evidence_id"]] = row
    if len(grouped) > 240:
        raise ContractError("sampled debug is limited to 240 observed sampling batches")
    return [
        {
            "time_s": time,
            "observations": sorted(rows.values(), key=lambda r: r["evidence_id"]),
        }
        for time, rows in sorted(grouped.items())
    ]


def _privacy(path: str | Path | None, plan: dict[str, Any]) -> dict[str, Any] | None:
    if path is None:
        return None
    from .privacy_exclusion_zones import validate_privacy_exclusion_zones

    zones = read_json_object(path, "debug privacy zones")
    validate_privacy_exclusion_zones(zones)
    if zones["source_media"]["sha256"] != plan["inputs"]["source_media"]["sha256"]:
        raise ContractError("debug privacy zones source hash mismatch")
    if zones["coordinate_space"] != "redaction_input_normalized":
        raise ContractError("source-space debug requires source-space privacy zones")
    if any(
        int(zones["media_clock"][k]) != int(plan["source_metadata"][k])
        for k in ("width", "height")
    ):
        raise ContractError("debug privacy coordinate dimensions mismatch")
    return zones


def render_supervision_debug(
    plan_path: str | Path,
    output_video_path: str | Path,
    work_dir: str | Path,
    *,
    font_path: str | Path | None = None,
    privacy_zones_path: str | Path | None = None,
    diagnostic_evidence_path: str | Path | None = None,
    timeout_s: float = 300.0,
) -> dict[str, Any]:
    import numpy as np
    from PIL import Image, ImageDraw, ImageFont

    from .reframe_qa import _crop_at_time, build_smart_reframe_qa

    health = supervision_doctor(font_path)
    if health["status"] != "ready":
        raise ContractError(f"Supervision debug unavailable: {health}")
    sv = importlib.import_module("supervision")
    plan_file = Path(plan_path).expanduser().resolve()
    plan = read_json_object(plan_file, "debug plan")
    schedule = build_sampled_debug_schedule(plan, diagnostic_evidence_path)
    zones = _privacy(privacy_zones_path, plan)
    source = validate_artifact_reference(plan["inputs"]["source_media"], "debug source")
    root = Path(work_dir).expanduser().resolve()
    output = Path(output_video_path).expanduser().resolve()
    if root.exists() or output.exists() or output == source or output == plan_file:
        raise ContractError("Supervision debug requires fresh work/output paths")
    protected_paths = [
        source,
        plan_file,
        *[Path(r["path"]).resolve() for r in plan["inputs"]["evidence"]],
    ]
    if privacy_zones_path:
        protected_paths.append(Path(privacy_zones_path).resolve())
    if diagnostic_evidence_path:
        protected_paths.append(Path(diagnostic_evidence_path).resolve())
    if any(p.is_relative_to(root) or p == output for p in protected_paths):
        raise ContractError("Supervision debug paths overlap inputs")
    root.mkdir(parents=True)
    font = Path(health["font_path"])
    input_refs = [
        {"role": "debug_source", "path": source},
        {"role": "debug_plan", "path": plan_file},
    ]
    input_refs.extend(
        {"role": f"debug_evidence_{i}", "path": r["path"]}
        for i, r in enumerate(plan["inputs"]["evidence"])
    )
    if privacy_zones_path:
        input_refs.append({"role": "privacy_zones", "path": privacy_zones_path})
    if diagnostic_evidence_path:
        input_refs.append(
            {"role": "diagnostic_evidence", "path": diagnostic_evidence_path}
        )
    input_snapshot = [
        artifact_reference(row["path"], row["role"]) for row in input_refs
    ]
    box = sv.BoxAnnotator(thickness=2, color_lookup=sv.ColorLookup.TRACK)
    label = sv.RichLabelAnnotator(
        font_path=str(font),
        font_size=16,
        smart_position=True,
        color_lookup=sv.ColorLookup.TRACK,
        text_padding=3,
    )
    trace = sv.TraceAnnotator(trace_length=12, thickness=2)
    plain_font = ImageFont.truetype(str(font), 17)
    track_ids: dict[str, int] = {}
    frames, extracts, rows_manifest = [], [], []
    previous_shot = None
    duration = float(plan["source_metadata"]["duration_s"])
    for index, sample in enumerate(schedule):
        time = sample["time_s"]
        raw, annotated = (
            root / f"raw-{index:04d}.png",
            root / f"annotated-{index:04d}.png",
        )
        extract = root / f"extract-{index:04d}.json"
        execute_ffmpeg_with_receipt(
            "frame_extract",
            [
                "ffmpeg",
                "-hide_banner",
                "-y",
                "-ss",
                f"{time:.6f}",
                "-i",
                str(source),
                "-frames:v",
                "1",
                "-vf",
                "scale=960:-2",
                str(raw),
            ],
            input_refs,
            [{"role": "frame", "path": raw, "media_time_s": time}],
            extract,
            timeout_s=timeout_s,
        )
        extracts.append(artifact_reference(extract, "frame extract receipt"))
        with Image.open(raw) as image:
            rgb = image.convert("RGB")
        width, height = rgb.size
        shot_index, shot = next(
            (i, s)
            for i, s in enumerate(plan["shots"])
            if s["start_s"] <= time < s["end_s"]
        )
        if shot_index != previous_shot:
            trace = sv.TraceAnnotator(trace_length=12, thickness=2)
            previous_shot = shot_index
        subjects = [
            row for row in sample["observations"] if row["kind"] in {"face", "person"}
        ]
        boxes, ids, labels = [], [], []
        selected = {
            row.get("track_id")
            for row in sample["observations"]
            if row["kind"] == "user_hint"
        }
        for row in subjects:
            rect = row["rect"]
            boxes.append(
                [
                    rect["x"] * width,
                    rect["y"] * height,
                    (rect["x"] + rect["width"]) * width,
                    (rect["y"] + rect["height"]) * height,
                ]
            )
            key = str(row.get("track_id") or row["evidence_id"])
            track_ids.setdefault(key, len(track_ids) + 1)
            ids.append(track_ids[key])
            labels.append(
                f"{'选定主体' if row.get('track_id') in selected else '原始检测'} {key} {row['score']:.2f}"
            )
        if subjects:
            detections = sv.Detections(
                xyxy=np.array(boxes),
                tracker_id=np.array(ids),
                class_id=np.zeros(len(ids), dtype=int),
            )
            bgr = np.asarray(rgb)[:, :, ::-1].copy()
            bgr = trace.annotate(scene=bgr, detections=detections)
            bgr = box.annotate(scene=bgr, detections=detections)
            bgr = label.annotate(scene=bgr, detections=detections, labels=labels)
            rgb = Image.fromarray(bgr[:, :, ::-1])
        draw = ImageDraw.Draw(rgb)
        crop = _crop_at_time(
            shot["keyframes"], time, hold=shot["interpolation"] == "hold"
        )
        if crop:
            draw.rectangle(
                (
                    crop["x"] * width,
                    crop["y"] * height,
                    (crop["x"] + crop["width"]) * width,
                    (crop["y"] + crop["height"]) * height,
                ),
                outline="#32e67c",
                width=3,
            )
        if zones:
            for track in zones["tracks"]:
                for zone in track["zones"]:
                    if (
                        int(zone["frame_index"])
                        == int(time * zones["media_clock"]["fps"])
                        and zone["visible"]
                    ):
                        r = zone["effective_rect"]
                        draw.rectangle(
                            (
                                r["x"] * width,
                                r["y"] * height,
                                (r["x"] + r["width"]) * width,
                                (r["y"] + r["height"]) * height,
                            ),
                            outline="#ff5566",
                            width=3,
                        )
        draw.rectangle((0, height - 28, width, height), fill="#111111")
        draw.text(
            (8, height - 25),
            f"采样诊断 {time:.3f}s | {shot['strategy']} | 绿框=实际裁切 红框=隐私区 | 非成片",
            font=plain_font,
            fill="white",
        )
        rgb.save(annotated)
        frames.append(annotated)
        rows_manifest.append(
            {
                "time_s": time,
                "observed_evidence_ids": [r["evidence_id"] for r in subjects],
                "frame": artifact_reference(annotated, "annotated sample"),
            }
        )
    durations = [
        schedule[i + 1]["time_s"] - row["time_s"]
        if i + 1 < len(schedule)
        else duration - row["time_s"]
        for i, row in enumerate(schedule)
    ]
    concat = root / "samples.ffconcat"
    atomic_write_text(
        "ffconcat version 1.0\n"
        + "".join(
            f"file '{p.name}'\nduration {dt:.6f}\n" for p, dt in zip(frames, durations)
        )
        + f"file '{frames[-1].name}'\n",
        concat,
    )
    contact = root / "contact-sheet.jpg"
    tile_width, tile_height = 320, round(320 * height / width)
    sheet = Image.new(
        "RGB", (tile_width * 4, tile_height * math.ceil(len(frames) / 4)), "#111111"
    )
    for index, path in enumerate(frames):
        with Image.open(path) as image:
            sheet.paste(
                image.resize((tile_width, tile_height)),
                (index % 4 * tile_width, index // 4 * tile_height),
            )
    sheet.save(contact, quality=90)
    staged, ffmpeg_receipt = (
        root / "debug-staging.mp4",
        root / "debug-encode-receipt.json",
    )
    encoded = execute_ffmpeg_with_receipt(
        "render",
        [
            "ffmpeg",
            "-hide_banner",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat),
            "-i",
            str(source),
            "-map",
            "0:v:0",
            "-map",
            "1:a?",
            "-t",
            f"{duration:.6f}",
            "-r",
            "30",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-crf",
            "20",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-movflags",
            "+faststart",
            str(staged),
        ],
        [
            *input_refs,
            {"role": "sample_schedule", "path": concat},
            *[{"role": f"annotated_{i}", "path": p} for i, p in enumerate(frames)],
        ],
        [{"role": "sampled_debug_staging", "path": staged}],
        ffmpeg_receipt,
        timeout_s=timeout_s,
    )
    probe = probe_media_metadata(staged)
    if abs(float(probe["format"]["duration"]) - duration) > 1 / 30 + 0.002:
        raise ContractError("debug duration drift exceeds one output frame")
    if plan["source_metadata"]["has_audio"] and not any(
        s["codec_type"] == "audio" for s in probe["streams"]
    ):
        raise ContractError("debug audio was lost")
    decode_receipt = root / "debug-decode-receipt.json"
    decode_log = root / "debug-decode.log"
    execute_ffmpeg_with_receipt(
        "technical_qa",
        ["ffmpeg", "-v", "error", "-xerror", "-i", str(staged), "-f", "null", "-"],
        [{"role": "debug_staging", "path": staged}],
        [{"role": "full_decode_log", "path": decode_log}],
        decode_receipt,
        capture_log_path=decode_log,
        timeout_s=timeout_s,
    )
    for ref in input_snapshot:
        validate_artifact_reference(ref, "debug immutable input")
    atomic_copy_promote(staged, output, label="sampled Supervision debug")
    qa = build_smart_reframe_qa(plan_file)
    qa_path = root / "geometric-qa.json"
    atomic_write_json(qa, qa_path)
    receipt = {
        "schema": VISUAL_QA_SCHEMA,
        "status": "completed_debug_candidate_only",
        "source_plan": artifact_reference(plan_file, "smart reframe plan"),
        "source_plan_sha256": plan["plan_sha256"],
        "inputs": input_snapshot,
        "diagnostic_evidence": artifact_reference(
            diagnostic_evidence_path, "diagnostic evidence"
        )
        if diagnostic_evidence_path
        else None,
        "engine": health,
        "font_sha256": file_sha256(font),
        "sampled_diagnostic": True,
        "display_fps": 30,
        "sampling_policy": "observed_samples_held_until_next_sample_not_full_frame_detection",
        "samples": rows_manifest,
        "frame_extract_receipts": extracts,
        "contact_sheet": artifact_reference(contact, "all sampled frames"),
        "geometric_qa": artifact_reference(qa_path, "sampled geometric QA"),
        "debug_video": artifact_reference(output, "Supervision debug video"),
        "ffmpeg_receipt": artifact_reference(ffmpeg_receipt, "debug encode receipt"),
        "ffmpeg_receipt_sha256": encoded["receipt_sha256"],
        "decode_receipt": artifact_reference(decode_receipt, "full decode receipt"),
        "media_probe": probe,
        "candidate_only": True,
        "automatic_publish": False,
    }
    receipt["receipt_sha256"] = payload_sha256(receipt, "receipt_sha256")
    validate_visual_qa_receipt(receipt)
    atomic_write_json(receipt, root / "smart-reframe-visual-qa-receipt.json")
    return receipt


def validate_visual_qa_receipt(receipt: dict[str, Any]) -> None:
    from .execution_receipts import validate_ffmpeg_execution_receipt
    from .smart_reframe import validate_smart_reframe_plan

    if receipt.get("schema") != VISUAL_QA_SCHEMA or receipt.get(
        "receipt_sha256"
    ) != payload_sha256(receipt, "receipt_sha256"):
        raise ContractError("visual QA receipt integrity check failed")
    if (
        receipt.get("candidate_only") is not True
        or receipt.get("automatic_publish") is not False
        or receipt.get("sampled_diagnostic") is not True
        or receipt.get("status") != "completed_debug_candidate_only"
    ):
        raise ContractError("visual QA authority boundary drift")
    plan_file = validate_artifact_reference(receipt["source_plan"], "visual QA plan")
    plan = read_json_object(plan_file, "visual QA plan")
    validate_smart_reframe_plan(plan)
    if receipt["source_plan_sha256"] != plan["plan_sha256"]:
        raise ContractError("visual QA plan hash drift")
    diagnostic = receipt.get("diagnostic_evidence")
    diagnostic_path = (
        validate_artifact_reference(diagnostic, "diagnostic evidence")
        if diagnostic
        else None
    )
    expected = build_sampled_debug_schedule(plan, diagnostic_path)
    if [r["time_s"] for r in receipt["samples"]] != [r["time_s"] for r in expected]:
        raise ContractError("visual QA sampling clock drift")
    for sample, row in zip(receipt["samples"], expected):
        if sample["observed_evidence_ids"] != [
            r["evidence_id"]
            for r in row["observations"]
            if r["kind"] in {"face", "person"}
        ]:
            raise ContractError("visual QA observation provenance drift")
        validate_artifact_reference(sample["frame"], "visual QA sample")
    for reference in receipt["inputs"]:
        validate_artifact_reference(reference, "visual QA input")
    for name in ("contact_sheet", "geometric_qa", "debug_video"):
        validate_artifact_reference(receipt[name], name)
    for reference, operation in [
        *[(r, "frame_extract") for r in receipt["frame_extract_receipts"]],
        (receipt["ffmpeg_receipt"], "render"),
        (receipt["decode_receipt"], "technical_qa"),
    ]:
        path = validate_artifact_reference(reference, "visual QA FFmpeg receipt")
        payload = read_json_object(path, "visual QA FFmpeg receipt")
        validate_ffmpeg_execution_receipt(payload)
        if payload["operation"] != operation:
            raise ContractError("visual QA FFmpeg operation drift")
    if receipt["engine"]["version"] != SUPERVISION_VERSION:
        raise ContractError("visual QA runtime version drift")
