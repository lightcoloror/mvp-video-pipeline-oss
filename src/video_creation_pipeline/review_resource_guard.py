from __future__ import annotations

import copy
import math
import os
import subprocess
import tempfile
import threading
import time
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

import psutil

from .errors import ContractError

REVIEW_RESOURCE_POLICY_ID = "video-review-resource-guard-v1"
RESOURCE_PRESSURE_PAUSE_REQUIRED = "resource_pressure_pause_required"

_PROCESS_LOCKS: dict[str, threading.Lock] = {}
_PROCESS_LOCKS_GUARD = threading.Lock()


@dataclass(frozen=True)
class ReviewResourcePolicy:
    policy_id: str = REVIEW_RESOURCE_POLICY_ID
    max_cpu_percent: float = 70.0
    min_available_memory_bytes: int = 8 * 1024**3
    min_available_memory_percent: float = 20.0
    max_disk_busy_percent: float = 80.0
    sample_interval_s: float = 0.1
    ffmpeg_threads: int = 2

    def __post_init__(self) -> None:
        if not self.policy_id.strip():
            raise ContractError("resource guard policy_id is required")
        if not 0 <= float(self.max_cpu_percent) <= 100:
            raise ContractError("resource guard max_cpu_percent is invalid")
        if int(self.min_available_memory_bytes) < 0:
            raise ContractError("resource guard memory threshold is invalid")
        if not 0 <= float(self.min_available_memory_percent) <= 100:
            raise ContractError("resource guard memory percent threshold is invalid")
        if not 0 <= float(self.max_disk_busy_percent) <= 100:
            raise ContractError("resource guard disk threshold is invalid")
        if not 0 < float(self.sample_interval_s) <= 1:
            raise ContractError("resource guard sample interval is invalid")
        if not 1 <= int(self.ffmpeg_threads) <= 64:
            raise ContractError("resource guard FFmpeg thread limit is invalid")

    @classmethod
    def from_environment(cls) -> ReviewResourcePolicy:
        return cls(
            max_cpu_percent=_environment_float(
                "MVP_VIDEO_REVIEW_MAX_CPU_PERCENT", cls.max_cpu_percent
            ),
            min_available_memory_bytes=_environment_int(
                "MVP_VIDEO_REVIEW_MIN_AVAILABLE_MEMORY_BYTES",
                cls.min_available_memory_bytes,
            ),
            min_available_memory_percent=_environment_float(
                "MVP_VIDEO_REVIEW_MIN_AVAILABLE_MEMORY_PERCENT",
                cls.min_available_memory_percent,
            ),
            max_disk_busy_percent=_environment_float(
                "MVP_VIDEO_REVIEW_MAX_DISK_BUSY_PERCENT",
                cls.max_disk_busy_percent,
            ),
            sample_interval_s=_environment_float(
                "MVP_VIDEO_REVIEW_SAMPLE_INTERVAL_S", cls.sample_interval_s
            ),
            ffmpeg_threads=_environment_int(
                "MVP_VIDEO_REVIEW_FFMPEG_THREADS", cls.ffmpeg_threads
            ),
        )

    def limits(self) -> dict[str, Any]:
        return {
            "max_cpu_percent": float(self.max_cpu_percent),
            "min_available_memory_bytes": int(self.min_available_memory_bytes),
            "min_available_memory_percent": float(
                self.min_available_memory_percent
            ),
            "max_disk_busy_percent": float(self.max_disk_busy_percent),
        }


class ResourcePressurePauseRequired(ContractError):
    code = RESOURCE_PRESSURE_PAUSE_REQUIRED

    def __init__(self, reason: str, resource_guard: Mapping[str, Any]) -> None:
        super().__init__(self.code)
        self.reason = str(reason)
        self.resource_guard = copy.deepcopy(dict(resource_guard))

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": "blocked",
            "code": self.code,
            "reason": self.reason,
            "automatic_retry": False,
            "resource_guard": copy.deepcopy(self.resource_guard),
        }


class ReviewResourceGuard(AbstractContextManager["ReviewResourceGuard"]):
    """Non-waiting, cross-process single-slot gate for heavy local review work."""

    def __init__(
        self,
        *,
        policy: ReviewResourcePolicy | None = None,
        lock_path: str | Path | None = None,
        snapshot_provider: Callable[[], Mapping[str, Any]] | None = None,
    ) -> None:
        self.policy = policy or ReviewResourcePolicy.from_environment()
        self.lock_path = _resolve_lock_path(lock_path)
        self.snapshot_provider = snapshot_provider
        self._process_lock: threading.Lock | None = None
        self._handle: Any = None
        self._os_lock_acquired = False
        self.metadata: dict[str, Any] = {
            "policy_id": self.policy.policy_id,
            "decision": "pending",
            "automatic_retry": False,
            "snapshot": None,
            "limits": self.policy.limits(),
            "ffmpeg": {
                "thread_limit": int(self.policy.ffmpeg_threads),
                "priority": _priority_metadata(),
            },
            "lock": {
                "scope": "cross_process_single_slot",
                "slots": 1,
                "path": str(self.lock_path),
                "mechanism": "msvcrt.locking" if os.name == "nt" else "fcntl.flock",
                "non_blocking": True,
                "acquired": False,
                "released": False,
                "lock_file_retained": True,
            },
            "cache": {
                "resource_snapshot": "uncached",
                "snapshot_reused": False,
                "ttl_s": 0,
            },
        }

    def __enter__(self) -> ReviewResourceGuard:
        self._acquire_lock()
        try:
            self.metadata["snapshot"] = self._take_snapshot()
            reason = _pressure_reason(self.metadata["snapshot"], self.policy)
            if reason is not None:
                self.metadata["decision"] = "pause_required"
                self.release()
                raise ResourcePressurePauseRequired(reason, self.metadata)
            self.metadata["decision"] = "admitted"
            return self
        except ResourcePressurePauseRequired:
            raise
        except Exception as exc:
            self.metadata["decision"] = "pause_required"
            self.release()
            raise ResourcePressurePauseRequired(
                "resource_snapshot_unavailable", self.metadata
            ) from exc

    def __exit__(self, *_exc: object) -> None:
        self.release()

    def release(self) -> None:
        if self._handle is not None:
            if self._os_lock_acquired:
                _unlock_file(self._handle)
                self._os_lock_acquired = False
            self._handle.close()
            self._handle = None
        if self._process_lock is not None:
            self._process_lock.release()
            self._process_lock = None
        if self.metadata["lock"]["acquired"]:
            self.metadata["lock"]["released"] = True

    def receipt_metadata(self) -> dict[str, Any]:
        return copy.deepcopy(self.metadata)

    def _acquire_lock(self) -> None:
        key = os.path.normcase(str(self.lock_path))
        with _PROCESS_LOCKS_GUARD:
            process_lock = _PROCESS_LOCKS.setdefault(key, threading.Lock())
        if not process_lock.acquire(blocking=False):
            self.metadata["decision"] = "pause_required"
            raise ResourcePressurePauseRequired("single_slot_busy", self.metadata)
        self._process_lock = process_lock
        try:
            self.lock_path.parent.mkdir(parents=True, exist_ok=True)
            handle = self.lock_path.open("a+b")
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            _lock_file(handle)
            self._handle = handle
            self._os_lock_acquired = True
            self.metadata["lock"]["acquired"] = True
        except (OSError, BlockingIOError) as exc:
            if "handle" in locals():
                handle.close()
            process_lock.release()
            self._process_lock = None
            self.metadata["decision"] = "pause_required"
            raise ResourcePressurePauseRequired("single_slot_busy", self.metadata) from exc

    def _take_snapshot(self) -> dict[str, Any]:
        raw = (
            self.snapshot_provider()
            if self.snapshot_provider is not None
            else capture_resource_snapshot(self.policy.sample_interval_s)
        )
        return normalize_resource_snapshot(raw)


def capture_resource_snapshot(sample_interval_s: float = 0.1) -> dict[str, Any]:
    disk_start, disk_source = _disk_busy_counter_ms()
    started = time.perf_counter()
    cpu_percent = float(psutil.cpu_percent(interval=float(sample_interval_s)))
    elapsed_ms = max((time.perf_counter() - started) * 1000.0, 0.001)
    disk_end, end_source = _disk_busy_counter_ms()
    memory = psutil.virtual_memory()
    disk_busy: float | None = None
    errors: list[str] = []
    if disk_start is None or disk_end is None or disk_source != end_source:
        errors.append("disk_busy_unavailable")
    else:
        disk_busy = min(100.0, max(0.0, (disk_end - disk_start) / elapsed_ms * 100.0))
    return {
        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        "cpu_percent": cpu_percent,
        "available_memory_bytes": int(memory.available),
        "available_memory_percent": round(
            (float(memory.available) / float(memory.total)) * 100.0, 3
        ),
        "disk_busy_percent": disk_busy,
        "disk_busy_counter": disk_source or end_source or "unavailable",
        "sample_window_ms": round(elapsed_ms, 3),
        "measurement_errors": errors,
    }


def normalize_resource_snapshot(raw: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise TypeError("resource snapshot must be a mapping")
    return {
        "captured_at_utc": str(
            raw.get("captured_at_utc") or datetime.now(timezone.utc).isoformat()
        ),
        "cpu_percent": _optional_float(raw.get("cpu_percent")),
        "available_memory_bytes": _optional_int(raw.get("available_memory_bytes")),
        "available_memory_percent": _optional_float(
            raw.get("available_memory_percent")
        ),
        "disk_busy_percent": _optional_float(raw.get("disk_busy_percent")),
        "disk_busy_counter": str(raw.get("disk_busy_counter") or "provided"),
        "sample_window_ms": _optional_float(raw.get("sample_window_ms")) or 0.0,
        "measurement_errors": [
            str(value) for value in (raw.get("measurement_errors") or [])
        ],
    }


def apply_ffmpeg_thread_limit(command: list[str], threads: int) -> list[str]:
    normalized = [str(value) for value in command]
    if not normalized or normalized[0] != "ffmpeg":
        raise ContractError("FFmpeg outlet command must start with ffmpeg")
    if type(threads) is not int or not 1 <= threads <= 64:
        raise ContractError("FFmpeg thread limit must be an integer from 1 to 64")
    limit = threads
    effective = list(normalized)
    for option in ("-threads", "-filter_threads", "-filter_complex_threads"):
        found = False
        for index, value in enumerate(effective[:-1]):
            if value.split(":", 1)[0] == option:
                found = True
                try:
                    requested = int(effective[index + 1])
                    effective[index + 1] = str(min(requested, limit) if requested > 0 else limit)
                except ValueError:
                    effective[index + 1] = str(limit)
        if not found:
            effective[1:1] = [option, str(limit)]
    return effective


def below_normal_priority_kwargs() -> dict[str, int]:
    if os.name != "nt":
        return {}
    return {
        "creationflags": int(
            getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0x00004000)
        )
    }


def _priority_metadata() -> dict[str, Any]:
    kwargs = below_normal_priority_kwargs()
    return {
        "requested": "below_normal",
        "applied": bool(kwargs),
        "creationflags": int(kwargs.get("creationflags", 0)),
        "platform": os.name,
    }


def _pressure_reason(
    snapshot: Mapping[str, Any], policy: ReviewResourcePolicy
) -> str | None:
    if snapshot.get("measurement_errors"):
        return "resource_snapshot_unavailable"
    cpu = snapshot.get("cpu_percent")
    memory = snapshot.get("available_memory_bytes")
    memory_percent = snapshot.get("available_memory_percent")
    disk = snapshot.get("disk_busy_percent")
    if cpu is None or memory is None or memory_percent is None or disk is None:
        return "resource_snapshot_unavailable"
    if any(not 0 <= float(value) <= 100 for value in (cpu, memory_percent, disk)) or int(memory) < 0:
        return "resource_snapshot_unavailable"
    if float(cpu) >= float(policy.max_cpu_percent):
        return "cpu_pressure"
    if int(memory) < int(policy.min_available_memory_bytes) or float(
        memory_percent
    ) < float(policy.min_available_memory_percent):
        return "memory_pressure"
    if float(disk) >= float(policy.max_disk_busy_percent):
        return "disk_pressure"
    return None


def _resolve_lock_path(value: str | Path | None) -> Path:
    configured = value or os.environ.get("MVP_VIDEO_REVIEW_RESOURCE_LOCK_PATH")
    if configured:
        return Path(configured).expanduser().resolve()
    return (
        Path(tempfile.gettempdir())
        / "video-creation-pipeline"
        / "single-ffmpeg-outlet.lock"
    ).resolve()


def _disk_busy_counter_ms() -> tuple[float | None, str | None]:
    counters = psutil.disk_io_counters()
    if counters is None:
        return None, None
    busy_time = getattr(counters, "busy_time", None)
    if busy_time is not None:
        return float(busy_time), "busy_time"
    read_time = getattr(counters, "read_time", None)
    write_time = getattr(counters, "write_time", None)
    if read_time is None or write_time is None:
        return None, None
    return float(read_time) + float(write_time), "read_time_plus_write_time"


def _lock_file(handle: Any) -> None:
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        return
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_file(handle: Any) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _environment_float(name: str, default: float) -> float:
    value = os.environ.get(name)
    if value is None:
        return float(default)
    try:
        return float(value)
    except ValueError as exc:
        raise ContractError(f"invalid {name}") from exc


def _environment_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None:
        return int(default)
    try:
        return int(value)
    except ValueError as exc:
        raise ContractError(f"invalid {name}") from exc


def _optional_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _optional_int(value: Any) -> int | None:
    return None if value is None or isinstance(value, bool) else int(value)
