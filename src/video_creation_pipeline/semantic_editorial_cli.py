from __future__ import annotations

import argparse
import json
from pathlib import Path

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
    commands.add_parser("enrichment-catalog", help="Read optional component reuse map; does not render")
    catalog = commands.add_parser("animation-catalog", help="Read reviewed animation sources and techniques")
    catalog.add_argument("--output")
    bundle = commands.add_parser("animation-source-bundle", help="Read native upstream indexes; optionally verify all preserved bytes")
    bundle.add_argument("--project", required=True)
    bundle.add_argument("--verify", action="store_true")
    bundle.add_argument("--entry", help="Original style slug, shot-card name or manifest-relative source path")
    bundle.add_argument("--output")
    m_build = commands.add_parser("motion-index-build", help="Compile unified offline motion-style entry index")
    m_build.add_argument("--output", help="Optional output path (defaults to policies/motion-entry-index.v1.json)")
    m_find = commands.add_parser("motion-find", help="Search unified motion-style entries by visual_job/technique/tone/medium/query")
    m_find.add_argument("--visual-job")
    m_find.add_argument("--technique")
    m_find.add_argument("--tone")
    m_find.add_argument("--medium")
    m_find.add_argument("--tier")
    m_find.add_argument("--source-project")
    m_find.add_argument("--query", default="")
    m_find.add_argument("--limit", type=int, default=5)
    m_find.add_argument("--index")
    m_find.add_argument("--output")
    m_unpack = commands.add_parser("motion-unpack", help="Resolve full relative import closure, live SHA-256 and binding template")
    m_unpack.add_argument("--entry-id", required=True)
    m_unpack.add_argument("--index")
    m_unpack.add_argument("--output")
    m_check = commands.add_parser("motion-binding-check", help="Fail-closed validation of motion_reference_binding.v1")
    m_check.add_argument("--binding", required=True)
    m_check.add_argument("--index")
    m_check.add_argument("--output")
    objects = commands.add_parser("object-scenes")
    for name in ("spec", "script-approval", "voice-approval", "storyboard-approval", "output"):
        objects.add_argument("--" + name, required=True)
    commands.add_parser("object-scenes-validate").add_argument("artifact")
    commands.add_parser("object-scene-components")
    route = commands.add_parser("object-scene-route")
    route.add_argument("--visual-job", required=True)
    route.add_argument("--needs-depth", action="store_true")
    route.add_argument("--capability-gap")
    route.add_argument("--technique", action="append")
    route.add_argument("--delivery", choices=["video", "interactive", "still"])
    route.add_argument("--source-project", action="append")
    route.add_argument("--narration-timing", choices=["unverified", "sentence", "word", "none"])
    route.add_argument("--output")
    recipes = commands.add_parser("explainer-recipes")
    for name in ("content-plan", "spec", "output"):
        recipes.add_argument("--" + name, required=True)
    assets = commands.add_parser("explainer-assets")
    assets.add_argument("--plan", required=True)
    assets.add_argument("--output-dir", required=True)
    check = commands.add_parser("explainer-check")
    check.add_argument("--plan", required=True)
    check.add_argument("--motion-plans", nargs="+", required=True)
    check.add_argument("--output", required=True)
    render = commands.add_parser("render-plan")
    for name in ("plan", "packaging", "output"):
        render.add_argument("--" + name, required=True)
    render.add_argument("--composition-id", default="EditorialCandidate")
    render.add_argument("--approval")
    render.add_argument("--safe-media-scene-plan")
    render.add_argument("--materials-approval")
    render.add_argument("--visual-beats")
    render.add_argument("--motion-reference-binding")
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
        if args.command == "animation-source-bundle":
            from .object_scene_routes import source_bundle_for_project
            result = source_bundle_for_project(args.project, verify_files=args.verify, entry=args.entry)
            if args.output:
                from .contract_io import atomic_write_json
                atomic_write_json(result, args.output)
        elif args.command == "motion-index-build":
            from .motion_entry_index import DEFAULT_INDEX_PATH, build_motion_entry_index
            target_out = Path(args.output) if args.output else DEFAULT_INDEX_PATH
            built = build_motion_entry_index(output_path=target_out)
            result = {
                "status": "built",
                "output": str(target_out.resolve()),
                "schema": built["schema"],
                "entry_count": built["entry_count"],
                "counts_by_tier": built["counts_by_tier"],
                "counts_by_source": built["counts_by_source"],
                "index_sha256": built["index_sha256"],
            }
        elif args.command == "motion-find":
            from .motion_entry_index import find_motion_entries
            result = find_motion_entries(
                visual_job=args.visual_job,
                technique=args.technique,
                tone=args.tone,
                medium=args.medium,
                tier=args.tier,
                source_project=args.source_project,
                query=args.query,
                limit=args.limit,
                index_path=args.index,
            )
            if args.output:
                from .contract_io import atomic_write_json
                atomic_write_json(result, args.output)
        elif args.command == "motion-unpack":
            from .motion_entry_index import unpack_motion_entry
            result = unpack_motion_entry(args.entry_id, index_path=args.index)
            if args.output:
                from .contract_io import atomic_write_json
                atomic_write_json(result, args.output)
        elif args.command == "motion-binding-check":
            from .motion_entry_index import validate_motion_reference_binding
            binding_value = read_json_object(args.binding, "motion reference binding")
            result = validate_motion_reference_binding(binding_value, index_path=args.index)
            if args.output:
                from .contract_io import atomic_write_json
                atomic_write_json(result, args.output)
        elif args.command == "animation-catalog":
            from .object_scene_routes import animation_source_catalog
            result = animation_source_catalog()
            if args.output:
                from .contract_io import atomic_write_json
                atomic_write_json(result, args.output)
        elif args.command == "enrichment-catalog":
            result = read_json_object(
                Path(__file__).with_name("policies") / "editorial-enrichment-catalog.v1.json",
                "editorial enrichment catalog",
            )
        elif args.command == "object-scene-components":
            from .object_scenes import object_scene_component_catalog
            result = object_scene_component_catalog()
        elif args.command == "object-scene-route":
            from .object_scene_routes import recommend_scene_route
            result = recommend_scene_route(
                args.visual_job,
                needs_depth=args.needs_depth,
                capability_gap=args.capability_gap,
                techniques=args.technique,
                delivery=args.delivery,
                source_projects=args.source_project,
                narration_timing=args.narration_timing,
            )
            if args.output:
                from .contract_io import atomic_write_json
                atomic_write_json(result, args.output)
        elif args.command in {"object-scenes", "object-scenes-validate"}:
            from .object_scenes import (
                build_object_scene_plan,
                validate_object_scene_plan,
                write_object_scene_plan,
            )
            if args.command == "object-scenes-validate":
                validate_object_scene_plan(read_json_object(args.artifact, "object scene plan"))
                result = {"status": "valid_candidate_object_scene_plan", "rendered": False}
            else:
                plan = build_object_scene_plan(
                    args.spec,
                    script_approval_path=args.script_approval,
                    voice_approval_path=args.voice_approval,
                    storyboard_approval_path=args.storyboard_approval,
                )
                path = write_object_scene_plan(plan, args.output)
                result = {
                    "status": "candidate_object_scene_plan",
                    "artifact": str(path),
                    "rendered": False,
                    "synthetic_fixture_only": plan["synthetic_fixture_only"],
                }
        elif args.command.startswith("explainer-"):
            from .contract_io import atomic_write_json
            from .explainer_recipes import (
                build_explainer_recipes,
                check_recipe_shots,
                materialize_recipe_assets,
            )
            if args.command == "explainer-recipes":
                result = build_explainer_recipes(args.content_plan, args.spec)
                atomic_write_json(result, args.output)
            elif args.command == "explainer-assets":
                result = materialize_recipe_assets(args.plan, args.output_dir)
            else:
                result = check_recipe_shots(args.plan, args.motion_plans)
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
                    motion_reference_binding_path=args.motion_reference_binding,
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
