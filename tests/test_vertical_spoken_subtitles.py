"""Tests for vertical spoken-caption punctuation & anti-orphan/anti-flash best practice."""

from __future__ import annotations

import unittest

from video_creation_pipeline.subtitle_readability import (
    clean_vertical_spoken_punctuation,
    split_vertical_spoken_cues,
    units,
)


class VerticalSpokenSubtitlesTests(unittest.TestCase):
    def test_clean_vertical_spoken_punctuation_strips_pause_keeps_semantic_and_data(self) -> None:
        self.assertEqual(
            clean_vertical_spoken_punctuation("刷信息流，经常看到两种套路。"),
            "刷信息流 经常看到两种套路",
        )
        self.assertEqual(
            clean_vertical_spoken_punctuation("真的不好用吗？太夸张了！看《边做边学》"),
            "真的不好用吗？太夸张了！看《边做边学》",
        )
        self.assertEqual(
            clean_vertical_spoken_punctuation("3:4、9:16、16:9，提速 1.14×，占盘 94.44 GB；"),
            "3:4 9:16 16:9 提速 1.14× 占盘 94.44 GB",
        )
        self.assertEqual(
            clean_vertical_spoken_punctuation("提到某个 [已消音]，地方的时候。"),
            "提到某个 [已消音] 地方的时候",
        )

    def test_split_vertical_spoken_cues_prevents_orphans_and_preserves_char_timestamps(self) -> None:
        raw = [
            {
                "start_s": 1.0,
                "end_s": 5.0,
                "text": "好不容易把字幕配图花字动效全部对齐了。",
                "char_timestamps": [(1.0 + i * 0.22, 1.0 + (i + 1) * 0.22) for i in range(18)],
            },
            {
                "start_s": 5.1,
                "end_s": 5.3,
                "text": "对的。",
            },
        ]
        out = split_vertical_spoken_cues(raw, max_units=13.0, min_units=4.0, min_duration_s=0.48)
        self.assertGreaterEqual(len(out), 2)
        for c in out:
            self.assertGreaterEqual(units(c["text"]), 4.0)
            self.assertNotIn("。", c["text"])
            self.assertNotIn("，", c["text"])
            self.assertGreaterEqual(c["end_s"] - c["start_s"], 0.45)

    def test_no_merge_boundary_protects_speed_gag_and_censor_beats(self) -> None:
        raw = [
            {"start_s": 10.0, "end_s": 11.4, "text": "比如说我现在就说的非常的慢。", "no_merge": True},
            {"start_s": 11.45, "end_s": 11.75, "text": "音量", "no_merge": False},
            {"start_s": 11.8, "end_s": 13.0, "text": "忽大忽小自动补平。", "no_merge": False},
        ]
        out = split_vertical_spoken_cues(raw, max_units=13.0, min_units=4.0, min_duration_s=0.48)
        self.assertEqual(out[0]["text"], "比如说我现在就说的非常的慢")
        self.assertNotIn("音量", out[0]["text"])


if __name__ == "__main__":
    unittest.main()
