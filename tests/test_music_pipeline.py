from __future__ import annotations

import json
import shutil
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from video_creation_pipeline.contract_io import (
    atomic_write_json,
    canonical_json_sha256,
)
from video_creation_pipeline.creative_gates import (
    build_approval_record,
    build_release_gate,
    validate_release_gate,
    write_creative_contract,
)
from video_creation_pipeline.errors import ContractError
from video_creation_pipeline.music_cli import main as music_cli_main
from video_creation_pipeline.music_pipeline import (
    MUSIC_CANDIDATE_SET_SCHEMA,
    MUSIC_EDIT_PLAN_SCHEMA,
    MUSIC_EDIT_PLAN_V1_SCHEMA,
    build_ducked_music_stem_command,
    build_music_asset_profile,
    build_music_candidate_set,
    build_music_edit_plan,
    build_music_intent_curve,
    build_music_mix_command,
    build_music_retrieval_results,
    validate_music_candidate_set,
    validate_music_edit_plan,
    write_music_contract,
)


class MusicPipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = ROOT / ".test-runtime" / self._testMethodName
        if self.root.exists():
            shutil.rmtree(self.root)
        self.root.mkdir(parents=True)
        self.source = self._json(
            "packaging-plan.json",
            {"scenes": [{"start": 0.0, "end": 12.0, "mood": "clear"}]},
        )
        self.intent_path = self.root / "music-intent.json"
        write_music_contract(
            build_music_intent_curve(
                self.source,
                [
                    {
                        "segment_id": "scene-1",
                        "start_s": 0.0,
                        "end_s": 12.0,
                        "relationship_mode": "neutral_support",
                        "text_query": "极简钢琴，无人声，清晰克制，低能量",
                        "text_query_en": "minimal instrumental piano, restrained",
                        "target": self._descriptors(
                            valence=0.65,
                            arousal=0.30,
                            warmth=0.60,
                            movement=0.25,
                            engagement=0.20,
                        ),
                        "vocal_policy": "instrumental_required",
                        "narration_density": 0.8,
                        "prohibited_tags": ["vocal"],
                        "evidence_ids": ["scene-1", "summary-1"],
                    }
                ],
            ),
            self.intent_path,
        )
        self.calm_profile_path = self._profile(
            "calm",
            descriptors=self._descriptors(
                valence=0.65,
                arousal=0.30,
                warmth=0.60,
                movement=0.25,
                engagement=0.20,
            ),
            tags=["piano", "instrumental", "minimal"],
            vocal_probability=0.0,
            commercial_use_allowed=True,
        )
        self.vocal_profile_path = self._profile(
            "vocal",
            descriptors=self._descriptors(
                valence=0.7,
                arousal=0.4,
                warmth=0.7,
                movement=0.4,
                engagement=0.8,
            ),
            tags=["vocal", "pop"],
            vocal_probability=0.95,
            commercial_use_allowed=True,
        )
        self.retrieval_path = self.root / "retrieval.json"
        write_music_contract(
            build_music_retrieval_results(
                self.intent_path,
                [
                    {
                        "segment_id": "scene-1",
                        "asset_id": "calm",
                        "window_id": "calm-main",
                        "semantic_similarity": 0.95,
                        "continuity_diversity": 0.7,
                        "query_text": "minimal instrumental piano",
                    },
                    {
                        "segment_id": "scene-1",
                        "asset_id": "vocal",
                        "window_id": "vocal-main",
                        "semantic_similarity": 0.98,
                        "continuity_diversity": 0.6,
                        "query_text": "minimal instrumental piano",
                    },
                ],
                provider={
                    "id": "microsoft-clap",
                    "model": "clap-2023",
                    "version": "2023",
                    "source_commit": "e8a6467b87cd85716e20c6a008126150d9740be0",
                    "execution_location": "local",
                    "model_license": "MIT",
                },
            ),
            self.retrieval_path,
        )

    def tearDown(self) -> None:
        if self.root.exists():
            shutil.rmtree(self.root)

    def test_filters_vocals_scores_candidates_and_auto_selects(self) -> None:
        candidates = build_music_candidate_set(
            self.intent_path,
            self.retrieval_path,
            [self.calm_profile_path, self.vocal_profile_path],
            purpose="commercial",
        )
        self.assertEqual(candidates["schema"], MUSIC_CANDIDATE_SET_SCHEMA)
        self.assertEqual(
            [row["asset_id"] for row in candidates["segments"][0]["candidates"]],
            ["calm"],
        )
        self.assertEqual(
            candidates["rejected"][0]["reasons"],
            ["vocal_content_forbidden", "prohibited_tag_present"],
        )
        candidate_path = self.root / "candidates.json"
        write_music_contract(candidates, candidate_path)
        plan = build_music_edit_plan(candidate_path)
        self.assertEqual(plan["schema"], MUSIC_EDIT_PLAN_SCHEMA)
        self.assertTrue(plan["readiness"]["render_eligible"])
        self.assertFalse(plan["readiness"]["manual_review_required"])
        self.assertEqual(plan["selections"][0]["status"], "auto_selected")
        self.assertEqual(
            plan["selections"][0]["music_family"]["family_id"], "clear_explainer"
        )
        self.assertEqual(plan["selections"][0]["gain_db"], -3.0)
        self.assertEqual(plan["selections"][0]["ducking"]["threshold"], 0.08)
        self.assertEqual(plan["selections"][0]["ducking"]["ratio"], 3.0)
        self.assertEqual(plan["ffmpeg_handoff"]["operation"], "music_mix")

    def test_explicit_no_music_and_protected_sound_windows_reach_mix_commands(self):
        original = json.loads(self.intent_path.read_text('utf-8'))
        retrieval = json.loads(self.retrieval_path.read_text('utf-8'))
        for mode in ('none','music'):
            segment = dict(original['segments'][0],music_mode=mode,music_mode_reason='preserve a meaningful response')
            if mode == 'music':
                segment['no_music_windows'] = [dict(start_s=4,end_s=5,reason='quiet response')]
            intent = atomic_write_json(build_music_intent_curve(self.source,[segment]),self.root/(mode+'-intent.json'))
            results = atomic_write_json(build_music_retrieval_results(intent,retrieval['matches'],provider=retrieval['provider']),self.root/(mode+'-retrieval.json'))
            candidates = atomic_write_json(build_music_candidate_set(intent,results,[self.calm_profile_path,self.vocal_profile_path]),self.root/(mode+'-candidates.json'))
            plan = build_music_edit_plan(candidates)
            validate_music_edit_plan(plan)
            selection = plan['selections'][0]
            command = build_music_mix_command(self.root/'program.mp4',self.root/'music.wav',self.root/'out.mp4',selection)
            if mode == 'none':
                self.assertEqual(selection['status'],'intentional_none')
                self.assertEqual(command.count('-i'),1)
                self.assertNotIn('-filter_complex',command)
                self.assertNotIn('source_audio',selection)
            else:
                self.assertEqual(selection['no_music_windows'],segment['no_music_windows'])
                self.assertIn('if(lte(t,5.000000),0',command[command.index('-filter_complex')+1])

    def test_dense_short_gaps_use_slower_music_release(self):
        from video_creation_pipeline.music_pipeline import _ducking_release_for_segment, spoken_bgm_policy
        policy = spoken_bgm_policy()['ducking']
        segment = dict(start_s=0,end_s=12)
        sparse = _ducking_release_for_segment(segment,[],policy)[0]
        words = [dict(start=i*.5,end=i*.5+.2) for i in range(24)]
        dense, reason = _ducking_release_for_segment(segment,words,policy)
        self.assertGreater(dense,sparse)
        self.assertIn('dense short pauses',reason)

    def test_v1_contract_retains_the_legacy_mix_behavior(self) -> None:
        candidates = build_music_candidate_set(
            self.intent_path,
            self.retrieval_path,
            [self.calm_profile_path, self.vocal_profile_path],
        )
        candidate_path = self.root / "candidates.json"
        write_music_contract(candidates, candidate_path)
        plan = build_music_edit_plan(candidate_path, contract_version="v1")
        self.assertEqual(plan["schema"], MUSIC_EDIT_PLAN_V1_SCHEMA)
        self.assertEqual(plan["selections"][0]["gain_db"], -18.0)
        self.assertEqual(plan["selections"][0]["ducking"]["threshold"], 0.02)
        self.assertNotIn("gain_envelope", plan["selections"][0])
        validate_music_edit_plan(plan)

    def test_v2_binds_word_timestamps_and_builds_a_risk_window(self) -> None:
        risk_intent_path = self.root / "risk-intent.json"
        write_music_contract(
            build_music_intent_curve(
                self.source,
                [
                    {
                        "segment_id": "risk-1",
                        "start_s": 0.0,
                        "end_s": 12.0,
                        "relationship_mode": "neutral_support",
                        "text_query": "癌症治疗费用是30万元，也要看免责条款",
                        "target": self._descriptors(
                            valence=0.45,
                            arousal=0.25,
                            warmth=0.45,
                            movement=0.20,
                            engagement=0.15,
                        ),
                        "vocal_policy": "instrumental_required",
                        "narration_density": 0.8,
                        "prohibited_tags": ["vocal"],
                        "evidence_ids": ["risk-1"],
                    }
                ],
            ),
            risk_intent_path,
        )
        risk_profile = self._profile(
            "risk",
            descriptors=self._descriptors(
                valence=0.45,
                arousal=0.25,
                warmth=0.45,
                movement=0.20,
                engagement=0.15,
            ),
            tags=["instrumental", "restrained_risk"],
            vocal_probability=0.0,
            commercial_use_allowed=True,
        )
        retrieval_path = self.root / "risk-retrieval.json"
        write_music_contract(
            build_music_retrieval_results(
                risk_intent_path,
                [
                    {
                        "segment_id": "risk-1",
                        "asset_id": "risk",
                        "window_id": "risk-main",
                        "semantic_similarity": 0.96,
                        "continuity_diversity": 0.7,
                        "query_text": "restrained risk music",
                    }
                ],
                provider={
                    "id": "fixture",
                    "model": "fixture",
                    "version": "1",
                    "source_commit": "fixture-reviewed",
                    "execution_location": "local",
                    "model_license": "MIT",
                },
            ),
            retrieval_path,
        )
        candidates_path = self.root / "risk-candidates.json"
        write_music_contract(
            build_music_candidate_set(
                risk_intent_path,
                retrieval_path,
                [risk_profile],
            ),
            candidates_path,
        )
        words_path = self._json(
            "words.json",
            {
                "words": [
                    {"word": "癌症", "start": 1.0, "end": 1.3},
                    {"word": "治疗", "start": 1.35, "end": 1.6},
                    {"word": "30万元", "start": 4.0, "end": 4.5},
                    {"word": "免责条款", "start": 8.0, "end": 8.6},
                ]
            },
        )
        plan = build_music_edit_plan(
            candidates_path,
            word_timestamps_path=words_path,
        )
        selection = plan["selections"][0]
        self.assertEqual(selection["music_family"]["family_id"], "restrained_risk")
        self.assertEqual(selection["gain_db"], -3.0)
        self.assertEqual(selection["gain_envelope"]["source_mode"], "word_timestamps")
        self.assertEqual(
            {row["music_gain_db"] for row in selection["gain_envelope"]["windows"]},
            {-6.0},
        )
        validate_music_edit_plan(plan)

        words_path.write_text('{"words": []}', encoding="utf-8")
        with self.assertRaisesRegex(ContractError, "changed after"):
            validate_music_edit_plan(plan)

    def test_mix_command_applies_a_smooth_attenuation_envelope(self) -> None:
        selection = {
            "timeline_interval": {"start_s": 0.0, "end_s": 4.0},
            "source_interval": {"start_s": 0.0, "end_s": 4.0},
            "loop": False,
            "gain_db": -3.0,
            "gain_envelope": {
                "base_music_gain_db": -3.0,
                "speech_priority_music_gain_db": -6.0,
                "ramp_s": 0.1,
                "source_mode": "word_timestamps",
                "coverage_ratio": 0.25,
                "windows": [{"start_s": 1.0, "end_s": 2.0, "music_gain_db": -6.0}],
                "semantic_timeline_moved": False,
            },
            "fade_in_s": 0.5,
            "fade_out_s": 0.5,
            "ducking": {
                "enabled": True,
                "threshold": 0.08,
                "ratio": 3.0,
                "attack_ms": 30,
                "release_ms": 400,
            },
        }
        command = build_music_mix_command(
            self.root / "program.mp4",
            self.root / "music.wav",
            self.root / "output.mp4",
            selection,
            deterministic=True,
        )
        graph = command[command.index("-filter_complex") + 1]
        self.assertIn("volume=-3.000dB", graph)
        self.assertIn("eval=frame", graph)
        self.assertIn("sidechaincompress=threshold=0.080000:ratio=3.000", graph)
        self.assertIn("alimiter=limit=0.841395142:level=false", graph)
        self.assertIn("aresample=osf=s16:dither_method=none", graph)
        self.assertNotIn("adelay", graph)
        self.assertNotIn("atrim=start=-", graph)

    def test_builds_auditable_post_ducking_music_stem_command(self) -> None:
        selection = {
            "timeline_interval": {"start_s": 0.0, "end_s": 2.0},
            "source_interval": {"start_s": 0.0, "end_s": 2.0},
            "loop": False,
            "fade_in_s": 0.1,
            "fade_out_s": 0.2,
            "gain_db": -3.0,
            "gain_envelope": {
                "base_music_gain_db": -3.0,
                "speech_priority_music_gain_db": -6.0,
                "ramp_s": 0.1,
                "source_mode": "none",
                "coverage_ratio": 0.0,
                "windows": [],
                "semantic_timeline_moved": False,
            },
            "ducking": {
                "enabled": True,
                "threshold": 0.08,
                "ratio": 3.0,
                "attack_ms": 30,
                "release_ms": 400,
            },
        }
        command = build_ducked_music_stem_command(
            self.root / "program.mp4",
            self.root / "music.wav",
            self.root / "ducked-music.wav",
            selection,
        )
        graph = command[command.index("-filter_complex") + 1]
        self.assertIn("sidechaincompress", graph)
        self.assertNotIn("amix=", graph)
        self.assertEqual(command[command.index("-map") + 1], "[music]")
        self.assertEqual(command[-1], str((self.root / "ducked-music.wav").resolve()))

    def test_legacy_mix_selection_keeps_the_original_filter_graph(self) -> None:
        command = build_music_mix_command(
            self.root / "program.mp4",
            self.root / "music.wav",
            self.root / "output.mp4",
            {
                "timeline_interval": {"start_s": 0.0, "end_s": 4.0},
                "source_interval": {"start_s": 0.0, "end_s": 4.0},
                "loop": False,
                "gain_db": -18.0,
                "fade_in_s": 0.5,
                "fade_out_s": 0.5,
                "ducking": {"enabled": False},
            },
        )
        graph = command[command.index("-filter_complex") + 1]
        self.assertIn(
            "[0:a][music]amix=inputs=2:duration=first:normalize=0[aout]", graph
        )
        self.assertNotIn("alimiter", graph)
        self.assertNotIn("eval=frame", graph)

    def test_release_gate_binds_music_plan_and_source_audio(self) -> None:
        candidates = build_music_candidate_set(
            self.intent_path,
            self.retrieval_path,
            [self.calm_profile_path, self.vocal_profile_path],
        )
        candidate_path = self.root / "candidates.json"
        write_music_contract(candidates, candidate_path)
        plan_path = self.root / "music-plan.json"
        plan = build_music_edit_plan(candidate_path)
        write_music_contract(plan, plan_path)

        approvals, visual_plan = self._release_approvals()
        visual_payload = json.loads(visual_plan.read_text(encoding="utf-8"))
        manifest = self._json(
            "render-manifest.json",
            {
                "visual_plan_sha256": canonical_json_sha256(visual_payload),
                "music_edit_plan_sha256": plan["music_edit_plan_sha256"],
            },
        )
        gate = build_release_gate(
            approvals,
            render_manifest_path=manifest,
            visual_plan_path=visual_plan,
            music_edit_plan_path=plan_path,
        )
        validate_release_gate(gate)

        (self.root / "calm.wav").write_bytes(b"replaced-after-render")
        with self.assertRaisesRegex(ContractError, "changed after"):
            validate_release_gate(gate)

    def test_profile_change_invalidates_candidates_and_plan(self) -> None:
        candidates = build_music_candidate_set(
            self.intent_path,
            self.retrieval_path,
            [self.calm_profile_path, self.vocal_profile_path],
        )
        candidate_path = self.root / "candidates.json"
        write_music_contract(candidates, candidate_path)
        plan = build_music_edit_plan(candidate_path)
        calm_audio = self.root / "calm.wav"
        calm_audio.write_bytes(b"changed-audio")
        with self.assertRaisesRegex(ContractError, "changed after"):
            validate_music_candidate_set(candidates)
        with self.assertRaisesRegex(ContractError, "changed after"):
            validate_music_edit_plan(plan)

    def test_commercial_license_is_a_hard_filter(self) -> None:
        personal_only = self._profile(
            "personal-only",
            descriptors=self._descriptors(),
            tags=["instrumental"],
            vocal_probability=0.0,
            commercial_use_allowed=False,
        )
        retrieval = build_music_retrieval_results(
            self.intent_path,
            [
                {
                    "segment_id": "scene-1",
                    "asset_id": "personal-only",
                    "window_id": "personal-only-main",
                    "semantic_similarity": 1.0,
                    "query_text": "instrumental",
                }
            ],
            provider={
                "id": "fixture",
                "model": "fixture",
                "version": "1",
                "source_commit": "fixture-reviewed",
                "execution_location": "local",
                "model_license": "MIT",
            },
        )
        retrieval_path = self.root / "personal-retrieval.json"
        write_music_contract(retrieval, retrieval_path)
        candidates = build_music_candidate_set(
            self.intent_path,
            retrieval_path,
            [personal_only],
            purpose="commercial",
        )
        self.assertEqual(candidates["segments"][0]["candidates"], [])
        self.assertEqual(
            candidates["rejected"][0]["reasons"],
            ["commercial_use_not_allowed"],
        )

    def test_cli_builds_an_unresolved_plan_without_silent_fallback(self) -> None:
        candidates = build_music_candidate_set(
            self.intent_path,
            self.retrieval_path,
            [self.calm_profile_path, self.vocal_profile_path],
        )
        candidate_path = self.root / "candidates.json"
        write_music_contract(candidates, candidate_path)
        output = self.root / "music-plan.json"
        code = music_cli_main(
            [
                "build-edit-plan",
                "--candidates",
                str(candidate_path),
                "--min-score",
                "0.99",
                "--output",
                str(output),
            ]
        )
        self.assertEqual(code, 0)
        plan = json.loads(output.read_text(encoding="utf-8"))
        self.assertFalse(plan["readiness"]["render_eligible"])
        self.assertTrue(plan["readiness"]["manual_review_required"])
        self.assertEqual(plan["selections"][0]["status"], "needs_creative_selection")
        self.assertFalse(plan["authority_boundary"]["automatic_provider_fallback"])

    def _release_approvals(self) -> tuple[list[Path], Path]:
        script = self._json("script.json", {"text": "approved script"})
        voice = self._json("voice.json", {"segments": [{"id": "voice-1"}]})
        measurements = self._json("measurements.json", {"duration_s": 12.0})
        voice_audio = self.root / "voice.wav"
        voice_audio.write_bytes(b"voice")
        visual = self._json("visual-plan.json", {"shots": [{"id": "shot-1"}]})
        review = self.root / "director-review.md"
        review.write_text("approved", encoding="utf-8")
        records = [
            build_approval_record(
                "script",
                script,
                approved_by="operator",
                approved_at="2026-08-03T22:00:00+08:00",
            ),
            build_approval_record(
                "voice",
                voice,
                approved_by="operator",
                approved_at="2026-08-03T22:01:00+08:00",
                attachments=[
                    {"role": "voice_measurements", "path": measurements},
                    {"role": "audio", "path": voice_audio},
                ],
            ),
            build_approval_record(
                "visuals",
                visual,
                approved_by="operator",
                approved_at="2026-08-03T22:02:00+08:00",
                attachments=[{"role": "director_review_sheet", "path": review}],
            ),
        ]
        paths = []
        for record in records:
            path = self.root / f"approval-{record['target']}.json"
            write_creative_contract(record, path)
            paths.append(path)
        return paths, visual

    def _profile(
        self,
        asset_id: str,
        *,
        descriptors: dict[str, float],
        tags: list[str],
        vocal_probability: float,
        commercial_use_allowed: bool,
    ) -> Path:
        audio = self.root / f"{asset_id}.wav"
        audio.write_bytes(f"audio-{asset_id}".encode())
        profile = build_music_asset_profile(
            audio,
            asset_id=asset_id,
            duration_s=20.0,
            windows=[
                {
                    "window_id": f"{asset_id}-main",
                    "start_s": 0.0,
                    "end_s": 20.0,
                    "structure_label": "intro",
                    "structure_confidence": 0.9,
                    "descriptors": descriptors,
                    "vocal_probability": vocal_probability,
                    "tags": tags,
                    "loopable": True,
                }
            ],
            license_info={
                "status": "allowed",
                "commercial_use_allowed": commercial_use_allowed,
                "license_uri": f"local://fixture/{asset_id}",
                "license_snapshot": "owned test fixture",
                "attribution": "",
            },
            analyzer={
                "id": "reviewed-manual-fixture",
                "model": "fixture",
                "version": "1",
                "source_commit": "fixture-reviewed",
                "execution_location": "local",
                "model_license": "MIT",
                "automatic_fallback_used": False,
            },
        )
        path = self.root / f"{asset_id}-profile.json"
        write_music_contract(profile, path)
        return path

    def _descriptors(self, **overrides: float) -> dict[str, float]:
        values = {
            "valence": 0.5,
            "arousal": 0.4,
            "tension": 0.3,
            "intimacy_scale": 0.4,
            "warmth": 0.5,
            "movement": 0.4,
            "engagement": 0.3,
        }
        values.update(overrides)
        return values

    def _json(self, name: str, payload: dict) -> Path:
        path = self.root / name
        atomic_write_json(payload, path)
        return path


if __name__ == "__main__":
    unittest.main()
