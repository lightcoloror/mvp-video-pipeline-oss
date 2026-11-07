from __future__ import annotations

import json
import shutil
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from video_creation_pipeline._sentryblur_runner import (
    _install_coordinate_capture,
    _promote_capture,
)


class SentryBlurRunnerCaptureTests(unittest.TestCase):
    def test_wrapper_records_without_changing_detector_result(self) -> None:
        root = ROOT / ".test-runtime" / f"runner-capture-{uuid4().hex}"
        output = root / "capture.jsonl"

        class FaceDetector:
            def detect(self, frame: object) -> list[list[float]]:
                return [[10.0, 20.0, 50.0, 80.0, 0.91]]

        class PlateDetector:
            def detect(self, frame: object) -> list[list[float]]:
                return []

        detectors = types.ModuleType("sentryblur.detectors")
        detectors.SCRFDFaceDetector = FaceDetector
        detectors.OpenImagePlateDetector = PlateDetector
        frame = types.SimpleNamespace(shape=(100, 200, 3))
        with patch.dict(sys.modules, {"sentryblur.detectors": detectors}):
            rows, path = _install_coordinate_capture(target="face", output_path=output)
            result = FaceDetector().detect(frame)
            self.assertEqual([[10.0, 20.0, 50.0, 80.0, 0.91]], result)
            _promote_capture(rows, path)
        captured = json.loads(output.read_text(encoding="utf-8").strip())
        self.assertEqual([10.0, 20.0, 50.0, 80.0], captured["xyxy"])
        self.assertEqual(0, captured["frame_index"])
        self.assertEqual("face", captured["target_type"])
        self.assertEqual(
            (200, 100), (captured["frame_width"], captured["frame_height"])
        )
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
