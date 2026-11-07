from __future__ import annotations

import json
import shutil
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from video_creation_pipeline.privacy_gate_cli import main


class PrivacyGateCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = (
            Path(__file__).resolve().parents[1]
            / ".test-runtime"
            / f"privacy-gate-cli-{uuid4().hex}"
        )
        self.root.mkdir(parents=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    @patch("video_creation_pipeline.privacy_gate_cli.run_redaction")
    def test_redact_run_forwards_explicit_coordinate_capture(self, run) -> None:
        run.return_value = {"status": "candidate_only"}
        receipt = self.root / "receipt.json"

        code = main(
            [
                "redact-run",
                "--job",
                str(self.root / "job.json"),
                "--receipt",
                str(receipt),
                "--capture-zones",
            ]
        )

        self.assertEqual(0, code)
        self.assertTrue(run.call_args.kwargs["capture_zones"])

    @patch("video_creation_pipeline.privacy_gate_cli.write_privacy_exclusion_zones")
    @patch("video_creation_pipeline.privacy_gate_cli.build_privacy_exclusion_zones")
    @patch("video_creation_pipeline.privacy_gate_cli.read_detection_capture")
    @patch("video_creation_pipeline.privacy_gate_cli.validate_redaction_receipt")
    def test_redact_zones_consumes_only_receipt_bound_capture(
        self, validate_receipt, read_capture, build_zones, write_zones
    ) -> None:
        source = self.root / "source.mp4"
        redacted = self.root / "redacted.mp4"
        job = self.root / "job.json"
        capture = self.root / "capture.jsonl"
        for path in (source, redacted, job, capture):
            path.write_bytes(b"fixture")
        receipt = self.root / "receipt.json"
        receipt.write_text(
            json.dumps(
                {
                    "job": {"path": str(job)},
                    "input": {"path": str(source)},
                    "output": {"path": str(redacted)},
                    "stages": [
                        {
                            "coordinate_capture": {"path": str(capture)},
                            "same_invocation_as_redaction": True,
                        }
                    ],
                    "ffprobe": {
                        "input": {
                            "format": {"duration": "1.0"},
                            "streams": [
                                {
                                    "codec_type": "video",
                                    "width": 640,
                                    "height": 360,
                                    "avg_frame_rate": "30/1",
                                    "duration": "1.0",
                                    "nb_frames": "30",
                                }
                            ],
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        zones = {
            "coverage": {"status": "complete"},
            "zones_sha256": "a" * 64,
        }
        read_capture.return_value = [{"frame_index": 0}]
        build_zones.return_value = zones
        output = self.root / "zones.json"
        write_zones.return_value = output

        def artifact_path(reference, _label):
            return Path(reference["path"])

        with patch(
            "video_creation_pipeline.privacy_gate_cli.validate_artifact_reference",
            side_effect=artifact_path,
        ):
            code = main(
                [
                    "redact-zones",
                    "--receipt",
                    str(receipt),
                    "--output",
                    str(output),
                ]
            )

        self.assertEqual(0, code)
        validate_receipt.assert_called_once()
        read_capture.assert_called_once_with([capture])
        self.assertEqual(640, build_zones.call_args.kwargs["media_clock"]["width"])
        write_zones.assert_called_once_with(zones, output)

    def test_redact_zones_rejects_two_projection_authorities(self) -> None:
        code = main(
            [
                "redact-zones",
                "--receipt",
                str(self.root / "receipt.json"),
                "--output",
                str(self.root / "zones.json"),
                "--reframe-plan",
                str(self.root / "reframe.json"),
                "--identity-final-canvas",
            ]
        )

        self.assertEqual(2, code)


if __name__ == "__main__":
    unittest.main()
