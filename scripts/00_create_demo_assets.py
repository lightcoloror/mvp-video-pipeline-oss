from __future__ import annotations

import shutil

from common import PROJECT, ensure_dirs, run, write_json


CLIPS = [
    {
        "file": "clip_01_hook_good.mp4",
        "text": "HOOK - clear problem",
        "color": "#1F2937",
        "tone": "620",
        "duration": 5,
        "role": "hook",
    },
    {
        "file": "clip_02_bad_take.mp4",
        "text": "BAD TAKE - um restart",
        "color": "#7F1D1D",
        "tone": "350",
        "duration": 4,
        "role": "rejected",
    },
    {
        "file": "clip_03_screen_demo.mp4",
        "text": "SCREEN DEMO - evidence",
        "color": "#14532D",
        "tone": "480",
        "duration": 5,
        "role": "evidence",
    },
    {
        "file": "clip_04_broll_phone.mp4",
        "text": "B-ROLL - phone shot",
        "color": "#1E3A8A",
        "tone": "540",
        "duration": 4,
        "role": "b-roll",
    },
]


def make_clip(clip: dict[str, str | int]) -> None:
    out = PROJECT / "raw" / str(clip["file"])
    run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c={clip['color']}:s=1280x720:r=30:d={clip['duration']}",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={clip['tone']}:duration={clip['duration']}:sample_rate=48000",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(out),
        ]
    )


def make_bgm(filename: str, expression: str) -> None:
    out = PROJECT / "assets" / "music" / filename
    out.parent.mkdir(parents=True, exist_ok=True)
    run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"aevalsrc={expression}:s=48000:d=30",
            "-c:a",
            "pcm_s16le",
            str(out),
        ]
    )


def main() -> None:
    if PROJECT.exists():
        shutil.rmtree(PROJECT)
    ensure_dirs()
    for clip in CLIPS:
        make_clip(clip)
    make_bgm(
        "bgm_clear.wav",
        "0.035*(sin(2*PI*220*t)+0.6*sin(2*PI*277.18*t)+0.4*sin(2*PI*329.63*t))",
    )
    make_bgm(
        "bgm_tense.wav",
        "0.035*(sin(2*PI*110*t)+0.8*sin(2*PI*116.5*t))",
    )
    write_json(PROJECT / "metadata" / "demo_source_manifest.json", CLIPS)
    print(f"demo assets ready: {PROJECT}")


if __name__ == "__main__":
    main()
