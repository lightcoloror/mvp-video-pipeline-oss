"""Canonical reviewed subtitle sequence, speaker/sound formatting, and exports.

Builds one single reviewed whole-video display sequence from either a reviewed
pack (`subtitle_cues` / `cues`) or binding segment subtitles. Distinguishes:
  - spoken_text: raw spoken words (or empty for non-speech cues)
  - display_text: single reviewed string including any offscreen speaker prefix
    and meaningful sound annotation (used for line-width, reading-speed, and
    reconciliation checks)
  - lines / render_lines / layout_breaks: explicit editorial or semantic wrap
    decisions, keeping lossless reconstruction between rendered lines and
    display_text for both CJK and space-delimited mixed/English text.

All canonical timestamps use the final whole-video clock. Segment-local timing
(`start_s - scene_start_s`) is projected ONLY when slicing for segment ASS.
"""
from __future__ import annotations

import re
from typing import Any

from .editing_presets import POLICY, number
from .errors import ContractError
from .subtitle_readability import subtitle_layout, units

SEQUENCE_SCHEMA = "video_creation_pipeline.reviewed_subtitle_sequence.v1"


def _clean_text(value: Any, label: str, *, allow_empty: bool = False) -> str:
    if value is None:
        if allow_empty:
            return ""
        raise ContractError(f"{label} must be text")
    if not isinstance(value, str) or any(c in value for c in "{}\\\r"):
        raise ContractError(f"{label} contains unsafe characters")
    stripped = value.strip()
    if not stripped and not allow_empty:
        raise ContractError(f"{label} must be nonempty text")
    return stripped


def _normalize_speaker_prefix(raw_prefix: Any) -> str:
    if raw_prefix is None:
        return ""
    text = _clean_text(raw_prefix, "speaker prefix", allow_empty=True)
    if not text:
        return ""
    label = text.rstrip("：:").strip()
    if not label:
        raise ContractError("speaker prefix label cannot be empty")
    return f"{label}："


def _normalize_sound_event(raw_sound: Any) -> str:
    if raw_sound is None:
        return ""
    text = _clean_text(raw_sound, "sound event", allow_empty=True)
    if not text:
        return ""
    inner = text.strip()
    if (inner.startswith("[") and inner.endswith("]")) or (
        inner.startswith("【") and inner.endswith("】")
    ):
        inner = inner[1:-1].strip()
    if not inner:
        raise ContractError("sound event annotation cannot be empty")
    return f"[{inner}]"


def _has_speaker_prefix(text: str, normalized_prefix: str) -> bool:
    label = normalized_prefix.rstrip("：:")
    return bool(
        re.match(rf"^\s*{re.escape(label)}\s*[：:]", text)
    )


def _check_no_duplicate_prefix(text: str, label: str = "") -> None:
    if label:
        pattern = rf"{re.escape(label)}\s*[：:]\s*{re.escape(label)}\s*[：:]"
        if re.search(pattern, text):
            raise ContractError("duplicate speaker prefix in subtitle display text")
    # Also catch any generic immediate doubled prefix like "采访者：采访者："
    m = re.match(r"^\s*([^\s：:]{1,20})\s*[：:]\s*([^\s：:]{1,20})\s*[：:]", text)
    if m and m.group(1) == m.group(2):
        raise ContractError("duplicate speaker prefix in subtitle display text")


def _has_sound_token(text: str, sound_token: str) -> bool:
    inner = sound_token.strip("[]")
    return f"[{inner}]" in text or f"【{inner}】" in text


def assemble_display_text(
    *,
    spoken_text: str,
    speaker_prefix: str = "",
    sound_token: str = "",
    sound_position: str = "prefix",
    explicit_display_text: str | None = None,
) -> str:
    """Build reviewed display text once, deduplicating speaker & sound tokens."""
    if explicit_display_text is not None:
        display = _clean_text(explicit_display_text, "display_text")
        _check_no_duplicate_prefix(
            display, speaker_prefix.rstrip("：:") if speaker_prefix else ""
        )
        return display

    body = spoken_text.strip()
    _check_no_duplicate_prefix(
        body, speaker_prefix.rstrip("：:") if speaker_prefix else ""
    )
    if sound_position not in ("prefix", "suffix"):
        raise ContractError("sound_position must be 'prefix' or 'suffix'")

    # Separate any existing speaker prefix before inserting sound token so
    # "采访者：原件送到了。" + "[门铃声]" becomes "采访者：[门铃声] 原件送到了。".
    existing_prefix = ""
    rest = body
    if speaker_prefix and _has_speaker_prefix(body, speaker_prefix):
        label = speaker_prefix.rstrip("：:")
        match = re.match(rf"^\s*({re.escape(label)})\s*[：:]\s*(.*)$", body, re.DOTALL)
        assert match is not None
        existing_prefix = f"{match.group(1)}："
        rest = match.group(2).strip()

    effective_prefix = existing_prefix or speaker_prefix
    if sound_token and not _has_sound_token(rest, sound_token):
        if not rest:
            rest = sound_token
        elif sound_position == "prefix":
            rest = f"{sound_token} {rest}"
        else:
            rest = f"{rest} {sound_token}"

    combined = f"{effective_prefix}{rest}" if effective_prefix else rest
    combined = combined.strip()
    if not combined:
        raise ContractError("subtitle requires spoken_text, display_text, or sound_event")
    _check_no_duplicate_prefix(
        combined, effective_prefix.rstrip("：:") if effective_prefix else ""
    )
    return combined


def _compute_render_lines(
    raw_lines: list[str],
) -> tuple[list[str], list[dict[str, Any]]]:
    """Strip trailing split whitespace for visual centering, recording the break."""
    if len(raw_lines) <= 1:
        return list(raw_lines), []
    render_lines: list[str] = []
    breaks: list[dict[str, Any]] = []
    for idx, line in enumerate(raw_lines):
        if idx < len(raw_lines) - 1:
            replaced_space = line.endswith(" ")
            cleaned = line.rstrip(" ") if replaced_space else line
            render_lines.append(cleaned)
            breaks.append({"after_line": idx, "replaced_space": replaced_space})
        else:
            render_lines.append(line)
    return render_lines, breaks


def reconstruct_display_text(
    lines: list[str] | tuple[str, ...],
    layout_breaks: list[dict[str, Any]] | None = None,
) -> str:
    """Losslessly reconstruct display_text from wrapped render lines."""
    if not lines:
        return ""
    if len(lines) == 1:
        return str(lines[0])
    if layout_breaks and len(layout_breaks) == len(lines) - 1:
        parts: list[str] = []
        for idx, line in enumerate(lines):
            parts.append(str(line))
            if idx < len(lines) - 1:
                if layout_breaks[idx].get("replaced_space"):
                    parts.append(" ")
        return "".join(parts)
    # Fallback when reconciling external lines without explicit layout_breaks:
    # insert a space ONLY between two ASCII alphanumeric/bracket word edges.
    result = str(lines[0])
    for nxt in lines[1:]:
        nxt_str = str(nxt)
        if result and nxt_str:
            left_ascii = result[-1].isascii() and (result[-1].isalnum() or result[-1] in "])")
            right_ascii = nxt_str[0].isascii() and (nxt_str[0].isalnum() or nxt_str[0] in "[(")
            if left_ascii and right_ascii:
                result += " " + nxt_str
            else:
                result += nxt_str
        else:
            result += nxt_str
    return result


def reconcile_observed_lines(
    observed_raw_text: str,
    expected_display_text: str,
    layout_breaks: list[dict[str, Any]] | None = None,
) -> tuple[bool, str]:
    """Compare observed ASS/SRT line-broken text against canonical display_text.

    Eliminates only registered layout line breaks so CJK wraps without spaces
    AND mixed CJK-English wraps with inter-word spaces both reconcile exact.
    """
    normalized_raw = observed_raw_text.replace("\\N", "\n").replace("\\n", "\n")
    obs_lines = normalized_raw.split("\n")
    expected_norm = " ".join(expected_display_text.split())
    if len(obs_lines) == 1:
        reconstructed = " ".join(obs_lines[0].split())
        return reconstructed == expected_norm, reconstructed
    candidates = set()
    if layout_breaks:
        candidates.add(" ".join(reconstruct_display_text(obs_lines, layout_breaks).split()))
    # Check both exact space-preserving join and direct join ONLY if layout_breaks absent
    if not layout_breaks:
        candidates.add(" ".join(reconstruct_display_text(obs_lines, None).split()))
        # Also allow space-at-boundary wrap if speaker/sound break inserted space at newline
        candidates.add(" ".join(line.strip() for line in obs_lines))
        candidates.add("".join(line.strip() for line in obs_lines))
    matched = expected_norm in candidates
    best = next(iter(candidates)) if candidates else ""
    return matched, best


def extract_cues_from_pack(pack: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract ordered raw cues from reviewed pack v1 or legacy cue list."""
    requests = pack.get("requests")
    if isinstance(requests, list) and requests:
        cues = [c for row in requests for c in row.get("subtitle_cues", [])]
        if cues:
            return cues
    cues = pack.get("cues")
    if isinstance(cues, list):
        return list(cues)
    return []


def build_reviewed_subtitle_sequence(
    raw_cues: list[dict[str, Any]],
    *,
    max_line_units: int | float | None = None,
    total_duration_s: float | None = None,
) -> dict[str, Any]:
    """Normalize reviewed cues into one canonical whole-video display sequence."""
    if not isinstance(raw_cues, list) or not raw_cues:
        raise ContractError("explicit subtitles required")

    config = POLICY["subtitles"]
    normalized: list[dict[str, Any]] = []
    previous_end = 0.0
    seen_ids: set[str] = set()

    for idx, raw in enumerate(raw_cues, 1):
        if not isinstance(raw, dict):
            raise ContractError("subtitle cue must be an object")
        start_val = raw["start_seconds"] if "start_seconds" in raw else raw.get("start_s")
        end_val = raw["end_seconds"] if "end_seconds" in raw else raw.get("end_s")
        start_s = float(number(start_val, "subtitle start"))
        end_s = float(number(end_val, "subtitle end"))
        if start_s < 0 or end_s <= start_s:
            raise ContractError("subtitle timing must satisfy 0 <= start < end")
        if start_s < previous_end - 1e-6:
            raise ContractError("subtitles overlap or are out of chronological order")
        if total_duration_s is not None and end_s > float(total_duration_s) + 1e-6:
            raise ContractError("subtitle exceeds total timeline duration")
        previous_end = end_s

        cue_id = str(raw.get("cue_id") or f"cue-{idx:03d}").strip()
        if not cue_id or cue_id in seen_ids:
            raise ContractError("duplicate or empty subtitle cue_id")
        seen_ids.add(cue_id)

        raw_spoken = raw.get("spoken_text")
        if raw_spoken is None:
            raw_spoken = raw.get("text", "")
        spoken_text = _clean_text(raw_spoken, "spoken_text", allow_empty=True)

        explicit_prefix = raw.get("speaker_prefix")
        offscreen = bool(raw.get("offscreen_speaker") or raw.get("show_speaker_prefix"))
        speaker = raw.get("speaker")
        if explicit_prefix is not None:
            speaker_prefix = _normalize_speaker_prefix(explicit_prefix)
        elif offscreen and isinstance(speaker, str) and speaker.strip():
            speaker_prefix = _normalize_speaker_prefix(speaker)
        else:
            speaker_prefix = ""

        sound_raw = raw.get("sound_event") if raw.get("sound_event") is not None else raw.get("non_speech")
        sound_token = _normalize_sound_event(sound_raw)
        sound_position = str(raw.get("sound_position") or "prefix")

        explicit_display = raw.get("display_text")
        display_text = assemble_display_text(
            spoken_text=spoken_text,
            speaker_prefix=speaker_prefix,
            sound_token=sound_token,
            sound_position=sound_position,
            explicit_display_text=explicit_display,
        )

        # Protect speaker prefix and sound token from mid-token line breaks.
        protected_terms = list(raw.get("protected_terms") or ())
        if speaker_prefix:
            lbl = speaker_prefix.rstrip("：:")
            for variant in (speaker_prefix, f"{lbl}:"):
                if variant in display_text and variant not in protected_terms:
                    protected_terms.append(variant)
        if sound_token and sound_token in display_text and sound_token not in protected_terms:
            protected_terms.append(sound_token)

        duration_s = end_s - start_s
        layout = subtitle_layout(
            display_text,
            semantic_breaks=tuple(raw.get("semantic_breaks") or ()),
            protected_terms=tuple(protected_terms),
            duration_s=duration_s,
            review=raw.get("readability_review"),
            max_line_units=max_line_units,
        )
        raw_lines = list(layout["lines"])
        render_lines, layout_breaks = _compute_render_lines(raw_lines)
        clean_units = units(display_text.replace("\n", ""))
        cps = round(clean_units / duration_s, 3)
        if cps > config["reading_speed_warning_cps"] and not raw.get("readability_review"):
            raise ContractError("subtitle reading speed exceeds threshold without review")

        normalized.append(
            {
                "cue_id": cue_id,
                "start_s": round(start_s, 6),
                "end_s": round(end_s, 6),
                "duration_s": round(duration_s, 6),
                "spoken_text": spoken_text,
                "display_text": display_text.replace("\n", ""),
                "speaker": str(speaker) if speaker else None,
                "speaker_prefix": speaker_prefix or None,
                "offscreen_speaker": offscreen,
                "sound_event": sound_token or None,
                "protected_terms": protected_terms,
                "lines": raw_lines,
                "render_lines": render_lines,
                "layout_breaks": layout_breaks,
                "units": clean_units,
                "reading_speed_cps": cps,
                "warnings": list(layout["warnings"]),
                "motion": str(raw.get("motion") or "none"),
                "style_preset": raw.get("style_preset"),
            }
        )

    return {
        "schema": SEQUENCE_SCHEMA,
        "cue_count": len(normalized),
        "total_duration_s": round(float(total_duration_s), 6)
        if total_duration_s is not None
        else normalized[-1]["end_s"],
        "cues": normalized,
    }


def slice_sequence_for_scene(
    sequence: dict[str, Any],
    *,
    scene_start_s: float,
    scene_end_s: float,
) -> list[dict[str, Any]]:
    """Project whole-video sequence cues into segment-local ASS timestamps."""
    a = float(scene_start_s)
    b = float(scene_end_s)
    if a < 0 or b <= a:
        raise ContractError("invalid scene window for subtitle projection")
    sliced: list[dict[str, Any]] = []
    for cue in sequence.get("cues", []):
        cs, ce = float(cue["start_s"]), float(cue["end_s"])
        if ce <= a + 1e-6 or cs >= b - 1e-6:
            continue
        if cs < a - 1e-6 or ce > b + 1e-6:
            raise ContractError("subtitle crosses scene boundary")
        projected_cue = {
            **cue,
            "global_start_s": cs,
            "global_end_s": ce,
            "start_s": round(max(0.0, cs - a), 6),
            "end_s": round(min(b - a, ce - a), 6),
        }
        if isinstance(cue.get("expressive"), dict):
            from .subtitle_expression import project_expressive_for_scene

            projected_cue["expressive"] = project_expressive_for_scene(
                cue["expressive"], scene_start_s=a, scene_end_s=b
            )
        sliced.append(projected_cue)
    if not sliced:
        raise ContractError("explicit subtitles required")
    return sliced


def _format_srt_timestamp(seconds: float) -> str:
    ms_total = max(0, round(float(seconds) * 1000))
    ms = ms_total % 1000
    s_total = ms_total // 1000
    sec = s_total % 60
    m_total = s_total // 60
    minute = m_total % 60
    hour = m_total // 60
    return f"{hour:02d}:{minute:02d}:{sec:02d},{ms:03d}"


def _format_vtt_timestamp(seconds: float) -> str:
    return _format_srt_timestamp(seconds).replace(",", ".")


def export_whole_srt(sequence: dict[str, Any]) -> str:
    """Export final-clock UTF-8 whole-video SRT from a reviewed sequence."""
    blocks: list[str] = []
    for idx, cue in enumerate(sequence.get("cues", []), 1):
        start_ts = _format_srt_timestamp(cue["start_s"])
        end_ts = _format_srt_timestamp(cue["end_s"])
        text_body = "\n".join(cue["render_lines"])
        blocks.append(f"{idx}\n{start_ts} --> {end_ts}\n{text_body}\n")
    return "\n".join(blocks)


def export_whole_vtt(sequence: dict[str, Any]) -> str:
    """Export final-clock UTF-8 WebVTT from the exact same reviewed sequence."""
    blocks: list[str] = ["WEBVTT\n"]
    for idx, cue in enumerate(sequence.get("cues", []), 1):
        start_ts = _format_vtt_timestamp(cue["start_s"])
        end_ts = _format_vtt_timestamp(cue["end_s"])
        text_body = "\n".join(cue["render_lines"])
        blocks.append(f"{cue.get('cue_id', idx)}\n{start_ts} --> {end_ts}\n{text_body}\n")
    return "\n".join(blocks)
