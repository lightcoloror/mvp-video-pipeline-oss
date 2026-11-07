from __future__ import annotations

from common import PROJECT, ensure_dirs, ffprobe_json, media_duration, write_json


def main() -> None:
    ensure_dirs()
    rows = []
    for path in sorted((PROJECT / "raw").glob("*.mp4")):
        info = ffprobe_json(path)
        vstream = next(s for s in info["streams"] if s["codec_type"] == "video")
        rows.append(
            {
                "file": path.name,
                "path": path.as_posix(),
                "duration_s": round(media_duration(path), 3),
                "width": vstream.get("width"),
                "height": vstream.get("height"),
                "codec": vstream.get("codec_name"),
                "status": "ingested",
            }
        )
    write_json(PROJECT / "metadata" / "ingest_manifest.json", rows)
    print(f"ingested {len(rows)} clips")


if __name__ == "__main__":
    main()
