from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .contract_io import read_json_object, atomic_write_json
from .errors import ContractError
from .video_use_review_handoff import (
    build_video_use_review_handoff,
    validate_video_use_review_handoff,
    video_use_review_doctor,
    write_video_use_review_handoff,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build a candidate-only video-use review handoff."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    doctor = commands.add_parser("doctor")
    doctor.add_argument("--source-root")
    build = commands.add_parser("build")
    build.add_argument("--source-root", required=True)
    build.add_argument("--candidate-video", required=True)
    build.add_argument("--packaging-plan", required=True)
    build.add_argument("--window-radius-s", type=float, default=1.5)
    build.add_argument("--output", required=True)
    validate = commands.add_parser("validate")
    validate.add_argument("--artifact", required=True)
    execute = commands.add_parser("execute", help="Generate bounded review artifacts; human review stays pending")
    execute.add_argument("--handoff", required=True)
    execute.add_argument("--context", required=True)
    execute.add_argument("--cache", required=True)
    execute.add_argument("--output", required=True)
    execute.add_argument("--font", required=True)
    execute.add_argument("--only", help="Comma-separated review window ids")
    suggest = commands.add_parser("suggest", help="Candidate-only timing/repetition suggestions")
    suggest.add_argument("--handoff", required=True)
    suggest.add_argument("--context", required=True)
    suggest.add_argument("--output", required=True)
    edit = commands.add_parser("prepare-edit", help="Project explicit selected suggestions into reversible EDL")
    edit.add_argument("--suggestions", required=True)
    edit.add_argument("--selection", required=True)
    edit.add_argument("--output", required=True)
    apply = commands.add_parser("apply-edit", help="Apply hash-approved edit and review adjacent seams")
    apply.add_argument("--edit-plan", required=True)
    apply.add_argument("--approval", required=True)
    apply.add_argument("--cache", required=True)
    apply.add_argument("--output-dir", required=True)
    apply.add_argument("--font", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "doctor":
            payload = video_use_review_doctor(args.source_root)
        elif args.command == "build":
            handoff = build_video_use_review_handoff(
                args.source_root,
                args.candidate_video,
                args.packaging_plan,
                window_radius_s=args.window_radius_s,
            )
            output = write_video_use_review_handoff(handoff, args.output)
            payload = {
                "status": "candidate_review_handoff_ready",
                "artifact": str(output),
                "handoff_sha256": handoff["handoff_sha256"],
                "candidate_only": True,
            }
        elif args.command == "execute":
            from .video_use_review_execution import execute_windows
            report = execute_windows(args.handoff, args.context, args.cache, args.output, args.font,
                                     only=args.only.split(',') if args.only else None)
            payload = {"status":"candidate_review_artifacts_ready", "output":args.output,
                       "rendered":sum(w['disposition']=='rendered' for w in report['windows']),
                       "reused":sum(w['disposition']=='reused' for w in report['windows']),"human_review":"pending"}
        elif args.command == "suggest":
            from .video_use_edit_loop import build_suggestions
            result=build_suggestions(args.handoff,args.context)
            atomic_write_json(result,args.output)
            payload={"status":"candidate_suggestions_ready","output":args.output,"count":len(result['suggestions'])}
        elif args.command == "prepare-edit":
            from .video_use_edit_loop import prepare_edit
            result=prepare_edit(args.suggestions,args.selection)
            atomic_write_json(result,args.output)
            payload={"status":"awaiting_hash_bound_approval","output":args.output,"duration":result['duration']}
        elif args.command == "apply-edit":
            from .video_use_edit_loop import apply_edit
            result=apply_edit(args.edit_plan,args.approval,args.cache,args.output_dir,args.font)
            payload={"status":"revised_candidate_ready","output":args.output_dir,"invocation":result['invocation'],
                     "rendered":result['rendered_segments'],"reused":result['reused_segments'],"human_review":"pending"}
        else:
            handoff = read_json_object(args.artifact, "video-use review handoff")
            validate_video_use_review_handoff(handoff)
            payload = {
                "status": "valid_candidate_only",
                "artifact": str(Path(args.artifact).resolve()),
                "handoff_sha256": handoff["handoff_sha256"],
                "candidate_only": True,
            }
    except (ContractError, OSError, ValueError, TypeError) as exc:
        detail = exc.as_dict() if callable(getattr(exc, "as_dict", None)) else {"status": "blocked", "error": str(exc)}
        print(json.dumps(detail, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
