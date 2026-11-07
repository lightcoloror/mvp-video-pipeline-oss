import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from video_creation_pipeline.semantic_editorial_cli import main

if __name__ == "__main__":
    raise SystemExit(main())
