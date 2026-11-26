"""Thin, hash-bound adapter for the external video-talkcraft recipe catalog."""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

from .contract_io import (
    artifact_reference,
    atomic_write_json,
    canonical_json_sha256,
    file_sha256,
    payload_sha256,
    read_json_object,
    validate_artifact_reference,
)
from .errors import ContractError
from .music_pipeline import _load_word_timestamps
from .semantic_motion import validate_semantic_motion_plan

VIDEO_TALKCRAFT_REPOSITORY = "https://github.com/Vincentwei1021/video-talkcraft"
VIDEO_TALKCRAFT_COMMIT = "05691f5f25d91d211f793f5bb0d6941d3ef5723d"
VIDEO_TALKCRAFT_LICENSE = "PolyForm-Noncommercial-1.0.0"
VIDEO_TALKCRAFT_CATALOG_SHA256 = (
    "40b6ec9a6fe6a1bdeb93629b3b86bb086c79223f6f3b15b0491126734ee48b5a"
)
VIDEO_TALKCRAFT_RECIPE_PLAN_SCHEMA = (
    "video_creation_pipeline.video_talkcraft_recipe_plan.v1"
)
VIDEO_TALKCRAFT_EXECUTION_RECEIPT_SCHEMA = (
    "video_creation_pipeline.video_talkcraft_execution_receipt.v1"
)
MOTION_ENTRY_OBSERVATIONS_SCHEMA = (
    "video_creation_pipeline.motion_entry_observations.v1"
)

_SCHEMA_ROOT = Path(__file__).with_name("schemas")
_PLAN_SCHEMA_PATH = _SCHEMA_ROOT / "video_talkcraft_recipe_plan.v1.schema.json"
_RECEIPT_SCHEMA_PATH = _SCHEMA_ROOT / "video_talkcraft_execution_receipt.v1.schema.json"

# This is deliberately a small, restrained subset. The external source stays the
# implementation reference; this table only declares compatibility and policy.
_RECIPE_POLICY: dict[str, dict[str, Any]] = {
    "callout-line-label": {
        "category": "annotation",
        "roles": ["relationship", "evidence"],
        "entry_offset_s": 0.35,
    },
    "chapter-title-card": {
        "category": "structure",
        "roles": ["structure"],
        "entry_offset_s": 0.35,
    },
    "focus-dim-spotlight": {
        "category": "annotation",
        "roles": ["comparison", "evidence"],
        "entry_offset_s": 0.45,
    },
    "highlighter-sweep": {
        "category": "annotation",
        "roles": ["evidence", "relationship"],
        "entry_offset_s": 0.70,
    },
    "host-shrink-to-chip": {
        "category": "presenter_layout",
        "roles": ["structure", "comparison", "evidence"],
        "entry_offset_s": 0.80,
    },
    "info-term-card": {
        "category": "explanation",
        "roles": ["structure", "evidence"],
        "entry_offset_s": 0.05,
    },
    "media-pop-in": {
        "category": "evidence",
        "roles": ["comparison", "evidence"],
        "entry_offset_s": 0.45,
    },
    "number-counter": {
        "category": "number",
        "roles": ["number"],
        "entry_offset_s": 0.30,
    },
    "slow-push-in": {
        "category": "camera",
        "roles": ["evidence"],
        "entry_offset_s": 0.00,
    },
    "step-timeline-vertical": {
        "category": "sequence",
        "roles": ["sequence", "structure"],
        "entry_offset_s": 0.52,
    },
}

_DEFAULT_RECIPE_BY_ROLE = {
    "structure": "chapter-title-card",
    "relationship": "callout-line-label",
    "sequence": "step-timeline-vertical",
    "comparison": "focus-dim-spotlight",
    "number": "number-counter",
    "evidence": "highlighter-sweep",
}


def video_talkcraft_doctor(
    source_root: str | Path,
    *,
    source_commit: str = VIDEO_TALKCRAFT_COMMIT,
) -> dict[str, Any]:
    try:
        catalog = inspect_video_talkcraft_source(
            source_root, source_commit=source_commit
        )
    except ContractError as exc:
        return {
            "schema": "video_creation_pipeline.video_talkcraft_doctor.v1",
            "status": "blocked",
            "reason": str(exc),
            "expected_commit": VIDEO_TALKCRAFT_COMMIT,
            "candidate_only": True,
        }
    return {
        "schema": "video_creation_pipeline.video_talkcraft_doctor.v1",
        "status": "ready_for_local_noncommercial_pilot",
        "expected_commit": VIDEO_TALKCRAFT_COMMIT,
        "catalog_sha256": catalog["catalog_sha256"],
        "whitelist_count": len(catalog["cards"]),
        "license": VIDEO_TALKCRAFT_LICENSE,
        "commercial_authorization": "not_evidenced",
        "source_code_vendored": False,
        "source_audio_reused": False,
        "candidate_only": True,
    }


def inspect_video_talkcraft_source(
    source_root: str | Path,
    *,
    source_commit: str,
) -> dict[str, Any]:
    root = Path(source_root).expanduser().resolve()
    if source_commit != VIDEO_TALKCRAFT_COMMIT:
        raise ContractError("video-talkcraft source commit is not the reviewed commit")
    if not root.is_dir():
        raise ContractError(f"video-talkcraft source root not found: {root}")
    license_path = root / "LICENSE"
    taxonomy_path = root / "references" / "taxonomy.md"
    sfx_map_path = root / "demos" / "_lib" / "sfx-map.js"
    for path, label in (
        (license_path, "license"),
        (taxonomy_path, "taxonomy"),
        (sfx_map_path, "SFX map"),
    ):
        if not path.is_file():
            raise ContractError(f"video-talkcraft {label} is missing: {path}")
    sfx_source = sfx_map_path.read_text(encoding="utf-8")
    cards: list[dict[str, Any]] = []
    for recipe_id, policy in sorted(_RECIPE_POLICY.items()):
        path = root / "template" / "cards" / f"{recipe_id}.tsx"
        if not path.is_file():
            raise ContractError(f"video-talkcraft recipe is missing: {recipe_id}")
        meta = _parse_card_meta(path)
        source_sfx = _extract_sfx_cues(sfx_source, recipe_id)
        cards.append(
            {
                "recipe_id": recipe_id,
                "relative_path": path.relative_to(root).as_posix(),
                "sha256": file_sha256(path),
                "category": policy["category"],
                "allowed_information_roles": list(policy["roles"]),
                "entry_offset_s": policy["entry_offset_s"],
                "source_meta": meta,
                "source_sfx_cues": source_sfx,
            }
        )
    digest_input = {
        "repository": VIDEO_TALKCRAFT_REPOSITORY,
        "commit": source_commit,
        "license_sha256": file_sha256(license_path),
        "taxonomy_sha256": file_sha256(taxonomy_path),
        "sfx_map_sha256": file_sha256(sfx_map_path),
        "cards": cards,
    }
    catalog = {
        "root": str(root),
        **digest_input,
        "catalog_sha256": canonical_json_sha256(digest_input),
    }
    if (
        VIDEO_TALKCRAFT_CATALOG_SHA256
        and catalog["catalog_sha256"] != VIDEO_TALKCRAFT_CATALOG_SHA256
    ):
        raise ContractError(
            "video-talkcraft source snapshot does not match the reviewed catalog"
        )
    return catalog


def build_video_talkcraft_recipe_plan(
    semantic_motion_plan_path: str | Path,
    source_root: str | Path,
    *,
    source_commit: str = VIDEO_TALKCRAFT_COMMIT,
    word_timestamps_path: str | Path | None = None,
    selections_path: str | Path | None = None,
    _skip_validation: bool = False,
) -> dict[str, Any]:
    semantic_path = Path(semantic_motion_plan_path).expanduser().resolve()
    semantic = read_json_object(semantic_path, "semantic motion plan")
    validate_semantic_motion_plan(semantic)
    catalog = inspect_video_talkcraft_source(source_root, source_commit=source_commit)
    catalog_by_id = {row["recipe_id"]: row for row in catalog["cards"]}
    selections, selections_ref = _load_selections(selections_path)
    words, word_ref = _load_timing_rows(word_timestamps_path)
    fps = int(_object(semantic.get("output")).get("fps") or 30)
    width = int(_object(semantic.get("output")).get("width") or 1080)
    height = int(_object(semantic.get("output")).get("height") or 1920)
    typography = _object(semantic.get("typography_gate"))
    motion_font = int(_object(typography.get("roles")).get("motion_label") or 0)
    if motion_font <= 0:
        raise ContractError("semantic motion typography gate lacks motion_label")

    planned_cues: list[dict[str, Any]] = []
    for raw_cue in semantic.get("cues") or []:
        cue = _object(raw_cue)
        cue_id = _required_text(cue.get("cue_id"), "semantic motion cue id")
        role = str(cue.get("information_role") or "evidence")
        override = selections.get(cue_id, {})
        recipe_id = str(
            override.get("recipe_id") or _DEFAULT_RECIPE_BY_ROLE.get(role) or ""
        )
        recipe = catalog_by_id.get(recipe_id)
        if recipe is None:
            raise ContractError(f"motion cue {cue_id} selects a non-whitelisted recipe")
        if role not in recipe["allowed_information_roles"]:
            raise ContractError(
                f"recipe {recipe_id} is not compatible with information role {role}"
            )
        timing = _object(cue.get("timing"))
        cue_start_s = _number(timing.get("timeline_start_s"), "motion cue start")
        semantic_impact_s = round(
            cue_start_s
            + _number(timing.get("full_frame_s"), "full-frame duration")
            + 0.82 * _number(timing.get("enter_s"), "entry duration"),
            6,
        )
        anchor = _resolve_anchor(
            cue,
            words,
            preferred_text=str(override.get("anchor_text") or "").strip(),
            preferred_time_s=semantic_impact_s,
        )
        entry_offset_s = float(recipe["entry_offset_s"])
        sequence_start_s = max(cue_start_s, float(anchor["start_s"]) - entry_offset_s)
        sequence_start_frame = round(sequence_start_s * fps)
        declared_entry_frame = sequence_start_frame + round(entry_offset_s * fps)
        anchor_frame = round(float(anchor["start_s"]) * fps)
        tolerance_frames = max(3, math.ceil(fps * 0.20))
        deadline_frame = anchor_frame + tolerance_frames
        if declared_entry_frame > deadline_frame:
            raise ContractError(
                f"recipe {recipe_id} cannot enter in time for motion cue {cue_id}"
            )
        source_duration_s = float(
            _object(recipe["source_meta"])["durationInFrames"]
        ) / float(_object(recipe["source_meta"])["fps"])
        semantic_duration_s = sum(
            _number(timing.get(field), f"motion cue {field}")
            for field in ("full_frame_s", "enter_s", "hold_s", "exit_s")
        )
        cue_end_s = cue_start_s + semantic_duration_s
        allocated_duration_s = max(
            0.01, min(source_duration_s, cue_end_s - sequence_start_s)
        )
        sfx_candidates = _project_sfx_candidates(
            recipe,
            cue_id=cue_id,
            sequence_start_s=sequence_start_s,
            allocated_duration_s=allocated_duration_s,
            information_role=role,
        )
        planned_cues.append(
            {
                "cue_id": cue_id,
                "story_segment_ref": cue.get("story_segment_ref"),
                "semantic_motion_cue_ref": cue_id,
                "information_role": role,
                "recipe_id": recipe_id,
                "recipe_source": {
                    "relative_path": recipe["relative_path"],
                    "sha256": recipe["sha256"],
                    "source_meta": recipe["source_meta"],
                },
                "anchor": anchor,
                "entry_contract": {
                    "sequence_start_s": round(sequence_start_s, 6),
                    "sequence_start_frame": sequence_start_frame,
                    "declared_first_meaningful_frame": declared_entry_frame,
                    "anchor_frame": anchor_frame,
                    "deadline_frame": deadline_frame,
                    "tolerance_frames": tolerance_frames,
                    "observation_required": True,
                },
                "duration_contract": {
                    "source_duration_s": round(source_duration_s, 6),
                    "allocated_duration_s": round(allocated_duration_s, 6),
                    "tail_truncated": allocated_duration_s + 1e-6 < source_duration_s,
                },
                "target_rect": dict(_object(cue.get("focus_destination"))),
                "protected_regions": list(cue.get("protected_regions") or []),
                "mobile_gate": {
                    "preview_widths": [270, 390],
                    "minimum_design_font_px": motion_font,
                    "overflow_must_be_false": True,
                    "critical_text_must_be_visible": True,
                },
                "sfx_candidates": sfx_candidates,
            }
        )

    plan: dict[str, Any] = {
        "schema": VIDEO_TALKCRAFT_RECIPE_PLAN_SCHEMA,
        "source_semantic_motion_plan": artifact_reference(
            semantic_path, "semantic motion plan"
        ),
        "source_word_timestamps": word_ref,
        "source_selections": selections_ref,
        "external_catalog": catalog,
        "output": {"width": width, "height": height, "fps": fps},
        "selection_policy": {
            "mode": "insurance_safe_whitelist",
            "automatic_full_catalog_selection": False,
            "unknown_recipe_fallback": False,
            "source_code_vendored": False,
            "source_audio_reused": False,
            "commercial_authorization": "not_evidenced",
        },
        "cues": planned_cues,
        "authority_boundary": {
            "status": "ready_for_local_noncommercial_pilot",
            "candidate_only": True,
            "semantic_timeline_moved": False,
            "second_asr_created": False,
            "second_ffmpeg_outlet_created": False,
            "automatic_publish": False,
        },
    }
    plan["plan_sha256"] = payload_sha256(plan, "plan_sha256")
    if not _skip_validation:
        validate_video_talkcraft_recipe_plan(plan)
    return plan


def validate_video_talkcraft_recipe_plan(plan: dict[str, Any]) -> None:
    _validate_schema(plan, _PLAN_SCHEMA_PATH, "video-talkcraft recipe plan")
    if plan.get("plan_sha256") != payload_sha256(plan, "plan_sha256"):
        raise ContractError("video-talkcraft recipe plan integrity check failed")
    semantic_path = validate_artifact_reference(
        _object(plan.get("source_semantic_motion_plan")), "semantic motion plan"
    )
    semantic = read_json_object(semantic_path, "semantic motion plan")
    validate_semantic_motion_plan(semantic)
    if plan.get("source_word_timestamps") is not None:
        validate_artifact_reference(
            _object(plan["source_word_timestamps"]), "word timestamps"
        )
    if plan.get("source_selections") is not None:
        validate_artifact_reference(_object(plan["source_selections"]), "selections")
    external = _object(plan.get("external_catalog"))
    actual_catalog = inspect_video_talkcraft_source(
        str(external.get("root") or ""),
        source_commit=str(external.get("commit") or ""),
    )
    if external != actual_catalog:
        raise ContractError("video-talkcraft external catalog changed after planning")
    semantic_by_id = {
        str(_object(row).get("cue_id") or ""): _object(row)
        for row in semantic.get("cues") or []
    }
    catalog_by_id = {
        str(_object(row).get("recipe_id") or ""): _object(row)
        for row in external.get("cards") or []
    }
    seen: set[str] = set()
    for raw in plan.get("cues") or []:
        cue = _object(raw)
        cue_id = str(cue.get("cue_id") or "")
        if cue_id in seen or cue_id not in semantic_by_id:
            raise ContractError("video-talkcraft cue binding is invalid")
        seen.add(cue_id)
        semantic_cue = semantic_by_id[cue_id]
        recipe = catalog_by_id.get(str(cue.get("recipe_id") or ""))
        if recipe is None:
            raise ContractError("video-talkcraft cue uses an unknown recipe")
        if cue.get("information_role") not in recipe["allowed_information_roles"]:
            raise ContractError("video-talkcraft recipe role binding drifted")
        if cue.get("recipe_source") != {
            "relative_path": recipe["relative_path"],
            "sha256": recipe["sha256"],
            "source_meta": recipe["source_meta"],
        }:
            raise ContractError("video-talkcraft recipe source binding drifted")
        if cue.get("target_rect") != semantic_cue.get("focus_destination"):
            raise ContractError("video-talkcraft target rectangle drifted")
        if cue.get("protected_regions") != semantic_cue.get("protected_regions"):
            raise ContractError("video-talkcraft protected regions drifted")
        entry = _object(cue.get("entry_contract"))
        if int(entry["declared_first_meaningful_frame"]) > int(entry["deadline_frame"]):
            raise ContractError(
                "video-talkcraft declared visual entry misses its deadline"
            )
        if int(entry["sequence_start_frame"]) > int(
            entry["declared_first_meaningful_frame"]
        ):
            raise ContractError("video-talkcraft entry frame precedes its sequence")
    if seen != set(semantic_by_id):
        raise ContractError(
            "video-talkcraft plan does not cover every semantic motion cue"
        )
    word_reference = plan.get("source_word_timestamps")
    selection_reference = plan.get("source_selections")
    expected = build_video_talkcraft_recipe_plan(
        semantic_path,
        str(external["root"]),
        source_commit=str(external["commit"]),
        word_timestamps_path=(
            str(_object(word_reference)["path"]) if word_reference is not None else None
        ),
        selections_path=(
            str(_object(selection_reference)["path"])
            if selection_reference is not None
            else None
        ),
        _skip_validation=True,
    )
    if plan != expected:
        raise ContractError(
            "video-talkcraft recipe plan drifted from its bound source contracts"
        )


def build_video_talkcraft_execution_receipt(
    recipe_plan_path: str | Path,
    render_receipt_path: str | Path,
    observations_path: str | Path,
) -> dict[str, Any]:
    plan_path = Path(recipe_plan_path).expanduser().resolve()
    render_path = Path(render_receipt_path).expanduser().resolve()
    observation_path = Path(observations_path).expanduser().resolve()
    plan = read_json_object(plan_path, "video-talkcraft recipe plan")
    validate_video_talkcraft_recipe_plan(plan)
    render = read_json_object(render_path, "video-talkcraft render receipt")
    render_candidate = _validated_render_candidate(render)
    observations = read_json_object(observation_path, "motion entry observations")
    if observations.get("schema") != MOTION_ENTRY_OBSERVATIONS_SCHEMA:
        raise ContractError("unsupported motion entry observations schema")
    if observations.get("recipe_plan_sha256") != plan.get("plan_sha256"):
        raise ContractError("motion entry observations bind a different recipe plan")
    _validate_observation_candidate(observations, render_candidate)
    checks = _execution_checks(plan, observations)
    passed = len(checks) == len(plan.get("cues") or []) and all(
        row.get("status") == "passed" for row in checks
    )
    receipt: dict[str, Any] = {
        "schema": VIDEO_TALKCRAFT_EXECUTION_RECEIPT_SCHEMA,
        "source_recipe_plan": artifact_reference(plan_path, "recipe plan"),
        "source_render_receipt": artifact_reference(render_path, "render receipt"),
        "source_observations": artifact_reference(
            observation_path, "motion entry observations"
        ),
        "status": "passed" if passed else "failed",
        "checks": checks,
        "release_gate": {
            "candidate_only": True,
            "render_eligible": passed,
            "publish_authorized": False,
        },
    }
    receipt["receipt_sha256"] = payload_sha256(receipt, "receipt_sha256")
    validate_video_talkcraft_execution_receipt(receipt)
    return receipt


def validate_video_talkcraft_execution_receipt(receipt: dict[str, Any]) -> None:
    _validate_schema(receipt, _RECEIPT_SCHEMA_PATH, "video-talkcraft receipt")
    if receipt.get("receipt_sha256") != payload_sha256(receipt, "receipt_sha256"):
        raise ContractError("video-talkcraft execution receipt integrity check failed")
    plan_path = validate_artifact_reference(
        _object(receipt.get("source_recipe_plan")), "recipe plan"
    )
    render_path = validate_artifact_reference(
        _object(receipt.get("source_render_receipt")), "render receipt"
    )
    validate_artifact_reference(
        _object(receipt.get("source_observations")), "motion entry observations"
    )
    plan = read_json_object(plan_path, "video-talkcraft recipe plan")
    validate_video_talkcraft_recipe_plan(plan)
    render = read_json_object(render_path, "video-talkcraft render receipt")
    render_candidate = _validated_render_candidate(render)
    observation_path = Path(
        str(_object(receipt.get("source_observations")).get("path") or "")
    )
    observations = read_json_object(observation_path, "motion entry observations")
    if observations.get("schema") != MOTION_ENTRY_OBSERVATIONS_SCHEMA:
        raise ContractError("unsupported motion entry observations schema")
    if observations.get("recipe_plan_sha256") != plan.get("plan_sha256"):
        raise ContractError("motion entry observations bind a different recipe plan")
    _validate_observation_candidate(observations, render_candidate)
    expected_checks = _execution_checks(plan, observations)
    checks = receipt.get("checks") or []
    if checks != expected_checks:
        raise ContractError(
            "video-talkcraft execution checks drifted from bound observations"
        )
    passed = len(checks) == len(plan.get("cues") or []) and all(
        _object(row).get("status") == "passed" for row in checks
    )
    expected_status = "passed" if passed else "failed"
    gate = _object(receipt.get("release_gate"))
    if (
        receipt.get("status") != expected_status
        or gate.get("candidate_only") is not True
        or gate.get("render_eligible") is not passed
        or gate.get("publish_authorized") is not False
    ):
        raise ContractError("video-talkcraft receipt status is inconsistent")


def write_video_talkcraft_contract(
    payload: dict[str, Any], output_path: str | Path
) -> Path:
    if payload.get("schema") == VIDEO_TALKCRAFT_RECIPE_PLAN_SCHEMA:
        validate_video_talkcraft_recipe_plan(payload)
    elif payload.get("schema") == VIDEO_TALKCRAFT_EXECUTION_RECEIPT_SCHEMA:
        validate_video_talkcraft_execution_receipt(payload)
    else:
        raise ContractError("unsupported video-talkcraft adapter artifact")
    return atomic_write_json(payload, output_path)


def _load_selections(
    selections_path: str | Path | None,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any] | None]:
    if selections_path is None:
        return {}, None
    path = Path(selections_path).expanduser().resolve()
    payload = read_json_object(path, "video-talkcraft selections")
    if payload.get("schema") != "video_creation_pipeline.video_talkcraft_selections.v1":
        raise ContractError("unsupported video-talkcraft selections schema")
    values = payload.get("selections")
    if not isinstance(values, list):
        raise ContractError("video-talkcraft selections must be a list")
    result: dict[str, dict[str, Any]] = {}
    for raw in values:
        row = _object(raw)
        cue_id = _required_text(row.get("cue_id"), "selection cue id")
        if cue_id in result:
            raise ContractError("video-talkcraft selection cue ids must be unique")
        result[cue_id] = row
    return result, artifact_reference(path, "video-talkcraft selections")


def _load_timing_rows(
    word_timestamps_path: str | Path | None,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    if word_timestamps_path is None:
        return [], None
    path = Path(word_timestamps_path).expanduser().resolve()
    rows, reference = _load_word_timestamps(path)
    if not rows:
        payload = read_json_object(path, "word timestamps")
        sentences = payload.get("sentences")
        if isinstance(sentences, list):
            rows = [
                word
                for sentence in sentences
                if isinstance(sentence, dict)
                for word in sentence.get("words", [])
                if isinstance(word, dict)
            ]
    normalized: list[dict[str, Any]] = []
    for raw in rows:
        text = str(raw.get("text", raw.get("word", raw.get("token", ""))) or "")
        start = raw.get("start_s", raw.get("start"))
        end = raw.get("end_s", raw.get("end"))
        if not text.strip() or start is None or end is None:
            continue
        normalized.append(
            {
                "text": text,
                "start_s": _number(start, "word start"),
                "end_s": _number(end, "word end"),
            }
        )
    if not normalized:
        raise ContractError("word timestamp artifact contains no timed words")
    return normalized, reference


def _resolve_anchor(
    cue: dict[str, Any],
    words: list[dict[str, Any]],
    *,
    preferred_text: str,
    preferred_time_s: float,
) -> dict[str, Any]:
    script_anchor = _object(cue.get("script_anchor"))
    anchor_start = _number(script_anchor.get("timeline_start_s"), "script anchor start")
    anchor_end = _number(script_anchor.get("timeline_end_s"), "script anchor end")
    if not words:
        return {
            "text": preferred_text or str(script_anchor.get("quote") or "")[:24],
            "start_s": round(preferred_time_s, 6),
            "end_s": round(min(anchor_end, preferred_time_s + 0.2), 6),
            "source": "semantic_script_anchor",
            "exact_text_match": False,
        }
    in_scope = [
        row
        for row in words
        if float(row["end_s"]) >= anchor_start - 0.001
        and float(row["start_s"]) <= anchor_end + 0.001
    ]
    if preferred_text:
        matches = _matching_word_spans(in_scope, preferred_text)
        if not matches:
            raise ContractError(
                f"requested anchor text was not found: {preferred_text}"
            )
        selected = min(matches, key=lambda row: abs(row["start_s"] - preferred_time_s))
        return {**selected, "source": "word_timestamps", "exact_text_match": True}
    if not in_scope:
        raise ContractError("no word timestamp overlaps the semantic script anchor")
    selected = min(
        in_scope, key=lambda row: abs(float(row["start_s"]) - preferred_time_s)
    )
    return {
        "text": str(selected["text"]),
        "start_s": round(float(selected["start_s"]), 6),
        "end_s": round(float(selected["end_s"]), 6),
        "source": "word_timestamps",
        "exact_text_match": True,
    }


def _matching_word_spans(
    words: list[dict[str, Any]], anchor_text: str
) -> list[dict[str, Any]]:
    chars: list[tuple[str, float, float]] = []
    for word in words:
        text = str(word["text"])
        duration = max(0.0, float(word["end_s"]) - float(word["start_s"]))
        for index, char in enumerate(text):
            start = float(word["start_s"]) + duration * index / max(1, len(text))
            end = float(word["start_s"]) + duration * (index + 1) / max(1, len(text))
            chars.append((char, start, end))
    full_text = "".join(row[0] for row in chars)
    results: list[dict[str, Any]] = []
    offset = full_text.find(anchor_text)
    while offset >= 0:
        end_index = offset + len(anchor_text) - 1
        results.append(
            {
                "text": anchor_text,
                "start_s": round(chars[offset][1], 6),
                "end_s": round(chars[end_index][2], 6),
            }
        )
        offset = full_text.find(anchor_text, offset + 1)
    return results


def _project_sfx_candidates(
    recipe: dict[str, Any],
    *,
    cue_id: str,
    sequence_start_s: float,
    allocated_duration_s: float,
    information_role: str,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for index, raw in enumerate(recipe.get("source_sfx_cues") or []):
        row = _object(raw)
        offset_s = _number(row.get("t"), "recipe SFX offset")
        if offset_s >= allocated_duration_s or len(results) >= 2:
            continue
        source_name = str(row.get("name") or "")
        results.append(
            {
                "candidate_id": f"talkcraft-{cue_id}-{index + 1:02d}",
                "timeline_start_s": round(sequence_start_s + offset_s, 6),
                "offset_s": round(offset_s, 6),
                "function": _sfx_function(source_name, information_role),
                "source_sound_name": source_name,
                "source_volume": float(row.get("vol", 1.0)),
                "action": str(row.get("note") or f"{recipe['recipe_id']} visual beat"),
                "source_audio_reused": False,
            }
        )
    return results


def _sfx_function(source_name: str, information_role: str) -> str:
    value = source_name.casefold()
    if "marker" in value or "pencil" in value:
        return "causality"
    if any(term in value for term in ("paper", "whoosh", "wind", "zoom", "riser")):
        return "transition"
    if information_role == "structure":
        return "orientation"
    return "emphasis"


def _mobile_observations_pass(
    cue: dict[str, Any],
    observation: dict[str, Any],
    *,
    design_width: float,
) -> bool:
    rows = observation.get("mobile_previews")
    if not isinstance(rows, list):
        return False
    expected = set(_object(cue["mobile_gate"])["preview_widths"])
    actual = {int(_object(row).get("width", 0)) for row in rows}
    if actual != expected:
        return False
    design_min = float(_object(cue["mobile_gate"])["minimum_design_font_px"])
    for raw in rows:
        row = _object(raw)
        minimum_expected = design_min * float(row["width"]) / design_width
        if (
            row.get("overflow_detected") is not False
            or row.get("text_clipped") is not False
            or row.get("critical_text_visible") is not True
            or float(row.get("minimum_rendered_font_px") or 0) + 0.01 < minimum_expected
        ):
            return False
    return True


def _execution_checks(
    plan: dict[str, Any], observations: dict[str, Any]
) -> list[dict[str, Any]]:
    design_width = _number(
        _object(plan.get("output")).get("width"), "video-talkcraft design width"
    )
    if design_width <= 0:
        raise ContractError("video-talkcraft design width must be positive")
    rows = observations.get("observations")
    if not isinstance(rows, list):
        raise ContractError("motion entry observations must be a list")
    observed_by_id: dict[str, dict[str, Any]] = {}
    for raw in rows:
        observation = _object(raw)
        cue_id = str(observation.get("cue_id") or "")
        if not cue_id or cue_id in observed_by_id:
            raise ContractError("motion entry observation cue ids must be unique")
        observed_by_id[cue_id] = observation
    plan_ids = {str(_object(row).get("cue_id") or "") for row in plan.get("cues") or []}
    if set(observed_by_id) - plan_ids:
        raise ContractError("motion entry observations contain an unknown cue")
    checks: list[dict[str, Any]] = []
    for raw in plan.get("cues") or []:
        cue = _object(raw)
        cue_id = str(cue["cue_id"])
        observation = observed_by_id.get(cue_id)
        if observation is None:
            checks.append({"cue_id": cue_id, "status": "failed_missing_observation"})
            continue
        source = observation.get("observation_source")
        if source not in {
            "remotion_frame_marker",
            "roi_frame_diff",
            "playwright_dom_probe",
        }:
            raise ContractError("motion entry observation source is unsupported")
        entry = _object(cue["entry_contract"])
        actual_frame = int(observation.get("observed_first_meaningful_frame", -1))
        target_matches = observation.get("target_rect") == cue.get("target_rect")
        entry_passed = (
            int(entry["sequence_start_frame"])
            <= actual_frame
            <= int(entry["deadline_frame"])
        )
        mobile_passed = _mobile_observations_pass(
            cue, observation, design_width=design_width
        )
        checks.append(
            {
                "cue_id": cue_id,
                "recipe_id": cue["recipe_id"],
                "observation_source": source,
                "declared_first_meaningful_frame": entry[
                    "declared_first_meaningful_frame"
                ],
                "observed_first_meaningful_frame": actual_frame,
                "deadline_frame": entry["deadline_frame"],
                "target_rect_matches": target_matches,
                "entry_passed": entry_passed,
                "mobile_readability_passed": mobile_passed,
                "status": (
                    "passed"
                    if entry_passed and target_matches and mobile_passed
                    else "failed"
                ),
            }
        )
    return checks


def _validated_render_candidate(receipt: dict[str, Any]) -> dict[str, Any]:
    schema = str(receipt.get("schema") or "")
    if schema == "video_creation_pipeline.ffmpeg_execution_receipt.v1":
        from .execution_receipts import validate_ffmpeg_execution_receipt

        validate_ffmpeg_execution_receipt(receipt)
        outputs = receipt.get("outputs") or []
        if len(outputs) != 1:
            raise ContractError(
                "video-talkcraft FFmpeg render receipt must have one candidate output"
            )
        candidate = _object(_object(outputs[0]).get("artifact"))
    elif schema == "video_creation_pipeline.semantic_motion_run_receipt.v1":
        from .semantic_motion import validate_semantic_motion_run_receipt

        validate_semantic_motion_run_receipt(receipt)
        candidate = _object(receipt.get("candidate_video"))
    elif schema == "video_creation_pipeline.remotion_run_receipt.v1":
        from .remotion_project import validate_remotion_run_receipt

        validate_remotion_run_receipt(receipt)
        candidate = _object(receipt.get("candidate_video"))
    else:
        raise ContractError("unsupported video-talkcraft render receipt schema")
    validate_artifact_reference(candidate, "video-talkcraft rendered candidate")
    return candidate


def _validate_observation_candidate(
    observations: dict[str, Any], render_candidate: dict[str, Any]
) -> None:
    observed_candidate = _object(observations.get("source_candidate"))
    validate_artifact_reference(
        observed_candidate, "motion entry observation candidate"
    )
    if observed_candidate != render_candidate:
        raise ContractError(
            "motion entry observations bind a different rendered candidate"
        )


def _parse_card_meta(path: Path) -> dict[str, int]:
    text = path.read_text(encoding="utf-8")
    match = re.search(r"export\s+const\s+meta\s*=\s*\{(?P<body>[^}]+)\}", text)
    if match is None:
        raise ContractError(f"video-talkcraft card meta is missing: {path.name}")
    values: dict[str, int] = {}
    for key in ("width", "height", "fps", "durationInFrames"):
        field = re.search(rf"\b{key}\s*:\s*(\d+)", match.group("body"))
        if field is None:
            raise ContractError(f"video-talkcraft card meta lacks {key}: {path.name}")
        values[key] = int(field.group(1))
    return values


def _extract_sfx_cues(source: str, recipe_id: str) -> list[dict[str, Any]]:
    marker = f'"{recipe_id}"'
    key_start = source.find(marker)
    if key_start < 0:
        return []
    start = source.find("[", key_start + len(marker))
    if start < 0:
        raise ContractError(f"video-talkcraft SFX map is malformed for {recipe_id}")
    depth = 0
    in_string = False
    escaped = False
    end = -1
    for index in range(start, len(source)):
        char = source[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                end = index
                break
    if end < 0:
        raise ContractError(f"video-talkcraft SFX map is unclosed for {recipe_id}")
    literal = re.sub(r",\s*([}\]])", r"\1", source[start : end + 1])
    try:
        value = json.loads(literal)
    except json.JSONDecodeError as exc:
        raise ContractError(
            f"video-talkcraft SFX map is unreadable for {recipe_id}"
        ) from exc
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise ContractError(f"video-talkcraft SFX map is invalid for {recipe_id}")
    return value


def _validate_schema(payload: dict[str, Any], schema_path: Path, label: str) -> None:
    schema = read_json_object(schema_path, f"{label} schema")
    try:
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(payload)
    except (SchemaError, ValidationError) as exc:
        raise ContractError(f"{label} schema validation failed: {exc.message}") from exc


def _required_text(value: Any, label: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise ContractError(f"{label} is required")
    return result


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractError(f"{label} must be numeric")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ContractError(f"{label} must be a finite nonnegative number")
    return result


def _object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}
