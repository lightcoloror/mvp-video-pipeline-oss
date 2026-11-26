from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .contract_io import read_json_object
from .errors import ContractError
from .external_motion_assets import (
    bootstrap_clipkit_sfx_pack,
    build_external_motion_catalog,
    build_external_motion_selection_plan,
    external_motion_assets_doctor,
    validate_external_motion_contract,
    write_external_motion_contract,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Use reviewed external motion/SFX sources through thin adapters."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    doctor = commands.add_parser("doctor")
    _add_source_args(doctor, onda_required=False)
    catalog = commands.add_parser("catalog")
    _add_source_args(catalog, onda_required=True)
    catalog.add_argument("--output", required=True)
    plan = commands.add_parser("plan")
    plan.add_argument("--catalog", required=True)
    plan.add_argument("--semantic-motion-plan", required=True)
    plan.add_argument("--overrides")
    plan.add_argument("--output", required=True)
    bootstrap = commands.add_parser("bootstrap-clipkit")
    bootstrap.add_argument("--clipkit-root", required=True)
    bootstrap.add_argument("--output-root", required=True)
    bootstrap.add_argument(
        "--purpose", choices=("personal", "commercial"), default="commercial"
    )
    bootstrap.add_argument("--platform", default="social_media")
    validate = commands.add_parser("validate")
    validate.add_argument("--artifact", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "doctor":
            payload = external_motion_assets_doctor(
                onda_root=args.onda_root,
                remotion_bits_root=args.remotion_bits_root,
                clipkit_root=args.clipkit_root,
            )
        elif args.command == "catalog":
            contract = build_external_motion_catalog(
                args.onda_root,
                remotion_bits_root=args.remotion_bits_root,
                clipkit_root=args.clipkit_root,
            )
            output = write_external_motion_contract(contract, args.output)
            payload = {
                "status": "catalog_ready",
                "artifact": str(output),
                "catalog_sha256": contract["catalog_sha256"],
                "counts": contract["counts"],
                "candidate_only": True,
            }
        elif args.command == "plan":
            contract = build_external_motion_selection_plan(
                args.catalog,
                args.semantic_motion_plan,
                overrides_path=args.overrides,
            )
            output = write_external_motion_contract(contract, args.output)
            payload = {
                "status": "selection_plan_ready",
                "artifact": str(output),
                "plan_sha256": contract["plan_sha256"],
                "selection_count": len(contract["selections"]),
                "candidate_only": True,
            }
        elif args.command == "bootstrap-clipkit":
            result = bootstrap_clipkit_sfx_pack(
                args.clipkit_root,
                args.output_root,
                purpose=args.purpose,
                platform=args.platform,
            )
            payload = {
                "status": "clipkit_pack_ready",
                "receipt": str(result["receipt"]),
                "profiles": [str(path) for path in result["profiles"]],
                "pack_sha256": result["payload"]["pack_sha256"],
                "candidate_only": True,
                "automatic_download": False,
            }
        else:
            artifact = Path(args.artifact).expanduser().resolve()
            contract = read_json_object(artifact, "external motion contract")
            validate_external_motion_contract(contract)
            payload = {
                "status": "valid",
                "artifact": str(artifact),
                "schema": contract["schema"],
                "candidate_only": True,
            }
    except (ContractError, OSError, ValueError, TypeError) as exc:
        print(json.dumps({"status": "blocked", "error": str(exc)}), file=sys.stderr)
        return 2
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


def _add_source_args(parser: argparse.ArgumentParser, *, onda_required: bool) -> None:
    parser.add_argument("--onda-root", required=onda_required)
    parser.add_argument("--remotion-bits-root")
    parser.add_argument("--clipkit-root")


if __name__ == "__main__":
    raise SystemExit(main())
