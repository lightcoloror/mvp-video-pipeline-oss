from __future__ import annotations

# ruff: noqa: E402

import math
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from video_creation_pipeline.errors import ContractError
from video_creation_pipeline.reframe_tracking import smooth_crop_keyframes


def _frame(time_s: float, x: float) -> dict[str, object]:
    return {
        "time_s": time_s,
        "crop": {"x": x, "y": 0.0, "width": 0.3, "height": 1.0},
        "evidence_ids": [f"e-{time_s}"],
        "motion_reason": "fixture",
    }


class SmartReframeTemporalTests(unittest.TestCase):
    def test_deadband_holds_small_motion(self) -> None:
        rows = smooth_crop_keyframes([_frame(0, 0.2), _frame(0.2, 0.21)])
        self.assertEqual(rows[0]["crop"], rows[1]["crop"])
        self.assertEqual("deadband_hold", rows[1]["motion_reason"])

    def test_speed_is_limited(self) -> None:
        rows = smooth_crop_keyframes(
            [_frame(0, 0.0), _frame(0.1, 0.6)],
            deadband=0,
            minimum_dwell_s=0,
            max_speed_per_s=0.2,
            max_acceleration_per_s2=1.0,
        )
        left = rows[0]["crop"]
        right = rows[1]["crop"]
        distance = math.dist(
            (left["x"] + left["width"] / 2, left["y"] + left["height"] / 2),
            (right["x"] + right["width"] / 2, right["y"] + right["height"] / 2),
        )
        self.assertLessEqual(distance, 0.0200001)

    def test_invalid_temporal_configuration_fails_closed(self) -> None:
        with self.assertRaises(ContractError):
            smooth_crop_keyframes([_frame(0, 0.2)], max_speed_per_s=0)


if __name__ == "__main__":
    unittest.main()
