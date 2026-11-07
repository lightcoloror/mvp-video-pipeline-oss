from __future__ import annotations

from common import PROJECT, read_json, write_csv, write_json


def infer_tags(file_name: str) -> dict[str, str]:
    lowered = file_name.lower()
    if "bad" in lowered:
        return {
            "use": "none",
            "content": "talking-head",
            "topic": "demo",
            "status": "rejected",
            "quality": "C",
            "rights": "self-generated",
        }
    if "hook" in lowered:
        return {
            "use": "hook",
            "content": "talking-head",
            "topic": "demo",
            "status": "usable",
            "quality": "A",
            "rights": "self-generated",
        }
    if "screen" in lowered:
        return {
            "use": "evidence",
            "content": "screen-recording",
            "topic": "demo",
            "status": "usable",
            "quality": "A",
            "rights": "self-generated",
        }
    return {
        "use": "b-roll",
        "content": "b-roll",
        "topic": "demo",
        "status": "usable",
        "quality": "B",
        "rights": "self-generated",
    }


def main() -> None:
    manifest = read_json(PROJECT / "metadata" / "ingest_manifest.json")
    index = []
    tag_rows = []
    for item in manifest:
        tags = infer_tags(item["file"])
        index.append(
            {
                **item,
                "tags": tags,
                "semantic_summary": f"{tags['use']} {tags['content']} {tags['topic']}",
            }
        )
        tag_rows.append({"file": item["file"], **tags})
    write_json(PROJECT / "metadata" / "asset_index.json", index)
    write_csv(
        PROJECT / "metadata" / "tags.csv",
        tag_rows,
        ["file", "use", "content", "topic", "status", "quality", "rights"],
    )
    print(f"indexed and tagged {len(tag_rows)} clips")


if __name__ == "__main__":
    main()
