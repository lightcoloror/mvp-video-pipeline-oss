from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .contract_io import read_json_object
from .errors import ContractError
from .seedance_prompt_handoff import (
    build_seedance_prompt_handoff,
    seedance_prompt_doctor,
    validate_seedance_prompt_handoff,
    write_seedance_prompt_handoff,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build a candidate-only Seedance prompt handoff."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    doctor = commands.add_parser("doctor")
    doctor.add_argument("--skill-root")
    build = commands.add_parser("build")
    build.add_argument("--skill-root", required=True)
    build.add_argument("--shot-unit", required=True)
    build.add_argument("--segment-limit-s", type=float, default=15.0)
    build.add_argument("--content-visual-routing-plan")
    build.add_argument("--visual-style-profile")
    build.add_argument("--output", required=True)
    validate = commands.add_parser("validate")
    validate.add_argument("--artifact", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "doctor":
            payload = seedance_prompt_doctor(args.skill_root)
        elif args.command == "build":
            handoff = build_seedance_prompt_handoff(
                args.skill_root,
                args.shot_unit,
                segment_limit_s=args.segment_limit_s,
                content_visual_routing_plan_path=args.content_visual_routing_plan,
                visual_style_profile_path=args.visual_style_profile,
            )
            output = write_seedance_prompt_handoff(handoff, args.output)
            payload = {
                "status": "candidate_prompt_handoff_ready",
                "artifact": str(output),
                "handoff_sha256": handoff["handoff_sha256"],
                "candidate_only": True,
            }
        else:
            handoff = read_json_object(args.artifact, "Seedance prompt handoff")
            validate_seedance_prompt_handoff(handoff)
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
