"""Locality, boundary and fail-closed regressions; not visual acceptance."""
import copy
import json
import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from video_creation_pipeline.performance_recipe import (
    pose_at, continuity_report, validate_encoded, _execute, _number)
from video_creation_pipeline.errors import ContractError


def row(phase, start):
    return {'phase': phase, 'cue': {'start_s':start+.5, 'end_s':start+1.5},
            'object':{'id':'parcel-01'}}


class PerformanceRecipeTests(unittest.TestCase):
    def test_seek_is_order_independent_and_identity_persistent(self):
        r = row('reaction',6)
        before = pose_at(r,7)
        for t in (8.9,6,7.8,6.5):
            pose_at(r,t)
        self.assertEqual(before, pose_at(r,7))
        self.assertEqual(before['object_id'],'parcel-01')

    def test_shared_boundaries_hold_exactly(self):
        phases = [row(p,i*3) for i,p in enumerate(('attention','understanding','reaction'))]
        rows = [{'left':pose_at(a,(i+1)*3),'right':pose_at(b,(i+1)*3),
                 'applicable':True,'boundary_s':(i+1)*3}
                for i,(a,b) in enumerate(zip(phases,phases[1:]))]
        result = continuity_report(rows)
        self.assertEqual(result['status'],'passed')
        self.assertEqual(result['considered'],2)

    def test_delayed_action_keeps_start_and_final_pose(self):
        r = row('reaction',6); changed=copy.deepcopy(r)
        changed['cue'] = {'start_s':7,'end_s':8}
        self.assertEqual(pose_at(r,6),pose_at(changed,6))
        self.assertEqual(pose_at(r,9),pose_at(changed,9))
        self.assertNotEqual(pose_at(r,7),pose_at(changed,7))

    def test_missing_failed_or_inapplicable_captures_never_pass(self):
        self.assertEqual(continuity_report([])['status'],'invalid')
        for bad in ({'left':{},'right':{}},{'failed':True,'left':{'a':1},'right':{'a':1}}):
            self.assertEqual(continuity_report([bad])['status'],'invalid')
        self.assertEqual(continuity_report([{'left':{'a':1},'right':{'a':1},'applicable':False}])['status'],'not_evaluated')

    def test_identity_change_and_jump_rejected(self):
        p=pose_at(row('attention',0),3)
        for field,value in [('object_id','other'),('parcel_x',900)]:
            q={**p,field:value}
            self.assertEqual(continuity_report([{'left':p,'right':q,'applicable':True,'boundary_s':3}])['status'],'failed')

    def test_numeric_expressions_and_nan_rejected(self):
        for x in ('1+2', float('nan'), float('inf'), True):
            with self.assertRaises(ContractError): _number(x,'parameter')

    def test_nonzero_renderer_exit_propagates(self):
        with tempfile.TemporaryDirectory() as tmp, patch('video_creation_pipeline.performance_recipe.execute_ffmpeg_with_receipt',side_effect=ContractError('exit code 9')):
            with self.assertRaisesRegex(ContractError,'exit code 9'):
                _execute('render',[],[],[],Path(tmp)/'receipt.json')
            self.assertFalse((Path(tmp)/'receipt.json').exists())

    def test_incomplete_decode_rejected_even_with_valid_duration(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp)
            (folder/'decoded.framemd5').write_text('# header\n0, 0, 0, 1, 99, hash\n')
            meta={'streams':[{'codec_type':'video','width':720,'height':720,'duration':'3'}],'format':{'duration':'3'}}
            with patch('video_creation_pipeline.performance_recipe.probe_media_metadata',return_value=meta), patch('video_creation_pipeline.performance_recipe._execute'):
                with self.assertRaisesRegex(ContractError,'incomplete visual decode'):
                    validate_encoded(folder/'partial.mp4',72,{'width':720,'height':720,'fps':24},folder)


if __name__ == '__main__': unittest.main()
