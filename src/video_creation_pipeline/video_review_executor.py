from __future__ import annotations

import inspect
import errno
import math
import os
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

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
from .execution_receipts import (
    FFMPEG_EXECUTION_RECEIPT_SCHEMA,
    FFMPEG_OUTLET_ID,
    execute_ffmpeg_with_receipt,
)


EXECUTION_RECEIPT_SCHEMA = "video_creation_pipeline.video_review_execution_receipt.v1"
CACHE_RECEIPT_SCHEMA = "video_creation_pipeline.video_review_execution_cache_receipt.v1"
QA_REPORT_SCHEMA = "video_creation_pipeline.video_review_qa_report.v1"
CHECK_PROFILE_SCHEMA = "video_creation_pipeline.video_review_check_profile.v1"
RESOURCE_POLICY_ID = "video-review-resource-guard-v1"
EXECUTION_POLICY_REF = {
    "canonical_owner": FFMPEG_OUTLET_ID,
    "policy_id": RESOURCE_POLICY_ID,
    "heavy_media_concurrency": 1,
    "full_decode_once_per": "exact_media_sha256_and_check_profile_sha256",
    "proxy_first": False,
    "limited_rework_scope": "changed_windows_only",
    "cached_report_analysis_parallel_allowed": True,
}

_DEFAULT_DETECTORS = {
    "black_duration_seconds": 0.5,
    "black_pixel_threshold": 0.10,
    "freeze_noise_db": -50.0,
    "freeze_duration_seconds": 1.5,
    "silence_noise_db": -50.0,
    "silence_duration_seconds": 1.0,
}


def execute_review(
    plan_path: str | Path,
    cache_dir: str | Path,
    output_path: str | Path,
    *,
    check_profile_extension: dict[str, Any] | None = None,
    ffmpeg_executor: Callable[..., dict[str, Any]] | None = None,
    ffmpeg_runner: Callable[..., Any] | None = None,
    resource_snapshot_provider: Callable[..., Any] | None = None,
    resource_lock_path: str | Path | None = None,
    timeout_s: float = 120.0,
    lock_timeout_s: float = 30.0,
) -> dict[str, Any]:
    """Execute or reuse hash-bound review QA for a self-media review plan."""

    plan_file = Path(plan_path).expanduser().resolve()
    output = Path(output_path).expanduser().resolve()
    cache_root = Path(cache_dir).expanduser().resolve()
    plan = _read_and_validate_plan(plan_file)
    media = _resolve_media(plan_file, plan)
    media_fingerprint = _file_fingerprint(media)
    media_sha256 = file_sha256(media)
    if _file_fingerprint(media) != media_fingerprint:
        raise ContractError("video review source media changed during initial hash")
    expected_media_sha256 = str(plan["inputs"]["source_media"]["sha256"])
    if media_sha256 != expected_media_sha256:
        raise ContractError("video review source media hash drift")

    profile = build_check_profile(plan, check_profile_extension)
    profile_sha256 = canonical_json_sha256(profile)
    cache_key = canonical_json_sha256(
        {
            "media_sha256": media_sha256,
            "check_profile_sha256": profile_sha256,
        }
    )
    cache_entry = cache_root / media_sha256 / profile_sha256
    cache_receipt_path = cache_entry / "cache-receipt.json"
    expected_scope = _scope_from_plan(plan)

    cached = _load_verified_cache(
        cache_receipt_path,
        media_sha256,
        profile_sha256,
        cache_key,
        expected_scope,
    )
    if cached is not None:
        return _write_invocation(
            output,
            disposition="reused",
            media_sha256=media_sha256,
            profile_sha256=profile_sha256,
            cache_key=cache_key,
            scope=expected_scope,
            cache_receipt_path=cache_receipt_path,
            report_path=cached,
            resource_guard={"policy_id": RESOURCE_POLICY_ID, "decision": "cache_reused"},
        )

    lock_path = cache_root / ".locks" / f"{cache_key}.lock"
    with _retained_os_lock(lock_path, timeout_s=lock_timeout_s):
        # Lock-internal revalidation is intentional: it closes both cache and
        # mutable-plan/media races between the optimistic lookup and execution.
        locked_plan = _read_and_validate_plan(plan_file)
        if locked_plan != plan or _file_fingerprint(media) != media_fingerprint:
            raise ContractError("video review inputs changed while waiting for cache lock")
        cached = _load_verified_cache(
            cache_receipt_path,
            media_sha256,
            profile_sha256,
            cache_key,
            expected_scope,
        )
        if cached is not None:
            return _write_invocation(
                output,
                disposition="reused",
                media_sha256=media_sha256,
                profile_sha256=profile_sha256,
                cache_key=cache_key,
                scope=expected_scope,
                cache_receipt_path=cache_receipt_path,
                report_path=cached,
                resource_guard={"policy_id": RESOURCE_POLICY_ID, "decision": "cache_reused"},
            )

        cache_entry.mkdir(parents=True, exist_ok=True)
        executor = ffmpeg_executor or execute_ffmpeg_with_receipt
        commands: list[dict[str, Any]] = []
        resource_rows: list[dict[str, Any]] = []
        try:
            for index, window in enumerate(_execution_windows(plan), start=1):
                label = window["window_id"]
                log_path = cache_entry / f"{index:02d}-{label}-detectors.log"
                ffmpeg_receipt_path = cache_entry / f"{index:02d}-{label}-ffmpeg.json"
                command = _detector_command(media, window, profile["detectors"])
                receipt = _call_ffmpeg_outlet(
                    executor,
                    command=command,
                    media=media,
                    media_artifact={
                        "path": str(media),
                        "bytes": media_fingerprint["size"],
                        "sha256": media_sha256,
                        "content_kind": "binary",
                        "canonical_sha256": None,
                    },
                    log_path=log_path,
                    receipt_path=ffmpeg_receipt_path,
                    timeout_s=timeout_s,
                    ffmpeg_runner=ffmpeg_runner,
                    resource_snapshot_provider=resource_snapshot_provider,
                    resource_lock_path=resource_lock_path,
                )
                commands.append(
                    {
                        "window_id": label,
                        "start_seconds": window["start_seconds"],
                        "end_seconds": window["end_seconds"],
                        "command": command,
                        "ffmpeg_receipt": artifact_reference(
                            ffmpeg_receipt_path, "video review FFmpeg receipt"
                        ),
                        "detector_log": artifact_reference(log_path, "video review detector log"),
                    }
                )
                guard = receipt.get("resource_guard")
                if isinstance(guard, dict):
                    resource_rows.append(dict(guard))
        except ContractError as exc:
            if getattr(exc, "code", "") != "resource_pressure_pause_required":
                raise
            detail = exc.as_dict() if callable(getattr(exc, "as_dict", None)) else {}
            return _write_blocked_invocation(
                output,
                media_sha256=media_sha256,
                profile_sha256=profile_sha256,
                cache_key=cache_key,
                scope=expected_scope,
                detail=detail,
            )

        if _file_fingerprint(media) != media_fingerprint:
            raise ContractError("video review source media changed during execution")
        report = {
            "schema": QA_REPORT_SCHEMA,
            "status": "verified",
            "media_sha256": media_sha256,
            "check_profile_sha256": profile_sha256,
            "cache_key": cache_key,
            "scope": expected_scope,
            "detectors": dict(profile["detectors"]),
            "commands": commands,
            "authority_boundary": {
                "read_only_shared_report": True,
                "source_media_modified": False,
                "creative_approval_claimed": False,
                "automatic_publish": False,
                "second_ffmpeg_orchestrator_created": False,
            },
        }
        report["report_sha256"] = payload_sha256(report, "report_sha256")
        report_path = atomic_write_json(report, cache_entry / "qa-report.json")
        cache_receipt = {
            "schema": CACHE_RECEIPT_SCHEMA,
            "status": "verified",
            "execution_disposition": "performed",
            "created_at": _now(),
            "media_sha256": media_sha256,
            "check_profile_sha256": profile_sha256,
            "cache_key": cache_key,
            "scope": expected_scope,
            "qa_report": artifact_reference(report_path, "video review QA report"),
            "ffmpeg_receipts": [row["ffmpeg_receipt"] for row in commands],
            "resource_guard": {
                "policy_id": RESOURCE_POLICY_ID,
                "decision": "executed",
                "executions": resource_rows,
            },
            "automatic_retry": False,
            "authority_boundary": {
                "cache_is_hash_bound": True,
                "source_media_modified": False,
                "automatic_publish": False,
                "second_ffmpeg_orchestrator_created": False,
            },
        }
        cache_receipt["receipt_sha256"] = payload_sha256(cache_receipt, "receipt_sha256")
        atomic_write_json(cache_receipt, cache_receipt_path)
        return _write_invocation(
            output,
            disposition="performed",
            media_sha256=media_sha256,
            profile_sha256=profile_sha256,
            cache_key=cache_key,
            scope=expected_scope,
            cache_receipt_path=cache_receipt_path,
            report_path=report_path,
            resource_guard=cache_receipt["resource_guard"],
        )


def build_check_profile(
    plan: dict[str, Any], extension: dict[str, Any] | None = None
) -> dict[str, Any]:
    extension = dict(extension or {})
    detectors = dict(_DEFAULT_DETECTORS)
    overrides = extension.pop("detectors", {})
    if not isinstance(overrides, dict) or any(key not in detectors for key in overrides):
        raise ContractError("unsupported video review detector profile")
    for key, value in overrides.items():
        if isinstance(value, bool):
            raise ContractError("video review detector values must be finite numbers")
        number = float(value)
        if not math.isfinite(number):
            raise ContractError("video review detector values must be finite numbers")
        detectors[key] = number
    return {
        "schema": CHECK_PROFILE_SCHEMA,
        "profile_id": "integrated-video-review-v1",
        "ffmpeg_outlet": FFMPEG_OUTLET_ID,
        "resource_policy_id": RESOURCE_POLICY_ID,
        "render_scope": plan["review_scope"]["render_scope"],
        "changed_windows": plan["review_scope"].get("changed_windows", []),
        "proxy_profile": plan.get("proxy_profile", {}),
        "detectors": detectors,
        "extension": extension,
    }


def _read_and_validate_plan(path: Path) -> dict[str, Any]:
    plan = read_json_object(path, "self-media video review plan")
    if plan.get("schema_version") != "self-media-video-review-plan.v1":
        raise ContractError("unsupported self-media video review plan")
    expected = plan.get("plan_payload_sha256")
    if expected != payload_sha256(plan, "plan_payload_sha256"):
        raise ContractError("self-media video review plan integrity check failed")
    scope = plan.get("review_scope")
    inputs = plan.get("inputs")
    source = inputs.get("source_media") if isinstance(inputs, dict) else None
    if not isinstance(scope, dict) or not isinstance(source, dict):
        raise ContractError("self-media video review plan is incomplete")
    render_scope = scope.get("render_scope")
    if render_scope not in {"full_timeline_source", "changed_windows_only"}:
        raise ContractError("self-media video review render scope is unsupported")
    if plan.get("mode") == "limited_rework" and render_scope != "changed_windows_only":
        raise ContractError("limited rework cannot run a full-timeline review")
    if plan.get("mode") == "limited_rework" and not scope.get("changed_windows"):
        raise ContractError("limited rework requires changed windows")
    if plan.get("owner_binding", {}).get("ffmpeg_outlet") != FFMPEG_OUTLET_ID:
        raise ContractError("video review plan is not bound to the single FFmpeg outlet")
    if plan.get("execution_policy_ref") != EXECUTION_POLICY_REF:
        raise ContractError("video review execution policy binding is invalid")
    if plan.get("proxy_profile") != {"enabled": False, "reason": "source_time_range_review"}:
        raise ContractError("review proxy export prohibited; regenerate a source-time-range plan")
    if any(row.get("review_access_mode", "source_time_range") != "source_time_range"
           for row in plan.get("human_review_requests", [])):
        raise ContractError("review media export prohibited")
    return plan


def _resolve_media(plan_path: Path, plan: dict[str, Any]) -> Path:
    raw = Path(str(plan["inputs"]["source_media"].get("path") or ""))
    if raw.is_absolute():
        raise ContractError("self-media review source path must be relative")
    media = (plan_path.parent / raw).resolve()
    try:
        media.relative_to(plan_path.parent.resolve())
    except ValueError as exc:
        raise ContractError("self-media review source path escapes the plan directory") from exc
    if not media.is_file():
        raise ContractError("self-media review source media is missing")
    return media


def _scope_from_plan(plan: dict[str, Any]) -> dict[str, Any]:
    scope = plan["review_scope"]
    windows = list(scope.get("changed_windows", []))
    duration = float(plan["inputs"]["source_media"]["duration_seconds"])
    scanned = duration if scope["render_scope"] == "full_timeline_source" else sum(
        float(row["end_seconds"]) - float(row["start_seconds"]) for row in windows
    )
    return {
        "render_scope": scope["render_scope"],
        "changed_windows": windows,
        "full_timeline_scanned": scope["render_scope"] == "full_timeline_source",
        "scanned_duration_seconds": round(scanned, 3),
    }


def _execution_windows(plan: dict[str, Any]) -> list[dict[str, Any]]:
    if plan["review_scope"]["render_scope"] == "changed_windows_only":
        return list(plan["review_scope"]["changed_windows"])
    duration = round(float(plan["inputs"]["source_media"]["duration_seconds"]), 3)
    return [{"window_id": "full-timeline", "start_seconds": 0.0, "end_seconds": duration}]


def _detector_command(
    media: Path, window: dict[str, Any], detectors: dict[str, float]
) -> list[str]:
    command = ["ffmpeg", "-hide_banner", "-nostats", "-v", "info"]
    if window["window_id"] != "full-timeline":
        command.extend(["-ss", str(window["start_seconds"]), "-t", str(round(window["end_seconds"] - window["start_seconds"], 3))])
    command.extend(
        [
            "-i",
            str(media),
            "-vf",
            "blackdetect=d={}:pix_th={},freezedetect=n={}dB:d={}".format(
                detectors["black_duration_seconds"],
                detectors["black_pixel_threshold"],
                detectors["freeze_noise_db"],
                detectors["freeze_duration_seconds"],
            ),
            "-af",
            "silencedetect=n={}dB:d={}".format(
                detectors["silence_noise_db"], detectors["silence_duration_seconds"]
            ),
            "-f",
            "null",
            "-",
        ]
    )
    return command


def _call_ffmpeg_outlet(
    executor: Callable[..., dict[str, Any]],
    *,
    command: list[str],
    media: Path,
    media_artifact: dict[str, Any],
    log_path: Path,
    receipt_path: Path,
    timeout_s: float,
    ffmpeg_runner: Callable[..., Any] | None,
    resource_snapshot_provider: Callable[..., Any] | None,
    resource_lock_path: str | Path | None,
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "capture_log_path": log_path,
        "timeout_s": timeout_s,
        "runner": ffmpeg_runner,
        "resource_snapshot_provider": resource_snapshot_provider,
        "resource_lock_path": resource_lock_path,
    }
    signature = inspect.signature(executor)
    accepts_kwargs = any(
        row.kind == inspect.Parameter.VAR_KEYWORD for row in signature.parameters.values()
    )
    if not accepts_kwargs:
        kwargs = {key: value for key, value in kwargs.items() if key in signature.parameters}
    return executor(
        "technical_qa",
        command,
        [{"role": "media", "path": media, "artifact": media_artifact}],
        [{"role": "detector_log", "path": log_path}],
        receipt_path,
        **kwargs,
    )


def _load_verified_cache(
    path: Path,
    media_sha256: str,
    profile_sha256: str,
    cache_key: str,
    scope: dict[str, Any],
) -> Path | None:
    if not path.is_file():
        return None
    try:
        receipt = read_json_object(path, "video review cache receipt")
        if (
            receipt.get("schema") != CACHE_RECEIPT_SCHEMA
            or receipt.get("status") != "verified"
            or receipt.get("execution_disposition") != "performed"
            or receipt.get("receipt_sha256") != payload_sha256(receipt, "receipt_sha256")
            or receipt.get("media_sha256") != media_sha256
            or receipt.get("check_profile_sha256") != profile_sha256
            or receipt.get("cache_key") != cache_key
            or receipt.get("scope") != scope
        ):
            return None
        report_path = validate_artifact_reference(receipt["qa_report"], "video review QA report")
        report = read_json_object(report_path, "video review QA report")
        if (
            report.get("schema") != QA_REPORT_SCHEMA
            or report.get("status") != "verified"
            or report.get("report_sha256") != payload_sha256(report, "report_sha256")
            or report.get("media_sha256") != media_sha256
            or report.get("check_profile_sha256") != profile_sha256
            or report.get("cache_key") != cache_key
            or report.get("scope") != scope
        ):
            return None
        commands = report.get("commands")
        if not isinstance(commands, list) or not commands:
            return None
        limited = scope["render_scope"] == "changed_windows_only"
        if limited and (scope["full_timeline_scanned"] or any("-ss" not in row.get("command", []) or "-t" not in row.get("command", []) for row in commands)):
            return None
        if not limited and (len(commands) != 1 or "-ss" in commands[0].get("command", []) or "-t" in commands[0].get("command", [])):
            return None
        for row in commands:
            ffmpeg_path = validate_artifact_reference(row["ffmpeg_receipt"], "video review FFmpeg receipt")
            validate_artifact_reference(row["detector_log"], "video review detector log")
            _validate_cached_ffmpeg_receipt(
                read_json_object(ffmpeg_path, "video review FFmpeg receipt"),
                media_sha256=media_sha256,
                command=row["command"],
                detector_log=row["detector_log"],
            )
        if receipt.get("ffmpeg_receipts") != [
            row["ffmpeg_receipt"] for row in commands
        ]:
            return None
        return report_path
    except (ContractError, KeyError, OSError, TypeError, ValueError):
        return None


def _write_invocation(
    output: Path,
    *,
    disposition: str,
    media_sha256: str,
    profile_sha256: str,
    cache_key: str,
    scope: dict[str, Any],
    cache_receipt_path: Path,
    report_path: Path,
    resource_guard: dict[str, Any],
) -> dict[str, Any]:
    receipt = {
        "schema": EXECUTION_RECEIPT_SCHEMA,
        "status": "completed",
        "disposition": disposition,
        "media_sha256": media_sha256,
        "check_profile_sha256": profile_sha256,
        "cache_key": cache_key,
        "scope": scope,
        "cache_receipt": artifact_reference(cache_receipt_path, "video review cache receipt"),
        "qa_report": artifact_reference(report_path, "video review QA report"),
        "automatic_retry": False,
        "resource_guard": resource_guard,
        "authority_boundary": {
            "shared_report_read_only": True,
            "heavy_command_started": disposition == "performed",
            "source_media_modified": False,
            "automatic_publish": False,
        },
    }
    receipt["receipt_sha256"] = payload_sha256(receipt, "receipt_sha256")
    atomic_write_json(receipt, output)
    return receipt


def _write_blocked_invocation(
    output: Path,
    *,
    media_sha256: str,
    profile_sha256: str,
    cache_key: str,
    scope: dict[str, Any],
    detail: dict[str, Any],
) -> dict[str, Any]:
    receipt = {
        "schema": EXECUTION_RECEIPT_SCHEMA,
        "status": "blocked",
        "disposition": "not_run",
        "reason": "resource_pressure_pause_required",
        "media_sha256": media_sha256,
        "check_profile_sha256": profile_sha256,
        "cache_key": cache_key,
        "scope": scope,
        "cache_receipt": None,
        "qa_report": None,
        "automatic_retry": False,
        "resource_guard": detail.get("resource_guard", detail),
        "authority_boundary": {
            "shared_report_read_only": True,
            "heavy_command_started": False,
            "source_media_modified": False,
            "automatic_publish": False,
        },
    }
    receipt["receipt_sha256"] = payload_sha256(receipt, "receipt_sha256")
    atomic_write_json(receipt, output)
    return receipt


def _validate_cached_ffmpeg_receipt(
    receipt: dict[str, Any],
    *,
    media_sha256: str,
    command: list[str],
    detector_log: dict[str, Any],
) -> None:
    if (
        receipt.get("schema") != FFMPEG_EXECUTION_RECEIPT_SCHEMA
        or receipt.get("receipt_sha256") != payload_sha256(receipt, "receipt_sha256")
        or receipt.get("outlet_id") != FFMPEG_OUTLET_ID
        or receipt.get("status") != "completed"
        or receipt.get("operation") != "technical_qa"
        or receipt.get("command") != command
    ):
        raise ContractError("cached video review FFmpeg receipt is invalid")
    inputs = receipt.get("inputs")
    outputs = receipt.get("outputs")
    media_rows = [
        row
        for row in inputs or []
        if isinstance(row, dict) and row.get("role") == "media"
    ]
    log_rows = [
        row
        for row in outputs or []
        if isinstance(row, dict) and row.get("role") == "detector_log"
    ]
    if (
        len(media_rows) != 1
        or len(log_rows) != 1
        or media_rows[0].get("artifact", {}).get("sha256") != media_sha256
        or log_rows[0].get("artifact") != detector_log
    ):
        raise ContractError("cached video review FFmpeg artifact binding is invalid")
    boundary = receipt.get("execution_boundary")
    if not isinstance(boundary, dict) or (
        boundary.get("orchestration_owner") != FFMPEG_OUTLET_ID
        or boundary.get("second_ffmpeg_orchestrator_created") is not False
        or boundary.get("source_media_modified") is not False
        or boundary.get("automatic_publish") is not False
    ):
        raise ContractError("cached video review FFmpeg boundary is invalid")


def _file_fingerprint(path: Path) -> dict[str, Any]:
    stat = path.stat()
    return {
        "path_identity": str(path.resolve()),
        "device": int(stat.st_dev),
        "inode": int(stat.st_ino),
        "size": int(stat.st_size),
        "mtime_ns": int(stat.st_mtime_ns),
    }


@contextmanager
def _retained_os_lock(path: Path, *, timeout_s: float) -> Iterator[None]:
    """Hold a retained lockfile with a crash-released native OS lock."""

    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + max(0.0, float(timeout_s))
    handle = path.open("a+b")
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"\0")
        handle.flush()
    acquired = False
    try:
        while not acquired:
            try:
                _try_os_lock(handle)
                acquired = True
            except OSError as exc:
                if not _lock_is_contended(exc):
                    raise
                if time.monotonic() >= deadline:
                    raise ContractError("video review cache lock timeout") from exc
                time.sleep(0.02)
        yield
    finally:
        if acquired:
            _release_os_lock(handle)
        handle.close()


def _try_os_lock(handle: Any) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        return
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _release_os_lock(handle: Any) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _lock_is_contended(exc: OSError) -> bool:
    return isinstance(exc, BlockingIOError) or exc.errno in {
        errno.EACCES,
        errno.EAGAIN,
        errno.EDEADLK,
    }


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
