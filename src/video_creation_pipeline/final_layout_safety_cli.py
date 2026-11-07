"""CLI for the non-rendering final-layout safety gate."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .contract_io import atomic_write_json
from .errors import ContractError
from .final_layout_safety import validate_final_layout_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("document", type=Path)
    parser.add_argument("--workspace-root", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.output and (args.output.exists() or args.output.is_symlink()):
            raise ContractError(
                "final layout output already exists; choose a new receipt path"
            )
        receipt = validate_final_layout_path(args.document, args.workspace_root)
        if args.output:
            atomic_write_json(receipt, args.output)
    except ContractError as error:
        print(json.dumps({"ok": False, "message": str(error)}, ensure_ascii=False))
        return 2
    payload = json.dumps(receipt, ensure_ascii=False, indent=2) + "\n"
    if not args.output:
        print(payload, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
