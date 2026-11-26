from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from video_creation_pipeline.motion_still_qa import (  # noqa: E402
    build_still_qa_artifacts,
    parse_crop_spec,
    parse_strip_spec,
    validate_timeline_continuity,
)


class MotionStillQATests(unittest.TestCase):
    def test_timeline_continuity_passes_on_contiguous_segments(self):
        segments = [
            {"id": "s1", "start_frame": 0, "duration_frames": 30, "anchor_frames": [0, 15]},
            {"id": "s2", "start_frame": 30, "duration_frames": 60, "anchor_frames": [45]},
            {"id": "s3", "start_frame": 90, "duration_frames": 30},
        ]
        report = validate_timeline_continuity(
            segments, fps=30, expected_total_frames=120, beat_frames=15
        )
        self.assertTrue(report["passed"])
        self.assertEqual(120, report["total_frames"])
        self.assertEqual([], report["errors"])

    def test_timeline_continuity_fails_closed_on_gap_overlap_and_total_mismatch(self):
        segments = [
            {"id": "s1", "start_frame": 0, "duration_frames": 30},
            {"id": "s2", "start_frame": 35, "duration_frames": 30},  # gap of 5
            {"id": "s3", "start_frame": 60, "duration_frames": 30, "anchor_frames": [95]},  # overlap + bad anchor
        ]
        report = validate_timeline_continuity(segments, fps=30, expected_total_frames=100)
        self.assertFalse(report["passed"])
        codes = {err["code"] for err in report["errors"]}
        self.assertIn("timeline_gap", codes)
        self.assertIn("timeline_overlap", codes)
        self.assertIn("anchor_out_of_segment_bounds", codes)
        self.assertIn("total_frames_mismatch", codes)

    def test_still_inspect_generates_1to1_crops_and_motion_strip(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            frames_dir = tmp_path / "frames"
            frames_dir.mkdir()
            for idx in range(6):
                img = Image.new("RGB", (1080, 1920), (idx * 35, 80, 160))
                img.save(frames_dir / f"f{idx:04d}.png")

            out_dir = tmp_path / "qa_out"
            receipt = build_still_qa_artifacts(
                output_dir=out_dir,
                frame_indices=parse_strip_spec("0:6:2"),
                frames_dir=frames_dir,
                crops=[parse_crop_spec("100,200,320,240")],
                make_strip=True,
                strip_cols=3,
                tile_width=270,
            )
            self.assertEqual([0, 2, 4], receipt["frames_checked"])
            self.assertEqual(3, len(receipt["crop_artifacts"]))
            crop_file = Path(receipt["crop_artifacts"][0]["path"])
            self.assertTrue(crop_file.exists())
            with Image.open(crop_file) as cim:
                self.assertEqual((320, 240), cim.size)  # strict 1:1 native pixel crop
            strip_file = Path(receipt["motion_strip_path"])
            self.assertTrue(strip_file.exists())
            with Image.open(strip_file) as sim:
                self.assertEqual(270 * 3, sim.size[0])
            self.assertTrue((out_dir / "still_qa_receipt.json").exists())

    def test_cli_timeline_check_exit_codes(self):
        with tempfile.TemporaryDirectory() as tmp:
            tl = Path(tmp) / "timeline.json"
            tl.write_text(
                json.dumps(
                    {
                        "fps": 30,
                        "expected_total_frames": 60,
                        "segments": [
                            {"id": "a", "start_frame": 0, "duration_frames": 30},
                            {"id": "b", "start_frame": 30, "duration_frames": 30},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            ok = subprocess.run(
                [sys.executable, str(ROOT / "scripts/motion_still_qa.py"), "timeline-check", "--timeline", str(tl)],
                capture_output=True,
                text=True,
            )
            self.assertEqual(0, ok.returncode, ok.stderr)

            bad = Path(tmp) / "bad.json"
            bad.write_text(
                json.dumps({"fps": 30, "segments": [{"id": "a", "start_frame": 0, "duration_frames": 20}, {"id": "b", "start_frame": 25, "duration_frames": 20}]}),
                encoding="utf-8",
            )
            fail = subprocess.run(
                [sys.executable, str(ROOT / "scripts/motion_still_qa.py"), "timeline-check", "--timeline", str(bad)],
                capture_output=True,
                text=True,
            )
            self.assertEqual(2, fail.returncode)


if __name__ == "__main__":
    unittest.main()
