from __future__ import annotations

from common import PROJECT, copy_file, markdown_table, read_json, write_json


def main() -> None:
    index = read_json(PROJECT / "metadata" / "asset_index.json")
    selected = []
    rejected = []
    for item in index:
        tags = item["tags"]
        if tags["status"] == "usable":
            src = PROJECT / "raw" / item["file"]
            dst = PROJECT / "selected" / item["file"]
            copy_file(src, dst)
            selected.append(
                {
                    "file": item["file"],
                    "path": f"selected/{item['file']}",
                    "use": tags["use"],
                    "quality": tags["quality"],
                    "duration_s": item["duration_s"],
                }
            )
        else:
            rejected.append({"file": item["file"], "reason": "demo rejected take"})

    write_json(
        PROJECT / "selected" / "project_materials.json",
        {"selected": selected, "rejected": rejected},
    )
    md = [
        "# 项目素材清单",
        "",
        "## 已选素材",
        "",
        markdown_table(selected, ["file", "path", "use", "quality", "duration_s"]),
        "",
        "## 弃用素材",
        "",
        markdown_table(rejected, ["file", "reason"]),
        "",
    ]
    (PROJECT / "selected" / "project_materials.md").write_text(
        "\n".join(md), encoding="utf-8"
    )
    print(f"selected {len(selected)} clips, rejected {len(rejected)} clips")


if __name__ == "__main__":
    main()
