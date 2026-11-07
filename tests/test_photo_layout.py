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

from video_creation_pipeline.contract_io import payload_sha256
from video_creation_pipeline.errors import ContractError
from video_creation_pipeline.photo_layout import (
    JUSTIFIED_LAYOUT_SOURCE_COMMIT,
    build_photo_media_preflight,
    build_safe_media_layout_plan,
    photo_layout_doctor,
    validate_photo_media_preflight,
    validate_safe_media_layout_plan,
    write_photo_layout_contract,
)


class PhotoLayoutTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = ROOT / ".test-runtime" / f"photo-layout-{uuid4().hex}"
        self.media = self.root / "media"
        self.media.mkdir(parents=True)
        self.engine_entry = self._write_layout_engine()
        self._write_image(self.media / "landscape.png", 640, 360)
        self._write_image(self.media / "portrait.png", 360, 640)
        (self.media / "camera.heic").write_bytes(b"intentionally not auto-converted")

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_preflight_is_hash_stable_and_does_not_modify_sources(self) -> None:
        before = {
            path.name: path.read_bytes()
            for path in self.media.iterdir()
            if path.is_file()
        }
        first = build_photo_media_preflight(self.media)
        second = build_photo_media_preflight(self.media)

        self.assertEqual(first["preflight_sha256"], second["preflight_sha256"])
        self.assertEqual(
            before["landscape.png"], (self.media / "landscape.png").read_bytes()
        )
        self.assertEqual(first["summary"]["asset_count"], 3)
        self.assertEqual(first["summary"]["ready_count"], 2)
        blocked = next(
            row for row in first["assets"] if row["relative_path"] == "camera.heic"
        )
        self.assertEqual(blocked["preflight_status"], "blocked_requires_derivative")
        self.assertFalse(first["authority_boundary"]["source_media_modified"])

    @patch("video_creation_pipeline.photo_layout._git_head")
    def test_builds_contain_only_plan_with_preview_requirements(
        self, git_head_mock
    ) -> None:
        git_head_mock.return_value = JUSTIFIED_LAYOUT_SOURCE_COMMIT
        preflight = build_photo_media_preflight(self.media)
        preflight_path = write_photo_layout_contract(
            preflight, self.root / "preflight.json"
        )
        asset_ids = [
            row["asset_id"]
            for row in preflight["assets"]
            if row["preflight_status"] == "ready"
        ]
        with patch(
            "video_creation_pipeline.photo_layout._run_justified_layout",
            side_effect=self._geometry,
        ):
            first = build_safe_media_layout_plan(
                preflight_path,
                self.engine_entry,
                asset_ids=asset_ids,
                width=1920,
                height=1080,
            )
            second = build_safe_media_layout_plan(
                preflight_path,
                self.engine_entry,
                asset_ids=asset_ids,
                width=1920,
                height=1080,
            )

        self.assertEqual(first["layout_plan_sha256"], second["layout_plan_sha256"])
        self.assertTrue(first["renderer_handoff"]["candidate_only"])
        self.assertFalse(first["authority_boundary"]["automatic_crop"])
        self.assertFalse(
            first["preview_requirements"]["full_render_allowed_before_sample"]
        )
        self.assertEqual(
            first["preview_requirements"]["transition_offsets_s"], [-0.3, 0.0, 0.3]
        )
        self.assertTrue(
            all(row["fit_mode"] == "contain" for row in first["placements"])
        )
        self.assertTrue(
            all(row["crop_allowed"] is False for row in first["placements"])
        )
        validate_safe_media_layout_plan(first)

    def test_preflight_source_hash_drift_fails_closed(self) -> None:
        preflight = build_photo_media_preflight(self.media)
        self._write_image(self.media / "landscape.png", 640, 360, value=30)

        with self.assertRaisesRegex(ContractError, "changed after"):
            validate_photo_media_preflight(preflight)

    @patch("video_creation_pipeline.photo_layout._git_head")
    def test_layout_engine_drift_and_blocked_media_fail_closed(
        self, git_head_mock
    ) -> None:
        git_head_mock.return_value = JUSTIFIED_LAYOUT_SOURCE_COMMIT
        preflight = build_photo_media_preflight(self.media)
        preflight_path = write_photo_layout_contract(
            preflight, self.root / "preflight.json"
        )
        ready_asset = next(
            row["asset_id"]
            for row in preflight["assets"]
            if row["preflight_status"] == "ready"
        )
        with patch(
            "video_creation_pipeline.photo_layout._run_justified_layout",
            side_effect=self._geometry,
        ):
            plan = build_safe_media_layout_plan(
                preflight_path,
                self.engine_entry,
                asset_ids=[ready_asset],
                width=1280,
                height=720,
            )
        self.engine_entry.write_text("module.exports = () => ({})", encoding="utf-8")
        with self.assertRaisesRegex(ContractError, "changed after"):
            validate_safe_media_layout_plan(plan)

        blocked_asset = next(
            row["asset_id"]
            for row in preflight["assets"]
            if row["preflight_status"] != "ready"
        )
        with self.assertRaisesRegex(ContractError, "not ready"):
            build_safe_media_layout_plan(
                preflight_path,
                self.engine_entry,
                asset_ids=[blocked_asset],
                width=1280,
                height=720,
            )

    def test_preflight_without_assets_fails_closed(self) -> None:
        payload = {
            "schema": "video_creation_pipeline.photo_media_preflight.v1",
            "source_root": str(self.media),
            "scan_policy": {
                "recursive": True,
                "exclude_tokens": [],
                "supported_image_extensions": [],
                "supported_heic_extensions": [],
                "supported_video_extensions": [],
                "exclude_by_default": False,
            },
            "assets": [],
            "summary": {"asset_count": 0, "ready_count": 0, "blocked_count": 0},
            "authority_boundary": {},
        }
        payload["preflight_sha256"] = payload_sha256(payload, "preflight_sha256")
        with self.assertRaisesRegex(ContractError, "requires assets"):
            validate_photo_media_preflight(payload)

    def test_doctor_never_claims_automatic_rendering(self) -> None:
        result = photo_layout_doctor()
        self.assertFalse(result["automatic_render"])
        self.assertFalse(result["network_execution_performed"])
        self.assertFalse(result["source_media_modified"])

    @patch("video_creation_pipeline.photo_layout._git_head")
    def test_unreviewed_layout_source_fails_closed(self, git_head_mock) -> None:
        git_head_mock.return_value = "0" * 40
        preflight = build_photo_media_preflight(self.media)
        preflight_path = write_photo_layout_contract(
            preflight, self.root / "preflight.json"
        )
        ready_asset = next(
            row["asset_id"]
            for row in preflight["assets"]
            if row["preflight_status"] == "ready"
        )

        with self.assertRaisesRegex(ContractError, "source commit drifted"):
            build_safe_media_layout_plan(
                preflight_path,
                self.engine_entry,
                asset_ids=[ready_asset],
                width=1280,
                height=720,
            )

    def _write_layout_engine(self) -> Path:
        engine = self.root / "justified-layout"
        lib = engine / "lib"
        lib.mkdir(parents=True)
        (engine / "package.json").write_text(
            json.dumps(
                {"name": "justified-layout", "version": "4.1.0", "license": "ISC"}
            ),
            encoding="utf-8",
        )
        (engine / "LICENSE").write_text("MIT License\n", encoding="utf-8")
        entry = lib / "index.js"
        entry.write_text(
            "module.exports = (items) => ({\n"
            "  containerHeight: 360,\n"
            "  boxes: items.map((item, index) => ({\n"
            "    left: 80 + index * 620, top: 30, width: 560, height: 300\n"
            "  }))\n"
            "});\n",
            encoding="utf-8",
        )
        return entry

    @staticmethod
    def _write_image(path: Path, width: int, height: int, value: int = 180) -> None:
        import cv2
        import numpy

        image = numpy.full((height, width, 3), value, dtype=numpy.uint8)
        if not cv2.imwrite(str(path), image):
            raise RuntimeError(f"could not write fixture image: {path}")

    @staticmethod
    def _geometry(
        _entry: Path, items: list[dict[str, int]], _config: dict[str, object]
    ) -> dict[str, object]:
        return {
            "containerHeight": 360,
            "boxes": [
                {"left": 80 + index * 620, "top": 30, "width": 560, "height": 300}
                for index, _item in enumerate(items)
            ],
        }


if __name__ == "__main__":
    unittest.main()
