from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from .background_processing import (
    BACKGROUND_PROCESSING_PLAN_SCHEMA,
    BACKGROUND_PROCESSING_RECEIPT_SCHEMA,
    background_processing_doctor,
    build_background_processing_plan,
    execute_background_processing,
    validate_background_processing_plan,
    validate_background_processing_receipt,
    write_background_processing_contract,
)
from .contract_io import read_json_object
from .errors import ContractError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build and run local person-background processing candidates."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    doctor = commands.add_parser("doctor", help="Check the local RVM Provider.")
    _add_provider_arguments(doctor)

    plan = commands.add_parser(
        "build-plan", help="Build a hash-bound person-background processing plan."
    )
    plan.add_argument("--source", type=Path, required=True)
    plan.add_argument("--output-video", type=Path, required=True)
    _add_provider_arguments(plan)
    plan.add_argument(
        "--mode",
        choices=("blur", "replace_color", "replace_image", "transparent"),
        required=True,
    )
    plan.add_argument("--background-image", type=Path)
    plan.add_argument("--background-color", default="#183153")
    plan.add_argument("--blur-sigma", type=float, default=18.0)
    plan.add_argument("--blur-steps", type=int, default=2)
    plan.add_argument("--seq-chunk", type=int, default=4)
    plan.add_argument("--downsample-ratio", type=float)
    plan.add_argument("--output", type=Path, required=True)

    execute = commands.add_parser(
        "execute", help="Run a plan through RVM and the single FFmpeg outlet."
    )
    execute.add_argument("--plan", type=Path, required=True)
    execute.add_argument("--work-dir", type=Path, required=True)
    execute.add_argument("--receipt", type=Path, required=True)
    execute.add_argument("--timeout-s", type=float, default=900.0)

    validate = commands.add_parser(
        "validate", help="Revalidate a background plan or receipt."
    )
    validate.add_argument("artifact", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "doctor":
            payload = background_processing_doctor(
                args.provider_source,
                args.model,
                args.runtime_dependencies,
                device=args.device,
            )
        elif args.command == "build-plan":
            payload = build_background_processing_plan(
                args.source,
                args.output_video,
                args.provider_source,
                args.model,
                args.runtime_dependencies,
                mode=args.mode,
                device=args.device,
                background_image_path=args.background_image,
                background_color=args.background_color,
                blur_sigma=args.blur_sigma,
                blur_steps=args.blur_steps,
                seq_chunk=args.seq_chunk,
                downsample_ratio=args.downsample_ratio,
            )
            output = write_background_processing_contract(payload, args.output)
            payload = {
                "status": "plan_written",
                "artifact": str(output),
                "plan_sha256": payload["plan_sha256"],
                "candidate_only": True,
            }
        elif args.command == "execute":
            receipt = execute_background_processing(
                args.plan,
                args.work_dir,
                args.receipt,
                timeout_s=args.timeout_s,
            )
            payload = {
                "status": "candidate_written",
                "artifact": str(Path(args.receipt).expanduser().resolve()),
                "receipt_sha256": receipt["receipt_sha256"],
                "review_status": receipt["review"]["status"],
                "publish_authorized": False,
            }
        else:
            payload = read_json_object(args.artifact, "background processing artifact")
            schema = payload.get("schema")
            if schema == BACKGROUND_PROCESSING_PLAN_SCHEMA:
                validate_background_processing_plan(payload)
            elif schema == BACKGROUND_PROCESSING_RECEIPT_SCHEMA:
                validate_background_processing_receipt(payload)
            else:
                raise ContractError("unsupported background processing artifact schema")
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


def _add_provider_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--provider-source", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--runtime-dependencies", type=Path, required=True)
    parser.add_argument("--device", choices=("cpu", "cuda"), default="cpu")


if __name__ == "__main__":
    raise SystemExit(main())
