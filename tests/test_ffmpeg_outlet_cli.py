from __future__ import annotations

import importlib.util
import json
import os
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
        self.runtime = Path(os.environ.get("TEST_TMP_ROOT", ROOT / ".test-runtime")) / f"ffmpeg-outlet-cli-{uuid4().hex}"
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

    def frame_argv(self, times):
        argv = [str(SCRIPT), '--operation', 'frame_extract', '--input',
                f'media={self.runtime / "synthetic.mp4"}', '--output',
                f'frame={self.runtime / "frame.jpg"}', '--receipt', str(self.runtime / 'receipt.json')]
        for value in times:
            argv += [f'--output-media-time-s={value}']
        return argv + ['--', 'ffmpeg', '-ss', '2', '-i', 'synthetic.mp4', 'frame.jpg']

    def test_frame_cli_forwards_validated_time(self):
        with patch.object(sys, 'argv', self.frame_argv(['2'])), \
             patch.object(CLI, 'probe_media_metadata', create=True, return_value={'format': {'duration':'5'}}), \
             patch.object(CLI, 'execute_ffmpeg_with_receipt', return_value={'receipt_sha256':'a'*64}) as execute, \
             patch('builtins.print'):
            self.assertEqual(CLI.main(), 0)
        self.assertEqual(execute.call_args.args[3][0]['media_time_s'], 2.0)
        self.assertEqual(execute.call_args.args[3][0]['role'], 'frame')

    def test_invalid_frame_times_never_execute_media(self):
        for times in ([], ['-1'], ['nan'], ['inf'], ['word'], ['5'], ['6'], ['1','2']):
            with self.subTest(times=times), \
                 patch.object(sys, 'argv', self.frame_argv(times)), \
                 patch.object(CLI, 'probe_media_metadata', create=True, return_value={'format': {'duration':'5'}}), \
                 patch.object(CLI, 'execute_ffmpeg_with_receipt') as execute, \
                 self.assertRaises((ValueError, SystemExit)):
                CLI.main()
            execute.assert_not_called()

    def test_time_rejected_for_non_frame_operation(self):
        argv = self.frame_argv(['2'])
        argv[2] = 'technical_qa'
        with patch.object(sys, 'argv', argv), patch.object(CLI, 'execute_ffmpeg_with_receipt') as execute, \
             self.assertRaises((ValueError, SystemExit)):
            CLI.main()
        execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
