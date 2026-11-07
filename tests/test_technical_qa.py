from __future__ import annotations

# ruff: noqa: E402

import json
import shutil
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from video_creation_pipeline.contract_io import payload_sha256
from video_creation_pipeline.errors import ContractError
from video_creation_pipeline.execution_receipts import (
    build_ffmpeg_execution_receipt,
    write_execution_contract,
)
from video_creation_pipeline.technical_qa import (
    build_technical_qa_report,
    parse_ffmpeg_detector_log,
    validate_technical_qa_report,
    write_technical_qa_report,
)


class TechnicalQaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = ROOT / ".test-runtime" / self._testMethodName
        if self.root.exists():
            shutil.rmtree(self.root)
        self.root.mkdir(parents=True)

    def tearDown(self) -> None:
        if self.root.exists():
            shutil.rmtree(self.root)

    def test_passes_objective_checks_without_granting_release(self) -> None:
        report = self._report(
            "black_duration:0.2 silence_duration:1.0 freeze_duration:6"
        )
        output = self.root / "technical-qa.json"
        write_technical_qa_report(report, output)

        self.assertTrue(output.is_file())
        self.assertEqual(report["readiness"]["status"], "passed")
        self.assertTrue(report["readiness"]["technical_ready"])
        self.assertFalse(report["readiness"]["release_eligible"])
        self.assertEqual(report["readiness"]["warnings"], ["freeze_ratio"])
        self.assertFalse(report["authority_boundary"]["automatic_publish"])

    def test_black_ratio_is_a_blocking_failure(self) -> None:
        report = self._report("black_duration:1.5 silence_duration:1.0")

        self.assertEqual(report["readiness"]["status"], "failed")
        self.assertIn("black_ratio", report["readiness"]["blocking_failures"])
        self.assertEqual(report["readiness"]["next_action"], "fix_technical_failures")

    def test_bound_detector_log_cannot_change_after_report(self) -> None:
        report = self._report("black_duration:0.1 silence_duration:0.5")
        detector = Path(report["inputs"]["detector_log"]["path"])
        detector.write_text("black_duration:4.0", encoding="utf-8")
        report["report_sha256"] = payload_sha256(report, "report_sha256")

        with self.assertRaisesRegex(ContractError, "changed after"):
            validate_technical_qa_report(report)

    def test_detector_parser_caps_accumulated_duration(self) -> None:
        metrics = parse_ffmpeg_detector_log(
            "black_duration:8 black_duration:8 silence_duration:2",
            10.0,
        )

        self.assertEqual(metrics["black_seconds"], 10.0)
        self.assertEqual(metrics["black_ratio"], 1.0)
        self.assertEqual(metrics["silence_ratio"], 0.2)

    def test_accepts_canonical_packaging_start_s_end_s(self) -> None:
        report = self._report("black_duration:0.1 silence_duration:0.5")
        packaging_path = Path(report["inputs"]["packaging_plan"]["path"])
        packaging_path.write_text(
            json.dumps({"scenes": [{"start_s": 0.0, "end_s": 10.0}]}),
            encoding="utf-8",
        )
        receipt_path = Path(report["inputs"]["ffmpeg_receipt"]["path"])
        probe_path = Path(report["inputs"]["probe"]["path"])
        media_path = Path(report["inputs"]["media"]["path"])
        subtitles_path = Path(report["inputs"]["subtitles"]["path"])

        canonical_report = build_technical_qa_report(
            media_path,
            probe_path,
            receipt_path,
            subtitles_path=subtitles_path,
            packaging_plan_path=packaging_path,
            expected_duration_s=10.0,
        )

        self.assertTrue(canonical_report["readiness"]["technical_ready"])
        packaging_check = next(
            row for row in canonical_report["checks"] if row["id"] == "packaging_plan"
        )
        self.assertEqual(packaging_check["actual"]["invalid_scene_count"], 0)

    def _report(self, detector_text: str) -> dict:
        media = self.root / "final.mp4"
        media.write_bytes(b"media-fixture")
        probe = self._json(
            "probe.json",
            {
                "streams": [
                    {
                        "codec_type": "video",
                        "codec_name": "h264",
                        "width": 1280,
                        "height": 720,
                        "avg_frame_rate": "30/1",
                        "pix_fmt": "yuv420p",
                    },
                    {
                        "codec_type": "audio",
                        "codec_name": "aac",
                        "sample_rate": "48000",
                        "channels": 2,
                    },
                ],
                "format": {"duration": "10.0"},
            },
        )
        detector = self.root / "detector.log"
        detector.write_text(detector_text, encoding="utf-8")
        command = [
            "ffmpeg",
            "-i",
            str(media),
            "-vf",
            "blackdetect,freezedetect",
            "-af",
            "silencedetect",
            "-f",
            "null",
            "-",
        ]
        receipt = build_ffmpeg_execution_receipt(
            "technical_qa",
            command,
            [{"role": "media", "path": media}],
            [{"role": "detector_log", "path": detector}],
        )
        receipt_path = self.root / "ffmpeg-receipt.json"
        write_execution_contract(receipt, receipt_path)
        subtitles = self.root / "subtitles.srt"
        subtitles.write_text(
            "1\n00:00:00,000 --> 00:00:03,000\nOpening\n\n"
            "2\n00:00:03,000 --> 00:00:09,900\nEvidence\n",
            encoding="utf-8",
        )
        packaging = self._json(
            "packaging.json",
            {"scenes": [{"start": 0.0, "end": 10.0}]},
        )
        return build_technical_qa_report(
            media,
            probe,
            receipt_path,
            subtitles_path=subtitles,
            packaging_plan_path=packaging,
            expected_duration_s=10.0,
        )

    def _json(self, name: str, payload: dict) -> Path:
        path = self.root / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path


if __name__ == "__main__":
    unittest.main()
