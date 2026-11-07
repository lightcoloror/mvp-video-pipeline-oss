from __future__ import annotations

# ruff: noqa: E402

import json
import shutil
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from video_creation_pipeline.contract_io import atomic_copy_promote, atomic_write_json
from video_creation_pipeline.errors import ContractError


class ContractIoSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = ROOT / ".test-runtime" / self._testMethodName
        if self.root.exists():
            shutil.rmtree(self.root)
        self.root.mkdir(parents=True)

    def tearDown(self) -> None:
        if self.root.exists():
            shutil.rmtree(self.root)

    def test_contract_cannot_overwrite_a_bound_artifact(self) -> None:
        source = self.root / "source.json"
        source.write_text(json.dumps({"source": True}), encoding="utf-8")
        payload = {"schema": "fixture.v1", "artifact": {"path": str(source)}}

        with self.assertRaisesRegex(ContractError, "cannot overwrite"):
            atomic_write_json(payload, source)

        self.assertEqual(
            json.loads(source.read_text(encoding="utf-8")), {"source": True}
        )

    def test_atomic_copy_promotion_keeps_staging_and_replaces_output(self) -> None:
        staged = self.root / "staged.mp4"
        output = self.root / "candidate.mp4"
        staged.write_bytes(b"candidate-v2")
        output.write_bytes(b"candidate-v1")

        atomic_copy_promote(staged, output, label="candidate")

        self.assertEqual(staged.read_bytes(), b"candidate-v2")
        self.assertEqual(output.read_bytes(), b"candidate-v2")

    def test_failed_atomic_copy_promotion_preserves_previous_output(self) -> None:
        staged = self.root / "staged.mp4"
        output = self.root / "candidate.mp4"
        staged.write_bytes(b"candidate-v2")
        output.write_bytes(b"candidate-v1")

        with patch(
            "video_creation_pipeline.contract_io.shutil.copyfile",
            side_effect=OSError("synthetic copy failure"),
        ):
            with self.assertRaisesRegex(ContractError, "atomic promotion failed"):
                atomic_copy_promote(staged, output, label="candidate")

        self.assertEqual(output.read_bytes(), b"candidate-v1")
        self.assertFalse(any(path.suffix == ".tmp" for path in self.root.iterdir()))

    def test_atomic_copy_promotion_rejects_same_path(self) -> None:
        staged = self.root / "staged.mp4"
        staged.write_bytes(b"candidate")

        with self.assertRaisesRegex(ContractError, "must be distinct"):
            atomic_copy_promote(staged, staged, label="candidate")


if __name__ == "__main__":
    unittest.main()
