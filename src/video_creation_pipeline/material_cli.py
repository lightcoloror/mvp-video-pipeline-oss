from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .contract_io import read_json_object
from .errors import ContractError
from .execution_receipts import (
    build_speech_input_contract,
    validate_ffmpeg_execution_receipt,
    validate_speech_execution_receipt,
    write_execution_contract,
)
from .local_multimodal import (
    build_frame_manifest,
    build_local_multimodal_plan,
    validate_local_multimodal_receipt,
    write_local_multimodal_contract,
)
from .material_index import (
    build_material_candidates_from_index,
    build_material_index_benchmark,
    write_material_index_contract,
)
from .rough_cut_workspace import (
    build_material_retry_plan,
    build_rough_cut_finalize_receipt,
    build_rough_cut_workspace,
    write_rough_cut_contract,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Build offline rough-cut, local-provider, and material-index contracts."
        )
    )
    commands = parser.add_subparsers(dest="command", required=True)

    workspace = commands.add_parser("build-rough-workspace")
    workspace.add_argument("--storyboard", required=True)
    workspace.add_argument("--candidates", required=True)
    workspace.add_argument("--allow-material", action="append", default=[])
    workspace.add_argument("--output", required=True)

    retry = commands.add_parser("build-retry-plan")
    retry.add_argument("--workspace", required=True)
    retry.add_argument("--round-one-selection", required=True)
    retry.add_argument("--output", required=True)

    finalize = commands.add_parser("finalize-rough-cut")
    finalize.add_argument("--workspace", required=True)
    finalize.add_argument("--round-one-selection", required=True)
    finalize.add_argument("--retry-plan")
    finalize.add_argument("--round-two-candidates")
    finalize.add_argument("--round-two-selection")
    finalize.add_argument("--confirmed-by", required=True)
    finalize.add_argument("--confirmed-at", required=True)
    finalize.add_argument("--output", required=True)

    speech = commands.add_parser("build-speech-input")
    speech.add_argument("--receipt", required=True)
    speech.add_argument("--output", required=True)

    verify_speech = commands.add_parser("verify-speech-receipt")
    verify_speech.add_argument("--receipt", required=True)

    verify_ffmpeg = commands.add_parser("verify-ffmpeg-receipt")
    verify_ffmpeg.add_argument("--receipt", required=True)

    frames = commands.add_parser("build-frame-manifest")
    frames.add_argument("--source-media", required=True)
    frames.add_argument("--ffmpeg-receipt", required=True)
    frames.add_argument("--output", required=True)

    llama = commands.add_parser("build-llama-plan")
    llama.add_argument("--frame-manifest", required=True)
    llama.add_argument("--prompt", required=True)
    llama.add_argument("--prompt-version", required=True)
    llama.add_argument("--model", required=True)
    llama.add_argument(
        "--endpoint",
        default="http://127.0.0.1:8080/v1/chat/completions",
    )
    llama.add_argument("--output", required=True)

    verify_llama = commands.add_parser("verify-llama-receipt")
    verify_llama.add_argument("--plan", required=True)
    verify_llama.add_argument("--receipt", required=True)

    benchmark = commands.add_parser("benchmark-material-index")
    benchmark.add_argument("--goldset", required=True)
    benchmark.add_argument("--results", required=True)
    benchmark.add_argument("--min-recall-at-5", type=float, default=0.8)
    benchmark.add_argument("--min-recall-at-10", type=float, default=0.9)
    benchmark.add_argument("--min-filtered-recall", type=float, default=0.8)
    benchmark.add_argument("--min-adjacent-coverage", type=float, default=0.8)
    benchmark.add_argument("--output", required=True)

    candidates = commands.add_parser("build-index-candidates")
    candidates.add_argument("--benchmark", required=True)
    candidates.add_argument("--shot-queries", required=True)
    candidates.add_argument("--output", required=True)
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
    if args.command == "build-rough-workspace":
        payload = build_rough_cut_workspace(
            args.storyboard,
            args.candidates,
            allowed_material_ids=args.allow_material,
        )
        output = write_rough_cut_contract(payload, args.output)
        return _written(output, payload, "workspace_sha256")
    if args.command == "build-retry-plan":
        payload = build_material_retry_plan(args.workspace, args.round_one_selection)
        output = write_rough_cut_contract(payload, args.output)
        return _written(output, payload, "retry_plan_sha256")
    if args.command == "finalize-rough-cut":
        payload = build_rough_cut_finalize_receipt(
            args.workspace,
            args.round_one_selection,
            retry_plan_path=args.retry_plan,
            round_two_candidates_path=args.round_two_candidates,
            round_two_selection_path=args.round_two_selection,
            confirmed_by=args.confirmed_by,
            confirmed_at=args.confirmed_at,
        )
        output = write_rough_cut_contract(payload, args.output)
        result = _written(output, payload, "finalize_receipt_sha256")
        result["render_eligible"] = payload["readiness"]["render_eligible"]
        return result
    if args.command == "build-speech-input":
        payload = build_speech_input_contract(args.receipt)
        output = write_execution_contract(payload, args.output)
        return _written(output, payload, "contract_sha256")
    if args.command == "verify-speech-receipt":
        payload = read_json_object(args.receipt, "speech execution receipt")
        validate_speech_execution_receipt(payload)
        return {"valid": True, "schema": payload["schema"], "owner": payload["owner"]}
    if args.command == "verify-ffmpeg-receipt":
        payload = read_json_object(args.receipt, "FFmpeg execution receipt")
        validate_ffmpeg_execution_receipt(payload)
        return {
            "valid": True,
            "schema": payload["schema"],
            "outlet_id": payload["outlet_id"],
            "operation": payload["operation"],
        }
    if args.command == "build-frame-manifest":
        payload = build_frame_manifest(args.source_media, args.ffmpeg_receipt)
        output = write_local_multimodal_contract(payload, args.output)
        return _written(output, payload, "manifest_sha256")
    if args.command == "build-llama-plan":
        payload = build_local_multimodal_plan(
            args.frame_manifest,
            args.prompt,
            args.model,
            prompt_version=args.prompt_version,
            endpoint=args.endpoint,
        )
        output = write_local_multimodal_contract(payload, args.output)
        return _written(output, payload, "plan_sha256")
    if args.command == "verify-llama-receipt":
        plan = read_json_object(args.plan, "local multimodal plan")
        receipt = read_json_object(args.receipt, "local multimodal receipt")
        return validate_local_multimodal_receipt(plan, receipt)
    if args.command == "benchmark-material-index":
        payload = build_material_index_benchmark(
            args.goldset,
            args.results,
            min_recall_at_5=args.min_recall_at_5,
            min_recall_at_10=args.min_recall_at_10,
            min_filtered_recall=args.min_filtered_recall,
            min_adjacent_coverage=args.min_adjacent_coverage,
        )
        output = write_material_index_contract(payload, args.output)
        result = _written(output, payload, "benchmark_sha256")
        result["passed"] = payload["gate"]["passed"]
        return result
    payload = build_material_candidates_from_index(args.benchmark, args.shot_queries)
    output = write_material_index_contract(payload, args.output)
    return {
        "output": str(output),
        "schema": payload["schema"],
        "shot_count": len(payload["shots"]),
        "acceptance_status": "candidate_only",
        "execution_performed": False,
    }


def _written(
    output: Path, payload: dict[str, object], digest_field: str
) -> dict[str, object]:
    return {
        "output": str(output),
        "schema": payload["schema"],
        digest_field: payload[digest_field],
        "network_execution_performed": False,
        "ffmpeg_execution_performed": False,
        "asr_execution_performed": False,
        "provider_execution_performed": False,
    }


if __name__ == "__main__":
    raise SystemExit(main())
