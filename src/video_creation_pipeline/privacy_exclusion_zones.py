from __future__ import annotations

import json
import math
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

from .contract_io import (
    artifact_reference,
    atomic_write_json,
    payload_sha256,
    read_json_object,
    validate_artifact_reference,
)
from .errors import ContractError
from .reframe_tracking import track_rectangles_with_norfair

PRIVACY_EXCLUSION_ZONES_SCHEMA = "video_creation_pipeline.privacy_exclusion_zones.v1"
PRIVACY_LAYOUT_COLLISION_REPORT_SCHEMA = (
    "video_creation_pipeline.privacy_layout_collision_report.v1"
)
SENTRYBLUR_COMMIT = "ded52d269b6e8e19dbbaf79f353426da3a228a98"


def media_clock_from_redaction_receipt(receipt: dict[str, Any]) -> dict[str, Any]:
    probes = receipt.get("ffprobe")
    if not isinstance(probes, dict) or not isinstance(probes.get("input"), dict):
        raise ContractError("redaction receipt lacks input media probe metadata")
    probe = probes["input"]
    streams = probe.get("streams")
    if not isinstance(streams, list):
        raise ContractError("redaction receipt input probe lacks streams")
    video = next(
        (
            row
            for row in streams
            if isinstance(row, dict) and row.get("codec_type") == "video"
        ),
        None,
    )
    if video is None:
        raise ContractError("redaction receipt input probe lacks a video stream")
    fps = _fraction(video.get("avg_frame_rate") or video.get("r_frame_rate"))
    format_value = probe.get("format") if isinstance(probe.get("format"), dict) else {}
    duration = float(video.get("duration") or format_value.get("duration") or 0)
    frame_count = int(video.get("nb_frames") or round(duration * fps))
    return _normalize_media_clock(
        {
            "fps": fps,
            "frame_count": frame_count,
            "width": video.get("width"),
            "height": video.get("height"),
            "duration_s": duration,
        }
    )


def read_detection_capture(paths: Iterable[str | Path]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for value in paths:
        path = Path(value).expanduser().resolve()
        if not path.is_file():
            raise ContractError(f"redaction coordinate capture not found: {path}")
        try:
            for line_number, raw in enumerate(
                path.read_text(encoding="utf-8-sig").splitlines(), start=1
            ):
                if not raw.strip():
                    continue
                row = json.loads(raw)
                if not isinstance(row, dict):
                    raise TypeError("capture row is not an object")
                rows.append(row)
        except (
            OSError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            TypeError,
            ValueError,
        ) as exc:
            raise ContractError(
                f"redaction coordinate capture is invalid at {path}: {exc}"
            ) from exc
    return rows


def build_privacy_exclusion_zones(
    source_media_path: str | Path,
    redacted_media_path: str | Path,
    redaction_job_path: str | Path,
    redaction_receipt_path: str | Path,
    detections: list[dict[str, Any]],
    *,
    media_clock: dict[str, Any],
    scene_cuts: list[int] | None = None,
) -> dict[str, Any]:
    source = Path(source_media_path).expanduser().resolve()
    redacted = Path(redacted_media_path).expanduser().resolve()
    job_path = Path(redaction_job_path).expanduser().resolve()
    receipt_path = Path(redaction_receipt_path).expanduser().resolve()
    job = read_json_object(job_path, "redaction job")
    receipt = read_json_object(receipt_path, "redaction receipt")
    _validate_redaction_bindings(job, receipt, source, redacted)
    clock = _normalize_media_clock(media_clock)
    cuts = _normalize_scene_cuts(scene_cuts or [], clock["frame_count"])
    normalized = [_normalize_capture_row(row, clock) for row in detections]
    tracked = track_rectangles_with_norfair(normalized, scene_cuts=cuts)
    dilation = int(job["parameters"]["dilation"])
    window = int(job["parameters"]["window"])
    expanded = [_with_expanded_rect(row, clock, dilation) for row in tracked]
    tracks = _temporal_tracks(expanded, clock, window, cuts)
    covered = sorted(
        {
            int(zone["frame_index"])
            for track in tracks
            for zone in track["zones"]
            if zone["visible"]
        }
    )
    uncovered = _uncovered_spans(covered, clock["frame_count"], cuts)
    model_hash = str(receipt.get("engine", {}).get("model_hash") or "")
    targets = sorted({str(row["target_type"]) for row in normalized})
    payload: dict[str, Any] = {
        "schema": PRIVACY_EXCLUSION_ZONES_SCHEMA,
        "source_media": artifact_reference(source, "privacy zones source media"),
        "redacted_media": artifact_reference(redacted, "privacy zones redacted media"),
        "source_redaction_job": artifact_reference(job_path, "redaction job"),
        "source_redaction_receipt": artifact_reference(
            receipt_path, "redaction receipt"
        ),
        "coordinate_space": "redaction_input_normalized",
        "media_clock": clock,
        "detector": {
            "tool": "SentryBlur",
            "source_commit": str(receipt.get("engine", {}).get("source_commit") or ""),
            "model_hash": model_hash,
            "target_types": targets,
            "same_invocation_as_redaction": True,
        },
        "redaction_parameters": {
            "dilation_px": dilation,
            "temporal_window_frames": window,
        },
        "scene_cuts": cuts,
        "tracks": tracks,
        "coverage": {
            "status": "review_required" if uncovered else "complete",
            "covered_frame_count": len(covered),
            "uncovered_spans": uncovered,
            "review_required": bool(uncovered),
        },
        "authority_boundary": _zones_boundary(),
    }
    payload["zones_sha256"] = payload_sha256(payload, "zones_sha256")
    validate_privacy_exclusion_zones(payload)
    return payload


def project_privacy_exclusion_zones(
    zones_path: str | Path, reframe_plan_path: str | Path
) -> dict[str, Any]:
    source_path = Path(zones_path).expanduser().resolve()
    plan_path = Path(reframe_plan_path).expanduser().resolve()
    zones = read_json_object(source_path, "privacy exclusion zones")
    validate_privacy_exclusion_zones(zones)
    if zones["coordinate_space"] != "redaction_input_normalized":
        raise ContractError("only source-space privacy zones can be projected")
    plan = read_json_object(plan_path, "smart reframe plan")
    from .smart_reframe import validate_smart_reframe_plan

    validate_smart_reframe_plan(plan)
    projected = dict(zones)
    projected["source_privacy_exclusion_zones"] = artifact_reference(
        source_path, "source privacy exclusion zones"
    )
    projected["source_smart_reframe_plan"] = artifact_reference(
        plan_path, "smart reframe plan"
    )
    projected["coordinate_space"] = "final_canvas_normalized"
    projected["projection"] = {
        "method": "smart_reframe_plan",
        "source_width": int(zones["media_clock"]["width"]),
        "source_height": int(zones["media_clock"]["height"]),
        "target_width": int(plan["target_profile"]["width"]),
        "target_height": int(plan["target_profile"]["height"]),
    }
    projected["media_clock"] = {
        **zones["media_clock"],
        "width": int(plan["target_profile"]["width"]),
        "height": int(plan["target_profile"]["height"]),
    }
    output_tracks: list[dict[str, Any]] = []
    for track in zones["tracks"]:
        output_zones = []
        for zone in track["zones"]:
            raw, _ = project_rect_through_reframe(
                zone["raw_rect"], plan, float(zone["pts_s"])
            )
            effective, _ = project_rect_through_reframe(
                zone["effective_rect"], plan, float(zone["pts_s"])
            )
            if effective is None:
                continue
            output_zones.append(
                {
                    **zone,
                    "source_raw_rect": dict(zone["raw_rect"]),
                    "source_effective_rect": dict(zone["effective_rect"]),
                    "raw_rect": raw or effective,
                    "effective_rect": effective,
                    "visible": True,
                }
            )
        if output_zones:
            output_tracks.append({**track, "zones": output_zones})
    projected["tracks"] = output_tracks
    covered = sorted(
        {zone["frame_index"] for track in output_tracks for zone in track["zones"]}
    )
    projected["coverage"] = {
        "status": zones["coverage"]["status"],
        "covered_frame_count": len(covered),
        "uncovered_spans": list(zones["coverage"]["uncovered_spans"]),
        "review_required": bool(zones["coverage"]["review_required"]),
    }
    projected["zones_sha256"] = payload_sha256(projected, "zones_sha256")
    validate_privacy_exclusion_zones(projected)
    return projected


def project_identity_privacy_exclusion_zones(
    zones_path: str | Path, *, target_width: int, target_height: int
) -> dict[str, Any]:
    source_path = Path(zones_path).expanduser().resolve()
    zones = read_json_object(source_path, "privacy exclusion zones")
    validate_privacy_exclusion_zones(zones)
    if zones["coordinate_space"] != "redaction_input_normalized":
        raise ContractError("identity projection requires source-space privacy zones")
    source_width = int(zones["media_clock"]["width"])
    source_height = int(zones["media_clock"]["height"])
    if target_width != source_width or target_height != source_height:
        raise ContractError(
            "identity projection requires source and final canvas dimensions to match"
        )
    projected = dict(zones)
    projected["source_privacy_exclusion_zones"] = artifact_reference(
        source_path, "source privacy exclusion zones"
    )
    projected["coordinate_space"] = "final_canvas_normalized"
    projected["projection"] = {
        "method": "identity",
        "source_width": source_width,
        "source_height": source_height,
        "target_width": target_width,
        "target_height": target_height,
    }
    projected["zones_sha256"] = payload_sha256(projected, "zones_sha256")
    validate_privacy_exclusion_zones(projected)
    return projected


def project_rect_through_reframe(
    rect: dict[str, Any], plan: dict[str, Any], time_s: float
) -> tuple[dict[str, float] | None, dict[str, Any]]:
    source_rect = _normalized_rect(rect, "source privacy rectangle")
    shot = _shot_at_time(plan, time_s)
    strategy = str(shot.get("strategy") or "")
    source_width = int(plan["source_metadata"]["width"])
    source_height = int(plan["source_metadata"]["height"])
    target_width = int(plan["target_profile"]["width"])
    target_height = int(plan["target_profile"]["height"])
    chain: dict[str, Any] = {
        "strategy": strategy,
        "time_s": round(float(time_s), 6),
        "shot_id": str(shot.get("shot_id") or ""),
        "source_size": [source_width, source_height],
        "target_size": [target_width, target_height],
        "interpolation": str(shot.get("interpolation") or "hold"),
        "smart_reframe_plan_sha256": str(plan.get("plan_sha256") or ""),
    }
    if strategy in {"fixed_crop", "tracked_crop"}:
        from .smart_reframe import _crop_at_time

        crop = _crop_at_time(
            list(shot.get("keyframes") or []),
            float(time_s),
            hold=shot.get("interpolation") == "hold",
        )
        if crop is None:
            raise ContractError("smart reframe crop is missing at privacy timestamp")
        chain["crop"] = crop
        projected = {
            "x": (source_rect["x"] - crop["x"]) / crop["width"],
            "y": (source_rect["y"] - crop["y"]) / crop["height"],
            "width": source_rect["width"] / crop["width"],
            "height": source_rect["height"] / crop["height"],
        }
    elif strategy == "contain":
        scale = min(target_width / source_width, target_height / source_height)
        drawn_width = source_width * scale / target_width
        drawn_height = source_height * scale / target_height
        projected = {
            "x": (1 - drawn_width) / 2 + source_rect["x"] * drawn_width,
            "y": (1 - drawn_height) / 2 + source_rect["y"] * drawn_height,
            "width": source_rect["width"] * drawn_width,
            "height": source_rect["height"] * drawn_height,
        }
        chain["foreground_scale"] = [drawn_width, drawn_height]
    elif strategy == "blur_pad":
        foreground_width = round(target_width * 0.97)
        foreground_height = round(target_height * 0.97)
        scale = min(foreground_width / source_width, foreground_height / source_height)
        drawn_width = source_width * scale / target_width
        drawn_height = source_height * scale / target_height
        projected = {
            "x": (1 - drawn_width) / 2 + source_rect["x"] * drawn_width,
            "y": (1 - drawn_height) / 2 + source_rect["y"] * drawn_height,
            "width": source_rect["width"] * drawn_width,
            "height": source_rect["height"] * drawn_height,
        }
        chain["foreground_scale"] = [drawn_width, drawn_height]
    else:
        raise ContractError(
            "privacy projection blocks mixed or unverified reframe strategies"
        )
    return _clip_rect(projected), chain


def privacy_regions_for_interval(
    zones: dict[str, Any], start_s: float, end_s: float
) -> list[dict[str, Any]]:
    if zones.get("coordinate_space") != "final_canvas_normalized":
        raise ContractError(
            "layout collision checks require final-canvas privacy zones"
        )
    if end_s <= start_s:
        raise ContractError("privacy interval end must be after start")
    regions: list[dict[str, Any]] = []
    for track in zones.get("tracks") or []:
        selected = [
            row["effective_rect"]
            for row in track.get("zones") or []
            if start_s - 1e-6 <= float(row["pts_s"]) <= end_s + 1e-6
        ]
        if not selected:
            continue
        regions.append(
            {
                "id": f"privacy_{track['target_type']}_{track['track_id']}",
                "rect": _rect_union(selected),
            }
        )
    return merge_overlapping_regions(regions)


def merge_overlapping_regions(
    regions: list[dict[str, Any]], *, adjacency: float = 0.005
) -> list[dict[str, Any]]:
    pending = [
        {"id": str(row["id"]), "rect": _normalized_rect(row["rect"], "region")}
        for row in regions
    ]
    merged: list[dict[str, Any]] = []
    while pending:
        current = pending.pop(0)
        changed = True
        while changed:
            changed = False
            remaining = []
            for candidate in pending:
                if _overlaps_or_adjacent(current["rect"], candidate["rect"], adjacency):
                    current = {
                        "id": f"{current['id']}+{candidate['id']}",
                        "rect": _rect_union([current["rect"], candidate["rect"]]),
                    }
                    changed = True
                else:
                    remaining.append(candidate)
            pending = remaining
        merged.append(current)
    return merged


def build_privacy_layout_collision_report(
    semantic_motion_plan_path: str | Path,
    privacy_zones_path: str | Path,
    *,
    redaction_order: str = "redaction_before_graphics",
) -> dict[str, Any]:
    if redaction_order not in {
        "redaction_before_graphics",
        "redaction_after_graphics",
    }:
        raise ContractError("privacy layout redaction order is invalid")
    motion_path = Path(semantic_motion_plan_path).expanduser().resolve()
    zones_path = Path(privacy_zones_path).expanduser().resolve()
    motion = read_json_object(motion_path, "semantic motion plan")
    zones = read_json_object(zones_path, "privacy exclusion zones")
    from .semantic_motion import validate_semantic_motion_plan

    validate_semantic_motion_plan(motion)
    validate_privacy_exclusion_zones(zones)
    _assert_motion_zones_binding(motion, zones, zones_path)
    cue_rows, total = _collision_rows(motion, zones, redaction_order)
    review_required = bool(zones["coverage"]["review_required"])
    passed = total == 0 and not review_required
    report: dict[str, Any] = {
        "schema": PRIVACY_LAYOUT_COLLISION_REPORT_SCHEMA,
        "source_semantic_motion_plan": artifact_reference(
            motion_path, "semantic motion plan"
        ),
        "source_privacy_exclusion_zones": artifact_reference(
            zones_path, "privacy exclusion zones"
        ),
        "redaction_order": redaction_order,
        "cues": cue_rows,
        "summary": {
            "collision_count": total,
            "geometry_gate_passed": passed,
            "status": (
                "review_required"
                if review_required
                else "passed_candidate_only"
                if passed
                else "blocked"
            ),
        },
        "authority_boundary": {
            "candidate_only": True,
            "publish_allowed": False,
            "final_privacy_rescan_required": True,
        },
    }
    report["report_sha256"] = payload_sha256(report, "report_sha256")
    validate_privacy_layout_collision_report(report)
    return report


def _collision_rows(
    motion: dict[str, Any], zones: dict[str, Any], redaction_order: str
) -> tuple[list[dict[str, Any]], int]:
    cue_rows: list[dict[str, Any]] = []
    total = 0
    for cue in motion["cues"]:
        start_s = float(cue["timing"]["timeline_start_s"])
        end_s = start_s + sum(
            float(cue["timing"][field])
            for field in ("full_frame_s", "enter_s", "hold_s", "exit_s")
        )
        privacy = privacy_regions_for_interval(zones, start_s, end_s)
        elements = [
            ("label", cue["label"]["rect"]),
            ("focus", cue["focus_destination"]),
            ("watermark", cue["watermark"]["rect"]),
            ("disclaimer", cue["disclaimer"]["rect"]),
        ]
        elements.extend(
            ("subtitle", row["rect"])
            for row in cue.get("protected_regions") or []
            if isinstance(row, dict) and row.get("id") == "subtitle"
        )
        collisions = []
        for element_id, element_rect in elements:
            for region in privacy:
                area = _intersection_area(element_rect, region["rect"])
                if area <= 0:
                    continue
                collisions.append(
                    {
                        "element_id": element_id,
                        "element_rect": _rounded_rect(element_rect),
                        "privacy_region_id": region["id"],
                        "privacy_rect": _rounded_rect(region["rect"]),
                        "intersection_area": round(area, 10),
                        "occludes_sensitive_subject": True,
                        "would_be_redacted": redaction_order
                        == "redaction_after_graphics",
                    }
                )
        total += len(collisions)
        cue_rows.append(
            {
                "cue_id": cue["cue_id"],
                "start_s": round(start_s, 6),
                "end_s": round(end_s, 6),
                "collisions": collisions,
                "selected_fallback": "blocked" if collisions else "none",
                "geometry_gate_passed": not collisions,
            }
        )
    return cue_rows, total


def validate_privacy_exclusion_zones(zones: dict[str, Any]) -> None:
    _validate_schema(zones, "privacy_exclusion_zones.v1.schema.json", "privacy zones")
    if zones.get("zones_sha256") != payload_sha256(zones, "zones_sha256"):
        raise ContractError("privacy exclusion zones integrity check failed")
    for key, label in (
        ("source_media", "privacy zones source media"),
        ("redacted_media", "privacy zones redacted media"),
        ("source_redaction_job", "privacy zones redaction job"),
        ("source_redaction_receipt", "privacy zones redaction receipt"),
    ):
        validate_artifact_reference(zones[key], label)
    for optional, label in (
        ("source_privacy_exclusion_zones", "source privacy exclusion zones"),
        ("source_smart_reframe_plan", "privacy zones smart reframe plan"),
    ):
        if zones.get(optional) is not None:
            validate_artifact_reference(zones[optional], label)
    clock = _normalize_media_clock(zones["media_clock"])
    projection = zones.get("projection")
    coordinate_space = zones["coordinate_space"]
    if coordinate_space == "redaction_input_normalized":
        if projection is not None or zones.get("source_privacy_exclusion_zones"):
            raise ContractError("source-space privacy zones cannot claim a projection")
    elif projection is not None:
        if zones.get("source_privacy_exclusion_zones") is None:
            raise ContractError("projected privacy zones lack their source artifact")
        if (
            int(projection["target_width"]) != clock["width"]
            or int(projection["target_height"]) != clock["height"]
        ):
            raise ContractError("privacy projection target dimensions drifted")
        if projection["method"] == "identity":
            if zones.get("source_smart_reframe_plan") is not None:
                raise ContractError("identity privacy projection cannot bind reframe")
            if int(projection["source_width"]) != int(
                projection["target_width"]
            ) or int(projection["source_height"]) != int(projection["target_height"]):
                raise ContractError("identity privacy projection dimensions drifted")
        elif zones.get("source_smart_reframe_plan") is None:
            raise ContractError("smart reframe privacy projection lacks its plan")
    cuts = _normalize_scene_cuts(zones["scene_cuts"], clock["frame_count"])
    if cuts != zones["scene_cuts"]:
        raise ContractError("privacy scene cuts are not normalized")
    if zones["detector"]["source_commit"] != SENTRYBLUR_COMMIT:
        raise ContractError("privacy detector source commit drifted")
    track_ids: set[str] = set()
    covered: set[int] = set()
    for track in zones["tracks"]:
        track_id = str(track["track_id"])
        if track_id in track_ids:
            raise ContractError("privacy track ids must be unique")
        track_ids.add(track_id)
        previous = -1
        for zone in track["zones"]:
            frame = int(zone["frame_index"])
            if frame <= previous or frame >= clock["frame_count"]:
                raise ContractError("privacy zone frames must be ordered and in range")
            previous = frame
            if abs(float(zone["pts_s"]) - frame / clock["fps"]) > 1 / clock["fps"]:
                raise ContractError("privacy zone timestamp drifted from media clock")
            raw = _normalized_rect(zone["raw_rect"], "privacy raw rectangle")
            effective = _normalized_rect(
                zone["effective_rect"], "privacy effective rectangle"
            )
            if not _contains(effective, raw):
                raise ContractError("privacy effective rectangle must contain raw box")
            if zone["visible"]:
                covered.add(frame)
    if zones["coverage"]["covered_frame_count"] != len(covered):
        raise ContractError("privacy coverage frame count drifted")
    uncovered = zones["coverage"]["uncovered_spans"]
    if bool(uncovered) != bool(zones["coverage"]["review_required"]):
        raise ContractError("privacy uncovered spans must require review")
    expected = "review_required" if uncovered else "complete"
    if zones["coverage"]["status"] != expected:
        raise ContractError("privacy coverage status drifted")


def validate_privacy_layout_collision_report(report: dict[str, Any]) -> None:
    _validate_schema(
        report,
        "privacy_layout_collision_report.v1.schema.json",
        "privacy layout collision report",
    )
    if report.get("report_sha256") != payload_sha256(report, "report_sha256"):
        raise ContractError("privacy layout collision report integrity check failed")
    motion_path = validate_artifact_reference(
        report["source_semantic_motion_plan"], "semantic motion plan"
    )
    zones_path = validate_artifact_reference(
        report["source_privacy_exclusion_zones"], "privacy exclusion zones"
    )
    motion = read_json_object(motion_path, "semantic motion plan")
    zones = read_json_object(zones_path, "privacy exclusion zones")
    from .semantic_motion import validate_semantic_motion_plan

    validate_semantic_motion_plan(motion)
    validate_privacy_exclusion_zones(zones)
    _assert_motion_zones_binding(motion, zones, zones_path)
    expected_cues, count = _collision_rows(
        motion, zones, str(report["redaction_order"])
    )
    if report["cues"] != expected_cues:
        raise ContractError("privacy collision geometry drifted")
    if count != report["summary"]["collision_count"]:
        raise ContractError("privacy collision count drifted")
    review_required = bool(zones["coverage"]["review_required"])
    passed = count == 0 and not review_required
    expected_status = (
        "review_required"
        if review_required
        else "passed_candidate_only"
        if passed
        else "blocked"
    )
    if (
        report["summary"]["geometry_gate_passed"] is not passed
        or report["summary"]["status"] != expected_status
    ):
        raise ContractError("privacy geometry gate summary drifted")


def _assert_motion_zones_binding(
    motion: dict[str, Any], zones: dict[str, Any], zones_path: Path
) -> None:
    motion_zones = motion.get("source_privacy_exclusion_zones")
    if not isinstance(motion_zones, dict):
        raise ContractError("semantic motion plan lacks privacy zones binding")
    expected_zones = artifact_reference(zones_path, "privacy exclusion zones")
    if motion_zones != expected_zones:
        raise ContractError("semantic motion and privacy zones binding drifted")
    motion_reframe = motion.get("source_smart_reframe_plan")
    zones_reframe = zones.get("source_smart_reframe_plan")
    if motion_reframe != zones_reframe:
        raise ContractError("semantic motion and privacy reframe binding drifted")


def write_privacy_exclusion_zones(
    zones: dict[str, Any], output_path: str | Path
) -> Path:
    validate_privacy_exclusion_zones(zones)
    return atomic_write_json(zones, output_path)


def write_privacy_layout_collision_report(
    report: dict[str, Any], output_path: str | Path
) -> Path:
    validate_privacy_layout_collision_report(report)
    return atomic_write_json(report, output_path)


def _validate_redaction_bindings(
    job: dict[str, Any], receipt: dict[str, Any], source: Path, redacted: Path
) -> None:
    if job.get("schema") != "video_creation_pipeline.privacy_redaction_job.v1":
        raise ContractError("privacy zones require a redaction job v1")
    if receipt.get("schema") != "video_creation_pipeline.privacy_redaction_receipt.v1":
        raise ContractError("privacy zones require a redaction receipt v1")
    if job.get("job_sha256") != payload_sha256(job, "job_sha256"):
        raise ContractError("privacy zones redaction job hash is invalid")
    if receipt.get("receipt_sha256") != payload_sha256(receipt, "receipt_sha256"):
        raise ContractError("privacy zones redaction receipt hash is invalid")
    if receipt.get("job_sha256") != job.get("job_sha256"):
        raise ContractError("privacy zones job and receipt are not bound")
    if Path(str(job.get("input", {}).get("path") or "")).resolve() != source:
        raise ContractError("privacy zones source does not match redaction job")
    if Path(str(job.get("output_path") or "")).resolve() != redacted:
        raise ContractError("privacy zones output does not match redaction job")
    if receipt.get("input") != job.get("input"):
        raise ContractError("privacy zones receipt input drifted")
    if (
        receipt.get("output", {}).get("sha256")
        != artifact_reference(redacted, "redacted media")["sha256"]
    ):
        raise ContractError("privacy zones redacted output hash drifted")
    if receipt.get("engine", {}).get("source_commit") != SENTRYBLUR_COMMIT:
        raise ContractError("privacy zones require the reviewed SentryBlur commit")


def _normalize_media_clock(value: dict[str, Any]) -> dict[str, Any]:
    try:
        fps = float(value["fps"])
        frame_count = int(value["frame_count"])
        width = int(value["width"])
        height = int(value["height"])
        duration_s = float(value.get("duration_s") or frame_count / fps)
    except (KeyError, TypeError, ValueError, ZeroDivisionError) as exc:
        raise ContractError("privacy media clock is invalid") from exc
    if (
        not math.isfinite(fps)
        or fps <= 0
        or frame_count <= 0
        or width <= 0
        or height <= 0
        or not math.isfinite(duration_s)
        or duration_s <= 0
    ):
        raise ContractError("privacy media clock is invalid")
    if abs(duration_s - frame_count / fps) > max(1 / fps, 0.05):
        raise ContractError("privacy media duration drifted by more than one frame")
    return {
        "fps": round(fps, 8),
        "frame_count": frame_count,
        "width": width,
        "height": height,
        "duration_s": round(duration_s, 8),
    }


def _fraction(value: Any) -> float:
    text = str(value or "").strip()
    try:
        if "/" in text:
            numerator, denominator = text.split("/", 1)
            result = float(numerator) / float(denominator)
        else:
            result = float(text)
    except (TypeError, ValueError, ZeroDivisionError) as exc:
        raise ContractError("redaction receipt frame rate is invalid") from exc
    if not math.isfinite(result) or result <= 0:
        raise ContractError("redaction receipt frame rate is invalid")
    return result


def _normalize_capture_row(
    row: dict[str, Any], clock: dict[str, Any]
) -> dict[str, Any]:
    try:
        frame = int(row["frame_index"])
        width = int(row.get("frame_width") or clock["width"])
        height = int(row.get("frame_height") or clock["height"])
        xyxy = [float(value) for value in row["xyxy"]]
        confidence = float(row.get("confidence", 1.0))
        target = str(row["target_type"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ContractError("redaction capture row is invalid") from exc
    if (
        frame < 0
        or frame >= clock["frame_count"]
        or width != clock["width"]
        or height != clock["height"]
        or len(xyxy) != 4
        or target not in {"face", "plate", "manual_region"}
        or not 0 <= confidence <= 1
    ):
        raise ContractError("redaction capture row is outside the media clock")
    x0, y0, x1, y1 = xyxy
    if x1 <= x0 or y1 <= y0:
        raise ContractError("redaction capture rectangle is empty")
    rect = _clip_rect(
        {
            "x": x0 / width,
            "y": y0 / height,
            "width": (x1 - x0) / width,
            "height": (y1 - y0) / height,
        }
    )
    if rect is None:
        raise ContractError("redaction capture rectangle is outside the frame")
    return {
        "observation_id": str(
            row.get("observation_id") or f"{target}-{frame}-{x0:.3f}-{y0:.3f}"
        ),
        "frame_index": frame,
        "time_s": round(frame / clock["fps"], 8),
        "rect": rect,
        "score": confidence,
        "target_type": target,
    }


def _with_expanded_rect(
    row: dict[str, Any], clock: dict[str, Any], dilation: int
) -> dict[str, Any]:
    rect = row["rect"]
    dx = dilation / clock["width"]
    dy = dilation / clock["height"]
    expanded = _clip_rect(
        {
            "x": rect["x"] - dx,
            "y": rect["y"] - dy,
            "width": rect["width"] + 2 * dx,
            "height": rect["height"] + 2 * dy,
        }
    )
    return {**row, "expanded_rect": expanded or dict(rect)}


def _temporal_tracks(
    detections: list[dict[str, Any]],
    clock: dict[str, Any],
    window: int,
    scene_cuts: list[int],
) -> list[dict[str, Any]]:
    by_track: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in detections:
        key = (str(row["target_type"]), str(row["track_id"]))
        by_track.setdefault(key, []).append(row)
    output = []
    for (target, track_id), rows in sorted(by_track.items()):
        zones = []
        segment_rows: dict[int, list[dict[str, Any]]] = {}
        for row in rows:
            segment_rows.setdefault(
                _scene_index(row["frame_index"], scene_cuts), []
            ).append(row)
        for segment, current_rows in sorted(segment_rows.items()):
            first = max(0, min(row["frame_index"] for row in current_rows) - window)
            last = min(
                clock["frame_count"] - 1,
                max(row["frame_index"] for row in current_rows) + window,
            )
            segment_start, segment_end = _scene_bounds(
                segment, scene_cuts, clock["frame_count"]
            )
            first = max(first, segment_start)
            last = min(last, segment_end)
            for frame in range(first, last + 1):
                neighbors = [
                    row
                    for row in current_rows
                    if abs(int(row["frame_index"]) - frame) <= window
                ]
                if not neighbors:
                    continue
                nearest = min(
                    neighbors,
                    key=lambda row: (abs(row["frame_index"] - frame), -row["score"]),
                )
                zones.append(
                    {
                        "frame_index": frame,
                        "pts_s": round(frame / clock["fps"], 8),
                        "confidence": round(max(row["score"] for row in neighbors), 8),
                        "source": (
                            "same_invocation_detection"
                            if any(row["frame_index"] == frame for row in neighbors)
                            else "temporal_union"
                        ),
                        "raw_rect": dict(nearest["rect"]),
                        "effective_rect": _rect_union(
                            [row["expanded_rect"] for row in neighbors]
                        ),
                        "visible": True,
                    }
                )
        output.append(
            {
                "track_id": _anonymous_track_id(target, track_id),
                "target_type": target,
                "identity_claimed": False,
                "zones": sorted(zones, key=lambda row: row["frame_index"]),
            }
        )
    return output


def _uncovered_spans(
    covered_frames: list[int], frame_count: int, scene_cuts: list[int]
) -> list[dict[str, Any]]:
    if not covered_frames:
        return []
    covered = set(covered_frames)
    start = min(covered_frames)
    end = max(covered_frames)
    spans = []
    gap_start: int | None = None
    for frame in range(start, end + 1):
        if frame not in covered and frame not in scene_cuts:
            gap_start = frame if gap_start is None else gap_start
        elif gap_start is not None:
            spans.append(
                {
                    "start_frame": gap_start,
                    "end_frame": frame - 1,
                    "reason": "no conservative detector or temporal-union coverage",
                }
            )
            gap_start = None
    if gap_start is not None:
        spans.append(
            {
                "start_frame": gap_start,
                "end_frame": end,
                "reason": "no conservative detector or temporal-union coverage",
            }
        )
    return spans


def _shot_at_time(plan: dict[str, Any], time_s: float) -> dict[str, Any]:
    for shot in plan.get("shots") or []:
        if float(shot["start_s"]) - 1e-6 <= time_s <= float(shot["end_s"]) + 1e-6:
            return shot
    raise ContractError("privacy timestamp is outside the smart reframe plan")


def _normalize_scene_cuts(value: list[int], frame_count: int) -> list[int]:
    cuts = sorted({int(item) for item in value})
    if any(item <= 0 or item >= frame_count for item in cuts):
        raise ContractError("privacy scene cut is outside the media clock")
    return cuts


def _scene_index(frame: int, cuts: list[int]) -> int:
    return sum(1 for cut in cuts if frame >= cut)


def _scene_bounds(index: int, cuts: list[int], frame_count: int) -> tuple[int, int]:
    start = 0 if index == 0 else cuts[index - 1]
    end = frame_count - 1 if index >= len(cuts) else cuts[index] - 1
    return start, end


def _anonymous_track_id(target: str, value: str) -> str:
    safe = "".join(
        character.lower() if character.isalnum() else "-" for character in value
    )
    safe = "-".join(part for part in safe.split("-") if part)
    return f"anonymous-{target}-{safe or 'track'}"


def _normalized_rect(value: dict[str, Any], label: str) -> dict[str, float]:
    try:
        rect = {name: float(value[name]) for name in ("x", "y", "width", "height")}
    except (KeyError, TypeError, ValueError) as exc:
        raise ContractError(f"{label} is invalid") from exc
    if not all(math.isfinite(number) for number in rect.values()):
        raise ContractError(f"{label} is invalid")
    if (
        rect["width"] <= 0
        or rect["height"] <= 0
        or rect["x"] < 0
        or rect["y"] < 0
        or rect["x"] + rect["width"] > 1.000001
        or rect["y"] + rect["height"] > 1.000001
    ):
        raise ContractError(f"{label} exceeds normalized bounds")
    return _rounded_rect(rect)


def _clip_rect(rect: dict[str, float]) -> dict[str, float] | None:
    left = max(0.0, float(rect["x"]))
    top = max(0.0, float(rect["y"]))
    right = min(1.0, float(rect["x"]) + float(rect["width"]))
    bottom = min(1.0, float(rect["y"]) + float(rect["height"]))
    if right <= left or bottom <= top:
        return None
    return _rounded_rect(
        {"x": left, "y": top, "width": right - left, "height": bottom - top}
    )


def _rounded_rect(rect: dict[str, Any]) -> dict[str, float]:
    return {name: round(float(rect[name]), 8) for name in ("x", "y", "width", "height")}


def _rect_union(rects: list[dict[str, Any]]) -> dict[str, float]:
    if not rects:
        raise ContractError("privacy rectangle union requires at least one rectangle")
    normalized = [_normalized_rect(rect, "privacy rectangle") for rect in rects]
    left = min(rect["x"] for rect in normalized)
    top = min(rect["y"] for rect in normalized)
    right = max(rect["x"] + rect["width"] for rect in normalized)
    bottom = max(rect["y"] + rect["height"] for rect in normalized)
    return _rounded_rect(
        {"x": left, "y": top, "width": right - left, "height": bottom - top}
    )


def _contains(outer: dict[str, float], inner: dict[str, float]) -> bool:
    return (
        outer["x"] <= inner["x"] + 1e-7
        and outer["y"] <= inner["y"] + 1e-7
        and outer["x"] + outer["width"] + 1e-7 >= inner["x"] + inner["width"]
        and outer["y"] + outer["height"] + 1e-7 >= inner["y"] + inner["height"]
    )


def _overlaps_or_adjacent(
    left: dict[str, float], right: dict[str, float], adjacency: float
) -> bool:
    return not (
        left["x"] + left["width"] + adjacency < right["x"]
        or right["x"] + right["width"] + adjacency < left["x"]
        or left["y"] + left["height"] + adjacency < right["y"]
        or right["y"] + right["height"] + adjacency < left["y"]
    )


def _intersection_area(left: dict[str, Any], right: dict[str, Any]) -> float:
    x = max(
        0.0,
        min(
            float(left["x"]) + float(left["width"]),
            float(right["x"]) + float(right["width"]),
        )
        - max(float(left["x"]), float(right["x"])),
    )
    y = max(
        0.0,
        min(
            float(left["y"]) + float(left["height"]),
            float(right["y"]) + float(right["height"]),
        )
        - max(float(left["y"]), float(right["y"])),
    )
    return x * y


def normalize_layout_rect(value: dict[str, Any], label: str) -> dict[str, float]:
    """Public geometry adapter for final-layout safety consumers.

    Intent: reuse the privacy owner's normalized-rectangle validation instead
    of creating a second geometry implementation. Decision: expose a narrow,
    read-only wrapper while preserving the existing private helper. Scope:
    geometry validation only; this does not render, redact, or mutate media.
    """

    return _normalized_rect(value, label)


def layout_intersection_area(left: dict[str, Any], right: dict[str, Any]) -> float:
    """Return normalized overlap area using the privacy collision engine."""

    return _intersection_area(left, right)


def _zones_boundary() -> dict[str, Any]:
    return {
        "candidate_only": True,
        "identity_recognition_performed": False,
        "layout_authority": "semantic_motion_plan",
        "publish_allowed": False,
        "final_privacy_rescan_required": True,
    }


def _validate_schema(payload: dict[str, Any], filename: str, label: str) -> None:
    path = Path(__file__).resolve().parent / "schemas" / filename
    try:
        schema = read_json_object(path, f"{label} schema")
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(payload)
    except (SchemaError, ValidationError) as exc:
        raise ContractError(f"{label} schema validation failed: {exc.message}") from exc
