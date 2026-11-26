"""Unit tests for unified reviewed subtitle sequence, shared styles, and dual renderers."""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from video_creation_pipeline import audio_timeline
from video_creation_pipeline.contract_io import artifact_reference
from video_creation_pipeline.errors import ContractError
from video_creation_pipeline.remotion_project import (
    _CAPTIONS_TSX,
    _COMPOSITION_TSX,
    REMOTION_RUNTIME_COMMIT,
    materialize_remotion_project,
    validate_remotion_project_manifest,
)
from video_creation_pipeline.remotion_skill_handoff import (
    REMOTION_SKILLS_COMMIT,
    build_remotion_skill_handoff,
    write_remotion_skill_handoff,
)
from video_creation_pipeline.subtitle_readability import subtitle_font
from video_creation_pipeline.subtitle_sequence import (
    build_reviewed_subtitle_sequence,
    export_whole_srt,
    export_whole_vtt,
    reconcile_observed_lines,
    reconstruct_display_text,
    slice_sequence_for_scene,
)
from video_creation_pipeline.subtitle_styles import (
    ass_alpha_to_opacity,
    build_ass_document,
    build_ass_ffmpeg_filter,
    check_font_glyph_coverage,
    ffmpeg_filter_escape,
    resolve_managed_font,
    resolve_subtitle_style,
    verify_libass_font_selection,
)


class SubtitleSequenceAndStylesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_step2_two_scene_sequence_spacing_prefixes_sounds_and_legacy_pack(self) -> None:
        style_9_16 = resolve_subtitle_style(1080, 1920)
        raw_cues = [
            {
                "cue_id": "c1",
                "start_seconds": 0.2,
                "end_seconds": 2.2,
                "spoken_text": "这份清单里，ICU 报销金额是12.5万元",
                "protected_terms": ["12.5万元", "ICU"],
            },
            {
                "cue_id": "c2",
                "start_seconds": 2.4,
                "end_seconds": 4.3,
                "spoken_text": "等待期是90天，不是交费后立即生效",
                "protected_terms": ["90天", "不是"],
            },
            {
                "cue_id": "c3",
                "start_seconds": 4.6,
                "end_seconds": 7.0,
                "spoken_text": "采访者：原件送到了。",
                "speaker": "采访者",
                "offscreen_speaker": True,
                "sound_event": "门铃声",
            },
            {
                "cue_id": "c4",
                "start_seconds": 7.2,
                "end_seconds": 9.2,
                "spoken_text": "请核对 PDF 条款里的免责说明。",
                "protected_terms": ["PDF"],
            },
        ]
        seq = build_reviewed_subtitle_sequence(
            raw_cues,
            max_line_units=style_9_16["max_line_units"],
            total_duration_s=9.5,
        )
        self.assertEqual(seq["cue_count"], 4)
        c1, c2, c3, c4 = seq["cues"]
        # Prefix kept once (not doubled into "采访者：采访者：") and sound token formatted once.
        self.assertEqual(c3["spoken_text"], "采访者：原件送到了。")
        self.assertEqual(c3["display_text"], "采访者：[门铃声] 原件送到了。")
        self.assertEqual(c3["render_lines"], ["采访者：[门铃声]", "原件送到了。"])
        self.assertEqual(
            reconstruct_display_text(c3["render_lines"], c3["layout_breaks"]),
            c3["display_text"],
        )
        # Protected terms and mixed CJK/English spaces preserved across 9:16 wrap.
        self.assertTrue(any("12.5万元" in ln for ln in c1["render_lines"]))
        self.assertTrue(any("ICU" in ln for ln in c1["render_lines"]))
        self.assertEqual(c4["render_lines"], ["请核对 PDF", "条款里的免责说明。"])
        self.assertEqual(
            reconstruct_display_text(c4["render_lines"], c4["layout_breaks"]),
            "请核对 PDF 条款里的免责说明。",
        )

        # Scene 2 slicing converts to scene-local clock while whole SRT/VTT retain final timestamps.
        scene2_cues = slice_sequence_for_scene(seq, scene_start_s=4.5, scene_end_s=9.5)
        self.assertEqual(len(scene2_cues), 2)
        self.assertAlmostEqual(scene2_cues[0]["start_s"], 0.1, places=4)
        self.assertAlmostEqual(scene2_cues[0]["end_s"], 2.5, places=4)
        self.assertAlmostEqual(scene2_cues[1]["start_s"], 2.7, places=4)
        self.assertAlmostEqual(scene2_cues[1]["end_s"], 4.7, places=4)

        srt = export_whole_srt(seq)
        vtt = export_whole_vtt(seq)
        self.assertIn("00:00:04,600 --> 00:00:07,000", srt)
        self.assertIn("00:00:07,200 --> 00:00:09,200", srt)
        self.assertIn("WEBVTT", vtt)
        self.assertIn("00:00:04.600 --> 00:00:07.000", vtt)

        # ASS/SRT line-break reconciliation handles both CJK (no space) and mixed (space) wraps.
        ok_cjk, _ = reconcile_observed_lines(
            "\\N".join(c2["render_lines"]), c2["display_text"], c2["layout_breaks"]
        )
        ok_mix, _ = reconcile_observed_lines(
            "\\N".join(c4["render_lines"]), c4["display_text"], c4["layout_breaks"]
        )
        self.assertTrue(ok_cjk)
        self.assertTrue(ok_mix)
        bad_space, _ = reconcile_observed_lines(
            "请核对 PDF条款里的免责说明。", c4["display_text"], c4["layout_breaks"]
        )
        self.assertFalse(bad_space)

        # Legacy plain-text pack remains readable.
        legacy = build_reviewed_subtitle_sequence(
            [{"start_seconds": 0.0, "end_seconds": 1.5, "text": "旧版纯文本字幕"}],
            max_line_units=18,
        )
        self.assertEqual(legacy["cues"][0]["display_text"], "旧版纯文本字幕")

        # Duplicate prefix in input raises ContractError, and sound exceeding CPS requires review.
        with self.assertRaisesRegex(ContractError, "duplicate speaker prefix"):
            build_reviewed_subtitle_sequence(
                [{"start_seconds": 0.0, "end_seconds": 2.0, "text": "采访者：采访者：原件到了"}]
            )
        with self.assertRaisesRegex(ContractError, "reading speed"):
            build_reviewed_subtitle_sequence(
                [
                    {
                        "start_seconds": 0.0,
                        "end_seconds": 0.9,
                        "spoken_text": "原件已经送到了",
                        "speaker_prefix": "采访者：",
                        "sound_event": "门铃声",
                    }
                ]
            )

    def test_step3_font_verification_presets_and_cjk_space_libass(self) -> None:
        self.assertEqual(subtitle_font(1920, 1080)[0], 59)
        self.assertEqual(subtitle_font(1080, 1920)[0], 59)

        deng = resolve_managed_font("dengxian_bold_static")
        ali = resolve_managed_font("alibaba_puhuiti_55_regular")
        self.assertFalse(deng["is_variable_font"])
        self.assertEqual(deng["postscript_name"], "DengXian-Bold")
        self.assertEqual(ali["postscript_name"], "AlibabaPuHuiTi_2_55_Regular")

        # Missing glyph fails closed (e.g. Private Use Area codepoint U+E000).
        with self.assertRaisesRegex(ContractError, "lacks glyphs"):
            check_font_glyph_coverage(deng["source_path"], ["\ue000"])

        style_default = resolve_subtitle_style(1920, 1080)
        self.assertEqual(style_default["preset_id"], "stroke_shadow_default")
        self.assertEqual(style_default["border_style"], 1)

        style_a = resolve_subtitle_style(1920, 1080, preset_id="accessible_box_a")
        self.assertEqual(style_a["preset_id"], "accessible_box_a")
        self.assertEqual(style_a["border_style"], 3)
        self.assertEqual(style_a["ass_outline_colour"], "&H59101010")
        self.assertAlmostEqual(ass_alpha_to_opacity(style_a["ass_outline_colour"]), 0.651, places=2)

        style_c = resolve_subtitle_style(1920, 1080, preset_id="outline_v2_c")
        self.assertEqual(style_c["preset_id"], "outline_v2_c")
        self.assertEqual(style_c["border_style"], 1)
        self.assertEqual(style_c["ass_outline_colour"], "&H00383224")

        cjk_dir = self.root / "中文 路径" / "字体 包"
        cjk_dir.mkdir(parents=True)
        shutil.copy2(deng["source_path"], cjk_dir / deng["filename"])
        ass_path = self.root / "中文 路径" / "测试 字幕.ass"
        ass_path.write_text(
            build_ass_document(
                [
                    {
                        "start_s": 0.0,
                        "end_s": 1.0,
                        "render_lines": ["这份清单里，ICU 报销金额是12.5万元"],
                    }
                ],
                style_a,
            ),
            encoding="utf-8",
        )
        flt = build_ass_ffmpeg_filter(ass_path, cjk_dir)
        self.assertIn("fontsdir=", flt)
        self.assertIn("中文 路径", ffmpeg_filter_escape(ass_path))
        ev = verify_libass_font_selection(ass_path, cjk_dir, deng, width=1920, height=1080)
        self.assertTrue(ev["verified"])
        self.assertEqual(ev["expected_postscript_name"], "DengXian-Bold")

    def test_step3_audio_timeline_scale_motion_and_cache_invalidation(self) -> None:
        wav = self.root / "a.wav"
        with wave.open(str(wav), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(24000)
            w.writeframes(b"\x01\x00" * 48000)
        vis = self.root / "v.mp4"
        vis.write_bytes(b"vis")
        prov = self.root / "p.json"
        prov.write_text("{}", encoding="utf-8")
        pp = self.root / "packaging.json"
        bp = self.root / "binding.json"
        packaging = {
            "schema": "video_creation_pipeline.video_packaging_plan.v1",
            "status": "candidate_only",
            "output": {"width": 1920, "height": 1080, "fps": 30},
            "scenes": [
                {"scene_id": "s1", "start_s": 0.0, "end_s": 1.0},
                {"scene_id": "s2", "start_s": 1.0, "end_s": 2.0},
            ],
        }
        pp.write_text(json.dumps(packaging), encoding="utf-8")
        binding = {
            "schema": "video_creation_pipeline.audio_timeline_binding.v1",
            "packaging": artifact_reference(pp, "packaging"),
            "audio": artifact_reference(wav, "audio"),
            "segments": [
                {
                    "scene_id": "s1",
                    "visual": artifact_reference(vis, "visual"),
                    "provenance": artifact_reference(prov, "prov"),
                    "semantic_description": "White receipt scene",
                    "visual_start_s": 0.0,
                    "subtitles": [{"start_s": 0.0, "end_s": 1.0, "text": "第一场景字幕"}],
                },
                {
                    "scene_id": "s2",
                    "visual": artifact_reference(vis, "visual"),
                    "provenance": artifact_reference(prov, "prov"),
                    "semantic_description": "Scale motion scene",
                    "visual_start_s": 0.0,
                    "scale_motion": {"targets": [{"start_s": 0.1, "end_s": 0.5, "rect": [160, 90, 1600, 900]}]},
                    "subtitles": [{"start_s": 1.0, "end_s": 2.0, "text": "第二场景字幕"}],
                },
            ],
        }
        bp.write_text(json.dumps(binding), encoding="utf-8")

        with patch(
            "video_creation_pipeline.audio_timeline.probe_media_metadata",
            return_value={"streams": [{"codec_type": "video", "duration": "2"}], "format": {"duration": "2"}},
        ):
            _, base_rows = audio_timeline.prepare(pp, bp)
            self.assertEqual(base_rows[0][0]["subtitle_preset_id"], "stroke_shadow_default")
            self.assertEqual(base_rows[0][0]["subtitle_font_px"], 59)
            self.assertTrue(base_rows[1][0]["scale_motion"]["events"])

            # Changing Scene 2 font invalidates ONLY Scene 2.
            binding["segments"][1]["subtitle_style"] = {"font_id": "alibaba_puhuiti_55_regular"}
            bp.write_text(json.dumps(binding), encoding="utf-8")
            _, font_rows = audio_timeline.prepare(pp, bp)
            self.assertEqual(base_rows[0][0]["cache_key"], font_rows[0][0]["cache_key"])
            self.assertNotEqual(base_rows[1][0]["cache_key"], font_rows[1][0]["cache_key"])

            # Changing Scene 2 box opacity invalidates ONLY Scene 2.
            binding["segments"][1]["subtitle_style"] = {"preset_id": "accessible_box_a", "background_opacity": 0.72}
            bp.write_text(json.dumps(binding), encoding="utf-8")
            _, op_rows = audio_timeline.prepare(pp, bp)
            self.assertEqual(base_rows[0][0]["cache_key"], op_rows[0][0]["cache_key"])
            self.assertNotEqual(font_rows[1][0]["cache_key"], op_rows[1][0]["cache_key"])

    def test_step3_remotion_landscape_and_portrait_font_materialization(self) -> None:
        skills = self.root / "skills"
        skills.mkdir()
        (skills / "package.json").write_text(json.dumps({"name": "@remotion/skills", "version": "4.0.513"}), encoding="utf-8")
        for rel in (
            "skills/remotion-best-practices/SKILL.md",
            "skills/remotion-markup/SKILL.md",
            "skills/remotion-interactivity/SKILL.md",
            "skills/remotion-render/SKILL.md",
            "skills/remotion-studio/SKILL.md",
            "skills/remotion-captions/SKILL.md",
        ):
            tf = skills / rel
            tf.parent.mkdir(parents=True, exist_ok=True)
            tf.write_text("ok", encoding="utf-8")
        runtime = self.root / "runtime"
        for name, pkg in (("core", "remotion"), ("cli", "@remotion/cli")):
            pf = runtime / "packages" / name / "package.json"
            pf.parent.mkdir(parents=True, exist_ok=True)
            pf.write_text(json.dumps({"name": pkg, "version": "4.0.477"}), encoding="utf-8")
        (runtime / "LICENSE.md").write_text("license", encoding="utf-8")
        srt = self.root / "subs.srt"
        srt.write_text("1\n00:00:00,000 --> 00:00:01,000\n采访者：[门铃声] 原件送到了。\n", encoding="utf-8")

        for label, (w, h) in (("landscape", (1920, 1080)), ("portrait", (1080, 1920))):
            pkg_plan = self.root / f"pkg-{label}.json"
            pkg_plan.write_text(
                json.dumps(
                    {
                        "schema": "video_creation_pipeline.video_packaging_plan.v1",
                        "output": {"width": w, "height": h, "fps": 30},
                        "scenes": [{"scene_id": "s1", "start_s": 0.0, "end_s": 1.0}],
                        "status": "candidate_only",
                    }
                ),
                encoding="utf-8",
            )
            with (
                patch("video_creation_pipeline.remotion_skill_handoff._git_head", return_value=REMOTION_SKILLS_COMMIT),
                patch("video_creation_pipeline.remotion_project._git_head", return_value=REMOTION_RUNTIME_COMMIT),
            ):
                handoff = build_remotion_skill_handoff(
                    skills,
                    pkg_plan,
                    composition_id="SubTest",
                    subtitle_path=srt,
                )
                hp = write_remotion_skill_handoff(handoff, self.root / f"handoff-{label}.json")
                proj = self.root / f"proj-{label}"
                paths = materialize_remotion_project(hp, runtime, proj, license_basis="individual")
                manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
                validate_remotion_project_manifest(manifest)
                self.assertEqual(manifest["subtitle_materialization"]["font_id"], "dengxian_bold_static")
                self.assertTrue((proj / "public" / "fonts" / "Dengb.ttf").is_file())
                data = json.loads(paths["data"].read_text(encoding="utf-8"))
                self.assertEqual(data["subtitleStyle"]["fontPx"], 59)
                self.assertEqual(data["subtitleStyle"]["backgroundColor"], "transparent")
                self.assertIn("FontFace", _CAPTIONS_TSX)
                self.assertIn("inline-block", _CAPTIONS_TSX)
                self.assertIn("subtitleStyle={data.subtitleStyle ?? null}", _COMPOSITION_TSX)


if __name__ == "__main__":
    unittest.main()
