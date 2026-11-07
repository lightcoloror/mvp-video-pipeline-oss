from __future__ import annotations

# ruff: noqa: E402

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
    atomic_write_json,
    file_sha256,
    read_json_object,
)
from video_creation_pipeline.errors import ContractError
from video_creation_pipeline.music_benchmark import (
    MUSIC_RETRIEVAL_GOLD_SET_SCHEMA,
    build_music_retrieval_benchmark,
    write_music_retrieval_benchmark,
)
from video_creation_pipeline.music_msclap import (
    MUSIC_ANALYSIS_AUDIO_MAP_SCHEMA,
    REVIEWED_SOURCE_SHA256,
    build_msclap_retrieval_plan,
    run_msclap_retrieval,
    validate_msclap_retrieval_plan,
    write_msclap_contract,
)
from video_creation_pipeline.music_pipeline import (
    build_music_asset_profile,
    build_music_candidate_set,
    build_music_edit_plan,
    build_music_intent_curve,
    write_music_contract,
)


class FakeMsClapBackend:
    def similarities(
        self, audio_paths: list[Path], queries: list[str]
    ) -> list[list[float]]:
        if len(audio_paths) != 2 or len(queries) != 2:
            raise AssertionError("fixture expected two clips and two queries")
        return [[0.98, 0.12], [0.10, 0.97]]


class MusicMsClapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = ROOT / ".test-runtime" / self._testMethodName
        if self.root.exists():
            shutil.rmtree(self.root)
        self.root.mkdir(parents=True)
        self.source_root = self.root / "Microsoft-CLAP"
        self._source_files = {
            "msclap/CLAPWrapper.py": b"reviewed-wrapper-fixture",
            "msclap/__init__.py": b"reviewed-init-fixture",
            "msclap/configs/config_2023.yml": b"duration: 7\n",
        }
        for relative, content in self._source_files.items():
            path = self.source_root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        self.reviewed_hashes = {
            relative: file_sha256(self.source_root / relative)
            for relative in self._source_files
        }
        self.model = self.root / "CLAP_weights_2023.pth"
        self.model.write_bytes(b"explicit-offline-model-fixture")
        self.license_snapshot = self.root / "model-license.txt"
        self.license_snapshot.write_text(
            "fixture model is cleared for commercial tests", encoding="utf-8"
        )
        self.hf_cache = self.root / "hf-cache"
        self.hf_cache.mkdir()

        source = self._json("packaging.json", {"scenes": [{"start": 0, "end": 14}]})
        self.intent_path = self.root / "intent.json"
        write_music_contract(
            build_music_intent_curve(
                source,
                [
                    self._intent_segment(
                        "scene-calm", 0, 7, "calm minimal instrumental", 0.25
                    ),
                    self._intent_segment(
                        "scene-tense", 7, 14, "tense urgent instrumental", 0.85
                    ),
                ],
            ),
            self.intent_path,
        )
        self.calm_profile = self._profile("calm", arousal=0.25, tension=0.15)
        self.tense_profile = self._profile("tense", arousal=0.85, tension=0.9)
        self.profiles = [self.calm_profile, self.tense_profile]
        self.audio_map = self._json(
            "audio-map.json",
            {
                "schema": MUSIC_ANALYSIS_AUDIO_MAP_SCHEMA,
                "windows": [
                    self._audio_map_row("calm"),
                    self._audio_map_row("tense"),
                ],
            },
        )

    def tearDown(self) -> None:
        if self.root.exists():
            shutil.rmtree(self.root)

    def test_pinned_adapter_benchmark_and_candidate_gate(self) -> None:
        with patch.dict(REVIEWED_SOURCE_SHA256, self.reviewed_hashes, clear=True):
            plan_path, retrieval_path = self._run_fixture_retrieval(
                prefix="gold", random_seed=20260803
            )
            self.assertTrue(plan_path.is_file())
            gold = self._gold_set()
            benchmark = build_music_retrieval_benchmark(
                retrieval_path,
                gold,
                min_query_count=2,
                min_recall_at_5=1.0,
                min_recall_at_10=1.0,
                min_mrr=1.0,
            )
            self.assertTrue(benchmark["readiness"]["passed"])
            self.assertEqual(benchmark["metrics"]["macro_recall_at_5"], 1.0)
            benchmark_path = self.root / "benchmark.json"
            write_music_retrieval_benchmark(benchmark, benchmark_path)

            business_plan_path, business_retrieval_path = self._run_fixture_retrieval(
                prefix="business", random_seed=20260804
            )
            self.assertNotEqual(
                read_json_object(plan_path, "gold plan")["plan_sha256"],
                read_json_object(business_plan_path, "business plan")["plan_sha256"],
            )

            with self.assertRaisesRegex(ContractError, "requires a passing"):
                build_music_candidate_set(
                    self.intent_path,
                    business_retrieval_path,
                    self.profiles,
                    purpose="commercial",
                )
            candidates = build_music_candidate_set(
                self.intent_path,
                business_retrieval_path,
                self.profiles,
                purpose="commercial",
                retrieval_benchmark_path=benchmark_path,
            )
            candidate_path = self.root / "candidates.json"
            write_music_contract(candidates, candidate_path)
            edit_plan = build_music_edit_plan(candidate_path)
            self.assertTrue(edit_plan["readiness"]["render_eligible"])
            self.assertEqual(edit_plan["readiness"]["automatic_selection_count"], 2)

    def test_failed_benchmark_blocks_candidate_generation(self) -> None:
        with patch.dict(REVIEWED_SOURCE_SHA256, self.reviewed_hashes, clear=True):
            _, retrieval_path = self._run_fixture_retrieval()
            benchmark = build_music_retrieval_benchmark(
                retrieval_path,
                self._gold_set(),
                min_query_count=3,
                min_recall_at_5=1.0,
                min_recall_at_10=1.0,
                min_mrr=1.0,
            )
            self.assertFalse(benchmark["readiness"]["passed"])
            benchmark_path = self.root / "failed-benchmark.json"
            write_music_retrieval_benchmark(benchmark, benchmark_path)
            with self.assertRaisesRegex(ContractError, "did not pass"):
                build_music_candidate_set(
                    self.intent_path,
                    retrieval_path,
                    self.profiles,
                    retrieval_benchmark_path=benchmark_path,
                )

    def test_reviewed_source_drift_invalidates_plan(self) -> None:
        with patch.dict(REVIEWED_SOURCE_SHA256, self.reviewed_hashes, clear=True):
            plan = self._build_plan()
            (self.source_root / "msclap" / "CLAPWrapper.py").write_bytes(b"changed")
            with self.assertRaisesRegex(ContractError, "hash mismatch"):
                validate_msclap_retrieval_plan(plan)

    def _run_fixture_retrieval(
        self,
        *,
        prefix: str = "run",
        random_seed: int = 20260803,
    ) -> tuple[Path, Path]:
        plan = self._build_plan(random_seed=random_seed)
        plan_path = self.root / f"{prefix}-msclap-plan.json"
        write_msclap_contract(plan, plan_path)
        retrieval = run_msclap_retrieval(
            plan_path,
            backend=FakeMsClapBackend(),
        )
        retrieval_path = self.root / f"{prefix}-retrieval.json"
        write_music_contract(retrieval, retrieval_path)
        return plan_path, retrieval_path

    def _build_plan(self, *, random_seed: int = 20260803) -> dict:
        return build_msclap_retrieval_plan(
            self.intent_path,
            self.profiles,
            self.audio_map,
            self.source_root,
            self.model,
            self.license_snapshot,
            model_license="fixture-commercial-license",
            commercial_use_allowed=True,
            purpose="commercial",
            hf_cache_dir=self.hf_cache,
            random_seed=random_seed,
        )

    def _profile(self, asset_id: str, *, arousal: float, tension: float) -> Path:
        audio = self.root / f"{asset_id}.wav"
        audio.write_bytes(f"audio-{asset_id}".encode())
        profile = build_music_asset_profile(
            audio,
            asset_id=asset_id,
            duration_s=7.0,
            windows=[
                {
                    "window_id": f"{asset_id}-main",
                    "start_s": 0.0,
                    "end_s": 7.0,
                    "structure_label": "main",
                    "structure_confidence": 0.9,
                    "descriptors": self._descriptors(arousal=arousal, tension=tension),
                    "vocal_probability": 0.0,
                    "tags": ["instrumental", asset_id],
                    "loopable": True,
                }
            ],
            license_info={
                "status": "allowed",
                "commercial_use_allowed": True,
                "license_uri": f"local://fixture/{asset_id}",
                "license_snapshot": "owned test fixture",
                "attribution": "",
            },
            analyzer={
                "id": "fixture",
                "model": "fixture",
                "version": "1",
                "source_commit": "fixture",
                "execution_location": "local",
                "model_license": "fixture",
                "automatic_fallback_used": False,
            },
        )
        path = self.root / f"{asset_id}-profile.json"
        write_music_contract(profile, path)
        return path

    def _intent_segment(
        self, segment_id: str, start: float, end: float, query: str, arousal: float
    ) -> dict:
        return {
            "segment_id": segment_id,
            "start_s": start,
            "end_s": end,
            "relationship_mode": "match",
            "text_query": query,
            "text_query_en": query,
            "target": self._descriptors(
                arousal=arousal, tension=0.15 if arousal < 0.5 else 0.9
            ),
            "vocal_policy": "instrumental_required",
            "narration_density": 0.6,
            "prohibited_tags": ["vocal"],
            "evidence_ids": [segment_id],
        }

    def _audio_map_row(self, asset_id: str) -> dict:
        return {
            "asset_id": asset_id,
            "window_id": f"{asset_id}-main",
            "clips": [
                {
                    "source_start_s": 0.0,
                    "source_end_s": 7.0,
                    "path": str(self.root / f"{asset_id}.wav"),
                }
            ],
        }

    def _gold_set(self) -> Path:
        return self._json(
            "gold.json",
            {
                "schema": MUSIC_RETRIEVAL_GOLD_SET_SCHEMA,
                "queries": [
                    {
                        "segment_id": "scene-calm",
                        "relevant": [{"asset_id": "calm", "window_id": "calm-main"}],
                    },
                    {
                        "segment_id": "scene-tense",
                        "relevant": [{"asset_id": "tense", "window_id": "tense-main"}],
                    },
                ],
            },
        )

    def _descriptors(self, *, arousal: float, tension: float) -> dict[str, float]:
        return {
            "valence": 0.5,
            "arousal": arousal,
            "tension": tension,
            "intimacy_scale": 0.4,
            "warmth": 0.5,
            "movement": arousal,
            "engagement": arousal,
        }

    def _json(self, name: str, payload: dict) -> Path:
        path = self.root / name
        atomic_write_json(payload, path)
        return path


if __name__ == "__main__":
    unittest.main()
