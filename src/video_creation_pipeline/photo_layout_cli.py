from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .contract_io import read_json_object
from .errors import ContractError
from .photo_layout import (
    build_photo_media_preflight,
    build_safe_media_layout_plan,
    photo_layout_doctor,
    validate_photo_media_preflight,
    validate_safe_media_layout_plan,
    write_photo_layout_contract,
)
from .safe_media_scene_plan import (
    SAFE_MEDIA_SCENE_PLAN_SCHEMA,
    build_safe_media_scene_plan,
    validate_safe_media_scene_plan,
    write_safe_media_scene_plan,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build candidate-only no-crop media preflight and layout contracts."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser("doctor", help="Check local layout prerequisites")
    doctor.add_argument("--engine-entry")

    preflight = subparsers.add_parser(
        "preflight",
        help="Inspect local photos and video metadata without modifying media",
    )
    preflight.add_argument("--source-dir", required=True)
    preflight.add_argument("--spec", required=True)
    preflight.add_argument("--output", required=True)

    build = subparsers.add_parser(
        "build", help="Build a candidate-only no-crop layout plan from a preflight"
    )
    build.add_argument("--preflight", required=True)
    build.add_argument("--engine-entry", required=True)
    build.add_argument("--spec", required=True)
    build.add_argument("--output", required=True)

    scene_plan = subparsers.add_parser(
        "build-scene-plan",
        help="Bind every packaging scene to an existing safe layout or an explicit gap",
    )
    scene_plan.add_argument("--packaging-plan", required=True)
    scene_plan.add_argument("--spec", required=True)
    scene_plan.add_argument("--output", required=True)

    validate = subparsers.add_parser(
        "validate", help="Validate a photo-layout artifact"
    )
    validate.add_argument("--artifact", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "doctor":
            payload = photo_layout_doctor(args.engine_entry)
        elif args.command == "preflight":
            spec = read_json_object(args.spec, "photo layout preflight spec")
            payload = build_photo_media_preflight(
                args.source_dir,
                recursive=spec.get("recursive", True) is not False,
                exclude_tokens=spec.get("exclude_tokens"),
            )
            output = write_photo_layout_contract(payload, args.output)
            payload = {
                "status": "candidate_preflight_ready",
                "artifact": str(output),
                "preflight_sha256": payload["preflight_sha256"],
                "source_media_modified": False,
                "candidate_only": True,
            }
        elif args.command == "build":
            spec = read_json_object(args.spec, "safe media layout build spec")
            asset_ids = spec.get("asset_ids")
            if not isinstance(asset_ids, list):
                raise ContractError("safe media layout build spec requires asset_ids")
            payload = build_safe_media_layout_plan(
                args.preflight,
                args.engine_entry,
                asset_ids=[str(value) for value in asset_ids],
                width=int(spec.get("width") or 0),
                height=int(spec.get("height") or 0),
                padding_px=int(spec.get("padding_px", 36)),
                gap_px=int(spec.get("gap_px", 18)),
                target_row_height_px=(
                    int(spec["target_row_height_px"])
                    if spec.get("target_row_height_px") is not None
                    else None
                ),
                target_row_height_tolerance=float(
                    spec.get("target_row_height_tolerance", 0.15)
                ),
            )
            output = write_photo_layout_contract(payload, args.output)
            payload = {
                "status": "candidate_layout_plan_ready",
                "artifact": str(output),
                "layout_plan_sha256": payload["layout_plan_sha256"],
                "candidate_only": True,
                "automatic_render": False,
            }
        elif args.command == "build-scene-plan":
            plan = build_safe_media_scene_plan(args.packaging_plan, args.spec)
            output = write_safe_media_scene_plan(plan, args.output)
            payload = {
                "status": "candidate_scene_plan_ready",
                "artifact": str(output),
                "plan_sha256": plan["plan_sha256"],
                "candidate_only": True,
                "automatic_render": False,
            }
        else:
            artifact = read_json_object(args.artifact, "photo layout artifact")
            if "preflight_sha256" in artifact:
                validate_photo_media_preflight(artifact)
                digest = artifact["preflight_sha256"]
            elif artifact.get("schema") == SAFE_MEDIA_SCENE_PLAN_SCHEMA:
                validate_safe_media_scene_plan(artifact)
                digest = artifact["plan_sha256"]
            else:
                validate_safe_media_layout_plan(artifact)
                digest = artifact["layout_plan_sha256"]
            payload = {
                "status": "valid_candidate_only",
                "artifact": str(Path(args.artifact).resolve()),
                "sha256": digest,
                "candidate_only": True,
            }
    except (ContractError, OSError, ValueError, TypeError) as exc:
        print(json.dumps({"status": "blocked", "error": str(exc)}), file=sys.stderr)
        return 2
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
