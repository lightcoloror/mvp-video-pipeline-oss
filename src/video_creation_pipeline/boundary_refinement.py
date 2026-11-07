from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import math
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .errors import ContractError
from .provider_handoff import resolve_human_selection
from .vkp_bridge import validate_edit_job

BOUNDARY_RUN_SCHEMA = "video_creation_pipeline.boundary_refinement_run.v1"
VIDEOCUT_KIT_COMMIT = "b07990f9e57e6eeb801887fa3e36af5c8450ae68"
SNAP_SCRIPT_SHA256 = "f2d081680cd8780270227724bf411f6e87a8332582d8ac899b839b21c8523b87"


def refine_selection_boundaries(
    edit_job_path: str | Path,
    selection_path: str | Path,
    tokens_path: str | Path,
    *,
    silence_path: str | Path | None = None,
    videocut_kit_root: str | Path | None = None,
) -> dict[str, Any]:
    job_path = Path(edit_job_path).expanduser().resolve()
    source_selection_path = Path(selection_path).expanduser().resolve()
    token_file = Path(tokens_path).expanduser().resolve()
    extra_silence = Path(silence_path).expanduser().resolve() if silence_path else None
    job = _read_object(job_path, "edit job")
    selection = _read_object(source_selection_path, "human selection")
    validate_edit_job(job)
    resolved = resolve_human_selection(selection, job, _sha256(job_path))
    _validate_tokens(token_file)
    if extra_silence is not None:
        _validate_silences(extra_silence)

    if not videocut_kit_root:
        raise ContractError(
            "videocut-kit has no root license; provide an explicit external source "
            "path after reviewing its terms"
        )
    source_root = Path(videocut_kit_root).expanduser().resolve()
    script_path = _validate_videocut_kit(source_root)
    decisions = _build_boundary_probes(resolved)
    snapped = _run_snap_boundaries(
        script_path,
        decisions,
        token_file,
        extra_silence,
        source_selection_path.parent,
    )
    refined = _assemble_refined_intervals(resolved, snapped)
    source_rows = {
        str(row.get("candidate_id") or ""): row
        for row in selection.get("selections") or []
        if isinstance(row, dict)
    }
    output_rows = []
    refined_count = 0
    for item in resolved:
        candidate_id = item["candidate_id"]
        original = item["confirmed_interval"]
        interval = refined[candidate_id]
        changed = interval != original
        refined_count += int(changed)
        row = dict(source_rows[candidate_id])
        row["confirmed_interval"] = interval
        row["boundary_refinement"] = {
            "status": "completed",
            "provider": "videocut-kit",
            "algorithm": "snap_boundaries",
            "source_commit": VIDEOCUT_KIT_COMMIT,
            "script_path": str(script_path),
            "script_sha256": SNAP_SCRIPT_SHA256,
            "original_interval": original,
            "refined_interval": interval,
            "changed": changed,
            "boundary_artifact_validation": "passed",
            "tokens_path": str(token_file),
            "tokens_sha256": _sha256(token_file),
            "silence_path": str(extra_silence) if extra_silence else "",
            "silence_sha256": _sha256(extra_silence) if extra_silence else "",
            "ffmpeg_executed": False,
            "network_calls": 0,
        }
        output_rows.append(row)

    run = {
        "schema": BOUNDARY_RUN_SCHEMA,
        "created_at": _now(),
        "source_selection": {
            "path": str(source_selection_path),
            "sha256": _sha256(source_selection_path),
        },
        "source_edit_job": {
            "path": str(job_path),
            "sha256": _sha256(job_path),
        },
        "source_bundle_path": str(job.get("source_bundle", {}).get("path") or ""),
        "videocut_kit": {
            "root": str(source_root),
            "commit": VIDEOCUT_KIT_COMMIT,
            "script_path": str(script_path),
            "script_sha256": SNAP_SCRIPT_SHA256,
        },
        "item_count": len(output_rows),
        "refined_selections_sha256": _canonical_sha256(output_rows),
        "refined_count": refined_count,
        "tokens_path": str(token_file),
        "tokens_sha256": _sha256(token_file),
        "silence_path": str(extra_silence) if extra_silence else "",
        "silence_sha256": _sha256(extra_silence) if extra_silence else "",
        "artifact_validation": "passed",
        "network_calls": 0,
        "ffmpeg_executed": False,
        "automatic_publish": False,
    }
    run["run_sha256"] = _payload_sha256(run, "run_sha256")
    result = dict(selection)
    result["selections"] = output_rows
    result["boundary_refinement_run"] = run
    return result


def write_refined_selection(selection: dict[str, Any], output_path: str | Path) -> Path:
    run = (
        selection.get("boundary_refinement_run")
        if isinstance(selection.get("boundary_refinement_run"), dict)
        else {}
    )
    if (
        run.get("schema") != BOUNDARY_RUN_SCHEMA
        or run.get("artifact_validation") != "passed"
        or run.get("run_sha256") != _payload_sha256(run, "run_sha256")
        or run.get("refined_selections_sha256")
        != _canonical_sha256(selection.get("selections"))
    ):
        raise ContractError("boundary refinement run integrity check failed")
    source_path = Path(str(run.get("source_selection", {}).get("path") or "")).resolve()
    source_hash = str(run.get("source_selection", {}).get("sha256") or "")
    if not source_path.is_file() or _sha256(source_path) != source_hash:
        raise ContractError("source human selection changed after refinement")
    output = Path(output_path).expanduser().resolve()
    if output == source_path:
        raise ContractError("refined selection must not overwrite its source")
    _require_outside_bundle(output, str(run.get("source_bundle_path") or ""))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(selection, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return output


def _build_boundary_probes(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    probes = []
    for item in items:
        candidate_id = item["candidate_id"]
        interval = item["confirmed_interval"]
        start = float(interval["start"])
        end = float(interval["end"])
        probes.extend(
            [
                {
                    "id": f"{candidate_id}:before",
                    "candidate_id": candidate_id,
                    "boundary_role": "keep_start",
                    "action": "delete",
                    "start": max(0.0, start - 60.0),
                    "end": start,
                    "lock_start": True,
                },
                {
                    "id": f"{candidate_id}:after",
                    "candidate_id": candidate_id,
                    "boundary_role": "keep_end",
                    "action": "delete",
                    "start": end,
                    "end": end + 60.0,
                    "lock_end": True,
                },
            ]
        )
    return probes


def _assemble_refined_intervals(
    items: list[dict[str, Any]], snapped: list[dict[str, Any]]
) -> dict[str, dict[str, float]]:
    expected_ids = {
        f"{item['candidate_id']}:{side}"
        for item in items
        for side in ("before", "after")
    }
    rows = {str(row.get("id") or ""): row for row in snapped if isinstance(row, dict)}
    if set(rows) != expected_ids:
        raise ContractError("videocut-kit returned an unexpected boundary probe set")
    result = {}
    for item in items:
        candidate_id = item["candidate_id"]
        start = _seconds(rows[f"{candidate_id}:before"].get("end"))
        end = _seconds(rows[f"{candidate_id}:after"].get("start"))
        if end <= start:
            raise ContractError(
                f"boundary refinement collapsed candidate interval: {candidate_id}"
            )
        result[candidate_id] = {"start": start, "end": end}
    return result


def _run_snap_boundaries(
    script_path: Path,
    decisions: list[dict[str, Any]],
    tokens_path: Path,
    silence_path: Path | None,
    work_dir: Path,
) -> list[dict[str, Any]]:
    node = shutil.which("node")
    if not node:
        raise ContractError("Node.js is required to execute videocut-kit")
    with _boundary_workspace(work_dir) as (decisions_path, output_path):
        decisions_path.write_text(
            json.dumps(decisions, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        command = [
            node,
            str(script_path),
            str(decisions_path),
            str(tokens_path),
            str(output_path),
        ]
        if silence_path:
            command.extend(["--silence", str(silence_path)])
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                shell=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ContractError(f"videocut-kit execution failed: {exc}") from exc
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise ContractError(f"videocut-kit execution failed: {detail}")
        payload = _read_json(output_path, "videocut-kit output")
        if not isinstance(payload, list) or len(payload) != len(decisions):
            raise ContractError("videocut-kit output failed artifact validation")
        return payload


@contextmanager
def _boundary_workspace(work_dir: Path):
    stem = f".boundary-{uuid4().hex}"
    decisions_path = work_dir / f"{stem}.decisions.json"
    output_path = work_dir / f"{stem}.snapped.json"
    try:
        yield decisions_path, output_path
    finally:
        for path in (decisions_path, output_path):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass


def _validate_videocut_kit(root: Path) -> Path:
    script = root / "scripts" / "snap_boundaries.js"
    if not script.is_file():
        raise ContractError(f"videocut-kit snap script not found: {script}")
    if _sha256(script) != SNAP_SCRIPT_SHA256:
        raise ContractError(
            "videocut-kit snap script hash does not match the reviewed source"
        )
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ContractError(f"cannot verify videocut-kit commit: {exc}") from exc
    if completed.returncode != 0 or completed.stdout.strip() != VIDEOCUT_KIT_COMMIT:
        raise ContractError("videocut-kit commit does not match the reviewed source")
    return script


def _validate_tokens(path: Path) -> None:
    rows = _read_json(path, "transcript tokens")
    if not isinstance(rows, list) or not rows:
        raise ContractError("transcript tokens must be a non-empty JSON array")
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("isGap"), bool):
            raise ContractError("each transcript token must include boolean isGap")
        start = _seconds(row.get("start"))
        end = _seconds(row.get("end"))
        if end < start:
            raise ContractError("transcript token interval is invalid")


def _validate_silences(path: Path) -> None:
    rows = _read_json(path, "silence intervals")
    if not isinstance(rows, list):
        raise ContractError("silence intervals must be a JSON array")
    for row in rows:
        if not isinstance(row, dict):
            raise ContractError("each silence interval must be an object")
        if _seconds(row.get("end")) <= _seconds(row.get("start")):
            raise ContractError("silence interval is invalid")


def _read_object(path: Path, label: str) -> dict[str, Any]:
    payload = _read_json(path, label)
    if not isinstance(payload, dict):
        raise ContractError(f"{label} must be a JSON object")
    return payload


def _read_json(path: Path, label: str) -> Any:
    if not path.is_file():
        raise ContractError(f"{label} not found: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"cannot read {label}: {exc}") from exc


def _require_outside_bundle(output: Path, bundle_value: str) -> None:
    bundle = Path(bundle_value).resolve()
    try:
        output.relative_to(bundle)
    except ValueError:
        return
    raise ContractError("boundary artifacts must stay outside the read-only VKP Bundle")


def _seconds(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"invalid timestamp: {value!r}") from exc
    if not math.isfinite(number) or number < 0:
        raise ContractError(f"invalid timestamp: {value!r}")
    return round(number, 6)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _payload_sha256(payload: dict[str, Any], field: str) -> str:
    value = dict(payload)
    value.pop(field, None)
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
