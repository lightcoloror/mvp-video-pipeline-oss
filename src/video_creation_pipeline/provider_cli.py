from __future__ import annotations

import argparse
import json
import sys

from .adapter_registry import build_adapter_plan, write_adapter_plan
from .boundary_refinement import (
    refine_selection_boundaries,
    write_refined_selection,
)
from .errors import ContractError
from .provider_handoff import (
    build_provider_request,
    verify_provider_receipt_files,
    write_provider_request,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build and verify offline provider handoff contracts."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    build = subparsers.add_parser("build-request")
    build.add_argument("--edit-job", required=True)
    build.add_argument("--selection", required=True)
    build.add_argument("--task", default="")
    build.add_argument("--output", required=True)
    verify = subparsers.add_parser("verify-receipt")
    verify.add_argument("--request", required=True)
    verify.add_argument("--receipt", required=True)
    refine = subparsers.add_parser("refine-boundaries")
    refine.add_argument("--edit-job", required=True)
    refine.add_argument("--selection", required=True)
    refine.add_argument("--tokens", required=True)
    refine.add_argument("--silence")
    refine.add_argument(
        "--videocut-kit-root",
        required=True,
        help="Explicit external path; upstream has no repository-level license",
    )
    refine.add_argument("--output", required=True)
    adapter = subparsers.add_parser("build-adapter-plan")
    adapter.add_argument("--request", required=True)
    adapter.add_argument("--output", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "build-request":
            request = build_provider_request(
                args.edit_job, args.selection, task=args.task
            )
            output = write_provider_request(request, args.output)
            result = {
                "output": str(output),
                "request_id": request["request_id"],
                "request_sha256": request["request_sha256"],
                "provider": request["provider"],
                "task": request["task"],
                "selected_count": len(request["timeline_items"]),
                "network_execution_performed": False,
            }
        elif args.command == "verify-receipt":
            result = verify_provider_receipt_files(args.request, args.receipt)
        elif args.command == "refine-boundaries":
            selection = refine_selection_boundaries(
                args.edit_job,
                args.selection,
                args.tokens,
                silence_path=args.silence,
                videocut_kit_root=args.videocut_kit_root,
            )
            output = write_refined_selection(selection, args.output)
            run = selection["boundary_refinement_run"]
            result = {
                "output": str(output),
                "schema": run["schema"],
                "run_sha256": run["run_sha256"],
                "item_count": run["item_count"],
                "refined_count": run["refined_count"],
                "network_execution_performed": False,
                "ffmpeg_executed": False,
            }
        else:
            plan = build_adapter_plan(args.request)
            output = write_adapter_plan(plan, args.output)
            result = {
                "output": str(output),
                "schema": plan["schema"],
                "plan_sha256": plan["plan_sha256"],
                "adapter_id": plan["adapter"]["adapter_id"],
                "status": plan["adapter"]["status"],
                "network_execution_performed": False,
            }
    except ContractError as exc:
        print(f"contract error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
