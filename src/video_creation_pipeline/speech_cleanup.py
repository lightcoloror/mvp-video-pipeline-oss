from __future__ import annotations

import math
import shutil
import subprocess
from pathlib import Path
from typing import Any, Iterable
from uuid import uuid4

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

from .contract_io import (
    artifact_reference,
    atomic_write_json,
    payload_sha256,
    read_json_object,
    validate_artifact_reference,
)
from .errors import ContractError
from .audio_edit_quality import cleanup_fallback
from .editing_presets import reviewed
from .execution_receipts import (
    FFMPEG_OUTLET_ID,
    execute_ffmpeg_with_receipt,
    probe_media_metadata,
    resolve_media_tool,
    validate_ffmpeg_execution_receipt,
)

SPEECH_CLEANUP_PLAN_SCHEMA = "video_creation_pipeline.speech_cleanup_plan.v1"
SPEECH_CLEANUP_RECEIPT_SCHEMA = "video_creation_pipeline.speech_cleanup_receipt.v1"
FFMPEG_SOURCE_COMMIT = "44d082edc87381d978e8588b148116b99fefdb43"

NOISE_KINDS = {"broadband_hiss", "mains_hum"}
PROFILE_SETTINGS: dict[str, dict[str, float | int]] = {
    "original": {},
    "gentle": {
        "highpass_hz": 70.0,
        "hum_notch_width_hz": 2.5,
        "hum_harmonics": 3,
        "noise_reduction_db": 10.0,
        "noise_floor_db": -50.0,
        "floor_offset": 1.0,
        "gain_smooth": 4,
    },
    "strong": {
        "highpass_hz": 80.0,
        "hum_notch_width_hz": 4.0,
        "hum_harmonics": 4,
        "noise_reduction_db": 18.0,
        "noise_floor_db": -35.0,
        "floor_offset": 1.5,
        "gain_smooth": 8,
    },
}


def speech_cleanup_doctor() -> dict[str, Any]:
    ffmpeg = resolve_media_tool("ffmpeg")
    ffprobe = resolve_media_tool("ffprobe")
    version = _run_tool([ffmpeg, "-hide_banner", "-version"], "FFmpeg version")
    filters = _run_tool([ffmpeg, "-hide_banner", "-filters"], "FFmpeg filters")
    required = {name: name in filters for name in ("afftdn", "bandreject", "highpass")}
    status = "ready" if all(required.values()) else "blocked_missing_filter"
    return {
        "schema": "video_creation_pipeline.speech_cleanup_doctor.v1",
        "status": status,
        "ffmpeg_version": version.splitlines()[0].strip(),
        "ffprobe_available": Path(ffprobe).is_file(),
        "filters": required,
        "profiles": sorted(PROFILE_SETTINGS),
        "noise_kinds": sorted(NOISE_KINDS),
        "source_commit": FFMPEG_SOURCE_COMMIT,
        "authority_boundary": {
            "local_only": True,
            "model_downloaded": False,
            "automatic_profile_selection": False,
            "voice_quality_claimed": False,
            "automatic_publish": False,
        },
    }


def build_speech_cleanup_plan(
    source_audio_path: str | Path,
    output_audio_path: str | Path,
    *,
    profile: str = "gentle",
    noise_kinds: Iterable[str],
    mains_hz: int = 50,
    sample_rate: int = 48000,
    processing_review: dict | None = None,
    audition_feedback: dict | None = None,
) -> dict[str, Any]:
    source = Path(source_audio_path).expanduser().resolve()
    output = Path(output_audio_path).expanduser().resolve()
    normalized_kinds = sorted({str(value).strip() for value in noise_kinds})
    _validate_settings(profile, normalized_kinds, mains_hz, sample_rate)
    if source == output:
        raise ContractError("speech cleanup cannot overwrite its source")
    if output.suffix.casefold() != ".wav":
        raise ContractError("speech cleanup output must be a WAV file")
    filter_spec = build_speech_cleanup_filter_spec(
        profile, normalized_kinds, mains_hz=mains_hz
    )
    plan: dict[str, Any] = {
        "schema": SPEECH_CLEANUP_PLAN_SCHEMA,
        "source_audio": artifact_reference(source, "speech cleanup source audio"),
        "output": {
            "path": str(output),
            "codec": "pcm_s24le",
            "sample_rate": sample_rate,
        },
        "profile": profile,
        "processing_review": processing_review,
        "audition_feedback": audition_feedback,
        "noise_kinds": normalized_kinds,
        "mains_hz": mains_hz,
        "filter_spec": filter_spec,
        "filter_chain": build_speech_cleanup_filter_chain(filter_spec),
        "source_provenance": _source_provenance(),
        "execution_handoff": {
            "outlet_id": FFMPEG_OUTLET_ID,
            "operation": "speech_cleanup",
            "followed_by": "audio_mastering_plan.v1",
        },
        "authority_boundary": _plan_boundary(),
    }
    plan["plan_sha256"] = payload_sha256(plan, "plan_sha256")
    validate_speech_cleanup_plan(plan)
    return plan


def build_speech_cleanup_filter_spec(
    profile: str,
    noise_kinds: Iterable[str],
    *,
    mains_hz: int,
) -> list[dict[str, Any]]:
    kinds = sorted({str(value).strip() for value in noise_kinds})
    _validate_settings(profile, kinds, mains_hz, 48000)
    if profile == "original":
        return [{"name": "anull", "options": {}}]
    settings = PROFILE_SETTINGS[profile]
    result: list[dict[str, Any]] = [
        {"name": "highpass", "options": {"f": settings["highpass_hz"]}}
    ]
    if "mains_hum" in kinds:
        for harmonic in range(1, int(settings["hum_harmonics"]) + 1):
            result.append(
                {
                    "name": "bandreject",
                    "options": {
                        "f": float(mains_hz * harmonic),
                        "t": "h",
                        "w": settings["hum_notch_width_hz"],
                    },
                }
            )
    if "broadband_hiss" in kinds:
        result.append(
            {
                "name": "afftdn",
                "options": {
                    "nr": settings["noise_reduction_db"],
                    "nf": settings["noise_floor_db"],
                    "tn": 1,
                    "fo": settings["floor_offset"],
                    "gs": settings["gain_smooth"],
                },
            }
        )
    return result


def build_speech_cleanup_filter_chain(spec: Iterable[dict[str, Any]]) -> str:
    filters = []
    for row in spec:
        name = str(row.get("name") or "")
        options = row.get("options") if isinstance(row.get("options"), dict) else {}
        if name == "anull":
            filters.append("anull")
        elif name == "highpass":
            filters.append(f"highpass=f={_format_number(options.get('f'))}")
        elif name == "bandreject":
            filters.append(
                "bandreject="
                f"f={_format_number(options.get('f'))}:"
                f"t={options.get('t')}:"
                f"w={_format_number(options.get('w'))}"
            )
        elif name == "afftdn":
            filters.append(
                "afftdn="
                f"nr={_format_number(options.get('nr'))}:"
                f"nf={_format_number(options.get('nf'))}:"
                f"tn={int(options.get('tn', 0))}:"
                f"fo={_format_number(options.get('fo'))}:"
                f"gs={int(options.get('gs', 0))}"
            )
        else:
            raise ContractError(f"unsupported speech cleanup filter: {name}")
    if not filters:
        raise ContractError("speech cleanup filter chain cannot be empty")
    return ",".join(filters)


def execute_speech_cleanup(
    plan_path: str | Path,
    work_dir: str | Path,
    receipt_path: str | Path,
    *,
    timeout_s: float = 180.0,
) -> dict[str, Any]:
    plan_file = Path(plan_path).expanduser().resolve()
    plan = read_json_object(plan_file, "speech cleanup plan")
    validate_speech_cleanup_plan(plan)
    source = validate_artifact_reference(
        _object(plan.get("source_audio")), "speech cleanup source audio"
    )
    output = Path(str(_object(plan.get("output")).get("path"))).resolve()
    root = Path(work_dir).expanduser().resolve()
    final_receipt = Path(receipt_path).expanduser().resolve()
    staged = root / "speech-cleanup-staging.wav"
    ffmpeg_receipt_path = root / "speech-cleanup-ffmpeg-receipt.json"
    _validate_output_paths(
        source,
        plan_file,
        output,
        final_receipt,
        staged,
        ffmpeg_receipt_path,
    )
    root.mkdir(parents=True, exist_ok=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    command = build_speech_cleanup_command(plan, source, staged)
    execute_ffmpeg_with_receipt(
        "speech_cleanup",
        command,
        [
            {"role": "source_audio", "path": source},
            {"role": "speech_cleanup_plan", "path": plan_file},
        ],
        [{"role": "cleaned_audio_staging", "path": staged}],
        ffmpeg_receipt_path,
        timeout_s=timeout_s,
    )
    _technical_qa(source, staged, plan)
    _atomic_promote(staged, output)
    receipt = build_speech_cleanup_receipt(
        plan_file, ffmpeg_receipt_path, staged, output
    )
    atomic_write_json(receipt, final_receipt)
    return receipt


def build_speech_cleanup_command(
    plan: dict[str, Any], source_path: str | Path, output_path: str | Path
) -> list[str]:
    validate_speech_cleanup_plan(plan)
    source = Path(source_path).expanduser().resolve()
    output = Path(output_path).expanduser().resolve()
    return [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-nostats",
        "-i",
        str(source),
        "-map",
        "0:a:0",
        "-vn",
        "-af",
        str(plan["filter_chain"]),
        "-ar",
        str(int(plan["output"]["sample_rate"])),
        "-c:a",
        str(plan["output"]["codec"]),
        "-map_metadata",
        "-1",
        "-fflags",
        "+bitexact",
        "-flags:a",
        "+bitexact",
        "-threads",
        "1",
        str(output),
    ]


def build_speech_cleanup_receipt(
    plan_path: str | Path,
    ffmpeg_receipt_path: str | Path,
    staged_audio_path: str | Path,
    output_audio_path: str | Path,
) -> dict[str, Any]:
    plan_file = Path(plan_path).expanduser().resolve()
    ffmpeg_receipt_file = Path(ffmpeg_receipt_path).expanduser().resolve()
    staged = Path(staged_audio_path).expanduser().resolve()
    output = Path(output_audio_path).expanduser().resolve()
    plan = read_json_object(plan_file, "speech cleanup plan")
    validate_speech_cleanup_plan(plan)
    source = validate_artifact_reference(
        _object(plan.get("source_audio")), "speech cleanup source audio"
    )
    receipt: dict[str, Any] = {
        "schema": SPEECH_CLEANUP_RECEIPT_SCHEMA,
        "status": "completed_technical_candidate_needs_audition",
        "source_plan": artifact_reference(plan_file, "speech cleanup plan"),
        "source_plan_sha256": plan["plan_sha256"],
        "ffmpeg_receipt": artifact_reference(
            ffmpeg_receipt_file, "speech cleanup FFmpeg receipt"
        ),
        "staged_audio": artifact_reference(staged, "speech cleanup staged audio"),
        "cleaned_audio": artifact_reference(output, "speech cleanup output audio"),
        "filter_chain": plan["filter_chain"],
        "technical_qa": _technical_qa(source, output, plan),
        "audition": {
            "status": "needs_audition",
            "required_checks": [
                "speech_naturalness",
                "residual_noise",
                "pumping_or_musical_noise",
            ],
            "automatic_release": False,
        },
        "authority_boundary": _receipt_boundary(),
    }
    receipt["receipt_sha256"] = payload_sha256(receipt, "receipt_sha256")
    validate_speech_cleanup_receipt(receipt)
    return receipt


def validate_speech_cleanup_plan(plan: dict[str, Any]) -> None:
    _validate_schema(plan, "speech_cleanup_plan.v1.schema.json", "speech cleanup plan")
    if plan.get("plan_sha256") != payload_sha256(plan, "plan_sha256"):
        raise ContractError("speech cleanup plan integrity check failed")
    source = validate_artifact_reference(
        _object(plan.get("source_audio")), "speech cleanup source audio"
    )
    output = Path(str(_object(plan.get("output")).get("path"))).resolve()
    if output == source or output.suffix.casefold() != ".wav":
        raise ContractError("speech cleanup output path is invalid")
    profile = str(plan.get("profile") or "")
    if profile == "strong":
        reviewed(plan.get("processing_review"),["audio_checked","gentle_insufficient"],"strong speech cleanup")
        if plan["processing_review"].get("source_sha256") != plan["source_audio"]["sha256"]:
            raise ContractError("strong cleanup review belongs to another source")
    feedback = plan.get("audition_feedback")
    if feedback is not None:
        reviewed(feedback,["audio_checked"],"cleanup feedback")
        if feedback.get("source_sha256") != plan["source_audio"]["sha256"]:
            raise ContractError("cleanup feedback belongs to another source")
        previous = feedback.get("previous_profile")
        if previous not in PROFILE_SETTINGS or profile != cleanup_fallback(previous,feedback.get("degradation_flags")):
            raise ContractError("degraded cleanup must be reduced or returned to original")
    kinds = list(plan.get("noise_kinds") or [])
    mains_hz = int(plan.get("mains_hz") or 0)
    sample_rate = int(_object(plan.get("output")).get("sample_rate") or 0)
    _validate_settings(profile, kinds, mains_hz, sample_rate)
    expected_spec = build_speech_cleanup_filter_spec(profile, kinds, mains_hz=mains_hz)
    if plan.get("filter_spec") != expected_spec:
        raise ContractError("speech cleanup filter specification drifted")
    if plan.get("filter_chain") != build_speech_cleanup_filter_chain(expected_spec):
        raise ContractError("speech cleanup filter chain drifted")
    if plan.get("source_provenance") != _source_provenance():
        raise ContractError("speech cleanup source provenance drifted")
    if plan.get("execution_handoff") != {
        "outlet_id": FFMPEG_OUTLET_ID,
        "operation": "speech_cleanup",
        "followed_by": "audio_mastering_plan.v1",
    }:
        raise ContractError("speech cleanup FFmpeg handoff is invalid")
    if plan.get("authority_boundary") != _plan_boundary():
        raise ContractError("speech cleanup plan boundary is invalid")


def validate_speech_cleanup_receipt(receipt: dict[str, Any]) -> None:
    _validate_schema(
        receipt, "speech_cleanup_receipt.v1.schema.json", "speech cleanup receipt"
    )
    if receipt.get("receipt_sha256") != payload_sha256(receipt, "receipt_sha256"):
        raise ContractError("speech cleanup receipt integrity check failed")
    plan_path = validate_artifact_reference(
        _object(receipt.get("source_plan")), "speech cleanup plan"
    )
    plan = read_json_object(plan_path, "speech cleanup plan")
    validate_speech_cleanup_plan(plan)
    if receipt.get("source_plan_sha256") != plan.get("plan_sha256"):
        raise ContractError("speech cleanup receipt binds a stale plan")
    ffmpeg_path = validate_artifact_reference(
        _object(receipt.get("ffmpeg_receipt")), "speech cleanup FFmpeg receipt"
    )
    ffmpeg_receipt = read_json_object(ffmpeg_path, "speech cleanup FFmpeg receipt")
    validate_ffmpeg_execution_receipt(ffmpeg_receipt)
    if ffmpeg_receipt.get("operation") != "speech_cleanup":
        raise ContractError("speech cleanup used the wrong FFmpeg operation")
    source = validate_artifact_reference(
        _object(plan.get("source_audio")), "speech cleanup source audio"
    )
    staged = validate_artifact_reference(
        _object(receipt.get("staged_audio")), "speech cleanup staged audio"
    )
    output = validate_artifact_reference(
        _object(receipt.get("cleaned_audio")), "speech cleanup output audio"
    )
    if output != Path(str(plan["output"]["path"])).resolve():
        raise ContractError("speech cleanup output differs from its plan")
    if _object(receipt.get("staged_audio")).get("sha256") != _object(
        receipt.get("cleaned_audio")
    ).get("sha256"):
        raise ContractError("speech cleanup output differs from the staging artifact")
    if ffmpeg_receipt.get("command") != build_speech_cleanup_command(
        plan, source, staged
    ):
        raise ContractError("speech cleanup FFmpeg command drifted")
    _require_ffmpeg_role(ffmpeg_receipt, "inputs", "source_audio", source)
    _require_ffmpeg_role(ffmpeg_receipt, "inputs", "speech_cleanup_plan", plan_path)
    _require_ffmpeg_role(ffmpeg_receipt, "outputs", "cleaned_audio_staging", staged)
    if receipt.get("filter_chain") != plan.get("filter_chain"):
        raise ContractError("speech cleanup receipt filter chain drifted")
    if receipt.get("technical_qa") != _technical_qa(source, output, plan):
        raise ContractError("speech cleanup technical QA drifted")
    if receipt.get("audition") != {
        "status": "needs_audition",
        "required_checks": [
            "speech_naturalness",
            "residual_noise",
            "pumping_or_musical_noise",
        ],
        "automatic_release": False,
    }:
        raise ContractError("speech cleanup audition boundary is invalid")
    if receipt.get("authority_boundary") != _receipt_boundary():
        raise ContractError("speech cleanup receipt boundary is invalid")


def write_speech_cleanup_contract(
    payload: dict[str, Any], output_path: str | Path
) -> Path:
    schema = payload.get("schema")
    if schema == SPEECH_CLEANUP_PLAN_SCHEMA:
        validate_speech_cleanup_plan(payload)
    elif schema == SPEECH_CLEANUP_RECEIPT_SCHEMA:
        validate_speech_cleanup_receipt(payload)
    else:
        raise ContractError("unsupported speech cleanup contract")
    return atomic_write_json(payload, output_path)


def _technical_qa(
    source_path: Path, output_path: Path, plan: dict[str, Any]
) -> dict[str, Any]:
    source = _audio_summary(probe_media_metadata(source_path))
    output = _audio_summary(probe_media_metadata(output_path))
    duration_delta_ms = round(
        abs(output["duration_s"] - source["duration_s"]) * 1000, 3
    )
    qa = {
        "source_audio": source,
        "cleaned_audio": output,
        "duration_delta_ms": duration_delta_ms,
        "duration_tolerance_ms": 100.0,
        "duration_within_tolerance": duration_delta_ms <= 100.0,
        "channel_count_preserved": output["channels"] == source["channels"],
        "output_sample_rate_matches_plan": output["sample_rate"]
        == int(plan["output"]["sample_rate"]),
        "spectral_improvement_claimed": False,
        "voice_quality_claimed": False,
    }
    if not (
        qa["duration_within_tolerance"]
        and qa["channel_count_preserved"]
        and qa["output_sample_rate_matches_plan"]
    ):
        raise ContractError("speech cleanup output failed technical QA")
    return qa


def _audio_summary(metadata: dict[str, Any]) -> dict[str, Any]:
    streams = [
        row
        for row in metadata.get("streams") or []
        if isinstance(row, dict) and row.get("codec_type") == "audio"
    ]
    if not streams:
        raise ContractError("speech cleanup media has no audio stream")
    stream = streams[0]
    duration_raw = stream.get("duration") or _object(metadata.get("format")).get(
        "duration"
    )
    duration = _positive_number(duration_raw, "audio duration")
    sample_rate = int(_positive_number(stream.get("sample_rate"), "audio sample rate"))
    channels = int(_positive_number(stream.get("channels"), "audio channels"))
    return {
        "codec_name": str(stream.get("codec_name") or ""),
        "sample_rate": sample_rate,
        "channels": channels,
        "duration_s": round(duration, 6),
    }


def _require_ffmpeg_role(
    receipt: dict[str, Any], section: str, role: str, expected_path: Path
) -> None:
    matches = [
        row
        for row in receipt.get(section) or []
        if isinstance(row, dict) and row.get("role") == role
    ]
    if len(matches) != 1:
        raise ContractError(f"speech cleanup FFmpeg receipt requires one {role}")
    actual = validate_artifact_reference(
        _object(matches[0].get("artifact")), f"speech cleanup {role}"
    )
    if actual != expected_path:
        raise ContractError(f"speech cleanup FFmpeg receipt {role} path drifted")


def _validate_settings(
    profile: str, noise_kinds: list[str], mains_hz: int, sample_rate: int
) -> None:
    if profile not in PROFILE_SETTINGS:
        raise ContractError("unsupported speech cleanup profile")
    if noise_kinds != sorted(set(noise_kinds)) or (not noise_kinds and profile != "original"):
        raise ContractError("speech cleanup noise kinds must be non-empty and unique")
    if not set(noise_kinds) <= NOISE_KINDS:
        raise ContractError("unsupported speech cleanup noise kind")
    if mains_hz not in {50, 60}:
        raise ContractError("speech cleanup mains frequency must be 50 or 60 Hz")
    if sample_rate != 48000:
        raise ContractError("speech cleanup output sample rate must be 48000 Hz")


def _source_provenance() -> dict[str, Any]:
    return {
        "project": "FFmpeg",
        "repo_url": "https://github.com/FFmpeg/FFmpeg.git",
        "source_commit": FFMPEG_SOURCE_COMMIT,
        "source_files": [
            "libavfilter/af_afftdn.c",
            "libavfilter/af_biquads.c",
            "doc/filters.texi",
        ],
        "reuse_mode": "direct_filter_reuse_thin_adapter",
    }


def _plan_boundary() -> dict[str, Any]:
    return {
        "local_only": True,
        "source_audio_modified": False,
        "automatic_noise_diagnosis": False,
        "automatic_profile_selection": False,
        "voice_quality_claimed": False,
        "release_eligible": False,
        "automatic_upload": False,
        "automatic_publish": False,
        "single_ffmpeg_outlet": FFMPEG_OUTLET_ID,
    }


def _receipt_boundary() -> dict[str, Any]:
    return {
        "candidate_only": True,
        "technical_processing_completed": True,
        "human_audition_required": True,
        "creative_approval_claimed": False,
        "release_eligible": False,
        "automatic_upload": False,
        "automatic_publish": False,
        "single_ffmpeg_outlet": FFMPEG_OUTLET_ID,
    }


def _atomic_promote(staged_path: Path, output_path: Path) -> None:
    if not staged_path.is_file():
        raise ContractError("speech cleanup staging output was not created")
    temporary = output_path.with_name(f".{output_path.name}.{uuid4().hex}.tmp")
    try:
        shutil.copyfile(staged_path, temporary)
        temporary.replace(output_path)
    except OSError as exc:
        raise ContractError(f"speech cleanup output promotion failed: {exc}") from exc
    finally:
        if temporary.exists():
            temporary.unlink()


def _validate_output_paths(source: Path, plan: Path, *outputs: Path) -> None:
    protected = {source, plan}
    seen: set[Path] = set()
    for path in outputs:
        if path in protected:
            raise ContractError("speech cleanup cannot overwrite a bound input")
        if path in seen:
            raise ContractError("speech cleanup output paths must be distinct")
        seen.add(path)


def _validate_schema(payload: dict[str, Any], filename: str, label: str) -> None:
    schema = read_json_object(Path(__file__).with_name("schemas") / filename, label)
    try:
        validator = Draft202012Validator(schema)
        validator.check_schema(schema)
        validator.validate(payload)
    except (SchemaError, ValidationError) as exc:
        raise ContractError(f"{label} schema validation failed: {exc.message}") from exc


def _run_tool(command: list[str], label: str) -> str:
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=30.0,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ContractError(f"{label} failed: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()[-2000:]
        raise ContractError(f"{label} failed: {detail}")
    return (result.stdout or "") + (result.stderr or "")


def _positive_number(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise ContractError(f"{label} must be a positive finite number")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{label} must be a positive finite number") from exc
    if not math.isfinite(number) or number <= 0:
        raise ContractError(f"{label} must be a positive finite number")
    return number


def _format_number(value: Any) -> str:
    number = _positive_or_negative_number(value, "FFmpeg filter value")
    return f"{number:.6f}"


def _positive_or_negative_number(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise ContractError(f"{label} must be a finite number")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{label} must be a finite number") from exc
    if not math.isfinite(number):
        raise ContractError(f"{label} must be a finite number")
    return number


def _object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}
