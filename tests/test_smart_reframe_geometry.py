from __future__ import annotations

# ruff: noqa: E402

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from smart_reframe_test_support import (
    cleanup,
    make_evidence,
    make_request,
    make_root,
    make_source,
    write_evidence_spec,
)
from video_creation_pipeline.reframe_profiles import get_reframe_profile
from video_creation_pipeline.reframe_qa import build_smart_reframe_qa
from video_creation_pipeline.smart_reframe import (
    build_smart_reframe_plan,
    build_smart_reframe_request,
    write_smart_reframe_contract,
)


class SmartReframeGeometryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = make_root("geometry")
        self.source = make_source(self.root)
        self.request = make_request(self.root)

    def tearDown(self) -> None:
        cleanup(self.root)

    def test_talking_head_crop_retains_all_faces_and_safe_zone(self) -> None:
        evidence = make_evidence(
            self.root, self.source, "talking-head-observations.json"
        )
        plan = build_smart_reframe_plan(
            self.source, self.request, evidence_paths=[evidence]
        )
        plan_path = self.root / "plan.json"
        write_smart_reframe_contract(plan, plan_path)
        qa = build_smart_reframe_qa(plan_path)
        self.assertIn(plan["shots"][0]["strategy"], {"fixed_crop", "tracked_crop"})
        self.assertEqual(1.0, qa["summary"]["required_region_retention"])
        self.assertEqual(0, qa["summary"]["safe_zone_violation_count"])
        self.assertEqual("passed_candidate_only", qa["summary"]["status"])

    def test_vertical_crop_tracks_face_horizontally_without_fake_vertical_motion(
        self,
    ) -> None:
        evidence = make_evidence(
            self.root, self.source, "talking-head-observations.json"
        )
        plan = build_smart_reframe_plan(
            self.source, self.request, evidence_paths=[evidence]
        )
        crop = plan["shots"][0]["keyframes"][0]["crop"]
        face_center_x = 0.31 + 0.09 / 2
        projected_x = (face_center_x - crop["x"]) / crop["width"]
        self.assertAlmostEqual(0.5, projected_x, places=4)
        self.assertEqual(0.0, crop["y"])
        self.assertEqual(1.0, crop["height"])

    def test_headroom_protection_expands_only_derived_crop_constraint(self) -> None:
        from video_creation_pipeline.smart_reframe import _expand_face_headroom

        rect = {"x": 0.2, "y": 0.2, "width": 0.3, "height": 0.4}
        protected = _expand_face_headroom(rect, 0.24)
        self.assertEqual(rect, {"x": 0.2, "y": 0.2, "width": 0.3, "height": 0.4})
        self.assertAlmostEqual(0.104, protected["y"], places=6)
        self.assertAlmostEqual(0.496, protected["height"], places=6)

    def test_production_720_profile_is_exact_and_conservative(self) -> None:
        profile = get_reframe_profile("production_vertical_9_16_720")
        self.assertEqual((720, 1280), (profile["width"], profile["height"]))
        large_root = self.root / "production-profile"
        large_root.mkdir()
        source = make_source(large_root, width=1920, height=1080)
        request = build_smart_reframe_request(
            profile_id="production_vertical_9_16_720",
            media_role="talking_head",
        )
        request_path = large_root / "request.json"
        write_smart_reframe_contract(request, request_path)
        plan = build_smart_reframe_plan(source, request_path)
        self.assertLessEqual(
            plan["shots"][0]["quality_estimate"]["upscale_factor"], 1.19
        )

    def test_six_stable_observations_choose_robust_median_fixed_crop(self) -> None:
        observations = [
            {
                "evidence_id": f"face-{index}",
                "kind": "face",
                "time_s": time_s,
                "required": True,
                "score": 0.98,
                "rect": {
                    "x": 0.40 + offset,
                    "y": 0.18,
                    "width": 0.10,
                    "height": 0.22,
                },
                "source_modality": "synthetic_fixture",
                "track_id": "speaker-a",
            }
            for index, (time_s, offset) in enumerate(
                zip(
                    (0.0, 0.6, 1.2, 1.8, 2.4, 3.0),
                    (-0.006, 0.0, 0.004, -0.002, 0.006, 0.002),
                ),
                start=1,
            )
        ]
        evidence = write_evidence_spec(
            self.root, self.source, observations, "stable-six-point-track"
        )
        plan = build_smart_reframe_plan(
            self.source, self.request, evidence_paths=[evidence]
        )
        shot = plan["shots"][0]
        self.assertEqual("fixed_crop", shot["strategy"])
        self.assertEqual(1, len(shot["keyframes"]))
        self.assertEqual(
            "robust_median_fixed_for_shot", shot["keyframes"][0]["motion_reason"]
        )
        self.assertIn("fixed_crop_p95_within_deadband", shot["reason_codes"])

    def test_real_subject_motion_chooses_tracked_crop(self) -> None:
        observations = [
            {
                "evidence_id": f"moving-face-{index}",
                "kind": "face",
                "time_s": time_s,
                "required": True,
                "score": 0.98,
                "rect": {
                    "x": x,
                    "y": 0.18,
                    "width": 0.10,
                    "height": 0.22,
                },
                "source_modality": "synthetic_fixture",
                "track_id": "speaker-a",
            }
            for index, (time_s, x) in enumerate(
                zip(
                    (0.0, 0.6, 1.2, 1.8, 2.4, 3.0), (0.20, 0.27, 0.34, 0.41, 0.48, 0.55)
                ),
                start=1,
            )
        ]
        evidence = write_evidence_spec(
            self.root, self.source, observations, "moving-six-point-track"
        )
        plan = build_smart_reframe_plan(
            self.source, self.request, evidence_paths=[evidence]
        )
        self.assertEqual("tracked_crop", plan["shots"][0]["strategy"])
        self.assertIn(
            "tracked_crop_requires_observed_motion_beyond_deadband",
            plan["shots"][0]["reason_codes"],
        )


if __name__ == "__main__":
    unittest.main()
