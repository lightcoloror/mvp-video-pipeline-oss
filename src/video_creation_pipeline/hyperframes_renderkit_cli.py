from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .contract_io import read_json_object
from .errors import ContractError
from .hyperframes_renderkit import (
    OFFICIAL_ROUTE,
    build_render_assurance_plan,
    inspect_renderkit_source,
    run_render_assurance_preflight,
    validate_render_assurance_plan,
    validate_render_preflight_receipt,
    write_render_assurance_plan,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Bind the reviewed HyperFrames RenderKit source as a fail-closed "
            "assurance layer without creating another renderer or FFmpeg outlet."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser("doctor", help="Inspect fixed RenderKit source")
    doctor.add_argument("--renderkit-source", required=True)
    doctor.add_argument("--runtime")

    build = subparsers.add_parser("build", help="Build a render assurance plan")
    build.add_argument("--semantic-motion-plan", required=True)
    build.add_argument("--project-dir", required=True)
    build.add_argument("--renderkit-source", required=True)
    build.add_argument("--route", default=OFFICIAL_ROUTE)
    build.add_argument("--runtime")
    build.add_argument("--output", required=True)

    validate_plan = subparsers.add_parser(
        "validate-plan", help="Validate a render assurance plan"
    )
    validate_plan.add_argument("--plan", required=True)

    preflight = subparsers.add_parser(
        "preflight", help="Run the upstream compatibility scanner"
    )
    preflight.add_argument("--plan", required=True)
    preflight.add_argument("--output", required=True)
    preflight.add_argument("--node", default="node")
    preflight.add_argument("--timeout", type=float, default=120.0)

    validate_receipt = subparsers.add_parser(
        "validate-receipt", help="Validate a render preflight receipt"
    )
    validate_receipt.add_argument("--receipt", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "doctor":
            payload = inspect_renderkit_source(
                args.renderkit_source, runtime_path=args.runtime
            )
        elif args.command == "build":
            plan = build_render_assurance_plan(
                args.semantic_motion_plan,
                args.project_dir,
                args.renderkit_source,
                selected_route=args.route,
                runtime_path=args.runtime,
            )
            path = write_render_assurance_plan(plan, args.output)
            payload = {
                "status": "render_assurance_plan_ready",
                "plan": str(path),
                "plan_sha256": plan["plan_sha256"],
                "selected_route": plan["selected_route"],
                "candidate_only": True,
            }
        elif args.command == "validate-plan":
            plan = read_json_object(args.plan, "HyperFrames render assurance plan")
            validate_render_assurance_plan(plan)
            payload = {
                "status": "valid",
                "plan": str(Path(args.plan).resolve()),
                "selected_route": plan["selected_route"],
            }
        elif args.command == "preflight":
            payload = run_render_assurance_preflight(
                args.plan,
                args.output,
                node_path=args.node,
                timeout_s=args.timeout,
            )
        else:
            receipt = read_json_object(
                args.receipt, "HyperFrames render preflight receipt"
            )
            validate_render_preflight_receipt(receipt)
            payload = {
                "status": "valid",
                "receipt": str(Path(args.receipt).resolve()),
                "selected_route": receipt["selected_route"],
            }
    except (ContractError, OSError, TypeError, ValueError) as exc:
        print(json.dumps({"status": "blocked", "error": str(exc)}), file=sys.stderr)
        return 2
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    if payload.get("status") == "blocked_for_renderkit_accelerated_handoff":
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
