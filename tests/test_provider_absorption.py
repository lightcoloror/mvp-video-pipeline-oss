from __future__ import annotations

# ruff: noqa: E402

import json
import shutil
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from video_creation_pipeline.contract_io import artifact_reference, payload_sha256
from video_creation_pipeline.errors import ContractError
from video_creation_pipeline.execution_receipts import (
    CRISPASR_COMMIT,
    CRISPASR_VERSION,
    FFMPEG_EXECUTION_RECEIPT_SCHEMA,
    FFMPEG_OUTLET_ID,
    SPEECH_EXECUTION_RECEIPT_SCHEMA,
    build_speech_input_contract,
    validate_speech_execution_receipt,
)
from video_creation_pipeline.local_multimodal import (
    LLAMA_CPP_COMMIT,
    LOCAL_MULTIMODAL_RECEIPT_SCHEMA,
    build_frame_manifest,
    build_local_multimodal_plan,
    validate_local_multimodal_receipt,
    write_local_multimodal_contract,
)
from video_creation_pipeline.material_index import (
    MATERIAL_INDEX_BENCHMARK_SCHEMA,
    MATERIAL_INDEX_GOLDSET_SCHEMA,
    MATERIAL_INDEX_RESULTS_SCHEMA,
    MATERIAL_INDEX_SHOT_QUERIES_SCHEMA,
    SQLITE_VEC_COMMIT,
    SQLITE_VEC_VERSION,
    build_material_candidates_from_index,
    build_material_index_benchmark,
    write_material_index_contract,
)


class ProviderAbsorptionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = ROOT / ".test-runtime" / self._testMethodName
        if self.root.exists():
            shutil.rmtree(self.root)
        self.root.mkdir(parents=True)

    def tearDown(self) -> None:
        if self.root.exists():
            shutil.rmtree(self.root)

    def test_consumes_vkp_crispasr_receipt_without_running_second_asr(self) -> None:
        audio = self._bytes("speech.wav", b"wav")
        transcript = self._json("transcript.json", {"text": "verified"})
        words = self._json(
            "words.json", [{"word": "verified", "start": 0.0, "end": 0.5}]
        )
        receipt = {
            "schema": SPEECH_EXECUTION_RECEIPT_SCHEMA,
            "owner": "video-knowledge-pipeline",
            "status": "completed",
            "engine": {
                "name": "CrispASR",
                "version": CRISPASR_VERSION,
                "source_commit": CRISPASR_COMMIT,
                "binary_sha256": "a" * 64,
                "model_sha256": "b" * 64,
                "requested_backend": "cuda",
                "used_backend": "cpu",
                "device": "CPU",
                "cpu_retry_performed": True,
            },
            "input_audio": artifact_reference(audio, "speech input"),
            "outputs": {
                "transcript": artifact_reference(transcript, "transcript"),
                "word_timestamps": artifact_reference(words, "word timestamps"),
            },
            "chunking": {
                "seconds": 30,
                "overlap_seconds": 2,
                "lcs_deduplication": True,
                "punctuation_split": True,
            },
            "attempts": [
                {"backend": "cuda", "status": "failed", "duration_ms": 120},
                {"backend": "cpu", "status": "completed", "duration_ms": 820},
            ],
            "arbitration": {
                "performed_by": "video-knowledge-pipeline",
                "status": "accepted",
                "transcript_is_authoritative": True,
                "overwrites_source_evidence": False,
            },
            "execution_boundary": {
                "video_creation_asr_executed": False,
                "automatic_local_cloud_fallback": False,
                "second_asr_truth_created": False,
            },
        }
        receipt["receipt_sha256"] = payload_sha256(receipt, "receipt_sha256")
        receipt_path = self._json("speech-receipt.json", receipt)

        validate_speech_execution_receipt(receipt)
        contract = build_speech_input_contract(receipt_path)

        self.assertFalse(contract["consumption_policy"]["video_creation_may_run_asr"])
        self.assertEqual(contract["engine"]["source_commit"], CRISPASR_COMMIT)
        invalid_attempts = json.loads(json.dumps(receipt))
        invalid_attempts["attempts"][-1]["status"] = "failed"
        invalid_attempts["receipt_sha256"] = payload_sha256(
            invalid_attempts, "receipt_sha256"
        )
        with self.assertRaisesRegex(ContractError, "completed final attempt"):
            validate_speech_execution_receipt(invalid_attempts)
        transcript.write_text('{"text":"changed"}', encoding="utf-8")
        with self.assertRaisesRegex(ContractError, "changed after"):
            validate_speech_execution_receipt(receipt)

    def test_llama_plan_consumes_existing_ffmpeg_frames_and_stays_loopback(
        self,
    ) -> None:
        source = self._bytes("source.mp4", b"video")
        frame_a = self._bytes("frame-a.jpg", b"jpeg-a")
        frame_b = self._bytes("frame-b.jpg", b"jpeg-b")
        ffmpeg_receipt = {
            "schema": FFMPEG_EXECUTION_RECEIPT_SCHEMA,
            "outlet_id": FFMPEG_OUTLET_ID,
            "status": "completed",
            "operation": "frame_extract",
            "execution_profile": {
                "selected_by": FFMPEG_OUTLET_ID,
                "requested_backend": "auto",
                "selected_backend": "nvenc",
                "hardware_accelerated": True,
                "fallback_used": False,
                "fallback_reason": "",
                "automatic_local_cloud_fallback": False,
            },
            "command": ["ffmpeg", "-i", str(source), "frame-%04d.jpg"],
            "inputs": [
                {
                    "role": "source_media",
                    "artifact": artifact_reference(source, "source"),
                }
            ],
            "outputs": [
                {
                    "role": "frame",
                    "media_time_s": 1.0,
                    "artifact": artifact_reference(frame_a, "frame a"),
                },
                {
                    "role": "frame",
                    "media_time_s": 2.5,
                    "artifact": artifact_reference(frame_b, "frame b"),
                },
            ],
            "execution_boundary": {
                "orchestration_owner": FFMPEG_OUTLET_ID,
                "second_ffmpeg_orchestrator_created": False,
                "source_media_modified": False,
                "automatic_publish": False,
            },
        }
        ffmpeg_receipt["receipt_sha256"] = payload_sha256(
            ffmpeg_receipt, "receipt_sha256"
        )
        ffmpeg_path = self._json("ffmpeg-receipt.json", ffmpeg_receipt)
        manifest = build_frame_manifest(source, ffmpeg_path)
        manifest_path = self.root / "frame-manifest.json"
        write_local_multimodal_contract(manifest, manifest_path)
        prompt = self._bytes("prompt.txt", b"Describe the visible action.")
        model = self._bytes("vision.gguf", b"gguf")

        plan = build_local_multimodal_plan(
            manifest_path,
            prompt,
            model,
            prompt_version="material-analysis-v1",
        )

        self.assertEqual(plan["runtime"]["source_commit"], LLAMA_CPP_COMMIT)
        self.assertEqual(
            plan["request_mapping"]["content_types"], ["text", "image_url"]
        )
        self.assertFalse(plan["request_mapping"]["custom_video_message_type"])
        self.assertFalse(plan["execution_boundary"]["automatic_online_fallback"])
        with self.assertRaisesRegex(ContractError, "loopback"):
            build_local_multimodal_plan(
                manifest_path,
                prompt,
                model,
                prompt_version="material-analysis-v1",
                endpoint="https://example.com/v1/chat/completions",
            )

        raw = self._json("raw-output.json", {"content": "candidate"})
        candidates = self._json(
            "material-candidates.json",
            {
                "schema": "video_creation_pipeline.material_candidates.v1",
                "shots": [
                    {
                        "shot_id": "shot-001",
                        "candidates": [
                            {
                                "asset_id": "asset-a",
                                "path": str(source),
                                "confidence": 0.9,
                                "score_components": {"vision": 0.9},
                            }
                        ],
                    }
                ],
            },
        )
        receipt = {
            "schema": LOCAL_MULTIMODAL_RECEIPT_SCHEMA,
            "plan_sha256": plan["plan_sha256"],
            "runtime_commit": LLAMA_CPP_COMMIT,
            "model_sha256": plan["runtime"]["model"]["sha256"],
            "endpoint": plan["runtime"]["endpoint"],
            "status": "completed",
            "raw_output": artifact_reference(raw, "raw output"),
            "material_candidates": artifact_reference(candidates, "candidates"),
            "acceptance_status": "candidate_only",
            "external_network_used": False,
            "fallback_used": False,
            "custom_video_message_type_used": False,
        }
        validation = validate_local_multimodal_receipt(plan, receipt)
        self.assertEqual(validation["candidate_count"], 1)
        receipt["fallback_used"] = True
        with self.assertRaisesRegex(ContractError, "execution boundary"):
            validate_local_multimodal_receipt(plan, receipt)

    def test_sqlite_vec_benchmark_gates_candidate_generation(self) -> None:
        gold = self._json(
            "goldset.json",
            {
                "schema": MATERIAL_INDEX_GOLDSET_SCHEMA,
                "queries": [
                    {
                        "query_id": "q1",
                        "relevant_asset_ids": ["asset-a"],
                        "required_tags": ["product"],
                        "adjacent_asset_ids": ["asset-b"],
                    },
                    {
                        "query_id": "q2",
                        "relevant_asset_ids": ["asset-c"],
                        "required_tags": ["demo"],
                        "adjacent_asset_ids": [],
                    },
                ],
            },
        )
        results_payload = self._results_payload()
        results = self._json("query-results.json", results_payload)

        benchmark = build_material_index_benchmark(gold, results)

        self.assertEqual(benchmark["schema"], MATERIAL_INDEX_BENCHMARK_SCHEMA)
        self.assertTrue(benchmark["gate"]["candidate_source_enabled"])
        self.assertEqual(benchmark["provider"]["source_commit"], SQLITE_VEC_COMMIT)
        benchmark_path = self.root / "benchmark.json"
        write_material_index_contract(benchmark, benchmark_path)
        mapping = self._json(
            "shot-queries.json",
            {
                "schema": MATERIAL_INDEX_SHOT_QUERIES_SCHEMA,
                "shots": [
                    {
                        "shot_id": "shot-001",
                        "query_id": "q1",
                        "max_candidates": 2,
                    }
                ],
            },
        )
        candidates = build_material_candidates_from_index(benchmark_path, mapping)
        self.assertEqual(candidates["shots"][0]["candidates"][0]["asset_id"], "asset-a")
        self.assertFalse(candidates["candidate_source"]["mutates_material_metadata"])

        failed_payload = self._results_payload()
        failed_payload["queries"][0]["hits"] = [self._hit("unrelated", 0.5, ["other"])]
        failed_results = self._json("failed-results.json", failed_payload)
        failed_benchmark = build_material_index_benchmark(gold, failed_results)
        self.assertFalse(failed_benchmark["gate"]["candidate_source_enabled"])
        failed_path = self.root / "failed-benchmark.json"
        write_material_index_contract(failed_benchmark, failed_path)
        with self.assertRaisesRegex(ContractError, "has not enabled"):
            build_material_candidates_from_index(failed_path, mapping)

    def _results_payload(self) -> dict:
        lifecycle = {
            name: {
                "passed": True,
                "duration_ms": index * 10,
                "evidence": f"{name}-fixture",
            }
            for index, name in enumerate(
                (
                    "incremental_insert",
                    "full_rebuild",
                    "deletion_cleanup",
                    "model_version_rebuild",
                ),
                1,
            )
        }
        return {
            "schema": MATERIAL_INDEX_RESULTS_SCHEMA,
            "provider": {
                "name": "sqlite-vec",
                "version": SQLITE_VEC_VERSION,
                "source_commit": SQLITE_VEC_COMMIT,
                "embedding_model": "fixture-embedding",
                "embedding_model_version": "1",
            },
            "queries": [
                {
                    "query_id": "q1",
                    "hits": [
                        self._hit("asset-a", 0.95, ["product"]),
                        self._hit("asset-b", 0.9, ["product"]),
                    ],
                },
                {
                    "query_id": "q2",
                    "hits": [self._hit("asset-c", 0.93, ["demo"])],
                },
            ],
            "lifecycle": lifecycle,
        }

    @staticmethod
    def _hit(asset_id: str, score: float, tags: list[str]) -> dict:
        return {
            "asset_id": asset_id,
            "path": f"raw/{asset_id}.mp4",
            "score": score,
            "tags": tags,
            "source_media_id": asset_id,
            "room_id": "room-1",
            "composition_id": "close",
        }

    def _json(self, name: str, payload: object) -> Path:
        path = self.root / name
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    def _bytes(self, name: str, payload: bytes) -> Path:
        path = self.root / name
        path.write_bytes(payload)
        return path


if __name__ == "__main__":
    unittest.main()
