from __future__ import annotations

import math
import shutil
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from video_creation_pipeline import audio_mastering as AM  # noqa: E402
from video_creation_pipeline.contract_io import artifact_reference, read_json_object  # noqa: E402
from video_creation_pipeline.errors import ContractError  # noqa: E402
from video_creation_pipeline.production_strategy import (  # noqa: E402
    build_audio_mastering_plan,
    write_production_strategy_contract,
)


class MeasureFinalAudioTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = ROOT / ".test-runtime" / self._testMethodName
        if self.root.exists():
            shutil.rmtree(self.root)
        self.root.mkdir(parents=True)
        self.source_audio = self.root / "synthetic-source.wav"
        self.source_audio.write_bytes(b"RIFF-synthetic-source")
        self.target_audio = self.root / "synthetic-mastered.wav"
        self.final_media = self.root / "synthetic-final.mp4"
        self.final_media.write_bytes(b"synthetic-final-media-bytes")
        self.plan_path = self.root / "audio-mastering-plan.json"
        write_production_strategy_contract(
            build_audio_mastering_plan(
                self.source_audio,
                self.target_audio,
                target_lufs=-16.0,
                target_true_peak_db=-1.0,
                target_lra_lu=5.0,
            ),
            self.plan_path,
        )
        self.out_dir = self.root / "measure-out"
        self.out_dir.mkdir()

    def tearDown(self) -> None:
        if self.root.exists():
            shutil.rmtree(self.root)

    def test_invalid_timeout_rejected(self) -> None:
        for bad_timeout in (True, False, 0, -5, math.nan, math.inf):
            with self.subTest(timeout=bad_timeout), self.assertRaisesRegex(
                ContractError, "positive finite number"
            ):
                AM.measure_final_audio(
                    self.final_media,
                    self.plan_path,
                    self.out_dir,
                    timeout_s=bad_timeout,  # type: ignore[arg-type]
                )

    def test_missing_output_dir_rejected(self) -> None:
        missing = self.root / "does-not-exist"
        with self.assertRaisesRegex(ContractError, "must already exist"):
            AM.measure_final_audio(self.final_media, self.plan_path, missing)

    def test_preexisting_output_paths_rejected(self) -> None:
        for filename in (
            "loudnorm-output-verification.log",
            "loudnorm-output-verification-receipt.json",
            "loudnorm-output-verification.json",
        ):
            case_dir = self.root / f"preexisting-{filename}"
            case_dir.mkdir()
            (case_dir / filename).write_text("existing", encoding="utf-8")
            with self.subTest(filename=filename), self.assertRaisesRegex(
                ContractError, "already exist"
            ):
                AM.measure_final_audio(self.final_media, self.plan_path, case_dir)

    def test_measure_final_audio_writes_verification_report_with_mocked_ffmpeg(self) -> None:
        expected_media_ref = artifact_reference(self.final_media, "final media")
        mocked_report = {
            "schema": AM.AUDIO_MASTERING_OUTPUT_VERIFICATION_SCHEMA,
            "mastered_audio": expected_media_ref,
            "readiness": {"technical_ready": True, "blocking_failures": []},
        }
        with (
            patch.object(AM, "execute_ffmpeg_with_receipt") as mock_exec,
            patch.object(
                AM, "build_audio_mastering_output_verification", return_value=mocked_report
            ) as mock_build,
        ):
            report_path = AM.measure_final_audio(
                self.final_media, self.plan_path, self.out_dir, timeout_s=45.0
            )
        self.assertEqual(report_path, self.out_dir / "loudnorm-output-verification.json")
        self.assertTrue(report_path.is_file())
        self.assertEqual(
            read_json_object(report_path, "verification report"), mocked_report
        )
        mock_exec.assert_called_once()
        self.assertEqual(mock_exec.call_args.kwargs["timeout_s"], 45.0)
        self.assertIs(mock_exec.call_args.kwargs["guard_resource"], True)
        self.assertEqual(
            mock_exec.call_args.kwargs["capture_log_path"],
            self.out_dir / "loudnorm-output-verification.log",
        )
        mock_build.assert_called_once()

    def test_media_mutation_during_measurement_fails_closed(self) -> None:
        mutated_ref = artifact_reference(self.final_media, "final media")
        mutated_ref["sha256"] = "0" * 64
        mocked_report = {
            "schema": AM.AUDIO_MASTERING_OUTPUT_VERIFICATION_SCHEMA,
            "mastered_audio": mutated_ref,
            "readiness": {"technical_ready": False, "blocking_failures": ["mutated"]},
        }
        with (
            patch.object(AM, "execute_ffmpeg_with_receipt"),
            patch.object(
                AM, "build_audio_mastering_output_verification", return_value=mocked_report
            ),
            self.assertRaisesRegex(ContractError, "final media changed"),
        ):
            AM.measure_final_audio(self.final_media, self.plan_path, self.out_dir)
        self.assertFalse((self.out_dir / "loudnorm-output-verification.json").exists())
