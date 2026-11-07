from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

from .contract_io import artifact_reference, atomic_write_json, read_json_object
from .errors import ContractError
from .music_timing_qa import (
    ALLIN1_SOURCE_COMMIT,
    PYMUSICLOOPER_SOURCE_COMMIT,
    adapt_allin1_result,
    adapt_librosa_bar_grid_result,
    build_music_timing_qa_report,
    evaluate_delta_lu,
    evaluate_ducking_pump_risk,
    recommend_mix_profile,
    snap_source_window,
    validate_music_structure_analysis,
    validate_music_timing_qa_report,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Analyze music boundaries and validate narration-first BGM timing."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    doctor = commands.add_parser("doctor")
    doctor.add_argument("--allin1-source")
    doctor.add_argument("--output")

    adapt = commands.add_parser("adapt-allin1")
    adapt.add_argument("--source", required=True)
    adapt.add_argument("--result", required=True)
    adapt.add_argument("--source-commit", default=ALLIN1_SOURCE_COMMIT)
    adapt.add_argument("--output", required=True)

    adapt_bar_grid = commands.add_parser("adapt-librosa-bar-grid")
    adapt_bar_grid.add_argument("--source", required=True)
    adapt_bar_grid.add_argument("--result", required=True)
    adapt_bar_grid.add_argument("--source-commit", default=PYMUSICLOOPER_SOURCE_COMMIT)
    adapt_bar_grid.add_argument("--output", required=True)

    validate_analysis = commands.add_parser("validate-analysis")
    validate_analysis.add_argument("--analysis", required=True)

    snap = commands.add_parser("snap-window")
    snap.add_argument("--analysis", required=True)
    snap.add_argument("--desired-start-s", required=True, type=float)
    snap.add_argument("--duration-s", required=True, type=float)
    snap.add_argument(
        "--mode",
        choices=("phrase_preferred", "downbeat", "beat"),
        default="phrase_preferred",
    )
    snap.add_argument("--tolerance-s", type=float)
    snap.add_argument("--output", required=True)

    profile = commands.add_parser("recommend-profile")
    profile.add_argument("--intent-segment", required=True)
    profile.add_argument("--functional-role", required=True)
    profile.add_argument("--output", required=True)

    delta = commands.add_parser("evaluate-delta")
    delta.add_argument("--dialogue-lufs", required=True, type=float)
    delta.add_argument("--music-lufs", required=True, type=float)
    delta.add_argument("--profile", required=True)
    delta.add_argument("--output", required=True)

    ducking = commands.add_parser("evaluate-ducking")
    ducking.add_argument("--silence-gaps", required=True)
    ducking.add_argument("--programme-duration-s", required=True, type=float)
    ducking.add_argument("--release-ms", required=True, type=int)
    ducking.add_argument("--output", required=True)

    report = commands.add_parser("build-report")
    report.add_argument("--source", required=True)
    report.add_argument("--cue-plan", required=True)
    report.add_argument("--segments", required=True)
    report.add_argument("--ducking", required=True)
    report.add_argument("--output", required=True)

    validate_report = commands.add_parser("validate-report")
    validate_report.add_argument("--report", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = _run(args)
    except ContractError as exc:
        print(f"contract error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0


def _run(args: argparse.Namespace) -> dict[str, object]:
    if args.command == "doctor":
        payload = _doctor(args.allin1_source)
        if args.output:
            atomic_write_json(payload, args.output)
        return payload
    if args.command == "adapt-allin1":
        payload = adapt_allin1_result(
            args.source,
            read_json_object(args.result, "All-In-One result"),
            source_commit=args.source_commit,
        )
        output = atomic_write_json(payload, args.output)
        return _written(payload, output, "analysis_sha256")
    if args.command == "adapt-librosa-bar-grid":
        payload = adapt_librosa_bar_grid_result(
            args.source,
            read_json_object(args.result, "librosa bar-grid result"),
            adapted_source_commit=args.source_commit,
        )
        output = atomic_write_json(payload, args.output)
        return _written(payload, output, "analysis_sha256")
    if args.command == "validate-analysis":
        payload = read_json_object(args.analysis, "music structure analysis")
        validate_music_structure_analysis(payload)
        return {
            "status": "valid",
            "schema": payload["schema"],
            "analysis_sha256": payload["analysis_sha256"],
        }
    if args.command == "snap-window":
        payload = snap_source_window(
            read_json_object(args.analysis, "music structure analysis"),
            desired_start_s=args.desired_start_s,
            duration_s=args.duration_s,
            mode=args.mode,
            tolerance_s=args.tolerance_s,
        )
        output = atomic_write_json(payload, args.output)
        return {"status": payload["status"], "path": str(output), **payload}
    if args.command == "recommend-profile":
        payload = recommend_mix_profile(
            read_json_object(args.intent_segment, "music intent segment"),
            functional_role=args.functional_role,
        )
        output = atomic_write_json(payload, args.output)
        return {"status": "completed", "path": str(output), **payload}
    if args.command == "evaluate-delta":
        payload = evaluate_delta_lu(
            dialogue_lufs=args.dialogue_lufs,
            music_lufs=args.music_lufs,
            profile=read_json_object(args.profile, "music mix profile"),
        )
        output = atomic_write_json(payload, args.output)
        return {"path": str(output), **payload}
    if args.command == "evaluate-ducking":
        document = read_json_object(args.silence_gaps, "silence gap evidence")
        gaps = document.get("gaps")
        if not isinstance(gaps, list):
            raise ContractError("silence gap evidence requires a gaps array")
        payload = evaluate_ducking_pump_risk(
            gaps,
            programme_duration_s=args.programme_duration_s,
            release_ms=args.release_ms,
        )
        output = atomic_write_json(payload, args.output)
        return {"path": str(output), **payload}
    if args.command == "build-report":
        segment_document = read_json_object(args.segments, "music QA segment rows")
        segment_rows = segment_document.get("segments")
        if not isinstance(segment_rows, list):
            raise ContractError("music QA segment rows require a segments array")
        ducking_document = read_json_object(args.ducking, "ducking QA")
        ducking = ducking_document.get("ducking", ducking_document)
        if not isinstance(ducking, dict):
            raise ContractError("ducking QA must be an object")
        payload = build_music_timing_qa_report(
            source_reference=artifact_reference(args.source, "QA source"),
            cue_plan_reference=artifact_reference(args.cue_plan, "QA cue plan"),
            segment_rows=segment_rows,
            ducking=ducking,
        )
        output = atomic_write_json(payload, args.output)
        return _written(payload, output, "report_sha256")
    payload = read_json_object(args.report, "music timing QA report")
    validate_music_timing_qa_report(payload)
    return {
        "status": "valid",
        "schema": payload["schema"],
        "report_sha256": payload["report_sha256"],
    }


def _doctor(allin1_source: str | None) -> dict[str, object]:
    source_status: dict[str, object] = {
        "status": "not_configured",
        "expected_source_commit": ALLIN1_SOURCE_COMMIT,
    }
    if allin1_source:
        root = Path(allin1_source).expanduser().resolve()
        source_status = {
            "status": (
                "source_present_commit_verification_required"
                if (root / "src" / "allin1" / "analyze.py").is_file()
                else "invalid_source_root"
            ),
            "path": str(root),
            "expected_source_commit": ALLIN1_SOURCE_COMMIT,
        }
    librosa_available = importlib.util.find_spec("librosa") is not None
    allin1_available = importlib.util.find_spec("allin1") is not None
    return {
        "schema": "video_creation_pipeline.music_timing_qa_doctor.v1",
        "status": "ready_baseline" if librosa_available else "blocked_optional_runtime",
        "providers": {
            "librosa_bar_grid": {
                "available_in_current_python": librosa_available,
                "adapted_source": "PyMusicLooper fixed-source candidate scoring",
                "adapted_source_commit": PYMUSICLOOPER_SOURCE_COMMIT,
                "automatic_fallback": False,
            },
            "allin1": {
                "available_in_current_python": allin1_available,
                "source": source_status,
                "required_source_commit": ALLIN1_SOURCE_COMMIT,
                "automatic_fallback": False,
                "status": "available" if allin1_available else "optional_blocked",
            },
        },
        "authority": {
            "analysis_is_candidate_evidence": True,
            "semantic_timeline_may_move": False,
            "automatic_provider_fallback": False,
        },
    }


def _written(
    payload: dict[str, object], output: Path, hash_field: str
) -> dict[str, object]:
    return {
        "schema": payload["schema"],
        "status": payload["status"],
        "path": str(output),
        hash_field: payload[hash_field],
    }


if __name__ == "__main__":
    raise SystemExit(main())
