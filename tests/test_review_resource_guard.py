from __future__ import annotations

# ruff: noqa: E402

import os
import shutil
import subprocess
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from video_creation_pipeline.errors import ContractError
from video_creation_pipeline.execution_receipts import execute_ffmpeg_with_receipt
from video_creation_pipeline.execution_receipts import build_ffmpeg_execution_receipt
from video_creation_pipeline.contract_io import artifact_reference
from video_creation_pipeline.review_resource_guard import (
    RESOURCE_PRESSURE_PAUSE_REQUIRED,
    ResourcePressurePauseRequired,
    ReviewResourceGuard,
    ReviewResourcePolicy,
    apply_ffmpeg_thread_limit,
)


class ReviewResourceGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        managed = os.environ.get("TEST_TMP_ROOT")
        parent = Path(managed).resolve() if managed else ROOT / ".test-runtime"
        self.root = parent / f"review-resource-guard-{uuid4().hex}"
        self.root.mkdir(parents=True, exist_ok=True)
        self.source = self.root / "source.mp4"
        self.source.write_bytes(b"source")
        self.policy = ReviewResourcePolicy(ffmpeg_threads=2)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    @staticmethod
    def safe_snapshot() -> dict[str, object]:
        return {
            "cpu_percent": 10.0,
            "available_memory_bytes": 8 * 1024**3,
            "available_memory_percent": 50.0,
            "disk_busy_percent": 5.0,
            "sample_window_ms": 100.0,
            "measurement_errors": [],
        }

    def test_invalid_measurements_do_not_admit_work(self) -> None:
        for field in ("cpu_percent", "available_memory_percent", "disk_busy_percent"):
            for value in (float("nan"), float("inf"), -1, 101, True):
                with self.subTest(field=field, value=value):
                    with self.assertRaises(ResourcePressurePauseRequired):
                        with ReviewResourceGuard(
                            policy=self.policy,
                            lock_path=self.root / "invalid.lock",
                            snapshot_provider=lambda: dict(self.safe_snapshot(), **{field: value}),
                        ):
                            self.fail("invalid telemetry admitted")

    def test_automatic_and_negative_thread_options_become_bounded(self) -> None:
        for value in ("0", "-1", "auto", "8", "1"):
            with self.subTest(value=value):
                command = apply_ffmpeg_thread_limit(
                    ["ffmpeg", "-threads:v", value, "-i", "input", "output"], 2
                )
                self.assertEqual(command[command.index("-threads:v") + 1], "1" if value == "1" else "2")

    def test_supplied_reference_does_not_hide_same_size_content_drift(self) -> None:
        reference = artifact_reference(self.source, "source")
        self.source.write_bytes(b"change")
        output = self.root / "out.log"
        output.write_text("ok", encoding="utf-8")
        with self.assertRaises(ContractError):
            build_ffmpeg_execution_receipt(
                "technical_qa", ["ffmpeg", "-i", str(self.source), "-f", "null", "-"],
                [{"role": "media", "artifact": reference}],
                [{"role": "log", "path": output}],
            )

    @patch("video_creation_pipeline.execution_receipts.shutil.which", return_value="ffmpeg")
    def test_pressure_starts_no_runner(self, _which: Mock) -> None:
        runner = Mock()
        pressured = dict(self.safe_snapshot(), cpu_percent=99.0)
        with self.assertRaises(ResourcePressurePauseRequired) as caught:
            execute_ffmpeg_with_receipt(
                "technical_qa",
                ["ffmpeg", "-i", str(self.source), "-f", "null", "-"],
                [{"role": "media", "path": self.source}],
                [{"role": "log", "path": self.root / "not-created.log"}],
                self.root / "not-created.json",
                resource_policy=self.policy,
                resource_snapshot_provider=lambda: pressured,
                runner=runner,
                resource_lock_path=self.root / "slot.lock",
            )

        self.assertEqual(caught.exception.code, RESOURCE_PRESSURE_PAUSE_REQUIRED)
        self.assertFalse(caught.exception.as_dict()["automatic_retry"])
        runner.assert_not_called()

    @patch("video_creation_pipeline.execution_receipts.shutil.which", return_value="ffmpeg")
    def test_threads_priority_and_guard_metadata_are_receipted(
        self, _which: Mock
    ) -> None:
        output = self.root / "detector.log"

        def runner(args, **kwargs):
            output.write_text("ok", encoding="utf-8")
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        runner_mock = Mock(side_effect=runner)
        receipt = execute_ffmpeg_with_receipt(
            "technical_qa",
            ["ffmpeg", "-i", str(self.source), "-f", "null", "-"],
            [{"role": "media", "path": self.source}],
            [{"role": "detector_log", "path": output}],
            self.root / "receipt.json",
            resource_policy=self.policy,
            resource_snapshot_provider=self.safe_snapshot,
            runner=runner_mock,
            resource_lock_path=self.root / "slot.lock",
        )

        executed = runner_mock.call_args.args[0]
        self.assertIn("-threads", executed)
        self.assertEqual(executed[executed.index("-threads") + 1], "2")
        if os.name == "nt":
            self.assertEqual(
                runner_mock.call_args.kwargs["creationflags"],
                subprocess.BELOW_NORMAL_PRIORITY_CLASS,
            )
        guard = receipt["resource_guard"]
        self.assertEqual(guard["policy_id"], "video-review-resource-guard-v1")
        self.assertEqual(guard["snapshot"]["cpu_percent"], 10.0)
        self.assertEqual(guard["ffmpeg"]["thread_limit"], 2)
        self.assertEqual(guard["ffmpeg"]["priority"]["requested"], "below_normal")
        self.assertTrue(guard["lock"]["acquired"])
        self.assertTrue(guard["lock"]["released"])
        self.assertFalse(guard["cache"]["snapshot_reused"])
        self.assertIn("-filter_threads", receipt["executed_command"])

    @patch("video_creation_pipeline.execution_receipts.shutil.which", return_value="ffmpeg")
    def test_lock_released_after_failure_timeout_and_exception(
        self, _which: Mock
    ) -> None:
        cases = [
            lambda *_args, **_kwargs: subprocess.CompletedProcess([], 4, "", "bad"),
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                subprocess.TimeoutExpired(["ffmpeg"], 1)
            ),
            lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("boom")),
        ]
        for index, runner in enumerate(cases):
            lock_path = self.root / f"slot-{index}.lock"
            with self.subTest(index=index), self.assertRaises(ContractError):
                execute_ffmpeg_with_receipt(
                    "technical_qa",
                    ["ffmpeg", "-i", str(self.source), "-f", "null", "-"],
                    [{"role": "media", "path": self.source}],
                    [{"role": "log", "path": self.root / f"out-{index}.log"}],
                    self.root / f"receipt-{index}.json",
                    resource_policy=self.policy,
                    resource_snapshot_provider=self.safe_snapshot,
                    runner=runner,
                    resource_lock_path=lock_path,
                )
            with ReviewResourceGuard(
                policy=self.policy,
                lock_path=lock_path,
                snapshot_provider=self.safe_snapshot,
            ):
                pass

    @patch("video_creation_pipeline.execution_receipts.shutil.which", return_value="ffmpeg")
    def test_concurrent_calls_never_start_more_than_one_runner(
        self, _which: Mock
    ) -> None:
        entered = threading.Event()
        release = threading.Event()
        errors: list[BaseException] = []
        active = 0
        maximum = 0
        counter_lock = threading.Lock()
        first_output = self.root / "first.log"

        def blocking_runner(args, **_kwargs):
            nonlocal active, maximum
            with counter_lock:
                active += 1
                maximum = max(maximum, active)
            entered.set()
            release.wait(timeout=5)
            first_output.write_text("ok", encoding="utf-8")
            with counter_lock:
                active -= 1
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        def first_call() -> None:
            try:
                execute_ffmpeg_with_receipt(
                    "technical_qa",
                    ["ffmpeg", "-i", str(self.source), "-f", "null", "-"],
                    [{"role": "media", "path": self.source}],
                    [{"role": "log", "path": first_output}],
                    self.root / "first.json",
                    resource_policy=self.policy,
                    resource_snapshot_provider=self.safe_snapshot,
                    runner=blocking_runner,
                    resource_lock_path=self.root / "shared.lock",
                )
            except BaseException as exc:  # pragma: no cover - assertion below
                errors.append(exc)

        thread = threading.Thread(target=first_call)
        thread.start()
        self.assertTrue(entered.wait(timeout=3))
        second_runner = Mock()
        with self.assertRaises(ResourcePressurePauseRequired):
            execute_ffmpeg_with_receipt(
                "technical_qa",
                ["ffmpeg", "-i", str(self.source), "-f", "null", "-"],
                [{"role": "media", "path": self.source}],
                [{"role": "log", "path": self.root / "second.log"}],
                self.root / "second.json",
                resource_policy=self.policy,
                resource_snapshot_provider=self.safe_snapshot,
                runner=second_runner,
                resource_lock_path=self.root / "shared.lock",
            )
        second_runner.assert_not_called()
        release.set()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(maximum, 1)
        with ReviewResourceGuard(
            policy=self.policy,
            lock_path=self.root / "shared.lock",
            snapshot_provider=self.safe_snapshot,
        ):
            pass


if __name__ == "__main__":
    unittest.main()
