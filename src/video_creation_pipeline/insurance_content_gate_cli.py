from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from .contract_io import read_json_object
from .errors import ContractError
from .insurance_content_gate import (
    build_insurance_content_scan_request,
    default_insurance_policy_path,
    run_insurance_content_scan,
    validate_insurance_content_policy_pack,
    validate_insurance_content_scan_report,
    validate_insurance_content_scan_request,
    write_insurance_content_scan_request,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="保险视频内容候选风险检查")
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", help="检查内置策略包的时效与完整性")
    doctor.add_argument("--policy-pack", type=Path, default=None)
    doctor.add_argument("--at", default=None)

    request = sub.add_parser("build-request", help="从本地 JSON 配置构建哈希绑定请求")
    request.add_argument("--spec", type=Path, required=True)
    request.add_argument("--policy-pack", type=Path, default=None)
    request.add_argument("--output", type=Path, required=True)

    scan = sub.add_parser("scan", help="执行离线内容扫描")
    scan.add_argument("--request", type=Path, required=True)
    scan.add_argument("--output", type=Path, required=True)

    validate = sub.add_parser("validate", help="验证策略包、请求或回执")
    validate.add_argument("artifact", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "doctor":
            path = (args.policy_pack or default_insurance_policy_path()).resolve()
            policy = read_json_object(path, "insurance content policy pack")
            validate_insurance_content_policy_pack(policy, at=args.at)
            result = {
                "status": "ok",
                "policy_pack": str(path),
                "policy_id": policy["policy_id"],
                "version": policy["version"],
                "review_due_at": policy["review_due_at"],
                "authority": "candidate_policy_only",
            }
        elif args.command == "build-request":
            spec_path = args.spec.resolve()
            spec = read_json_object(spec_path, "insurance content request spec")
            content = _resolve_paths(spec.get("content"), spec_path.parent)
            evidence = _resolve_paths(spec.get("evidence", []), spec_path.parent)
            request = build_insurance_content_scan_request(
                (args.policy_pack or default_insurance_policy_path()).resolve(),
                content=content,
                evidence=evidence,
                created_at=spec.get("created_at"),
                distribution_channel=str(
                    spec.get("distribution_channel") or "unspecified"
                ),
            )
            output = write_insurance_content_scan_request(request, args.output)
            result = {
                "status": "ok",
                "output": str(output),
                "request_sha256": request["request_sha256"],
            }
        elif args.command == "scan":
            report = run_insurance_content_scan(args.request, args.output)
            result = {
                "status": "ok",
                "output": str(args.output.resolve()),
                "readiness": report["readiness"],
                "report_sha256": report["report_sha256"],
            }
        else:
            payload = read_json_object(args.artifact, "insurance content artifact")
            schema = payload.get("schema")
            if schema == "video_creation_pipeline.insurance_content_policy_pack.v1":
                validate_insurance_content_policy_pack(payload)
            elif schema == "video_creation_pipeline.insurance_content_scan_request.v1":
                validate_insurance_content_scan_request(payload)
            elif schema == "video_creation_pipeline.insurance_content_scan_report.v1":
                validate_insurance_content_scan_report(payload)
            else:
                raise ContractError("unsupported insurance content artifact schema")
            result = {
                "status": "ok",
                "schema": schema,
                "artifact": str(args.artifact.resolve()),
            }
    except ContractError as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _resolve_paths(value: object, base: Path) -> list[dict[str, object]]:
    if not isinstance(value, list):
        raise ContractError("insurance content request spec lists are invalid")
    result = []
    for row in value:
        if not isinstance(row, dict):
            raise ContractError(
                "insurance content request spec entry must be an object"
            )
        normalized = dict(row)
        raw_path = str(normalized.get("path") or "").strip()
        if not raw_path:
            raise ContractError("insurance content request spec entry requires path")
        path = Path(raw_path).expanduser()
        normalized["path"] = (
            path.resolve() if path.is_absolute() else (base / path).resolve()
        )
        result.append(normalized)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
