from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .contract_io import read_json_object
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
        print(json.dumps({"status": "blocked", "error": str(exc)}), file=sys.stderr)
        return 2
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
