from __future__ import annotations

import contextlib
import io
import json
import sys
import shutil
import unittest
import wave
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from video_creation_pipeline.contract_io import file_sha256
from video_creation_pipeline.errors import ContractError
from video_creation_pipeline.music_cli import main
from video_creation_pipeline.music_diversity import build_music_arrangement_report
from video_creation_pipeline.music_pipeline import _select_music_v2, build_music_mix_command


class ArrangementTests(unittest.TestCase):
    assets = {"a": {"sha256": "a" * 64, "duration_s": 180},
              "alias": {"sha256": "a" * 64, "duration_s": 180},
              "b": {"sha256": "b" * 64, "duration_s": 180},
              "c": {"sha256": "c" * 64, "duration_s": 180}}

    def layer(self, asset="a", at=0, duration=20, start=0, end=180, loop=False):
        return dict(asset_id=asset, timeline_start_s=at, duration_s=duration,
                    source_start_s=start, source_end_s=end, loop=loop)

    def audit(self, *layers):
        return build_music_arrangement_report(list(layers), self.assets)

    @contextlib.contextmanager
    def workspace_temporary_directory(self):
        # Python 3.13 mkdtemp's private Windows ACL is incompatible with some
        # desktop sandbox tokens; use an inherited ACL inside our test workspace.
        runtime = (ROOT / ".test-runtime").resolve()
        root = (runtime / ("arrangement-" + uuid4().hex)).resolve()
        self.assertEqual(root.parent, runtime)
        root.mkdir(parents=True)
        try:
            yield root
        finally:
            shutil.rmtree(root)

    def test_explicit_short_loop_is_blocked_across_entire_program(self):
        r = self.audit(self.layer(duration=126.1333, end=21.406479, loop=True))
        self.assertEqual(r["status"], "blocked_repetitive_arrangement")
        self.assertEqual({v["code"] for v in r["violations"]},
                         {"source_passage_repeated_too_often", "dominant_short_source"})

    def test_six_split_clips_and_aliases_cannot_hide_repetition(self):
        r = self.audit(*[self.layer("a" if i % 2 else "alias", at=i * 21.3333,
                                    duration=21.4 if i < 5 else 19.4667) for i in range(6)])
        self.assertEqual(r["sources"][0]["max_source_passes"], 6)
        self.assertFalse(r["readiness"]["candidate_render_allowed"])

    def test_long_one_pass_and_long_bounded_repeat_are_allowed(self):
        for layer in (self.layer(duration=126), self.layer(duration=126, end=64, loop=True)):
            self.assertEqual(self.audit(layer)["status"], "passed")

    def test_current_three_long_arrangements_are_allowed(self):
        r = self.audit(self.layer(duration=54.9),
                       self.layer("b", at=1609/30, duration=45.4),
                       self.layer("c", at=2933/30, duration=851/30))
        self.assertEqual(r["status"], "passed")
        self.assertAlmostEqual(r["music_coverage_s"], 3784/30, places=5)

    def test_nonoverlapping_passages_of_same_long_file_are_allowed(self):
        self.assertEqual(self.audit(*[self.layer(at=i*21, start=i*21, duration=21)
                                      for i in range(6)])["status"], "passed")

    def test_short_video_one_pass_does_not_require_multiple_assets(self):
        self.assertEqual(self.audit(self.layer(duration=12, end=20))["status"], "passed")

    def test_short_video_cannot_repeat_one_second_thirty_times(self):
        self.assertNotEqual(self.audit(self.layer(duration=30, end=1, loop=True))["status"], "passed")

    def test_tiny_other_track_does_not_excuse_dominant_short_repeat(self):
        self.assertNotEqual(self.audit(self.layer(duration=42, end=21, loop=True),
                                       self.layer("b", at=42, duration=1))["status"], "passed")

    def test_two_real_arrangements_allowed_without_fixed_three_track_rule(self):
        self.assertEqual(self.audit(self.layer(duration=40, end=20, loop=True),
                                    self.layer("b", at=40, duration=40, end=20, loop=True))["status"], "passed")

    def test_tail_repetition_is_not_hidden_by_an_initial_comparison_horizon(self):
        self.assertNotEqual(self.audit(self.layer(duration=90),
                                       *[self.layer("b", at=90+i*10, duration=10) for i in range(4)])["status"], "passed")

    def test_empty_music_is_allowed_but_missing_or_invalid_evidence_is_not(self):
        self.assertEqual(self.audit()["status"], "passed")
        for change in ({"duration_s": float("nan")}, {"duration_s": float("inf")},
                       {"loop": "false"}, {"asset_id": "missing"},
                       {"source_end_s": 1}, {"source_start_s": -1}):
            with self.subTest(change=change), self.assertRaises(ContractError):
                self.audit({**self.layer(), **change})

    def test_same_sha_cannot_claim_conflicting_lengths(self):
        assets = {**self.assets, "alias": {"sha256": "a"*64, "duration_s": 300}}
        with self.assertRaises(ContractError):
            build_music_arrangement_report([self.layer(), self.layer("alias", at=20)], assets)

    def test_direct_mix_command_cannot_bypass_repeat_gate(self):
        with self.assertRaisesRegex(ContractError, "repetitive BGM blocked"):
            build_music_mix_command("in.mp4", "music.wav", "out.mp4", {
                "timeline_interval": {"start_s": 0, "end_s": 126},
                "source_interval": {"start_s": 0, "end_s": 21},
                "source_audio": {"sha256": "a"*64}, "loop": True,
            })

    def test_cli_verifies_actual_source_and_returns_nonzero_for_block(self):
        with self.workspace_temporary_directory() as folder:
            root = Path(folder)
            audio = root / "short.wav"
            with wave.open(str(audio), "wb") as handle:
                handle.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
                handle.writeframes(b"\0\0" * (21 * 8000))
            spec = {"layers": [self.layer(duration=126, end=21, loop=True)],
                    "asset_sources": {"a": {"path": str(audio), "sha256": file_sha256(audio)}}}
            source, output = root / "spec.json", root / "report.json"
            args = ["audit-arrangement", "--spec", str(source), "--output", str(output)]
            source.write_text(json.dumps(spec), encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(args), 3)
            self.assertEqual(json.loads(output.read_text())["status"], "blocked_repetitive_arrangement")
            spec["layers"] = [self.layer(duration=12, end=21)]
            source.write_text(json.dumps(spec), encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(args), 0)
            with patch("video_creation_pipeline.music_cli.wave.open", side_effect=wave.Error("FLOAT WAV")), \
                 patch("video_creation_pipeline.music_cli.probe_media_metadata", return_value={"format": {"duration": "21"}}) as probe, \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(args), 0)
                probe.assert_called_once_with(audio.resolve())
            spec["asset_sources"]["a"]["duration_s"] = 200
            source.write_text(json.dumps(spec), encoding="utf-8")
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(args), 2)
            del spec["asset_sources"]["a"]["duration_s"]
            source.write_text(json.dumps(spec), encoding="utf-8")
            audio.write_bytes(b"modified source")
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(args), 2)


class ArrangementSelectionTests(unittest.TestCase):
    def row(self, asset, length, score=0.9, loopable=True):
        return {"asset_id": asset, "window_id": asset, "score": score,
                "source_audio": {"sha256": asset*64}, "score_components": {},
                "source_interval": {"start_s": 0, "end_s": length, "loopable": loopable}}

    def select(self, segments, families=None):
        intent = {"segments": [{"segment_id": s["segment_id"],
                    "start_s": s["timeline_interval"]["start_s"],
                    "end_s": s["timeline_interval"]["end_s"],
                    "text_query": "清晰解释", "narration_density": 0.8} for s in segments]}
        families = families or {(r["asset_id"], r["window_id"]): "clear_explainer"
                                 for s in segments for r in s["candidates"]}
        assets = {r["asset_id"]: {"sha256": r["asset_id"]*64, "duration_s": 180}
                  for s in segments for r in s["candidates"]}
        with patch("video_creation_pipeline.music_pipeline._candidate_family_lookup", return_value=families), \
             patch("video_creation_pipeline.music_pipeline._candidate_asset_sources", return_value=assets):
            return _select_music_v2({"segments": segments}, intent, min_score=.72, min_margin=.05,
                                    word_timestamps=[], subtitle_cues=[])

    def segment(self, rows, start=0, end=126, name="one"):
        return {"segment_id": name, "candidates": rows,
                "timeline_interval": {"start_s": start, "end_s": end}}

    def test_selects_long_same_family_candidate_over_higher_short_loop_score(self):
        rows, ready = self.select([self.segment([self.row("a", 21, .95), self.row("b", 180, .85)])])
        self.assertEqual(rows[0]["asset_id"], "b")
        self.assertTrue(ready["render_eligible"])

    def test_restarts_across_semantic_segments_choose_another_arrangement(self):
        rows, ready = self.select([
            self.segment([self.row("a", 20)], end=20),
            self.segment([self.row("a", 20, .95), self.row("b", 20, .85)], start=20, end=40, name="two")])
        self.assertEqual([r["asset_id"] for r in rows], ["a", "b"])
        self.assertTrue(ready["render_eligible"])

    def test_short_only_or_wrong_family_long_candidate_is_blocked(self):
        rows, ready = self.select([self.segment([self.row("a", 21), self.row("b", 180, .85)])],
                                 {("a", "a"): "clear_explainer", ("b", "b"): "warm_story"})
        self.assertFalse(ready["render_eligible"])
        self.assertIn("nonrepetitive", rows[0]["reason"])

    def test_score_and_margin_are_not_relaxed_for_long_assets(self):
        for candidates in ([self.row("a", 180, .5)],
                           [self.row("a", 180, .9), self.row("b", 180, .89)]):
            _, ready = self.select([self.segment(candidates)])
            self.assertFalse(ready["render_eligible"])

    def test_nonloopable_source_cannot_be_extended(self):
        _, ready = self.select([self.segment([self.row("a", 64, loopable=False)])])
        self.assertFalse(ready["render_eligible"])


if __name__ == "__main__":
    unittest.main()
