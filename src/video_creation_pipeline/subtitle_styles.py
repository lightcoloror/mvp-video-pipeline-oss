"""Shared accessible subtitle style resolver, managed font verifier, and ASS builder.

Single source of truth for both FFmpeg/libass (`audio_timeline`, `video_delivery`)
and Remotion (`remotion_project`). Enforces static CJK font verification,
hash-bound font bundling, safe-area alignment, and libass font selection proof.
"""
from __future__ import annotations

import colorsys
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable

from .contract_io import artifact_reference, atomic_write_json, file_sha256, read_json_object
from .editing_presets import POLICY
from .errors import ContractError
from .execution_receipts import resolve_media_tool
from .subtitle_readability import subtitle_font

STYLE_POLICY_SCHEMA = "video_creation_pipeline.subtitle_style_presets.v1"
FONT_BUNDLE_SCHEMA = "video_creation_pipeline.subtitle_font_bundle.v1"
STYLE_POLICY_PATH = Path(__file__).with_name("policies") / "subtitle-style-presets.v1.json"

_IMPL_FILES = (
    Path(__file__),
    Path(__file__).with_name("subtitle_sequence.py"),
    Path(__file__).with_name("subtitle_readability.py"),
    Path(__file__).with_name("subtitle_expression.py"),
)


def implementation_sha256() -> str:
    """Digest of subtitle style, sequence, and readability modules."""
    digest = hashlib.sha256()
    for path in _IMPL_FILES:
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def load_subtitle_style_policy() -> tuple[dict[str, Any], str]:
    policy = read_json_object(STYLE_POLICY_PATH, "subtitle style presets policy")
    if policy.get("schema") != STYLE_POLICY_SCHEMA:
        raise ContractError("unsupported subtitle style presets policy schema")
    return policy, file_sha256(STYLE_POLICY_PATH)


def hex_rgb_to_ass_bgr(hex_color: str, opacity: float = 1.0) -> str:
    """Convert #RRGGBB and opacity [0..1] into ASS &HAABBGGRR."""
    match = re.fullmatch(r"#?([0-9A-Fa-f]{6})", str(hex_color or "").strip())
    if not match:
        raise ContractError(f"invalid RGB hex color: {hex_color!r}")
    if isinstance(opacity, bool) or not isinstance(opacity, (int, float)) or not 0.0 <= float(opacity) <= 1.0:
        raise ContractError(f"invalid opacity value: {opacity!r}")
    rgb = match.group(1).upper()
    rr, gg, bb = rgb[0:2], rgb[2:4], rgb[4:6]
    alpha = round((1.0 - float(opacity)) * 255)
    return f"&H{alpha:02X}{bb}{gg}{rr}"


def hex_rgb_to_rgba_css(hex_color: str, opacity: float = 1.0) -> str:
    match = re.fullmatch(r"#?([0-9A-Fa-f]{6})", str(hex_color or "").strip())
    if not match:
        raise ContractError(f"invalid RGB hex color: {hex_color!r}")
    rgb = match.group(1)
    r, g, b = int(rgb[0:2], 16), int(rgb[2:4], 16), int(rgb[4:6], 16)
    return f"rgba({r}, {g}, {b}, {round(float(opacity), 4):g})"


def ass_alpha_to_opacity(ass_color: str) -> float:
    match = re.fullmatch(r"&H([0-9A-Fa-f]{2})([0-9A-Fa-f]{6})&?", ass_color.strip())
    if not match:
        raise ContractError(f"invalid ASS color token: {ass_color!r}")
    alpha = int(match.group(1), 16)
    return round(1.0 - alpha / 255.0, 4)


def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    match = re.fullmatch(r"#?([0-9A-Fa-f]{6})", str(hex_color or "").strip())
    if not match:
        raise ContractError(f"invalid RGB hex color: {hex_color!r}")
    rgb = match.group(1)
    return int(rgb[0:2], 16), int(rgb[2:4], 16), int(rgb[4:6], 16)


def _srgb_relative_luminance(rgb: tuple[int, int, int]) -> float:
    def _chan(v: int) -> float:
        c = v / 255.0
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = rgb
    return 0.2126 * _chan(r) + 0.7152 * _chan(g) + 0.0722 * _chan(b)


def wcag_contrast_ratio(rgb1: tuple[int, int, int], rgb2: tuple[int, int, int]) -> float:
    l1 = _srgb_relative_luminance(rgb1)
    l2 = _srgb_relative_luminance(rgb2)
    hi, lo = max(l1, l2), min(l1, l2)
    return (hi + 0.05) / (lo + 0.05)


def worst_case_subtitle_backing_rgb(style_spec: dict[str, Any]) -> tuple[int, int, int]:
    """Compute worst-case immediate backing RGB behind glyph strokes for WCAG checks."""
    bs = int(style_spec["border_style"])
    if bs == 3:
        bg_r, bg_g, bg_b = _hex_to_rgb(str(style_spec["background_color"]))
        alpha = float(style_spec["background_opacity"])
        return (
            round(alpha * bg_r + (1.0 - alpha) * 255.0),
            round(alpha * bg_g + (1.0 - alpha) * 255.0),
            round(alpha * bg_b + (1.0 - alpha) * 255.0),
        )
    if bs == 4:
        if float(style_spec.get("outline_px", 0)) > 0 and style_spec.get("outline_color"):
            return _hex_to_rgb(str(style_spec["outline_color"]))
        bg_r, bg_g, bg_b = _hex_to_rgb(str(style_spec["background_color"]))
        alpha = float(style_spec["background_opacity"])
        return (
            round(alpha * bg_r + (1.0 - alpha) * 255.0),
            round(alpha * bg_g + (1.0 - alpha) * 255.0),
            round(alpha * bg_b + (1.0 - alpha) * 255.0),
        )
    return _hex_to_rgb(str(style_spec.get("outline_color") or "#101010"))


def _hue_collision(rgb: tuple[int, int, int], accent_hex: str) -> bool:
    r, g, b = (c / 255.0 for c in rgb)
    h, s, _v = colorsys.rgb_to_hsv(r, g, b)
    if s < 0.22:
        return False
    deg = h * 360.0
    ar, ag, ab = (c / 255.0 for c in _hex_to_rgb(accent_hex))
    ah, a_s, _ = colorsys.rgb_to_hsv(ar, ag, ab)
    if a_s >= 0.22:
        adeg = ah * 360.0
        dist = min(abs(deg - adeg), 360.0 - abs(deg - adeg))
        if dist <= 18.0:
            return True
    # Also guard default gold/amber emphasis zone (40..75 deg) when using gold accent
    if accent_hex.upper() in ("#FFE066", "#FFE68C") and 40.0 <= deg <= 75.0:
        return True
    return False


def resolve_speaker_styles(
    style_spec: dict[str, Any],
    speaker_colors: dict[str, Any],
    *,
    emphasis_accent_hex: str = "#FFE066",
) -> dict[str, dict[str, Any]]:
    """Validate scene-adaptive per-video speaker colors against immediate backing and emphasis hue."""
    if not isinstance(speaker_colors, dict):
        raise ContractError("speaker_colors must be an object mapping speaker names to color specs")
    worst_backing = worst_case_subtitle_backing_rgb(style_spec)
    default_hex = f"#{str(style_spec['text_color']).lstrip('#').upper()}"
    accent_norm = f"#{str(emphasis_accent_hex).lstrip('#').upper()}"
    resolved: dict[str, dict[str, Any]] = {}
    non_default_idx = 1
    for speaker_name in sorted(speaker_colors):
        if not isinstance(speaker_name, str) or not speaker_name.strip() or any(c in speaker_name for c in "{}\r\n"):
            raise ContractError("speaker name must be safe nonempty text")
        entry = speaker_colors[speaker_name]
        if not isinstance(entry, dict):
            raise ContractError(f"speaker color spec for {speaker_name!r} must be an object")
        rgb = _hex_to_rgb(str(entry.get("color") or ""))
        norm_hex = f"#{rgb[0]:02X}{rgb[1]:02X}{rgb[2]:02X}"
        label = entry.get("label")
        reason = entry.get("color_reason")
        for field_name, val in (("label", label), ("color_reason", reason)):
            if not isinstance(val, str) or not val.strip() or any(c in val for c in "{}\\\r\n"):
                raise ContractError(f"speaker {speaker_name!r} {field_name} must be safe nonempty text")
        if _hue_collision(rgb, accent_norm):
            raise ContractError(
                f"speaker {speaker_name!r} color {norm_hex} collides with emphasis accent hue {accent_norm}"
            )
        contrast = wcag_contrast_ratio(rgb, worst_backing)
        if contrast < 4.5:
            raise ContractError(
                f"speaker {speaker_name!r} color {norm_hex} contrast {contrast:.2f}:1 is below 4.5:1"
            )
        if norm_hex == default_hex:
            assigned_style = "Default"
        else:
            assigned_style = f"Speaker_{non_default_idx:02d}"
            non_default_idx += 1
        resolved[speaker_name] = {
            "style_name": assigned_style,
            "text_color": norm_hex,
            "ass_primary_colour": hex_rgb_to_ass_bgr(norm_hex, 1.0),
            "label": label.strip(),
            "color_reason": reason.strip(),
            "wcag_contrast_worst_case": round(contrast, 3),
        }
    return resolved


def _default_font_search_dirs() -> list[Path]:
    dirs: list[Path] = []
    env_root = os.environ.get("SELF_MEDIA_SUBTITLE_RUNTIME_ROOT", "").strip()
    if env_root:
        dirs.append(Path(env_root) / "fonts")
    pkg_root = Path(__file__).resolve().parents[2]
    dirs.append(pkg_root / "fonts")
    windir = os.environ.get("WINDIR", "").strip()
    if windir:
        dirs.append(Path(windir) / "Fonts")
    return dirs


def _inspect_ttf_metadata(font_path: Path) -> dict[str, Any]:
    try:
        from fontTools.ttLib import TTFont
    except ImportError as exc:
        raise ContractError("fontTools is required to verify managed subtitle fonts") from exc
    if font_path.suffix.casefold() == ".ttc":
        raise ContractError("TTC font collections are forbidden; use a verified static TTF/OTF")
    with TTFont(font_path, lazy=True) as tt:
        if "fvar" in tt:
            raise ContractError(f"variable font is forbidden for ASS/Remotion subtitles: {font_path.name}")
        names = tt["name"]
        # Read Windows Unicode (platformID=3, langID=1033) first, then generic debug name.
        win_fam = next(
            (r.toUnicode() for r in names.names if r.nameID == 1 and r.platformID == 3 and r.langID == 1033),
            None,
        )
        family = win_fam or names.getDebugName(1) or ""
        postscript = names.getDebugName(6) or ""
        weight = int(tt["OS/2"].usWeightClass) if "OS/2" in tt else 400
    return {
        "family": family,
        "postscript_name": postscript,
        "weight": weight,
        "is_variable_font": False,
    }


def resolve_managed_font(
    font_id: str | None = None,
    *,
    search_dirs: tuple[Path | str, ...] = (),
    extra_font_path: Path | str | None = None,
) -> dict[str, Any]:
    """Locate and SHA-256-verify a static CJK subtitle font from the policy registry."""
    policy, _ = load_subtitle_style_policy()
    selected_id = str(font_id or policy["default_font_id"]).strip()
    fonts_cfg = policy.get("fonts") or {}
    if selected_id not in fonts_cfg:
        raise ContractError(f"unknown managed subtitle font_id: {selected_id}")
    spec = dict(fonts_cfg[selected_id])
    filename = str(spec["filename"])

    candidates: list[Path] = []
    if extra_font_path is not None:
        candidates.append(Path(extra_font_path).expanduser())
    for directory in (*search_dirs, *_default_font_search_dirs()):
        candidates.append(Path(directory).expanduser() / filename)
    for fallback in spec.get("system_fallback_paths", ()):
        candidates.append(Path(fallback).expanduser())

    found: Path | None = None
    for cand in candidates:
        if cand.is_file():
            found = cand.resolve()
            break
    if found is None:
        raise ContractError(f"managed subtitle font file not found: {filename}")

    actual_bytes = found.stat().st_size
    actual_sha = file_sha256(found)
    if actual_bytes != int(spec["bytes"]) or actual_sha != str(spec["sha256"]).lower():
        raise ContractError(f"managed subtitle font hash mismatch for {filename}")

    meta = _inspect_ttf_metadata(found)
    if meta["family"] != spec["family"]:
        raise ContractError(
            f"managed font family mismatch: expected {spec['family']!r}, got {meta['family']!r}"
        )
    if int(meta["weight"]) != int(spec["weight"]):
        raise ContractError(
            f"managed font weight mismatch: expected {spec['weight']}, got {meta['weight']}"
        )

    return {
        "font_id": selected_id,
        "filename": filename,
        "source_path": str(found),
        "bytes": actual_bytes,
        "sha256": actual_sha,
        "family": spec["family"],
        "localized_family": spec.get("localized_family", spec["family"]),
        "postscript_name": spec["postscript_name"],
        "weight": int(spec["weight"]),
        "is_variable_font": False,
    }


def check_font_glyph_coverage(font_path: Path | str, texts: list[str] | tuple[str, ...]) -> list[str]:
    """Fail closed if any non-whitespace character in reviewed text is missing from the font."""
    try:
        from fontTools.ttLib import TTFont
    except ImportError as exc:
        raise ContractError("fontTools is required to verify subtitle glyph coverage") from exc
    with TTFont(Path(font_path), lazy=True) as tt:
        cmap = tt.getBestCmap() or {}
        missing = sorted(
            {
                ch
                for text in texts
                for ch in str(text)
                if not ch.isspace() and ord(ch) not in cmap
            }
        )
    if missing:
        raise ContractError("managed subtitle font lacks glyphs: " + "".join(missing))
    return []


def materialize_font_bundle(
    resolved_font: dict[str, Any],
    fonts_dir: Path | str,
    *,
    manifest_path: Path | str | None = None,
) -> dict[str, Any]:
    """Copy a verified managed font into fonts_dir and write a sibling font-bundle-manifest.json."""
    target_dir = Path(fonts_dir).expanduser().resolve()
    target_dir.mkdir(parents=True, exist_ok=True)
    source = Path(resolved_font["source_path"]).resolve()
    dest = target_dir / resolved_font["filename"]
    if not dest.is_file() or file_sha256(dest) != resolved_font["sha256"]:
        shutil.copyfile(source, dest)
    if file_sha256(dest) != resolved_font["sha256"]:
        raise ContractError("copied font bundle hash verification failed")
    manifest_target = (
        Path(manifest_path).expanduser().resolve()
        if manifest_path is not None
        else target_dir.parent / "font-bundle-manifest.json"
    )
    manifest = {
        "schema": FONT_BUNDLE_SCHEMA,
        "font_id": resolved_font["font_id"],
        "family": resolved_font["family"],
        "localized_family": resolved_font["localized_family"],
        "postscript_name": resolved_font["postscript_name"],
        "weight": resolved_font["weight"],
        "is_variable_font": False,
        "fonts_dir": str(target_dir),
        "files": [
            {
                "relative_path": resolved_font["filename"],
                "bytes": resolved_font["bytes"],
                "sha256": resolved_font["sha256"],
            }
        ],
    }
    atomic_write_json(manifest, manifest_target)
    return {
        "fonts_dir": str(target_dir),
        "font_file": artifact_reference(dest, "managed subtitle font"),
        "bundle_manifest": artifact_reference(manifest_target, "subtitle font bundle manifest"),
        "manifest_data": manifest,
    }


def resolve_subtitle_style(
    width: int,
    height: int,
    *,
    preset_id: str | None = None,
    font_id: str | None = None,
    requested_font_px: int | float | None = None,
    background_opacity: float | None = None,
    background_color: str | None = None,
    speaker_colors: dict[str, Any] | None = None,
    emphasis_accent_hex: str | None = None,
    search_dirs: tuple[Path | str, ...] = (),
) -> dict[str, Any]:
    """Resolve unified ASS & Remotion style parameters from the shared policy."""
    policy, policy_sha256 = load_subtitle_style_policy()
    selected_preset_id = str(preset_id or policy["default_preset"]).strip()
    presets_cfg = policy.get("presets") or {}
    if selected_preset_id not in presets_cfg:
        raise ContractError(f"unknown subtitle preset: {selected_preset_id}")
    preset = dict(presets_cfg[selected_preset_id])

    font_px, typography_sha256 = subtitle_font(width, height, requested_font_px)
    resolved_font = resolve_managed_font(font_id, search_dirs=search_dirs)

    orientation_key = "portrait_9_16" if height > width else "landscape_16_9"
    area = dict(policy["safe_areas"][orientation_key])
    margin_l = round(width * float(area["margin_l_ratio"]))
    margin_r = round(width * float(area["margin_r_ratio"]))
    margin_v = round(height * float(area["margin_v_ratio"]))
    usable_width = max(1.0, width - margin_l - margin_r)
    max_line_units = min(
        int(POLICY["subtitles"]["max_chars_per_line"]),
        int(math.floor(usable_width / float(font_px))),
    )
    if max_line_units <= 0:
        raise ContractError("usable subtitle safe width too narrow for canonical font size")

    border_style = int(preset["border_style"])
    min_ol = float(preset["min_outline_px"])
    scaled_ol = float(font_px) * float(preset["outline_ratio"])
    raw_ol = max(min_ol, scaled_ol if min_ol.is_integer() else round(scaled_ol, 1))
    outline_px: int | float = int(round(raw_ol)) if abs(raw_ol - round(raw_ol)) < 1e-6 else round(raw_ol, 2)
    raw_sh = float(preset["shadow"])
    shadow_px: int | float = int(round(raw_sh)) if abs(raw_sh - round(raw_sh)) < 1e-6 else round(raw_sh, 2)

    if border_style == 3:
        bg_hex = str(background_color or preset["background_color"])
        opacity = float(preset["background_opacity"] if background_opacity is None else background_opacity)
        low_op, high_op = preset["allowed_opacity_range"]
        if not float(low_op) <= opacity <= float(high_op):
            raise ContractError(
                f"box opacity {opacity} outside accessible range [{low_op}, {high_op}]"
            )
        ass_primary = hex_rgb_to_ass_bgr(preset["text_color"], 1.0)
        ass_outline = hex_rgb_to_ass_bgr(bg_hex, opacity)
        ass_back = "&H00000000"
        css_bg = hex_rgb_to_rgba_css(bg_hex, opacity)
        css_outline_px = 0
        outline_hex = None
    elif border_style == 4:
        bg_hex = str(background_color or preset["background_color"])
        opacity = float(preset["background_opacity"] if background_opacity is None else background_opacity)
        low_op, high_op = preset["allowed_opacity_range"]
        if not float(low_op) <= opacity <= float(high_op):
            raise ContractError(
                f"box opacity {opacity} outside accessible range [{low_op}, {high_op}]"
            )
        outline_hex = str(preset.get("outline_color") or "#0C0C0C")
        ass_primary = hex_rgb_to_ass_bgr(preset["text_color"], 1.0)
        ass_outline = hex_rgb_to_ass_bgr(outline_hex, 1.0) if float(outline_px) > 0 else "&HFF000000"
        ass_back = hex_rgb_to_ass_bgr(bg_hex, opacity)
        css_bg = hex_rgb_to_rgba_css(bg_hex, opacity)
        css_outline_px = outline_px
    else:
        bg_hex = None
        opacity = 0.0
        outline_hex = str(preset["outline_color"])
        ass_primary = hex_rgb_to_ass_bgr(preset["text_color"], 1.0)
        ass_outline = hex_rgb_to_ass_bgr(outline_hex, 1.0)
        sh_hex = str(preset.get("shadow_color") or "#000000")
        sh_op = float(preset.get("shadow_opacity", 0.5))
        ass_back = hex_rgb_to_ass_bgr(sh_hex, sh_op)
        css_bg = "transparent"
        css_outline_px = max(
            float(preset["min_outline_px"]),
            round(float(font_px) * float(preset["css_outline_px_ratio"]), 1),
        )

    accent_hex = f"#{str(emphasis_accent_hex or '#FFE066').lstrip('#').upper()}"
    resolved = {
        "preset_id": selected_preset_id,
        "policy_sha256": policy_sha256,
        "typography_policy_sha256": typography_sha256,
        "implementation_sha256": implementation_sha256(),
        "orientation": orientation_key,
        "width": int(width),
        "height": int(height),
        "font": resolved_font,
        "font_px": int(font_px),
        "bold": -1 if resolved_font["weight"] >= 700 else 0,
        "text_color": str(preset["text_color"]),
        "emphasis_accent_hex": accent_hex,
        "background_color": bg_hex,
        "background_opacity": round(opacity, 4),
        "outline_color": outline_hex,
        "border_style": border_style,
        "outline_px": outline_px,
        "shadow": shadow_px,
        "ass_primary_colour": ass_primary,
        "ass_secondary_colour": ass_primary,
        "ass_outline_colour": ass_outline,
        "ass_back_colour": ass_back,
        "alignment": int(area["alignment"]),
        "margin_l": margin_l,
        "margin_r": margin_r,
        "margin_v": margin_v,
        "safe_region": {k: float(area[k]) for k in ("x", "y", "width", "height")},
        "max_line_units": max_line_units,
        "max_lines": int(POLICY["subtitles"]["max_lines"]),
        "css": {
            "textColor": str(preset["text_color"]),
            "backgroundColor": css_bg,
            "backgroundHex": bg_hex,
            "backgroundOpacity": round(opacity, 4),
            "outlineColor": outline_hex or "#000000",
            "outlinePx": css_outline_px,
            "shadowCss": str(preset["css_shadow"]),
            "paddingY": max(4, round(float(font_px) * float(preset["css_padding_y_ratio"]))),
            "paddingX": max(10, round(float(font_px) * float(preset["css_padding_x_ratio"]))),
            "borderRadius": int(preset["css_border_radius_px"]),
            "fontPx": int(font_px),
        },
    }
    if speaker_colors:
        resolved["speaker_styles"] = resolve_speaker_styles(
            resolved, speaker_colors, emphasis_accent_hex=accent_hex
        )
    return resolved


def ass_timestamp(seconds: float) -> str:
    ticks = max(0, round(float(seconds) * 100))
    return f"{ticks // 360000}:{ticks // 6000 % 60:02}:{ticks // 100 % 60:02}.{ticks % 100:02}"


def build_ass_document(
    cues: list[dict[str, Any]],
    style_spec: dict[str, Any],
    *,
    style_name: str = "Default",
) -> str:
    """Build a complete ASS v4.00+ subtitle file from a resolved style spec and cues."""
    w = int(style_spec["width"])
    h = int(style_spec["height"])
    font_family = str(style_spec["font"]["family"])
    if any(ch in font_family for ch in ",{}\\\n\r"):
        raise ContractError("unsafe font family name for ASS")
    ol_val = style_spec["outline_px"]
    sh_val = style_spec["shadow"]
    ol_str = f"{float(ol_val):g}"
    sh_str = f"{float(sh_val):g}"

    def _format_style_line(name: str, primary_ass_color: str) -> str:
        return (
            f"Style: {name},{font_family},{style_spec['font_px']},"
            f"{primary_ass_color},{primary_ass_color},"
            f"{style_spec['ass_outline_colour']},{style_spec['ass_back_colour']},"
            f"{style_spec['bold']},0,0,0,100,100,0,0,"
            f"{style_spec['border_style']},{ol_str},{sh_str},"
            f"{style_spec['alignment']},{style_spec['margin_l']},{style_spec['margin_r']},{style_spec['margin_v']},1"
        )

    speaker_styles_map = style_spec.get("speaker_styles") or {}
    extra_style_lines: list[str] = []
    emitted_style_names = {style_name}
    for sp_cfg in speaker_styles_map.values():
        sp_style_name = str(sp_cfg["style_name"])
        if sp_style_name not in emitted_style_names:
            emitted_style_names.add(sp_style_name)
            extra_style_lines.append(_format_style_line(sp_style_name, str(sp_cfg["ass_primary_colour"])))

    header_lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {w}",
        f"PlayResY: {h}",
        "WrapStyle: 2",
        "ScaledBorderAndShadow: yes",
        "YCbCr Matrix: TV.709",
        "[V4+ Styles]",
        (
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
            "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
            "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
            "Alignment, MarginL, MarginR, MarginV, Encoding"
        ),
        _format_style_line(style_name, str(style_spec["ass_primary_colour"])),
        *extra_style_lines,
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    event_lines: list[str] = []
    for cue in cues:
        sp_name = cue.get("speaker")
        sp_cfg = speaker_styles_map.get(sp_name) if sp_name else None
        cue_style_name = str(sp_cfg["style_name"]) if sp_cfg else style_name
        cue_text_color = str(sp_cfg["text_color"]) if sp_cfg else str(style_spec["text_color"])
        if isinstance(cue.get("expressive"), dict):
            from .subtitle_expression import compile_expressive_ass_dialogues

            expr_lines = compile_expressive_ass_dialogues(
                cue,
                style_spec,
                style_name=cue_style_name,
                base_text_color=cue_text_color,
            )
            if expr_lines is not None:
                event_lines.extend(expr_lines)
                continue
        lines = cue.get("render_lines") or cue.get("lines")
        if not lines:
            text_val = str(cue.get("display_text") or cue.get("text") or "").strip()
            lines = text_val.split("\n")
        body = "\\N".join(str(line) for line in lines)
        if any(c in body for c in "{}\r"):
            raise ContractError("ASS dialogue event cannot contain raw braces or CR")
        start_str = ass_timestamp(float(cue["start_s"]))
        end_str = ass_timestamp(float(cue["end_s"]))
        event_lines.append(f"Dialogue: 0,{start_str},{end_str},{cue_style_name},,0,0,0,,{body}")
    return "\n".join([*header_lines, *event_lines]) + "\n"


def ffmpeg_filter_escape(path: Path | str) -> str:
    """Escape a local path for FFmpeg single-quoted filter options (supports CJK and spaces)."""
    text = Path(path).expanduser().resolve().as_posix()
    if any(ch in text for ch in "'\n\r[];,\x00"):
        raise ContractError(f"unsupported filter path characters: {text}")
    return text.replace(":", r"\:")


def build_ass_ffmpeg_filter(ass_path: Path | str, fonts_dir: Path | str) -> str:
    return (
        f"subtitles=filename='{ffmpeg_filter_escape(ass_path)}':"
        f"fontsdir='{ffmpeg_filter_escape(fonts_dir)}'"
    )


def parse_libass_font_log(
    log_text: str,
    *,
    expected_font: dict[str, Any],
    fonts_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Extract and verify libass font-loading & fontselect evidence from verbose FFmpeg output."""
    loaded_files: list[str] = []
    fontselect_lines: list[str] = []
    missing_glyph_lines: list[str] = []
    for raw in log_text.splitlines():
        line = raw.strip()
        if "Loading font file" in line:
            loaded_files.append(line)
        elif "fontselect:" in line:
            fontselect_lines.append(line)
        elif "Glyph" in line and "not found" in line:
            missing_glyph_lines.append(line)

    expected_ps = str(expected_font["postscript_name"])
    expected_filename = str(expected_font["filename"])
    if not fontselect_lines:
        raise ContractError("libass fontselect evidence missing from FFmpeg log")
    if missing_glyph_lines:
        raise ContractError("libass reported missing glyphs: " + "; ".join(missing_glyph_lines))

    matched_ps = any(f"-> {expected_ps}," in line for line in fontselect_lines)
    if not matched_ps:
        raise ContractError(
            f"libass selected unexpected font (expected {expected_ps}): {fontselect_lines}"
        )
    if fonts_dir is not None and loaded_files:
        if not any(expected_filename in line for line in loaded_files):
            raise ContractError(f"libass did not load expected font file {expected_filename}")

    return {
        "verified": True,
        "expected_family": expected_font["family"],
        "expected_postscript_name": expected_ps,
        "expected_filename": expected_filename,
        "loaded_font_files": loaded_files,
        "fontselect_lines": fontselect_lines,
        "missing_glyph_lines": missing_glyph_lines,
    }


def verify_libass_font_selection(
    ass_path: Path | str,
    fonts_dir: Path | str,
    expected_font: dict[str, Any],
    *,
    width: int,
    height: int,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> dict[str, Any]:
    """Run a 1-frame null FFmpeg/libass probe to prove exact font selection and glyph coverage."""
    executable = resolve_media_tool("ffmpeg")
    sub_filter = build_ass_ffmpeg_filter(ass_path, fonts_dir)
    cmd = [
        executable,
        "-nostdin",
        "-v",
        "verbose",
        "-f",
        "lavfi",
        "-i",
        f"color=c=black:s={int(width)}x{int(height)}:d=0.1",
        "-vf",
        sub_filter,
        "-frames:v",
        "1",
        "-f",
        "null",
        "-",
    ]
    selected_runner = runner or subprocess.run
    proc = selected_runner(
        cmd,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=30.0,
        check=False,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[-1500:]
        raise ContractError(f"libass font probe failed (rc={proc.returncode}): {detail}")
    combined = (proc.stderr or "") + "\n" + (proc.stdout or "")
    return parse_libass_font_log(combined, expected_font=expected_font, fonts_dir=fonts_dir)
