from __future__ import annotations

import json
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from tempfile import TemporaryDirectory
from unittest.mock import patch

import video_creation_pipeline.background_processing as background
from video_creation_pipeline.background_processing import (
    RVM_MODEL_SHA256,
    RVM_SOURCE_COMMIT,
    background_processing_doctor,
    build_background_frame_extract_command,
    build_background_processing_plan,
    build_rvm_provider_command,
    validate_background_processing_plan,
    write_background_processing_contract,
)
from video_creation_pipeline.contract_io import payload_sha256
from video_creation_pipeline.errors import ContractError


class BackgroundProcessingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "source.mp4"
        self.source.write_bytes(b"synthetic-video-fixture")
        self.provider = self.root / "rvm"
        self.provider.mkdir()
        self.runtime = self.root / "runtime"
        self.runtime.mkdir()
        self.model = self.root / "rvm_mobilenetv3.pth"
        self.model.write_bytes(b"synthetic-model-placeholder")
        self.image = self.root / "background.png"
        self.image.write_bytes(b"synthetic-background-image")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_blur_plan_and_commands_keep_provider_and_ffmpeg_boundaries(self) -> None:
        with self._plan_patches():
            plan = self._build_plan("blur", self.root / "blur.mp4")
            frame_command = build_background_frame_extract_command(
                plan, self.source, self.root / "frames" / "%06d.png"
            )
            provider_command = build_rvm_provider_command(
                plan, self.root / "frames", self.root / "alpha"
            )

        self.assertIn("format=rgb24", frame_command[frame_command.index("-vf") + 1])
        self.assertEqual(
            provider_command[provider_command.index("--output-type") + 1],
            "png_sequence",
        )
        self.assertNotIn("--output-composition", provider_command)
        self.assertFalse(plan["execution_handoff"]["automatic_provider_fallback"])
        self.assertTrue(plan["authority_boundary"]["human_visual_review_required"])
        self.assertFalse(plan["authority_boundary"]["automatic_upload"])
        self.assertFalse(plan["authority_boundary"]["automatic_publish"])

    def test_replace_image_binds_background_artifact(self) -> None:
        with self._plan_patches():
            plan = self._build_plan(
                "replace_image",
                self.root / "replacement.mp4",
                background_image=self.image,
            )
            self.image.write_bytes(b"changed-background-image")
            with self.assertRaisesRegex(ContractError, "changed after the contract"):
                validate_background_processing_plan(plan)

    def test_replace_image_requires_background_image(self) -> None:
        with self._plan_patches():
            with self.assertRaisesRegex(ContractError, "requires a background image"):
                self._build_plan("replace_image", self.root / "replacement.mp4")

    def test_transparent_output_requires_mov(self) -> None:
        with self._plan_patches():
            with self.assertRaisesRegex(ContractError, "must use .mov"):
                self._build_plan("transparent", self.root / "transparent.mp4")

    def test_provider_fallback_drift_fails_closed(self) -> None:
        with self._plan_patches():
            plan = self._build_plan("replace_color", self.root / "color.mp4")
            plan["execution_handoff"]["automatic_provider_fallback"] = True
            plan["plan_sha256"] = payload_sha256(plan, "plan_sha256")
            with self.assertRaisesRegex(ContractError, "schema validation failed"):
                validate_background_processing_plan(plan)

    def test_provider_failure_leaves_visible_hash_bound_receipt(self) -> None:
        plan_path = self.root / "plan.json"
        failure_path = self.root / "provider-failure.json"
        log_path = self.root / "provider.log"
        with self._plan_patches():
            plan = self._build_plan("blur", self.root / "blur.mp4")
            write_background_processing_contract(plan, plan_path)
        command = ["python", "inference.py"]
        result = CompletedProcess(command, 7, stdout="", stderr="synthetic failure")

        with patch(
            "video_creation_pipeline.background_processing.subprocess.run",
            return_value=result,
        ):
            with self.assertRaisesRegex(ContractError, "returned exit code 7"):
                background._execute_rvm_provider(
                    plan_path,
                    plan,
                    command,
                    log_path,
                    failure_path,
                    timeout_s=10,
                )

        payload = json.loads(failure_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["status"], "failed_visible_operator_review_required")
        self.assertFalse(payload["execution_boundary"]["automatic_provider_fallback"])
        self.assertEqual(
            payload["failure_sha256"], payload_sha256(payload, "failure_sha256")
        )

    def test_doctor_reports_blocked_when_media_tools_are_missing(self) -> None:
        with patch(
            "video_creation_pipeline.background_processing.resolve_media_tool",
            side_effect=ContractError("missing"),
        ):
            report = background_processing_doctor(
                self.provider, self.model, self.runtime, device="cpu"
            )
        self.assertEqual(report["status"], "blocked_runtime_unavailable")
        self.assertFalse(report["checks"]["ffmpeg_available"])
        self.assertFalse(report["checks"]["ffprobe_available"])

    def _build_plan(
        self,
        mode: str,
        output: Path,
        *,
        background_image: Path | None = None,
    ) -> dict[str, object]:
        return build_background_processing_plan(
            self.source,
            output,
            self.provider,
            self.model,
            self.runtime,
            mode=mode,
            device="cpu",
            background_image_path=background_image,
        )

    def _plan_patches(self):
        return _PlanPatches(self)


class _PlanPatches:
    def __init__(self, case: BackgroundProcessingTests) -> None:
        self.case = case
        self.probe = None
        self.doctor = None

    def __enter__(self):
        self.probe = patch(
            "video_creation_pipeline.background_processing.probe_media_metadata",
            return_value={
                "streams": [
                    {
                        "codec_type": "video",
                        "codec_name": "h264",
                        "pix_fmt": "yuv420p",
                        "width": 320,
                        "height": 180,
                        "duration": "1.0",
                        "avg_frame_rate": "24/1",
                        "r_frame_rate": "24/1",
                    },
                    {
                        "codec_type": "audio",
                        "codec_name": "aac",
                        "sample_rate": "48000",
                        "channels": 2,
                    },
                ],
                "format": {"duration": "1.0"},
            },
        )
        self.doctor = patch(
            "video_creation_pipeline.background_processing.background_processing_doctor",
            return_value={
                "schema": "video_creation_pipeline.background_processing_doctor.v1",
                "status": "ready",
                "provider": {
                    "source_root": str(self.case.provider.resolve()),
                    "source_commit": RVM_SOURCE_COMMIT,
                    "source_files": {
                        "inference.py": {
                            "exists": True,
                            "sha256": "a" * 64,
                            "expected_sha256": "a" * 64,
                            "matches": True,
                        }
                    },
                    "model_path": str(self.case.model.resolve()),
                    "model_sha256": RVM_MODEL_SHA256,
                    "runtime_dependencies_path": str(self.case.runtime.resolve()),
                    "runtime": {
                        "status": "ready",
                        "torch": "test",
                        "cuda_available": False,
                    },
                    "device": "cpu",
                },
            },
        )
        self.probe.start()
        self.doctor.start()
        return self

    def __exit__(self, exc_type, exc, traceback):
        assert self.doctor is not None
        assert self.probe is not None
        self.doctor.stop()
        self.probe.stop()
        return False


if __name__ == "__main__":
    unittest.main()
