from __future__ import annotations

import copy
import json
import shutil
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from video_creation_pipeline.contract_io import (
    artifact_reference,
    atomic_write_json,
    payload_sha256,
)
from video_creation_pipeline.errors import ContractError
from video_creation_pipeline.privacy_exclusion_zones import (
    build_privacy_exclusion_zones,
    build_privacy_layout_collision_report,
    media_clock_from_redaction_receipt,
    project_identity_privacy_exclusion_zones,
    project_rect_through_reframe,
    validate_privacy_exclusion_zones,
    validate_privacy_layout_collision_report,
    write_privacy_exclusion_zones,
)
from video_creation_pipeline.reframe_tracking import track_rectangles_with_norfair


class PrivacyExclusionZonesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = ROOT / ".test-runtime" / f"privacy-zones-{uuid4().hex}"
        self.root.mkdir(parents=True)
        self.source = self.root / "source.mp4"
        self.redacted = self.root / "redacted.mp4"
        self.source.write_bytes(b"source-video")
        self.redacted.write_bytes(b"redacted-video")
        self.job_path = self.root / "job.json"
        self.receipt_path = self.root / "receipt.json"
        self._write_redaction_contracts()
        self.clock = {
            "fps": 10.0,
            "frame_count": 10,
            "width": 1000,
            "height": 500,
            "duration_s": 1.0,
        }
        self.detections = [
            {
                "frame_index": 2,
                "frame_width": 1000,
                "frame_height": 500,
                "xyxy": [400, 100, 500, 250],
                "confidence": 0.96,
                "target_type": "face",
            },
            {
                "frame_index": 7,
                "frame_width": 1000,
                "frame_height": 500,
                "xyxy": [420, 100, 520, 250],
                "confidence": 0.94,
                "target_type": "face",
            },
        ]

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_contract_binds_media_job_receipt_and_conservative_boxes(self) -> None:
        tracked = self._tracked_rows()
        with patch(
            "video_creation_pipeline.privacy_exclusion_zones."
            "track_rectangles_with_norfair",
            return_value=tracked,
        ):
            zones = self._build()
        self.assertEqual("redaction_input_normalized", zones["coordinate_space"])
        self.assertFalse(zones["tracks"][0]["identity_claimed"])
        self.assertEqual("review_required", zones["coverage"]["status"])
        first = zones["tracks"][0]["zones"][0]
        self.assertLessEqual(first["effective_rect"]["x"], first["raw_rect"]["x"])
        self.assertGreaterEqual(
            first["effective_rect"]["width"], first["raw_rect"]["width"]
        )
        validate_privacy_exclusion_zones(zones)

    def test_empty_same_invocation_capture_fails_closed(self) -> None:
        with (
            patch(
                "video_creation_pipeline.privacy_exclusion_zones."
                "track_rectangles_with_norfair",
                return_value=[],
            ),
            self.assertRaisesRegex(ContractError, "schema validation failed"),
        ):
            self._build(detections=[])

    def test_hash_rect_and_tool_drift_fail_closed(self) -> None:
        with patch(
            "video_creation_pipeline.privacy_exclusion_zones."
            "track_rectangles_with_norfair",
            return_value=self._tracked_rows(),
        ):
            zones = self._build()

        broken = copy.deepcopy(zones)
        broken["tracks"][0]["zones"][0]["effective_rect"] = {
            "x": 0.45,
            "y": 0.2,
            "width": 0.01,
            "height": 0.01,
        }
        broken["zones_sha256"] = payload_sha256(broken, "zones_sha256")
        with self.assertRaisesRegex(ContractError, "must contain"):
            validate_privacy_exclusion_zones(broken)

        broken = copy.deepcopy(zones)
        broken["detector"]["source_commit"] = "0" * 40
        broken["zones_sha256"] = payload_sha256(broken, "zones_sha256")
        with self.assertRaisesRegex(ContractError, "commit drifted"):
            validate_privacy_exclusion_zones(broken)

        broken = copy.deepcopy(zones)
        broken["zones_sha256"] = "0" * 64
        with self.assertRaisesRegex(ContractError, "integrity"):
            validate_privacy_exclusion_zones(broken)

    def test_fixed_crop_contain_blur_pad_projection_matches_render_geometry(
        self,
    ) -> None:
        rect = {"x": 0.4, "y": 0.2, "width": 0.1, "height": 0.2}
        fixed = self._reframe_plan("fixed_crop")
        fixed_rect, fixed_chain = project_rect_through_reframe(rect, fixed, 0.5)
        self.assertEqual("fixed_crop", fixed_chain["strategy"])
        self.assertAlmostEqual(0.5, fixed_rect["x"], places=6)
        self.assertAlmostEqual(0.2, fixed_rect["width"], places=6)

        contain_rect, _ = project_rect_through_reframe(
            rect, self._reframe_plan("contain"), 0.5
        )
        self.assertGreater(contain_rect["y"], 0.2)
        blur_rect, _ = project_rect_through_reframe(
            rect, self._reframe_plan("blur_pad"), 0.5
        )
        self.assertGreater(blur_rect["y"], contain_rect["y"])

    def test_mixed_projection_is_blocked(self) -> None:
        with self.assertRaisesRegex(ContractError, "mixed or unverified"):
            project_rect_through_reframe(
                {"x": 0.1, "y": 0.1, "width": 0.2, "height": 0.2},
                self._reframe_plan("split_layout"),
                0.5,
            )

    def test_identity_projection_requires_exact_canvas_dimensions(self) -> None:
        with patch(
            "video_creation_pipeline.privacy_exclusion_zones."
            "track_rectangles_with_norfair",
            return_value=self._tracked_rows(),
        ):
            source_zones = self._build()
        source_path = write_privacy_exclusion_zones(
            source_zones, self.root / "source-zones.json"
        )

        projected = project_identity_privacy_exclusion_zones(
            source_path, target_width=1000, target_height=500
        )
        self.assertEqual("final_canvas_normalized", projected["coordinate_space"])
        self.assertEqual("identity", projected["projection"]["method"])
        self.assertIn("source_privacy_exclusion_zones", projected)
        validate_privacy_exclusion_zones(projected)

        with self.assertRaisesRegex(ContractError, "dimensions to match"):
            project_identity_privacy_exclusion_zones(
                source_path, target_width=1080, target_height=1920
            )

    def test_media_clock_is_derived_from_bound_ffprobe_metadata(self) -> None:
        clock = media_clock_from_redaction_receipt(
            {
                "ffprobe": {
                    "input": {
                        "format": {"duration": "2.0"},
                        "streams": [
                            {
                                "codec_type": "video",
                                "width": 1280,
                                "height": 720,
                                "avg_frame_rate": "30000/1001",
                                "duration": "2.0",
                                "nb_frames": "60",
                            }
                        ],
                    }
                }
            }
        )
        self.assertEqual(1280, clock["width"])
        self.assertEqual(720, clock["height"])
        self.assertEqual(60, clock["frame_count"])
        self.assertAlmostEqual(29.97002997, clock["fps"], places=8)

        with self.assertRaisesRegex(ContractError, "width|media clock"):
            media_clock_from_redaction_receipt(
                {
                    "ffprobe": {
                        "input": {
                            "format": {"duration": "2.0"},
                            "streams": [
                                {
                                    "codec_type": "video",
                                    "avg_frame_rate": "30/1",
                                    "duration": "2.0",
                                }
                            ],
                        }
                    }
                }
            )

    def test_collision_report_answers_both_occlusion_questions(self) -> None:
        with patch(
            "video_creation_pipeline.privacy_exclusion_zones."
            "track_rectangles_with_norfair",
            return_value=self._tracked_rows(),
        ):
            zones = self._build()
        zones["coordinate_space"] = "final_canvas_normalized"
        zones["coverage"] = {
            "status": "complete",
            "covered_frame_count": zones["coverage"]["covered_frame_count"],
            "uncovered_spans": [],
            "review_required": False,
        }
        zones["zones_sha256"] = payload_sha256(zones, "zones_sha256")
        zones_path = write_privacy_exclusion_zones(zones, self.root / "zones.json")
        motion_path = self.root / "motion.json"
        motion_path.write_text(
            json.dumps(
                {
                    "source_privacy_exclusion_zones": artifact_reference(
                        zones_path, "privacy exclusion zones"
                    ),
                    "cues": [
                        {
                            "cue_id": "cue-1",
                            "timing": {
                                "timeline_start_s": 0.0,
                                "full_frame_s": 0.2,
                                "enter_s": 0.1,
                                "hold_s": 0.5,
                                "exit_s": 0.1,
                            },
                            "label": {
                                "rect": {
                                    "x": 0.2,
                                    "y": 0.0,
                                    "width": 0.7,
                                    "height": 0.7,
                                }
                            },
                            "focus_destination": {
                                "x": 0.0,
                                "y": 0.0,
                                "width": 0.1,
                                "height": 0.1,
                            },
                            "watermark": {
                                "rect": {
                                    "x": 0.0,
                                    "y": 0.9,
                                    "width": 0.1,
                                    "height": 0.05,
                                }
                            },
                            "disclaimer": {
                                "rect": {
                                    "x": 0.8,
                                    "y": 0.9,
                                    "width": 0.1,
                                    "height": 0.05,
                                }
                            },
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        with patch(
            "video_creation_pipeline.semantic_motion.validate_semantic_motion_plan"
        ):
            report = build_privacy_layout_collision_report(
                motion_path,
                zones_path,
                redaction_order="redaction_after_graphics",
            )
        self.assertGreater(report["summary"]["collision_count"], 0)
        collision = report["cues"][0]["collisions"][0]
        self.assertTrue(collision["occludes_sensitive_subject"])
        self.assertTrue(collision["would_be_redacted"])
        with patch(
            "video_creation_pipeline.semantic_motion.validate_semantic_motion_plan"
        ):
            validate_privacy_layout_collision_report(report)

        broken = copy.deepcopy(report)
        broken["cues"][0]["collisions"] = []
        broken["summary"]["collision_count"] = 0
        broken["summary"]["geometry_gate_passed"] = True
        broken["summary"]["status"] = "passed_candidate_only"
        broken["report_sha256"] = payload_sha256(broken, "report_sha256")
        with (
            patch(
                "video_creation_pipeline.semantic_motion.validate_semantic_motion_plan"
            ),
            self.assertRaisesRegex(ContractError, "geometry drifted"),
        ):
            validate_privacy_layout_collision_report(broken)

    def test_norfair_batches_multiple_faces_and_resets_at_scene_cut(self) -> None:
        pytest.importorskip("norfair")
        observations = [
            {
                "observation_id": "left-a",
                "frame_index": 0,
                "target_type": "face",
                "score": 0.9,
                "rect": {"x": 0.1, "y": 0.2, "width": 0.1, "height": 0.2},
            },
            {
                "observation_id": "right-a",
                "frame_index": 0,
                "target_type": "face",
                "score": 0.9,
                "rect": {"x": 0.7, "y": 0.2, "width": 0.1, "height": 0.2},
            },
            {
                "observation_id": "left-b",
                "frame_index": 1,
                "target_type": "face",
                "score": 0.9,
                "rect": {"x": 0.11, "y": 0.2, "width": 0.1, "height": 0.2},
            },
            {
                "observation_id": "left-after-cut",
                "frame_index": 2,
                "target_type": "face",
                "score": 0.9,
                "rect": {"x": 0.11, "y": 0.2, "width": 0.1, "height": 0.2},
            },
        ]
        tracked = track_rectangles_with_norfair(observations, scene_cuts=[2])
        by_id = {row["observation_id"]: row for row in tracked}
        self.assertEqual(by_id["left-a"]["track_id"], by_id["left-b"]["track_id"])
        self.assertNotEqual(
            by_id["left-b"]["track_id"], by_id["left-after-cut"]["track_id"]
        )
        self.assertNotEqual(by_id["left-a"]["track_id"], by_id["right-a"]["track_id"])

    def _build(self, *, detections: list[dict] | None = None) -> dict:
        return build_privacy_exclusion_zones(
            self.source,
            self.redacted,
            self.job_path,
            self.receipt_path,
            self.detections if detections is None else detections,
            media_clock=self.clock,
        )

    def _tracked_rows(self) -> list[dict]:
        output = []
        for row in self.detections:
            x0, y0, x1, y1 = row["xyxy"]
            output.append(
                {
                    "observation_id": f"face-{row['frame_index']}",
                    "frame_index": row["frame_index"],
                    "time_s": row["frame_index"] / self.clock["fps"],
                    "rect": {
                        "x": x0 / self.clock["width"],
                        "y": y0 / self.clock["height"],
                        "width": (x1 - x0) / self.clock["width"],
                        "height": (y1 - y0) / self.clock["height"],
                    },
                    "estimated_rect": {
                        "x": x0 / self.clock["width"],
                        "y": y0 / self.clock["height"],
                        "width": (x1 - x0) / self.clock["width"],
                        "height": (y1 - y0) / self.clock["height"],
                    },
                    "score": row["confidence"],
                    "target_type": "face",
                    "track_id": "scene-0-track-1",
                    "identity_claimed": False,
                }
            )
        return output

    def _write_redaction_contracts(self) -> None:
        job = {
            "schema": "video_creation_pipeline.privacy_redaction_job.v1",
            "input": artifact_reference(self.source, "source"),
            "output_path": str(self.redacted.resolve()),
            "parameters": {"dilation": 112, "window": 1},
        }
        job["job_sha256"] = payload_sha256(job, "job_sha256")
        atomic_write_json(job, self.job_path)
        receipt = {
            "schema": "video_creation_pipeline.privacy_redaction_receipt.v1",
            "job_sha256": job["job_sha256"],
            "input": job["input"],
            "output": artifact_reference(self.redacted, "redacted"),
            "engine": {
                "source_commit": "ded52d269b6e8e19dbbaf79f353426da3a228a98",
                "model_hash": "a" * 64,
            },
        }
        receipt["receipt_sha256"] = payload_sha256(receipt, "receipt_sha256")
        atomic_write_json(receipt, self.receipt_path)

    @staticmethod
    def _reframe_plan(strategy: str) -> dict:
        shot = {
            "shot_id": "shot-1",
            "start_s": 0.0,
            "end_s": 1.0,
            "strategy": strategy,
            "interpolation": "linear",
            "keyframes": [],
        }
        if strategy in {"fixed_crop", "tracked_crop"}:
            shot["keyframes"] = [
                {
                    "time_s": 0.0,
                    "crop": {"x": 0.15, "y": 0.0, "width": 0.5, "height": 1.0},
                }
            ]
        return {
            "plan_sha256": "b" * 64,
            "source_metadata": {"width": 1920, "height": 1080},
            "target_profile": {"width": 1080, "height": 1920},
            "shots": [shot],
        }


if __name__ == "__main__":
    unittest.main()
