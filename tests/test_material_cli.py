from __future__ import annotations

# ruff: noqa: E402

import json
import shutil
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from video_creation_pipeline.material_cli import main as material_cli_main
from video_creation_pipeline.rough_cut_workspace import ROUGH_CUT_WORKSPACE_SCHEMA


class MaterialCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = ROOT / ".test-runtime" / self._testMethodName
        if self.root.exists():
            shutil.rmtree(self.root)
        self.root.mkdir(parents=True)

    def tearDown(self) -> None:
        if self.root.exists():
            shutil.rmtree(self.root)

    def test_builds_rough_workspace_without_executing_media_tools(self) -> None:
        storyboard = self._json(
            "storyboard.json",
            {
                "schema": "video_creation_pipeline.reference_recipe.v1",
                "shots": [{"shot_id": "shot-001", "start_s": 0, "end_s": 2}],
            },
        )
        candidates = self._json(
            "candidates.json",
            {
                "schema": "video_creation_pipeline.material_candidates.v1",
                "shots": [
                    {
                        "shot_id": "shot-001",
                        "candidates": [
                            {
                                "asset_id": "asset-a",
                                "path": "raw/asset-a.mp4",
                                "confidence": 0.9,
                                "score_components": {"semantic": 0.9},
                            }
                        ],
                    }
                ],
            },
        )
        output = self.root / "workspace.json"

        code = material_cli_main(
            [
                "build-rough-workspace",
                "--storyboard",
                str(storyboard),
                "--candidates",
                str(candidates),
                "--output",
                str(output),
            ]
        )

        self.assertEqual(code, 0)
        payload = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(
            payload["schema"],
            ROUGH_CUT_WORKSPACE_SCHEMA,
        )
        self.assertEqual(
            payload["review_units"],
            [
                {
                    "script_segment_id": "shot-001",
                    "candidate_ids": ["asset-a"],
                    "review_mode": "single_mapped_clip",
                }
            ],
        )
        self.assertFalse(payload["authority_boundary"]["ffmpeg_executed"])
        self.assertFalse(payload["authority_boundary"]["asr_executed"])

    def _json(self, name: str, payload: dict) -> Path:
        path = self.root / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path


if __name__ == "__main__":
    unittest.main()
