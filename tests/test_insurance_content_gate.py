from __future__ import annotations

import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from video_creation_pipeline.errors import ContractError
from video_creation_pipeline.contract_io import canonical_json_sha256, payload_sha256
from video_creation_pipeline.creative_gates import (
    build_approval_record,
    build_release_gate,
    validate_release_gate,
    write_creative_contract,
)
from video_creation_pipeline.insurance_content_gate import (
    build_insurance_content_scan_request,
    default_insurance_policy_path,
    run_insurance_content_scan,
    validate_insurance_content_policy_pack,
    validate_insurance_content_scan_report,
    validate_insurance_content_scan_request,
    write_insurance_content_scan_request,
)
from video_creation_pipeline.insurance_content_gate_cli import (
    main as insurance_cli_main,
)


FIXED_AT = "2026-08-20T12:00:00+08:00"


class _FrozenClock(datetime):
    """Test-only clock pinning release-gate creation to the historical fixture window."""

    @classmethod
    def now(cls, tz=None):
        return datetime.fromisoformat(FIXED_AT).astimezone(tz or timezone.utc)


class InsuranceContentGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.policy_path = default_insurance_policy_path()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _text(self, name: str, value: str) -> Path:
        path = self.root / name
        path.write_text(value, encoding="utf-8")
        return path

    def _evidence(
        self,
        evidence_type: str,
        *,
        valid_until: str | None = None,
    ) -> dict[str, object]:
        path = self._text(f"{evidence_type}.txt", f"synthetic {evidence_type} evidence")
        return {
            "evidence_id": f"ev-{evidence_type}",
            "evidence_type": evidence_type,
            "supports_rule_ids": ["*"],
            "valid_from": "2026-08-01T00:00:00+08:00",
            "valid_until": valid_until,
            "path": path,
        }

    def _request(
        self,
        *,
        role: str,
        text: str,
        evidence: list[dict[str, object]] | None = None,
        filename: str = "script.txt",
        created_at: str = FIXED_AT,
    ) -> tuple[Path, dict[str, object], Path]:
        content_path = self._text(filename, text)
        request = build_insurance_content_scan_request(
            self.policy_path,
            content=[{"content_id": "main", "role": role, "path": content_path}],
            evidence=evidence or [],
            created_at=created_at,
            distribution_channel="synthetic_test",
        )
        request_path = self.root / f"{filename}.request.json"
        write_insurance_content_scan_request(request, request_path)
        return request_path, request, content_path

    def test_pack_is_current_but_fails_closed_at_future_effective_date(self) -> None:
        policy = json.loads(self.policy_path.read_text(encoding="utf-8"))
        validate_insurance_content_policy_pack(policy, at=FIXED_AT)
        with self.assertRaisesRegex(ContractError, "requires current review"):
            validate_insurance_content_policy_pack(
                policy, at="2026-09-30T00:00:00+08:00"
            )

    def test_safe_education_script_is_review_ready_candidate(self) -> None:
        request_path, request, _ = self._request(
            role="script",
            text="保险是否赔付，应以合同条款、责任范围和保险公司的实际审核为准。",
        )
        validate_insurance_content_scan_request(request)
        report = run_insurance_content_scan(request_path, self.root / "report.json")
        self.assertEqual(report["readiness"]["status"], "review_ready_candidate")
        self.assertTrue(report["readiness"]["render_candidate_eligible"])
        self.assertFalse(report["readiness"]["publish_authorized"])
        self.assertEqual(report["findings"], [])

    def test_guaranteed_claim_is_blocked_and_raw_phrase_is_not_stored(self) -> None:
        phrase = "买了就赔"
        request_path, _, _ = self._request(
            role="hook", text=f"这款保险{phrase}，不用担心。"
        )
        report = run_insurance_content_scan(request_path, self.root / "blocked.json")
        self.assertEqual(report["readiness"]["status"], "blocked")
        self.assertFalse(report["readiness"]["machine_gate_passed"])
        self.assertEqual(report["findings"][0]["disposition"], "blocked_expression")
        self.assertNotIn(phrase, json.dumps(report, ensure_ascii=False))

    def test_product_numbers_pass_machine_gate_only_when_evidence_is_bound(
        self,
    ) -> None:
        missing_request, _, _ = self._request(
            role="product_explanation",
            text="示例保额100万元，示例等待期90天。",
            filename="missing-product.txt",
        )
        missing_report = run_insurance_content_scan(
            missing_request, self.root / "missing-product.report.json"
        )
        self.assertEqual(missing_report["readiness"]["status"], "needs_fact_check")
        self.assertGreater(missing_report["readiness"]["evidence_gap_count"], 0)

        evidence = [
            self._evidence("product_terms"),
            self._evidence("product_current_status"),
            self._evidence("assumptions_and_limitations"),
        ]
        bound_request, _, _ = self._request(
            role="product_explanation",
            text="示例保额100万元，示例等待期90天。",
            evidence=evidence,
            filename="bound-product.txt",
        )
        bound_report = run_insurance_content_scan(
            bound_request, self.root / "bound-product.report.json"
        )
        self.assertEqual(bound_report["readiness"]["status"], "review_ready_candidate")
        self.assertEqual(bound_report["evidence_gaps"], [])
        self.assertTrue(
            all(
                row["disposition"] == "evidence_bound_signal"
                for row in bound_report["findings"]
            )
        )

    def test_expired_evidence_does_not_satisfy_product_role(self) -> None:
        evidence = [
            self._evidence("product_terms", valid_until="2026-08-19T23:59:59+08:00"),
            self._evidence("product_current_status"),
            self._evidence("assumptions_and_limitations"),
        ]
        request_path, _, _ = self._request(
            role="product_explanation",
            text="示例保额100万元。",
            evidence=evidence,
            filename="expired.txt",
        )
        report = run_insurance_content_scan(
            request_path, self.root / "expired.report.json"
        )
        self.assertEqual(report["readiness"]["status"], "needs_fact_check")
        self.assertIn(
            "product_terms", report["evidence_gaps"][0]["missing_evidence_types"]
        )

    def test_customer_case_requires_privacy_and_subject_authorization(self) -> None:
        request_path, _, _ = self._request(
            role="customer_case",
            text="这是一段已经虚构化的客户案例文本。",
        )
        report = run_insurance_content_scan(
            request_path, self.root / "case.report.json"
        )
        self.assertEqual(report["readiness"]["status"], "blocked")
        self.assertEqual(
            report["evidence_gaps"][0]["missing_evidence_types"],
            ["privacy_scan_report", "subject_authorization"],
        )

    def test_improper_ranking_still_needs_review_with_evidence(self) -> None:
        evidence = [
            self._evidence("official_source"),
            self._evidence("assumptions_and_limitations"),
        ]
        request_path, _, _ = self._request(
            role="cover_text",
            text="全网第一的保险方案",
            evidence=evidence,
            filename="ranking.txt",
        )
        report = run_insurance_content_scan(
            request_path, self.root / "ranking.report.json"
        )
        self.assertEqual(report["readiness"]["status"], "needs_compliance_review")
        self.assertEqual(
            report["findings"][0]["disposition"], "manual_compliance_review"
        )

    def test_json_content_records_pointer_without_raw_match(self) -> None:
        content_path = self.root / "script.json"
        content_path.write_text(
            json.dumps(
                {"scenes": [{"narration": "监管推荐这款产品"}]}, ensure_ascii=False
            ),
            encoding="utf-8",
        )
        request = build_insurance_content_scan_request(
            self.policy_path,
            content=[{"content_id": "json", "role": "script", "path": content_path}],
            created_at=FIXED_AT,
        )
        request_path = self.root / "json.request.json"
        write_insurance_content_scan_request(request, request_path)
        report = run_insurance_content_scan(
            request_path, self.root / "json.report.json"
        )
        self.assertEqual(
            report["findings"][0]["json_pointer_or_line"], "/scenes/0/narration"
        )
        self.assertNotIn("监管推荐", json.dumps(report, ensure_ascii=False))

    def test_same_request_is_hash_stable(self) -> None:
        request_path, _, _ = self._request(role="script", text="仅讨论一般保险知识。")
        first = run_insurance_content_scan(request_path, self.root / "first.json")
        second = run_insurance_content_scan(request_path, self.root / "second.json")
        self.assertEqual(first["report_sha256"], second["report_sha256"])
        self.assertEqual(first, second)

    def test_changed_content_invalidates_request_and_report(self) -> None:
        request_path, request, content_path = self._request(
            role="script", text="原始候选文本。"
        )
        report = run_insurance_content_scan(request_path, self.root / "report.json")
        content_path.write_text("修改后的候选文本。", encoding="utf-8")
        with self.assertRaisesRegex(ContractError, "changed after"):
            validate_insurance_content_scan_request(request)
        with self.assertRaisesRegex(ContractError, "changed after"):
            validate_insurance_content_scan_report(report)

    def test_tampered_report_hash_is_rejected(self) -> None:
        request_path, _, _ = self._request(role="script", text="普通保险知识。")
        report = run_insurance_content_scan(request_path, self.root / "report.json")
        tampered = copy.deepcopy(report)
        tampered["readiness"]["publish_authorized"] = True
        with self.assertRaisesRegex(ContractError, "schema validation failed"):
            validate_insurance_content_scan_report(tampered)

    def test_report_timestamp_cannot_drift_from_source_request(self) -> None:
        request_path, _, _ = self._request(role="script", text="普通保险知识。")
        report = run_insurance_content_scan(request_path, self.root / "report.json")
        tampered = copy.deepcopy(report)
        tampered["created_at"] = "2026-08-21T12:00:00+08:00"
        tampered["report_sha256"] = payload_sha256(tampered, "report_sha256")
        with self.assertRaisesRegex(ContractError, "creation time drifted"):
            validate_insurance_content_scan_report(tampered)

    @mock.patch("video_creation_pipeline.creative_gates.datetime", new=_FrozenClock)
    def test_release_gate_requires_and_binds_review_ready_insurance_scan(self) -> None:
        fresh_at = FIXED_AT
        request_path, _, content_path = self._request(
            role="script",
            text="保险科普应以合同条款和实际审核为准。",
            created_at=fresh_at,
        )
        report_path = self.root / "release-insurance-report.json"
        report = run_insurance_content_scan(request_path, report_path)
        approvals, manifest, visual = self._release_inputs(content_path)
        gate = build_release_gate(
            approvals,
            render_manifest_path=manifest,
            visual_plan_path=visual,
            required_content_gates=["insurance_content"],
            insurance_content_scan_path=report_path,
        )
        validate_release_gate(gate)
        self.assertEqual(gate["required_content_gates"], ["insurance_content"])
        self.assertEqual(
            gate["insurance_content_report_sha256"], report["report_sha256"]
        )
        self.assertFalse(gate["automatic_publish"])

        stale = copy.deepcopy(gate)
        report_created_at = datetime.fromisoformat(report["created_at"])
        stale["created_at"] = (report_created_at + timedelta(hours=169)).isoformat()
        stale["release_gate_sha256"] = payload_sha256(stale, "release_gate_sha256")
        with self.assertRaisesRegex(ContractError, "scan report is stale"):
            validate_release_gate(stale)

        content_path.write_text("后来修改的保险科普文本。", encoding="utf-8")
        with self.assertRaisesRegex(ContractError, "changed after"):
            validate_release_gate(gate)

    @mock.patch("video_creation_pipeline.creative_gates.datetime", new=_FrozenClock)
    def test_release_gate_rejects_missing_or_blocked_insurance_scan(self) -> None:
        fresh_at = FIXED_AT
        safe_path = self._text("approved-script.txt", "普通保险知识。")
        approvals, manifest, visual = self._release_inputs(safe_path)
        with self.assertRaisesRegex(ContractError, "supplied together"):
            build_release_gate(
                approvals,
                render_manifest_path=manifest,
                visual_plan_path=visual,
                required_content_gates=["insurance_content"],
            )

        request_path, _, _ = self._request(
            role="hook", text="这款保险买了就赔。", created_at=fresh_at
        )
        blocked_path = self.root / "blocked-release-report.json"
        run_insurance_content_scan(request_path, blocked_path)
        with self.assertRaisesRegex(ContractError, "not review-ready"):
            build_release_gate(
                approvals,
                render_manifest_path=manifest,
                visual_plan_path=visual,
                required_content_gates=["insurance_content"],
                insurance_content_scan_path=blocked_path,
            )

    def test_cli_build_scan_and_validate(self) -> None:
        content = self._text("cli-script.txt", "普通保险知识。")
        spec = self.root / "cli-spec.json"
        spec.write_text(
            json.dumps(
                {
                    "created_at": FIXED_AT,
                    "distribution_channel": "synthetic_cli_test",
                    "content": [
                        {
                            "content_id": "cli-main",
                            "role": "script",
                            "path": content.name,
                        }
                    ],
                    "evidence": [],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        request_path = self.root / "cli-request.json"
        report_path = self.root / "cli-report.json"
        self.assertEqual(
            insurance_cli_main(
                [
                    "build-request",
                    "--spec",
                    str(spec),
                    "--policy-pack",
                    str(self.policy_path),
                    "--output",
                    str(request_path),
                ]
            ),
            0,
        )
        self.assertEqual(
            insurance_cli_main(
                ["scan", "--request", str(request_path), "--output", str(report_path)]
            ),
            0,
        )
        self.assertEqual(insurance_cli_main(["validate", str(report_path)]), 0)

    def _release_inputs(self, script: Path) -> tuple[list[Path], Path, Path]:
        voice = self._text("release-voice.json", '{"segments":[{"id":"voice-1"}]}')
        measurements = self._text("release-measurements.json", '{"duration_s":8.0}')
        audio = self._text("release-audio.wav", "synthetic audio")
        visual = self._text("release-visual.json", '{"shots":[{"id":"shot-1"}]}')
        review = self._text("release-director-review.md", "synthetic approval")
        records = [
            build_approval_record(
                "script",
                script,
                approved_by="synthetic-operator",
                approved_at=FIXED_AT,
            ),
            build_approval_record(
                "voice",
                voice,
                approved_by="synthetic-operator",
                approved_at=FIXED_AT,
                attachments=[
                    {"role": "voice_measurements", "path": measurements},
                    {"role": "audio", "path": audio},
                ],
            ),
            build_approval_record(
                "visuals",
                visual,
                approved_by="synthetic-operator",
                approved_at=FIXED_AT,
                attachments=[{"role": "director_review_sheet", "path": review}],
            ),
        ]
        approvals = []
        for record in records:
            path = self.root / f"release-{record['target']}-approval.json"
            write_creative_contract(record, path)
            approvals.append(path)
        visual_payload = json.loads(visual.read_text(encoding="utf-8"))
        manifest = self.root / "release-render-manifest.json"
        manifest.write_text(
            json.dumps(
                {"visual_plan_sha256": canonical_json_sha256(visual_payload)},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return approvals, manifest, visual


if __name__ == "__main__":
    unittest.main()
