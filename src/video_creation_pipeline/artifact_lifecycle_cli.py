from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .artifact_lifecycle_adapter import (
    build_doctor,
    build_media_manifest,
    explain_artifact,
    load_adapter_config,
    load_core,
    plan_gc,
    validate_synthetic_restore_fixture,
    verify_gc_plan,
)
from .errors import ContractError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="MVP Video thin adapter for artifact-lifecycle-core media profile"
    )
    parser.add_argument("--core-root", required=True)
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser("doctor")
    doctor.add_argument("--workspace-root", required=True)
    doctor.add_argument("--config", required=True)
    doctor.add_argument("--process-snapshot")
    doctor.add_argument(
        "--operation-kind",
        choices=["lifecycle", "render", "transcode"],
        default="lifecycle",
    )
    doctor.add_argument("--estimated-output-bytes", type=int)

    scan = subparsers.add_parser("scan")
    scan.add_argument("--workspace-root", required=True)
    scan.add_argument("--config", required=True)
    scan.add_argument("--checkpoint", required=True)
    scan.add_argument("--output", required=True)

    explain = subparsers.add_parser("explain")
    explain.add_argument("--manifest", required=True)
    explain.add_argument("--artifact-id", required=True)

    plan = subparsers.add_parser("plan-gc")
    plan.add_argument("--manifest", required=True)
    plan.add_argument("--workspace-root", required=True)
    plan.add_argument("--quarantine-root", required=True)
    plan.add_argument("--config", required=True)
    plan.add_argument("--owner-state", default="unknown")
    plan.add_argument("--dispatch-state", default="unknown")
    plan.add_argument("--process-snapshot")
    plan.add_argument("--output", required=True)

    verify = subparsers.add_parser("verify-plan")
    verify.add_argument("--plan", required=True)
    verify.add_argument("--manifest", required=True)
    verify.add_argument("--workspace-root", required=True)
    verify.add_argument("--quarantine-root", required=True)

    restore = subparsers.add_parser("restore")
    restore.add_argument("--fixture", required=True)
    return parser


def _snapshot(path: str | None) -> list[dict[str, object]] | None:
    if not path:
        return None
    value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ContractError("process snapshot must be a JSON array of objects")
    return value


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        core = load_core(args.core_root)
        if args.command == "doctor":
            payload = build_doctor(
                core,
                args.workspace_root,
                load_adapter_config(args.config),
                process_snapshot=_snapshot(args.process_snapshot),
                operation_kind=args.operation_kind,
                estimated_output_bytes=args.estimated_output_bytes,
            )
        elif args.command == "scan":
            config = load_adapter_config(args.config)
            payload = build_media_manifest(
                core,
                args.workspace_root,
                config,
                checkpoint_path=args.checkpoint,
            )
            core.atomic_write_json(Path(args.output).expanduser().resolve(), payload)
            payload = {
                "status": "manifest_written",
                "manifest": str(Path(args.output).expanduser().resolve()),
                "manifest_sha256": payload["manifest_sha256"],
                "artifact_count": len(payload["artifacts"]),
                "filesystem_mutation_performed": False,
            }
        elif args.command == "explain":
            payload = explain_artifact(core, args.manifest, args.artifact_id)
        elif args.command == "plan-gc":
            payload = plan_gc(
                core,
                args.manifest,
                args.workspace_root,
                args.quarantine_root,
                load_adapter_config(args.config),
                owner_state=args.owner_state,
                dispatch_state=args.dispatch_state,
                process_snapshot=_snapshot(args.process_snapshot),
            )
            core.atomic_write_json(Path(args.output).expanduser().resolve(), payload)
            payload = {
                "status": "plan_written",
                "plan": str(Path(args.output).expanduser().resolve()),
                "plan_sha256": payload["plan_sha256"],
                "eligible": sum(bool(item["eligible"]) for item in payload["entries"]),
                "blocked": sum(
                    not bool(item["eligible"]) for item in payload["entries"]
                ),
                "authorization_state": "not_authorized",
                "filesystem_mutation_performed": False,
            }
        elif args.command == "verify-plan":
            payload = verify_gc_plan(
                core,
                args.plan,
                args.manifest,
                args.workspace_root,
                args.quarantine_root,
            )
        else:
            payload = validate_synthetic_restore_fixture(core, args.fixture)
    except (ContractError, OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        print(
            json.dumps(
                {
                    "status": "blocked",
                    "error": str(exc),
                    "filesystem_mutation_performed": False,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    if payload.get("status") == "blocked" or payload.get("ok") is False:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
