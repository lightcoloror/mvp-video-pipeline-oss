"""Boundary and local invalidation regression; actual FFmpeg acceptance is separate."""
import copy
import json
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from video_creation_pipeline.audio_timeline import prepare
from video_creation_pipeline.contract_io import artifact_reference
from video_creation_pipeline.errors import ContractError


class AudioTimelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.audio = self.root/'audio.wav'
        self.write_audio(b'\x01\x00'*48000)
        self.visual = self.root/'visual.mp4'; self.visual.write_bytes(b'bounded test visual')
        self.provenance = self.root/'provenance.json'; self.provenance.write_text('{}')
        self.packaging = {'schema': 'video_creation_pipeline.video_packaging_plan.v1', 'status': 'candidate_only', 'output': {'width': 540, 'height': 960, 'fps': 30}, 'scenes': [{'scene_id': f's{i}', 'start_s': i, 'end_s': i+1} for i in range(2)]}
        self.pp, self.bp = self.root/'packaging.json', self.root/'binding.json'
        self.pp.write_text(json.dumps(self.packaging))
        self.binding = {'schema': 'video_creation_pipeline.audio_timeline_binding.v1', 'packaging': artifact_reference(self.pp, 'packaging'), 'audio': artifact_reference(self.audio, 'audio'), 'segments': [{'scene_id': f's{i}', 'visual': artifact_reference(self.visual, 'visual'), 'provenance': artifact_reference(self.provenance, 'visual provenance'), 'semantic_description': 'Two-stage diagram', 'visual_start_s': i, 'subtitles': [{'start_s': i, 'end_s': i+1, 'text': 'Synthetic label'}]} for i in range(2)]}
        self.probe = patch('video_creation_pipeline.audio_timeline.probe_media_metadata', return_value={'streams': [{'codec_type': 'video', 'duration': '2'}], 'format': {'duration': '2'}})
        self.probe.start(); self.addCleanup(self.probe.stop)

    def write_audio(self, pcm):
        with wave.open(str(self.audio), 'wb') as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000); w.writeframes(pcm)

    def run_prepare(self):
        self.pp.write_text(json.dumps(self.packaging))
        self.binding['packaging'] = artifact_reference(self.pp, 'packaging')
        self.bp.write_text(json.dumps(self.binding))
        return prepare(self.pp, self.bp)[1]

    def test_pcm_edit_invalidates_only_affected_segment(self):
        before = self.run_prepare()
        self.write_audio(b'\x01\x00'*24000+b'\x02\x00'*24000)
        self.binding['audio'] = artifact_reference(self.audio, 'audio')
        after = self.run_prepare()
        self.assertEqual(before[0][0]['cache_key'], after[0][0]['cache_key'])
        self.assertNotEqual(before[1][0]['cache_key'], after[1][0]['cache_key'])

    def test_subtitle_edit_invalidates_only_affected_segment(self):
        before = self.run_prepare()
        self.binding['segments'][1]['subtitles'][0]['text'] = 'Changed label'
        after = self.run_prepare()
        self.assertEqual(before[0][0]['cache_key'], after[0][0]['cache_key'])
        self.assertNotEqual(before[1][0]['cache_key'], after[1][0]['cache_key'])

    def test_uncovered_audio_tail_rejected(self):
        self.packaging['scenes'].pop(); self.binding['segments'].pop()
        with self.assertRaisesRegex(ContractError, 'tail'):
            self.run_prepare()

    def test_scene_gap_rejected(self):
        self.packaging['scenes'][1]['start_s'] = 1.1
        with self.assertRaisesRegex(ContractError, 'gap'):
            self.run_prepare()

    def test_subtitle_cross_scene_rejected(self):
        self.binding['segments'][0]['subtitles'][0]['end_s'] = 1.1
        with self.assertRaisesRegex(ContractError, 'subtitle outside'):
            self.run_prepare()

    def test_hash_drift_rejected(self):
        self.visual.write_bytes(b'different')
        with self.assertRaises(ContractError):
            self.run_prepare()

    def test_nan_boundary_rejected(self):
        self.packaging['scenes'][0]['end_s'] = float('nan')
        with self.assertRaises((ContractError, ValueError)):
            self.run_prepare()

    def test_visual_overrun_rejected(self):
        self.binding['segments'][1]['visual_start_s'] = 2
        with self.assertRaisesRegex(ContractError, 'visual interval'):
            self.run_prepare()

    def test_semantic_wrap_is_in_cached_render_row_and_small_font_is_rejected(self):
        cue = self.binding['segments'][0]['subtitles'][0]
        cue.update(text='等待期是90天，不是交费后立即生效',protected_terms=['90天','不是'],
                   readability_review=dict(readability_checked=True,evidence='synthetic short cue'))
        row = self.run_prepare()[0][0]
        self.assertEqual(''.join(row['subtitles'][0]['lines']),cue['text'])
        self.assertEqual(len(row['subtitles'][0]['lines']),2)
        self.assertEqual(row['subtitle_font_px'],30)
        self.binding['segments'][0]['subtitle_font_px'] = 8
        with self.assertRaisesRegex(ContractError,'canonical'):
            self.run_prepare()


if __name__ == '__main__':
    unittest.main()
