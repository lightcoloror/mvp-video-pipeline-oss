from __future__ import annotations

import argparse
import json

from .contract_io import read_json_object
from .errors import ContractError
from .semantic_editorial import (
    aggregate_editorial_preferences,
    build_editorial_feedback,
    build_editorial_packaging_projection,
    build_editorial_visual_beats,
    build_semantic_editorial_plan,
    editorial_doctor,
    editorial_review_handoff,
    validate_editorial_artifact,
    validate_screen_recording_profile,
    write_editorial_artifact,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Candidate-only semantic editorial adapter"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor")
    catalog = commands.add_parser("animation-catalog", help="Read reviewed animation sources and techniques")
    catalog.add_argument("--output")
    route = commands.add_parser("object-scene-route")
    route.add_argument("--visual-job", required=True)
    route.add_argument("--needs-depth", action="store_true")
    route.add_argument("--capability-gap")
    route.add_argument("--technique", action="append")
    route.add_argument("--delivery", choices=["video", "interactive", "still"])
    route.add_argument("--source-project", action="append")
    route.add_argument("--narration-timing", choices=["unverified", "sentence", "word", "none"])
    route.add_argument("--output")
    render = commands.add_parser("render-plan")
    for name in ("plan", "packaging", "output"):
        render.add_argument("--" + name, required=True)
    render.add_argument("--composition-id", default="EditorialCandidate")
    render.add_argument("--approval")
    render.add_argument("--safe-media-scene-plan")
    render.add_argument("--materials-approval")
    render.add_argument("--visual-beats")
    commands.add_parser("render-plan-validate").add_argument("artifact")
    preferences = commands.add_parser("preferences")
    preferences.add_argument("receipts", nargs="+")
    plan = commands.add_parser("plan")
    for name in ("timebase", "bundle-root", "spec", "output"):
        plan.add_argument("--" + name, required=True)
    beats = commands.add_parser("visual-beats")
    for name in ("routing", "spec", "output"):
        beats.add_argument("--" + name, required=True)
    beats.add_argument("--screen-profile")
    packaging = commands.add_parser("packaging")
    packaging.add_argument("--plan", required=True)
    packaging.add_argument("--output", required=True)
    packaging.add_argument("--width", type=int, default=720)
    packaging.add_argument("--height", type=int, default=1280)
    packaging.add_argument("--fps", type=float, default=30)
    for name in ("validate", "screen-validate"):
        commands.add_parser(name).add_argument("artifact")
    handoff = commands.add_parser("review-handoff")
    feedback = commands.add_parser("feedback")
    for command in (handoff, feedback):
        for name in ("plan", "approval", "output"):
            command.add_argument("--" + name, required=True)
    feedback.add_argument("--feedback", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "animation-catalog":
            from .object_scene_routes import animation_source_catalog
            result = animation_source_catalog()
            if args.output:
                from .contract_io import atomic_write_json
                atomic_write_json(result, args.output)
        elif args.command == "object-scene-route":
            from .object_scene_routes import recommend_scene_route
            result = recommend_scene_route(args.visual_job, needs_depth=args.needs_depth, capability_gap=args.capability_gap,
                techniques=args.technique, delivery=args.delivery, source_projects=args.source_project,
                narration_timing=args.narration_timing)
            if args.output:
                from .contract_io import atomic_write_json
                atomic_write_json(result, args.output)
        elif args.command in {"render-plan", "render-plan-validate"}:
            from .semantic_render_plan import (
                build_semantic_render_plan,
                validate_semantic_render_plan,
                write_semantic_render_plan,
            )

            if args.command == "render-plan-validate":
                validate_semantic_render_plan(
                    read_json_object(args.artifact, "render plan")
                )
                result = {"status": "valid_plan_only", "real_video_produced": False}
            else:
                result = build_semantic_render_plan(
                    args.plan,
                    args.packaging,
                    composition_id=args.composition_id,
                    approval_path=args.approval,
                    safe_media_scene_plan_path=args.safe_media_scene_plan,
                    materials_approval_path=args.materials_approval,
                    visual_beats_path=args.visual_beats,
                )
                write_semantic_render_plan(result, args.output)
        elif args.command == "doctor":
            result = editorial_doctor()
            result["candidate_render_plan"] = {
                "entrypoint": "render-plan",
                "mode": "plan_only",
                "renderer": "existing_remotion_project_data",
                "real_render_verified": False,
                "media_layout_validation": "existing_git_checkout_check_requires_admission",
            }
        elif args.command == "preferences":
            result = aggregate_editorial_preferences(args.receipts)
        elif args.command in {"validate", "screen-validate"}:
            value = read_json_object(args.artifact, "editorial artifact")
            if args.command == "validate":
                validate_editorial_artifact(value)
            else:
                validate_screen_recording_profile(value)
            result = {"status": "valid", "candidate_only": True}
        else:
            if args.command == "plan":
                result = build_semantic_editorial_plan(
                    args.timebase, args.bundle_root, args.spec
                )
            elif args.command == "packaging":
                result = build_editorial_packaging_projection(
                    args.plan, width=args.width, height=args.height, fps=args.fps
                )
            elif args.command == "visual-beats":
                result = build_editorial_visual_beats(
                    args.routing, args.spec, args.screen_profile
                )
            elif args.command == "feedback":
                result = build_editorial_feedback(
                    args.plan, args.feedback, args.approval
                )
            else:
                result = editorial_review_handoff(args.plan, args.approval)
            write_editorial_artifact(result, args.output)
        print(json.dumps(result, ensure_ascii=True, indent=2))
        return 0
    except (ContractError, OSError, KeyError, TypeError, ValueError) as exc:
        print(json.dumps({"status": "blocked", "error": str(exc)}, ensure_ascii=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
