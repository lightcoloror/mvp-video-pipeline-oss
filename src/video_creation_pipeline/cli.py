from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .vkp_bridge import ContractError, build_edit_job, write_edit_job


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build a local or explicitly authorized remote video-creation contract from a read-only VKP Bundle."
    )
    parser.add_argument("bundle", help="VKP Bundle directory")
    parser.add_argument(
        "--output",
        required=True,
        help="Output JSON path outside the source VKP Bundle",
    )
    parser.add_argument(
        "--confirmed-diffs",
        help="Optional JSON array containing human-confirmed final edit diffs",
    )
    parser.add_argument(
        "--remote-policy",
        help="Optional JSON object authorizing online model calls and explicit artifact uploads",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    diffs = None
    if args.confirmed_diffs:
        path = Path(args.confirmed_diffs).expanduser().resolve()
        try:
            diffs = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"confirmed diff file is unreadable: {exc}", file=sys.stderr)
            return 2
        if not isinstance(diffs, list):
            print("confirmed diff file must contain a JSON array", file=sys.stderr)
            return 2
    remote_policy = None
    if args.remote_policy:
        path = Path(args.remote_policy).expanduser().resolve()
        try:
            remote_policy = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"remote policy file is unreadable: {exc}", file=sys.stderr)
            return 2
        if not isinstance(remote_policy, dict):
            print("remote policy file must contain a JSON object", file=sys.stderr)
            return 2
    try:
        job = build_edit_job(
            args.bundle, confirmed_diffs=diffs, remote_policy=remote_policy
        )
        output = write_edit_job(job, args.output)
    except ContractError as exc:
        print(f"contract error: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "output": str(output),
                "candidate_count": len(job["candidate_clips"]),
                "storyboard_count": len(job["storyboard_scenes"]),
                "readiness": job["readiness"]["status"],
                "remote_execution_authorized": job["remote_capabilities"]["execute"],
                "upload_artifact_count": job["remote_capabilities"]["upload"][
                    "artifact_count"
                ],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
