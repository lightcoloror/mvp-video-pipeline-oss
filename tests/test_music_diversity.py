from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from video_creation_pipeline.errors import ContractError
from video_creation_pipeline.music_diversity import (
    build_music_batch_alignment_report,
    build_music_batch_diversity_report,
)


class MusicBatchDiversityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.assets = {
            "clear-a": {"sha256": "a" * 64, "duration_s": 60.0},
            "clear-alias": {"sha256": "a" * 64, "duration_s": 60.0},
            "clear-b": {"sha256": "b" * 64, "duration_s": 60.0},
        }

    def test_empty_alignment_batch_is_rejected(self) -> None:
        with self.assertRaisesRegex(ContractError, "at least one plan"):
            build_music_batch_alignment_report({}, {})

    def test_nonfinite_audit_values_are_rejected(self) -> None:
        plan = {"video-a": self._plan("clear-a", 0.0, 12.0)}
        for value in (float("nan"), float("inf"), float("-inf")):
            for field in (
                "max_shared_body_source_s",
                "brand_motif_max_s",
                "comparison_horizon_s",
            ):
                with (
                    self.subTest(value=value, field=field),
                    self.assertRaisesRegex(ContractError, "finite"),
                ):
                    build_music_batch_diversity_report(
                        plan, self.assets, **{field: value}
                    )
            with (
                self.subTest(value=value, field="asset_duration"),
                self.assertRaisesRegex(ContractError, "finite"),
            ):
                build_music_batch_diversity_report(
                    plan, {"clear-a": {"sha256": "a" * 64, "duration_s": value}}
                )

    def test_aliases_with_the_same_passage_are_blocked(self) -> None:
        report = build_music_batch_diversity_report(
            {
                "video-a": self._plan("clear-a", 8.0, 24.0),
                "video-b": self._plan("clear-alias", 8.0, 24.0),
            },
            self.assets,
        )
        self.assertEqual(report["status"], "blocked_repeated_source_passage")
        self.assertEqual(report["readiness"]["violation_count"], 1)
        self.assertFalse(report["policy"]["asset_aliases_count_as_diversity"])

    def test_non_overlapping_windows_of_one_source_are_distinct(self) -> None:
        report = build_music_batch_diversity_report(
            {
                "video-a": self._plan("clear-a", 0.0, 12.0),
                "video-b": self._plan("clear-alias", 24.0, 12.0),
            },
            self.assets,
        )
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["findings"], [])

    def test_distinct_audio_hashes_are_distinct(self) -> None:
        report = build_music_batch_diversity_report(
            {
                "video-a": self._plan("clear-a", 0.0, 24.0),
                "video-b": self._plan("clear-b", 0.0, 24.0),
            },
            self.assets,
        )
        self.assertEqual(report["status"], "passed")

    def test_explicit_short_brand_motif_may_repeat(self) -> None:
        report = build_music_batch_diversity_report(
            {
                "video-a": self._plan("clear-a", 0.0, 5.0, brand_motif=True),
                "video-b": self._plan("clear-alias", 0.0, 5.0, brand_motif=True),
            },
            self.assets,
            brand_motif_max_s=5.0,
        )
        self.assertEqual(report["status"], "passed")
        self.assertTrue(report["findings"][0]["allowed_brand_motif"])

    def test_long_brand_motif_is_rejected(self) -> None:
        with self.assertRaisesRegex(ContractError, "brand motif exceeds"):
            build_music_batch_diversity_report(
                {"video-a": self._plan("clear-a", 0.0, 8.0, brand_motif=True)},
                self.assets,
                brand_motif_max_s=5.0,
            )

    def test_looped_aliases_with_the_same_initial_phase_are_blocked(self) -> None:
        report = build_music_batch_diversity_report(
            {
                "video-a": self._plan("clear-a", 0.0, 30.0, loop=True),
                "video-b": self._plan("clear-alias", 0.0, 30.0, loop=True),
            },
            self.assets,
        )
        self.assertEqual(report["status"], "blocked_repeated_source_passage")
        self.assertEqual(report["violations"][0]["shared_source_s"], 24.0)

    def test_looped_aliases_with_different_initial_phases_may_rotate(self) -> None:
        report = build_music_batch_diversity_report(
            {
                "video-a": self._plan("clear-a", 0.0, 20.0, loop=True),
                "video-b": self._plan("clear-alias", 24.0, 20.0, loop=True),
            },
            self.assets,
            comparison_horizon_s=20.0,
        )
        self.assertEqual(report["status"], "passed")
        self.assertEqual(len(report["asset_reuse_groups"]), 1)

    def test_asset_family_must_match_the_semantic_family(self) -> None:
        aligned = build_music_batch_alignment_report(
            {
                "video-a": {
                    "layers": [
                        {
                            **self._plan("clear-a", 0.0, 10.0)["layers"][0],
                            "segment_id": "segment-a",
                            "music_family": {"family_id": "clear_explainer"},
                        }
                    ]
                }
            },
            {"clear-a": "clear_explainer"},
        )
        self.assertEqual(aligned["status"], "passed")

        mismatched = build_music_batch_alignment_report(
            {
                "video-a": {
                    "layers": [
                        {
                            **self._plan("clear-a", 0.0, 10.0)["layers"][0],
                            "segment_id": "segment-a",
                            "music_family": "restrained_risk",
                        }
                    ]
                }
            },
            {"clear-a": "clear_explainer"},
        )
        self.assertEqual(mismatched["status"], "blocked_family_mismatch")
        self.assertFalse(mismatched["readiness"]["candidate_render_allowed"])

    def _plan(
        self,
        asset_id: str,
        source_start_s: float,
        duration_s: float,
        *,
        brand_motif: bool = False,
        loop: bool = False,
    ) -> dict:
        return {
            "layers": [
                {
                    "asset_id": asset_id,
                    "source_start_s": source_start_s,
                    "duration_s": duration_s,
                    "loop": loop,
                    "semantic_role": "opening_clear"
                    if brand_motif
                    else "clear_explainer_body",
                    "brand_motif": brand_motif,
                }
            ]
        }


if __name__ == "__main__":
    unittest.main()
