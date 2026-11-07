from __future__ import annotations

# ruff: noqa: E402

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

from video_creation_pipeline.adapter_registry import (
    build_adapter_plan,
    validate_adapter_plan,
    write_adapter_plan,
)
from video_creation_pipeline.boundary_refinement import (
    SNAP_SCRIPT_SHA256,
    VIDEOCUT_KIT_COMMIT,
    refine_selection_boundaries,
    write_refined_selection,
)
from video_creation_pipeline.errors import ContractError
from video_creation_pipeline.provider_cli import main as provider_cli_main
from video_creation_pipeline.provider_handoff import (
    build_provider_request,
    write_provider_request,
)
from video_creation_pipeline.vkp_bridge import build_edit_job, write_edit_job

FIXTURE = ROOT / "tests" / "fixtures" / "vkp_bundle"
VIDEOCUT_KIT = ROOT.parent / "sources" / "videocut-kit"


@unittest.skipUnless(
    shutil.which("node") and VIDEOCUT_KIT.is_dir(),
    "requires Node.js and the reviewed local videocut-kit source",
)
class BoundaryAndAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = ROOT / ".test-runtime" / self._testMethodName
        if self.root.exists():
            shutil.rmtree(self.root)
        self.root.mkdir(parents=True)
        self.bundle = self.root / "bundle"
        shutil.copytree(FIXTURE, self.bundle)
        self.media = self.root / "talking-head.mp4"
        self.media.write_bytes(b"test-video-bytes")
        self.edit_job_path = self.root / "video-creation" / "edit-job.json"
        job = build_edit_job(self.bundle, remote_policy=self._remote_policy())
        write_edit_job(job, self.edit_job_path)
        self.selection_path = self.root / "video-creation" / "human-selection.json"
        self.selection_path.write_text(
            json.dumps(
                {
                    "schema": "video_creation_pipeline.human_selection.v1",
                    "edit_job_sha256": file_sha256(self.edit_job_path),
                    "selections": [{"candidate_id": "timeline-0001"}],
                    "operator_confirmation": {
                        "confirmed": True,
                        "confirmed_at": "2026-07-15T12:05:00+00:00",
                        "method": "visible_operator_selection",
                    },
                }
            ),
            encoding="utf-8",
        )
        self.tokens_path = self.root / "transcript.tokens.json"
        self.tokens_path.write_text(
            json.dumps(
                [
                    {"id": "t0", "start": 0, "end": 4.8, "isGap": False},
                    {"id": "t1", "start": 5.2, "end": 6.0, "isGap": False},
                ]
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        if self.root.exists():
            shutil.rmtree(self.root)

    def test_runs_pinned_videocut_kit_and_builds_provider_request(self) -> None:
        refined = refine_selection_boundaries(
            self.edit_job_path,
            self.selection_path,
            self.tokens_path,
            videocut_kit_root=VIDEOCUT_KIT,
        )
        row = refined["selections"][0]
        run = refined["boundary_refinement_run"]

        self.assertEqual(row["confirmed_interval"], {"start": 0.0, "end": 5.08})
        self.assertTrue(row["boundary_refinement"]["changed"])
        self.assertEqual(
            row["boundary_refinement"]["boundary_artifact_validation"], "passed"
        )
        self.assertEqual(run["videocut_kit"]["commit"], VIDEOCUT_KIT_COMMIT)
        self.assertEqual(run["videocut_kit"]["script_sha256"], SNAP_SCRIPT_SHA256)
        self.assertEqual(run["network_calls"], 0)
        self.assertFalse(run["ffmpeg_executed"])

        refined_path = self.root / "video-creation" / "human-selection-refined.json"
        write_refined_selection(refined, refined_path)
        tampered_path = self.root / "video-creation" / "tampered-selection.json"
        tampered = json.loads(refined_path.read_text(encoding="utf-8"))
        tampered["selections"][0]["confirmed_interval"]["end"] = 4.0
        tampered_path.write_text(json.dumps(tampered), encoding="utf-8")
        with self.assertRaisesRegex(
            ContractError, "does not bind the exact selections"
        ):
            build_provider_request(self.edit_job_path, tampered_path)

        request = build_provider_request(self.edit_job_path, refined_path)

        self.assertEqual(
            request["timeline_items"][0]["confirmed_interval"]["end"], 5.08
        )
        self.assertEqual(
            request["timeline_items"][0]["interval_source"],
            "operator_confirmed_override",
        )

    def test_cli_refines_then_builds_a_nonexecuting_adapter_plan(self) -> None:
        refined_path = self.root / "video-creation" / "refined.json"
        refine_code = provider_cli_main(
            [
                "refine-boundaries",
                "--edit-job",
                str(self.edit_job_path),
                "--selection",
                str(self.selection_path),
                "--tokens",
                str(self.tokens_path),
                "--videocut-kit-root",
                str(VIDEOCUT_KIT),
                "--output",
                str(refined_path),
            ]
        )
        self.assertEqual(refine_code, 0)
        request = build_provider_request(self.edit_job_path, refined_path)
        request_path = self.root / "video-creation" / "provider-request.json"
        write_provider_request(request, request_path)
        plan_path = self.root / "video-creation" / "adapter-plan.json"

        plan_code = provider_cli_main(
            [
                "build-adapter-plan",
                "--request",
                str(request_path),
                "--output",
                str(plan_path),
            ]
        )

        self.assertEqual(plan_code, 0)
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        self.assertEqual(plan["adapter"]["adapter_id"], "provider_mcp")
        self.assertEqual(plan["adapter"]["status"], "awaiting_provider_plugin")
        self.assertEqual(
            plan["adapter"]["invocation"]["provider_request_path"],
            str(request_path.resolve()),
        )
        self.assertFalse(plan["adapter"]["execution_supported_by_this_repo"])
        self.assertFalse(plan["execution_boundary"]["network_execution_performed"])
        validate_adapter_plan(plan)

    def test_requires_explicit_unlicensed_videocut_kit_path(self) -> None:
        with self.assertRaisesRegex(ContractError, "no root license"):
            refine_selection_boundaries(
                self.edit_job_path,
                self.selection_path,
                self.tokens_path,
            )

    def test_rejects_unreviewed_videocut_kit_source_and_plan_drift(self) -> None:
        unreviewed = self.root / "videocut-kit"
        script = unreviewed / "scripts" / "snap_boundaries.js"
        script.parent.mkdir(parents=True)
        script.write_text("console.log('different source')", encoding="utf-8")

        with self.assertRaisesRegex(ContractError, "hash does not match"):
            refine_selection_boundaries(
                self.edit_job_path,
                self.selection_path,
                self.tokens_path,
                videocut_kit_root=unreviewed,
            )

        refined = refine_selection_boundaries(
            self.edit_job_path,
            self.selection_path,
            self.tokens_path,
            videocut_kit_root=VIDEOCUT_KIT,
        )
        tampered_refinement = json.loads(json.dumps(refined))
        tampered_refinement["selections"][0]["confirmed_interval"]["end"] = 4.0
        with self.assertRaisesRegex(ContractError, "boundary refinement run integrity"):
            write_refined_selection(
                tampered_refinement, self.root / "tampered-refinement.json"
            )
        refined_path = self.root / "video-creation" / "refined.json"
        write_refined_selection(refined, refined_path)
        request = build_provider_request(self.edit_job_path, refined_path)
        request_path = self.root / "video-creation" / "provider-request.json"
        write_provider_request(request, request_path)
        plan = build_adapter_plan(request_path)
        tampered_plan = json.loads(json.dumps(plan))
        tampered_plan["adapter"]["status"] = "ready"
        tampered_plan["plan_sha256"] = payload_sha256(tampered_plan, "plan_sha256")
        with self.assertRaisesRegex(ContractError, "differs from the exact"):
            validate_adapter_plan(tampered_plan)
        request_path.write_text("{}", encoding="utf-8")

        with self.assertRaisesRegex(ContractError, "changed after adapter"):
            write_adapter_plan(plan, self.root / "adapter-plan.json")

    def _remote_policy(self) -> dict:
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
                    {
                        "path": str(self.media),
                        "purpose": "editable_timeline_finishing",
                    }
                ],
            },
        }


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def payload_sha256(payload: dict, field: str) -> str:
    value = dict(payload)
    value.pop(field, None)
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


if __name__ == "__main__":
    unittest.main()
