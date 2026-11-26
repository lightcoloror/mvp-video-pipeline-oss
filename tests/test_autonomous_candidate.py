from __future__ import annotations

import shutil
import sys
import unittest
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from video_creation_pipeline.autonomous_candidate import (  # noqa: E402
    DEFAULT_CLOSURE_RUN_ID,
    DEFAULT_FIXTURE_ATTESTATION,
    _build_receipt,
    _load_completed_receipt,
    _prepare_output_root,
    _srt_time,
    _validate_fixture_attestation,
    validate_autonomous_candidate_run,
)
from video_creation_pipeline.contract_io import (  # noqa: E402
    artifact_reference,
    atomic_write_json,
    payload_sha256,
)
from video_creation_pipeline.errors import ContractError  # noqa: E402


class AutonomousCandidateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = ROOT / ".test-runtime" / f"autonomous-candidate-{uuid4().hex}"
        self.root.mkdir(parents=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_public_synthetic_attestation_rejects_customer_claim_drift(self) -> None:
        _validate_fixture_attestation(DEFAULT_FIXTURE_ATTESTATION)
        drifted = self.root / "fixture-attestation.json"
        drifted.write_text(
            DEFAULT_FIXTURE_ATTESTATION.read_text(encoding="utf-8").replace(
                '"contains_customer_media": false',
                '"contains_customer_media": true',
            ),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ContractError, "attestation drifted"):
            _validate_fixture_attestation(drifted)

    def test_unmanaged_nonempty_output_root_is_preserved_and_rejected(self) -> None:
        output = self.root / "unmanaged"
        sentinel = output / "user-media-sentinel.mp4"
        sentinel.parent.mkdir()
        sentinel.write_bytes(b"do-not-overwrite")

        with self.assertRaisesRegex(ContractError, "non-empty and unmanaged"):
            _prepare_output_root(output, DEFAULT_CLOSURE_RUN_ID)

        self.assertEqual(sentinel.read_bytes(), b"do-not-overwrite")

    def test_completed_receipt_resumes_only_when_every_artifact_is_intact(self) -> None:
        output = self.root / "managed"
        _prepare_output_root(output, DEFAULT_CLOSURE_RUN_ID)
        files = {
            "authoritative/input-manifest.json": b"{}\n",
            "authoritative/semantic-motion-plan.json": b"{}\n",
            "render-work/hyperframes-project/semantic-motion-run-receipt.json": b"{}\n",
            "candidate/candidate.mp4": b"synthetic-video-bytes",
            "candidate/captions.srt": b"1\n00:00:00,000 --> 00:00:01,000\nfixture\n",
            "qa/qa-report.json": b"{}\n",
            "review/review-packet.json": b"{}\n",
            "lifecycle/media-manifest.json": b"{}\n",
        }
        for relative, content in files.items():
            path = output / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        receipt = _build_receipt(
            output,
            DEFAULT_CLOSURE_RUN_ID,
            output / "candidate/candidate.mp4",
            output / "authoritative/semantic-motion-plan.json",
            output / "render-work/hyperframes-project/semantic-motion-run-receipt.json",
            output / "candidate/captions.srt",
            output / "qa/qa-report.json",
            output / "review/review-packet.json",
        )
        receipt["lifecycle_manifest"] = artifact_reference(
            output / "lifecycle/media-manifest.json", "media lifecycle manifest"
        )
        receipt["run_receipt_sha256"] = payload_sha256(receipt, "run_receipt_sha256")
        receipt_path = output / "receipts/run-receipt.json"
        atomic_write_json(receipt, receipt_path)

        self.assertEqual(
            validate_autonomous_candidate_run(receipt_path)["status"],
            "completed_candidate_only",
        )
        self.assertEqual(
            _load_completed_receipt(output, DEFAULT_CLOSURE_RUN_ID)["disposition"],
            "resumed_verified",
        )

        (output / "candidate/captions.srt").write_text("drift\n", encoding="utf-8")
        with self.assertRaisesRegex(ContractError, "captions changed"):
            _load_completed_receipt(output, DEFAULT_CLOSURE_RUN_ID)

    def test_srt_time_is_stable_at_frame_adjacent_boundaries(self) -> None:
        self.assertEqual(_srt_time(0.0), "00:00:00,000")
        self.assertEqual(_srt_time(5.82), "00:00:05,820")
        self.assertEqual(_srt_time(3661.001), "01:01:01,001")


if __name__ == "__main__":
    unittest.main()
