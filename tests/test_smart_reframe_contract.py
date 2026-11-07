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
)
from video_creation_pipeline.contract_io import payload_sha256
from video_creation_pipeline.errors import ContractError
from video_creation_pipeline.smart_reframe import (
    build_smart_reframe_plan,
    validate_smart_reframe_plan,
    write_smart_reframe_contract,
)


class SmartReframeContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = make_root("contract")
        self.source = make_source(self.root)
        self.request = make_request(self.root)

    def tearDown(self) -> None:
        cleanup(self.root)

    def test_blind_crop_is_rejected_in_favour_of_safe_fallback(self) -> None:
        plan = build_smart_reframe_plan(self.source, self.request)
        self.assertEqual(["blur_pad"], [shot["strategy"] for shot in plan["shots"]])
        self.assertIn(
            "no_spatial_evidence_blind_crop_forbidden", plan["shots"][0]["reason_codes"]
        )
        self.assertEqual("candidate_ready", plan["summary"]["status"])

    def test_required_regions_that_do_not_fit_fall_back_without_truncation(
        self,
    ) -> None:
        evidence = make_evidence(self.root, self.source, "wide-required-regions.json")
        plan = build_smart_reframe_plan(
            self.source, self.request, evidence_paths=[evidence]
        )
        self.assertEqual("contain", plan["shots"][0]["strategy"])
        self.assertIn(
            "multiple_subjects_require_explicit_track_selection",
            plan["shots"][0]["reason_codes"],
        )
        self.assertTrue(
            plan["shots"][0]["quality_estimate"]["required_regions_retained"]
        )

    def test_plan_is_deterministic_and_rejects_rehashed_decision_tamper(self) -> None:
        evidence = make_evidence(
            self.root, self.source, "talking-head-observations.json"
        )
        first = build_smart_reframe_plan(
            self.source, self.request, evidence_paths=[evidence]
        )
        second = build_smart_reframe_plan(
            self.source, self.request, evidence_paths=[evidence]
        )
        self.assertEqual(first["plan_sha256"], second["plan_sha256"])
        tampered = dict(first)
        tampered["summary"] = dict(first["summary"])
        tampered["summary"]["shot_count"] = 99
        tampered["plan_sha256"] = payload_sha256(tampered, "plan_sha256")
        with self.assertRaisesRegex(ContractError, "summary drifted"):
            validate_smart_reframe_plan(tampered)

    def test_source_hash_drift_invalidates_saved_plan(self) -> None:
        plan = build_smart_reframe_plan(self.source, self.request)
        path = self.root / "plan.json"
        write_smart_reframe_contract(plan, path)
        self.source.write_bytes(self.source.read_bytes() + b"drift")
        with self.assertRaises(ContractError):
            validate_smart_reframe_plan(plan)


if __name__ == "__main__":
    unittest.main()
