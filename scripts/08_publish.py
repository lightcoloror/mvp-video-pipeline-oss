from __future__ import annotations

from common import PROJECT, copy_file


def main() -> None:
    copy_file(PROJECT / "edit" / "subtitles.srt", PROJECT / "publish" / "subtitles.srt")
    copy_file(
        PROJECT / "render" / "video_packaging_plan.json",
        PROJECT / "publish" / "video_packaging_plan.json",
    )
    copy_file(
        PROJECT / "selected" / "project_materials.md",
        PROJECT / "publish" / "project_materials.md",
    )
    copy_file(PROJECT / "qa" / "qa_report.md", PROJECT / "publish" / "qa_report.md")

    log = [
        "# 本地发布候选包记录",
        "",
        "状态：未发布",
        "平台：未选择",
        "网络操作：未执行上传或发布",
        "标题：AI Video Pipeline MVP",
        "版本：final_1280x720.mp4",
        "",
        "## 关键素材",
        "",
        "- hook",
        "- evidence",
        "- b-roll",
        "",
        "## 剪辑策略",
        "",
        "自动筛掉 bad take，保留 hook、evidence、b-roll 三段。",
        "",
        "## 包装策略",
        "",
        "FFmpeg 生成标题卡，rough cut 烧录字幕，合并为发布视频。",
        "",
        "## 下次替换",
        "",
        "- 索引层替换为 SentrySearch。",
        "- 标签层替换为 TagStudio 导出。",
        "- 剪辑层替换为 chengfeng-videocut-skills 或 browser-use/video-use。",
        "- 包装层替换为 HyperFrames。",
    ]
    (PROJECT / "publish" / "publish-log.md").write_text(
        "\n".join(log), encoding="utf-8"
    )
    print(f"local release-candidate package ready: {PROJECT / 'publish'}")


if __name__ == "__main__":
    main()
