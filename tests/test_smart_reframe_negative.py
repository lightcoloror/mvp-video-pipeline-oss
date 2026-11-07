from __future__ import annotations

# ruff: noqa: E402

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from smart_reframe_test_support import (
    cleanup,
    make_request,
    make_root,
    make_source,
    write_evidence_spec,
)
from video_creation_pipeline.capability_truth import build_capability_doctor
from video_creation_pipeline.contract_io import (
    artifact_reference,
    atomic_write_json,
    payload_sha256,
    read_json_object,
)
from video_creation_pipeline.errors import ContractError
from video_creation_pipeline.reframe_detectors import (
    adapt_vkp_reframe_evidence,
    source_media_dimensions,
    smart_reframe_doctor,
    select_smart_reframe_track,
    write_smart_reframe_evidence,
)
from video_creation_pipeline.reframe_tracking import optional_norfair_track_centers
from video_creation_pipeline.smart_reframe import build_smart_reframe_plan


class SmartReframeNegativeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = make_root("negative")
        self.source = make_source(self.root)

    def tearDown(self) -> None:
        cleanup(self.root)

    def test_vkp_projection_requires_hash_source_and_evidence_refs(self) -> None:
        source_ref = artifact_reference(self.source, "synthetic source")
        projection = {
            "schema": "video_knowledge_pipeline.reframe_evidence_projection.v1",
            "source_media_sha256": source_ref["sha256"],
            "source_artifacts": [{"artifact_id": "timeline", "artifact": source_ref}],
            "observations": [
                {
                    "evidence_id": "face-1",
                    "kind": "face",
                    "time_s": 0,
                    "required": True,
                    "score": 0.9,
                    "rect": {"x": 0.4, "y": 0.2, "width": 0.1, "height": 0.2},
                    "source_modality": "vkp_temporal_visual_evidence",
                    "evidence_ref": "timeline",
                }
            ],
            "authority_boundary": {
                "read_only_projection": True,
                "mutates_vkp_timeline": False,
                "creates_second_evidence_store": False,
                "identity_recognition_performed": False,
            },
        }
        projection["projection_sha256"] = payload_sha256(
            projection, "projection_sha256"
        )
        path = self.root / "projection.json"
        atomic_write_json(projection, path)
        evidence = adapt_vkp_reframe_evidence(self.source, path)
        self.assertEqual(
            "VKP reframe evidence projection", evidence["provider"]["name"]
        )
        projection["observations"][0]["evidence_ref"] = "missing"
        projection["projection_sha256"] = payload_sha256(
            projection, "projection_sha256"
        )
        atomic_write_json(projection, path)
        with self.assertRaisesRegex(ContractError, "source artifact ref"):
            adapt_vkp_reframe_evidence(self.source, path)

    def test_optional_runtime_absence_is_visible_and_never_falls_back(self) -> None:
        with patch.dict(sys.modules, {"norfair": None}):
            with self.assertRaisesRegex(
                ContractError, "Norfair runtime is unavailable"
            ):
                optional_norfair_track_centers([])
        with self.assertRaisesRegex(ContractError, "model not found"):
            from video_creation_pipeline.reframe_detectors import (
                detect_faces_with_mediapipe,
            )

            detect_faces_with_mediapipe(self.source, self.root / "missing.task")

    def test_opencv_haar_rejects_invalid_parameters(self) -> None:
        from video_creation_pipeline.reframe_detectors import (
            detect_faces_with_opencv_haar,
        )

        with self.assertRaisesRegex(ContractError, "invalid OpenCV Haar"):
            detect_faces_with_opencv_haar(self.source, scale_factor=1.0)

    def test_doctor_reports_candidate_boundary_not_enabled_crop(self) -> None:
        doctor = smart_reframe_doctor()
        self.assertFalse(doctor["authority_boundary"]["automatic_crop_enabled"])
        self.assertFalse(doctor["authority_boundary"]["automatic_publish"])
        aggregate = build_capability_doctor(repository_root=ROOT)
        row = aggregate["capabilities"]["smart_reframe_candidate"]
        self.assertTrue(row["contract_ready"])
        self.assertTrue(row["runtime_ready"])
        self.assertIn("candidate", row["reason"].lower())

    def test_unknown_media_mime_and_excessive_upscale_fail_closed(self) -> None:
        unknown = self.root / "synthetic.bin"
        self.source.replace(unknown)
        with self.assertRaisesRegex(ContractError, "media MIME"):
            source_media_dimensions(unknown)

        low_root = self.root / "low-resolution"
        low_root.mkdir()
        low_source = make_source(low_root, width=160, height=90)
        request = make_request(low_root)
        plan = build_smart_reframe_plan(low_source, request)
        self.assertEqual("blocked", plan["summary"]["status"])
        self.assertEqual(["shot-0001"], plan["summary"]["blocked_shot_ids"])

    def test_multi_person_without_explicit_track_uses_non_crop_fallback(self) -> None:
        request = make_request(self.root)
        observations = []
        for index, time_s in enumerate((0.0, 0.6, 1.2, 1.8, 2.4, 3.0), start=1):
            for track_id, x in (("left", 0.18), ("right", 0.68)):
                observations.append(
                    {
                        "evidence_id": f"face-{track_id}-{index}",
                        "kind": "face",
                        "time_s": time_s,
                        "required": True,
                        "score": 0.95,
                        "rect": {"x": x, "y": 0.18, "width": 0.10, "height": 0.22},
                        "source_modality": "synthetic_fixture",
                        "track_id": track_id,
                    }
                )
        evidence_path = write_evidence_spec(
            self.root, self.source, observations, "two-person-tracks"
        )
        plan = build_smart_reframe_plan(
            self.source, request, evidence_paths=[evidence_path]
        )
        self.assertEqual("contain", plan["shots"][0]["strategy"])
        self.assertIn(
            "multiple_subjects_require_explicit_track_selection",
            plan["shots"][0]["reason_codes"],
        )

        evidence = read_json_object(evidence_path, "two-person evidence")
        selected = select_smart_reframe_track(evidence, "left")
        selected_path = self.root / "selected-left.json"
        write_smart_reframe_evidence(selected, selected_path)
        selected_plan = build_smart_reframe_plan(
            self.source, request, evidence_paths=[selected_path]
        )
        self.assertIn(
            selected_plan["shots"][0]["strategy"], {"fixed_crop", "tracked_crop"}
        )
        self.assertTrue(
            any(row["kind"] == "user_hint" for row in selected["observations"])
        )

    def test_track_selection_rejects_unknown_track(self) -> None:
        evidence_path = write_evidence_spec(
            self.root,
            self.source,
            [
                {
                    "evidence_id": "face-left",
                    "kind": "face",
                    "time_s": 0.0,
                    "required": True,
                    "score": 0.9,
                    "rect": {"x": 0.2, "y": 0.2, "width": 0.1, "height": 0.2},
                    "source_modality": "synthetic_fixture",
                    "track_id": "left",
                }
            ],
            "one-track",
        )
        evidence = read_json_object(evidence_path, "one track evidence")
        with self.assertRaisesRegex(ContractError, "track was not found"):
            select_smart_reframe_track(evidence, "missing")


if __name__ == "__main__":
    unittest.main()
