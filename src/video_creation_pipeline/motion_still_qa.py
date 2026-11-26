"""Clean-room motion timeline assertion & 1:1 crop / strip still QA utilities.

Provides two pre-render / post-render QA primitives for code-driven motion pipelines:
1. ``validate_timeline_continuity``: fail-closed deterministic check verifying integer
   frame bounds, contiguous segment stitching (``prev_end == next_start``), optional
   packaging total-frame parity, and optional integer-frame anchor/beat alignment.
2. ``build_still_qa_artifacts``: extracts 1:1 native-pixel region crops (for Chinese
   typography, safe-zone, and collision checks) and multi-frame horizontal/grid motion
   strips from either a frame directory or a rendered MP4 (via the single FFmpeg outlet).
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from PIL import Image, ImageDraw


@dataclass(frozen=True)
class CropBox:
    x: int
    y: int
    w: int
    h: int

    def label(self) -> str:
        return f"x{self.x}_y{self.y}_{self.w}x{self.h}"


def parse_crop_spec(spec: str, default_w: int = 600, default_h: int = 450) -> CropBox:
    """Parse ``x,y`` or ``x,y,w,h`` into a ``CropBox``."""
    parts = [int(p.strip()) for p in spec.split(",") if p.strip()]
    if len(parts) == 2:
        x, y = parts
        w, h = default_w, default_h
    elif len(parts) == 4:
        x, y, w, h = parts
    else:
        raise ValueError(f"invalid crop spec {spec!r}; expected 'x,y' or 'x,y,w,h'")
    if x < 0 or y < 0 or w <= 0 or h <= 0:
        raise ValueError(f"invalid non-positive crop dimensions in {spec!r}")
    return CropBox(x=x, y=y, w=w, h=h)


def parse_strip_spec(spec: str) -> list[int]:
    """Parse ``start:end:step`` (or ``start:end``) into a list of frame indices."""
    parts = [int(p.strip()) for p in spec.split(":")]
    if len(parts) == 2:
        start, end = parts
        step = 1
    elif len(parts) == 3:
        start, end, step = parts
    else:
        raise ValueError(f"invalid strip spec {spec!r}; expected 'start:end[:step]'")
    if start < 0 or end <= start or step <= 0:
        raise ValueError(f"invalid strip range {spec!r}")
    return list(range(start, end, step))


def validate_timeline_continuity(
    segments: list[dict[str, Any]],
    *,
    fps: int | float,
    expected_total_frames: int | None = None,
    require_zero_start: bool = True,
    beat_frames: int | None = None,
) -> dict[str, Any]:
    """Deterministically validate shot/motion timeline continuity before rendering.

    Each segment requires ``id``, integer ``start_frame`` (>= 0), and integer
    ``duration_frames`` (> 0). Optional ``anchor_frames`` (list of int) are verified
    to lie within ``[start_frame, start_frame + duration_frames)``.
    """
    errors: list[dict[str, Any]] = []
    if fps <= 0:
        errors.append({"code": "invalid_fps", "detail": f"fps must be > 0, got {fps}"})
    if not segments:
        errors.append({"code": "empty_timeline", "detail": "timeline has zero segments"})
        return {
            "schema": "motion_timeline_continuity_check.v1",
            "passed": False,
            "fps": fps,
            "segment_count": 0,
            "total_frames": 0,
            "errors": errors,
        }

    seen_ids: set[str] = set()
    prev_end: int | None = None
    total_frames = 0

    for idx, seg in enumerate(segments):
        seg_id = str(seg.get("id") or f"seg-{idx}")
        if seg_id in seen_ids:
            errors.append({"code": "duplicate_segment_id", "segment_id": seg_id})
        seen_ids.add(seg_id)

        start = seg.get("start_frame")
        duration = seg.get("duration_frames")
        if not isinstance(start, int) or isinstance(start, bool) or start < 0:
            errors.append(
                {
                    "code": "invalid_start_frame",
                    "segment_id": seg_id,
                    "start_frame": start,
                }
            )
            continue
        if not isinstance(duration, int) or isinstance(duration, bool) or duration <= 0:
            errors.append(
                {
                    "code": "invalid_duration_frames",
                    "segment_id": seg_id,
                    "duration_frames": duration,
                }
            )
            continue

        if idx == 0 and require_zero_start and start != 0:
            errors.append(
                {
                    "code": "nonzero_initial_frame",
                    "segment_id": seg_id,
                    "start_frame": start,
                }
            )

        if prev_end is not None:
            if start > prev_end:
                errors.append(
                    {
                        "code": "timeline_gap",
                        "segment_id": seg_id,
                        "expected_start_frame": prev_end,
                        "actual_start_frame": start,
                        "gap_frames": start - prev_end,
                    }
                )
            elif start < prev_end:
                errors.append(
                    {
                        "code": "timeline_overlap",
                        "segment_id": seg_id,
                        "expected_start_frame": prev_end,
                        "actual_start_frame": start,
                        "overlap_frames": prev_end - start,
                    }
                )

        if beat_frames is not None and beat_frames > 0:
            if start % beat_frames != 0 or duration % beat_frames != 0:
                errors.append(
                    {
                        "code": "beat_grid_misaligned",
                        "segment_id": seg_id,
                        "start_frame": start,
                        "duration_frames": duration,
                        "beat_frames": beat_frames,
                    }
                )

        end = start + duration
        for anchor in seg.get("anchor_frames") or []:
            if not isinstance(anchor, int) or isinstance(anchor, bool) or not (start <= anchor < end):
                errors.append(
                    {
                        "code": "anchor_out_of_segment_bounds",
                        "segment_id": seg_id,
                        "anchor_frame": anchor,
                        "segment_range": [start, end],
                    }
                )

        prev_end = end
        total_frames = max(total_frames, end)

    if expected_total_frames is not None and total_frames != expected_total_frames:
        errors.append(
            {
                "code": "total_frames_mismatch",
                "expected_total_frames": expected_total_frames,
                "actual_total_frames": total_frames,
            }
        )

    return {
        "schema": "motion_timeline_continuity_check.v1",
        "passed": len(errors) == 0,
        "fps": fps,
        "segment_count": len(segments),
        "total_frames": total_frames,
        "duration_s": round(total_frames / float(fps), 4) if fps > 0 else 0.0,
        "errors": errors,
    }


def _load_frame_from_dir(frames_dir: Path, frame_index: int) -> Image.Image:
    candidates = [
        frames_dir / f"f{frame_index:04d}.png",
        frames_dir / f"f{frame_index:04d}.jpg",
        frames_dir / f"frame_{frame_index:04d}.png",
        frames_dir / f"{frame_index:04d}.png",
        frames_dir / f"{frame_index}.png",
    ]
    for path in candidates:
        if path.exists():
            return Image.open(path).convert("RGB")
    raise FileNotFoundError(f"frame {frame_index} not found in {frames_dir}")


def _extract_frames_from_video(
    video_path: Path,
    frame_indices: list[int],
    ffmpeg_bin: str | None = None,
) -> dict[int, Image.Image]:
    exe = ffmpeg_bin or shutil.which("ffmpeg")
    if not exe:
        raise RuntimeError("ffmpeg binary not found on PATH; pass --ffmpeg or use --frames-dir")
    unique_frames = sorted(set(frame_indices))
    select_expr = "+".join(f"eq(n\\,{idx})" for idx in unique_frames)
    loaded: dict[int, Image.Image] = {}
    with tempfile.TemporaryDirectory(prefix="motion-still-qa-") as tmp:
        out_pattern = str(Path(tmp) / "extracted_%04d.png")
        cmd = [
            exe,
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(video_path),
            "-vf",
            f"select='{select_expr}'",
            "-vsync",
            "0",
            out_pattern,
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg frame extract failed: {proc.stderr.strip()}")
        extracted = sorted(Path(tmp).glob("extracted_*.png"))
        if len(extracted) != len(unique_frames):
            raise RuntimeError(
                f"expected {len(unique_frames)} extracted frames, got {len(extracted)}"
            )
        for frame_idx, img_path in zip(unique_frames, extracted):
            with Image.open(img_path) as im:
                loaded[frame_idx] = im.convert("RGB")
    return loaded


def build_still_qa_artifacts(
    *,
    output_dir: Path,
    frame_indices: Iterable[int],
    frames_dir: Path | None = None,
    video_path: Path | None = None,
    crops: list[CropBox] | None = None,
    make_strip: bool = False,
    strip_cols: int = 3,
    tile_width: int = 480,
    ffmpeg_bin: str | None = None,
) -> dict[str, Any]:
    """Generate 1:1 native-pixel crops and/or labeled motion strip contact sheet."""
    if (frames_dir is None) == (video_path is None):
        raise ValueError("provide exactly one of frames_dir or video_path")
    indices = list(frame_indices)
    if not indices:
        raise ValueError("frame_indices must not be empty")

    output_dir.mkdir(parents=True, exist_ok=True)
    if video_path is not None:
        loaded_frames = _extract_frames_from_video(video_path, indices, ffmpeg_bin=ffmpeg_bin)
    else:
        assert frames_dir is not None
        loaded_frames = {idx: _load_frame_from_dir(frames_dir, idx) for idx in set(indices)}

    crop_artifacts: list[dict[str, Any]] = []
    strip_tiles: list[tuple[int, Image.Image]] = []
    source_size: tuple[int, int] | None = None

    for idx in indices:
        img = loaded_frames[idx]
        w, h = img.size
        source_size = (w, h)
        for box in crops or []:
            x1 = min(w, box.x + box.w)
            y1 = min(h, box.y + box.h)
            if box.x >= w or box.y >= h or x1 <= box.x or y1 <= box.y:
                raise ValueError(
                    f"crop {box} out of frame bounds ({w}x{h}) on frame {idx}"
                )
            cropped = img.crop((box.x, box.y, x1, y1))
            crop_path = output_dir / f"f{idx:04d}_crop_{box.label()}.png"
            cropped.save(crop_path)
            crop_artifacts.append(
                {
                    "frame": idx,
                    "crop": {"x": box.x, "y": box.y, "w": x1 - box.x, "h": y1 - box.y},
                    "scale": "1:1",
                    "path": str(crop_path),
                }
            )
        if make_strip:
            tile_h = max(1, round(h * (tile_width / float(w))))
            tile = img.resize((tile_width, tile_h), Image.Resampling.LANCZOS)
            draw = ImageDraw.Draw(tile)
            label = f"f{idx:04d}"
            draw.rectangle((4, 4, 4 + len(label) * 8 + 10, 24), fill=(0, 0, 0))
            draw.text((9, 8), label, fill=(255, 255, 255))
            strip_tiles.append((idx, tile))

    strip_path_str: str | None = None
    if make_strip and strip_tiles:
        cols = max(1, strip_cols)
        rows = (len(strip_tiles) + cols - 1) // cols
        tw, th = strip_tiles[0][1].size
        sheet = Image.new("RGB", (cols * tw, rows * th), (18, 18, 18))
        for pos, (_, tile) in enumerate(strip_tiles):
            r, c = divmod(pos, cols)
            sheet.paste(tile, (c * tw, r * th))
        strip_path = output_dir / "motion_strip_contact_sheet.png"
        sheet.save(strip_path)
        strip_path_str = str(strip_path)

    receipt = {
        "schema": "motion_still_qa_receipt.v1",
        "source_type": "video" if video_path is not None else "frames_dir",
        "source_path": str(video_path or frames_dir),
        "source_resolution": list(source_size) if source_size else None,
        "frames_checked": indices,
        "crop_artifacts": crop_artifacts,
        "motion_strip_path": strip_path_str,
    }
    receipt_path = output_dir / "still_qa_receipt.json"
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return receipt
