from __future__ import annotations

# ruff: noqa: E402

import shutil
import sys
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from video_creation_pipeline.contract_io import (
    file_sha256,
    payload_sha256,
    read_json_object,
)
from video_creation_pipeline.errors import ContractError
from video_creation_pipeline.music_acestep import (
    REVIEWED_SOURCE_SHA256,
    REQUIRED_CHECKPOINT_COMPONENTS,
    _disable_acestep_model_mutation,
    build_acestep_checkpoint_manifest,
    build_acestep_generation_plan,
    materialize_acestep_music_profiles,
    run_acestep_generation,
    validate_acestep_checkpoint_manifest,
    validate_acestep_generation_plan,
    validate_music_generation_receipt,
    write_acestep_contract,
)
from video_creation_pipeline.music_pipeline import (
    build_music_candidate_set,
    build_music_edit_plan,
    build_music_intent_curve,
    build_music_retrieval_results,
    write_music_contract,
)


class FakeAceStepBackend:
    def generate(
        self, request: dict[str, object], output_dir: Path
    ) -> list[dict[str, object]]:
        generation = request["generation"]
        assert isinstance(generation, dict)
        outputs = []
        for seed in reversed(generation["seeds"]):
            path = output_dir / f"candidate-{seed}.wav"
            _write_wav(path, float(generation["duration_s"]))
            outputs.append(
                {
                    "path": str(path),
                    "seed": seed,
                    "key": generation["keyscale"],
                    "bpm": float(generation["bpm"]),
                }
            )
        return outputs


class FailingAceStepBackend:
    def generate(
        self, request: dict[str, object], output_dir: Path
    ) -> list[dict[str, object]]:
        raise RuntimeError("fixture generation failed")


class MusicAceStepTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = ROOT / ".test-runtime" / self._testMethodName
        if self.root.exists():
            shutil.rmtree(self.root)
        self.root.mkdir(parents=True)
        self.source_root = self.root / "ACE-Step-1.5"
        source_files = {
            relative: f"fixture:{relative}\n".encode()
            for relative in REVIEWED_SOURCE_SHA256
        }
        for relative, content in source_files.items():
            path = self.source_root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        hashes = {
            relative: file_sha256(self.source_root / relative)
            for relative in source_files
        }
        self.source_patch = patch.dict(REVIEWED_SOURCE_SHA256, hashes, clear=True)
        self.source_patch.start()

        self.checkpoint_root = self.root / "checkpoints"
        for component in REQUIRED_CHECKPOINT_COMPONENTS:
            model = self.checkpoint_root / component / "model.safetensors"
            model.parent.mkdir(parents=True, exist_ok=True)
            model.write_bytes(f"weights:{component}".encode())
        self.license_snapshot = self.root / "ACE-Step-LICENSE.txt"
        self.license_snapshot.write_text("MIT fixture", encoding="utf-8")
        self.manifest_path = self.root / "checkpoint-manifest.json"
        manifest = build_acestep_checkpoint_manifest(
            self.checkpoint_root,
            self.license_snapshot,
            commercial_use_allowed=True,
        )
        write_acestep_contract(manifest, self.manifest_path)

        source = self.root / "timeline.json"
        source.write_text('{"timeline":"fixture"}', encoding="utf-8")
        self.intent_path = self.root / "music-intent.json"
        intent = build_music_intent_curve(
            source,
            [
                self._intent_segment("calm", 0.0, 10.0, 0.25, 0.15),
                self._intent_segment("tense", 10.0, 22.0, 0.82, 0.9),
            ],
            created_by="test fixture",
        )
        write_music_contract(intent, self.intent_path)
        self.generated_root = self.root / "generated"
        self.plan_path = self.root / "acestep-plan.json"
        plan = build_acestep_generation_plan(
            self.intent_path,
            self.manifest_path,
            self.source_root,
            self.generated_root,
            purpose="commercial",
            device="cpu",
            candidates_per_segment=2,
            random_seed=42,
            segment_overrides={"calm": {"bpm": 72, "keyscale": "C Major"}},
        )
        write_acestep_contract(plan, self.plan_path)

    def tearDown(self) -> None:
        self.source_patch.stop()
        if self.root.exists():
            shutil.rmtree(self.root)

    def test_generation_profiles_and_existing_auto_selection_chain(self) -> None:
        plan = read_json_object(self.plan_path, "ACE-Step plan")
        self.assertFalse(plan["generation_defaults"]["thinking"])
        self.assertEqual(plan["requests"][0]["generation"]["bpm"], 72)
        self.assertTrue(plan["requests"][0]["generation"]["instrumental"])
        self.assertEqual(plan["requests"][0]["generation"]["batch_size"], 2)

        receipt = run_acestep_generation(self.plan_path, backend=FakeAceStepBackend())
        self.assertEqual(receipt["status"], "completed")
        self.assertTrue(receipt["readiness"]["eligible_for_clap_ranking"])
        receipt_path = self.root / "generation-receipt.json"
        write_acestep_contract(receipt, receipt_path)
        profiles = materialize_acestep_music_profiles(
            receipt_path, self.root / "profiles"
        )
        self.assertEqual(len(profiles), 4)

        profile_payloads = [
            read_json_object(path, "generated profile") for path in profiles
        ]
        matches = []
        by_segment: dict[str, list[dict]] = {"calm": [], "tense": []}
        for attempt in receipt["attempts"]:
            by_segment[attempt["segment_id"]] = attempt["outputs"]
        for segment_id, outputs in by_segment.items():
            for index, output in enumerate(outputs):
                matches.append(
                    {
                        "segment_id": segment_id,
                        "asset_id": output["asset_id"],
                        "window_id": f"{output['asset_id']}-full",
                        "semantic_similarity": 0.98 if index == 0 else 0.35,
                        "continuity_diversity": 0.7,
                        "query_text": segment_id,
                    }
                )
        retrieval_path = self.root / "retrieval.json"
        retrieval = build_music_retrieval_results(
            self.intent_path,
            matches,
            provider={
                "id": "fixture-clap",
                "model": "fixture",
                "version": "1",
                "source_commit": "fixture",
                "execution_location": "local",
                "model_license": "fixture",
            },
        )
        write_music_contract(retrieval, retrieval_path)
        candidates = build_music_candidate_set(
            self.intent_path,
            retrieval_path,
            profiles,
            purpose="commercial",
        )
        candidate_path = self.root / "candidates.json"
        write_music_contract(candidates, candidate_path)
        edit_plan = build_music_edit_plan(candidate_path)
        self.assertTrue(edit_plan["readiness"]["render_eligible"])
        self.assertEqual(edit_plan["readiness"]["automatic_selection_count"], 2)
        self.assertTrue(
            all(
                profile["license"]["commercial_use_allowed"]
                for profile in profile_payloads
            )
        )

    def test_reviewed_source_drift_invalidates_plan(self) -> None:
        (self.source_root / "acestep" / "inference.py").write_bytes(b"changed")
        with self.assertRaisesRegex(ContractError, "hash mismatch"):
            validate_acestep_generation_plan(
                read_json_object(self.plan_path, "ACE-Step plan")
            )

    def test_checkpoint_drift_invalidates_manifest_and_plan(self) -> None:
        model = self.checkpoint_root / "vae" / "model.safetensors"
        model.write_bytes(b"changed-weights")
        manifest = read_json_object(self.manifest_path, "checkpoint manifest")
        with self.assertRaisesRegex(ContractError, "checkpoints changed"):
            validate_acestep_checkpoint_manifest(manifest)
        with self.assertRaisesRegex(ContractError, "checkpoints changed"):
            validate_acestep_generation_plan(
                read_json_object(self.plan_path, "ACE-Step plan")
            )

    def test_failed_generation_is_visible_and_cannot_be_profiled(self) -> None:
        receipt = run_acestep_generation(
            self.plan_path, backend=FailingAceStepBackend()
        )
        self.assertEqual(receipt["status"], "failed")
        self.assertFalse(receipt["readiness"]["eligible_for_clap_ranking"])
        self.assertEqual(receipt["attempts"][0]["status"], "failed")
        receipt_path = self.root / "failed-receipt.json"
        write_acestep_contract(receipt, receipt_path)
        with self.assertRaisesRegex(ContractError, "failed music generation"):
            materialize_acestep_music_profiles(
                receipt_path, self.root / "failed-profiles"
            )

    def test_failed_receipt_requires_complete_blocking_readiness(self) -> None:
        receipt = run_acestep_generation(
            self.plan_path, backend=FailingAceStepBackend()
        )
        del receipt["readiness"]["eligible_for_clap_ranking"]
        receipt["receipt_sha256"] = payload_sha256(receipt, "receipt_sha256")
        with self.assertRaisesRegex(ContractError, "failed music generation"):
            validate_music_generation_receipt(receipt)

        receipt = run_acestep_generation(
            self.plan_path, backend=FailingAceStepBackend()
        )
        receipt["error"]["stage"] = "another-segment"
        receipt["receipt_sha256"] = payload_sha256(receipt, "receipt_sha256")
        with self.assertRaisesRegex(ContractError, "failed music generation"):
            validate_music_generation_receipt(receipt)

    def test_generated_audio_drift_invalidates_receipt(self) -> None:
        receipt = run_acestep_generation(self.plan_path, backend=FakeAceStepBackend())
        output = Path(receipt["attempts"][0]["outputs"][0]["audio"]["path"])
        output.write_bytes(b"changed")
        with self.assertRaisesRegex(ContractError, "changed after"):
            validate_music_generation_receipt(receipt)

    def test_upstream_download_and_code_sync_are_disabled_during_init(self) -> None:
        def download(*_args, **_kwargs):
            return True, "downloaded"

        def sync(*_args, **_kwargs):
            return ["changed.py"]

        handler = SimpleNamespace(
            _ensure_models_present=download,
            _sync_model_code_if_needed=sync,
        )
        with _disable_acestep_model_mutation(handler):
            self.assertIsNone(handler._ensure_models_present())
            self.assertIsNone(handler._sync_model_code_if_needed())
        self.assertIs(handler._ensure_models_present, download)
        self.assertIs(handler._sync_model_code_if_needed, sync)

    def test_output_root_cannot_overlap_reviewed_source(self) -> None:
        with self.assertRaisesRegex(ContractError, "must not overlap"):
            build_acestep_generation_plan(
                self.intent_path,
                self.manifest_path,
                self.source_root,
                self.source_root / "outputs",
                device="cpu",
            )
        with self.assertRaisesRegex(ContractError, "time signature"):
            build_acestep_generation_plan(
                self.intent_path,
                self.manifest_path,
                self.source_root,
                self.generated_root,
                device="cpu",
                segment_overrides={"calm": {"timesignature": "5"}},
            )

    def _intent_segment(
        self,
        segment_id: str,
        start_s: float,
        end_s: float,
        arousal: float,
        tension: float,
    ) -> dict:
        return {
            "segment_id": segment_id,
            "start_s": start_s,
            "end_s": end_s,
            "relationship_mode": "match",
            "text_query": f"{segment_id} instrumental background",
            "text_query_en": f"{segment_id} instrumental background",
            "target": {
                "valence": 0.55,
                "arousal": arousal,
                "tension": tension,
                "intimacy_scale": 0.4,
                "warmth": 0.5,
                "movement": arousal,
                "engagement": 0.3,
            },
            "vocal_policy": "instrumental_required",
            "narration_density": 0.8,
            "prohibited_tags": ["vocal"],
            "evidence_ids": [f"timeline-{segment_id}"],
        }


def _write_wav(path: Path, duration_s: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    sample_rate = 8000
    frame_count = int(round(sample_rate * duration_s))
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(b"\x00\x00" * frame_count)


if __name__ == "__main__":
    unittest.main()
