from __future__ import annotations

# ruff: noqa: E402

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

from video_creation_pipeline.contract_io import artifact_reference, payload_sha256
from video_creation_pipeline.errors import ContractError
from video_creation_pipeline.photo_layout import (
    JUSTIFIED_LAYOUT_SOURCE_COMMIT,
    JUSTIFIED_LAYOUT_VERSION,
)
from video_creation_pipeline.photo_layout_cli import main as photo_layout_cli_main
from video_creation_pipeline.safe_media_scene_plan import (
    SAFE_MEDIA_SCENE_PLAN_SPEC_SCHEMA,
    build_safe_media_scene_plan,
    validate_safe_media_scene_plan,
    write_safe_media_scene_plan,
)


class SafeMediaScenePlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = ROOT / ".test-runtime" / f"safe-media-scenes-{uuid4().hex}"
        self.root.mkdir(parents=True)
        self.packaging = self.root / "packaging.json"
        self.packaging.write_text(
            json.dumps(
                {
                    "schema": "video_creation_pipeline.video_packaging_plan.v1",
                    "output": {"width": 1080, "height": 1920, "fps": 30},
                    "scenes": [
                        {"scene_id": "scene-01", "start_s": 0.0, "end_s": 2.0},
                        {"scene_id": "scene-02", "start_s": 2.0, "end_s": 4.0},
                    ],
                }
            ),
            encoding="utf-8",
        )
        self.layout = self._write_layout("layout-01", width=1080, height=1920)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    @patch(
        "video_creation_pipeline.photo_layout._git_head",
        return_value=JUSTIFIED_LAYOUT_SOURCE_COMMIT,
    )
    def test_build_validate_write_and_cli_are_hash_stable(self, _git_head) -> None:
        spec = self._write_spec(
            [
                {
                    "scene_id": "scene-01",
                    "safe_media_layout_path": str(self.layout),
                },
                {
                    "scene_id": "scene-02",
                    "missing_material_reason": "no approved visual source",
                },
            ]
        )
        first = build_safe_media_scene_plan(self.packaging, spec)
        second = build_safe_media_scene_plan(self.packaging, spec)
        self.assertEqual(first["plan_sha256"], second["plan_sha256"])
        self.assertEqual(
            first["summary"],
            {"scene_count": 2, "layout_ready_count": 1, "missing_material_count": 1},
        )
        self.assertEqual(
            first["scenes"][1]["missing_material_reason"],
            "no approved visual source",
        )
        output = write_safe_media_scene_plan(first, self.root / "scene-plan.json")
        validate_safe_media_scene_plan(first)
        self.assertEqual(
            photo_layout_cli_main(["validate", "--artifact", str(output)]), 0
        )
        cli_output = self.root / "scene-plan-cli.json"
        self.assertEqual(
            photo_layout_cli_main(
                [
                    "build-scene-plan",
                    "--packaging-plan",
                    str(self.packaging),
                    "--spec",
                    str(spec),
                    "--output",
                    str(cli_output),
                ]
            ),
            0,
        )
        self.assertEqual(
            json.loads(cli_output.read_text(encoding="utf-8"))["plan_sha256"],
            first["plan_sha256"],
        )

    @patch(
        "video_creation_pipeline.photo_layout._git_head",
        return_value=JUSTIFIED_LAYOUT_SOURCE_COMMIT,
    )
    def test_incomplete_unknown_and_ambiguous_assignments_fail_closed(
        self, _git_head
    ) -> None:
        cases = (
            (
                [{"scene_id": "scene-01", "safe_media_layout_path": str(self.layout)}],
                "every packaging scene",
            ),
            (
                [
                    {
                        "scene_id": "scene-01",
                        "safe_media_layout_path": str(self.layout),
                    },
                    {"scene_id": "scene-03", "missing_material_reason": "missing"},
                ],
                "unknown scene",
            ),
            (
                [
                    {
                        "scene_id": "scene-01",
                        "safe_media_layout_path": str(self.layout),
                        "missing_material_reason": "ambiguous",
                    },
                    {"scene_id": "scene-02", "missing_material_reason": "missing"},
                ],
                "exactly one",
            ),
        )
        for index, (rows, message) in enumerate(cases):
            with self.subTest(case=index):
                spec = self._write_spec(rows, name=f"invalid-{index}.json")
                with self.assertRaisesRegex(ContractError, message):
                    build_safe_media_scene_plan(self.packaging, spec)

    @patch(
        "video_creation_pipeline.photo_layout._git_head",
        return_value=JUSTIFIED_LAYOUT_SOURCE_COMMIT,
    )
    def test_dimension_and_source_hash_drift_fail_closed(self, _git_head) -> None:
        wrong_size = self._write_layout("wrong-size", width=720, height=1280)
        spec = self._write_spec(
            [
                {
                    "scene_id": "scene-01",
                    "safe_media_layout_path": str(wrong_size),
                },
                {"scene_id": "scene-02", "missing_material_reason": "missing"},
            ]
        )
        with self.assertRaisesRegex(ContractError, "dimensions must match"):
            build_safe_media_scene_plan(self.packaging, spec)

        valid_spec = self._write_spec(
            [
                {
                    "scene_id": "scene-01",
                    "safe_media_layout_path": str(self.layout),
                },
                {"scene_id": "scene-02", "missing_material_reason": "missing"},
            ],
            name="valid.json",
        )
        plan = build_safe_media_scene_plan(self.packaging, valid_spec)
        self.packaging.write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(ContractError, "changed after"):
            validate_safe_media_scene_plan(plan)

    def _write_spec(
        self, scenes: list[dict[str, str]], name: str = "spec.json"
    ) -> Path:
        path = self.root / name
        path.write_text(
            json.dumps({"schema": SAFE_MEDIA_SCENE_PLAN_SPEC_SCHEMA, "scenes": scenes}),
            encoding="utf-8",
        )
        return path

    def _write_layout(self, name: str, *, width: int, height: int) -> Path:
        media = self.root / f"{name}.png"
        media.write_bytes(f"synthetic {name}".encode())
        media_ref = artifact_reference(media, name)
        dimensions = {
            "width": 640,
            "height": 360,
            "aspect_ratio": round(640 / 360, 8),
            "orientation": "landscape",
        }
        preflight = {
            "schema": "video_creation_pipeline.photo_media_preflight.v1",
            "source_root": str(self.root.resolve()),
            "scan_policy": {
                "recursive": True,
                "exclude_tokens": [],
                "exclude_by_default": False,
            },
            "assets": [
                {
                    "asset_id": name,
                    "media_type": "image",
                    "source_asset": media_ref,
                    "preflight_status": "ready",
                    "dimensions": dimensions,
                    "face_boxes_normalized": [],
                }
            ],
            "summary": {
                "asset_count": 1,
                "ready_count": 1,
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
        preflight_path = self.root / f"{name}-preflight.json"
        preflight_path.write_text(json.dumps(preflight), encoding="utf-8")

        engine = self.root / "justified-layout"
        entry = engine / "lib" / "index.js"
        entry.parent.mkdir(parents=True, exist_ok=True)
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
                "width": width,
                "height": height,
                "fit_mode": "contain",
                "crop_allowed": False,
                "background_policy": "neutral_unfilled_region",
            },
            "layout_config": {},
            "placements": [
                {
                    "asset_id": name,
                    "media_type": "image",
                    "source_asset": media_ref,
                    "source_dimensions": dimensions,
                    "bounds_px": {
                        "x": 20,
                        "y": 20,
                        "width": min(640, width - 40),
                        "height": min(360, height - 40),
                    },
                    "fit_mode": "contain",
                    "crop_allowed": False,
                    "protected_regions": [],
                }
            ],
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
        path = self.root / f"{name}.layout.json"
        path.write_text(json.dumps(layout), encoding="utf-8")
        return path


if __name__ == "__main__":
    unittest.main()
