"""Hash-bound word anchors for one original-speed creator narration segment.

This imports timing evidence; it does not run ASR, invent timing, approve a
script, or render audio. The projection is shared data for downstream consumers.
"""
from __future__ import annotations

import math
import wave
from copy import deepcopy
from pathlib import Path
from typing import Any

from .contract_io import (
    artifact_reference,
    atomic_write_json,
    payload_sha256,
    read_json_object,
    validate_artifact_reference,
)
from .creative_gates import validate_approval_record
from .errors import ContractError
from .production_strategy import (
    CONTENT_VISUAL_PRODUCTION_PLAN_SCHEMA,
    content_visual_source,
    validate_visual_production_plan,
)

AUTHOR_BINDING_SPEC_SCHEMA = "video_creation_pipeline.author_binding_spec.v1"
AUTHOR_BINDING_PROJECTION_SCHEMA = "video_creation_pipeline.author_binding_projection.v1"
WORD_ALIGNMENT_EVIDENCE_SCHEMA = "video_creation_pipeline.word_alignment_evidence.v1"


def _object(value: Any, label: str) -> dict:
    if not isinstance(value, dict):
        raise ContractError(f"{label} must be an object")
    return value


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ContractError(f"{label} must be a finite number")
    return float(value)


def _ref(spec: dict, field: str) -> Path:
    return validate_artifact_reference(_object(spec.get(field), field), field)


def _approval(path: str | Path, target: str, artifact: dict) -> dict:
    record = read_json_object(path, f"{target} approval")
    validate_approval_record(record)
    if record["target"] != target or record["artifact"] != artifact:
        raise ContractError(f"{target} approval is for a different artifact")
    return record


def load_author_timebase_context(
    spec_path: str | Path, *, script_approval_path: str | Path,
    voice_approval_path: str | Path, storyboard_approval_path: str | Path,
    expected_schema: str = AUTHOR_BINDING_SPEC_SCHEMA,
    source_label: str = "author binding spec", require_asset: bool = True,
) -> dict[str, Any]:
    """Shared recording/provenance validation; never measures or infers timing."""
    spec = read_json_object(spec_path, source_label)
    if spec.get("schema") != expected_schema:
        raise ContractError("unsupported author binding spec schema")
    if (spec.get("source_media_role") != "creator_narration"
            or type(spec.get("playback_rate")) not in (int, float) or spec["playback_rate"] != 1):
        raise ContractError("author binding supports one original-speed creator narration only")
    mode = spec.get("evidence_mode")
    if mode not in {"synthetic_fixture", "measured_recording"}:
        raise ContractError("author binding requires an explicit evidence mode")
    if (require_asset and set(_object(spec.get("asset"), "asset")) != {
        "asset_id", "media_type", "evidence_status", "authorization_status", "deidentified"
    }) or set(_object(spec.get("output"), "output")) != {"width", "height", "fps"}:
        raise ContractError("author binding requires explicit asset and output fields")
    visual_path = _ref(spec, "source_visual_production_plan")
    visual = read_json_object(visual_path, "visual production plan")
    validate_visual_production_plan(visual)
    if visual["schema"] != CONTENT_VISUAL_PRODUCTION_PLAN_SCHEMA:
        raise ContractError("author binding requires a content visual plan")
    scene_id = spec.get("scene_id")
    if scene_id != visual["scene_id"]:
        raise ContractError("author binding scene identity differs from the visual plan")
    content, unit = content_visual_source(visual["source_content_plan"]["path"], scene_id)
    script_reference = content["inputs"]["content_video_brief"]
    recording_path = _ref(spec, "source_recording")
    alignment_path = _ref(spec, "source_word_alignment")
    packaging_path = _ref(spec, "source_packaging_plan")
    if require_asset:
        _ref(spec, "source_asset")
    _approval(script_approval_path, "script", script_reference)
    voice = _approval(voice_approval_path, "voice", spec["source_recording"])
    attachments = {row["role"]: row["artifact"] for row in voice["attachments"]}
    if (attachments.get("word_alignment") != spec["source_word_alignment"]
            or not any(ref == spec["source_recording"] for role, ref in attachments.items()
                       if role == "audio" or role.startswith("audio:"))):
        raise ContractError("voice approval must separately bind this recording and word alignment")
    _approval(storyboard_approval_path, "storyboard", artifact_reference(spec_path, source_label))
    try:
        with wave.open(str(recording_path), "rb") as audio:
            audio_duration = audio.getnframes() / audio.getframerate()
            # Reading proves a truncated payload is not a complete recording.
            expected_bytes = audio.getnframes() * audio.getnchannels() * audio.getsampwidth()
            actual_bytes = 0
            while chunk := audio.readframes(65536):
                actual_bytes += len(chunk)
            if actual_bytes != expected_bytes:
                raise ContractError("recording WAV payload is truncated")
    except (wave.Error, EOFError, OSError, ZeroDivisionError) as exc:
        raise ContractError("author binding requires a readable PCM WAV recording") from exc
    alignment = read_json_object(alignment_path, "word alignment evidence")
    if (alignment.get("schema") != WORD_ALIGNMENT_EVIDENCE_SCHEMA
            or alignment.get("evidence_mode") != mode
            or alignment.get("time_domain") != "recording_ms"
            or alignment.get("source_script") != script_reference
            or alignment.get("source_recording") != spec["source_recording"]
            or alignment.get("scene_id") != scene_id
            or alignment.get("quote") != unit["narration"]):
        raise ContractError("word alignment does not bind the exact recording and frozen script scene")
    raw_path = _ref(alignment, "source_raw_alignment")
    if raw_path == alignment_path:
        raise ContractError("alignment evidence must retain its separate raw source")
    if not str(alignment.get("engine") or "").strip():
        raise ContractError("word alignment engine provenance is required")
    rows = alignment.get("words")
    if not isinstance(rows, list) or not rows:
        raise ContractError("word alignment requires words")
    words: dict[str, dict] = {}
    expected_origin = "synthetic_supplied_word" if mode == "synthetic_fixture" else "measured_word"
    last_end, last_text_end = 0.0, 0
    for raw in rows:
        row = _object(raw, "word")
        word_id = row.get("word_id")
        if not isinstance(word_id, str) or not word_id.strip() or word_id in words:
            raise ContractError("word IDs must be nonempty and unique")
        start = _number(row.get("start_ms"), "word start")
        end = _number(row.get("end_ms"), "word end")
        if start < last_end or end <= start or end > audio_duration * 1000 + 0.001:
            raise ContractError("word timestamps overlap or exceed the recording")
        last_end = end
        if row.get("timing_origin") != expected_origin:
            raise ContractError("interpolated or unknown word timing cannot anchor a cue")
        lo, hi = row.get("text_start"), row.get("text_end")
        if (type(lo) is not int or type(hi) is not int or not last_text_end <= lo < hi <= len(unit["narration"])
                or row.get("text") != unit["narration"][lo:hi]):
            raise ContractError("word text must identify an exact range in the frozen narration")
        last_text_end = hi
        words[word_id] = row
    packaging = read_json_object(packaging_path, "packaging plan")
    from .semantic_motion import _validate_packaging_plan
    _validate_packaging_plan(packaging)
    from .motion_sfx import _packaging_scenes
    program_scenes, _ = _packaging_scenes(packaging)
    scenes = [row for row in program_scenes if row.get("scene_id") == scene_id]
    if len(scenes) != 1 or scenes[0].get("story_segment_ref", scene_id) != scene_id:
        raise ContractError("author binding requires one exact packaging scene")
    scene = scenes[0]
    program_start = _number(scene.get("start_s", scene.get("start")), "scene start")
    program_end = _number(scene.get("end_s", scene.get("end")), "scene end")
    recording_start = _number(spec.get("recording_start_s"), "recording start")
    recording_end = _number(spec.get("recording_end_s"), "recording end")
    duration = program_end - program_start
    if (any(key in scene and _number(scene[key], key) != value
            for key, value in (("start", program_start), ("end", program_end)))
            or abs(duration - round(duration, 3)) > 0.000001):
        raise ContractError("program scene timing must be unambiguous and use millisecond duration")
    duration = round(duration, 3)
    if (program_start < 0 or recording_start < 0 or duration <= 0
            or recording_end > audio_duration + 0.000001
            or abs(recording_end - recording_start - duration) > 0.000001):
        raise ContractError("scene and recording span must have equal original-speed duration")
    return {
        "spec": spec, "mode": mode, "visual": visual, "unit": unit,
        "script_reference": script_reference, "words": words,
        "scene_id": scene_id, "program_start": program_start,
        "program_end": program_end, "recording_start": recording_start,
        "recording_end": recording_end, "duration": duration,
        "packaging": packaging,
    }


def build_author_binding_projection(
    spec_path: str | Path, *, script_approval_path: str | Path,
    voice_approval_path: str | Path, storyboard_approval_path: str | Path,
) -> dict[str, Any]:
    context = load_author_timebase_context(spec_path,
        script_approval_path=script_approval_path,
        voice_approval_path=voice_approval_path,
        storyboard_approval_path=storyboard_approval_path)
    spec, mode, unit = context["spec"], context["mode"], context["unit"]
    script_reference, words = context["script_reference"], context["words"]
    scene_id, packaging = context["scene_id"], context["packaging"]
    program_start, program_end = context["program_start"], context["program_end"]
    recording_start, recording_end = context["recording_start"], context["recording_end"]
    duration = context["duration"]
    bindings = spec.get("cue_bindings")
    if not isinstance(bindings, list) or not bindings:
        raise ContractError("author binding needs explicit cue bindings")
    cues, positions, seen = [], [], set()
    for value in bindings:
        binding = _object(value, "cue binding")
        cue = deepcopy(_object(binding.get("cue"), "motion cue"))
        cue_id = cue.get("cue_id")
        if not isinstance(cue_id, str) or not cue_id or cue_id in seen:
            raise ContractError("bound cue IDs must be nonempty and unique")
        seen.add(cue_id)
        timing = _object(cue.get("timing"), "cue timing")
        if "timeline_start_s" in timing:
            raise ContractError("bound cue start must come solely from its word anchor")
        if "temporal_expression" in binding:
            from .temporal_expressions import resolve_window
            if set(binding) != {"cue", "temporal_expression"}:
                raise ContractError("temporal expression cannot compete with a legacy anchor")
            window = resolve_window(binding["temporal_expression"], words,
                fps=spec["output"]["fps"], recording_start_s=recording_start,
                recording_end_s=recording_end)
            local = window["start_s"]
            if "hold_s" in timing:
                raise ContractError("expression duration owns hold_s; omit authored hold_s")
            transitions = sum(_number(timing.get(k), k) for k in ("full_frame_s", "enter_s", "exit_s"))
            timing["hold_s"] = round(window["end_s"] - local - transitions, 9)
            position = {"temporal_expression": deepcopy(binding["temporal_expression"]), "window": window}
        else:
            word = words.get(binding.get("word_id"))
            if word is None or binding.get("boundary") not in {"start", "end"}:
                raise ContractError("cue needs an existing word ID and explicit boundary")
            if (word["start_ms"] < recording_start * 1000
                    or word["end_ms"] > recording_end * 1000):
                raise ContractError("cue word lies outside the selected recording span")
            offset = _number(binding.get("offset_ms"), "cue offset")
            local = (word[binding['boundary'] + '_ms'] + offset) / 1000 - recording_start
            position = {"word_id": binding["word_id"], "boundary": binding["boundary"],
                        "offset_ms": offset, "recording_ms": word[binding['boundary'] + '_ms']}
        phases = [_number(timing.get(name), name) for name in ("full_frame_s", "enter_s", "hold_s", "exit_s")]
        if any(t < 0 for t in phases) or sum(phases) <= 0 or local < 0 or local + sum(phases) > duration + 0.000001:
            raise ContractError("bound cue exceeds its isolated segment")
        if cue.get("story_segment_ref") != scene_id:
            raise ContractError("bound cue and content scene identity must match")
        timing["timeline_start_s"] = round(local, 6)
        cues.append(cue)
        positions.append({"cue_id": cue_id, "story_segment_ref": scene_id,
                          **position,
                          "program_time_s": round(program_start + local, 6),
                          "segment_local_time_s": round(local, 6)})
    local_packaging = {
        "schema": packaging["schema"], "status": "candidate_only",
        "scenes": [{"scene_id": scene_id, "story_segment_ref": scene_id,
                    "start_s": 0.0, "end_s": duration}],
    }
    local_anchors = {"schema": "video_creation_pipeline.script_anchor.v1", "anchors": [{
        "story_segment_ref": scene_id, "status": "confirmed", "quote": unit["narration"],
        "source_media_role": "creator_narration", "timeline_start_s": 0.0, "timeline_end_s": duration,
        "evidence_refs": ["sha256:" + script_reference["sha256"], "sha256:" + spec["source_recording"]["sha256"]],
    }]}
    projection = {
        "schema": AUTHOR_BINDING_PROJECTION_SCHEMA,
        "source_spec": artifact_reference(spec_path, "author binding spec"),
        "approvals": {"script": artifact_reference(script_approval_path, "script approval"),
                      "voice": artifact_reference(voice_approval_path, "voice approval"),
                      "storyboard": artifact_reference(storyboard_approval_path, "storyboard approval")},
        "evidence_mode": mode, "candidate_only": True, "synthetic_fixture_only": mode == "synthetic_fixture",
        "source_visual_production_plan": spec["source_visual_production_plan"],
        "source_asset": spec["source_asset"], "asset": _object(spec.get("asset"), "asset"),
        "output": {**_object(spec.get("output"), "output"), "duration_s": duration},
        "segment": {"scene_id": scene_id, "program_start_s": program_start,
                    "program_end_s": program_end, "recording_start_s": recording_start,
                    "recording_end_s": recording_end, "playback_rate": 1},
        "positions": positions, "cues": cues,
        "local_packaging_plan": local_packaging, "local_script_anchor": local_anchors,
        "authority_boundary": {"automatic_approval": False, "timing_measurement_performed": False,
                               "rendered": False, "published": False},
    }
    projection["projection_sha256"] = payload_sha256(projection, "projection_sha256")
    return projection


def validate_author_binding_projection(projection: dict) -> None:
    if (projection.get("schema") != AUTHOR_BINDING_PROJECTION_SCHEMA
            or projection.get("projection_sha256") != payload_sha256(projection, "projection_sha256")):
        raise ContractError("author binding projection schema or integrity is invalid")
    source = _ref(projection, "source_spec")
    approvals = _object(projection.get("approvals"), "approvals")
    paths = {role: _ref(approvals, role) for role in ("script", "voice", "storyboard")}
    rebuilt = build_author_binding_projection(source, script_approval_path=paths["script"],
               voice_approval_path=paths["voice"], storyboard_approval_path=paths["storyboard"])
    if rebuilt != projection:
        raise ContractError("author binding projection differs from the reviewed source")


def validate_motion_author_binding(plan: dict) -> None:
    projection = read_json_object(_ref(plan, "source_author_binding_projection"), "author binding projection")
    validate_author_binding_projection(projection)
    for field in ("source_visual_production_plan", "source_asset", "asset", "output"):
        if plan.get(field) != projection[field]:
            raise ContractError(f"author bound motion {field} changed")
    for source, expected in (("source_packaging_plan", "local_packaging_plan"),
                             ("source_script_anchor", "local_script_anchor")):
        if read_json_object(_ref(plan, source), source) != projection[expected]:
            raise ContractError("author bound motion local timeline changed")
    if len(plan["cues"]) != len(projection["cues"]):
        raise ContractError("author bound motion cue count changed")
    for cue, original in zip(plan["cues"], projection["cues"], strict=True):
        expected = deepcopy(original)
        expected["script_anchor"] = {key: value for key, value in projection["local_script_anchor"]["anchors"][0].items()
                                     if key != "story_segment_ref"}
        expected["source_asset_id"] = projection["asset"]["asset_id"]
        expected["source_asset_sha256"] = projection["source_asset"]["sha256"]
        for key, value in {"full_frame_first": True, "information_role": "evidence",
                           "presentation_mode": "fullscreen_evidence",
                           "reduced_motion_fallback": "static_focus_with_label",
                           "static_fallback": "full_frame_with_box_and_label"}.items():
            expected.setdefault(key, value)
        if cue != expected:
            raise ContractError("author bound motion cue differs from its shared projection")


def _write_once(payload: dict, path: Path) -> Path:
    if path.exists():
        if read_json_object(path, "existing author binding output") != payload:
            raise ContractError("author binding output exists with different content; use a new directory")
        return path
    return atomic_write_json(payload, path)


def build_author_bound_motion_bundle(
    spec_path: str | Path, output_dir: str | Path, *,
    script_approval_path: str | Path, voice_approval_path: str | Path,
    storyboard_approval_path: str | Path,
) -> dict[str, Path]:
    projection = build_author_binding_projection(spec_path, script_approval_path=script_approval_path,
        voice_approval_path=voice_approval_path, storyboard_approval_path=storyboard_approval_path)
    root = Path(output_dir).expanduser().resolve()
    paths = {
        "projection": _write_once(projection, root / "author-binding-projection.json"),
        "packaging": _write_once(projection["local_packaging_plan"], root / "segment-packaging-plan.json"),
        "anchors": _write_once(projection["local_script_anchor"], root / "segment-script-anchor.json"),
    }
    from .semantic_motion import build_semantic_motion_plan
    motion = build_semantic_motion_plan(paths["packaging"], paths["anchors"],
        projection["source_asset"]["path"], projection["source_visual_production_plan"]["path"],
        cues=projection["cues"], **projection["asset"], **projection["output"],
        author_binding_projection_path=paths["projection"])
    paths["motion_plan"] = _write_once(motion, root / "semantic-motion-plan.json")
    from .motion_sfx import (
        build_motion_sfx_source_manifest,
        derive_motion_sfx_cue_sheet,
    )
    sfx_manifest = build_motion_sfx_source_manifest(paths["packaging"],
        semantic_motion_plan_path=paths["motion_plan"], script_anchor_path=paths["anchors"])
    paths["sfx_manifest"] = _write_once(sfx_manifest, root / "motion-sfx-source-manifest.json")
    paths["sfx_cue_sheet"] = _write_once(derive_motion_sfx_cue_sheet(paths["sfx_manifest"]),
                                       root / "motion-sfx-cue-sheet.json")
    return paths
