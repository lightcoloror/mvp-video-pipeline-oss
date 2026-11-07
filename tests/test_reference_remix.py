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

from video_creation_pipeline.reference_remix import (
    build_material_match_plan,
    build_voice_timing_plan,
    normalize_reference_recipe,
)
from video_creation_pipeline.remix_cli import main as remix_cli_main


class ReferenceRemixTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = ROOT / ".test-runtime" / self._testMethodName
        if self.root.exists():
            shutil.rmtree(self.root)
        self.root.mkdir(parents=True)

    def tearDown(self) -> None:
        if self.root.exists():
            shutil.rmtree(self.root)

    def test_normalizes_reference_as_candidate_structure_only(self) -> None:
        recipe = normalize_reference_recipe(self.recipe())

        self.assertEqual(recipe["shots"][0]["duration_s"], 2.0)
        self.assertFalse(recipe["human_review"]["cut_points_confirmed"])
        self.assertEqual(
            recipe["creative_boundary"]["reference_role"],
            "structure_and_pacing_candidate",
        )
        self.assertFalse(recipe["creative_boundary"]["copy_reference_media"])

    def test_scores_candidates_with_context_penalty_and_copy_only_plan(self) -> None:
        candidates = self.candidates()
        candidates["shots"][1]["candidates"] = [
            self.candidate(
                "asset-a2",
                "raw/a2.mp4",
                0.78,
                source="source-a",
                room="room-1",
                composition="close",
            ),
            self.candidate(
                "asset-b",
                "raw/b.mp4",
                0.72,
                source="source-b",
                room="room-2",
                composition="wide",
            ),
        ]

        plan = build_material_match_plan(self.recipe(), candidates)

        second = plan["assignments"][1]
        self.assertEqual(second["selected"]["asset_id"], "asset-b")
        self.assertEqual(second["copy_instruction"]["operation"], "copy")
        self.assertFalse(second["copy_instruction"]["executed"])
        self.assertEqual(plan["execution_boundary"]["files_moved"], 0)
        self.assertFalse(plan["execution_boundary"]["ffmpeg_executed"])

    def test_missing_material_is_a_hard_gate_without_fallback(self) -> None:
        candidates = self.candidates()
        candidates["shots"][2]["candidates"][0]["confidence"] = 0.59

        plan = build_material_match_plan(self.recipe(), candidates)

        third = plan["assignments"][2]
        self.assertEqual(third["status"], "missing_material")
        self.assertIsNone(third["selected"])
        self.assertFalse(third["automatic_fallback"])
        self.assertEqual(plan["readiness"]["status"], "needs_material")

    def test_rejects_third_consecutive_use_of_the_same_source(self) -> None:
        candidates = self.candidates()
        for index, row in enumerate(candidates["shots"]):
            row["candidates"] = [
                self.candidate(
                    f"asset-a{index}",
                    f"raw/a{index}.mp4",
                    0.9,
                    source="source-a",
                    room=f"room-{index}",
                    composition=f"composition-{index}",
                )
            ]

        plan = build_material_match_plan(
            self.recipe(), candidates, max_consecutive_uses=2
        )

        third = plan["assignments"][2]
        self.assertEqual(third["status"], "missing_material")
        self.assertTrue(third["ranked_candidates"][0]["hard_rejected"])
        self.assertEqual(
            third["ranked_candidates"][0]["rejection_reason"],
            "max_consecutive_source_use",
        )

    def test_voice_measurements_drive_timing_and_surface_mismatch(self) -> None:
        measurements = {
            "schema": "video_creation_pipeline.voice_measurements.v1",
            "shots": [
                {
                    "shot_id": "shot-001",
                    "audio_path": "voice/shot-001.wav",
                    "measured_duration_s": 2.05,
                },
                {
                    "shot_id": "shot-002",
                    "audio_path": "voice/shot-002.wav",
                    "measured_duration_s": 3.8,
                },
            ],
        }

        plan = build_voice_timing_plan(self.recipe(), measurements)

        self.assertEqual(plan["shots"][0]["status"], "within_tolerance")
        self.assertEqual(plan["shots"][1]["status"], "needs_timing_review")
        self.assertEqual(plan["shots"][1]["timeline_duration_s"], 3.8)
        self.assertEqual(plan["shots"][2]["status"], "missing_voice_measurement")
        self.assertFalse(plan["execution_boundary"]["tts_executed"])
        self.assertFalse(plan["execution_boundary"]["audio_concatenated"])

    def test_cli_writes_auditable_match_plan(self) -> None:
        recipe_path = self.root / "recipe.json"
        candidates_path = self.root / "candidates.json"
        output = self.root / "match-plan.json"
        recipe_path.write_text(json.dumps(self.recipe()), encoding="utf-8")
        candidates_path.write_text(json.dumps(self.candidates()), encoding="utf-8")

        code = remix_cli_main(
            [
                "build-match-plan",
                "--recipe",
                str(recipe_path),
                "--candidates",
                str(candidates_path),
                "--output",
                str(output),
            ]
        )

        self.assertEqual(code, 0)
        payload = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(
            payload["schema"], "video_creation_pipeline.material_match_plan.v1"
        )
        self.assertEqual(len(payload["plan_sha256"]), 64)

    @staticmethod
    def recipe() -> dict:
        return {
            "schema": "video_creation_pipeline.reference_recipe.v1",
            "reference": {"title": "测试参考结构"},
            "shots": [
                {
                    "shot_id": "shot-001",
                    "start_s": 0,
                    "end_s": 2,
                    "script": "开场",
                    "requirements": {"scene": "产品近景", "avoid": ["品牌水印"]},
                },
                {
                    "shot_id": "shot-002",
                    "start_s": 2,
                    "end_s": 5,
                    "script": "卖点",
                    "requirements": {"scene": "功能演示"},
                },
                {
                    "shot_id": "shot-003",
                    "start_s": 5,
                    "end_s": 7,
                    "script": "行动提示",
                    "requirements": {"scene": "产品全景"},
                },
            ],
        }

    def candidates(self) -> dict:
        return {
            "schema": "video_creation_pipeline.material_candidates.v1",
            "shots": [
                {
                    "shot_id": "shot-001",
                    "candidates": [
                        self.candidate(
                            "asset-a",
                            "raw/a.mp4",
                            0.9,
                            source="source-a",
                            room="room-1",
                            composition="close",
                        )
                    ],
                },
                {
                    "shot_id": "shot-002",
                    "candidates": [
                        self.candidate(
                            "asset-b",
                            "raw/b.mp4",
                            0.85,
                            source="source-b",
                            room="room-2",
                            composition="wide",
                        )
                    ],
                },
                {
                    "shot_id": "shot-003",
                    "candidates": [
                        self.candidate(
                            "asset-c",
                            "raw/c.mp4",
                            0.8,
                            source="source-c",
                            room="room-3",
                            composition="top",
                        )
                    ],
                },
            ],
        }

    @staticmethod
    def candidate(
        asset_id: str,
        path: str,
        confidence: float,
        *,
        source: str,
        room: str,
        composition: str,
    ) -> dict:
        return {
            "asset_id": asset_id,
            "path": path,
            "confidence": confidence,
            "source_media_id": source,
            "room_id": room,
            "composition_id": composition,
            "score_components": {"semantic": confidence},
            "reason": "测试候选",
        }


if __name__ == "__main__":
    unittest.main()
