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

from video_creation_pipeline.cli import main as cli_main  # noqa: E402
from video_creation_pipeline.vkp_bridge import (  # noqa: E402
    ContractError,
    build_edit_job,
    build_preference_candidates,
    write_edit_job,
)

FIXTURE = ROOT / "tests" / "fixtures" / "vkp_bundle"


class VkpBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = ROOT / ".test-runtime" / self._testMethodName
        if self.root.exists():
            shutil.rmtree(self.root)
        self.root.mkdir(parents=True)
        self.bundle = self.root / "bundle"
        shutil.copytree(FIXTURE, self.bundle)

    def tearDown(self) -> None:
        if self.root.exists():
            shutil.rmtree(self.root)

    def test_builds_p0_job_from_authoritative_vkp_artifacts(self) -> None:
        job = build_edit_job(self.bundle)

        self.assertEqual(job["readiness"]["status"], "ready_for_human_selection")
        self.assertEqual(job["status_source"]["run_count"], 3)
        self.assertTrue(job["status_source"]["authoritative"])
        self.assertFalse(job["status_source"]["local_status_machine_created"])
        self.assertEqual(len(job["candidate_clips"]), 2)
        self.assertEqual(
            job["transcript_contract"]["selected_source"],
            "source_arbitrated_transcript_json",
        )
        self.assertEqual(
            job["transcript_contract"]["term_evidence"]["high_confidence_terms"][0][
                "canonical_term"
            ],
            "Playwright MCP",
        )
        self.assertFalse(job["boundary_refinement"]["execute"])
        self.assertEqual(
            job["boundary_refinement"]["requests"][0]["status"],
            "blocked_until_human_confirmation",
        )
        self.assertTrue(
            job["seam_review"]["workbench_path"].endswith("video-workbench.json")
        )
        self.assertFalse(job["seam_review"]["new_review_server_created"])

    def test_storyboard_is_candidate_only_and_decorations_are_blocked(self) -> None:
        job = build_edit_job(self.bundle)
        scene = job["storyboard_scenes"][0]

        self.assertEqual(scene["timeline_indexes"], [1, 2])
        self.assertFalse(scene["human_confirmed"])
        self.assertFalse(scene["mutates_vkp_timeline"])
        self.assertEqual(
            scene["decoration_contract"]["status"],
            "blocked_until_scene_confirmation",
        )
        self.assertTrue(scene["citation_digest"])

    def test_preference_learning_requires_confirmed_cross_video_evidence(self) -> None:
        diffs = [
            {
                "human_confirmed": False,
                "source_video_sha256": "0" * 64,
                "category": "edit",
                "direction": "restore",
                "rule": "保留完整句尾",
            },
            *[
                {
                    "human_confirmed": True,
                    "source_video_sha256": char * 64,
                    "category": "edit",
                    "direction": "restore",
                    "rule": "保留完整句尾",
                }
                for char in ("a", "b", "c")
            ],
            {
                "human_confirmed": True,
                "source_video_sha256": "d" * 64,
                "category": "pace",
                "direction": "slower",
                "rule": "案例段降低节奏",
                "explicit_user_pin": True,
            },
        ]

        result = build_preference_candidates(diffs)

        self.assertEqual(result["ignored_diff_count"], 1)
        self.assertEqual(result["candidate_count"], 2)
        self.assertTrue(all(row["eligible"] for row in result["candidates"]))
        self.assertTrue(all(not row["rule_written"] for row in result["candidates"]))
        self.assertFalse(result["automatic_rule_write"])

    def test_missing_registry_is_rejected_instead_of_creating_status(self) -> None:
        (self.bundle / "run-artifact-registry.json").unlink()

        with self.assertRaisesRegex(ContractError, "run-artifact-registry"):
            build_edit_job(self.bundle)

    def test_bundle_artifact_path_escape_is_rejected(self) -> None:
        manifest_path = self.bundle / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["run_artifact_registry_json"] = "../outside.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        with self.assertRaisesRegex(ContractError, "escapes selected root"):
            build_edit_job(self.bundle)

    def test_cli_writes_outside_bundle_without_mutating_source(self) -> None:
        before = tree_hash(self.bundle)
        output = self.root / "video-creation" / "edit-job.json"

        code = cli_main([str(self.bundle), "--output", str(output)])

        self.assertEqual(code, 0)
        self.assertTrue(output.exists())
        self.assertEqual(before, tree_hash(self.bundle))
        payload = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(
            payload["operator_boundary"]["execution_mode"], "hybrid_local_remote"
        )
        self.assertTrue(payload["operator_boundary"]["read_only_bundle"])
        self.assertFalse(
            payload["operator_boundary"]["online_model_execution_authorized"]
        )
        self.assertFalse(payload["operator_boundary"]["upload_execution_authorized"])
        self.assertFalse(payload["operator_boundary"]["bridge_executes_ffmpeg"])

    def test_output_inside_vkp_bundle_is_rejected(self) -> None:
        job = build_edit_job(self.bundle)

        with self.assertRaisesRegex(ContractError, "outside the read-only VKP Bundle"):
            write_edit_job(job, self.bundle / "edit-job.json")

    def test_remote_capabilities_are_available_but_unapproved_by_default(self) -> None:
        job = build_edit_job(self.bundle)
        remote = job["remote_capabilities"]

        self.assertFalse(remote["execute"])
        self.assertEqual(remote["status"], "awaiting_explicit_authorization")
        self.assertFalse(remote["automatic_fallback"])
        self.assertFalse(remote["upload"]["allowed"])
        self.assertIn("authorization_reference", remote["required_before_execution"])
        self.assertIn("upload_unlisted_artifacts", remote["forbidden"])
        self.assertTrue(
            any(row["provider"] == "chatcut" for row in remote["capabilities"])
        )

    def test_authorized_remote_policy_allows_online_model_and_manifest_upload(
        self,
    ) -> None:
        source = self.root / "talking-head.mp4"
        source.write_bytes(b"test-video-bytes")

        job = build_edit_job(self.bundle, remote_policy=self.remote_policy(source))
        remote = job["remote_capabilities"]

        self.assertTrue(remote["execute"])
        self.assertTrue(remote["online_model_calls_allowed"])
        self.assertEqual(remote["status"], "authorized_for_execution")
        self.assertEqual(remote["provider"], "chatcut")
        self.assertEqual(remote["capabilities"][0]["result_role"], "candidate_timeline")
        self.assertTrue(remote["upload"]["allowed"])
        self.assertEqual(remote["upload"]["artifact_count"], 1)
        self.assertEqual(
            remote["upload"]["artifacts"][0]["sha256"],
            hashlib.sha256(source.read_bytes()).hexdigest(),
        )
        self.assertEqual(remote["authorization"]["kind"], "provider_mcp_consent")
        self.assertNotIn("consent", remote)
        self.assertTrue(job["operator_boundary"]["online_model_execution_authorized"])
        self.assertTrue(job["operator_boundary"]["upload_execution_authorized"])

    def test_remote_policy_requires_confirmed_dated_provider_authorization(
        self,
    ) -> None:
        source = self.root / "talking-head.mp4"
        source.write_bytes(b"test-video-bytes")
        policy = self.remote_policy(source)
        policy["authorization"]["operator_confirmed"] = False

        with self.assertRaisesRegex(ContractError, "operator-confirmed"):
            build_edit_job(self.bundle, remote_policy=policy)

        policy = self.remote_policy(source)
        policy["authorization"]["confirmed_at"] = "not-a-date"
        with self.assertRaisesRegex(ContractError, "dated"):
            build_edit_job(self.bundle, remote_policy=policy)

    def test_remote_policy_requires_schema_and_boolean_flags(self) -> None:
        source = self.root / "talking-head.mp4"
        source.write_bytes(b"test-video-bytes")
        policy = self.remote_policy(source)
        policy["schema"] = "unsupported"

        with self.assertRaisesRegex(ContractError, "must use"):
            build_edit_job(self.bundle, remote_policy=policy)

        policy = self.remote_policy(source)
        policy["enabled"] = "true"
        with self.assertRaisesRegex(ContractError, "JSON boolean"):
            build_edit_job(self.bundle, remote_policy=policy)

    def test_upload_manifest_detects_file_change_before_write(self) -> None:
        source = self.root / "talking-head.mp4"
        source.write_bytes(b"authorized-bytes")
        job = build_edit_job(self.bundle, remote_policy=self.remote_policy(source))
        source.write_bytes(b"changed-after-authorization")

        with self.assertRaisesRegex(ContractError, "changed after authorization"):
            write_edit_job(job, self.root / "edit-job.json")

    def test_cli_accepts_remote_policy_without_performing_network_calls(self) -> None:
        source = self.root / "talking-head.mp4"
        source.write_bytes(b"test-video-bytes")
        policy_path = self.root / "remote-policy.json"
        policy_path.write_text(json.dumps(self.remote_policy(source)), encoding="utf-8")
        output = self.root / "video-creation" / "edit-job.json"

        code = cli_main(
            [
                str(self.bundle),
                "--output",
                str(output),
                "--remote-policy",
                str(policy_path),
            ]
        )

        self.assertEqual(code, 0)
        payload = json.loads(output.read_text(encoding="utf-8"))
        self.assertTrue(payload["remote_capabilities"]["execute"])
        self.assertEqual(payload["remote_capabilities"]["upload"]["artifact_count"], 1)

    def remote_policy(self, source: Path) -> dict:
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
                    {"path": str(source), "purpose": "editable_timeline_finishing"}
                ],
            },
        }


def tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


if __name__ == "__main__":
    unittest.main()
