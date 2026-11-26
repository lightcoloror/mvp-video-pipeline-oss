import copy
import hashlib
import json
import unittest
from unittest.mock import patch

import test_creative_tool_handoff as fixtures
import test_photocraft_revision as photo_fixtures
from video_creation_pipeline.contract_io import artifact_reference, atomic_write_json, payload_sha256
from video_creation_pipeline.creative_tool_handoff import main, REVIEWED_TOOLS
from video_creation_pipeline.creative_tool_verification import (
    FILM_RETURN_SCHEMA, FORMATS, LOSS_AXES, TRIAL_STEPS, SHADER_EXTRA_PINS,
    audit_shader_snapshot, build_verification_plan, validate_verification_plan,
    validate_filmcraft_return, consume_photocraft_return,
)
from video_creation_pipeline.errors import ContractError
from video_creation_pipeline.creative_gates import build_approval_record
from video_creation_pipeline.generation_contracts import build_generation_task, CAPABILITY_PREFLIGHT_SCHEMA, GENERATION_RECEIPT_SCHEMA


class CreativeVerificationTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.CreativeToolHandoffTests()
        self.f.setUp(); self.addCleanup(self.f.doCleanups)
        self.root = self.f.root
        self.handoff = atomic_write_json(self.f.build('filmcraft'), self.root / 'handoff.json')
        self.plan = build_verification_plan(self.handoff, 30000, 1001)
        self.plan_path = atomic_write_json(self.plan, self.root / 'plan.json')

    def returned(self):
        return self.rehash({'schema': FILM_RETURN_SCHEMA, 'source_plan': artifact_reference(self.plan_path, 'plan'),
            'scene_id': self.plan['scene_id'], 'clock': self.plan['clock'], 'status': 'candidate_only', 'publish_approved': False,
            'software_trial': {'status': 'blocked', 'limitations': 'Synthetic contract only; no installed FilmCraft'},
            'loss_report': {axis: {'status': 'unverified', 'detail': 'not measured', 'evidence': []} for axis in LOSS_AXES},
            'interchange_reports': {'import': {'status': 'unverified'}, 'export': {'status': 'unverified'}},
            'native_project': None, 'candidate_media': None, 'manual_media': None})

    def rehash(self, value):
        value['return_sha256'] = payload_sha256(value, 'return_sha256'); return value

    def reported(self):
        data = self.returned()
        # Arbitrary files deliberately count only as reported evidence, never native runtime proof.
        evidence = self.root / 'reported.txt'; evidence.write_text('synthetic reported evidence, not application output')
        native = self.root / 'candidate.project'; native.write_text('synthetic native-project placeholder, not actually imported')
        media = self.root / 'candidate.mp4'; media.write_bytes(b'synthetic media placeholder, not decoded')
        empty = atomic_write_json({'entries': []}, self.root / 'report.json')
        ref = artifact_reference(evidence, 'evidence')
        data['software_trial'] = {'status': 'reported_executed', 'limitations': 'Synthetic reported-artifact contract only',
            'steps': {step: ref for step in TRIAL_STEPS},
            'windows_provenance': {'platform': 'windows', 'version': 'synthetic', 'binary': ref, 'origin_evidence': ref}}
        data['native_project'] = artifact_reference(native, 'native')
        data['candidate_media'] = artifact_reference(media, 'media')
        data['loss_report'] = {axis: {'status': 'preserved', 'detail': 'synthetic assertion only', 'evidence': [ref]} for axis in LOSS_AXES}
        data['interchange_reports'] = {k: {'status': 'reported', 'format': 'Otio', 'artifact': artifact_reference(empty, 'report')} for k in ('import', 'export')}
        return self.rehash(data)

    def test_plan_keeps_rational_rate_scene_binding_and_no_execution(self):
        self.assertEqual(self.plan, build_verification_plan(self.handoff, 30000, 1001))
        self.assertEqual(self.plan['clock']['fps_denominator'], 1001)
        self.assertEqual(self.plan['source_audit']['declared_import_export_formats'], list(FORMATS))
        self.assertFalse(self.plan['application_import_file']); self.assertFalse(self.plan['runtime_verified'])
        validate_verification_plan(self.plan)

    def test_bad_rates_and_authority_drift_fail(self):
        for n, d in [(0, 1), (30, 0), (True, 1), (30.0, 1), (500, 1)]:
            with self.subTest(n=n, d=d), self.assertRaises(ContractError): build_verification_plan(self.handoff, n, d)
        self.plan['runtime_verified'] = True
        self.plan['plan_sha256'] = payload_sha256(self.plan, 'plan_sha256')
        with self.assertRaises(ContractError): validate_verification_plan(self.plan)

    def test_blocked_return_is_valid_but_cannot_enter_qa(self):
        result = validate_filmcraft_return(self.returned())
        self.assertFalse(result['can_enter_existing_qa']); self.assertFalse(result['runtime_verified'])
        self.assertEqual(set(result['loss_review_required']), set(LOSS_AXES))

    def test_unrun_software_cannot_claim_preserved_losses_or_native_project(self):
        data = self.returned(); data['loss_report']['audio']['status'] = 'preserved'
        data['loss_report']['audio']['evidence'] = [artifact_reference(self.handoff, 'evidence')]
        with self.assertRaisesRegex(ContractError, 'unrun'): validate_filmcraft_return(self.rehash(data))
        data = self.returned(); data['native_project'] = artifact_reference(self.handoff, 'request')
        with self.assertRaisesRegex(ContractError, 'unrun'): validate_filmcraft_return(self.rehash(data))

    def test_every_loss_axis_and_both_reports_required(self):
        for field, key in [('loss_report', 'timing'), ('interchange_reports', 'export')]:
            data = self.returned(); del data[field][key]
            with self.assertRaises(ContractError): validate_filmcraft_return(self.rehash(data))

    def test_clock_or_scene_drift_rejected_after_rehash(self):
        data = self.returned(); data['clock'] = {**data['clock'], 'fps_denominator': 1000}
        with self.assertRaisesRegex(ContractError, 'clock'): validate_filmcraft_return(self.rehash(data))
        data = self.returned(); data['scene_id'] = 'foreign-scene'
        with self.assertRaisesRegex(ContractError, 'mismatch'): validate_filmcraft_return(self.rehash(data))

    def test_reported_evidence_never_proves_runtime_or_native_format(self):
        result = validate_filmcraft_return(self.reported())
        self.assertFalse(result['runtime_verified']); self.assertFalse(result['native_project_verified'])
        self.assertFalse(result['can_enter_existing_qa']) # Missing existing manual-media receipt.

    def test_warning_and_info_reports_remain_reviewable(self):
        data = self.reported()
        report = atomic_write_json({'entries': [{'level': 'Warning', 'message': 'Unsupported transition dropped', 'count': 2},
                                              {'level': 'Info', 'message': 'Assumed media base directory', 'count': 1}]}, self.root / 'losses.json')
        data['interchange_reports']['export']['artifact'] = artifact_reference(report, 'report')
        result = validate_filmcraft_return(self.rehash(data))
        self.assertEqual(len(result['interchange_review_required']), 2)
        self.assertFalse(result['can_enter_existing_qa'])

    def test_renamed_request_packet_is_not_native_project(self):
        data = self.reported(); copied = self.root / 'renamed.filmcraft'; copied.write_bytes(self.handoff.read_bytes())
        data['native_project'] = artifact_reference(copied, 'native')
        with self.assertRaisesRegex(ContractError, 'not a native project'): validate_filmcraft_return(self.rehash(data))

    def test_missing_windows_origin_or_software_step_rejected(self):
        data = self.reported(); del data['software_trial']['steps']['reopen']
        with self.assertRaises(ContractError): validate_filmcraft_return(self.rehash(data))
        data = self.reported(); del data['software_trial']['windows_provenance']['origin_evidence']
        with self.assertRaises(ContractError): validate_filmcraft_return(self.rehash(data))
        data = self.reported(); data['software_trial']['steps'] = list(TRIAL_STEPS)
        with self.assertRaises(ContractError): validate_filmcraft_return(self.rehash(data))

    def test_manual_media_gate_reused_and_warning_still_blocks_qa(self):
        data = self.reported()
        ap = atomic_write_json(build_approval_record('storyboard', self.f.unit, approved_by='synthetic-test-fixture',
                               approved_at='2026-10-12T00:00:00+08:00'), self.root / 'approval.json')
        pf = atomic_write_json({'schema': CAPABILITY_PREFLIGHT_SCHEMA, 'checked_at': '2026-10-12T00:00:00+08:00',
            'capabilities': [{'id': 'manual_media_import', 'ready': True, 'probe_method': 'manual_operator_verification',
                              'installation_performed': False, 'evidence': {'synthetic': True}}]}, self.root / 'preflight.json')
        task = build_generation_task(self.f.unit, ap, pf, output_dir=self.root)
        tp = atomic_write_json(task, self.root / 'task.json')
        receipt = {'schema': GENERATION_RECEIPT_SCHEMA, 'task_id': task['task_id'], 'task_sha256': task['task_sha256'],
            'generator': 'manual', 'status': 'completed', 'published': False, 'fallback_used': False, 'output': data['candidate_media'],
            'technical_verification': {'width': 1080, 'height': 1920, 'has_video_stream': True, 'duration_s': task['duration_s'], 'probe_method': 'synthetic-test-only'},
            'visual_verification': {'inspected': True, 'blank': False, 'black': False, 'wrong_composition': False, 'representative_frames': [data['candidate_media']]}}
        rp = atomic_write_json(receipt, self.root / 'receipt.json')
        data['manual_media'] = {'task': artifact_reference(tp, 'task'), 'receipt': artifact_reference(rp, 'receipt')}
        result = validate_filmcraft_return(self.rehash(data))
        self.assertTrue(result['can_enter_existing_qa']); self.assertFalse(result['runtime_verified'])
        warning = atomic_write_json({'entries': [{'level': 'Warning', 'message': 'lost font', 'count': 1}]}, self.root / 'warning.json')
        data['interchange_reports']['export']['artifact'] = artifact_reference(warning, 'warning')
        self.assertFalse(validate_filmcraft_return(self.rehash(data))['can_enter_existing_qa'])
        receipt['visual_verification']['blank'] = True; atomic_write_json(receipt, rp)
        data['manual_media']['receipt'] = artifact_reference(rp, 'receipt')
        with self.assertRaisesRegex(ContractError, 'visual inspection'): validate_filmcraft_return(self.rehash(data))

    def test_unknown_format_bad_warning_and_changed_artifact_rejected(self):
        data = self.reported(); data['interchange_reports']['import']['format'] = 'Prproj'
        with self.assertRaisesRegex(ContractError, 'format'): validate_filmcraft_return(self.rehash(data))
        data = self.reported(); self.root.joinpath('report.json').write_text('{"entries":[]} ')
        with self.assertRaisesRegex(ContractError, 'changed'): validate_filmcraft_return(data)
        data = self.reported(); bad = atomic_write_json({'entries': [{'level': 'Warning', 'message': 'drop', 'count': 0}]}, self.root / 'bad.json')
        data['interchange_reports']['import']['artifact'] = artifact_reference(bad, 'bad')
        with self.assertRaisesRegex(ContractError, 'invalid upstream'): validate_filmcraft_return(self.rehash(data))

    def test_cli_uses_existing_entrypoint_and_never_overwrites_plan(self):
        target = self.root / 'cli-plan.json'
        args = ['verification-plan', '--handoff', str(self.handoff), '--fps-numerator', '30', '--fps-denominator', '1', '--output', str(target)]
        self.assertEqual(main(args), 0); self.assertEqual(main(args), 2)
        self.assertEqual(main(['verification-validate', '--input', str(target)]), 0)

    def test_shader_supplemental_pins_and_missing_dependency_closure(self):
        extra = {}
        pin = REVIEWED_TOOLS['shaders']
        for relative in SHADER_EXTRA_PINS:
            content = b"import {missing} from './missing';\n// synthetic pinned audit fixture\n"
            local = f"upstream/{pin['repo']}/{relative}"
            file = self.f.review / local; file.parent.mkdir(parents=True, exist_ok=True); file.write_bytes(content)
            sha = hashlib.sha256(content).hexdigest(); extra[relative] = sha
            self.f.manifest['files'].append({'repo': pin['repo'], 'commit': pin['commit'], 'path': relative,
                                           'local_path': local, 'sha256': sha, 'bytes': len(content)})
        self.f.save_manifest()
        with patch.dict(SHADER_EXTRA_PINS, extra):
            handoff = self.f.build('shaders'); audit = audit_shader_snapshot(handoff)
            self.assertFalse(audit['dependency_closure_complete']); self.assertTrue(audit['missing_imports'])
            self.assertFalse(audit['telemetry']['sampling_activation_verified'])
            file.write_bytes(b'changed')
            with self.assertRaises(ContractError): audit_shader_snapshot(handoff)


class PhotoCraftConsumerTests(unittest.TestCase):
    def setUp(self):
        self.f = photo_fixtures.PhotoCraftRevisionTests(); self.f.setUp(); self.addCleanup(self.f.doCleanups)

    def test_existing_unverified_return_stays_blocked_from_qa(self):
        path = atomic_write_json(self.f.returned(), self.f.root / 'return.json')
        result = consume_photocraft_return(path, self.f.handoff)
        self.assertFalse(result['can_enter_existing_qa']); self.assertFalse(result['editor_execution_performed'])

    def test_wrong_handoff_rejected_even_with_same_scene(self):
        path = atomic_write_json(self.f.returned(), self.f.root / 'return.json')
        other = self.f.root / 'other-handoff.json'; other.write_bytes(self.f.handoff.read_bytes())
        with self.assertRaisesRegex(ContractError, 'different handoff'): consume_photocraft_return(path, other)


if __name__ == '__main__':
    unittest.main()
