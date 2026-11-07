from __future__ import annotations

import argparse
import json
import sys

from .errors import ContractError
from .music_msclap import (
    build_msclap_retrieval_plan,
    run_msclap_retrieval,
    write_msclap_contract,
)
from .music_pipeline import write_music_contract


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Plan or explicitly run the pinned Microsoft CLAP adapter offline."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    plan = commands.add_parser("build-plan")
    plan.add_argument("--intent", required=True)
    plan.add_argument("--profile", action="append", required=True)
    plan.add_argument("--audio-map", required=True)
    plan.add_argument("--source-root", required=True)
    plan.add_argument("--model", required=True)
    plan.add_argument("--license-snapshot", required=True)
    plan.add_argument("--model-license", required=True)
    plan.add_argument("--commercial-use-allowed", action="store_true")
    plan.add_argument(
        "--purpose", choices=("personal", "commercial"), default="personal"
    )
    plan.add_argument("--hf-cache-dir", required=True)
    plan.add_argument("--device", choices=("cpu", "cuda"), default="cpu")
    plan.add_argument("--random-seed", type=int, default=20260803)
    plan.add_argument("--output", required=True)

    execute = commands.add_parser("run")
    execute.add_argument("--plan", required=True)
    execute.add_argument("--output", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "build-plan":
            payload = build_msclap_retrieval_plan(
                args.intent,
                args.profile,
                args.audio_map,
                args.source_root,
                args.model,
                args.license_snapshot,
                model_license=args.model_license,
                commercial_use_allowed=args.commercial_use_allowed,
                purpose=args.purpose,
                hf_cache_dir=args.hf_cache_dir,
                device=args.device,
                random_seed=args.random_seed,
            )
            output = write_msclap_contract(payload, args.output)
            hash_field = "plan_sha256"
        else:
            payload = run_msclap_retrieval(args.plan)
            output = write_music_contract(payload, args.output)
            hash_field = "retrieval_results_sha256"
    except ContractError as exc:
        print(f"contract error: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "schema": payload["schema"],
                "path": str(output),
                hash_field: payload[hash_field],
                "network_execution_performed": False,
                "automatic_model_download": False,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
