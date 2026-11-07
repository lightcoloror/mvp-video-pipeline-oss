from __future__ import annotations

import importlib.util
import json
import shutil
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "ffmpeg_outlet.py"
SPEC = importlib.util.spec_from_file_location("ffmpeg_outlet_cli", SCRIPT)
assert SPEC and SPEC.loader
CLI = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CLI)


class FfmpegOutletCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime = ROOT / ".test-runtime" / f"ffmpeg-outlet-cli-{uuid4().hex}"
        self.runtime.mkdir(parents=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.runtime, ignore_errors=True)

    def test_cli_forwards_job_to_canonical_outlet(self) -> None:
        source = self.runtime / "synthetic-source.mp4"
        output = self.runtime / "scan.log"
        receipt = output.parent / "receipt.json"
        source.write_bytes(b"synthetic")

        def fake_execute(operation, command, inputs, outputs, receipt_path, **kwargs):
            self.assertEqual(operation, "technical_qa")
            self.assertEqual(command[0], "ffmpeg")
            self.assertEqual(inputs[0]["role"], "media")
            self.assertEqual(outputs[0]["role"], "detector_log")
            self.assertEqual(kwargs["capture_log_path"], output)
            self.assertTrue(kwargs["guard_resource"])
            return {
                "receipt_sha256": "a" * 64,
                "resource_guard": {"policy_id": "video-review-resource-guard-v1"},
            }

        argv = [
            str(SCRIPT),
            "--operation",
            "technical_qa",
            "--input",
            f"media={source}",
            "--output",
            f"detector_log={output}",
            "--receipt",
            str(receipt),
            "--capture-log",
            str(output),
            "--",
            "ffmpeg",
            "-i",
            str(source),
            "-f",
            "null",
            "-",
        ]
        with (
            patch.object(sys, "argv", argv),
            patch.object(CLI, "execute_ffmpeg_with_receipt", side_effect=fake_execute),
            patch("builtins.print") as printed,
        ):
            self.assertEqual(CLI.main(), 0)
        payload = json.loads(printed.call_args.args[0])
        self.assertEqual(payload["status"], "completed")


if __name__ == "__main__":
    unittest.main()
