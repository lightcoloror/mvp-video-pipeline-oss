"""Additive, fail-closed expressive cues, typed segment planning, and expected_plan builders.

Keeps canonical `spoken_text`, `display_text`, and whole-video `subtitles.srt` intact.
Distinguishes typed `speaker_label`, `tone_annotation`, and `speech` segments to prevent
regex over-stripping bugs and guarantees Python/JS UTF-16 safety via pre-sliced `runs`.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import unicodedata
from typing import Any

from .errors import ContractError
from .subtitle_readability import subtitle_layout, units
from .subtitle_sequence import reconstruct_display_text

EXPRESSIVE_SCHEMA = "video_creation_pipeline.expressive_subtitle_cues.v1"
EXPRESSIVE_AUDIT_SCHEMA = "video_creation_pipeline.expressive_subtitle_audit.v1"
EXPECTED_PLAN_SCHEMA = "video_creation_pipeline.subtitle_expected_plan.v1"

ALLOWED_EVIDENCE_STATUS = frozenset({"verified_synthetic", "human_audio_verified"})
FORBIDDEN_EVIDENCE_METHODS = frozenset(
    {
        "pitch_heuristic",
        "volume_heuristic",
        "loudness_heuristic",
        "keyword_inference",
        "auto_emotion",
        "guess",
        "unverified",
    }
)
ALLOWED_TONE_LABELS = frozenset({"轻笑", "笑着说", "低声", "叹气"})
FORBIDDEN_EMOTION_TOKENS = (
    "嘲讽",
    "反讽",
    "讽刺",
    "愤怒",
    "生气",
    "惊讶",
    "震惊",
    "阴阳",
    "委屈",
    "开心",
    "难过",
    "sarcas",
    "irony",
    "angry",
    "surpris",
)

NUMBER_UNIT_RE = re.compile(
    r"\d+(?:\.\d+)?(?:\s*(?:[%‰℃万亿元块天年月周小时分钟秒个条项次倍]|mg|ml|kg|cm|mm|km|GB|MB))?"
)

DEFAULT_EMPHASIS_SCALE = 1.25
DEFAULT_EMPHASIS_HEX = "#FFE066"
DEFAULT_TONE_HEX = "#DCE4EC"
MIN_SUBSEGMENT_DURATION_S = 0.50
MAX_CPS = 9.0
MIN_PAUSE_GAP_S = 0.25


def canonical_json_sha256(obj: Any) -> str:
    payload = json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _is_safe_plain_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip()) and not any(c in value for c in "{}\\\r\n")


def _hex_rgb_to_ass_1c(hex_color: str) -> str:
    match = re.fullmatch(r"#?([0-9A-Fa-f]{6})", str(hex_color or "").strip())
    if not match:
        raise ContractError(f"invalid RGB hex color: {hex_color!r}")
    rgb = match.group(1).upper()
    return f"&H{rgb[4:6]}{rgb[2:4]}{rgb[0:2]}&"


def quantize_ass_cs(seconds: float) -> float:
    return round(round(float(seconds) * 100.0) / 100.0, 2)


def _ass_time(seconds: float) -> str:
    ticks = max(0, round(float(seconds) * 100))
    return f"{ticks // 360000}:{ticks // 6000 % 60:02}:{ticks // 100 % 60:02}.{ticks % 100:02}"


def _locate_spoken_offset_in_display(display_text: str, spoken_text: str) -> int | None:
    idx = display_text.find(spoken_text)
    return idx if idx >= 0 else None


def _valid_grapheme_boundary(text: str, idx: int) -> bool:
    if idx <= 0 or idx >= len(text):
        return True
    if unicodedata.combining(text[idx]) != 0:
        return False
    # Guard zero-width joiner sequences
    if text[idx] == "\u200d" or text[idx - 1] == "\u200d":
        return False
    return True


def _line_display_ranges(
    render_lines: list[str],
    layout_breaks: list[dict[str, Any]] | None,
    display_text: str,
) -> list[tuple[int, int, str]]:
    """Map each render_line to its exact [start, end) range in display_text."""
    reconstructed = reconstruct_display_text(render_lines, layout_breaks)
    if reconstructed != display_text:
        raise ContractError("render_lines cannot reconstruct display_text losslessly")
    ranges: list[tuple[int, int, str]] = []
    cursor = 0
    for idx, line in enumerate(render_lines):
        start = cursor
        end = start + len(line)
        ranges.append((start, end, line))
        cursor = end
        if idx < len(render_lines) - 1:
            if layout_breaks and idx < len(layout_breaks) and layout_breaks[idx].get("replaced_space"):
                cursor += 1
    return ranges


def _split_term_violations(spoken_text: str, split_idx: int, protected_terms: list[str]) -> bool:
    for term in protected_terms:
        if not term:
            continue
        start = 0
        while True:
            pos = spoken_text.find(term, start)
            if pos < 0:
                break
            if pos < split_idx < pos + len(term):
                return True
            start = pos + 1
    for match in NUMBER_UNIT_RE.finditer(spoken_text):
        if match.start() < split_idx < match.end():
            return True
    return False


def _usable_width_px(style_spec: dict[str, Any] | None) -> float:
    if not style_spec:
        return 1080.0 - 120.0 - 260.0
    base = max(1.0, float(style_spec["width"]) - float(style_spec["margin_l"]) - float(style_spec["margin_r"]))
    bs = int(style_spec.get("border_style", 1))
    ol = float(style_spec.get("outline_px", 0))
    sh = float(style_spec.get("shadow", 0))
    pad = (2.0 * (sh + ol)) if bs == 4 else (2.0 * ol)
    return max(1.0, base - pad)


def _skip_record(ann: dict[str, Any], reason: str) -> dict[str, Any]:
    return {
        "annotation_id": str(ann.get("annotation_id") or ""),
        "cue_id": str(ann.get("cue_id") or ""),
        "kind": str(ann.get("kind") or ""),
        "status": "skipped_fallback_to_plain",
        "skip_reason": reason,
    }


def _typed_segments_for_plain_line(text: str, speaker_prefix: str) -> list[dict[str, str]]:
    if speaker_prefix and text.startswith(speaker_prefix):
        rest = text[len(speaker_prefix) :]
        segs = [{"type": "speaker_label", "text": speaker_prefix}]
        if rest:
            segs.append({"type": "speech", "text": rest})
        return segs
    return [{"type": "speech", "text": text}]


def _build_emphasis_runs(
    cue: dict[str, Any],
    *,
    spans: list[dict[str, Any]],
    base_font_px: int,
    emp_font_px: int,
    base_color_hex: str,
    accent_hex: str,
) -> list[dict[str, Any]]:
    render_lines = list(cue.get("render_lines") or cue.get("lines") or [])
    line_ranges = _line_display_ranges(render_lines, cue.get("layout_breaks"), cue["display_text"])
    span_bounds = [(int(s["display_start"]), int(s["display_end"])) for s in spans]
    speaker_prefix = str(cue.get("speaker_prefix") or "")
    prefix_end = len(speaker_prefix) if speaker_prefix and cue["display_text"].startswith(speaker_prefix) else 0

    runs: list[dict[str, Any]] = []
    for line_idx, (l_start, l_end, l_text) in enumerate(line_ranges):
        boundaries = {l_start, l_end}
        if l_start < prefix_end < l_end:
            boundaries.add(prefix_end)
        for ds, de in span_bounds:
            if l_start < ds < l_end:
                boundaries.add(ds)
            if l_start < de < l_end:
                boundaries.add(de)
        pts = sorted(boundaries)
        for a, b in zip(pts[:-1], pts[1:]):
            seg_text = l_text[a - l_start : b - l_start]
            if not seg_text:
                continue
            seg_type = "speaker_label" if b <= prefix_end else "speech"
            is_emp = any(ds <= a and b <= de for ds, de in span_bounds)
            runs.append(
                {
                    "line_index": line_idx,
                    "type": seg_type,
                    "text": seg_text,
                    "emphasis": is_emp,
                    "font_px": emp_font_px if is_emp else base_font_px,
                    "color_hex": accent_hex if is_emp else base_color_hex,
                }
            )
    return runs


def apply_expressive_cues(
    sequence: dict[str, Any],
    expressive_doc: dict[str, Any],
    *,
    style_spec: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Validate and attach non-destructive expressive cues onto a reviewed sequence."""
    if not isinstance(expressive_doc, dict) or expressive_doc.get("schema") != EXPRESSIVE_SCHEMA:
        raise ContractError("unsupported expressive subtitle cues schema")

    annotated = copy.deepcopy(sequence)
    cues = annotated.get("cues", [])
    cue_by_id = {cue["cue_id"]: cue for cue in cues}

    applied: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    annotations = expressive_doc.get("annotations")
    if not isinstance(annotations, list):
        raise ContractError("expressive annotations must be a list")

    expected_dur = expressive_doc.get("timeline_duration_s")
    if expected_dur is not None:
        if (
            isinstance(expected_dur, bool)
            or not isinstance(expected_dur, (int, float))
            or abs(float(expected_dur) - float(annotated.get("total_duration_s", 0.0))) > 0.02
        ):
            for ann in annotations:
                if isinstance(ann, dict):
                    skipped.append(_skip_record(ann, "stale_binding_timeline_duration_mismatch"))
            return annotated, {
                "schema": EXPRESSIVE_AUDIT_SCHEMA,
                "applied_count": 0,
                "skipped_count": len(skipped),
                "applied": applied,
                "skipped": skipped,
            }

    max_line_units = int(style_spec["max_line_units"]) if style_spec else 11
    base_font_px = int(style_spec["font_px"]) if style_spec else 59
    usable_width_px = _usable_width_px(style_spec)
    emphasis_hex = str((style_spec or {}).get("emphasis_accent_hex") or DEFAULT_EMPHASIS_HEX)

    for ann in annotations:
        if not isinstance(ann, dict):
            raise ContractError("expressive annotation entry must be an object")
        ann_id = str(ann.get("annotation_id") or "").strip()
        cue_id = str(ann.get("cue_id") or "").strip()
        kind = str(ann.get("kind") or "").strip()
        if not ann_id or not cue_id or kind not in ("emphasis", "pause", "tone"):
            skipped.append(_skip_record(ann, "invalid_annotation_identity_or_kind"))
            continue

        cue = cue_by_id.get(cue_id)
        if cue is None:
            skipped.append(_skip_record(ann, "stale_binding_cue_id_not_found"))
            continue
        if "expressive" in cue:
            skipped.append(_skip_record(ann, "duplicate_cue_annotation_skipped"))
            continue

        if ann.get("expected_spoken_text") != cue["spoken_text"]:
            skipped.append(_skip_record(ann, "stale_binding_text_mismatch"))
            continue
        exp_start = ann.get("expected_start_s")
        exp_end = ann.get("expected_end_s")
        if (
            isinstance(exp_start, bool)
            or isinstance(exp_end, bool)
            or not isinstance(exp_start, (int, float))
            or not isinstance(exp_end, (int, float))
            or abs(float(exp_start) - float(cue["start_s"])) > 0.02
            or abs(float(exp_end) - float(cue["end_s"])) > 0.02
        ):
            skipped.append(_skip_record(ann, "stale_binding_timing_mismatch"))
            continue

        ev_status = str(ann.get("evidence_status") or "").strip()
        ev_method = str(ann.get("evidence_method") or "").strip()
        ev_source = str(ann.get("evidence_source") or "").strip()
        if ev_status not in ALLOWED_EVIDENCE_STATUS or not ev_source:
            skipped.append(_skip_record(ann, "unverified_or_ambiguous_evidence"))
            continue
        if ev_method in FORBIDDEN_EVIDENCE_METHODS or not ev_method:
            skipped.append(_skip_record(ann, "heuristic_or_inferred_evidence_forbidden"))
            continue

        spoken_text = cue["spoken_text"]
        display_text = cue["display_text"]

        if kind == "emphasis":
            activation = str(ann.get("activation") or "whole_segment").strip()
            if activation != "whole_segment":
                skipped.append(_skip_record(ann, "unsupported_emphasis_activation"))
                continue
            raw_spans = ann.get("spans")
            if not isinstance(raw_spans, list) or not raw_spans:
                skipped.append(_skip_record(ann, "missing_emphasis_spans"))
                continue
            spoken_offset = _locate_spoken_offset_in_display(display_text, spoken_text)
            if spoken_offset is None:
                skipped.append(_skip_record(ann, "emphasis_spoken_mapping_ambiguous"))
                continue
            valid_spans: list[dict[str, Any]] = []
            prev_end = -1
            span_ok = True
            for sp in raw_spans:
                if not isinstance(sp, dict):
                    span_ok = False
                    break
                cs, ce, txt = sp.get("char_start"), sp.get("char_end"), sp.get("text")
                if (
                    isinstance(cs, bool)
                    or isinstance(ce, bool)
                    or not isinstance(cs, int)
                    or not isinstance(ce, int)
                    or not _is_safe_plain_text(txt)
                    or not (0 <= cs < ce <= len(spoken_text))
                    or cs < prev_end
                    or not _valid_grapheme_boundary(spoken_text, cs)
                    or not _valid_grapheme_boundary(spoken_text, ce)
                    or spoken_text[cs:ce] != txt
                ):
                    span_ok = False
                    break
                prev_end = ce
                valid_spans.append(
                    {
                        "char_start": cs,
                        "char_end": ce,
                        "display_start": spoken_offset + cs,
                        "display_end": spoken_offset + ce,
                        "text": txt,
                    }
                )
            if not span_ok:
                skipped.append(_skip_record(ann, "invalid_or_mismatched_emphasis_span"))
                continue

            scale_factor = float(ann.get("scale_factor") or DEFAULT_EMPHASIS_SCALE)
            if not 1.1 <= scale_factor <= 1.35:
                skipped.append(_skip_record(ann, "emphasis_scale_out_of_bounds"))
                continue
            emp_px = round(base_font_px * scale_factor)
            line_ranges = _line_display_ranges(cue["render_lines"], cue.get("layout_breaks"), display_text)
            emp_chars = {i for sp in valid_spans for i in range(sp["display_start"], sp["display_end"])}
            width_ok = True
            for l_start, _l_end, l_text in line_ranges:
                est_px = 0.0
                for offset, ch in enumerate(l_text):
                    px = emp_px if (l_start + offset) in emp_chars else base_font_px
                    est_px += units(ch) * px
                if est_px > usable_width_px + 1e-6:
                    width_ok = False
                    break
            if not width_ok:
                skipped.append(_skip_record(ann, "emphasis_would_overflow_safe_width"))
                continue

            cue["expressive"] = {
                "annotation_id": ann_id,
                "kind": "emphasis",
                "activation": activation,
                "spans": valid_spans,
                "scale_factor": scale_factor,
                "emphasis_font_px": emp_px,
                "accent_hex": str(ann.get("accent_hex") or emphasis_hex).upper(),
                "evidence_status": ev_status,
                "evidence_method": ev_method,
                "evidence_source": ev_source,
            }
            applied.append({"annotation_id": ann_id, "cue_id": cue_id, "kind": kind, "status": "applied"})

        elif kind == "pause":
            pause_mode = str(ann.get("pause_mode") or "progressive_reveal").strip()
            if pause_mode not in ("progressive_reveal", "clear_gap"):
                skipped.append(_skip_record(ann, "unsupported_pause_mode"))
                continue
            split_idx = ann.get("split_char_index")
            p1_end = ann.get("part1_end_s")
            p2_start = ann.get("part2_start_s")
            if (
                isinstance(split_idx, bool)
                or not isinstance(split_idx, int)
                or not (0 < split_idx < len(spoken_text))
                or not _valid_grapheme_boundary(spoken_text, split_idx)
                or isinstance(p1_end, bool)
                or isinstance(p2_start, bool)
                or not isinstance(p1_end, (int, float))
                or not isinstance(p2_start, (int, float))
            ):
                skipped.append(_skip_record(ann, "invalid_pause_split_or_timestamps"))
                continue
            c_start_q = quantize_ass_cs(float(cue["start_s"]))
            c_end_q = quantize_ass_cs(float(cue["end_s"]))
            p1_end_q = quantize_ass_cs(float(p1_end))
            p2_start_q = quantize_ass_cs(float(p2_start))
            if not (math.isfinite(p1_end_q) and math.isfinite(p2_start_q)):
                skipped.append(_skip_record(ann, "nonfinite_pause_timestamps"))
                continue
            if not (c_start_q <= p1_end_q < p2_start_q <= c_end_q):
                skipped.append(_skip_record(ann, "pause_window_outside_parent_bounds"))
                continue
            if round(p2_start_q - p1_end_q, 2) < MIN_PAUSE_GAP_S - 1e-6:
                skipped.append(_skip_record(ann, "pause_gap_too_short"))
                continue
            if _split_term_violations(spoken_text, split_idx, list(cue.get("protected_terms") or ())):
                skipped.append(_skip_record(ann, "pause_splits_protected_term_or_number_unit"))
                continue

            raw_s1 = spoken_text[:split_idx]
            raw_s2 = spoken_text[split_idx:]
            if not raw_s1.strip() or not raw_s2.strip():
                skipped.append(_skip_record(ann, "pause_produces_empty_segment"))
                continue
            if raw_s1 + raw_s2 != spoken_text:
                skipped.append(_skip_record(ann, "pause_split_non_lossless"))
                continue

            prefix = str(cue.get("speaker_prefix") or "")
            p1_display = f"{prefix}{raw_s1.lstrip()}" if prefix else raw_s1
            if pause_mode == "progressive_reveal":
                p1_dur = round(p2_start_q - c_start_q, 2)
                p2_dur = round(c_end_q - p2_start_q, 2)
                total_dur = round(c_end_q - c_start_q, 2)
                p1_cps = units(p1_display) / max(1e-6, total_dur)
                p2_cps = units(raw_s2.strip()) / max(1e-6, p2_dur)
                if p1_dur < MIN_SUBSEGMENT_DURATION_S - 1e-6 or p2_dur < MIN_SUBSEGMENT_DURATION_S - 1e-6:
                    skipped.append(_skip_record(ann, "pause_segment_fails_cps_or_min_duration"))
                    continue
                if p1_cps > MAX_CPS + 1e-6 or p2_cps > MAX_CPS + 1e-6:
                    skipped.append(_skip_record(ann, "pause_segment_fails_cps_or_min_duration"))
                    continue
                try:
                    l1 = subtitle_layout(
                        p1_display,
                        protected_terms=tuple(cue.get("protected_terms") or ()),
                        max_line_units=max_line_units,
                    )
                    l2_only = subtitle_layout(
                        raw_s2.strip(),
                        protected_terms=tuple(cue.get("protected_terms") or ()),
                        max_line_units=max_line_units,
                    )
                except ContractError:
                    skipped.append(_skip_record(ann, "pause_segment_fails_readability_layout"))
                    continue
                # Align progressive reveal with two-line layout when part1 fits line 1 and part2 fits line 2
                if len(l1["lines"]) == 1 and len(l2_only["lines"]) == 1:
                    full_lines = [l1["lines"][0], l2_only["lines"][0]]
                    full_cps = round(units("".join(full_lines)) / max(1e-6, p2_dur), 3)
                    # Check incremental reveal readability <= MAX_CPS
                else:
                    full_lines = list(cue["render_lines"])
                    if len(full_lines) > 2:
                        skipped.append(_skip_record(ann, "pause_segment_fails_readability_layout"))
                        continue
                cue["expressive"] = {
                    "annotation_id": ann_id,
                    "kind": "pause",
                    "pause_mode": "progressive_reveal",
                    "split_char_index": split_idx,
                    "part1_speech": raw_s1,
                    "part2_speech": raw_s2,
                    "part1_lines": list(l1["lines"]),
                    "part2_lines": full_lines,
                    "part1_end_s": p1_end_q,
                    "part2_start_s": p2_start_q,
                    "gap_s": round(p2_start_q - p1_end_q, 2),
                    "sub_segment_cps": [round(units(p1_display) / total_dur, 3), round(p2_cps, 3)],
                    "evidence_status": ev_status,
                    "evidence_method": ev_method,
                    "evidence_source": ev_source,
                }
            else:  # clear_gap
                p1_dur = round(p1_end_q - c_start_q, 2)
                p2_dur = round(c_end_q - p2_start_q, 2)
                if p1_dur < MIN_SUBSEGMENT_DURATION_S - 1e-6 or p2_dur < MIN_SUBSEGMENT_DURATION_S - 1e-6:
                    skipped.append(_skip_record(ann, "pause_segment_fails_cps_or_min_duration"))
                    continue
                p2_display = f"{prefix}{raw_s2.lstrip()}" if prefix else raw_s2
                p1_cps = units(p1_display) / p1_dur
                p2_cps = units(p2_display) / p2_dur
                if p1_cps > MAX_CPS + 1e-6 or p2_cps > MAX_CPS + 1e-6:
                    skipped.append(_skip_record(ann, "pause_segment_fails_cps_or_min_duration"))
                    continue
                try:
                    l1 = subtitle_layout(
                        p1_display,
                        protected_terms=tuple(cue.get("protected_terms") or ()),
                        max_line_units=max_line_units,
                    )
                    l2 = subtitle_layout(
                        p2_display,
                        protected_terms=tuple(cue.get("protected_terms") or ()),
                        max_line_units=max_line_units,
                    )
                except ContractError:
                    skipped.append(_skip_record(ann, "pause_segment_fails_readability_layout"))
                    continue
                cue["expressive"] = {
                    "annotation_id": ann_id,
                    "kind": "pause",
                    "pause_mode": "clear_gap",
                    "split_char_index": split_idx,
                    "part1_speech": raw_s1,
                    "part2_speech": raw_s2,
                    "part1_lines": list(l1["lines"]),
                    "part2_lines": list(l2["lines"]),
                    "part1_end_s": p1_end_q,
                    "part2_start_s": p2_start_q,
                    "gap_s": round(p2_start_q - p1_end_q, 2),
                    "sub_segment_cps": [round(p1_cps, 3), round(p2_cps, 3)],
                    "evidence_status": ev_status,
                    "evidence_method": ev_method,
                    "evidence_source": ev_source,
                }
            applied.append({"annotation_id": ann_id, "cue_id": cue_id, "kind": kind, "status": "applied"})

        else:  # tone
            raw_label = str(ann.get("tone_label") or "").strip().strip("[]【】").strip()
            lowered = raw_label.casefold()
            if any(tok in lowered for tok in FORBIDDEN_EMOTION_TOKENS):
                skipped.append(_skip_record(ann, "inferred_emotion_forbidden"))
                continue
            if raw_label not in ALLOWED_TONE_LABELS:
                skipped.append(_skip_record(ann, "unsupported_tone_label"))
                continue
            tone_tag = f"[{raw_label}]"
            render_lines = list(cue.get("render_lines") or [])
            if len(render_lines) == 1:
                if units(tone_tag) > max_line_units:
                    skipped.append(_skip_record(ann, "tone_tag_exceeds_line_width"))
                    continue
                placement = "prefix_newline"
            elif len(render_lines) == 2 and units(f"{tone_tag} {render_lines[0]}") <= max_line_units:
                placement = "inline_line1"
            else:
                skipped.append(_skip_record(ann, "tone_tag_would_exceed_two_lines"))
                continue

            cue["expressive"] = {
                "annotation_id": ann_id,
                "kind": "tone",
                "tone_label": raw_label,
                "tone_tag": tone_tag,
                "placement": placement,
                "tag_color_hex": DEFAULT_TONE_HEX,
                "evidence_status": ev_status,
                "evidence_method": ev_method,
                "evidence_source": ev_source,
            }
            applied.append({"annotation_id": ann_id, "cue_id": cue_id, "kind": kind, "status": "applied"})

    return annotated, {
        "schema": EXPRESSIVE_AUDIT_SCHEMA,
        "applied_count": len(applied),
        "skipped_count": len(skipped),
        "applied": applied,
        "skipped": skipped,
    }


def project_expressive_for_scene(
    expressive: dict[str, Any],
    *,
    scene_start_s: float,
    scene_end_s: float,
) -> dict[str, Any]:
    """Project pause timestamps in cue['expressive'] to scene-local clock."""
    projected = copy.deepcopy(expressive)
    if projected.get("kind") == "pause":
        p1_global = float(projected["part1_end_s"])
        p2_global = float(projected["part2_start_s"])
        a = float(scene_start_s)
        b = float(scene_end_s)
        if p1_global < a - 1e-6 or p2_global > b + 1e-6:
            raise ContractError("pause expressive cue crosses scene boundary")
        projected["global_part1_end_s"] = p1_global
        projected["global_part2_start_s"] = p2_global
        projected["part1_end_s"] = quantize_ass_cs(max(0.0, p1_global - a))
        projected["part2_start_s"] = quantize_ass_cs(min(b - a, p2_global - a))
    return projected


def extra_glyph_texts(cues: list[dict[str, Any]]) -> list[str]:
    extras: list[str] = []
    for cue in cues:
        expr = cue.get("expressive")
        if isinstance(expr, dict) and expr.get("kind") == "tone":
            tag = expr.get("tone_tag")
            if isinstance(tag, str) and tag:
                extras.append(tag)
    return extras


def build_expected_render_plan(
    reviewed_sequence: dict[str, Any],
    *,
    expressive_doc: dict[str, Any] | None = None,
    style_spec: dict[str, Any],
    requested_features: list[str] | tuple[str, ...] | None = None,
    time_offset: float = 0.0,
) -> dict[str, Any]:
    """Derive deterministic Root-of-Trust expected_plan from upstream approved inputs."""
    seq_sha = canonical_json_sha256(reviewed_sequence)
    exp_sha = canonical_json_sha256(expressive_doc) if expressive_doc is not None else None
    fallback_reasons: list[str] = []

    if expressive_doc is not None:
        effective_seq, diag = apply_expressive_cues(reviewed_sequence, expressive_doc, style_spec=style_spec)
        for sk in diag.get("skipped", []):
            fallback_reasons.append(f"{sk['cue_id']}:{sk['skip_reason']}")
    else:
        effective_seq = copy.deepcopy(reviewed_sequence)

    base_font_px = int(style_spec["font_px"])
    speaker_styles_map = style_spec.get("speaker_styles") or {}
    expected_events: list[dict[str, Any]] = []
    applied_features: set[str] = set()

    for cue in effective_seq.get("cues", []):
        sp_name = cue.get("speaker")
        sp_cfg = speaker_styles_map.get(sp_name) if sp_name else None
        style_name = str(sp_cfg["style_name"]) if sp_cfg else "Default"
        base_color_hex = str(sp_cfg["text_color"]) if sp_cfg else str(style_spec["text_color"])
        if sp_cfg and base_color_hex.upper() != str(style_spec["text_color"]).upper():
            applied_features.add("speaker_colors")

        t_start = quantize_ass_cs(float(cue["start_s"]) + float(time_offset))
        t_end = quantize_ass_cs(float(cue["end_s"]) + float(time_offset))
        speaker_prefix = str(cue.get("speaker_prefix") or "")
        expr = cue.get("expressive")

        if isinstance(expr, dict) and expr.get("kind") == "emphasis":
            applied_features.add("emphasis")
            emp_px = int(expr.get("emphasis_font_px") or round(base_font_px * float(expr["scale_factor"])))
            accent_hex = str(expr.get("accent_hex") or DEFAULT_EMPHASIS_HEX).upper()
            runs = _build_emphasis_runs(
                cue,
                spans=expr["spans"],
                base_font_px=base_font_px,
                emp_font_px=emp_px,
                base_color_hex=base_color_hex,
                accent_hex=accent_hex,
            )
            vis_lines = list(cue.get("render_lines") or cue.get("lines") or [])
            vis_dur = round(t_end - t_start, 2)
            expected_events.append(
                {
                    "source_cue_id": cue["cue_id"],
                    "sub_index": 0,
                    "sub_count": 1,
                    "start_s": t_start,
                    "end_s": t_end,
                    "visible_duration_s": vis_dur,
                    "style_name": style_name,
                    "base_font_px": base_font_px,
                    "emphasis_font_px": emp_px,
                    "base_color_hex": base_color_hex,
                    "emphasis_color_hex": accent_hex,
                    "tone_color_hex": None,
                    "expected_visible_lines": vis_lines,
                    "segments": _typed_segments_for_plain_line("".join(vis_lines), speaker_prefix),
                    "runs": runs,
                    "speech_reconstruction": cue["spoken_text"],
                    "sub_segment_cps": round(units("".join(vis_lines)) / max(1e-6, vis_dur), 3),
                }
            )
        elif isinstance(expr, dict) and expr.get("kind") == "pause":
            applied_features.add("pause")
            p_mode = expr["pause_mode"]
            p1_end = quantize_ass_cs(float(expr["part1_end_s"]) + float(time_offset))
            p2_start = quantize_ass_cs(float(expr["part2_start_s"]) + float(time_offset))
            ev1_end = p2_start if p_mode == "progressive_reveal" else p1_end
            l1 = list(expr["part1_lines"])
            l2 = list(expr["part2_lines"])
            d1 = round(ev1_end - t_start, 2)
            d2 = round(t_end - p2_start, 2)
            common = {
                "source_cue_id": cue["cue_id"],
                "sub_count": 2,
                "pause_mode": p_mode,
                "style_name": style_name,
                "base_font_px": base_font_px,
                "emphasis_font_px": None,
                "base_color_hex": base_color_hex,
                "emphasis_color_hex": None,
                "tone_color_hex": None,
            }
            expected_events.append(
                {
                    **common,
                    "sub_index": 0,
                    "start_s": t_start,
                    "end_s": ev1_end,
                    "visible_duration_s": d1,
                    "expected_visible_lines": l1,
                    "segments": _typed_segments_for_plain_line("".join(l1), speaker_prefix),
                    "runs": [
                        {
                            "line_index": idx,
                            "type": "speech",
                            "text": ln,
                            "emphasis": False,
                            "font_px": base_font_px,
                            "color_hex": base_color_hex,
                        }
                        for idx, ln in enumerate(l1)
                    ],
                    "speech_reconstruction": expr["part1_speech"],
                    "sub_segment_cps": round(units("".join(l1)) / max(1e-6, d1), 3),
                }
            )
            expected_events.append(
                {
                    **common,
                    "sub_index": 1,
                    "start_s": p2_start,
                    "end_s": t_end,
                    "visible_duration_s": d2,
                    "expected_visible_lines": l2,
                    "segments": _typed_segments_for_plain_line("".join(l2), speaker_prefix),
                    "runs": [
                        {
                            "line_index": idx,
                            "type": "speech",
                            "text": ln,
                            "emphasis": False,
                            "font_px": base_font_px,
                            "color_hex": base_color_hex,
                        }
                        for idx, ln in enumerate(l2)
                    ],
                    "speech_reconstruction": expr["part2_speech"],
                    "full_cue_speech_check": cue["spoken_text"],
                    "sub_segment_cps": round(units("".join(l2)) / max(1e-6, d2), 3),
                }
            )
        elif isinstance(expr, dict) and expr.get("kind") == "tone":
            applied_features.add("tone")
            tag_text = str(expr["tone_tag"])
            tone_hex = str(expr.get("tag_color_hex") or DEFAULT_TONE_HEX).upper()
            render_lines = list(cue.get("render_lines") or cue.get("lines") or [])
            if expr.get("placement") == "prefix_newline":
                vis_lines = [tag_text, *render_lines]
            else:
                vis_lines = [f"{tag_text} {render_lines[0]}", *render_lines[1:]]
            vis_dur = round(t_end - t_start, 2)
            expected_events.append(
                {
                    "source_cue_id": cue["cue_id"],
                    "sub_index": 0,
                    "sub_count": 1,
                    "start_s": t_start,
                    "end_s": t_end,
                    "visible_duration_s": vis_dur,
                    "style_name": style_name,
                    "base_font_px": base_font_px,
                    "emphasis_font_px": None,
                    "base_color_hex": base_color_hex,
                    "emphasis_color_hex": None,
                    "tone_color_hex": tone_hex,
                    "expected_visible_lines": vis_lines,
                    "segments": [
                        {"type": "tone_annotation", "text": tag_text},
                        *_typed_segments_for_plain_line("".join(render_lines), speaker_prefix),
                    ],
                    "runs": [],
                    "speech_reconstruction": cue["spoken_text"],
                    "sub_segment_cps": round(units("".join(vis_lines)) / max(1e-6, vis_dur), 3),
                }
            )
        else:
            vis_lines = list(cue.get("render_lines") or cue.get("lines") or [])
            if not vis_lines:
                vis_lines = str(cue.get("display_text") or cue.get("text") or "").strip().split("\n")
            vis_dur = round(t_end - t_start, 2)
            expected_events.append(
                {
                    "source_cue_id": cue["cue_id"],
                    "sub_index": 0,
                    "sub_count": 1,
                    "start_s": t_start,
                    "end_s": t_end,
                    "visible_duration_s": vis_dur,
                    "style_name": style_name,
                    "base_font_px": base_font_px,
                    "emphasis_font_px": None,
                    "base_color_hex": base_color_hex,
                    "emphasis_color_hex": None,
                    "tone_color_hex": None,
                    "expected_visible_lines": vis_lines,
                    "segments": _typed_segments_for_plain_line("".join(vis_lines), speaker_prefix),
                    "runs": [],
                    "speech_reconstruction": cue["spoken_text"],
                    "sub_segment_cps": round(units("".join(vis_lines)) / max(1e-6, vis_dur), 3),
                }
            )

    req_list = sorted(set(requested_features or []))
    app_list = sorted(applied_features)
    status = "PASS_WITH_FALLBACK" if fallback_reasons else "PASS"
    return {
        "schema": EXPECTED_PLAN_SCHEMA,
        "reviewed_sequence_sha256": seq_sha,
        "expressive_cues_sha256": exp_sha,
        "style_policy_sha256": style_spec.get("policy_sha256"),
        "preset_id": style_spec.get("preset_id"),
        "requested_features": req_list,
        "applied_features": app_list,
        "fallback_reasons": fallback_reasons,
        "delivery_status": status,
        "expected_events": expected_events,
    }


def compile_expressive_ass_dialogues(
    cue: dict[str, Any],
    style_spec: dict[str, Any],
    *,
    style_name: str = "Default",
    base_text_color: str | None = None,
) -> list[str] | None:
    """Build clean single-layer ASS Dialogue line(s) for an expressive cue."""
    expr = cue.get("expressive")
    if not isinstance(expr, dict):
        return None

    kind = expr.get("kind")
    base_px = int(style_spec["font_px"])
    base_bold = int(style_spec["bold"])
    border_style = int(style_spec.get("border_style", 1))
    base_hex = (base_text_color or style_spec["text_color"]).upper()
    base_1c = _hex_rgb_to_ass_1c(base_hex)

    if kind == "emphasis":
        render_lines = list(cue.get("render_lines") or cue.get("lines") or [])
        line_ranges = _line_display_ranges(render_lines, cue.get("layout_breaks"), cue["display_text"])
        spans = [(int(s["display_start"]), int(s["display_end"])) for s in expr["spans"]]
        emp_px = int(expr.get("emphasis_font_px") or round(base_px * float(expr.get("scale_factor", DEFAULT_EMPHASIS_SCALE))))
        emp_1c = _hex_rgb_to_ass_1c(expr.get("accent_hex", DEFAULT_EMPHASIS_HEX))
        bold_on = "\\b700" if base_bold == 0 else ""
        bold_off = "\\b0" if base_bold == 0 else ""

        if border_style in (1, 4):
            # Native BorderStyle=1 and BorderStyle=4 produce a single seamless stroke/box with zero \xbord0 hack!
            lead_tag = ""
            reset_tag = f"{{{bold_off}\\fs{base_px}\\1c{base_1c}}}"
            on_tag = f"{{{bold_on}\\fs{emp_px}\\1c{emp_1c}}}"
            line_reset = f"{{{bold_off}\\fs{base_px}\\1c{base_1c}}}"
        else:
            # Legacy BorderStyle=3 compatibility mode
            base_ybord = int(round(float(style_spec["outline_px"])))
            emp_ybord = max(2, base_ybord - round((emp_px - base_px) * 0.45))
            lead_tag = f"{{\\xbord0\\ybord{base_ybord}}}"
            reset_tag = f"{{\\xbord0\\ybord{base_ybord}{bold_off}\\fs{base_px}\\1c{base_1c}}}"
            on_tag = f"{{\\xbord0\\ybord{emp_ybord}{bold_on}\\fs{emp_px}\\1c{emp_1c}}}"
            line_reset = f"{{\\xbord{base_ybord}\\ybord{base_ybord}{bold_off}\\fs{base_px}\\1c{base_1c}}}"

        ass_lines: list[str] = []
        active_emphasis = False
        for l_start, l_end, l_text in line_ranges:
            if any(c in l_text for c in "{}\r"):
                raise ContractError("ASS dialogue event cannot contain raw braces or CR")
            has_overlap = any(max(l_start, ds) < min(l_end, de) for ds, de in spans)
            if not has_overlap:
                if active_emphasis:
                    ass_lines.append(f"{line_reset}{l_text}")
                    active_emphasis = False
                else:
                    ass_lines.append(l_text)
                continue
            cur = l_start
            pieces: list[str] = []
            for ds, de in spans:
                ov_s = max(l_start, ds)
                ov_e = min(l_end, de)
                if ov_s >= ov_e:
                    continue
                if ov_s > cur:
                    prefix_tag = lead_tag if (cur == l_start and not active_emphasis) else reset_tag
                    pieces.append(f"{prefix_tag}{l_text[cur - l_start : ov_s - l_start]}")
                pieces.append(f"{on_tag}{l_text[ov_s - l_start : ov_e - l_start]}")
                active_emphasis = True
                cur = ov_e
            if cur < l_end:
                pieces.append(f"{reset_tag}{l_text[cur - l_start : l_end - l_start]}")
                active_emphasis = False
            ass_lines.append("".join(pieces))
        body = "\\N".join(ass_lines)
        start_str = _ass_time(float(cue["start_s"]))
        end_str = _ass_time(float(cue["end_s"]))
        return [f"Dialogue: 0,{start_str},{end_str},{style_name},,0,0,0,,{body}"]

    if kind == "pause":
        p1_body = "\\N".join(str(ln) for ln in expr["part1_lines"])
        p2_body = "\\N".join(str(ln) for ln in expr["part2_lines"])
        if any(c in (p1_body + p2_body) for c in "{}\r"):
            raise ContractError("ASS dialogue event cannot contain raw braces or CR")
        t0 = _ass_time(float(cue["start_s"]))
        t1 = _ass_time(float(expr["part1_end_s"]))
        t2 = _ass_time(float(expr["part2_start_s"]))
        t3 = _ass_time(float(cue["end_s"]))
        ev1_end = t2 if expr.get("pause_mode", "progressive_reveal") == "progressive_reveal" else t1
        return [
            f"Dialogue: 0,{t0},{ev1_end},{style_name},,0,0,0,,{p1_body}",
            f"Dialogue: 0,{t2},{t3},{style_name},,0,0,0,,{p2_body}",
        ]

    if kind == "tone":
        render_lines = list(cue.get("render_lines") or cue.get("lines") or [])
        tag_text = str(expr["tone_tag"])
        if any(c in tag_text for c in "{}\r") or any(any(c in ln for c in "{}\r") for ln in render_lines):
            raise ContractError("ASS dialogue event cannot contain raw braces or CR")
        tone_1c = _hex_rgb_to_ass_1c(expr.get("tag_color_hex", DEFAULT_TONE_HEX))
        if expr.get("placement") == "prefix_newline":
            body = f"{{\\1c{tone_1c}}}{tag_text}{{\\1c{base_1c}}}\\N" + "\\N".join(render_lines)
        else:
            first = f"{{\\1c{tone_1c}}}{tag_text} {{\\1c{base_1c}}}{render_lines[0]}"
            body = "\\N".join([first, *render_lines[1:]])
        start_str = _ass_time(float(cue["start_s"]))
        end_str = _ass_time(float(cue["end_s"]))
        return [f"Dialogue: 0,{start_str},{end_str},{style_name},,0,0,0,,{body}"]

    return None
