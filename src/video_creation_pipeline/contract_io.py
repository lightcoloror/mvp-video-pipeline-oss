from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any
from uuid import uuid4

from .errors import ContractError


def canonical_json_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def payload_sha256(payload: dict[str, Any], hash_field: str) -> str:
    value = dict(payload)
    value.pop(hash_field, None)
    return canonical_json_sha256(value)


def file_sha256(path: str | Path) -> str:
    resolved = Path(path).expanduser().resolve()
    digest = hashlib.sha256()
    with resolved.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json_object(source: str | Path, label: str) -> dict[str, Any]:
    path = Path(source).expanduser().resolve()
    if not path.is_file():
        raise ContractError(f"{label} not found: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"{label} is unreadable: {exc}") from exc
    if not isinstance(payload, dict):
        raise ContractError(f"{label} must be a JSON object")
    return payload


def artifact_reference(source: str | Path, label: str) -> dict[str, Any]:
    path = Path(source).expanduser().resolve()
    if not path.is_file():
        raise ContractError(f"{label} not found: {path}")
    reference: dict[str, Any] = {
        "path": str(path),
        "bytes": path.stat().st_size,
        "sha256": file_sha256(path),
        "content_kind": "binary",
        "canonical_sha256": None,
    }
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return reference
    reference["content_kind"] = "json"
    reference["canonical_sha256"] = canonical_json_sha256(value)
    return reference


def validate_artifact_reference(reference: dict[str, Any], label: str) -> Path:
    path = Path(str(reference.get("path") or "")).expanduser().resolve()
    actual = artifact_reference(path, label)
    for field in ("bytes", "sha256", "content_kind", "canonical_sha256"):
        if reference.get(field) != actual.get(field):
            raise ContractError(f"{label} changed after the contract was created")
    return path


def atomic_write_json(payload: dict[str, Any], output_path: str | Path) -> Path:
    """Write JSON with the temp-file replace pattern used by Video Pilot."""

    path = Path(output_path).expanduser().resolve()
    if path in _bound_paths(payload):
        raise ContractError("contract output cannot overwrite a bound artifact")
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temp_path.replace(path)
    finally:
        if temp_path.exists():
            temp_path.unlink()
    return path


def atomic_write_text(
    value: str,
    output_path: str | Path,
    *,
    encoding: str = "utf-8",
) -> Path:
    path = Path(output_path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temp_path.write_text(value, encoding=encoding)
        temp_path.replace(path)
    finally:
        if temp_path.exists():
            temp_path.unlink()
    return path


def atomic_copy_promote(
    staged_path: str | Path,
    output_path: str | Path,
    *,
    label: str = "artifact",
) -> Path:
    """Copy a completed staging file and atomically replace its public output."""

    staged = Path(staged_path).expanduser().resolve()
    output = Path(output_path).expanduser().resolve()
    if staged == output:
        raise ContractError(f"{label} staging and output paths must be distinct")
    if not staged.is_file():
        raise ContractError(f"{label} staging artifact was not created")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.{uuid4().hex}.tmp")
    try:
        shutil.copyfile(staged, temporary)
        temporary.replace(output)
    except OSError as exc:
        raise ContractError(f"{label} atomic promotion failed: {exc}") from exc
    finally:
        if temporary.exists():
            temporary.unlink()
    return output


def _bound_paths(value: Any) -> set[Path]:
    paths: set[Path] = set()
    if isinstance(value, dict):
        raw_path = value.get("path")
        if isinstance(raw_path, str) and raw_path.strip():
            paths.add(Path(raw_path).expanduser().resolve())
        for child in value.values():
            paths.update(_bound_paths(child))
    elif isinstance(value, list):
        for child in value:
            paths.update(_bound_paths(child))
    return paths
