from __future__ import annotations

import argparse
import json
import sys

from .errors import ContractError
from .tts_pipeline import (
    build_audio_stitch_qa,
    build_resume_plan,
    build_tts_job,
    build_voice_measurements,
    write_tts_contract,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="构建可审计的长文本 TTS 契约。")
    commands = parser.add_subparsers(dest="command", required=True)

    job = commands.add_parser("build-job", help="生成固定音色和硬上限分段任务")
    job.add_argument("--recipe", required=True)
    job.add_argument("--config", required=True)
    job.add_argument("--output", required=True)

    resume = commands.add_parser("build-resume-plan", help="从任务和回执派生续跑计划")
    resume.add_argument("--job", required=True)
    resume.add_argument("--receipts", required=True)
    resume.add_argument("--output", required=True)

    qa = commands.add_parser("build-audio-qa", help="检查逐段 WAV 与拼接接缝")
    qa.add_argument("--job", required=True)
    qa.add_argument("--receipts", required=True)
    qa.add_argument("--output", required=True)
    qa.add_argument("--max-loudness-delta-db", type=float, default=4.0)
    qa.add_argument("--max-seam-delta", type=float, default=0.35)
    qa.add_argument("--max-clipping-ratio", type=float, default=0.001)
    qa.add_argument("--max-edge-silence-ms", type=float, default=1500.0)

    measurements = commands.add_parser(
        "build-voice-measurements", help="测量按镜头拼接后的 WAV"
    )
    measurements.add_argument("--job", required=True)
    measurements.add_argument("--stitched-outputs", required=True)
    measurements.add_argument("--output", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "build-job":
            payload = build_tts_job(args.recipe, args.config)
            summary = {"chunk_count": len(payload["chunks"])}
        elif args.command == "build-resume-plan":
            payload = build_resume_plan(args.job, args.receipts)
            summary = payload["summary"]
        elif args.command == "build-audio-qa":
            payload = build_audio_stitch_qa(
                args.job,
                args.receipts,
                max_loudness_delta_db=args.max_loudness_delta_db,
                max_seam_delta=args.max_seam_delta,
                max_clipping_ratio=args.max_clipping_ratio,
                max_edge_silence_ms=args.max_edge_silence_ms,
            )
            summary = payload["readiness"]
        else:
            payload = build_voice_measurements(args.job, args.stitched_outputs)
            summary = payload["readiness"]
        output = write_tts_contract(payload, args.output)
    except ContractError as exc:
        print(f"contract error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"output": str(output), **summary}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
