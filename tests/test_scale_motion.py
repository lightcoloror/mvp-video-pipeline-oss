"""Behavioral geometry checks; actual FFmpeg raster verification is separate."""
import math
import unittest

from video_creation_pipeline.errors import ContractError
from video_creation_pipeline.scale_motion import build_scale_motion, sample_pose


class ScaleMotionTests(unittest.TestCase):
    def make(self, targets, **extra):
        return build_scale_motion(dict(targets=targets, **extra), 540, 960, 10, 30)

    def target(self, a=2, b=5, rect=None, **extra):
        return dict(start_s=a, end_s=b, rect=rect or [420, 0, 96, 170.666667], **extra)

    def test_zero_velocity_endpoints_and_monotonic_intermediate_sizes(self):
        plan = self.make([self.target()])
        event = plan['events'][0]; a, b = event['start_s'], event['end_s']
        widths = [sample_pose(plan, a+(b-a)*i/60)[2] for i in range(61)]
        self.assertEqual(widths[0], 540)
        self.assertAlmostEqual(widths[-1], 96)
        self.assertTrue(all(x >= y for x, y in zip(widths, widths[1:])))
        self.assertLess(abs(sample_pose(plan, a+1e-5)[2]-540)/1e-5, .1)
        self.assertLess(abs(sample_pose(plan, b-1e-5)[2]-96)/1e-5, .1)
        self.assertEqual(sample_pose(plan, 9), [0, 0, 540, 960])

    def test_adjacent_same_pose_does_not_bounce_back(self):
        p = self.make([self.target(2, 4), self.target(4.4, 6)])
        self.assertEqual(len(p['events']), 2)
        self.assertEqual(sample_pose(p, 4.2)[2], 96)

    def test_adjacent_different_pose_retargets_without_fullscreen_flash(self):
        p = self.make([self.target(2, 4), self.target(4, 6, [90, 0, 360, 640])])
        events = p['events']
        self.assertEqual(len(events), 3)
        self.assertEqual(events[1]['from_rect'], events[0]['to_rect'])
        self.assertEqual(events[1]['start_s'], 4)
        self.assertTrue(96 < sample_pose(p, 4.3)[2] < 360)

    def test_cadence_changes_duration_and_is_labelled_as_proxy(self):
        rect = [90, 0, 360, 640]
        fast = self.make([self.target(rect=rect)], onsets_s=[i*.14 for i in range(60)])
        slow = self.make([self.target(rect=rect)], onsets_s=[i*.3 for i in range(30)])
        f, s = fast['events'][0], slow['events'][0]
        self.assertLess(f['end_s']-f['start_s'], s['end_s']-s['start_s'])
        self.assertEqual(f['rhythm']['kind'], 'local_onset_cadence_proxy')
        self.assertEqual(self.make([self.target()])['events'][0]['rhythm']['kind'], 'explicit_policy_fallback')

    def test_cut_requires_reason_and_unsupported_geometry_is_rejected(self):
        with self.assertRaises(ContractError):
            self.make([self.target(mode='cut')])
        p = self.make([self.target(mode='cut', reason='intentional comic punch-in')])
        self.assertEqual(sample_pose(p, 2)[2], 96)
        for rect in ([0, 0, 100, 100], [0, 0, math.nan, 200]):
            with self.assertRaises(ContractError):
                self.make([self.target(rect=rect)])

    def test_other_project_landscape_and_anticipation(self):
        p = build_scale_motion({'targets': [dict(start_s=2, end_s=5, rect=[320, 0, 320, 180], anticipate=True)]}, 640, 360, 8, 25)
        self.assertAlmostEqual(p['events'][0]['end_s'], 2)
        self.assertEqual(sample_pose(p, 3), [320, 0, 320, 180])
        self.assertEqual(p['fps'], 25)

    def test_short_targets_and_overlaps_fail_instead_of_silent_jump(self):
        for targets in ([self.target(2, 2.1)], [self.target(2, 5), self.target(4, 6)], [self.target(9, 9.9)]):
            with self.assertRaises(ContractError):
                self.make(targets)


if __name__ == '__main__':
    unittest.main()
