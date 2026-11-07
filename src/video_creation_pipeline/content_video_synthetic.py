from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .content_video_pipeline import validate_content_video_project_plan
from .contract_io import (
    artifact_reference,
    atomic_copy_promote,
    atomic_write_json,
    atomic_write_text,
    payload_sha256,
    read_json_object,
    validate_artifact_reference,
)
from .errors import ContractError
from .execution_receipts import (
    FFMPEG_OUTLET_ID,
    execute_ffmpeg_with_receipt,
    validate_ffmpeg_execution_receipt,
)
from .music_pipeline import (
    build_music_asset_profile,
    build_music_candidate_set,
    build_music_edit_plan,
    build_music_intent_curve,
    build_music_mix_command,
    build_music_retrieval_results,
    write_music_contract,
)
from .technical_qa import build_technical_qa_report, write_technical_qa_report

SYNTHETIC_RUN_RECEIPT_SCHEMA = (
    "video_creation_pipeline.content_video_synthetic_run_receipt.v2"
)
CONTENT_VIDEO_RENDER_MANIFEST_SCHEMA = (
    "video_creation_pipeline.content_video_render_manifest.v1"
)
CONTENT_VIDEO_ATOMIC_OUTPUT_MANIFEST_SCHEMA = (
    "video_creation_pipeline.content_video_atomic_output_manifest.v1"
)


def generate_synthetic_candidate(
    plan_root: str | Path,
    *,
    timeout_s: float = 180.0,
) -> dict[str, Any]:
    root = Path(plan_root).expanduser().resolve()
    plan_path = root / "project-plan.json"
    plan = read_json_object(plan_path, "content video project plan")
    validate_content_video_project_plan(plan)
    if plan["readiness"]["synthetic_fixture_render_eligible"] is not True:
        raise ContractError("content video plan is blocked from synthetic rendering")
    _validate_plan_bundle(root, plan)
    _write_owner_marker(root, plan)

    scenes_dir = root / "synthetic" / "scenes"
    receipts_dir = root / "render" / "receipts"
    staging_dir = root / "render" / "staging"
    scene_staging_dir = staging_dir / "scenes"
    publish_dir = root / "publish"
    qa_dir = root / "qa"
    for directory in (
        scenes_dir,
        receipts_dir,
        scene_staging_dir,
        publish_dir,
        qa_dir,
    ):
        directory.mkdir(parents=True, exist_ok=True)

    receipt_paths: list[Path] = []
    promotions: dict[Path, Path] = {}
    scene_paths = []
    for index, (unit, scene) in enumerate(
        zip(
            plan["shot_units"],
            plan["packaging_plan"]["scenes"],
            strict=True,
        ),
        1,
    ):
        scene_path = scenes_dir / f"{unit['scene_id']}.mp4"
        scene_staging = scene_staging_dir / f"{unit['scene_id']}.mp4"
        shot_path = root / "shot-units" / f"{unit['scene_id']}.json"
        duration = float(scene["end_s"]) - float(scene["start_s"])
        command = _scene_command(unit["scene_id"], duration, scene_staging, index)
        receipt_path = receipts_dir / f"{unit['scene_id']}-render.json"
        execute_ffmpeg_with_receipt(
            "render",
            command,
            [
                {"role": "project_plan", "path": plan_path},
                {"role": "shot_unit", "path": shot_path},
            ],
            [{"role": "synthetic_scene_staging", "path": scene_staging}],
            receipt_path,
            timeout_s=timeout_s,
        )
        _promote_ffmpeg_output(
            scene_staging,
            scene_path,
            promotions,
            label=f"synthetic scene {unit['scene_id']}",
        )
        receipt_paths.append(receipt_path)
        scene_paths.append(scene_path)

    concat_path = root / "render" / "synthetic-scenes.concat.txt"
    atomic_write_text(
        "\n".join(f"file '{path.as_posix()}'" for path in scene_paths) + "\n",
        concat_path,
    )
    program_path = root / "render" / "synthetic-program.mp4"
    program_staging = staging_dir / "synthetic-program.mp4"
    concat_command = [
        "ffmpeg",
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(concat_path),
        "-map_metadata",
        "-1",
        "-fflags",
        "+bitexact",
        "-flags:v",
        "+bitexact",
        "-flags:a",
        "+bitexact",
        "-threads",
        "1",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        str(program_staging),
    ]
    concat_receipt = receipts_dir / "synthetic-program-concat.json"
    execute_ffmpeg_with_receipt(
        "concat",
        concat_command,
        [
            {"role": f"scene_{index:02}", "path": path}
            for index, path in enumerate(scene_paths, 1)
        ],
        [{"role": "synthetic_program_staging", "path": program_staging}],
        concat_receipt,
        timeout_s=timeout_s,
    )
    _promote_ffmpeg_output(
        program_staging,
        program_path,
        promotions,
        label="synthetic program",
    )
    receipt_paths.append(concat_receipt)

    music_plan_path, music_receipts = _prepare_synthetic_music(
        root,
        plan,
        promotions,
        timeout_s=timeout_s,
    )
    receipt_paths.extend(music_receipts)
    music_plan = read_json_object(music_plan_path, "music edit plan")
    selection = _single_selection(music_plan)
    music_path = Path(selection["source_audio"]["path"]).expanduser().resolve()
    candidate_path = publish_dir / "candidate-synthetic.mp4"
    candidate_staging = staging_dir / "candidate-synthetic.mp4"
    mix_command = build_music_mix_command(
        program_path,
        music_path,
        candidate_staging,
        selection,
        deterministic=True,
    )
    mix_receipt = receipts_dir / "synthetic-music-mix.json"
    execute_ffmpeg_with_receipt(
        "music_mix",
        mix_command,
        [
            {"role": "program_video", "path": program_path},
            {"role": "selected_music", "path": music_path},
            {"role": "music_edit_plan", "path": music_plan_path},
        ],
        [{"role": "mixed_program_video_staging", "path": candidate_staging}],
        mix_receipt,
        timeout_s=timeout_s,
    )
    _promote_ffmpeg_output(
        candidate_staging,
        candidate_path,
        promotions,
        label="synthetic candidate",
    )
    receipt_paths.append(mix_receipt)

    cover_path = publish_dir / "cover-synthetic.png"
    cover_staging = staging_dir / "cover-synthetic.png"
    cover_command = [
        "ffmpeg",
        "-y",
        "-ss",
        "1",
        "-i",
        str(candidate_path),
        "-frames:v",
        "1",
        "-update",
        "1",
        str(cover_staging),
    ]
    cover_receipt = receipts_dir / "synthetic-cover-extract.json"
    execute_ffmpeg_with_receipt(
        "frame_extract",
        cover_command,
        [{"role": "candidate_video", "path": candidate_path}],
        [{"role": "frame", "path": cover_staging, "media_time_s": 1.0}],
        cover_receipt,
        timeout_s=timeout_s,
    )
    _promote_ffmpeg_output(
        cover_staging,
        cover_path,
        promotions,
        label="synthetic cover",
    )
    receipt_paths.append(cover_receipt)

    qa_report_path, qa_receipt = _run_technical_qa(
        root,
        plan,
        candidate_path,
        timeout_s=timeout_s,
    )
    receipt_paths.append(qa_receipt)
    qa_report = read_json_object(qa_report_path, "technical QA report")
    if qa_report["readiness"]["technical_ready"] is not True:
        raise ContractError("synthetic candidate failed automatic technical QA")

    atomic_output_manifest = build_content_video_atomic_output_manifest(
        plan_path,
        receipt_paths,
        promotions,
    )
    atomic_output_manifest_path = atomic_write_json(
        atomic_output_manifest,
        root / "render" / "atomic-output-manifest.json",
    )

    manifest = _build_render_manifest(
        root,
        plan_path,
        candidate_path,
        cover_path,
        qa_report_path,
        music_plan_path,
        atomic_output_manifest_path,
        receipt_paths,
    )
    manifest_path = atomic_write_json(
        manifest,
        root / "render-manifest.json",
    )
    run_receipt: dict[str, Any] = {
        "schema": SYNTHETIC_RUN_RECEIPT_SCHEMA,
        "status": "completed_candidate_only",
        "source_plan": artifact_reference(plan_path, "content video project plan"),
        "render_manifest": artifact_reference(manifest_path, "render manifest"),
        "atomic_output_manifest": artifact_reference(
            atomic_output_manifest_path, "atomic output manifest"
        ),
        "candidate_video": artifact_reference(candidate_path, "candidate video"),
        "qa_report": artifact_reference(qa_report_path, "technical QA report"),
        "ffmpeg_receipt_count": len(receipt_paths),
        "synthetic_material_only": True,
        "production_success_claimed": False,
        "upload_performed": False,
        "publish_performed": False,
    }
    run_receipt["receipt_sha256"] = payload_sha256(
        run_receipt,
        "receipt_sha256",
    )
    atomic_write_json(run_receipt, root / "synthetic-run-receipt.json")
    return run_receipt


def _prepare_synthetic_music(
    root: Path,
    plan: dict[str, Any],
    promotions: dict[Path, Path],
    *,
    timeout_s: float,
) -> tuple[Path, list[Path]]:
    duration = float(plan["target_duration_s"])
    plan_path = root / "project-plan.json"
    music_dir = root / "synthetic" / "music"
    music_staging_dir = root / "render" / "staging" / "music"
    receipts_dir = root / "render" / "receipts"
    music_dir.mkdir(parents=True, exist_ok=True)
    music_staging_dir.mkdir(parents=True, exist_ok=True)
    receipts: list[Path] = []
    profiles = []
    fixtures = (
        (
            "calm",
            196,
            "clear_explainer",
            _descriptors(0.56, 0.30, 0.18, 0.40, 0.58, 0.24, 0.20),
        ),
        (
            "tense",
            330,
            "restrained_risk",
            _descriptors(0.18, 0.82, 0.88, 0.70, 0.12, 0.78, 0.86),
        ),
    )
    for asset_id, frequency, music_family, descriptors in fixtures:
        audio_path = music_dir / f"{asset_id}.wav"
        audio_staging = music_staging_dir / f"{asset_id}.wav"
        command = [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={frequency}:sample_rate=48000:duration={duration:.3f}",
            "-af",
            "volume=0.05",
            "-c:a",
            "pcm_s16le",
            str(audio_staging),
        ]
        receipt_path = receipts_dir / f"synthetic-music-{asset_id}.json"
        execute_ffmpeg_with_receipt(
            "render",
            command,
            [{"role": "project_plan", "path": plan_path}],
            [{"role": "synthetic_music_staging", "path": audio_staging}],
            receipt_path,
            timeout_s=timeout_s,
        )
        _promote_ffmpeg_output(
            audio_staging,
            audio_path,
            promotions,
            label=f"synthetic music {asset_id}",
        )
        receipts.append(receipt_path)
        profile = build_music_asset_profile(
            audio_path,
            asset_id=f"synthetic-{asset_id}",
            duration_s=duration,
            windows=[
                {
                    "window_id": f"synthetic-{asset_id}-main",
                    "start_s": 0.0,
                    "end_s": duration,
                    "structure_label": "synthetic-bed",
                    "structure_confidence": 1.0,
                    "descriptors": descriptors,
                    "vocal_probability": 0.0,
                    "tags": [
                        "instrumental",
                        asset_id,
                        music_family,
                        "synthetic-fixture",
                    ],
                    "loopable": True,
                }
            ],
            license_info={
                "status": "allowed",
                "commercial_use_allowed": True,
                "license_uri": f"local://project-owned-fixture/{asset_id}",
                "license_snapshot": "generated deterministic synthetic fixture",
                "attribution": "",
            },
            analyzer={
                "id": "deterministic-fixture",
                "model": "declared-synthetic-profile",
                "version": "1",
                "source_commit": "project-owned-fixture",
                "execution_location": "local",
                "model_license": "project-owned-fixture",
                "automatic_fallback_used": False,
            },
        )
        profile_path = root / "audio" / f"synthetic-{asset_id}-profile.json"
        write_music_contract(profile, profile_path)
        profiles.append(profile_path)

    production_intent_path = root / "audio" / "music-intent-curve.json"
    production_intent = read_json_object(
        production_intent_path,
        "production music intent curve",
    )
    synthetic_segment = dict(production_intent["segments"][0])
    synthetic_segment["narration_density"] = 0.0
    synthetic_segment["text_query"] += "；合成测试音轨，无旁白"
    synthetic_segment["text_query_en"] += "; synthetic fixture without speech"
    synthetic_intent = build_music_intent_curve(
        plan_path,
        [synthetic_segment],
        created_by="synthetic-fixture-no-speech",
    )
    intent_path = root / "audio" / "synthetic-music-intent-curve.json"
    write_music_contract(synthetic_intent, intent_path)
    intent = synthetic_intent
    segment_id = str(intent["segments"][0]["segment_id"])
    retrieval = build_music_retrieval_results(
        intent_path,
        [
            {
                "segment_id": segment_id,
                "asset_id": "synthetic-calm",
                "window_id": "synthetic-calm-main",
                "semantic_similarity": 0.96,
                "continuity_diversity": 0.8,
                "query_text": intent["segments"][0]["text_query_en"],
            },
            {
                "segment_id": segment_id,
                "asset_id": "synthetic-tense",
                "window_id": "synthetic-tense-main",
                "semantic_similarity": 0.35,
                "continuity_diversity": 0.7,
                "query_text": intent["segments"][0]["text_query_en"],
            },
        ],
        provider={
            "id": "reviewed-offline-fixture",
            "model": "deterministic-synthetic-retrieval",
            "version": "1",
            "source_commit": "project-owned-fixture",
            "execution_location": "local",
            "model_license": "project-owned-fixture",
        },
    )
    retrieval_path = root / "audio" / "synthetic-music-retrieval.json"
    write_music_contract(retrieval, retrieval_path)
    candidates = build_music_candidate_set(
        intent_path,
        retrieval_path,
        profiles,
        purpose="commercial",
    )
    candidates_path = root / "audio" / "synthetic-music-candidates.json"
    write_music_contract(candidates, candidates_path)
    music_plan = build_music_edit_plan(candidates_path)
    music_plan_path = root / "audio" / "synthetic-music-edit-plan.json"
    write_music_contract(music_plan, music_plan_path)
    if music_plan["readiness"]["render_eligible"] is not True:
        raise ContractError("synthetic music failed the existing confidence gate")
    return music_plan_path, receipts


def _run_technical_qa(
    root: Path,
    plan: dict[str, Any],
    candidate_path: Path,
    *,
    timeout_s: float,
) -> tuple[Path, Path]:
    qa_dir = root / "qa"
    probe_path = qa_dir / "ffprobe.json"
    atomic_write_json(_ffprobe(candidate_path), probe_path)
    detector_log = qa_dir / "ffmpeg-detection.log"
    detector_command = [
        "ffmpeg",
        "-hide_banner",
        "-nostats",
        "-v",
        "info",
        "-i",
        str(candidate_path),
        "-vf",
        "blackdetect=d=0.5:pix_th=0.10,freezedetect=n=-50dB:d=1.5",
        "-af",
        "silencedetect=n=-50dB:d=1.0",
        "-f",
        "null",
        "-",
    ]
    receipt_path = root / "render" / "receipts" / "synthetic-technical-qa.json"
    execute_ffmpeg_with_receipt(
        "technical_qa",
        detector_command,
        [{"role": "media", "path": candidate_path}],
        [{"role": "detector_log", "path": detector_log}],
        receipt_path,
        capture_log_path=detector_log,
        timeout_s=timeout_s,
    )
    report = build_technical_qa_report(
        candidate_path,
        probe_path,
        receipt_path,
        subtitles_path=root / "edit" / "subtitles.srt",
        packaging_plan_path=root / "render" / "video-packaging-plan.json",
        expected_duration_s=float(plan["target_duration_s"]),
    )
    report_path = qa_dir / "qa-report.json"
    write_technical_qa_report(report, report_path)
    return report_path, receipt_path


def _build_render_manifest(
    root: Path,
    plan_path: Path,
    candidate_path: Path,
    cover_path: Path,
    qa_report_path: Path,
    music_plan_path: Path,
    atomic_output_manifest_path: Path,
    receipt_paths: list[Path],
) -> dict[str, Any]:
    manifest: dict[str, Any] = {
        "schema": CONTENT_VIDEO_RENDER_MANIFEST_SCHEMA,
        "status": "candidate_only",
        "source_project_plan": artifact_reference(plan_path, "project plan"),
        "plan_manifest": artifact_reference(
            root / "plan-manifest.json", "plan manifest"
        ),
        "candidate_video": artifact_reference(candidate_path, "candidate video"),
        "subtitles": artifact_reference(root / "edit" / "subtitles.srt", "subtitles"),
        "cover": artifact_reference(cover_path, "cover"),
        "packaging_plan": artifact_reference(
            root / "render" / "video-packaging-plan.json", "packaging plan"
        ),
        "music_edit_plan": artifact_reference(music_plan_path, "music edit plan"),
        "atomic_output_manifest": artifact_reference(
            atomic_output_manifest_path, "atomic output manifest"
        ),
        "technical_qa": artifact_reference(qa_report_path, "technical QA report"),
        "ffmpeg_receipts": [
            artifact_reference(path, f"FFmpeg receipt {index}")
            for index, path in enumerate(receipt_paths, 1)
        ],
        "synthetic_material_only": True,
        "production_success_claimed": False,
        "external_provider_called": False,
        "upload_performed": False,
        "publish_performed": False,
    }
    manifest["render_manifest_sha256"] = payload_sha256(
        manifest,
        "render_manifest_sha256",
    )
    return manifest


def build_content_video_atomic_output_manifest(
    plan_path: str | Path,
    receipt_paths: list[str | Path],
    promotions: dict[Path, Path],
) -> dict[str, Any]:
    plan_file = Path(plan_path).expanduser().resolve()
    plan = read_json_object(plan_file, "content video project plan")
    validate_content_video_project_plan(plan)
    normalized_promotions = {
        Path(staged).expanduser().resolve(): Path(output).expanduser().resolve()
        for staged, output in promotions.items()
    }
    rows: list[dict[str, Any]] = []
    consumed_promotions: set[Path] = set()
    normalized_receipts: list[Path] = []
    for raw_receipt in receipt_paths:
        receipt_path = Path(raw_receipt).expanduser().resolve()
        receipt = read_json_object(receipt_path, "FFmpeg execution receipt")
        validate_ffmpeg_execution_receipt(receipt)
        normalized_receipts.append(receipt_path)
        for output_index, output_row in enumerate(receipt["outputs"]):
            ffmpeg_output = validate_artifact_reference(
                _object(output_row.get("artifact")),
                "FFmpeg output awaiting atomic commit",
            )
            role = str(output_row.get("role") or "").strip()
            if ffmpeg_output in normalized_promotions:
                committed_output = normalized_promotions[ffmpeg_output]
                consumed_promotions.add(ffmpeg_output)
                commit_mode = "copy_then_replace"
            elif receipt.get("operation") == "technical_qa" and role == "detector_log":
                committed_output = ffmpeg_output
                commit_mode = "atomic_text_replace"
            else:
                raise ContractError(
                    "canonical FFmpeg output lacks atomic commit evidence"
                )
            committed_ref = artifact_reference(
                committed_output, "atomically committed FFmpeg output"
            )
            ffmpeg_ref = _object(output_row.get("artifact"))
            if ffmpeg_ref.get("sha256") != committed_ref.get("sha256"):
                raise ContractError(
                    "atomically committed output differs from its FFmpeg output"
                )
            rows.append(
                {
                    "ffmpeg_receipt": artifact_reference(
                        receipt_path, "FFmpeg execution receipt"
                    ),
                    "receipt_output_index": output_index,
                    "receipt_output_role": role,
                    "ffmpeg_output": dict(ffmpeg_ref),
                    "commit_mode": commit_mode,
                    "committed_output": committed_ref,
                }
            )
    if consumed_promotions != set(normalized_promotions):
        raise ContractError(
            "atomic promotion evidence contains an unused staging output"
        )
    if len(set(normalized_receipts)) != len(normalized_receipts):
        raise ContractError("atomic output manifest cannot repeat an FFmpeg receipt")
    manifest: dict[str, Any] = {
        "schema": CONTENT_VIDEO_ATOMIC_OUTPUT_MANIFEST_SCHEMA,
        "status": "completed",
        "source_project_plan": artifact_reference(
            plan_file, "content video project plan"
        ),
        "ffmpeg_receipt_count": len(normalized_receipts),
        "ffmpeg_output_count": len(rows),
        "outputs": rows,
        "authority_boundary": {
            "ffmpeg_outlet": FFMPEG_OUTLET_ID,
            "all_ffmpeg_outputs_accounted_for": True,
            "direct_final_media_write_allowed": False,
            "source_media_modified": False,
            "automatic_publish": False,
        },
    }
    manifest["atomic_output_manifest_sha256"] = payload_sha256(
        manifest, "atomic_output_manifest_sha256"
    )
    validate_content_video_atomic_output_manifest(manifest)
    return manifest


def validate_content_video_atomic_output_manifest(manifest: dict[str, Any]) -> None:
    if manifest.get("schema") != CONTENT_VIDEO_ATOMIC_OUTPUT_MANIFEST_SCHEMA:
        raise ContractError("unsupported content video atomic output manifest schema")
    if manifest.get("atomic_output_manifest_sha256") != payload_sha256(
        manifest, "atomic_output_manifest_sha256"
    ):
        raise ContractError("content video atomic output manifest integrity failed")
    if manifest.get("status") != "completed":
        raise ContractError("content video atomic output manifest is incomplete")
    plan_path = validate_artifact_reference(
        _object(manifest.get("source_project_plan")),
        "content video project plan",
    )
    output_root = plan_path.parent
    staging_root = output_root / "render" / "staging"
    validate_content_video_project_plan(
        read_json_object(plan_path, "content video project plan")
    )
    rows = manifest.get("outputs")
    if not isinstance(rows, list) or not rows:
        raise ContractError("content video atomic output manifest requires outputs")
    actual_keys: set[tuple[Path, int]] = set()
    receipt_outputs: dict[Path, int] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ContractError("content video atomic output row must be an object")
        receipt_path = validate_artifact_reference(
            _object(row.get("ffmpeg_receipt")), "FFmpeg execution receipt"
        )
        receipt = read_json_object(receipt_path, "FFmpeg execution receipt")
        validate_ffmpeg_execution_receipt(receipt)
        receipt_outputs[receipt_path] = len(receipt["outputs"])
        output_index = row.get("receipt_output_index")
        if (
            isinstance(output_index, bool)
            or not isinstance(output_index, int)
            or output_index < 0
            or output_index >= len(receipt["outputs"])
        ):
            raise ContractError("content video atomic output index is invalid")
        key = (receipt_path, output_index)
        if key in actual_keys:
            raise ContractError("content video atomic output row is duplicated")
        actual_keys.add(key)
        receipt_row = receipt["outputs"][output_index]
        ffmpeg_ref = _object(row.get("ffmpeg_output"))
        if row.get("receipt_output_role") != receipt_row.get(
            "role"
        ) or ffmpeg_ref != receipt_row.get("artifact"):
            raise ContractError("content video atomic output drifted from its receipt")
        ffmpeg_path = validate_artifact_reference(ffmpeg_ref, "FFmpeg output")
        committed_ref = _object(row.get("committed_output"))
        committed_path = validate_artifact_reference(
            committed_ref, "atomically committed FFmpeg output"
        )
        if ffmpeg_ref.get("sha256") != committed_ref.get("sha256") or ffmpeg_ref.get(
            "bytes"
        ) != committed_ref.get("bytes"):
            raise ContractError("atomic output content differs from its FFmpeg output")
        mode = row.get("commit_mode")
        if mode == "copy_then_replace":
            if (
                ffmpeg_path == committed_path
                or not ffmpeg_path.is_relative_to(staging_root)
                or not committed_path.is_relative_to(output_root)
                or committed_path.is_relative_to(staging_root)
            ):
                raise ContractError(
                    "media output bypassed the managed staging boundary"
                )
        elif mode == "atomic_text_replace":
            if (
                ffmpeg_path != committed_path
                or receipt.get("operation") != "technical_qa"
                or row.get("receipt_output_role") != "detector_log"
                or committed_path != output_root / "qa" / "ffmpeg-detection.log"
            ):
                raise ContractError("atomic log output boundary is invalid")
        else:
            raise ContractError("content video atomic commit mode is unsupported")
    expected_keys = {
        (receipt_path, index)
        for receipt_path, count in receipt_outputs.items()
        for index in range(count)
    }
    if actual_keys != expected_keys:
        raise ContractError("not every canonical FFmpeg output has commit evidence")
    if manifest.get("ffmpeg_receipt_count") != len(receipt_outputs):
        raise ContractError("atomic output FFmpeg receipt count drifted")
    if manifest.get("ffmpeg_output_count") != len(rows):
        raise ContractError("atomic output count drifted")
    if manifest.get("authority_boundary") != {
        "ffmpeg_outlet": FFMPEG_OUTLET_ID,
        "all_ffmpeg_outputs_accounted_for": True,
        "direct_final_media_write_allowed": False,
        "source_media_modified": False,
        "automatic_publish": False,
    }:
        raise ContractError("content video atomic output authority boundary drifted")


def _scene_command(
    scene_id: str,
    duration_s: float,
    output: Path,
    index: int,
) -> list[str]:
    color = hashlib.sha256(scene_id.encode("utf-8")).hexdigest()[:6]
    frequency = 220 + index * 30
    return [
        "ffmpeg",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"color=c=0x{color}:s=1280x720:r=30:d={duration_s:.3f}",
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency={frequency}:sample_rate=48000:duration={duration_s:.3f}",
        "-map_metadata",
        "-1",
        "-fflags",
        "+bitexact",
        "-flags:v",
        "+bitexact",
        "-flags:a",
        "+bitexact",
        "-threads",
        "1",
        "-shortest",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        str(output),
    ]


def _single_selection(plan: dict[str, Any]) -> dict[str, Any]:
    selections = plan.get("selections")
    if not isinstance(selections, list) or len(selections) != 1:
        raise ContractError("synthetic render requires one continuous music selection")
    selection = selections[0]
    if selection.get("status") != "auto_selected":
        raise ContractError("synthetic music selection did not pass the automatic gate")
    return selection


def _promote_ffmpeg_output(
    staged_path: Path,
    output_path: Path,
    promotions: dict[Path, Path],
    *,
    label: str,
) -> None:
    staged = staged_path.expanduser().resolve()
    output = output_path.expanduser().resolve()
    if staged in promotions or output in promotions.values():
        raise ContractError("canonical FFmpeg output promotion path is duplicated")
    atomic_copy_promote(staged, output, label=label)
    staged_ref = artifact_reference(staged, f"{label} staging")
    output_ref = artifact_reference(output, f"{label} output")
    if staged_ref.get("sha256") != output_ref.get("sha256") or staged_ref.get(
        "bytes"
    ) != output_ref.get("bytes"):
        raise ContractError(f"{label} output differs from its staging artifact")
    promotions[staged] = output


def _ffprobe(path: Path) -> dict[str, Any]:
    executable = shutil.which("ffprobe")
    if not executable:
        raise ContractError("ffprobe executable was not found")
    result = subprocess.run(
        [
            executable,
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ],
        capture_output=True,
        text=True,
        errors="replace",
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        raise ContractError(f"ffprobe failed: {result.stderr.strip()}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ContractError("ffprobe returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise ContractError("ffprobe returned an invalid document")
    return payload


def _validate_plan_bundle(root: Path, plan: dict[str, Any]) -> None:
    owner = read_json_object(
        root / ".content-video-output-owner.json",
        "content video output owner",
    )
    if (
        owner.get("schema") != "video_creation_pipeline.content_video_output_owner.v1"
        or owner.get("source_plan_sha256") != plan.get("plan_sha256")
        or owner.get("user_media_overwrite_allowed") is not False
    ):
        raise ContractError("content video output ownership marker is invalid")
    manifest = read_json_object(root / "plan-manifest.json", "plan manifest")
    if manifest.get("source_plan_sha256") != plan.get("plan_sha256"):
        raise ContractError("plan manifest is stale for the project plan")
    if manifest.get("synthetic_fixture_render_eligible") is not True:
        raise ContractError("plan manifest blocks synthetic fixture rendering")


def _write_owner_marker(root: Path, plan: dict[str, Any]) -> None:
    marker_path = root / ".synthetic-run-owner.json"
    if marker_path.is_file():
        marker = read_json_object(marker_path, "synthetic run owner marker")
        if marker.get("source_plan_sha256") != plan.get("plan_sha256"):
            raise ContractError("output root belongs to a different synthetic run")
    marker = {
        "schema": "video_creation_pipeline.synthetic_output_owner.v1",
        "source_plan_sha256": plan["plan_sha256"],
        "managed_outputs_only": True,
    }
    atomic_write_json(marker, marker_path)


def _descriptors(
    valence: float,
    arousal: float,
    tension: float,
    intimacy_scale: float,
    warmth: float,
    movement: float,
    engagement: float,
) -> dict[str, float]:
    return {
        "valence": valence,
        "arousal": arousal,
        "tension": tension,
        "intimacy_scale": intimacy_scale,
        "warmth": warmth,
        "movement": movement,
        "engagement": engagement,
    }


def _object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}
