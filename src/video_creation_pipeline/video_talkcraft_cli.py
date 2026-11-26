from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .contract_io import read_json_object
from .errors import ContractError
from .video_talkcraft_adapter import (
    VIDEO_TALKCRAFT_COMMIT,
    VIDEO_TALKCRAFT_EXECUTION_RECEIPT_SCHEMA,
    VIDEO_TALKCRAFT_RECIPE_PLAN_SCHEMA,
    build_video_talkcraft_execution_receipt,
    build_video_talkcraft_recipe_plan,
    validate_video_talkcraft_execution_receipt,
    validate_video_talkcraft_recipe_plan,
    video_talkcraft_doctor,
    write_video_talkcraft_contract,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Adapt a fixed external video-talkcraft catalog into local contracts."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    doctor = commands.add_parser("doctor")
    doctor.add_argument("--source-root", required=True)
    doctor.add_argument("--source-commit", default=VIDEO_TALKCRAFT_COMMIT)

    plan = commands.add_parser("plan")
    plan.add_argument("--semantic-motion-plan", required=True)
    plan.add_argument("--source-root", required=True)
    plan.add_argument("--source-commit", default=VIDEO_TALKCRAFT_COMMIT)
    plan.add_argument("--word-timestamps")
    plan.add_argument("--selections")
    plan.add_argument("--output", required=True)

    verify = commands.add_parser("verify")
    verify.add_argument("--recipe-plan", required=True)
    verify.add_argument("--render-receipt", required=True)
    verify.add_argument("--observations", required=True)
    verify.add_argument("--output", required=True)

    validate = commands.add_parser("validate")
    validate.add_argument("--artifact", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = _execute(args)
    except ContractError as exc:
        print(f"contract error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0


def _execute(args: argparse.Namespace) -> dict[str, Any]:
    if args.command == "doctor":
        return video_talkcraft_doctor(
            args.source_root, source_commit=args.source_commit
        )
    if args.command == "plan":
        plan = build_video_talkcraft_recipe_plan(
            args.semantic_motion_plan,
            args.source_root,
            source_commit=args.source_commit,
            word_timestamps_path=args.word_timestamps,
            selections_path=args.selections,
        )
        path = write_video_talkcraft_contract(plan, args.output)
        return {
            "schema": plan["schema"],
            "status": plan["authority_boundary"]["status"],
            "artifact": str(path),
            "plan_sha256": plan["plan_sha256"],
            "cue_count": len(plan["cues"]),
            "candidate_only": True,
        }
    if args.command == "verify":
        receipt = build_video_talkcraft_execution_receipt(
            args.recipe_plan, args.render_receipt, args.observations
        )
        path = write_video_talkcraft_contract(receipt, args.output)
        return {
            "schema": receipt["schema"],
            "status": receipt["status"],
            "artifact": str(path),
            "receipt_sha256": receipt["receipt_sha256"],
            "render_eligible": receipt["release_gate"]["render_eligible"],
            "candidate_only": True,
        }
    path = Path(args.artifact).expanduser().resolve()
    payload = read_json_object(path, "video-talkcraft adapter artifact")
    if payload.get("schema") == VIDEO_TALKCRAFT_RECIPE_PLAN_SCHEMA:
        validate_video_talkcraft_recipe_plan(payload)
    elif payload.get("schema") == VIDEO_TALKCRAFT_EXECUTION_RECEIPT_SCHEMA:
        validate_video_talkcraft_execution_receipt(payload)
    else:
        raise ContractError("unsupported video-talkcraft adapter artifact")
    return {"valid": True, "schema": payload["schema"], "artifact": str(path)}


if __name__ == "__main__":
    raise SystemExit(main())
