# 进度条默认脚本标题与独立导航字号

历史方法记录：2026-10-28 09:18:10 +08:00，Codex（GPT-6，精确运行变体未独立核验）。
公开适配：2026-11-08，Codex（精确运行模型未独立核验）。保留通用规则和现有入口；原本机项目、媒体及私有验收日志不随本文分发。

## 标题从当前稿件来

章节模式默认 `title_source=script`，读取当前 Markdown 稿件的二级标题，复用作者原文，不自动另写摘要。例如 `## 小标题` 或 `## 01｜小标题`；编号用于对应章节，显示标题保留正文。

通过 MVP 的 `build --script`，或在 spec/packaging 中提供 `script_ref` 绑定稿件。引用包含 `path` 和 `sha256`，完整 artifact 引用也兼容。建议使用明确的绝对路径；稿件须在给定工作根内。多个绑定不一致、文件哈希变化、没有二级标题或编号重复时会拒绝该计划。

省略 `chapters` 时，一个场景对应一个小标题。多场景章节可用 `scene_id` 和 `script_heading` 指定章起场景；时间仍来自既有场景时间轴，不在章节条中另填独立时间戳。

确需不同标题时，显式使用 `title_source=manual`、非空 `title_override_reason` 和 `chapters[].title`。只提供另一组标题但未声明覆盖，会与稿件原文核对；不一致即报错。填写覆盖理由本身不等于作者已确认。

## 导航采用独立字号

现有 [导航字体预设](../src/video_creation_pipeline/policies/progress-font-presets.v1.json) 默认 `compact`，按画布短边等比缩放：

| 预设 | 短边1080px | 短边540px |
| --- | ---: | ---: |
| `small` | 32px | 16px |
| `compact`（默认） | 40px | 20px |
| `comfortable` | 48px | 24px |

显式 `font_size_px` 优先，必须是正整数。进度导航不套用正文、字幕或 motion label 的最小字号；旧 `typography_profile` / `typography_role` 可读入，但不决定进度字号。组件不靠自动缩字挤入条带。标题太长、越界、遮挡等仍由现有布局检查处理，实际手机可读性仍需人工观看。

正文、字幕、封面的字体策略保持各自原有含义。导航尺寸较小不构成放宽其他文字规则的理由。

## 原入口与分工

在 MVP 源码根目录中运行已有 [progress_overlay CLI](../scripts/progress_overlay.py)。以下是参数示意，需先提供自己的输入文件；不是可直接执行的完整样例：

```text
python scripts/progress_overlay.py build --program <base.mp4> --packaging <packaging.json> --layout <final-layout.json> --spec <progress-spec.json> --script <current-script.md> --workspace-root <workspace> --output <new-plan.json>
python scripts/progress_overlay.py validate --plan <new-plan.json>
```

自媒体系统的 `video-delivery progress-select --script` 是单独消费者的选择入口，不属于本仓库安装命令。它把当前稿件、packaging、layout、spec 和选用理由传给既有交付合同；实际选用与审核须由该消费者完成。本组件不创建另一套时间映射、渲染器或发布流程。

脚本引用、导航预设文件哈希、最终字号及其来源进入计划。稿件或预设变化后需重建计划，不能手改旧回执冒充新执行。后续渲染继续走已有 FFmpeg 出口、执行回执和布局/交付检查。选择或生成计划不是编码完成，也不是作者通过。

## 验证范围

本次公开适配只独立核对当前公开代码的标题来源、稿件漂移/重复编号拒绝、字体缩放/覆盖规则，以及实际 CLI `build --help` 的 `--script` 参数；没有运行渲染或读取真实媒体。历史项目的测试数量、成片版本、观看效果和本机日志不作为这次公开验收证据。

本文说明已有代码。跨项目完整交付、实际编码、手机观看、作者确认及平台发布，均需相应输入、执行和证据；文档与合成检查不能替代这些结果。
