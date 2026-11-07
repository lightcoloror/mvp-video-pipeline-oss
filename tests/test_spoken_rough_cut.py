from __future__ import annotations

# ruff: noqa: E402

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from video_creation_pipeline.contract_io import atomic_write_json, payload_sha256
from video_creation_pipeline.errors import ContractError
from video_creation_pipeline.spoken_rough_cut import (
    DEFAULT_NATURAL_PACING_POLICY,
    VIDEOCUT_KIT_COMMIT,
    build_spoken_edit_decision_template,
    build_spoken_edit_plan,
    prepare_spoken_edit_judge_packet,
    restore_spoken_edit_decision,
    validate_spoken_edit_agent_decisions,
    validate_spoken_edit_plan,
)


class SpokenRoughCutTests(unittest.TestCase):
    def setUp(self) -> None:
        managed_root = Path(os.environ.get("TEST_TMP_ROOT", tempfile.gettempdir()))
        self.root = managed_root / "mvp-spoken-rough-cut" / self._testMethodName
        if self.root.exists():
            shutil.rmtree(self.root)
        self.root.mkdir(parents=True)
        self.video = self.root / "talking-head.mp4"
        self.video.write_bytes(b"synthetic-video")
        self.tokens = self.root / "transcript.tokens.json"
        self.tokens.write_text(
            json.dumps(
                [
                    {
                        "isGap": False,
                        "start": 0.2,
                        "end": 0.5,
                        "original_text": "这",
                    },
                    {
                        "isGap": False,
                        "start": 0.5,
                        "end": 0.8,
                        "original_text": "是",
                    },
                    {
                        "isGap": False,
                        "start": 1.6,
                        "end": 1.8,
                        "original_text": "嗯",
                    },
                    {
                        "isGap": False,
                        "start": 2.0,
                        "end": 2.4,
                        "original_text": "重新",
                    },
                    {
                        "isGap": False,
                        "start": 2.4,
                        "end": 2.8,
                        "original_text": "开始。",
                    },
                    {
                        "isGap": False,
                        "start": 3.6,
                        "end": 4.0,
                        "original_text": "最后",
                    },
                    {
                        "isGap": False,
                        "start": 4.0,
                        "end": 4.4,
                        "original_text": "讲顺。",
                    },
                ],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        self.videocut = self.root / "videocut-kit"
        self.videocut.mkdir()
        self.source_manifest = {
            "repository": "Jiang59991/videocut-kit",
            "commit": VIDEOCUT_KIT_COMMIT,
            "root": str(self.videocut.resolve()),
            "scripts": {
                name: {
                    "path": str((self.videocut / f"{name}.js").resolve()),
                    "sha256": f"{index:064x}",
                    "relative_path": f"scripts/{name}.js",
                }
                for index, name in enumerate(
                    ("prepare_judge", "snap_boundaries", "auto_cut"), start=1
                )
            },
            "license_status": "no_repository_license_detected",
            "distribution_policy": "external_source_required_no_vendoring",
        }
        for script in self.source_manifest["scripts"].values():
            Path(script["path"]).write_text("// fixture\n", encoding="utf-8")

    def tearDown(self) -> None:
        if self.root.exists():
            shutil.rmtree(self.root)

    def test_prepare_and_template_keep_short_pauses_and_flag_fillers(self) -> None:
        def fake_prepare(command, _timeout, _label):
            prefix = Path(command[-1])
            Path(f"{prefix}.candidates.json").write_text(
                json.dumps(
                    [
                        {
                            "id": "c0",
                            "kind": "pause",
                            "start": 0.8,
                            "end": 1.6,
                            "dur": 0.8,
                            "where": "between_sentences",
                            "context": "这是 ⟨停0.8s⟩ 嗯",
                        },
                        {
                            "id": "c1",
                            "kind": "filler",
                            "start": 1.6,
                            "end": 1.8,
                            "text": "嗯",
                            "context": "这是【嗯】重新开始",
                        },
                    ],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            Path(f"{prefix}.fulltext.txt").write_text(
                "[0 0.2s] 这是 ⟨间隔0.8s⟩\n[1 1.6s] 嗯重新开始。\n",
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(command, 0, "", "prepared")

        with (
            patch(
                "video_creation_pipeline.spoken_rough_cut._videocut_source_manifest",
                return_value=self.source_manifest,
            ),
            patch(
                "video_creation_pipeline.spoken_rough_cut._run",
                side_effect=fake_prepare,
            ),
        ):
            packet = prepare_spoken_edit_judge_packet(
                self.video,
                self.tokens,
                self.root / "judge",
                self.videocut,
            )
            template = build_spoken_edit_decision_template(
                self.root / "judge" / "spoken-edit-judge-packet.json"
            )

        self.assertEqual(packet["candidate_count"], 2)
        by_id = {row["candidate_id"]: row for row in template["decisions"]}
        self.assertEqual(by_id["c0"]["action"], "keep")
        self.assertEqual(by_id["c1"]["action"], "review_required")
        self.assertEqual(template["status"], "awaiting_agent_context_review")
        self.assertEqual(packet["pacing_policy"], DEFAULT_NATURAL_PACING_POLICY)

    def test_completed_context_review_compiles_auditable_nle_segments(self) -> None:
        packet_path = self._packet()
        packet = json.loads(packet_path.read_text(encoding="utf-8"))
        with patch(
            "video_creation_pipeline.spoken_rough_cut._videocut_source_manifest",
            return_value=self.source_manifest,
        ):
            decisions = build_spoken_edit_decision_template(packet_path)
        for row in decisions["decisions"]:
            if row["candidate_id"] == "c0":
                row.update(
                    {
                        "action": "keep",
                        "reason": "强调停顿保留",
                        "confidence": 0.9,
                    }
                )
        decisions["decisions"].append(
            {
                "decision_id": "semantic-retry-0001",
                "candidate_id": "",
                "kind": "retry_previous_take",
                "action": "delete",
                "start": 1.0,
                "end": 2.0,
                "reason": "删除前一次未讲顺的版本，保留最后一次",
                "confidence": 0.95,
                "context": "前一次重讲",
            }
        )
        decisions["boundary_review"] = dict(opening_checked=True,ending_checked=True,cut_boundaries_checked=True,events_checked=True,evidence="synthetic boundary review")
        decisions['rough_audio_review']=dict(levels_checked=True,clipping_checked=True,evidence='synthetic level and clipping review fixture')
        from segment_level_fixture import make_review
        levels = make_review(self.root/'audio-levels', self.video,
            [dict(start=0,end=1),dict(start=2,end=6)], json.loads(self.tokens.read_text(encoding='utf-8')))
        decisions['rough_audio_review']['segment_leveling'] = levels
        decisions['clip_gains'] = levels['clip_gains']
        decisions["decisions"][-1]["take_review"] = dict(complete_checked=True,meaning_checked=True,natural_delivery_checked=True,quality_checked=True,unique_information_accounted=True,evidence="synthetic comparison",retained_interval={"start":2.0,"end":2.8},compared_intervals=[{"start":1.0,"end":2.0},{"start":2.0,"end":2.8}])
        decisions["status"] = "agent_review_complete"
        decisions["reviewed_fulltext_sha256"] = packet["judge_fulltext"]["sha256"]
        decisions["review"].update(
            {"fulltext_read": True, "semantic_retry_scan_completed": True}
        )
        decisions["decisions_sha256"] = payload_sha256(decisions, "decisions_sha256")
        decisions_path = self.root / "agent-decisions.json"
        atomic_write_json(decisions, decisions_path)

        def fake_run(command, _timeout, label):
            if "snap_boundaries" in label:
                source = Path(command[2])
                target = Path(command[4])
                target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
            else:
                work = Path(command[2])
                (work / "delete_segments.json").write_text(
                    '[{"start":1.0,"end":2.0}]', encoding="utf-8"
                )
                (work / "cut.segments.json").write_text(
                    '[{"start":0.0,"end":1.0},{"start":2.0,"end":6.0}]',
                    encoding="utf-8",
                )
                (work / "auto_cut.report.md").write_text(
                    "# synthetic dry-run report\n", encoding="utf-8"
                )
            return subprocess.CompletedProcess(command, 0, "", "")

        with (
            patch(
                "video_creation_pipeline.spoken_rough_cut._videocut_source_manifest",
                return_value=self.source_manifest,
            ),
            patch(
                "video_creation_pipeline.spoken_rough_cut._media_duration",
                return_value=6.0,
            ),
            patch(
                "video_creation_pipeline.spoken_rough_cut._run", side_effect=fake_run
            ),
        ):
            validate_spoken_edit_agent_decisions(
                decisions, packet, require_complete=True
            )
            plan = build_spoken_edit_plan(
                packet_path, decisions_path, self.root / "plan"
            )
            validate_spoken_edit_plan(plan)

        self.assertEqual(plan["summary"]["removed_duration_s"], 1.0)
        self.assertEqual(len(plan["nle_segments"]), 2)
        self.assertFalse(plan["execution_receipt"]["ffmpeg_executed"])
        self.assertEqual(plan["next_action"], "select_delivery_from_request")

    def test_reference_script_cannot_be_promoted_to_authority(self) -> None:
        packet_path = self._packet()
        packet = json.loads(packet_path.read_text(encoding="utf-8"))
        with patch(
            "video_creation_pipeline.spoken_rough_cut._videocut_source_manifest",
            return_value=self.source_manifest,
        ):
            decisions = build_spoken_edit_decision_template(packet_path)
        decisions["review"]["reference_script_is_authoritative"] = True
        decisions["decisions_sha256"] = payload_sha256(decisions, "decisions_sha256")
        with (
            patch(
                "video_creation_pipeline.spoken_rough_cut._videocut_source_manifest",
                return_value=self.source_manifest,
            ),
            self.assertRaisesRegex(ContractError, "authority boundary"),
        ):
            validate_spoken_edit_agent_decisions(decisions, packet)

    def test_compile_cannot_skip_segment_audio_but_preserves_cut_recipe(self):
        self.test_completed_context_review_compiles_auditable_nle_segments()
        plan=json.loads((self.root/'plan/spoken-edit-plan.json').read_text(encoding='utf-8'))
        packet_path=Path(plan['source_packet']['path'])
        decisions=json.loads((self.root/'agent-decisions.json').read_text(encoding='utf-8'))
        decisions['rough_audio_review'].pop('segment_leveling')
        decisions['clip_gains']=[]
        decisions['decisions_sha256']=payload_sha256(decisions,'decisions_sha256')
        dp=atomic_write_json(decisions,self.root/'missing-levels.json')
        def dry_run(command,_timeout,label):
            if 'snap_boundaries' in label:
                shutil.copyfile(command[2],command[4])
            else:
                for name in ('delete_segments.json','cut.segments.json'):
                    shutil.copyfile(self.root/'plan'/name,Path(command[2])/name)
                (Path(command[2])/'auto_cut.report.md').write_text('synthetic dry run')
            return subprocess.CompletedProcess(command,0,'','')
        out=self.root/'pending-audio'
        with patch('video_creation_pipeline.spoken_rough_cut._videocut_source_manifest',return_value=self.source_manifest), \
             patch('video_creation_pipeline.spoken_rough_cut._media_duration',return_value=6), \
             patch('video_creation_pipeline.spoken_rough_cut._run',side_effect=dry_run), \
             self.assertRaisesRegex(ContractError,'whole-track|Whole-track'):
            build_spoken_edit_plan(packet_path,dp,out)
        self.assertTrue((out/'cut.segments.json').is_file())
        self.assertTrue((out/'segment-level-spec.json').is_file())
        self.assertFalse((out/'spoken-edit-plan.json').exists())

    @unittest.skipUnless(
        os.environ.get("VIDEOCUT_KIT_SOURCE_ROOT"),
        "fixed videocut-kit source is required",
    )
    def test_actual_prepare_judge_source_executes(self) -> None:
        packet = prepare_spoken_edit_judge_packet(
            self.video,
            self.tokens,
            self.root / "actual-judge",
            os.environ["VIDEOCUT_KIT_SOURCE_ROOT"],
        )
        self.assertEqual(packet["videocut_kit"]["commit"], VIDEOCUT_KIT_COMMIT)
        self.assertGreaterEqual(packet["candidate_count"], 1)

    def _restore_versions(self):
        packet_path = self._packet()
        packet = json.loads(packet_path.read_text(encoding="utf-8"))
        with patch("video_creation_pipeline.spoken_rough_cut._videocut_source_manifest", return_value=self.source_manifest):
            before = build_spoken_edit_decision_template(packet_path)
        before['reviewed_fulltext_sha256'] = packet['judge_fulltext']['sha256']
        before['boundary_review'] = dict(opening_checked=True,ending_checked=True,cut_boundaries_checked=True,events_checked=True,evidence='synthetic boundary review')
        before['rough_audio_review']=dict(levels_checked=True,clipping_checked=True,evidence='synthetic level and clipping review fixture')
        before['status'] = 'agent_review_complete'
        before['review'].update(fulltext_read=True, semantic_retry_scan_completed=True)
        before['decisions'][0].update(action='keep', confidence=.9, reason='synthetic pause retained')
        before['decisions'].append(dict(decision_id='independent',kind='manual_context_deletion',action='keep',start=.2,end=.8,confidence=.9,reason='independent synthetic row'))
        before['decisions_sha256'] = payload_sha256(before, 'decisions_sha256')
        current = json.loads(json.dumps(before))
        current['decisions'][0].update(action='compress', preserve_s=.55, reason='synthetic preview compression',
            pause_review=dict(semantic_role='ordinary',audio_checked=True,context_checked=True,evidence='synthetic fixture review'))
        current['decisions'][1]['reason'] = 'later independent update remains'
        current['decisions_sha256'] = payload_sha256(current, 'decisions_sha256')
        for name,value in [('before',before),('current',current)]:
            atomic_write_json(value,self.root/f'{name}.json')
        return packet_path,before,current

    def test_restore_one_row_preserves_unrelated_edit_and_sources(self):
        packet,before,current = self._restore_versions()
        with patch("video_creation_pipeline.spoken_rough_cut._videocut_source_manifest", return_value=self.source_manifest):
            restored = restore_spoken_edit_decision(packet,self.root/'current.json',self.root/'before.json','candidate-c0')
        self.assertEqual(restored['decisions'][0],before['decisions'][0])
        self.assertEqual(restored['decisions'][1],current['decisions'][1])
        self.assertEqual(restored['authority_boundary'],current['authority_boundary'])
        self.assertEqual(json.loads((self.root/'current.json').read_text(encoding='utf-8')),current)

    def test_restore_rejects_missing_id_and_tampered_version(self):
        packet,before,current = self._restore_versions()
        with patch("video_creation_pipeline.spoken_rough_cut._videocut_source_manifest", return_value=self.source_manifest):
            with self.assertRaisesRegex(ContractError,'both versions'):
                restore_spoken_edit_decision(packet,self.root/'current.json',self.root/'before.json','absent')
            before['judge_packet_sha256'] = '0'*64
            before['decisions_sha256'] = payload_sha256(before,'decisions_sha256')
            atomic_write_json(before,self.root/'before.json')
            with self.assertRaisesRegex(ContractError,'bind the judge packet'):
                restore_spoken_edit_decision(packet,self.root/'current.json',self.root/'before.json','candidate-c0')

    def test_restore_cli_rejects_overwrite(self):
        from video_creation_pipeline.spoken_rough_cut_cli import main
        packet,before,current = self._restore_versions()
        result = main(['restore-decision','--packet',str(packet),'--decisions',str(self.root/'current.json'),'--restore-from',str(self.root/'before.json'),'--decision-id','candidate-c0','--output',str(self.root/'current.json')])
        self.assertEqual(result,2)
        self.assertEqual(json.loads((self.root/'current.json').read_text(encoding='utf-8')),current)

    def _packet(self) -> Path:
        candidates = self.root / "judge.candidates.json"
        candidates.write_text(
            json.dumps(
                [
                    {
                        "id": "c0",
                        "kind": "pause",
                        "start": 2.8,
                        "end": 3.6,
                        "dur": 0.8,
                        "where": "between_sentences",
                        "context": "重新开始。 ⟨停0.8s⟩ 最后讲顺。",
                    }
                ],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        fulltext = self.root / "judge.fulltext.txt"
        fulltext.write_text(
            "[0 0.2s] 这是\n[1 1.6s] 嗯重新开始。\n[2 3.6s] 最后讲顺。\n",
            encoding="utf-8",
        )
        from video_creation_pipeline.contract_io import artifact_reference

        packet = {
            "schema": "video_creation_pipeline.spoken_edit_judge_packet.v1",
            "source_video": artifact_reference(self.video, "source video"),
            "tokens": artifact_reference(self.tokens, "transcript tokens"),
            "silence": None,
            "reference_script": None,
            "reference_script_authority": "non_authoritative_reference",
            "videocut_kit": self.source_manifest,
            "judge_candidates": artifact_reference(candidates, "judge candidates"),
            "judge_fulltext": artifact_reference(fulltext, "judge full text"),
            "candidate_count": 1,
            "candidate_kind_counts": {"pause": 1},
            "pacing_policy": dict(DEFAULT_NATURAL_PACING_POLICY),
            "agent_review_contract": {
                "must_read_fulltext": True,
                "must_decide_every_objective_candidate": True,
                "must_find_retries_false_starts_and_incomplete_phrases": True,
                "default_retry_resolution": DEFAULT_NATURAL_PACING_POLICY["retry_rule"],
                "low_confidence_semantic_deletion_allowed": False,
                "reference_script_may_override_recorded_speech": False,
            },
            "execution_boundary": {
                "candidate_material_only": True,
                "asr_executed": False,
                "ffmpeg_executed": False,
                "source_media_modified": False,
                "jianying_opened": False,
                "automatic_publish": False,
            },
            "prepare_log": {"returncode": 0, "stderr_tail": ""},
        }
        packet["packet_sha256"] = payload_sha256(packet, "packet_sha256")
        path = self.root / "spoken-edit-judge-packet.json"
        atomic_write_json(packet, path)
        return path


if __name__ == "__main__":
    unittest.main()
