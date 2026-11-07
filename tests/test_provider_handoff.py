from __future__ import annotations

import hashlib
import json
import shutil
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from video_creation_pipeline.provider_cli import main as provider_cli_main  # noqa: E402
from video_creation_pipeline.provider_handoff import (  # noqa: E402
    ContractError,
    build_provider_request,
    validate_provider_receipt,
    validate_provider_request,
)
from video_creation_pipeline.vkp_bridge import build_edit_job, write_edit_job  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "vkp_bundle"


class ProviderHandoffTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = ROOT / ".test-runtime" / self._testMethodName
        if self.root.exists():
            shutil.rmtree(self.root)
        self.root.mkdir(parents=True)
        self.bundle = self.root / "bundle"
        shutil.copytree(FIXTURE, self.bundle)
        self.media = self.root / "talking-head.mp4"
        self.media.write_bytes(b"test-video-bytes")
        self.edit_job_path = self.root / "video-creation" / "edit-job.json"
        job = build_edit_job(self.bundle, remote_policy=self._remote_policy())
        write_edit_job(job, self.edit_job_path)
        self.selection_path = self.root / "video-creation" / "human-selection.json"
        self._write_selection()

    def tearDown(self) -> None:
        if self.root.exists():
            shutil.rmtree(self.root)

    def test_builds_chatcut_request_from_human_confirmed_candidates(self) -> None:
        request = build_provider_request(self.edit_job_path, self.selection_path)

        self.assertEqual(request["provider"], "chatcut")
        self.assertEqual(request["task"], "editable_timeline_finishing")
        self.assertEqual(len(request["timeline_items"]), 1)
        self.assertEqual(
            request["timeline_items"][0]["selection_status"], "human_confirmed"
        )
        self.assertFalse(request["provider_instructions"]["flatten_output"])
        self.assertFalse(request["provider_instructions"]["run_provider_asr"])
        self.assertFalse(request["operator_boundary"]["network_execution_performed"])
        self.assertTrue(request["source_edit_job"]["vkp_remains_authoritative"])

    def test_selection_must_bind_exact_job_and_known_candidate(self) -> None:
        selection = json.loads(self.selection_path.read_text(encoding="utf-8"))
        selection["edit_job_sha256"] = "0" * 64
        self.selection_path.write_text(json.dumps(selection), encoding="utf-8")
        with self.assertRaisesRegex(ContractError, "exact edit job"):
            build_provider_request(self.edit_job_path, self.selection_path)

        self._write_selection(candidate_id="unknown")
        with self.assertRaisesRegex(ContractError, "unknown candidate"):
            build_provider_request(self.edit_job_path, self.selection_path)

        self._write_selection()
        selection = json.loads(self.selection_path.read_text(encoding="utf-8"))
        selection["selections"][0]["confirmed_interval"] = {"start": 0, "end": 2}
        self.selection_path.write_text(json.dumps(selection), encoding="utf-8")
        with self.assertRaisesRegex(ContractError, "boundary refinement"):
            build_provider_request(self.edit_job_path, self.selection_path)

    def test_request_fields_cannot_drift_from_authorized_edit_job(self) -> None:
        request = build_provider_request(self.edit_job_path, self.selection_path)
        request["upload_manifest"]["artifacts"][0]["purpose"] = "different-purpose"
        request["request_sha256"] = payload_sha256(request)

        with self.assertRaisesRegex(ContractError, "upload_manifest differs"):
            validate_provider_request(request)

    def test_validates_hashed_candidate_receipt_and_rejects_policy_breaches(
        self,
    ) -> None:
        request = build_provider_request(self.edit_job_path, self.selection_path)
        output = self.root / "provider-output.json"
        output.write_text('{"timeline":"candidate"}', encoding="utf-8")
        receipt = self._receipt(request, output)

        result = validate_provider_receipt(request, receipt)

        self.assertTrue(result["valid"])
        self.assertEqual(result["next_action"], "human_review")

        receipt["published"] = True
        with self.assertRaisesRegex(ContractError, "nothing was published"):
            validate_provider_receipt(request, receipt)

        receipt = self._receipt(request, output)
        receipt["usage"]["calls"] = 9
        with self.assertRaisesRegex(ContractError, "call limit"):
            validate_provider_receipt(request, receipt)

        bundle_output = self.bundle / "provider-output.json"
        bundle_output.write_text('{"timeline":"candidate"}', encoding="utf-8")
        receipt = self._receipt(request, bundle_output)
        with self.assertRaisesRegex(ContractError, "outside the read-only VKP Bundle"):
            validate_provider_receipt(request, receipt)

    def test_cli_builds_request_and_verifies_receipt_without_network(self) -> None:
        request_path = self.root / "video-creation" / "provider-request.json"
        code = provider_cli_main(
            [
                "build-request",
                "--edit-job",
                str(self.edit_job_path),
                "--selection",
                str(self.selection_path),
                "--output",
                str(request_path),
            ]
        )
        self.assertEqual(code, 0)
        request = json.loads(request_path.read_text(encoding="utf-8"))
        output = self.root / "provider-output.json"
        output.write_text('{"timeline":"candidate"}', encoding="utf-8")
        receipt_path = self.root / "provider-receipt.json"
        receipt_path.write_text(
            json.dumps(self._receipt(request, output)), encoding="utf-8"
        )

        verify_code = provider_cli_main(
            [
                "verify-receipt",
                "--request",
                str(request_path),
                "--receipt",
                str(receipt_path),
            ]
        )

        self.assertEqual(verify_code, 0)

    def _remote_policy(self) -> dict:
        return {
            "schema": "video_creation_pipeline.remote_execution_policy.v2",
            "enabled": True,
            "provider": "chatcut",
            "tasks": ["editable_timeline_finishing"],
            "route": {"id": "chatcut-mcp", "revision": "2026-07-15"},
            "authorization": {
                "kind": "provider_mcp_consent",
                "reference": "chatcut-plugin-session-test",
                "operator_confirmed": True,
                "confirmed_at": "2026-07-15T12:00:00+00:00",
            },
            "limits": {"max_calls": 8, "max_cost": 20, "currency": "CNY"},
            "upload": {
                "allowed": True,
                "artifacts": [
                    {"path": str(self.media), "purpose": "editable_timeline_finishing"}
                ],
            },
        }

    def _write_selection(self, candidate_id: str = "timeline-0001") -> None:
        selection = {
            "schema": "video_creation_pipeline.human_selection.v1",
            "edit_job_sha256": file_sha256(self.edit_job_path),
            "selections": [{"candidate_id": candidate_id}],
            "operator_confirmation": {
                "confirmed": True,
                "confirmed_at": "2026-07-15T12:05:00+00:00",
                "method": "visible_operator_selection",
            },
        }
        self.selection_path.parent.mkdir(parents=True, exist_ok=True)
        self.selection_path.write_text(json.dumps(selection), encoding="utf-8")

    def _receipt(self, request: dict, output: Path) -> dict:
        return {
            "schema": "video_creation_pipeline.provider_receipt.v1",
            "request_id": request["request_id"],
            "request_sha256": request["request_sha256"],
            "provider": request["provider"],
            "route": request["route"],
            "task": request["task"],
            "status": "completed",
            "published": False,
            "fallback_used": False,
            "usage": {"calls": 1, "cost": 1.0, "currency": "CNY"},
            "outputs": [
                {
                    "path": str(output),
                    "bytes": output.stat().st_size,
                    "sha256": file_sha256(output),
                    "role": request["result_role"],
                    "acceptance_status": "candidate_only",
                }
            ],
        }


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def payload_sha256(payload: dict) -> str:
    value = dict(payload)
    value.pop("request_sha256", None)
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


if __name__ == "__main__":
    unittest.main()
