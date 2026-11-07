from __future__ import annotations

import json
import shutil
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from video_creation_pipeline.contract_io import file_sha256
from video_creation_pipeline.errors import ContractError
from video_creation_pipeline.execution_receipts import (
    build_ffmpeg_execution_receipt,
    write_execution_contract,
)
from video_creation_pipeline.privacy_redaction import (
    CLOAKFRAME_ADOPTION_SCHEMA,
    adopt_redaction,
    build_cloakframe_handoff,
    build_redaction_job,
    preview_redaction,
    privacy_redaction_doctor,
    run_redaction,
    validate_redaction_job,
    validate_redaction_receipt,
    write_redaction_job,
)


class PrivacyRedactionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = ROOT / ".test-runtime" / f"redaction-{uuid4().hex}"
        self.root.mkdir(parents=True)
        self.source = self.root / "source.mp4"
        self.source.write_bytes(b"source")
        self.tool = self.root / "sentryblur.exe"
        self.tool.write_bytes(b"fake executable")
        self.runtime_python = self.root / "python.exe"
        self.runtime_python.write_bytes(b"fake runtime")
        self.model_root = self.root / "models"
        self.model_root.mkdir()
        self.face_model = self.model_root / "scrfd.onnx"
        self.face_model.write_bytes(b"face model")
        self.plate_model = self.model_root / "plate.onnx"
        self.plate_model.write_bytes(b"plate model")

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def job(self, **extra: object) -> dict:
        spec: dict[str, object] = {
            "mode": "mask_all",
            "input": self.source,
            "output": self.root / "out.mp4",
            "sentryblur_executable": self.tool,
            "sentryblur_model_root": self.model_root,
            "sentryblur_models": {
                "face": self.face_model,
                "plate": self.plate_model,
            },
            "created_at": "2026-08-24T00:00:00Z",
        }
        spec.update(extra)
        return build_redaction_job(spec)

    def write_job(self, job: dict) -> Path:
        path = self.root / "job.json"
        write_redaction_job(job, path)
        return path

    @staticmethod
    def probe(*, audio: bool = True, duration: str = "2.0") -> dict:
        streams = [
            {
                "codec_type": "video",
                "avg_frame_rate": "30/1",
                "r_frame_rate": "30/1",
                "duration": duration,
            }
        ]
        if audio:
            streams.append({"codec_type": "audio", "duration": duration})
        return {"format": {"duration": duration}, "streams": streams}

    @staticmethod
    def decode_ok() -> dict:
        return {
            "verified": True,
            "command": ["ffmpeg", "-i", "output", "-f", "null", "-"],
            "returncode": 0,
            "stderr_tail": "",
        }

    @staticmethod
    def provider_stdout(*, gpu: bool = True) -> str:
        requested = "CUDAExecutionProvider" if gpu else "CPUExecutionProvider"
        available = ["CUDAExecutionProvider", "CPUExecutionProvider"]
        actual = available if gpu else ["CPUExecutionProvider"]
        return "\n".join(
            [
                "VIDEO_CREATION_ORT_RUNTIME="
                + (
                    '{"runtime_version":"1.25.0",'
                    f'"requested_execution_provider":"{requested}",'
                    f'"available_execution_providers":{available!r},'
                    f'"cuda_libraries_preloaded":{str(gpu).lower()}'
                    "}"
                ).replace("'", '"'),
                "VIDEO_CREATION_ORT_SESSION="
                + (
                    f'{{"requested_execution_provider":"{requested}",'
                    f'"actual_execution_providers":{actual!r}}}'
                ).replace("'", '"'),
            ]
        )

    def test_fixed_created_at_makes_plan_idempotent(self) -> None:
        first = self.job(targets=["face", "plate"])
        second = self.job(targets=["face", "plate"])
        self.assertEqual(first, second)
        self.assertEqual(first["job_sha256"], second["job_sha256"])
        self.assertEqual(first["parameters"]["dilation"], 112)
        self.assertEqual(first["parameters"]["window"], 5)
        self.assertEqual(first["parameters"]["pixel_size"], 48)
        self.assertEqual(first["parameters"]["blur_strength"], 51)
        self.assertEqual(first["parameters"]["confidence"], 0.25)
        self.assertTrue(first["parameters"]["gpu"])

    def test_mode_tool_model_and_output_are_explicit(self) -> None:
        with self.assertRaisesRegex(ContractError, "explicitly choose"):
            self.job(mode="")
        with self.assertRaisesRegex(ContractError, "executable"):
            self.job(sentryblur_executable=None, targets=["face"])
        with self.assertRaisesRegex(ContractError, "model root"):
            self.job(sentryblur_model_root=None, targets=["face"])
        with self.assertRaisesRegex(ContractError, "model file"):
            self.job(sentryblur_models={}, targets=["face"])
        (self.root / "out.mp4").write_bytes(b"existing")
        with self.assertRaisesRegex(ContractError, "distinct and absent"):
            self.job(targets=["face"])

    def test_model_and_executable_hash_drift_fail_closed(self) -> None:
        job = self.job(targets=["face"])
        self.face_model.write_bytes(b"changed model")
        with self.assertRaisesRegex(ContractError, "changed after"):
            validate_redaction_job(job)
        self.face_model.write_bytes(b"face model")
        job = self.job(targets=["face"])
        self.tool.write_bytes(b"changed executable")
        with self.assertRaisesRegex(ContractError, "executable hash drifted"):
            validate_redaction_job(job)

    def test_solid_is_visible_block_not_cloakframe_route(self) -> None:
        job = self.job(parameters={"style": "solid"}, targets=["face"])
        self.assertEqual(
            job["parameters"]["execution_route"], "blocked_unsupported_solid"
        )
        self.assertFalse(job["parameters"]["cloakframe_required"])
        path = self.write_job(job)
        with self.assertRaisesRegex(ContractError, "solid redaction is unsupported"):
            preview_redaction(path, self.root / "preview.jpg")
        with self.assertRaisesRegex(ContractError, "solid redaction is unsupported"):
            run_redaction(path, self.root / "receipt.json")

    def test_manual_region_requires_normalized_region_and_handoff(self) -> None:
        with self.assertRaisesRegex(ContractError, "at least one manual region"):
            self.job(targets=["manual_region"])
        job = self.job(
            mode="preserve_authorized_subject",
            targets=["face", "manual_region"],
            parameters={
                "manual_regions": [
                    {
                        "region_id": "screen-1",
                        "start_s": 0,
                        "end_s": 1.5,
                        "rect": {"x": 0.1, "y": 0.2, "width": 0.3, "height": 0.2},
                    }
                ]
            },
        )
        job_path = self.write_job(job)
        handoff = build_cloakframe_handoff(job_path, self.root / "handoff.json")
        self.assertTrue(handoff.is_file())
        preview = preview_redaction(job_path, self.root / "preview-handoff.json")
        self.assertEqual(preview["status"], "manual_review_required")

    @patch("video_creation_pipeline.privacy_redaction.subprocess.run")
    def test_preview_uses_real_plural_commands_and_separate_outputs(
        self, run: Mock
    ) -> None:
        def create_preview(command: list[str], **kwargs: object) -> Mock:
            self.assertEqual(kwargs["env"]["HF_HUB_OFFLINE"], "1")
            self.assertEqual(kwargs["env"]["USERPROFILE"], str(self.model_root))
            self.assertEqual(kwargs["env"]["HTTP_PROXY"], "http://127.0.0.1:9")
            self.assertNotIn("--last", command)
            Path(command[command.index("--output") + 1]).write_bytes(b"preview")
            return Mock(returncode=0, stdout=self.provider_stdout(), stderr="")

        run.side_effect = create_preview
        path = self.write_job(self.job(targets=["face", "plate"]))
        result = preview_redaction(path, self.root / "preview.jpg")
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(
            [
                next(value for value in command if value in {"faces", "plates"})
                for command in commands
            ],
            ["faces", "plates"],
        )
        self.assertTrue(
            all(command[0] == str(self.runtime_python) for command in commands)
        )
        self.assertTrue(
            all(command[1].endswith("_sentryblur_runner.py") for command in commands)
        )
        self.assertTrue(all("--preview" in command for command in commands))
        output_paths = [Path(row["path"]) for row in result["outputs"]]
        self.assertEqual(len(set(output_paths)), 2)
        self.assertTrue(all(path.is_file() for path in output_paths))
        self.assertTrue(result["model_cache_unchanged"])

    @patch("video_creation_pipeline.privacy_redaction.subprocess.run")
    def test_model_cache_mutation_fails_closed(self, run: Mock) -> None:
        def mutate_cache(command: list[str], **_: object) -> Mock:
            (self.model_root / "unexpected-download.bin").write_bytes(b"download")
            Path(command[command.index("--output") + 1]).write_bytes(b"preview")
            return Mock(returncode=0, stdout=self.provider_stdout(), stderr="")

        run.side_effect = mutate_cache
        path = self.write_job(self.job(targets=["face"]))
        with self.assertRaisesRegex(ContractError, "changed the pinned model root"):
            preview_redaction(path, self.root / "preview.jpg")
        self.assertFalse((self.root / "preview.jpg").exists())

    @patch(
        "video_creation_pipeline.privacy_redaction._full_decode",
        return_value=decode_ok(),
    )
    @patch("video_creation_pipeline.privacy_redaction.probe_media_metadata")
    @patch("video_creation_pipeline.privacy_redaction.execute_ffmpeg_with_receipt")
    @patch("video_creation_pipeline.privacy_redaction.subprocess.run")
    def test_run_multi_stage_records_hashes_logs_and_qa(
        self, run: Mock, execute_ffmpeg: Mock, probe: Mock, decode: Mock
    ) -> None:
        probe.return_value = self.probe()

        def make_stage(command: list[str], **_: object) -> Mock:
            Path(command[command.index("--output") + 1]).write_bytes(
                command[1].encode("ascii")
            )
            return Mock(returncode=0, stdout=self.provider_stdout(), stderr="")

        run.side_effect = make_stage

        def restore_audio(
            operation: str,
            command: list[str],
            inputs: list[dict[str, object]],
            outputs: list[dict[str, object]],
            receipt_path: str | Path,
            **kwargs: object,
        ) -> dict:
            Path(str(outputs[0]["path"])).write_bytes(b"audio restored")
            Path(str(kwargs["capture_log_path"])).write_text(
                "audio restored", encoding="utf-8"
            )
            receipt = build_ffmpeg_execution_receipt(
                operation, command, inputs, outputs
            )
            write_execution_contract(receipt, receipt_path)
            return receipt

        execute_ffmpeg.side_effect = restore_audio
        job_path = self.write_job(self.job(targets=["face", "plate"]))
        receipt = run_redaction(job_path, self.root / "receipt.json")
        self.assertTrue(receipt["multi_encode"])
        self.assertEqual(
            [stage["operation"] for stage in receipt["stages"]],
            ["faces", "plates", "restore_source_audio"],
        )
        restore_command = execute_ffmpeg.call_args.args[1]
        self.assertIn("1:a?", restore_command)
        self.assertEqual(restore_command[restore_command.index("-c:v") + 1], "copy")
        self.assertEqual(restore_command[restore_command.index("-c:a") + 1], "copy")
        self.assertEqual(
            receipt["stages"][-1]["ffmpeg_outlet_id"],
            "video_creation_pipeline.single_ffmpeg_outlet",
        )
        self.assertTrue(receipt["duration"]["within_one_frame"])
        self.assertTrue(receipt["audio"]["preserved"])
        self.assertEqual(receipt["engine"]["name"], "SentryBlur")
        self.assertTrue(receipt["engine"]["model_cache_unchanged"])
        self.assertEqual(
            receipt["engine"]["provider_audit"]["requested_execution_provider"],
            "CUDAExecutionProvider",
        )
        self.assertIn(
            "CUDAExecutionProvider",
            receipt["engine"]["provider_audit"]["actual_execution_providers"],
        )
        self.assertFalse(receipt["publish_allowed"])
        self.assertTrue(receipt["rescan_required"])
        validate_redaction_receipt(receipt)

    @patch(
        "video_creation_pipeline.privacy_redaction._full_decode",
        return_value=decode_ok(),
    )
    @patch("video_creation_pipeline.privacy_redaction.probe_media_metadata")
    @patch("video_creation_pipeline.privacy_redaction.subprocess.run")
    def test_run_can_capture_same_invocation_detector_coordinates(
        self, run: Mock, probe: Mock, decode: Mock
    ) -> None:
        probe.return_value = self.probe(audio=False)

        def make_stage(command: list[str], **kwargs: object) -> Mock:
            Path(command[command.index("--output") + 1]).write_bytes(b"stage")
            environment = kwargs["env"]
            capture = Path(environment["VIDEO_CREATION_REDACTION_CAPTURE_PATH"])
            capture.parent.mkdir(parents=True, exist_ok=True)
            capture.write_text(
                json.dumps(
                    {
                        "observation_id": "face-00000000-0000",
                        "frame_index": 0,
                        "frame_width": 1280,
                        "frame_height": 720,
                        "xyxy": [100.0, 100.0, 300.0, 400.0],
                        "confidence": 0.95,
                        "target_type": "face",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            self.assertEqual(
                "face", environment["VIDEO_CREATION_REDACTION_CAPTURE_TARGET"]
            )
            return Mock(returncode=0, stdout=self.provider_stdout(), stderr="")

        run.side_effect = make_stage
        job_path = self.write_job(self.job(targets=["face"]))
        receipt = run_redaction(
            job_path, self.root / "receipt.json", capture_zones=True
        )
        capture = receipt["stages"][0]["coordinate_capture"]
        self.assertTrue(Path(capture["path"]).is_file())
        self.assertTrue(receipt["stages"][0]["same_invocation_as_redaction"])
        self.assertEqual(
            {
                "enabled": True,
                "same_invocation_as_redaction": True,
                "format": "jsonl_xyxy_v1",
                "stage_count": 1,
            },
            receipt["engine"]["coordinate_capture"],
        )
        validate_redaction_receipt(receipt)

    @patch(
        "video_creation_pipeline.privacy_redaction._full_decode",
        return_value=decode_ok(),
    )
    @patch("video_creation_pipeline.privacy_redaction.probe_media_metadata")
    def test_adopt_requires_bound_manual_review(
        self, probe: Mock, decode: Mock
    ) -> None:
        probe.return_value = self.probe(audio=False)
        candidate = self.root / "candidate.mp4"
        candidate.write_bytes(b"candidate")
        job = self.job(mode="preserve_authorized_subject", targets=["face"])
        job_path = self.write_job(job)
        incomplete = {
            "schema": CLOAKFRAME_ADOPTION_SCHEMA,
            "job_sha256": job["job_sha256"],
            "candidate_sha256": file_sha256(candidate),
            "manual_track_review_completed": False,
            "operator": "reviewer",
            "tool_version": "local-build",
            "model_hash": "a" * 64,
        }
        with self.assertRaisesRegex(ContractError, "manual_track_review"):
            adopt_redaction(job_path, candidate, incomplete, self.root / "receipt.json")
        adoption = {**incomplete, "manual_track_review_completed": True}
        receipt = adopt_redaction(
            job_path, candidate, adoption, self.root / "receipt.json"
        )
        self.assertTrue((self.root / "out.mp4").is_file())
        self.assertEqual(receipt["engine"]["name"], "CloakFrame")
        validate_redaction_receipt(receipt)

    @patch("video_creation_pipeline.privacy_redaction.subprocess.run")
    def test_failure_does_not_leave_final(self, run: Mock) -> None:
        run.return_value = Mock(returncode=9, stdout="", stderr="failed")
        path = self.write_job(self.job(targets=["face"]))
        with self.assertRaises(ContractError):
            run_redaction(path, self.root / "receipt.json")
        self.assertFalse((self.root / "out.mp4").exists())

    @patch(
        "video_creation_pipeline.privacy_redaction._full_decode",
        return_value=decode_ok(),
    )
    @patch("video_creation_pipeline.privacy_redaction.probe_media_metadata")
    @patch("video_creation_pipeline.privacy_redaction.execute_ffmpeg_with_receipt")
    @patch("video_creation_pipeline.privacy_redaction.subprocess.run")
    def test_audio_loss_and_duration_drift_fail_qa(
        self, run: Mock, execute_ffmpeg: Mock, probe: Mock, decode: Mock
    ) -> None:
        def stage(command: list[str], **_: object) -> Mock:
            Path(command[command.index("--output") + 1]).write_bytes(b"stage")
            return Mock(returncode=0, stdout=self.provider_stdout(), stderr="")

        run.side_effect = stage

        def restore_audio(
            operation: str,
            command: list[str],
            inputs: list[dict[str, object]],
            outputs: list[dict[str, object]],
            receipt_path: str | Path,
            **kwargs: object,
        ) -> dict:
            Path(str(outputs[0]["path"])).write_bytes(b"audio restore failed")
            Path(str(kwargs["capture_log_path"])).write_text(
                "audio restore failed", encoding="utf-8"
            )
            receipt = build_ffmpeg_execution_receipt(
                operation, command, inputs, outputs
            )
            write_execution_contract(receipt, receipt_path)
            return receipt

        execute_ffmpeg.side_effect = restore_audio
        probe.side_effect = [
            self.probe(audio=True),
            self.probe(audio=True),
            self.probe(audio=False),
        ]
        path = self.write_job(self.job(targets=["face"]))
        with self.assertRaisesRegex(ContractError, "media QA"):
            run_redaction(path, self.root / "receipt.json")
        self.assertFalse((self.root / "out.mp4").exists())

    def test_source_hash_drift_fails_before_execution(self) -> None:
        job_path = self.write_job(self.job(targets=["face"]))
        self.source.write_bytes(b"changed")
        with self.assertRaisesRegex(ContractError, "changed after"):
            run_redaction(job_path, self.root / "receipt.json")

    def test_doctor_reports_truth_without_running_tools(self) -> None:
        report = privacy_redaction_doctor(sentryblur_executable=self.tool)
        self.assertTrue(report["contract_ready"])
        self.assertEqual(
            report["routes"]["solid"], "blocked_unsupported_by_selected_tools"
        )
        self.assertEqual(
            report["tools"]["sentryblur"]["status"], "available_unexecuted"
        )
        self.assertEqual(
            report["default_parameters"],
            {
                "style": "pixelate",
                "dilation": 112,
                "window": 5,
                "pixel_size": 48,
                "blur_strength": 51,
                "confidence": 0.25,
                "gpu": True,
            },
        )
        self.assertFalse(report["boundaries"]["network_model_download_allowed"])

    @patch("video_creation_pipeline.privacy_redaction.subprocess.run")
    def test_gpu_request_blocks_silent_cpu_fallback(self, run: Mock) -> None:
        def cpu_fallback(command: list[str], **_: object) -> Mock:
            Path(command[command.index("--output") + 1]).write_bytes(b"preview")
            return Mock(
                returncode=0,
                stdout=self.provider_stdout(gpu=False),
                stderr="",
            )

        run.side_effect = cpu_fallback
        path = self.write_job(self.job(targets=["face"]))
        with self.assertRaisesRegex(ContractError, "requested execution provider"):
            preview_redaction(path, self.root / "preview.jpg")

    @patch("video_creation_pipeline.privacy_redaction.subprocess.run")
    def test_gpu_inference_error_blocks_upstream_cpu_retry(self, run: Mock) -> None:
        def hidden_retry(command: list[str], **_: object) -> Mock:
            Path(command[command.index("--output") + 1]).write_bytes(b"preview")
            return Mock(
                returncode=0,
                stdout=(
                    self.provider_stdout()
                    + "\nEP Error: CUDA failed\n"
                    + "Falling back to ['CPUExecutionProvider'] and retrying."
                ),
                stderr="",
            )

        run.side_effect = hidden_retry
        path = self.write_job(self.job(targets=["face"]))
        with self.assertRaisesRegex(ContractError, "attempted a CPU fallback"):
            preview_redaction(path, self.root / "preview.jpg")

    @patch("video_creation_pipeline.privacy_redaction.subprocess.run")
    def test_explicit_cpu_route_remains_available(self, run: Mock) -> None:
        def cpu_stage(command: list[str], **_: object) -> Mock:
            self.assertNotIn("--gpu", command)
            Path(command[command.index("--output") + 1]).write_bytes(b"preview")
            return Mock(
                returncode=0,
                stdout=self.provider_stdout(gpu=False),
                stderr="",
            )

        run.side_effect = cpu_stage
        path = self.write_job(self.job(targets=["face"], parameters={"gpu": False}))
        result = preview_redaction(path, self.root / "preview.jpg")
        self.assertEqual(
            result["provider_audit"]["requested_execution_provider"],
            "CPUExecutionProvider",
        )


if __name__ == "__main__":
    unittest.main()
