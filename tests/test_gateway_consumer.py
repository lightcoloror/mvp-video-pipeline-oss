from __future__ import annotations

import base64
import copy
import importlib
import json
import shutil
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
GATEWAY_ROOT = ROOT.parents[1] / "model-provider-gateway"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# ruff: noqa: E402

from video_creation_pipeline.content_video_foundation import (
    build_doctor_report as build_content_doctor_report,
)
from video_creation_pipeline.contract_io import canonical_json_sha256, payload_sha256
from video_creation_pipeline.errors import ContractError
from video_creation_pipeline.gateway_consumer import (
    GATEWAY_CANONICAL_COMMIT,
    GATEWAY_SEMANTIC_REVISION,
    build_gateway_doctor_report,
    run_gateway_loopback,
    validate_gateway_candidate_receipt,
    verify_gateway_bundle,
)

PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class GatewayConsumerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = (
            ROOT / ".test-runtime" / f"gateway-{self._testMethodName}-{uuid4().hex}"
        )
        self.root.mkdir(parents=True)
        self.text_artifact = self.root / "synthetic-brief.json"
        self.text_payload = {
            "schema": "content-video-brief.v1",
            "title": "Synthetic Gateway fixture",
            "evidence_refs": ["source_fixture_primary"],
        }
        self._write_json(self.text_artifact, self.text_payload)
        self.image_artifact = self.root / "synthetic-frame.png"
        self.image_artifact.write_bytes(PNG_1X1)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    @patch("video_creation_pipeline.content_video_foundation._version_line")
    @patch("video_creation_pipeline.content_video_foundation.shutil.which")
    def test_doctor_uses_canonical_capability_truth(self, which, version_line) -> None:
        which.side_effect = lambda value: str(Path("tools") / f"{value}.exe")
        version_line.return_value = "synthetic version"

        gateway = build_gateway_doctor_report(GATEWAY_ROOT)
        content = build_content_doctor_report(gateway_root=GATEWAY_ROOT)
        unavailable = build_gateway_doctor_report(self.root / "missing-gateway")

        self.assertEqual(gateway["status"], "ready")
        self.assertEqual(gateway["canonical_commit"], GATEWAY_CANONICAL_COMMIT)
        self.assertEqual(gateway["package_version"], "0.3.0")
        self.assertEqual(len(gateway["package_commit"]), 40)
        self.assertFalse(gateway["release_identity_is_compatibility_gate"])
        self.assertEqual(
            gateway["semantic_contract"]["revision"], GATEWAY_SEMANTIC_REVISION
        )
        self.assertEqual(
            gateway["semantic_contract"]["http_api_schema"],
            "model_provider_gateway.http.v1",
        )
        self.assertEqual(
            set(gateway["schema_sha256"]),
            {"execution_request", "execution_result"},
        )
        statuses = {
            row["use_case"]: row["execution_status"] for row in gateway["capabilities"]
        }
        self.assertEqual(statuses["script"], "enabled")
        self.assertEqual(statuses["visual"], "enabled")
        for capability in ("tts", "asr", "ocr", "embedding"):
            self.assertEqual(statuses[capability], "blocked")
        self.assertEqual(content["status"], "ready")
        gateway_check = next(
            row for row in content["checks"] if row["id"] == "model_provider_gateway"
        )
        self.assertEqual(gateway_check["status"], "pass")
        self.assertEqual(unavailable["status"], "blocked")
        self.assertFalse(unavailable["real_provider_called"])

    def test_release_identity_and_hash_staleness_are_provenance_only(self) -> None:
        self.assertEqual(build_gateway_doctor_report(GATEWAY_ROOT)["status"], "ready")
        package = importlib.import_module("model_provider_gateway")
        stale_commit = "0" * 40
        with (
            patch.object(package, "__version__", "9.0.0-compatible-provenance"),
            patch(
                "video_creation_pipeline.gateway_consumer._git_source_provenance",
                return_value={"commit": stale_commit, "dirty": True},
            ),
        ):
            doctor = build_gateway_doctor_report(GATEWAY_ROOT)
            receipt = self._run(
                "script", self.text_artifact, self.root / "stale-provenance"
            )

        self.assertEqual(doctor["status"], "ready")
        self.assertEqual(doctor["package_commit"], stale_commit)
        self.assertEqual(doctor["package_version"], "9.0.0-compatible-provenance")
        self.assertEqual(receipt["status"], "completed_candidate_only")
        provenance = receipt["gateway_provenance"]
        provenance["canonical_commit"] = "1" * 40
        provenance["package_commit"] = "2" * 40
        provenance["package_version"] = "10.0.0-compatible-provenance"
        provenance["capability_matrix_revision"] = "3" * 64
        provenance["schema_sha256"] = {
            key: "4" * 64 for key in provenance["schema_sha256"]
        }
        provenance["gateway_component_sha256"] = {
            key: "5" * 64 for key in provenance["gateway_component_sha256"]
        }
        receipt["receipt_sha256"] = payload_sha256(receipt, "receipt_sha256")
        validate_gateway_candidate_receipt(receipt)

    def test_schema_and_capability_incompatibility_fail_closed(self) -> None:
        self.assertEqual(build_gateway_doctor_report(GATEWAY_ROOT)["status"], "ready")
        runtime_module = importlib.import_module("model_provider_gateway.runtime")
        coverage_module = importlib.import_module("model_provider_gateway.coverage")

        with patch.object(
            runtime_module,
            "HTTP_API_SCHEMA",
            "model_provider_gateway.http.v2",
        ):
            incompatible_schema = build_gateway_doctor_report(GATEWAY_ROOT)
        self.assertEqual(incompatible_schema["status"], "blocked")
        self.assertIn("incompatible", incompatible_schema["checks"][0]["detail"])

        matrix = coverage_module.build_consumer_capability_matrix(
            consumer_id="mvp_video_pipeline"
        )
        matrix["consumers"][0]["capabilities"]["vision"]["status"] = "blocked"
        with patch.object(
            coverage_module,
            "build_consumer_capability_matrix",
            return_value=matrix,
        ):
            missing_vision = build_gateway_doctor_report(GATEWAY_ROOT)
        self.assertEqual(missing_vision["status"], "blocked")
        self.assertIn("vision", missing_vision["checks"][0]["detail"])

    def test_text_and_vision_loopback_bind_artifacts_and_candidate_receipts(
        self,
    ) -> None:
        text_root = self.root / "text-run"
        text = self._run("script", self.text_artifact, text_root)
        text_validation = verify_gateway_bundle(GATEWAY_ROOT, text_root)

        self.assertEqual(text["status"], "completed_candidate_only")
        self.assertEqual(text["candidate"]["text"], "mock-ok")
        self.assertEqual(text["artifact_manifest"][0]["mime_type"], "application/json")
        self.assertEqual(
            text["artifact_manifest"][0]["bytes"], self.text_artifact.stat().st_size
        )
        self.assertEqual(len(text["artifact_manifest"][0]["sha256"]), 64)
        self.assertEqual(
            text["gateway_result"]["redacted_audit"]["transport"], "loopback_stub"
        )
        self.assertFalse(text["execution_boundary"]["real_provider_called"])
        self.assertTrue(text_validation["valid"])

        vision_root = self.root / "vision-run"
        vision = self._run("visual", self.image_artifact, vision_root)
        vision_validation = verify_gateway_bundle(GATEWAY_ROOT, vision_root)
        self.assertEqual(vision["candidate"]["text"], "mock-vision-ok")
        self.assertEqual(vision["artifact_manifest"][0]["mime_type"], "image/png")
        self.assertTrue(vision_validation["valid"])

        with self.assertRaises((ContractError, ValueError)):
            self._run("visual", self.text_artifact, self.root / "mime-mismatch")

        original_text = self.text_artifact.read_bytes()
        self.text_artifact.write_bytes(original_text + b"\n")
        with self.assertRaises((ContractError, ValueError)):
            verify_gateway_bundle(GATEWAY_ROOT, text_root)
        self.text_artifact.write_bytes(original_text)

        self.image_artifact.write_bytes(b"not-an-image")
        with self.assertRaises((ContractError, ValueError)):
            verify_gateway_bundle(GATEWAY_ROOT, vision_root)

    def test_profile_route_consent_and_request_drift_fail_closed(self) -> None:
        bundle = self.root / "drift-run"
        self._run("script", self.text_artifact, bundle)
        cases = []

        profile_path = bundle / "gateway-profile.json"
        profile = self._read_json(profile_path)
        changed_profile = copy.deepcopy(profile)
        changed_profile["provider_id"] = "drifted"
        cases.append((profile_path, profile, changed_profile))

        route_path = bundle / "gateway-route.json"
        route = self._read_json(route_path)
        changed_route = copy.deepcopy(route)
        changed_route["fallback_policy"]["enabled"] = True
        changed_route["fallback_policy"]["ordered"] = True
        revision_payload = dict(changed_route)
        revision_payload.pop("route_revision")
        changed_route["route_revision"] = canonical_json_sha256(revision_payload)
        cases.append((route_path, route, changed_route))

        consent_path = bundle / "gateway-consent.json"
        consent = self._read_json(consent_path)
        changed_consent = copy.deepcopy(consent)
        changed_consent["purpose"] = "drifted purpose"
        cases.append((consent_path, consent, changed_consent))

        request_path = bundle / "gateway-request.json"
        request = self._read_json(request_path)
        changed_request = copy.deepcopy(request)
        changed_request["profile_revision"] = "0" * 64
        cases.append((request_path, request, changed_request))

        for path, original, changed in cases:
            with self.subTest(path=path.name):
                self._write_json(path, changed)
                with self.assertRaises((ContractError, ValueError)):
                    verify_gateway_bundle(GATEWAY_ROOT, bundle)
                self._write_json(path, original)
                self.assertTrue(verify_gateway_bundle(GATEWAY_ROOT, bundle)["valid"])

    def test_retryable_failures_do_not_retry_or_fallback(self) -> None:
        expected = {
            "429": "rate_limited",
            "500": "provider_server_error",
            "timeout": "timeout",
        }
        for trigger, error_class in expected.items():
            with self.subTest(trigger=trigger):
                receipt = run_gateway_loopback(
                    GATEWAY_ROOT,
                    use_case="script",
                    artifact_path=self.text_artifact,
                    instruction="Return a synthetic script candidate only.",
                    output_root=self.root / f"failure-{trigger}",
                    trigger=trigger,
                )
                self.assertEqual(receipt["status"], "failed_candidate_only")
                self.assertEqual(receipt["gateway_result"]["error_class"], error_class)
                self.assertTrue(receipt["gateway_result"]["retryable"])
                self.assertIsNone(receipt["candidate"])
                self.assertFalse(
                    receipt["execution_boundary"]["automatic_fallback_used"]
                )

                incompatible = copy.deepcopy(receipt)
                incompatible["gateway_result"]["retryable"] = False
                incompatible["receipt_sha256"] = payload_sha256(
                    incompatible, "receipt_sha256"
                )
                with self.assertRaisesRegex(ContractError, "retryable"):
                    validate_gateway_candidate_receipt(incompatible)

    def test_blocked_capabilities_never_build_gateway_execution_contracts(self) -> None:
        for use_case in ("tts", "asr", "ocr", "embedding"):
            with self.subTest(use_case=use_case):
                bundle = self.root / f"blocked-{use_case}"
                receipt = run_gateway_loopback(
                    GATEWAY_ROOT,
                    use_case=use_case,
                    artifact_path=None,
                    instruction="This must remain blocked.",
                    output_root=bundle,
                )
                self.assertEqual(receipt["status"], "blocked")
                self.assertIsNone(receipt["gateway_result"])
                self.assertIsNone(receipt["candidate"])
                self.assertEqual(
                    [path.name for path in bundle.iterdir()], ["candidate-receipt.json"]
                )
                self.assertTrue(verify_gateway_bundle(GATEWAY_ROOT, bundle)["valid"])

    def test_fact_fallback_and_publication_escalation_are_rejected(self) -> None:
        receipt = self._run("script", self.text_artifact, self.root / "policy-run")

        added_fact = copy.deepcopy(receipt)
        added_fact["fact_policy"]["fact_assertions"] = [
            {
                "text": "An unsupported new fact",
                "evidence_refs": [],
                "status": "candidate_supported",
            }
        ]
        added_fact["receipt_sha256"] = payload_sha256(added_fact, "receipt_sha256")
        with self.assertRaisesRegex(ContractError, "requires evidence"):
            validate_gateway_candidate_receipt(added_fact)

        fallback = copy.deepcopy(receipt)
        fallback["gateway_result"]["redacted_audit"]["automatic_fallback_used"] = True
        fallback["receipt_sha256"] = payload_sha256(fallback, "receipt_sha256")
        with self.assertRaisesRegex(ContractError, "redaction or fallback"):
            validate_gateway_candidate_receipt(fallback)

        publication = copy.deepcopy(receipt)
        publication["execution_boundary"]["technical_qa_authorizes_publication"] = True
        publication["receipt_sha256"] = payload_sha256(publication, "receipt_sha256")
        with self.assertRaisesRegex(ContractError, "execution boundary"):
            validate_gateway_candidate_receipt(publication)

        retry = copy.deepcopy(receipt)
        retry["execution_boundary"]["automatic_retry_used"] = True
        retry["receipt_sha256"] = payload_sha256(retry, "receipt_sha256")
        with self.assertRaisesRegex(ContractError, "execution boundary"):
            validate_gateway_candidate_receipt(retry)

    def test_double_run_has_stable_candidate_identity(self) -> None:
        first = self._run("storyboard", self.text_artifact, self.root / "double-a")
        second = self._run("storyboard", self.text_artifact, self.root / "double-b")

        self.assertEqual(
            first["candidate"]["candidate_sha256"],
            second["candidate"]["candidate_sha256"],
        )
        self.assertEqual(
            first["candidate"]["text_sha256"], second["candidate"]["text_sha256"]
        )
        self.assertEqual(
            first["gateway_provenance"]["input_manifest_hash"],
            second["gateway_provenance"]["input_manifest_hash"],
        )

    def _run(self, use_case: str, artifact: Path, output_root: Path) -> dict:
        return run_gateway_loopback(
            GATEWAY_ROOT,
            use_case=use_case,
            artifact_path=artifact,
            instruction=f"Return a synthetic {use_case} candidate only.",
            output_root=output_root,
            allowed_evidence_refs=["source_fixture_primary"],
        )

    @staticmethod
    def _read_json(path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def _write_json(path: Path, payload: dict) -> None:
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )


if __name__ == "__main__":
    unittest.main()
