from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from .artifact_lifecycle_adapter import build_media_manifest, load_core
from .contract_io import (
    artifact_reference,
    atomic_copy_promote,
    atomic_write_json,
    atomic_write_text,
    file_sha256,
    payload_sha256,
    read_json_object,
)
from .errors import ContractError
from .execution_receipts import execute_ffmpeg_with_receipt, resolve_media_tool
from .generation_contracts import build_shot_generation_unit, write_generation_contract
from .music_pipeline import build_music_mix_command
from .production_strategy import build_visual_production_plan, write_production_strategy_contract
from .semantic_motion import (
    build_semantic_motion_plan,
    run_hyperframes_candidate,
    validate_semantic_motion_run_receipt,
    write_semantic_motion_plan,
)
from .technical_qa import build_technical_qa_report, write_technical_qa_report
from .video_use_review_handoff import (
    build_video_use_review_handoff,
    write_video_use_review_handoff,
)
from .vkp_bridge import build_edit_job, write_edit_job


AUTONOMOUS_CANDIDATE_SCHEMA = "video_creation_pipeline.autonomous_candidate_run.v1"
INPUT_MANIFEST_SCHEMA = "video_creation_pipeline.autonomous_candidate_input_manifest.v1"
DEFAULT_CLOSURE_RUN_ID = "autonomous-video-candidate-20260831-05"
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_FIXTURE_ROOT = REPOSITORY_ROOT / "tests" / "fixtures" / "semantic-motion"
DEFAULT_FIXTURE_ATTESTATION = (
    REPOSITORY_ROOT
    / "tests"
    / "fixtures"
    / "autonomous-video-candidate-v1"
    / "fixture-attestation.json"
)
DEFAULT_VKP_BUNDLE = REPOSITORY_ROOT / "tests" / "fixtures" / "vkp_bundle"
DEFAULT_HYPERFRAMES_ROOT = Path(
    os.environ.get("MVP_HYPERFRAMES_ROOT", "external/hyperframes")
)
DEFAULT_VIDEO_USE_ROOT = Path(
    os.environ.get("MVP_VIDEO_USE_ROOT", "external/video-use")
)
DEFAULT_LIFECYCLE_CORE = Path(
    os.environ.get("MVP_LIFECYCLE_CORE_ROOT", "external/artifact-lifecycle-core")
)


def run_autonomous_candidate(
    output_root: str | Path,
    *,
    fixture_root: str | Path = DEFAULT_FIXTURE_ROOT,
    fixture_attestation: str | Path = DEFAULT_FIXTURE_ATTESTATION,
    vkp_bundle: str | Path = DEFAULT_VKP_BUNDLE,
    hyperframes_root: str | Path = DEFAULT_HYPERFRAMES_ROOT,
    video_use_root: str | Path = DEFAULT_VIDEO_USE_ROOT,
    lifecycle_core_root: str | Path = DEFAULT_LIFECYCLE_CORE,
    closure_run_id: str = DEFAULT_CLOSURE_RUN_ID,
    timeout_s: float = 300.0,
) -> dict[str, Any]:
    """Render the repository-owned motion fixture as a candidate-only MP4.

    This is deliberately a thin integration of existing contracts.  It neither
    treats the fixture as production media nor adds a renderer, speech engine,
    review state machine, or FFmpeg outlet of its own.
    """

    root = Path(output_root).expanduser().resolve()
    fixture = Path(fixture_root).expanduser().resolve()
    attestation = Path(fixture_attestation).expanduser().resolve()
    bundle = Path(vkp_bundle).expanduser().resolve()
    hyperframes = Path(hyperframes_root).expanduser().resolve()
    video_use = Path(video_use_root).expanduser().resolve()
    lifecycle_core = Path(lifecycle_core_root).expanduser().resolve()
    run_id = _required_text(closure_run_id, "closure run id")
    _validate_source_roots(
        fixture, attestation, bundle, hyperframes, video_use, lifecycle_core
    )
    _validate_fixture_attestation(attestation)

    existing = _load_completed_receipt(root, run_id)
    if existing is not None:
        return existing

    _prepare_output_root(root, run_id)
    inputs = _copy_fixture_inputs(root, fixture, attestation, bundle, run_id)
    _write_input_manifest(root, inputs, run_id)

    edit_job_path = root / "authoritative" / "edit-job.json"
    edit_job = build_edit_job(inputs["vkp_bundle"])
    write_edit_job(edit_job, edit_job_path)
    unit = build_shot_generation_unit(
        edit_job_path,
        "storyboard-0001",
        generator="hyperframes",
        media_type="video",
        width=1080,
        height=1920,
        visual_prompt="公开合成票据的聚焦动画；仅用于离线候选验证。",
    )
    unit_path = root / "authoritative" / "shot-unit.json"
    write_generation_contract(unit, unit_path)
    visual_plan = build_visual_production_plan(unit_path, cinema_layer=False)
    visual_plan_path = root / "authoritative" / "visual-production-plan.json"
    write_production_strategy_contract(visual_plan, visual_plan_path)

    motion_plan = build_semantic_motion_plan(
        inputs["packaging_plan"],
        inputs["script_anchor"],
        inputs["source_asset"],
        visual_plan_path,
        asset_id=str(inputs["motion_spec_payload"]["asset_id"]),
        media_type=str(inputs["motion_spec_payload"]["media_type"]),
        cues=list(inputs["motion_spec_payload"]["cues"]),
        width=int(inputs["motion_spec_payload"]["width"]),
        height=int(inputs["motion_spec_payload"]["height"]),
        fps=int(inputs["motion_spec_payload"]["fps"]),
        duration_s=float(inputs["motion_spec_payload"]["duration_s"]),
        evidence_status=str(inputs["motion_spec_payload"]["evidence_status"]),
        authorization_status=str(inputs["motion_spec_payload"]["authorization_status"]),
        deidentified=inputs["motion_spec_payload"]["deidentified"] is True,
    )
    motion_plan_path = root / "authoritative" / "semantic-motion-plan.json"
    write_semantic_motion_plan(motion_plan, motion_plan_path)

    motion_project = root / "render-work" / "hyperframes-project"
    motion_receipt = run_hyperframes_candidate(
        motion_plan_path,
        motion_project,
        hyperframes,
        browser_path=_chrome_path(),
        ffmpeg_path=resolve_media_tool("ffmpeg"),
        ffprobe_path=resolve_media_tool("ffprobe"),
        timeout_s=timeout_s,
    )
    validate_semantic_motion_run_receipt(motion_receipt)

    candidate = _mix_synthetic_music(
        root,
        Path(motion_receipt["candidate_video"]["path"]),
        float(motion_plan["output"]["duration_s"]),
        motion_plan_path,
        timeout_s=timeout_s,
    )
    subtitles = _write_caption_sidecar(root, inputs["script_anchor"])
    qa_report = _run_technical_qa(
        root,
        candidate,
        subtitles,
        inputs["packaging_plan"],
        expected_duration_s=float(motion_plan["output"]["duration_s"]),
        timeout_s=timeout_s,
    )
    if qa_report["readiness"]["technical_ready"] is not True:
        raise ContractError("autonomous candidate failed technical QA")

    review = build_video_use_review_handoff(video_use, candidate, inputs["packaging_plan"])
    review_path = root / "review" / "review-packet.json"
    write_video_use_review_handoff(review, review_path)

    receipt_path = root / "receipts" / "run-receipt.json"
    receipt = _build_receipt(
        root,
        run_id,
        candidate,
        motion_plan_path,
        motion_project / "semantic-motion-run-receipt.json",
        subtitles,
        root / "qa" / "qa-report.json",
        review_path,
    )
    atomic_write_json(receipt, receipt_path)
    lifecycle_manifest = _scan_lifecycle(root, lifecycle_core, run_id)

    final = dict(receipt)
    final["lifecycle_manifest"] = artifact_reference(
        lifecycle_manifest, "media lifecycle manifest"
    )
    final["run_receipt_sha256"] = payload_sha256(final, "run_receipt_sha256")
    atomic_write_json(final, receipt_path)
    return final


def validate_autonomous_candidate_run(path: str | Path) -> dict[str, Any]:
    receipt = read_json_object(path, "autonomous candidate run receipt")
    if receipt.get("schema") != AUTONOMOUS_CANDIDATE_SCHEMA:
        raise ContractError("unsupported autonomous candidate receipt schema")
    if receipt.get("run_receipt_sha256") != payload_sha256(
        receipt, "run_receipt_sha256"
    ):
        raise ContractError("autonomous candidate receipt integrity failed")
    if receipt.get("status") != "completed_candidate_only":
        raise ContractError("autonomous candidate run is not complete")
    for field, label in (
        ("input_manifest", "autonomous input manifest"),
        ("candidate_video", "candidate video"),
        ("motion_plan", "semantic motion plan"),
        ("motion_receipt", "semantic motion receipt"),
        ("captions", "captions"),
        ("technical_qa", "technical QA report"),
        ("review_packet", "review packet"),
        ("lifecycle_manifest", "media lifecycle manifest"),
    ):
        reference = receipt.get(field)
        if not isinstance(reference, dict):
            raise ContractError(f"autonomous candidate receipt lacks {field}")
        from .contract_io import validate_artifact_reference

        validate_artifact_reference(reference, label)
    boundary = receipt.get("authority_boundary")
    if boundary != {
        "candidate_only": True,
        "production_success_claimed": False,
        "external_provider_called": False,
        "upload_performed": False,
        "publish_performed": False,
        "source_media_modified": False,
        "second_ffmpeg_orchestrator_created": False,
    }:
        raise ContractError("autonomous candidate authority boundary is invalid")
    return receipt


def _validate_source_roots(
    fixture: Path,
    attestation: Path,
    bundle: Path,
    hyperframes: Path,
    video_use: Path,
    lifecycle_core: Path,
) -> None:
    required = {
        fixture / "packaging-plan.json": "semantic motion packaging fixture",
        fixture / "script-anchor.json": "semantic motion script fixture",
        fixture / "source-evidence.svg": "semantic motion source asset",
        fixture / "build-spec.json": "semantic motion build fixture",
        attestation: "synthetic fixture attestation",
        bundle / "manifest.json": "VKP bridge fixture",
        hyperframes / "packages" / "cli" / "bin" / "hyperframes.mjs": "HyperFrames CLI",
        video_use / "SKILL.md": "video-use skill",
        video_use / "helpers" / "timeline_view.py": "video-use helper",
        lifecycle_core / "artifact_lifecycle_core.py": "artifact lifecycle core",
    }
    for path, label in required.items():
        if not path.is_file():
            raise ContractError(f"{label} not found: {path}")


def _prepare_output_root(root: Path, run_id: str) -> None:
    marker_path = root / ".autonomous-candidate-owner.json"
    if root.exists() and not root.is_dir():
        raise ContractError("autonomous candidate output root is not a directory")
    if root.is_dir() and any(root.iterdir()):
        if not marker_path.is_file():
            raise ContractError("autonomous candidate output root is non-empty and unmanaged")
        marker = read_json_object(marker_path, "autonomous candidate output owner")
        if marker.get("closure_run_id") != run_id:
            raise ContractError("autonomous candidate output root belongs to another run")
    root.mkdir(parents=True, exist_ok=True)
    (root / "cache").mkdir(exist_ok=True)
    (root / "quarantine").mkdir(exist_ok=True)
    marker = {
        "schema": "video_creation_pipeline.autonomous_candidate_owner.v1",
        "closure_run_id": run_id,
        "managed_outputs_only": True,
        "user_media_overwrite_allowed": False,
    }
    marker["owner_sha256"] = payload_sha256(marker, "owner_sha256")
    atomic_write_json(marker, marker_path)


def _validate_fixture_attestation(path: Path) -> None:
    payload = read_json_object(path, "synthetic fixture attestation")
    expected = {
        "schema": "video_creation_pipeline.autonomous_candidate_fixture_attestation.v1",
        "classification": "project_owned_public_synthetic_fixture",
        "contains_customer_media": False,
        "contains_personal_media": False,
        "contains_credentials": False,
        "authorized_for_offline_candidate": True,
        "production_media_claimed": False,
    }
    for key, value in expected.items():
        if payload.get(key) != value:
            raise ContractError(f"synthetic fixture attestation drifted: {key}")


def _copy_fixture_inputs(
    root: Path, fixture: Path, attestation: Path, bundle: Path, run_id: str
) -> dict[str, Any]:
    destination = root / "inputs"
    destination.mkdir(parents=True, exist_ok=True)
    copied = {}
    for name in (
        "packaging-plan.json",
        "script-anchor.json",
        "source-evidence.svg",
        "build-spec.json",
    ):
        source = fixture / name
        target = destination / name
        if not target.is_file() or file_sha256(target) != file_sha256(source):
            shutil.copy2(source, target)
        copied[name] = target
    copied_attestation = destination / "fixture-attestation.json"
    if (
        not copied_attestation.is_file()
        or file_sha256(copied_attestation) != file_sha256(attestation)
    ):
        shutil.copy2(attestation, copied_attestation)
    copied_bundle = destination / "vkp-bundle"
    if not copied_bundle.exists():
        shutil.copytree(bundle, copied_bundle)
    elif not (copied_bundle / "manifest.json").is_file():
        raise ContractError("managed VKP fixture bundle is incomplete")
    spec = read_json_object(copied["build-spec.json"], "semantic motion build spec")
    return {
        "fixture_root": fixture,
        "packaging_plan": copied["packaging-plan.json"],
        "script_anchor": copied["script-anchor.json"],
        "source_asset": copied["source-evidence.svg"],
        "motion_spec": copied["build-spec.json"],
        "motion_spec_payload": spec,
        "fixture_attestation": copied_attestation,
        "vkp_bundle": copied_bundle,
        "closure_run_id": run_id,
    }


def _write_input_manifest(root: Path, inputs: dict[str, Any], run_id: str) -> Path:
    payload = {
        "schema": INPUT_MANIFEST_SCHEMA,
        "closure_run_id": run_id,
        "fixture_attestation": {
            "classification": "project_owned_public_synthetic_fixture",
            "contains_customer_media": False,
            "contains_personal_media": False,
            "contains_credentials": False,
            "authorized_for_offline_candidate": True,
            "production_media_claimed": False,
        },
        "inputs": {
            "fixture_attestation": artifact_reference(
                inputs["fixture_attestation"], "synthetic fixture attestation"
            ),
            "script": artifact_reference(inputs["script_anchor"], "script anchor"),
            "timeline": artifact_reference(inputs["packaging_plan"], "packaging plan"),
            "visual_asset": artifact_reference(inputs["source_asset"], "source asset"),
            "motion_spec": artifact_reference(inputs["motion_spec"], "motion spec"),
            "vkp_bundle_manifest": artifact_reference(
                inputs["vkp_bundle"] / "manifest.json", "VKP bundle manifest"
            ),
        },
        "output_contract": {
            "aspect_ratio": "9:16",
            "width": 1080,
            "height": 1920,
            "fps": 30,
            "candidate_only": True,
            "upload_performed": False,
            "publish_performed": False,
        },
    }
    payload["input_manifest_sha256"] = payload_sha256(payload, "input_manifest_sha256")
    return atomic_write_json(payload, root / "authoritative" / "input-manifest.json")


def _mix_synthetic_music(
    root: Path,
    motion_video: Path,
    duration_s: float,
    motion_plan: Path,
    *,
    timeout_s: float,
) -> Path:
    staging = root / "render-work" / "staging"
    receipts = root / "receipts"
    staging.mkdir(parents=True, exist_ok=True)
    receipts.mkdir(parents=True, exist_ok=True)
    program_staging = staging / "motion-with-silence.mp4"
    program = root / "render-work" / "motion-with-silence.mp4"
    execute_ffmpeg_with_receipt(
        "transcode",
        [
            "ffmpeg", "-y", "-i", str(motion_video), "-f", "lavfi", "-i",
            "anullsrc=r=48000:cl=stereo", "-t", f"{duration_s:.3f}", "-map", "0:v:0",
            "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", str(program_staging),
        ],
        [
            {"role": "semantic_motion_candidate", "path": motion_video},
            {"role": "semantic_motion_plan", "path": motion_plan},
        ],
        [{"role": "silent_program_staging", "path": program_staging}],
        receipts / "motion-silence-mux.json",
        timeout_s=timeout_s,
    )
    atomic_copy_promote(program_staging, program, label="motion silent program")
    bed_staging = staging / "synthetic-bed.wav"
    bed = root / "render-work" / "synthetic-bed.wav"
    execute_ffmpeg_with_receipt(
        "render",
        [
            "ffmpeg", "-y", "-f", "lavfi", "-i",
            f"sine=frequency=196:sample_rate=48000:duration={duration_s:.3f}",
            # This is intentionally audible enough for the existing silence QA.
            # The common music policy applies the final -3 dB presentation gain.
            "-af", "volume=0.160", "-c:a", "pcm_s16le", str(bed_staging),
        ],
        [{"role": "semantic_motion_plan", "path": motion_plan}],
        [{"role": "synthetic_music_staging", "path": bed_staging}],
        receipts / "synthetic-music-bed.json",
        timeout_s=timeout_s,
    )
    atomic_copy_promote(bed_staging, bed, label="synthetic music bed")
    candidate_staging = staging / "candidate-with-music.mp4"
    candidate = root / "candidate" / "candidate.mp4"
    selection = {
        "timeline_interval": {"start_s": 0.0, "end_s": duration_s},
        "source_interval": {"start_s": 0.0, "end_s": duration_s},
        "loop": False,
        "gain_db": -3.0,
        "gain_envelope": {
            "base_music_gain_db": -3.0,
            "speech_priority_music_gain_db": -6.0,
            "ramp_s": 0.15,
            "source_mode": "synthetic_fixture_no_asr",
            "application_mode": "no_speech_fixture",
            "coverage_ratio": 0.0,
            "windows": [],
            "semantic_timeline_moved": False,
        },
        "fade_in_s": 0.25,
        "fade_out_s": 0.35,
        "ducking": {
            "enabled": False,
            "threshold": 0.08,
            "ratio": 3.0,
            "attack_ms": 30,
            "release_ms": 400,
        },
    }
    execute_ffmpeg_with_receipt(
        "music_mix",
        build_music_mix_command(
            program,
            bed,
            candidate_staging,
            selection,
            deterministic=True,
        ),
        [
            {"role": "silent_program", "path": program},
            {"role": "synthetic_music_bed", "path": bed},
        ],
        [{"role": "candidate_video_staging", "path": candidate_staging}],
        receipts / "candidate-music-mix.json",
        timeout_s=timeout_s,
    )
    atomic_copy_promote(candidate_staging, candidate, label="autonomous candidate")
    return candidate


def _write_caption_sidecar(root: Path, script_path: Path) -> Path:
    script = read_json_object(script_path, "script anchor")
    anchors = script.get("anchors")
    if not isinstance(anchors, list) or len(anchors) != 1:
        raise ContractError("autonomous fixture requires exactly one script anchor")
    anchor = anchors[0]
    if not isinstance(anchor, dict):
        raise ContractError("script anchor is invalid")
    start = _srt_time(float(anchor["timeline_start_s"]))
    end = _srt_time(float(anchor["timeline_end_s"]))
    caption = str(anchor["quote"]).strip()
    if not caption:
        raise ContractError("script anchor quote is empty")
    return atomic_write_text(f"1\n{start} --> {end}\n{caption}\n", root / "candidate" / "captions.srt")


def _run_technical_qa(
    root: Path,
    candidate: Path,
    subtitles: Path,
    packaging: Path,
    *,
    expected_duration_s: float,
    timeout_s: float,
) -> dict[str, Any]:
    qa_dir = root / "qa"
    qa_dir.mkdir(parents=True, exist_ok=True)
    from .execution_receipts import probe_media_metadata

    probe_path = qa_dir / "ffprobe.json"
    atomic_write_json(probe_media_metadata(candidate), probe_path)
    detector_log = qa_dir / "detector.log"
    receipt_path = root / "receipts" / "technical-qa.json"
    execute_ffmpeg_with_receipt(
        "technical_qa",
        [
            "ffmpeg", "-hide_banner", "-nostats", "-v", "info", "-i", str(candidate),
            "-vf", "blackdetect=d=0.5:pix_th=0.10,freezedetect=n=-50dB:d=1.5",
            "-af", "silencedetect=n=-50dB:d=1.0", "-f", "null", "-",
        ],
        [{"role": "media", "path": candidate}],
        [{"role": "detector_log", "path": detector_log}],
        receipt_path,
        capture_log_path=detector_log,
        timeout_s=timeout_s,
    )
    report = build_technical_qa_report(
        candidate,
        probe_path,
        receipt_path,
        subtitles_path=subtitles,
        packaging_plan_path=packaging,
        expected_duration_s=expected_duration_s,
        policy={
            "target_width": 1080,
            "target_height": 1920,
            "target_fps": 30.0,
            "fps_tolerance": 0.1,
            "duration_tolerance_s": 0.35,
            "max_black_ratio": 0.05,
            "max_silence_ratio": 0.25,
            "freeze_warning_ratio": 0.5,
            "max_subtitle_line_chars": 80,
            "require_audio": True,
            "require_subtitles": True,
            "require_packaging_plan": True,
        },
    )
    write_technical_qa_report(report, qa_dir / "qa-report.json")
    return report


def _build_receipt(
    root: Path,
    run_id: str,
    candidate: Path,
    motion_plan: Path,
    motion_receipt: Path,
    captions: Path,
    technical_qa: Path,
    review_packet: Path,
) -> dict[str, Any]:
    return {
        "schema": AUTONOMOUS_CANDIDATE_SCHEMA,
        "status": "completed_candidate_only",
        "closure_run_id": run_id,
        "input_manifest": artifact_reference(
            root / "authoritative" / "input-manifest.json", "input manifest"
        ),
        "candidate_video": artifact_reference(candidate, "candidate video"),
        "motion_plan": artifact_reference(motion_plan, "semantic motion plan"),
        "motion_receipt": artifact_reference(motion_receipt, "semantic motion receipt"),
        "captions": artifact_reference(captions, "captions"),
        "technical_qa": artifact_reference(technical_qa, "technical QA report"),
        "review_packet": artifact_reference(review_packet, "review packet"),
        "stage_order": ["script", "timeline", "captions", "motion", "music", "candidate_mp4", "technical_qa", "review_packet"],
        "authority_boundary": {
            "candidate_only": True,
            "production_success_claimed": False,
            "external_provider_called": False,
            "upload_performed": False,
            "publish_performed": False,
            "source_media_modified": False,
            "second_ffmpeg_orchestrator_created": False,
        },
    }


def _scan_lifecycle(root: Path, core_root: Path, run_id: str) -> Path:
    config = _lifecycle_config(run_id)
    config_path = root / "lifecycle" / "adapter-config.json"
    atomic_write_json(config, config_path)
    core = load_core(core_root)
    manifest = build_media_manifest(
        core, root, config, checkpoint_path=root / "lifecycle" / "scan-checkpoint.json"
    )
    manifest_path = root / "lifecycle" / "media-manifest.json"
    atomic_write_json(manifest, manifest_path)
    return manifest_path


def _lifecycle_config(run_id: str) -> dict[str, Any]:
    protected = [
        "inputs/packaging-plan.json", "inputs/script-anchor.json", "inputs/source-evidence.svg",
        "authoritative/input-manifest.json", "authoritative/edit-job.json", "authoritative/shot-unit.json",
        "authoritative/visual-production-plan.json", "authoritative/semantic-motion-plan.json",
        "candidate/candidate.mp4", "candidate/captions.srt", "qa/qa-report.json",
        "review/review-packet.json", "receipts/technical-qa.json",
    ]
    return {
        "schema_version": "video_creation_pipeline.media_artifact_adapter_config.v1",
        "lifecycle_profile": "media",
        "cleanup_mode": "plan_only",
        "automatic_purge": False,
        "closure_run_id": run_id,
        "producer_tool_id": "mvp-video-media-artifact-lifecycle-adapter",
        "owner_lane": "video-creation-pipeline",
        "artifact_roots": ["render-work", "cache", "quarantine"],
        "authoritative_paths": protected,
        "active_run_ids": [],
        "producer_process_names": ["hyperframes-render.exe"],
        "producer_command_markers": ["autonomous-video-candidate"],
        "max_depth": 8,
        "max_items": 400,
        "rules": [
            {
                "path_prefix": "render-work", "artifact_role": "reproducible_derived",
                "candidate_kind": "rebuildable_render_workspace", "lifecycle_state": "superseded",
                "sensitivity": "synthetic", "pin": False,
                "rebuild": {"command": "autonomous-video-candidate run", "input_paths": ["authoritative/semantic-motion-plan.json"], "dependency_lock": "fixed-hyperframes", "verification_path": "qa/qa-report.json"},
            },
            {
                "path_prefix": "cache", "artifact_role": "cache_build_test_temp", "candidate_kind": "preview",
                "lifecycle_state": "expired", "sensitivity": "synthetic", "pin": False,
                "rebuild": {"command": "autonomous-video-candidate run", "input_paths": ["authoritative/semantic-motion-plan.json"], "dependency_lock": "fixed-hyperframes", "verification_path": "qa/qa-report.json"},
            },
            {
                "path_prefix": "quarantine", "artifact_role": "reproducible_derived", "candidate_kind": "failed_encode",
                "lifecycle_state": "failed", "sensitivity": "synthetic", "pin": False,
                "rebuild": {"command": "autonomous-video-candidate run", "input_paths": ["authoritative/semantic-motion-plan.json"], "dependency_lock": "fixed-hyperframes", "verification_path": "qa/qa-report.json"},
            },
            {"path_prefix": "inputs", "artifact_role": "authoritative_input", "lifecycle_state": "locked", "sensitivity": "synthetic", "pin": True},
            {"path_prefix": "authoritative", "artifact_role": "evidence_receipt_manifest", "lifecycle_state": "locked", "sensitivity": "synthetic", "pin": True},
            {"path_prefix": "candidate", "artifact_role": "final_deliverable", "lifecycle_state": "locked", "sensitivity": "synthetic", "pin": True},
            {"path_prefix": "qa", "artifact_role": "evidence_receipt_manifest", "lifecycle_state": "locked", "sensitivity": "synthetic", "pin": True},
            {"path_prefix": "review", "artifact_role": "evidence_receipt_manifest", "lifecycle_state": "locked", "sensitivity": "synthetic", "pin": True},
            {"path_prefix": "receipts", "artifact_role": "evidence_receipt_manifest", "lifecycle_state": "locked", "sensitivity": "synthetic", "pin": True},
            {"path_prefix": "lifecycle", "artifact_role": "evidence_receipt_manifest", "lifecycle_state": "locked", "sensitivity": "synthetic", "pin": True},
        ],
    }


def _load_completed_receipt(root: Path, run_id: str) -> dict[str, Any] | None:
    path = root / "receipts" / "run-receipt.json"
    if not path.is_file():
        return None
    receipt = validate_autonomous_candidate_run(path)
    if receipt.get("closure_run_id") != run_id:
        raise ContractError("completed autonomous candidate belongs to another run")
    receipt = dict(receipt)
    receipt["disposition"] = "resumed_verified"
    return receipt


def _chrome_path() -> Path:
    env_chrome = os.environ.get("MVP_CHROME_EXECUTABLE")
    if env_chrome and Path(env_chrome).is_file():
        return Path(env_chrome)
    for binary in ("chrome", "google-chrome", "chromium", "msedge"):
        found = shutil.which(binary)
        if found:
            return Path(found)
    program_files = os.environ.get("PROGRAMFILES")
    program_files_x86 = os.environ.get("PROGRAMFILES(X86)")
    candidates = []
    if program_files:
        candidates.append(Path(program_files) / "Google/Chrome/Application/chrome.exe")
    if program_files_x86:
        candidates.append(Path(program_files_x86) / "Microsoft/Edge/Application/msedge.exe")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise ContractError("Chrome or Edge executable was not found for HyperFrames")


def _srt_time(seconds: float) -> str:
    milliseconds = max(0, int(round(seconds * 1000)))
    hours, remaining = divmod(milliseconds, 3_600_000)
    minutes, remaining = divmod(remaining, 60_000)
    seconds_value, milliseconds = divmod(remaining, 1_000)
    return f"{hours:02}:{minutes:02}:{seconds_value:02},{milliseconds:03}"


def _required_text(value: str, label: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ContractError(f"{label} is required")
    return text
