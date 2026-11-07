from __future__ import annotations

import json
import os
import shutil
import sys
import unittest
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from video_creation_pipeline.contract_io import payload_sha256
from video_creation_pipeline.errors import ContractError
from video_creation_pipeline.privacy_gate import (
    PRIVACY_TERMS_SCHEMA,
    assert_privacy_gate_passed,
    build_privacy_review_decision,
    build_privacy_review_plan,
    privacy_gate_doctor,
    run_privacy_scan,
    validate_privacy_review_decision,
    validate_privacy_review_plan,
    validate_privacy_scan_report,
    write_privacy_review_plan,
)


class PrivacyGateTests(unittest.TestCase):
    def setUp(self) -> None:
        managed = os.environ.get("TEST_TMP_ROOT")
        parent = Path(managed).resolve() if managed else ROOT / ".test-runtime"
        self.root = parent / f"privacy-gate-{uuid4().hex}"
        self.root.mkdir(parents=True, exist_ok=True)
        self.final = self.root / "candidate.txt"
        self.final.write_text("最终候选文件", encoding="utf-8")
        self.script = self.root / "script.md"
        self.script.write_text(
            "这里不应出现测试医院，也不应出现手机号码 13800138000。",
            encoding="utf-8",
        )
        self.terms = self.root / "privacy-terms.json"
        self.terms.write_text(
            json.dumps(
                {
                    "schema": PRIVACY_TERMS_SCHEMA,
                    "terms": [
                        {
                            "label": "真实医院名",
                            "entity_type": "HOSPITAL_NAME",
                            "value": "测试医院",
                            "lanes": ["text", "audio", "visual", "metadata"],
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_scan_finds_candidates_without_storing_raw_matches(self) -> None:
        plan_path = self._write_plan()
        report = run_privacy_scan(plan_path, self.root / "scan")

        validate_privacy_scan_report(report)
        self.assertTrue(report["readiness"]["machine_scan_complete"])
        self.assertTrue(report["readiness"]["release_blocked"])
        self.assertEqual(report["readiness"]["candidate_count"], 2)
        rendered = json.dumps(report, ensure_ascii=False)
        self.assertNotIn("测试医院", rendered)
        self.assertNotIn("13800138000", rendered)
        self.assertTrue(
            all(row["human_disposition_required"] for row in report["findings"])
        )

    def test_review_requires_every_candidate_and_full_media_review(self) -> None:
        plan_path = self._write_plan()
        report_path = self.root / "scan" / "privacy-scan-report.json"
        report = run_privacy_scan(
            plan_path, self.root / "scan", report_path=report_path
        )
        blocked = build_privacy_review_decision(
            report_path,
            {
                "reviewer": "测试审核人",
                "human_full_media_review_completed": False,
                "decisions": [],
            },
        )
        self.assertFalse(blocked["outcome"]["privacy_gate_passed"])
        with self.assertRaisesRegex(ContractError, "blocked"):
            assert_privacy_gate_passed(blocked)

        passed = build_privacy_review_decision(
            report_path,
            {
                "reviewer": "测试审核人",
                "human_full_media_review_completed": True,
                "decisions": [
                    {
                        "finding_id": row["finding_id"],
                        "disposition": "false_positive",
                        "reason": "合成测试夹具，不进入真实发布",
                    }
                    for row in report["findings"]
                ],
            },
        )
        validate_privacy_review_decision(passed)
        assert_privacy_gate_passed(passed)
        self.assertTrue(passed["outcome"]["privacy_gate_passed"])
        self.assertFalse(
            passed["authority_boundary"][
                "technical_or_privacy_pass_is_publish_permission"
            ]
        )

    def test_final_artifact_hash_change_invalidates_downstream_contracts(self) -> None:
        plan = self._build_plan()
        self.final.write_text("候选文件已变更", encoding="utf-8")

        with self.assertRaisesRegex(ContractError, "changed after"):
            validate_privacy_review_plan(plan)

    def test_identifiable_face_or_voice_requires_authorization_evidence(self) -> None:
        with self.assertRaisesRegex(ContractError, "authorization evidence"):
            build_privacy_review_plan(
                self.final,
                self.terms,
                authorization_evidence_path=None,
                evidence=[{"role": "script", "path": self.script}],
                subject_policy={
                    "mode": "authorized_identifiable",
                    "face": "allowed",
                    "voice": "allowed",
                },
                required_lanes=["text", "metadata"],
            )

    def test_tampered_review_outcome_fails_integrity(self) -> None:
        plan_path = self._write_plan()
        report_path = self.root / "scan" / "privacy-scan-report.json"
        report = run_privacy_scan(
            plan_path, self.root / "scan", report_path=report_path
        )
        decision = build_privacy_review_decision(
            report_path,
            {
                "reviewer": "测试审核人",
                "human_full_media_review_completed": True,
                "decisions": [
                    {
                        "finding_id": row["finding_id"],
                        "disposition": "false_positive",
                        "reason": "合成夹具",
                    }
                    for row in report["findings"]
                ],
            },
        )
        decision["outcome"]["release_blocked"] = True
        decision["decision_sha256"] = payload_sha256(decision, "decision_sha256")
        with self.assertRaisesRegex(ContractError, "outcome drifted"):
            validate_privacy_review_decision(decision)

    def test_doctor_reports_dynamic_layout_layers_without_execution_claims(
        self,
    ) -> None:
        report = privacy_gate_doctor()
        layout = report["dynamic_layout_avoidance"]
        self.assertTrue(layout["contract_ready"])
        self.assertEqual("same_invocation_contract_ready", layout["coordinate_capture"])
        self.assertFalse(layout["runtime_layout_reposition_allowed"])
        self.assertFalse(layout["synthetic_verified"])
        self.assertFalse(layout["real_media_verified"])
        self.assertIn(
            layout["status"], {"runtime_ready", "blocked_missing_tracking_dependency"}
        )

    def _build_plan(self) -> dict:
        return build_privacy_review_plan(
            self.final,
            self.terms,
            authorization_evidence_path=None,
            evidence=[{"role": "script", "path": self.script}],
            subject_policy={
                "mode": "synthetic",
                "face": "not_applicable",
                "voice": "not_applicable",
            },
            required_lanes=["text", "metadata"],
        )

    def _write_plan(self) -> Path:
        plan = self._build_plan()
        path = self.root / "privacy-plan.json"
        write_privacy_review_plan(plan, path)
        return path


if __name__ == "__main__":
    unittest.main()
