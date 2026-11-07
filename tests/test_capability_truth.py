from __future__ import annotations

import json
import tomllib
import unittest
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from video_creation_pipeline.background_processing import (
    BACKGROUND_PROCESSING_RECEIPT_SCHEMA,
)
from video_creation_pipeline.capability_truth import (
    BACKGROUND_PROCESSING_COMPLETION_RECEIPT_SCHEMA,
    CAPABILITY_TRUTH_SCHEMA,
    COLOR_PROCESSING_COMPLETION_RECEIPT_SCHEMA,
    CONTENT_SYNTHETIC_RUN_RECEIPT_SCHEMA,
    REMOTION_RUN_RECEIPT_SCHEMA,
    REMOTION_SCENE_COMPLETION_RECEIPT_SCHEMA,
    SEMANTIC_MOTION_RUN_RECEIPT_SCHEMA,
    SPEECH_CLEANUP_COMPLETION_RECEIPT_SCHEMA,
    build_capability_doctor,
    build_capability_truth,
    validate_capability_truth,
    validate_content_synthetic_run_receipt,
    write_capability_doctor,
    write_capability_truth,
)
from video_creation_pipeline.color_processing import COLOR_PROCESSING_RECEIPT_SCHEMA
from video_creation_pipeline.contract_io import artifact_reference, payload_sha256
from video_creation_pipeline.errors import ContractError
from video_creation_pipeline.speech_cleanup import SPEECH_CLEANUP_RECEIPT_SCHEMA

ROOT = Path(__file__).resolve().parents[1]


class CapabilityTruthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.observed_at = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_doctor_tests_and_synthetic_receipt_promote_only_proven_capabilities(
        self,
    ) -> None:
        doctor = self._write_doctor(self.observed_at)
        test_receipt = self._write_test_receipt(self.observed_at - timedelta(minutes=2))
        run_receipt = self._write_content_receipt()

        with patch(
            "video_creation_pipeline.capability_truth.validate_content_synthetic_run_receipt"
        ):
            first = build_capability_truth(
                doctor,
                evidence_paths=[run_receipt],
                test_receipt_path=test_receipt,
                observed_at=self.observed_at,
            )
            second = build_capability_truth(
                doctor,
                evidence_paths=[run_receipt],
                test_receipt_path=test_receipt,
                observed_at=self.observed_at,
            )

        self.assertEqual(first, second)
        self.assertEqual(first["schema"], CAPABILITY_TRUTH_SCHEMA)
        self.assertEqual(
            first["capabilities"]["canonical_candidate_pipeline"]["status"],
            "verified_synthetic",
        )
        self.assertEqual(
            first["capabilities"]["licensed_audio_mix"]["status"],
            "verified_synthetic",
        )
        self.assertEqual(first["capabilities"]["nle_import"]["status"], "handoff_only")
        self.assertEqual(
            first["capabilities"]["real_person_gold_e2e"]["status"], "blocked"
        )
        self.assertEqual(first["summary"]["verified_synthetic"], 2)
        self.assertEqual(first["summary"]["verified_real_media"], 0)
        self.assertFalse(first["authority_boundary"]["publish_authorized"])

    def test_doctor_and_tests_alone_never_promote_execution(self) -> None:
        doctor = self._write_doctor(self.observed_at)
        test_receipt = self._write_test_receipt(self.observed_at - timedelta(minutes=1))
        truth = build_capability_truth(
            doctor,
            test_receipt_path=test_receipt,
            observed_at=self.observed_at,
        )
        self.assertEqual(
            truth["capabilities"]["canonical_candidate_pipeline"]["status"],
            "blocked",
        )
        self.assertFalse(
            truth["capabilities"]["canonical_candidate_pipeline"]["fresh_e2e_pass"]
        )

    def test_legacy_v1_doctor_and_truth_remain_valid_without_speech_extension(
        self,
    ) -> None:
        doctor = self._write_doctor(self.observed_at)
        payload = json.loads(doctor.read_text(encoding="utf-8"))
        payload["probes"].pop("speech_cleanup")
        payload["capabilities"].pop("speech_noise_cleanup")
        payload["doctor_sha256"] = payload_sha256(payload, "doctor_sha256")
        self._write_json(doctor, payload)

        truth = build_capability_truth(doctor, observed_at=self.observed_at)

        self.assertNotIn("speech_noise_cleanup", truth["capabilities"])
        validate_capability_truth(truth)

    def test_stale_test_receipt_keeps_execution_blocked(self) -> None:
        doctor = self._write_doctor(self.observed_at)
        test_receipt = self._write_test_receipt(self.observed_at - timedelta(hours=169))
        run_receipt = self._write_content_receipt()
        with patch(
            "video_creation_pipeline.capability_truth.validate_content_synthetic_run_receipt"
        ):
            truth = build_capability_truth(
                doctor,
                evidence_paths=[run_receipt],
                test_receipt_path=test_receipt,
                observed_at=self.observed_at,
            )
        self.assertEqual(truth["test_evidence"]["status"], "stale_pass")
        self.assertEqual(
            truth["capabilities"]["canonical_candidate_pipeline"]["status"],
            "blocked",
        )

    def test_stale_doctor_keeps_execution_blocked(self) -> None:
        doctor = self._write_doctor(self.observed_at - timedelta(hours=2))
        test_receipt = self._write_test_receipt(self.observed_at - timedelta(minutes=1))
        run_receipt = self._write_content_receipt()
        with patch(
            "video_creation_pipeline.capability_truth.validate_content_synthetic_run_receipt"
        ):
            truth = build_capability_truth(
                doctor,
                evidence_paths=[run_receipt],
                test_receipt_path=test_receipt,
                observed_at=self.observed_at,
            )
        self.assertEqual(truth["doctor_evidence"]["status"], "stale")
        self.assertEqual(
            truth["capabilities"]["canonical_candidate_pipeline"]["status"],
            "blocked",
        )

    def test_repository_mismatch_test_receipt_is_not_accepted(self) -> None:
        doctor = self._write_doctor(self.observed_at)
        test_receipt = self._write_test_receipt(
            self.observed_at - timedelta(minutes=1), cwd=self.root
        )
        truth = build_capability_truth(
            doctor,
            test_receipt_path=test_receipt,
            observed_at=self.observed_at,
        )
        self.assertEqual(truth["test_evidence"]["status"], "repository_mismatch")

    def test_unknown_receipt_schema_fails_closed(self) -> None:
        doctor = self._write_doctor(self.observed_at)
        receipt = self.root / "unknown.json"
        self._write_json(receipt, {"schema": "unknown.receipt.v1"})
        with self.assertRaisesRegex(ContractError, "unsupported capability execution"):
            build_capability_truth(
                doctor,
                evidence_paths=[receipt],
                observed_at=self.observed_at,
            )

    def test_legacy_non_atomic_content_receipt_is_not_current_evidence(self) -> None:
        receipt = {
            "schema": "video_creation_pipeline.content_video_synthetic_run_receipt.v1",
            "status": "completed_candidate_only",
        }
        receipt["receipt_sha256"] = payload_sha256(receipt, "receipt_sha256")

        with self.assertRaisesRegex(ContractError, "unsupported content synthetic"):
            validate_content_synthetic_run_receipt(receipt)

    def test_orphan_scope_attestation_fails_closed(self) -> None:
        doctor = self._write_doctor(self.observed_at, remotion_ready=True)
        attestation = self._write_remotion_attestation("a" * 64)
        with self.assertRaisesRegex(ContractError, "not bound"):
            build_capability_truth(
                doctor,
                evidence_paths=[attestation],
                observed_at=self.observed_at,
            )

    def test_tampered_managed_test_receipt_is_rejected(self) -> None:
        doctor = self._write_doctor(self.observed_at)
        test_receipt = self._write_test_receipt(self.observed_at - timedelta(minutes=1))
        payload = json.loads(test_receipt.read_text(encoding="utf-8"))
        payload["status"] = "failed"
        self._write_json(test_receipt, payload)
        with self.assertRaisesRegex(ContractError, "integrity check failed"):
            build_capability_truth(
                doctor,
                test_receipt_path=test_receipt,
                observed_at=self.observed_at,
            )

    def test_remotion_requires_hash_bound_synthetic_scope_attestation(self) -> None:
        doctor = self._write_doctor(self.observed_at, remotion_ready=True)
        test_receipt = self._write_test_receipt(self.observed_at - timedelta(minutes=1))
        run_receipt = self._write_remotion_receipt()
        attestation = self._write_remotion_attestation(
            json.loads(run_receipt.read_text(encoding="utf-8"))["receipt_sha256"]
        )
        with patch(
            "video_creation_pipeline.capability_truth.validate_remotion_run_receipt"
        ):
            unclassified = build_capability_truth(
                doctor,
                evidence_paths=[run_receipt],
                test_receipt_path=test_receipt,
                observed_at=self.observed_at,
            )
            classified = build_capability_truth(
                doctor,
                evidence_paths=[attestation, run_receipt],
                test_receipt_path=test_receipt,
                observed_at=self.observed_at,
            )
        self.assertEqual(
            unclassified["capabilities"]["remotion_scene_media_render"]["status"],
            "blocked",
        )
        self.assertEqual(
            unclassified["capabilities"]["remotion_scene_media_render"][
                "execution_scope"
            ],
            "unclassified_local_candidate",
        )
        self.assertEqual(
            classified["capabilities"]["remotion_scene_media_render"]["status"],
            "verified_synthetic",
        )

    def test_speech_cleanup_requires_hash_bound_synthetic_scope_attestation(
        self,
    ) -> None:
        doctor = self._write_doctor(self.observed_at)
        test_receipt = self._write_test_receipt(self.observed_at - timedelta(minutes=1))
        run_receipt = self._write_speech_cleanup_receipt()
        run_payload = json.loads(run_receipt.read_text(encoding="utf-8"))
        attestation = self._write_speech_cleanup_attestation(
            run_payload["receipt_sha256"]
        )
        with patch(
            "video_creation_pipeline.capability_truth.validate_speech_cleanup_receipt"
        ):
            unclassified = build_capability_truth(
                doctor,
                evidence_paths=[run_receipt],
                test_receipt_path=test_receipt,
                observed_at=self.observed_at,
            )
            classified = build_capability_truth(
                doctor,
                evidence_paths=[attestation, run_receipt],
                test_receipt_path=test_receipt,
                observed_at=self.observed_at,
            )

        self.assertEqual(
            unclassified["capabilities"]["speech_noise_cleanup"]["status"],
            "blocked",
        )
        self.assertEqual(
            unclassified["capabilities"]["speech_noise_cleanup"]["execution_scope"],
            "unclassified_local_candidate",
        )
        self.assertEqual(
            classified["capabilities"]["speech_noise_cleanup"]["status"],
            "verified_synthetic",
        )
        evidence = next(
            row
            for row in classified["evidence"]
            if "speech_noise_cleanup" in row["capability_ids"]
        )
        self.assertEqual(evidence["supporting_receipts"][0]["path"], str(attestation))

    def test_background_processing_requires_hash_bound_synthetic_scope_attestation(
        self,
    ) -> None:
        doctor = self._write_doctor(self.observed_at, background_ready=True)
        test_receipt = self._write_test_receipt(self.observed_at - timedelta(minutes=1))
        run_receipt = self._write_background_processing_receipt("blur")
        run_payload = json.loads(run_receipt.read_text(encoding="utf-8"))
        attestation = self._write_background_processing_attestation(
            "blur", run_payload["receipt_sha256"]
        )
        with patch(
            "video_creation_pipeline.capability_truth.validate_background_processing_receipt"
        ):
            unclassified = build_capability_truth(
                doctor,
                evidence_paths=[run_receipt],
                test_receipt_path=test_receipt,
                observed_at=self.observed_at,
            )
            classified = build_capability_truth(
                doctor,
                evidence_paths=[attestation, run_receipt],
                test_receipt_path=test_receipt,
                observed_at=self.observed_at,
            )

        self.assertEqual(
            unclassified["capabilities"]["person_background_processing"]["status"],
            "blocked",
        )
        self.assertEqual(
            unclassified["capabilities"]["person_background_processing"][
                "execution_scope"
            ],
            "unclassified_local_candidate",
        )
        self.assertEqual(
            classified["capabilities"]["person_background_processing"]["status"],
            "verified_synthetic",
        )
        self.assertFalse(
            classified["capabilities"]["person_background_processing"][
                "real_media_proof"
            ]
        )

    def test_background_processing_scope_drift_fails_closed(self) -> None:
        doctor = self._write_doctor(self.observed_at, background_ready=True)
        run_receipt = self._write_background_processing_receipt("blur")
        run_payload = json.loads(run_receipt.read_text(encoding="utf-8"))
        attestation = self._write_background_processing_attestation(
            "blur", run_payload["receipt_sha256"]
        )
        payload = json.loads(attestation.read_text(encoding="utf-8"))
        payload["boundary_check"]["human_visual_review_required"] = False
        self._write_json(attestation, payload)

        with self.assertRaisesRegex(ContractError, "authority boundary drifted"):
            build_capability_truth(
                doctor,
                evidence_paths=[attestation, run_receipt],
                observed_at=self.observed_at,
            )

    def test_color_processing_requires_hash_bound_synthetic_scope_attestation(
        self,
    ) -> None:
        doctor = self._write_doctor(self.observed_at, color_ready=True)
        test_receipt = self._write_test_receipt(self.observed_at - timedelta(minutes=1))
        run_receipt = self._write_color_processing_receipt()
        run_payload = json.loads(run_receipt.read_text(encoding="utf-8"))
        attestation = self._write_color_processing_attestation(
            run_payload["receipt_sha256"]
        )
        with patch(
            "video_creation_pipeline.capability_truth.validate_color_processing_receipt"
        ):
            unclassified = build_capability_truth(
                doctor,
                evidence_paths=[run_receipt],
                test_receipt_path=test_receipt,
                observed_at=self.observed_at,
            )
            classified = build_capability_truth(
                doctor,
                evidence_paths=[attestation, run_receipt],
                test_receipt_path=test_receipt,
                observed_at=self.observed_at,
            )

        self.assertEqual(
            unclassified["capabilities"]["basic_color_processing"]["status"],
            "blocked",
        )
        self.assertEqual(
            unclassified["capabilities"]["basic_color_processing"]["execution_scope"],
            "unclassified_local_candidate",
        )
        self.assertEqual(
            classified["capabilities"]["basic_color_processing"]["status"],
            "verified_synthetic",
        )
        self.assertFalse(
            classified["capabilities"]["basic_color_processing"]["real_media_proof"]
        )

    def test_speech_cleanup_scope_attestation_drift_fails_closed(self) -> None:
        doctor = self._write_doctor(self.observed_at)
        run_receipt = self._write_speech_cleanup_receipt()
        run_payload = json.loads(run_receipt.read_text(encoding="utf-8"))
        attestation = self._write_speech_cleanup_attestation(
            run_payload["receipt_sha256"]
        )
        payload = json.loads(attestation.read_text(encoding="utf-8"))
        payload["boundary_check"]["human_audition_required"] = False
        self._write_json(attestation, payload)

        with self.assertRaisesRegex(ContractError, "authority boundary drifted"):
            build_capability_truth(
                doctor,
                evidence_paths=[attestation, run_receipt],
                observed_at=self.observed_at,
            )

    def test_orphan_speech_cleanup_scope_attestation_fails_closed(self) -> None:
        doctor = self._write_doctor(self.observed_at)
        attestation = self._write_speech_cleanup_attestation("a" * 64)
        with self.assertRaisesRegex(ContractError, "not bound"):
            build_capability_truth(
                doctor,
                evidence_paths=[attestation],
                observed_at=self.observed_at,
            )

    def test_privacy_report_uses_bound_subject_scope(self) -> None:
        doctor = self._write_doctor(self.observed_at)
        test_receipt = self._write_test_receipt(self.observed_at - timedelta(minutes=1))
        candidate = self.root / "privacy-candidate.mp4"
        candidate.write_bytes(b"synthetic privacy candidate")
        plan = self.root / "privacy-plan.json"
        self._write_json(plan, {"subject_policy": {"mode": "synthetic"}})
        report = self.root / "privacy-report.json"
        self._write_json(
            report,
            {
                "schema": "video_creation_pipeline.privacy_scan_report.v1",
                "source_plan": artifact_reference(plan, "privacy plan"),
                "scanned_final_artifact": artifact_reference(
                    candidate, "privacy candidate"
                ),
            },
        )
        with patch(
            "video_creation_pipeline.capability_truth.validate_privacy_scan_report"
        ):
            truth = build_capability_truth(
                doctor,
                evidence_paths=[report],
                test_receipt_path=test_receipt,
                observed_at=self.observed_at,
            )
        self.assertEqual(
            truth["capabilities"]["privacy_candidate_scan"]["status"],
            "verified_synthetic",
        )

    def test_hyperframes_scope_reuses_privacy_report_bound_to_candidate(self) -> None:
        doctor = self._write_doctor(self.observed_at, hyperframes_ready=True)
        test_receipt = self._write_test_receipt(self.observed_at - timedelta(minutes=1))
        candidate = self.root / "semantic-motion-candidate.mp4"
        candidate.write_bytes(b"synthetic HyperFrames candidate")
        run_receipt = self._write_semantic_motion_receipt(candidate)
        privacy_report = self._write_privacy_scope_report(candidate)

        with ExitStack() as stack:
            stack.enter_context(
                patch(
                    "video_creation_pipeline.capability_truth.validate_semantic_motion_run_receipt"
                )
            )
            stack.enter_context(
                patch(
                    "video_creation_pipeline.capability_truth.validate_privacy_scan_report"
                )
            )
            unclassified = build_capability_truth(
                doctor,
                evidence_paths=[run_receipt],
                test_receipt_path=test_receipt,
                observed_at=self.observed_at,
            )
            classified = build_capability_truth(
                doctor,
                evidence_paths=[run_receipt, privacy_report],
                test_receipt_path=test_receipt,
                observed_at=self.observed_at,
            )

        self.assertEqual(
            unclassified["capabilities"]["hyperframes_semantic_motion"]["status"],
            "blocked",
        )
        self.assertEqual(
            unclassified["capabilities"]["hyperframes_semantic_motion"][
                "execution_scope"
            ],
            "unclassified_local_candidate",
        )
        self.assertEqual(
            classified["capabilities"]["hyperframes_semantic_motion"]["status"],
            "verified_synthetic",
        )
        hyperframes_evidence = next(
            row
            for row in classified["evidence"]
            if "hyperframes_semantic_motion" in row["capability_ids"]
        )
        self.assertEqual(
            hyperframes_evidence["supporting_receipts"],
            [artifact_reference(privacy_report, "privacy scope report")],
        )

    def test_duplicate_privacy_scope_for_same_candidate_fails_closed(self) -> None:
        doctor = self._write_doctor(self.observed_at, hyperframes_ready=True)
        candidate = self.root / "semantic-motion-candidate.mp4"
        candidate.write_bytes(b"duplicate privacy scope candidate")
        run_receipt = self._write_semantic_motion_receipt(candidate)
        first = self._write_privacy_scope_report(candidate, suffix="first")
        second = self._write_privacy_scope_report(candidate, suffix="second")

        with ExitStack() as stack:
            stack.enter_context(
                patch(
                    "video_creation_pipeline.capability_truth.validate_semantic_motion_run_receipt"
                )
            )
            stack.enter_context(
                patch(
                    "video_creation_pipeline.capability_truth.validate_privacy_scan_report"
                )
            )
            with self.assertRaisesRegex(ContractError, "duplicate privacy scope"):
                build_capability_truth(
                    doctor,
                    evidence_paths=[run_receipt, first, second],
                    observed_at=self.observed_at,
                )

    def test_bound_receipt_change_invalidates_written_truth(self) -> None:
        doctor = self._write_doctor(self.observed_at)
        test_receipt = self._write_test_receipt(self.observed_at - timedelta(minutes=1))
        run_receipt = self._write_content_receipt()
        truth_path = self.root / "truth.json"
        with patch(
            "video_creation_pipeline.capability_truth.validate_content_synthetic_run_receipt"
        ):
            truth = build_capability_truth(
                doctor,
                evidence_paths=[run_receipt],
                test_receipt_path=test_receipt,
                observed_at=self.observed_at,
            )
            write_capability_truth(truth, truth_path)
            payload = json.loads(run_receipt.read_text(encoding="utf-8"))
            payload["tampered"] = True
            self._write_json(run_receipt, payload)
            loaded = json.loads(truth_path.read_text(encoding="utf-8"))
            with self.assertRaisesRegex(ContractError, "changed after"):
                validate_capability_truth(loaded)

    def test_installed_entry_point_targets_capability_truth_cli(self) -> None:
        with (ROOT / "pyproject.toml").open("rb") as handle:
            scripts = tomllib.load(handle)["project"]["scripts"]
        self.assertEqual(
            scripts["video-capability-truth"],
            "video_creation_pipeline.capability_truth_cli:main",
        )

    def _write_doctor(
        self,
        observed_at: datetime,
        *,
        remotion_ready: bool = False,
        hyperframes_ready: bool = False,
        speech_cleanup_ready: bool = True,
        background_ready: bool = False,
        color_ready: bool = True,
    ) -> Path:
        canonical = {
            "schema": "video_creation_pipeline.content_video_doctor.v1",
            "status": "ready",
            "blocking_failure_count": 0,
            "checks": [],
            "model_provider_gateway": {"status": "not_configured"},
        }
        privacy = {
            "schema": "video_creation_pipeline.privacy_gate_doctor.v1",
            "status": "ready",
            "tools": {},
            "dynamic_layout_avoidance": {
                "status": "runtime_ready",
                "contract_ready": True,
            },
            "boundaries": {
                "candidate_detection_only": True,
                "human_review_required": True,
                "automatic_publish": False,
            },
        }
        remotion = {
            "schema": "video_creation_pipeline.remotion_project_doctor.v1",
            "status": (
                "ready_for_local_candidate_render" if remotion_ready else "not_supplied"
            ),
            "materialization_ready": remotion_ready,
            "render_ready": remotion_ready,
            "dependency_installation_performed_by_doctor": False,
            "render_execution_performed": False,
        }
        hyperframes = {
            "schema": "video_creation_pipeline.hyperframes_runtime_doctor.v1",
            "status": (
                "ready_for_local_candidate_render"
                if hyperframes_ready
                else "not_supplied"
            ),
            "render_ready": hyperframes_ready,
            "execution_performed": False,
        }
        speech_cleanup = {
            "schema": "video_creation_pipeline.speech_cleanup_doctor.v1",
            "status": "ready" if speech_cleanup_ready else "blocked_missing_filter",
            "filters": {
                "afftdn": speech_cleanup_ready,
                "bandreject": speech_cleanup_ready,
                "highpass": speech_cleanup_ready,
            },
        }
        background_processing = {
            "schema": "video_creation_pipeline.background_processing_doctor.v1",
            "status": "ready" if background_ready else "not_supplied",
            "execution_performed": False,
        }
        color_processing = {
            "schema": "video_creation_pipeline.color_processing_doctor.v1",
            "status": "ready" if color_ready else "blocked_runtime_unavailable",
            "checks": {},
        }
        with ExitStack() as stack:
            stack.enter_context(
                patch(
                    "video_creation_pipeline.capability_truth.build_doctor_report",
                    return_value=canonical,
                )
            )
            stack.enter_context(
                patch(
                    "video_creation_pipeline.capability_truth.privacy_gate_doctor",
                    return_value=privacy,
                )
            )
            stack.enter_context(
                patch(
                    "video_creation_pipeline.capability_truth.remotion_project_doctor",
                    return_value=remotion,
                )
            )
            stack.enter_context(
                patch(
                    "video_creation_pipeline.capability_truth._hyperframes_doctor",
                    return_value=hyperframes,
                )
            )
            stack.enter_context(
                patch(
                    "video_creation_pipeline.capability_truth._speech_cleanup_probe",
                    return_value=speech_cleanup,
                )
            )
            stack.enter_context(
                patch(
                    "video_creation_pipeline.capability_truth._background_processing_probe",
                    return_value=background_processing,
                )
            )
            stack.enter_context(
                patch(
                    "video_creation_pipeline.capability_truth._color_processing_probe",
                    return_value=color_processing,
                )
            )
            report = build_capability_doctor(
                observed_at=observed_at,
                repository_root=ROOT,
            )
        path = self.root / f"doctor-{len(list(self.root.glob('doctor-*.json')))}.json"
        write_capability_doctor(report, path)
        return path

    def _write_test_receipt(self, completed_at: datetime, *, cwd: Path = ROOT) -> Path:
        receipt = {
            "cleanup": {
                "attempts": 1,
                "errors": [],
                "exists_after_cleanup": False,
                "ownership_verified": True,
                "status": "cleaned",
            },
            "completed_at": completed_at.isoformat(),
            "created_at": (completed_at - timedelta(seconds=3)).isoformat(),
            "details": {
                "adapter": "pytest",
                "cwd": str(cwd.resolve()),
                "returncode": 0,
                "timed_out": False,
            },
            "profile": "pytest",
            "purpose": "mvp-video-capability-truth-tests",
            "run_id": "synthetic-managed-test",
            "schema_version": "windows-test-runtime-command-receipt.v3",
            "status": "passed",
        }
        receipt["receipt_sha256"] = payload_sha256(receipt, "receipt_sha256")
        path = self.root / f"test-{len(list(self.root.glob('test-*.json')))}.json"
        self._write_json(path, receipt)
        return path

    def _write_content_receipt(self) -> Path:
        receipt = {
            "schema": CONTENT_SYNTHETIC_RUN_RECEIPT_SCHEMA,
            "status": "completed_candidate_only",
            "synthetic_material_only": True,
            "production_success_claimed": False,
            "upload_performed": False,
            "publish_performed": False,
        }
        receipt["receipt_sha256"] = payload_sha256(receipt, "receipt_sha256")
        path = self.root / "content-run.json"
        self._write_json(path, receipt)
        return path

    def _write_remotion_receipt(self) -> Path:
        receipt = {
            "schema": REMOTION_RUN_RECEIPT_SCHEMA,
            "status": "completed_candidate_only",
        }
        receipt["receipt_sha256"] = payload_sha256(receipt, "receipt_sha256")
        path = self.root / "remotion-run.json"
        self._write_json(path, receipt)
        return path

    def _write_semantic_motion_receipt(self, candidate: Path) -> Path:
        receipt = {
            "schema": SEMANTIC_MOTION_RUN_RECEIPT_SCHEMA,
            "status": "completed_candidate_only",
            "candidate_video": artifact_reference(
                candidate, "semantic motion candidate"
            ),
        }
        receipt["receipt_sha256"] = payload_sha256(receipt, "receipt_sha256")
        path = self.root / "semantic-motion-run.json"
        self._write_json(path, receipt)
        return path

    def _write_speech_cleanup_receipt(self) -> Path:
        receipt = {
            "schema": SPEECH_CLEANUP_RECEIPT_SCHEMA,
            "status": "completed_technical_candidate_needs_audition",
        }
        receipt["receipt_sha256"] = payload_sha256(receipt, "receipt_sha256")
        path = self.root / "speech-cleanup-run.json"
        self._write_json(path, receipt)
        return path

    def _write_background_processing_receipt(self, mode: str) -> Path:
        receipt = {
            "schema": BACKGROUND_PROCESSING_RECEIPT_SCHEMA,
            "status": "completed_technical_candidate_needs_visual_review",
            "mode": mode,
        }
        receipt["receipt_sha256"] = payload_sha256(receipt, "receipt_sha256")
        path = self.root / f"background-processing-{mode}-run.json"
        self._write_json(path, receipt)
        return path

    def _write_color_processing_receipt(self) -> Path:
        receipt = {
            "schema": COLOR_PROCESSING_RECEIPT_SCHEMA,
            "status": "completed_technical_candidate_needs_visual_review",
        }
        receipt["receipt_sha256"] = payload_sha256(receipt, "receipt_sha256")
        path = self.root / "color-processing-run.json"
        self._write_json(path, receipt)
        return path

    def _write_privacy_scope_report(
        self, candidate: Path, *, suffix: str = "scope"
    ) -> Path:
        plan = self.root / f"privacy-plan-{suffix}.json"
        self._write_json(
            plan,
            {
                "subject_policy": {"mode": "synthetic"},
                "authorization_evidence": None,
            },
        )
        report = self.root / f"privacy-report-{suffix}.json"
        self._write_json(
            report,
            {
                "schema": "video_creation_pipeline.privacy_scan_report.v1",
                "source_plan": artifact_reference(plan, "privacy plan"),
                "scanned_final_artifact": artifact_reference(
                    candidate, "privacy candidate"
                ),
            },
        )
        return report

    def _write_remotion_attestation(self, receipt_sha256: str) -> Path:
        attestation = {
            "schema": REMOTION_SCENE_COMPLETION_RECEIPT_SCHEMA,
            "status": "complete",
            "completed_at": self.observed_at.isoformat(),
            "synthetic_e2e": {"latest_run_receipt_sha256": receipt_sha256},
            "boundary_check": {
                "candidate_only": True,
                "real_person_or_customer_media_used": False,
                "online_provider_called": False,
                "remote_asset_fetched": False,
                "cookie_or_secret_read": False,
                "media_uploaded": False,
                "video_published": False,
                "second_ffmpeg_or_audio_pipeline_created": False,
                "vkp_modified": False,
            },
        }
        path = self.root / "remotion-attestation.json"
        self._write_json(path, attestation)
        return path

    def _write_speech_cleanup_attestation(self, receipt_sha256: str) -> Path:
        attestation = {
            "schema": SPEECH_CLEANUP_COMPLETION_RECEIPT_SCHEMA,
            "status": "complete",
            "completed_at": self.observed_at.isoformat(),
            "verification": {
                "synthetic_e2e": {
                    "scope": "synthetic_noisy_video_only",
                    "execution_receipt_payload_sha256": receipt_sha256,
                    "double_run_byte_identical": True,
                    "audition_status": "needs_audition",
                    "publish_authorized": False,
                }
            },
            "boundary_check": {
                "external_provider_called": False,
                "real_media_used": False,
                "media_uploaded": False,
                "source_media_modified": False,
                "automatic_profile_selection": False,
                "voice_quality_claimed": False,
                "human_audition_required": True,
                "release_eligible": False,
                "video_published": False,
                "automatic_publish_enabled": False,
                "second_ffmpeg_orchestration_added": False,
                "second_audio_engine_added": False,
                "second_state_machine_added": False,
            },
        }
        path = self.root / "speech-cleanup-attestation.json"
        self._write_json(path, attestation)
        return path

    def _write_background_processing_attestation(
        self, mode: str, receipt_sha256: str
    ) -> Path:
        attestation = {
            "schema": BACKGROUND_PROCESSING_COMPLETION_RECEIPT_SCHEMA,
            "status": "complete",
            "completed_at": self.observed_at.isoformat(),
            "verification": {
                "synthetic_e2e": {
                    "scope": "synthetic_illustration_person_only",
                    "executions": [
                        {
                            "mode": mode,
                            "execution_receipt_payload_sha256": receipt_sha256,
                        }
                    ],
                    "visual_review_status": "needs_visual_review",
                    "publish_authorized": False,
                    "real_person_quality_claimed": False,
                }
            },
            "boundary_check": {
                "external_local_provider_used": True,
                "human_visual_review_required": True,
                "real_person_or_customer_media_used": False,
                "online_provider_called": False,
                "media_uploaded": False,
                "source_media_modified": False,
                "automatic_provider_fallback_used": False,
                "visual_quality_claimed": False,
                "real_person_gold_claimed": False,
                "release_eligible": False,
                "video_published": False,
                "automatic_publish_enabled": False,
                "second_ffmpeg_orchestration_added": False,
                "second_state_machine_added": False,
            },
        }
        path = self.root / f"background-processing-{mode}-attestation.json"
        self._write_json(path, attestation)
        return path

    def _write_color_processing_attestation(self, receipt_sha256: str) -> Path:
        attestation = {
            "schema": COLOR_PROCESSING_COMPLETION_RECEIPT_SCHEMA,
            "status": "complete",
            "completed_at": self.observed_at.isoformat(),
            "verification": {
                "synthetic_e2e": {
                    "scope": "synthetic_color_fixture_only",
                    "execution_receipt_payload_sha256": receipt_sha256,
                    "double_run_byte_identical": True,
                    "audio_packet_sha256_matches": True,
                    "visual_review_status": "needs_visual_review",
                    "publish_authorized": False,
                }
            },
            "boundary_check": {
                "human_visual_review_required": True,
                "real_person_or_customer_media_used": False,
                "online_provider_called": False,
                "media_uploaded": False,
                "source_media_modified": False,
                "automatic_white_balance_used": False,
                "creative_grade_quality_claimed": False,
                "skin_tone_quality_claimed": False,
                "release_eligible": False,
                "video_published": False,
                "automatic_publish_enabled": False,
                "second_ffmpeg_orchestration_added": False,
                "second_video_encoder_added": False,
                "second_state_machine_added": False,
            },
        }
        path = self.root / "color-processing-attestation.json"
        self._write_json(path, attestation)
        return path

    @staticmethod
    def _write_json(path: Path, payload: dict[str, object]) -> None:
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )


if __name__ == "__main__":
    unittest.main()
