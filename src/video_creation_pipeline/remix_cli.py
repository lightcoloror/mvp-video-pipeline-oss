from __future__ import annotations

import argparse
import json
import sys

from .errors import ContractError
from .reference_remix import (
    build_material_match_plan,
    build_voice_timing_plan,
    write_contract,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build offline reference-remix planning contracts."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    match = subparsers.add_parser("build-match-plan")
    match.add_argument("--recipe", required=True)
    match.add_argument("--candidates", required=True)
    match.add_argument("--output", required=True)
    match.add_argument("--min-confidence", type=float, default=0.6)
    match.add_argument("--max-consecutive-uses", type=int, default=2)
    match.add_argument("--repeated-context-penalty", type=float, default=0.08)

    voice = subparsers.add_parser("build-voice-plan")
    voice.add_argument("--recipe", required=True)
    voice.add_argument("--measurements", required=True)
    voice.add_argument("--output", required=True)
    voice.add_argument("--tolerance-seconds", type=float, default=0.25)
    voice.add_argument("--tolerance-ratio", type=float, default=0.08)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "build-match-plan":
            payload = build_material_match_plan(
                args.recipe,
                args.candidates,
                min_confidence=args.min_confidence,
                max_consecutive_uses=args.max_consecutive_uses,
                repeated_context_penalty=args.repeated_context_penalty,
            )
            summary = {
                "status": payload["readiness"]["status"],
                "shot_count": payload["readiness"]["shot_count"],
                "missing_material_count": payload["readiness"][
                    "missing_material_count"
                ],
            }
        else:
            payload = build_voice_timing_plan(
                args.recipe,
                args.measurements,
                tolerance_s=args.tolerance_seconds,
                tolerance_ratio=args.tolerance_ratio,
            )
            summary = {
                "status": payload["readiness"]["status"],
                "missing_measurement_count": payload["readiness"][
                    "missing_measurement_count"
                ],
                "timing_review_count": payload["readiness"]["timing_review_count"],
            }
        output = write_contract(payload, args.output)
    except ContractError as exc:
        print(f"contract error: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {"output": str(output), "plan_sha256": payload["plan_sha256"], **summary},
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
