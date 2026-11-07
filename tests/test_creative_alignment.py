from __future__ import annotations

# ruff: noqa: E402

import json
import shutil
import sys
import unittest
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
FIXTURES = ROOT / "tests" / "fixtures" / "content-video-brief-v1" / "upstream-canonical"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from video_creation_pipeline.content_video_pipeline import (
    build_content_video_project_plan,
    validate_content_video_inputs,
)
from video_creation_pipeline.contract_io import (
    atomic_write_json,
    canonical_json_sha256,
    file_sha256,
)
from video_creation_pipeline.creative_alignment import (
    build_creative_alignment_plan,
    validate_creative_alignment_plan,
    write_creative_alignment_plan,
)
from video_creation_pipeline.creative_cli import main as creative_cli_main
from video_creation_pipeline.creative_gates import (
    build_approval_record,
    build_release_gate,
    validate_release_gate,
    write_creative_contract,
)
from video_creation_pipeline.errors import ContractError


class CreativeAlignmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = ROOT / ".test-runtime" / f"creative-alignment-{uuid4().hex}"
        self.root.mkdir(parents=True)
        inputs = validate_content_video_inputs(
            FIXTURES / "content-video-brief.v1.json",
            FIXTURES / "content-video-brief.v1.schema.json",
            FIXTURES / "research-output.v1.json",
            FIXTURES / "research-output.v1.schema.json",
            max_age_hours=1_000_000,
        )
        self.project = build_content_video_project_plan(inputs)
        self.project_path = atomic_write_json(
            self.project, self.root / "project-plan.json"
        )
        self.cover = self.root / "cover-candidate.svg"
        self.cover.write_text(
            '<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="720">'
            '<rect width="1280" height="720" fill="#ffffff"/>'
            '<text x="80" y="360">candidate cover</text></svg>',
            encoding="utf-8",
        )
        self.storyboard = self.root / "storyboard-candidate.json"
        self.storyboard.write_text(
            json.dumps({"shots": [{"scene_id": "scene-01"}]}),
            encoding="utf-8",
        )
        self.temporary_voice = self.root / "temporary-voice.wav"
        self.temporary_voice.write_bytes(b"internal-reference-voice")
        self.spec_path = self._write_spec()

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_plan_is_deterministic_and_keeps_reference_assets_internal(self) -> None:
        first = self._build()
        second = self._build()

        self.assertEqual(first["plan_sha256"], second["plan_sha256"])
        self.assertEqual(first["production_mode"]["mode"], "ai_assisted")
        self.assertTrue(first["shooting_handoff"]["required"])
        self.assertTrue(first["release_policy"]["temporary_voice_replacement_required"])
        self.assertTrue(
            all(
                row["intended_use"] == "internal_alignment_only"
                and row["final_material"] is False
                for row in first["reference_video"]["assets"]
            )
        )
        self.assertFalse(first["authority_boundary"]["publish_performed"])

    def test_title_and_cover_hypothesis_are_required_before_plan(self) -> None:
        spec = self._spec()
        spec["selected_title"] = "另一个未绑定标题"
        self._write_json(self.spec_path, spec)
        with self.assertRaisesRegex(ContractError, "selected title"):
            self._build()

        self._write_json(self.spec_path, self._spec())
        self.cover.unlink()
        with self.assertRaisesRegex(ContractError, "cover candidate not found"):
            self._build()

    def test_unknown_scene_and_wrong_voice_extension_fail_closed(self) -> None:
        spec = self._spec()
        spec["reference_assets"][0]["scene_ids"] = ["scene-missing"]
        self._write_json(self.spec_path, spec)
        with self.assertRaisesRegex(ContractError, "unknown scene id"):
            self._build()

        bad_voice = self.root / "temporary-voice.txt"
        bad_voice.write_text("not audio", encoding="utf-8")
        spec = self._spec()
        spec["reference_assets"][1]["path"] = str(bad_voice)
        self._write_json(self.spec_path, spec)
        with self.assertRaisesRegex(ContractError, "supported audio"):
            self._build()

    def test_ai_led_sandbox_still_cannot_claim_release_or_identity_clone(self) -> None:
        spec = self._spec()
        spec["production_mode"] = "ai_led_sandbox"
        self._write_json(self.spec_path, spec)

        plan = self._build()

        self.assertFalse(plan["shooting_handoff"]["required"])
        self.assertFalse(plan["production_mode"]["identity_clone_allowed"])
        self.assertTrue(plan["authority_boundary"]["candidate_only"])
        self.assertFalse(plan["reference_video"]["may_be_used_as_final_program"])

    def test_source_drift_invalidates_alignment_plan(self) -> None:
        plan = self._build()
        self._write_json(
            self.spec_path, {**self._spec(), "production_mode": "live_action_primary"}
        )

        with self.assertRaisesRegex(ContractError, "changed after"):
            validate_creative_alignment_plan(plan)

    def test_release_gate_rejects_temporary_voice_as_final_audio(self) -> None:
        plan_path, plan = self._write_plan()
        approvals, visual = self._approvals(self.temporary_voice)
        manifest = self._render_manifest(visual, plan, self.temporary_voice)

        with self.assertRaisesRegex(ContractError, "temporary voice must be replaced"):
            build_release_gate(
                approvals,
                render_manifest_path=manifest,
                visual_plan_path=visual,
                creative_alignment_plan_path=plan_path,
            )

    def test_release_gate_binds_replacement_voice_and_alignment_freshness(self) -> None:
        plan_path, plan = self._write_plan()
        final_voice = self.root / "final-human-voice.wav"
        final_voice.write_bytes(b"approved-final-human-voice")
        approvals, visual = self._approvals(final_voice)
        manifest = self._render_manifest(visual, plan, final_voice)

        gate = build_release_gate(
            approvals,
            render_manifest_path=manifest,
            visual_plan_path=visual,
            creative_alignment_plan_path=plan_path,
        )
        validate_release_gate(gate)
        self.assertEqual(
            gate["render_manifest_final_voice_audio_sha256s"],
            [file_sha256(final_voice)],
        )

        final_voice.write_bytes(b"changed-final-voice")
        with self.assertRaisesRegex(ContractError, "changed after"):
            validate_release_gate(gate)

    def test_existing_release_gate_remains_compatible_without_alignment(self) -> None:
        final_voice = self.root / "final-human-voice.wav"
        final_voice.write_bytes(b"approved-final-human-voice")
        approvals, visual = self._approvals(final_voice)
        visual_payload = json.loads(visual.read_text(encoding="utf-8"))
        manifest = self.root / "legacy-render-manifest.json"
        self._write_json(
            manifest,
            {"visual_plan_sha256": canonical_json_sha256(visual_payload)},
        )

        gate = build_release_gate(
            approvals,
            render_manifest_path=manifest,
            visual_plan_path=visual,
        )
        validate_release_gate(gate)
        self.assertIsNone(gate["creative_alignment_plan"])

    def test_cli_builds_and_validates_without_rendering(self) -> None:
        output = self.root / "alignment-plan.json"
        code = creative_cli_main(
            [
                "build-creative-alignment",
                "--project-plan",
                str(self.project_path),
                "--cover-candidate",
                str(self.cover),
                "--spec",
                str(self.spec_path),
                "--output",
                str(output),
            ]
        )
        self.assertEqual(code, 0)
        self.assertEqual(
            creative_cli_main(["validate-creative-alignment", "--plan", str(output)]),
            0,
        )
        payload = json.loads(output.read_text(encoding="utf-8"))
        self.assertFalse(payload["authority_boundary"]["render_execution_performed"])

    def _build(self) -> dict:
        return build_creative_alignment_plan(
            self.project_path,
            self.cover,
            self.spec_path,
        )

    def _write_plan(self) -> tuple[Path, dict]:
        plan = self._build()
        path = self.root / "creative-alignment-plan.json"
        write_creative_alignment_plan(plan, path)
        return path, plan

    def _spec(self) -> dict:
        return {
            "schema": "video_creation_pipeline.creative_alignment_spec.v1",
            "selected_title": self.project["title"],
            "production_mode": "ai_assisted",
            "cover_hypothesis": {
                "visual_focus": "一份可核验的证据清单",
                "audience_promise": "展示证据如何进入视频计划",
                "click_reason": "观众可以看到完整的可审计链路",
                "overlay_text": "证据怎样变成视频",
            },
            "reference_assets": [
                {
                    "role": "storyboard_candidate",
                    "path": str(self.storyboard),
                    "scene_ids": ["scene-01"],
                },
                {
                    "role": "temporary_voice",
                    "path": str(self.temporary_voice),
                    "scene_ids": ["scene-01"],
                },
            ],
        }

    def _write_spec(self) -> Path:
        path = self.root / "creative-alignment-spec.json"
        self._write_json(path, self._spec())
        return path

    def _approvals(self, voice_audio: Path) -> tuple[list[Path], Path]:
        script = self.root / "script.json"
        voice = self.root / f"voice-{voice_audio.stem}.json"
        measurements = self.root / f"measurements-{voice_audio.stem}.json"
        visual = self.root / "visual-plan.json"
        review = self.root / "director-review.md"
        self._write_json(script, {"text": "approved script"})
        self._write_json(voice, {"segments": [{"id": "voice-1"}]})
        self._write_json(measurements, {"duration_s": 45.0})
        self._write_json(visual, {"shots": [{"id": "scene-01"}]})
        review.write_text("approved", encoding="utf-8")
        records = [
            build_approval_record(
                "script",
                script,
                approved_by="operator",
                approved_at="2026-08-18T09:00:00+08:00",
            ),
            build_approval_record(
                "voice",
                voice,
                approved_by="operator",
                approved_at="2026-08-18T09:01:00+08:00",
                attachments=[
                    {"role": "voice_measurements", "path": measurements},
                    {"role": "audio", "path": voice_audio},
                ],
            ),
            build_approval_record(
                "visuals",
                visual,
                approved_by="operator",
                approved_at="2026-08-18T09:02:00+08:00",
                attachments=[{"role": "director_review_sheet", "path": review}],
            ),
        ]
        paths = []
        for record in records:
            path = self.root / f"approval-{record['target']}.json"
            write_creative_contract(record, path)
            paths.append(path)
        return paths, visual

    def _render_manifest(
        self, visual: Path, alignment_plan: dict, final_voice: Path
    ) -> Path:
        path = self.root / f"render-manifest-{final_voice.stem}.json"
        visual_payload = json.loads(visual.read_text(encoding="utf-8"))
        self._write_json(
            path,
            {
                "visual_plan_sha256": canonical_json_sha256(visual_payload),
                "creative_alignment_plan_sha256": alignment_plan["plan_sha256"],
                "final_voice_audio_sha256s": [file_sha256(final_voice)],
            },
        )
        return path

    @staticmethod
    def _write_json(path: Path, payload: dict) -> None:
        path.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )


if __name__ == "__main__":
    unittest.main()
