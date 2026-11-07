from __future__ import annotations

import hashlib
import json
import shutil
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from video_creation_pipeline.vkp_bridge import (  # noqa: E402
    ContractError,
    build_edit_job,
    write_edit_job,
)

FIXTURE = ROOT / "tests" / "fixtures" / "vkp_bundle"


class RemoteAuthorizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = ROOT / ".test-runtime" / self._testMethodName
        if self.root.exists():
            shutil.rmtree(self.root)
        self.root.mkdir(parents=True)
        self.bundle = self.root / "bundle"
        shutil.copytree(FIXTURE, self.bundle)
        self.artifact = self.root / "online-input.json"
        self.artifact.write_text('{"prompt":"test"}', encoding="utf-8")
        self.consent_path = self.root / "model-connector-consent.json"
        self.consent_path.write_text(
            json.dumps(self._consent_payload(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        if self.root.exists():
            shutil.rmtree(self.root)

    def test_vkp_policy_derives_all_authority_from_consent_v2(self) -> None:
        job = build_edit_job(self.bundle, remote_policy=self._policy())
        remote = job["remote_capabilities"]

        self.assertEqual(job["schema"], "video_creation_pipeline.vkp_edit_job.v3")
        self.assertEqual(remote["provider"], "test-provider")
        self.assertEqual(remote["route"]["id"], "route-script-analysis")
        self.assertEqual(remote["limits"]["currency"], "USD")
        self.assertEqual(remote["authorization"]["kind"], "vkp_consent_v2")
        self.assertEqual(
            remote["authorization"]["reference_path"], str(self.consent_path.resolve())
        )
        self.assertFalse(remote["authorization"]["embedded_vkp_consent_copy"])
        self.assertNotIn("consent", remote)

    def test_vkp_policy_rejects_duplicate_authority_fields(self) -> None:
        policy = self._policy()
        policy["provider"] = "shadow-provider"

        with self.assertRaisesRegex(ContractError, "must derive"):
            build_edit_job(self.bundle, remote_policy=policy)

    def test_vkp_consent_derived_limits_cannot_be_rewritten(self) -> None:
        job = build_edit_job(self.bundle, remote_policy=self._policy())
        job["remote_capabilities"]["limits"]["max_calls"] = 99

        with self.assertRaisesRegex(ContractError, "allowance changed"):
            write_edit_job(job, self.root / "edit-job.json")

    def test_vkp_consent_and_artifact_are_rechecked_before_write(self) -> None:
        job = build_edit_job(self.bundle, remote_policy=self._policy())
        self.artifact.write_text('{"prompt":"changed"}', encoding="utf-8")

        with self.assertRaisesRegex(ContractError, "changed after authorization"):
            write_edit_job(job, self.root / "edit-job.json")

    def _policy(self) -> dict:
        return {
            "schema": "video_creation_pipeline.remote_execution_policy.v2",
            "enabled": True,
            "tasks": ["script_analysis"],
            "authorization": {
                "kind": "vkp_consent_v2",
                "consent_path": str(self.consent_path),
            },
        }

    def _consent_payload(self) -> dict:
        artifact = {
            "path": str(self.artifact.resolve()),
            "data_type": "text",
            "bytes": self.artifact.stat().st_size,
            "sha256": file_sha256(self.artifact),
        }
        deployment = {
            "provider": "test-provider",
            "model": "test-model",
            "base_url": "https://example.invalid/v1",
            "interface": "openai_compatible",
        }
        route = {
            "route_id": "route-script-analysis",
            "route_revision": "revision-001",
            "virtual_model": "vkp-test-script-analysis",
            "execution_location": "remote",
        }
        route["route_snapshot_sha256"] = json_sha256(
            {**route, "deployments": [deployment]}
        )
        manifest_hash = json_sha256([artifact])
        now = datetime.now(timezone.utc).replace(microsecond=0)
        payload = {
            "schema": "video_knowledge_pipeline.model_connector_consent.v2",
            "consent_id": "consent-test-v2",
            "status": "active",
            "user_confirmed_data_export": True,
            "task": "script_analysis",
            "model_type": "text",
            "provider": deployment,
            "authorized_deployments": [deployment],
            "route": route,
            "instructions": "analyze selected script evidence",
            "instructions_sha256": hashlib.sha256(
                b"analyze selected script evidence"
            ).hexdigest(),
            "artifacts": [artifact],
            "upload_manifest": {
                "schema": "video_knowledge_pipeline.model_upload_manifest.v1",
                "file_count": 1,
                "total_bytes": artifact["bytes"],
                "files": [artifact],
                "manifest_sha256": manifest_hash,
            },
            "operator_confirmation": {
                "confirmed": True,
                "confirmed_at": now.isoformat(),
                "confirmation_method": "visible_operator_shell",
                "exact_manifest_sha256": manifest_hash,
            },
            "scope": {
                "purpose": "test",
                "max_calls": 2,
                "max_estimated_cost_usd": 2.0,
                "max_cost_per_call_usd": 1.0,
                "total_bytes": artifact["bytes"],
                "allowed_data_types": ["text"],
            },
            "usage": {
                "calls_attempted": 0,
                "calls_completed": 0,
                "cost_committed_usd": 0.0,
                "cost_limit_exceeded": False,
            },
            "created_at": now.isoformat(),
            "expires_at": (now + timedelta(hours=24)).isoformat(),
            "consent_path": str(self.consent_path.resolve()),
        }
        payload["consent_sha256"] = json_sha256(payload)
        return payload


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def json_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


if __name__ == "__main__":
    unittest.main()
