from __future__ import annotations

import shutil
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from video_creation_pipeline.contract_io import artifact_reference, atomic_write_json
from video_creation_pipeline.errors import ContractError
from video_creation_pipeline.music_timing_qa import (
    ALLIN1_SOURCE_COMMIT,
    PYMUSICLOOPER_SOURCE_COMMIT,
    adapt_allin1_result,
    adapt_librosa_bar_grid_result,
    build_music_structure_analysis,
    build_music_timing_qa_report,
    build_speech_priority_windows,
    classify_music_family,
    evaluate_delta_lu,
    evaluate_ducking_pump_risk,
    recommend_mix_profile,
    snap_source_window,
    validate_music_structure_analysis,
    validate_music_timing_qa_report,
)
from video_creation_pipeline.music_timing_qa_cli import main as timing_cli_main


class MusicTimingQATests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = ROOT / ".test-runtime" / self._testMethodName
        if self.root.exists():
            shutil.rmtree(self.root)
        self.root.mkdir(parents=True)
        self.audio = self.root / "music.wav"
        self.audio.write_bytes(b"synthetic-music-fixture")

    def tearDown(self) -> None:
        if self.root.exists():
            shutil.rmtree(self.root)

    def test_builds_hash_bound_structure_analysis_and_detects_drift(self) -> None:
        analysis = self._analysis()
        self.assertEqual(analysis["tempo"]["bpm"], 90.0)
        self.assertIn(4.0, analysis["phrase_boundary_candidates"])
        validate_music_structure_analysis(analysis)

        self.audio.write_bytes(b"changed")
        with self.assertRaisesRegex(ContractError, "changed after"):
            validate_music_structure_analysis(analysis)

    def test_adapts_only_the_reviewed_allin1_revision(self) -> None:
        result = {
            "bpm": 90,
            "beats": [0, 1, 2, 3, 4, 5, 6, 7, 8],
            "downbeats": [0, 4, 8],
            "beat_positions": [1, 2, 3, 4, 1, 2, 3, 4, 1],
            "segments": [
                {"start": 0, "end": 4, "label": "intro"},
                {"start": 4, "end": 8, "label": "verse"},
            ],
        }
        analysis = adapt_allin1_result(self.audio, result)
        self.assertEqual(analysis["provider"]["source_commit"], ALLIN1_SOURCE_COMMIT)
        with self.assertRaisesRegex(ContractError, "reviewed revision"):
            adapt_allin1_result(self.audio, result, source_commit="drifted")

    def test_adapts_only_the_reviewed_pymusiclooper_revision(self) -> None:
        result = {
            "duration_s": 8,
            "bpm": 90,
            "beats": [0, 1, 2, 3, 4, 5, 6, 7, 8],
            "downbeats": [0, 4, 8],
            "beat_positions": [1, 2, 3, 4, 1, 2, 3, 4, 1],
            "segments": [
                {"start": 0, "end": 4, "label": "phrase_a"},
                {"start": 4, "end": 8, "label": "phrase_b"},
            ],
            "librosa_version": "0.11.0",
        }
        analysis = adapt_librosa_bar_grid_result(self.audio, result)
        self.assertEqual(analysis["provider"]["id"], "librosa_bar_grid")
        self.assertEqual(
            analysis["provider"]["adapted_source_commit"],
            PYMUSICLOOPER_SOURCE_COMMIT,
        )
        with self.assertRaisesRegex(ContractError, "reviewed revision"):
            adapt_librosa_bar_grid_result(
                self.audio, result, adapted_source_commit="drifted"
            )

    def test_snaps_source_windows_without_moving_the_semantic_timeline(self) -> None:
        analysis = self._analysis()
        snapped = snap_source_window(
            analysis,
            desired_start_s=3.7,
            duration_s=2.0,
        )
        self.assertEqual(snapped["boundary_type"], "phrase")
        self.assertEqual(snapped["snapped_start_s"], 4.0)
        self.assertFalse(snapped["semantic_timeline_moved"])

        kept = snap_source_window(
            analysis,
            desired_start_s=2.25,
            duration_s=2.0,
            tolerance_s=0.1,
        )
        self.assertEqual(kept["status"], "kept_original")

    def test_selects_quieter_music_for_dense_or_risk_content(self) -> None:
        dense = recommend_mix_profile(
            {
                "narration_density": 0.99,
                "text_query": "解释赔付金额与免责条款",
            },
            functional_role="body_clear",
        )
        self.assertEqual(dense["music_gain_db"], -6.0)
        self.assertIn("条款", dense["matched_high_information_terms"])

        opening = recommend_mix_profile(
            {"narration_density": 1.0, "text_query": "开场"},
            functional_role="opening_warm",
        )
        self.assertEqual(opening["music_gain_db"], -3.0)

    def test_risk_terms_take_precedence_over_story_terms(self) -> None:
        family = classify_music_family(
            {"text_query": "客户故事里也必须解释免责条款和费用风险"}
        )
        self.assertEqual(family["family_id"], "restrained_risk")
        self.assertIn("条款", family["matched_terms"])

        default = classify_music_family({"text_query": "解释四个治疗方向"})
        self.assertEqual(default["family_id"], "clear_explainer")

        warm = classify_music_family({"text_query": "稳暖克制的真实人物经历"})
        self.assertEqual(warm["family_id"], "warm_story")

    def test_word_timestamps_create_merged_priority_windows(self) -> None:
        result = build_speech_priority_windows(
            {
                "start_s": 0.0,
                "end_s": 10.0,
                "narration_density": 0.8,
                "text_query": "这笔30万元理赔需要说明",
            },
            word_timestamps=[
                {"word": "这笔", "start": 0.8, "end": 1.0},
                {"word": "30", "start": 1.25, "end": 1.35},
                {"word": "万元", "start": 1.35, "end": 1.5},
                {"word": "理赔", "start": 2.0, "end": 2.2},
            ],
        )
        self.assertEqual(result["source_mode"], "word_timestamps")
        self.assertEqual(result["application_mode"], "windowed_attenuation")
        self.assertEqual(len(result["windows"]), 1)
        self.assertAlmostEqual(result["windows"][0]["start_s"], 1.1)
        self.assertAlmostEqual(result["windows"][0]["end_s"], 2.55)
        self.assertEqual(result["windows"][0]["music_gain_db"], -6.0)
        self.assertFalse(result["semantic_timeline_moved"])

    def test_word_timestamps_can_trigger_priority_for_a_generic_summary(self) -> None:
        result = build_speech_priority_windows(
            {
                "start_s": 0.0,
                "end_s": 8.0,
                "narration_density": 0.7,
                "text_query": "接下来解释一个具体例子",
            },
            word_timestamps=[
                {"word": "需要", "start": 2.0, "end": 2.2},
                {"word": "30万元", "start": 2.25, "end": 2.7},
            ],
        )
        self.assertEqual(result["source_mode"], "word_timestamps")
        self.assertEqual(result["application_mode"], "windowed_attenuation")
        self.assertEqual(result["windows"][0]["start_s"], 2.1)
        self.assertEqual(result["windows"][0]["end_s"], 3.05)
        self.assertEqual(result["windows"][0]["music_gain_db"], -6.0)

    def test_subtitles_then_semantic_segment_are_explicit_fallbacks(self) -> None:
        segment = {
            "start_s": 0.0,
            "end_s": 10.0,
            "narration_density": 0.8,
            "text_query": "癌症治疗方案",
        }
        subtitle_result = build_speech_priority_windows(
            segment,
            word_timestamps=[{"word": "普通内容", "start": 0.5, "end": 1.0}],
            subtitle_cues=[{"text": "癌症治疗", "start_s": 4.0, "end_s": 5.0}],
        )
        self.assertEqual(subtitle_result["source_mode"], "srt_subtitles")
        self.assertEqual(subtitle_result["application_mode"], "windowed_attenuation")

        segment_result = build_speech_priority_windows(segment)
        self.assertEqual(segment_result["source_mode"], "semantic_segment_fallback")
        self.assertEqual(segment_result["application_mode"], "full_segment_coverage")
        self.assertEqual(segment_result["coverage_ratio"], 1.0)

    def test_subtitle_presentation_digits_do_not_create_priority_windows(self) -> None:
        result = build_speech_priority_windows(
            {
                "start_s": 0.0,
                "end_s": 5.0,
                "narration_density": 0.8,
                "text_query": "普通解释内容",
            },
            subtitle_cues=[
                {
                    "text": '<font face="Microsoft YaHei" size="76"><b>普通内容</b></font>',
                    "start_s": 0.5,
                    "end_s": 4.5,
                }
            ],
        )
        self.assertEqual(result["source_mode"], "none")
        self.assertEqual(result["windows"], [])

    def test_visible_numbers_survive_subtitle_markup_cleanup(self) -> None:
        result = build_speech_priority_windows(
            {
                "start_s": 0.0,
                "end_s": 5.0,
                "narration_density": 0.8,
                "text_query": "普通解释内容",
            },
            subtitle_cues=[
                {
                    "text": '<font size="76"><b>{\\an8}赔付30万元</b></font>',
                    "start_s": 1.0,
                    "end_s": 2.0,
                }
            ],
        )
        self.assertEqual(result["source_mode"], "srt_subtitles")
        self.assertEqual(result["application_mode"], "windowed_attenuation")
        self.assertIn("numeric_expression", result["windows"][0]["matched_terms"])

    def test_dense_priority_coverage_uses_one_quiet_segment(self) -> None:
        result = build_speech_priority_windows(
            {
                "start_s": 0.0,
                "end_s": 2.0,
                "narration_density": 0.99,
                "text_query": "解释条款",
            },
            subtitle_cues=[{"text": "条款", "start_s": 0.1, "end_s": 1.4}],
        )
        self.assertEqual(result["source_mode"], "srt_subtitles")
        self.assertEqual(result["application_mode"], "full_segment_coverage")
        self.assertEqual(result["windows"][0]["start_s"], 0.0)
        self.assertEqual(result["windows"][0]["end_s"], 2.0)

    def test_ordinary_segment_has_no_gain_windows(self) -> None:
        result = build_speech_priority_windows(
            {
                "start_s": 0.0,
                "end_s": 5.0,
                "narration_density": 0.5,
                "text_query": "今天分享一个简单方法",
            }
        )
        self.assertEqual(result["source_mode"], "none")
        self.assertEqual(result["application_mode"], "none")
        self.assertEqual(result["windows"], [])
        self.assertEqual(result["base_music_gain_db"], -3.0)

    def test_delta_lu_only_auto_reduces_music(self) -> None:
        profile = recommend_mix_profile(
            {"narration_density": 0.8, "text_query": "客户故事"},
            functional_role="body_warm",
        )
        prominent = evaluate_delta_lu(
            dialogue_lufs=-18.0,
            music_lufs=-21.0,
            profile=profile,
        )
        self.assertEqual(prominent["status"], "music_too_prominent")
        self.assertEqual(prominent["safe_gain_correction_db"], -3.0)

        subtle = evaluate_delta_lu(
            dialogue_lufs=-18.0,
            music_lufs=-34.0,
            profile=profile,
        )
        self.assertEqual(subtle["status"], "music_subtle_user_review")
        self.assertEqual(subtle["safe_gain_correction_db"], 0.0)
        self.assertFalse(subtle["automatic_music_boost_applied"])

    def test_short_pause_density_flags_pumping_release(self) -> None:
        gaps = [{"duration_s": 0.4} for _ in range(12)]
        result = evaluate_ducking_pump_risk(
            gaps,
            programme_duration_s=60.0,
            release_ms=400,
        )
        self.assertEqual(result["status"], "review_release_for_pumping")
        self.assertEqual(result["recommended_release_ms"], 600)

    def test_report_binds_inputs_and_blocks_prominent_music(self) -> None:
        cue_plan = self.root / "cue-plan.json"
        atomic_write_json({"schema": "fixture.cue-plan.v1"}, cue_plan)
        profile = recommend_mix_profile(
            {"narration_density": 0.8, "text_query": "客户故事"},
            functional_role="body_warm",
        )
        report = build_music_timing_qa_report(
            source_reference=artifact_reference(self.audio, "QA source"),
            cue_plan_reference=artifact_reference(cue_plan, "QA cue plan"),
            segment_rows=[
                {
                    "segment_id": "segment-1",
                    "delta_lu": evaluate_delta_lu(
                        dialogue_lufs=-18.0,
                        music_lufs=-21.0,
                        profile=profile,
                    ),
                }
            ],
            ducking=evaluate_ducking_pump_risk(
                [], programme_duration_s=30.0, release_ms=400
            ),
        )
        self.assertEqual(report["status"], "blocked")
        validate_music_timing_qa_report(report)

        report["status"] = "candidate_only_passed"
        report["report_sha256"] = "0" * 64
        with self.assertRaisesRegex(ContractError, "integrity"):
            validate_music_timing_qa_report(report)

    def test_cli_adapts_and_validates_allin1_result(self) -> None:
        result_path = self.root / "allin1.json"
        atomic_write_json(
            {
                "bpm": 90,
                "beats": [0, 1, 2, 3, 4, 5, 6, 7, 8],
                "downbeats": [0, 4, 8],
                "beat_positions": [1, 2, 3, 4, 1, 2, 3, 4, 1],
                "segments": [
                    {"start": 0, "end": 4, "label": "intro"},
                    {"start": 4, "end": 8, "label": "verse"},
                ],
            },
            result_path,
        )
        output = self.root / "analysis.json"
        self.assertEqual(
            timing_cli_main(
                [
                    "adapt-allin1",
                    "--source",
                    str(self.audio),
                    "--result",
                    str(result_path),
                    "--output",
                    str(output),
                ]
            ),
            0,
        )
        self.assertEqual(
            timing_cli_main(["validate-analysis", "--analysis", str(output)]),
            0,
        )

    def _analysis(self) -> dict:
        return build_music_structure_analysis(
            self.audio,
            provider={
                "id": "librosa_bar_grid",
                "version": "librosa-0.11.0",
                "source_commit": "local-runtime",
                "adapted_source_commit": PYMUSICLOOPER_SOURCE_COMMIT,
                "method": "librosa onset/chroma plus PyMusicLooper fixed-source score",
                "runtime_status": "local_completed",
                "media_duration_s": 8.0,
            },
            bpm=90.0,
            beats=[0, 1, 2, 3, 4, 5, 6, 7, 8],
            downbeats=[0, 4, 8],
            beat_positions=[1, 2, 3, 4, 1, 2, 3, 4, 1],
            segments=[
                {"start": 0, "end": 4, "label": "phrase_a"},
                {"start": 4, "end": 8, "label": "phrase_b"},
            ],
        )


if __name__ == "__main__":
    unittest.main()
