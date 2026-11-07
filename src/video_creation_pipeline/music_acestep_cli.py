from __future__ import annotations

import argparse
import json
import sys

from .contract_io import read_json_object
from .errors import ContractError
from .music_acestep import (
    build_acestep_checkpoint_manifest,
    build_acestep_generation_plan,
    materialize_acestep_music_profiles,
    run_acestep_generation,
    write_acestep_contract,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Plan and run the pinned ACE-Step 1.5 music generator."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    manifest = commands.add_parser("build-checkpoint-manifest")
    manifest.add_argument("--checkpoint-root", required=True)
    manifest.add_argument("--license-snapshot", required=True)
    manifest.add_argument("--model-license", default="MIT")
    manifest.add_argument("--commercial-use-allowed", action="store_true")
    manifest.add_argument("--output", required=True)

    plan = commands.add_parser("build-plan")
    plan.add_argument("--intent", required=True)
    plan.add_argument("--checkpoint-manifest", required=True)
    plan.add_argument("--source-root", required=True)
    plan.add_argument("--output-root", required=True)
    plan.add_argument("--spec", required=True)
    plan.add_argument("--output", required=True)

    execute = commands.add_parser("run")
    execute.add_argument("--plan", required=True)
    execute.add_argument("--output", required=True)

    profiles = commands.add_parser("materialize-profiles")
    profiles.add_argument("--receipt", required=True)
    profiles.add_argument("--output-dir", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "build-checkpoint-manifest":
            payload = build_acestep_checkpoint_manifest(
                args.checkpoint_root,
                args.license_snapshot,
                model_license=args.model_license,
                commercial_use_allowed=args.commercial_use_allowed,
            )
            output = write_acestep_contract(payload, args.output)
            result = {
                "schema": payload["schema"],
                "path": str(output),
                "manifest_sha256": payload["manifest_sha256"],
            }
        elif args.command == "build-plan":
            spec = read_json_object(args.spec, "ACE-Step generation specification")
            payload = build_acestep_generation_plan(
                args.intent,
                args.checkpoint_manifest,
                args.source_root,
                args.output_root,
                purpose=str(spec.get("purpose") or "personal"),
                device=str(spec.get("device") or "cuda"),
                candidates_per_segment=spec.get("candidates_per_segment", 3),
                random_seed=spec.get("random_seed", 20260805),
                inference_steps=spec.get("inference_steps", 8),
                guidance_scale=spec.get("guidance_scale", 7.0),
                segment_overrides=spec.get("segment_overrides") or {},
            )
            output = write_acestep_contract(payload, args.output)
            result = {
                "schema": payload["schema"],
                "path": str(output),
                "plan_sha256": payload["plan_sha256"],
                "request_count": len(payload["requests"]),
            }
        elif args.command == "run":
            payload = run_acestep_generation(args.plan)
            output = write_acestep_contract(payload, args.output)
            result = {
                "schema": payload["schema"],
                "path": str(output),
                "receipt_sha256": payload["receipt_sha256"],
                "status": payload["status"],
                "eligible_for_clap_ranking": payload["readiness"][
                    "eligible_for_clap_ranking"
                ],
            }
        else:
            paths = materialize_acestep_music_profiles(args.receipt, args.output_dir)
            result = {
                "schema": "video_creation_pipeline.music_asset_profile_set.v1",
                "paths": [str(path) for path in paths],
                "profile_count": len(paths),
            }
    except ContractError as exc:
        print(f"contract error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False))
    if args.command == "run" and result.get("status") == "failed":
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
