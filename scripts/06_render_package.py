from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from common import PROJECT, ROOT, copy_file, read_json, run

SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from video_creation_pipeline.execution_receipts import (  # noqa: E402
    build_ffmpeg_execution_receipt,
    write_execution_contract,
)
from video_creation_pipeline.music_pipeline import (  # noqa: E402
    build_music_mix_command,
    validate_music_edit_plan,
)


def main() -> None:
    title = PROJECT / "render" / "title_card.mp4"
    rough = PROJECT / "edit" / "rough_cut.mp4"
    rough_captioned = PROJECT / "render" / "rough_captioned.mp4"
    pre_bgm = PROJECT / "render" / "pre_bgm.mp4"
    final = PROJECT / "publish" / "final_1280x720.mp4"

    # Keep the MVP font-independent. Real text/motion packaging should be handled
    # by HyperFrames or Remotion after this skeleton is connected to real assets.
    run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=#334155:s=1280x720:r=30:d=3",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=channel_layout=stereo:sample_rate=48000",
            "-t",
            "3",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(title),
        ]
    )

    copy_file(rough, rough_captioned)

    concat = PROJECT / "render" / "final_concat.txt"
    concat.write_text(
        f"file '{title.as_posix()}'\nfile '{rough_captioned.as_posix()}'\n",
        encoding="utf-8",
    )
    run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat),
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(pre_bgm),
        ]
    )

    music_plan_path = PROJECT / "render" / "music_edit_plan.json"
    music_plan = read_json(music_plan_path)
    validate_music_edit_plan(music_plan)
    selection = _single_demo_selection(music_plan)
    music_path = Path(selection["source_audio"]["path"]).expanduser().resolve()
    command = build_music_mix_command(pre_bgm, music_path, final, selection)
    run(command)

    receipt = build_ffmpeg_execution_receipt(
        "music_mix",
        command,
        [
            {"role": "program_video", "path": str(pre_bgm)},
            {"role": "selected_music", "path": str(music_path)},
            {"role": "music_edit_plan", "path": str(music_plan_path)},
        ],
        [{"role": "mixed_program_video", "path": str(final)}],
    )
    write_execution_contract(receipt, PROJECT / "render" / "music-mix-receipt.json")
    print(f"final render with BGM ready: {final}")


def _single_demo_selection(music_plan: dict[str, Any]) -> dict[str, Any]:
    if music_plan["readiness"]["render_eligible"] is not True:
        raise SystemExit("music edit plan is not render eligible")
    selections = music_plan["selections"]
    if len(selections) != 1 or selections[0]["status"] != "auto_selected":
        raise SystemExit("offline demo expects one automatically selected BGM segment")
    selection = selections[0]
    interval = selection["timeline_interval"]
    if abs(float(interval["start_s"])) > 0.001:
        raise SystemExit("offline demo BGM must start at timeline zero")
    return selection


if __name__ == "__main__":
    main()
