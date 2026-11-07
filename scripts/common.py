from __future__ import annotations

import csv
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = (
    Path(os.environ.get("MVP_VIDEO_WORKSPACE", ROOT / "workspace"))
    .expanduser()
    .resolve()
)
PROJECT = WORKSPACE / "demo_project"


def ensure_dirs() -> None:
    for rel in [
        "raw",
        "selected",
        "assets",
        "references",
        "metadata",
        "edit",
        "render",
        "publish",
        "qa",
    ]:
        (PROJECT / rel).mkdir(parents=True, exist_ok=True)


def resolve_cmd(cmd: list[str]) -> list[str]:
    exe = shutil.which(cmd[0]) or cmd[0]
    exe_path = Path(exe)
    if exe.lower().endswith((".cmd", ".bat")):
        bundled = exe_path.parent.parent / "app" / "ffmpeg" / f"{cmd[0]}.exe"
        if bundled.exists():
            return [str(bundled), *cmd[1:]]
        return ["cmd.exe", "/c", f'"{exe}"', *cmd[1:]]
    return [exe, *cmd[1:]]


def run(cmd: list[str], cwd: Path | None = None) -> None:
    resolved = resolve_cmd(cmd)
    print("$ " + " ".join(resolved[:10]) + (" ..." if len(resolved) > 10 else ""))
    subprocess.run(resolved, cwd=str(cwd) if cwd else None, check=True)


def run_capture(
    cmd: list[str], cwd: Path | None = None
) -> subprocess.CompletedProcess[str]:
    resolved = resolve_cmd(cmd)
    print("$ " + " ".join(resolved[:10]) + (" ..." if len(resolved) > 10 else ""))
    return subprocess.run(
        resolved,
        cwd=str(cwd) if cwd else None,
        check=True,
        capture_output=True,
        text=True,
        errors="replace",
    )


def ffprobe_json(path: Path) -> dict[str, Any]:
    out = subprocess.check_output(
        resolve_cmd(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_format",
                "-show_streams",
                "-of",
                "json",
                str(path),
            ]
        ),
        text=True,
    )
    return json.loads(out)


def media_duration(path: Path) -> float:
    info = ffprobe_json(path)
    return float(info["format"]["duration"])


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def copy_file(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def project_rel(path: Path) -> str:
    return path.resolve().relative_to(PROJECT.resolve()).as_posix()


def markdown_table(rows: list[dict[str, Any]], columns: list[str]) -> str:
    header = "| " + " | ".join(columns) + " |"
    sep = "| " + " | ".join(["---"] * len(columns)) + " |"
    body = []
    for row in rows:
        body.append("| " + " | ".join(str(row.get(c, "")) for c in columns) + " |")
    return "\n".join([header, sep, *body])
