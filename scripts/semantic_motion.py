from __future__ import annotations

# ruff: noqa: E402

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from video_creation_pipeline.semantic_motion_cli import main


if __name__ == "__main__":
    raise SystemExit(main())
