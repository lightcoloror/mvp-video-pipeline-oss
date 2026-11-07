from __future__ import annotations

# ruff: noqa: E402

import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import video_creation_pipeline.contract_io as contract_io_module

from video_creation_pipeline.contract_io import artifact_reference
from video_creation_pipeline.errors import ContractError
from video_creation_pipeline.execution_receipts import (
    build_ffmpeg_execution_receipt,
    execute_ffmpeg_frame_series_with_receipt,
)


class FfmpegFrameSeriesReceiptTests(unittest.TestCase):
    def setUp(self) -> None:
        managed = os.environ.get("TEST_TMP_ROOT")
        parent = Path(managed).resolve() if managed else ROOT / ".test-runtime"
        self.root = parent / f"ffmpeg-frame-series-{uuid4().hex}"
        self.root.mkdir(parents=True, exist_ok=True)
        self.source = self.root / "source.mp4"
        self.source.write_bytes(b"synthetic source")
        self.pattern = self.root / "frames" / "frame-%06d.jpg"
        self.receipt = self.root / "receipt.json"

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    @patch("video_creation_pipeline.execution_receipts.shutil.which")
    @patch("video_creation_pipeline.execution_receipts.subprocess.run")
    def test_inventory_only_includes_numbered_frame_outputs(
        self,
        run_mock,
        which_mock,
    ) -> None:
        which_mock.return_value = "ffmpeg"

        def complete_run(*_args, **_kwargs):
            self.pattern.parent.mkdir(parents=True, exist_ok=True)
            (self.pattern.parent / "frame-000002.jpg").write_bytes(b"frame 2")
            (self.pattern.parent / "frame-000001.jpg").write_bytes(b"frame 1")
            (self.pattern.parent / "unrelated.json").write_text("{}", encoding="utf-8")
            return subprocess.CompletedProcess([], 0, stdout="", stderr="ok")

        run_mock.side_effect = complete_run
        log_path = self.pattern.parent / "ffmpeg.log"
        receipt, frames = execute_ffmpeg_frame_series_with_receipt(
            ["ffmpeg", "-i", str(self.source), str(self.pattern)],
            [{"role": "source", "path": self.source}],
            self.pattern,
            self.receipt,
            media_time_step_s=2.0,
            capture_log_path=log_path,
            resource_snapshot_provider=lambda: {
                "cpu_percent": 10.0,
                "available_memory_bytes": 16 * 1024**3,
                "available_memory_percent": 50.0,
                "disk_busy_percent": 5.0,
            },
            resource_lock_path=self.root / "test-slot.lock",
        )

        self.assertEqual(
            [path.name for path in frames],
            ["frame-000001.jpg", "frame-000002.jpg"],
        )
        self.assertEqual(
            [Path(row["artifact"]["path"]).name for row in receipt["outputs"]],
            ["frame-000001.jpg", "frame-000002.jpg"],
        )
        self.assertTrue(log_path.is_file())

    def test_rejects_pattern_drift_and_existing_frames(self) -> None:
        with self.assertRaisesRegex(ContractError, "must match"):
            execute_ffmpeg_frame_series_with_receipt(
                ["ffmpeg", "-i", str(self.source), str(self.root / "other-%06d.jpg")],
                [{"role": "source", "path": self.source}],
                self.pattern,
                self.receipt,
                media_time_step_s=1.0,
            )

        self.pattern.parent.mkdir(parents=True, exist_ok=True)
        (self.pattern.parent / "frame-000001.jpg").write_bytes(b"stale")
        with self.assertRaisesRegex(ContractError, "already contains"):
            execute_ffmpeg_frame_series_with_receipt(
                ["ffmpeg", "-i", str(self.source), str(self.pattern)],
                [{"role": "source", "path": self.source}],
                self.pattern,
                self.receipt,
                media_time_step_s=1.0,
            )


class SuppliedArtifactReferenceValidationTests(unittest.TestCase):
    """Content validation of caller-supplied artifact references.

    A caller supplies an already computed artifact reference so the outlet can
    avoid recomputing it. The receipt builder must still content-verify those
    bytes; accepting the reference from metadata alone would let a same-size,
    well-formed but forged SHA-256 digest through.
    """

    def setUp(self) -> None:
        managed = os.environ.get("TEST_TMP_ROOT")
        parent = Path(managed).resolve() if managed else ROOT / ".test-runtime"
        self.root = parent / f"supplied-references-{uuid4().hex}"
        self.root.mkdir(parents=True, exist_ok=True)
        self.source = self.root / "source.mp4"
        self.source.write_bytes(b"synthetic source")
        self.detector_log = self.root / "detectors.log"
        self.detector_log.write_text("black_duration:0.1\n", encoding="utf-8")

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def _build(self, media_reference: dict) -> dict:
        return build_ffmpeg_execution_receipt(
            "technical_qa",
            ["ffmpeg", "-hide_banner", "-i", str(self.source), "-f", "null", "-"],
            [{"role": "media", "path": str(self.source), "artifact": media_reference}],
            [{"role": "detector_log", "path": str(self.detector_log)}],
        )

    def test_accepts_intact_supplied_reference_after_content_validation(self) -> None:
        reference = artifact_reference(self.source, "test media")
        receipt = self._build(reference)
        self.assertEqual(receipt["status"], "completed")
        self.assertEqual(receipt["inputs"][0]["artifact"], reference)

    def test_rejects_same_size_valid_shape_supplied_reference_with_wrong_sha(self) -> None:
        reference = artifact_reference(self.source, "test media")
        forged = dict(reference)
        forged["sha256"] = "0" * 64
        self.assertEqual(forged["bytes"], self.source.stat().st_size)
        self.assertRegex(forged["sha256"], r"^[0-9a-f]{64}$")

        reads: list[Path] = []
        original_hasher = contract_io_module.file_sha256

        def counting_hasher(path):
            reads.append(Path(path).resolve())
            return original_hasher(path)

        with patch.object(
            contract_io_module, "file_sha256", side_effect=counting_hasher
        ):
            with self.assertRaisesRegex(
                ContractError, "changed after the contract was created"
            ):
                self._build(forged)
        # The rejection must come from re-reading the supplied bytes, not from a
        # metadata/shape check: the forged row is well formed and same-sized.
        self.assertIn(self.source.resolve(), reads)


if __name__ == "__main__":
    unittest.main()
