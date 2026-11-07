from __future__ import annotations

# ruff: noqa: E402

import json
import shutil
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from smart_reframe_test_support import make_evidence, make_request, make_source
from video_creation_pipeline.contract_io import (
    artifact_reference,
    atomic_write_json,
    payload_sha256,
)
from video_creation_pipeline.errors import ContractError
from video_creation_pipeline.remotion_project import _project_data
from video_creation_pipeline.remotion_skill_handoff import (
    REMOTION_SKILLS_COMMIT,
    build_remotion_skill_handoff,
)
from video_creation_pipeline.seedance_prompt_handoff import (
    SEEDANCE_PROMPT_SKILL_COMMIT,
    build_seedance_prompt_handoff,
)
from video_creation_pipeline.smart_reframe import (
    build_smart_reframe_plan,
    write_smart_reframe_contract,
)
from video_creation_pipeline.visual_routing import (
    CONTENT_VISUAL_ROUTING_SPEC_SCHEMA,
    HYPERFRAMES_MUSIC_SOURCE_COMMIT,
    build_content_visual_routing_plan,
    build_hyperframes_music_handoff,
    build_visual_style_profile,
    run_hyperframes_music_analysis,
    validate_content_visual_routing_plan,
    validate_hyperframes_music_handoff,
    validate_hyperframes_music_analysis_receipt,
    validate_visual_style_profile,
    write_visual_routing_contract,
)


class VisualRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = ROOT / ".test-runtime" / f"visual-routing-{uuid4().hex}"
        self.root.mkdir(parents=True)
        self.packaging = self.root / "packaging.json"
        self.scene_ids = [f"scene-{index:02d}" for index in range(1, 9)]
        scenes = []
        for index, scene_id in enumerate(self.scene_ids):
            row = {
                "scene_id": scene_id,
                "start_s": float(index * 2),
                "end_s": float(index * 2 + 2),
                "material_status": "missing_material",
                "evidence_refs": [],
            }
            if scene_id == "scene-01":
                row["evidence_refs"] = ["evidence:policy-page"]
            if scene_id == "scene-04":
                row["material_status"] = "authorized_local_candidate"
            scenes.append(row)
        atomic_write_json(
            {
                "schema": "video_creation_pipeline.video_packaging_plan.v1",
                "output": {"width": 1080, "height": 1920, "fps": 30},
                "scenes": scenes,
                "status": "candidate_only",
            },
            self.packaging,
        )
        self.spec = self.root / "routing-spec.json"
        self._write_spec(self.spec)
        self.paper_style = self.root / "paper-style.json"
        write_visual_routing_contract(
            build_visual_style_profile("paper_cutout"), self.paper_style
        )
        self.editorial_style = self.root / "editorial-style.json"
        write_visual_routing_contract(
            build_visual_style_profile("editorial_clean"), self.editorial_style
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def _write_spec(
        self,
        path: Path,
        *,
        changed_goal_scene: str | None = None,
        generation_allowed: bool = True,
    ) -> None:
        kinds = [
            "evidence_document",
            "concept_explanation",
            "transition_or_emphasis",
            "real_world_action",
            "emotional_story",
            "speaker_connection",
            "music_driven_promo",
            "comparison",
        ]
        atomic_write_json(
            {
                "schema": CONTENT_VISUAL_ROUTING_SPEC_SCHEMA,
                "scenes": [
                    {
                        "scene_id": scene_id,
                        "content_kind": kind,
                        "communication_goal": (
                            "changed explanation"
                            if scene_id == changed_goal_scene
                            else f"explain {kind}"
                        ),
                        "generation_allowed": (
                            generation_allowed if scene_id == "scene-05" else False
                        ),
                    }
                    for scene_id, kind in zip(self.scene_ids, kinds, strict=True)
                ],
            },
            path,
        )

    def _music_source(self) -> Path:
        source = self.root / "hyperframes"
        paths = (
            "skills/music-to-video/SKILL.md",
            "skills/music-to-video/scripts/analyze-beatgrid.py",
            "skills/music-to-video/references/frame-skeleton.md",
            "skills/music-to-video/references/storyboard-format.md",
        )
        for relative in paths:
            target = source / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("reviewed fixture", encoding="utf-8")
        (source / "LICENSE").write_text("Apache License\nVersion 2.0", encoding="utf-8")
        return source

    def _music_handoff(self) -> Path:
        source = self._music_source()
        audio = self.root / "music.wav"
        audio.write_bytes(b"synthetic audio fixture")
        audiomap = self.root / "audiomap.json"
        atomic_write_json(
            {
                "version": 2,
                "audio": {"path": str(audio.resolve()), "duration_sec": 16.0},
                "grid": {"beats_sec": [0.0, 0.5, 1.0, 1.5]},
                "energy_phases": [
                    {
                        "start": 0.0,
                        "end": 16.0,
                        "level": "HIGH",
                        "density": "dense",
                    },
                    {
                        "start": 16.0,
                        "end": 16.0,
                        "level": "VOID",
                        "density": "sparse",
                    },
                ],
                "key_moments": [{"t": 8, "kind": "SURGE", "delta": 0.4}],
                "phrases": [{"index": 0, "start": 0.0, "end": 16.0}],
                "rolls": [],
                "events": [{"t": index / 2} for index in range(32)],
            },
            audiomap,
        )
        output = self.root / "music-handoff.json"
        with patch(
            "video_creation_pipeline.visual_routing._git_head",
            return_value=HYPERFRAMES_MUSIC_SOURCE_COMMIT,
        ):
            payload = build_hyperframes_music_handoff(source, audio, audiomap)
            self.assertEqual(payload["audiomap_summary"]["pacing"], "beat_cut")
            write_visual_routing_contract(payload, output)
            validate_hyperframes_music_handoff(payload)
        return output

    def test_style_presets_are_stable_and_renderer_neutral(self) -> None:
        paper = build_visual_style_profile("paper_cutout")
        second = build_visual_style_profile("paper_cutout")
        editorial = build_visual_style_profile("editorial_clean")
        self.assertEqual(paper["profile_sha256"], second["profile_sha256"])
        self.assertNotEqual(paper["profile_sha256"], editorial["profile_sha256"])
        self.assertEqual(
            paper["renderer_bindings"]["hyperframes"]["motion_profile"],
            "stop_motion_soft",
        )
        self.assertIn(
            "layered paper cutout",
            paper["renderer_bindings"]["provider_prompt"]["style_prefix"],
        )
        validate_visual_style_profile(paper)

    def test_music_analysis_delegates_to_fixed_upstream_and_writes_receipt(
        self,
    ) -> None:
        source = self._music_source()
        audio = self.root / "analysis.wav"
        audio.write_bytes(b"synthetic audio fixture")
        output = self.root / "analyzed-audiomap.json"
        receipt_path = self.root / "analysis-receipt.json"
        ffmpeg = self.root / "ffmpeg.exe"
        ffmpeg.write_bytes(b"synthetic ffmpeg fixture")

        def fake_run(command, **_kwargs):
            temporary = Path(command[command.index("-o") + 1])
            atomic_write_json(
                {
                    "version": 2,
                    "audio": {"path": str(audio.resolve()), "duration_sec": 4.0},
                    "grid": {"beats_sec": [0.0, 1.0, 2.0, 3.0]},
                    "energy_phases": [
                        {
                            "start": 0.0,
                            "end": 4.0,
                            "level": "LOW",
                            "density": "sparse",
                        }
                    ],
                    "key_moments": [],
                    "phrases": [{"index": 0, "start": 0.0, "end": 4.0}],
                    "rolls": [],
                    "events": [],
                },
                temporary,
            )
            return SimpleNamespace(returncode=0, stdout="analyzed", stderr="")

        with (
            patch(
                "video_creation_pipeline.visual_routing._git_head",
                return_value=HYPERFRAMES_MUSIC_SOURCE_COMMIT,
            ),
            patch(
                "video_creation_pipeline.visual_routing.subprocess.run",
                side_effect=fake_run,
            ),
            patch(
                "video_creation_pipeline.visual_routing.shutil.which",
                side_effect=lambda value: (
                    sys.executable if value == "python-on-path" else None
                ),
            ),
        ):
            receipt = run_hyperframes_music_analysis(
                source,
                audio,
                output,
                receipt_path,
                python_executable="python-on-path",
                ffmpeg_path=ffmpeg,
            )
            validate_hyperframes_music_analysis_receipt(receipt)
        self.assertTrue(output.is_file())
        self.assertTrue(receipt_path.is_file())
        self.assertEqual(receipt["status"], "completed_local_analysis")
        self.assertEqual(
            receipt["execution"]["python_executable"],
            str(Path(sys.executable).resolve()),
        )
        self.assertTrue(receipt["authority_boundary"]["local_analysis_performed"])
        self.assertFalse(receipt["authority_boundary"]["provider_execution_performed"])

    def test_routes_cover_content_material_generation_and_music(self) -> None:
        music_handoff = self._music_handoff()
        output = self.root / "routes.json"
        with patch(
            "video_creation_pipeline.visual_routing._git_head",
            return_value=HYPERFRAMES_MUSIC_SOURCE_COMMIT,
        ):
            first = build_content_visual_routing_plan(
                self.packaging,
                self.spec,
                self.paper_style,
                music_handoff_path=music_handoff,
            )
            second = build_content_visual_routing_plan(
                self.packaging,
                self.spec,
                self.paper_style,
                music_handoff_path=music_handoff,
            )
            self.assertEqual(first["plan_sha256"], second["plan_sha256"])
            write_visual_routing_contract(first, output)
            validate_content_visual_routing_plan(first)
        routes = {row["scene_id"]: row for row in first["scene_routes"]}
        self.assertEqual(
            routes["scene-01"]["visual_route"], "hyperframes_evidence_focus"
        )
        self.assertEqual(
            routes["scene-02"]["visual_route"], "remotion_knowledge_animation"
        )
        self.assertEqual(
            routes["scene-03"]["visual_route"], "remotion_kinetic_typography"
        )
        self.assertEqual(routes["scene-04"]["visual_route"], "existing_media_broll")
        self.assertEqual(routes["scene-05"]["visual_route"], "provider_generated_broll")
        self.assertEqual(routes["scene-06"]["visual_route"], "missing_material")
        self.assertEqual(
            routes["scene-07"]["visual_route"], "hyperframes_music_to_video"
        )
        self.assertEqual(
            first["timing_authority"]["subtitle_srt_role"], "projection_only"
        )
        self.assertFalse(first["authority_boundary"]["automatic_provider_fallback"])

    def test_verified_smart_reframe_plan_is_bound_as_read_only_input(self) -> None:
        source = make_source(self.root)
        request = make_request(self.root)
        evidence = make_evidence(self.root, source, "talking-head-observations.json")
        reframe_path = self.root / "smart-reframe-plan.json"
        write_smart_reframe_contract(
            build_smart_reframe_plan(source, request, evidence_paths=[evidence]),
            reframe_path,
        )
        plan = build_content_visual_routing_plan(
            self.packaging,
            self.spec,
            self.paper_style,
            smart_reframe_plan_path=reframe_path,
        )
        validate_content_visual_routing_plan(plan)
        self.assertEqual(
            artifact_reference(reframe_path, "smart reframe plan"),
            plan["inputs"]["smart_reframe_plan"],
        )

    def test_style_and_one_scene_change_have_expected_invalidation_scope(self) -> None:
        baseline = build_content_visual_routing_plan(
            self.packaging, self.spec, self.paper_style
        )
        restyled = build_content_visual_routing_plan(
            self.packaging, self.spec, self.editorial_style
        )
        changed_spec = self.root / "routing-spec-changed.json"
        self._write_spec(changed_spec, changed_goal_scene="scene-02")
        changed = build_content_visual_routing_plan(
            self.packaging, changed_spec, self.paper_style
        )
        baseline_keys = {
            row["scene_id"]: row["generation_scope"]["regeneration_key"]
            for row in baseline["scene_routes"]
        }
        restyled_keys = {
            row["scene_id"]: row["generation_scope"]["regeneration_key"]
            for row in restyled["scene_routes"]
        }
        changed_keys = {
            row["scene_id"]: row["generation_scope"]["regeneration_key"]
            for row in changed["scene_routes"]
        }
        self.assertTrue(
            all(baseline_keys[key] != restyled_keys[key] for key in baseline_keys)
        )
        self.assertNotEqual(baseline_keys["scene-02"], changed_keys["scene-02"])
        self.assertTrue(
            all(
                baseline_keys[key] == changed_keys[key]
                for key in baseline_keys
                if key != "scene-02"
            )
        )

    def test_missing_music_and_incompatible_preference_fail_closed(self) -> None:
        plan = build_content_visual_routing_plan(
            self.packaging, self.spec, self.paper_style
        )
        scene = next(
            row for row in plan["scene_routes"] if row["scene_id"] == "scene-07"
        )
        self.assertEqual(scene["execution_status"], "blocked_missing_music_audiomap")
        bad_spec = json.loads(self.spec.read_text(encoding="utf-8"))
        bad_spec["scenes"][1]["preferred_route"] = "existing_media_broll"
        bad_path = self.root / "bad-spec.json"
        atomic_write_json(bad_spec, bad_path)
        with self.assertRaisesRegex(ContractError, "incompatible"):
            build_content_visual_routing_plan(
                self.packaging, bad_path, self.paper_style
            )

    def test_policy_tampering_fails_after_recalculating_integrity_hash(self) -> None:
        plan = build_content_visual_routing_plan(
            self.packaging, self.spec, self.paper_style
        )
        plan["approval_projection"]["new_approval_ledger_created"] = True
        plan["plan_sha256"] = payload_sha256(plan, "plan_sha256")
        with self.assertRaisesRegex(ContractError, "approval projection"):
            validate_content_visual_routing_plan(plan)

        plan = build_content_visual_routing_plan(
            self.packaging, self.spec, self.paper_style
        )
        plan["local_regeneration_policy"][
            "invalidate_final_render_and_release_gate"
        ] = False
        plan["plan_sha256"] = payload_sha256(plan, "plan_sha256")
        with self.assertRaisesRegex(ContractError, "local regeneration policy"):
            validate_content_visual_routing_plan(plan)

        handoff_path = self._music_handoff()
        handoff = json.loads(handoff_path.read_text(encoding="utf-8"))
        handoff["integration_policy"]["no_secondary_audio_analyzer"] = False
        handoff["handoff_sha256"] = payload_sha256(handoff, "handoff_sha256")
        with (
            patch(
                "video_creation_pipeline.visual_routing._git_head",
                return_value=HYPERFRAMES_MUSIC_SOURCE_COMMIT,
            ),
            self.assertRaisesRegex(ContractError, "integration policy"),
        ):
            validate_hyperframes_music_handoff(handoff)

    @patch("video_creation_pipeline.remotion_skill_handoff._git_head")
    def test_remotion_consumes_route_and_style_without_new_runtime(
        self, git_head
    ) -> None:
        git_head.return_value = REMOTION_SKILLS_COMMIT
        skills = self.root / "remotion-skills"
        (skills / "package.json").parent.mkdir(parents=True, exist_ok=True)
        (skills / "package.json").write_text(
            json.dumps({"name": "@remotion/skills", "version": "4.0.513"}),
            encoding="utf-8",
        )
        for relative in (
            "skills/remotion-best-practices/SKILL.md",
            "skills/remotion-markup/SKILL.md",
            "skills/remotion-interactivity/SKILL.md",
            "skills/remotion-render/SKILL.md",
            "skills/remotion-studio/SKILL.md",
            "skills/remotion-captions/SKILL.md",
        ):
            target = skills / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("fixture", encoding="utf-8")
        routes = self.root / "routes-no-music.json"
        write_visual_routing_contract(
            build_content_visual_routing_plan(
                self.packaging, self.spec, self.paper_style
            ),
            routes,
        )
        handoff = build_remotion_skill_handoff(
            skills,
            self.packaging,
            composition_id="main",
            content_visual_routing_plan_path=routes,
            visual_style_profile_path=self.paper_style,
        )
        data = _project_data(handoff)
        self.assertEqual(data["style"]["background"], "#f4efe6")
        self.assertEqual(
            data["scenes"][1]["visualRoute"], "remotion_knowledge_animation"
        )
        self.assertTrue(
            handoff["handoff_constraints"]["preserve_visual_route_and_style_bindings"]
        )

    @patch("video_creation_pipeline.seedance_prompt_handoff._git_head")
    def test_generated_broll_handoff_gets_style_without_provider_execution(
        self, git_head
    ) -> None:
        git_head.return_value = SEEDANCE_PROMPT_SKILL_COMMIT
        skill_root = self.root / "seedance"
        skill = skill_root / ".claude" / "skills" / "seedance" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("reviewed fixture", encoding="utf-8")
        routes = self.root / "routes-provider.json"
        write_visual_routing_contract(
            build_content_visual_routing_plan(
                self.packaging, self.spec, self.paper_style
            ),
            routes,
        )
        edit_job = self.root / "edit-job.json"
        atomic_write_json({"fixture": True}, edit_job)
        unit = {
            "schema": "video_creation_pipeline.shot_generation_unit.v1",
            "source_edit_job": artifact_reference(edit_job, "edit job"),
            "scene_id": "scene-05",
            "title": "generated story",
            "timeline_indexes": [],
            "duration_s": 2.0,
            "media_type": "video",
            "output": {"width": 1080, "height": 1920},
            "generator": "manual",
            "required_capability": "manual_media_import",
            "narration": "fixture",
            "visual_prompt": "a family reviews a timeline",
            "visual_notes": [],
            "continuity": {
                "characters": [],
                "props": [],
                "environment": [],
                "must_preserve_across_shots": True,
            },
            "reference_assets": [],
            "negative_constraints": [],
            "status": "candidate_only",
            "human_confirmed": False,
            "mutates_vkp_timeline": False,
        }
        unit["unit_sha256"] = payload_sha256(unit, "unit_sha256")
        unit_path = self.root / "shot-unit.json"
        atomic_write_json(unit, unit_path)
        handoff = build_seedance_prompt_handoff(
            skill_root,
            unit_path,
            content_visual_routing_plan_path=routes,
            visual_style_profile_path=self.paper_style,
        )
        self.assertIn(
            "layered paper cutout", handoff["prompt_contract"]["visual_prompt"]
        )
        self.assertEqual(handoff["visual_route"], "provider_generated_broll")
        self.assertFalse(handoff["execution_route"]["provider_execution_authorized"])
        self.assertFalse(handoff["authority_boundary"]["provider_execution_performed"])


if __name__ == "__main__":
    unittest.main()
