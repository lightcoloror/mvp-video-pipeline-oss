from __future__ import annotations

import json
import platform
import shutil
import subprocess
from pathlib import Path
from typing import Any

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
from .semantic_motion import (
    HYPERFRAMES_SOURCE_COMMIT,
    HYPERFRAMES_VERSION,
    validate_semantic_motion_plan,
)

RENDER_ASSURANCE_PLAN_SCHEMA = (
    "video_creation_pipeline.hyperframes_render_assurance_plan.v1"
)
RENDER_PREFLIGHT_RECEIPT_SCHEMA = (
    "video_creation_pipeline.hyperframes_render_preflight_receipt.v1"
)
RENDERKIT_REPO_URL = "https://github.com/xiaotianfotos/HyperFrames-RenderKit"
RENDERKIT_SOURCE_COMMIT = "3bec38942ad1b4040c96968771afe00bf9fd8802"
RENDERKIT_DELIVERY_SCRIPT = Path(
    "skills/hyperframes-render-optimization/scripts/delivery.mjs"
)
RENDERKIT_MOTION_CONTRACT_SCRIPT = Path(
    "skills/hyperframes-render-optimization/scripts/motion_contract.mjs"
)
OFFICIAL_ROUTE = "official_hyperframes_faithful"
ACCELERATED_ROUTE = "renderkit_linux_intel_h264_handoff"
ALLOWED_ROUTES = {OFFICIAL_ROUTE, ACCELERATED_ROUTE}


def inspect_renderkit_source(
    source_root: str | Path,
    *,
    runtime_path: str | Path | None = None,
) -> dict[str, Any]:
    root = Path(source_root).expanduser().resolve()
    if not root.is_dir():
        raise ContractError(f"HyperFrames RenderKit source not found: {root}")
    commit = _git_head(root)
    if commit != RENDERKIT_SOURCE_COMMIT:
        raise ContractError("HyperFrames RenderKit source commit drifted")

    delivery = root / RENDERKIT_DELIVERY_SCRIPT
    motion_contract = root / RENDERKIT_MOTION_CONTRACT_SCRIPT
    version_lock = root / "VERSION.lock"
    for path, label in (
        (delivery, "RenderKit delivery scanner"),
        (motion_contract, "RenderKit motion contract"),
        (version_lock, "RenderKit version lock"),
    ):
        if not path.is_file():
            raise ContractError(f"{label} was not found in the reviewed source")

    system = platform.system().lower()
    machine = platform.machine().lower()
    host_supported = system == "linux" and machine in {"x86_64", "amd64"}
    runtime_reference = None
    if runtime_path is not None:
        runtime = Path(runtime_path).expanduser().resolve()
        if not runtime.is_file():
            raise ContractError(f"configured RenderKit runtime not found: {runtime}")
        runtime_reference = artifact_reference(runtime, "RenderKit custom runtime")

    return {
        "status": "reviewed_source_available",
        "repo_url": RENDERKIT_REPO_URL,
        "source_root": str(root),
        "source_commit": commit,
        "project_license_status": "not_granted_at_reviewed_commit",
        "scripts": {
            "delivery": artifact_reference(delivery, "RenderKit delivery scanner"),
            "motion_contract": artifact_reference(
                motion_contract, "RenderKit motion contract"
            ),
            "version_lock": artifact_reference(version_lock, "RenderKit version lock"),
        },
        "host": {
            "system": system,
            "machine": machine,
            "matches_upstream_acceleration_scope": host_supported,
        },
        "custom_runtime": runtime_reference,
        "capabilities": {
            "static_compatibility_scan": "executable",
            "official_hyperframes_assurance": "executable",
            "accelerated_render": (
                "handoff_only_external_execution_not_implemented"
                if host_supported and runtime_reference is not None
                else "blocked_requires_linux_x86_64_intel_vaapi_and_bound_runtime"
            ),
            "alpha_or_prores4444": "blocked_not_implemented_upstream",
        },
    }


def build_render_assurance_plan(
    semantic_motion_plan_path: str | Path,
    project_dir: str | Path,
    renderkit_source_root: str | Path,
    *,
    selected_route: str = OFFICIAL_ROUTE,
    runtime_path: str | Path | None = None,
) -> dict[str, Any]:
    if selected_route not in ALLOWED_ROUTES:
        raise ContractError(f"unsupported HyperFrames render route: {selected_route}")
    semantic_path = Path(semantic_motion_plan_path).expanduser().resolve()
    semantic_plan = read_json_object(semantic_path, "semantic motion plan")
    validate_semantic_motion_plan(semantic_plan)
    project = Path(project_dir).expanduser().resolve()
    project_artifacts = _validate_managed_project(project, semantic_plan)
    source = inspect_renderkit_source(
        renderkit_source_root,
        runtime_path=runtime_path,
    )

    plan: dict[str, Any] = {
        "schema": RENDER_ASSURANCE_PLAN_SCHEMA,
        "source_semantic_motion_plan": artifact_reference(
            semantic_path, "semantic motion plan"
        ),
        "managed_project": {
            "root": str(project),
            **project_artifacts,
        },
        "renderkit_source": {
            "repo_url": RENDERKIT_REPO_URL,
            "source_root": source["source_root"],
            "source_commit": source["source_commit"],
            "project_license_status": source["project_license_status"],
            "delivery_script": source["scripts"]["delivery"],
            "motion_contract_script": source["scripts"]["motion_contract"],
            "version_lock": source["scripts"]["version_lock"],
            "custom_runtime": source["custom_runtime"],
        },
        "selected_route": selected_route,
        "route_policy": {
            "automatic_fallback_allowed": False,
            "static_scan_role": (
                "evidence_only"
                if selected_route == OFFICIAL_ROUTE
                else "accelerated_route_hard_gate"
            ),
            "official_renderer": {
                "version": HYPERFRAMES_VERSION,
                "source_commit": HYPERFRAMES_SOURCE_COMMIT,
            },
            "accelerated_scope": {
                "platform": "linux_x86_64",
                "encoder": "intel_vaapi_h264",
                "output_alpha_supported": False,
                "current_host_matches": source["host"][
                    "matches_upstream_acceleration_scope"
                ],
                "custom_runtime_bound": source["custom_runtime"] is not None,
            },
        },
        "motion_identity": {
            "mechanism": "semantic_plan_and_managed_project_artifact_hashes",
            "upstream_motion_contract": "not_used_for_native_waapi_authoring",
            "reason": (
                "reviewed motion_contract.mjs counts GSAP timeline calls but the "
                "managed project uses deterministic native WAAPI"
            ),
        },
        "acceptance_policy": {
            "representative_dynamic_sample_required_for_acceleration": True,
            "consecutive_frame_motion_oracle_required_for_acceleration": True,
            "deterministic_double_run_required_for_acceleration": True,
            "exact_frame_and_audio_contract_required": True,
            "color_contract_required": True,
            "bounded_resource_budget_required": True,
            "atomic_output_promotion_required": True,
            "command_success_alone_is_sufficient": False,
        },
        "authority_boundary": {
            "candidate_only": True,
            "render_execution_performed": False,
            "external_provider_called": False,
            "upload_performed": False,
            "publish_performed": False,
            "automatic_fallback_performed": False,
            "second_ffmpeg_outlet_created": False,
        },
    }
    plan["plan_sha256"] = payload_sha256(plan, "plan_sha256")
    validate_render_assurance_plan(plan)
    return plan


def write_render_assurance_plan(plan: dict[str, Any], output_path: str | Path) -> Path:
    validate_render_assurance_plan(plan)
    return atomic_write_json(plan, output_path)


def validate_render_assurance_plan(plan: dict[str, Any]) -> None:
    _validate_schema(plan, "hyperframes_render_assurance_plan.v1.schema.json")
    if plan.get("plan_sha256") != payload_sha256(plan, "plan_sha256"):
        raise ContractError("HyperFrames render assurance plan integrity check failed")
    semantic_path = validate_artifact_reference(
        _object(plan.get("source_semantic_motion_plan")), "semantic motion plan"
    )
    semantic_plan = read_json_object(semantic_path, "semantic motion plan")
    validate_semantic_motion_plan(semantic_plan)
    managed = _object(plan.get("managed_project"))
    project = Path(str(managed.get("root") or "")).expanduser().resolve()
    expected = _validate_managed_project(project, semantic_plan)
    for field in ("owner", "html", "meta"):
        if managed.get(field) != expected[field]:
            raise ContractError(f"managed HyperFrames project {field} binding drifted")

    source = _object(plan.get("renderkit_source"))
    if source.get("repo_url") != RENDERKIT_REPO_URL:
        raise ContractError("HyperFrames RenderKit repository identity drifted")
    if source.get("source_commit") != RENDERKIT_SOURCE_COMMIT:
        raise ContractError("HyperFrames RenderKit commit identity drifted")
    inspected = inspect_renderkit_source(
        str(source.get("source_root") or ""),
        runtime_path=(
            _object(source["custom_runtime"])["path"]
            if source.get("custom_runtime") is not None
            else None
        ),
    )
    for field, inspected_key in (
        ("delivery_script", "delivery"),
        ("motion_contract_script", "motion_contract"),
        ("version_lock", "version_lock"),
    ):
        if source.get(field) != inspected["scripts"][inspected_key]:
            raise ContractError(f"HyperFrames RenderKit {field} binding drifted")
    if source.get("custom_runtime") != inspected["custom_runtime"]:
        raise ContractError("HyperFrames RenderKit runtime binding drifted")
    if source.get("project_license_status") != "not_granted_at_reviewed_commit":
        raise ContractError("reviewed RenderKit license boundary drifted")
    route = str(plan.get("selected_route") or "")
    policy = _object(plan.get("route_policy"))
    if (
        route not in ALLOWED_ROUTES
        or policy.get("automatic_fallback_allowed") is not False
    ):
        raise ContractError("HyperFrames render route policy is invalid")
    expected_role = (
        "evidence_only" if route == OFFICIAL_ROUTE else "accelerated_route_hard_gate"
    )
    if policy.get("static_scan_role") != expected_role:
        raise ContractError("HyperFrames static scan authority drifted")


def run_render_assurance_preflight(
    assurance_plan_path: str | Path,
    output_path: str | Path,
    *,
    node_path: str | Path = "node",
    timeout_s: float = 120.0,
) -> dict[str, Any]:
    plan_path = Path(assurance_plan_path).expanduser().resolve()
    plan = read_json_object(plan_path, "HyperFrames render assurance plan")
    validate_render_assurance_plan(plan)
    node = _resolve_executable(node_path, "Node.js")
    project = Path(str(_object(plan["managed_project"])["root"])).resolve()
    delivery = validate_artifact_reference(
        _object(_object(plan["renderkit_source"])["delivery_script"]),
        "RenderKit delivery scanner",
    )
    command = [
        str(node),
        str(delivery),
        "check",
        str(project),
        "--entry=index.html",
        "--json",
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=str(project),
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=timeout_s,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ContractError(
            f"RenderKit compatibility scan could not run: {exc}"
        ) from exc
    if completed.returncode not in {0, 2}:
        detail = (completed.stderr or completed.stdout).strip()
        raise ContractError(
            f"RenderKit compatibility scan failed with exit {completed.returncode}: {detail}"
        )
    try:
        scan = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ContractError(
            "RenderKit compatibility scan returned invalid JSON"
        ) from exc
    _validate_scan(scan)

    output = Path(output_path).expanduser().resolve()
    raw_scan_path = output.with_name(f"{output.stem}.compatibility-scan.json")
    atomic_write_json(scan, raw_scan_path)
    route = str(plan["selected_route"])
    accelerated = _object(_object(plan["route_policy"])["accelerated_scope"])
    accelerated_prerequisites_bound = (
        scan.get("productionEligibleForLayered") is True
        and accelerated.get("current_host_matches") is True
        and accelerated.get("custom_runtime_bound") is True
    )
    status = (
        "passed_for_official_hyperframes_faithful"
        if route == OFFICIAL_ROUTE
        else "blocked_for_renderkit_accelerated_handoff"
    )
    receipt: dict[str, Any] = {
        "schema": RENDER_PREFLIGHT_RECEIPT_SCHEMA,
        "source_assurance_plan": artifact_reference(
            plan_path, "HyperFrames render assurance plan"
        ),
        "status": status,
        "selected_route": route,
        "observed_host": {
            "system": platform.system().lower(),
            "machine": platform.machine().lower(),
        },
        "compatibility_scan": {
            "artifact": artifact_reference(
                raw_scan_path, "RenderKit compatibility scan"
            ),
            "project_scan_sha256": scan["projectScanSha256"],
            "scanned_files": int(scan["scannedFiles"]),
            "blocker_count": int(scan["blockerCount"]),
            "review_count": int(scan["reviewCount"]),
            "recommended_backend": str(scan["recommendedBackend"]),
            "production_eligible_for_layered": bool(
                scan["productionEligibleForLayered"]
            ),
            "limitation": str(scan["limitation"]),
        },
        "motion_identity": plan["motion_identity"],
        "decision": {
            "official_route_allowed": route == OFFICIAL_ROUTE,
            "accelerated_route_allowed": False,
            "reason": _decision_reason(
                route,
                scan,
                accelerated,
                accelerated_prerequisites_bound=accelerated_prerequisites_bound,
            ),
            "static_scan_is_visual_proof": False,
        },
        "authority_boundary": {
            "render_execution_performed": False,
            "automatic_fallback_performed": False,
            "external_provider_called": False,
            "upload_performed": False,
            "publish_performed": False,
        },
    }
    receipt["receipt_sha256"] = payload_sha256(receipt, "receipt_sha256")
    validate_render_preflight_receipt(receipt)
    atomic_write_json(receipt, output)
    return receipt


def validate_render_preflight_receipt(receipt: dict[str, Any]) -> None:
    _validate_schema(receipt, "hyperframes_render_preflight_receipt.v1.schema.json")
    if receipt.get("receipt_sha256") != payload_sha256(receipt, "receipt_sha256"):
        raise ContractError(
            "HyperFrames render preflight receipt integrity check failed"
        )
    plan_path = validate_artifact_reference(
        _object(receipt.get("source_assurance_plan")),
        "HyperFrames render assurance plan",
    )
    plan = read_json_object(plan_path, "HyperFrames render assurance plan")
    validate_render_assurance_plan(plan)
    if receipt.get("selected_route") != plan.get("selected_route"):
        raise ContractError("HyperFrames render preflight route drifted")
    scan_ref = _object(_object(receipt.get("compatibility_scan")).get("artifact"))
    scan_path = validate_artifact_reference(scan_ref, "RenderKit compatibility scan")
    scan = read_json_object(scan_path, "RenderKit compatibility scan")
    _validate_scan(scan)
    summary = _object(receipt.get("compatibility_scan"))
    expected = {
        "project_scan_sha256": scan["projectScanSha256"],
        "scanned_files": int(scan["scannedFiles"]),
        "blocker_count": int(scan["blockerCount"]),
        "review_count": int(scan["reviewCount"]),
        "recommended_backend": str(scan["recommendedBackend"]),
        "production_eligible_for_layered": bool(scan["productionEligibleForLayered"]),
        "limitation": str(scan["limitation"]),
    }
    if {key: summary.get(key) for key in expected} != expected:
        raise ContractError("HyperFrames render preflight scan summary drifted")
    route = str(receipt.get("selected_route") or "")
    status = str(receipt.get("status") or "")
    decision = _object(receipt.get("decision"))
    if route == OFFICIAL_ROUTE:
        if status != "passed_for_official_hyperframes_faithful":
            raise ContractError("official HyperFrames route preflight did not pass")
        if decision.get("official_route_allowed") is not True:
            raise ContractError("official HyperFrames route permission drifted")
    elif status == "blocked_for_renderkit_accelerated_handoff":
        if decision.get("accelerated_route_allowed") is not False:
            raise ContractError("blocked accelerated HyperFrames route was authorized")
    else:
        raise ContractError("HyperFrames render preflight status is invalid")


def _validate_managed_project(
    project: Path, semantic_plan: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    owner_path = project / ".semantic-motion-owner.json"
    html_path = project / "index.html"
    meta_path = project / "meta.json"
    owner = read_json_object(owner_path, "semantic motion project owner")
    meta = read_json_object(meta_path, "HyperFrames project metadata")
    if owner.get("source_plan_sha256") != semantic_plan.get("plan_sha256"):
        raise ContractError(
            "managed HyperFrames project belongs to another motion plan"
        )
    if owner.get("managed_outputs_only") is not True:
        raise ContractError("HyperFrames project is not marked as managed output")
    if (
        meta.get("renderer") != "hyperframes"
        or meta.get("rendererVersion") != HYPERFRAMES_VERSION
        or meta.get("sourceCommit") != HYPERFRAMES_SOURCE_COMMIT
        or meta.get("candidateOnly") is not True
    ):
        raise ContractError("managed HyperFrames project metadata drifted")
    if not html_path.is_file():
        raise ContractError("managed HyperFrames project index.html is missing")
    return {
        "owner": artifact_reference(owner_path, "semantic motion project owner"),
        "html": artifact_reference(html_path, "HyperFrames project HTML"),
        "meta": artifact_reference(meta_path, "HyperFrames project metadata"),
    }


def _decision_reason(
    route: str,
    scan: dict[str, Any],
    accelerated: dict[str, Any],
    *,
    accelerated_prerequisites_bound: bool,
) -> str:
    if route == OFFICIAL_ROUTE:
        return (
            "static compatibility findings are recorded as evidence; the selected "
            "official faithful renderer remains explicit and does not auto-switch"
        )
    missing = []
    if scan.get("productionEligibleForLayered") is not True:
        missing.append("static compatibility scan did not authorize layered capture")
    if accelerated.get("current_host_matches") is not True:
        missing.append("host is outside the reviewed Linux x86_64 scope")
    if accelerated.get("custom_runtime_bound") is not True:
        missing.append("no custom runtime artifact is hash-bound")
    if missing:
        return "; ".join(missing)
    if accelerated_prerequisites_bound:
        return (
            "static, host, and runtime prerequisites are bound, but this adapter does "
            "not execute the custom renderer or validate its dynamic sample, "
            "consecutive-frame oracle, deterministic pair, and final movie contract"
        )
    return "accelerated handoff remains blocked"


def _validate_scan(scan: Any) -> None:
    if not isinstance(scan, dict):
        raise ContractError("RenderKit compatibility scan must be a JSON object")
    if (
        scan.get("kind") != "hyperframes-delivery-compatibility-scan"
        or scan.get("schemaVersion") != 2
    ):
        raise ContractError("unsupported RenderKit compatibility scan contract")
    required = (
        "projectScanSha256",
        "scannedFiles",
        "blockerCount",
        "reviewCount",
        "recommendedBackend",
        "productionEligibleForLayered",
        "limitation",
        "findings",
    )
    if any(key not in scan for key in required):
        raise ContractError("RenderKit compatibility scan is incomplete")
    if not isinstance(scan.get("findings"), list):
        raise ContractError("RenderKit compatibility findings must be a list")


def _resolve_executable(value: str | Path, label: str) -> Path:
    candidate = Path(value).expanduser()
    if candidate.is_file():
        return candidate.resolve()
    found = shutil.which(str(value))
    if found is None:
        raise ContractError(f"{label} executable not found: {value}")
    return Path(found).resolve()


def _git_head(root: Path) -> str:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(root),
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=15,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise ContractError(
            f"could not inspect RenderKit source commit: {exc}"
        ) from exc
    return completed.stdout.strip().lower()


def _validate_schema(payload: dict[str, Any], file_name: str) -> None:
    path = Path(__file__).with_name("schemas") / file_name
    schema = read_json_object(path, f"{file_name} schema")
    try:
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(payload)
    except (SchemaError, ValidationError) as exc:
        raise ContractError(f"{file_name} validation failed: {exc.message}") from exc


def _object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError("expected a JSON object")
    return value
