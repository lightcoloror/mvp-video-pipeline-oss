from __future__ import annotations

import argparse
import json
import sys

from .contract_io import read_json_object
from .errors import ContractError
from .music_benchmark import (
    MUSIC_RETRIEVAL_BENCHMARK_SCHEMA,
    build_music_retrieval_benchmark,
    write_music_retrieval_benchmark,
)
from .music_pipeline import (
    build_music_asset_profile,
    build_music_candidate_set,
    build_music_edit_plan,
    build_music_intent_curve,
    build_music_retrieval_results,
    write_music_contract,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build auditable music intent, retrieval, ranking, and edit contracts."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    intent = commands.add_parser("build-intent")
    intent.add_argument("--source", required=True)
    intent.add_argument("--spec", required=True)
    intent.add_argument("--output", required=True)

    profile = commands.add_parser("build-profile")
    profile.add_argument("--audio", required=True)
    profile.add_argument("--spec", required=True)
    profile.add_argument("--output", required=True)

    retrieval = commands.add_parser("build-retrieval-results")
    retrieval.add_argument("--intent", required=True)
    retrieval.add_argument("--spec", required=True)
    retrieval.add_argument("--output", required=True)

    benchmark = commands.add_parser("build-benchmark")
    benchmark.add_argument("--retrieval-results", required=True)
    benchmark.add_argument("--gold-set", required=True)
    benchmark.add_argument("--min-query-count", type=int, default=10)
    benchmark.add_argument("--min-recall-at-5", type=float, default=0.8)
    benchmark.add_argument("--min-recall-at-10", type=float, default=0.9)
    benchmark.add_argument("--min-mrr", type=float, default=0.6)
    benchmark.add_argument("--output", required=True)

    candidates = commands.add_parser("build-candidates")
    candidates.add_argument("--intent", required=True)
    candidates.add_argument("--retrieval-results", required=True)
    candidates.add_argument("--profile", action="append", required=True)
    candidates.add_argument(
        "--purpose", choices=("personal", "commercial"), default="personal"
    )
    candidates.add_argument("--top-k", type=int, default=5)
    candidates.add_argument("--benchmark")
    candidates.add_argument("--output", required=True)

    plan = commands.add_parser("build-edit-plan")
    plan.add_argument("--candidates", required=True)
    plan.add_argument("--min-score", type=float, default=0.72)
    plan.add_argument("--min-margin", type=float, default=0.05)
    plan.add_argument("--word-timestamps")
    plan.add_argument("--subtitles")
    plan.add_argument("--contract-version", choices=("v1", "v2"), default="v2")
    plan.add_argument("--output", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        payload, hash_field = _execute(args)
        if payload.get("schema") == MUSIC_RETRIEVAL_BENCHMARK_SCHEMA:
            output = write_music_retrieval_benchmark(payload, args.output)
        else:
            output = write_music_contract(payload, args.output)
    except ContractError as exc:
        print(f"contract error: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "schema": payload["schema"],
                "path": str(output),
                hash_field: payload[hash_field],
            },
            ensure_ascii=False,
        )
    )
    return 0


def _execute(args: argparse.Namespace) -> tuple[dict[str, object], str]:
    if args.command == "build-intent":
        spec = read_json_object(args.spec, "music intent specification")
        return (
            build_music_intent_curve(
                args.source,
                spec.get("segments") or [],
                created_by=str(
                    spec.get("created_by") or "operator_or_authorized_provider"
                ),
            ),
            "intent_curve_sha256",
        )
    if args.command == "build-profile":
        spec = read_json_object(args.spec, "music asset profile specification")
        return (
            build_music_asset_profile(
                args.audio,
                asset_id=str(spec.get("asset_id") or ""),
                duration_s=spec.get("duration_s", 0),
                windows=spec.get("windows") or [],
                license_info=spec.get("license") or {},
                analyzer=spec.get("analyzer") or {},
            ),
            "profile_sha256",
        )
    if args.command == "build-retrieval-results":
        spec = read_json_object(args.spec, "music retrieval specification")
        return (
            build_music_retrieval_results(
                args.intent,
                spec.get("matches") or [],
                provider=spec.get("provider") or {},
            ),
            "retrieval_results_sha256",
        )
    if args.command == "build-benchmark":
        return (
            build_music_retrieval_benchmark(
                args.retrieval_results,
                args.gold_set,
                min_query_count=args.min_query_count,
                min_recall_at_5=args.min_recall_at_5,
                min_recall_at_10=args.min_recall_at_10,
                min_mrr=args.min_mrr,
            ),
            "benchmark_sha256",
        )
    if args.command == "build-candidates":
        return (
            build_music_candidate_set(
                args.intent,
                args.retrieval_results,
                args.profile,
                purpose=args.purpose,
                top_k=args.top_k,
                retrieval_benchmark_path=args.benchmark,
            ),
            "candidate_set_sha256",
        )
    return (
        build_music_edit_plan(
            args.candidates,
            min_score=args.min_score,
            min_margin=args.min_margin,
            word_timestamps_path=args.word_timestamps,
            subtitles_path=args.subtitles,
            contract_version=args.contract_version,
        ),
        "music_edit_plan_sha256",
    )


if __name__ == "__main__":
    raise SystemExit(main())
