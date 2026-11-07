"""Keep source text intact; wrap only at punctuation or supplied semantic breaks."""

from __future__ import annotations

import unicodedata

from .editing_presets import POLICY, number, reviewed
from .errors import ContractError
from .typography_gates import build_typography_gate


def units(text):
    return sum(1 if unicodedata.east_asian_width(c) in "WF" else 0.5 for c in text)


def subtitle_layout(
    text,
    *,
    semantic_breaks=(),
    protected_terms=(),
    duration_s=None,
    review=None,
    max_line_units=None,
):
    if (
        not isinstance(text, str)
        or not text.strip()
        or any(c in text for c in "{}\\\r")
    ):
        raise ContractError("subtitle requires safe plain text")
    config = POLICY["subtitles"]
    if not isinstance(protected_terms, (list, tuple)) or any(
        not isinstance(t, str) or not t for t in protected_terms
    ):
        raise ContractError("protected subtitle terms must be nonempty text")
    # An explicit line break is already an editorial decision, not a text deletion.
    original = text.replace("\n", "")
    lines = text.split("\n")
    limit = (
        min(config["max_chars_per_line"], number(max_line_units, "subtitle line width"))
        if max_line_units is not None
        else config["max_chars_per_line"]
    )
    if limit <= 0:
        raise ContractError("subtitle line width must be positive")
    if len(lines) == 1 and units(text) > limit:
        breaks = set(semantic_breaks)
        breaks.update(i + 1 for i, c in enumerate(text) if c in "，。！？；、,.!?; ")
        possible = []
        for i in breaks:
            if not isinstance(i, int) or isinstance(i, bool) or not 0 < i < len(text):
                raise ContractError("invalid semantic subtitle break")
            if units(text[:i]) > limit or units(text[i:]) > limit:
                continue
            if any(
                any(
                    start < i < start + len(term)
                    for start in range(len(text))
                    if text.startswith(term, start)
                )
                for term in protected_terms
            ):
                continue
            possible.append(i)
        if not possible:
            raise ContractError(
                "subtitle needs shorter timed cues or explicit semantic breaks; font will not shrink"
            )
        split = min(possible, key=lambda i: abs(units(text[:i]) - units(text[i:])))
        lines = [text[:split], text[split:]]
    if len(lines) > config["max_lines"] or any(
        not line.strip() or units(line) > limit for line in lines
    ):
        raise ContractError(
            "subtitle exceeds two-line layout; split timed cues instead of shrinking"
        )
    if "".join(lines) != original:
        raise ContractError("subtitle layout changed source text")
    for term in protected_terms:
        if term in original and not any(term in line for line in lines):
            raise ContractError("subtitle split divides a protected term or number")
    warnings = []
    if duration_s is not None:
        duration = number(duration_s, "subtitle duration")
        if duration <= 0:
            raise ContractError("subtitle duration must be positive")
        if duration < config["minimum_duration_warning_s"]:
            warnings.append("short_display")
        if units(original) / duration > config["reading_speed_warning_cps"]:
            warnings.append("fast_reading")
        if warnings:
            reviewed(review, ["readability_checked"], "subtitle reading speed")
    return {
        "lines": lines,
        "text_preserved": True,
        "warnings": warnings,
        "font_shrunk": False,
    }


def subtitle_font(width, height, requested=None):
    gate = build_typography_gate("semantic_motion_1080x1920", width=1080, height=1920)
    # Canonical 1080-short-edge baseline; keep the existing larger width-based choice.
    minimum = gate["roles"]["subtitle"] * min(width, height) / 1080
    actual = (
        round(width * 0.055)
        if requested is None
        else number(requested, "subtitle font")
    )
    if actual < minimum:
        raise ContractError(
            "subtitle font below canonical scaled minimum; reflow instead"
        )
    return actual, gate["policy_sha256"]
