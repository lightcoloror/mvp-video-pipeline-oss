from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .content_video_cli import main as content_video_main


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the canonical content-video synthetic candidate path. "
            "Age expiry is enforced only for explicitly high-temporal content."
        )
    )
    parser.add_argument("--brief", required=True)
    parser.add_argument("--schema", required=True)
    parser.add_argument("--research-output", required=True)
    parser.add_argument("--research-schema", required=True)
    parser.add_argument("--output-root")
    parser.add_argument(
        "--time-sensitivity",
        choices=("high_temporal", "standard", "evergreen"),
        default="standard",
    )
    parser.add_argument("--max-age-hours", type=float, default=168.0)
    parser.add_argument("--ffmpeg-timeout", type=float, default=180.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    workspace = (
        Path(os.environ.get("MVP_VIDEO_WORKSPACE", REPOSITORY_ROOT / "workspace"))
        .expanduser()
        .resolve()
    )
    output_root = (
        Path(args.output_root).expanduser().resolve()
        if args.output_root
        else workspace / "demo_project"
    )
    exit_code = content_video_main(
        [
            "generate-synthetic",
            "--brief",
            str(Path(args.brief).expanduser().resolve()),
            "--schema",
            str(Path(args.schema).expanduser().resolve()),
            "--research-output",
            str(Path(args.research_output).expanduser().resolve()),
            "--research-schema",
            str(Path(args.research_schema).expanduser().resolve()),
            "--output-root",
            str(output_root),
            "--max-age-hours",
            str(args.max_age_hours),
            "--time-sensitivity",
            args.time_sensitivity,
            "--ffmpeg-timeout",
            str(args.ffmpeg_timeout),
        ]
    )
    if exit_code != 0:
        return exit_code

    receipt_path = output_root / "synthetic-run-receipt.json"
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        candidate = Path(receipt["candidate_video"]["path"])
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        print(
            json.dumps(
                {
                    "status": "blocked",
                    "error": f"canonical demo receipt is unavailable: {exc}",
                }
            ),
            file=sys.stderr,
        )
        return 2
    if receipt.get("status") != "completed_candidate_only" or not candidate.is_file():
        print(
            json.dumps(
                {
                    "status": "blocked",
                    "error": "canonical demo did not produce a candidate-only artifact",
                }
            ),
            file=sys.stderr,
        )
        return 2
    print("\nCanonical MVP pipeline completed as candidate_only.")
    print(f"Candidate: {candidate.resolve()}")
    print(f"Receipt: {receipt_path.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
