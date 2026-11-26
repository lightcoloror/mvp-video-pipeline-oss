"""Keep source text intact; wrap only at punctuation or supplied semantic breaks."""

from __future__ import annotations

import math
import re
import unicodedata

from .editing_presets import POLICY, number, reviewed
from .errors import ContractError
from .typography_gates import build_typography_gate


# Pause-only punctuation stripped by vertical spoken-caption best practice.
# Semantic/emotional/data marks (？ ！ 《》 “” "" [] numeric : . × %) are preserved.
_PAUSE_PUNCT_RE = re.compile(r"[。，、；]+")
_NON_NUMERIC_COLON_RE = re.compile(r"(?<!\d)[：:](?!\d)")


def units(text):
    return sum(1 if unicodedata.east_asian_width(c) in "WF" else 0.5 for c in text)


def clean_vertical_spoken_punctuation(text: str) -> str:
    """Apply vertical spoken-caption punctuation best practice.

    - Strips pause-only punctuation (。 ， 、 ； and non-numeric colons), replacing
      internal pauses with a single space for visual symmetry and line-width savings.
    - Preserves semantic/emotional/data punctuation (？ ！ 《》 numeric ratios like
      9:16, decimals like 1.14× / 94.44 GB, and sound annotations like [已消音]).
    """
    if not isinstance(text, str):
        raise ContractError("subtitle text must be a string")
    cleaned = _PAUSE_PUNCT_RE.sub(" ", text)
    cleaned = _NON_NUMERIC_COLON_RE.sub(" ", cleaned)
    # Normalize ASCII question/exclamation marks attached to CJK text cleanly and collapse whitespace
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    # Remove any stray spaces right before preserved closing punctuation
    cleaned = re.sub(r"\s+([？！?!》”\]])", r"\1", cleaned)
    cleaned = re.sub(r"([《“\[])\s+", r"\1", cleaned)
    cleaned = cleaned.strip(" ，。、；：")
    # Strip stray leading sentence-ending punctuation caused by ASR boundary cuts (e.g. "？先看写稿")
    return cleaned.lstrip(" ？?!！")


def _safe_word_cut(text: str, target_idx: int, protected_words: tuple[str, ...] = ()) -> int:
    """Pick a cut index near target_idx that never splits inside an ASCII token or protected CJK word."""
    n = len(text)
    if target_idx <= 1:
        return 1
    if target_idx >= n - 1:
        return n - 1

    # Prefer nearby space first
    sp = text.find(" ", max(2, target_idx - 3), min(n - 2, target_idx + 4))
    if sp != -1:
        return sp

    forbidden: set[int] = set()
    # Never split inside contiguous ASCII words/numbers
    for m in re.finditer(r"[A-Za-z0-9._:/×%-]+", text):
        for pos in range(m.start() + 1, m.end()):
            forbidden.add(pos)
    # Never split inside protected compound Chinese words
    for word in protected_words:
        if len(word) < 2:
            continue
        start = 0
        while True:
            idx = text.find(word, start)
            if idx == -1:
                break
            for pos in range(idx + 1, idx + len(word)):
                forbidden.add(pos)
            start = idx + 1

    if target_idx not in forbidden:
        return target_idx
    for delta in range(1, min(6, n)):
        if target_idx + delta < n - 1 and (target_idx + delta) not in forbidden:
            return target_idx + delta
        if target_idx - delta > 1 and (target_idx - delta) not in forbidden:
            return target_idx - delta
    return target_idx


def _balanced_split_text(
    text: str,
    max_units: float,
    min_units: float,
    protected_words: tuple[str, ...] = (),
) -> list[str]:
    """Split text into balanced chunks with <= max_units, no < min_units orphan tails, and whole-word breaks."""
    if not text:
        return []
    if units(text) <= max_units:
        return [text]

    tokens = [w for w in text.split(" ") if w]
    chunks: list[str] = []
    buf = ""
    for tok in tokens:
        cand = f"{buf} {tok}".strip() if buf else tok
        if units(cand) <= max_units:
            buf = cand
        else:
            if buf:
                chunks.append(buf)
            if units(tok) <= max_units:
                buf = tok
            else:
                rem = tok
                while units(rem) > max_units:
                    n_parts = max(2, math.ceil(units(rem) / max_units))
                    ideal = math.ceil(len(rem) / n_parts)
                    cut = _safe_word_cut(rem, ideal, protected_words)
                    piece = rem[:cut].strip()
                    if not piece:
                        break
                    chunks.append(piece)
                    rem = rem[cut:].strip()
                buf = rem
    if buf:
        chunks.append(buf)

    # Anti-orphan pass: eliminate trailing/leading fragments below min_units
    merged: list[str] = []
    for ch in chunks:
        if merged and units(ch) < min_units:
            comb = f"{merged[-1]} {ch}".strip()
            if units(comb) <= max_units + 2.0:
                merged[-1] = comb
            else:
                cut = _safe_word_cut(comb, len(comb) // 2, protected_words)
                merged[-1] = comb[:cut].strip()
                merged.append(comb[cut:].strip())
        else:
            merged.append(ch)

    if len(merged) >= 2 and units(merged[0]) < min_units:
        comb = f"{merged[0]} {merged[1]}".strip()
        if units(comb) <= max_units + 2.0:
            merged = [comb] + merged[2:]
        else:
            cut = _safe_word_cut(comb, len(comb) // 2, protected_words)
            merged = [comb[:cut].strip(), comb[cut:].strip()] + merged[2:]

    return [x.strip() for x in merged if x.strip()]


def split_vertical_spoken_cues(
    raw_cues: list[dict],
    *,
    max_units: float = 13.0,
    min_units: float = 4.0,
    min_duration_s: float = 0.48,
    max_merge_gap_s: float = 0.45,
    clean_punctuation: bool = True,
    protected_words: tuple[str, ...] = (),
) -> list[dict]:
    """Normalize timed spoken cues into orphan-free, anti-flash vertical captions.

    Each input cue dict expects `start_s`, `end_s`, `text`, and optional:
      - `char_timestamps`: list of (start_s, end_s) per spoken character for exact split timing
      - `no_merge`: bool (True prevents merging across special beats like censor beeps / speed gags)
    """
    if not protected_words:
        from .post_cut_qa import DEFAULT_PROTECTED_WORDS
        protected_words = DEFAULT_PROTECTED_WORDS

    expanded: list[dict] = []
    for cue in raw_cues:
        start_s = float(cue["start_s"])
        end_s = float(cue["end_s"])
        if end_s <= start_s:
            continue
        no_merge = bool(cue.get("no_merge", False))
        raw_text = str(cue.get("text", "")).strip()
        text = clean_vertical_spoken_punctuation(raw_text) if clean_punctuation else raw_text
        if not text:
            continue
        pieces = _balanced_split_text(text, max_units=max_units, min_units=min_units, protected_words=protected_words)
        if not pieces:
            continue

        char_ts = cue.get("char_timestamps")
        if len(pieces) == 1:
            expanded.append(
                {"start_s": round(start_s, 3), "end_s": round(end_s, 3), "text": pieces[0], "no_merge": no_merge}
            )
            continue

        # Project exact timestamps from char_timestamps when aligned, else proportional by non-space length
        nonspace_lens = [max(1, len(p.replace(" ", ""))) for p in pieces]
        total_ns = sum(nonspace_lens)
        if isinstance(char_ts, list) and len(char_ts) >= len(pieces):
            acc = 0
            n_ts = len(char_ts)
            for idx_p, (p, nl) in enumerate(zip(pieces, nonspace_lens)):
                i0 = min(n_ts - 1, int(round(acc * n_ts / total_ns)))
                acc += nl
                i1 = min(n_ts - 1, max(i0, int(round(acc * n_ts / total_ns)) - 1))
                ps = start_s if idx_p == 0 else max(start_s, float(char_ts[i0][0]))
                pe = end_s if idx_p == len(pieces) - 1 else min(end_s, float(char_ts[i1][1]))
                if pe <= ps + 0.04:
                    pe = min(end_s, ps + 0.15)
                expanded.append({"start_s": round(ps, 3), "end_s": round(pe, 3), "text": p, "no_merge": no_merge})
        else:
            cur = start_s
            for idx_p, (p, nl) in enumerate(zip(pieces, nonspace_lens)):
                nxt = end_s if idx_p == len(pieces) - 1 else cur + (end_s - start_s) * (nl / total_ns)
                expanded.append({"start_s": round(cur, 3), "end_s": round(nxt, 3), "text": p, "no_merge": no_merge})
                cur = nxt

    # Enforce strict chronology + anti-flash / anti-orphan merge pass
    merged: list[dict] = []
    for item in expanded:
        t0, t1, tx, nm = item["start_s"], item["end_s"], item["text"], item["no_merge"]
        if merged and t0 < merged[-1]["end_s"]:
            t0 = merged[-1]["end_s"]
        if t1 <= t0 + 0.04:
            continue
        is_short = (units(tx) < min_units) or ((t1 - t0) < min_duration_s)
        if (
            merged
            and is_short
            and not nm
            and not merged[-1]["no_merge"]
            and (t0 - merged[-1]["end_s"]) <= max_merge_gap_s
        ):
            prev = merged[-1]
            comb = f"{prev['text']} {tx}".strip()
            if units(comb) <= max_units + 2.0:
                merged[-1] = {"start_s": prev["start_s"], "end_s": round(t1, 3), "text": comb, "no_merge": False}
                continue
            mid = len(comb) // 2
            cut = _safe_word_cut(comb, mid, protected_words)
            p1, p2 = comb[:cut].strip(), comb[cut:].strip()
            tot_d = t1 - prev["start_s"]
            split_t = round(prev["start_s"] + tot_d * (units(p1) / max(1.0, units(p1) + units(p2))), 3)
            merged[-1] = {"start_s": prev["start_s"], "end_s": split_t, "text": p1, "no_merge": False}
            merged.append({"start_s": split_t, "end_s": round(t1, 3), "text": p2, "no_merge": False})
            continue
        merged.append({"start_s": round(t0, 3), "end_s": round(t1, 3), "text": tx, "no_merge": nm})

    # Cross-cue protected-word boundary repair pass (handles ASR sentence breaks inside compound words)
    for idx_m in range(len(merged) - 1):
        a, b = merged[idx_m], merged[idx_m + 1]
        if a["no_merge"] or b["no_merge"]:
            continue
        ta, tb = a["text"], b["text"]
        for word in protected_words:
            wlen = len(word)
            if wlen < 2:
                continue
            repaired = False
            for cut in range(1, wlen):
                if ta.endswith(word[:cut]) and tb.startswith(word[cut:]):
                    new_ta = ta[: -cut].rstrip()
                    new_tb = (word[:cut] + tb).lstrip()
                    if units(new_ta) >= min_units and units(new_tb) <= max_units + 3.0:
                        a["text"], b["text"] = new_ta, new_tb
                        ta, tb = new_ta, new_tb
                        repaired = True
                        break
                    alt_ta = (ta + word[cut:]).rstrip()
                    alt_tb = tb[wlen - cut :].lstrip()
                    if units(alt_tb) >= min_units and units(alt_ta) <= max_units + 3.0:
                        a["text"], b["text"] = alt_ta, alt_tb
                        ta, tb = alt_ta, alt_tb
                        repaired = True
                        break
            if repaired:
                break

    return [{"start_s": c["start_s"], "end_s": c["end_s"], "text": c["text"]} for c in merged]


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
        breaks.update(i + 1 for i, c in enumerate(text[:-1]) if c in "，。！？；、,.!?; ")
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
    # Canonical 1080-short-edge baseline; scale default from short edge so both
    # portrait and landscape stay proportional and never drop below the floor.
    short_edge = min(width, height)
    minimum = gate["roles"]["subtitle"] * short_edge / 1080
    actual = (
        round(short_edge * 0.055)
        if requested is None
        else number(requested, "subtitle font")
    )
    if actual < minimum:
        raise ContractError(
            "subtitle font below canonical scaled minimum; reflow instead"
        )
    return actual, gate["policy_sha256"]


def subtitle_render_preset(width, height, requested=None):
    """Expose the existing audio_timeline composition defaults to thin adapters.

    A font's nominal ASS size is not a measurement of its final Chinese glyphs;
    adapters may record a separately measured renderer size without shrinking
    this baseline or changing these margins.
    """
    font, policy_hash = subtitle_font(width, height, requested)
    return {
        "font_px": font,
        "typography_policy_sha256": policy_hash,
        "margin_l": round(width * .07),
        "margin_r": round(width * .07),
        "margin_v": round(height * .09),
        "alignment": 2,
        "max_line_units": min(POLICY["subtitles"]["max_chars_per_line"],
                              int(width * .86 // font)),
        "max_lines": POLICY["subtitles"]["max_lines"],
    }
