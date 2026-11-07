from __future__ import annotations

# ruff: noqa: E402

import os
import re
import shutil
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from video_creation_pipeline.contract_io import (
    atomic_write_json,
    file_sha256,
    payload_sha256,
    read_json_object,
)
from video_creation_pipeline.errors import ContractError
from video_creation_pipeline.execution_receipts import execute_ffmpeg_with_receipt
from video_creation_pipeline.speech_cleanup import (
    FFMPEG_SOURCE_COMMIT,
    build_speech_cleanup_plan,
    execute_speech_cleanup,
    speech_cleanup_doctor,
    validate_speech_cleanup_plan,
    validate_speech_cleanup_receipt,
    write_speech_cleanup_contract,
)
from video_creation_pipeline.speech_cleanup_cli import main as speech_cleanup_cli_main


class SpeechCleanupTests(unittest.TestCase):
    def setUp(self) -> None:
        managed = os.environ.get("TEST_TMP_ROOT")
        parent = Path(managed).resolve() if managed else ROOT / ".test-runtime"
        self.root = parent / f"speech-cleanup-{uuid4().hex}"
        self.root.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg is required")
    def test_doctor_reports_reviewed_local_filters(self) -> None:
        doctor = speech_cleanup_doctor()
        self.assertEqual(doctor["status"], "ready")
        self.assertEqual(doctor["source_commit"], FFMPEG_SOURCE_COMMIT)
        self.assertTrue(all(doctor["filters"].values()))
        self.assertFalse(doctor["authority_boundary"]["model_downloaded"])
        self.assertFalse(doctor["authority_boundary"]["automatic_profile_selection"])

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg is required")
    def test_strong_hiss_and_hum_cleanup_is_deterministic_and_audition_only(
        self,
    ) -> None:
        source = self._synthetic_noisy_source()
        output = self.root / "cleaned.wav"
        plan_path = self.root / "speech-cleanup-plan.json"
        review_path = atomic_write_json(dict(audio_checked=True,gentle_insufficient=True,evidence="synthetic strong-filter fixture, not real voice approval",source_sha256=file_sha256(source)),self.root/"strong-review.json")
        self.assertEqual(
            speech_cleanup_cli_main(
                [
                    "build-plan",
                    "--source",
                    str(source),
                    "--output-audio",
                    str(output),
                    "--profile",
                    "strong", "--processing-review", str(review_path),
                    "--noise-kind",
                    "broadband_hiss",
                    "--noise-kind",
                    "mains_hum",
                    "--mains-hz",
                    "50",
                    "--output",
                    str(plan_path),
                ]
            ),
            0,
        )
        plan = read_json_object(plan_path, "speech cleanup plan")
        self.assertEqual(
            [
                row["options"]["f"]
                for row in plan["filter_spec"]
                if row["name"] == "bandreject"
            ],
            [50.0, 100.0, 150.0, 200.0],
        )
        self.assertIn("afftdn=nr=18.000000", plan["filter_chain"])
        self.assertIn("nf=-35.000000:tn=1:fo=1.500000", plan["filter_chain"])

        work_dir = self.root / "work"
        receipt_path = self.root / "speech-cleanup-receipt.json"
        self.assertEqual(
            speech_cleanup_cli_main(
                [
                    "execute",
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
        first = read_json_object(receipt_path, "speech cleanup receipt")
        first_output_sha256 = file_sha256(output)
        second = execute_speech_cleanup(plan_path, work_dir, receipt_path)

        self.assertEqual(first, second)
        self.assertEqual(first_output_sha256, file_sha256(output))
        self.assertEqual(first["audition"]["status"], "needs_audition")
        self.assertFalse(first["audition"]["automatic_release"])
        self.assertFalse(first["authority_boundary"]["release_eligible"])
        self.assertFalse(first["technical_qa"]["spectral_improvement_claimed"])
        self.assertTrue(first["technical_qa"]["duration_within_tolerance"])
        validate_speech_cleanup_receipt(first)
        self.assertEqual(speech_cleanup_cli_main(["validate", str(receipt_path)]), 0)

        ffmpeg_receipt = read_json_object(
            work_dir / "speech-cleanup-ffmpeg-receipt.json", "FFmpeg receipt"
        )
        self.assertEqual(ffmpeg_receipt["operation"], "speech_cleanup")
        self.assertEqual(
            ffmpeg_receipt["outlet_id"],
            "video_creation_pipeline.single_ffmpeg_outlet",
        )

        hum_before = self._mean_volume(source, "bandpass=f=50:t=h:w=4", "hum-before")
        hum_after = self._mean_volume(output, "bandpass=f=50:t=h:w=4", "hum-after")
        hiss_before = self._mean_volume(source, "highpass=f=5000", "hiss-before")
        hiss_after = self._mean_volume(output, "highpass=f=5000", "hiss-after")
        self.assertGreater(hum_before - hum_after, 8.0)
        self.assertGreater(hiss_before - hiss_after, 3.0)

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg is required")
    def test_source_or_filter_drift_fails_closed_before_execution(self) -> None:
        source = self._synthetic_noisy_source()
        plan = build_speech_cleanup_plan(
            source,
            self.root / "cleaned.wav",
            profile="gentle",
            noise_kinds=["broadband_hiss"],
        )
        tampered = dict(plan)
        tampered["filter_chain"] = "volume=10"
        tampered["plan_sha256"] = payload_sha256(tampered, "plan_sha256")
        with self.assertRaisesRegex(ContractError, "filter chain drifted"):
            validate_speech_cleanup_plan(tampered)

        plan_path = self.root / "plan.json"
        write_speech_cleanup_contract(plan, plan_path)
        source.write_bytes(source.read_bytes() + b"drift")
        work_dir = self.root / "work"
        with self.assertRaisesRegex(ContractError, "changed after"):
            execute_speech_cleanup(plan_path, work_dir, self.root / "receipt.json")
        self.assertFalse(work_dir.exists())

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg is required")
    def test_mp4_input_extracts_audio_without_modifying_source_video(self) -> None:
        source = self._synthetic_noisy_video()
        source_sha256 = file_sha256(source)
        output = self.root / "video-audio-cleaned.wav"
        plan = build_speech_cleanup_plan(
            source,
            output,
            profile="strong",
            processing_review=dict(audio_checked=True,gentle_insufficient=True,evidence="synthetic filter fixture",source_sha256=file_sha256(source)),
            noise_kinds=["mains_hum"],
            mains_hz=50,
        )
        plan_path = self.root / "video-plan.json"
        receipt_path = self.root / "video-receipt.json"
        write_speech_cleanup_contract(plan, plan_path)

        receipt = execute_speech_cleanup(
            plan_path,
            self.root / "video-work",
            receipt_path,
        )

        self.assertEqual(file_sha256(source), source_sha256)
        self.assertEqual(receipt["technical_qa"]["source_audio"]["codec_name"], "aac")
        self.assertEqual(
            receipt["technical_qa"]["cleaned_audio"]["codec_name"], "pcm_s24le"
        )
        self.assertEqual(receipt["technical_qa"]["cleaned_audio"]["sample_rate"], 48000)
        self.assertTrue(receipt["technical_qa"]["duration_within_tolerance"])
        self.assertTrue(receipt["technical_qa"]["channel_count_preserved"])

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg is required")
    def test_failed_ffmpeg_pass_preserves_previous_candidate(self) -> None:
        source = self._synthetic_noisy_source()
        output = self.root / "cleaned.wav"
        previous = b"previous-cleanup-candidate"
        output.write_bytes(previous)
        plan = build_speech_cleanup_plan(
            source,
            output,
            profile="gentle",
            noise_kinds=["mains_hum"],
        )
        plan_path = self.root / "plan.json"
        write_speech_cleanup_contract(plan, plan_path)
        with patch(
            "video_creation_pipeline.speech_cleanup.build_speech_cleanup_command",
            return_value=["ffmpeg", "-definitely-invalid-option"],
        ):
            with self.assertRaisesRegex(ContractError, "returned exit code"):
                execute_speech_cleanup(
                    plan_path, self.root / "work", self.root / "receipt.json"
                )
        self.assertEqual(output.read_bytes(), previous)

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg is required")
    def test_failed_staging_qa_preserves_previous_candidate(self) -> None:
        source = self._synthetic_noisy_source()
        output = self.root / "cleaned.wav"
        previous = b"previous-cleanup-candidate"
        output.write_bytes(previous)
        plan = build_speech_cleanup_plan(
            source,
            output,
            profile="gentle",
            noise_kinds=["mains_hum"],
        )
        plan_path = self.root / "plan.json"
        write_speech_cleanup_contract(plan, plan_path)
        with patch(
            "video_creation_pipeline.speech_cleanup._technical_qa",
            side_effect=ContractError("synthetic staging QA failure"),
        ):
            with self.assertRaisesRegex(ContractError, "staging QA failure"):
                execute_speech_cleanup(
                    plan_path, self.root / "work", self.root / "receipt.json"
                )
        self.assertEqual(output.read_bytes(), previous)

    def _synthetic_noisy_source(self) -> Path:
        spec = self.root / "synthetic-noisy-speech.json"
        atomic_write_json(
            {
                "schema": "video_creation_pipeline.synthetic_noisy_speech.v1",
                "duration_s": 2.0,
                "speech_proxy_hz": 440,
                "mains_hz": 50,
                "noise_seed": 12345,
            },
            spec,
        )
        output = self.root / "noisy-source.wav"
        command = [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=2",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=50:sample_rate=48000:duration=2",
            "-f",
            "lavfi",
            "-i",
            "anoisesrc=color=white:amplitude=0.08:sample_rate=48000:duration=2:seed=12345",
            "-filter_complex",
            "[0:a]volume=0.18[speech];[1:a]volume=0.12[hum];"
            "[speech][hum][2:a]amix=inputs=3:normalize=0[out]",
            "-map",
            "[out]",
            "-map_metadata",
            "-1",
            "-fflags",
            "+bitexact",
            "-flags:a",
            "+bitexact",
            "-threads",
            "1",
            "-c:a",
            "pcm_s24le",
            str(output),
        ]
        execute_ffmpeg_with_receipt(
            "render",
            command,
            [{"role": "synthetic_fixture_spec", "path": spec}],
            [{"role": "synthetic_noisy_audio", "path": output}],
            self.root / "synthetic-noisy-audio-receipt.json",
        )
        return output

    def _synthetic_noisy_video(self) -> Path:
        spec = self.root / "synthetic-noisy-video.json"
        atomic_write_json(
            {
                "schema": "video_creation_pipeline.synthetic_noisy_video.v1",
                "duration_s": 1.0,
                "size": "320x180",
                "speech_proxy_hz": 440,
                "mains_hz": 50,
            },
            spec,
        )
        output = self.root / "noisy-source.mp4"
        command = [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=320x180:r=24:d=1",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=1",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=50:sample_rate=48000:duration=1",
            "-filter_complex",
            "[1:a]volume=0.18[speech];[2:a]volume=0.12[hum];"
            "[speech][hum]amix=inputs=2:normalize=0[aout]",
            "-map",
            "0:v:0",
            "-map",
            "[aout]",
            "-c:v",
            "mpeg4",
            "-q:v",
            "5",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-map_metadata",
            "-1",
            str(output),
        ]
        execute_ffmpeg_with_receipt(
            "render",
            command,
            [{"role": "synthetic_fixture_spec", "path": spec}],
            [{"role": "synthetic_noisy_video", "path": output}],
            self.root / "synthetic-noisy-video-receipt.json",
        )
        return output

    def _mean_volume(self, source: Path, filter_chain: str, label: str) -> float:
        log = self.root / f"{label}.log"
        receipt = self.root / f"{label}.receipt.json"
        command = [
            "ffmpeg",
            "-hide_banner",
            "-nostats",
            "-i",
            str(source),
            "-map",
            "0:a:0",
            "-vn",
            "-af",
            f"{filter_chain},volumedetect",
            "-f",
            "null",
            "-",
        ]
        execute_ffmpeg_with_receipt(
            "audio_qa",
            command,
            [{"role": "audio", "path": source}],
            [{"role": "measurement_log", "path": log}],
            receipt,
            capture_log_path=log,
        )
        match = re.findall(
            r"mean_volume:\s*(-?\d+(?:\.\d+)?) dB", log.read_text(encoding="utf-8")
        )
        if not match:
            self.fail(f"mean volume was not reported for {label}")
        return float(match[-1])


if __name__ == "__main__":
    unittest.main()
