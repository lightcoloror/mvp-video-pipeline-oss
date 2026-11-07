"""Deliver an existing spoken edit through existing review, FFmpeg and NLE tools.

Selection is deterministic from an Agent's explicit deliverable assessment. It
does not interpret free text as permission, render on compile, or pick an editor
just because that editor is installed. A request is not publication authority.
"""

from __future__ import annotations

import argparse
import importlib
import json
import math
import os
import sys
from pathlib import Path

from jsonschema import Draft202012Validator

from .contract_io import (
    artifact_reference as ref,
    atomic_write_json as write,
    payload_sha256,
    read_json_object as read,
    validate_artifact_reference as bound,
)
from .errors import ContractError
from .editing_presets import fade_filter
from .execution_receipts import execute_ffmpeg_with_receipt, probe_media_metadata
from .spoken_rough_cut import validate_spoken_edit_plan

REQUEST_SCHEMA = "video_creation_pipeline.spoken_delivery_request.v1"
SELECTION_SCHEMA = "video_creation_pipeline.spoken_delivery_selection.v1"
ROOT = Path(__file__).resolve().parents[2]
WORKSPACE = Path(os.environ.get("MVP_VIDEO_WORKSPACE", str(ROOT.parent.parent))).expanduser().resolve()


def validate_delivery_request(request: dict) -> str:
    schema = json.loads(
        (
            Path(__file__).parent / "schemas/spoken_delivery_request.v1.schema.json"
        ).read_text(encoding="utf8")
    )
    errors = sorted(
        Draft202012Validator(schema).iter_errors(request), key=lambda e: str(e.path)
    )
    if errors:
        raise ContractError("spoken delivery request invalid: " + errors[0].message)
    from .production_delivery import settings
    settings(request)
    kind = request["deliverable"]
    return {"review": "source_review", "video": "ffmpeg"}.get(
        kind, request.get("editor")
    )


def require_spoken_route(plan_path, request_path, expected_route):
    if request_path is None:
        raise ContractError(
            "explicit spoken delivery request required; old next_action is not routing authority"
        )
    request = read(request_path, "spoken delivery request")
    if validate_delivery_request(request) != expected_route:
        raise ContractError(
            "spoken delivery route conflicts with requested deliverable/editor"
        )
    plan = read(plan_path, "spoken edit plan")
    validate_spoken_edit_plan(plan)
    if expected_route != "source_review":
        from .segment_leveling import validate_leveling
        validate_leveling(plan.get("segment_leveling"), source=plan["source_video"],
            keeps=[dict(start=s['in_s'],end=s['out_s']) for s in plan['nle_segments']],
            gains=plan.get('clip_gains', []))
    return plan, request


def select_delivery(plan_path, request_path):
    request = read(request_path, "spoken delivery request")
    route = validate_delivery_request(request)
    plan, _ = require_spoken_route(plan_path, request_path, route)
    row = {
        "schema": SELECTION_SCHEMA,
        "source_spoken_edit_plan": ref(plan_path, "spoken edit plan"),
        "source_delivery_request": ref(request_path, "spoken delivery request"),
        "route": route,
        "deliverable": request["deliverable"],
        "selection_basis": request["selection_basis"],
        "source_schema": plan["schema"],
        "legacy_next_action_ignored": plan["schema"].endswith(".v1"),
        "next_action": "prepare_delivery",
        "candidate_only": True,
        "execution_performed": False,
        "publish_authorized": False,
    }
    row["selection_sha256"] = payload_sha256(row, "selection_sha256")
    return row


def validate_delivery_selection(selection):
    if selection.get("schema") != SELECTION_SCHEMA:
        raise ContractError("unsupported spoken delivery selection")
    if selection.get("selection_sha256") != payload_sha256(
        selection, "selection_sha256"
    ):
        raise ContractError("spoken delivery selection integrity mismatch")
    pp = bound(selection["source_spoken_edit_plan"], "spoken edit plan")
    rp = bound(selection["source_delivery_request"], "spoken delivery request")
    if selection != select_delivery(pp, rp):
        raise ContractError("spoken delivery selection differs from current request")
    return pp, rp


def _context(selection_path, expected_route=None):
    selection = read(selection_path, "spoken delivery selection")
    pp, rp = validate_delivery_selection(selection)
    if expected_route and selection["route"] != expected_route:
        raise ContractError("selected route cannot perform this operation")
    return selection, pp, rp, read(pp, "spoken edit plan")


def _new_directory(path):
    path = Path(path).expanduser().resolve()
    if path.exists():
        raise ContractError(
            "delivery output must be a new directory; preserve existing artifacts"
        )
    path.mkdir(parents=True)
    return path


def _self_media_module(name):
    scripts = WORKSPACE / "self-media-creation-system/scripts"
    if not (scripts / (name + ".py")).is_file():
        raise ContractError(
            "existing self-media consumer unavailable; no fallback export"
        )
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    return importlib.import_module(name)


def timeline_mapping(plan):
    cursor, rows = 0.0, []
    for segment in plan["nle_segments"]:
        end = round(cursor + segment["duration_s"], 3)
        rows.append(
            {
                "segment_id": segment["segment_id"],
                "source_path": segment["source_path"],
                "source_sha256": segment["source_sha256"],
                "source_start": segment["in_s"],
                "source_end": segment["out_s"],
                "target_start": cursor,
                "target_end": end,
            }
        )
        cursor = end
    return rows


def prepare_delivery(
    selection_path,
    output_dir,
    *,
    vectcut_source=None,
    canvas=None,
    project_id="",
    sequence_id="",
    playhead_s=0.0,
):
    selection, pp, rp, plan = _context(selection_path)
    route = selection["route"]
    if route != "jianying" and (vectcut_source is not None or canvas is not None):
        raise ContractError("Jianying options conflict with selected delivery route")
    if route != "premiere" and (project_id or sequence_id or playhead_s):
        raise ContractError("Premiere options conflict with selected delivery route")
    # Build and validate jobs before creating any output. Missing dependencies
    # must never trigger a silent switch to a different editor or renderer.
    job = None
    if route == "jianying":
        if not vectcut_source or not canvas:
            raise ContractError(
                "selected Jianying delivery requires fixed VectCut source and explicit canvas"
            )
        from .jianying_draft_handoff import build_jianying_spoken_draft_job

        job = build_jianying_spoken_draft_job(
            pp, vectcut_source, delivery_request_path=rp, **canvas
        )
    elif route == "premiere":
        from .nle_bridge import build_spoken_premiere_import_job

        job = build_spoken_premiere_import_job(
            pp,
            rp,
            project_id=project_id,
            sequence_id=sequence_id,
            playhead_s=playhead_s,
        )
    elif route == "source_review":
        reviewer = _self_media_module("video_review_delivery")
    out = _new_directory(output_dir)
    mapping = write(
        {
            "source_spoken_edit_plan": ref(pp, "spoken edit plan"),
            "segments": timeline_mapping(plan),
        },
        out / "source-timeline-map.json",
    )
    result = {
        "route": route,
        "selection": ref(selection_path, "delivery selection"),
        "timeline_map": ref(mapping, "timeline map"),
        "candidate_only": True,
        "video_rendered": False,
        "editor_opened": False,
        "publish_authorized": False,
        "boundary_review_windows": plan.get("boundary_review_windows", []),
        "audio_processing": {
            "segment_leveling": plan.get("segment_leveling"),
            "rough_cut_audio_gate": plan.get("rough_cut_audio_gate"),
            "per_segment_gain_application_and_remeasurement_required": True,
            "ffmpeg_applies_reviewed_clip_gains_and_safe_fades": route == "ffmpeg",
            "editor_audio_adjustments_require_manual_verification": route in ("jianying", "premiere"),
            "audition_status": "pending",
        },
    }
    if route == "source_review":
        request = {
            "schema_version": "self-media-human-review-request-pack.v1",
            "scope": "spoken_rough_cut_recipe",
            "requests": [],
        }
        for s in plan["nle_segments"]:
            request["requests"].append(
                {
                    "review_id": s["segment_id"],
                    "source_path": s["source_path"],
                    "artifact_sha256": s["source_sha256"],
                    "review_lane": "mixed",
                    "playback_mode": "normal",
                    "review_access_mode": "source_time_range",
                    "review_start_seconds": s["in_s"],
                    "review_end_seconds": s["out_s"],
                    "review_purpose": "核对口播净剪保留段、原声、起止点和接续语义",
                    "status": "pending",
                }
            )
        req_path = write(request, out / "source-review-request.json")
        receipt = reviewer.prepare(req_path, out / "review", "rough_cut_recipe")
        result.update(
            status="source_review_prepared",
            review=receipt,
            review_page=str(out / "review/targeted-review.html"),
            media_copies_created=0,
        )
    elif job is not None:
        job_path = write(job, out / (route + "-job.json"))
        result.update(
            status=job["status"],
            job=ref(job_path, "editor job"),
            desktop_validation_required=True,
        )
        if route == "jianying":
            result["next_action"] = [
                "video-jianying-draft",
                "materialize",
                "--job",
                str(job_path),
                "--output-dir",
                str(out / "draft-candidate"),
            ]
        else:
            result["next_action"] = (
                "existing_premiere_atomic_file_queue_after_live_target_check"
            )
    else:
        result.update(
            status="ready_for_explicit_video_export",
            next_action=[
                "video-spoken-rough-cut",
                "render-delivery",
                "--selection",
                str(Path(selection_path).resolve()),
                "--output-dir",
                str(out / "render"),
                "--project-root",
                "<project-root>",
                "--lifecycle-manifest",
                "<manifest.json>",
                "--estimated-output-bytes",
                "<positive-bytes>",
            ],
            output_scope="rough_cut_original_audio; packaging remains a separate stage",
        )
    write(result, out / "delivery-preparation.json")
    return result


def _render_command(plan, output, has_audio, production_options=None):
    filters = []
    for i, s in enumerate(plan["nle_segments"]):
        filters.append(
            f"[0:v]trim=start={s['in_s']}:end={s['out_s']},setpts=PTS-STARTPTS[v{i}]"
        )
        if has_audio:
            splice = plan.get("audio_splices", [])
            fade = fade_filter(splice[i]) if splice else ""
            gains = plan.get("clip_gains", [])
            gain = f",volume={gains[i]['gain_db']}dB" if gains else ""
            filters.append(
                f"[0:a]atrim=start={s['in_s']}:end={s['out_s']},asetpts=PTS-STARTPTS,aresample=48000"
                + gain + ("," + fade if fade else "") + f"[a{i}]"
            )
    n = len(plan["nle_segments"])
    inputs = "".join(f"[v{i}]" + (f"[a{i}]" if has_audio else "") for i in range(n))
    filters.append(
        inputs
        + f"concat=n={n}:v=1:a={int(has_audio)}[v]"
        + ("[a]" if has_audio else "")
    )
    if production_options is not None:
        from .production_delivery import treatment_filters
        filters = treatment_filters(plan, production_options, has_audio)
    command = [
        "ffmpeg",
        "-nostdin",
        "-v",
        "error",
        "-n",
        "-filter_complex_threads",
        "1",
        "-i",
        plan["source_video"]["path"],
        "-filter_complex",
        ";".join(filters),
        "-map",
        "[v]",
    ]
    if has_audio:
        command += ["-map", "[a]", "-c:a", "aac", "-b:a", "192k", "-ar", "48000"]
    if production_options is not None:
        from .production_delivery import export_arguments
        command += export_arguments(production_options)
    return command + [
        "-c:v",
        "libx264",
        "-preset",
        "fast",
        "-crf",
        "18",
        "-pix_fmt",
        "yuv420p",
        "-threads",
        "1",
        "-map_metadata",
        "-1",
        "-movflags",
        "+faststart",
        str(output),
    ]


def render_delivery(
    selection_path,
    output_dir,
    *,
    project_root,
    lifecycle_manifest,
    estimated_output_bytes,
):
    # No generic --force: a review or editor selection is not export intent.
    selection, pp, rp, plan = _context(selection_path, "ffmpeg")
    root = Path(project_root).expanduser().resolve()
    out = Path(output_dir).expanduser().resolve()
    if not root.is_dir() or not out.is_relative_to(root) or out == root:
        raise ContractError(
            "render output must be a new directory inside the existing project root"
        )
    if (
        isinstance(estimated_output_bytes, bool)
        or not isinstance(estimated_output_bytes, int)
        or estimated_output_bytes <= 0
    ):
        raise ContractError("positive output byte estimate required")
    manifest_ref = ref(lifecycle_manifest, "lifecycle manifest")
    lifecycle = _self_media_module("artifact_lifecycle")
    disk = lifecycle.preflight(
        argparse.Namespace(
            manifest=Path(lifecycle_manifest),
            workspace_root=root,
            now=None,
            output_class="large_or_unknown",
            estimated_output_bytes=estimated_output_bytes,
            core_root=None,
        )
    )
    if disk["status"] != "passed":
        raise ContractError(
            "existing lifecycle preflight blocked spoken export: "
            + json.dumps(disk, ensure_ascii=False)
        )
    info = probe_media_metadata(bound(plan["source_video"], "source video"))
    from .production_delivery import prepare_render_options, final_metadata_check, detection_findings, verify_working_copy_result
    production = prepare_render_options(plan, read(rp, "delivery request"), info)
    videos = [s for s in info.get("streams", []) if s.get("codec_type") == "video"]
    if not videos:
        raise ContractError("source video stream required")
    audio = any(s.get("codec_type") == "audio" for s in info.get("streams", []))
    out = _new_directory(out)
    candidate = out / "spoken-rough-cut.mp4"
    command = _render_command(plan, candidate, audio, production)
    # Validate again immediately before execution, including request and plan hashes.
    _context(selection_path, "ffmpeg")
    manifest = bound(manifest_ref, "lifecycle manifest")
    receipt = execute_ffmpeg_with_receipt(
        "render",
        command,
        [
            {"role": "source", "path": plan["source_video"]["path"]},
            {"role": "plan", "path": pp},
            {"role": "delivery_request", "path": rp},
            {"role": "delivery_selection", "path": selection_path},
            {"role": "lifecycle_manifest", "path": manifest},
        ],
        [{"role": "candidate", "path": candidate}],
        out / "render-receipt.json",
        capture_log_path=out / "render.log",
        timeout_s=3600,
        guard_resource=True,
    )
    actual = probe_media_metadata(candidate)
    length = float(actual.get("format", {}).get("duration", 0))
    expected = sum(s["duration_s"] for s in plan["nle_segments"])
    rate = str(videos[0].get("avg_frame_rate", "30/1")).split("/")
    fps = float(rate[0]) / float(rate[1]) if len(rate) == 2 and float(rate[1]) else 30.0
    tolerance = max(0.15, 2 / max(1, fps))
    rendered_audio = any(
        s.get("codec_type") == "audio" for s in actual.get("streams", [])
    )
    rendered_video = any(
        s.get("codec_type") == "video" for s in actual.get("streams", [])
    )
    if (
        not rendered_video
        or not math.isfinite(length)
        or abs(length - expected) > tolerance
        or rendered_audio != audio
    ):
        raise ContractError(
            "rendered media differs from selected spoken timeline/audio; preserve failed candidate"
        )
    final_metadata = final_metadata_check(actual, expected, production)
    if final_metadata["status"] == "failed":
        raise ContractError("final export profile mismatch: " + ", ".join(final_metadata["errors"]))
    working_copy_storage = verify_working_copy_result(plan, production, candidate)
    execute_ffmpeg_with_receipt(
        "technical_qa",
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "info",
            "-xerror",
            "-i",
            str(candidate),
            "-vf",
            "blackdetect=d=0.1:pix_th=0.02,freezedetect=n=0.003:d=2",
            "-f",
            "null",
            "-",
        ],
        [{"role": "candidate", "path": candidate}],
        [{"role": "decode_log", "path": out / "decode.log"}],
        out / "decode-receipt.json",
        capture_log_path=out / "decode.log",
        timeout_s=3600,
        guard_resource=True,
    )
    mapping = write(
        {
            "source_spoken_edit_plan": ref(pp, "spoken edit plan"),
            "segments": timeline_mapping(plan),
        },
        out / "source-timeline-map.json",
    )
    result = {
        "status": "rough_cut_rendered_pending_review",
        "route": "ffmpeg",
        "candidate_only": True,
        "selection": ref(selection_path, "delivery selection"),
        "candidate": ref(candidate, "candidate"),
        "timeline_map": ref(mapping, "timeline map"),
        "render_receipt": ref(out / "render-receipt.json", "render receipt"),
        "decode_receipt": ref(out / "decode-receipt.json", "decode receipt"),
        "outlet_id": receipt["outlet_id"],
        "video_rendered": True,
        "editor_opened": False,
        "packaging_complete": False,
        "publish_authorized": False,
        "human_review_completed": False,
        "boundary_review_windows": plan.get("boundary_review_windows", []),
        "audio_audition": "pending",
        "segment_leveling": plan.get("segment_leveling"),
        "rough_cut_audio_gate": "pending_measurement_of_this_render",
        "rough_cut_audio_accepted": False,
        "final_mix_loudness_check": "pending_after_packaging",
        "production_presets": production,
        "final_metadata_check": final_metadata,
        "working_copy_storage": working_copy_storage,
        "final_detector_findings": detection_findings(out / "decode.log"),
        "disk_preflight": disk,
    }
    write(result, out / "delivery-result.json")
    return result
