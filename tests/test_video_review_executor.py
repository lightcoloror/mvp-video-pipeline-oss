from __future__ import annotations

import json
import subprocess
import shutil
import threading
import time
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import video_creation_pipeline.video_review_executor as executor_module
import video_creation_pipeline.contract_io as contract_io_module
import video_creation_pipeline.execution_receipts as receipts_module
from video_creation_pipeline.contract_io import (
    canonical_json_sha256,
    file_sha256,
    payload_sha256,
)
from video_creation_pipeline.execution_receipts import (
    build_ffmpeg_execution_receipt,
    write_execution_contract,
)
from video_creation_pipeline.errors import ContractError
from video_creation_pipeline.video_review_executor import (
    EXECUTION_POLICY_REF,
    build_check_profile,
    execute_review,
)


ROOT = Path(__file__).resolve().parents[1]
TEST_ROOT = ROOT / ".test-runtime"


class _FakeOutlet:
    def __init__(self, delay: float = 0.0) -> None:
        self.calls: list[list[str]] = []
        self.delay = delay
        self.lock = threading.Lock()

    def __call__(
        self,
        operation,
        command,
        inputs,
        outputs,
        receipt_path,
        *,
        capture_log_path=None,
        **_kwargs,
    ):
        with self.lock:
            self.calls.append(list(command))
        if self.delay:
            time.sleep(self.delay)
        log = Path(capture_log_path)
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(
            "black_duration:0.1\nfreeze_duration:0.2\nsilence_duration:0.3\n",
            encoding="utf-8",
        )
        receipt = build_ffmpeg_execution_receipt(operation, command, inputs, outputs)
        write_execution_contract(receipt, receipt_path)
        return receipt


class _ResourceBlockedOutlet:
    def __call__(self, *_args, **_kwargs):
        class ResourceBlocked(ContractError):
            code = "resource_pressure_pause_required"

            def as_dict(self):
                return {
                    "status": "blocked",
                    "reason": self.code,
                    "automatic_retry": False,
                    "resource_guard": {"policy_id": "video-review-resource-guard-v1"},
                }

        raise ResourceBlocked("resource pressure")


class VideoReviewExecutorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = TEST_ROOT / ("video-review-executor-" + uuid.uuid4().hex)
        self.root.mkdir(parents=True)
        self.media = self.root / "candidate.mp4"
        self.media.write_bytes(b"synthetic-video-v1")
        self.plan_path = self.root / "plan.json"
        self.cache = self.root / "cache"
        self._write_plan()

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def _write_plan(self, *, limited: bool = False, force_full: bool = False) -> None:
        changed = [
            {
                "window_id": "change-001",
                "start_seconds": 2.0,
                "end_seconds": 5.0,
                "reason": "synthetic changed window",
            }
        ] if limited else []
        plan = {
            "schema_version": "self-media-video-review-plan.v1",
            "plan_id": "review-synthetic-task",
            "task_id": "synthetic-task",
            "mode": "limited_rework" if limited else "initial_review",
            "inputs": {
                "source_media": {
                    "path": self.media.name,
                    "sha256": file_sha256(self.media),
                    "bytes": self.media.stat().st_size,
                    "duration_seconds": 10.0,
                }
            },
            "review_scope": {
                "render_scope": "full_timeline_source" if force_full or not limited else "changed_windows_only",
                "changed_windows": changed,
                "frozen_windows": [],
                "acceptance_frame_timestamps": [2.5, 3.5, 4.5] if limited else [1.0, 5.0, 9.0],
                "change_kinds": ["recompression"] if limited else [],
            },
            "human_review_requests": [],
            "proxy_profile": {"enabled": False, "reason": "source_time_range_review"},
            "owner_binding": {
                "tool_id": "mvp-video-pipeline",
                "ffmpeg_outlet": "video_creation_pipeline.single_ffmpeg_outlet",
            },
            "execution_policy_ref": dict(EXECUTION_POLICY_REF),
        }
        plan["plan_payload_sha256"] = payload_sha256(plan, "plan_payload_sha256")
        self.plan_path.write_text(json.dumps(plan), encoding="utf-8")

    def test_review_proxy_or_clip_plan_rejected_before_ffmpeg_even_if_resealed(self) -> None:
        for mutation in ("proxy", "clip"):
            self._write_plan()
            plan = json.loads(self.plan_path.read_text(encoding="utf-8"))
            if mutation == "proxy":
                plan["proxy_profile"] = {"enabled": True}
            else:
                plan["human_review_requests"] = [{"review_access_mode": "independent_clip"}]
            plan["plan_payload_sha256"] = payload_sha256(plan, "plan_payload_sha256")
            self.plan_path.write_text(json.dumps(plan), encoding="utf-8")
            outlet = _FakeOutlet()
            with self.assertRaises(ContractError):
                execute_review(self.plan_path, self.cache, self.root / "denied.json", ffmpeg_executor=outlet)
            self.assertEqual(outlet.calls, [])

    def test_second_identical_execution_reuses_cache_without_outlet_call(self) -> None:
        outlet = _FakeOutlet()
        first = execute_review(
            self.plan_path, self.cache, self.root / "first.json", ffmpeg_executor=outlet
        )
        second = execute_review(
            self.plan_path, self.cache, self.root / "second.json", ffmpeg_executor=outlet
        )
        self.assertEqual(first["disposition"], "performed")
        self.assertEqual(second["disposition"], "reused")
        self.assertEqual(len(outlet.calls), 1)
        self.assertRegex(first["cache_key"], r"^[a-f0-9]{64}$")
        self.assertEqual(
            first["cache_key"],
            canonical_json_sha256(
                {
                    "media_sha256": first["media_sha256"],
                    "check_profile_sha256": first["check_profile_sha256"],
                }
            ),
        )
        self.assertTrue(
            (self.cache / ".locks" / f"{first['cache_key']}.lock").is_file()
        )

    def test_each_invocation_hashes_media_once_then_uses_fingerprint(self) -> None:
        outlet = _FakeOutlet()
        with patch.object(
            executor_module, "file_sha256", wraps=file_sha256
        ) as media_hasher:
            execute_review(
                self.plan_path,
                self.cache,
                self.root / "single-hash.json",
                ffmpeg_executor=outlet,
            )
        self.assertEqual(media_hasher.call_count, 1)

    def test_canonical_outlet_content_verifies_supplied_media_when_building_receipt(self) -> None:
        # The caller hashes the source once to bind the plan/cache contract. The
        # canonical FFmpeg outlet then re-reads the supplied artifact bytes while
        # ``build_ffmpeg_execution_receipt`` runs after the runner returns, so a
        # supplied reference is content-verified instead of trusted by metadata
        # alone. The reviewed contract is caller hash + receipt revalidation = two
        # full content reads; the obsolete "no second hash" performance
        # expectation is intentionally not asserted.
        media_reads = 0
        executor_reads = 0
        receipt_reads = 0
        executor_hasher = executor_module.file_sha256
        contract_hasher = contract_io_module.file_sha256

        def count_executor_hash(path):
            nonlocal media_reads, executor_reads
            if Path(path).resolve() == self.media.resolve():
                media_reads += 1
                executor_reads += 1
            return executor_hasher(path)

        def count_contract_hash(path):
            nonlocal media_reads, receipt_reads
            if Path(path).resolve() == self.media.resolve():
                media_reads += 1
                receipt_reads += 1
            return contract_hasher(path)

        def fake_runner(command, **_kwargs):
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        healthy = {
            "cpu_percent": 1.0,
            "available_memory_bytes": 16 * 1024**3,
            "available_memory_percent": 50.0,
            "disk_busy_percent": 1.0,
            "measurement_errors": [],
        }
        with (
            patch.object(executor_module, "file_sha256", side_effect=count_executor_hash),
            patch.object(contract_io_module, "file_sha256", side_effect=count_contract_hash),
            patch.object(receipts_module.shutil, "which", return_value="ffmpeg"),
        ):
            execute_review(
                self.plan_path,
                self.cache,
                self.root / "canonical-outlet.json",
                ffmpeg_runner=fake_runner,
                resource_snapshot_provider=lambda: healthy,
            )
        self.assertEqual(media_reads, 2)
        self.assertEqual(executor_reads, 1)
        self.assertEqual(receipt_reads, 1)

    def test_preexisting_retained_lockfile_does_not_block_execution(self) -> None:
        plan = json.loads(self.plan_path.read_text(encoding="utf-8"))
        media_sha256 = file_sha256(self.media)
        profile_sha256 = canonical_json_sha256(build_check_profile(plan))
        cache_key = canonical_json_sha256(
            {
                "media_sha256": media_sha256,
                "check_profile_sha256": profile_sha256,
            }
        )
        lock_path = self.cache / ".locks" / f"{cache_key}.lock"
        lock_path.parent.mkdir(parents=True)
        lock_path.write_bytes(b"retained")
        result = execute_review(
            self.plan_path,
            self.cache,
            self.root / "retained-lock.json",
            ffmpeg_executor=_FakeOutlet(),
        )
        self.assertEqual(result["disposition"], "performed")
        self.assertTrue(lock_path.is_file())

    def test_profile_or_media_hash_change_executes_again(self) -> None:
        outlet = _FakeOutlet()
        execute_review(
            self.plan_path,
            self.cache,
            self.root / "first.json",
            check_profile_extension={"revision": "one"},
            ffmpeg_executor=outlet,
        )
        execute_review(
            self.plan_path,
            self.cache,
            self.root / "second.json",
            check_profile_extension={"revision": "two"},
            ffmpeg_executor=outlet,
        )
        self.media.write_bytes(b"synthetic-video-v2")
        self._write_plan()
        execute_review(
            self.plan_path,
            self.cache,
            self.root / "third.json",
            check_profile_extension={"revision": "two"},
            ffmpeg_executor=outlet,
        )
        self.assertEqual(len(outlet.calls), 3)

    def test_concurrent_double_check_executes_only_once(self) -> None:
        outlet = _FakeOutlet(delay=0.1)

        def run(index: int):
            return execute_review(
                self.plan_path,
                self.cache,
                self.root / f"concurrent-{index}.json",
                ffmpeg_executor=outlet,
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(run, (1, 2)))
        self.assertEqual(len(outlet.calls), 1)
        self.assertEqual(sorted(row["disposition"] for row in results), ["performed", "reused"])

    def test_limited_rework_rejects_full_timeline_scope(self) -> None:
        outlet = _FakeOutlet()
        self._write_plan(limited=True, force_full=True)
        with self.assertRaisesRegex(ContractError, "limited rework"):
            execute_review(
                self.plan_path,
                self.cache,
                self.root / "limited.json",
                ffmpeg_executor=outlet,
            )
        self.assertEqual(outlet.calls, [])

    def test_limited_rework_commands_are_window_bounded(self) -> None:
        outlet = _FakeOutlet()
        self._write_plan(limited=True)
        result = execute_review(
            self.plan_path,
            self.cache,
            self.root / "limited.json",
            ffmpeg_executor=outlet,
        )
        self.assertFalse(result["scope"]["full_timeline_scanned"])
        self.assertEqual(len(outlet.calls), 1)
        self.assertIn("-ss", outlet.calls[0])
        self.assertIn("-t", outlet.calls[0])

    def test_resource_pressure_is_blocked_without_automatic_retry(self) -> None:
        result = execute_review(
            self.plan_path,
            self.cache,
            self.root / "blocked.json",
            ffmpeg_executor=_ResourceBlockedOutlet(),
        )
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["reason"], "resource_pressure_pause_required")
        self.assertFalse(result["automatic_retry"])
        self.assertIsNone(result["qa_report"])


if __name__ == "__main__":
    unittest.main()
