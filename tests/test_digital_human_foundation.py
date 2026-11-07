from __future__ import annotations

# ruff: noqa: E402

import copy
import json
import shutil
import sys
import unittest
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
FIXTURES = ROOT / "tests" / "fixtures" / "content-video-brief-v1" / "upstream-canonical"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from video_creation_pipeline.content_video_pipeline import (
    build_content_video_project_plan,
    validate_content_video_inputs,
    write_content_video_plan_bundle,
)
from video_creation_pipeline.contract_io import (
    artifact_reference,
    file_sha256,
    payload_sha256,
)
from video_creation_pipeline.digital_human_foundation import (
    FOUNDATION_STATUS,
    FEATURE_STATE,
    validate_digital_human_candidate_edl,
    generate_digital_human_foundation_synthetic,
    validate_digital_human_candidate_timeline,
    validate_digital_human_foundation_bundle,
    validate_digital_human_render_manifest,
    validate_digital_human_review_packet,
    validate_digital_human_source_manifest,
    validate_spoken_script_review,
)
from video_creation_pipeline.errors import ContractError


@unittest.skipUnless(
    shutil.which("ffmpeg") and shutil.which("ffprobe"),
    "FFmpeg and ffprobe are required for the digital-human foundation tests",
)
class DigitalHumanFoundationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = ROOT / ".test-runtime" / f"digital-human-foundation-{uuid4().hex}"
        inputs = validate_content_video_inputs(
            FIXTURES / "content-video-brief.v1.json",
            FIXTURES / "content-video-brief.v1.schema.json",
            FIXTURES / "research-output.v1.json",
            FIXTURES / "research-output.v1.schema.json",
            max_age_hours=1_000_000,
        )
        plan = build_content_video_project_plan(inputs)
        write_content_video_plan_bundle(plan, cls.root)
        cls.first = generate_digital_human_foundation_synthetic(
            cls.root,
            timeout_s=180,
        )
        cls.first_video_sha256 = file_sha256(
            cls.root
            / "digital-human-foundation"
            / "render"
            / "digital-human-foundation-candidate.mp4"
        )
        cls.first_identity = cls._read_contract("foundation-run-receipt.json")[
            "deterministic_identity"
        ]
        cls.second = generate_digital_human_foundation_synthetic(
            cls.root,
            timeout_s=180,
        )
        cls.contract_dir = cls.root / "digital-human-foundation" / "contracts"
        cls.render_dir = cls.root / "digital-human-foundation" / "render"

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.root, ignore_errors=True)

    @classmethod
    def _read_contract(cls, name: str) -> dict[str, object]:
        path = cls.root / "digital-human-foundation" / "contracts" / name
        return json.loads(path.read_text(encoding="utf-8"))

    def test_full_synthetic_e2e_is_candidate_only_and_hash_stable(self) -> None:
        validation = validate_digital_human_foundation_bundle(self.root)
        receipt = self._read_contract("foundation-run-receipt.json")
        review = self._read_contract("human-review-packet.json")
        source = self._read_contract("source-manifest.json")
        timeline = self._read_contract("candidate-timeline.json")
        edl = self._read_contract("candidate-edl.json")
        render = self._read_contract("render-manifest.json")
        qa = json.loads(
            (
                self.root
                / "digital-human-foundation"
                / "qa"
                / "technical-qa-report.json"
            ).read_text(encoding="utf-8")
        )

        self.assertEqual(validation["status"], FOUNDATION_STATUS)
        self.assertEqual(validation["feature_state"], FEATURE_STATE)
        self.assertTrue(validation["valid"])
        self.assertTrue(validation["candidate_only"])
        self.assertTrue(validation["avatar_adapter_required"])
        self.assertEqual(self.first_video_sha256, validation["candidate_video_sha256"])
        self.assertEqual(
            self.first_video_sha256,
            file_sha256(self.render_dir / "digital-human-foundation-candidate.mp4"),
        )
        self.assertEqual(
            self.first_identity,
            receipt["deterministic_identity"],
        )
        self.assertEqual(
            receipt["deterministic_identity"]["edl_sha256"],
            validation["edl_sha256"],
        )
        self.assertEqual(
            self.first["candidate_video_sha256"], self.second["candidate_video_sha256"]
        )
        self.assertTrue(qa["readiness"]["technical_ready"])
        self.assertFalse(qa["readiness"]["release_eligible"])
        self.assertEqual(review["status"], "awaiting_human_review")
        self.assertFalse(review["authority_boundary"]["review_packet_is_approval"])
        self.assertFalse(receipt["authority_boundary"]["network_used"])
        self.assertFalse(receipt["authority_boundary"]["upload_performed"])
        self.assertFalse(receipt["authority_boundary"]["publish_performed"])
        for artifact in (receipt, review, source, timeline, edl, render):
            self.assertEqual(artifact["feature_state"], FEATURE_STATE)

        enabled = copy.deepcopy(timeline)
        enabled["feature_state"] = "enabled"
        enabled["timeline_sha256"] = payload_sha256(enabled, "timeline_sha256")
        with self.assertRaisesRegex(ContractError, "authority boundary"):
            validate_digital_human_candidate_timeline(enabled)
        gate_handoff = review["existing_gate_handoff"]
        self.assertEqual(
            gate_handoff["approval_gate"]["schema"],
            "video_creation_pipeline.approval_record.v1",
        )
        self.assertEqual(gate_handoff["avatar_generation_gate"]["status"], "blocked")
        self.assertFalse(gate_handoff["release_gate"]["release_ready"])

    def test_candidate_edl_is_a_read_only_timeline_projection(self) -> None:
        edl = self._read_contract("candidate-edl.json")
        validate_digital_human_candidate_edl(edl)
        self.assertEqual(len(edl["ranges"]), 7)
        self.assertFalse(edl["readiness"]["production_execution_ready"])
        self.assertTrue(edl["authority_boundary"]["derived_projection_only"])
        self.assertFalse(edl["authority_boundary"]["independent_edit_allowed"])

        drifted = copy.deepcopy(edl)
        drifted["ranges"][0]["output_end_s"] = 2.5
        drifted["edl_sha256"] = payload_sha256(drifted, "edl_sha256")
        with self.assertRaisesRegex(ContractError, "drifted from its source timeline"):
            validate_digital_human_candidate_edl(drifted)

    def test_source_manifest_preserves_types_order_ranges_and_fallbacks(self) -> None:
        manifest = self._read_contract("source-manifest.json")
        assets = manifest["assets"]
        counts: dict[str, int] = {}
        for row in assets:
            counts[row["source_type"]] = counts.get(row["source_type"], 0) + 1
        self.assertEqual(
            counts,
            {
                "slide": 3,
                "interview_clip": 2,
                "avatar_placeholder": 1,
                "narration_audio": 1,
                "music": 1,
                "captions": 1,
            },
        )
        slides = [row for row in assets if row["source_type"] == "slide"]
        self.assertEqual([row["page_number"] for row in slides], [1, 2, 3])
        interviews = [row for row in assets if row["source_type"] == "interview_clip"]
        self.assertEqual(
            [row["fallback_asset_id"] for row in interviews],
            ["slide-001", "slide-002"],
        )
        self.assertTrue(
            all(row["authorization_status"] == "synthetic" for row in interviews)
        )

    def test_interview_requires_authorization_and_bounded_source_range(self) -> None:
        manifest = self._read_contract("source-manifest.json")
        interview = next(
            row for row in manifest["assets"] if row["source_type"] == "interview_clip"
        )
        for field, value, expected in (
            ("authorization_status", "missing", "not authorized"),
            ("source_out_s", 4.0, "range is out of bounds"),
        ):
            with self.subTest(field=field):
                candidate = copy.deepcopy(manifest)
                target = next(
                    row
                    for row in candidate["assets"]
                    if row["asset_id"] == interview["asset_id"]
                )
                target[field] = value
                candidate["manifest_sha256"] = payload_sha256(
                    candidate, "manifest_sha256"
                )
                with self.assertRaisesRegex(ContractError, expected):
                    validate_digital_human_source_manifest(candidate)

    def test_ppt_missing_reordered_hash_drift_and_duplicate_identity_fail(self) -> None:
        manifest = self._read_contract("source-manifest.json")

        missing = copy.deepcopy(manifest)
        missing["assets"] = [
            row for row in missing["assets"] if row["asset_id"] != "slide-003"
        ]
        missing["manifest_sha256"] = payload_sha256(missing, "manifest_sha256")
        with self.assertRaisesRegex(ContractError, "composition is incomplete"):
            validate_digital_human_source_manifest(missing)

        reordered = copy.deepcopy(manifest)
        slide = next(
            row for row in reordered["assets"] if row["asset_id"] == "slide-002"
        )
        slide["original_order"] = 3
        reordered["manifest_sha256"] = payload_sha256(reordered, "manifest_sha256")
        with self.assertRaisesRegex(ContractError, "missing, duplicated, or reordered"):
            validate_digital_human_source_manifest(reordered)

        hash_drift = copy.deepcopy(manifest)
        slide = next(
            row for row in hash_drift["assets"] if row["asset_id"] == "slide-001"
        )
        slide["primary_artifact"]["sha256"] = "0" * 64
        hash_drift["manifest_sha256"] = payload_sha256(hash_drift, "manifest_sha256")
        with self.assertRaisesRegex(ContractError, "changed after"):
            validate_digital_human_source_manifest(hash_drift)

        duplicated = copy.deepcopy(manifest)
        slide = next(
            row for row in duplicated["assets"] if row["asset_id"] == "slide-002"
        )
        slide["asset_id"] = "slide-001"
        duplicated["manifest_sha256"] = payload_sha256(duplicated, "manifest_sha256")
        with self.assertRaisesRegex(ContractError, "identities must be unique"):
            validate_digital_human_source_manifest(duplicated)

    def test_spoken_script_rejects_new_fact_and_free_rewrite(self) -> None:
        review = self._read_contract("spoken-script-review.json")
        for field, value in (("new_fact_count", 1), ("free_rewrite_performed", True)):
            with self.subTest(field=field):
                candidate = copy.deepcopy(review)
                candidate["change_summary"][field] = value
                candidate["review_sha256"] = payload_sha256(candidate, "review_sha256")
                with self.assertRaisesRegex(ContractError, "boundary is invalid"):
                    validate_spoken_script_review(candidate)

    def test_subtitle_text_drift_fails_after_all_artifacts_are_rebound(self) -> None:
        drift_srt = self.render_dir / "candidate-subtitles-drift.srt"
        original = (self.render_dir / "candidate-subtitles.srt").read_text(
            encoding="utf-8"
        )
        drifted = original.replace("支持证据", "新增事实")
        self.assertNotEqual(original, drifted)
        drift_srt.write_text(drifted, encoding="utf-8")
        manifest = self._read_contract("source-manifest.json")
        caption = next(
            row for row in manifest["assets"] if row["source_type"] == "captions"
        )
        caption["source_file"] = artifact_reference(drift_srt, "drift captions")
        caption["primary_artifact"] = artifact_reference(drift_srt, "drift captions")
        manifest["manifest_sha256"] = payload_sha256(manifest, "manifest_sha256")
        manifest_path = self.contract_dir / "source-manifest-subtitle-drift.json"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        timeline = self._read_contract("candidate-timeline.json")
        timeline["source_manifest"] = artifact_reference(
            manifest_path, "source manifest"
        )
        caption_clip = next(
            row for row in timeline["clips"] if row["track"] == "captions"
        )
        caption_clip["source_sha256"] = caption["primary_artifact"]["sha256"]
        timeline["timeline_sha256"] = payload_sha256(timeline, "timeline_sha256")

        with self.assertRaisesRegex(ContractError, "subtitle text drifted"):
            validate_digital_human_candidate_timeline(timeline)

    def test_avatar_placeholder_cannot_claim_generated_output(self) -> None:
        manifest = self._read_contract("source-manifest.json")
        placeholder = next(
            row
            for row in manifest["assets"]
            if row["source_type"] == "avatar_placeholder"
        )
        placeholder["actual_avatar_generated"] = True
        manifest["manifest_sha256"] = payload_sha256(manifest, "manifest_sha256")
        with self.assertRaisesRegex(ContractError, "cannot claim generated"):
            validate_digital_human_source_manifest(manifest)

    def test_unrun_avatar_capabilities_cannot_be_marked_passed(self) -> None:
        manifest = self._read_contract("render-manifest.json")
        manifest["digital_human_capability"]["lip_sync_status"] = "passed"
        manifest["render_manifest_sha256"] = payload_sha256(
            manifest, "render_manifest_sha256"
        )
        with self.assertRaisesRegex(ContractError, "cannot be marked as tested"):
            validate_digital_human_render_manifest(manifest)

    def test_timeline_overlap_negative_time_and_total_drift_fail(self) -> None:
        timeline = self._read_contract("candidate-timeline.json")
        mutations = (
            ("overlap", lambda row: row.update({"start_s": 2.5, "source_in_s": 0.5})),
            ("negative", lambda row: row.update({"start_s": -0.5})),
            ("total", lambda row: row.update({"end_s": 14.5, "source_out_s": 1.5})),
        )
        for name, mutate in mutations:
            with self.subTest(name=name):
                candidate = copy.deepcopy(timeline)
                target = next(
                    row for row in candidate["clips"] if row["clip_id"] == "visual-007"
                )
                mutate(target)
                candidate["timeline_sha256"] = payload_sha256(
                    candidate, "timeline_sha256"
                )
                with self.assertRaises(ContractError):
                    validate_digital_human_candidate_timeline(candidate)

    def test_old_review_packet_cannot_bind_a_new_timeline(self) -> None:
        review = self._read_contract("human-review-packet.json")
        timeline = self._read_contract("candidate-timeline.json")
        timeline["layout_safety"]["priority"] = [
            "evidence",
            "captions",
            "avatar",
            "semantic_cards",
        ]
        timeline["timeline_sha256"] = payload_sha256(timeline, "timeline_sha256")
        new_timeline_path = self.contract_dir / "candidate-timeline-new.json"
        new_timeline_path.write_text(
            json.dumps(timeline, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        review["inputs"]["candidate_timeline"] = artifact_reference(
            new_timeline_path, "new candidate timeline"
        )
        review["review_packet_sha256"] = payload_sha256(review, "review_packet_sha256")
        with self.assertRaisesRegex(ContractError, "one artifact generation"):
            validate_digital_human_review_packet(review)

    def test_review_packet_rejects_publish_upload_and_automatic_approval(self) -> None:
        review = self._read_contract("human-review-packet.json")
        for action in ("publish", "upload", "automatic_approval"):
            with self.subTest(action=action):
                candidate = copy.deepcopy(review)
                candidate["available_actions"].append(action)
                candidate["review_packet_sha256"] = payload_sha256(
                    candidate, "review_packet_sha256"
                )
                with self.assertRaisesRegex(ContractError, "prohibited action"):
                    validate_digital_human_review_packet(candidate)

    def test_review_packet_recomputes_existing_tts_resume_contract(self) -> None:
        review = self._read_contract("human-review-packet.json")
        resume = self._read_contract("spoken-tts-resume.json")
        resume["summary"]["status"] = "ready"
        resume["plan_sha256"] = payload_sha256(resume, "plan_sha256")
        resume_path = self.contract_dir / "spoken-tts-resume-drift.json"
        resume_path.write_text(
            json.dumps(resume, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        review["inputs"]["tts_resume_plan"] = artifact_reference(
            resume_path, "TTS resume plan"
        )
        review["review_packet_sha256"] = payload_sha256(review, "review_packet_sha256")
        with self.assertRaisesRegex(ContractError, "TTS resume plan drifted"):
            validate_digital_human_review_packet(review)

    def test_render_manifest_rejects_non_outlet_ffmpeg_receipt(self) -> None:
        render = self._read_contract("render-manifest.json")
        receipt_ref = render["ffmpeg_receipts"][0]
        receipt = json.loads(Path(receipt_ref["path"]).read_text(encoding="utf-8"))
        receipt["outlet_id"] = "second_ffmpeg_orchestrator"
        receipt["receipt_sha256"] = payload_sha256(receipt, "receipt_sha256")
        receipt_path = self.contract_dir / "forged-ffmpeg-receipt.json"
        receipt_path.write_text(
            json.dumps(receipt, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        render["ffmpeg_receipts"][0] = artifact_reference(
            receipt_path, "forged FFmpeg receipt"
        )
        render["render_manifest_sha256"] = payload_sha256(
            render, "render_manifest_sha256"
        )
        with self.assertRaisesRegex(ContractError, "existing single outlet"):
            validate_digital_human_render_manifest(render)


if __name__ == "__main__":
    unittest.main()
