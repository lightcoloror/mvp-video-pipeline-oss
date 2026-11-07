from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tomllib
import unittest
from datetime import UTC, datetime
from pathlib import Path

from video_creation_pipeline.contract_io import canonical_json_sha256
from video_creation_pipeline.capability_truth import (
    validate_content_synthetic_run_receipt,
)
from video_creation_pipeline.content_video_synthetic import (
    CONTENT_VIDEO_ATOMIC_OUTPUT_MANIFEST_SCHEMA,
    validate_content_video_atomic_output_manifest,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "content-video-brief-v1" / "upstream-canonical"


class DemoPipelineE2eTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = ROOT / ".test-runtime" / self._testMethodName
        if self.root.exists():
            shutil.rmtree(self.root)
        self.root.mkdir(parents=True)

    def tearDown(self) -> None:
        if self.root.exists():
            shutil.rmtree(self.root)

    @unittest.skipUnless(
        shutil.which("ffmpeg") and shutil.which("ffprobe"),
        "FFmpeg and ffprobe are required for the offline end-to-end test",
    )
    def test_demo_pipeline_renders_and_passes_automatic_technical_qa(self) -> None:
        workspace = self.root / "workspace"
        environment = os.environ.copy()
        environment["MVP_VIDEO_WORKSPACE"] = str(workspace)
        inputs = self._fresh_synthetic_inputs()

        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "run_demo.py"),
                "--brief",
                str(inputs["brief"]),
                "--schema",
                str(inputs["brief_schema"]),
                "--research-output",
                str(inputs["research"]),
                "--research-schema",
                str(inputs["research_schema"]),
            ],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=180,
            check=False,
        )

        if result.returncode != 0:
            self.fail(
                "offline canonical demo failed\n"
                f"stdout tail:\n{result.stdout[-4000:]}\n"
                f"stderr tail:\n{result.stderr[-4000:]}"
            )
        project = workspace / "demo_project"
        report_path = project / "qa" / "qa-report.json"
        report = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertTrue(report["readiness"]["technical_ready"])
        self.assertFalse(report["readiness"]["release_eligible"])
        self.assertFalse(report["authority_boundary"]["automatic_publish"])
        self.assertEqual(report["readiness"]["blocking_failure_count"], 0)
        self.assertTrue((project / "publish" / "candidate-synthetic.mp4").is_file())
        self.assertTrue((project / "publish" / "cover-synthetic.png").is_file())
        self.assertTrue(
            (project / "render" / "receipts" / "synthetic-technical-qa.json").is_file()
        )
        self.assertTrue((project / "project-plan.json").is_file())
        self.assertTrue((project / "render-manifest.json").is_file())
        atomic_output_path = project / "render" / "atomic-output-manifest.json"
        self.assertTrue(atomic_output_path.is_file())
        run_receipt = json.loads(
            (project / "synthetic-run-receipt.json").read_text(encoding="utf-8")
        )
        validate_content_synthetic_run_receipt(run_receipt)
        self.assertEqual(run_receipt["status"], "completed_candidate_only")
        self.assertTrue(run_receipt["synthetic_material_only"])
        self.assertFalse(run_receipt["production_success_claimed"])
        self.assertFalse(run_receipt["upload_performed"])
        self.assertFalse(run_receipt["publish_performed"])
        atomic_output = json.loads(atomic_output_path.read_text(encoding="utf-8"))
        validate_content_video_atomic_output_manifest(atomic_output)
        self.assertEqual(
            atomic_output["schema"], CONTENT_VIDEO_ATOMIC_OUTPUT_MANIFEST_SCHEMA
        )
        self.assertTrue(
            atomic_output["authority_boundary"]["all_ffmpeg_outputs_accounted_for"]
        )
        self.assertFalse(
            atomic_output["authority_boundary"]["direct_final_media_write_allowed"]
        )
        self.assertEqual(
            atomic_output["ffmpeg_receipt_count"],
            run_receipt["ffmpeg_receipt_count"],
        )
        self.assertTrue(
            all(
                row["commit_mode"] in {"copy_then_replace", "atomic_text_replace"}
                for row in atomic_output["outputs"]
            )
        )
        self.assertTrue(
            all(
                row["ffmpeg_output"]["sha256"] == row["committed_output"]["sha256"]
                for row in atomic_output["outputs"]
            )
        )
        music_plan = json.loads(
            (project / "audio" / "synthetic-music-edit-plan.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertTrue(music_plan["readiness"]["render_eligible"])
        self.assertFalse(music_plan["readiness"]["manual_review_required"])
        music_receipt = json.loads(
            (project / "render" / "receipts" / "synthetic-music-mix.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(music_receipt["operation"], "music_mix")
        self.assertEqual(
            music_receipt["outlet_id"],
            "video_creation_pipeline.single_ffmpeg_outlet",
        )
        receipts = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in (project / "render" / "receipts").glob("*.json")
        ]
        self.assertEqual(len(receipts), run_receipt["ffmpeg_receipt_count"])
        self.assertTrue(
            all(
                receipt["outlet_id"] == "video_creation_pipeline.single_ffmpeg_outlet"
                for receipt in receipts
            )
        )
        self.assertNotIn("00_create_demo_assets.py", result.stdout)
        self.assertIn(
            "Canonical MVP pipeline completed as candidate_only", result.stdout
        )
        self.assertIn(str(project / "synthetic-run-receipt.json"), result.stdout)

    def test_demo_requires_explicit_canonical_inputs(self) -> None:
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "run_demo.py")],
            cwd=ROOT,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("--brief", result.stderr)

    def test_installed_entry_point_targets_canonical_demo(self) -> None:
        with (ROOT / "pyproject.toml").open("rb") as handle:
            project = tomllib.load(handle)["project"]
        self.assertEqual(
            project["scripts"]["video-canonical-demo"],
            "video_creation_pipeline.demo_cli:main",
        )

    def _fresh_synthetic_inputs(self) -> dict[str, Path]:
        input_root = self.root / "canonical-inputs"
        input_root.mkdir()
        research = json.loads(
            (FIXTURES / "research-output.v1.json").read_text(encoding="utf-8")
        )
        brief = json.loads(
            (FIXTURES / "content-video-brief.v1.json").read_text(encoding="utf-8")
        )
        run_clock = datetime.now(UTC).replace(microsecond=0).isoformat()
        research["created_at"] = run_clock
        brief["provenance"]["generated_at"] = run_clock
        brief["provenance"]["input_sha256"] = canonical_json_sha256(research)

        paths = {
            "brief": input_root / "content-video-brief.v1.json",
            "brief_schema": input_root / "content-video-brief.v1.schema.json",
            "research": input_root / "research-output.v1.json",
            "research_schema": input_root / "research-output.v1.schema.json",
        }
        paths["brief"].write_text(
            json.dumps(brief, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        paths["research"].write_text(
            json.dumps(research, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        shutil.copyfile(
            FIXTURES / "content-video-brief.v1.schema.json", paths["brief_schema"]
        )
        shutil.copyfile(
            FIXTURES / "research-output.v1.schema.json", paths["research_schema"]
        )
        return paths


if __name__ == "__main__":
    unittest.main()
