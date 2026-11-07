from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .contract_io import atomic_write_json, read_json_object
from .errors import ContractError
from .editing_presets import POLICY
from .audio_edit_quality import inspect_pcm_levels
from .spoken_rough_cut import (
    AGENT_DECISIONS_SCHEMA,
    DEFAULT_NATURAL_PACING_POLICY,
    JUDGE_PACKET_SCHEMA,
    LEGACY_SPOKEN_EDIT_PLAN_SCHEMA,
    SPOKEN_EDIT_PLAN_SCHEMA,
    build_spoken_edit_decision_template,
    build_spoken_edit_plan,
    prepare_spoken_edit_judge_packet,
    restore_spoken_edit_decision,
    validate_spoken_edit_agent_decisions,
    validate_spoken_edit_judge_packet,
    validate_spoken_edit_plan,
    videocut_spoken_doctor,
)
from .spoken_delivery import (
    SELECTION_SCHEMA, prepare_delivery, render_delivery, select_delivery,
    validate_delivery_selection,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Prepare and compile a candidate-only talking-head rough cut via fixed "
            "videocut-kit source."
        )
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("pacing-policy", help="Show the active creator pause preset; no media processing")
    commands.add_parser("editing-policy", help="Show the ten editing presets; no media processing")
    commands.add_parser("production-policy", help="Source-first production presets and project profiles")
    inspect = commands.add_parser("inspect-source", help="Metadata and original source-time-range review; no video copies")
    inspect.add_argument("--source-video", required=True)
    inspect.add_argument("--project-type", choices=("spoken", "life", "tutorial"), default="spoken")
    inspect.add_argument("--checks", help="JSON object with ranges: [{start,end,checks}]")
    inspect.add_argument("--sample-cadence", action="store_true", help="Read at most three bounded packet samples; no decoding/transcode")
    inspect.add_argument("--output-dir", required=True)
    copy_check = commands.add_parser("assess-working-copy", help="Assess exceptional storage reduction; never render or delete")
    copy_check.add_argument("--source-video", required=True)
    copy_check.add_argument("--assessment", required=True)
    copy_check.add_argument("--output", required=True)
    treatment = commands.add_parser("review-treatment", help="Validate source/candidate comparison and return accepted/fallback selection")
    treatment.add_argument("--review", required=True)
    treatment.add_argument("--output", required=True)
    levels = commands.add_parser("inspect-audio-levels", help="Read explicit speech windows from local PCM; no gain applied")
    levels.add_argument("--audio", required=True)
    levels.add_argument("--windows", required=True, help="JSON object with speech_windows: [{start,end}]")
    levels.add_argument("--output", required=True)
    segments = commands.add_parser("inspect-segment-levels", help="Measure every spoken cut on dialogue-only PCM; generate bounded gain candidates")
    segments.add_argument("--audio", required=True)
    segments.add_argument("--spec", required=True, help="Bound source/measured media, phase, segments and source-clock speech_windows")
    segments.add_argument("--output", required=True)
    review_template = commands.add_parser("segment-leveling-template", help="Create an unapproved per-segment gain review from measured evidence")
    review_template.add_argument("--report", required=True)
    review_template.add_argument("--output", required=True)
    audio_gate = commands.add_parser("check-segment-leveling", help="Require before/after speech measurements, applied gains and listening evidence")
    audio_gate.add_argument("--review", required=True)
    audio_gate.add_argument("--measured-media", required=True, help="Current edited media or saved editor snapshot")
    audio_gate.add_argument("--output", required=True)

    doctor = commands.add_parser("doctor")
    doctor.add_argument("--videocut-source")

    prepare = commands.add_parser("prepare")
    prepare.add_argument("--source-video", required=True)
    prepare.add_argument("--tokens", required=True)
    prepare.add_argument("--silence")
    prepare.add_argument("--reference-script")
    prepare.add_argument("--videocut-source", required=True)
    prepare.add_argument("--output-dir", required=True)

    template = commands.add_parser("decision-template")
    template.add_argument("--packet", required=True)
    template.add_argument("--output", required=True)

    compile_plan = commands.add_parser("compile")
    compile_plan.add_argument("--packet", required=True)
    compile_plan.add_argument("--decisions", required=True)
    compile_plan.add_argument("--output-dir", required=True)

    select = commands.add_parser("select-delivery", help="Choose existing delivery from explicit requirements; no export")
    select.add_argument("--plan", required=True)
    select.add_argument("--request", required=True)
    select.add_argument("--output", required=True)

    delivery = commands.add_parser("prepare-delivery", help="Create source review page or existing editor job; no video rendering")
    delivery.add_argument("--selection", required=True)
    delivery.add_argument("--output-dir", required=True)
    delivery.add_argument("--vectcut-source")
    delivery.add_argument("--width", type=int)
    delivery.add_argument("--height", type=int)
    delivery.add_argument("--fps", type=int)
    delivery.add_argument("--project-id", default="")
    delivery.add_argument("--sequence-id", default="")
    delivery.add_argument("--playhead", type=float, default=0.0)

    render = commands.add_parser("render-delivery", help="Explicit original-audio rough-cut export through guarded FFmpeg")
    render.add_argument("--selection", required=True)
    render.add_argument("--output-dir", required=True)
    render.add_argument("--project-root", required=True)
    render.add_argument("--lifecycle-manifest", required=True)
    render.add_argument("--estimated-output-bytes", type=int, required=True)

    restore = commands.add_parser("restore-decision", help="Restore one row into a new candidate version; no rendering")
    restore.add_argument("--packet", required=True)
    restore.add_argument("--decisions", required=True)
    restore.add_argument("--restore-from", required=True)
    restore.add_argument("--decision-id", required=True)
    restore.add_argument("--output", required=True)

    validate = commands.add_parser("validate")
    validate.add_argument("--artifact", required=True)
    validate.add_argument("--packet")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = _execute(args)
    except (ContractError, ValueError, OSError) as exc:
        print(f"contract error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0


def _execute(args: argparse.Namespace) -> dict[str, object]:
    if args.command == "production-policy":
        from .production_presets import POLICY as PRODUCTION_POLICY
        return PRODUCTION_POLICY
    if args.command == "inspect-source":
        from .production_presets import inspect_source, prepare_source_review
        out = Path(args.output_dir).expanduser().resolve()
        if out.exists():
            raise ContractError("source inspection requires a new directory")
        checks = read_json_object(args.checks, "review checks")["ranges"] if args.checks else None
        audit = inspect_source(args.source_video, project_type=args.project_type, checks=checks, sample_timestamps=args.sample_cadence)
        path = atomic_write_json(audit, out / "production-audit.json")
        return prepare_source_review(path, out / "source-review")
    if args.command == "assess-working-copy":
        from .production_presets import assess_working_copy, media_facts
        from .execution_receipts import probe_media_metadata
        if Path(args.output).exists():
            raise ContractError("working-copy decision must be a new file")
        assessment = read_json_object(args.assessment, "working-copy assessment")
        result = assess_working_copy(args.source_video, media_facts(probe_media_metadata(args.source_video))["duration_s"], assessment)
        atomic_write_json(result, args.output)
        return result
    if args.command == "review-treatment":
        from .production_presets import restoration_decision
        from .contract_io import validate_artifact_reference
        if Path(args.output).exists():
            raise ContractError("treatment decision must be a new file")
        review = read_json_object(args.review, "restoration review")
        result = restoration_decision(validate_artifact_reference(review["source"], "source"), validate_artifact_reference(review["candidate"], "candidate"), review)
        atomic_write_json(result, args.output)
        return result
    if args.command == "editing-policy":
        return {"editing_policy": POLICY, "candidate_only": True}
    if args.command == "inspect-audio-levels":
        if Path(args.output).exists():
            raise ContractError('inspection output must be a new file')
        windows = read_json_object(args.windows, 'speech windows').get('speech_windows')
        result = inspect_pcm_levels(args.audio, windows)
        atomic_write_json(result, args.output)
        return result
    if args.command in ("inspect-segment-levels", "check-segment-leveling", "segment-leveling-template"):
        from .segment_leveling import inspect_segments, validate_leveling, load_report
        from .contract_io import artifact_reference
        if Path(args.output).exists():
            raise ContractError("segment audio evidence must be a new file")
        if args.command == "inspect-segment-levels":
            result = inspect_segments(args.audio, read_json_object(args.spec, "segment level spec"))
        elif args.command == "segment-leveling-template":
            reference = artifact_reference(args.report, "before segment levels")
            report = load_report(reference)
            if report['phase'] != 'before':
                raise ContractError('before report required for gain review template')
            result = dict(before_ref=reference, after_ref=None, clip_gains=report['candidate_clip_gains'],
                          joins_checked=False, expression_preserved=False, evidence='')
        else:
            result = validate_leveling(read_json_object(args.review, "segment leveling review"),
                require_after=True, measured_media=artifact_reference(args.measured_media, "current media"))
        atomic_write_json(result, args.output)
        return result
    if args.command == "pacing-policy":
        return {"pacing_policy": DEFAULT_NATURAL_PACING_POLICY, "candidate_only": True}
    if args.command == "doctor":
        return videocut_spoken_doctor(args.videocut_source)
    if args.command == "prepare":
        packet = prepare_spoken_edit_judge_packet(
            args.source_video,
            args.tokens,
            args.output_dir,
            args.videocut_source,
            silence_path=args.silence,
            reference_script_path=args.reference_script,
        )
        return {
            "status": "judge_packet_ready",
            "packet": str(
                Path(args.output_dir).expanduser().resolve()
                / "spoken-edit-judge-packet.json"
            ),
            "packet_sha256": packet["packet_sha256"],
            "candidate_count": packet["candidate_count"],
            "candidate_only": True,
        }
    if args.command == "decision-template":
        payload = build_spoken_edit_decision_template(args.packet)
        output = atomic_write_json(payload, args.output)
        return {
            "status": payload["status"],
            "output": str(output),
            "decision_count": len(payload["decisions"]),
            "next_action": "agent_reads_fulltext_and_resolves_review_required",
        }
    if args.command == "compile":
        plan = build_spoken_edit_plan(
            args.packet,
            args.decisions,
            args.output_dir,
        )
        return {
            "status": "spoken_edit_plan_ready",
            "plan": str(
                Path(args.output_dir).expanduser().resolve() / "spoken-edit-plan.json"
            ),
            "plan_sha256": plan["plan_sha256"],
            "summary": plan["summary"],
            "rough_cut_audio_gate": plan["rough_cut_audio_gate"],
            "candidate_only": True,
            "ffmpeg_executed": False,
            "next_action": plan["next_action"],
        }

    if args.command == "select-delivery":
        if Path(args.output).exists():
            raise ContractError("selection output must be a new file")
        result = select_delivery(args.plan, args.request)
        atomic_write_json(result, args.output)
        return result
    if args.command == "prepare-delivery":
        values = (args.width, args.height, args.fps)
        if any(v is not None for v in values) and not all(v is not None for v in values):
            raise ContractError("explicit canvas requires width, height and fps together")
        canvas = dict(zip(("width", "height", "fps"), values)) if all(v is not None for v in values) else None
        return prepare_delivery(args.selection, args.output_dir, vectcut_source=args.vectcut_source,
                                canvas=canvas, project_id=args.project_id,
                                sequence_id=args.sequence_id, playhead_s=args.playhead)
    if args.command == "render-delivery":
        return render_delivery(args.selection, args.output_dir, project_root=args.project_root,
                               lifecycle_manifest=args.lifecycle_manifest,
                               estimated_output_bytes=args.estimated_output_bytes)

    if args.command == "restore-decision":
        output = Path(args.output).expanduser().resolve()
        if output.exists():
            raise ContractError("restore output must be a new version; existing files are preserved")
        restored = restore_spoken_edit_decision(
            args.packet, args.decisions, args.restore_from, args.decision_id
        )
        atomic_write_json(restored, output)
        return {
            "status": "decision_restored", "output": str(output),
            "decision_id": args.decision_id,
            "current_version": str(Path(args.decisions).expanduser().resolve()),
            "restore_from": str(Path(args.restore_from).expanduser().resolve()),
            "decisions_sha256": restored["decisions_sha256"],
            "candidate_only": True, "ffmpeg_executed": False,
        }

    artifact = read_json_object(args.artifact, "spoken rough-cut artifact")
    schema = artifact.get("schema")
    if schema == JUDGE_PACKET_SCHEMA:
        validate_spoken_edit_judge_packet(artifact)
    elif schema == AGENT_DECISIONS_SCHEMA:
        if not args.packet:
            raise ContractError("agent decisions validation requires --packet")
        packet = read_json_object(args.packet, "spoken edit judge packet")
        validate_spoken_edit_agent_decisions(artifact, packet)
    elif schema in {SPOKEN_EDIT_PLAN_SCHEMA, LEGACY_SPOKEN_EDIT_PLAN_SCHEMA}:
        validate_spoken_edit_plan(artifact)
    elif schema == SELECTION_SCHEMA:
        validate_delivery_selection(artifact)
    else:
        raise ContractError("unsupported spoken rough-cut artifact schema")
    return {"valid": True, "schema": schema, "artifact": args.artifact}


if __name__ == "__main__":
    raise SystemExit(main())
