from __future__ import annotations

import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import test_safe_media_scene_plan as safe_media_fixtures
from test_semantic_editorial import make_fixture

from video_creation_pipeline.contract_io import (
    atomic_write_json,
    file_sha256,
    payload_sha256,
    read_json_object,
)
from video_creation_pipeline.creative_gates import build_approval_record
from video_creation_pipeline.errors import ContractError
from video_creation_pipeline.photo_layout import JUSTIFIED_LAYOUT_SOURCE_COMMIT
from video_creation_pipeline.remotion_project import (
    _project_data,
    build_remotion_timeline_preview,
)
from video_creation_pipeline.safe_media_scene_plan import build_safe_media_scene_plan
from video_creation_pipeline.semantic_editorial import (
    build_editorial_packaging_projection,
    build_semantic_editorial_plan,
)
from video_creation_pipeline.semantic_editorial_cli import main
from video_creation_pipeline.semantic_render_plan import (
    build_semantic_render_plan,
    validate_semantic_render_plan,
    write_semantic_render_plan,
)


class SemanticRenderPlanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ["TEST_TMP_ROOT"])
        self.root = Path(self.temp.name)
        self.timebase, self.spec = make_fixture(self.root)
        self.plan = self.root / "plan.json"
        self.packaging = self.root / "packaging.json"
        self.refresh()

    def tearDown(self):
        self.temp.cleanup()

    def refresh(self):
        atomic_write_json(
            build_semantic_editorial_plan(self.timebase, self.root, self.spec),
            self.plan,
        )
        atomic_write_json(
            build_editorial_packaging_projection(self.plan), self.packaging
        )

    def approval(self, path, target="storyboard"):
        return atomic_write_json(
            build_approval_record(
                target,
                path,
                approved_by="synthetic-reviewer",
                approved_at="2026-09-08T01:30:00+00:00",
                comment="Synthetic only",
            ),
            self.root / (target + "-approval.json"),
        )

    def build(self, **kwargs):
        return build_semantic_render_plan(self.plan, self.packaging, **kwargs)

    def test_real_production_converter_and_double_run(self):
        with patch(
            "video_creation_pipeline.remotion_project._project_data",
            wraps=_project_data,
        ) as converter:
            first = self.build()
            self.assertEqual(first, self.build())
            self.assertEqual(converter.call_count, 2)
        self.assertEqual(first["renderer"]["data"]["durationInFrames"], 300)
        self.assertFalse(first["renderer"]["data"]["sourceMediaRendered"])
        self.assertEqual(len(first["timeline"]), 5)
        self.assertIn("missing_material", first["readiness"]["blockers"])
        validate_semantic_render_plan(first)

    def test_offset_not_applied_twice(self):
        tb = read_json_object(self.timebase, "fixture")
        tb["duration_ms"] += 40000
        for key in ("semantic_segments", "sentence_segments"):
            for row in tb[key]:
                row["start_ms"] += 40000
                row["end_ms"] += 40000
        tb["contract_sha256"] = payload_sha256(tb, "contract_sha256")
        atomic_write_json(tb, self.timebase)
        self.refresh()
        value = self.build()
        self.assertEqual(value["timeline"][0]["source_interval_ms"]["start"], 40000)
        self.assertEqual(value["timeline"][0]["renderer_start_frame"], 1200)
        self.assertEqual(value["adapter_offset_applied_ms"], 0)

    def test_edits_and_low_confidence_preserved_even_with_approval(self):
        for action in ("delete", "compress", "move", "review_required"):
            with self.subTest(action=action):
                spec = read_json_object(self.spec, "spec")
                spec["decisions"][1].update(
                    action=action, confidence=0.3, after_segment_id="segment-4"
                )
                if action != "move":
                    spec["decisions"][1].pop("after_segment_id")
                atomic_write_json(spec, self.spec)
                self.refresh()
                value = self.build(approval_path=self.approval(self.plan))
                self.assertEqual(len(value["timeline"]), 5)
                self.assertEqual(value["timeline"][1]["action_applied"], "keep")
                self.assertIn(
                    "editorial_resolution_required", value["readiness"]["blockers"]
                )

    def test_high_confidence_delete_still_not_applied(self):
        spec = read_json_object(self.spec, "spec")
        spec["decisions"][1].update(action="delete", confidence=0.95)
        atomic_write_json(spec, self.spec)
        self.refresh()
        value = self.build(approval_path=self.approval(self.plan))
        self.assertEqual(value["timeline"][1]["action_applied"], "keep")
        self.assertTrue(value["review_queue"])

    def test_approval_clears_only_its_own_gate(self):
        value = self.build(approval_path=self.approval(self.plan))
        self.assertNotIn("storyboard_approval_required", value["readiness"]["blockers"])
        self.assertFalse(value["readiness"]["render_eligible"])

    def test_wrong_target_and_stale_review_rejected(self):
        with self.assertRaises(ContractError):
            self.build(approval_path=self.approval(self.plan, "materials"))
        review = self.approval(self.plan)
        spec = read_json_object(self.spec, "spec")
        spec["decisions"][0]["keep_reason"] = "new review scope"
        atomic_write_json(spec, self.spec)
        self.refresh()
        with self.assertRaises(ContractError):
            self.build(approval_path=review)

    def test_nested_media_hash_drift_rejected(self):
        value = self.build()
        (self.root / "synthetic-media.txt").write_text("drift", encoding="utf-8")
        with self.assertRaises(ContractError):
            validate_semantic_render_plan(value)

    def test_spec_and_timebase_drift_rejected(self):
        for path in (self.spec, self.timebase):
            with self.subTest(path=path.name):
                original = path.read_bytes()
                value = self.build()
                path.write_bytes(original + b" ")
                with self.assertRaises(ContractError):
                    validate_semantic_render_plan(value)
                path.write_bytes(original)

    def test_packaging_retime_rejected(self):
        packaging = read_json_object(self.packaging, "packaging")
        packaging["scenes"][0]["start_s"] = 0.25
        atomic_write_json(packaging, self.packaging)
        with self.assertRaisesRegex(ContractError, "packaging drift"):
            self.build()

    def test_resealed_authority_and_renderer_drift_rejected(self):
        for field in ("authority", "renderer", "evidence"):
            value = self.build()
            if field == "authority":
                value["boundary"]["render_execution_performed"] = True
            elif field == "renderer":
                value["renderer"]["data"]["scenes"][0]["startFrame"] = 22
            else:
                value["timeline"][0]["evidence_refs"] = ["invented"]
            value["artifact_sha256"] = payload_sha256(value, "artifact_sha256")
            with self.assertRaises(ContractError):
                validate_semantic_render_plan(value)

    def test_atomic_resume_conflict_and_source_unchanged(self):
        before = file_sha256(self.timebase)
        path = self.root / "render-plan.json"
        value = self.build()
        write_semantic_render_plan(value, path)
        self.assertEqual(write_semantic_render_plan(value, path), path)
        with self.assertRaisesRegex(ContractError, "already exists"):
            write_semantic_render_plan(
                self.build(composition_id="AnotherCandidate"), path
            )
        self.assertEqual(before, file_sha256(self.timebase))

    def test_invalid_composition_id(self):
        for name in ("", "../escape", "wrong id"):
            with self.assertRaises(ContractError):
                self.build(composition_id=name)

    def test_cli_plan_validate_without_execution(self):
        output = self.root / "cli-plan.json"
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(
                main(
                    [
                        "render-plan",
                        "--plan",
                        str(self.plan),
                        "--packaging",
                        str(self.packaging),
                        "--output",
                        str(output),
                    ]
                ),
                0,
            )
            self.assertEqual(main(["render-plan-validate", str(output)]), 0)
        self.assertFalse(
            read_json_object(output, "output")["readiness"]["real_video_produced"]
        )

    def scene_plan(self):
        # Reuse the existing contract fixture; only the Git lookup is mocked.
        fixture = safe_media_fixtures.SafeMediaScenePlanTests()
        fixture.root = self.root
        layout = fixture._write_layout("synthetic-card", width=720, height=1280)
        spec = atomic_write_json(
            {
                "schema": "video_creation_pipeline.safe_media_scene_plan_spec.v1",
                "scenes": [
                    {"scene_id": f"segment-{i}", "safe_media_layout_path": str(layout)}
                    for i in range(5)
                ],
            },
            self.root / "scene-spec.json",
        )
        return atomic_write_json(
            build_safe_media_scene_plan(self.packaging, spec),
            self.root / "scene-plan.json",
        )

    @patch(
        "video_creation_pipeline.photo_layout._git_head",
        return_value=JUSTIFIED_LAYOUT_SOURCE_COMMIT,
    )
    def test_material_contract_mocked_git_and_two_reviews(self, _git):
        scene_plan = self.scene_plan()
        value = self.build(
            safe_media_scene_plan_path=scene_plan,
            approval_path=self.approval(self.plan),
            materials_approval_path=self.approval(scene_plan, "materials"),
        )
        self.assertEqual(value["readiness"]["status"], "approved_plan_only")
        self.assertFalse(value["readiness"]["render_eligible"])
        self.assertFalse(value["renderer"]["data"]["sourceMediaRendered"])
        validate_semantic_render_plan(value)
        (self.root / "synthetic-card.png").write_bytes(b"corrupt synthetic asset")
        with self.assertRaises(ContractError):
            validate_semantic_render_plan(value)

    @patch(
        "video_creation_pipeline.photo_layout._git_head",
        return_value=JUSTIFIED_LAYOUT_SOURCE_COMMIT,
    )
    def test_media_plan_cross_packaging_rejected(self, _git):
        scene_plan = self.scene_plan()
        other = atomic_write_json(
            read_json_object(self.packaging, "packaging"), self.root / "other.json"
        )
        with self.assertRaises(ContractError):
            build_remotion_timeline_preview(
                other, composition_id="Example", safe_media_scene_plan_path=scene_plan
            )

    def test_no_avatar_or_model_import_needed(self):
        value = self.build()
        self.assertFalse(value["boundary"]["avatar_enabled"])
        self.assertFalse(value["boundary"]["provider_called"])

    def test_visual_beat_review_and_unsupported_effects_block_render_handoff(self):
        from video_creation_pipeline.visual_routing import build_content_visual_routing_plan, build_visual_style_profile
        from video_creation_pipeline.semantic_editorial import build_editorial_visual_beats
        style = atomic_write_json(build_visual_style_profile('editorial_clean'),self.root/'style.json')
        routing_spec = atomic_write_json({'schema':'video_creation_pipeline.content_visual_routing_spec.v1','scenes':[
            dict(scene_id=f'segment-{i}',content_kind='concept_explanation',communication_goal='explain evidence',generation_allowed=False) for i in range(5)]},self.root/'routing-spec.json')
        routing = atomic_write_json(build_content_visual_routing_plan(self.packaging,routing_spec,style),self.root/'routing.json')
        rows = [dict(scene_id=f'segment-{i}',visual_role='knowledge_explanation',semantic_trigger='spoken fact',
            information_advance='show relationship',safe_zone='content_safe_area',motion_intensity='low',sfx_role='none',
            source_or_evidence_refs=[f'semantic:segment-{i}'],visual_motif='comparison') for i in range(5)]
        rows[0]['information_advance'] = ''
        rows[1]['transition'] = dict(kind='dissolve',reason='a new time period')
        spec = atomic_write_json({'schema':'video_creation_pipeline.editorial_visual_spec.v1','scenes':rows},self.root/'beats-spec.json')
        beats = atomic_write_json(build_editorial_visual_beats(routing,spec),self.root/'beats.json')
        value = self.build(visual_beats_path=beats)
        self.assertIn('visual_semantics_review_required',value['readiness']['blockers'])
        self.assertIn('visual_effect_renderer_binding_required',value['readiness']['blockers'])
        validate_semantic_render_plan(value)


if __name__ == "__main__":
    unittest.main()
