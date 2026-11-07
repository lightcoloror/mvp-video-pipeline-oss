from __future__ import annotations

import json
import os
import shutil
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
FIXTURES = ROOT / "tests" / "fixtures" / "content-video-brief-v1" / "upstream-canonical"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# ruff: noqa: E402

from video_creation_pipeline.content_video_cli import main
from video_creation_pipeline.content_video_foundation import (
    build_doctor_report,
    read_artifact_summary,
    validate_canonical_artifact,
)
from video_creation_pipeline.errors import ContractError


class ContentVideoFoundationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = (
            ROOT
            / ".test-runtime"
            / (f"content-video-foundation-{self._testMethodName}-{uuid4().hex}")
        )
        self.root.mkdir(parents=True)
        self.schema = self.root / "content-video-brief.schema.json"
        self.brief = self.root / "content-video-brief.json"
        self.schema.write_text(
            json.dumps(
                {
                    "$schema": "https://json-schema.org/draft/2020-12/schema",
                    "$id": "https://example.invalid/content-video-brief.v1.schema.json",
                    "type": "object",
                    "required": ["schema", "title"],
                    "properties": {
                        "schema": {"const": "content-video-brief.v1"},
                        "title": {"type": "string", "minLength": 1},
                    },
                    "additionalProperties": False,
                }
            ),
            encoding="utf-8",
        )
        self.brief.write_text(
            json.dumps({"schema": "content-video-brief.v1", "title": "Synthetic"}),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_validate_binds_schema_hash_and_standard_time_policy(self) -> None:
        result = validate_canonical_artifact(
            self.brief,
            self.schema,
            max_age_hours=1,
        )

        self.assertIsNone(result["fresh"])
        self.assertFalse(result["freshness_evaluation"]["age_gate_applied"])
        self.assertEqual(
            result["freshness_evaluation"]["decision"],
            "age_not_required",
        )
        self.assertEqual(result["schema_version"], "content-video-brief.v1")
        self.assertEqual(len(result["artifact"]["sha256"]), 64)
        self.assertEqual(len(result["schema_artifact"]["sha256"]), 64)
        self.assertEqual(result["payload"]["title"], "Synthetic")

    def test_standard_content_does_not_expire_by_file_age(self) -> None:
        stale = time.time() - 7200
        os.utime(self.brief, (stale, stale))

        result = validate_canonical_artifact(
            self.brief,
            self.schema,
            max_age_hours=1,
        )

        self.assertFalse(result["freshness_evaluation"]["age_gate_applied"])

    def test_standard_content_ignores_an_unused_age_limit(self) -> None:
        result = validate_canonical_artifact(
            self.brief,
            self.schema,
            max_age_hours=0,
            time_sensitivity="standard",
        )

        self.assertEqual(
            result["freshness_evaluation"]["decision"],
            "age_not_required",
        )

    def test_high_temporal_content_requires_a_positive_age_limit(self) -> None:
        with self.assertRaisesRegex(ContractError, "greater than zero"):
            validate_canonical_artifact(
                self.brief,
                self.schema,
                max_age_hours=0,
                time_sensitivity="high_temporal",
            )

    def test_high_temporal_content_rejects_stale_artifact(self) -> None:
        stale = time.time() - 7200
        os.utime(self.brief, (stale, stale))

        with self.assertRaisesRegex(ContractError, "is stale"):
            validate_canonical_artifact(
                self.brief,
                self.schema,
                max_age_hours=1,
                time_sensitivity="high_temporal",
            )

    def test_unknown_time_sensitivity_is_rejected(self) -> None:
        with self.assertRaisesRegex(ContractError, "time_sensitivity"):
            validate_canonical_artifact(
                self.brief,
                self.schema,
                max_age_hours=1,
                time_sensitivity="automatic_guess",
            )

    def test_validate_rejects_schema_mismatch(self) -> None:
        self.brief.write_text(
            json.dumps({"schema": "content-video-brief.v1", "title": ""}),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ContractError, "schema validation failed"):
            validate_canonical_artifact(
                self.brief,
                self.schema,
                max_age_hours=1,
            )

    def test_read_artifact_returns_hash_bound_summary(self) -> None:
        summary = read_artifact_summary(self.brief)

        self.assertEqual(summary["artifact_schema"], "content-video-brief.v1")
        self.assertEqual(summary["top_level_keys"], ["schema", "title"])
        self.assertFalse(summary["candidate_only"])
        self.assertEqual(summary["artifact"]["path"], str(self.brief.resolve()))

    @patch("video_creation_pipeline.content_video_foundation._version_line")
    @patch("video_creation_pipeline.content_video_foundation.shutil.which")
    def test_doctor_reports_optional_upstream_as_warning(
        self,
        which,
        version_line,
    ) -> None:
        which.side_effect = lambda value: str(Path("tools") / f"{value}.exe")
        version_line.return_value = "synthetic version"

        report = build_doctor_report()

        self.assertEqual(report["status"], "ready")
        warnings = [row for row in report["checks"] if row["status"] == "warning"]
        self.assertEqual(
            {row["id"] for row in warnings},
            {
                "canonical_brief",
                "canonical_schema",
                "research_output",
                "research_schema",
                "output_root",
            },
        )

    def test_cli_validate_emits_machine_readable_receipt(self) -> None:
        with patch("builtins.print") as output:
            exit_code = main(
                [
                    "validate",
                    "--brief",
                    str(FIXTURES / "content-video-brief.v1.json"),
                    "--schema",
                    str(FIXTURES / "content-video-brief.v1.schema.json"),
                    "--research-output",
                    str(FIXTURES / "research-output.v1.json"),
                    "--research-schema",
                    str(FIXTURES / "research-output.v1.schema.json"),
                    "--max-age-hours",
                    "1000000",
                ]
            )

        self.assertEqual(exit_code, 0)
        payload = json.loads(output.call_args.args[0])
        self.assertEqual(payload["status"], "valid")
        self.assertEqual(
            payload["referenced_source_ids"],
            ["source_fixture_primary", "source_fixture_video"],
        )
        self.assertEqual(
            payload["freshness_policy"]["time_sensitivity"],
            "standard",
        )
        self.assertFalse(payload["freshness_policy"]["age_gate_applied"])


if __name__ == "__main__":
    unittest.main()
