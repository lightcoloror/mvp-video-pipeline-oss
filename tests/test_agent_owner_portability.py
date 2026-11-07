from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import shutil
import socket
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "owner_manifest_doctor.py"
SPEC = importlib.util.spec_from_file_location("mvp_video_owner_manifest_doctor", SCRIPT)
assert SPEC and SPEC.loader
DOCTOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DOCTOR)
TOOL_ID = "mvp-video-pipeline"


class AgentOwnerPortabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        managed = os.environ.get("TEST_TMP_ROOT")
        parent = (
            Path(managed).resolve()
            if managed
            else ROOT / ".test-runtime" / "agent-owner-portability"
        )
        parent.mkdir(parents=True, exist_ok=True)
        self.temp = Path(
            tempfile.mkdtemp(prefix=f"mvp-owner-{uuid4().hex}-", dir=parent)
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.temp, ignore_errors=True)

    def _relocate(self) -> Path:
        target = self.temp / "relocated"
        manifest = json.loads((ROOT / DOCTOR.MANIFEST_NAME).read_text(encoding="utf-8"))
        paths = {DOCTOR.MANIFEST_NAME}
        for group in ("no_write_checks", "runtime_checks"):
            paths.update(
                item["path"] for item in manifest["provenance"][group] if "path" in item
            )
        for relative in paths:
            source = ROOT / relative
            destination = target / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
        return target

    def _receipt(self, parent: Path, checked_at: datetime | None = None) -> Path:
        parent.mkdir(parents=True, exist_ok=True)
        artifact = parent / "synthetic-result.json"
        artifact.write_text('{"candidate_only":true}\n', encoding="utf-8")
        receipt = parent / "receipt.json"
        payload = {
            "schema_version": DOCTOR.RECEIPT_SCHEMA,
            "tool_id": TOOL_ID,
            "status": "passed",
            "checked_at": (checked_at or datetime.now(timezone.utc)).isoformat(),
            "artifact": {"path": artifact.name, "sha256": DOCTOR.sha256_file(artifact)},
            "truth": {
                "fresh_e2e_pass": True,
                "real_usage_proof": False,
                "current_online": False,
            },
        }
        receipt.write_text(json.dumps(payload), encoding="utf-8")
        return receipt

    @staticmethod
    def _hash_tree(root: Path) -> dict[str, str]:
        return {
            path.relative_to(root).as_posix(): DOCTOR.sha256_file(path)
            for path in root.rglob("*")
            if path.is_file()
        }

    def test_relocated_doctor_is_no_write_offline_and_idempotent(self) -> None:
        root = self._relocate()
        before = self._hash_tree(root)
        with (
            mock.patch.object(
                socket, "socket", side_effect=AssertionError("network forbidden")
            ),
            mock.patch.object(
                DOCTOR.shutil, "which", side_effect=lambda name: f"tools/{name}"
            ),
        ):
            first = DOCTOR.doctor(root, TOOL_ID)
            second = DOCTOR.doctor(root, TOOL_ID)
        self.assertEqual(first, second)
        self.assertEqual(before, self._hash_tree(root))
        self.assertEqual(first["status"], "ready_metadata_only")
        self.assertTrue(first["truth"]["contract_ready"]["value"])
        self.assertTrue(first["truth"]["runtime_ready"]["value"])
        self.assertIsNone(first["truth"]["fresh_e2e_pass"]["value"])
        self.assertFalse(first["truth"]["real_usage_proof"]["value"])
        self.assertFalse(first["truth"]["current_online"]["value"])
        self.assertFalse(first["runtime_probe_performed"])
        self.assertFalse(first["network_performed"])
        self.assertFalse(first["write_performed"])

    def test_missing_runtime_is_reported_without_fallback(self) -> None:
        root = self._relocate()
        with mock.patch.object(DOCTOR.shutil, "which", return_value=None):
            result = DOCTOR.doctor(root, TOOL_ID)
        self.assertEqual(result["status"], "blocked_runtime")
        self.assertIn("ffmpeg", result["failures"]["runtime"])
        self.assertIn("ffprobe", result["failures"]["runtime"])
        self.assertFalse(result["silent_fallback"])

    def test_current_receipt_passes_but_stale_future_and_hash_drift_fail_closed(
        self,
    ) -> None:
        root = self._relocate()
        with mock.patch.object(
            DOCTOR.shutil, "which", side_effect=lambda name: f"tools/{name}"
        ):
            current = self._receipt(self.temp / "current")
            self.assertTrue(
                DOCTOR.doctor(root, TOOL_ID, current)["truth"]["fresh_e2e_pass"][
                    "value"
                ]
            )

            stale = self._receipt(
                self.temp / "stale", datetime.now(timezone.utc) - timedelta(hours=169)
            )
            stale_truth = DOCTOR.doctor(root, TOOL_ID, stale)["truth"]["fresh_e2e_pass"]
            self.assertFalse(stale_truth["value"])
            self.assertIn("receipt_stale", stale_truth["reason_codes"])

            future = self._receipt(
                self.temp / "future", datetime.now(timezone.utc) + timedelta(minutes=10)
            )
            future_truth = DOCTOR.doctor(root, TOOL_ID, future)["truth"][
                "fresh_e2e_pass"
            ]
            self.assertFalse(future_truth["value"])
            self.assertIn("receipt_future_timestamp", future_truth["reason_codes"])

            (current.parent / "synthetic-result.json").write_text(
                "drift", encoding="utf-8"
            )
            drift_truth = DOCTOR.doctor(root, TOOL_ID, current)["truth"][
                "fresh_e2e_pass"
            ]
            self.assertFalse(drift_truth["value"])
            self.assertIn("artifact_hash_drift", drift_truth["reason_codes"])

    def test_corrupt_manifest_path_escape_and_structured_error_fail_closed(
        self,
    ) -> None:
        root = self._relocate()
        manifest_path = root / DOCTOR.MANIFEST_NAME
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["executionAuthority"] = "execute"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(DOCTOR.DoctorError, "execute"):
            DOCTOR.doctor(root, TOOL_ID)
        with self.assertRaisesRegex(DOCTOR.DoctorError, "escape"):
            DOCTOR.resolve_inside(root, "../escape")
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            exit_code = DOCTOR.main(["--root", str(root), "--tool-id", TOOL_ID])
        error = json.loads(stderr.getvalue())
        self.assertEqual(exit_code, 2)
        self.assertEqual(error["schema_version"], "agent-tool-owner-doctor-error.v1")
        self.assertEqual(error["error"]["code"], "manifest_execution_authority_invalid")
        self.assertFalse(error["network_performed"])
        self.assertFalse(error["write_performed"])

    def test_synthetic_example_is_hash_bound_and_recovery_fixture_is_complete(
        self,
    ) -> None:
        request = json.loads(
            (
                ROOT / "examples" / "agent-owner-discovery" / "synthetic-request.json"
            ).read_text(encoding="utf-8")
        )
        self.assertFalse(request["execute"])
        self.assertTrue(request["expected_boundary"]["candidate_only"])
        self.assertFalse(request["expected_boundary"]["network"])
        self.assertFalse(request["expected_boundary"]["upload"])
        self.assertFalse(request["expected_boundary"]["publish"])
        for binding in request["input_bindings"]:
            self.assertEqual(
                DOCTOR.sha256_file(ROOT / binding["path"]), binding["sha256"]
            )

        recovery = json.loads(
            (
                ROOT / "examples" / "agent-owner-discovery" / "recovery-scenarios.json"
            ).read_text(encoding="utf-8")
        )
        codes = {item["code"] for item in recovery["scenarios"]}
        self.assertTrue(
            {
                "missing_file",
                "runtime_missing",
                "receipt_stale",
                "receipt_future_timestamp",
                "artifact_hash_drift",
                "real_media_not_proved",
                "current_online_not_probed",
                "external_action_requested",
            }.issubset(codes)
        )


if __name__ == "__main__":
    unittest.main()
