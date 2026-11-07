from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

CHECKS = [
    ("ruff", [sys.executable, "-m", "ruff", "check", "."]),
    (
        "format",
        [sys.executable, "-m", "ruff", "format", "--check", "."],
    ),
    (
        "compile",
        [sys.executable, "-m", "compileall", "-q", "src", "scripts", "tests"],
    ),
    (
        "tests",
        [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
    ),
    ("diff", ["git", "diff", "--check"]),
]


def main() -> int:
    started = time.monotonic()
    for name, command in CHECKS:
        check_started = time.monotonic()
        print(f"\n=== quality:{name} ===", flush=True)
        result = subprocess.run(command, cwd=ROOT, check=False)
        elapsed = time.monotonic() - check_started
        if result.returncode != 0:
            print(f"quality check failed: {name} ({elapsed:.2f}s)")
            return result.returncode
        print(f"quality check passed: {name} ({elapsed:.2f}s)")
    elapsed = time.monotonic() - started
    print(f"\nAll quality checks passed in {elapsed:.2f}s.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
