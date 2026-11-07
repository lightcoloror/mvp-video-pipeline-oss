from __future__ import annotations

# ruff: noqa: E402

import shutil
import sys
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from video_creation_pipeline.contract_io import atomic_write_json, read_json_object
from video_creation_pipeline.execution_receipts import execute_ffmpeg_with_receipt
from video_creation_pipeline.reframe_detectors import (
    build_smart_reframe_evidence,
    write_smart_reframe_evidence,
)
from video_creation_pipeline.smart_reframe import (
    build_smart_reframe_request,
    write_smart_reframe_contract,
)

FIXTURES = ROOT / "tests" / "fixtures" / "smart-reframe"


def make_root(label: str) -> Path:
    root = ROOT / ".test-runtime" / f"smart-reframe-{label}-{uuid4().hex}"
    root.mkdir(parents=True)
    return root


def cleanup(root: Path) -> None:
    shutil.rmtree(root, ignore_errors=True)


def make_source(
    root: Path, *, duration_s: float = 3.0, width: int = 960, height: int = 540
) -> Path:
    source = root / "synthetic-landscape.mp4"
    receipt = root / "synthetic-source-ffmpeg-receipt.json"
    spec = root / "synthetic-source-spec.json"
    atomic_write_json(
        {
            "schema": "video_creation_pipeline.synthetic_reframe_source_spec.v1",
            "kind": "lavfi_testsrc2_with_sine",
            "width": width,
            "height": height,
            "fps": 30,
            "duration_s": duration_s,
        },
        spec,
    )
    command = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-f",
        "lavfi",
        "-i",
        f"testsrc2=size={width}x{height}:rate=30:duration={duration_s}",
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency=440:sample_rate=48000:duration={duration_s}",
        "-shortest",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-crf",
        "20",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        str(source),
    ]
    execute_ffmpeg_with_receipt(
        "render",
        command,
        [{"role": "synthetic_reframe_source_spec", "path": spec}],
        [{"role": "synthetic_reframe_source", "path": source}],
        receipt,
        timeout_s=90,
    )
    return source


def make_image(root: Path) -> Path:
    source = root / "synthetic-landscape.png"
    receipt = root / "synthetic-image-ffmpeg-receipt.json"
    spec = root / "synthetic-image-spec.json"
    atomic_write_json(
        {
            "schema": "video_creation_pipeline.synthetic_reframe_image_spec.v1",
            "kind": "lavfi_testsrc2_still",
            "width": 960,
            "height": 540,
        },
        spec,
    )
    command = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=960x540:rate=1:duration=1",
        "-frames:v",
        "1",
        str(source),
    ]
    execute_ffmpeg_with_receipt(
        "render",
        command,
        [{"role": "synthetic_reframe_image_spec", "path": spec}],
        [{"role": "synthetic_reframe_image", "path": source}],
        receipt,
        timeout_s=90,
    )
    return source


def make_request(root: Path, *, media_role: str = "talking_head") -> Path:
    path = root / "request.json"
    request = build_smart_reframe_request(
        profile_id="preview_vertical_9_16",
        media_role=media_role,
    )
    write_smart_reframe_contract(request, path)
    return path


def make_evidence(root: Path, source: Path, fixture_name: str) -> Path:
    spec = read_json_object(FIXTURES / fixture_name, "smart reframe fixture")
    payload = build_smart_reframe_evidence(
        source,
        provider=spec["provider"],
        observations=spec["observations"],
    )
    path = root / f"{Path(fixture_name).stem}-evidence.json"
    write_smart_reframe_evidence(payload, path)
    return path


def write_evidence_spec(
    root: Path, source: Path, observations: list[dict[str, object]], name: str
) -> Path:
    payload = build_smart_reframe_evidence(
        source,
        provider={"name": name, "version": "1.0.0"},
        observations=observations,
    )
    path = root / f"{name}.json"
    write_smart_reframe_evidence(payload, path)
    return path
