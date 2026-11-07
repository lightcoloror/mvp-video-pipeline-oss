from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .contract_io import read_json_object
from .errors import ContractError
from .reframe_detectors import (
    SMART_REFRAME_EVIDENCE_SCHEMA,
    adapt_vkp_reframe_evidence,
    build_smart_reframe_evidence,
    detect_faces_with_mediapipe,
    detect_faces_with_opencv_haar,
    detect_scene_boundaries,
    detect_static_attention_with_libvips,
    select_smart_reframe_track,
    smart_reframe_doctor,
    track_smart_reframe_evidence_with_norfair,
    validate_smart_reframe_evidence,
    write_smart_reframe_evidence,
)
from .reframe_qa import (
    SMART_REFRAME_QA_SCHEMA,
    build_smart_reframe_qa,
    validate_smart_reframe_qa,
    write_smart_reframe_qa,
)
from .smart_reframe import (
    SMART_REFRAME_PLAN_SCHEMA,
    SMART_REFRAME_RENDER_RECEIPT_SCHEMA,
    SMART_REFRAME_REQUEST_SCHEMA,
    build_smart_reframe_plan,
    build_smart_reframe_request,
    render_smart_reframe_candidate,
    render_smart_reframe_debug_overlay,
    validate_smart_reframe_plan,
    validate_smart_reframe_render_receipt,
    validate_smart_reframe_request,
    write_smart_reframe_contract,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build auditable candidate-only smart reframe plans."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    doctor = commands.add_parser("doctor")
    doctor.add_argument("--mediapipe-model")
    doctor.add_argument("--font")

    request = commands.add_parser("create-request")
    request.add_argument("--profile", required=True)
    request.add_argument("--media-role", required=True)
    request.add_argument("--project-type", choices=("spoken", "life", "tutorial"))
    request.add_argument("--composition-review")
    request.add_argument("--fallback", action="append")
    request.add_argument("--output", required=True)

    evidence = commands.add_parser("build-evidence")
    evidence.add_argument("--source", required=True)
    evidence.add_argument("--spec", required=True)
    evidence.add_argument("--output", required=True)

    scenes = commands.add_parser("detect-scenes")
    scenes.add_argument("--source", required=True)
    scenes.add_argument(
        "--detector", choices=("adaptive", "content"), default="adaptive"
    )
    scenes.add_argument("--threshold", type=float)
    scenes.add_argument("--output", required=True)

    faces = commands.add_parser("detect-faces")
    faces.add_argument("--source", required=True)
    faces.add_argument("--model", required=True)
    faces.add_argument("--sample-interval-s", type=float, default=0.5)
    faces.add_argument("--minimum-confidence", type=float, default=0.5)
    faces.add_argument("--output", required=True)

    haar = commands.add_parser("detect-faces-haar")
    haar.add_argument("--source", required=True)
    haar.add_argument("--sample-interval-s", type=float, default=0.5)
    haar.add_argument("--scale-factor", type=float, default=1.1)
    haar.add_argument("--min-neighbors", type=int, default=5)
    haar.add_argument("--output", required=True)

    smartcrop = commands.add_parser("static-smartcrop")
    smartcrop.add_argument("--source", required=True)
    smartcrop.add_argument("--target-width", type=int, required=True)
    smartcrop.add_argument("--target-height", type=int, required=True)
    smartcrop.add_argument("--output", required=True)

    vkp = commands.add_parser("adapt-vkp")
    vkp.add_argument("--source", required=True)
    vkp.add_argument("--projection", required=True)
    vkp.add_argument("--output", required=True)

    track = commands.add_parser("track-evidence")
    track.add_argument("--evidence", required=True)
    track.add_argument("--output", required=True)
    track.add_argument("--scene-evidence")

    select_track = commands.add_parser("select-track")
    select_track.add_argument("--evidence", required=True)
    select_track.add_argument("--track-id", required=True)
    select_track.add_argument("--output", required=True)

    build = commands.add_parser("build")
    build.add_argument("--source", required=True)
    build.add_argument("--request", required=True)
    build.add_argument("--evidence", action="append", default=[])
    build.add_argument("--output", required=True)

    qa = commands.add_parser("qa")
    qa.add_argument("--plan", required=True)
    qa.add_argument("--render-receipt")
    qa.add_argument("--output", required=True)

    render = commands.add_parser("render-candidate")
    render.add_argument("--plan", required=True)
    render.add_argument("--output", required=True)
    render.add_argument("--work-dir", required=True)
    render.add_argument("--timeout-s", type=float, default=300.0)

    debug = commands.add_parser("render-debug-overlay")
    debug.add_argument("--plan", required=True)
    debug.add_argument("--output", required=True)
    debug.add_argument("--work-dir", required=True)
    debug.add_argument("--timeout-s", type=float, default=300.0)
    debug.add_argument("--annotator", choices=("crop", "supervision"), default="crop")
    debug.add_argument("--font")
    debug.add_argument("--privacy-zones")
    debug.add_argument("--diagnostic-evidence")

    validate = commands.add_parser("validate")
    validate.add_argument("--artifact", required=True)

    read = commands.add_parser("read-artifact")
    read.add_argument("--artifact", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        payload = _run(args)
    except (ContractError, OSError, TypeError, ValueError) as exc:
        print(
            json.dumps({"status": "blocked", "error": str(exc)}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


def _run(args: argparse.Namespace) -> dict[str, object]:
    if args.command == "doctor":
        return smart_reframe_doctor(
            mediapipe_model_path=args.mediapipe_model, font_path=args.font
        )
    if args.command == "create-request":
        request = build_smart_reframe_request(
            profile_id=args.profile,
            media_role=args.media_role,
            production_project_type=args.project_type,
            composition_review_path=args.composition_review,
            fallback_order=args.fallback,
        )
        path = write_smart_reframe_contract(request, args.output)
        return _written("candidate_request_ready", path, request["request_sha256"])
    if args.command == "build-evidence":
        spec = read_json_object(args.spec, "smart reframe evidence spec")
        provider = spec.get("provider")
        observations = spec.get("observations")
        if not isinstance(provider, dict) or not isinstance(observations, list):
            raise ContractError("evidence spec requires provider and observations")
        evidence = build_smart_reframe_evidence(
            args.source, provider=provider, observations=observations
        )
        path = write_smart_reframe_evidence(evidence, args.output)
        return _written("candidate_evidence_ready", path, evidence["evidence_sha256"])
    if args.command == "detect-scenes":
        evidence = detect_scene_boundaries(
            args.source, detector=args.detector, threshold=args.threshold
        )
        path = write_smart_reframe_evidence(evidence, args.output)
        return _written("scene_evidence_ready", path, evidence["evidence_sha256"])
    if args.command == "detect-faces":
        evidence = detect_faces_with_mediapipe(
            args.source,
            args.model,
            sample_interval_s=args.sample_interval_s,
            minimum_confidence=args.minimum_confidence,
        )
        path = write_smart_reframe_evidence(evidence, args.output)
        return _written("face_evidence_ready", path, evidence["evidence_sha256"])
    if args.command == "detect-faces-haar":
        evidence = detect_faces_with_opencv_haar(
            args.source,
            sample_interval_s=args.sample_interval_s,
            scale_factor=args.scale_factor,
            min_neighbors=args.min_neighbors,
        )
        path = write_smart_reframe_evidence(evidence, args.output)
        return _written(
            "opencv_haar_face_evidence_ready", path, evidence["evidence_sha256"]
        )
    if args.command == "static-smartcrop":
        evidence = detect_static_attention_with_libvips(
            args.source,
            target_width=args.target_width,
            target_height=args.target_height,
        )
        path = write_smart_reframe_evidence(evidence, args.output)
        return _written("attention_evidence_ready", path, evidence["evidence_sha256"])
    if args.command == "adapt-vkp":
        evidence = adapt_vkp_reframe_evidence(args.source, args.projection)
        path = write_smart_reframe_evidence(evidence, args.output)
        return _written("vkp_evidence_adapted", path, evidence["evidence_sha256"])
    if args.command == "track-evidence":
        evidence = read_json_object(args.evidence, "smart reframe evidence")
        scenes = (
            read_json_object(args.scene_evidence, "scene evidence")
            if args.scene_evidence
            else None
        )
        tracked = track_smart_reframe_evidence_with_norfair(
            evidence, scene_evidence=scenes
        )
        path = write_smart_reframe_evidence(tracked, args.output)
        return _written("tracked_evidence_ready", path, tracked["evidence_sha256"])
    if args.command == "select-track":
        evidence = read_json_object(args.evidence, "smart reframe evidence")
        selected = select_smart_reframe_track(evidence, args.track_id)
        path = write_smart_reframe_evidence(selected, args.output)
        return {
            **_written("explicit_track_selected", path, selected["evidence_sha256"]),
            "track_id": args.track_id,
        }
    if args.command == "build":
        plan = build_smart_reframe_plan(
            args.source, args.request, evidence_paths=args.evidence
        )
        path = write_smart_reframe_contract(plan, args.output)
        return {
            **_written("candidate_plan_ready", path, plan["plan_sha256"]),
            "readiness": plan["summary"]["status"],
            "strategies": [row["strategy"] for row in plan["shots"]],
        }
    if args.command == "qa":
        qa = build_smart_reframe_qa(args.plan, render_receipt_path=args.render_receipt)
        path = write_smart_reframe_qa(qa, args.output)
        return {
            **_written("qa_complete", path, qa["qa_sha256"]),
            "qa_status": qa["summary"]["status"],
        }
    if args.command == "render-candidate":
        receipt = render_smart_reframe_candidate(
            args.plan,
            args.output,
            args.work_dir,
            timeout_s=args.timeout_s,
        )
        return {
            "status": receipt["status"],
            "receipt": str(
                Path(args.work_dir).expanduser().resolve()
                / "smart-reframe-render-receipt.json"
            ),
            "candidate_video": receipt["candidate_video"]["path"],
            "receipt_sha256": receipt["receipt_sha256"],
            "candidate_only": True,
        }
    if args.command == "render-debug-overlay":
        result = render_smart_reframe_debug_overlay(
            args.plan,
            args.output,
            args.work_dir,
            timeout_s=args.timeout_s,
            annotator=args.annotator,
            font_path=args.font,
            privacy_zones_path=args.privacy_zones,
            diagnostic_evidence_path=args.diagnostic_evidence,
        )
        return {
            "status": result["status"],
            "debug_video": result["debug_video"]["path"],
            "ffmpeg_receipt": result["ffmpeg_receipt"]["path"],
            "ffmpeg_receipt_sha256": result["ffmpeg_receipt_sha256"],
            "candidate_only": True,
            "diagnostic_receipt": str(
                Path(args.work_dir).resolve() / "smart-reframe-visual-qa-receipt.json"
            )
            if args.annotator == "supervision"
            else None,
            "contact_sheet": result.get("contact_sheet"),
        }
    artifact_path = Path(args.artifact).expanduser().resolve()
    artifact = read_json_object(artifact_path, "smart reframe artifact")
    schema = _validate_artifact(artifact)
    digest = next(
        str(artifact[field])
        for field in (
            "request_sha256",
            "evidence_sha256",
            "plan_sha256",
            "qa_sha256",
            "receipt_sha256",
        )
        if field in artifact
    )
    if args.command == "read-artifact":
        return {
            "status": "read_only_summary",
            "schema": schema,
            "artifact": str(artifact_path),
            "sha256": digest,
            "candidate_only": True,
        }
    return {
        "status": "valid_candidate_only",
        "schema": schema,
        "artifact": str(artifact_path),
        "sha256": digest,
        "candidate_only": True,
    }


def _validate_artifact(payload: dict[str, object]) -> str:
    schema = str(payload.get("schema") or "")
    if schema == SMART_REFRAME_REQUEST_SCHEMA:
        validate_smart_reframe_request(payload)
    elif schema == SMART_REFRAME_EVIDENCE_SCHEMA:
        validate_smart_reframe_evidence(payload)
    elif schema == SMART_REFRAME_PLAN_SCHEMA:
        validate_smart_reframe_plan(payload)
    elif schema == SMART_REFRAME_QA_SCHEMA:
        validate_smart_reframe_qa(payload)
    elif schema == SMART_REFRAME_RENDER_RECEIPT_SCHEMA:
        validate_smart_reframe_render_receipt(payload)
    elif schema == "video_creation_pipeline.smart_reframe_visual_qa_receipt.v1":
        from .reframe_visual_qa import validate_visual_qa_receipt

        validate_visual_qa_receipt(payload)
    else:
        raise ContractError("unsupported smart reframe artifact schema")
    return schema


def _written(status: str, path: Path, digest: str) -> dict[str, object]:
    return {
        "status": status,
        "artifact": str(path),
        "sha256": digest,
        "candidate_only": True,
        "automatic_publish": False,
    }


if __name__ == "__main__":
    raise SystemExit(main())
