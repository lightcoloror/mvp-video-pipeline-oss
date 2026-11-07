from __future__ import annotations


from common import PROJECT, media_duration, read_json, run, write_json


def srt_time(seconds: float) -> str:
    millis = int(round(seconds * 1000))
    h = millis // 3_600_000
    millis %= 3_600_000
    m = millis // 60_000
    millis %= 60_000
    s = millis // 1000
    ms = millis % 1000
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def main() -> None:
    materials = read_json(PROJECT / "selected" / "project_materials.json")["selected"]
    ordered = sorted(
        materials,
        key=lambda x: {"hook": 0, "evidence": 1, "b-roll": 2}.get(x["use"], 9),
    )

    edl_ranges = []
    concat_lines = []
    output_offset = 0.0
    subtitle_blocks = []
    subtitle_text = {
        "hook": "MVP starts from a reusable pipeline.",
        "evidence": "Selected clips become structured project materials.",
        "b-roll": "Packaging and QA turn rough cuts into publishable assets.",
    }

    for idx, item in enumerate(ordered, 1):
        source = PROJECT / item["path"]
        duration = media_duration(source)
        edl_ranges.append(
            {
                "source": item["path"],
                "start": 0,
                "end": round(duration, 3),
                "output_start": round(output_offset, 3),
                "use": item["use"],
                "reason": f"selected as {item['use']}",
            }
        )
        concat_lines.append(f"file '{source.as_posix()}'")
        text = subtitle_text.get(item["use"], "Reusable video workflow.")
        subtitle_blocks.append(
            f"{idx}\n{srt_time(output_offset)} --> {srt_time(output_offset + duration)}\n{text}\n"
        )
        output_offset += duration

    concat_file = PROJECT / "edit" / "concat.txt"
    concat_file.write_text("\n".join(concat_lines), encoding="utf-8")
    write_json(
        PROJECT / "edit" / "edl.json",
        {
            "version": 1,
            "ranges": edl_ranges,
            "total_duration_s": round(output_offset, 3),
            "grade": "demo-neutral",
        },
    )
    (PROJECT / "edit" / "subtitles.srt").write_text(
        "\n".join(subtitle_blocks), encoding="utf-8"
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
            str(concat_file),
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(PROJECT / "edit" / "rough_cut.mp4"),
        ]
    )
    print("rough cut ready")


if __name__ == "__main__":
    main()
