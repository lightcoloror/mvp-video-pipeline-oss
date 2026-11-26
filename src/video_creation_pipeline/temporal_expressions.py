"""Exact, data-only word-relative windows shared by authoring consumers.

No timing is inferred. Seconds and frames retain their authored units; frame
projection rounds ties toward the later frame. Outputs remain candidate data.
"""
from fractions import Fraction
from .errors import ContractError


def rational(value):
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ContractError("time value must be a finite decimal or rational")
    try:
        return Fraction(str(value))
    except (ValueError, ZeroDivisionError, OverflowError) as exc:
        raise ContractError("invalid time value") from exc


def quantity(value, fps):
    if not isinstance(value, dict) or set(value) != {"value", "unit"}:
        raise ContractError("time quantity needs value and unit")
    amount = rational(value["value"])
    if value["unit"] == "seconds":
        return amount
    if value["unit"] == "frames":
        return amount / fps
    raise ContractError("time unit must be seconds or frames")


def resolve_window(expression, words, *, fps, recording_start_s, recording_end_s):
    """Resolve at/for, until/for or span into one recording-local window."""
    fps = rational(fps)
    if fps <= 0:
        raise ContractError("fps must be positive")
    origin, limit = rational(recording_start_s), rational(recording_end_s)

    def anchor(value):
        if not isinstance(value, dict) or not {"word_id", "boundary"} <= set(value) or set(value) - {"word_id", "boundary", "offset"}:
            raise ContractError("anchor needs word_id, boundary and optional offset")
        word = words.get(value["word_id"])
        if word is None or value["boundary"] not in {"start", "end"}:
            raise ContractError("unknown word anchor")
        if rational(word["start_ms"]) / 1000 < origin or rational(word["end_ms"]) / 1000 > limit:
            raise ContractError("anchor word outside recording span")
        return rational(word[value["boundary"] + "_ms"]) / 1000 + (
            quantity(value["offset"], fps) if "offset" in value else 0)

    if not isinstance(expression, dict):
        raise ContractError("temporal expression must be an object")
    kind = expression.get("kind")
    if kind in {"at_for", "until_for"} and set(expression) == {"kind", "anchor", "duration"}:
        point = anchor(expression["anchor"])
        length = quantity(expression["duration"], fps)
        start, end = (point, point + length) if kind == "at_for" else (point - length, point)
    elif kind == "span" and set(expression) == {"kind", "start", "end"}:
        start, end = anchor(expression["start"]), anchor(expression["end"])
    else:
        raise ContractError("unsupported temporal expression shape")
    if not origin <= start < end <= limit:
        raise ContractError("temporal window exceeds recording span or is empty")
    start, end = start - origin, end - origin
    def frame(t):
        x = t * fps + Fraction(1, 2)
        return x.numerator // x.denominator
    if frame(end) <= frame(start):
        raise ContractError("temporal window collapses at output frame rate")
    return {"start_s": float(start), "end_s": float(end),
            "start_exact": str(start), "end_exact": str(end),
            "start_frame": frame(start), "end_frame": frame(end)}
