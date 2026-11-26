# 进度条默认脚本标题与独立导航字号

## 2026-10-28 09:18:10 +08:00 | Codex（GPT-6，精确运行变体未独立核验）

- Action：将作者要求落实到原共享组件、统一交付入口和RWE调用方。
- Files：`src/video_creation_pipeline/progress_overlay.py`、`policies/progress-font-presets.v1.json`、`scripts/progress_overlay.py`、章节模板、进度条/交付测试；自媒体 `video_progress_contract.py`、`video_delivery.py`、`self_media_agent.py`；RWE `edit/package_progress_preview.py`。
- Summary：默认复用当前视频脚本小标题；进度导航字号不再调用正文、字幕或motion_label最小字号门。原FFmpeg执行、时间映射、布局/遮挡、完整解码及人审检查继续使用。

## 代码中的默认行为

1. 章节模式默认 `title_source=script`。通过MVP `build --script`、自媒体 `progress-select --script`，或packaging/spec的 `script_ref` 绑定当前脚本。引用至少含绝对path和sha256，完整artifact引用也兼容。多处绑定不一致、稿件变更、缺小标题或编号重复均报错。
2. 读取Markdown二级标题：`## 小标题`、`## 01｜小标题` 等。编号只用于对应关系，标题正文原文复用；不另造摘要。省略chapters时要求一个场景对应一个小标题；多场景视频用 `scene_id / script_heading` 显式标明章起场景。时间仍只取原packaging，不接受独立时间戳。
3. 需要另一组标题时，显式填 `title_source=manual`、`title_override_reason` 和 `chapters[].title`。只填title而不声明覆盖，会与原稿核对；不一致就阻断。填写理由不等于自动获得作者确认。
4. 默认 `font_preset=compact`。独立预设存于 [progress-font-presets.v1.json](../src/video_creation_pipeline/policies/progress-font-presets.v1.json)，按画布短边缩放；不读取 `typography-gates.v1.json` 的正文/字幕/动效标签最小字号。显式 `font_size_px` 优先，不自动放大到正文下限，也不自动缩字挤入。
5. 旧spec的 `typography_profile / typography_role` 可读入但不影响进度字号；新模板和RWE调用方已去掉。像素值须为正整数；文字/条带越界、遮挡、标题过长仍失败，实际手机可读性由现有 `progress_mobile_readable` 人审覆盖。
6. 脚本、导航预设文件哈希、最终选用字号和来源进入计划；改稿或改预设需重建。通用交付的 `script_ref` 进入选择依据，新脚本不能沿用旧选择。

| 导航预设 | 1080短边 | 540短边 | 说明 |
|---|---:|---:|---|
| small | 32px | 16px | 较弱的辅助导航 |
| compact（默认） | 40px | 20px | 当前RWE采用的层级 |
| comfortable | 48px | 24px | 需要更醒目时选用 |

这是可选预设，不是新一套正文最小字号；作者可明确指定其他正整数大小。布局检查和真实输出可读性检查保留。

## 原入口用法

```text
python scripts/self_media_agent.py video-delivery progress-select --input <manifest.json> --output <新的manifest.json> --decision use --reason <本片选用理由> --script <当前video-script.md> --packaging <packaging.json> --layout <final-layout.json> --spec <progress-spec.json> --workspace-root <项目根>
python scripts/progress_overlay.py build --program <base.mp4> --packaging <packaging.json> --layout <final-layout.json> --spec <progress-spec.json> --script <当前video-script.md> --workspace-root <项目根> --output <新的progress-plan.json>
```

上面第一条在自媒体项目运行，第二条在MVP项目运行；路径均是占位符。字体文件、颜色/位置等沿用原spec。选择/生成计划不是渲染成功，也不是作者通过；后续沿用原render/check/deliver流程。

## 每项变动的依据

| 变动 | 意图 | 决策 | 理由 | 证据 | 生效范围 |
|---|---|---|---|---|---|
| 共享章节来源解析 | 默认复用视频脚本结构 | 将RWE已验证的小标题读取适配到原progress_overlay，绑定脚本哈希并核对标题/映射 | 上一轮只有RWE项目代码读取原稿，其他项目仍须手填标题 | 默认省略标题正例；缺稿、不同摘要、重复编号、错来源/改稿反例；RWE十章CLI结果 | 新章节计划；旧手写标题需补脚本或显式覆盖理由 |
| 独立导航字号 | 解除正文/字幕最小值对辅助导航的约束 | 原组件改读专用progress-font-presets；支持明确像素覆盖；原正文策略不改 | 作者本轮明确要求各自预设；不能只在RWE填20px而继续受正文门限制 | small预设及16px覆盖通过；正文/字幕原回归通过，策略哈希与v0.7所绑原值相同 | 只影响进度条，不放宽正文、字幕、封面规则 |
| 脚本/预设失效检查 | 避免标题来源或字号变了仍用旧计划 | 新依赖进入计划与交付选择；保留原hash/最终成片/执行回执核对 | 默认行为要贯通实际生产和消费 | 改稿/改独立预设计划失效；统一交付脚本透传及旧选择拒绝测试 | preflight、build/validate和check/deliver |
| CLI、模板、发现入口 | 其他项目能直接复用 | 两个原CLI加入script参数；模板不再写另一组标题和正文profile；context暴露默认值 | 不新增第二个渲染器/独立流程，不靠登记待办 | MVP CLI build/validate、自媒体CLI progress-select和context回归 | 原统一入口及今后项目 |
| RWE薄适配器 | 当前项目与通用逻辑同源 | 调共享script_headings与navigation_typography；去掉本地解析重复实现及硬编码字号 | 后续调预设无需再改项目脚本 | RWE十章标题/几何/滤镜图与实际v0.7相同 | 后续RWE包装；本轮不改当前媒体/审核文件/绑定 |

## 实际验证与边界

- MVP进度条/布局/正文原字号回归41项通过，另9子测试通过。交付回归36项中35通过、1项原有浏览器测试因未提供显式Node/Playwright路径跳过；合计76项通过、1项跳过。
- 真实合成媒体分别走普通条和默认脚本章节条：原字幕烧录→进度渲染→完整解码/最终布局→统一交付合同检查通过。合成fixture中的人审字段只验证合同，不是实际作者审核。
- 当前RWE经两个原CLI生成/核验新默认计划：十个标题来自原稿、compact=20px，几何和实际v0.7已执行滤镜图一致。它是输入/计划等价性验证，没有冒充新编码回执。当前视频、预览绑定、原声时钟和136项修订保持。
- 新实现哈希/依赖与旧计划不同，旧计划留作历史证据；今后重做包装须重建计划，不手改历史回执。现有v0.7媒体可继续观看。
- 未安装新依赖。复用本地已运行的progress_overlay、FFmpeg、contract_io、布局检查和原RWE小标题提取方式；预设文件由原有policies/*.json打包规则覆盖。
- 文档本地保存；此前Obsidian MCP写入未能确认，本轮未直接写库文件或虚构同步完成。

证据目录：`<workspace>/deliverables\rwe-video-edit-20261025\review\progress-shared-defaults-tests-20261028`。最终日志 `mvp-final2.log`、`delivery-final2.log`；实际项目等价性 `rwe-consumer/verification.json` 与CLI `cli-verification.json`。最初回归确实被旧正文profile缺项拦住；中间修复共享引用元数据格式差异后，最终两套检查通过。未把初次基础设施错误算作需求反例。
