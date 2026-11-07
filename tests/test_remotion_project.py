from __future__ import annotations

import json
import shutil
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import video_creation_pipeline as public_api
from video_creation_pipeline.contract_io import (
    artifact_reference,
    payload_sha256,
    read_json_object,
)
from video_creation_pipeline.errors import ContractError
from video_creation_pipeline.photo_layout import (
    JUSTIFIED_LAYOUT_SOURCE_COMMIT,
    JUSTIFIED_LAYOUT_VERSION,
)
from video_creation_pipeline.remotion_project import (
    REMOTION_RUNTIME_COMMIT,
    _atomic_copy_promote,
    _project_data,
    _semantic_subtitle_region,
    materialize_remotion_project,
    remotion_project_doctor,
    validate_remotion_project_manifest,
)
from video_creation_pipeline.remotion_skill_cli import main as remotion_cli_main
from video_creation_pipeline.remotion_skill_handoff import (
    REMOTION_SKILLS_COMMIT,
    build_remotion_skill_handoff,
    write_remotion_skill_handoff,
)
from video_creation_pipeline.safe_media_scene_plan import (
    SAFE_MEDIA_SCENE_PLAN_SPEC_SCHEMA,
    build_safe_media_scene_plan,
    write_safe_media_scene_plan,
)


class RemotionProjectTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = ROOT / ".test-runtime" / f"remotion-project-{uuid4().hex}"
        self.root.mkdir(parents=True)
        self.skills = self.root / "skills"
        self.runtime = self.root / "runtime"
        self.project = self.root / "project"
        self._write_skills_source()
        self._write_runtime_source()
        self.packaging = self.root / "packaging.json"
        self.packaging.write_text(
            json.dumps(
                {
                    "schema": "video_creation_pipeline.video_packaging_plan.v1",
                    "aspect_ratio": "16:9",
                    "output": {"width": 1080, "height": 1920, "fps": 30},
                    "scenes": [
                        {
                            "scene_id": "scene-01",
                            "story_segment_ref": "scene-01",
                            "start_s": 0.0,
                            "end_s": 1.0,
                            "evidence_refs": ["evidence:synthetic"],
                        }
                    ],
                    "status": "candidate_only",
                }
            ),
            encoding="utf-8",
        )
        self.subtitle = self.root / "subtitles.srt"
        self.subtitle.write_text(
            "1\n00:00:00,000 --> 00:00:00,900\nSynthetic caption\n",
            encoding="utf-8",
        )
        with patch(
            "video_creation_pipeline.remotion_skill_handoff._git_head",
            return_value=REMOTION_SKILLS_COMMIT,
        ):
            handoff = build_remotion_skill_handoff(
                self.skills,
                self.packaging,
                composition_id="MainCandidate",
                subtitle_path=self.subtitle,
            )
            self.handoff = write_remotion_skill_handoff(
                handoff, self.root / "handoff.json"
            )

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_package_exports_materialize_run_and_validation_api(self) -> None:
        for name in (
            "build_remotion_skill_handoff",
            "materialize_remotion_project",
            "remotion_project_doctor",
            "run_remotion_candidate",
            "validate_remotion_project_manifest",
            "validate_remotion_run_receipt",
        ):
            self.assertTrue(callable(getattr(public_api, name)))

    def test_semantic_subtitle_region_must_be_stable_across_cues(self) -> None:
        rect = {"x": 0.11, "y": 0.65, "width": 0.65, "height": 0.08}
        motion = {
            "cues": [
                {"protected_regions": [{"id": "subtitle", "rect": dict(rect)}]},
                {"protected_regions": [{"id": "subtitle", "rect": dict(rect)}]},
            ]
        }
        self.assertEqual(_semantic_subtitle_region(motion), rect)
        motion["cues"][1]["protected_regions"][0]["rect"]["y"] = 0.64
        with self.assertRaisesRegex(ContractError, "drifted across cues"):
            _semantic_subtitle_region(motion)

    def test_semantic_privacy_regions_are_projected_without_runtime_layout(
        self,
    ) -> None:
        subtitle_rect = {"x": 0.11, "y": 0.65, "width": 0.65, "height": 0.08}
        privacy_rect = {"x": 0.72, "y": 0.27, "width": 0.2, "height": 0.24}
        motion_path = self.root / "motion-with-privacy.json"
        motion_path.write_text(
            json.dumps(
                {
                    "cues": [
                        {
                            "cue_id": "cue-privacy",
                            "story_segment_ref": "scene-01",
                            "timing": {
                                "timeline_start_s": 0.0,
                                "full_frame_s": 0.2,
                                "enter_s": 0.1,
                                "hold_s": 0.5,
                                "exit_s": 0.1,
                            },
                            "label": {"text": "Evidence"},
                            "focus_destination": {
                                "x": 0.08,
                                "y": 0.18,
                                "width": 0.52,
                                "height": 0.3,
                            },
                            "protected_regions": [
                                {"id": "subtitle", "rect": subtitle_rect},
                                {
                                    "id": "privacy_face_anonymous-track-1",
                                    "rect": privacy_rect,
                                },
                            ],
                            "privacy_avoidance": {
                                "status": "label_relocated",
                                "region_ids": ["privacy_face_anonymous-track-1"],
                                "label_position_changed": True,
                                "runtime_reposition_allowed": False,
                            },
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        handoff = read_json_object(self.handoff, "handoff")
        handoff["inputs"]["semantic_motion_plan"] = artifact_reference(
            motion_path, "semantic motion plan"
        )

        data = _project_data(handoff)

        cue = data["motionCues"][0]
        self.assertEqual(
            cue["protectedRegions"][1],
            {"id": "privacy_face_anonymous-track-1", "rect": privacy_rect},
        )
        self.assertEqual(False, cue["privacyAvoidance"]["runtimeRepositionAllowed"])
        self.assertEqual(
            ["privacy_face_anonymous-track-1"], cue["privacyAvoidance"]["regionIds"]
        )

    def test_atomic_candidate_promotion_preserves_previous_output_on_failure(
        self,
    ) -> None:
        staging = self.root / "candidate-staging.mp4"
        output = self.root / "candidate.mp4"
        staging.write_bytes(b"new candidate")
        output.write_bytes(b"previous candidate")

        with (
            patch(
                "video_creation_pipeline.remotion_project.shutil.copyfile",
                side_effect=OSError("synthetic copy failure"),
            ),
            self.assertRaisesRegex(ContractError, "promotion failed"),
        ):
            _atomic_copy_promote(staging, output)
        self.assertEqual(output.read_bytes(), b"previous candidate")

        _atomic_copy_promote(staging, output)
        self.assertEqual(output.read_bytes(), b"new candidate")
        self.assertTrue(staging.is_file())

    def test_explicit_safe_layout_media_is_copied_hash_bound_and_muted(self) -> None:
        with (
            patch(
                "video_creation_pipeline.remotion_skill_handoff._git_head",
                return_value=REMOTION_SKILLS_COMMIT,
            ),
            patch(
                "video_creation_pipeline.remotion_project._git_head",
                return_value=REMOTION_RUNTIME_COMMIT,
            ),
            patch(
                "video_creation_pipeline.photo_layout._git_head",
                return_value=JUSTIFIED_LAYOUT_SOURCE_COMMIT,
            ),
        ):
            handoff, image, video = self._write_safe_layout_handoff()
            first = materialize_remotion_project(
                handoff,
                self.runtime,
                self.project,
                license_basis="individual",
                include_safe_layout_media=True,
            )
            manifest = read_json_object(first["manifest"], "manifest")
            validate_remotion_project_manifest(manifest)
            self.assertTrue(manifest["capabilities"]["source_media_rendering"])
            self.assertEqual(
                manifest["capabilities"]["render_mode"],
                "explicit_safe_layout_media_preview",
            )
            assets = manifest["media_materialization"]["assets"]
            self.assertEqual(len(assets), 2)
            for row, source in zip(assets, (image, video), strict=True):
                copied = Path(row["project_asset"]["path"])
                self.assertTrue(copied.is_file())
                self.assertEqual(copied.read_bytes(), source.read_bytes())
                self.assertEqual(
                    row["project_asset"]["sha256"], row["source_asset"]["sha256"]
                )
            self.assertEqual(assets[0]["audio_policy"], "not_applicable")
            self.assertEqual(assets[1]["audio_policy"], "muted_visual_only")

            data = read_json_object(
                self.project / "src" / "project-data.json", "project data"
            )
            self.assertTrue(data["sourceMediaRendered"])
            self.assertEqual(len(data["safeLayout"]["placements"]), 2)
            self.assertTrue(data["safeLayout"]["placements"][1]["audioMuted"])
            self.assertEqual(
                data["safeLayout"]["placements"][1]["sourceDurationInFrames"],
                30,
            )
            source = (self.project / "src" / "Composition.tsx").read_text(
                encoding="utf-8"
            )
            self.assertIn("OffthreadVideo", source)
            self.assertIn("staticFile", source)
            self.assertIn("muted", source)

            second = materialize_remotion_project(
                handoff,
                self.runtime,
                self.project,
                license_basis="individual",
                include_safe_layout_media=True,
            )
            self.assertEqual(
                manifest["manifest_sha256"],
                read_json_object(second["manifest"], "manifest")["manifest_sha256"],
            )
            with self.assertRaisesRegex(ContractError, "source media mode drifted"):
                materialize_remotion_project(
                    handoff,
                    self.runtime,
                    self.project,
                    license_basis="individual",
                    include_safe_layout_media=False,
                )

            video.write_bytes(b"mutated after materialization")
            with self.assertRaisesRegex(ContractError, "changed after"):
                validate_remotion_project_manifest(manifest)

    @patch(
        "video_creation_pipeline.remotion_skill_handoff._git_head",
        return_value=REMOTION_SKILLS_COMMIT,
    )
    @patch(
        "video_creation_pipeline.remotion_project._git_head",
        return_value=REMOTION_RUNTIME_COMMIT,
    )
    @patch(
        "video_creation_pipeline.photo_layout._git_head",
        return_value=JUSTIFIED_LAYOUT_SOURCE_COMMIT,
    )
    def test_explicit_scene_media_is_assigned_copied_and_hash_bound(
        self, _layout_head, _runtime_head, _skills_head
    ) -> None:
        handoff, image, video, _scene_plan = self._write_scene_plan_handoff()
        first = materialize_remotion_project(
            handoff,
            self.runtime,
            self.project,
            license_basis="individual",
            include_safe_layout_media=True,
        )
        manifest = read_json_object(first["manifest"], "manifest")
        validate_remotion_project_manifest(manifest)
        self.assertEqual(
            manifest["media_materialization"]["mode"],
            "explicit_copy_from_scene_plan",
        )
        self.assertEqual(
            manifest["capabilities"]["render_mode"],
            "explicit_scene_safe_layout_media_preview",
        )
        self.assertTrue(manifest["capabilities"]["scene_media_assignment"])
        assets = manifest["media_materialization"]["assets"]
        self.assertEqual([row["scene_id"] for row in assets], ["scene-01", "scene-02"])
        self.assertEqual([row["asset_id"] for row in assets], ["image-01", "video-01"])
        for row, source in zip(assets, (image, video), strict=True):
            copied = Path(row["project_asset"]["path"])
            self.assertEqual(copied.read_bytes(), source.read_bytes())
            self.assertEqual(
                row["project_asset"]["sha256"], row["source_asset"]["sha256"]
            )
        self.assertEqual(assets[1]["audio_policy"], "muted_visual_only")

        data = read_json_object(first["data"], "project data")
        self.assertIsNone(data["safeLayout"])
        self.assertEqual(
            data["scenes"][0]["mediaLayout"]["placements"][0]["assetId"],
            "image-01",
        )
        self.assertEqual(
            data["scenes"][1]["mediaLayout"]["placements"][0]["assetId"],
            "video-01",
        )
        self.assertTrue(data["scenes"][1]["mediaLayout"]["placements"][0]["audioMuted"])
        source = first["composition"].read_text(encoding="utf-8")
        self.assertIn("scene.mediaLayout ?? data.safeLayout", source)
        self.assertIn("Missing material:", source)

        second = materialize_remotion_project(
            handoff,
            self.runtime,
            self.project,
            license_basis="individual",
            include_safe_layout_media=True,
        )
        self.assertEqual(
            manifest["manifest_sha256"],
            read_json_object(second["manifest"], "manifest")["manifest_sha256"],
        )

    @patch(
        "video_creation_pipeline.remotion_skill_handoff._git_head",
        return_value=REMOTION_SKILLS_COMMIT,
    )
    @patch(
        "video_creation_pipeline.remotion_project._git_head",
        return_value=REMOTION_RUNTIME_COMMIT,
    )
    @patch(
        "video_creation_pipeline.photo_layout._git_head",
        return_value=JUSTIFIED_LAYOUT_SOURCE_COMMIT,
    )
    def test_scene_plan_metadata_mode_preserves_explicit_missing_material(
        self, _layout_head, _runtime_head, _skills_head
    ) -> None:
        handoff, _image, _video, _scene_plan = self._write_scene_plan_handoff(
            missing_second=True
        )
        paths = materialize_remotion_project(
            handoff,
            self.runtime,
            self.project,
            license_basis="individual",
            include_safe_layout_media=False,
        )
        manifest = read_json_object(paths["manifest"], "manifest")
        self.assertEqual(
            manifest["media_materialization"], {"mode": "none", "assets": []}
        )
        self.assertFalse(manifest["capabilities"]["source_media_rendering"])
        self.assertTrue(manifest["capabilities"]["scene_media_assignment"])
        data = read_json_object(paths["data"], "project data")
        self.assertEqual(data["scenes"][0]["mediaLayout"]["placementCount"], 1)
        self.assertNotIn("placements", data["scenes"][0]["mediaLayout"])
        self.assertIsNone(data["scenes"][1]["mediaLayout"])
        self.assertEqual(
            data["scenes"][1]["missingMaterialReason"], "no approved scene media"
        )

    @patch(
        "video_creation_pipeline.remotion_skill_handoff._git_head",
        return_value=REMOTION_SKILLS_COMMIT,
    )
    @patch(
        "video_creation_pipeline.photo_layout._git_head",
        return_value=JUSTIFIED_LAYOUT_SOURCE_COMMIT,
    )
    def test_scene_plan_for_different_packaging_fails_closed(
        self, _layout_head, _skills_head
    ) -> None:
        _handoff, _image, _video, scene_plan = self._write_scene_plan_handoff()
        other_packaging = self.root / "other-packaging.json"
        other_packaging.write_bytes(self.packaging.read_bytes())
        with self.assertRaisesRegex(ContractError, "bind the handoff packaging"):
            build_remotion_skill_handoff(
                self.skills,
                other_packaging,
                composition_id="MainCandidate",
                safe_media_scene_plan_path=scene_plan,
            )

    @patch(
        "video_creation_pipeline.remotion_skill_handoff._git_head",
        return_value=REMOTION_SKILLS_COMMIT,
    )
    @patch(
        "video_creation_pipeline.remotion_project._git_head",
        return_value=REMOTION_RUNTIME_COMMIT,
    )
    def test_explicit_media_mode_requires_safe_layout(
        self, _runtime_head, _skills_head
    ) -> None:
        with self.assertRaisesRegex(ContractError, "requires a safe layout plan"):
            materialize_remotion_project(
                self.handoff,
                self.runtime,
                self.project,
                license_basis="individual",
                include_safe_layout_media=True,
            )

    @patch(
        "video_creation_pipeline.remotion_skill_handoff._git_head",
        return_value=REMOTION_SKILLS_COMMIT,
    )
    @patch(
        "video_creation_pipeline.remotion_project._git_head",
        return_value=REMOTION_RUNTIME_COMMIT,
    )
    def test_materialize_is_stable_and_keeps_source_media_out(
        self, _runtime_head, _skills_head
    ) -> None:
        first = materialize_remotion_project(
            self.handoff,
            self.runtime,
            self.project,
            license_basis="individual",
        )
        first_manifest = read_json_object(first["manifest"], "manifest")
        second = materialize_remotion_project(
            self.handoff,
            self.runtime,
            self.project,
            license_basis="individual",
        )
        second_manifest = read_json_object(second["manifest"], "manifest")

        self.assertEqual(
            first_manifest["manifest_sha256"], second_manifest["manifest_sha256"]
        )
        self.assertFalse(first_manifest["capabilities"]["source_media_rendering"])
        self.assertTrue(first_manifest["capabilities"]["subtitle_rendering"])
        project_data = read_json_object(
            self.project / "src" / "project-data.json", "data"
        )
        self.assertEqual(
            project_data["subtitleRegion"],
            {"x": 0.1111, "y": 0.6458, "width": 0.6481, "height": 0.0896},
        )
        self.assertEqual(
            (self.project / "public" / "subtitles.srt").read_text(encoding="utf-8"),
            self.subtitle.read_text(encoding="utf-8"),
        )
        source = (self.project / "src" / "Composition.tsx").read_text(encoding="utf-8")
        self.assertIn("CANDIDATE ONLY", source)
        self.assertIn("Source media intentionally not rendered", source)
        captions = (self.project / "src" / "Captions.tsx").read_text(encoding="utf-8")
        self.assertIn("overflowWrap", captions)
        self.assertIn("regionFontSize", captions)
        self.assertNotIn("http://", source)
        self.assertNotIn("https://", source)

    @patch(
        "video_creation_pipeline.remotion_skill_handoff._git_head",
        return_value=REMOTION_SKILLS_COMMIT,
    )
    @patch(
        "video_creation_pipeline.remotion_project._git_head",
        return_value=REMOTION_RUNTIME_COMMIT,
    )
    def test_legacy_no_media_manifest_remains_reusable(
        self, _runtime_head, _skills_head
    ) -> None:
        paths = materialize_remotion_project(
            self.handoff,
            self.runtime,
            self.project,
            license_basis="individual",
        )
        manifest = read_json_object(paths["manifest"], "manifest")
        manifest.pop("media_materialization")
        manifest["authority_boundary"].pop("source_media_copy_explicitly_requested")
        manifest["manifest_sha256"] = payload_sha256(manifest, "manifest_sha256")
        paths["manifest"].write_text(json.dumps(manifest), encoding="utf-8")
        validate_remotion_project_manifest(manifest)
        reused = materialize_remotion_project(
            self.handoff,
            self.runtime,
            self.project,
            license_basis="individual",
        )
        self.assertEqual(reused["manifest"], paths["manifest"])

    @patch(
        "video_creation_pipeline.remotion_skill_handoff._git_head",
        return_value=REMOTION_SKILLS_COMMIT,
    )
    @patch(
        "video_creation_pipeline.remotion_project._git_head",
        return_value=REMOTION_RUNTIME_COMMIT,
    )
    def test_generated_file_drift_and_foreign_directory_fail_closed(
        self, _runtime_head, _skills_head
    ) -> None:
        paths = materialize_remotion_project(
            self.handoff,
            self.runtime,
            self.project,
            license_basis="individual",
        )
        (self.project / "src" / "Composition.tsx").write_text(
            "user edit", encoding="utf-8"
        )
        with self.assertRaisesRegex(ContractError, "changed after"):
            validate_remotion_project_manifest(
                read_json_object(paths["manifest"], "manifest")
            )

        foreign = self.root / "foreign"
        foreign.mkdir()
        (foreign / "README.txt").write_text("keep me", encoding="utf-8")
        with self.assertRaisesRegex(ContractError, "without a managed manifest"):
            materialize_remotion_project(
                self.handoff,
                self.runtime,
                foreign,
                license_basis="individual",
            )

    @patch(
        "video_creation_pipeline.remotion_skill_handoff._git_head",
        return_value=REMOTION_SKILLS_COMMIT,
    )
    @patch(
        "video_creation_pipeline.remotion_project._git_head",
        return_value=REMOTION_RUNTIME_COMMIT,
    )
    def test_doctor_distinguishes_materialization_from_render_readiness(
        self, _runtime_head, _skills_head
    ) -> None:
        source_only = remotion_project_doctor(self.runtime)
        self.assertEqual(source_only["status"], "ready_for_materialization")
        self.assertFalse(source_only["render_ready"])
        materialize_remotion_project(
            self.handoff,
            self.runtime,
            self.project,
            license_basis="individual",
        )
        self._write_installed_runtime()
        browser = self.root / "chrome.exe"
        browser.write_bytes(b"synthetic browser fixture")
        ready = remotion_project_doctor(self.runtime, self.project, browser)
        self.assertEqual(ready["status"], "ready_for_local_candidate_render")
        self.assertTrue(ready["render_ready"])
        self.assertFalse(ready["dependency_installation_performed_by_doctor"])

    @patch(
        "video_creation_pipeline.remotion_skill_handoff._git_head",
        return_value=REMOTION_SKILLS_COMMIT,
    )
    @patch(
        "video_creation_pipeline.remotion_project._git_head",
        return_value=REMOTION_RUNTIME_COMMIT,
    )
    def test_cli_materializes_and_rejects_undeclared_license_basis(
        self, _runtime_head, _skills_head
    ) -> None:
        result = remotion_cli_main(
            [
                "materialize",
                "--handoff",
                str(self.handoff),
                "--runtime-source",
                str(self.runtime),
                "--project-dir",
                str(self.project),
                "--license-basis",
                "individual",
            ]
        )
        self.assertEqual(result, 0)
        with self.assertRaisesRegex(ContractError, "license basis"):
            materialize_remotion_project(
                self.handoff,
                self.runtime,
                self.root / "invalid-license",
                license_basis="assumed_free",
            )

    def _write_skills_source(self) -> None:
        self.skills.mkdir()
        (self.skills / "package.json").write_text(
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
            target = self.skills / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("reviewed fixture", encoding="utf-8")

    def _write_runtime_source(self) -> None:
        for name, package_name in (("core", "remotion"), ("cli", "@remotion/cli")):
            target = self.runtime / "packages" / name / "package.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                json.dumps({"name": package_name, "version": "4.0.477"}),
                encoding="utf-8",
            )
        (self.runtime / "LICENSE.md").write_text(
            "synthetic Remotion license fixture", encoding="utf-8"
        )

    def _write_installed_runtime(self) -> None:
        (self.project / "package-lock.json").write_text(
            json.dumps({"lockfileVersion": 3}), encoding="utf-8"
        )
        versions = {
            "@remotion/captions": "4.0.477",
            "@remotion/cli": "4.0.477",
            "react": "19.2.3",
            "react-dom": "19.2.3",
            "remotion": "4.0.477",
        }
        for name, version in versions.items():
            target = (
                self.project / "node_modules" / Path(*name.split("/")) / "package.json"
            )
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                json.dumps({"name": name, "version": version}), encoding="utf-8"
            )
        cli = self.project / "node_modules" / "@remotion" / "cli" / "remotion-cli.js"
        cli.write_text("// synthetic CLI fixture", encoding="utf-8")

    def _write_safe_layout_handoff(self) -> tuple[Path, Path, Path]:
        media = self.root / "media"
        media.mkdir()
        image = media / "synthetic.png"
        video = media / "synthetic.mp4"
        image.write_bytes(b"synthetic image fixture")
        video.write_bytes(b"synthetic video fixture")
        image_ref = artifact_reference(image, "synthetic image")
        video_ref = artifact_reference(video, "synthetic video")
        dimensions = {
            "width": 640,
            "height": 360,
            "aspect_ratio": round(640 / 360, 8),
            "orientation": "landscape",
        }
        preflight = {
            "schema": "video_creation_pipeline.photo_media_preflight.v1",
            "source_root": str(media.resolve()),
            "scan_policy": {
                "recursive": True,
                "exclude_tokens": [],
                "exclude_by_default": False,
            },
            "assets": [
                {
                    "asset_id": "image-01",
                    "media_type": "image",
                    "source_asset": image_ref,
                    "preflight_status": "ready",
                    "dimensions": dimensions,
                    "face_boxes_normalized": [],
                },
                {
                    "asset_id": "video-01",
                    "media_type": "video",
                    "source_asset": video_ref,
                    "preflight_status": "ready",
                    "dimensions": dimensions,
                    "duration_s": 1.0,
                    "face_boxes_normalized": [],
                },
            ],
            "summary": {
                "asset_count": 2,
                "ready_count": 2,
                "blocked_count": 0,
                "excluded_count": 0,
                "unsupported_count": 0,
            },
            "authority_boundary": {
                "source_media_modified": False,
                "derivative_media_created": False,
                "face_identity_recognition_performed": False,
                "face_detection_is_candidate_only": True,
                "network_execution_performed": False,
                "automatic_render": False,
                "automatic_publish": False,
            },
        }
        preflight["preflight_sha256"] = payload_sha256(preflight, "preflight_sha256")
        preflight_path = self.root / "preflight.json"
        preflight_path.write_text(json.dumps(preflight), encoding="utf-8")

        engine = self.root / "justified-layout"
        entry = engine / "lib" / "index.js"
        entry.parent.mkdir(parents=True)
        entry.write_text("module.exports = () => ({})", encoding="utf-8")
        package = engine / "package.json"
        package.write_text(
            json.dumps(
                {
                    "name": "justified-layout",
                    "version": JUSTIFIED_LAYOUT_VERSION,
                    "license": "ISC",
                }
            ),
            encoding="utf-8",
        )
        license_path = engine / "LICENSE"
        license_path.write_text("MIT fixture", encoding="utf-8")
        placements = [
            {
                "asset_id": "image-01",
                "media_type": "image",
                "source_asset": image_ref,
                "source_dimensions": dimensions,
                "bounds_px": {"x": 40, "y": 200, "width": 480, "height": 270},
                "fit_mode": "contain",
                "crop_allowed": False,
                "protected_regions": [],
            },
            {
                "asset_id": "video-01",
                "media_type": "video",
                "source_asset": video_ref,
                "source_dimensions": dimensions,
                "bounds_px": {"x": 560, "y": 200, "width": 480, "height": 270},
                "fit_mode": "contain",
                "crop_allowed": False,
                "protected_regions": [],
            },
        ]
        layout = {
            "schema": "video_creation_pipeline.safe_media_layout_plan.v1",
            "source_preflight": artifact_reference(preflight_path, "preflight"),
            "layout_engine": {
                "name": "justified-layout",
                "version": JUSTIFIED_LAYOUT_VERSION,
                "reviewed_source_commit": JUSTIFIED_LAYOUT_SOURCE_COMMIT,
                "entry": artifact_reference(entry, "layout entry"),
                "package_metadata": artifact_reference(package, "layout package"),
                "license_file": artifact_reference(license_path, "layout license"),
                "license_observation": {
                    "license_file": "MIT",
                    "package_metadata": "ISC",
                    "metadata_discrepancy_recorded": True,
                },
            },
            "output": {
                "width": 1080,
                "height": 1920,
                "fit_mode": "contain",
                "crop_allowed": False,
                "background_policy": "neutral_unfilled_region",
            },
            "layout_config": {},
            "placements": placements,
            "preview_requirements": {
                "render_representative_sample_before_full": True,
                "transition_frames_per_cut": 3,
                "full_render_allowed_before_sample": False,
            },
            "renderer_handoff": {
                "allowed_generators": ["hyperframes", "remotion"],
                "requires_existing_single_ffmpeg_outlet": True,
                "renderer_execution_performed": False,
                "candidate_only": True,
                "automatic_publish": False,
            },
            "authority_boundary": {
                "source_media_modified": False,
                "face_boxes_are_candidate_only": True,
                "face_identity_recognition_performed": False,
                "automatic_crop": False,
                "automatic_render": False,
                "network_execution_performed": False,
                "automatic_publish": False,
            },
        }
        layout["layout_plan_sha256"] = payload_sha256(layout, "layout_plan_sha256")
        layout_path = self.root / "layout.json"
        layout_path.write_text(json.dumps(layout), encoding="utf-8")
        handoff = build_remotion_skill_handoff(
            self.skills,
            self.packaging,
            composition_id="MainCandidate",
            safe_media_layout_path=layout_path,
            subtitle_path=self.subtitle,
        )
        return (
            write_remotion_skill_handoff(handoff, self.root / "media-handoff.json"),
            image,
            video,
        )

    def _write_scene_plan_handoff(
        self, *, missing_second: bool = False
    ) -> tuple[Path, Path, Path, Path]:
        _global_handoff, image, video = self._write_safe_layout_handoff()
        base_layout = read_json_object(self.root / "layout.json", "layout")
        image_layout = dict(base_layout)
        image_layout["placements"] = [dict(base_layout["placements"][0])]
        image_layout["layout_plan_sha256"] = payload_sha256(
            image_layout, "layout_plan_sha256"
        )
        image_layout_path = self.root / "scene-image-layout.json"
        image_layout_path.write_text(json.dumps(image_layout), encoding="utf-8")

        video_layout = dict(base_layout)
        video_layout["placements"] = [dict(base_layout["placements"][1])]
        video_layout["layout_plan_sha256"] = payload_sha256(
            video_layout, "layout_plan_sha256"
        )
        video_layout_path = self.root / "scene-video-layout.json"
        video_layout_path.write_text(json.dumps(video_layout), encoding="utf-8")

        packaging = json.loads(self.packaging.read_text(encoding="utf-8"))
        packaging["scenes"] = [
            {
                "scene_id": "scene-01",
                "story_segment_ref": "scene-01",
                "start_s": 0.0,
                "end_s": 1.0,
                "evidence_refs": ["evidence:image"],
            },
            {
                "scene_id": "scene-02",
                "story_segment_ref": "scene-02",
                "start_s": 1.0,
                "end_s": 2.0,
                "evidence_refs": ["evidence:video"],
            },
        ]
        self.packaging.write_text(json.dumps(packaging), encoding="utf-8")
        second_assignment = (
            {
                "scene_id": "scene-02",
                "missing_material_reason": "no approved scene media",
            }
            if missing_second
            else {
                "scene_id": "scene-02",
                "safe_media_layout_path": str(video_layout_path),
            }
        )
        spec = self.root / "scene-plan-spec.json"
        spec.write_text(
            json.dumps(
                {
                    "schema": SAFE_MEDIA_SCENE_PLAN_SPEC_SCHEMA,
                    "scenes": [
                        {
                            "scene_id": "scene-01",
                            "safe_media_layout_path": str(image_layout_path),
                        },
                        second_assignment,
                    ],
                }
            ),
            encoding="utf-8",
        )
        plan = build_safe_media_scene_plan(self.packaging, spec)
        plan_path = write_safe_media_scene_plan(
            plan, self.root / "safe-media-scene-plan.json"
        )
        handoff = build_remotion_skill_handoff(
            self.skills,
            self.packaging,
            composition_id="MainCandidate",
            safe_media_scene_plan_path=plan_path,
            subtitle_path=self.subtitle,
        )
        return (
            write_remotion_skill_handoff(
                handoff, self.root / "scene-media-handoff.json"
            ),
            image,
            video,
            plan_path,
        )


if __name__ == "__main__":
    unittest.main()
