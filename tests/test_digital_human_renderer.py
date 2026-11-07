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
    atomic_write_json,
    file_sha256,
    payload_sha256,
)
from video_creation_pipeline.digital_human_foundation import (
    generate_digital_human_foundation_synthetic,
)
from video_creation_pipeline.digital_human_renderer import (
    build_avatar_renderer_request,
    build_blocked_renderer_receipt,
    generate_digital_human_renderer_synthetic_fixture,
    run_synthetic_avatar_renderer_stub,
    validate_avatar_renderer_receipt,
    validate_avatar_renderer_request,
    validate_digital_human_renderer_fixture_bundle,
    validate_renderer_candidate_timeline,
    validate_renderer_review_packet,
)
from video_creation_pipeline.errors import ContractError


@unittest.skipUnless(
    shutil.which("ffmpeg") and shutil.which("ffprobe"),
    "FFmpeg and ffprobe are required for the digital-human renderer tests",
)
class DigitalHumanRendererTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = ROOT / ".test-runtime" / f"digital-human-renderer-{uuid4().hex}"
        inputs = validate_content_video_inputs(
            FIXTURES / "content-video-brief.v1.json",
            FIXTURES / "content-video-brief.v1.schema.json",
            FIXTURES / "research-output.v1.json",
            FIXTURES / "research-output.v1.schema.json",
            max_age_hours=1_000_000,
        )
        plan = build_content_video_project_plan(inputs)
        write_content_video_plan_bundle(plan, cls.root)
        generate_digital_human_foundation_synthetic(cls.root, timeout_s=180)
        cls.first = generate_digital_human_renderer_synthetic_fixture(
            cls.root, timeout_s=180
        )
        cls.contract_dir = cls.root / "digital-human-renderer" / "contracts"
        cls.output_dir = cls.root / "digital-human-renderer" / "provider-output"
        cls.fixture_dir = cls.root / "digital-human-renderer" / "fixture"
        cls.first_identity = cls._read_contract("renderer-run-receipt.json")[
            "deterministic_identity"
        ]
        cls.second = generate_digital_human_renderer_synthetic_fixture(
            cls.root, timeout_s=180
        )

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.root, ignore_errors=True)

    @classmethod
    def _read_contract(cls, name: str) -> dict[str, object]:
        return json.loads((cls.contract_dir / name).read_text(encoding="utf-8"))

    @classmethod
    def _read_receipt(cls) -> dict[str, object]:
        return json.loads(
            (cls.output_dir / "renderer-receipt.json").read_text(encoding="utf-8")
        )

    @staticmethod
    def _rehash(payload: dict[str, object], field: str) -> dict[str, object]:
        payload[field] = payload_sha256(payload, field)
        return payload

    def test_synthetic_stub_bundle_is_candidate_only_and_double_run_idempotent(
        self,
    ) -> None:
        validation = validate_digital_human_renderer_fixture_bundle(self.root)
        run = self._read_contract("renderer-run-receipt.json")
        request = self._read_contract("renderer-request.json")
        receipt = self._read_receipt()
        timeline = self._read_contract("renderer-candidate-timeline.json")
        self.assertTrue(validation["valid"])
        self.assertTrue(validation["candidate_only"])
        self.assertTrue(validation["synthetic_only"])
        self.assertEqual(self.first_identity, run["deterministic_identity"])
        self.assertEqual(
            self.first["candidate_timeline_sha256"],
            self.second["candidate_timeline_sha256"],
        )
        self.assertEqual(request["idempotency_key"], receipt["idempotency_key"])
        self.assertFalse(run["authority_boundary"]["provider_called"])
        self.assertFalse(run["authority_boundary"]["network_used"])
        self.assertFalse(run["authority_boundary"]["upload_performed"])
        self.assertFalse(run["authority_boundary"]["publish_performed"])
        self.assertEqual(request["correction_ranges"], [])
        self.assertEqual(request["rerecord_ranges"], [])
        self.assertEqual(request["inputs"]["b_roll"], [])
        self.assertEqual(
            [row["clip_id"] for row in timeline["clips"]],
            [
                row["clip_id"]
                for row in json.loads(
                    (
                        self.root
                        / "digital-human-foundation"
                        / "contracts"
                        / "candidate-timeline.json"
                    ).read_text(encoding="utf-8")
                )["clips"]
            ],
        )

    def test_provider_unavailable_blocks_before_renderer_call(self) -> None:
        preflight_path = self.fixture_dir / "preflight-unavailable.json"
        preflight = json.loads(
            (self.fixture_dir / "preflight.json").read_text(encoding="utf-8")
        )
        preflight["ready"] = False
        atomic_write_json(preflight, preflight_path)
        with self.assertRaisesRegex(ContractError, "provider unavailable"):
            build_avatar_renderer_request(
                self.root,
                capability_preflight_path=preflight_path,
                avatar_asset_path=self.fixture_dir / "avatar-asset.json",
                voice_asset_path=self.fixture_dir / "voice-asset.json",
                avatar_authorization_path=self.fixture_dir
                / "avatar-authorization.json",
                voice_authorization_path=self.fixture_dir / "voice-authorization.json",
            )
        self.assertFalse((self.output_dir / "unavailable-output.mp4").exists())

    def test_missing_avatar_or_voice_authorization_blocks(self) -> None:
        for kind in ("avatar", "voice"):
            with self.subTest(kind=kind):
                authorization_path = self.fixture_dir / f"{kind}-invalid-auth.json"
                authorization = json.loads(
                    (self.fixture_dir / f"{kind}-authorization.json").read_text(
                        encoding="utf-8"
                    )
                )
                authorization["authorization_status"] = "missing"
                atomic_write_json(authorization, authorization_path)
                kwargs = {
                    "capability_preflight_path": self.fixture_dir / "preflight.json",
                    "avatar_asset_path": self.fixture_dir / "avatar-asset.json",
                    "voice_asset_path": self.fixture_dir / "voice-asset.json",
                    "avatar_authorization_path": self.fixture_dir
                    / "avatar-authorization.json",
                    "voice_authorization_path": self.fixture_dir
                    / "voice-authorization.json",
                }
                kwargs[f"{kind}_authorization_path"] = authorization_path
                with self.assertRaisesRegex(ContractError, "authorization missing"):
                    build_avatar_renderer_request(self.root, **kwargs)

    def test_asset_hash_drift_blocks_before_stub_execution(self) -> None:
        request = self._read_contract("renderer-request.json")
        candidate = copy.deepcopy(request)
        candidate["inputs"]["avatar_asset"]["sha256"] = "0" * 64
        candidate["idempotency_key"] = self._request_identity_for_test(candidate)
        self._rehash(candidate, "request_sha256")
        with self.assertRaisesRegex(ContractError, "changed after the contract"):
            validate_avatar_renderer_request(candidate)

    def test_audio_video_or_slot_duration_drift_is_rejected(self) -> None:
        request = self._read_contract("renderer-request.json")
        receipt = self._read_receipt()
        candidate = copy.deepcopy(receipt)
        candidate["outputs"][0]["output_artifact"] = copy.deepcopy(
            candidate["outputs"][1]["output_artifact"]
        )
        candidate["outputs"][0]["ffmpeg_receipt"] = copy.deepcopy(
            candidate["outputs"][1]["ffmpeg_receipt"]
        )
        candidate["outputs"][0]["technical_probe"] = copy.deepcopy(
            candidate["outputs"][1]["technical_probe"]
        )
        self._rehash(candidate, "receipt_sha256")
        with self.assertRaisesRegex(ContractError, "A/V or slot duration drifted"):
            validate_avatar_renderer_receipt(request, candidate)

    def test_failed_or_untested_lip_sync_cannot_promote_candidate(self) -> None:
        request = self._read_contract("renderer-request.json")
        for status in ("failed", "not_tested"):
            with self.subTest(status=status):
                receipt = self._read_receipt()
                receipt["outputs"][0]["lip_sync_qa"]["status"] = status
                self._rehash(receipt, "receipt_sha256")
                with self.assertRaisesRegex(
                    ContractError, "lip sync or face QA failed"
                ):
                    validate_avatar_renderer_receipt(request, receipt)

    def test_incomplete_tts_blocks_request(self) -> None:
        request = self._read_contract("renderer-request.json")
        original_path = Path(request["inputs"]["tts_resume_plan"]["path"])
        resume = json.loads(original_path.read_text(encoding="utf-8"))
        resume["summary"]["status"] = "blocked"
        drift_path = self.contract_dir / "tts-resume-blocked.json"
        atomic_write_json(resume, drift_path)
        candidate = copy.deepcopy(request)
        candidate["inputs"]["tts_resume_plan"] = artifact_reference(
            drift_path, "blocked TTS resume"
        )
        candidate["idempotency_key"] = self._request_identity_for_test(candidate)
        self._rehash(candidate, "request_sha256")
        with self.assertRaisesRegex(ContractError, "TTS incomplete"):
            validate_avatar_renderer_request(candidate)

    def test_missing_ppt_or_interview_never_falls_back_silently(self) -> None:
        request = self._read_contract("renderer-request.json")
        for key, message in (("slides", "PPT"), ("interviews", "interview")):
            with self.subTest(key=key):
                candidate = copy.deepcopy(request)
                candidate["inputs"][key] = []
                candidate["idempotency_key"] = self._request_identity_for_test(
                    candidate
                )
                self._rehash(candidate, "request_sha256")
                with self.assertRaisesRegex(ContractError, message):
                    validate_avatar_renderer_request(candidate)

    def test_receipt_rejects_silent_fallback_provider_change_and_publish(self) -> None:
        request = self._read_contract("renderer-request.json")
        mutations = (
            (
                "fallback",
                lambda row: row["execution"].update({"fallback_used": True}),
            ),
            (
                "provider",
                lambda row: row["provider"].update({"id": "different-provider"}),
            ),
            (
                "publish",
                lambda row: row["execution"].update({"publish_performed": True}),
            ),
        )
        for name, mutate in mutations:
            with self.subTest(name=name):
                receipt = self._read_receipt()
                mutate(receipt)
                self._rehash(receipt, "receipt_sha256")
                with self.assertRaises(ContractError):
                    validate_avatar_renderer_receipt(request, receipt)

    def test_non_avatar_timeline_mutation_and_release_upgrade_fail(self) -> None:
        timeline = self._read_contract("renderer-candidate-timeline.json")
        non_avatar = copy.deepcopy(timeline)
        target = next(row for row in non_avatar["clips"] if row["track"] == "slides")
        target["end_s"] -= 0.25
        self._rehash(non_avatar, "timeline_sha256")
        with self.assertRaisesRegex(ContractError, "non-avatar"):
            validate_renderer_candidate_timeline(non_avatar)
        review = self._read_contract("renderer-review-packet.json")
        review["authority_boundary"]["release_eligible"] = True
        self._rehash(review, "review_packet_sha256")
        with self.assertRaisesRegex(ContractError, "authority boundary"):
            validate_renderer_review_packet(review)

    def test_blocked_receipt_has_stable_error_and_no_output(self) -> None:
        request = self._read_contract("renderer-request.json")
        receipt = build_blocked_renderer_receipt(
            request,
            code="renderer_provider_unavailable",
            message="synthetic provider is unavailable",
            next_action="repair_provider_preflight",
        )
        self.assertEqual(receipt["status"], "blocked")
        self.assertIsNone(receipt["outputs"])
        self.assertFalse(receipt["error"]["retryable"])
        validate_avatar_renderer_receipt(request, receipt)

    def test_stub_cache_is_single_invocation_and_output_hash_stable(self) -> None:
        request_path = self.contract_dir / "renderer-request.json"
        before = {
            path.name: file_sha256(path)
            for path in self.output_dir.glob("visual-*.mp4")
        }
        first = run_synthetic_avatar_renderer_stub(request_path, self.output_dir)
        second = run_synthetic_avatar_renderer_stub(request_path, self.output_dir)
        after = {
            path.name: file_sha256(path)
            for path in self.output_dir.glob("visual-*.mp4")
        }
        self.assertEqual(first["receipt_sha256"], second["receipt_sha256"])
        self.assertEqual(before, after)

    def test_input_change_changes_idempotency_key(self) -> None:
        request = self._read_contract("renderer-request.json")
        candidate = copy.deepcopy(request)
        candidate["correction_ranges"] = [{"start_s": 0.5, "end_s": 1.0}]
        changed = self._request_identity_for_test(candidate)
        self.assertNotEqual(request["idempotency_key"], changed)

    @staticmethod
    def _request_identity_for_test(request: dict[str, object]) -> str:
        fields = (
            "mode",
            "provider",
            "adapter",
            "capability_preflight",
            "inputs",
            "target_slots",
            "correction_ranges",
            "rerecord_ranges",
            "output_requirements",
            "execution_policy",
            "authority_boundary",
        )
        from video_creation_pipeline.contract_io import canonical_json_sha256

        return canonical_json_sha256({key: request.get(key) for key in fields})


if __name__ == "__main__":
    unittest.main()
