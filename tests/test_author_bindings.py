from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'tests'))
from author_binding_fixture import make_case, approval_args, refresh_fixture_approvals
from video_creation_pipeline.author_bindings import (
    build_author_binding_projection, validate_author_binding_projection, build_author_bound_motion_bundle,
)
from video_creation_pipeline.contract_io import artifact_reference, atomic_write_json, payload_sha256, read_json_object
from video_creation_pipeline.errors import ContractError
from video_creation_pipeline.semantic_motion import materialize_hyperframes_project, validate_semantic_motion_plan
from video_creation_pipeline.semantic_motion_cli import main as motion_cli
from video_creation_pipeline.motion_sfx import derive_motion_sfx_cue_sheet, build_motion_sfx_source_manifest


class AuthorBindingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='author-binding-')
        self.addCleanup(self.temp.cleanup)
        self.root = make_case(Path(self.temp.name) / 'case')
        self.spec_path = self.root / 'binding-spec.json'

    def projection(self):
        return build_author_binding_projection(self.spec_path, **approval_args(self.root))

    def change_spec(self, change):
        spec = read_json_object(self.spec_path, 'test spec')
        change(spec)
        atomic_write_json(spec, self.spec_path)
        refresh_fixture_approvals(self.root)

    def change_alignment(self, change):
        path = self.root / 'alignment.json'
        alignment = read_json_object(path, 'test alignment')
        change(alignment)
        atomic_write_json(alignment, path)
        self.change_spec(lambda spec: spec.update(source_word_alignment=artifact_reference(path, 'alignment')))

    def test_word_projection_enters_native_plan_and_materializer(self):
        projection = self.projection()
        self.assertEqual(projection['positions'][0]['program_time_s'], 46.1)
        self.assertEqual(projection['positions'][0]['segment_local_time_s'], 1.1)
        self.assertTrue(projection['synthetic_fixture_only'])
        paths = build_author_bound_motion_bundle(self.spec_path, self.root / 'result', **approval_args(self.root))
        plan = read_json_object(paths['motion_plan'], 'motion')
        self.assertEqual(plan['cues'][0]['timing']['timeline_start_s'], 1.1)
        self.assertEqual(plan['output']['duration_s'], 8.0)
        project = materialize_hyperframes_project(paths['motion_plan'], self.root / 'project')
        first = project['html'].read_bytes()
        second = build_author_bound_motion_bundle(self.spec_path, self.root / 'result', **approval_args(self.root))
        self.assertEqual(paths, second)
        materialize_hyperframes_project(paths['motion_plan'], self.root / 'project')
        self.assertEqual(first, project['html'].read_bytes())

    def test_temporal_window_enters_existing_motion_bundle(self):
        def edit(spec):
            binding = spec['cue_bindings'][0]
            cue = binding['cue']
            cue['timing'].pop('hold_s')
            spec['cue_bindings'] = [{'cue': cue, 'temporal_expression': {
                'kind': 'at_for',
                'anchor': {'word_id': 'word-001', 'boundary': 'start'},
                'duration': {'value': 150, 'unit': 'frames'},
            }}]
        self.change_spec(edit)
        result = self.projection()
        self.assertEqual(result['positions'][0]['window']['start_frame'], 30)
        paths = build_author_bound_motion_bundle(self.spec_path, self.root / 'temporal', **approval_args(self.root))
        plan = read_json_object(paths['motion_plan'], 'motion')
        self.assertEqual(plan['cues'][0]['timing']['timeline_start_s'], 1)
        validate_semantic_motion_plan(plan)

    def test_scene_move_preserves_cue_identity_and_local_clock(self):
        original = self.projection()
        path = self.root / 'program-packaging.json'
        packaging = read_json_object(path, 'packaging')
        packaging['scenes'][0].update(start_s=60.0, end_s=68.0)
        atomic_write_json(packaging, path)
        self.change_spec(lambda spec: spec.update(source_packaging_plan=artifact_reference(path, 'packaging')))
        moved = self.projection()
        self.assertEqual(original['cues'], moved['cues'])
        self.assertEqual(moved['positions'][0]['program_time_s'], 61.1)
        self.assertEqual(moved['positions'][0]['segment_local_time_s'], 1.1)
        self.assertNotEqual(original['projection_sha256'], moved['projection_sha256'])

    def test_changed_recording_invalidates_existing_projection(self):
        projection = self.projection()
        with (self.root / 'synthetic-silence.wav').open('ab') as stream:
            stream.write(b'changed take')
        with self.assertRaises(ContractError):
            validate_author_binding_projection(projection)

    def test_changed_script_invalidates_existing_projection(self):
        projection = self.projection()
        with (self.root / 'inputs/content-video-brief.v1.json').open('ab') as stream:
            stream.write(b' ')
        with self.assertRaises(ContractError):
            validate_author_binding_projection(projection)

    def test_interpolated_word_rejected_even_after_new_fixture_approval(self):
        self.change_alignment(lambda data: data['words'][0].update(timing_origin='interpolated'))
        with self.assertRaisesRegex(ContractError, 'interpolated or unknown'):
            self.projection()

    def test_wrong_word_text_rejected(self):
        self.change_alignment(lambda data: data['words'][0].update(text='different'))
        with self.assertRaisesRegex(ContractError, 'exact range'):
            self.projection()

    def test_alignment_cannot_claim_a_different_mode(self):
        self.change_spec(lambda spec: spec.update(evidence_mode='measured_recording'))
        with self.assertRaises(ContractError):
            self.projection()

    def test_duplicate_word_ids_rejected(self):
        self.change_alignment(lambda data: data['words'].append(deepcopy(data['words'][0])))
        with self.assertRaisesRegex(ContractError, 'unique'):
            self.projection()

    def test_speed_customer_interview_and_unknown_word_rejected(self):
        original = read_json_object(self.spec_path, 'spec')
        for delta in ({'playback_rate': 2}, {'source_media_role': 'customer_interview'}):
            self.change_spec(lambda spec: spec.update(delta))
            with self.assertRaisesRegex(ContractError, 'original-speed creator'):
                self.projection()
            atomic_write_json(original, self.spec_path)
        self.change_spec(lambda spec: spec['cue_bindings'][0].update(word_id='missing'))
        with self.assertRaisesRegex(ContractError, 'existing word'):
            self.projection()

    def test_negative_local_time_rejected(self):
        self.change_spec(lambda spec: spec['cue_bindings'][0].update(offset_ms=-2000))
        with self.assertRaisesRegex(ContractError, 'exceeds'):
            self.projection()

    def test_nonfinite_timing_rejected(self):
        self.change_spec(lambda spec: spec['cue_bindings'][0].update(offset_ms=float('nan')))
        with self.assertRaisesRegex(ContractError, 'finite'):
            self.projection()

    def test_missing_or_wrong_approval_cannot_pass(self):
        args = approval_args(self.root)
        args['voice_approval_path'] = self.root / 'absent.json'
        with self.assertRaises(ContractError):
            build_author_binding_projection(self.spec_path, **args)
        args['voice_approval_path'] = args['script_approval_path']
        with self.assertRaisesRegex(ContractError, 'different artifact'):
            build_author_binding_projection(self.spec_path, **args)

    def test_voice_measurements_do_not_substitute_for_word_alignment(self):
        path = self.root / 'voice-approval.json'
        record = read_json_object(path, 'fixture voice approval')
        record['attachments'] = [row for row in record['attachments'] if row['role'] != 'word_alignment']
        for row in record['attachments']:
            if row['role'] == 'voice_measurements':
                row['artifact'] = artifact_reference(self.root / 'alignment.json', 'alignment')
        record['approval_record_sha256'] = payload_sha256(record, 'approval_record_sha256')
        atomic_write_json(record, path)
        with self.assertRaisesRegex(ContractError, 'separately bind'):
            self.projection()

    def test_rehashed_motion_timing_drift_rejected(self):
        paths = build_author_bound_motion_bundle(self.spec_path, self.root / 'result', **approval_args(self.root))
        plan = read_json_object(paths['motion_plan'], 'motion')
        plan['cues'][0]['timing']['timeline_start_s'] = 0.1
        plan['plan_sha256'] = payload_sha256(plan, 'plan_sha256')
        with self.assertRaisesRegex(ContractError, 'shared projection'):
            validate_semantic_motion_plan(plan)

    def test_raw_alignment_drift_blocked_at_native_consumption(self):
        paths = build_author_bound_motion_bundle(self.spec_path, self.root / 'result', **approval_args(self.root))
        atomic_write_json({'changed': True}, self.root / 'raw-alignment.json')
        with self.assertRaises(ContractError):
            materialize_hyperframes_project(paths['motion_plan'], self.root / 'project')
        with self.assertRaises(ContractError):
            derive_motion_sfx_cue_sheet(paths['sfx_manifest'])

    def test_sfx_uses_the_same_projected_motion_clock(self):
        paths = build_author_bound_motion_bundle(self.spec_path, self.root / 'result', **approval_args(self.root))
        sfx = read_json_object(paths['sfx_cue_sheet'], 'SFX cue sheet')
        motion = read_json_object(paths['motion_plan'], 'motion')
        cue = motion['cues'][0]
        matched = [row for row in sfx['cues'] if row['cue_id'] == 'motion-' + cue['cue_id']]
        self.assertEqual(len(matched), 1)
        timing = cue['timing']
        self.assertAlmostEqual(matched[0]['timeline_start_s'],
            timing['timeline_start_s'] + timing['full_frame_s'] + 0.82 * timing['enter_s'], places=3)
        self.assertLess(matched[0]['timeline_start_s'], 8)

    def test_sfx_rejects_competing_word_clock(self):
        paths = build_author_bound_motion_bundle(self.spec_path, self.root / 'result', **approval_args(self.root))
        with self.assertRaisesRegex(ContractError, 'same isolated motion projection'):
            build_motion_sfx_source_manifest(paths['packaging'], semantic_motion_plan_path=paths['motion_plan'],
                word_timestamps_path=self.root / 'alignment.json')

    def test_new_approval_cannot_make_an_overlapping_program_valid(self):
        path = self.root / 'program-packaging.json'
        packaging = read_json_object(path, 'packaging')
        packaging['scenes'].append({'scene_id': 'overlap', 'start_s': 50, 'end_s': 55})
        atomic_write_json(packaging, path)
        self.change_spec(lambda spec: spec.update(source_packaging_plan=artifact_reference(path, 'packaging')))
        with self.assertRaisesRegex(ContractError, 'overlap'):
            self.projection()

    def test_outputs_never_overwrite_different_existing_content(self):
        (self.root / 'result').mkdir()
        atomic_write_json({'unrelated': True}, self.root / 'result/author-binding-projection.json')
        with self.assertRaisesRegex(ContractError, 'different content'):
            build_author_bound_motion_bundle(self.spec_path, self.root / 'result', **approval_args(self.root))

    def test_cli_builds_author_bound_bundle(self):
        self.assertEqual(motion_cli(['build-author-bound', '--spec', str(self.spec_path),
            '--script-approval', str(self.root / 'script-approval.json'),
            '--voice-approval', str(self.root / 'voice-approval.json'),
            '--storyboard-approval', str(self.root / 'storyboard-approval.json'),
            '--output-dir', str(self.root / 'cli-result')]), 0)


if __name__ == '__main__':
    unittest.main()
