from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import tempfile
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any
from uuid import uuid4

import psutil

from .errors import ContractError


ADAPTER_CONFIG_SCHEMA = "video_creation_pipeline.media_artifact_adapter_config.v1"
SYNTHETIC_RESTORE_SCHEMA = (
    "video_creation_pipeline.media_artifact_synthetic_restore_fixture.v1"
)
ADAPTER_ID = "mvp-video-media-artifact-lifecycle-adapter"
OWNER_LANE = "video-creation-pipeline"
PROFILE_ID = "media"
LARGE_OUTPUT_OPERATIONS = {"render", "transcode"}
DOCTOR_OPERATION_KINDS = {"lifecycle", *LARGE_OUTPUT_OPERATIONS}
DEFAULT_MAX_DEPTH = 6
DEFAULT_MAX_ITEMS = 2_000
PROTECTED_ROLES = {
    "authoritative_input",
    "final_deliverable",
    "evidence_receipt_manifest",
    "runtime_state",
    "secret_or_account_state",
}
CANDIDATE_ROLES = {
    "reproducible_derived",
    "cache_build_test_temp",
    "duplicate_or_obsolete",
}
ALLOWED_CANDIDATE_KINDS = {
    "rebuildable_render_workspace",
    "proxy",
    "failed_encode",
    "duplicate_transcode",
    "preview",
    "superseded_intermediate",
}
DEFAULT_ACTIVE_PROCESS_NAMES = {"ffmpeg", "ffmpeg.exe", "ffprobe", "ffprobe.exe"}
REQUIRED_CORE_SYMBOLS = {
    "LifecycleError",
    "artifact_path",
    "assert_no_reparse",
    "attach_hash",
    "atomic_write_json",
    "canonical_sha256",
    "directory_identity",
    "doctor",
    "load_manifest",
    "plan_operation",
    "read_json",
    "restore_from_receipt",
    "sha256_file",
    "validate_manifest",
    "validate_profiles",
    "verify_plan",
    "volume_identity",
}


def load_core(core_root: str | Path) -> ModuleType:
    root = Path(core_root).expanduser().resolve()
    module_path = root / "artifact_lifecycle_core.py"
    if not module_path.is_file():
        raise ContractError(f"artifact-lifecycle-core module not found: {module_path}")
    digest = hashlib.sha256(module_path.read_bytes()).hexdigest()
    module_name = f"artifact_lifecycle_core_{digest[:16]}"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise ContractError(f"cannot load artifact-lifecycle-core: {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    missing = sorted(
        name for name in REQUIRED_CORE_SYMBOLS if not hasattr(module, name)
    )
    if missing:
        raise ContractError(
            "artifact-lifecycle-core contract is incompatible: " + ", ".join(missing)
        )
    module.__adapter_source_sha256__ = digest
    return module


def load_adapter_config(source: str | Path) -> dict[str, Any]:
    path = Path(source).expanduser().resolve()
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(
            f"media lifecycle adapter config is unreadable: {exc}"
        ) from exc
    if not isinstance(value, dict):
        raise ContractError("media lifecycle adapter config must be a JSON object")
    errors = validate_adapter_config(value)
    if errors:
        raise ContractError(
            "media lifecycle adapter config is invalid: " + ";".join(errors)
        )
    return value


def _media_profile(core: ModuleType) -> dict[str, Any]:
    profile_path = (
        Path(core.__file__).resolve().parent
        / "profiles"
        / "artifact-lifecycle-profiles.v1.json"
    )
    try:
        payload = core.read_json(profile_path)
        errors = core.validate_profiles(payload)
    except (OSError, core.LifecycleError) as exc:
        code = getattr(exc, "code", type(exc).__name__)
        detail = getattr(exc, "detail", str(exc))
        raise ContractError(
            f"shared media profile is unreadable: {code}: {detail}"
        ) from exc
    if errors:
        raise ContractError("shared media profile is invalid: " + ";".join(errors))
    profile = next(
        (
            item
            for item in payload.get("profiles", [])
            if item.get("profile_id") == PROFILE_ID
        ),
        None,
    )
    if profile is None:
        raise ContractError("shared media profile is missing")
    if profile.get("large_output_preflight") is not True:
        raise ContractError("shared media profile must require large output preflight")
    return {
        "profile_id": PROFILE_ID,
        "minimum_free_bytes": int(profile["minimum_free_bytes"]),
        "warning_free_bytes": int(profile["warning_free_bytes"]),
        "output_headroom_multiplier": float(profile["output_headroom_multiplier"]),
        "large_output_preflight": True,
        "cleanup_mode": profile["cleanup_mode"],
        "automatic_purge": profile["automatic_purge"],
        "source_path": str(profile_path),
        "source_sha256": core.sha256_file(profile_path),
    }


def validate_adapter_config(value: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if value.get("schema_version") != ADAPTER_CONFIG_SCHEMA:
        errors.append("config_schema_version_invalid")
    if value.get("lifecycle_profile") != PROFILE_ID:
        errors.append("lifecycle_profile_must_be_media")
    if value.get("cleanup_mode") != "plan_only":
        errors.append("cleanup_mode_must_be_plan_only")
    if value.get("automatic_purge") is not False:
        errors.append("automatic_purge_must_be_false")
    roots = value.get("artifact_roots")
    if not isinstance(roots, list) or not roots:
        errors.append("artifact_roots_required")
    else:
        for raw in roots:
            try:
                relative = _relative_path(raw)
            except ContractError as exc:
                errors.append(str(exc))
                continue
            if relative == Path("."):
                errors.append("workspace_root_scan_forbidden")
    authoritative = value.get("authoritative_paths")
    if not isinstance(authoritative, list):
        errors.append("authoritative_paths_array_required")
    rules = value.get("rules")
    if not isinstance(rules, list) or not rules:
        errors.append("classification_rules_required")
    else:
        for index, rule in enumerate(rules):
            if not isinstance(rule, dict):
                errors.append(f"rule_object_required:{index}")
                continue
            try:
                _relative_path(rule.get("path_prefix"))
            except ContractError as exc:
                errors.append(f"rule_path_invalid:{index}:{exc}")
            if not rule.get("artifact_role"):
                errors.append(f"rule_artifact_role_required:{index}")
            if not rule.get("lifecycle_state"):
                errors.append(f"rule_lifecycle_state_required:{index}")
    max_depth = value.get("max_depth", DEFAULT_MAX_DEPTH)
    max_items = value.get("max_items", DEFAULT_MAX_ITEMS)
    if not isinstance(max_depth, int) or not 0 <= max_depth <= 64:
        errors.append("max_depth_invalid")
    if not isinstance(max_items, int) or not 1 <= max_items <= 100_000:
        errors.append("max_items_invalid")
    if not str(value.get("closure_run_id") or "").strip():
        errors.append("closure_run_id_required")
    if value.get("producer_tool_id") != ADAPTER_ID:
        errors.append("producer_tool_id_invalid")
    if value.get("owner_lane") != OWNER_LANE:
        errors.append("owner_lane_invalid")
    return sorted(set(errors))


def _relative_path(raw: Any) -> Path:
    if not isinstance(raw, str) or not raw.strip():
        raise ContractError("relative path is required")
    path = Path(raw.replace("/", os.sep))
    if path.is_absolute() or ".." in path.parts:
        raise ContractError(f"path must be repository-relative: {raw}")
    normalized = Path(os.path.normpath(str(path)))
    if normalized == Path(".") and raw.strip() not in {".", "./", ".\\"}:
        raise ContractError(f"path normalization drifted: {raw}")
    return normalized


def _resolve_scoped_path(workspace_root: Path, raw: Any, *, must_exist: bool) -> Path:
    relative = _relative_path(raw)
    target = workspace_root / relative
    if must_exist and not target.exists():
        raise ContractError(f"scoped path does not exist: {raw}")
    try:
        target.resolve(strict=must_exist).relative_to(
            workspace_root.resolve(strict=True)
        )
    except ValueError as exc:
        raise ContractError(f"path escapes workspace root: {raw}") from exc
    return target


def _resolve_directory_no_reparse(core: ModuleType, raw: str | Path) -> Path:
    unresolved = Path(raw).expanduser().absolute()
    try:
        core.assert_no_reparse(unresolved, unresolved)
    except core.LifecycleError as exc:
        raise ContractError(f"{exc.code}: {exc.detail}") from exc
    resolved = unresolved.resolve(strict=True)
    if not resolved.is_dir():
        raise ContractError(f"directory is required: {resolved}")
    return resolved


def _rule_for(relative: str, rules: list[dict[str, Any]]) -> dict[str, Any]:
    matches: list[tuple[int, dict[str, Any]]] = []
    relative_path = Path(relative)
    for rule in rules:
        prefix = _relative_path(rule["path_prefix"])
        try:
            relative_path.relative_to(prefix)
        except ValueError:
            continue
        matches.append((len(prefix.parts), rule))
    if not matches:
        raise ContractError(f"unclassified artifact path: {relative}")
    matches.sort(key=lambda item: item[0], reverse=True)
    if len(matches) > 1 and matches[0][0] == matches[1][0]:
        raise ContractError(f"ambiguous artifact classification: {relative}")
    return matches[0][1]


def _iter_bounded_files(
    workspace_root: Path,
    roots: Iterable[str],
    *,
    max_depth: int,
    max_items: int,
    core: ModuleType,
) -> Iterable[Path]:
    node_count = 0
    seen: set[Path] = set()
    for raw_root in roots:
        root = _resolve_scoped_path(workspace_root, raw_root, must_exist=True)
        if root == workspace_root:
            raise ContractError("unbounded workspace scan is forbidden")
        if not root.is_dir():
            raise ContractError(f"artifact root must be a directory: {raw_root}")
        try:
            core.assert_no_reparse(root, workspace_root)
        except core.LifecycleError as exc:
            raise ContractError(f"{exc.code}: {exc.detail}") from exc
        stack: list[tuple[Path, int]] = [(root, 0)]
        while stack:
            directory, depth = stack.pop()
            try:
                core.assert_no_reparse(directory, workspace_root)
                entries = []
                with os.scandir(directory) as iterator:
                    for entry in iterator:
                        node_count += 1
                        if node_count > max_items:
                            raise ContractError(
                                f"scan item limit exceeded: {max_items}"
                            )
                        entries.append(entry)
                entries.sort(key=lambda entry: entry.name.casefold())
            except (OSError, core.LifecycleError) as exc:
                raise ContractError(
                    f"bounded scan failed closed at {directory}: {exc}"
                ) from exc
            child_directories: list[Path] = []
            for entry in entries:
                path = Path(entry.path)
                try:
                    core.assert_no_reparse(path, workspace_root)
                except core.LifecycleError as exc:
                    raise ContractError(f"{exc.code}: {exc.detail}") from exc
                if entry.is_dir(follow_symlinks=False):
                    if depth >= max_depth:
                        raise ContractError(
                            f"scan depth limit exceeded at {path}: {max_depth}"
                        )
                    child_directories.append(path)
                    continue
                if not entry.is_file(follow_symlinks=False):
                    raise ContractError(f"unsupported filesystem entry: {path}")
                if path in seen:
                    continue
                seen.add(path)
                yield path
            for child in reversed(child_directories):
                stack.append((child, depth + 1))


def _read_checkpoint(path: Path | None) -> dict[str, dict[str, Any]]:
    if path is None or not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"scan checkpoint is unreadable: {exc}") from exc
    if (
        value.get("schema_version")
        != "video_creation_pipeline.media_scan_checkpoint.v1"
    ):
        raise ContractError("scan checkpoint schema is incompatible")
    records = value.get("records")
    if not isinstance(records, dict):
        raise ContractError("scan checkpoint records are invalid")
    return records


def _checkpoint_record(info: os.stat_result, digest: str) -> dict[str, Any]:
    return {
        "bytes": int(info.st_size),
        "mtime_ns": int(info.st_mtime_ns),
        "volume_id": int(info.st_dev),
        "file_id": int(info.st_ino),
        "hardlink_count": int(info.st_nlink),
        "sha256": digest,
    }


def _digest_with_checkpoint(
    path: Path,
    relative: str,
    info: os.stat_result,
    checkpoint: dict[str, dict[str, Any]],
    core: ModuleType,
) -> tuple[str, bool]:
    cached = checkpoint.get(relative) or {}
    identity = _checkpoint_record(info, str(cached.get("sha256") or ""))
    if len(str(cached.get("sha256") or "")) == 64 and all(
        cached.get(key) == identity[key] for key in identity if key != "sha256"
    ):
        return str(cached["sha256"]), True
    try:
        return core.sha256_file(path), False
    except (OSError, core.LifecycleError) as exc:
        raise ContractError(f"artifact digest failed closed: {path}: {exc}") from exc


def _rebuild_proof(
    rule: dict[str, Any],
    path_to_item: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    rebuild = rule.get("rebuild")
    if not isinstance(rebuild, dict):
        return {
            "status": "unknown",
            "command": None,
            "input_hashes": [],
            "dependency_lock": None,
            "verification_ref": None,
        }
    input_hashes = []
    for raw in rebuild.get("input_paths", []):
        relative = _relative_path(raw).as_posix()
        item = path_to_item.get(relative)
        if item is None:
            raise ContractError(f"unknown rebuild input reference: {relative}")
        input_hashes.append(
            {"artifact_id": item["artifact_id"], "sha256": item["digest"]["value"]}
        )
    verification_path = _relative_path(rebuild.get("verification_path")).as_posix()
    verification = path_to_item.get(verification_path)
    if verification is None:
        raise ContractError(
            f"unknown rebuild verification reference: {verification_path}"
        )
    command = rebuild.get("command")
    dependency_lock = rebuild.get("dependency_lock")
    complete = bool(command and dependency_lock and input_hashes and verification)
    return {
        "status": "verified" if complete else "incomplete",
        "command": command,
        "input_hashes": input_hashes,
        "dependency_lock": dependency_lock,
        "verification_ref": verification["artifact_id"],
    }


def _rebuild_complete(item: dict[str, Any]) -> bool:
    proof = item.get("rebuild_proof") or {}
    return (
        proof.get("status") == "verified"
        and bool(proof.get("command"))
        and bool(proof.get("input_hashes"))
        and bool(proof.get("dependency_lock"))
        and bool(proof.get("verification_ref"))
    )


def build_media_manifest(
    core: ModuleType,
    workspace_root: str | Path,
    config: dict[str, Any],
    *,
    checkpoint_path: str | Path | None = None,
) -> dict[str, Any]:
    errors = validate_adapter_config(config)
    if errors:
        raise ContractError(
            "media lifecycle adapter config is invalid: " + ";".join(errors)
        )
    root = _resolve_directory_no_reparse(core, workspace_root)
    if not root.is_dir():
        raise ContractError(f"workspace root must be a directory: {root}")
    try:
        core.assert_no_reparse(root, root)
    except core.LifecycleError as exc:
        raise ContractError(f"{exc.code}: {exc.detail}") from exc
    checkpoint_file = (
        Path(checkpoint_path).expanduser().resolve() if checkpoint_path else None
    )
    checkpoint = _read_checkpoint(checkpoint_file)
    max_depth = int(config.get("max_depth", DEFAULT_MAX_DEPTH))
    max_items = int(config.get("max_items", DEFAULT_MAX_ITEMS))
    paths = list(
        _iter_bounded_files(
            root,
            config["artifact_roots"],
            max_depth=max_depth,
            max_items=max_items,
            core=core,
        )
    )
    for raw in config["authoritative_paths"]:
        path = _resolve_scoped_path(root, raw, must_exist=True)
        if not path.is_file():
            raise ContractError(f"authoritative path must be a file: {raw}")
        try:
            core.assert_no_reparse(path, root)
        except core.LifecycleError as exc:
            raise ContractError(f"{exc.code}: {exc.detail}") from exc
        if path not in paths:
            if len(paths) >= max_items:
                raise ContractError(f"scan item limit exceeded: {max_items}")
            paths.append(path)
    records: list[tuple[str, Path, os.stat_result, str, dict[str, Any]]] = []
    hardlinks: dict[tuple[int, int], int] = {}
    checkpoint_records: dict[str, dict[str, Any]] = {}
    for path in paths:
        relative = path.relative_to(root).as_posix()
        rule = _rule_for(relative, config["rules"])
        try:
            info = path.stat()
        except OSError as exc:
            raise ContractError(f"artifact stat failed closed: {path}: {exc}") from exc
        digest, _ = _digest_with_checkpoint(path, relative, info, checkpoint, core)
        checkpoint_records[relative] = _checkpoint_record(info, digest)
        hardlinks[(int(info.st_dev), int(info.st_ino))] = (
            hardlinks.get((int(info.st_dev), int(info.st_ino)), 0) + 1
        )
        records.append((relative, path, info, digest, rule))

    path_to_item: dict[str, dict[str, Any]] = {}
    active_runs = set(config.get("active_run_ids", []))
    authoritative = {
        _relative_path(path).as_posix() for path in config["authoritative_paths"]
    }
    for relative, _path, info, digest, rule in records:
        artifact_role = str(rule["artifact_role"])
        candidate_kind = rule.get("candidate_kind")
        pin = bool(rule.get("pin"))
        sensitivity = str(rule.get("sensitivity") or "unknown")
        closure_run_id = str(rule.get("closure_run_id") or config["closure_run_id"])
        if closure_run_id in active_runs:
            pin = True
        if sensitivity in {"customer", "personal", "sensitive", "restricted"}:
            pin = True
        if relative in authoritative:
            if artifact_role not in PROTECTED_ROLES or not pin:
                raise ContractError(
                    f"authoritative path must use a pinned protected role: {relative}"
                )
        if artifact_role in PROTECTED_ROLES and not pin:
            raise ContractError(f"protected artifact must be pinned: {relative}")
        if candidate_kind is not None and candidate_kind not in ALLOWED_CANDIDATE_KINDS:
            raise ContractError(f"unsupported cleanup candidate kind: {candidate_kind}")
        item = {
            "artifact_id": f"media-{hashlib.sha256(relative.encode('utf-8')).hexdigest()[:20]}",
            "closure_run_id": closure_run_id,
            "producer_tool_id": str(
                rule.get("producer_tool_id") or config["producer_tool_id"]
            ),
            "owner_lane": str(rule.get("owner_lane") or config["owner_lane"]),
            "path": relative,
            "artifact_role": artifact_role,
            "candidate_kind": candidate_kind,
            "digest": {"algorithm": "sha256", "value": digest, "status": "verified"},
            "bytes": int(info.st_size),
            "lifecycle_state": str(rule["lifecycle_state"]),
            "storage_state": "present",
            "depends_on": [],
            "referenced_by": [],
            "sensitivity": sensitivity,
            "rebuild_proof": {
                "status": "unknown",
                "command": None,
                "input_hashes": [],
                "dependency_lock": None,
                "verification_ref": None,
            },
            "minimum_keep_until": rule.get("minimum_keep_until"),
            "pin": pin,
            "file_identity": {
                "volume_id": int(info.st_dev),
                "file_id": int(info.st_ino),
                "hardlink_count": int(info.st_nlink),
                "observed_links": hardlinks[(int(info.st_dev), int(info.st_ino))],
            },
        }
        path_to_item[relative] = item

    for relative, item in path_to_item.items():
        rule = _rule_for(relative, config["rules"])
        depends_on_paths = [
            _relative_path(path).as_posix() for path in rule.get("depends_on", [])
        ]
        for dependency_path in depends_on_paths:
            dependency = path_to_item.get(dependency_path)
            if dependency is None:
                raise ContractError(f"unknown artifact dependency: {dependency_path}")
            item["depends_on"].append(dependency["artifact_id"])
            dependency["referenced_by"].append(item["artifact_id"])
        item["rebuild_proof"] = _rebuild_proof(rule, path_to_item)

    artifacts = list(path_to_item.values())
    scan_errors: list[dict[str, str]] = []
    for item in artifacts:
        identity = item["file_identity"]
        if identity["hardlink_count"] != identity["observed_links"]:
            scan_errors.append(
                {"path": item["path"], "error": "HARDLINK_COVERAGE_INCOMPLETE"}
            )
    newest_mtime = max((record[2].st_mtime for record in records), default=0.0)
    checked_at = (
        datetime.fromtimestamp(newest_mtime, UTC)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
    payload = {
        "schema_version": "artifact-lifecycle-manifest.v1",
        "manifest_id": f"manifest-{core.canonical_sha256([item['artifact_id'] for item in artifacts])[:20]}",
        "closure_run_id": config["closure_run_id"],
        "producer_tool_id": config["producer_tool_id"],
        "owner_lane": config["owner_lane"],
        "root_identity": core.directory_identity(root),
        "profile_id": PROFILE_ID,
        "cleanup_mode": "plan_only",
        "automatic_purge": False,
        "adapter": {
            "adapter_id": ADAPTER_ID,
            "core_source_sha256": core.__adapter_source_sha256__,
            "bounded_scan": True,
            "explicit_roots": list(config["artifact_roots"]),
            "max_depth": max_depth,
            "max_items": max_items,
            "incremental_checkpoint": checkpoint_file.name if checkpoint_file else None,
        },
        "scan": {
            "checked_at": checked_at,
            "errors": scan_errors,
            "item_count": len(artifacts),
        },
        "artifacts": artifacts,
    }
    manifest = core.attach_hash(payload, "manifest_sha256")
    core_errors = core.validate_manifest(manifest)
    if core_errors:
        raise ContractError("core rejected media manifest: " + ";".join(core_errors))
    _validate_reference_integrity(manifest)
    if checkpoint_file:
        checkpoint_payload = {
            "schema_version": "video_creation_pipeline.media_scan_checkpoint.v1",
            "workspace_identity": core.directory_identity(root),
            "records": checkpoint_records,
        }
        checkpoint_payload = core.attach_hash(checkpoint_payload, "checkpoint_sha256")
        core.atomic_write_json(checkpoint_file, checkpoint_payload)
    return manifest


def _validate_reference_integrity(manifest: dict[str, Any]) -> None:
    ids = {item["artifact_id"] for item in manifest.get("artifacts", [])}
    for item in manifest.get("artifacts", []):
        for field in ("depends_on", "referenced_by"):
            for reference in item.get(field, []):
                if reference not in ids:
                    raise ContractError(
                        f"unknown artifact reference: {item['artifact_id']}:{field}:{reference}"
                    )
        proof = item.get("rebuild_proof") or {}
        verification_ref = proof.get("verification_ref")
        if verification_ref and verification_ref not in ids:
            raise ContractError(
                f"unknown rebuild verification reference: {verification_ref}"
            )
        for reference in proof.get("input_hashes", []):
            if reference.get("artifact_id") not in ids:
                raise ContractError(
                    f"unknown rebuild input artifact: {reference.get('artifact_id')}"
                )


def process_probe(
    config: dict[str, Any],
    *,
    snapshot: list[dict[str, Any]] | None = None,
    process_iter: Callable[..., Iterable[Any]] = psutil.process_iter,
) -> dict[str, Any]:
    names = {name.casefold() for name in DEFAULT_ACTIVE_PROCESS_NAMES}
    names.update(
        str(name).casefold() for name in config.get("producer_process_names", [])
    )
    markers = [
        str(marker).casefold() for marker in config.get("producer_command_markers", [])
    ]
    try:
        observations = snapshot
        if observations is None:
            observations = []
            for process in process_iter(["pid", "name", "cmdline"]):
                info = process.info
                observations.append(
                    {
                        "pid": info.get("pid"),
                        "name": info.get("name"),
                        "cmdline": info.get("cmdline") or [],
                    }
                )
    except (OSError, psutil.Error) as exc:
        return {
            "state": "unknown",
            "reason_codes": [f"process_probe_failed:{type(exc).__name__}"],
            "matches": [],
        }
    matches = []
    current_pid = os.getpid()
    for item in observations:
        pid = item.get("pid")
        if pid == current_pid:
            continue
        name = str(item.get("name") or "").casefold()
        command = " ".join(str(part) for part in item.get("cmdline") or []).casefold()
        if name in names or any(marker in command for marker in markers):
            matches.append({"pid": pid, "name": item.get("name"), "cmdline": command})
    return {
        "state": "active_detected" if matches else "idle_confirmed",
        "reason_codes": ["active_media_process"] if matches else [],
        "matches": matches,
    }


def build_doctor(
    core: ModuleType,
    workspace_root: str | Path,
    config: dict[str, Any],
    *,
    process_snapshot: list[dict[str, Any]] | None = None,
    operation_kind: str = "lifecycle",
    estimated_output_bytes: int | None = None,
) -> dict[str, Any]:
    errors = validate_adapter_config(config)
    if errors:
        raise ContractError(
            "media lifecycle adapter config is invalid: " + ";".join(errors)
        )
    if operation_kind not in DOCTOR_OPERATION_KINDS:
        raise ContractError(f"unsupported doctor operation kind: {operation_kind}")
    if estimated_output_bytes is not None and (
        isinstance(estimated_output_bytes, bool)
        or not isinstance(estimated_output_bytes, int)
        or estimated_output_bytes < 0
    ):
        raise ContractError("estimated output bytes must be a non-negative integer")
    profile = _media_profile(core)
    root = _resolve_directory_no_reparse(core, workspace_root)
    large_output_requested = operation_kind in LARGE_OUTPUT_OPERATIONS
    estimate_missing = large_output_requested and not estimated_output_bytes
    try:
        core_report = core.doctor(
            Path(core.__file__).resolve().parent,
            root,
            profile_id=PROFILE_ID,
            estimated_output_bytes=estimated_output_bytes or 0,
            minimum_free_bytes=profile["minimum_free_bytes"],
            warning_free_bytes=profile["warning_free_bytes"],
            output_headroom_multiplier=profile["output_headroom_multiplier"],
            minimum_free_ratio=None,
        )
    except core.LifecycleError as exc:
        raise ContractError(f"{exc.code}: {exc.detail}") from exc
    process = process_probe(config, snapshot=process_snapshot)
    reason_codes = list(core_report.get("reason_codes", []))
    blocking_reason_codes: list[str] = []
    if core_report.get("status") != "ready":
        blocking_reason_codes.extend(
            code
            for code in core_report.get("reason_codes", [])
            if code != "low_disk_allowed"
        )
    if estimate_missing:
        reason_codes.append("blocked_output_estimate_required")
        blocking_reason_codes.append("blocked_output_estimate_required")
    if process["state"] != "idle_confirmed":
        reason_codes.extend(process["reason_codes"] or ["process_state_unknown"])
        blocking_reason_codes.extend(
            process["reason_codes"] or ["process_state_unknown"]
        )
    status = "ready" if not blocking_reason_codes else "blocked"
    disk = core_report.get("disk") or {}
    if not large_output_requested:
        preflight_status = "not_requested"
    elif status == "blocked":
        preflight_status = "blocked"
    elif disk.get("status") == "low_disk_allowed":
        preflight_status = "warning_allowed"
    else:
        preflight_status = "allowed"
    return {
        "schema_version": "video_creation_pipeline.media_artifact_lifecycle_doctor.v2",
        "status": status,
        "profile_id": PROFILE_ID,
        "cleanup_mode": "plan_only",
        "automatic_purge": False,
        "interfaces": [
            "doctor",
            "scan",
            "explain",
            "plan_gc",
            "verify_plan",
            "restore",
        ],
        "core": {
            "path": str(Path(core.__file__).resolve()),
            "source_sha256": core.__adapter_source_sha256__,
            "contract_ready": core_report.get("contract_ready"),
        },
        "core_profile": profile,
        "disk": disk,
        "large_output_preflight": {
            "operation_kind": operation_kind,
            "status": preflight_status,
            "estimate_source": (
                "caller_declared"
                if estimated_output_bytes is not None
                else "not_declared"
            ),
            "estimated_output_bytes": estimated_output_bytes,
            "required_free_bytes_formula": (
                "minimum_free_bytes + ceil(estimated_output_bytes * "
                "output_headroom_multiplier)"
            ),
            "reason_codes": sorted(set(blocking_reason_codes)),
        },
        "large_render_or_transcode_allowed": (
            large_output_requested and status == "ready"
        ),
        "process_probe": process,
        "reason_codes": sorted(set(reason_codes)),
        "bounded_scan": {
            "explicit_roots_only": True,
            "max_depth": int(config.get("max_depth", DEFAULT_MAX_DEPTH)),
            "max_items": int(config.get("max_items", DEFAULT_MAX_ITEMS)),
            "workspace_recursive_scan": False,
        },
        "filesystem_mutation_performed": False,
    }


def _strict_blockers(item: dict[str, Any]) -> set[str]:
    blockers: set[str] = set()
    if item.get("artifact_role") in CANDIDATE_ROLES:
        if item.get("candidate_kind") not in ALLOWED_CANDIDATE_KINDS:
            blockers.add("candidate_kind_not_allowlisted")
        if not _rebuild_complete(item):
            blockers.add("rebuild_proof_incomplete")
    if item.get("sensitivity") in {"customer", "personal", "sensitive", "restricted"}:
        blockers.add("sensitive_media_protected")
    return blockers


def _verify_manifest_files(
    core: ModuleType, manifest: dict[str, Any], root: Path
) -> list[str]:
    errors: list[str] = []
    for item in manifest.get("artifacts", []):
        artifact_id = item.get("artifact_id")
        try:
            path = core.artifact_path(root, item)
            if not path.is_file():
                errors.append(f"artifact_missing:{artifact_id}")
                continue
            info = path.stat()
            if core.sha256_file(path) != item["digest"]["value"]:
                errors.append(f"artifact_digest_drift:{artifact_id}")
            identity = item.get("file_identity") or {}
            if identity.get("volume_id") not in {None, int(info.st_dev)}:
                errors.append(f"artifact_volume_drift:{artifact_id}")
            if identity.get("file_id") not in {None, int(info.st_ino)}:
                errors.append(f"artifact_file_identity_drift:{artifact_id}")
            if int(identity.get("hardlink_count") or 1) != int(info.st_nlink):
                errors.append(f"artifact_hardlink_drift:{artifact_id}")
            if int(identity.get("hardlink_count") or 1) != int(
                identity.get("observed_links") or 0
            ):
                errors.append(f"hardlink_coverage_incomplete:{artifact_id}")
        except (OSError, core.LifecycleError, KeyError, TypeError, ValueError) as exc:
            errors.append(f"artifact_verification_failed:{artifact_id}:{exc}")
    return sorted(set(errors))


def explain_artifact(
    core: ModuleType, manifest_path: str | Path, artifact_id: str
) -> dict[str, Any]:
    try:
        manifest = core.load_manifest(Path(manifest_path).expanduser().resolve())
    except core.LifecycleError as exc:
        raise ContractError(f"{exc.code}: {exc.detail}") from exc
    _validate_reference_integrity(manifest)
    item = next(
        (
            candidate
            for candidate in manifest["artifacts"]
            if candidate["artifact_id"] == artifact_id
        ),
        None,
    )
    if item is None:
        raise ContractError(f"artifact not found: {artifact_id}")
    blockers = _strict_blockers(item)
    if item.get("pin"):
        blockers.add("artifact_pinned")
    if item.get("artifact_role") in PROTECTED_ROLES:
        blockers.add(f"protected_role:{item['artifact_role']}")
    if item.get("referenced_by"):
        blockers.add("authoritative_reference_present")
    return {
        "status": "ok",
        "artifact": item,
        "blocking_gates": sorted(blockers),
        "authorizes_operation": False,
    }


def plan_gc(
    core: ModuleType,
    manifest_path: str | Path,
    workspace_root: str | Path,
    quarantine_root: str | Path,
    config: dict[str, Any],
    *,
    owner_state: str,
    dispatch_state: str,
    process_snapshot: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    config_errors = validate_adapter_config(config)
    if config_errors:
        raise ContractError(
            "media lifecycle adapter config is invalid: " + ";".join(config_errors)
        )
    root = _resolve_directory_no_reparse(core, workspace_root)
    quarantine = _resolve_directory_no_reparse(core, quarantine_root)
    try:
        manifest = core.load_manifest(Path(manifest_path).expanduser().resolve())
    except core.LifecycleError as exc:
        raise ContractError(f"{exc.code}: {exc.detail}") from exc
    _validate_reference_integrity(manifest)
    if manifest.get("profile_id") != PROFILE_ID:
        raise ContractError("manifest must use the media profile")
    if (
        manifest.get("cleanup_mode") != "plan_only"
        or manifest.get("automatic_purge") is not False
    ):
        raise ContractError("manifest cleanup policy is unsafe")
    if manifest.get("compatibility_source"):
        raise ContractError(
            "self-media v2 compatibility view must be rescanned before plan-gc"
        )
    if manifest.get("root_identity") != core.directory_identity(root):
        raise ContractError("manifest root identity drifted")
    file_errors = _verify_manifest_files(core, manifest, root)
    if file_errors:
        raise ContractError(
            "manifest files failed verification: " + ";".join(file_errors)
        )
    process = process_probe(config, snapshot=process_snapshot)
    try:
        plan = core.plan_operation(
            manifest,
            root,
            quarantine,
            "quarantine",
            owner_state=owner_state,
            process_state=process["state"],
            dispatch_state=dispatch_state,
        )
    except core.LifecycleError as exc:
        raise ContractError(f"{exc.code}: {exc.detail}") from exc
    manifest_index = {item["artifact_id"]: item for item in manifest["artifacts"]}
    for entry in plan["entries"]:
        item = manifest_index[entry["artifact_id"]]
        blockers = set(entry.get("blocking_gates", []))
        blockers.update(_strict_blockers(item))
        entry["blocking_gates"] = sorted(blockers)
        entry["eligible"] = not blockers
    plan["adapter_policy"] = {
        "adapter_id": ADAPTER_ID,
        "candidate_kinds": sorted(ALLOWED_CANDIDATE_KINDS),
        "rebuild_proof_required_for_all_candidates": True,
        "process_probe": process,
        "real_mutation_authorized": False,
    }
    plan["boundary"] = (
        "Immutable scan/explain/plan/verify metadata only; the MVP adapter exposes no "
        "quarantine or purge executor."
    )
    return core.attach_hash(plan, "plan_sha256")


def verify_gc_plan(
    core: ModuleType,
    plan_path: str | Path,
    manifest_path: str | Path,
    workspace_root: str | Path,
    quarantine_root: str | Path,
) -> dict[str, Any]:
    plan = core.read_json(Path(plan_path).expanduser().resolve())
    try:
        manifest = core.load_manifest(Path(manifest_path).expanduser().resolve())
        root = _resolve_directory_no_reparse(core, workspace_root)
        quarantine = _resolve_directory_no_reparse(core, quarantine_root)
        result = core.verify_plan(
            plan,
            manifest,
            root,
            quarantine,
        )
    except core.LifecycleError as exc:
        raise ContractError(f"{exc.code}: {exc.detail}") from exc
    errors = list(result.get("errors", []))
    if not manifest.get("compatibility_source"):
        if manifest.get("root_identity") != core.directory_identity(root):
            errors.append("manifest_root_identity_drift")
        errors.extend(_verify_manifest_files(core, manifest, root))
    result["errors"] = sorted(set(errors))
    result["ok"] = not result["errors"]
    result["authorization_state"] = "not_authorized"
    result["filesystem_mutation_performed"] = False
    return result


def validate_synthetic_restore_fixture(
    core: ModuleType, fixture_path: str | Path
) -> dict[str, Any]:
    fixture = json.loads(Path(fixture_path).read_text(encoding="utf-8-sig"))
    if fixture.get("schema_version") != SYNTHETIC_RESTORE_SCHEMA:
        raise ContractError("restore accepts only the synthetic restore fixture schema")
    if fixture.get("synthetic_fixture") is not True:
        raise ContractError("restore fixture must declare synthetic_fixture=true")
    relative = _relative_path(fixture.get("relative_path"))
    content = fixture.get("content")
    if not isinstance(content, str):
        raise ContractError("synthetic restore fixture content must be text")
    temp_root_value = os.environ.get("VIDEO_ARTIFACT_LIFECYCLE_TEMP_ROOT")
    temp_root = None
    if temp_root_value:
        temp_root = Path(temp_root_value).expanduser().resolve(strict=True)
        if not temp_root.is_dir():
            raise ContractError("synthetic restore temp root must be a directory")
    parent = temp_root or Path(tempfile.gettempdir()).resolve()
    base = parent / f"mvp-media-lifecycle-restore-{uuid4().hex}"
    base.mkdir(parents=False, exist_ok=False)
    try:
        root = base / "workspace"
        quarantine = base / "quarantine"
        root.mkdir()
        quarantine.mkdir()
        (root / ".synthetic-artifact-lifecycle-fixture").write_text(
            "synthetic-only\n", encoding="utf-8"
        )
        (quarantine / ".synthetic-artifact-lifecycle-fixture").write_text(
            "synthetic-only\n", encoding="utf-8"
        )
        target = root / relative
        source = quarantine / "synthetic-manifest" / "synthetic-artifact" / target.name
        source.parent.mkdir(parents=True)
        source.write_text(content, encoding="utf-8")
        digest = core.sha256_file(source)
        plan = core.attach_hash(
            {
                "schema_version": "artifact-lifecycle-operation-plan.v1",
                "plan_id": "synthetic-restore-plan",
                "operation": "quarantine",
                "cleanup_mode": "plan_only",
                "automatic_purge": False,
                "created_at": "1970-01-01T00:00:00Z",
                "manifest_sha256": "0" * 64,
                "closure_run_id": "synthetic-restore",
                "root_identity": core.directory_identity(root),
                "quarantine_root_identity": core.directory_identity(quarantine),
                "gate_evidence": {},
                "entries": [],
                "authorization_state": "not_authorized",
                "boundary": "synthetic fixture only",
            },
            "plan_sha256",
        )
        receipt = core.attach_hash(
            {
                "schema_version": "artifact-lifecycle-operation-receipt.v1",
                "operation": "quarantine",
                "status": "completed",
                "plan_sha256": plan["plan_sha256"],
                "manifest_sha256": "0" * 64,
                "closure_run_id": "synthetic-restore",
                "completed_at": "1970-01-01T00:00:00Z",
                "items": [
                    {
                        "artifact_id": "synthetic-artifact",
                        "from": str(target),
                        "to": str(source),
                        "digest": {"algorithm": "sha256", "value": digest},
                    }
                ],
                "filesystem_mutation_scope": "synthetic_fixture_only",
            },
            "receipt_sha256",
        )
        output = base / "restore-receipt.json"
        try:
            restored = core.restore_from_receipt(
                receipt, plan, root, quarantine, output
            )
        except core.LifecycleError as exc:
            raise ContractError(f"{exc.code}: {exc.detail}") from exc
        if (
            not target.is_file()
            or core.sha256_file(target) != digest
            or source.exists()
        ):
            raise ContractError("synthetic restore verification failed")
        return {
            "schema_version": "video_creation_pipeline.media_artifact_restore_validation.v1",
            "status": "passed",
            "core_receipt_sha256": restored["receipt_sha256"],
            "restored_sha256": digest,
            "synthetic_fixture_mutation_performed": True,
            "real_media_mutation_performed": False,
            "temporary_fixture_removed_on_return": True,
        }
    finally:
        shutil.rmtree(base, ignore_errors=False)
