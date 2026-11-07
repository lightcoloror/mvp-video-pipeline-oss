"""Run one explicit FFmpeg job through the canonical guarded outlet.

This is a thin command-line adapter for legacy workspace scripts.  It does not
create another FFmpeg scheduler: all execution remains owned by
``video_creation_pipeline.single_ffmpeg_outlet``.
"""

from __future__ import annotations

import argparse
import inspect
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from video_creation_pipeline.execution_receipts import (
    execute_ffmpeg_with_receipt,
)

try:  # Keep the thin outlet usable before the optional review guard is installed.
    from video_creation_pipeline.review_resource_guard import (
        ResourcePressurePauseRequired,
    )
except ImportError:  # pragma: no cover - exercised by the clean release snapshot.
    ResourcePressurePauseRequired = None  # type: ignore[assignment,misc]


def _role_path(value: str, label: str) -> dict[str, object]:
    role, separator, raw_path = value.partition("=")
    if not separator or not role.strip() or not raw_path.strip():
        raise ValueError(f"{label} must use role=path")
    return {"role": role.strip(), "path": Path(raw_path).expanduser().resolve()}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--operation", required=True)
    parser.add_argument("--input", action="append", default=[], dest="inputs")
    parser.add_argument("--output", action="append", default=[], dest="outputs")
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--capture-log", type=Path)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        help="FFmpeg argv after --; the first item must be ffmpeg.",
    )
    args = parser.parse_args()
    if args.command and args.command[0] == "--":
        args.command = args.command[1:]
    if not args.command or args.command[0] != "ffmpeg":
        parser.error("command after -- must start with ffmpeg")
    if not args.inputs or not args.outputs:
        parser.error("at least one --input and --output are required")
    return args


def main() -> int:
    args = parse_args()
    inputs = [_role_path(value, "input") for value in args.inputs]
    outputs = [_role_path(value, "output") for value in args.outputs]
    execute_kwargs = {
        "capture_log_path": args.capture_log,
        "timeout_s": args.timeout,
    }
    signature = inspect.signature(execute_ffmpeg_with_receipt)
    if "guard_resource" in signature.parameters or any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    ):
        execute_kwargs["guard_resource"] = True
    try:
        receipt = execute_ffmpeg_with_receipt(
            args.operation,
            args.command,
            inputs,
            outputs,
            args.receipt,
            **execute_kwargs,
        )
    except Exception as exc:
        if ResourcePressurePauseRequired is not None and isinstance(
            exc, ResourcePressurePauseRequired
        ):
            print(json.dumps(exc.as_dict(), ensure_ascii=False, sort_keys=True))
            return 3
        raise
    print(
        json.dumps(
            {
                "status": "completed",
                "receipt": str(args.receipt.expanduser().resolve()),
                "receipt_sha256": receipt["receipt_sha256"],
                "resource_guard": receipt.get("resource_guard"),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
