from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from .contract_io import read_json_object
from .errors import ContractError
from .speech_cleanup import (
    SPEECH_CLEANUP_PLAN_SCHEMA,
    SPEECH_CLEANUP_RECEIPT_SCHEMA,
    build_speech_cleanup_plan,
    execute_speech_cleanup,
    speech_cleanup_doctor,
    validate_speech_cleanup_plan,
    validate_speech_cleanup_receipt,
    write_speech_cleanup_contract,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build and execute local speech-noise cleanup candidates."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("doctor", help="Check the required local FFmpeg filters.")

    plan = commands.add_parser(
        "build-plan", help="Build a hash-bound speech cleanup plan."
    )
    plan.add_argument("--source", type=Path, required=True)
    plan.add_argument("--output-audio", type=Path, required=True)
    plan.add_argument("--profile", choices=("original", "gentle", "strong"), default="gentle")
    plan.add_argument("--processing-review", type=Path)
    plan.add_argument("--audition-feedback", type=Path)
    plan.add_argument(
        "--noise-kind",
        choices=("broadband_hiss", "mains_hum"),
        action="append",
        default=[],
    )
    plan.add_argument("--mains-hz", type=int, choices=(50, 60), default=50)
    plan.add_argument("--output", type=Path, required=True)

    execute = commands.add_parser(
        "execute", help="Execute a plan through the single FFmpeg outlet."
    )
    execute.add_argument("--plan", type=Path, required=True)
    execute.add_argument("--work-dir", type=Path, required=True)
    execute.add_argument("--receipt", type=Path, required=True)
    execute.add_argument("--timeout-s", type=float, default=180.0)

    validate = commands.add_parser(
        "validate", help="Revalidate a plan or execution receipt."
    )
    validate.add_argument("artifact", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "doctor":
            result = speech_cleanup_doctor()
        elif args.command == "build-plan":
            payload = build_speech_cleanup_plan(
                args.source,
                args.output_audio,
                profile=args.profile,
                noise_kinds=args.noise_kind,
                mains_hz=args.mains_hz,
                processing_review=read_json_object(args.processing_review,"processing review") if args.processing_review else None,
                audition_feedback=read_json_object(args.audition_feedback,"audition feedback") if args.audition_feedback else None,
            )
            output = write_speech_cleanup_contract(payload, args.output)
            result = {
                "status": "ok",
                "output": str(output),
                "plan_sha256": payload["plan_sha256"],
            }
        elif args.command == "execute":
            payload = execute_speech_cleanup(
                args.plan,
                args.work_dir,
                args.receipt,
                timeout_s=args.timeout_s,
            )
            result = {
                "status": "ok",
                "receipt": str(args.receipt.resolve()),
                "receipt_sha256": payload["receipt_sha256"],
                "audition_status": payload["audition"]["status"],
            }
        else:
            payload = read_json_object(args.artifact, "speech cleanup artifact")
            schema = payload.get("schema")
            if schema == SPEECH_CLEANUP_PLAN_SCHEMA:
                validate_speech_cleanup_plan(payload)
            elif schema == SPEECH_CLEANUP_RECEIPT_SCHEMA:
                validate_speech_cleanup_receipt(payload)
            else:
                raise ContractError("unsupported speech cleanup artifact schema")
            result = {
                "status": "ok",
                "schema": schema,
                "artifact": str(args.artifact.resolve()),
            }
    except ContractError as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
