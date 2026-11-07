"""Source-checkout wrapper for the existing video-spoken-rough-cut CLI."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from video_creation_pipeline.spoken_rough_cut_cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
