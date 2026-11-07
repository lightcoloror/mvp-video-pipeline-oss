from __future__ import annotations

# ruff: noqa: E402

import hashlib
import json
import shutil
import sys
import unittest
import wave
from array import array
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from video_creation_pipeline.errors import ContractError
from video_creation_pipeline.reference_remix import build_voice_timing_plan
from video_creation_pipeline.tts_cli import main as tts_cli_main
from video_creation_pipeline.tts_pipeline import (
    build_audio_stitch_qa,
    build_resume_plan,
    build_tts_job,
    build_voice_measurements,
    normalize_tts_text,
)


class TtsPipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = ROOT / ".test-runtime" / self._testMethodName
        if self.root.exists():
            shutil.rmtree(self.root)
        self.root.mkdir(parents=True)

    def tearDown(self) -> None:
        if self.root.exists():
            shutil.rmtree(self.root)

    def test_normalizes_terms_dates_percent_phone_and_acronyms(self) -> None:
        value, replacements = normalize_tts_text(
            "SKU在2026-07-17增长12.5%，电话13800138000。",
            [{"find": "SKU", "replace": "库存单位"}],
        )

        self.assertEqual(
            value,
            "库存单位在二零二六年七月十七日增长百分之十二点五，电话一 三 八 零 零 一 三 八 零 零 零。",
        )
        self.assertEqual(
            {row["kind"] for row in replacements},
            {"term", "date", "percent", "phone"},
        )

    def test_hard_chunk_limits_apply_without_punctuation(self) -> None:
        recipe = self.recipe("这是很长的一段口播" * 40)
        config = self.config(max_chars=25, max_tokens=25, max_duration=5)

        job = build_tts_job(recipe, config)

        self.assertGreater(len(job["chunks"]), 1)
        self.assertTrue(
            all(row["normalized_char_count"] <= 20 for row in job["chunks"])
        )
        self.assertTrue(
            all(row["estimated_token_count"] <= 25 for row in job["chunks"])
        )
        self.assertTrue(all(row["estimated_duration_s"] <= 5 for row in job["chunks"]))
        self.assertEqual(len({row["voice_profile_sha256"] for row in job["chunks"]}), 1)
        self.assertEqual(job["run_registry"]["local_status_machine_created"], False)

    def test_commercial_use_rejects_noncommercial_provider(self) -> None:
        config = self.config()
        config["intended_use"] = "commercial"
        config["provider"]["commercial_use_allowed"] = True

        with self.assertRaisesRegex(ContractError, "not authorized for commercial"):
            build_tts_job(self.recipe("测试口播"), config)

    def test_resume_is_derived_from_receipts_and_honors_retry_limit(self) -> None:
        job = build_tts_job(
            self.recipe("第一句。第二句。第三句。"), self.config(max_chars=5)
        )
        completed = self.write_wav("completed.wav")
        rows = [self.receipt(job, job["chunks"][0], 1, "completed", completed)]
        rows.append(self.receipt(job, job["chunks"][1], 1, "failed"))
        rows.append(self.receipt(job, job["chunks"][2], 3, "failed"))

        plan = build_resume_plan(job, self.receipts(job, rows))

        self.assertEqual(plan["summary"]["completed_count"], 1)
        self.assertEqual(plan["summary"]["retryable_count"], 1)
        self.assertEqual(plan["summary"]["blocked_count"], 1)
        self.assertFalse(plan["derivation_boundary"]["stored_as_runtime_state"])

    def test_audio_qa_detects_format_mismatch(self) -> None:
        job = build_tts_job(self.recipe("第一句。第二句。"), self.config(max_chars=5))
        paths = [
            self.write_wav("one.wav", sample_rate=16000),
            self.write_wav("two.wav", sample_rate=24000),
        ]
        rows = [
            self.receipt(job, chunk, 1, "completed", paths[index])
            for index, chunk in enumerate(job["chunks"])
        ]

        qa = build_audio_stitch_qa(job, self.receipts(job, rows))

        self.assertFalse(qa["readiness"]["ready"])
        self.assertIn("format_mismatch", {row["kind"] for row in qa["issues"]})
        self.assertFalse(qa["ffmpeg_handoff"]["executed"])

    def test_audio_qa_and_measurements_feed_existing_timing_plan(self) -> None:
        recipe = self.recipe("第一句。第二句。")
        job = build_tts_job(recipe, self.config(max_chars=5))
        chunk_paths = [
            self.write_wav(f"chunk-{index}.wav") for index in range(len(job["chunks"]))
        ]
        receipts = self.receipts(
            job,
            [
                self.receipt(job, chunk, 1, "completed", chunk_paths[index])
                for index, chunk in enumerate(job["chunks"])
            ],
        )

        qa = build_audio_stitch_qa(job, receipts)
        stitched = self.write_wav("shot-001.wav", duration=2.0)
        measurements = build_voice_measurements(
            job,
            {
                "schema": "video_creation_pipeline.stitched_voice_outputs.v1",
                "shots": [{"shot_id": "shot-001", "path": str(stitched)}],
            },
        )
        timing = build_voice_timing_plan(recipe, measurements)

        self.assertTrue(qa["readiness"]["ready"])
        self.assertTrue(all(row["pause_after_ms"] >= 0 for row in qa["segments"]))
        self.assertEqual(
            measurements["schema"], "video_creation_pipeline.voice_measurements.v1"
        )
        self.assertEqual(measurements["shots"][0]["measured_duration_s"], 2.0)
        self.assertEqual(timing["shots"][0]["timing_basis"], "measured_voice_duration")

    def test_changed_audio_invalidates_completed_receipt(self) -> None:
        job = build_tts_job(self.recipe("测试。"), self.config())
        path = self.write_wav("changed.wav")
        receipts = self.receipts(
            job, [self.receipt(job, job["chunks"][0], 1, "completed", path)]
        )
        path.write_bytes(path.read_bytes() + b"changed")

        with self.assertRaisesRegex(ContractError, "missing or changed"):
            build_resume_plan(job, receipts)

    def test_cli_builds_job(self) -> None:
        recipe_path = self.root / "recipe.json"
        config_path = self.root / "config.json"
        output = self.root / "job.json"
        recipe_path.write_text(json.dumps(self.recipe("测试口播。")), encoding="utf-8")
        config_path.write_text(json.dumps(self.config()), encoding="utf-8")

        code = tts_cli_main(
            [
                "build-job",
                "--recipe",
                str(recipe_path),
                "--config",
                str(config_path),
                "--output",
                str(output),
            ]
        )

        self.assertEqual(code, 0)
        self.assertEqual(
            json.loads(output.read_text(encoding="utf-8"))["schema"],
            "video_creation_pipeline.tts_job.v1",
        )

    @staticmethod
    def recipe(script: str) -> dict:
        return {
            "schema": "video_creation_pipeline.reference_recipe.v1",
            "shots": [
                {
                    "shot_id": "shot-001",
                    "start_s": 0,
                    "end_s": 2,
                    "script": script,
                    "requirements": {},
                }
            ],
        }

    @staticmethod
    def config(
        *, max_chars: int = 140, max_tokens: int = 180, max_duration: float = 18
    ) -> dict:
        return {
            "schema": "video_creation_pipeline.tts_config.v1",
            "intended_use": "research_noncommercial",
            "provider": {
                "id": "chattts-local-research",
                "kind": "local_adapter",
                "model": "ChatTTS",
                "source_commit": "77b89ee281cd479f5b1a787ada330dc975ca1f2a",
                "license_scope": "AGPL-3.0+ code; CC BY-NC 4.0 model",
                "commercial_use_allowed": False,
            },
            "voice_profile": {
                "profile_id": "speaker-a",
                "speaker_embedding_ref": "voices/speaker-a.json",
                "seed": 42,
                "temperature": 0.3,
                "top_p": 0.7,
                "top_k": 20,
            },
            "chunk_policy": {
                "max_chars": max_chars,
                "max_tokens": max_tokens,
                "max_estimated_duration_s": max_duration,
                "chars_per_second": 4,
                "retry_limit": 3,
            },
            "run_registry": {
                "reference": "project-run-registry",
                "authoritative": True,
            },
        }

    def write_wav(
        self, name: str, *, sample_rate: int = 16000, duration: float = 0.1
    ) -> Path:
        path = self.root / name
        samples = array("h", [1000] * int(sample_rate * duration))
        with wave.open(str(path), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(sample_rate)
            handle.writeframes(samples.tobytes())
        return path

    @staticmethod
    def receipt(
        job: dict,
        chunk: dict,
        attempt: int,
        status: str,
        path: Path | None = None,
    ) -> dict:
        row = {
            "chunk_id": chunk["chunk_id"],
            "attempt": attempt,
            "status": status,
            "provider_id": job["provider"]["id"],
            "voice_profile_sha256": job["voice_profile_sha256"],
            "input_sha256": chunk["input_sha256"],
            "idempotency_key": chunk["idempotency_key"],
        }
        if status == "completed" and path is not None:
            with wave.open(str(path), "rb") as handle:
                duration = round(handle.getnframes() / handle.getframerate(), 3)
            row["output"] = {
                "path": str(path.resolve()),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "duration_s": duration,
            }
        return row

    @staticmethod
    def receipts(job: dict, rows: list[dict]) -> dict:
        return {
            "schema": "video_creation_pipeline.tts_chunk_receipts.v1",
            "tts_job_sha256": job["job_sha256"],
            "receipts": rows,
        }


if __name__ == "__main__":
    unittest.main()
