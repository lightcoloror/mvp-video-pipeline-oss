"""Exercise real routing/validators/consumers; fake only upstream and media IO."""

# ruff: noqa: E402
import copy
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import test_spoken_rough_cut as upstream_fixture
from video_creation_pipeline import spoken_delivery as delivery
sys.path.insert(0, str(delivery.WORKSPACE / "video-knowledge-pipeline/src"))
from video_creation_pipeline.contract_io import (
    artifact_reference,
    atomic_write_json,
    payload_sha256,
)
from video_creation_pipeline.errors import ContractError
from video_creation_pipeline.jianying_draft_handoff import (
    build_jianying_spoken_draft_job,
)
from video_creation_pipeline.nle_bridge import (
    build_spoken_premiere_import_job,
    validate_premiere_import_job,
)
from video_creation_pipeline.spoken_rough_cut import validate_spoken_edit_plan
from video_creation_pipeline.spoken_rough_cut_cli import main


class SpokenDeliveryTests(unittest.TestCase):
    def setUp(self):
        temp_root = Path(os.environ.get("TEST_TMP_ROOT", tempfile.gettempdir()))
        temp_root.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(prefix="spoken-delivery-", dir=temp_root)
        self.addCleanup(self.tmp.cleanup)
        # Reuse the existing semantic-decision fixture, including its real plan
        # validator. Only the external fixed-source CLI is simulated there.
        fixture = upstream_fixture.SpokenRoughCutTests(
            "test_completed_context_review_compiles_auditable_nle_segments"
        )
        with patch.dict(os.environ, {"TEST_TMP_ROOT": self.tmp.name}):
            fixture.setUp()
            fixture.test_completed_context_review_compiles_auditable_nle_segments()
        self.root = fixture.root
        self.plan_path = self.root / "plan/spoken-edit-plan.json"
        self.plan = json.loads(self.plan_path.read_text(encoding="utf8"))
        self.source_patch = patch(
            "video_creation_pipeline.spoken_rough_cut._videocut_source_manifest",
            return_value=fixture.source_manifest,
        )
        self.source_patch.start()
        self.addCleanup(self.source_patch.stop)
        self.serial = 0

    def write(self, value, name=None):
        self.serial += 1
        return atomic_write_json(
            value, self.root / (name or f"item-{self.serial}.json")
        )

    def request(self, kind, editor=None):
        value = {
            "schema": delivery.REQUEST_SCHEMA,
            "deliverable": kind,
            "selection_basis": "Synthetic requested deliverable",
        }
        if editor is not None:
            value["editor"] = editor
        return self.write(value)

    def selection(self, kind, editor=None):
        req = self.request(kind, editor)
        return self.write(delivery.select_delivery(self.plan_path, req))

    def test_compile_is_editor_independent_and_all_destinations_share_same_plan(self):
        self.assertTrue(self.plan["schema"].endswith(".v2"))
        self.assertEqual(self.plan["next_action"], "select_delivery_from_request")
        before = self.plan_path.read_bytes()
        for kind, editor, route in [
            ("review", None, "source_review"),
            ("video", None, "ffmpeg"),
            ("editable_project", "jianying", "jianying"),
            ("editable_project", "premiere", "premiere"),
        ]:
            value = delivery.select_delivery(self.plan_path, self.request(kind, editor))
            self.assertEqual(value["route"], route)
            delivery.validate_delivery_selection(value)
            self.assertFalse(value["execution_performed"])
        self.assertEqual(before, self.plan_path.read_bytes())

    def test_request_rejects_unknown_mismatched_editor_and_implicit_project_choice(
        self,
    ):
        for kind, editor in [
            ("review", "jianying"),
            ("video", "premiere"),
            ("editable_project", None),
            ("editable_project", "unknown"),
        ]:
            with (
                self.subTest(kind=kind, editor=editor),
                self.assertRaises(ContractError),
            ):
                delivery.select_delivery(self.plan_path, self.request(kind, editor))
        value = {
            "schema": delivery.REQUEST_SCHEMA,
            "deliverable": "video",
            "selection_basis": " ",
            "force": True,
        }
        with self.assertRaises(ContractError):
            delivery.validate_delivery_request(value)

    def test_old_plan_is_readable_but_does_not_select_jianying(self):
        old = copy.deepcopy(self.plan)
        old.update(
            schema="video_creation_pipeline.spoken_edit_plan.v1",
            next_action="materialize_editable_jianying_candidate",
        )
        old["plan_sha256"] = payload_sha256(old, "plan_sha256")
        validate_spoken_edit_plan(old)
        path = self.write(old)
        selected = delivery.select_delivery(path, self.request("review"))
        self.assertTrue(selected["legacy_next_action_ignored"])
        self.assertEqual(selected["route"], "source_review")
        with self.assertRaisesRegex(ContractError, "explicit spoken delivery request"):
            build_jianying_spoken_draft_job(path, self.root / "missing-vectcut")

    def test_v2_cannot_reintroduce_old_default_by_rehashing(self):
        value = copy.deepcopy(self.plan)
        value["next_action"] = "materialize_editable_jianying_candidate"
        value["plan_sha256"] = payload_sha256(value, "plan_sha256")
        with self.assertRaisesRegex(ContractError, "next action"):
            validate_spoken_edit_plan(value)

    def test_selection_rehash_cannot_change_requested_route(self):
        value = delivery.select_delivery(self.plan_path, self.request("review"))
        value["route"] = "jianying"
        value["selection_sha256"] = payload_sha256(value, "selection_sha256")
        with self.assertRaises(ContractError):
            delivery.validate_delivery_selection(value)

    def test_changed_plan_or_request_invalidates_selection_before_consumers(self):
        req = self.request("video")
        selected = self.write(delivery.select_delivery(self.plan_path, req))
        value = json.loads(req.read_text(encoding="utf8"))
        value["deliverable"] = "review"
        atomic_write_json(value, req)
        with (
            patch.object(delivery, "execute_ffmpeg_with_receipt") as run,
            self.assertRaises(ContractError),
        ):
            delivery.prepare_delivery(selected, self.root / "blocked")
        run.assert_not_called()
        self.assertFalse((self.root / "blocked").exists())
        selected = self.selection("video")
        value = copy.deepcopy(self.plan)
        value["summary"]["kept_duration_s"] += 0.1
        value["plan_sha256"] = payload_sha256(value, "plan_sha256")
        atomic_write_json(value, self.plan_path)
        with self.assertRaises(ContractError):
            delivery.prepare_delivery(selected, self.root / "drift")

    def test_review_builds_existing_page_with_exact_source_ranges_and_no_media(self):
        from video_knowledge_pipeline import targeted_review

        def probe(path):
            return SimpleNamespace(
                sha256=hashlib.sha256(Path(path).read_bytes()).hexdigest(),
                duration_seconds=6,
                width=320,
                height=240,
                fps=30,
            )

        with (
            patch.object(targeted_review, "probe_video", side_effect=probe),
            patch.object(targeted_review, "_playback_duration_seconds", side_effect=lambda path, duration: duration),
            patch.object(delivery, "execute_ffmpeg_with_receipt") as render,
        ):
            result = delivery.prepare_delivery(
                self.selection("review"), self.root / "review-out"
            )
        render.assert_not_called()
        self.assertTrue(Path(result["review_page"]).is_file())
        pack = json.loads(
            (self.root / "review-out/review/targeted-review-pack.json").read_text(
                encoding="utf8"
            )
        )
        self.assertEqual(
            [
                (r["review_start_seconds"], r["review_end_seconds"])
                for r in pack["requests"]
            ],
            [(0, 1), (2, 6)],
        )
        sources = {s["source_id"]: s for s in pack["sources"]}
        self.assertTrue(
            all(
                Path(sources[r["source_id"]]["path"])
                == Path(self.plan["source_video"]["path"])
                for r in pack["requests"]
            )
        )
        self.assertEqual(result["media_copies_created"], 0)
        self.assertFalse(list((self.root / "review-out").rglob("*.mp4")))

    def test_review_cannot_use_render_entry_even_with_all_render_options(self):
        with (
            patch.object(delivery, "execute_ffmpeg_with_receipt") as run,
            self.assertRaises(ContractError),
        ):
            delivery.render_delivery(
                self.selection("review"),
                self.root / "no-render",
                project_root=self.root,
                lifecycle_manifest=self.root / "missing.json",
                estimated_output_bytes=1000,
            )
        run.assert_not_called()

    def test_video_preparation_is_not_an_export_and_preserves_time_mapping(self):
        with patch.object(delivery, "execute_ffmpeg_with_receipt") as run:
            result = delivery.prepare_delivery(
                self.selection("video"), self.root / "video-plan"
            )
        run.assert_not_called()
        self.assertFalse(result["video_rendered"])
        mapping = json.loads(
            Path(result["timeline_map"]["path"]).read_text(encoding="utf8")
        )
        self.assertEqual(
            [(r["target_start"], r["target_end"]) for r in mapping["segments"]],
            [(0, 1), (1, 5)],
        )

    def test_jianying_requires_matching_request_before_loading_any_serializer(self):
        with patch(
            "video_creation_pipeline.jianying_draft_handoff._vectcut_source_manifest"
        ) as source:
            for kind in ("review", "video"):
                with self.assertRaises(ContractError):
                    build_jianying_spoken_draft_job(
                        self.plan_path,
                        self.root,
                        delivery_request_path=self.request(kind),
                    )
            source.assert_not_called()

    def test_premiere_job_reuses_existing_contract_without_fabricating_desktop_success(
        self,
    ):
        request = self.request("editable_project", "premiere")
        job = build_spoken_premiere_import_job(self.plan_path, request)
        validate_premiere_import_job(job)
        self.assertEqual(job["status"], "awaiting_target_identity")
        self.assertEqual(job["segments"], self.plan["nle_segments"])
        result = delivery.prepare_delivery(
            self.selection("editable_project", "premiere"),
            self.root / "premiere",
            project_id="synthetic-project",
            sequence_id="synthetic-sequence",
        )
        self.assertEqual(result["status"], "ready_for_premiere_import")
        self.assertFalse(result["editor_opened"])
        ready = json.loads(Path(result["job"]["path"]).read_text(encoding="utf8"))
        validate_premiere_import_job(ready)
        bad = copy.deepcopy(ready)
        bad["source_delivery_request"] = artifact_reference(
            self.request("editable_project", "jianying"), "request"
        )
        bad["task_sha256"] = payload_sha256(bad, "task_sha256")
        with self.assertRaises(ContractError):
            validate_premiere_import_job(bad)

    def test_missing_editor_dependency_does_not_fallback_or_write(self):
        with self.assertRaises(ContractError):
            delivery.prepare_delivery(
                self.selection("editable_project", "jianying"), self.root / "no-editor"
            )
        self.assertFalse((self.root / "no-editor").exists())
        with self.assertRaises(ContractError):
            delivery.prepare_delivery(
                self.selection("video"), self.root / "options", vectcut_source=self.root
            )

    def test_renderer_uses_existing_guard_and_checks_audio_duration_decode(self):
        source_info = {
            "streams": [
                {"codec_type": "video", "avg_frame_rate": "30/1"},
                {"codec_type": "audio"},
            ],
            "format": {"duration": "6"},
        }
        output_info = {**source_info, "format": {"duration": "5"}}
        manifest = self.write({"synthetic": True})
        disk = SimpleNamespace(preflight=Mock(return_value={"status": "passed"}))
        calls = []

        def outlet(op, cmd, inputs, outputs, receipt, **kwargs):
            calls.append((op, cmd, inputs, kwargs))
            for output in outputs:
                Path(output["path"]).write_bytes(b"synthetic-encoded-media")
            return atomic_receipt(receipt)

        def atomic_receipt(path):
            value = {"outlet_id": "video_creation_pipeline.single_ffmpeg_outlet"}
            atomic_write_json(value, path)
            return value

        with (
            patch.object(delivery, "_self_media_module", return_value=disk),
            patch.object(
                delivery, "probe_media_metadata", side_effect=[source_info, output_info]
            ),
            patch.object(delivery, "execute_ffmpeg_with_receipt", side_effect=outlet),
        ):
            result = delivery.render_delivery(
                self.selection("video"),
                self.root / "render",
                project_root=self.root,
                lifecycle_manifest=manifest,
                estimated_output_bytes=100000,
            )
        self.assertEqual([c[0] for c in calls], ["render", "technical_qa"])
        self.assertTrue(all(c[3]["guard_resource"] for c in calls))
        self.assertIn("-n", calls[0][1])
        self.assertIn("trim=start=2.0:end=6.0", " ".join(calls[0][1]))
        self.assertTrue(result["video_rendered"])
        self.assertFalse(result["human_review_completed"])
        self.assertFalse(result["packaging_complete"])

    def test_disk_block_does_not_run_ffmpeg_or_create_output(self):
        disk = SimpleNamespace(preflight=Mock(return_value={"status": "blocked"}))
        with (
            patch.object(delivery, "_self_media_module", return_value=disk),
            patch.object(delivery, "execute_ffmpeg_with_receipt") as run,
            self.assertRaises(ContractError),
        ):
            delivery.render_delivery(
                self.selection("video"),
                self.root / "disk-blocked",
                project_root=self.root,
                lifecycle_manifest=self.write({}),
                estimated_output_bytes=100000,
            )
        run.assert_not_called()
        self.assertFalse((self.root / "disk-blocked").exists())

    def test_source_media_drift_blocks_all_consumers(self):
        selected = self.selection("video")
        Path(self.plan["source_video"]["path"]).write_bytes(b"changed synthetic source")
        with (
            patch.object(delivery, "execute_ffmpeg_with_receipt") as run,
            self.assertRaises(ContractError),
        ):
            delivery.prepare_delivery(selected, self.root / "changed-source")
        run.assert_not_called()
        self.assertFalse((self.root / "changed-source").exists())

    def test_bad_render_duration_or_missing_original_audio_cannot_report_success(self):
        source = {
            "streams": [
                {"codec_type": "video", "avg_frame_rate": "30/1"},
                {"codec_type": "audio"},
            ],
            "format": {"duration": "6"},
        }
        invalid = [
            {**source, "format": {"duration": "6"}},
            {"streams": [{"codec_type": "video"}], "format": {"duration": "5"}},
        ]
        manifest = self.write({"synthetic": True})
        disk = SimpleNamespace(preflight=Mock(return_value={"status": "passed"}))
        for index, observed in enumerate(invalid):
            out = self.root / f"bad-render-{index}"
            with (
                self.subTest(index=index),
                patch.object(delivery, "_self_media_module", return_value=disk),
                patch.object(
                    delivery, "probe_media_metadata", side_effect=[source, observed]
                ),
                patch.object(
                    delivery, "execute_ffmpeg_with_receipt", return_value={}
                ) as run,
                self.assertRaisesRegex(ContractError, "timeline/audio"),
            ):
                delivery.render_delivery(
                    self.selection("video"),
                    out,
                    project_root=self.root,
                    lifecycle_manifest=manifest,
                    estimated_output_bytes=100000,
                )
            self.assertEqual(run.call_count, 1)
            self.assertFalse((out / "delivery-result.json").exists())

    def test_cli_select_and_prepare_reach_existing_consumers(self):
        req = self.request("editable_project", "premiere")
        selected = self.root / "cli-selection.json"
        self.assertEqual(
            main(
                [
                    "select-delivery",
                    "--plan",
                    str(self.plan_path),
                    "--request",
                    str(req),
                    "--output",
                    str(selected),
                ]
            ),
            0,
        )
        out = self.root / "cli-prepared"
        self.assertEqual(
            main(
                [
                    "prepare-delivery",
                    "--selection",
                    str(selected),
                    "--output-dir",
                    str(out),
                ]
            ),
            0,
        )
        self.assertTrue((out / "premiere-job.json").exists())
        self.assertEqual(
            main(
                [
                    "select-delivery",
                    "--plan",
                    str(self.plan_path),
                    "--request",
                    str(req),
                    "--output",
                    str(selected),
                ]
            ),
            2,
        )


if __name__ == "__main__":
    unittest.main()
