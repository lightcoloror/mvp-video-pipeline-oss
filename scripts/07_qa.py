from __future__ import annotations

import sys
from pathlib import Path

from common import PROJECT, ROOT, ffprobe_json, read_json, run, run_capture, write_json

SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from video_creation_pipeline.execution_receipts import (  # noqa: E402
    build_ffmpeg_execution_receipt,
    write_execution_contract,
)
from video_creation_pipeline.technical_qa import (  # noqa: E402
    build_technical_qa_report,
    write_technical_qa_report,
)


def main() -> None:
    final = PROJECT / "publish" / "final_1280x720.mp4"
    qa_dir = PROJECT / "qa"
    qa_dir.mkdir(parents=True, exist_ok=True)

    probe_path = qa_dir / "ffprobe.json"
    write_json(probe_path, ffprobe_json(final))

    detector_log = qa_dir / "ffmpeg-detection.log"
    detector_command = [
        "ffmpeg",
        "-hide_banner",
        "-nostats",
        "-v",
        "info",
        "-i",
        str(final),
        "-vf",
        "blackdetect=d=0.5:pix_th=0.10,freezedetect=n=-50dB:d=1.5",
        "-af",
        "silencedetect=n=-50dB:d=1.0",
        "-f",
        "null",
        "-",
    ]
    detector_result = run_capture(detector_command)
    detector_log.write_text(
        detector_result.stderr + detector_result.stdout,
        encoding="utf-8",
    )
    receipt = build_ffmpeg_execution_receipt(
        "technical_qa",
        detector_command,
        [{"role": "media", "path": final}],
        [{"role": "detector_log", "path": detector_log}],
    )
    receipt_path = qa_dir / "ffmpeg-technical-qa-receipt.json"
    write_execution_contract(receipt, receipt_path)

    edl = read_json(PROJECT / "edit" / "edl.json")
    expected_duration = round(3.0 + float(edl["total_duration_s"]), 3)
    report = build_technical_qa_report(
        final,
        probe_path,
        receipt_path,
        subtitles_path=PROJECT / "edit" / "subtitles.srt",
        packaging_plan_path=PROJECT / "render" / "video_packaging_plan.json",
        expected_duration_s=expected_duration,
    )
    report_path = qa_dir / "qa_report.json"
    write_technical_qa_report(report, report_path)
    _write_markdown(report, qa_dir / "qa_report.md")
    _extract_cover(final)

    if not report["readiness"]["technical_ready"]:
        raise SystemExit(2)
    print("automatic technical QA passed")


def _extract_cover(final: Path) -> None:
    run(
        [
            "ffmpeg",
            "-y",
            "-ss",
            "1",
            "-i",
            str(final),
            "-frames:v",
            "1",
            "-update",
            "1",
            str(PROJECT / "publish" / "cover.png"),
        ]
    )


def _write_markdown(report: dict, output: Path) -> None:
    metrics = report["media_metrics"]
    detector = report["detector_metrics"]
    readiness = report["readiness"]
    lines = [
        "# 自动技术 QA 报告",
        "",
        f"- 状态：`{readiness['status']}`",
        f"- 技术就绪：`{readiness['technical_ready']}`",
        f"- 文件：`{report['inputs']['media']['path']}`",
        f"- 时长：{metrics['duration_s']} 秒",
        f"- 分辨率：{metrics['width']}x{metrics['height']}",
        f"- 帧率：{metrics['fps']}",
        f"- 视频编码：{metrics['video_codec']}",
        f"- 音频编码：{metrics['audio_codec']}",
        f"- 黑屏比例：{detector['black_ratio']}",
        f"- 静音比例：{detector['silence_ratio']}",
        f"- 静帧比例：{detector['freeze_ratio']}",
        "",
        "## 检查结果",
        "",
        "| 检查项 | 状态 | 阻断 |",
        "| --- | --- | --- |",
    ]
    lines.extend(
        f"| {row['id']} | {row['status']} | {row['blocking']} |"
        for row in report["checks"]
    )
    lines.extend(
        [
            "",
            "## 边界",
            "",
            "技术检查已自动完成，但不构成创作审批或发布授权。",
        ]
    )
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
