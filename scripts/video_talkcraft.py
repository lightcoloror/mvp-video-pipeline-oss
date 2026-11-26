from __future__ import annotations

import sys
from importlib import import_module
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

main = import_module("video_creation_pipeline.video_talkcraft_cli").main


if __name__ == "__main__":
    raise SystemExit(main())
