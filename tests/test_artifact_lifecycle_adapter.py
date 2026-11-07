from __future__ import annotations

import json
import os
import shutil
import tomllib
import unittest
from collections import namedtuple
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from video_creation_pipeline.artifact_lifecycle_adapter import (
    ADAPTER_ID,
    build_doctor,
    build_media_manifest,
    explain_artifact,
    load_core,
    plan_gc,
    process_probe,
    validate_synthetic_restore_fixture,
    verify_gc_plan,
)
from video_creation_pipeline.artifact_lifecycle_cli import main as lifecycle_cli_main
from video_creation_pipeline.errors import ContractError


ROOT = Path(__file__).resolve().parents[1]
CORE_ROOT = ROOT.parents[1] / "artifact-lifecycle-core"
FIXTURE_ROOT = ROOT / "tests" / "fixtures" / "artifact-lifecycle-media"
DISK_USAGE = namedtuple("DiskUsage", ["total", "used", "free"])
MIB = 1024 * 1024
GIB = 1024 * MIB


class ArtifactLifecycleAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        runtime_root = ROOT / ".test-runtime" / "artifact-lifecycle-adapter"
        runtime_root.mkdir(parents=True, exist_ok=True)
        self.base = runtime_root / f"case-{uuid4().hex}"
        self.base.mkdir()
        self.workspace = self.base / "workspace"
        self.quarantine = self.base / "quarantine"
        self.workspace.mkdir()
        self.quarantine.mkdir()
        self.checkpoint = self.base / "scan-checkpoint.json"
        self.manifest_path = self.base / "manifest.json"
        self.plan_path = self.base / "plan.json"
        self.core = load_core(CORE_ROOT)
        self.git_status_patch = patch.object(
            self.core, "git_status", return_value="clean"
        )
        self.git_status_patch.start()
        self.config = json.loads(
            (FIXTURE_ROOT / "adapter-config.v1.json").read_text(encoding="utf-8")
        )
        self._write_synthetic_tree()

    def tearDown(self) -> None:
        self.git_status_patch.stop()
        shutil.rmtree(self.base)

    def test_bounded_incremental_scan_is_idempotent_and_protects_authority(
        self,
    ) -> None:
        first = self._build_manifest()
        second = self._build_manifest()

        self.assertEqual(first, second)
        self.assertEqual(first["profile_id"], "media")
        self.assertEqual(first["cleanup_mode"], "plan_only")
        self.assertFalse(first["automatic_purge"])
        self.assertEqual(first["adapter"]["adapter_id"], ADAPTER_ID)
        self.assertEqual(first["scan"]["errors"], [])
        for item in first["artifacts"]:
            for field in (
                "closure_run_id",
                "producer_tool_id",
                "owner_lane",
                "digest",
                "artifact_role",
                "depends_on",
                "referenced_by",
                "rebuild_proof",
                "minimum_keep_until",
                "sensitivity",
                "pin",
            ):
                self.assertIn(field, item)
        self.assertTrue(self._artifact(first, "originals/source.mp4")["pin"])
        self.assertTrue(self._artifact(first, "final/final.mp4")["pin"])
        self.assertTrue(self._artifact(first, "final/subtitles.srt")["pin"])
        self.assertTrue(self._artifact(first, "active/checkpoint.json")["pin"])
        self.assertTrue(self._artifact(first, "sensitive/customer.mp4")["pin"])
        candidate = self._artifact(first, "render-work/preview/preview.mp4")
        self.assertEqual(candidate["rebuild_proof"]["status"], "verified")

    def test_plan_gc_only_selects_allowlisted_rebuildable_candidates(self) -> None:
        manifest = self._build_manifest(write=True)
        before = self._source_snapshot()

        plan = plan_gc(
            self.core,
            self.manifest_path,
            self.workspace,
            self.quarantine,
            self.config,
            owner_state="idle_confirmed",
            dispatch_state="none_active",
            process_snapshot=[],
        )

        by_id = {entry["artifact_id"]: entry for entry in plan["entries"]}
        for item in manifest["artifacts"]:
            entry = by_id[item["artifact_id"]]
            if item.get("candidate_kind"):
                self.assertTrue(entry["eligible"], item["path"])
            else:
                self.assertFalse(entry["eligible"], item["path"])
        self.assertEqual(plan["authorization_state"], "not_authorized")
        self.assertFalse(plan["adapter_policy"]["real_mutation_authorized"])
        self.assertEqual(before, self._source_snapshot())

    def test_original_final_qa_approval_publish_sensitive_and_active_are_blocked(
        self,
    ) -> None:
        manifest = self._build_manifest(write=True)
        plan = plan_gc(
            self.core,
            self.manifest_path,
            self.workspace,
            self.quarantine,
            self.config,
            owner_state="idle_confirmed",
            dispatch_state="none_active",
            process_snapshot=[],
        )
        by_id = {entry["artifact_id"]: entry for entry in plan["entries"]}
        protected = (
            "originals/source.mp4",
            "locked/script.md",
            "locked/storyboard.json",
            "locked/timeline.json",
            "final/final.mp4",
            "final/subtitles.srt",
            "qa/technical.json",
            "receipts/approval.json",
            "receipts/publish.json",
            "sensitive/customer.mp4",
            "active/checkpoint.json",
        )
        for relative in protected:
            entry = by_id[self._artifact(manifest, relative)["artifact_id"]]
            self.assertFalse(entry["eligible"], relative)
            self.assertTrue(entry["blocking_gates"], relative)

    def test_missing_rebuild_proof_fails_closed_even_for_cache_role(self) -> None:
        manifest = self._build_manifest()
        proxy = self._artifact(manifest, "proxies/proxy.mp4")
        proxy["rebuild_proof"] = {
            "status": "unknown",
            "command": None,
            "input_hashes": [],
            "dependency_lock": None,
            "verification_ref": None,
        }
        manifest = self.core.attach_hash(manifest, "manifest_sha256")
        self.core.atomic_write_json(self.manifest_path, manifest)

        plan = plan_gc(
            self.core,
            self.manifest_path,
            self.workspace,
            self.quarantine,
            self.config,
            owner_state="idle_confirmed",
            dispatch_state="none_active",
            process_snapshot=[],
        )
        entry = next(
            row for row in plan["entries"] if row["artifact_id"] == proxy["artifact_id"]
        )
        self.assertFalse(entry["eligible"])
        self.assertIn("rebuild_proof_incomplete", entry["blocking_gates"])

    def test_unknown_reference_fails_before_plan(self) -> None:
        manifest = self._build_manifest()
        self._artifact(manifest, "proxies/proxy.mp4")["depends_on"].append(
            "missing-artifact"
        )
        manifest = self.core.attach_hash(manifest, "manifest_sha256")
        self.core.atomic_write_json(self.manifest_path, manifest)
        with self.assertRaisesRegex(ContractError, "unknown artifact reference"):
            plan_gc(
                self.core,
                self.manifest_path,
                self.workspace,
                self.quarantine,
                self.config,
                owner_state="idle_confirmed",
                dispatch_state="none_active",
                process_snapshot=[],
            )

    def test_active_ffmpeg_or_worker_blocks_every_entry(self) -> None:
        self._build_manifest(write=True)
        plan = plan_gc(
            self.core,
            self.manifest_path,
            self.workspace,
            self.quarantine,
            self.config,
            owner_state="idle_confirmed",
            dispatch_state="none_active",
            process_snapshot=[
                {"pid": 4242, "name": "ffmpeg.exe", "cmdline": ["ffmpeg", "-i"]}
            ],
        )
        self.assertTrue(plan["adapter_policy"]["process_probe"]["matches"])
        for entry in plan["entries"]:
            self.assertFalse(entry["eligible"])
            self.assertIn("active_process_not_cleared", entry["blocking_gates"])

    def test_process_probe_error_is_unknown_not_idle(self) -> None:
        def fail_probe(*_args: object, **_kwargs: object) -> list[object]:
            raise OSError("synthetic access failure")

        result = process_probe(self.config, process_iter=fail_probe)
        self.assertEqual(result["state"], "unknown")

    def test_media_profile_small_output_warns_but_allows_at_three_gib_free(
        self,
    ) -> None:
        usage = DISK_USAGE(total=100 * GIB, used=97 * GIB, free=3 * GIB)
        with patch.object(self.core.shutil, "disk_usage", return_value=usage):
            report = build_doctor(
                self.core,
                self.workspace,
                self.config,
                process_snapshot=[],
                operation_kind="render",
                estimated_output_bytes=256 * MIB,
            )

        self.assertEqual(report["status"], "ready")
        self.assertTrue(report["large_render_or_transcode_allowed"])
        self.assertEqual(report["disk"]["status"], "low_disk_allowed")
        self.assertEqual(report["large_output_preflight"]["status"], "warning_allowed")
        self.assertEqual(report["core_profile"]["minimum_free_bytes"], 512 * MIB)
        self.assertEqual(report["core_profile"]["warning_free_bytes"], 5 * GIB)
        self.assertEqual(report["core_profile"]["output_headroom_multiplier"], 1.25)
        self.assertEqual(report["disk"]["required_free_bytes"], 512 * MIB + 320 * MIB)
        self.assertIn("low_disk_allowed", report["reason_codes"])

    def test_media_profile_large_output_blocks_when_reserve_would_be_breached(
        self,
    ) -> None:
        free = 5 * GIB // 2
        usage = DISK_USAGE(total=100 * GIB, used=100 * GIB - free, free=free)
        with patch.object(self.core.shutil, "disk_usage", return_value=usage):
            report = build_doctor(
                self.core,
                self.workspace,
                self.config,
                process_snapshot=[],
                operation_kind="transcode",
                estimated_output_bytes=2 * GIB,
            )

        self.assertEqual(report["status"], "blocked")
        self.assertFalse(report["large_render_or_transcode_allowed"])
        self.assertEqual(report["disk"]["status"], "blocked_low_disk")
        self.assertEqual(report["large_output_preflight"]["status"], "blocked")
        self.assertIn("blocked_low_disk", report["reason_codes"])

    def test_large_output_without_declared_estimate_fails_closed(self) -> None:
        usage = DISK_USAGE(total=100 * GIB, used=97 * GIB, free=3 * GIB)
        with patch.object(self.core.shutil, "disk_usage", return_value=usage):
            report = build_doctor(
                self.core,
                self.workspace,
                self.config,
                process_snapshot=[],
                operation_kind="render",
            )

        self.assertEqual(report["status"], "blocked")
        self.assertFalse(report["large_render_or_transcode_allowed"])
        self.assertEqual(report["large_output_preflight"]["status"], "blocked")
        self.assertIn("blocked_output_estimate_required", report["reason_codes"])
        self.assertEqual(
            report["large_output_preflight"]["reason_codes"],
            ["blocked_output_estimate_required"],
        )

    def test_lifecycle_doctor_does_not_claim_large_output_authority(self) -> None:
        usage = DISK_USAGE(total=100 * GIB, used=97 * GIB, free=3 * GIB)
        with patch.object(self.core.shutil, "disk_usage", return_value=usage):
            report = build_doctor(
                self.core, self.workspace, self.config, process_snapshot=[]
            )

        self.assertEqual(report["status"], "ready")
        self.assertFalse(report["large_render_or_transcode_allowed"])
        self.assertEqual(report["large_output_preflight"]["status"], "not_requested")

    def test_cross_volume_plan_fails_closed(self) -> None:
        self._build_manifest(write=True)
        original = self.core.volume_identity

        def different_volume(path: Path) -> int:
            if Path(path) == self.workspace:
                return 1
            if Path(path) == self.quarantine:
                return 2
            return original(path)

        with patch.object(self.core, "volume_identity", side_effect=different_volume):
            with self.assertRaisesRegex(ContractError, "CROSS_VOLUME_BLOCKED"):
                plan_gc(
                    self.core,
                    self.manifest_path,
                    self.workspace,
                    self.quarantine,
                    self.config,
                    owner_state="idle_confirmed",
                    dispatch_state="none_active",
                    process_snapshot=[],
                )

    def test_reparse_signal_and_bounded_limits_fail_closed(self) -> None:
        original = self.core.assert_no_reparse

        def reject_preview(path: Path, root: Path) -> None:
            if Path(path).name == "preview":
                raise self.core.LifecycleError("REPARSE_POINT_BLOCKED", str(path))
            original(path, root)

        with patch.object(self.core, "assert_no_reparse", side_effect=reject_preview):
            with self.assertRaisesRegex(ContractError, "REPARSE_POINT_BLOCKED"):
                self._build_manifest()

        shallow = dict(self.config)
        shallow["max_depth"] = 0
        with self.assertRaisesRegex(ContractError, "depth limit"):
            build_media_manifest(
                self.core, self.workspace, shallow, checkpoint_path=self.checkpoint
            )

        tiny = dict(self.config)
        tiny["max_items"] = 1
        with self.assertRaisesRegex(ContractError, "item limit"):
            build_media_manifest(
                self.core, self.workspace, tiny, checkpoint_path=self.checkpoint
            )

    def test_unobserved_hardlink_blocks_plan(self) -> None:
        source = self.workspace / "proxies" / "proxy.mp4"
        hidden = self.workspace / "not-scanned" / "proxy-hardlink.mp4"
        hidden.parent.mkdir()
        try:
            os.link(source, hidden)
        except OSError as exc:
            self.skipTest(f"hardlinks unavailable in this test environment: {exc}")
        manifest = self._build_manifest(write=True)
        self.assertIn(
            {"path": "proxies/proxy.mp4", "error": "HARDLINK_COVERAGE_INCOMPLETE"},
            manifest["scan"]["errors"],
        )
        with self.assertRaisesRegex(ContractError, "hardlink_coverage_incomplete"):
            plan_gc(
                self.core,
                self.manifest_path,
                self.workspace,
                self.quarantine,
                self.config,
                owner_state="idle_confirmed",
                dispatch_state="none_active",
                process_snapshot=[],
            )

    def test_plan_drift_is_detected_without_source_mutation(self) -> None:
        self._build_manifest(write=True)
        plan = plan_gc(
            self.core,
            self.manifest_path,
            self.workspace,
            self.quarantine,
            self.config,
            owner_state="idle_confirmed",
            dispatch_state="none_active",
            process_snapshot=[],
        )
        self.core.atomic_write_json(self.plan_path, plan)
        candidate = self.workspace / "render-work" / "preview" / "preview.mp4"
        candidate.write_text("drifted synthetic preview", encoding="utf-8")

        verification = verify_gc_plan(
            self.core,
            self.plan_path,
            self.manifest_path,
            self.workspace,
            self.quarantine,
        )
        self.assertFalse(verification["ok"])
        self.assertTrue(
            any(
                error.startswith("artifact_digest_drift:")
                for error in verification["errors"]
            )
        )
        self.assertFalse(verification["filesystem_mutation_performed"])

    def test_self_media_v2_is_consumed_as_compatibility_view(self) -> None:
        source = self.base / "self-media-v2.json"
        source.write_text(
            json.dumps(
                {
                    "schema_version": "self-media-artifact-lifecycle-manifest.v2",
                    "manifest_id": "synthetic-v2",
                    "workspace_ref": "synthetic-workspace",
                    "artifacts": [
                        {
                            "artifact_id": "cache-v2",
                            "path": "cache.txt",
                            "bytes": 5,
                            "artifact_kind": "cache",
                            "status": "candidate",
                            "source_sha256": "a" * 64,
                            "depends_on": [],
                            "referenced_by": [],
                            "delete_after": None,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        converted = self.core.load_manifest(source)
        self.assertEqual(converted["profile_id"], "media")
        report = explain_artifact(self.core, source, "cache-v2")
        self.assertIn("candidate_kind_not_allowlisted", report["blocking_gates"])
        self.assertIn("rebuild_proof_incomplete", report["blocking_gates"])

    def test_restore_is_limited_to_synthetic_fixture(self) -> None:
        with patch.dict(
            os.environ,
            {"VIDEO_ARTIFACT_LIFECYCLE_TEMP_ROOT": str(self.base)},
        ):
            report = validate_synthetic_restore_fixture(
                self.core, FIXTURE_ROOT / "synthetic-restore.v1.json"
            )
        self.assertEqual(report["status"], "passed")
        self.assertTrue(report["synthetic_fixture_mutation_performed"])
        self.assertFalse(report["real_media_mutation_performed"])

        unsafe = self.base / "unsafe-restore.json"
        unsafe.write_text(
            json.dumps(
                {
                    "schema_version": "artifact-lifecycle-operation-receipt.v1",
                    "synthetic_fixture": False,
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ContractError, "only the synthetic"):
            with patch.dict(
                os.environ,
                {"VIDEO_ARTIFACT_LIFECYCLE_TEMP_ROOT": str(self.base)},
            ):
                validate_synthetic_restore_fixture(self.core, unsafe)

    def test_installed_entry_point_targets_thin_adapter_cli(self) -> None:
        with (ROOT / "pyproject.toml").open("rb") as handle:
            scripts = tomllib.load(handle)["project"]["scripts"]
        self.assertEqual(
            scripts["video-artifact-lifecycle"],
            "video_creation_pipeline.artifact_lifecycle_cli:main",
        )

    def test_cli_scan_and_synthetic_restore_use_only_safe_interfaces(self) -> None:
        with redirect_stdout(StringIO()):
            scan_status = lifecycle_cli_main(
                [
                    "--core-root",
                    str(CORE_ROOT),
                    "scan",
                    "--workspace-root",
                    str(self.workspace),
                    "--config",
                    str(FIXTURE_ROOT / "adapter-config.v1.json"),
                    "--checkpoint",
                    str(self.checkpoint),
                    "--output",
                    str(self.manifest_path),
                ]
            )
        self.assertEqual(scan_status, 0)
        self.assertTrue(self.manifest_path.is_file())

        with patch.dict(
            os.environ,
            {"VIDEO_ARTIFACT_LIFECYCLE_TEMP_ROOT": str(self.base)},
        ):
            with redirect_stdout(StringIO()):
                restore_status = lifecycle_cli_main(
                    [
                        "--core-root",
                        str(CORE_ROOT),
                        "restore",
                        "--fixture",
                        str(FIXTURE_ROOT / "synthetic-restore.v1.json"),
                    ]
                )
        self.assertEqual(restore_status, 0)

    def test_cli_render_doctor_requires_output_estimate(self) -> None:
        usage = DISK_USAGE(total=100 * GIB, used=97 * GIB, free=3 * GIB)
        output = StringIO()
        with patch.object(self.core.shutil, "disk_usage", return_value=usage):
            with patch(
                "video_creation_pipeline.artifact_lifecycle_cli.load_core",
                return_value=self.core,
            ):
                with redirect_stdout(output):
                    status = lifecycle_cli_main(
                        [
                            "--core-root",
                            str(CORE_ROOT),
                            "doctor",
                            "--workspace-root",
                            str(self.workspace),
                            "--config",
                            str(FIXTURE_ROOT / "adapter-config.v1.json"),
                            "--process-snapshot",
                            str(FIXTURE_ROOT / "empty-process-snapshot.json"),
                            "--operation-kind",
                            "render",
                        ]
                    )
        self.assertEqual(status, 2)
        self.assertIn("blocked_output_estimate_required", output.getvalue())

    def _build_manifest(self, *, write: bool = False) -> dict[str, object]:
        manifest = build_media_manifest(
            self.core,
            self.workspace,
            self.config,
            checkpoint_path=self.checkpoint,
        )
        if write:
            self.core.atomic_write_json(self.manifest_path, manifest)
        return manifest

    @staticmethod
    def _artifact(manifest: dict[str, object], relative: str) -> dict[str, object]:
        return next(item for item in manifest["artifacts"] if item["path"] == relative)

    def _source_snapshot(self) -> dict[str, str]:
        result = {}
        for path in self.workspace.rglob("*"):
            if path.is_file():
                result[path.relative_to(self.workspace).as_posix()] = (
                    self.core.sha256_file(path)
                )
        return result

    def _write_synthetic_tree(self) -> None:
        files = {
            "render-work/failed/failed.mp4": "failed encode",
            "render-work/preview/preview.mp4": "preview",
            "render-work/duplicates/duplicate.mp4": "duplicate transcode",
            "render-work/superseded/intermediate.mov": "superseded intermediate",
            "render-work/workspace/frame-cache.bin": "rebuildable workspace",
            "proxies/proxy.mp4": "proxy",
            "originals/source.mp4": "authoritative source",
            "locked/script.md": "locked script",
            "locked/storyboard.json": "{}",
            "locked/timeline.json": "{}",
            "final/final.mp4": "final deliverable",
            "final/subtitles.srt": "1\n00:00:00,000 --> 00:00:01,000\nsynthetic\n",
            "qa/rebuild.json": '{"status":"verified"}',
            "qa/technical.json": '{"status":"passed"}',
            "receipts/approval.json": '{"approved":true}',
            "receipts/publish.json": '{"published":false}',
            "sensitive/customer.mp4": "synthetic customer placeholder",
            "active/checkpoint.json": '{"status":"active"}',
            "recipe/render.json": '{"render":"synthetic"}',
        }
        for relative, content in files.items():
            target = self.workspace / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
