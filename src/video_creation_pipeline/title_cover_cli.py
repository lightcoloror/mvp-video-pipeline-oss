from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .contract_io import atomic_write_json, read_json_object
from .errors import ContractError
from .insurance_content_gate import (
    run_insurance_content_scan,
    validate_insurance_content_scan_request,
    write_insurance_content_scan_request,
)
from .title_cover import (
    build_creative_alignment_spec_from_title_cover_plan,
    build_title_cover_candidate_plan,
    materialize_title_cover_insurance_request,
    validate_title_cover_candidate_plan,
    write_title_cover_candidate_plan,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build auditable title and cover candidates before expensive video production."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    build = commands.add_parser("build")
    build.add_argument("--project-plan", required=True)
    build.add_argument("--spec", required=True)
    build.add_argument("--output", required=True)

    validate = commands.add_parser("validate")
    validate.add_argument("--plan", required=True)

    project = commands.add_parser("project-alignment-spec")
    project.add_argument("--plan", required=True)
    project.add_argument("--spec", required=True)
    project.add_argument("--output", required=True)

    insurance = commands.add_parser("build-insurance-request")
    insurance.add_argument("--plan", required=True)
    insurance.add_argument("--policy", required=True)
    insurance.add_argument("--output-dir", required=True)
    insurance.add_argument("--output", required=True)
    insurance.add_argument("--created-at")
    insurance.add_argument("--distribution-channel", default="unspecified")

    scan = commands.add_parser("scan-insurance")
    scan.add_argument("--request", required=True)
    scan.add_argument("--output", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "build":
            payload = build_title_cover_candidate_plan(args.project_plan, args.spec)
            path = write_title_cover_candidate_plan(payload, args.output)
            result = _result("candidate_plan_ready", path, payload["plan_sha256"])
        elif args.command == "validate":
            payload = read_json_object(args.plan, "title cover candidate plan")
            validate_title_cover_candidate_plan(payload)
            result = _result(
                "valid_candidate_only", Path(args.plan), payload["plan_sha256"]
            )
        elif args.command == "project-alignment-spec":
            payload = build_creative_alignment_spec_from_title_cover_plan(
                args.plan, args.spec
            )
            path = atomic_write_json(payload, args.output)
            result = {
                "status": "creative_alignment_spec_ready",
                "artifact": str(path),
                "selected_candidate_id": payload["selected_title_cover_candidate_id"],
                "candidate_only": True,
            }
        elif args.command == "build-insurance-request":
            payload, content_paths = materialize_title_cover_insurance_request(
                args.plan,
                args.policy,
                args.output_dir,
                created_at=args.created_at,
                distribution_channel=args.distribution_channel,
            )
            path = write_insurance_content_scan_request(payload, args.output)
            result = {
                "status": "insurance_scan_request_ready",
                "artifact": str(path),
                "content_artifacts": {
                    key: str(value) for key, value in content_paths.items()
                },
                "candidate_only": True,
            }
        else:
            request = read_json_object(args.request, "insurance scan request")
            validate_insurance_content_scan_request(request)
            payload = run_insurance_content_scan(args.request, args.output)
            result = {
                "status": payload["readiness"]["status"],
                "artifact": str(Path(args.output).resolve()),
                "candidate_only": True,
                "publish_authorized": False,
            }
    except (ContractError, OSError, TypeError, ValueError) as exc:
        print(json.dumps({"status": "blocked", "error": str(exc)}), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


def _result(status: str, path: str | Path, digest: str) -> dict[str, object]:
    return {
        "status": status,
        "artifact": str(Path(path).expanduser().resolve()),
        "plan_sha256": digest,
        "candidate_only": True,
    }


if __name__ == "__main__":
    raise SystemExit(main())
