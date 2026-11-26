from __future__ import annotations

import math
import shutil
import struct
import subprocess
import sys
import unittest
import wave
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from video_creation_pipeline.contract_io import (
    artifact_reference,
    atomic_write_json,
    payload_sha256,
)
from video_creation_pipeline.errors import ContractError
from video_creation_pipeline.execution_receipts import resolve_media_tool
from video_creation_pipeline.motion_sfx import (
    VIDEO_SHOTCRAFT_COMMIT,
    bootstrap_warm_paper_wood_pack,
    build_motion_sfx_batch_receipt,
    build_motion_sfx_edit_plan,
    build_motion_sfx_source_manifest,
    derive_motion_sfx_cue_sheet,
    execute_motion_sfx_mix,
    prepare_motion_sfx_output_root,
    profile_paths_from_pack_receipt,
    validate_motion_sfx_batch_receipt,
    validate_motion_sfx_source_manifest,
    write_motion_sfx_contract,
)
from video_creation_pipeline.stock_media import (
    build_licensed_media_asset,
    build_sound_effect_profile,
    write_stock_media_contract,
)


class MotionSfxTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = ROOT / ".test-runtime" / f"motion-sfx-{uuid4().hex}"
        self.root.mkdir(parents=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_all_routes_share_balanced_cues_and_renderer_events_dedupe(self) -> None:
        packaging = self._packaging(6, scene_duration_s=10.0)
        packaging_path = self._json("packaging.json", packaging)
        routes = {
            "schema": "video_creation_pipeline.content_visual_routing_plan.v1",
            "scene_routes": [
                {
                    "scene_id": f"scene-{index:02d}",
                    "visual_route": route,
                }
                for index, route in enumerate(
                    (
                        "hyperframes_evidence_focus",
                        "remotion_knowledge_animation",
                        "remotion_kinetic_typography",
                        "existing_media_broll",
                        "provider_generated_broll",
                        "hyperframes_music_to_video",
                    ),
                    1,
                )
            ],
        }
        routes["plan_sha256"] = payload_sha256(routes, "plan_sha256")
        routes_path = self._json("routes.json", routes)
        motion = {
            "schema": "video_creation_pipeline.semantic_motion_plan.v1",
            "source_packaging_plan": artifact_reference(
                packaging_path, "video packaging plan"
            ),
            "cues": [
                {
                    "cue_id": "focus-a",
                    "story_segment_ref": "scene-01",
                    "information_role": "evidence",
                    "label": {"text": "保费证据"},
                    "timing": {
                        "timeline_start_s": 0.0,
                        "full_frame_s": 1.0,
                        "enter_s": 0.6,
                        "hold_s": 2.0,
                        "exit_s": 0.4,
                    },
                },
                {
                    "cue_id": "focus-b",
                    "story_segment_ref": "scene-04",
                    "information_role": "relationship",
                    "label": {"text": "因果关系"},
                    "timing": {
                        "timeline_start_s": 30.0,
                        "full_frame_s": 1.0,
                        "enter_s": 0.5,
                        "hold_s": 2.0,
                        "exit_s": 0.5,
                    },
                },
            ],
        }
        motion["plan_sha256"] = payload_sha256(motion, "plan_sha256")
        motion_path = self._json("motion.json", motion)
        remotion_path = self._json(
            "project-data.json",
            {
                "fps": 30,
                "scenes": [{"sceneId": f"scene-{index:02d}"} for index in range(1, 7)],
                "motionCues": [
                    {
                        "cueId": "focus-a",
                        "sceneId": "scene-01",
                        "startFrame": 0,
                        "durationInFrames": 120,
                        "label": "duplicate projection",
                    },
                    {
                        "cueId": "remotion-only",
                        "sceneId": "scene-03",
                        "startFrame": 600,
                        "durationInFrames": 90,
                        "label": "方法卡片",
                    },
                ],
            },
        )
        manifest = build_motion_sfx_source_manifest(
            packaging_path,
            semantic_motion_plan_path=motion_path,
            remotion_project_data_path=remotion_path,
            visual_routing_plan_path=routes_path,
        )
        root = prepare_motion_sfx_output_root(self.root / "run", manifest)
        manifest_path = write_motion_sfx_contract(
            manifest, root / "source-manifest.json"
        )
        cues = derive_motion_sfx_cue_sheet(manifest_path)

        self.assertEqual(
            manifest["projection_policy"]["route_scope_mode"],
            "all_packaging_scenes",
        )
        self.assertTrue(
            cues["semantic_spotting"]["density_plan"]["density_within_target"]
        )
        self.assertLessEqual(len(cues["cues"]), 5)
        self.assertEqual(
            sum(row["cue_id"] == "motion-focus-a" for row in cues["cues"]), 1
        )
        self.assertFalse(any("_rank" in row for row in cues["cues"]))
        self.assertTrue(
            {row["function"] for row in cues["cues"]}.intersection(
                {"emphasis", "causality", "transition"}
            )
        )

    def test_plain_script_uses_scene_alignment_and_keeps_cta_quiet(self) -> None:
        packaging_path = self._json(
            "packaging.json", self._packaging(2, scene_duration_s=20.0)
        )
        script = self.root / "script.txt"
        script.write_text(
            "第一，这里必须注意免责和退保损失。\n最后欢迎关注并私信咨询。",
            encoding="utf-8",
        )
        manifest = build_motion_sfx_source_manifest(packaging_path, script_path=script)
        manifest_path = write_motion_sfx_contract(
            manifest, self.root / "source-manifest.json"
        )
        cues = derive_motion_sfx_cue_sheet(manifest_path)

        self.assertEqual(
            manifest["projection_policy"]["script_only_alignment"],
            "scene_level_candidate_only",
        )
        self.assertEqual(len(cues["semantic_spotting"]["omissions"]), 1)
        self.assertEqual(cues["semantic_spotting"]["omissions"][0]["start_s"], 20.0)
        self.assertTrue(all(row["timeline_start_s"] < 20.0 for row in cues["cues"]))
        self.assertEqual(cues["cues"][0]["function"], "caution")

    def test_remotion_cues_inherit_script_semantics_for_asset_family(self) -> None:
        packaging = self._packaging(3, scene_duration_s=12.0)
        packaging["scenes"][0]["subtitle_candidates"] = ["第一，必须确认续保条件"]
        packaging["scenes"][1]["subtitle_candidates"] = ["注意免责、费用和退保损失"]
        packaging["scenes"][2]["subtitle_candidates"] = ["最后给出结论和建议"]
        packaging_path = self._json("packaging.json", packaging)
        remotion_path = self._json(
            "project-data.json",
            {
                "fps": 30,
                "scenes": [{"sceneId": f"scene-{index:02d}"} for index in range(1, 4)],
                "motionCues": [
                    {
                        "cueId": "commit",
                        "sceneId": "scene-01",
                        "startFrame": 48,
                        "durationInFrames": 24,
                        "label": "问题卡片进入",
                    },
                    {
                        "cueId": "risk",
                        "sceneId": "scene-02",
                        "startFrame": 408,
                        "durationInFrames": 30,
                        "label": "风险清单进入",
                    },
                    {
                        "cueId": "resolve",
                        "sceneId": "scene-03",
                        "startFrame": 768,
                        "durationInFrames": 24,
                        "label": "结论卡片完成",
                    },
                ],
            },
        )
        manifest = build_motion_sfx_source_manifest(
            packaging_path, remotion_project_data_path=remotion_path
        )
        manifest_path = write_motion_sfx_contract(
            manifest, self.root / "source-manifest.json"
        )

        cues = derive_motion_sfx_cue_sheet(manifest_path)

        self.assertEqual(
            [row["function"] for row in cues["cues"]],
            ["commitment", "caution", "resolution"],
        )
        self.assertEqual(
            [row["preferred_asset_id"] for row in cues["cues"]],
            ["warm-paper-staple", "warm-weak-emphasis", "warm-low-confirm"],
        )

    def test_external_motion_selection_sets_template_default_on_central_cue(
        self,
    ) -> None:
        packaging_path = self._json(
            "packaging.json", self._packaging(1, scene_duration_s=10.0)
        )
        semantic = {
            "schema": "video_creation_pipeline.semantic_motion_plan.v1",
            "source_packaging_plan": artifact_reference(
                packaging_path, "video packaging plan"
            ),
            "cues": [
                {
                    "cue_id": "focus-evidence",
                    "story_segment_ref": "scene-01",
                    "information_role": "evidence",
                    "label": {"text": "证据卡片"},
                    "timing": {
                        "timeline_start_s": 0.0,
                        "full_frame_s": 1.0,
                        "enter_s": 0.6,
                        "hold_s": 2.0,
                        "exit_s": 0.4,
                    },
                }
            ],
        }
        semantic["plan_sha256"] = payload_sha256(semantic, "plan_sha256")
        semantic_path = self._json("semantic.json", semantic)
        selection = {
            "schema": "video_creation_pipeline.external_motion_selection_plan.v1",
            "source_semantic_motion_plan": artifact_reference(
                semantic_path, "semantic motion plan"
            ),
            "selections": [
                {
                    "cue_id": "focus-evidence",
                    "sound_effect": {
                        "asset_id": "clipkit-soft-pop",
                        "selection_basis": "template_default",
                        "central_timeline_cue_required": True,
                        "component_embedded_audio_forbidden": True,
                    },
                }
            ],
        }
        selection["plan_sha256"] = payload_sha256(selection, "plan_sha256")
        selection_path = self._json("external-selection.json", selection)
        with patch(
            "video_creation_pipeline.motion_sfx.validate_external_motion_selection_plan"
        ):
            manifest = build_motion_sfx_source_manifest(
                packaging_path,
                semantic_motion_plan_path=semantic_path,
                external_motion_selection_plan_path=selection_path,
            )
            manifest_path = write_motion_sfx_contract(
                manifest, self.root / "external-source-manifest.json"
            )
            cues = derive_motion_sfx_cue_sheet(manifest_path)

        self.assertEqual("clipkit-soft-pop", cues["cues"][0]["preferred_asset_id"])
        self.assertEqual("needs_audition", cues["cues"][0]["spotting_status"])

    def test_bound_script_drift_fails_closed(self) -> None:
        packaging_path = self._json("packaging.json", self._packaging(1, 5.0))
        script = self.root / "script.txt"
        script.write_text("第一条建议。", encoding="utf-8")
        manifest = build_motion_sfx_source_manifest(packaging_path, script_path=script)
        script.write_text("mutated after planning", encoding="utf-8")
        with self.assertRaisesRegex(ContractError, "changed after"):
            validate_motion_sfx_source_manifest(manifest)

    def test_recomputed_policy_and_batch_drift_still_fail_closed(self) -> None:
        packaging_path = self._json("packaging.json", self._packaging(1, 5.0))
        manifest = build_motion_sfx_source_manifest(packaging_path)
        drifted_manifest = deepcopy(manifest)
        drifted_manifest["projection_policy"]["minimum_event_gap_s"] = 0.1
        drifted_manifest["manifest_sha256"] = payload_sha256(
            drifted_manifest, "manifest_sha256"
        )
        with self.assertRaisesRegex(ContractError, "projection policy"):
            validate_motion_sfx_source_manifest(drifted_manifest)

        manifest_path = write_motion_sfx_contract(
            manifest, self.root / "source-manifest.json"
        )
        cue_sheet = derive_motion_sfx_cue_sheet(manifest_path)
        cue_path = write_stock_media_contract(cue_sheet, self.root / "cue-sheet.json")
        batch = build_motion_sfx_batch_receipt(manifest_path, cue_path)
        drifted_batch = deepcopy(batch)
        drifted_batch["readiness"]["cue_count"] = 99
        drifted_batch["receipt_sha256"] = payload_sha256(
            drifted_batch, "receipt_sha256"
        )
        with self.assertRaisesRegex(ContractError, "readiness or status"):
            validate_motion_sfx_batch_receipt(drifted_batch)

        missing_boundary = deepcopy(batch)
        del missing_boundary["authority_boundary"]["publish_performed"]
        missing_boundary["receipt_sha256"] = payload_sha256(
            missing_boundary, "receipt_sha256"
        )
        with self.assertRaisesRegex(ContractError, "authority boundary"):
            validate_motion_sfx_batch_receipt(missing_boundary)

    @patch("video_creation_pipeline.motion_sfx.probe_media_metadata")
    @patch("video_creation_pipeline.motion_sfx._git_head")
    def test_pack_bootstrap_reuses_fixed_source_and_is_idempotent(
        self, git_head, probe
    ) -> None:
        git_head.return_value = VIDEO_SHOTCRAFT_COMMIT
        probe.return_value = {
            "format": {"duration": "0.8"},
            "streams": [{"codec_type": "audio"}],
        }
        source = self.root / "video-shotcraft"
        attribution = source / "assets" / "audio" / "ATTRIBUTION.md"
        attribution.parent.mkdir(parents=True)
        attribution.write_text("Mixkit reviewed fixture", encoding="utf-8")
        relative_paths = (
            "paper/paper-move-quick.mp3",
            "paper/paper-page-turn.mp3",
            "paper/paper-slide.mp3",
            "paper/paper-staple.mp3",
            "ui/switch-tap.mp3",
            "ui/switch-light.mp3",
            "impact/hit-weak.mp3",
            "text/pencil-write-short.mp3",
        )
        for index, relative in enumerate(relative_paths):
            path = source / "assets" / "audio" / "sfx" / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(f"fixture-{index}".encode())

        output = self.root / "pack"
        first = bootstrap_warm_paper_wood_pack(source, output)
        second = bootstrap_warm_paper_wood_pack(source, output)
        profiles = profile_paths_from_pack_receipt(first["receipt"])

        self.assertEqual(
            first["payload"]["pack_sha256"], second["payload"]["pack_sha256"]
        )
        self.assertEqual(len(profiles), 8)
        self.assertTrue(all(path.is_file() for path in profiles))
        self.assertFalse(first["payload"]["selection_policy"]["automatic_download"])

    @unittest.skipUnless(
        shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required"
    )
    def test_synthetic_mp4_runs_through_existing_sound_effect_outlet(self) -> None:
        packaging_path = self._json("packaging.json", self._packaging(1, 2.0))
        script = self.root / "script.txt"
        script.write_text("第一条关键建议。", encoding="utf-8")
        manifest = build_motion_sfx_source_manifest(packaging_path, script_path=script)
        run_root = prepare_motion_sfx_output_root(self.root / "run", manifest)
        manifest_path = write_motion_sfx_contract(
            manifest, run_root / "source-manifest.json"
        )
        cue_sheet = derive_motion_sfx_cue_sheet(manifest_path)
        cue_path = write_stock_media_contract(cue_sheet, run_root / "cue-sheet.json")
        profile_path = self._synthetic_profile(
            asset_id="warm-paper-staple",
            tags=["commitment", "confirmation", "impact", "paper"],
        )
        edit_plan = build_motion_sfx_edit_plan(cue_path, [profile_path])
        edit_path = write_stock_media_contract(edit_plan, run_root / "edit-plan.json")
        program = self.root / "program.mp4"
        subprocess.run(
            [
                resolve_media_tool("ffmpeg"),
                "-y",
                "-f",
                "lavfi",
                "-i",
                "color=c=0x233044:s=320x180:r=30:d=2",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=220:sample_rate=48000:duration=2",
                "-shortest",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                str(program),
            ],
            capture_output=True,
            check=True,
        )
        candidate = run_root / "candidate.mp4"
        ffmpeg_receipt = run_root / "ffmpeg-receipt.json"
        execution = execute_motion_sfx_mix(
            program,
            edit_path,
            candidate,
            ffmpeg_receipt,
            deterministic=True,
        )
        batch = build_motion_sfx_batch_receipt(
            manifest_path,
            cue_path,
            edit_plan_path=edit_path,
            ffmpeg_receipt_path=ffmpeg_receipt,
        )
        validate_motion_sfx_batch_receipt(batch)

        self.assertTrue(candidate.is_file())
        self.assertEqual(execution["operation"], "sound_effect_mix")
        self.assertEqual(
            execution["outlet_id"], "video_creation_pipeline.single_ffmpeg_outlet"
        )
        self.assertEqual(batch["status"], "candidate_mixed")
        self.assertFalse(batch["readiness"]["release_eligible"])

    def _packaging(
        self, scene_count: int, scene_duration_s: float
    ) -> dict[str, object]:
        return {
            "schema": "video_creation_pipeline.video_packaging_plan.v1",
            "output": {"width": 1080, "height": 1920, "fps": 30},
            "scenes": [
                {
                    "scene_id": f"scene-{index:02d}",
                    "start_s": (index - 1) * scene_duration_s,
                    "end_s": index * scene_duration_s,
                    "purpose": "解释保险知识",
                    "subtitle_candidates": [f"第{index}个要点"],
                    "evidence_refs": [],
                }
                for index in range(1, scene_count + 1)
            ],
            "status": "candidate_only",
        }

    def _synthetic_profile(self, *, asset_id: str, tags: list[str]) -> Path:
        audio = self.root / f"{asset_id}.wav"
        sample_rate = 48000
        frames = int(sample_rate * 0.4)
        with wave.open(str(audio), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(sample_rate)
            for index in range(frames):
                envelope = max(0.0, 1.0 - index / frames)
                sample = int(
                    4200 * envelope * math.sin(2 * math.pi * 180 * index / sample_rate)
                )
                handle.writeframesraw(struct.pack("<h", sample))
        snapshot = self.root / "license.txt"
        snapshot.write_text("synthetic Mixkit fixture license", encoding="utf-8")
        manifest = build_licensed_media_asset(
            audio,
            snapshot,
            {
                "asset_id": asset_id,
                "media_type": "sound_effect",
                "source_id": "mixkit",
                "item_url": f"https://mixkit.co/free-sound-effects/{asset_id}/",
                "license_id": "mixkit-free-license",
                "title": asset_id,
                "author": "synthetic fixture",
                "description": "synthetic sound effect fixture",
                "tags": tags,
                "download_method": "manual",
                "downloaded_at": "2026-08-31T00:00:00+08:00",
                "attribution_text": "",
            },
            purpose="commercial",
            platform="social_media",
        )
        manifest_path = self.root / f"{asset_id}.asset.json"
        write_stock_media_contract(manifest, manifest_path)
        profile = build_sound_effect_profile(
            manifest_path,
            duration_s=0.4,
            category="paper_commitment",
            tags=tags,
        )
        profile_path = self.root / f"{asset_id}.profile.json"
        write_stock_media_contract(profile, profile_path)
        return profile_path

    def _json(self, name: str, payload: dict[str, object]) -> Path:
        return atomic_write_json(payload, self.root / name)


if __name__ == "__main__":
    unittest.main()
