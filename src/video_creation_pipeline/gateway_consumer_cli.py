from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .errors import ContractError
from .gateway_consumer import (
    build_gateway_doctor_report,
    project_gateway_result,
    run_gateway_loopback,
    verify_gateway_bundle,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Consume the external canonical model-provider-gateway without copying it."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    doctor = commands.add_parser(
        "doctor", help="Check Gateway semantic compatibility and capability truth"
    )
    doctor.add_argument("--gateway-root", required=True)

    loopback = commands.add_parser(
        "loopback",
        help="Run one synthetic text/vision request through the Gateway loopback stub",
    )
    _add_projection_arguments(loopback)
    loopback.add_argument("--artifact")
    loopback.add_argument("--instruction", required=True)
    loopback.add_argument("--output-root", required=True)
    loopback.add_argument(
        "--trigger",
        choices=["", "400", "429", "500", "timeout"],
        default="",
    )

    project = commands.add_parser(
        "project-result",
        help="Project an existing canonical Gateway bundle into a candidate-only receipt",
    )
    _add_projection_arguments(project)
    project.add_argument("--bundle-root", required=True)

    verify = commands.add_parser(
        "verify",
        help="Revalidate Gateway contracts, artifact hashes, result, and candidate receipt",
    )
    verify.add_argument("--gateway-root", required=True)
    verify.add_argument("--bundle-root", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "doctor":
            payload = build_gateway_doctor_report(args.gateway_root)
            exit_code = 0 if payload["status"] == "ready" else 2
        elif args.command == "loopback":
            payload = run_gateway_loopback(
                args.gateway_root,
                use_case=args.use_case,
                artifact_path=args.artifact,
                instruction=args.instruction,
                output_root=args.output_root,
                trigger=args.trigger,
                allowed_evidence_refs=args.evidence_ref,
                fact_assertions=_read_fact_assertions(args.fact_assertions),
            )
            exit_code = 0 if payload["status"] == "completed_candidate_only" else 2
        elif args.command == "project-result":
            payload = project_gateway_result(
                args.gateway_root,
                bundle_root=args.bundle_root,
                use_case=args.use_case,
                allowed_evidence_refs=args.evidence_ref,
                fact_assertions=_read_fact_assertions(args.fact_assertions),
            )
            exit_code = 0 if payload["status"] == "completed_candidate_only" else 2
        else:
            payload = verify_gateway_bundle(args.gateway_root, args.bundle_root)
            exit_code = 0
    except (ContractError, OSError, ValueError) as exc:
        print(
            json.dumps(
                {"status": "blocked", "error": str(exc)},
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return exit_code


def _add_projection_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--gateway-root", required=True)
    parser.add_argument(
        "--use-case",
        required=True,
        choices=[
            "script",
            "storyboard",
            "narration",
            "visual",
            "tts",
            "asr",
            "ocr",
            "embedding",
        ],
    )
    parser.add_argument("--evidence-ref", action="append", default=[])
    parser.add_argument("--fact-assertions")


def _read_fact_assertions(path: str | None) -> list[dict[str, object]]:
    if path is None:
        return []
    value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(value, list) or not all(isinstance(row, dict) for row in value):
        raise ContractError("fact assertions file must contain a JSON array of objects")
    return value


if __name__ == "__main__":
    raise SystemExit(main())
