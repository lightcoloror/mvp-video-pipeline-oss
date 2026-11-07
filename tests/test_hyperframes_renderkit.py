from __future__ import annotations

# ruff: noqa: E402

import json
import os
import shutil
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
FIXTURES = ROOT / "tests" / "fixtures" / "semantic-motion"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from video_creation_pipeline.errors import ContractError
from video_creation_pipeline.generation_contracts import (
    build_shot_generation_unit,
    write_generation_contract,
)
from video_creation_pipeline.hyperframes_renderkit import (
    ACCELERATED_ROUTE,
    OFFICIAL_ROUTE,
    RENDERKIT_SOURCE_COMMIT,
    build_render_assurance_plan,
    run_render_assurance_preflight,
    validate_render_assurance_plan,
    validate_render_preflight_receipt,
    write_render_assurance_plan,
)
from video_creation_pipeline.production_strategy import (
    build_visual_production_plan,
    write_production_strategy_contract,
)
from video_creation_pipeline.semantic_motion import (
    build_semantic_motion_plan,
    materialize_hyperframes_project,
    write_semantic_motion_plan,
)
from video_creation_pipeline.vkp_bridge import build_edit_job, write_edit_job


class HyperFramesRenderKitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = ROOT / ".test-runtime" / f"renderkit-{uuid4().hex}"
        shutil.copytree(FIXTURES, self.root)
        bundle = self.root / "vkp-bundle"
        shutil.copytree(ROOT / "tests" / "fixtures" / "vkp_bundle", bundle)
        edit_job = self.root / "edit-job.json"
        write_edit_job(build_edit_job(bundle), edit_job)
        shot_unit = self.root / "shot-unit.json"
        write_generation_contract(
            build_shot_generation_unit(
                edit_job,
                "storyboard-0001",
                generator="remotion",
                visual_prompt="Synthetic RenderKit assurance fixture.",
            ),
            shot_unit,
        )
        visual_plan = self.root / "visual-production-plan.json"
        write_production_strategy_contract(
            build_visual_production_plan(shot_unit), visual_plan
        )
        spec = json.loads((self.root / "build-spec.json").read_text(encoding="utf-8"))
        semantic = build_semantic_motion_plan(
            self.root / "packaging-plan.json",
            self.root / "script-anchor.json",
            self.root / "source-evidence.svg",
            visual_plan,
            asset_id=spec["asset_id"],
            media_type=spec["media_type"],
            cues=spec["cues"],
            width=spec["width"],
            height=spec["height"],
            fps=spec["fps"],
            duration_s=spec["duration_s"],
            evidence_status=spec["evidence_status"],
            authorization_status=spec["authorization_status"],
            deidentified=spec["deidentified"],
        )
        self.semantic_path = write_semantic_motion_plan(
            semantic, self.root / "semantic-motion-plan.json"
        )
        self.project = self.root / "project"
        materialize_hyperframes_project(self.semantic_path, self.project)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_official_route_binds_reviewed_source_without_vendoring_it(self) -> None:
        source = self._write_renderkit_source(eligible=False)
        with self._fixed_commit():
            first = build_render_assurance_plan(
                self.semantic_path, self.project, source
            )
            second = build_render_assurance_plan(
                self.semantic_path, self.project, source
            )
            validate_render_assurance_plan(first)

        self.assertEqual(first["plan_sha256"], second["plan_sha256"])
        self.assertEqual(first["selected_route"], OFFICIAL_ROUTE)
        self.assertEqual(
            first["renderkit_source"]["project_license_status"],
            "not_granted_at_reviewed_commit",
        )
        self.assertEqual(
            first["motion_identity"]["upstream_motion_contract"],
            "not_used_for_native_waapi_authoring",
        )
        self.assertFalse(first["route_policy"]["automatic_fallback_allowed"])

    def test_official_route_records_blockers_without_switching_renderer(self) -> None:
        source = self._write_renderkit_source(eligible=False)
        with self._fixed_commit():
            plan = build_render_assurance_plan(self.semantic_path, self.project, source)
            plan_path = write_render_assurance_plan(plan, self.root / "assurance.json")
            receipt = run_render_assurance_preflight(
                plan_path, self.root / "preflight.json"
            )
            validate_render_preflight_receipt(receipt)

        self.assertEqual(receipt["status"], "passed_for_official_hyperframes_faithful")
        self.assertEqual(receipt["compatibility_scan"]["blocker_count"], 1)
        self.assertFalse(receipt["decision"]["accelerated_route_allowed"])
        self.assertFalse(receipt["decision"]["static_scan_is_visual_proof"])
        self.assertFalse(receipt["authority_boundary"]["automatic_fallback_performed"])

    def test_accelerated_route_stays_blocked_without_supported_host_and_runtime(
        self,
    ) -> None:
        source = self._write_renderkit_source(eligible=True)
        with self._fixed_commit():
            plan = build_render_assurance_plan(
                self.semantic_path,
                self.project,
                source,
                selected_route=ACCELERATED_ROUTE,
            )
            plan_path = write_render_assurance_plan(
                plan, self.root / "accelerated-assurance.json"
            )
            receipt = run_render_assurance_preflight(
                plan_path, self.root / "accelerated-preflight.json"
            )
            validate_render_preflight_receipt(receipt)

        self.assertEqual(receipt["status"], "blocked_for_renderkit_accelerated_handoff")
        self.assertFalse(receipt["decision"]["accelerated_route_allowed"])
        self.assertIn("custom runtime", receipt["decision"]["reason"])

    def test_source_script_drift_fails_closed(self) -> None:
        source = self._write_renderkit_source(eligible=True)
        with self._fixed_commit():
            plan = build_render_assurance_plan(self.semantic_path, self.project, source)
        delivery = (
            source
            / "skills"
            / "hyperframes-render-optimization"
            / "scripts"
            / "delivery.mjs"
        )
        delivery.write_text("console.log('{}');\n", encoding="utf-8")
        with (
            self._fixed_commit(),
            self.assertRaisesRegex(ContractError, "binding drifted"),
        ):
            validate_render_assurance_plan(plan)

    def test_linux_runtime_binding_still_does_not_authorize_execution(self) -> None:
        source = self._write_renderkit_source(eligible=True)
        runtime = self.root / "custom-electron"
        runtime.write_bytes(b"synthetic-runtime-identity")
        with (
            self._fixed_commit(),
            patch(
                "video_creation_pipeline.hyperframes_renderkit.platform.system",
                return_value="Linux",
            ),
            patch(
                "video_creation_pipeline.hyperframes_renderkit.platform.machine",
                return_value="x86_64",
            ),
        ):
            plan = build_render_assurance_plan(
                self.semantic_path,
                self.project,
                source,
                selected_route=ACCELERATED_ROUTE,
                runtime_path=runtime,
            )
            plan_path = write_render_assurance_plan(
                plan, self.root / "linux-assurance.json"
            )
            receipt = run_render_assurance_preflight(
                plan_path, self.root / "linux-preflight.json"
            )
            validate_render_preflight_receipt(receipt)

        self.assertEqual(receipt["status"], "blocked_for_renderkit_accelerated_handoff")
        self.assertFalse(receipt["decision"]["accelerated_route_allowed"])
        self.assertIn("does not execute", receipt["decision"]["reason"])

    def test_reviewed_source_scans_the_managed_project_when_configured(self) -> None:
        raw_source = os.environ.get("HYPERFRAMES_RENDERKIT_SOURCE")
        if not raw_source:
            self.skipTest("HYPERFRAMES_RENDERKIT_SOURCE is not configured")
        source = Path(raw_source).resolve()
        plan = build_render_assurance_plan(self.semantic_path, self.project, source)
        plan_path = write_render_assurance_plan(
            plan, self.root / "real-source-assurance.json"
        )
        receipt = run_render_assurance_preflight(
            plan_path, self.root / "real-source-preflight.json"
        )
        validate_render_preflight_receipt(receipt)
        self.assertEqual(receipt["status"], "passed_for_official_hyperframes_faithful")
        self.assertGreaterEqual(receipt["compatibility_scan"]["scanned_files"], 1)

    def _write_renderkit_source(self, *, eligible: bool) -> Path:
        source = self.root / ("renderkit-safe" if eligible else "renderkit-blocked")
        scripts = source / "skills" / "hyperframes-render-optimization" / "scripts"
        scripts.mkdir(parents=True)
        blocker_count = 0 if eligible else 1
        scan = {
            "kind": "hyperframes-delivery-compatibility-scan",
            "schemaVersion": 2,
            "projectRoot": "fixture",
            "entry": "index.html",
            "projectScanSha256": "a" * 64,
            "scanMode": "configured-entry-closure",
            "scannedFiles": 1,
            "findingCount": blocker_count,
            "blockerCount": blocker_count,
            "reviewCount": 0,
            "acknowledgedFindingCount": 0,
            "acknowledgedRuleIds": [],
            "legacyApprovedRuleIds": [],
            "recommendedBackend": (
                "modified-electron-layered"
                if eligible
                else "faithful-chromium-screenshot"
            ),
            "productionEligibleForLayered": eligible,
            "approvalSemantics": "fixture",
            "limitation": "Static conservative scan only; not visual proof.",
            "findings": (
                []
                if eligible
                else [
                    {
                        "severity": "blocker",
                        "ruleId": "fixture-blocker",
                        "file": "index.html",
                        "line": 1,
                        "evidence": "fixture",
                    }
                ]
            ),
        }
        script = (
            f"console.log({json.dumps(json.dumps(scan))});\n"
            f"process.exitCode = {0 if eligible else 2};\n"
        )
        (scripts / "delivery.mjs").write_text(script, encoding="utf-8")
        (scripts / "motion_contract.mjs").write_text(
            "export const fixture = true;\n", encoding="utf-8"
        )
        (source / "VERSION.lock").write_text(
            "electron=43.3.0\nchromium=150.0.7871.212\n", encoding="utf-8"
        )
        return source

    @staticmethod
    def _fixed_commit():
        return patch(
            "video_creation_pipeline.hyperframes_renderkit._git_head",
            return_value=RENDERKIT_SOURCE_COMMIT,
        )


if __name__ == "__main__":
    unittest.main()
