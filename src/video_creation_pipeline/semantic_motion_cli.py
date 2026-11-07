from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .contract_io import read_json_object
from .errors import ContractError
from .semantic_motion import (
    build_semantic_motion_plan,
    materialize_hyperframes_project,
    run_hyperframes_candidate,
    validate_semantic_motion_plan,
    validate_semantic_motion_run_receipt,
    write_semantic_motion_plan,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build and run a candidate-only HyperFrames semantic motion slot."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    build = subparsers.add_parser("build", help="Build a semantic motion plan")
    build.add_argument("--packaging-plan", required=True)
    build.add_argument("--script-anchor", required=True)
    build.add_argument("--source-asset", required=True)
    build.add_argument("--visual-production-plan", required=True)
    build.add_argument("--spec", required=True)
    build.add_argument("--output", required=True)

    validate = subparsers.add_parser("validate", help="Validate a motion plan")
    validate.add_argument("--plan", required=True)

    materialize = subparsers.add_parser(
        "materialize", help="Write deterministic local HyperFrames project files"
    )
    materialize.add_argument("--plan", required=True)
    materialize.add_argument("--project-dir", required=True)
    materialize.add_argument("--static-fallback", action="store_true")

    run = subparsers.add_parser(
        "run", help="Render via fixed HyperFrames source and the single FFmpeg outlet"
    )
    run.add_argument("--plan", required=True)
    run.add_argument("--project-dir", required=True)
    run.add_argument("--hyperframes-source", required=True)
    run.add_argument("--browser", required=True)
    run.add_argument("--ffmpeg", required=True)
    run.add_argument("--ffprobe", required=True)
    run.add_argument("--timeout", type=float, default=300.0)
    run.add_argument("--static-fallback", action="store_true")
    run.add_argument("--render-assurance-plan")
    run.add_argument("--node", default="node")

    validate_run = subparsers.add_parser(
        "validate-run", help="Validate a completed semantic motion receipt"
    )
    validate_run.add_argument("--receipt", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "build":
            spec = read_json_object(args.spec, "semantic motion build spec")
            plan = build_semantic_motion_plan(
                args.packaging_plan,
                args.script_anchor,
                args.source_asset,
                args.visual_production_plan,
                asset_id=str(spec.get("asset_id") or ""),
                media_type=str(spec.get("media_type") or ""),
                cues=list(spec.get("cues") or []),
                width=int(spec.get("width") or 1080),
                height=int(spec.get("height") or 1920),
                fps=int(spec.get("fps") or 30),
                duration_s=spec.get("duration_s"),
                evidence_status=str(spec.get("evidence_status") or "verified"),
                authorization_status=str(
                    spec.get("authorization_status") or "authorized_local_candidate"
                ),
                deidentified=spec.get("deidentified") is True,
                content_visual_routing_plan_path=spec.get(
                    "content_visual_routing_plan_path"
                ),
                visual_style_profile_path=spec.get("visual_style_profile_path"),
                privacy_exclusion_zones_path=spec.get("privacy_exclusion_zones_path"),
                smart_reframe_plan_path=spec.get("smart_reframe_plan_path"),
            )
            path = write_semantic_motion_plan(plan, args.output)
            payload = {
                "status": "ready_for_renderer_slot",
                "plan": str(path),
                "plan_sha256": plan["plan_sha256"],
                "candidate_only": True,
            }
        elif args.command == "validate":
            plan = read_json_object(args.plan, "semantic motion plan")
            validate_semantic_motion_plan(plan)
            payload = {
                "status": "valid",
                "plan": str(Path(args.plan).resolve()),
                "plan_sha256": plan["plan_sha256"],
                "candidate_only": True,
            }
        elif args.command == "materialize":
            paths = materialize_hyperframes_project(
                args.plan,
                args.project_dir,
                static_fallback=args.static_fallback,
            )
            payload = {
                "status": "materialized_candidate_only",
                "artifacts": {key: str(path) for key, path in paths.items()},
                "candidate_only": True,
            }
        elif args.command == "validate-run":
            receipt = read_json_object(args.receipt, "semantic motion run receipt")
            validate_semantic_motion_run_receipt(receipt)
            payload = {
                "status": "valid_candidate_only",
                "receipt": str(Path(args.receipt).resolve()),
                "candidate_only": True,
            }
        else:
            payload = run_hyperframes_candidate(
                args.plan,
                args.project_dir,
                args.hyperframes_source,
                browser_path=args.browser,
                ffmpeg_path=args.ffmpeg,
                ffprobe_path=args.ffprobe,
                timeout_s=args.timeout,
                static_fallback=args.static_fallback,
                render_assurance_plan_path=args.render_assurance_plan,
                node_path=args.node,
            )
    except (ContractError, OSError, ValueError, TypeError) as exc:
        print(json.dumps({"status": "blocked", "error": str(exc)}), file=sys.stderr)
        return 2
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
