"""Synthetic revision inputs for the existing creative handoff CLI."""
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch
from uuid import uuid4

from video_creation_pipeline import drawai_revision
from video_creation_pipeline.contract_io import atomic_write_json
from video_creation_pipeline.creative_tool_handoff import main
from video_creation_pipeline.errors import ContractError
from video_creation_pipeline.generation_contracts import build_shot_generation_unit, write_generation_contract
from video_creation_pipeline.vkp_bridge import build_edit_job, write_edit_job

ROOT = Path(__file__).resolve().parents[1]


class DrawaiRevisionTests(unittest.TestCase):
    def setUp(self):
        base = Path(os.environ.get("TEST_TMP_ROOT", ROOT / ".test-runtime")).resolve()
        base.mkdir(parents=True, exist_ok=True)
        self.root = base / ("drawai-" + uuid4().hex)
        self.root.mkdir()
        bundle = self.root / "bundle"
        shutil.copytree(ROOT / "tests/fixtures/vkp_bundle", bundle)
        self.job = self.root / "edit-job.json"
        write_edit_job(build_edit_job(bundle), self.job)
        self.unit = self.root / "unit.json"
        unit = build_shot_generation_unit(self.job, "storyboard-0001", generator="manual",
                                         media_type="video", width=320, height=180,
                                         visual_prompt="Synthetic revision fixture")
        write_generation_contract(unit, self.unit)
        self.review = self.root / "source-review"
        rows = []
        for index in range(28):
            path = self.review / "source" / f"synthetic-{index}.txt"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"synthetic source evidence {index}", encoding="utf-8")
            rows.append({"path":path.name, "sha256":hashlib.sha256(path.read_bytes()).hexdigest()})
        manifest = atomic_write_json({"repo":"https://github.com/fourzkw/dsh-drawai",
                                      "commit":drawai_revision.SOURCE_COMMIT,"files":rows},
                                     self.review / "source-manifest.json")
        self.pin = patch.object(drawai_revision, "MANIFEST_SHA256", hashlib.sha256(manifest.read_bytes()).hexdigest())
        self.pin.start()
        self.addCleanup(self.pin.stop)
        self.before = self.root / "human.drawai"
        self.after = self.root / "ai.drawai"
        canvas = {"format":"dsh-drawai","schemaVersion":1,"id":"project",
                  "pages":[{"id":"page-1","doc":{"meta":{"pinned":True},"edges":[],
                     "nodes":[{"id":"video-1","shape":"video","label":"confirmed video","x":10},
                              {"id":"label-1","label":"Human title","x":20},
                              {"id":"protected","label":"Confirmed","x":30}]}}]}
        atomic_write_json(canvas, self.before)
        self.candidate = copy.deepcopy(canvas)
        self.candidate["pages"][0]["doc"]["nodes"][1]["label"] = "AI local title"
        atomic_write_json(self.candidate, self.after)
        self.old = self.root / "original-project"
        self.new = self.root / "candidate-project"
        self.old.mkdir()
        total = round(unit["duration_s"] * 30)
        half = total // 2
        timeline = {"version":1,"width":320,"height":180,"fps":30,"durationInFrames":total,
            "shots":[{"id":"shot-1","file":"scene-0.tsx","startFrame":0,"durationFrames":half},
                     {"id":"shot-2","file":"scene-1.tsx","startFrame":half,"durationFrames":total-half}]}
        atomic_write_json(timeline, self.old / "timeline.json")
        atomic_write_json({"entries":[]}, self.old / "asset-sources.json")
        (self.old / "scene-0.tsx").write_text("export default function Scene() { return 'human'; }", encoding="utf-8")
        (self.old / "scene-1.tsx").write_text("export default function Scene() { return 'confirmed'; }", encoding="utf-8")
        shutil.copytree(self.old, self.new)
        (self.new / "scene-0.tsx").write_text("export default function Scene() { return 'local AI change'; }", encoding="utf-8")
        self.brief = {"before_canvas":str(self.before),"candidate_canvas":str(self.after),
             "expected_revision":hashlib.sha256(self.before.read_bytes()).hexdigest()[:12],
             "page_id":"page-1","video_node_id":"video-1","mutable_node_fields":{"label-1":["label"]},
             "before_project":str(self.old),"candidate_project":str(self.new),"mutable_shot_ids":["shot-1"],
             "upstream_warnings":["reported upstream warning retained"],"preview_frames":[]}
        self.brief_path = self.root / "brief.json"
        self.save()

    def tearDown(self):
        # Exact test-owned directory, checked before recursive fixture cleanup.
        self.assertTrue(self.root.resolve().is_relative_to(self.root.parent.resolve()))
        self.assertTrue(self.root.name.startswith("drawai-"))
        shutil.rmtree(self.root)

    def save(self):
        atomic_write_json(self.brief, self.brief_path)

    def check(self):
        return drawai_revision.check_drawai_revision(self.unit, self.brief_path, self.review)

    def test_local_revision_preserves_pinned_nodes_clock_and_protected_scene(self):
        report = self.check()
        self.assertEqual(report["canvas"]["changed_fields"], {"label-1":["label"]})
        self.assertEqual(report["project_mapping"]["changed_files"], ["scene-0.tsx"])
        self.assertEqual(report["upstream_warnings"], self.brief["upstream_warnings"])
        self.assertFalse(report["runtime_verified"])
        self.assertIsNone(report["candidate_mp4"])

    def test_stale_revision_rejected(self):
        self.brief["expected_revision"] = "000000000000"
        self.save()
        with self.assertRaisesRegex(ContractError, "stale"):
            self.check()

    def test_missing_revision_rejected(self):
        self.brief.pop("expected_revision")
        self.save()
        with self.assertRaisesRegex(ContractError, "expected_revision"):
            self.check()

    def test_confirmed_node_and_geometry_and_pin_changes_rejected(self):
        for change in ("protected", "geometry", "pin", "project", "page"):
            candidate = copy.deepcopy(self.candidate)
            if change == "protected":
                candidate["pages"][0]["doc"]["nodes"][2]["label"] = "lost"
            elif change == "geometry":
                candidate["pages"][0]["doc"]["nodes"][1]["x"] = 99
            elif change == "pin":
                candidate["pages"][0]["doc"]["meta"]["pinned"] = False
            elif change == "project":
                candidate["id"] = "another"
            else:
                candidate["pages"][0]["id"] = "another"
            atomic_write_json(candidate, self.after)
            with self.subTest(change=change), self.assertRaises(ContractError):
                self.check()

    def test_protected_scene_and_extra_file_rejected(self):
        protected = self.new / "scene-1.tsx"
        protected.write_text("changed confirmed scene", encoding="utf-8")
        with self.assertRaisesRegex(ContractError, "confirmed project"):
            self.check()
        shutil.copyfile(self.old / protected.name, protected)
        (self.new / "extra.tsx").write_text("extra", encoding="utf-8")
        with self.assertRaisesRegex(ContractError, "inventory"):
            self.check()

    def test_timeline_gap_and_clock_drift_rejected_even_when_both_snapshots_agree(self):
        original = json.loads((self.old / "timeline.json").read_text())
        for change in ("gap", "fps", "duration"):
            data = copy.deepcopy(original)
            if change == "gap":
                data["shots"][1]["startFrame"] += 10
            elif change == "fps":
                data["fps"] = 60
            else:
                data["durationInFrames"] += 30
            for root in (self.old, self.new):
                atomic_write_json(data, root / "timeline.json")
            with self.subTest(change=change), self.assertRaises(ContractError):
                self.check()

    def test_missing_asset_provenance_rejected(self):
        for root in (self.old, self.new):
            (root / "assets").mkdir()
            (root / "assets" / "synthetic.txt").write_text("synthetic asset", encoding="utf-8")
        with self.assertRaisesRegex(ContractError, "omitted"):
            self.check()

    def test_reviewed_source_drift_rejected(self):
        (self.review / "source" / "synthetic-0.txt").write_text("drift", encoding="utf-8")
        with self.assertRaisesRegex(ContractError, "source hash"):
            self.check()

    def test_malformed_asset_path_is_reported_as_contract_error(self):
        for root in (self.old, self.new):
            atomic_write_json({"entries":[{"path":[],"source":"synthetic","license":"owned"}]},
                              root / "asset-sources.json")
        with self.assertRaisesRegex(ContractError, "provenance"):
            self.check()

    def test_native_cli_and_output_collision(self):
        output = self.root / "check.json"
        args = ["drawai-revision-check","--shot-unit",str(self.unit),"--brief",str(self.brief_path),
                "--source-review-root",str(self.review),"--output",str(output)]
        self.assertEqual(main(args), 0)
        self.assertEqual(json.loads(output.read_text())["return_kind"], "editable_project_candidate")
        old_bytes = output.read_bytes()
        self.assertEqual(main(args), 2)
        self.assertEqual(output.read_bytes(), old_bytes)


if __name__ == "__main__":
    unittest.main()
