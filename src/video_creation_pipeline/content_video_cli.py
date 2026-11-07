from __future__ import annotations

import argparse
import json
import sys

from .content_video_foundation import build_doctor_report, read_artifact_summary
from .content_video_pipeline import (
    build_content_video_project_plan,
    build_plan_dry_run,
    validate_content_video_inputs,
    write_content_video_plan_bundle,
)
from .content_video_synthetic import generate_synthetic_candidate
from .digital_human_foundation import (
    generate_digital_human_foundation_synthetic,
    validate_digital_human_foundation_bundle,
)
from .digital_human_renderer import (
    generate_digital_human_renderer_synthetic_fixture,
    validate_digital_human_renderer_fixture_bundle,
)
from .errors import ContractError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Consume a canonical content-video brief without mutating its source."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser("doctor", help="Check the offline runtime")
    doctor.add_argument("--brief")
    doctor.add_argument("--schema")
    doctor.add_argument("--research-output")
    doctor.add_argument("--research-schema")
    doctor.add_argument("--output-root")
    doctor.add_argument("--gateway-root")

    read_artifact = subparsers.add_parser(
        "read-artifact", help="Read a compact JSON artifact summary"
    )
    read_artifact.add_argument("artifact")

    validate = subparsers.add_parser(
        "validate", help="Validate canonical contracts, hashes, refs, and time policy"
    )
    _add_input_arguments(validate)

    plan = subparsers.add_parser(
        "plan", help="Write the auditable candidate-only project plan bundle"
    )
    _add_input_arguments(plan)
    plan.add_argument("--output-root", required=True)

    dry_run = subparsers.add_parser(
        "dry-run", help="Validate and report planned writes without creating files"
    )
    _add_input_arguments(dry_run)
    dry_run.add_argument("--output-root", required=True)

    generate = subparsers.add_parser(
        "generate-synthetic",
        help="Render a candidate MP4 using generated local fixture media",
    )
    _add_input_arguments(generate)
    generate.add_argument("--output-root", required=True)
    generate.add_argument("--ffmpeg-timeout", type=float, default=180.0)

    digital_human = subparsers.add_parser(
        "generate-digital-human-foundation-synthetic",
        help=(
            "Render a route-unselected digital-human foundation from entirely "
            "synthetic fixture media"
        ),
    )
    _add_input_arguments(digital_human)
    digital_human.add_argument("--output-root", required=True)
    digital_human.add_argument("--ffmpeg-timeout", type=float, default=180.0)
    digital_human.add_argument(
        "--dispatch-id",
        default="DIGITAL-HUMAN-INDEPENDENT-FOUNDATION-20260812-01",
    )

    validate_digital_human = subparsers.add_parser(
        "validate-digital-human-foundation",
        help="Validate a complete route-unselected digital-human review bundle",
    )
    validate_digital_human.add_argument("--output-root", required=True)
    renderer_fixture = subparsers.add_parser(
        "generate-digital-human-renderer-synthetic",
        help=(
            "Exercise the avatar renderer seam with an offline synthetic provider stub"
        ),
    )
    renderer_fixture.add_argument("--output-root", required=True)
    renderer_fixture.add_argument("--ffmpeg-timeout", type=float, default=180.0)
    renderer_fixture.add_argument(
        "--dispatch-id",
        default="DIGITAL-HUMAN-PPT-INTERVIEW-PIPELINE-20260812-01",
    )
    validate_renderer = subparsers.add_parser(
        "validate-digital-human-renderer",
        help="Validate the offline synthetic avatar renderer bundle",
    )
    validate_renderer.add_argument("--output-root", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "doctor":
            payload = build_doctor_report(
                artifact_path=args.brief,
                schema_path=args.schema,
                research_output_path=args.research_output,
                research_schema_path=args.research_schema,
                output_root=args.output_root,
                gateway_root=args.gateway_root,
            )
            exit_code = 0 if payload["status"] == "ready" else 2
        elif args.command == "read-artifact":
            payload = read_artifact_summary(args.artifact)
            exit_code = 0
        elif args.command == "validate-digital-human-foundation":
            payload = validate_digital_human_foundation_bundle(args.output_root)
            exit_code = 0
        elif args.command == "validate-digital-human-renderer":
            payload = validate_digital_human_renderer_fixture_bundle(args.output_root)
            exit_code = 0
        elif args.command == "generate-digital-human-renderer-synthetic":
            payload = generate_digital_human_renderer_synthetic_fixture(
                args.output_root,
                dispatch_id=args.dispatch_id,
                timeout_s=args.ffmpeg_timeout,
            )
            exit_code = 0
        else:
            inputs = validate_content_video_inputs(
                args.brief,
                args.schema,
                args.research_output,
                args.research_schema,
                max_age_hours=args.max_age_hours,
                time_sensitivity=args.time_sensitivity,
                case_handoff_manifest_path=args.case_handoff_manifest,
                case_handoff_manifest_sha256=args.case_handoff_sha256,
                require_case_handoff=args.require_case_handoff,
            )
            if args.command == "validate":
                payload = _validation_receipt(inputs)
            else:
                plan = build_content_video_project_plan(inputs)
                if args.command == "dry-run":
                    payload = build_plan_dry_run(plan, args.output_root)
                else:
                    paths = write_content_video_plan_bundle(plan, args.output_root)
                    if args.command == "generate-synthetic":
                        payload = generate_synthetic_candidate(
                            args.output_root,
                            timeout_s=args.ffmpeg_timeout,
                        )
                    elif args.command == "generate-digital-human-foundation-synthetic":
                        payload = generate_digital_human_foundation_synthetic(
                            args.output_root,
                            dispatch_id=args.dispatch_id,
                            timeout_s=args.ffmpeg_timeout,
                        )
                    else:
                        payload = {
                            "schema": "video_creation_pipeline.content_video_plan_receipt.v1",
                            "status": plan["readiness"]["status"],
                            "plan_sha256": plan["plan_sha256"],
                            "output_root": str(paths["project_plan"].parent),
                            "artifact_count": len(paths),
                            "manifest": str(paths["manifest"]),
                            "candidate_only": True,
                        }
            exit_code = 0 if payload.get("status") != "blocked" else 2
    except ContractError as exc:
        print(
            json.dumps({"status": "blocked", "error": str(exc)}),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return exit_code


def _add_input_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--brief", required=True)
    parser.add_argument("--schema", required=True)
    parser.add_argument("--research-output", required=True)
    parser.add_argument("--research-schema", required=True)
    parser.add_argument(
        "--time-sensitivity",
        choices=("high_temporal", "standard", "evergreen"),
        default="standard",
        help=(
            "Content time sensitivity. Only high_temporal applies the age gate; "
            "all levels still validate future timestamps, hashes, schemas, and refs."
        ),
    )
    parser.add_argument(
        "--max-age-hours",
        type=float,
        default=168.0,
        help="Maximum age for high_temporal content only (default: 168).",
    )
    parser.add_argument("--case-handoff-manifest")
    parser.add_argument("--case-handoff-sha256")
    parser.add_argument("--require-case-handoff", action="store_true")


def _validation_receipt(inputs: dict[str, object]) -> dict[str, object]:
    receipt = {
        "schema": "video_creation_pipeline.content_video_validation.v1",
        "status": "valid" if not inputs["blockers"] else "blocked",
        "content_video_brief": inputs["brief_artifact"],
        "content_video_brief_schema": inputs["brief_schema_artifact"],
        "research_output": inputs["research_artifact"],
        "research_output_schema": inputs["research_schema_artifact"],
        "schema_version": inputs["schema_version"],
        "referenced_source_ids": inputs["referenced_source_ids"],
        "freshness_policy": inputs["freshness_policy"],
        "freshness_evaluation": inputs["freshness_evaluation"],
        "blockers": inputs["blockers"],
    }
    if "case_handoff_manifest_artifact" in inputs:
        receipt["case_handoff_manifest"] = inputs["case_handoff_manifest_artifact"]
        receipt["case_projection_sha256"] = inputs["case_projection_sha256"]
        receipt["case_handoff_verified"] = True
    return receipt


if __name__ == "__main__":
    raise SystemExit(main())
