"""Thin MVP Video adapter for shared context and bounded review contracts."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SELF_MEDIA_ROOT = REPO_ROOT.parents[1] / "self-media-creation-system"


def _shared(module_name="shared_context_efficiency", filename="context_efficiency.py"):
    root = Path(os.environ.get("SELF_MEDIA_SYSTEM_ROOT", str(DEFAULT_SELF_MEDIA_ROOT)))
    spec = importlib.util.spec_from_file_location(
        module_name, root / "scripts" / filename
    )
    if not spec or not spec.loader:
        raise RuntimeError("shared context_efficiency.py unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def plan_review(handoff: Path, review_manifest: Path, plan: Path):
    agent = _shared("shared_self_media_agent", "self_media_agent.py")
    agent.validate_step_handoff(handoff, expected_tool_id="mvp-video-pipeline")
    shared = _shared()
    return shared.build_video_review_plan(review_manifest, plan)


def verify_review(plan: Path, receipt: Path, output: Path):
    return _shared().verify_video_review_receipt(plan, receipt, output)


def main() -> int:
    import argparse
    import json

    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    plan = subparsers.add_parser("plan-review")
    plan.add_argument("--handoff", type=Path, required=True)
    plan.add_argument("--review-manifest", type=Path, required=True)
    plan.add_argument("--output", type=Path, required=True)
    verify = subparsers.add_parser("verify-review")
    verify.add_argument("--plan", type=Path, required=True)
    verify.add_argument("--receipt", type=Path, required=True)
    verify.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "plan-review":
        result = plan_review(args.handoff, args.review_manifest, args.output)
    else:
        result = verify_review(args.plan, args.receipt, args.output)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
