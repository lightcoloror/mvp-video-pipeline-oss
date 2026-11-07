from __future__ import annotations

import json
import math
import shutil
import subprocess
import tempfile
from collections import Counter
from copy import deepcopy
from pathlib import Path
from typing import Any

from .contract_io import (
    artifact_reference,
    atomic_write_json,
    atomic_write_text,
    file_sha256,
    payload_sha256,
    read_json_object,
    validate_artifact_reference,
)
from .errors import ContractError
from .audio_edit_quality import audio_review_template, validate_rough_audio_review, validate_clip_gains
from .editing_presets import (POLICY as EDITING_POLICY, boundary_review_template, boundary_windows,
    splice_fades, validate_boundary_review, validate_edit_boundaries, validate_protected_events, validate_take_review)
from .execution_receipts import probe_media_metadata
from .spoken_pacing import (
    DEFAULT_NATURAL_PACING_POLICY,
    LEGACY_PACING_V2,
    bound_snapped_pauses,
    pause_cut_window,
    pause_recommendation,
    validate_compiled_pause_bounds,
    validate_pause_decision,
)

JUDGE_PACKET_SCHEMA = "video_creation_pipeline.spoken_edit_judge_packet.v1"
AGENT_DECISIONS_SCHEMA = "video_creation_pipeline.spoken_edit_agent_decisions.v1"
LEGACY_SPOKEN_EDIT_PLAN_SCHEMA = "video_creation_pipeline.spoken_edit_plan.v1"
SPOKEN_EDIT_PLAN_SCHEMA = "video_creation_pipeline.spoken_edit_plan.v2"

VIDEOCUT_KIT_REPOSITORY = "Jiang59991/videocut-kit"
VIDEOCUT_KIT_COMMIT = "b07990f9e57e6eeb801887fa3e36af5c8450ae68"
VIDEOCUT_SCRIPTS = {
    "prepare_judge": (
        "scripts/prepare_judge.js",
        "d7d156fb80a5e19c2b7bcbd82df3076f1f9245e12b7e41dec5e2f17ec110c286",
    ),
    "snap_boundaries": (
        "scripts/snap_boundaries.js",
        "f2d081680cd8780270227724bf411f6e87a8332582d8ac899b839b21c8523b87",
    ),
    "auto_cut": (
        "scripts/auto_cut.js",
        "002c883c7f567f7a59337f935808404fc744f895b23589f7185155adc48a3c6c",
    ),
}

LEGACY_NATURAL_PACING_POLICY = {
    "name": "natural_conversational_v1",
    "retry_rule": "delete_earlier_keep_last_fluent",
    "reference_script_role": "terminology_and_structure_reference_only",
    "source_speech_is_authoritative": True,
    "minimum_pause_candidate_s": 0.3,
    "within_sentence_compress_from_s": 0.5,
    "within_sentence_preserve_s": 0.22,
    "between_sentences_compress_from_s": 0.9,
    "between_sentences_preserve_s": 0.55,
    "leading_pause_compress_from_s": 0.4,
    "leading_pause_preserve_s": 0.2,
    "topic_or_emphasis_pause_preserve_range_s": [0.7, 1.0],
    "minimum_delete_confidence": 0.7,
    "time_stretch_allowed": False,
    "remove_all_breaths": False,
    "machine_tight_pacing_allowed": False,
}

SEMANTIC_KINDS = {
    "retry_previous_take",
    "false_start",
    "misstatement_restart",
    "incomplete_phrase",
    "stutter_restart",
    "manual_context_deletion",
}
OBJECTIVE_KINDS = {"pause", "filler", "connective", "demo"}
DECISION_ACTIONS = {"keep", "delete", "compress", "review_required"}


def videocut_spoken_doctor(source_root: str | Path | None = None) -> dict[str, Any]:
    report: dict[str, Any] = {
        "schema": "video_creation_pipeline.videocut_spoken_doctor.v1",
        "repository": VIDEOCUT_KIT_REPOSITORY,
        "required_commit": VIDEOCUT_KIT_COMMIT,
        "network_required": False,
        "asr_provided": False,
        "ffmpeg_editing_provided": False,
    }
    if source_root is None:
        report.update({"status": "blocked_missing_source", "source_ready": False})
        return report
    try:
        report["source"] = _videocut_source_manifest(source_root)
    except ContractError as exc:
        report.update(
            {"status": "blocked_source_drift", "source_ready": False, "error": str(exc)}
        )
        return report
    report.update({"status": "ready", "source_ready": True})
    return report


def prepare_spoken_edit_judge_packet(
    source_video_path: str | Path,
    tokens_path: str | Path,
    output_dir: str | Path,
    videocut_source_root: str | Path,
    *,
    silence_path: str | Path | None = None,
    reference_script_path: str | Path | None = None,
    timeout_s: float = 30.0,
) -> dict[str, Any]:
    source_video = Path(source_video_path).expanduser().resolve()
    tokens_file = Path(tokens_path).expanduser().resolve()
    output = Path(output_dir).expanduser().resolve()
    silence_file = Path(silence_path).expanduser().resolve() if silence_path else None
    reference_script = (
        Path(reference_script_path).expanduser().resolve()
        if reference_script_path
        else None
    )
    if not source_video.is_file():
        raise ContractError(f"source video not found: {source_video}")
    _read_tokens(tokens_file)
    if silence_file is not None:
        _read_silences(silence_file)
    if reference_script is not None and not reference_script.is_file():
        raise ContractError(f"reference script not found: {reference_script}")
    source = _videocut_source_manifest(videocut_source_root)

    packet_path = output / "spoken-edit-judge-packet.json"
    if packet_path.is_file():
        packet = read_json_object(packet_path, "spoken edit judge packet")
        validate_spoken_edit_judge_packet(packet)
        if packet["pacing_policy"] != DEFAULT_NATURAL_PACING_POLICY:
            raise ContractError("legacy pacing packet is read-only; prepare current policy in a new output directory")
        expected = {
            "source_video": artifact_reference(source_video, "source video"),
            "tokens": artifact_reference(tokens_file, "transcript tokens"),
            "silence": artifact_reference(silence_file, "silence intervals")
            if silence_file
            else None,
            "reference_script": artifact_reference(reference_script, "reference script")
            if reference_script
            else None,
        }
        if any(packet.get(key) != value for key, value in expected.items()):
            raise ContractError("existing judge packet belongs to different inputs")
        return packet

    output.mkdir(parents=True, exist_ok=True)
    node = shutil.which("node")
    if not node:
        raise ContractError("Node.js is required to execute videocut-kit")
    with tempfile.TemporaryDirectory(prefix="spoken-judge-", dir=output) as temp:
        prefix = Path(temp) / "judge"
        command = [
            node,
            source["scripts"]["prepare_judge"]["path"],
            str(tokens_file),
            str(prefix),
        ]
        result = _run(command, timeout_s, "videocut-kit prepare_judge")
        candidates_source = Path(f"{prefix}.candidates.json")
        fulltext_source = Path(f"{prefix}.fulltext.txt")
        candidates = _read_candidate_array(candidates_source)
        try:
            fulltext = fulltext_source.read_text(encoding="utf-8")
        except OSError as exc:
            raise ContractError(f"judge full text is unreadable: {exc}") from exc
        if not fulltext.strip():
            raise ContractError("judge full text is empty")
        candidates_path = atomic_write_text(
            json.dumps(candidates, ensure_ascii=False, indent=2) + "\n",
            output / "judge.candidates.json",
        )
        fulltext_path = atomic_write_text(fulltext, output / "judge.fulltext.txt")

    counts = Counter(str(row["kind"]) for row in candidates)
    packet: dict[str, Any] = {
        "schema": JUDGE_PACKET_SCHEMA,
        "source_video": artifact_reference(source_video, "source video"),
        "tokens": artifact_reference(tokens_file, "transcript tokens"),
        "silence": artifact_reference(silence_file, "silence intervals")
        if silence_file
        else None,
        "reference_script": artifact_reference(reference_script, "reference script")
        if reference_script
        else None,
        "reference_script_authority": "non_authoritative_reference",
        "videocut_kit": source,
        "judge_candidates": artifact_reference(candidates_path, "judge candidates"),
        "judge_fulltext": artifact_reference(fulltext_path, "judge full text"),
        "candidate_count": len(candidates),
        "candidate_kind_counts": dict(sorted(counts.items())),
        "pacing_policy": deepcopy(DEFAULT_NATURAL_PACING_POLICY),
        "agent_review_contract": {
            "must_read_fulltext": True,
            "must_decide_every_objective_candidate": True,
            "must_find_retries_false_starts_and_incomplete_phrases": True,
            "default_retry_resolution": DEFAULT_NATURAL_PACING_POLICY["retry_rule"],
            "low_confidence_semantic_deletion_allowed": False,
            "reference_script_may_override_recorded_speech": False,
        },
        "execution_boundary": {
            "candidate_material_only": True,
            "asr_executed": False,
            "ffmpeg_executed": False,
            "source_media_modified": False,
            "jianying_opened": False,
            "automatic_publish": False,
        },
        "prepare_log": {
            "returncode": result.returncode,
            "stderr_tail": result.stderr.strip()[-1000:],
        },
    }
    packet["packet_sha256"] = payload_sha256(packet, "packet_sha256")
    validate_spoken_edit_judge_packet(packet)
    atomic_write_json(packet, packet_path)
    return packet


def validate_spoken_edit_judge_packet(packet: dict[str, Any]) -> None:
    if packet.get("schema") != JUDGE_PACKET_SCHEMA:
        raise ContractError("unsupported spoken edit judge packet schema")
    if packet.get("packet_sha256") != payload_sha256(packet, "packet_sha256"):
        raise ContractError("spoken edit judge packet integrity check failed")
    validate_artifact_reference(_object(packet.get("source_video")), "source video")
    tokens_path = validate_artifact_reference(
        _object(packet.get("tokens")), "transcript tokens"
    )
    _read_tokens(tokens_path)
    if packet.get("silence") is not None:
        silence_path = validate_artifact_reference(
            _object(packet.get("silence")), "silence intervals"
        )
        _read_silences(silence_path)
    if packet.get("reference_script") is not None:
        validate_artifact_reference(
            _object(packet.get("reference_script")), "reference script"
        )
    if packet.get("reference_script_authority") != "non_authoritative_reference":
        raise ContractError("reference script must remain non-authoritative")
    source = _object(packet.get("videocut_kit"))
    expected_source = _videocut_source_manifest(str(source.get("root") or ""))
    if source != expected_source:
        raise ContractError("videocut-kit source manifest drifted")
    candidates_path = validate_artifact_reference(
        _object(packet.get("judge_candidates")), "judge candidates"
    )
    candidates = _read_candidate_array(candidates_path)
    fulltext_path = validate_artifact_reference(
        _object(packet.get("judge_fulltext")), "judge full text"
    )
    if not fulltext_path.read_text(encoding="utf-8").strip():
        raise ContractError("judge full text is empty")
    counts = Counter(str(row["kind"]) for row in candidates)
    if (
        packet.get("candidate_count") != len(candidates)
        or packet.get("candidate_kind_counts") != dict(sorted(counts.items()))
        or packet.get("pacing_policy") not in (DEFAULT_NATURAL_PACING_POLICY, LEGACY_PACING_V2, LEGACY_NATURAL_PACING_POLICY)
    ):
        raise ContractError("judge packet candidate inventory or policy drifted")
    if packet.get("agent_review_contract") != {
        "must_read_fulltext": True,
        "must_decide_every_objective_candidate": True,
        "must_find_retries_false_starts_and_incomplete_phrases": True,
        "default_retry_resolution": packet["pacing_policy"]["retry_rule"],
        "low_confidence_semantic_deletion_allowed": False,
        "reference_script_may_override_recorded_speech": False,
    }:
        raise ContractError("judge packet agent review contract is invalid")
    boundary = _object(packet.get("execution_boundary"))
    if boundary != {
        "candidate_material_only": True,
        "asr_executed": False,
        "ffmpeg_executed": False,
        "source_media_modified": False,
        "jianying_opened": False,
        "automatic_publish": False,
    }:
        raise ContractError("judge packet execution boundary is invalid")


def build_spoken_edit_decision_template(packet_path: str | Path) -> dict[str, Any]:
    source_packet = Path(packet_path).expanduser().resolve()
    packet = read_json_object(source_packet, "spoken edit judge packet")
    validate_spoken_edit_judge_packet(packet)
    if packet["pacing_policy"] != DEFAULT_NATURAL_PACING_POLICY:
        raise ContractError("legacy pacing packet is read-only; prepare current policy before new decisions")
    candidates_path = validate_artifact_reference(
        _object(packet["judge_candidates"]), "judge candidates"
    )
    decisions = [
        _default_candidate_decision(row)
        for row in _read_candidate_array(candidates_path)
    ]
    payload: dict[str, Any] = {
        "schema": AGENT_DECISIONS_SCHEMA,
        "judge_packet_sha256": packet["packet_sha256"],
        "reviewed_fulltext_sha256": "",
        "status": "awaiting_agent_context_review",
        "review": {
            "method": "agent_full_context_review",
            "fulltext_read": False,
            "semantic_retry_scan_completed": False,
            "source_speech_is_authoritative": True,
            "reference_script_is_authoritative": False,
        },
        "decisions": decisions,
        "protected_events": [],
        "boundary_review": boundary_review_template(),
        "rough_audio_review": audio_review_template(),
        "clip_gains": [],
        "authority_boundary": {
            "candidate_only": True,
            "creative_facts_added": False,
            "release_approval_claimed": False,
            "automatic_publish": False,
        },
    }
    payload["decisions_sha256"] = payload_sha256(payload, "decisions_sha256")
    validate_spoken_edit_agent_decisions(payload, packet)
    return payload


def validate_spoken_edit_agent_decisions(
    payload: dict[str, Any],
    packet: dict[str, Any],
    *,
    require_complete: bool = False,
) -> None:
    validate_spoken_edit_judge_packet(packet)
    if payload.get("schema") != AGENT_DECISIONS_SCHEMA:
        raise ContractError("unsupported spoken edit agent decisions schema")
    if payload.get("decisions_sha256") != payload_sha256(payload, "decisions_sha256"):
        raise ContractError("spoken edit agent decisions integrity check failed")
    if payload.get("judge_packet_sha256") != packet.get("packet_sha256"):
        raise ContractError("agent decisions do not bind the judge packet")
    candidate_path = validate_artifact_reference(
        _object(packet.get("judge_candidates")), "judge candidates"
    )
    candidates = _read_candidate_array(candidate_path)
    expected_ids = {str(row["id"]) for row in candidates}
    by_id = {str(row["id"]): row for row in candidates}
    current_policy = packet["pacing_policy"] == DEFAULT_NATURAL_PACING_POLICY
    tokens = _read_tokens(Path(packet["tokens"]["path"])) if current_policy else []
    rows = payload.get("decisions")
    if not isinstance(rows, list) or not rows:
        raise ContractError("spoken edit agent decisions are empty")
    seen_candidates: set[str] = set()
    seen_decisions: set[str] = set()
    for raw in rows:
        if not isinstance(raw, dict):
            raise ContractError("spoken edit decision must be an object")
        decision_id = str(raw.get("decision_id") or "").strip()
        if not decision_id or decision_id in seen_decisions:
            raise ContractError("spoken edit decision ids must be unique")
        seen_decisions.add(decision_id)
        kind = str(raw.get("kind") or "")
        if kind not in OBJECTIVE_KINDS | SEMANTIC_KINDS:
            raise ContractError(f"unsupported spoken edit decision kind: {kind}")
        action = str(raw.get("action") or "")
        if action not in DECISION_ACTIONS:
            raise ContractError(f"unsupported spoken edit decision action: {action}")
        start = _seconds(raw.get("start"), "decision start")
        end = _seconds(raw.get("end"), "decision end")
        if end <= start:
            raise ContractError("spoken edit decision interval is invalid")
        confidence = _confidence(raw.get("confidence"))
        if (
            action in {"delete", "compress"}
            and not str(raw.get("reason") or "").strip()
        ):
            raise ContractError("spoken edit deletion requires a visible reason")
        if action == "compress":
            if kind != "pause":
                raise ContractError("compress action is only valid for a pause")
            preserve = _seconds(raw.get("preserve_s"), "preserve_s")
            if preserve <= 0 or preserve >= end - start:
                raise ContractError("pause compression preserve_s is invalid")
        elif raw.get("preserve_s") not in (None, 0, 0.0):
            raise ContractError("preserve_s is only valid for pause compression")
        candidate_id = str(raw.get("candidate_id") or "").strip()
        if kind in OBJECTIVE_KINDS:
            if candidate_id not in expected_ids or candidate_id in seen_candidates:
                raise ContractError("objective candidate decisions must be one-to-one")
            seen_candidates.add(candidate_id)
            if current_policy:
                validate_pause_decision(raw, by_id[candidate_id], tokens)
        elif candidate_id:
            raise ContractError(
                "semantic context decisions must not reuse candidate ids"
            )
        if action == "review_required" and confidence != 0.0:
            raise ContractError("unreviewed decisions must use zero confidence")
    if seen_candidates != expected_ids:
        raise ContractError("every objective candidate must receive one decision")
    status = str(payload.get("status") or "")
    review = _object(payload.get("review"))
    if status not in {"awaiting_agent_context_review", "agent_review_complete"}:
        raise ContractError("spoken edit decision status is invalid")
    if (
        review.get("source_speech_is_authoritative") is not True
        or review.get("reference_script_is_authoritative") is not False
    ):
        raise ContractError("spoken edit authority boundary is invalid")
    fulltext_sha = str(packet["judge_fulltext"]["sha256"])
    complete = (
        status == "agent_review_complete"
        and review.get("method") == "agent_full_context_review"
        and review.get("fulltext_read") is True
        and review.get("semantic_retry_scan_completed") is True
        and payload.get("reviewed_fulltext_sha256") == fulltext_sha
        and all(row.get("action") != "review_required" for row in rows)
    )
    if current_policy:
        validate_protected_events(payload.get("protected_events"))
        if complete:
            validate_boundary_review(payload.get("boundary_review"))
            validate_rough_audio_review(payload.get("rough_audio_review"))
            deletions = [{"start": r["start"], "end": r["end"]} for r in rows if r["action"] == "delete"]
            for row in rows:
                if row["kind"] == "retry_previous_take" and row["action"] == "delete":
                    validate_take_review(row.get("take_review"), row, deletions)
    if require_complete and not complete:
        raise ContractError("spoken edit decisions are not ready for NLE compilation")
    if status == "agent_review_complete" and not complete:
        raise ContractError("completed spoken edit review is internally inconsistent")
    if _object(payload.get("authority_boundary")) != {
        "candidate_only": True,
        "creative_facts_added": False,
        "release_approval_claimed": False,
        "automatic_publish": False,
    }:
        raise ContractError("spoken edit decision authority boundary is invalid")


def restore_spoken_edit_decision(
    packet_path: str | Path,
    decisions_path: str | Path,
    restore_from_path: str | Path,
    decision_id: str,
) -> dict[str, Any]:
    """Restore one reviewed row from an immutable version of the same packet.

    This returns a new candidate spec only; it never renders or approves a cut.
    Full-packet binding and the existing validators remain authoritative.
    """
    packet = read_json_object(packet_path, "spoken edit judge packet")
    current = read_json_object(decisions_path, "current spoken edit decisions")
    previous = read_json_object(restore_from_path, "previous spoken edit decisions")
    for value in (current, previous):
        validate_spoken_edit_agent_decisions(value, packet, require_complete=True)
    current_ids = {row["decision_id"] for row in current["decisions"]}
    previous_rows = {row["decision_id"]: row for row in previous["decisions"]}
    if decision_id not in current_ids or decision_id not in previous_rows:
        raise ContractError("restore requires the decision id in both versions")
    restored = deepcopy(current)
    restored["decisions"] = [
        deepcopy(previous_rows[decision_id]) if row["decision_id"] == decision_id else row
        for row in restored["decisions"]
    ]
    restored["decisions_sha256"] = payload_sha256(restored, "decisions_sha256")
    validate_spoken_edit_agent_decisions(restored, packet, require_complete=True)
    return restored


def build_spoken_edit_plan(
    packet_path: str | Path,
    decisions_path: str | Path,
    output_dir: str | Path,
    *,
    timeout_s: float = 60.0,
) -> dict[str, Any]:
    packet_file = Path(packet_path).expanduser().resolve()
    decisions_file = Path(decisions_path).expanduser().resolve()
    output = Path(output_dir).expanduser().resolve()
    packet = read_json_object(packet_file, "spoken edit judge packet")
    decisions = read_json_object(decisions_file, "spoken edit agent decisions")
    validate_spoken_edit_judge_packet(packet)
    validate_spoken_edit_agent_decisions(decisions, packet, require_complete=True)
    if packet["pacing_policy"] != DEFAULT_NATURAL_PACING_POLICY:
        raise ContractError("legacy pacing packet is read-only; prepare current policy before new compilation")
    source = _object(packet["videocut_kit"])
    source_video = validate_artifact_reference(
        _object(packet["source_video"]), "source video"
    )
    tokens_path = validate_artifact_reference(
        _object(packet["tokens"]), "transcript tokens"
    )
    silence_path = None
    if packet.get("silence") is not None:
        silence_path = validate_artifact_reference(
            _object(packet["silence"]), "silence intervals"
        )
    duration_s = _media_duration(source_video)
    plan_path = output / "spoken-edit-plan.json"
    if plan_path.is_file():
        plan = read_json_object(plan_path, "spoken edit plan")
        validate_spoken_edit_plan(plan)
        if plan.get("source_packet") != artifact_reference(
            packet_file, "spoken edit judge packet"
        ) or plan.get("source_agent_decisions") != artifact_reference(
            decisions_file, "spoken edit agent decisions"
        ):
            raise ContractError("existing spoken edit plan belongs to other inputs")
        return plan

    prepared = _effective_deletion_intents(decisions)
    output.mkdir(parents=True, exist_ok=True)
    node = shutil.which("node")
    if not node:
        raise ContractError("Node.js is required to execute videocut-kit")
    with tempfile.TemporaryDirectory(prefix="spoken-plan-", dir=output) as temp:
        temp_dir = Path(temp)
        input_decisions = temp_dir / "edit.decisions.input.json"
        snapped_path = temp_dir / "edit.decisions.snapped.json"
        input_decisions.write_text(
            json.dumps(prepared, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        snap_command = [
            node,
            source["scripts"]["snap_boundaries"]["path"],
            str(input_decisions),
            str(tokens_path),
            str(snapped_path),
        ]
        if silence_path is not None:
            snap_command.extend(["--silence", str(silence_path)])
        snap_result = _run(snap_command, timeout_s, "videocut-kit snap_boundaries")
        snapped = bound_snapped_pauses(_read_decision_array(snapped_path), prepared)
        (temp_dir / "edit.decisions.json").write_text(
            json.dumps(snapped, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        auto_command = [
            node,
            source["scripts"]["auto_cut"]["path"],
            str(temp_dir),
            "--dry-run",
            "--duration",
            f"{duration_s:.6f}",
            "--min-conf",
            str(DEFAULT_NATURAL_PACING_POLICY["minimum_delete_confidence"]),
        ]
        auto_result = _run(auto_command, timeout_s, "videocut-kit auto_cut dry-run")
        effective = _read_decision_array(temp_dir / "edit.decisions.json")
        delete_segments = _read_interval_array(temp_dir / "delete_segments.json")
        keep_segments = _read_interval_array(temp_dir / "cut.segments.json")
        report = (temp_dir / "auto_cut.report.md").read_text(encoding="utf-8")
        _validate_segment_complement(delete_segments, keep_segments, duration_s)
        validate_compiled_pause_bounds(decisions["decisions"], delete_segments)
        validate_edit_boundaries(delete_segments, _read_tokens(tokens_path), decisions["protected_events"])
        validate_protected_events(decisions["protected_events"], duration_s)
        snapped_output = atomic_write_text(
            json.dumps(snapped, ensure_ascii=False, indent=2) + "\n",
            output / "edit.decisions.snapped.json",
        )
        effective_output = atomic_write_text(
            json.dumps(effective, ensure_ascii=False, indent=2) + "\n",
            output / "edit.decisions.effective.json",
        )
        delete_output = atomic_write_text(
            json.dumps(delete_segments, ensure_ascii=False, indent=2) + "\n",
            output / "delete_segments.json",
        )
        keep_output = atomic_write_text(
            json.dumps(keep_segments, ensure_ascii=False, indent=2) + "\n",
            output / "cut.segments.json",
        )
        report_output = atomic_write_text(report, output / "spoken-edit-report.md")

    nle_segments = _nle_segments(source_video, packet["source_video"], keep_segments)
    from .segment_leveling import validate_leveling
    level_spec = atomic_write_json(dict(phase="before", audio_content="dialogue_only",
        source_media=packet["source_video"], measured_media=packet["source_video"],
        segments=keep_segments, speech_windows=[t for t in _read_tokens(tokens_path) if not t.get("isGap")]),
        output / "segment-level-spec.json")
    level_review = decisions["rough_audio_review"].get("segment_leveling")
    if not isinstance(level_review, dict) or not level_review.get("before_ref"):
        raise ContractError("rough cut pending segment speech levels; cut recipe preserved. "
            f"Run inspect-segment-levels --audio <dialogue-only PCM> --spec {level_spec} --output <before.json>; "
            "then segment-leveling-template, review gains and recompile. Whole-track LUFS is insufficient.")
    audio_leveling = validate_leveling(
        decisions["rough_audio_review"].get("segment_leveling"),
        keeps=keep_segments, source=packet["source_video"],
        gains=decisions.get("clip_gains", []),
        speech_windows=[t for t in _read_tokens(tokens_path) if not t.get("isGap")])
    removed_s = round(sum(row["end"] - row["start"] for row in delete_segments), 3)
    kept_s = round(sum(row["end"] - row["start"] for row in keep_segments), 3)
    plan: dict[str, Any] = {
        "schema": SPOKEN_EDIT_PLAN_SCHEMA,
        "source_packet": artifact_reference(packet_file, "spoken edit judge packet"),
        "source_agent_decisions": artifact_reference(
            decisions_file, "spoken edit agent decisions"
        ),
        "source_video": dict(packet["source_video"]),
        "source_duration_s": round(duration_s, 3),
        "pacing_policy": deepcopy(DEFAULT_NATURAL_PACING_POLICY),
        "editing_policy": deepcopy(EDITING_POLICY),
        "clip_gains": validate_clip_gains(decisions.get("clip_gains"), keep_segments),
        "segment_leveling": decisions["rough_audio_review"]["segment_leveling"],
        "rough_cut_audio_gate": audio_leveling,
        "audio_splices": splice_fades(keep_segments, _read_tokens(tokens_path), decisions["protected_events"]),
        "boundary_review_windows": boundary_windows(keep_segments, kept_s),
        "videocut_kit": source,
        "artifacts": {
            "snapped_decisions": artifact_reference(
                snapped_output, "snapped spoken edit decisions"
            ),
            "effective_decisions": artifact_reference(
                effective_output, "effective spoken edit decisions"
            ),
            "delete_segments": artifact_reference(
                delete_output, "spoken edit delete segments"
            ),
            "keep_segments": artifact_reference(
                keep_output, "spoken edit keep segments"
            ),
            "report": artifact_reference(report_output, "spoken edit report"),
        },
        "summary": {
            "objective_candidate_count": packet["candidate_count"],
            "agent_decision_count": len(decisions["decisions"]),
            "executed_delete_segment_count": len(delete_segments),
            "kept_segment_count": len(keep_segments),
            "removed_duration_s": removed_s,
            "kept_duration_s": kept_s,
        },
        "nle_segments": nle_segments,
        "execution_receipt": {
            "prepare_judge_executed": True,
            "snap_boundaries_executed": True,
            "auto_cut_dry_run_executed": True,
            "auto_cut_returncode": auto_result.returncode,
            "snap_returncode": snap_result.returncode,
            "ffmpeg_executed": False,
            "video_rendered": False,
        },
        "next_action": "select_delivery_from_request",
        "authority_boundary": {
            "candidate_only": True,
            "source_media_modified": False,
            "reference_script_overrode_speech": False,
            "hyperframes_executed": False,
            "effects_added": False,
            "automatic_publish": False,
        },
    }
    plan["plan_sha256"] = payload_sha256(plan, "plan_sha256")
    validate_spoken_edit_plan(plan)
    atomic_write_json(plan, plan_path)
    return plan


def validate_spoken_edit_plan(plan: dict[str, Any]) -> None:
    if plan.get("schema") not in {SPOKEN_EDIT_PLAN_SCHEMA, LEGACY_SPOKEN_EDIT_PLAN_SCHEMA}:
        raise ContractError("unsupported spoken edit plan schema")
    if plan.get("plan_sha256") != payload_sha256(plan, "plan_sha256"):
        raise ContractError("spoken edit plan integrity check failed")
    packet_path = validate_artifact_reference(
        _object(plan.get("source_packet")), "spoken edit judge packet"
    )
    decisions_path = validate_artifact_reference(
        _object(plan.get("source_agent_decisions")), "spoken edit agent decisions"
    )
    packet = read_json_object(packet_path, "spoken edit judge packet")
    decisions = read_json_object(decisions_path, "spoken edit agent decisions")
    validate_spoken_edit_judge_packet(packet)
    validate_spoken_edit_agent_decisions(decisions, packet, require_complete=True)
    if plan.get("source_video") != packet.get("source_video"):
        raise ContractError("spoken edit plan source video drifted")
    source_video = validate_artifact_reference(
        _object(plan.get("source_video")), "source video"
    )
    duration_s = _seconds(plan.get("source_duration_s"), "source duration")
    if duration_s <= 0:
        raise ContractError("spoken edit source duration is invalid")
    if plan.get("pacing_policy") != packet.get("pacing_policy"):
        raise ContractError("spoken edit pacing policy drifted")
    source = _object(plan.get("videocut_kit"))
    if source != _videocut_source_manifest(str(source.get("root") or "")):
        raise ContractError("spoken edit videocut-kit source drifted")
    artifacts = _object(plan.get("artifacts"))
    for field, label in (
        ("snapped_decisions", "snapped spoken edit decisions"),
        ("effective_decisions", "effective spoken edit decisions"),
        ("delete_segments", "spoken edit delete segments"),
        ("keep_segments", "spoken edit keep segments"),
        ("report", "spoken edit report"),
    ):
        validate_artifact_reference(_object(artifacts.get(field)), label)
    delete_segments = _read_interval_array(Path(artifacts["delete_segments"]["path"]))
    keep_segments = _read_interval_array(Path(artifacts["keep_segments"]["path"]))
    _validate_segment_complement(delete_segments, keep_segments, duration_s)
    if plan["pacing_policy"] == DEFAULT_NATURAL_PACING_POLICY:
        validate_compiled_pause_bounds(decisions["decisions"], delete_segments)
        if plan.get("clip_gains") != validate_clip_gains(decisions.get("clip_gains"), keep_segments):
            raise ContractError("clip gain review drifted")
        from .segment_leveling import validate_leveling
        if plan.get("segment_leveling") != decisions["rough_audio_review"].get("segment_leveling"):
            raise ContractError("segment leveling review drifted")
        gate = validate_leveling(plan.get("segment_leveling"), keeps=keep_segments,
                                 source=plan["source_video"], gains=plan["clip_gains"],
                                 speech_windows=[t for t in _read_tokens(Path(packet["tokens"]["path"])) if not t.get("isGap")])
        if plan.get("rough_cut_audio_gate") != gate:
            raise ContractError("rough-cut audio gate drifted")
        tokens = _read_tokens(Path(packet["tokens"]["path"]))
        validate_edit_boundaries(delete_segments, tokens, decisions["protected_events"])
        validate_protected_events(decisions["protected_events"], duration_s)
        if plan.get("editing_policy") != EDITING_POLICY or plan.get("audio_splices") != splice_fades(keep_segments, tokens, decisions["protected_events"]):
            raise ContractError("editorial protection or audio splice policy drifted")
        if plan.get("boundary_review_windows") != boundary_windows(keep_segments, round(sum(r["end"]-r["start"] for r in keep_segments), 3)):
            raise ContractError("boundary review windows drifted")
    expected_nle = _nle_segments(source_video, packet["source_video"], keep_segments)
    if plan.get("nle_segments") != expected_nle:
        raise ContractError("spoken edit NLE segments drifted")
    summary = _object(plan.get("summary"))
    expected_removed = round(
        sum(row["end"] - row["start"] for row in delete_segments), 3
    )
    expected_kept = round(sum(row["end"] - row["start"] for row in keep_segments), 3)
    if summary != {
        "objective_candidate_count": packet["candidate_count"],
        "agent_decision_count": len(decisions["decisions"]),
        "executed_delete_segment_count": len(delete_segments),
        "kept_segment_count": len(keep_segments),
        "removed_duration_s": expected_removed,
        "kept_duration_s": expected_kept,
    }:
        raise ContractError("spoken edit plan summary drifted")
    receipt = _object(plan.get("execution_receipt"))
    if (
        receipt.get("prepare_judge_executed") is not True
        or receipt.get("snap_boundaries_executed") is not True
        or receipt.get("auto_cut_dry_run_executed") is not True
        or receipt.get("auto_cut_returncode") != 0
        or receipt.get("snap_returncode") != 0
        or receipt.get("ffmpeg_executed") is not False
        or receipt.get("video_rendered") is not False
    ):
        raise ContractError("spoken edit execution receipt is invalid")
    expected_next = (
        "materialize_editable_jianying_candidate"
        if plan["schema"] == LEGACY_SPOKEN_EDIT_PLAN_SCHEMA
        else "select_delivery_from_request"
    )
    # Legacy plans remain readable evidence; their old next_action is never an
    # execution directive. All new delivery consumers require a separate request.
    if plan.get("next_action") != expected_next:
        raise ContractError("spoken edit plan next action is invalid")
    if _object(plan.get("authority_boundary")) != {
        "candidate_only": True,
        "source_media_modified": False,
        "reference_script_overrode_speech": False,
        "hyperframes_executed": False,
        "effects_added": False,
        "automatic_publish": False,
    }:
        raise ContractError("spoken edit plan authority boundary is invalid")


def write_spoken_edit_agent_decisions(
    payload: dict[str, Any], packet_path: str | Path, output_path: str | Path
) -> Path:
    packet = read_json_object(packet_path, "spoken edit judge packet")
    validate_spoken_edit_agent_decisions(payload, packet)
    return atomic_write_json(payload, output_path)


def _default_candidate_decision(candidate: dict[str, Any]) -> dict[str, Any]:
    kind = str(candidate["kind"])
    start = float(candidate["start"])
    end = float(candidate["end"])
    action = "review_required"
    reason = "需要结合全文判断是否有实际语义"
    confidence = 0.0
    where = str(candidate.get("where") or "")
    recommendation = None
    if kind == "pause":
        recommendation = pause_recommendation(start, end, where, candidate.get("pause_role", "unknown"))
        reason = recommendation["reason"]
        if recommendation["action"] == "keep":
            action = "keep"
    row: dict[str, Any] = {
        "decision_id": f"candidate-{candidate['id']}",
        "candidate_id": str(candidate["id"]),
        "kind": kind,
        "action": action,
        "start": start,
        "end": end,
        "reason": reason,
        "confidence": confidence,
        "context": str(candidate.get("context") or ""),
    }
    if kind == "pause":
        row.update(where=where, recommendation=recommendation, pause_review={
            "semantic_role": candidate.get("pause_role", "unknown"),
            "audio_checked": False, "context_checked": False, "evidence": "",
        })
    return row


def _effective_deletion_intents(decisions: dict[str, Any]) -> list[dict[str, Any]]:
    prepared: list[dict[str, Any]] = []
    for raw in decisions["decisions"]:
        action = str(raw["action"])
        if action not in {"delete", "compress"}:
            continue
        start = float(raw["start"])
        end = float(raw["end"])
        if action == "compress":
            start, end = pause_cut_window(start, end, float(raw["preserve_s"]), raw["where"])
        prepared.append(
            {
                "id": str(raw["decision_id"]),
                "kind": str(raw["kind"]),
                "action": "delete",
                "start": round(start, 3),
                "end": round(end, 3),
                "reason": str(raw["reason"]),
                "confidence": float(raw["confidence"]),
                "source_action": action,
            }
        )
    return prepared


def _nle_segments(
    source_video: Path,
    source_reference: dict[str, Any],
    keep_segments: list[dict[str, float]],
) -> list[dict[str, Any]]:
    rows = []
    for index, interval in enumerate(keep_segments, start=1):
        start = round(float(interval["start"]), 3)
        end = round(float(interval["end"]), 3)
        rows.append(
            {
                "segment_id": f"spoken-keep-{index:04d}",
                "candidate_id": f"spoken-keep-{index:04d}",
                "timeline_index": index,
                "source_path": str(source_video),
                "source_bytes": source_reference["bytes"],
                "source_sha256": source_reference["sha256"],
                "in_s": start,
                "out_s": end,
                "duration_s": round(end - start, 3),
                "label": f"口播净剪保留段 {index}",
            }
        )
    return rows


def _videocut_source_manifest(source_root: str | Path) -> dict[str, Any]:
    root = Path(source_root).expanduser().resolve()
    if not root.is_dir():
        raise ContractError("videocut-kit source root was not found")
    if _git_head(root) != VIDEOCUT_KIT_COMMIT:
        raise ContractError("videocut-kit commit drifted from the reviewed source")
    scripts: dict[str, Any] = {}
    for name, (relative, expected_hash) in VIDEOCUT_SCRIPTS.items():
        path = root / relative
        if not path.is_file() or file_sha256(path) != expected_hash:
            raise ContractError(f"videocut-kit script drifted: {relative}")
        scripts[name] = {
            "path": str(path),
            "sha256": expected_hash,
            "relative_path": relative,
        }
    return {
        "repository": VIDEOCUT_KIT_REPOSITORY,
        "commit": VIDEOCUT_KIT_COMMIT,
        "root": str(root),
        "scripts": scripts,
        "license_status": "no_repository_license_detected",
        "distribution_policy": "external_source_required_no_vendoring",
    }


def _git_head(root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ContractError(f"videocut-kit commit could not be read: {exc}") from exc
    head = result.stdout.strip()
    if result.returncode != 0 or len(head) != 40:
        raise ContractError("videocut-kit commit could not be verified")
    return head


def _read_tokens(path: Path) -> list[dict[str, Any]]:
    rows = _read_json_array(path, "transcript tokens")
    speech_count = 0
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("isGap"), bool):
            raise ContractError("transcript token requires boolean isGap")
        start = _seconds(row.get("start"), "token start")
        end = _seconds(row.get("end"), "token end")
        if end < start:
            raise ContractError("transcript token interval is invalid")
        if not row["isGap"]:
            speech_count += 1
            if not str(row.get("original_text") or ""):
                raise ContractError("speech token requires original_text")
    if speech_count == 0:
        raise ContractError("transcript tokens contain no speech")
    return rows


def _read_silences(path: Path) -> list[dict[str, Any]]:
    rows = _read_json_array(path, "silence intervals")
    for row in rows:
        if not isinstance(row, dict):
            raise ContractError("silence interval must be an object")
        start = _seconds(row.get("start"), "silence start")
        end = _seconds(row.get("end"), "silence end")
        if end <= start:
            raise ContractError("silence interval is invalid")
    return rows


def _read_candidate_array(path: Path) -> list[dict[str, Any]]:
    rows = _read_json_array(path, "judge candidates")
    seen: set[str] = set()
    for raw in rows:
        if not isinstance(raw, dict):
            raise ContractError("judge candidate must be an object")
        candidate_id = str(raw.get("id") or "")
        kind = str(raw.get("kind") or "")
        if not candidate_id or candidate_id in seen or kind not in OBJECTIVE_KINDS:
            raise ContractError("judge candidate id or kind is invalid")
        seen.add(candidate_id)
        start = _seconds(raw.get("start"), "candidate start")
        end = _seconds(raw.get("end"), "candidate end")
        if end <= start:
            raise ContractError("judge candidate interval is invalid")
    return rows


def _read_decision_array(path: Path) -> list[dict[str, Any]]:
    rows = _read_json_array(path, "spoken edit decisions")
    for row in rows:
        if not isinstance(row, dict):
            raise ContractError("spoken edit decision output must be an object")
        start = _seconds(row.get("start"), "decision start")
        end = _seconds(row.get("end"), "decision end")
        if end <= start:
            raise ContractError("spoken edit decision output interval is invalid")
    return rows


def _read_interval_array(path: Path) -> list[dict[str, float]]:
    rows = _read_json_array(path, "edit intervals")
    result: list[dict[str, float]] = []
    last_end = -1.0
    for raw in rows:
        if not isinstance(raw, dict):
            raise ContractError("edit interval must be an object")
        start = _seconds(raw.get("start"), "interval start")
        end = _seconds(raw.get("end"), "interval end")
        if end <= start or start < last_end - 0.001:
            raise ContractError("edit intervals must be positive and ordered")
        result.append({"start": start, "end": end})
        last_end = end
    return result


def _read_json_array(path: Path, label: str) -> list[Any]:
    if not path.is_file():
        raise ContractError(f"{label} not found: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"{label} is unreadable: {exc}") from exc
    if not isinstance(value, list):
        raise ContractError(f"{label} must be a JSON array")
    return value


def _validate_segment_complement(
    deletes: list[dict[str, float]],
    keeps: list[dict[str, float]],
    duration_s: float,
) -> None:
    if not keeps:
        raise ContractError("spoken edit plan would delete the entire source")
    intervals = sorted(
        [(row["start"], row["end"], "delete") for row in deletes]
        + [(row["start"], row["end"], "keep") for row in keeps]
    )
    cursor = 0.0
    for start, end, _kind in intervals:
        if abs(start - cursor) > 0.011 or end <= start or end > duration_s + 0.011:
            raise ContractError("delete and keep intervals do not cover the source")
        cursor = end
    if abs(cursor - duration_s) > 0.011:
        raise ContractError("delete and keep intervals do not reach source duration")


def _media_duration(path: Path) -> float:
    probe = probe_media_metadata(path)
    format_info = _object(probe.get("format"))
    candidates = [format_info.get("duration")]
    for stream in probe.get("streams") or []:
        if isinstance(stream, dict):
            candidates.append(stream.get("duration"))
    for raw in candidates:
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if math.isfinite(value) and value > 0:
            return value
    raise ContractError("source video duration is unavailable")


def _run(
    command: list[str], timeout_s: float, label: str
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=float(timeout_s),
            check=False,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ContractError(f"{label} failed to start: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "unknown error").strip()[-2000:]
        raise ContractError(f"{label} failed: {detail}")
    return result


def _seconds(value: Any, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{label} must be numeric") from exc
    if not math.isfinite(number) or number < 0:
        raise ContractError(f"{label} must be a finite nonnegative number")
    return number


def _confidence(value: Any) -> float:
    number = _seconds(value, "decision confidence")
    if number > 1:
        raise ContractError("decision confidence must be between zero and one")
    return number


def _object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}
