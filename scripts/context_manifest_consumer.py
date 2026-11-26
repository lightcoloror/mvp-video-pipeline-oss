"""Thin MVP Video adapter for shared context and bounded review contracts."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SELF_MEDIA_ROOT = REPO_ROOT.parents[1] / "self-media-creation-system"
DEFAULT_REVIEW_CACHE = Path(
    os.environ.get(
        "MVP_VIDEO_REVIEW_CACHE_DIR",
        str(Path(tempfile.gettempdir()) / "video-creation-pipeline" / "review-cache"),
    )
).expanduser().resolve()
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from video_creation_pipeline.video_review_executor import execute_review  # noqa: E402


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


def execute_review_plan(
    plan: Path,
    cache_dir: Path,
    output: Path,
    *,
    check_profile: Path | None = None,
    timeout_s: float = 120.0,
):
    extension = None
    if check_profile is not None:
        extension = json.loads(check_profile.read_text(encoding="utf-8-sig"))
        if not isinstance(extension, dict):
            raise ValueError("check profile must be a JSON object")
    return execute_review(
        plan,
        cache_dir,
        output,
        check_profile_extension=extension,
        timeout_s=timeout_s,
    )


def main() -> int:
    import argparse
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
    execute = subparsers.add_parser(
        "execute-review",
        help="Execute or reuse hash-bound QA through the existing FFmpeg outlet.",
    )
    execute.add_argument("--plan", type=Path, required=True)
    execute.add_argument(
        "--cache-dir",
        type=Path,
        default=DEFAULT_REVIEW_CACHE,
        help="Managed exact-hash cache; omit to share the canonical local cache.",
    )
    execute.add_argument("--output", type=Path, required=True)
    execute.add_argument("--check-profile", type=Path)
    execute.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args()
    if args.command == "plan-review":
        result = plan_review(args.handoff, args.review_manifest, args.output)
    elif args.command == "verify-review":
        result = verify_review(args.plan, args.receipt, args.output)
    else:
        result = execute_review_plan(
            args.plan,
            args.cache_dir,
            args.output,
            check_profile=args.check_profile,
            timeout_s=args.timeout,
        )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 2 if result.get("status") == "blocked" else 0


if __name__ == "__main__":
    raise SystemExit(main())
