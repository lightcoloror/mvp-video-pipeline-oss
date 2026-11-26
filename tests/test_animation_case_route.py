"""Local case selection is read-only, filterable and fails visibly on drift."""
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from video_creation_pipeline.object_scene_routes import (
    animation_source_catalog,
    recommend_scene_route,
)


class CaseRouteTests(unittest.TestCase):
    def test_filter_and_preview_drift(self):
        with tempfile.TemporaryDirectory() as folder:
            video = Path(folder) / "preview.mp4"
            video.write_bytes(b"example")
            project = {
                "id": "sample",
                "layer": "shot",
                "deliveries": ["video"],
                "clock": "frame",
                "adoption": "reference_only",
            }
            case = {
                "id": "local",
                "techniques": ["diagram"],
                "source_project": "sample",
                "preview": str(video),
                "preview_sha256": hashlib.sha256(b"example").hexdigest(),
            }
            catalog = {
                "catalog_sha256": "test",
                "projects": [project],
                "techniques": {"diagram": {"projects": ["sample"], "checks": []}},
                "existing_consumers": {},
                "local_case_examples": [case],
            }
            with patch(
                "video_creation_pipeline.object_scene_routes.animation_source_catalog",
                return_value=catalog,
            ):
                result = recommend_scene_route("flow", techniques=["diagram"], delivery="video")
                self.assertEqual(
                    result["module_selection"]["local_case_examples"][0]["preview_status"],
                    "verified",
                )
                self.assertTrue(
                    result["module_selection"]["local_case_examples"][0]["recommendation_only"]
                )
                video.write_bytes(b"changed")
                result = recommend_scene_route("flow", techniques=["diagram"], delivery="video")
                self.assertEqual(
                    result["module_selection"]["local_case_examples"][0]["preview_status"],
                    "changed",
                )
                video.unlink()
                result = recommend_scene_route("flow", techniques=["diagram"], delivery="video")
                self.assertEqual(
                    result["module_selection"]["local_case_examples"][0]["preview_status"],
                    "missing",
                )
                result = recommend_scene_route("flow", techniques=["diagram"], delivery="still")
                self.assertEqual(result["module_selection"]["local_case_examples"], [])

    def test_live_catalog_local_cases_verify_for_three_core_categories(self):
        catalog = animation_source_catalog()
        cases = catalog.get("local_case_examples", [])
        self.assertGreaterEqual(len(cases), 13)
        checks = [
            ("flow", ["continuous_transform"], "case-20261125-shotcraft-icon-morph"),
            ("flow", ["continuous_transform"], "case-20261115-shape-transition"),
            ("authored_performance", ["character_performance"], "case-20261125-pelican-rig-motion"),
            ("authored_performance", ["character_performance"], "case-20261115-ip-illustration-movement"),
            ("flow", ["diagram", "narration"], "case-20261115-narrated-relation"),
            ("flow", ["narration"], "case-20261120-juzu-b01-screen"),
        ]
        for job, techs, expected_id in checks:
            result = recommend_scene_route(job, techniques=techs, delivery="video", narration_timing="none")
            matched = {
                row["id"]: row
                for row in result["module_selection"]["local_case_examples"]
            }
            self.assertIn(expected_id, matched)
            self.assertEqual(matched[expected_id]["preview_status"], "verified")
            self.assertTrue(matched[expected_id]["recommendation_only"])


if __name__ == "__main__":
    unittest.main()
