from __future__ import annotations

from common import PROJECT, read_json, write_json


def main() -> None:
    edl = read_json(PROJECT / "edit" / "edl.json")
    materials = read_json(PROJECT / "selected" / "project_materials.json")["selected"]
    scenes = []
    for item, segment in zip(materials, edl["ranges"]):
        scenes.append(
            {
                "start": segment["output_start"],
                "end": round(
                    segment["output_start"] + (segment["end"] - segment["start"]), 3
                ),
                "asset": item["path"],
                "use": item["use"],
                "visual_intent": f"show {item['use']} material in a clean MVP layout",
                "layout": "full-frame source with final subtitles",
                "animation": "title card before rough cut; future HyperFrames overlay slot",
                "caption": "burned SRT in final render",
                "audio": "source audio plus music_edit_plan.v1 through the single FFmpeg outlet",
            }
        )
    plan = {
        "project": "demo_project",
        "aspect_ratio": "16:9",
        "target": "mvp-local",
        "style": {
            "mood": "clear, minimal, workflow demo",
            "colors": ["#111827", "#FFFFFF", "#38BDF8"],
            "font": "Arial or system sans",
        },
        "inputs": {
            "rough_cut": "edit/rough_cut.mp4",
            "subtitles": "edit/subtitles.srt",
            "materials": "selected/project_materials.md",
        },
        "scenes": scenes,
        "music_route": (
            "intent curve -> reviewed provider retrieval -> automatic hard gates "
            "-> single FFmpeg outlet"
        ),
        "render_route": "ffmpeg-mvp-now; HyperFrames-slot-later",
    }
    write_json(PROJECT / "render" / "video_packaging_plan.json", plan)
    print("packaging plan ready")


if __name__ == "__main__":
    main()
