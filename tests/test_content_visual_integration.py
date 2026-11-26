from copy import deepcopy
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from video_creation_pipeline.content_video_pipeline import build_content_video_project_plan, validate_content_video_inputs
from video_creation_pipeline.contract_io import atomic_write_json, payload_sha256
from video_creation_pipeline.creative_cli import main as creative_main
from video_creation_pipeline.errors import ContractError
from video_creation_pipeline.production_strategy import (
    build_content_visual_production_plan, build_visual_production_plan,
    validate_visual_production_plan,
)
from video_creation_pipeline.semantic_motion import build_semantic_motion_plan, materialize_hyperframes_project


class ContentVisualIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='content-visual-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        fixtures = ROOT / 'tests/fixtures'
        shutil.copytree(fixtures / 'content-video-brief-v1/upstream-canonical', self.root / 'inputs')
        shutil.copytree(fixtures / 'semantic-motion', self.root / 'motion')
        inputs = self.root / 'inputs'
        validated = validate_content_video_inputs(
            inputs / 'content-video-brief.v1.json', inputs / 'content-video-brief.v1.schema.json',
            inputs / 'research-output.v1.json', inputs / 'research-output.v1.schema.json',
            max_age_hours=168,
        )
        self.content = build_content_video_project_plan(validated, width=1080, height=1920)
        self.content_path = atomic_write_json(self.content, self.root / 'content.json')
        self.scene = self.content['shot_units'][0]['scene_id']

    def visual(self):
        return build_content_visual_production_plan(self.content_path, self.scene)

    def motion_inputs(self):
        visual_path = atomic_write_json(self.visual(), self.root / 'visual.json')
        packaging = atomic_write_json(self.content['packaging_plan'], self.root / 'packaging.json')
        anchors = json.loads((self.root / 'motion/script-anchor.json').read_text(encoding='utf-8'))
        anchors['anchors'][0]['story_segment_ref'] = self.scene
        anchors['anchors'][0]['quote'] = self.content['shot_units'][0]['narration']
        anchor_path = atomic_write_json(anchors, self.root / 'anchor.json')
        spec = json.loads((self.root / 'motion/build-spec.json').read_text(encoding='utf-8'))
        spec['cues'][0]['story_segment_ref'] = self.scene
        return packaging, anchor_path, self.root / 'motion/source-evidence.svg', visual_path, spec

    def test_content_source_enters_native_materializer(self):
        args = self.motion_inputs()
        motion = build_semantic_motion_plan(*args[:4], **args[4])
        plan_path = atomic_write_json(motion, self.root / 'motion-plan.json')
        project = materialize_hyperframes_project(plan_path, self.root / 'project')
        first = project['html'].read_bytes()
        materialize_hyperframes_project(plan_path, self.root / 'project')
        self.assertEqual(first, project['html'].read_bytes())
        self.assertEqual(self.visual()['source_material_status'], 'missing_material')
        self.assertNotIn('source_unit', self.visual())

    def test_old_unit_validator_stays_strict(self):
        unit = atomic_write_json(self.content['shot_units'][0], self.root / 'unit.json')
        with self.assertRaisesRegex(ContractError, 'unsupported shot generation'):
            build_visual_production_plan(unit)

    def test_changed_source_files_invalidate_visual_plan(self):
        visual = self.visual()
        for filename in ('content-video-brief.v1.json', 'research-output.v1.json'):
            path = self.root / 'inputs' / filename
            original = path.read_bytes()
            path.write_bytes(original + b' ')
            with self.assertRaises(ContractError):
                validate_visual_production_plan(visual)
            path.write_bytes(original)

    def test_rehashed_forged_scene_rejected(self):
        changed = deepcopy(self.content)
        changed['shot_units'][0]['narration'] = 'forged narration'
        changed['shot_units'][0]['unit_sha256'] = payload_sha256(changed['shot_units'][0], 'unit_sha256')
        changed['plan_sha256'] = payload_sha256(changed, 'plan_sha256')
        atomic_write_json(changed, self.content_path)
        with self.assertRaisesRegex(ContractError, 'canonical brief projection'):
            self.visual()

    def test_unknown_scene_and_source_kind_rejected(self):
        with self.assertRaises(ContractError):
            build_content_visual_production_plan(self.content_path, 'unknown')
        visual = self.visual()
        visual['source_kind'] = 'vkp_edit_job'
        visual['plan_sha256'] = payload_sha256(visual, 'plan_sha256')
        with self.assertRaisesRegex(ContractError, 'source kind'):
            validate_visual_production_plan(visual)

    def test_material_status_cannot_be_promoted(self):
        visual = self.visual()
        visual['source_material_status'] = 'ready'
        visual['plan_sha256'] = payload_sha256(visual, 'plan_sha256')
        with self.assertRaises(ContractError):
            validate_visual_production_plan(visual)

    def test_different_motion_scene_rejected(self):
        args = self.motion_inputs()
        spec = args[4]
        spec['cues'][0]['story_segment_ref'] = 'segment-04'
        original_anchor = self.root / 'motion/script-anchor.json'
        with self.assertRaisesRegex(ContractError, 'identity must match'):
            build_semantic_motion_plan(args[0], original_anchor, args[2], args[3], **spec)

    def test_cli_content_route(self):
        self.assertEqual(creative_main(['build-visual-strategy', '--content-plan', str(self.content_path),
                                      '--scene-id', self.scene, '--output', str(self.root / 'cli-visual.json')]), 0)
        self.assertEqual(creative_main(['build-visual-strategy', '--content-plan', str(self.content_path),
                                      '--output', str(self.root / 'invalid.json')]), 2)


if __name__ == '__main__':
    unittest.main()
