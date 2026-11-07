from __future__ import annotations

# ruff: noqa: E402

import shutil
import sys
import unittest
import wave
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from video_creation_pipeline.audio_mastering import (
    AUDIO_MASTERING_EXECUTION_RECEIPT_SCHEMA,
    AUDIO_MASTERING_OUTPUT_VERIFICATION_SCHEMA,
    LEGACY_AUDIO_MASTERING_EXECUTION_RECEIPT_SCHEMA,
    build_output_verification_command,
    evaluate_audio_mastering_output_measurements,
    execute_audio_mastering,
    parse_loudnorm_measurement_log,
    validate_audio_mastering_execution_receipt,
)
from video_creation_pipeline.contract_io import (
    atomic_write_json,
    file_sha256,
    payload_sha256,
    read_json_object,
)
from video_creation_pipeline.creative_cli import main as creative_cli_main
from video_creation_pipeline.errors import ContractError
from video_creation_pipeline.execution_receipts import execute_ffmpeg_with_receipt
from video_creation_pipeline.production_strategy import (
    build_audio_mastering_plan,
    write_production_strategy_contract,
)


class AudioMasteringExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = ROOT / ".test-runtime" / self._testMethodName
        if self.root.exists():
            shutil.rmtree(self.root)
        self.root.mkdir(parents=True)

    def tearDown(self) -> None:
        if self.root.exists():
            shutil.rmtree(self.root)

    def test_parser_extracts_the_last_complete_loudnorm_document(self) -> None:
        text = """
        unrelated {"status": "ok"}
        {
          "input_i": "-31.25",
          "input_tp": "-24.10",
          "input_lra": "0.00",
          "input_thresh": "-41.25",
          "target_offset": "0.15"
        }
        """
        self.assertEqual(
            parse_loudnorm_measurement_log(text),
            {
                "input_i": -31.25,
                "input_tp": -24.1,
                "input_lra": 0.0,
                "input_thresh": -41.25,
                "target_offset": 0.15,
            },
        )
        with self.assertRaisesRegex(ContractError, "measurement JSON was not found"):
            parse_loudnorm_measurement_log("no loudness measurements")

    def test_output_measurement_policy_checks_lufs_true_peak_and_lra(self) -> None:
        policy = {
            "target_lufs": -16.0,
            "target_true_peak_db": -1.0,
            "target_lra_lu": 5.0,
        }
        passed = evaluate_audio_mastering_output_measurements(
            {
                "input_i": -16.3,
                "input_tp": -1.0,
                "input_lra": 4.0,
                "input_thresh": -26.0,
                "target_offset": 0.0,
            },
            policy,
        )
        failed = evaluate_audio_mastering_output_measurements(
            {
                "input_i": -17.0,
                "input_tp": -0.5,
                "input_lra": 7.0,
                "input_thresh": -27.0,
                "target_offset": 0.0,
            },
            policy,
        )

        self.assertTrue(passed["readiness"]["technical_ready"])
        self.assertEqual(
            failed["readiness"]["blocking_failures"],
            ["integrated_lufs", "true_peak_ceiling", "loudness_range_ceiling"],
        )
        command = build_output_verification_command("candidate.wav", policy)
        filter_graph = command[command.index("-af") + 1]
        self.assertIn("loudnorm=", filter_graph)
        self.assertNotIn("highpass", filter_graph)
        self.assertNotIn("alimiter", filter_graph)

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg is required")
    def test_mastering_is_hash_stable_and_verifies_final_output(self) -> None:
        source = self._synthetic_source()
        output = self.root / "mastered.wav"
        plan_path = self.root / "audio-mastering-plan.json"
        write_production_strategy_contract(
            build_audio_mastering_plan(
                source,
                output,
                target_lufs=-16.0,
                target_true_peak_db=-1.0,
                target_lra_lu=5.0,
            ),
            plan_path,
        )
        work_dir = self.root / "mastering"
        receipt_path = self.root / "audio-mastering-receipt.json"

        first = execute_audio_mastering(plan_path, work_dir, receipt_path)
        first_output_sha = file_sha256(output)
        first_measurements = read_json_object(
            work_dir / "loudnorm-measurements.json", "loudnorm measurements"
        )["measurements"]
        self.assertEqual(
            creative_cli_main(
                [
                    "execute-audio-mastering",
                    "--plan",
                    str(plan_path),
                    "--work-dir",
                    str(work_dir),
                    "--receipt",
                    str(receipt_path),
                ]
            ),
            0,
        )
        second = read_json_object(receipt_path, "audio mastering receipt")

        self.assertEqual(
            first["mastered_audio"]["sha256"],
            second["mastered_audio"]["sha256"],
        )
        self.assertEqual(first_output_sha, file_sha256(output))
        self.assertEqual(second["schema"], AUDIO_MASTERING_EXECUTION_RECEIPT_SCHEMA)
        self.assertEqual(second["status"], "completed_technical_candidate")
        self.assertTrue(second["output_quality"]["technical_ready"])
        self.assertTrue(second["authority_boundary"]["candidate_only"])
        self.assertTrue(second["authority_boundary"]["output_loudness_verified"])
        self.assertFalse(second["authority_boundary"]["release_eligible"])
        self.assertFalse(second["authority_boundary"]["automatic_upload"])
        self.assertFalse(second["authority_boundary"]["automatic_publish"])
        validate_audio_mastering_execution_receipt(second)

        output_verification = read_json_object(
            work_dir / "loudnorm-output-verification.json",
            "audio mastering output verification",
        )
        self.assertEqual(
            output_verification["schema"], AUDIO_MASTERING_OUTPUT_VERIFICATION_SCHEMA
        )
        self.assertTrue(output_verification["readiness"]["technical_ready"])
        self.assertEqual(
            [row["status"] for row in output_verification["checks"]],
            ["passed", "passed", "passed"],
        )

        legacy = deepcopy(second)
        legacy["schema"] = LEGACY_AUDIO_MASTERING_EXECUTION_RECEIPT_SCHEMA
        legacy.pop("output_verification")
        legacy.pop("output_verification_pass_receipt")
        legacy.pop("output_quality")
        legacy["authority_boundary"].pop("output_loudness_verified")
        legacy["receipt_sha256"] = payload_sha256(legacy, "receipt_sha256")
        validate_audio_mastering_execution_receipt(legacy)

        measurements = read_json_object(
            work_dir / "loudnorm-measurements.json", "loudnorm measurements"
        )
        self.assertEqual(first_measurements, measurements["measurements"])
        self.assertEqual(measurements["policy"]["target_lufs"], -16.0)
        self.assertEqual(
            set(measurements["measurements"]),
            {"input_i", "input_tp", "input_lra", "input_thresh", "target_offset"},
        )
        for name in (
            "loudnorm-measurement-receipt.json",
            "loudnorm-apply-receipt.json",
            "loudnorm-output-verification-receipt.json",
        ):
            pass_receipt = read_json_object(work_dir / name, "FFmpeg receipt")
            self.assertEqual(
                pass_receipt["outlet_id"],
                "video_creation_pipeline.single_ffmpeg_outlet",
            )
            self.assertEqual(pass_receipt["operation"], "audio_mastering")
        with wave.open(str(output), "rb") as mastered:
            self.assertEqual(mastered.getframerate(), 48000)
            self.assertEqual(mastered.getsampwidth(), 3)

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg is required")
    def test_source_drift_blocks_before_measurement_execution(self) -> None:
        source = self._synthetic_source()
        plan_path = self.root / "audio-mastering-plan.json"
        write_production_strategy_contract(
            build_audio_mastering_plan(source, self.root / "mastered.wav", target_lufs=-14.0),
            plan_path,
        )
        source.write_bytes(source.read_bytes() + b"drift")

        with self.assertRaisesRegex(ContractError, "changed after"):
            execute_audio_mastering(
                plan_path,
                self.root / "mastering",
                self.root / "receipt.json",
            )
        self.assertFalse((self.root / "mastering").exists())

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg is required")
    def test_failed_apply_pass_preserves_the_previous_candidate(self) -> None:
        source = self._synthetic_source()
        output = self.root / "mastered.wav"
        previous = b"previous-candidate-must-survive"
        output.write_bytes(previous)
        plan_path = self.root / "audio-mastering-plan.json"
        write_production_strategy_contract(
            build_audio_mastering_plan(source, output, target_lufs=-14.0),
            plan_path,
        )

        with patch(
            "video_creation_pipeline.audio_mastering.build_apply_command",
            return_value=["ffmpeg", "-definitely-invalid-option"],
        ):
            with self.assertRaisesRegex(ContractError, "returned exit code"):
                execute_audio_mastering(
                    plan_path,
                    self.root / "mastering",
                    self.root / "receipt.json",
                )

        self.assertEqual(output.read_bytes(), previous)

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg is required")
    def test_failed_output_quality_gate_preserves_the_previous_candidate(self) -> None:
        source = self._synthetic_source()
        output = self.root / "mastered.wav"
        previous = b"previous-candidate-must-survive"
        output.write_bytes(previous)
        plan_path = self.root / "audio-mastering-plan.json"
        write_production_strategy_contract(
            build_audio_mastering_plan(source, output, target_lufs=-14.0),
            plan_path,
        )
        failed_evaluation = evaluate_audio_mastering_output_measurements(
            {
                "input_i": -20.0,
                "input_tp": 0.0,
                "input_lra": 9.0,
                "input_thresh": -30.0,
                "target_offset": 0.0,
            },
            {
                "target_lufs": -14.0,
                "target_true_peak_db": -1.0,
                "target_lra_lu": 5.0,
            },
        )
        with patch(
            "video_creation_pipeline.audio_mastering.evaluate_audio_mastering_output_measurements",
            return_value=failed_evaluation,
        ):
            with self.assertRaisesRegex(
                ContractError, "failed final loudness verification"
            ):
                execute_audio_mastering(
                    plan_path,
                    self.root / "mastering",
                    self.root / "receipt.json",
                )

        self.assertEqual(output.read_bytes(), previous)
        self.assertFalse((self.root / "receipt.json").exists())

    def _synthetic_source(self) -> Path:
        fixture_spec = self.root / "synthetic-audio-fixture.json"
        atomic_write_json(
            {
                "schema": "video_creation_pipeline.synthetic_audio_fixture.v1",
                "frequency_hz": 440,
                "duration_s": 1.5,
                "volume": 0.02,
            },
            fixture_spec,
        )
        source = self.root / "source.wav"
        command = [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=1.5",
            "-af",
            "volume=0.02",
            "-map_metadata",
            "-1",
            "-fflags",
            "+bitexact",
            "-flags:a",
            "+bitexact",
            "-threads",
            "1",
            "-c:a",
            "pcm_s16le",
            str(source),
        ]
        execute_ffmpeg_with_receipt(
            "render",
            command,
            [{"role": "synthetic_fixture_spec", "path": fixture_spec}],
            [{"role": "synthetic_audio", "path": source}],
            self.root / "synthetic-audio-receipt.json",
        )
        return source


if __name__ == "__main__":
    unittest.main()
