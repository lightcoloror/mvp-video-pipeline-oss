from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .contract_io import read_json_object
from .errors import ContractError
from .visual_routing import (
    build_content_visual_routing_plan,
    build_hyperframes_music_handoff,
    build_visual_style_profile,
    hyperframes_music_doctor,
    run_hyperframes_music_analysis,
    validate_content_visual_routing_plan,
    validate_hyperframes_music_handoff,
    validate_visual_style_profile,
    write_visual_routing_contract,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build deterministic candidate-only content and style routes."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    style = commands.add_parser("build-style")
    style.add_argument("--preset", required=True)
    style.add_argument("--output", required=True)

    music_doctor = commands.add_parser("music-doctor")
    music_doctor.add_argument("--source-root")

    music = commands.add_parser("build-music-handoff")
    music.add_argument("--source-root", required=True)
    music.add_argument("--audio", required=True)
    music.add_argument("--audiomap", required=True)
    music.add_argument("--output", required=True)

    analyze = commands.add_parser("analyze-music")
    analyze.add_argument("--source-root", required=True)
    analyze.add_argument("--audio", required=True)
    analyze.add_argument("--output-audiomap", required=True)
    analyze.add_argument("--receipt", required=True)
    analyze.add_argument("--python", default=sys.executable)
    analyze.add_argument("--ffmpeg", required=True)
    analyze.add_argument("--timeout", type=float, default=300.0)

    routes = commands.add_parser("build-routes")
    routes.add_argument("--packaging-plan", required=True)
    routes.add_argument("--routing-spec", required=True)
    routes.add_argument("--style-profile", required=True)
    routes.add_argument("--safe-media-scene-plan")
    routes.add_argument("--smart-reframe-plan")
    routes.add_argument("--music-handoff")
    routes.add_argument("--output", required=True)

    validate = commands.add_parser("validate")
    validate.add_argument("--artifact", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "build-style":
            payload = build_visual_style_profile(args.preset)
            output = write_visual_routing_contract(payload, args.output)
            result = {
                "status": "style_profile_ready",
                "artifact": str(output),
                "profile_sha256": payload["profile_sha256"],
                "candidate_only": True,
            }
        elif args.command == "music-doctor":
            result = hyperframes_music_doctor(args.source_root)
        elif args.command == "build-music-handoff":
            payload = build_hyperframes_music_handoff(
                args.source_root, args.audio, args.audiomap
            )
            output = write_visual_routing_contract(payload, args.output)
            result = {
                "status": "music_handoff_ready",
                "artifact": str(output),
                "handoff_sha256": payload["handoff_sha256"],
                "pacing": payload["audiomap_summary"]["pacing"],
                "candidate_only": True,
            }
        elif args.command == "analyze-music":
            payload = run_hyperframes_music_analysis(
                args.source_root,
                args.audio,
                args.output_audiomap,
                args.receipt,
                python_executable=args.python,
                ffmpeg_path=args.ffmpeg,
                timeout_s=args.timeout,
            )
            result = {
                "status": payload["status"],
                "receipt": str(Path(args.receipt).resolve()),
                "receipt_sha256": payload["receipt_sha256"],
                "audiomap": payload["output_audiomap"],
                "candidate_only": True,
            }
        elif args.command == "build-routes":
            payload = build_content_visual_routing_plan(
                args.packaging_plan,
                args.routing_spec,
                args.style_profile,
                safe_media_scene_plan_path=args.safe_media_scene_plan,
                smart_reframe_plan_path=args.smart_reframe_plan,
                music_handoff_path=args.music_handoff,
            )
            output = write_visual_routing_contract(payload, args.output)
            result = {
                "status": "visual_routes_ready",
                "artifact": str(output),
                "plan_sha256": payload["plan_sha256"],
                "candidate_only": True,
                "scene_statuses": {
                    row["scene_id"]: row["execution_status"]
                    for row in payload["scene_routes"]
                },
            }
        else:
            payload = read_json_object(args.artifact, "visual routing artifact")
            schema = payload.get("schema")
            if schema == "video_creation_pipeline.visual_style_profile.v1":
                validate_visual_style_profile(payload)
                digest = payload["profile_sha256"]
            elif schema == "video_creation_pipeline.content_visual_routing_plan.v1":
                validate_content_visual_routing_plan(payload)
                digest = payload["plan_sha256"]
            elif schema == (
                "video_creation_pipeline.hyperframes_music_to_video_handoff.v1"
            ):
                validate_hyperframes_music_handoff(payload)
                digest = payload["handoff_sha256"]
            elif schema == (
                "video_creation_pipeline.hyperframes_music_analysis_receipt.v1"
            ):
                from .visual_routing import (
                    validate_hyperframes_music_analysis_receipt,
                )

                validate_hyperframes_music_analysis_receipt(payload)
                digest = payload["receipt_sha256"]
            else:
                raise ContractError("unsupported visual routing artifact schema")
            result = {
                "status": "valid_candidate_only",
                "artifact": str(Path(args.artifact).resolve()),
                "sha256": digest,
                "candidate_only": True,
            }
    except (ContractError, OSError, TypeError, ValueError) as exc:
        print(json.dumps({"status": "blocked", "error": str(exc)}), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
