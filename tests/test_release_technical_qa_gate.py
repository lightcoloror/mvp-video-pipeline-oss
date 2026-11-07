from __future__ import annotations

# ruff: noqa: E402

import json
import shutil
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from video_creation_pipeline.contract_io import (
    artifact_reference,
    canonical_json_sha256,
    payload_sha256,
)
from video_creation_pipeline.audio_mastering import (
    AUDIO_MASTERING_EXECUTION_RECEIPT_SCHEMA,
    LEGACY_AUDIO_MASTERING_EXECUTION_RECEIPT_SCHEMA,
)
from video_creation_pipeline.creative_cli import main as creative_cli_main
from video_creation_pipeline.creative_gates import (
    LEGACY_RELEASE_GATE_SCHEMA,
    RELEASE_GATE_SCHEMA,
    bind_audio_mastering_render_manifest,
    build_approval_record,
    build_release_gate,
    validate_audio_mastering_bound_render_manifest,
    validate_release_gate,
    write_audio_mastering_bound_render_manifest,
    write_creative_contract,
)
from video_creation_pipeline.errors import ContractError
from video_creation_pipeline.execution_receipts import (
    build_ffmpeg_execution_receipt,
    write_execution_contract,
)
from video_creation_pipeline.technical_qa import (
    build_technical_qa_report,
    write_technical_qa_report,
)


class ReleaseTechnicalQaGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = ROOT / ".test-runtime" / self._testMethodName
        if self.root.exists():
            shutil.rmtree(self.root)
        self.root.mkdir(parents=True)

    def tearDown(self) -> None:
        if self.root.exists():
            shutil.rmtree(self.root)

    def test_missing_export_qa_stops_before_release_review(self) -> None:
        approvals, visual = self._approvals()
        manifest = self._manifest(visual)

        gate = build_release_gate(
            approvals,
            render_manifest_path=manifest,
            visual_plan_path=visual,
        )

        validate_release_gate(gate)
        self.assertEqual(gate["schema"], RELEASE_GATE_SCHEMA)
        self.assertFalse(gate["release_ready"])
        self.assertEqual(gate["next_action"], "run_export_technical_qa")
        self.assertEqual(
            gate["workflow_locks"]["release_lock"]["status"],
            "blocked_missing_export_qa",
        )

    def test_passed_export_qa_binds_the_rendered_candidate(self) -> None:
        approvals, visual = self._approvals()
        candidate = self.root / "candidate.mp4"
        candidate.write_bytes(b"final-candidate")
        qa = self._technical_qa(candidate, detector_text="silence_duration:0.2")
        manifest = self._manifest(visual, candidate=candidate, technical_qa=qa)

        gate = build_release_gate(
            approvals,
            render_manifest_path=manifest,
            visual_plan_path=visual,
            technical_qa_path=qa,
        )

        validate_release_gate(gate)
        self.assertTrue(gate["release_ready"])
        self.assertEqual(gate["next_action"], "human_release_review")
        self.assertEqual(gate["workflow_locks"]["release_lock"]["status"], "bound")
        candidate.write_bytes(b"changed-after-qa")
        with self.assertRaisesRegex(ContractError, "changed after"):
            validate_release_gate(gate)

    def test_rejects_qa_for_a_different_candidate(self) -> None:
        approvals, visual = self._approvals()
        candidate = self.root / "candidate.mp4"
        candidate.write_bytes(b"final-candidate")
        other = self.root / "other.mp4"
        other.write_bytes(b"other-candidate")
        qa = self._technical_qa(candidate, detector_text="silence_duration:0.2")
        manifest = self._manifest(visual, candidate=other, technical_qa=qa)

        with self.assertRaisesRegex(ContractError, "not bound to the render manifest"):
            build_release_gate(
                approvals,
                render_manifest_path=manifest,
                visual_plan_path=visual,
                technical_qa_path=qa,
            )

    def test_rejects_failed_export_qa(self) -> None:
        approvals, visual = self._approvals()
        candidate = self.root / "candidate.mp4"
        candidate.write_bytes(b"final-candidate")
        qa = self._technical_qa(candidate, detector_text="black_duration:2.0")
        manifest = self._manifest(visual, candidate=candidate, technical_qa=qa)

        with self.assertRaisesRegex(ContractError, "requires passed export"):
            build_release_gate(
                approvals,
                render_manifest_path=manifest,
                visual_plan_path=visual,
                technical_qa_path=qa,
            )

    def test_legacy_v1_gate_remains_readable(self) -> None:
        approvals, visual = self._approvals()
        manifest = self._manifest(visual)
        gate = build_release_gate(
            approvals,
            render_manifest_path=manifest,
            visual_plan_path=visual,
        )
        for field in (
            "technical_qa",
            "render_manifest_candidate_video_sha256",
            "postproduction_sequence",
            "workflow_locks",
        ):
            gate.pop(field)
        gate["schema"] = LEGACY_RELEASE_GATE_SCHEMA
        gate["release_ready"] = True
        gate["next_action"] = "human_release_review"
        gate["release_gate_sha256"] = payload_sha256(gate, "release_gate_sha256")

        validate_release_gate(gate)

    def test_cli_accepts_export_technical_qa(self) -> None:
        approvals, visual = self._approvals()
        candidate = self.root / "candidate.mp4"
        candidate.write_bytes(b"final-candidate")
        qa = self._technical_qa(candidate, detector_text="silence_duration:0.2")
        manifest = self._manifest(visual, candidate=candidate, technical_qa=qa)
        output = self.root / "release-gate.json"
        arguments = ["build-release-gate"]
        for approval in approvals:
            arguments.extend(["--approval", str(approval)])
        arguments.extend(
            [
                "--render-manifest",
                str(manifest),
                "--visual-plan",
                str(visual),
                "--technical-qa",
                str(qa),
                "--output",
                str(output),
            ]
        )

        self.assertEqual(creative_cli_main(arguments), 0)
        payload = json.loads(output.read_text(encoding="utf-8"))
        self.assertTrue(payload["release_ready"])

    def test_output_verified_mastering_binds_manifest_and_voice_approval(self) -> None:
        approvals, visual = self._approvals()
        approved_audio = self._approved_audio(approvals)
        receipt, receipt_payload = self._mastering_receipt(approved_audio)
        manifest = self._manifest(visual)
        manifest_payload = json.loads(manifest.read_text(encoding="utf-8"))
        manifest_payload.update(
            {
                "audio_mastering_receipt_sha256": receipt_payload["receipt_sha256"],
                "mastered_audio_sha256": receipt_payload["mastered_audio"]["sha256"],
                "final_voice_audio_sha256s": [
                    receipt_payload["mastered_audio"]["sha256"]
                ],
            }
        )
        self._write_json(manifest, manifest_payload)

        with patch(
            "video_creation_pipeline.audio_mastering."
            "validate_audio_mastering_execution_receipt"
        ):
            gate = build_release_gate(
                approvals,
                render_manifest_path=manifest,
                visual_plan_path=visual,
                audio_mastering_receipt_path=receipt,
            )
            validate_release_gate(gate)

        self.assertEqual(gate["audio_mastering_status"], "output_verified")
        self.assertEqual(
            gate["render_manifest_audio_mastering_receipt_sha256"],
            receipt_payload["receipt_sha256"],
        )
        self.assertEqual(
            gate["render_manifest_mastered_audio_sha256"],
            receipt_payload["mastered_audio"]["sha256"],
        )
        self.assertFalse(gate["release_ready"])

    def test_manifest_mastering_claim_requires_an_explicit_v2_receipt(self) -> None:
        approvals, visual = self._approvals()
        approved_audio = self._approved_audio(approvals)
        _receipt, receipt_payload = self._mastering_receipt(approved_audio)
        manifest = self._manifest(visual)
        manifest_payload = json.loads(manifest.read_text(encoding="utf-8"))
        manifest_payload.update(
            {
                "audio_mastering_receipt_sha256": receipt_payload["receipt_sha256"],
                "mastered_audio_sha256": receipt_payload["mastered_audio"]["sha256"],
            }
        )
        self._write_json(manifest, manifest_payload)

        with self.assertRaisesRegex(ContractError, "without a v2 execution receipt"):
            build_release_gate(
                approvals,
                render_manifest_path=manifest,
                visual_plan_path=visual,
            )

    def test_manifest_binder_is_atomic_idempotent_and_integrity_checked(self) -> None:
        approvals, visual = self._approvals()
        voice_approval = self._voice_approval(approvals)
        approved_audio = self._approved_audio(approvals)
        receipt, receipt_payload = self._mastering_receipt(approved_audio)
        source_manifest = self._manifest(visual)
        output_manifest = self.root / "bound-render-manifest.json"

        with patch(
            "video_creation_pipeline.audio_mastering."
            "validate_audio_mastering_execution_receipt"
        ):
            bound = bind_audio_mastering_render_manifest(
                source_manifest,
                receipt,
                voice_approval,
            )
            validate_audio_mastering_bound_render_manifest(bound)
            write_audio_mastering_bound_render_manifest(
                bound,
                output_manifest,
                source_manifest_path=source_manifest,
            )
            rebound = bind_audio_mastering_render_manifest(
                output_manifest,
                receipt,
                voice_approval,
            )

        self.assertEqual(bound, rebound)
        self.assertEqual(
            bound["audio_mastering_receipt_sha256"],
            receipt_payload["receipt_sha256"],
        )
        self.assertEqual(
            bound["mastered_audio_sha256"],
            receipt_payload["mastered_audio"]["sha256"],
        )
        self.assertEqual(
            bound["render_manifest_sha256"],
            payload_sha256(bound, "render_manifest_sha256"),
        )
        with self.assertRaisesRegex(ContractError, "must differ from its source"):
            write_audio_mastering_bound_render_manifest(
                bound,
                source_manifest,
                source_manifest_path=source_manifest,
            )

        tampered = dict(bound)
        tampered["synthetic_tamper"] = True
        self._write_json(output_manifest, tampered)
        with self.assertRaisesRegex(ContractError, "integrity check failed"):
            build_release_gate(
                approvals,
                render_manifest_path=output_manifest,
                visual_plan_path=visual,
                audio_mastering_receipt_path=receipt,
            )

    def test_manifest_binder_rejects_existing_conflicting_claim(self) -> None:
        approvals, visual = self._approvals()
        voice_approval = self._voice_approval(approvals)
        approved_audio = self._approved_audio(approvals)
        receipt, _receipt_payload = self._mastering_receipt(approved_audio)
        manifest = self._manifest(visual)
        manifest_payload = json.loads(manifest.read_text(encoding="utf-8"))
        manifest_payload["audio_mastering_receipt_sha256"] = "different-receipt"
        self._write_json(manifest, manifest_payload)

        with (
            patch(
                "video_creation_pipeline.audio_mastering."
                "validate_audio_mastering_execution_receipt"
            ),
            self.assertRaisesRegex(ContractError, "stale for audio mastering"),
        ):
            bind_audio_mastering_render_manifest(
                manifest,
                receipt,
                voice_approval,
            )

    def test_manifest_binder_rejects_final_voice_excluding_mastered_audio(
        self,
    ) -> None:
        approvals, visual = self._approvals()
        voice_approval = self._voice_approval(approvals)
        approved_audio = self._approved_audio(approvals)
        receipt, _receipt_payload = self._mastering_receipt(approved_audio)
        manifest = self._manifest(visual)
        manifest_payload = json.loads(manifest.read_text(encoding="utf-8"))
        manifest_payload["final_voice_audio_sha256s"] = ["different-final-audio"]
        self._write_json(manifest, manifest_payload)

        with (
            patch(
                "video_creation_pipeline.audio_mastering."
                "validate_audio_mastering_execution_receipt"
            ),
            self.assertRaisesRegex(ContractError, "final voice excludes"),
        ):
            bind_audio_mastering_render_manifest(
                manifest,
                receipt,
                voice_approval,
            )

    def test_mastering_receipt_requires_the_approved_audio_and_manifest_hashes(
        self,
    ) -> None:
        approvals, visual = self._approvals()
        unapproved_audio = self.root / "unapproved-mastered.wav"
        unapproved_audio.write_bytes(b"not-the-approved-audio")
        receipt, receipt_payload = self._mastering_receipt(unapproved_audio)
        manifest = self._manifest(visual)
        manifest_payload = json.loads(manifest.read_text(encoding="utf-8"))
        manifest_payload.update(
            {
                "audio_mastering_receipt_sha256": receipt_payload["receipt_sha256"],
                "mastered_audio_sha256": receipt_payload["mastered_audio"]["sha256"],
            }
        )
        self._write_json(manifest, manifest_payload)

        with (
            patch(
                "video_creation_pipeline.audio_mastering."
                "validate_audio_mastering_execution_receipt"
            ),
            self.assertRaisesRegex(ContractError, "not bound to the voice approval"),
        ):
            build_release_gate(
                approvals,
                render_manifest_path=manifest,
                visual_plan_path=visual,
                audio_mastering_receipt_path=receipt,
            )

        approved_audio = self._approved_audio(approvals)
        receipt, receipt_payload = self._mastering_receipt(approved_audio)
        manifest_payload["audio_mastering_receipt_sha256"] = "stale-receipt"
        manifest_payload["mastered_audio_sha256"] = receipt_payload["mastered_audio"][
            "sha256"
        ]
        self._write_json(manifest, manifest_payload)
        with (
            patch(
                "video_creation_pipeline.audio_mastering."
                "validate_audio_mastering_execution_receipt"
            ),
            self.assertRaisesRegex(ContractError, "stale for audio mastering"),
        ):
            build_release_gate(
                approvals,
                render_manifest_path=manifest,
                visual_plan_path=visual,
                audio_mastering_receipt_path=receipt,
            )

        manifest_payload.update(
            {
                "audio_mastering_receipt_sha256": receipt_payload["receipt_sha256"],
                "final_voice_audio_sha256s": ["different-final-audio"],
            }
        )
        self._write_json(manifest, manifest_payload)
        with (
            patch(
                "video_creation_pipeline.audio_mastering."
                "validate_audio_mastering_execution_receipt"
            ),
            self.assertRaisesRegex(ContractError, "final voice excludes"),
        ):
            build_release_gate(
                approvals,
                render_manifest_path=manifest,
                visual_plan_path=visual,
                audio_mastering_receipt_path=receipt,
            )

    def test_legacy_mastering_receipt_cannot_claim_output_verification(self) -> None:
        approvals, visual = self._approvals()
        approved_audio = self._approved_audio(approvals)
        receipt, receipt_payload = self._mastering_receipt(
            approved_audio,
            schema=LEGACY_AUDIO_MASTERING_EXECUTION_RECEIPT_SCHEMA,
        )
        manifest = self._manifest(visual)
        manifest_payload = json.loads(manifest.read_text(encoding="utf-8"))
        manifest_payload.update(
            {
                "audio_mastering_receipt_sha256": receipt_payload["receipt_sha256"],
                "mastered_audio_sha256": receipt_payload["mastered_audio"]["sha256"],
            }
        )
        self._write_json(manifest, manifest_payload)

        with (
            patch(
                "video_creation_pipeline.audio_mastering."
                "validate_audio_mastering_execution_receipt"
            ),
            self.assertRaisesRegex(ContractError, "requires a v2 output-verified"),
        ):
            build_release_gate(
                approvals,
                render_manifest_path=manifest,
                visual_plan_path=visual,
                audio_mastering_receipt_path=receipt,
            )

    def test_cli_accepts_audio_mastering_receipt(self) -> None:
        approvals, visual = self._approvals()
        voice_approval = self._voice_approval(approvals)
        approved_audio = self._approved_audio(approvals)
        receipt, _receipt_payload = self._mastering_receipt(approved_audio)
        source_manifest = self._manifest(visual)
        manifest = self.root / "cli-bound-render-manifest.json"
        output = self.root / "release-gate-with-mastering.json"
        arguments = ["build-release-gate"]
        for approval in approvals:
            arguments.extend(["--approval", str(approval)])
        arguments.extend(
            [
                "--render-manifest",
                str(manifest),
                "--visual-plan",
                str(visual),
                "--audio-mastering-receipt",
                str(receipt),
                "--output",
                str(output),
            ]
        )

        with patch(
            "video_creation_pipeline.audio_mastering."
            "validate_audio_mastering_execution_receipt"
        ):
            self.assertEqual(
                creative_cli_main(
                    [
                        "bind-audio-mastering-render-manifest",
                        "--render-manifest",
                        str(source_manifest),
                        "--audio-mastering-receipt",
                        str(receipt),
                        "--voice-approval",
                        str(voice_approval),
                        "--output",
                        str(manifest),
                    ]
                ),
                0,
            )
            self.assertEqual(creative_cli_main(arguments), 0)
        payload = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(payload["audio_mastering_status"], "output_verified")

    def _approvals(self) -> tuple[list[Path], Path]:
        script = self._json("script.json", {"text": "approved"})
        voice = self._json("voice.json", {"segments": [{"id": "voice-1"}]})
        measurements = self._json("measurements.json", {"duration_s": 10.0})
        audio = self.root / "voice.wav"
        audio.write_bytes(b"approved-voice")
        visual = self._json("visual.json", {"shots": [{"id": "shot-001"}]})
        review = self.root / "director-review.md"
        review.write_text("approved", encoding="utf-8")
        records = [
            build_approval_record(
                "script",
                script,
                approved_by="operator",
                approved_at="2026-08-18T17:00:00+08:00",
            ),
            build_approval_record(
                "voice",
                voice,
                approved_by="operator",
                approved_at="2026-08-18T17:00:00+08:00",
                attachments=[
                    {"role": "voice_measurements", "path": measurements},
                    {"role": "audio", "path": audio},
                ],
            ),
            build_approval_record(
                "visuals",
                visual,
                approved_by="operator",
                approved_at="2026-08-18T17:00:00+08:00",
                attachments=[{"role": "director_review_sheet", "path": review}],
            ),
        ]
        paths: list[Path] = []
        for record in records:
            path = self.root / f"approval-{record['target']}.json"
            write_creative_contract(record, path)
            paths.append(path)
        return paths, visual

    def _technical_qa(self, candidate: Path, *, detector_text: str) -> Path:
        probe = self._json(
            "probe.json",
            {
                "streams": [
                    {
                        "codec_type": "video",
                        "codec_name": "h264",
                        "width": 1280,
                        "height": 720,
                        "avg_frame_rate": "30/1",
                        "pix_fmt": "yuv420p",
                    },
                    {
                        "codec_type": "audio",
                        "codec_name": "aac",
                        "sample_rate": "48000",
                        "channels": 2,
                    },
                ],
                "format": {"duration": "10.0"},
            },
        )
        detector = self.root / "detector.log"
        detector.write_text(detector_text, encoding="utf-8")
        receipt = build_ffmpeg_execution_receipt(
            "technical_qa",
            [
                "ffmpeg",
                "-i",
                str(candidate),
                "-vf",
                "blackdetect,freezedetect",
                "-af",
                "silencedetect",
                "-f",
                "null",
                "-",
            ],
            [{"role": "media", "path": candidate}],
            [{"role": "detector_log", "path": detector}],
        )
        receipt_path = self.root / "technical-qa-receipt.json"
        write_execution_contract(receipt, receipt_path)
        subtitles = self.root / "subtitles.srt"
        subtitles.write_text(
            "1\n00:00:00,000 --> 00:00:09,900\nApproved subtitle\n",
            encoding="utf-8",
        )
        packaging = self._json(
            "packaging.json",
            {"scenes": [{"start_s": 0.0, "end_s": 10.0}]},
        )
        report = build_technical_qa_report(
            candidate,
            probe,
            receipt_path,
            subtitles_path=subtitles,
            packaging_plan_path=packaging,
            expected_duration_s=10.0,
        )
        path = self.root / "technical-qa.json"
        write_technical_qa_report(report, path)
        return path

    @staticmethod
    def _approved_audio(approvals: list[Path]) -> Path:
        for approval in approvals:
            payload = json.loads(approval.read_text(encoding="utf-8"))
            if payload.get("target") == "voice":
                for attachment in payload["attachments"]:
                    if str(attachment.get("role") or "").startswith("audio"):
                        return Path(attachment["artifact"]["path"])
        raise AssertionError("voice approval lacks an audio attachment")

    @staticmethod
    def _voice_approval(approvals: list[Path]) -> Path:
        for approval in approvals:
            payload = json.loads(approval.read_text(encoding="utf-8"))
            if payload.get("target") == "voice":
                return approval
        raise AssertionError("voice approval is missing")

    def _mastering_receipt(
        self,
        mastered_audio: Path,
        *,
        schema: str = AUDIO_MASTERING_EXECUTION_RECEIPT_SCHEMA,
    ) -> tuple[Path, dict[str, object]]:
        payload: dict[str, object] = {
            "schema": schema,
            "receipt_sha256": "synthetic-receipt-payload-sha256",
            "mastered_audio": artifact_reference(mastered_audio, "mastered audio"),
            "output_quality": {
                "technical_ready": True,
                "blocking_failures": [],
            },
            "authority_boundary": {
                "candidate_only": True,
                "output_loudness_verified": True,
                "creative_approval_claimed": False,
            },
        }
        path = self.root / "audio-mastering-receipt.json"
        self._write_json(path, payload)
        return path, payload

    def _manifest(
        self,
        visual: Path,
        *,
        candidate: Path | None = None,
        technical_qa: Path | None = None,
    ) -> Path:
        visual_payload = json.loads(visual.read_text(encoding="utf-8"))
        payload: dict[str, object] = {
            "visual_plan_sha256": canonical_json_sha256(visual_payload)
        }
        if candidate is not None:
            payload["candidate_video"] = artifact_reference(
                candidate, "candidate video"
            )
        if technical_qa is not None:
            payload["technical_qa"] = artifact_reference(
                technical_qa, "technical QA report"
            )
        return self._json("render-manifest.json", payload)

    def _json(self, name: str, payload: dict) -> Path:
        path = self.root / name
        self._write_json(path, payload)
        return path

    @staticmethod
    def _write_json(path: Path, payload: dict) -> None:
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
