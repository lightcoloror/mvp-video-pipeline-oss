"""Post-cut production QA gates: semantic A-roll anchor alignment, subtitle typo &
whole-word audit, retake-duplicate detection, 25ms audio cut-edge tail-swallow check,
and B-roll visual role validation.
"""

from __future__ import annotations

import math
import re
from difflib import SequenceMatcher
from typing import Any

from .errors import ContractError
from .subtitle_readability import units

# Common Chinese compound words that must never be split across two consecutive captions.
DEFAULT_PROTECTED_WORDS: tuple[str, ...] = (
    "事实", "只能", "痛点", "折磨", "核心", "字幕", "语音识别", "发视频", "置顶评论",
    "举例", "返工", "卡壳", "底账", "审校", "提词器", "重录", "时间轴", "花字", "动效",
    "锚定", "进度条", "亲手精调", "剪映", "远控", "双语", "全貌", "横屏", "竖屏",
    "构图", "降噪", "语速", "音量", "二维码", "消音", "打码", "逐帧", "漏帧",
    "封面", "比例", "圆角", "门禁", "发布", "辅助页", "风控", "卡死", "短板",
    "硬盘", "模型", "缓存", "压缩包", "重建", "网关", "助手", "冷启动", "复盘",
    "杂务", "阻力", "翻车", "合集", "外链", "子集", "脱敏", "编排", "侧边栏",
    "出门", "发之前", "模块", "自动化", "稳妥", "腾地方", "氛围",
    "明白", "镜头", "复制", "产物", "弹窗", "各种各样",
    "项目", "牵一发动全身", "跑通过", "活动", "屏幕", "文件路径", "各个", "或者", "每一个", "每个", "六七个", "示例频道",
)

# High-frequency SenseVoice / ASR Chinese homophone errors in creator workflows.
DEFAULT_HOMOPHONE_WATCHLIST: dict[str, str] = {
    "唱歌": "示例",
    "静静的边学": "精进的《边做边学",
    "边学边做之美": "《边做边学·自媒体》",
    "鞭甲回执": "编假回执",
    "边假回": "编假回执",
    "跳步漏布": "跳步漏步",
    "照搬就文档": "照搬旧文档",
    "翻车盒子": "翻车合集",
    "托敏": "脱敏",
    "可好通自己": "可跑通子集",
    "可跑通自己": "可跑通子集",
    "卡格": "卡壳",
    "怕反工": "怕返工",
    "切重物": "切重录",
    "毛定": "锚定",
    "锚腻": "锚定",
    "新手金条": "亲手精调",
    "新手精条": "亲手精调",
    "编程入手": "编程助手",
    "事实题账": "事实底账",
    "事实抵账": "事实底账",
    "写稿审教": "写稿审校",
    "胎拗口": "太拗口",
    "气孔": "气口",
    "防漏发": "防漏帧",
    "防止漏发": "防止漏帧",
    "防止漏增": "防止漏帧",
    "视频耗": "视频号",
    "快手无设": "快手不设",
    "资源了": "资源门禁",
    "暂时不放": "暂不放行",
    "暂不放行心": "暂不放行",
    "两盘备份": "清过一次两G的废缓存",
    "横屏改触屏": "横屏改竖屏",
    "房间话": "画面里晃动",
    "推动间": "在中间",
    "按分为": "按氛围",
    "BDM": "BGM",
    "物理原角": "物理圆角",
    "这段话预填": "自动化预填",
    "又快又温好": "又快又稳妥",
    "清文件层地方": "清文件腾地方",
    "替代文块": "替代模块",
    "和刚和": "和",
    "点印": "剪映",
    "灯界面": "真界面",
    "导出成片辑": "导出成片前",
    "项木": "项目",
    "反 的压缩包": "返工的压缩包",
    "尽历": "精进",
    "他还会把画面": "它还会把画面",
}


def _norm_zh(text: str) -> str:
    return re.sub(r"[\s，。！？、；：,.!?;:\-—《》\"'“”\[\]()（）]+", "", text).lower()


def find_phrase_window_in_asr(
    asr_sentences: list[dict[str, Any]],
    phrase: str,
    *,
    min_search_s: float = 0.0,
    min_ratio: float = 0.60,
) -> tuple[float, float, float]:
    """Locate a spoken anchor phrase inside locked post-cut ASR sentences.

    Returns (start_s, end_s, match_score) using character-level timestamps when present.
    Raises ContractError when no confident match exists after `min_search_s`.
    """
    target = _norm_zh(phrase)
    if not target:
        raise ContractError("anchor phrase cannot be empty")

    # Build flat spoken-character stream with per-char timestamps from locked ASR
    chars: list[str] = []
    char_ts: list[tuple[float, float]] = []
    for sent in asr_sentences:
        st = float(sent["start_s"])
        en = float(sent["end_s"])
        if en < min_search_s:
            continue
        raw = str(sent.get("text", ""))
        norm_chars = [c for c in _norm_zh(raw) if c]
        if not norm_chars:
            continue
        ts = sent.get("char_timestamps")
        n = len(norm_chars)
        for idx, ch in enumerate(norm_chars):
            if isinstance(ts, list) and len(ts) >= n:
                i_ts = min(len(ts) - 1, int(round(idx * len(ts) / n)))
                c0, c1 = float(ts[i_ts][0]), float(ts[i_ts][1])
            else:
                c0 = st + (en - st) * (idx / n)
                c1 = st + (en - st) * ((idx + 1) / n)
            if c1 >= min_search_s:
                chars.append(ch)
                char_ts.append((c0, c1))

    stream = "".join(chars)
    tlen = len(target)
    if len(stream) < max(2, tlen // 2):
        raise ContractError(f"locked ASR stream too short to resolve anchor '{phrase}'")

    # Exact substring hit first
    pos = stream.find(target)
    if pos != -1:
        return round(char_ts[pos][0], 3), round(char_ts[pos + tlen - 1][1], 3), 1.0

    # Sliding-window fuzzy match
    best_score = 0.0
    best_span: tuple[int, int] | None = None
    win = tlen
    for i in range(0, max(1, len(stream) - win + 1)):
        cand = stream[i : i + win]
        score = SequenceMatcher(None, target, cand).ratio()
        if score > best_score:
            best_score = score
            best_span = (i, min(len(stream) - 1, i + win - 1))

    if best_span is None or best_score < min_ratio:
        raise ContractError(
            f"failed to resolve semantic anchor '{phrase}' after {min_search_s:.2f}s (best={best_score:.2f})"
        )
    return round(char_ts[best_span[0]][0], 3), round(char_ts[best_span[1]][1], 3), round(best_score, 3)


def realign_overlays_to_locked_asr(
    overlay_specs: list[dict[str, Any]],
    asr_sentences: list[dict[str, Any]],
    *,
    total_duration_s: float,
) -> list[dict[str, Any]]:
    """Resolve every B-roll / motion / HUD overlay window strictly from locked post-cut ASR.

    Each spec requires:
      - `overlay_id`: unique identifier
      - `start_anchor`: spoken phrase marking overlay start
      - `end_anchor`: spoken phrase marking overlay end (or `duration_s` if fixed)
    Optional: `start_pad_s`, `end_pad_s`, `min_search_s`, `track` (non-overlapping tracks checked).
    Fails closed if any anchor is missing or inverted.
    """
    resolved: list[dict[str, Any]] = []
    last_by_track: dict[str, float] = {}

    for spec in overlay_specs:
        oid = str(spec["overlay_id"])
        min_s = float(spec.get("min_search_s", 0.0))
        s0, s1, sc_s = find_phrase_window_in_asr(asr_sentences, spec["start_anchor"], min_search_s=min_s)
        start_s = max(0.0, s0 + float(spec.get("start_pad_s", 0.0)))

        if "end_anchor" in spec and spec["end_anchor"]:
            e0, e1, sc_e = find_phrase_window_in_asr(asr_sentences, spec["end_anchor"], min_search_s=max(min_s, s0))
            end_s = min(total_duration_s, e1 + float(spec.get("end_pad_s", 0.0)))
        elif "duration_s" in spec:
            sc_e = 1.0
            end_s = min(total_duration_s, start_s + float(spec["duration_s"]))
        else:
            raise ContractError(f"overlay '{oid}' requires end_anchor or duration_s")

        if end_s <= start_s + 0.15:
            raise ContractError(
                f"overlay '{oid}' resolved inverted/empty window ({start_s:.2f}s..{end_s:.2f}s)"
            )

        track = str(spec.get("track", "default"))
        if spec.get("enforce_non_overlap", False) and track in last_by_track:
            if start_s < last_by_track[track] - 0.05:
                raise ContractError(
                    f"overlay '{oid}' overlaps previous item on track '{track}' ({start_s:.2f}s < {last_by_track[track]:.2f}s)"
                )
        last_by_track[track] = end_s

        item = dict(spec)
        item["resolved_start_s"] = round(start_s, 3)
        item["resolved_end_s"] = round(end_s, 3)
        item["anchor_scores"] = {"start": sc_s, "end": sc_e}
        resolved.append(item)

    return resolved


def detect_retake_duplicates(
    asr_sentences: list[dict[str, Any]],
    *,
    min_ngram: int = 4,
    window_chars: int = 24,
) -> list[dict[str, Any]]:
    """Scan locked post-cut ASR text for adjacent repeated phrases indicating unedited retakes."""
    full_chars: list[tuple[str, float]] = []
    for sent in asr_sentences:
        st = float(sent["start_s"])
        en = float(sent["end_s"])
        norm = _norm_zh(str(sent.get("text", "")))
        n = max(1, len(norm))
        for i, ch in enumerate(norm):
            full_chars.append((ch, st + (en - st) * (i / n)))

    stream = "".join(c for c, _ in full_chars)
    findings: list[dict[str, Any]] = []
    i = 0
    while i < len(stream) - min_ngram:
        matched = False
        for n in range(min(10, len(stream) - i), min_ngram - 1, -1):
            sub = stream[i : i + n]
            pos = stream.find(sub, i + n, min(len(stream), i + n + window_chars))
            if pos != -1:
                findings.append(
                    {
                        "phrase": sub,
                        "first_s": round(full_chars[i][1], 2),
                        "second_s": round(full_chars[pos][1], 2),
                        "context": stream[max(0, i - 4) : min(len(stream), pos + n + 4)],
                    }
                )
                i = pos + n
                matched = True
                break
        if not matched:
            i += 1
    return findings


def audit_subtitles_against_script(
    cues: list[dict[str, Any]],
    *,
    script_text: str = "",
    homophone_watchlist: dict[str, str] | None = None,
    protected_words: tuple[str, ...] = DEFAULT_PROTECTED_WORDS,
    max_units: float = 15.0,
    min_units: float = 4.0,
    min_duration_s: float = 0.40,
) -> dict[str, Any]:
    """Fail-closed pre-burn subtitle audit gate: typos, split words, pause punct, orphans, flashes."""
    watchlist = dict(DEFAULT_HOMOPHONE_WATCHLIST)
    if homophone_watchlist:
        watchlist.update(homophone_watchlist)

    issues: list[dict[str, Any]] = []
    norm_script = _norm_zh(script_text) if script_text else ""

    for idx, cue in enumerate(cues, 1):
        tx = str(cue.get("text", ""))
        st = float(cue["start_s"])
        en = float(cue["end_s"])
        dur = en - st

        # 1. Pause punctuation & stray leading sentence-ending punctuation check
        for bad_p in ("。", "，", "、", "；"):
            if bad_p in tx:
                issues.append({"cue": idx, "time_s": st, "kind": "pause_punctuation", "detail": f"found '{bad_p}' in '{tx}'"})
        if tx and tx[0] in "？?!！。，、；：":
            issues.append({"cue": idx, "time_s": st, "kind": "leading_punctuation", "detail": f"stray leading '{tx[0]}' in '{tx}'"})

        # 2. Orphan / flash / overflow checks
        u = units(tx)
        if u < min_units:
            issues.append({"cue": idx, "time_s": st, "kind": "orphan_short_cue", "detail": f"'{tx}' ({u}u < {min_units}u)"})
        if u > max_units:
            issues.append({"cue": idx, "time_s": st, "kind": "line_too_wide", "detail": f"'{tx}' ({u}u > {max_units}u)"})
        if dur < min_duration_s:
            issues.append({"cue": idx, "time_s": st, "kind": "flash_short_duration", "detail": f"'{tx}' ({dur:.3f}s < {min_duration_s}s)"})

        # 3. Homophone typo watchlist check
        for bad, expected in watchlist.items():
            if bad in tx:
                # Only skip if the exact bad token legitimately appears in the locked script
                if norm_script and _norm_zh(bad) in norm_script:
                    continue
                issues.append({"cue": idx, "time_s": st, "kind": "homophone_typo", "detail": f"'{bad}' -> '{expected}' in '{tx}'"})

        # 4. Cross-cue split protected word check
        if idx < len(cues):
            nxt_tx = str(cues[idx].get("text", ""))
            boundary_window = tx[-4:] + nxt_tx[:4]
            for word in protected_words:
                wlen = len(word)
                if wlen < 2:
                    continue
                for cut in range(1, wlen):
                    if tx.endswith(word[:cut]) and nxt_tx.startswith(word[cut:]):
                        issues.append(
                            {
                                "cue": idx,
                                "time_s": en,
                                "kind": "split_compound_word",
                                "detail": f"'{word}' split across '{tx}' | '{nxt_tx}'",
                            }
                        )

    return {
        "schema": "video_creation_pipeline.subtitle_wording_gate.v1",
        "cue_count": len(cues),
        "issue_count": len(issues),
        "passed": len(issues) == 0,
        "issues": issues,
    }


def audit_audio_cut_edges(
    samples_mono_f32: Any,
    sample_rate: int,
    cut_intervals: list[tuple[float, float]],
    *,
    edge_ms: float = 25.0,
    max_edge_db: float = -40.0,
) -> dict[str, Any]:
    """Verify every internal cut start/end lands in true silence (< max_edge_db) to prevent tail-swallowing."""
    import numpy as np

    n_samples = len(samples_mono_f32)
    win = max(1, int(round(sample_rate * edge_ms / 1000.0)))
    hot_edges: list[dict[str, Any]] = []

    def _edge_db(center_s: float, side: str) -> float:
        idx = int(round(center_s * sample_rate))
        if side == "left":
            a, b = max(0, idx - win), min(n_samples, idx)
        else:
            a, b = max(0, idx), min(n_samples, idx + win)
        if b <= a:
            return -90.0
        seg = samples_mono_f32[a:b]
        rms = float(np.sqrt(np.mean(np.square(seg))))
        return 20.0 * math.log10(max(rms, 1e-6))

    for idx_i, (s0, s1) in enumerate(cut_intervals):
        db_in = _edge_db(s0, "right")
        db_out = _edge_db(s1, "left")
        if db_in > max_edge_db:
            hot_edges.append({"clip_idx": idx_i, "edge": "head", "source_s": s0, "rms_db": round(db_in, 1)})
        if db_out > max_edge_db:
            hot_edges.append({"clip_idx": idx_i, "edge": "tail", "source_s": s1, "rms_db": round(db_out, 1)})

    return {
        "schema": "video_creation_pipeline.audio_cut_edge_gate.v1",
        "clip_count": len(cut_intervals),
        "hot_edge_count": len(hot_edges),
        "passed": len(hot_edges) == 0,
        "hot_edges": hot_edges,
    }


def validate_broll_asset_role(
    image_bgr: Any,
    *,
    role: str,
    max_face_area_ratio_for_evidence: float = 0.08,
) -> dict[str, Any]:
    """Fail-closed check preventing talking-head portrait frames from entering evidence/chart B-roll slots."""
    if role not in ("evidence_document", "chart", "ui_demo", "portrait"):
        raise ContractError(f"unsupported broll role '{role}'")
    if role == "portrait":
        return {"role": role, "passed": True, "face_area_ratio": None}

    import cv2

    h, w = image_bgr.shape[:2]
    gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)
    cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    faces = cascade.detectMultiScale(gray, 1.12, 5, minSize=(48, 48))
    max_ratio = 0.0
    for (_, _, fw, fh) in faces:
        max_ratio = max(max_ratio, float(fw * fh) / max(1.0, float(w * h)))

    passed = True if role == "ui_demo" else (max_ratio <= max_face_area_ratio_for_evidence)
    return {
        "role": role,
        "face_area_ratio": round(max_ratio, 4),
        "max_allowed": max_face_area_ratio_for_evidence,
        "passed": passed,
    }


_ZH_DIGIT_MAP = str.maketrans({"两": "2", "二": "2", "三": "3", "四": "4", "五": "5", "六": "6", "七": "7", "八": "8", "九": "9"})


def _normalize_entity_text(text: str) -> str:
    """Normalize Chinese numerals attached to units (e.g. 两G -> 2g, 四套 -> 4套) for fact comparison."""
    t = re.sub(r"([两二三四五六七八九])\s*([GgMmKkTt][Bb]?|个|套|万|亿|倍|次|帧|秒|分|小时|平台|比例)", lambda m: m.group(1).translate(_ZH_DIGIT_MAP) + m.group(2), text)
    return t


def extract_fact_entities(text: str) -> dict[str, list[str]]:
    """Extract numeric/unit/ratio/proper-ASCII fact entities from script or caption text."""
    norm = _normalize_entity_text(text)
    capacities = sorted({
        re.sub(r"\s+", "", m.group(0)).upper().replace("GB", "G")
        for m in re.finditer(r"(?<![0-9])\d+(?:\.\d+)?\s*(?:GB|MB|TB|KB|G|M)(?![A-Za-z])", norm, flags=re.IGNORECASE)
    })
    ratios = sorted({
        m.group(0)
        for m in re.finditer(r"(?<!\d)\d{1,2}:\d{1,2}(?!\d)", norm)
    })
    ascii_terms = sorted({
        m.group(0).upper()
        for m in re.finditer(r"(?<![A-Za-z0-9])[A-Za-z][A-Za-z0-9_-]{1,24}(?![A-Za-z0-9])", norm)
        if m.group(0).upper() not in {"GB", "MB", "TB", "KB"}
    })
    return {
        "capacities": capacities,
        "ratios": ratios,
        "ascii_terms": ascii_terms,
    }


def audit_script_entities_and_trigrams(
    cues: list[dict[str, Any]],
    *,
    script_text: str,
    min_trigram_misses: int = 2,
    ignore_cue_substrings: tuple[str, ...] = ("[已消音]",),
    ignore_missing_entities: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Gate 6: Compare final cues/ASR against locked script for numeric/fact entity drift and 3-gram homophone anomalies."""
    if not script_text or not script_text.strip():
        raise ContractError("script_text is required for entity and trigram audit")

    joined_captions = " ".join(str(c.get("text", "")) for c in cues)
    script_ents = extract_fact_entities(script_text)
    cap_ents = extract_fact_entities(joined_captions)
    ignored = {x.upper() for x in ignore_missing_entities}

    missing_entities: list[dict[str, str]] = []
    unexpected_entities: list[dict[str, str]] = []
    for cat in ("capacities", "ratios", "ascii_terms"):
        s_set = set(script_ents[cat])
        c_set = set(cap_ents[cat])
        for item in sorted(s_set - c_set):
            if item.upper() not in ignored:
                missing_entities.append({"category": cat, "entity": item})
        for item in sorted(c_set - s_set):
            if item.upper() not in ignored:
                unexpected_entities.append({"category": cat, "entity": item})

    norm_script = _norm_zh(_normalize_entity_text(script_text))
    trigram_anomalies: list[dict[str, Any]] = []
    for idx, cue in enumerate(cues, 1):
        tx = str(cue.get("text", ""))
        if any(tok in tx for tok in ignore_cue_substrings):
            continue
        nt = _norm_zh(_normalize_entity_text(tx))
        if len(nt) < 4:
            continue
        miss_flags = [nt[j : j + 3] not in norm_script for j in range(len(nt) - 2)]
        misses = [nt[j : j + 3] for j, is_m in enumerate(miss_flags) if is_m]
        max_run = 0
        cur_run = 0
        for is_m in miss_flags:
            cur_run = cur_run + 1 if is_m else 0
            if cur_run > max_run:
                max_run = cur_run
        if len(misses) >= max(min_trigram_misses, len(nt) // 2) or (len(misses) >= 3 and max_run >= 3):
            trigram_anomalies.append(
                {
                    "cue": idx,
                    "time_s": float(cue.get("start_s", 0.0)),
                    "text": tx,
                    "miss_count": len(misses),
                    "total_trigrams": len(nt) - 2,
                    "sample_misses": misses[:5],
                }
            )

    issue_count = len(missing_entities) + len(unexpected_entities) + len(trigram_anomalies)
    return {
        "schema": "video_creation_pipeline.entity_and_trigram_gate.v1",
        "script_entities": script_ents,
        "caption_entities": cap_ents,
        "missing_entities": missing_entities,
        "unexpected_entities": unexpected_entities,
        "trigram_anomalies": trigram_anomalies,
        "issue_count": issue_count,
        "passed": issue_count == 0,
    }


def extract_and_verify_rendered_mp4_anchors(
    mp4_path: Any,
    *,
    resolved_overlays: list[dict[str, Any]],
    cues: list[dict[str, Any]],
    output_dir: Any,
    ffmpeg_bin: str = "ffmpeg",
    extra_checks: list[dict[str, Any]] | None = None,
    max_face_area_ratio_for_evidence: float = 0.08,
    require_ai_disclosure_badge: bool = False,
    ai_badge_box_xyxy: tuple[int, int, int, int] = (108, 16, 340, 78),
    ocr_fn: Any = None,
    min_ocr_ratio: float = 0.55,
) -> dict[str, Any]:
    """Gate 7: Post-render closed-loop verification directly on burned MP4 frames at semantic anchors.

    Extracts frames via ffmpeg, validates non-blank visuals, subtitle-band ink contrast,
    evidence-slot face-area exclusion on actual burned pixels, optional OCR text verification,
    and writes a contact sheet + machine-readable JSON receipt.
    """
    import json
    import subprocess
    from pathlib import Path
    import cv2
    import numpy as np

    mp4 = Path(mp4_path)
    if not mp4.exists():
        raise ContractError(f"rendered mp4 not found: {mp4}")

    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    checks: list[dict[str, Any]] = []
    for ov in resolved_overlays:
        s0 = float(ov["resolved_start_s"])
        s1 = float(ov["resolved_end_s"])
        mid = round((s0 + s1) / 2.0, 2)
        checks.append(
            {
                "check_id": str(ov["overlay_id"]),
                "time_s": mid,
                "kind": str(ov.get("kind", "")),
                "role": str(ov.get("role", "evidence_document" if ov.get("kind") == "anim_evidence_zoom" else "overlay")),
            }
        )
    if extra_checks:
        for ex in extra_checks:
            checks.append(
                {
                    "check_id": str(ex["check_id"]),
                    "time_s": float(ex["time_s"]),
                    "kind": str(ex.get("kind", "caption_spot")),
                    "role": str(ex.get("role", "caption_spot")),
                    "expected_substring": str(ex.get("expected_substring", "")),
                }
            )

    def _active_cue_text(t: float) -> str:
        for c in cues:
            if float(c["start_s"]) - 0.05 <= t <= float(c["end_s"]) + 0.05:
                return str(c.get("text", ""))
        return ""

    results: list[dict[str, Any]] = []
    thumbs: list[Any] = []
    failures: list[dict[str, Any]] = []

    for idx, chk in enumerate(checks, 1):
        cid = chk["check_id"]
        t = chk["time_s"]
        frame_p = out_dir / f"mp4_anchor_{idx:02d}_{cid}.jpg"
        proc = subprocess.run(
            [ffmpeg_bin, "-y", "-nostdin", "-v", "error", "-ss", f"{t:.3f}", "-i", str(mp4), "-vframes", "1", "-q:v", "2", str(frame_p)],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0 or not frame_p.exists():
            fail = {"check_id": cid, "time_s": t, "reason": f"ffmpeg extract failed: {proc.stderr.strip()}"}
            failures.append(fail)
            results.append({"check_id": cid, "time_s": t, "passed": False, "error": fail["reason"]})
            continue

        bgr = cv2.imread(str(frame_p))
        if bgr is None:
            fail = {"check_id": cid, "time_s": t, "reason": "cv2 failed to read extracted frame"}
            failures.append(fail)
            results.append({"check_id": cid, "time_s": t, "passed": False, "error": fail["reason"]})
            continue

        h, w = bgr.shape[:2]
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        mean_luma = float(np.mean(gray))
        std_luma = float(np.std(gray))
        frame_ok = (10.0 <= mean_luma <= 248.0) and (std_luma >= 8.0)

        expected_sub = _active_cue_text(t)
        sub_roi = bgr[int(h * 0.74) : int(h * 0.88), int(w * 0.05) : int(w * 0.95)]
        sub_gray = cv2.cvtColor(sub_roi, cv2.COLOR_BGR2GRAY)
        sub_contrast = float(np.max(sub_gray) - np.min(sub_gray)) if sub_gray.size else 0.0
        sub_ok = True if not expected_sub else (sub_contrast >= 70.0)

        evidence_check: dict[str, Any] | None = None
        evidence_ok = True
        if chk["role"] == "evidence_document":
            top_stage = bgr[int(h * 0.04) : int(h * 0.44), int(w * 0.03) : int(w * 0.97)]
            evidence_check = validate_broll_asset_role(
                top_stage,
                role="evidence_document",
                max_face_area_ratio_for_evidence=max_face_area_ratio_for_evidence,
            )
            evidence_ok = bool(evidence_check["passed"])

        badge_ok = True
        badge_contrast: float | None = None
        if require_ai_disclosure_badge:
            bx0, by0, bx1, by1 = ai_badge_box_xyxy
            broi = bgr[max(0, by0) : min(h, by1), max(0, bx0) : min(w, bx1)]
            bgray = cv2.cvtColor(broi, cv2.COLOR_BGR2GRAY) if broi.size else np.zeros((1, 1), dtype=np.uint8)
            badge_contrast = float(np.max(bgray) - np.min(bgray)) if bgray.size else 0.0
            badge_ok = badge_contrast >= 70.0

        ocr_info: dict[str, Any] | None = None
        ocr_ok = True
        if ocr_fn is not None and expected_sub:
            ocr_raw = str(ocr_fn(bgr, sub_roi) or "")
            ratio = SequenceMatcher(None, _norm_zh(expected_sub), _norm_zh(ocr_raw)).ratio()
            ocr_ok = ratio >= min_ocr_ratio
            ocr_info = {"ocr_text": ocr_raw, "expected_text": expected_sub, "match_ratio": round(ratio, 3), "passed": ocr_ok}

        item_ok = frame_ok and sub_ok and evidence_ok and badge_ok and ocr_ok
        rec = {
            "check_id": cid,
            "time_s": t,
            "frame_path": str(frame_p),
            "mean_luma": round(mean_luma, 1),
            "std_luma": round(std_luma, 1),
            "expected_subtitle": expected_sub,
            "subtitle_band_contrast": round(sub_contrast, 1),
            "ai_badge_contrast": round(badge_contrast, 1) if badge_contrast is not None else None,
            "evidence_role_check": evidence_check,
            "ocr_check": ocr_info,
            "passed": item_ok,
        }
        results.append(rec)
        if not item_ok:
            failures.append({"check_id": cid, "time_s": t, "frame_ok": frame_ok, "sub_ok": sub_ok, "evidence_ok": evidence_ok, "badge_ok": badge_ok, "ocr_ok": ocr_ok})

        # Build compact thumbnail for contact sheet
        th = cv2.resize(bgr, (270, 480), interpolation=cv2.INTER_AREA)
        cv2.rectangle(th, (0, 0), (270, 34), (26, 25, 23), -1)
        cv2.putText(th, f"{cid[:16]} @{t:.1f}s", (8, 23), cv2.FONT_HERSHEY_SIMPLEX, 0.52, (245, 240, 232), 1, cv2.LINE_AA)
        thumbs.append(th)

    contact_sheet_path = out_dir / "mp4_anchor_contact_sheet.png"
    if thumbs:
        cols = 4
        rows_n = math.ceil(len(thumbs) / cols)
        sheet = np.full((rows_n * 480, cols * 270, 3), 240, dtype=np.uint8)
        for i, th in enumerate(thumbs):
            r, c = divmod(i, cols)
            sheet[r * 480 : (r + 1) * 480, c * 270 : (c + 1) * 270] = th
        cv2.imwrite(str(contact_sheet_path), sheet)

    receipt = {
        "schema": "video_creation_pipeline.mp4_anchor_verification_gate.v1",
        "mp4_path": str(mp4),
        "check_count": len(results),
        "failure_count": len(failures),
        "passed": len(failures) == 0,
        "contact_sheet_path": str(contact_sheet_path) if thumbs else None,
        "failures": failures,
        "checks": results,
    }
    receipt_p = out_dir / "mp4_anchor_verification_receipt.json"
    receipt_p.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
    return receipt
