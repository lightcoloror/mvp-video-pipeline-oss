from __future__ import annotations

import shutil
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from video_creation_pipeline.execution_receipts import (
    execute_ffmpeg_with_receipt,
    validate_ffmpeg_execution_receipt,
)
from video_creation_pipeline.music_pipeline import build_music_mix_command


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is not installed")
class MusicMixExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = ROOT / ".test-runtime" / self._testMethodName
        if self.root.exists():
            shutil.rmtree(self.root)
        self.root.mkdir(parents=True)
        self.fixture_spec = self.root / "synthetic-fixture.txt"
        self.fixture_spec.write_text("synthetic lavfi fixture", encoding="utf-8")

    def tearDown(self) -> None:
        if self.root.exists():
            shutil.rmtree(self.root)

    def test_gain_envelope_and_gentle_ducking_execute_through_single_outlet(
        self,
    ) -> None:
        program = self.root / "program.mp4"
        music = self.root / "music.wav"
        output = self.root / "mixed.mp4"
        self._fixture(
            [
                "ffmpeg",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "color=c=black:s=320x240:r=30:d=2",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:sample_rate=48000:duration=2",
                "-shortest",
                "-c:v",
                "mpeg4",
                "-c:a",
                "aac",
                str(program),
            ],
            program,
            "program",
        )
        self._fixture(
            [
                "ffmpeg",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=220:sample_rate=48000:duration=2",
                "-c:a",
                "pcm_s16le",
                str(music),
            ],
            music,
            "music",
        )
        selection = {
            "timeline_interval": {"start_s": 0.0, "end_s": 2.0},
            "source_interval": {"start_s": 0.0, "end_s": 2.0},
            "loop": False,
            "gain_db": -3.0,
            "gain_envelope": {
                "base_music_gain_db": -3.0,
                "speech_priority_music_gain_db": -6.0,
                "ramp_s": 0.1,
                "source_mode": "word_timestamps",
                "coverage_ratio": 0.25,
                "windows": [{"start_s": 0.75, "end_s": 1.25, "music_gain_db": -6.0}],
                "semantic_timeline_moved": False,
            },
            "fade_in_s": 0.25,
            "fade_out_s": 0.25,
            "ducking": {
                "enabled": True,
                "threshold": 0.08,
                "ratio": 3.0,
                "attack_ms": 30,
                "release_ms": 400,
            },
        }
        command = build_music_mix_command(program, music, output, selection)
        receipt = execute_ffmpeg_with_receipt(
            "music_mix",
            command,
            [
                {"role": "program", "path": program},
                {"role": "music", "path": music},
            ],
            [{"role": "candidate", "path": output}],
            self.root / "mix-receipt.json",
        )
        self.assertTrue(output.is_file())
        self.assertGreater(output.stat().st_size, 0)
        self.assertEqual(
            receipt["outlet_id"], "video_creation_pipeline.single_ffmpeg_outlet"
        )
        validate_ffmpeg_execution_receipt(receipt)

    def _fixture(self, command: list[str], output: Path, role: str) -> None:
        execute_ffmpeg_with_receipt(
            "transcode",
            command,
            [{"role": "synthetic_spec", "path": self.fixture_spec}],
            [{"role": role, "path": output}],
            self.root / f"{role}-fixture-receipt.json",
        )


if __name__ == "__main__":
    unittest.main()
