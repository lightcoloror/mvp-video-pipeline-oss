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
    make_image,
    make_request,
    make_root,
    make_source,
    write_evidence_spec,
)
from video_creation_pipeline.contract_io import (
    file_sha256,
    payload_sha256,
    read_json_object,
)
from video_creation_pipeline.errors import ContractError
from video_creation_pipeline.execution_receipts import (
    FFMPEG_OUTLET_ID,
    validate_ffmpeg_execution_receipt,
)
from video_creation_pipeline.reframe_detectors import source_media_dimensions
from video_creation_pipeline.reframe_qa import (
    build_smart_reframe_qa,
    validate_smart_reframe_qa,
    write_smart_reframe_qa,
)
from video_creation_pipeline.smart_reframe import (
    build_smart_reframe_plan,
    render_smart_reframe_candidate,
    render_smart_reframe_debug_overlay,
    write_smart_reframe_contract,
)


class SmartReframeIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = make_root("integration")

    def tearDown(self) -> None:
        cleanup(self.root)

    def test_real_ffmpeg_video_candidate_is_deterministic_and_qa_passes(self) -> None:
        source = make_source(self.root)
        request = make_request(self.root)
        evidence = make_evidence(self.root, source, "talking-head-observations.json")
        plan = build_smart_reframe_plan(source, request, evidence_paths=[evidence])
        plan_path = self.root / "plan.json"
        write_smart_reframe_contract(plan, plan_path)

        first = render_smart_reframe_candidate(
            plan_path, self.root / "candidate-a.mp4", self.root / "run-a"
        )
        second = render_smart_reframe_candidate(
            plan_path, self.root / "candidate-b.mp4", self.root / "run-b"
        )
        self.assertEqual(
            file_sha256(self.root / "candidate-a.mp4"),
            file_sha256(self.root / "candidate-b.mp4"),
        )
        metadata = source_media_dimensions(self.root / "candidate-a.mp4")
        self.assertEqual((360, 640), (metadata["width"], metadata["height"]))
        self.assertTrue(metadata["has_audio"])
        qa = build_smart_reframe_qa(
            plan_path,
            render_receipt_path=self.root
            / "run-a"
            / "smart-reframe-render-receipt.json",
        )
        write_smart_reframe_qa(qa, self.root / "qa.json")
        self.assertEqual("passed_candidate_only", qa["summary"]["status"])
        self.assertEqual("completed_candidate_only", first["status"])
        self.assertEqual("completed_candidate_only", second["status"])
        qa["summary"]["status"] = "failed"
        qa["qa_sha256"] = payload_sha256(qa, "qa_sha256")
        with self.assertRaisesRegex(ContractError, "summary drifted"):
            validate_smart_reframe_qa(qa)

    def test_static_image_uses_same_ffmpeg_outlet_and_renders_vertical_candidate(
        self,
    ) -> None:
        source = make_image(self.root)
        request = make_request(self.root, media_role="photo")
        evidence = write_evidence_spec(
            self.root,
            source,
            [
                {
                    "evidence_id": "attention-1",
                    "kind": "visual_attention",
                    "time_s": 0,
                    "required": False,
                    "score": 0.8,
                    "rect": {"x": 0.55, "y": 0.2, "width": 0.2, "height": 0.5},
                    "source_modality": "synthetic_fixture",
                }
            ],
            "synthetic-static-attention",
        )
        plan = build_smart_reframe_plan(source, request, evidence_paths=[evidence])
        plan_path = self.root / "image-plan.json"
        write_smart_reframe_contract(plan, plan_path)
        render_smart_reframe_candidate(
            plan_path, self.root / "image-candidate.mp4", self.root / "image-run"
        )
        metadata = source_media_dimensions(self.root / "image-candidate.mp4")
        self.assertEqual((360, 640), (metadata["width"], metadata["height"]))
        self.assertAlmostEqual(1.0, metadata["duration_s"], delta=0.1)
        self.assertFalse(metadata["has_audio"])

    def test_debug_overlay_uses_shared_ffmpeg_outlet(self) -> None:
        source = make_source(self.root, duration_s=3.0)
        request = make_request(self.root)
        evidence = make_evidence(self.root, source, "talking-head-observations.json")
        plan = build_smart_reframe_plan(source, request, evidence_paths=[evidence])
        plan_path = self.root / "debug-plan.json"
        write_smart_reframe_contract(plan, plan_path)

        result = render_smart_reframe_debug_overlay(
            plan_path, self.root / "debug-overlay.mp4", self.root / "debug-run"
        )
        receipt = read_json_object(
            result["ffmpeg_receipt"]["path"], "smart reframe debug receipt"
        )
        validate_ffmpeg_execution_receipt(receipt)
        self.assertEqual("render", receipt["operation"])
        self.assertEqual(FFMPEG_OUTLET_ID, receipt["outlet_id"])
        self.assertEqual("completed_debug_candidate_only", result["status"])
        self.assertEqual(
            (960, 540),
            tuple(
                source_media_dimensions(self.root / "debug-overlay.mp4")[field]
                for field in ("width", "height")
            ),
        )


if __name__ == "__main__":
    unittest.main()
