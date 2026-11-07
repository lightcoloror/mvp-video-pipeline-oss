from __future__ import annotations

import argparse
import json
import sys

from .contract_io import read_json_object
from .decomposition_adoption import (
    build_decomposition_adoption,
    build_reference_recipe_from_decomposition,
    validate_decomposition_adoption,
    validate_vkp_decomposition_report,
    write_decomposition_adoption,
    write_decomposition_reference_recipe,
)
from .errors import ContractError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Consume evidence-bound VKP video decomposition reports."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate_report = subparsers.add_parser("validate-report")
    validate_report.add_argument("--report", required=True)

    adoption = subparsers.add_parser("build-adoption")
    adoption.add_argument("--report", required=True)
    adoption.add_argument("--output", required=True)

    recipe = subparsers.add_parser("build-reference-recipe")
    recipe.add_argument("--report", required=True)
    recipe.add_argument("--output", required=True)

    validate_adoption = subparsers.add_parser("validate-adoption")
    validate_adoption.add_argument("--adoption", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = _execute(args)
    except ContractError as exc:
        print(f"contract error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0


def _execute(args: argparse.Namespace) -> dict[str, object]:
    if args.command == "validate-report":
        report = read_json_object(args.report, "VKP decomposition report")
        validate_vkp_decomposition_report(report)
        return {
            "schema": report["schema"],
            "valid": True,
            "finding_count": len(report["findings"]),
            "network_execution_performed": False,
        }
    if args.command == "build-adoption":
        payload = build_decomposition_adoption(args.report)
        output = write_decomposition_adoption(payload, args.output)
        return {
            "output": str(output),
            "schema": payload["schema"],
            "adoption_sha256": payload["adoption_sha256"],
            "network_execution_performed": False,
        }
    if args.command == "build-reference-recipe":
        payload = build_reference_recipe_from_decomposition(args.report)
        output = write_decomposition_reference_recipe(payload, args.output)
        return {
            "output": str(output),
            "schema": payload["schema"],
            "shot_count": len(payload["shots"]),
            "network_execution_performed": False,
        }
    payload = read_json_object(args.adoption, "decomposition adoption")
    validate_decomposition_adoption(payload)
    return {
        "schema": payload["schema"],
        "valid": True,
        "network_execution_performed": False,
    }


if __name__ == "__main__":
    raise SystemExit(main())
