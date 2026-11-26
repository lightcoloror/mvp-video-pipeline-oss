"""Tests for post-cut QA gates: semantic A-roll anchor alignment, typo/split-word audit,
retake duplicate detection, 25ms audio cut-edge tail-swallow check, and B-roll role validation.
"""

from __future__ import annotations

import unittest
import numpy as np

from video_creation_pipeline.errors import ContractError
from video_creation_pipeline.post_cut_qa import (
    audit_audio_cut_edges,
    audit_script_entities_and_trigrams,
    audit_subtitles_against_script,
    detect_retake_duplicates,
    extract_and_verify_rendered_mp4_anchors,
    extract_fact_entities,
    realign_overlays_to_locked_asr,
    validate_broll_asset_role,
)


class PostCutQAGatesTests(unittest.TestCase):
    def test_realign_overlays_to_locked_asr_follows_trimmed_timeline(self) -> None:
        asr = [
            {"start_s": 0.5, "end_s": 3.5, "text": "公开参考视频提前归档拉片"},
            {"start_s": 4.0, "end_s": 8.0, "text": "拍讲解竖屏时直接开个提词器网页"},
            {"start_s": 8.5, "end_s": 12.0, "text": "配合OBS一次就能连人带画面录完"},
        ]
        specs = [
            {"overlay_id": "archive_dl", "start_anchor": "公开参考视频", "end_anchor": "归档拉片", "track": "main", "enforce_non_overlap": True},
            {"overlay_id": "obs_dual", "start_anchor": "开个提词器网页", "end_anchor": "连人带画面录完", "track": "main", "enforce_non_overlap": True},
        ]
        res = realign_overlays_to_locked_asr(specs, asr, total_duration_s=15.0)
        self.assertEqual(len(res), 2)
        self.assertLess(res[0]["resolved_start_s"], 1.5)
        self.assertGreater(res[1]["resolved_start_s"], 5.0)
        self.assertLess(res[0]["resolved_end_s"], res[1]["resolved_start_s"])

        with self.assertRaises(ContractError):
            realign_overlays_to_locked_asr(
                [{"overlay_id": "missing", "start_anchor": "完全不存在的口播句子", "duration_s": 2.0}],
                asr,
                total_duration_s=15.0,
            )

    def test_audit_subtitles_catches_homophones_split_words_and_pause_punct(self) -> None:
        bad_cues = [
            {"start_s": 0.0, "end_s": 1.2, "text": "这里是唱歌，静静的边学边做之美"},
            {"start_s": 1.3, "end_s": 2.4, "text": "吐出来的东西只"},
            {"start_s": 2.5, "end_s": 3.6, "text": "能当个大概参考"},
            {"start_s": 3.7, "end_s": 5.0, "text": "新手金条或者编程入手"},
        ]
        report = audit_subtitles_against_script(bad_cues, script_text="这里是示例频道的《边做边学·自媒体》亲手精调编程助手")
        self.assertFalse(report["passed"])
        kinds = {x["kind"] for x in report["issues"]}
        self.assertIn("homophone_typo", kinds)
        self.assertIn("split_compound_word", kinds)
        self.assertIn("pause_punctuation", kinds)

        clean_cues = [
            {"start_s": 0.0, "end_s": 1.6, "text": "这里是示例频道的《边做边学·自媒体》"},
            {"start_s": 1.7, "end_s": 3.2, "text": "吐出来的东西 只能当大概参考"},
            {"start_s": 3.3, "end_s": 4.8, "text": "亲手精调或者编程助手替代"},
        ]
        clean_rep = audit_subtitles_against_script(clean_cues, max_units=19.0)
        self.assertTrue(clean_rep["passed"], clean_rep["issues"])

    def test_detect_retake_duplicates_flags_untrimmed_retakes(self) -> None:
        asr = [
            {"start_s": 10.0, "end_s": 12.5, "text": "或者出门远控电脑的时候"},
            {"start_s": 12.6, "end_s": 15.2, "text": "或者出门远程控制电脑的时候就让AI生成网页"},
        ]
        dups = detect_retake_duplicates(asr, min_ngram=4)
        self.assertGreaterEqual(len(dups), 1)
        self.assertIn("或者出门", dups[0]["phrase"])

    def test_audit_audio_cut_edges_detects_tail_swallow(self) -> None:
        sr = 16000
        # 2s signal: silence 0..0.3, loud tone 0.3..1.4, silence 1.4..2.0
        sig = np.zeros(sr * 2, dtype=np.float32)
        sig[int(0.3 * sr) : int(1.4 * sr)] = 0.2
        good = audit_audio_cut_edges(sig, sr, [(0.25, 1.45)], max_edge_db=-40.0)
        self.assertTrue(good["passed"])
        swallowed = audit_audio_cut_edges(sig, sr, [(0.25, 1.20)], max_edge_db=-40.0)
        self.assertFalse(swallowed["passed"])
        self.assertEqual(swallowed["hot_edges"][0]["edge"], "tail")

    def test_validate_broll_asset_role_accepts_clean_chart(self) -> None:
        chart = np.full((600, 900, 3), 240, dtype=np.uint8)
        res = validate_broll_asset_role(chart, role="evidence_document")
        self.assertTrue(res["passed"])

    def test_audit_script_entities_and_trigrams_catches_numeric_fact_drift_and_homophones(self) -> None:
        script = "之前跑通一个项目时清过一次两 G 的废缓存，按氛围挑选授权 BGM，横屏改竖屏，找到更好的替代模块。"
        bad_cues = [
            {"start_s": 1.0, "end_s": 3.0, "text": "之前跑通一个项目时两盘备份"},
            {"start_s": 3.1, "end_s": 5.0, "text": "按分为挑选授权BDM横屏改触屏"},
            {"start_s": 5.1, "end_s": 7.0, "text": "找到更好的替代文块"},
        ]
        rep = audit_script_entities_and_trigrams(bad_cues, script_text=script)
        self.assertFalse(rep["passed"])
        missing = {(x["category"], x["entity"]) for x in rep["missing_entities"]}
        unexpected = {(x["category"], x["entity"]) for x in rep["unexpected_entities"]}
        self.assertIn(("capacities", "2G"), missing)
        self.assertIn(("ascii_terms", "BGM"), missing)
        self.assertIn(("ascii_terms", "BDM"), unexpected)
        self.assertGreaterEqual(len(rep["trigram_anomalies"]), 2)

        good_cues = [
            {"start_s": 1.0, "end_s": 3.0, "text": "之前跑通一个项目时清过一次两 G 的废缓存"},
            {"start_s": 3.1, "end_s": 5.0, "text": "按氛围挑选授权 BGM 横屏改竖屏"},
            {"start_s": 5.1, "end_s": 7.0, "text": "找到更好的替代模块"},
        ]
        clean = audit_script_entities_and_trigrams(good_cues, script_text=script)
        self.assertTrue(clean["passed"], clean)

    def test_extract_and_verify_rendered_mp4_anchors_closed_loop(self) -> None:
        import tempfile
        from pathlib import Path
        from unittest import mock
        import cv2

        with tempfile.TemporaryDirectory() as td:
            td_p = Path(td)
            fake_mp4 = td_p / "demo.mp4"
            fake_mp4.write_bytes(b"mp4")

            def _fake_ffmpeg(cmd, **_kwargs):
                out_jpg = Path(cmd[-1])
                img = np.full((1920, 1080, 3), 235, dtype=np.uint8)
                cv2.rectangle(img, (114, 18), (310, 68), (26, 25, 23), -1)
                cv2.putText(img, "AI", (140, 56), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (250, 250, 250), 3)
                cv2.rectangle(img, (80, 1450), (1000, 1620), (20, 20, 20), -1)
                cv2.putText(img, "SUBTITLE", (180, 1560), cv2.FONT_HERSHEY_SIMPLEX, 2.0, (250, 250, 250), 4)
                cv2.imwrite(str(out_jpg), img)
                return mock.Mock(returncode=0, stderr="")

            with mock.patch("subprocess.run", side_effect=_fake_ffmpeg):
                receipt = extract_and_verify_rendered_mp4_anchors(
                    fake_mp4,
                    resolved_overlays=[
                        {"overlay_id": "ev1", "kind": "anim_evidence_zoom", "resolved_start_s": 1.0, "resolved_end_s": 3.0}
                    ],
                    cues=[{"start_s": 0.5, "end_s": 3.5, "text": "资料图先看全貌再放大"}],
                    output_dir=td_p / "qa",
                    require_ai_disclosure_badge=True,
                    ocr_fn=lambda _bgr, _roi: "资料图先看全貌再放大",
                )
            self.assertTrue(receipt["passed"], receipt)
            self.assertTrue((td_p / "qa" / "mp4_anchor_verification_receipt.json").exists())
            self.assertTrue((td_p / "qa" / "mp4_anchor_contact_sheet.png").exists())


if __name__ == "__main__":
    unittest.main()
