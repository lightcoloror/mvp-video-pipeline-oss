"""CLI entrypoint for deterministic motion timeline checks & 1:1 crop / strip still QA."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from video_creation_pipeline.motion_still_qa import (  # noqa: E402
    build_still_qa_artifacts,
    parse_crop_spec,
    parse_strip_spec,
    validate_timeline_continuity,
)


def _cmd_timeline_check(args: argparse.Namespace) -> int:
    payload = json.loads(Path(args.timeline).read_text(encoding="utf-8"))
    segments = payload["segments"] if isinstance(payload, dict) else payload
    fps = args.fps or (payload.get("fps") if isinstance(payload, dict) else None) or 30
    expected_total = (
        args.expected_total_frames
        if args.expected_total_frames is not None
        else (payload.get("expected_total_frames") if isinstance(payload, dict) else None)
    )
    report = validate_timeline_continuity(
        segments,
        fps=fps,
        expected_total_frames=expected_total,
        beat_frames=args.beat_frames,
    )
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if report["passed"] else 2


def _cmd_still_inspect(args: argparse.Namespace) -> int:
    frames: list[int] = list(args.frames or [])
    make_strip = bool(args.strip or args.sheet)
    if args.strip:
        frames.extend(parse_strip_spec(args.strip))
    crops = [parse_crop_spec(spec) for spec in (args.crop or [])]
    receipt = build_still_qa_artifacts(
        output_dir=Path(args.out),
        frame_indices=frames,
        frames_dir=Path(args.frames_dir) if args.frames_dir else None,
        video_path=Path(args.video) if args.video else None,
        crops=crops,
        make_strip=make_strip,
        strip_cols=args.cols,
        tile_width=args.tile_width,
        ffmpeg_bin=args.ffmpeg,
    )
    print(json.dumps(receipt, ensure_ascii=False, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Motion timeline assertion & 1:1 still/strip QA")
    sub = parser.add_subparsers(dest="command", required=True)

    p_tl = sub.add_parser("timeline-check", help="Validate shot/motion frame continuity (fail-closed)")
    p_tl.add_argument("--timeline", required=True, help="Path to timeline JSON ({fps, segments} or list)")
    p_tl.add_argument("--fps", type=float, default=None)
    p_tl.add_argument("--expected-total-frames", type=int, default=None)
    p_tl.add_argument("--beat-frames", type=int, default=None)
    p_tl.add_argument("--output", default=None)
    p_tl.set_defaults(func=_cmd_timeline_check)

    p_st = sub.add_parser("still-inspect", help="Generate 1:1 pixel crops and motion strip contact sheets")
    src_group = p_st.add_mutually_exclusive_group(required=True)
    src_group.add_argument("--frames-dir", help="Directory containing rendered frames (f0000.png ...)")
    src_group.add_argument("--video", help="Rendered MP4 video path (uses ffmpeg)")
    p_st.add_argument("--frames", nargs="*", type=int, default=[], help="Explicit frame indices")
    p_st.add_argument("--strip", default=None, help="Consecutive frame range 'start:end[:step]'")
    p_st.add_argument("--sheet", action="store_true", help="Build contact sheet even without --strip")
    p_st.add_argument("--crop", action="append", default=[], help="1:1 crop box 'x,y[,w,h]' (repeatable)")
    p_st.add_argument("--cols", type=int, default=3)
    p_st.add_argument("--tile-width", type=int, default=480)
    p_st.add_argument("--ffmpeg", default=None)
    p_st.add_argument("--out", required=True, help="Output directory for QA crops/strip/receipt")
    p_st.set_defaults(func=_cmd_still_inspect)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
