from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path
from typing import Callable


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--job", required=True)
    parser.add_argument("--source-root", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    job = json.loads(Path(args.job).read_text(encoding="utf-8-sig"))
    source_root = Path(args.source_root).expanduser().resolve()
    if str(source_root) not in sys.path:
        sys.path.insert(0, str(source_root))
    _install_deterministic_uuid(job["idempotency_key"])

    import pyJianYingDraft as draft  # type: ignore[import-not-found]
    from draft_profiles import get_draft_profile  # type: ignore[import-not-found]

    canvas = job["canvas"]
    script = draft.Script_file(canvas["width"], canvas["height"], fps=canvas["fps"])
    script.add_track(draft.Track_type.video)
    cursor = 0.0
    for row in job["segments"]:
        duration = float(row["duration_s"])
        source_duration = max(float(row["out_s"]), duration)
        material = draft.Video_material(
            path=row["source_path"],
            material_type="video",
            material_name=Path(row["source_path"]).name,
            duration=source_duration,
            width=0,
            height=0,
        )
        segment = draft.Video_segment(
            material,
            target_timerange=draft.trange(f"{cursor}s", f"{duration}s"),
            source_timerange=draft.trange(f"{row['in_s']}s", f"{duration}s"),
            speed=1.0,
        )
        script.add_segment(segment)
        cursor = round(cursor + duration, 6)
    profile = get_draft_profile(job["profile"])
    payload = json.loads(script.dumps(profile))
    Path(args.output).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return 0


def _install_deterministic_uuid(seed: str) -> None:
    counter = 0
    original: Callable[[], uuid.UUID] = uuid.uuid4

    def deterministic_uuid4() -> uuid.UUID:
        nonlocal counter
        counter += 1
        return uuid.uuid5(uuid.NAMESPACE_URL, f"{seed}:{counter}")

    if not seed:
        uuid.uuid4 = original
        raise ValueError("deterministic VectCut worker seed is required")
    uuid.uuid4 = deterministic_uuid4


if __name__ == "__main__":
    raise SystemExit(main())
