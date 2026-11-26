from __future__ import annotations

import argparse
import json
import sys

from .autonomous_candidate import run_autonomous_candidate, validate_autonomous_candidate_run
from .errors import ContractError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Render the fixed offline autonomous candidate fixture."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    run = subparsers.add_parser("run")
    run.add_argument("--output-root", required=True)
    run.add_argument("--fixture-root")
    run.add_argument("--vkp-bundle")
    run.add_argument("--hyperframes-root")
    run.add_argument("--video-use-root")
    run.add_argument("--lifecycle-core-root")
    run.add_argument("--closure-run-id")
    run.add_argument("--timeout", type=float, default=300.0)
    validate = subparsers.add_parser("validate")
    validate.add_argument("--receipt", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "validate":
            payload = validate_autonomous_candidate_run(args.receipt)
            payload = {"status": "valid_candidate_only", "receipt": payload["closure_run_id"]}
        else:
            values = {key: value for key, value in vars(args).items() if value is not None}
            values.pop("command", None)
            values["timeout_s"] = values.pop("timeout")
            payload = run_autonomous_candidate(**values)
    except (ContractError, OSError, ValueError, TypeError) as exc:
        print(json.dumps({"status": "blocked", "error": str(exc)}), file=sys.stderr)
        return 2
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
