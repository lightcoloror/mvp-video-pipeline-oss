from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable, Mapping

from .contract_io import (
    artifact_reference,
    atomic_write_json,
    atomic_write_text,
    payload_sha256,
    read_json_object,
    validate_artifact_reference,
)
from .errors import ContractError
from .review_resource_guard import (
    ReviewResourceGuard,
    ReviewResourcePolicy,
    apply_ffmpeg_thread_limit,
    below_normal_priority_kwargs,
)

SPEECH_EXECUTION_RECEIPT_SCHEMA = "video_creation_pipeline.speech_execution_receipt.v1"
SPEECH_INPUT_CONTRACT_SCHEMA = "video_creation_pipeline.speech_input_contract.v1"
FFMPEG_EXECUTION_RECEIPT_SCHEMA = "video_creation_pipeline.ffmpeg_execution_receipt.v1"

CRISPASR_VERSION = "v0.8.18"
CRISPASR_COMMIT = "9deefe8f47273722415e4b4be5d87361b96177c9"
FFMPEG_OUTLET_ID = "video_creation_pipeline.single_ffmpeg_outlet"
FFMPEG_OPERATIONS = {
    "frame_extract",
    "render",
    "concat",
    "trim",
    "transcode",
    "audio_qa",
    "audio_mastering",
    "speech_cleanup",
    "music_mix",
    "sound_effect_mix",
    "technical_qa",
}
RESOURCE_GUARDED_REVIEW_OPERATIONS = {
    "frame_extract",
    "audio_qa",
    "technical_qa",
}


def resolve_media_tool(tool: str) -> str:
    """Resolve the FFmpeg/FFprobe executable behind a Windows command wrapper."""

    if tool not in {"ffmpeg", "ffprobe"}:
        raise ContractError("unsupported media tool")
    executable = _resolve_windows_tool_wrapper(shutil.which(tool), tool)
    if not executable:
        raise ContractError(f"{tool} executable was not found")
    return executable


def probe_media_metadata(
    media_path: str | Path,
    *,
    timeout_s: float = 30.0,
) -> dict[str, Any]:
    """Read local media metadata through the shared FFprobe tool boundary."""
    media = Path(media_path).expanduser().resolve()
    if not media.is_file():
        raise ContractError(f"media file not found: {media}")
    executable = _resolve_windows_tool_wrapper(shutil.which("ffprobe"), "ffprobe")
    if not executable:
        raise ContractError("ffprobe executable was not found")
    try:
        result = subprocess.run(
            [
                executable,
                "-v",
                "error",
                "-show_streams",
                "-show_format",
                "-of",
                "json",
                str(media),
            ],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=float(timeout_s),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ContractError(f"ffprobe metadata read failed: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()[-2000:]
        raise ContractError(f"ffprobe metadata read failed: {detail}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ContractError("ffprobe returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise ContractError("ffprobe returned an invalid metadata document")
    return payload


def probe_packet_timestamps(media_path, start_s, *, packet_limit=120, timeout_s=10.0):
    """Bounded read-only cadence evidence. Packet probing does not decode or render."""
    media = Path(media_path).expanduser().resolve()
    if not media.is_file() or isinstance(start_s, bool) or not isinstance(start_s, (int, float)) or not math.isfinite(start_s) or start_s < 0:
        raise ContractError("invalid packet probe source/start")
    if isinstance(packet_limit, bool) or not isinstance(packet_limit, int) or not 2 <= packet_limit <= 240:
        raise ContractError("packet probe limit must be 2..240")
    executable = _resolve_windows_tool_wrapper(shutil.which("ffprobe"), "ffprobe")
    if not executable:
        raise ContractError("ffprobe executable was not found")
    command = [executable, "-v", "error", "-select_streams", "v:0", "-read_intervals", f"{start_s}%+#{packet_limit}", "-show_packets", "-show_entries", "packet=pts_time,duration_time", "-of", "json", str(media)]
    try:
        result = subprocess.run(command, capture_output=True, text=True, errors="replace", timeout=float(timeout_s), check=False)
        if result.returncode:
            raise ContractError("bounded cadence probe failed")
        value = json.loads(result.stdout)
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        raise ContractError("bounded cadence probe unavailable") from exc
    if not isinstance(value, dict) or not isinstance(value.get("packets"), list):
        raise ContractError("invalid cadence probe result")
    return value["packets"]


def validate_speech_execution_receipt(receipt: dict[str, Any]) -> None:
    if receipt.get("schema") != SPEECH_EXECUTION_RECEIPT_SCHEMA:
        raise ContractError("unsupported speech execution receipt schema")
    if receipt.get("receipt_sha256") != payload_sha256(receipt, "receipt_sha256"):
        raise ContractError("speech execution receipt integrity check failed")
    if receipt.get("owner") != "video-knowledge-pipeline":
        raise ContractError("speech execution must remain owned by VKP")
    if receipt.get("status") != "completed":
        raise ContractError("video creation only consumes completed speech receipts")

    engine = _object(receipt.get("engine"))
    if (
        engine.get("name") != "CrispASR"
        or engine.get("version") != CRISPASR_VERSION
        or engine.get("source_commit") != CRISPASR_COMMIT
    ):
        raise ContractError("speech receipt is not the reviewed CrispASR candidate")
    for field in ("binary_sha256", "model_sha256"):
        if not _is_sha256(engine.get(field)):
            raise ContractError(f"speech engine {field} is invalid")
    for field in ("requested_backend", "used_backend", "device"):
        if not str(engine.get(field) or "").strip():
            raise ContractError(f"speech engine {field} is required")
    if not isinstance(engine.get("cpu_retry_performed"), bool):
        raise ContractError("speech receipt must report whether CPU retry occurred")
    retries = receipt.get("attempts")
    if not isinstance(retries, list) or not retries:
        raise ContractError("speech receipt requires visible execution attempts")
    if engine["cpu_retry_performed"]:
        if len(retries) < 2 or str(retries[-1].get("backend") or "").lower() != "cpu":
            raise ContractError("CrispASR CPU retry must be visible in attempts")
    for index, attempt in enumerate(retries, 1):
        if not isinstance(attempt, dict):
            raise ContractError("speech execution attempt must be an object")
        if (
            not str(attempt.get("backend") or "").strip()
            or str(attempt.get("status") or "") not in {"completed", "failed"}
            or _nonnegative_number(
                attempt.get("duration_ms"), f"attempt {index} duration_ms"
            )
            < 0
        ):
            raise ContractError("speech execution attempt is incomplete")
    final_attempt = retries[-1]
    if final_attempt.get("status") != "completed":
        raise ContractError(
            "completed speech receipt requires a completed final attempt"
        )
    if (
        str(final_attempt.get("backend") or "").strip().lower()
        != str(engine["used_backend"]).strip().lower()
    ):
        raise ContractError(
            "speech engine used_backend must match the completed final attempt"
        )

    validate_artifact_reference(
        _object(receipt.get("input_audio")), "speech input audio"
    )
    outputs = _object(receipt.get("outputs"))
    validate_artifact_reference(
        _object(outputs.get("transcript")), "arbitrated transcript"
    )
    validate_artifact_reference(
        _object(outputs.get("word_timestamps")), "speech word timestamps"
    )
    if outputs.get("srt") is not None:
        validate_artifact_reference(_object(outputs.get("srt")), "speech SRT")

    chunking = _object(receipt.get("chunking"))
    seconds = _nonnegative_number(chunking.get("seconds"), "chunk seconds")
    overlap = _nonnegative_number(chunking.get("overlap_seconds"), "chunk overlap")
    if seconds and overlap >= seconds:
        raise ContractError("speech chunk overlap must be shorter than a chunk")
    if (
        chunking.get("lcs_deduplication") is not True
        or chunking.get("punctuation_split") is not True
    ):
        raise ContractError("speech chunk recovery policy is incomplete")

    arbitration = _object(receipt.get("arbitration"))
    if (
        arbitration.get("performed_by") != "video-knowledge-pipeline"
        or arbitration.get("status") != "accepted"
        or arbitration.get("transcript_is_authoritative") is not True
        or arbitration.get("overwrites_source_evidence") is not False
    ):
        raise ContractError("speech receipt lacks accepted VKP arbitration")
    boundary = _object(receipt.get("execution_boundary"))
    if (
        boundary.get("video_creation_asr_executed") is not False
        or boundary.get("automatic_local_cloud_fallback") is not False
        or boundary.get("second_asr_truth_created") is not False
    ):
        raise ContractError("speech execution boundary is invalid")


def build_speech_input_contract(
    receipt_path: str | Path,
) -> dict[str, Any]:
    source = Path(receipt_path).expanduser().resolve()
    receipt = read_json_object(source, "speech execution receipt")
    validate_speech_execution_receipt(receipt)
    contract: dict[str, Any] = {
        "schema": SPEECH_INPUT_CONTRACT_SCHEMA,
        "source_receipt": artifact_reference(source, "speech execution receipt"),
        "engine": dict(receipt["engine"]),
        "transcript": dict(receipt["outputs"]["transcript"]),
        "word_timestamps": dict(receipt["outputs"]["word_timestamps"]),
        "arbitration": dict(receipt["arbitration"]),
        "consumption_policy": {
            "read_only": True,
            "candidate_for_edit_timing": True,
            "video_creation_may_run_asr": False,
            "may_replace_vkp_transcript": False,
        },
    }
    contract["contract_sha256"] = payload_sha256(contract, "contract_sha256")
    validate_speech_input_contract(contract)
    return contract


def validate_speech_input_contract(contract: dict[str, Any]) -> None:
    if contract.get("schema") != SPEECH_INPUT_CONTRACT_SCHEMA:
        raise ContractError("unsupported speech input contract schema")
    if contract.get("contract_sha256") != payload_sha256(contract, "contract_sha256"):
        raise ContractError("speech input contract integrity check failed")
    receipt_path = validate_artifact_reference(
        _object(contract.get("source_receipt")), "speech execution receipt"
    )
    receipt = read_json_object(receipt_path, "speech execution receipt")
    validate_speech_execution_receipt(receipt)
    if (
        contract.get("engine") != receipt.get("engine")
        or contract.get("transcript") != receipt.get("outputs", {}).get("transcript")
        or contract.get("word_timestamps")
        != receipt.get("outputs", {}).get("word_timestamps")
        or contract.get("arbitration") != receipt.get("arbitration")
    ):
        raise ContractError("speech input contract drifted from the VKP receipt")
    policy = _object(contract.get("consumption_policy"))
    if (
        policy.get("read_only") is not True
        or policy.get("candidate_for_edit_timing") is not True
        or policy.get("video_creation_may_run_asr") is not False
        or policy.get("may_replace_vkp_transcript") is not False
    ):
        raise ContractError("speech input consumption policy is incomplete")


def build_ffmpeg_execution_receipt(
    operation: str,
    command: list[str],
    inputs: list[dict[str, Any]],
    outputs: list[dict[str, Any]],
    *,
    requested_backend: str = "software",
    selected_backend: str = "software",
    hardware_accelerated: bool = False,
    fallback_used: bool = False,
    fallback_reason: str = "",
    resource_guard: Mapping[str, Any] | None = None,
    executed_command: list[str] | None = None,
) -> dict[str, Any]:
    if operation not in FFMPEG_OPERATIONS:
        raise ContractError("FFmpeg receipt operation is unsupported")
    normalized_command = [str(value) for value in command]
    if not normalized_command or normalized_command[0] != "ffmpeg":
        raise ContractError("FFmpeg receipt command must start with ffmpeg")
    if not str(requested_backend).strip() or not str(selected_backend).strip():
        raise ContractError("FFmpeg execution backends are required")
    if not isinstance(hardware_accelerated, bool) or not isinstance(
        fallback_used, bool
    ):
        raise ContractError("FFmpeg execution profile flags must be booleans")
    if fallback_used and not str(fallback_reason).strip():
        raise ContractError("FFmpeg fallback requires a visible reason")
    receipt: dict[str, Any] = {
        "schema": FFMPEG_EXECUTION_RECEIPT_SCHEMA,
        "outlet_id": FFMPEG_OUTLET_ID,
        "status": "completed",
        "operation": operation,
        "execution_profile": {
            "selected_by": FFMPEG_OUTLET_ID,
            "requested_backend": str(requested_backend),
            "selected_backend": str(selected_backend),
            "hardware_accelerated": hardware_accelerated,
            "fallback_used": fallback_used,
            "fallback_reason": str(fallback_reason),
            "automatic_local_cloud_fallback": False,
        },
        "command": normalized_command,
        "inputs": _build_artifact_rows(inputs, "FFmpeg input"),
        "outputs": _build_artifact_rows(outputs, "FFmpeg output"),
        "execution_boundary": {
            "orchestration_owner": FFMPEG_OUTLET_ID,
            "second_ffmpeg_orchestrator_created": False,
            "source_media_modified": False,
            "automatic_publish": False,
        },
    }
    if resource_guard is not None:
        receipt["resource_guard"] = dict(resource_guard)
    if executed_command is not None:
        receipt["executed_command"] = [str(value) for value in executed_command]
    receipt["receipt_sha256"] = payload_sha256(receipt, "receipt_sha256")
    # Both computed and supplied references were content-verified above.
    # Avoid a second full-file pass; public validation still verifies contents.
    validate_ffmpeg_execution_receipt(receipt, verify_artifacts=False)
    return receipt


def execute_ffmpeg_with_receipt(
    operation: str,
    command: list[str],
    inputs: list[dict[str, Any]],
    outputs: list[dict[str, Any]],
    receipt_path: str | Path,
    *,
    capture_log_path: str | Path | None = None,
    timeout_s: float = 120.0,
    resource_policy: ReviewResourcePolicy | None = None,
    resource_snapshot_provider: Callable[[], Mapping[str, Any]] | None = None,
    guard_resource: bool | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    resource_lock_path: str | Path | None = None,
) -> dict[str, Any]:
    if not command or command[0] != "ffmpeg":
        raise ContractError("FFmpeg outlet command must start with ffmpeg")
    executable = _resolve_windows_tool_wrapper(shutil.which("ffmpeg"), "ffmpeg")
    if not executable:
        raise ContractError("ffmpeg executable was not found")
    selected_runner = runner or subprocess.run
    guarded = (
        operation in RESOURCE_GUARDED_REVIEW_OPERATIONS
        if guard_resource is None
        else bool(guard_resource)
    )
    resource_guard: Mapping[str, Any] | None = None
    executed_command: list[str] | None = None
    try:
        if guarded:
            policy = resource_policy or ReviewResourcePolicy.from_environment()
            guard = ReviewResourceGuard(
                policy=policy,
                lock_path=resource_lock_path,
                snapshot_provider=resource_snapshot_provider,
            )
            executed_command = apply_ffmpeg_thread_limit(
                command, policy.ffmpeg_threads
            )
            with guard:
                result = selected_runner(
                    [executable, *executed_command[1:]],
                    capture_output=True,
                    text=True,
                    errors="replace",
                    timeout=float(timeout_s),
                    check=False,
                    **below_normal_priority_kwargs(),
                )
            resource_guard = guard.receipt_metadata()
        else:
            result = selected_runner(
                [executable, *command[1:]],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=float(timeout_s),
                check=False,
            )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ContractError(f"FFmpeg outlet execution failed: {exc}") from exc
    if capture_log_path is not None:
        log_path = Path(capture_log_path).expanduser().resolve()
        atomic_write_text(
            (result.stderr or "") + (result.stdout or ""),
            log_path,
        )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()[-2000:]
        raise ContractError(
            f"FFmpeg outlet returned exit code {result.returncode}: {detail}"
        )
    receipt = build_ffmpeg_execution_receipt(
        operation,
        command,
        inputs,
        outputs,
        resource_guard=resource_guard,
        executed_command=executed_command,
    )
    # ``build_ffmpeg_execution_receipt`` has already validated the receipt and
    # bound its artifacts.  Writing it directly avoids hashing a large input a
    # second time merely to persist the same contract.
    atomic_write_json(receipt, receipt_path)
    return receipt


def execute_ffmpeg_frame_series_with_receipt(
    command: list[str],
    inputs: list[dict[str, Any]],
    output_pattern: str | Path,
    receipt_path: str | Path,
    *,
    media_time_step_s: float,
    capture_log_path: str | Path | None = None,
    timeout_s: float = 120.0,
    resource_policy: ReviewResourcePolicy | None = None,
    resource_snapshot_provider: Callable[[], Mapping[str, Any]] | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    resource_lock_path: str | Path | None = None,
) -> tuple[dict[str, Any], list[Path]]:
    """Run one FFmpeg frame-series extraction through the existing outlet.

    ``execute_ffmpeg_with_receipt`` expects output files to be known before the
    command starts. Frame-series extraction is different because FFmpeg decides
    the final sequence length. This outlet variant executes once, inventories the
    newly written numbered frames, and then produces the same hash-bound receipt.
    It is intentionally limited to ``frame_extract`` and never edits the source.
    """

    if not command or command[0] != "ffmpeg":
        raise ContractError("FFmpeg outlet command must start with ffmpeg")
    pattern = Path(output_pattern).expanduser().resolve()
    _frame_pattern_regex(pattern)
    if not any(
        _resolved_command_path(value) == pattern
        for value in command[1:]
        if "%" in str(value)
    ):
        raise ContractError("frame-series output pattern must match the FFmpeg command")
    step = _nonnegative_number(media_time_step_s, "frame media time step")
    if step <= 0:
        raise ContractError("frame media time step must be positive")
    pattern.parent.mkdir(parents=True, exist_ok=True)
    if _ffmpeg_frame_inventory(pattern):
        raise ContractError("frame-series output already contains matching frames")
    executable = _resolve_windows_tool_wrapper(shutil.which("ffmpeg"), "ffmpeg")
    if not executable:
        raise ContractError("ffmpeg executable was not found")
    policy = resource_policy or ReviewResourcePolicy.from_environment()
    guard = ReviewResourceGuard(
        policy=policy,
        lock_path=resource_lock_path,
        snapshot_provider=resource_snapshot_provider,
    )
    effective_command = apply_ffmpeg_thread_limit(command, policy.ffmpeg_threads)
    selected_runner = runner or subprocess.run
    try:
        with guard:
            result = selected_runner(
                [executable, *effective_command[1:]],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=float(timeout_s),
                check=False,
                **below_normal_priority_kwargs(),
            )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ContractError(f"FFmpeg outlet execution failed: {exc}") from exc
    if capture_log_path is not None:
        log_path = Path(capture_log_path).expanduser().resolve()
        atomic_write_text(
            (result.stderr or "") + (result.stdout or ""),
            log_path,
        )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()[-2000:]
        raise ContractError(
            f"FFmpeg outlet returned exit code {result.returncode}: {detail}"
        )
    frames = _ffmpeg_frame_inventory(pattern)
    if not frames:
        raise ContractError("FFmpeg frame extraction produced no frames")
    outputs = [
        {"role": "frame", "path": path, "media_time_s": index * step}
        for index, path in enumerate(frames)
    ]
    receipt = build_ffmpeg_execution_receipt(
        "frame_extract",
        command,
        inputs,
        outputs,
        resource_guard=guard.receipt_metadata(),
        executed_command=effective_command,
    )
    atomic_write_json(receipt, receipt_path)
    return receipt, frames


def _frame_pattern_regex(pattern: Path) -> re.Pattern[str]:
    placeholders = list(re.finditer(r"%(?:0([1-9]\d*))?d", pattern.name))
    if len(placeholders) != 1:
        raise ContractError(
            "frame-series output must contain one numbered FFmpeg pattern"
        )
    placeholder = placeholders[0]
    width = placeholder.group(1)
    digits = rf"(?P<index>\d{{{int(width)}}})" if width else r"(?P<index>\d+)"
    return re.compile(
        rf"^{re.escape(pattern.name[: placeholder.start()])}"
        rf"{digits}{re.escape(pattern.name[placeholder.end() :])}$"
    )


def _ffmpeg_frame_inventory(pattern: Path) -> list[Path]:
    matcher = _frame_pattern_regex(pattern)
    indexed: list[tuple[int, Path]] = []
    for path in pattern.parent.iterdir():
        match = matcher.fullmatch(path.name) if path.is_file() else None
        if match is not None:
            indexed.append((int(match.group("index")), path.resolve()))
    indexed.sort(key=lambda row: row[0])
    if len({index for index, _path in indexed}) != len(indexed):
        raise ContractError("frame-series output contains duplicate frame indexes")
    return [path for _index, path in indexed]


def _resolved_command_path(value: Any) -> Path | None:
    try:
        return Path(str(value)).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return None


def _resolve_windows_tool_wrapper(candidate: str | None, tool: str) -> str | None:
    if not candidate:
        return None
    path = Path(candidate).expanduser().resolve()
    if path.suffix.casefold() not in {".cmd", ".bat"}:
        return str(path)
    try:
        content = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise ContractError(f"{tool} command wrapper is unreadable: {exc}") from exc
    pattern = re.compile(
        r'^\s*"%~dp0(?P<relative>[^"\r\n]+\.exe)"\s+%\*\s*$',
        flags=re.IGNORECASE | re.MULTILINE,
    )
    match = pattern.search(content)
    if match is None:
        raise ContractError(
            f"{tool} command wrapper is unsupported; an explicit executable is required"
        )
    target = (path.parent / match.group("relative")).resolve()
    if not target.is_file():
        raise ContractError(f"{tool} command wrapper target was not found")
    return str(target)


def validate_ffmpeg_execution_receipt(
    receipt: dict[str, Any], *, verify_artifacts: bool = True
) -> None:
    if receipt.get("schema") != FFMPEG_EXECUTION_RECEIPT_SCHEMA:
        raise ContractError("unsupported FFmpeg execution receipt schema")
    if receipt.get("receipt_sha256") != payload_sha256(receipt, "receipt_sha256"):
        raise ContractError("FFmpeg execution receipt integrity check failed")
    if receipt.get("outlet_id") != FFMPEG_OUTLET_ID:
        raise ContractError("FFmpeg work must use the existing single outlet")
    if receipt.get("status") != "completed":
        raise ContractError("only completed FFmpeg receipts can be consumed")
    if receipt.get("operation") not in FFMPEG_OPERATIONS:
        raise ContractError("FFmpeg receipt operation is unsupported")

    profile = _object(receipt.get("execution_profile"))
    if profile.get("selected_by") != FFMPEG_OUTLET_ID:
        raise ContractError("FFmpeg hardware profile was not selected by the outlet")
    for field in ("requested_backend", "selected_backend"):
        if not str(profile.get(field) or "").strip():
            raise ContractError(f"FFmpeg execution profile {field} is required")
    if not isinstance(profile.get("hardware_accelerated"), bool):
        raise ContractError("FFmpeg execution profile must report acceleration")
    if not isinstance(profile.get("fallback_used"), bool):
        raise ContractError("FFmpeg execution profile must report fallback")
    if (
        profile["fallback_used"]
        and not str(profile.get("fallback_reason") or "").strip()
    ):
        raise ContractError("FFmpeg hardware fallback requires a visible reason")
    if profile.get("automatic_local_cloud_fallback") is not False:
        raise ContractError("FFmpeg receipt cannot use local/cloud fallback")
    command = receipt.get("command")
    if not isinstance(command, list) or not command or command[0] != "ffmpeg":
        raise ContractError("FFmpeg receipt requires the executed argument vector")

    inputs = receipt.get("inputs")
    outputs = receipt.get("outputs")
    if not isinstance(inputs, list) or not inputs:
        raise ContractError("FFmpeg receipt requires input artifacts")
    if not isinstance(outputs, list) or not outputs:
        raise ContractError("FFmpeg receipt requires output artifacts")
    _validate_artifact_rows(
        inputs, "FFmpeg input", verify_artifacts=verify_artifacts
    )
    _validate_artifact_rows(
        outputs, "FFmpeg output", verify_artifacts=verify_artifacts
    )
    for row in outputs:
        if receipt["operation"] == "frame_extract":
            _nonnegative_number(row.get("media_time_s"), "frame media_time_s")
            if str(row.get("role") or "") != "frame":
                raise ContractError("frame extraction outputs must use role=frame")

    boundary = _object(receipt.get("execution_boundary"))
    if (
        boundary.get("orchestration_owner") != FFMPEG_OUTLET_ID
        or boundary.get("second_ffmpeg_orchestrator_created") is not False
        or boundary.get("source_media_modified") is not False
        or boundary.get("automatic_publish") is not False
    ):
        raise ContractError("FFmpeg receipt execution boundary is invalid")


def write_execution_contract(payload: dict[str, Any], output_path: str | Path) -> Path:
    schema = payload.get("schema")
    if schema == SPEECH_INPUT_CONTRACT_SCHEMA:
        validate_speech_input_contract(payload)
    elif schema == SPEECH_EXECUTION_RECEIPT_SCHEMA:
        validate_speech_execution_receipt(payload)
    elif schema == FFMPEG_EXECUTION_RECEIPT_SCHEMA:
        validate_ffmpeg_execution_receipt(payload)
    else:
        raise ContractError("unsupported execution contract")
    return atomic_write_json(payload, output_path)


def _validate_artifact_rows(
    rows: list[Any], label: str, *, verify_artifacts: bool = True
) -> None:
    roles: set[str] = set()
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise ContractError(f"{label} row must be an object")
        role = str(row.get("role") or "").strip()
        if not role:
            raise ContractError(f"{label} role is required")
        unique_role = f"{role}:{index}"
        if unique_role in roles:
            raise ContractError(f"{label} role is duplicated")
        roles.add(unique_role)
        artifact = _object(row.get("artifact"))
        if verify_artifacts:
            validate_artifact_reference(artifact, f"{label} {index}")
        else:
            _validate_artifact_reference_metadata(artifact, f"{label} {index}")


def _object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _is_sha256(value: Any) -> bool:
    return bool(re.fullmatch(r"[0-9a-f]{64}", str(value or "").lower()))


def _nonnegative_number(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise ContractError(f"{label} must be a non-negative finite number")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{label} must be a non-negative finite number") from exc
    if result < 0 or not math.isfinite(result):
        raise ContractError(f"{label} must be a non-negative finite number")

    return result


def _build_artifact_rows(
    rows: list[dict[str, Any]], label: str
) -> list[dict[str, Any]]:
    if not isinstance(rows, list) or not rows:
        raise ContractError(f"{label} artifacts must be a non-empty list")
    normalized: list[dict[str, Any]] = []
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise ContractError(f"{label} row must be an object")
        role = str(row.get("role") or "").strip()
        if not role:
            raise ContractError(f"{label} role is required")
        supplied = row.get("artifact")
        if supplied is not None:
            validate_artifact_reference(_object(supplied), f"{label} {index}")
        artifact = (
            _validate_artifact_reference_metadata(
                _object(supplied), f"{label} {index}"
            )
            if supplied is not None
            else artifact_reference(str(row.get("path") or ""), f"{label} {index}")
        )
        normalized_row: dict[str, Any] = {"role": role, "artifact": artifact}
        if row.get("media_time_s") is not None:
            normalized_row["media_time_s"] = _nonnegative_number(
                row["media_time_s"], f"{label} media_time_s"
            )
        normalized.append(normalized_row)
    return normalized


def _validate_artifact_reference_metadata(
    reference: dict[str, Any], label: str
) -> dict[str, Any]:
    """Validate a precomputed artifact without re-reading its full contents."""

    path = Path(str(reference.get("path") or "")).expanduser().resolve()
    if not path.is_file():
        raise ContractError(f"{label} not found: {path}")
    try:
        byte_count = int(reference.get("bytes"))
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{label} byte count is invalid") from exc
    if byte_count != path.stat().st_size:
        raise ContractError(f"{label} size changed after hashing")
    sha256 = str(reference.get("sha256") or "").lower()
    if not _is_sha256(sha256):
        raise ContractError(f"{label} SHA-256 is invalid")
    content_kind = str(reference.get("content_kind") or "")
    canonical = reference.get("canonical_sha256")
    if content_kind == "binary":
        if canonical is not None:
            raise ContractError(f"{label} binary canonical hash must be null")
    elif content_kind == "json":
        if not _is_sha256(canonical):
            raise ContractError(f"{label} JSON canonical hash is invalid")
    else:
        raise ContractError(f"{label} content kind is invalid")
    return {
        "path": str(path),
        "bytes": byte_count,
        "sha256": sha256,
        "content_kind": content_kind,
        "canonical_sha256": canonical,
    }
