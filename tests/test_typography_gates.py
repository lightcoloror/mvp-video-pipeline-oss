# ruff: noqa: E402

from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from video_creation_pipeline.case_cover_visual_plan import (
    case_cover_variant_projection,
    validate_case_cover_variant_projection,
)
from video_creation_pipeline.errors import ContractError
from video_creation_pipeline.remotion_project import (
    _CAPTIONS_TSX,
    _COMPOSITION_TSX,
    _COVER_TSX,
)
from video_creation_pipeline.typography_gates import (
    build_typography_gate,
    validate_typography_gate,
)
from video_creation_pipeline.semantic_motion import _motion_label_font_px


class TypographyGateTests(unittest.TestCase):
    """Focused positive/negative checks for the shared, fail-closed gate."""

    def test_short_motion_labels_grow_above_floor_without_shrinking_long_text(self):
        gate = build_typography_gate("semantic_motion_1080x1920", width=1080, height=1920)
        cue = {"label": {"text": "短结论", "rect": {"width": .7, "height": .08}}}
        self.assertGreater(_motion_label_font_px(cue, 1080, 1920, gate), gate["roles"]["motion_label"])
        cue["label"]["text"] = "很长的文字" * 30
        self.assertEqual(_motion_label_font_px(cue, 1080, 1920, gate), gate["roles"]["motion_label"])

    def test_policy_binds_cover_motion_and_content_profiles(self) -> None:
        cover = build_typography_gate(
            "vertical_profile_3_4", width=1080, height=1440
        )
        motion = build_typography_gate(
            "semantic_motion_1080x1920", width=1080, height=1920
        )
        social = build_typography_gate(
            "social_card_1080x1440", width=1080, height=1440
        )
        mobile = build_typography_gate("article_html_mobile", width=390, height=844)

        self.assertEqual(144, cover["roles"]["overlay"])
        self.assertEqual(36, motion["roles"]["motion_label"])
        self.assertEqual(60, social["roles"]["primary_headline"])
        self.assertEqual(17, mobile["roles"]["body"])
        self.assertTrue(all(gate["hard_gate"] for gate in (cover, motion, social, mobile)))

    def test_missing_low_or_stale_binding_fails_closed(self) -> None:
        gate = build_typography_gate(
            "vertical_profile_3_4", width=1080, height=1440
        )
        with self.assertRaisesRegex(ContractError, "required"):
            validate_typography_gate(
                None, profile_id="vertical_profile_3_4", width=1080, height=1440
            )
        low = copy.deepcopy(gate)
        low["roles"]["overlay"] = 72
        with self.assertRaisesRegex(ContractError, "drifted"):
            validate_typography_gate(
                low, profile_id="vertical_profile_3_4", width=1080, height=1440
            )
        with self.assertRaisesRegex(ContractError, "requires"):
            build_typography_gate("vertical_profile_3_4", width=1080, height=1920)
        with self.assertRaisesRegex(ContractError, "requires 1080x1920"):
            build_typography_gate(
                "semantic_motion_1080x1920", width=720, height=1280
            )

    def test_case_cover_projection_carries_and_checks_exact_gate(self) -> None:
        variants = case_cover_variant_projection(self._case_cover_plan(), safe_layout=None)
        self.assertEqual(1, len(variants))
        variant = variants[0]
        self.assertEqual(
            144, variant["caseCover"]["typography"]["roles"]["overlay"]
        )
        validate_case_cover_variant_projection(variant)

        missing = copy.deepcopy(variant)
        del missing["caseCover"]["typography"]
        with self.assertRaisesRegex(ContractError, "required"):
            validate_case_cover_variant_projection(missing)
        low = copy.deepcopy(variant)
        low["caseCover"]["typography"]["roles"]["structure"] = 8
        with self.assertRaisesRegex(ContractError, "drifted"):
            validate_case_cover_variant_projection(low)

    def test_remotion_case_cover_never_goes_below_bound_minima(self) -> None:
        self.assertIn('fontSize: actual("overlay", overlayPreferred)', _COVER_TSX)
        self.assertIn("Math.max(minimum(role), preferred)", _COVER_TSX)
        self.assertIn("const length = Array.from(variant.overlayText)", _COVER_TSX)
        self.assertIn('type("structure", 64 * scale)', _COVER_TSX)

    def test_remotion_captions_bind_an_adaptive_size_above_minimum(self) -> None:
        self.assertIn(
            "Math.max(minimumFontSize, regionFontSize - lengthPenalty)",
            _CAPTIONS_TSX,
        )
        self.assertNotIn("Math.max(24, Math.min(42", _CAPTIONS_TSX)
        self.assertIn(
            "minimumFontSize={data.subtitleTypography.roles.subtitle}",
            _COMPOSITION_TSX,
        )
        self.assertIn("typography={data.motionTypography}", _COMPOSITION_TSX)

    @staticmethod
    def _case_cover_plan() -> dict:
        return {
            "candidate": {
                "candidate_id": "fixture-cover",
                "strategy": "evidence_led",
                "title": "标题",
                "overlay_text": "被遗忘的复查",
                "promise": "候选说明",
            },
            "visual_route": {
                "primary_reading_target": "复查节点",
                "composition_strategy": "typography_only",
            },
            "render_spec": {
                "kind": "structure",
                "graphic_kind": "calendar_reminder",
                "tag": "事实",
                "helper_labels": ["到时间"],
                "profile_layouts": {
                    "vertical_profile_3_4": {
                        "text_box": {"x": 0.08, "y": 0.10, "width": 0.84, "height": 0.30},
                        "text_align": "left",
                        "graphic_box": {"x": 0.08, "y": 0.48, "width": 0.84, "height": 0.32},
                        "media": [],
                    }
                },
            },
            "profiles": [
                {
                    "profile_id": "vertical_profile_3_4",
                    "width": 1080,
                    "height": 1440,
                    "safe_zone": {"x": 0.05, "y": 0.05, "width": 0.90, "height": 0.90},
                }
            ],
            "brand": {"account": "fixture", "palette": {}},
        }


if __name__ == "__main__":
    unittest.main()
