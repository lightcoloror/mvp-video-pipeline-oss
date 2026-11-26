"""Standalone unit tests for mvp-video-pipeline P0-v2 subtitle style, expression, and Path 3 guard."""
from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))


from video_creation_pipeline.contract_io import artifact_reference  # noqa: E402
from video_creation_pipeline.errors import ContractError  # noqa: E402
from video_creation_pipeline.remotion_project import _project_data  # noqa: E402
from video_creation_pipeline.subtitle_expression import (  # noqa: E402
    EXPRESSIVE_SCHEMA,
    apply_expressive_cues,
    build_expected_render_plan,
)
from video_creation_pipeline.subtitle_sequence import (  # noqa: E402
    build_reviewed_subtitle_sequence,
    export_whole_srt,
)
from video_creation_pipeline.subtitle_styles import (  # noqa: E402
    build_ass_document,
    resolve_subtitle_style,
)


class P0V2SubtitleEngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = pathlib.Path(self.tmp.name)
        self.scene_colors = {
            "受访者": {"color": "#8EE3FF", "label": "冷青", "color_reason": "Warm backdrop contrast"},
            "旁白": {"color": "#FFB499", "label": "珊瑚", "color_reason": "Distinct secondary speaker"},
        }
        self.style_default = resolve_subtitle_style(1080, 1920, speaker_colors=self.scene_colors)

    def tearDown(self):
        self.tmp.cleanup()

    def test_presets_and_tv709_and_legacy_compat(self):
        self.assertEqual(self.style_default["preset_id"], "stroke_shadow_default")
        self.assertEqual(self.style_default["border_style"], 1)
        self.assertEqual(self.style_default["outline_px"], 3.5)
        self.assertEqual(self.style_default["shadow"], 2.5)
        bs4 = resolve_subtitle_style(1080, 1920, preset_id="box_stroke_bs4", speaker_colors=self.scene_colors)
        self.assertEqual(bs4["border_style"], 4)
        self.assertEqual(bs4["outline_px"], 2)
        self.assertEqual(resolve_subtitle_style(1080, 1920, preset_id="accessible_box_a")["border_style"], 3)
        self.assertEqual(resolve_subtitle_style(1080, 1920, preset_id="outline_v2_c")["border_style"], 1)

        seq = build_reviewed_subtitle_sequence(
            [{"cue_id": "c1", "start_s": 0.0, "end_s": 4.0, "speaker": "受访者", "speaker_prefix": "受访者：", "spoken_text": "其实这个，条款真的关键"}],
            max_line_units=11,
            total_duration_s=4.0,
        )
        ass = build_ass_document(seq["cues"], self.style_default)
        self.assertIn("YCbCr Matrix: TV.709", ass)
        self.assertIn("Style: Speaker_01,", ass)

    def test_hue_collision_and_astral_runs(self):
        with self.assertRaises(ContractError):
            resolve_subtitle_style(
                1080, 1920,
                speaker_colors={"A": {"color": "#FFE47A", "label": "黄", "color_reason": "Overlap check"}},
            )
        seq = build_reviewed_subtitle_sequence(
            [{"cue_id": "u1", "start_s": 0.0, "end_s": 3.0, "spoken_text": "姓氏𠮷很关键"}],
            max_line_units=11,
            total_duration_s=3.0,
        )
        exp_doc = {
            "schema": EXPRESSIVE_SCHEMA,
            "timeline_duration_s": 3.0,
            "annotations": [
                {
                    "annotation_id": "a1",
                    "cue_id": "u1",
                    "kind": "emphasis",
                    "expected_spoken_text": "姓氏𠮷很关键",
                    "expected_start_s": 0.0,
                    "expected_end_s": 3.0,
                    "spans": [{"char_start": 2, "char_end": 3, "text": "𠮷"}],
                    "evidence_status": "verified_synthetic",
                    "evidence_method": "reviewed_emphasis",
                    "evidence_source": "fixture",
                }
            ],
        }
        plan = build_expected_render_plan(seq, expressive_doc=exp_doc, style_spec=self.style_default)
        self.assertEqual([r["text"] for r in plan["expected_events"][0]["runs"]], ["姓氏", "𠮷", "很关键"])

    def test_N2_N3_N4_N6_N7_N9_engine_guards(self):
        # N2: squeezed sub-segment skipped
        seq_p = build_reviewed_subtitle_sequence(
            [{"cue_id": "p1", "start_s": 0.0, "end_s": 5.0, "spoken_text": "前半句内容，后半句也很重要"}],
            max_line_units=11, total_duration_s=5.0,
        )
        _, diag_n2 = apply_expressive_cues(seq_p, {
            "schema": EXPRESSIVE_SCHEMA, "timeline_duration_s": 5.0,
            "annotations": [{
                "annotation_id": "p_bad", "cue_id": "p1", "kind": "pause", "pause_mode": "clear_gap",
                "split_char_index": 6, "part1_end_s": 4.93, "part2_start_s": 4.97,
                "expected_spoken_text": "前半句内容，后半句也很重要", "expected_start_s": 0.0, "expected_end_s": 5.0,
                "evidence_status": "verified_synthetic", "evidence_method": "vkp_word_gap", "evidence_source": "fixture",
            }],
        }, style_spec=self.style_default)
        self.assertEqual(diag_n2["applied_count"], 0)

        # N3: literal [笑着说] in speech preserved verbatim in ASS & SRT
        seq_t = build_reviewed_subtitle_sequence(
            [{"cue_id": "t1", "start_s": 0.0, "end_s": 4.0, "spoken_text": '不爱"[笑着说]"'}],
            max_line_units=11, total_duration_s=4.0,
        )
        app_t, _ = apply_expressive_cues(seq_t, {
            "schema": EXPRESSIVE_SCHEMA, "timeline_duration_s": 4.0,
            "annotations": [{
                "annotation_id": "t3", "cue_id": "t1", "kind": "tone", "tone_label": "轻笑",
                "expected_spoken_text": '不爱"[笑着说]"', "expected_start_s": 0.0, "expected_end_s": 4.0,
                "evidence_status": "verified_synthetic", "evidence_method": "reviewed_tone", "evidence_source": "fixture",
            }],
        }, style_spec=self.style_default)
        ass_t = build_ass_document(app_t["cues"], self.style_default)
        srt_t = export_whole_srt(app_t)
        self.assertIn('"[笑着说]"', ass_t)
        self.assertIn('"[笑着说]"', srt_t)
        self.assertIn("[轻笑]", ass_t)
        self.assertNotIn("[轻笑]", srt_t)

        # N4: progressive reveal & clear_gap both reconstruct speech losslessly
        seq_c = build_reviewed_subtitle_sequence(
            [{"cue_id": "c4", "start_s": 0.0, "end_s": 6.0, "speaker": "受访者", "speaker_prefix": "受访者：", "spoken_text": "这件事关键，先看条件"}],
            max_line_units=11, total_duration_s=6.0,
        )
        plan_c = build_expected_render_plan(seq_c, expressive_doc={
            "schema": EXPRESSIVE_SCHEMA, "timeline_duration_s": 6.0,
            "annotations": [{
                "annotation_id": "p4", "cue_id": "c4", "kind": "pause", "pause_mode": "progressive_reveal",
                "split_char_index": 6, "part1_end_s": 2.4, "part2_start_s": 3.0,
                "expected_spoken_text": "这件事关键，先看条件", "expected_start_s": 0.0, "expected_end_s": 6.0,
                "evidence_status": "verified_synthetic", "evidence_method": "vkp_word_gap", "evidence_source": "fixture",
            }],
        }, style_spec=self.style_default)
        self.assertEqual("".join(e["speech_reconstruction"] for e in plan_c["expected_events"]), "这件事关键，先看条件")

        # N6: 11-char overflow guard
        seq_w = build_reviewed_subtitle_sequence(
            [{"cue_id": "w1", "start_s": 0.0, "end_s": 4.0, "spoken_text": "一二三四五六七八九十甲"}],
            max_line_units=11, total_duration_s=4.0,
        )
        _, diag_w = apply_expressive_cues(seq_w, {
            "schema": EXPRESSIVE_SCHEMA, "timeline_duration_s": 4.0,
            "annotations": [{
                "annotation_id": "ew", "cue_id": "w1", "kind": "emphasis",
                "expected_spoken_text": "一二三四五六七八九十甲", "expected_start_s": 0.0, "expected_end_s": 4.0,
                "spans": [{"char_start": 0, "char_end": 4, "text": "一二三四"}],
                "evidence_status": "verified_synthetic", "evidence_method": "reviewed_emphasis", "evidence_source": "fixture",
            }],
        }, style_spec=self.style_default)
        self.assertEqual(diag_w["skipped"][0]["skip_reason"], "emphasis_would_overflow_safe_width")

        # N7: stale expressive -> PASS_WITH_FALLBACK
        plan_stale = build_expected_render_plan(seq_t, expressive_doc={
            "schema": EXPRESSIVE_SCHEMA, "timeline_duration_s": 99.0, "annotations": []
        }, style_spec=self.style_default, requested_features=["tone"])
        # Empty annotations triggers valid 0-skip unless mismatched ann exists:
        plan_stale2 = build_expected_render_plan(seq_t, expressive_doc={
            "schema": EXPRESSIVE_SCHEMA, "timeline_duration_s": 99.0,
            "annotations": [{
                "annotation_id": "s1", "cue_id": "t1", "kind": "tone", "tone_label": "轻笑",
                "expected_spoken_text": '不爱"[笑着说]"', "expected_start_s": 0.0, "expected_end_s": 4.0,
                "evidence_status": "verified_synthetic", "evidence_method": "reviewed_tone", "evidence_source": "fixture",
            }],
        }, style_spec=self.style_default, requested_features=["tone"])
        self.assertEqual(plan_stale["delivery_status"], "PASS")
        self.assertEqual(plan_stale2["delivery_status"], "PASS_WITH_FALLBACK")

        # N9: Remotion Path 3 blocks speaker_colors unless explicit allow_plain_fallback=True
        pkg = self.tmp_path / "pkg.json"
        pkg.write_text(json.dumps({
            "schema": "video_creation_pipeline.video_packaging_plan.v1",
            "output": {"width": 1080, "height": 1920, "fps": 30},
            "scenes": [{"scene_id": "s1", "start_s": 0.0, "end_s": 4.0}],
        }), encoding="utf-8")
        srt = self.tmp_path / "sub.srt"
        srt.write_text("1\n00:00:00,000 --> 00:00:04,000\n测试\n", encoding="utf-8")
        handoff = {
            "schema": "video_creation_pipeline.remotion_skill_handoff.v1",
            "composition": {"id": "Main"},
            "inputs": {
                "packaging_plan": artifact_reference(pkg, "video packaging plan"),
                "subtitle_artifact": artifact_reference(srt, "Remotion subtitle artifact"),
            },
            "subtitle_style": {"preset_id": "stroke_shadow_default", "speaker_colors": self.scene_colors},
        }
        with self.assertRaises(ContractError):
            _project_data(handoff)
        data = _project_data(handoff, allow_plain_fallback=True)
        self.assertEqual(data["subtitleCapabilities"]["delivery_status"], "PASS_WITH_FALLBACK")


if __name__ == "__main__":
    unittest.main(verbosity=2)
