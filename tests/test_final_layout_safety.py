from __future__ import annotations

import hashlib
import json
import shutil
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from video_creation_pipeline.errors import ContractError
from video_creation_pipeline.final_layout_safety import validate_final_layout
from video_creation_pipeline.final_layout_safety_cli import main as layout_cli


class FinalLayoutSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = ROOT / ".test-runtime" / self._testMethodName
        if self.root.exists():
            shutil.rmtree(self.root)
        self.root.mkdir(parents=True)
        (self.root / "candidate.mp4").write_bytes(b"synthetic candidate")
        (self.root / "layout.json").write_text("{}", encoding="utf-8")

    def tearDown(self) -> None:
        if self.root.exists():
            shutil.rmtree(self.root)

    def artifact(self, name: str) -> dict[str, str]:
        value = (self.root / name).read_bytes()
        return {"path": name, "expected_sha256": hashlib.sha256(value).hexdigest()}

    def document(self) -> dict:
        corners = [
            ("corner-tl", {"x": 0.0, "y": 0.0, "width": 0.1, "height": 0.08}),
            ("corner-tr", {"x": 0.9, "y": 0.0, "width": 0.1, "height": 0.08}),
            ("corner-bl", {"x": 0.0, "y": 0.92, "width": 0.1, "height": 0.08}),
            ("corner-br", {"x": 0.9, "y": 0.92, "width": 0.1, "height": 0.08}),
        ]
        return {
            "schema_version": "video-final-layout-safety.v1",
            "layout_id": "layout-001",
            "duration_seconds": 10.0,
            "candidate": self.artifact("candidate.mp4"),
            "layout_evidence": self.artifact("layout.json"),
            "required_roles": [
                "subtitle",
                "watermark",
                "disclaimer",
                "presenter_window",
                "information_motion",
            ],
            "reserved_regions": [
                {"region_id": region_id, "region_kind": "rounded_corner", "rect": rect}
                for region_id, rect in corners
            ],
            "elements": [
                {
                    "element_id": "watermark-001",
                    "role": "watermark",
                    "start_seconds": 0.0,
                    "end_seconds": 10.0,
                    "rect": {"x": 0.12, "y": 0.04, "width": 0.30, "height": 0.03},
                    "critical_content": True,
                },
                {
                    "element_id": "presenter-001",
                    "role": "presenter_window",
                    "start_seconds": 0.0,
                    "end_seconds": 10.0,
                    "rect": {"x": 0.65, "y": 0.14, "width": 0.22, "height": 0.26},
                    "critical_content": True,
                },
                {
                    "element_id": "motion-001",
                    "role": "information_motion",
                    "start_seconds": 0.0,
                    "end_seconds": 10.0,
                    "rect": {"x": 0.10, "y": 0.28, "width": 0.45, "height": 0.28},
                    "critical_content": True,
                },
                {
                    "element_id": "subtitle-001",
                    "role": "subtitle",
                    "start_seconds": 0.0,
                    "end_seconds": 10.0,
                    "rect": {"x": 0.10, "y": 0.70, "width": 0.80, "height": 0.12},
                    "critical_content": True,
                },
                {
                    "element_id": "disclaimer-001",
                    "role": "disclaimer",
                    "start_seconds": 0.0,
                    "end_seconds": 10.0,
                    "rect": {"x": 0.15, "y": 0.88, "width": 0.70, "height": 0.03},
                    "critical_content": True,
                },
                {
                    "element_id": "ai-label-001", "role": "ai_label",
                    "text": "内容由AI辅助生成。",
                    "start_seconds": 0.0, "end_seconds": 10.0,
                    "rect": {"x": .12, "y": .10, "width": .35, "height": .03},
                    "critical_content": True,
                },
            ],
        }

    def test_all_critical_layers_pass(self) -> None:
        receipt = validate_final_layout(self.document(), self.root)
        self.assertEqual(receipt["checked_elements"], 6)
        self.assertIn("ai_label", receipt["required_roles"])
        self.assertFalse(receipt["automatic_publish"])

    def test_corner_overlap_fails_closed(self) -> None:
        document = self.document()
        document["elements"][0]["rect"] = {
            "x": 0.01,
            "y": 0.01,
            "width": 0.20,
            "height": 0.05,
        }
        with self.assertRaises(ContractError) as error:
            validate_final_layout(document, self.root)
        self.assertIn("enters reserved region corner-tl", str(error.exception))

    def test_missing_required_role_fails_closed(self) -> None:
        document = self.document()
        document["required_roles"].append("qr_code")
        with self.assertRaises(ContractError) as error:
            validate_final_layout(document, self.root)
        self.assertIn("qr_code", str(error.exception))

    def test_ai_label_required_even_when_caller_omits_required_role(self):
        document = self.document()
        document["elements"] = [e for e in document["elements"] if e["role"] != "ai_label"]
        self.assertNotIn("ai_label", document["required_roles"])
        with self.assertRaisesRegex(ContractError, "ai_label"):
            validate_final_layout(document, self.root)

    def test_script_or_editing_only_still_requires_ai_label(self):
        for stage in ("script", "editing", "illustration"):
            document = self.document()
            document["ai_disclosure"] = {"mode": "ai_assisted", "stages": [stage],
                                         "reason": "真人实拍，使用AI辅助制作"}
            self.assertTrue(validate_final_layout(document, self.root)["ai_disclosure"]["required"])
            document["elements"].pop()
            with self.assertRaisesRegex(ContractError, "ai_label"):
                validate_final_layout(document, self.root)

    def test_ai_label_must_cover_beginning_middle_and_end(self):
        for start, end in ((1, 10), (0, 9)):
            document = self.document()
            document["elements"][-1].update(start_seconds=start, end_seconds=end)
            with self.assertRaisesRegex(ContractError, "full video"):
                validate_final_layout(document, self.root)
        document = self.document()
        label = document["elements"][-1]
        label["end_seconds"] = 4
        document["elements"].append({**label, "element_id": "ai-label-002", "start_seconds": 5, "end_seconds": 10})
        with self.assertRaisesRegex(ContractError, "full video"):
            validate_final_layout(document, self.root)
        document["elements"][-1]["start_seconds"] = 4
        self.assertEqual(validate_final_layout(document, self.root)["status"], "passed")

    def test_ai_label_cannot_be_blank_or_downgraded_to_auxiliary(self):
        for text in ("", " ", "制作说明", "mail"):
            document = self.document()
            document["elements"][-1]["text"] = text
            with self.assertRaises(ContractError):
                validate_final_layout(document, self.root)
        document = self.document()
        self.auxiliary(document["elements"][-1])
        with self.assertRaises(ContractError):
            validate_final_layout(document, self.root)

    def test_no_ai_exception_requires_explicit_consistent_record(self):
        document = self.document()
        document["elements"].pop()
        document["ai_disclosure"] = {"mode": "no_ai", "stages": [],
                                     "reason": "作者确认本片各制作环节均未使用AI"}
        self.assertFalse(validate_final_layout(document, self.root)["ai_disclosure"]["required"])
        for change in ({"reason": "   "}, {"stages": ["editing"]}):
            bad = {**document, "ai_disclosure": {**document["ai_disclosure"], **change}}
            with self.assertRaises(ContractError):
                validate_final_layout(bad, self.root)
        document["elements"].append(self.document()["elements"][-1])
        with self.assertRaisesRegex(ContractError, "contradicts"):
            validate_final_layout(document, self.root)

    def auxiliary(self, element):
        element["critical_content"] = False
        element["auxiliary_visibility"] = {
            "mode": "fullscreen_or_controls_hidden", "reason": "Optional corner attribution"}

    def test_auxiliary_corner_and_platform_reservations_are_allowed(self) -> None:
        document = self.document()
        element = document["elements"][0]
        element["rect"] = {"x": .01, "y": .01, "width": .20, "height": .05}
        self.auxiliary(element)
        document["reserved_regions"].append({"region_id": "platform-like", "region_kind": "platform_overlay",
                                             "rect": {"x": 0, "y": 0, "width": .3, "height": .1}})
        receipt = validate_final_layout(document, self.root)
        self.assertTrue(receipt["auxiliary_reserved_region_overlaps"])
        self.assertEqual(receipt["auxiliary_visibility_review"][0]["element_id"], "watermark-001")

    def test_auxiliary_must_remain_inside_canvas_and_not_cover_body(self) -> None:
        document = self.document()
        self.auxiliary(document["elements"][0])
        document["elements"][0]["rect"] = {"x": .95, "y": .2, "width": .2, "height": .03}
        with self.assertRaises(ContractError):
            validate_final_layout(document, self.root)
        document["elements"][0]["rect"] = document["elements"][3]["rect"]
        with self.assertRaisesRegex(ContractError, "collide"):
            validate_final_layout(document, self.root)

    def test_auxiliary_cannot_bypass_important_roles_or_manual_exclusion(self) -> None:
        for index in (1, 3, 4):
            document = self.document()
            self.auxiliary(document["elements"][index])
            with self.assertRaises(ContractError):
                validate_final_layout(document, self.root)
        document = self.document()
        self.auxiliary(document["elements"][0])
        document["reserved_regions"].append({"region_id": "manual-mask", "region_kind": "manual_exclusion",
                                             "rect": document["elements"][0]["rect"]})
        with self.assertRaisesRegex(ContractError, "manual-mask"):
            validate_final_layout(document, self.root)

    def test_shrunken_corner_input_does_not_hide_critical_face_collision(self) -> None:
        document = self.document()
        for region in document["reserved_regions"]:
            region["rect"]["width"] = .015
            region["rect"]["height"] = .015
        document["elements"][1]["rect"] = {"x": .91, "y": .03, "width": .07, "height": .07}
        with self.assertRaisesRegex(ContractError, "preset-corner-top-right"):
            validate_final_layout(document, self.root)

    def test_critical_elements_cannot_collide_at_same_time(self) -> None:
        document = self.document()
        document["elements"][2]["rect"] = document["elements"][1]["rect"]
        with self.assertRaises(ContractError) as error:
            validate_final_layout(document, self.root)
        self.assertIn("presenter-001/motion-001", str(error.exception))

    def test_same_geometry_is_allowed_in_separate_time_windows(self) -> None:
        document = self.document()
        document["elements"][1]["end_seconds"] = 5.0
        document["elements"][2]["start_seconds"] = 5.0
        document["elements"][2]["rect"] = document["elements"][1]["rect"]
        receipt = validate_final_layout(document, self.root)
        self.assertEqual(receipt["status"], "passed")

    def test_nonfinite_times_fail_closed(self) -> None:
        for value in (float("nan"), float("inf"), -float("inf")):
            for field in ("duration_seconds", "start_seconds", "end_seconds"):
                with self.subTest(value=value, field=field):
                    document = self.document()
                    target = (
                        document
                        if field == "duration_seconds"
                        else document["elements"][0]
                    )
                    target[field] = value
                    with self.assertRaises(ContractError):
                        validate_final_layout(document, self.root)

    def test_path_escape_fails_closed(self) -> None:
        document = self.document()
        document["candidate"]["path"] = "../candidate.mp4"
        with self.assertRaises(ContractError):
            validate_final_layout(document, self.root)

    def test_cli_will_not_overwrite_existing_artifact(self) -> None:
        path = self.root / "input.json"
        path.write_text(json.dumps(self.document()), encoding="utf-8")
        target = self.root / "candidate.mp4"
        original = target.read_bytes()
        with patch.object(
            sys,
            "argv",
            [
                "layout",
                str(path),
                "--workspace-root",
                str(self.root),
                "--output",
                str(target),
            ],
        ):
            self.assertEqual(layout_cli(), 2)
        self.assertEqual(target.read_bytes(), original)

    def test_hash_drift_fails_closed(self) -> None:
        document = self.document()
        document["candidate"]["expected_sha256"] = "0" * 64
        with self.assertRaises(ContractError) as error:
            validate_final_layout(document, self.root)
        self.assertIn("hash drifted", str(error.exception))


if __name__ == "__main__":
    unittest.main()
