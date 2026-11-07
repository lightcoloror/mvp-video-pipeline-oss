from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from .color_processing import (
    COLOR_ANALYSIS_REPORT_SCHEMA,
    COLOR_PROCESSING_PLAN_SCHEMA,
    COLOR_PROCESSING_RECEIPT_SCHEMA,
    analyze_color_source,
    build_color_processing_plan,
    color_processing_doctor,
    execute_color_processing,
    validate_color_analysis_report,
    validate_color_processing_plan,
    validate_color_processing_receipt,
    write_color_processing_contract,
)
from .contract_io import read_json_object
from .errors import ContractError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Analyze and render auditable basic color-correction candidates."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("doctor", help="Check the reviewed local FFmpeg filters.")

    analyze = commands.add_parser(
        "analyze", help="Measure a video through signalstats and write a report."
    )
    analyze.add_argument("--source", type=Path, required=True)
    analyze.add_argument("--work-dir", type=Path, required=True)
    analyze.add_argument("--report", type=Path, required=True)
    analyze.add_argument("--sample-fps", type=float, default=2.0)
    analyze.add_argument("--timeout-s", type=float, default=300.0)

    plan = commands.add_parser(
        "build-plan", help="Build a hash-bound basic color-processing plan."
    )
    plan.add_argument("--source", type=Path, required=True)
    plan.add_argument("--analysis-report", type=Path, required=True)
    plan.add_argument("--output-video", type=Path, required=True)
    plan.add_argument(
        "--source-role",
        choices=(
            "camera_plate",
            "talking_head_plate",
            "interview_plate",
            "synthetic_camera_plate",
        ),
        required=True,
    )
    plan.add_argument(
        "--mode", choices=("cautious_auto", "explicit_basic"), required=True
    )
    plan.add_argument("--brightness", type=float, default=0.0)
    plan.add_argument("--contrast", type=float, default=1.0)
    plan.add_argument("--gamma", type=float, default=1.0)
    plan.add_argument("--saturation", type=float, default=1.0)
    plan.add_argument("--temperature-kelvin", type=int, default=6500)
    plan.add_argument("--temperature-mix", type=float, default=0.0)
    plan.add_argument("--preserve-lightness", type=float, default=1.0)
    plan.add_argument("--output", type=Path, required=True)

    execute = commands.add_parser(
        "execute", help="Render a plan through the single FFmpeg outlet."
    )
    execute.add_argument("--plan", type=Path, required=True)
    execute.add_argument("--work-dir", type=Path, required=True)
    execute.add_argument("--receipt", type=Path, required=True)
    execute.add_argument("--timeout-s", type=float, default=900.0)

    validate = commands.add_parser("validate", help="Revalidate a color artifact.")
    validate.add_argument("artifact", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "doctor":
            payload = color_processing_doctor()
        elif args.command == "analyze":
            report = analyze_color_source(
                args.source,
                args.work_dir,
                args.report,
                sample_fps=args.sample_fps,
                timeout_s=args.timeout_s,
            )
            payload = {
                "status": "analysis_written",
                "artifact": str(args.report.expanduser().resolve()),
                "report_sha256": report["report_sha256"],
                "technical_measurement_only": True,
            }
        elif args.command == "build-plan":
            plan = build_color_processing_plan(
                args.source,
                args.output_video,
                args.analysis_report,
                source_role=args.source_role,
                mode=args.mode,
                brightness=args.brightness,
                contrast=args.contrast,
                gamma=args.gamma,
                saturation=args.saturation,
                temperature_kelvin=args.temperature_kelvin,
                temperature_mix=args.temperature_mix,
                preserve_lightness=args.preserve_lightness,
            )
            output = write_color_processing_contract(plan, args.output)
            payload = {
                "status": "plan_written",
                "artifact": str(output),
                "plan_sha256": plan["plan_sha256"],
                "candidate_only": True,
            }
        elif args.command == "execute":
            receipt = execute_color_processing(
                args.plan,
                args.work_dir,
                args.receipt,
                timeout_s=args.timeout_s,
            )
            payload = {
                "status": "candidate_written",
                "artifact": str(args.receipt.expanduser().resolve()),
                "receipt_sha256": receipt["receipt_sha256"],
                "review_status": receipt["review"]["status"],
                "publish_authorized": False,
            }
        else:
            artifact = read_json_object(args.artifact, "color processing artifact")
            schema = artifact.get("schema")
            if schema == COLOR_ANALYSIS_REPORT_SCHEMA:
                validate_color_analysis_report(artifact)
            elif schema == COLOR_PROCESSING_PLAN_SCHEMA:
                validate_color_processing_plan(artifact)
            elif schema == COLOR_PROCESSING_RECEIPT_SCHEMA:
                validate_color_processing_receipt(artifact)
            else:
                raise ContractError("unsupported color processing artifact schema")
            payload = {
                "status": "valid",
                "artifact": str(args.artifact.expanduser().resolve()),
                "schema": schema,
                "publish_authorized": False,
            }
    except (ContractError, OSError, ValueError, TypeError) as exc:
        print(
            json.dumps({"status": "blocked", "error": str(exc)}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
