from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .contract_io import read_json_object
from .errors import ContractError
from .remotion_project import (
    materialize_remotion_project,
    remotion_project_doctor,
    run_remotion_cover_candidates,
    run_remotion_candidate,
    validate_remotion_cover_run_receipt,
    validate_remotion_project_manifest,
    validate_remotion_run_receipt,
)
from .remotion_skill_handoff import (
    build_remotion_skill_handoff,
    remotion_skill_doctor,
    validate_remotion_skill_handoff,
    write_remotion_skill_handoff,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build a candidate-only Remotion Skills handoff."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    doctor = commands.add_parser("doctor")
    doctor.add_argument("--skills-root")
    doctor.add_argument("--runtime-source")
    doctor.add_argument("--project-dir")
    doctor.add_argument("--browser")
    build = commands.add_parser("build")
    build.add_argument("--skills-root", required=True)
    build.add_argument("--packaging-plan", required=True)
    build.add_argument("--spec", required=True)
    build.add_argument("--output", required=True)
    validate = commands.add_parser("validate")
    validate.add_argument("--artifact", required=True)
    materialize = commands.add_parser("materialize")
    materialize.add_argument("--handoff", required=True)
    materialize.add_argument("--runtime-source", required=True)
    materialize.add_argument("--project-dir", required=True)
    materialize.add_argument(
        "--license-basis",
        required=True,
        choices=(
            "individual",
            "for_profit_up_to_3_employees",
            "non_profit",
            "evaluation",
            "company_license",
        ),
    )
    materialize.add_argument(
        "--include-safe-layout-media",
        action="store_true",
        help=(
            "Explicitly copy and render only hash-bound image/video assets from "
            "the handoff safe_media_layout_plan or safe_media_scene_plan"
        ),
    )
    validate_project = commands.add_parser("validate-project")
    validate_project.add_argument("--manifest", required=True)
    run = commands.add_parser("run")
    run.add_argument("--manifest", required=True)
    run.add_argument("--runtime-source", required=True)
    run.add_argument("--browser", required=True)
    run.add_argument("--timeout", type=float, default=600.0)
    validate_run = commands.add_parser("validate-run")
    validate_run.add_argument("--receipt", required=True)
    run_covers = commands.add_parser("run-covers")
    run_covers.add_argument("--manifest", required=True)
    run_covers.add_argument("--runtime-source", required=True)
    run_covers.add_argument("--browser", required=True)
    run_covers.add_argument("--timeout", type=float, default=600.0)
    validate_covers = commands.add_parser("validate-cover-run")
    validate_covers.add_argument("--receipt", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "doctor":
            if args.runtime_source is None:
                payload = remotion_skill_doctor(args.skills_root)
            else:
                payload = {
                    "schema": "video_creation_pipeline.remotion_doctor.v1",
                    "skills": remotion_skill_doctor(args.skills_root),
                    "project": remotion_project_doctor(
                        args.runtime_source,
                        args.project_dir,
                        args.browser,
                    ),
                }
                project_status = payload["project"]["status"]
                payload["status"] = (
                    "ready_for_local_candidate_render"
                    if project_status == "ready_for_local_candidate_render"
                    else project_status
                )
        elif args.command == "build":
            spec = read_json_object(args.spec, "Remotion skill handoff spec")
            shotcraft = spec.get("video_shotcraft")
            if shotcraft is not None and not isinstance(shotcraft, dict):
                raise ContractError("video_shotcraft spec must be an object")
            handoff = build_remotion_skill_handoff(
                args.skills_root,
                args.packaging_plan,
                composition_id=str(spec.get("composition_id") or ""),
                safe_media_layout_path=spec.get("safe_media_layout_path"),
                safe_media_scene_plan_path=spec.get("safe_media_scene_plan_path"),
                semantic_motion_path=spec.get("semantic_motion_path"),
                subtitle_path=spec.get("subtitle_path"),
                video_shotcraft_root=(
                    shotcraft.get("source_root") if shotcraft is not None else None
                ),
                video_shotcraft_recipes=(
                    shotcraft.get("recipes") if shotcraft is not None else None
                ),
                content_visual_routing_plan_path=spec.get(
                    "content_visual_routing_plan_path"
                ),
                visual_style_profile_path=spec.get("visual_style_profile_path"),
                title_cover_plan_path=spec.get("title_cover_plan_path"),
            )
            output = write_remotion_skill_handoff(handoff, args.output)
            payload = {
                "status": "candidate_handoff_ready",
                "artifact": str(output),
                "handoff_sha256": handoff["handoff_sha256"],
                "candidate_only": True,
            }
        elif args.command == "validate":
            handoff = read_json_object(args.artifact, "Remotion skill handoff")
            validate_remotion_skill_handoff(handoff)
            payload = {
                "status": "valid_candidate_only",
                "artifact": str(Path(args.artifact).resolve()),
                "handoff_sha256": handoff["handoff_sha256"],
                "candidate_only": True,
            }
        elif args.command == "materialize":
            paths = materialize_remotion_project(
                args.handoff,
                args.runtime_source,
                args.project_dir,
                license_basis=args.license_basis,
                include_safe_layout_media=args.include_safe_layout_media,
            )
            manifest = read_json_object(paths["manifest"], "Remotion project manifest")
            source_media_rendered = (
                manifest["capabilities"]["source_media_rendering"] is True
            )
            payload = {
                "status": (
                    "materialized_safe_layout_media_preview"
                    if source_media_rendered
                    else "materialized_contract_preview"
                ),
                "artifacts": {key: str(path) for key, path in paths.items()},
                "candidate_only": True,
                "source_media_rendered": source_media_rendered,
            }
        elif args.command == "validate-project":
            manifest = read_json_object(args.manifest, "Remotion project manifest")
            validate_remotion_project_manifest(manifest)
            payload = {
                "status": (
                    "valid_materialized_safe_layout_media_preview"
                    if manifest["capabilities"]["source_media_rendering"] is True
                    else "valid_materialized_contract_preview"
                ),
                "manifest": str(Path(args.manifest).resolve()),
                "manifest_sha256": manifest["manifest_sha256"],
                "candidate_only": True,
            }
        elif args.command == "run":
            receipt = run_remotion_candidate(
                args.manifest,
                args.runtime_source,
                browser_path=args.browser,
                timeout_s=args.timeout,
            )
            payload = {
                "status": receipt["status"],
                "receipt_sha256": receipt["receipt_sha256"],
                "candidate_video": receipt["candidate_video"],
                "candidate_only": True,
            }
        elif args.command == "validate-run":
            receipt = read_json_object(args.receipt, "Remotion run receipt")
            validate_remotion_run_receipt(receipt)
            payload = {
                "status": "valid_candidate_only",
                "receipt": str(Path(args.receipt).resolve()),
                "receipt_sha256": receipt["receipt_sha256"],
                "candidate_only": True,
            }
        elif args.command == "run-covers":
            receipt = run_remotion_cover_candidates(
                args.manifest,
                args.runtime_source,
                browser_path=args.browser,
                timeout_s=args.timeout,
            )
            payload = {
                "status": receipt["status"],
                "receipt_sha256": receipt["receipt_sha256"],
                "cover_count": len(receipt["outputs"]),
                "candidate_only": True,
                "publish_authorized": False,
            }
        else:
            receipt = read_json_object(args.receipt, "Remotion cover run receipt")
            validate_remotion_cover_run_receipt(receipt)
            payload = {
                "status": "valid_candidate_only",
                "receipt": str(Path(args.receipt).resolve()),
                "receipt_sha256": receipt["receipt_sha256"],
                "cover_count": len(receipt["outputs"]),
                "candidate_only": True,
                "publish_authorized": False,
            }
    except (ContractError, OSError, ValueError, TypeError) as exc:
        print(json.dumps({"status": "blocked", "error": str(exc)}), file=sys.stderr)
        return 2
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
