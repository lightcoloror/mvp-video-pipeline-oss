# 视频顶部／底部进度条接入

更新时间：2026-10-11 15:50:04 +08:00

执行者：Codex（GPT-6）

进度条是锁定剪辑之后的可选包装步骤。已有本地 CLI 可生成普通条或章节条，复用 packaging 场景时钟、最终布局检查和统一 FFmpeg 出口。章节名默认复用视频脚本小标题，导航字号使用独立预设。未安装新服务或引入另一套渲染器。

## 放进既有流程的位置

1. 粗剪完成叙事、原声音量和字幕覆盖；精剪锁定顺序、变速、片头片尾与总时长。
2. 在现有 visual route / scene plan 决定本片是否需要进度提示，以及普通条、章节条、顶部或底部。短情绪片、唱歌或沉浸记录无需机械加条。
3. 更新同一份 packaging，绑定视频脚本 `script_ref`。默认使用脚本的 Markdown 二级小标题；一场景对应一章时无需重复填标题。场景更多时，在 chapters 中用 `scene_id / script_heading` 指定对应小标题，时间仍从现有场景投影。不要把原素材 SRT、参考视频或外部工具产生的旧时间轴直接当作成片时间。如果边界位于场景中间，先在既有场景计划中明确该边界。
4. 读取本次 final-layout 的字幕、人物/证据重点、标识、圆角与平台遮挡区域；选好位置并生成进度条计划。所有重要主体必须已在布局材料中声明，自动检查不能发现漏标的人脸。
5. 用既有 FFmpeg 出口合成到新的候选文件，音频流直接复制。普通条为完整成片全长；章节条为同一进度加章节刻度和当前章节名称。片头片尾计入全长。当前不提供自动正文区间截取。
6. 对新候选重新做完整解码、最终布局检查、实际画面/手机显示检查及所需隐私复扫，再走原交付流程。生成成功不等于人工审美通过。

修改剪辑、脚本、packaging、导航字体预设、字体、布局或 spec 后，旧计划校验失败，重新 build/render。进度条仅位于包装层，修改条的样式不需要重新生成原声、ASR 或下游之前已缓存的场景。

## Agent 直接调用

在本项目根目录运行。`workspace-root` 必须包含成片、布局证据、计划和新输出；输入程序应为零起点、方形像素、无待应用旋转、与 packaging 同尺寸和帧率的已锁定成片。

```powershell
python scripts/progress_overlay.py build --program <base.mp4> --packaging <packaging.json> --layout <final-layout.json> --spec <progress-spec.json> --script <video-script.md> --workspace-root <project-root> --output <project-root/progress-plan.json>
python scripts/progress_overlay.py validate --plan <project-root/progress-plan.json>
python scripts/progress_overlay.py render --plan <project-root/progress-plan.json> --output-root <project-root/progress-candidate>
```

模板：[顶部章节条](../examples/progress-overlay/chapters-top.json)、[底部普通条](../examples/progress-overlay/plain-bottom.json)。示例尺寸为 1080×1920。根据当前画幅和平台修改尺寸参数；模板不是通用安全区。

- `mode`：`plain` / `chapters`；`position`：`top` / `bottom`，自由组合。
- `margin_x_px`：左右留白；`edge_offset_px`：整个条带到选定边缘的距离；`bar_height_px`：条高。全为最终画布像素，条的宽高须为偶数。
- 章节条只有当前脚本小标题。默认 `title_source=script`；脚本通过 `--script`、packaging 或 spec 中的 `script_ref` 提供，引用必须带真实哈希，多处来源须一致。省略 chapters 时按场景顺序一一对应脚本小标题；数量不符需显式 `scene_id / script_heading` 映射。支持 `## 小标题`、`## 01｜小标题` 等二级标题，编号不作为标题正文。
- 不同标题必须显式设置 `title_source=manual`、`title_override_reason` 和 `chapters[].title`；不能用缺脚本或偷偷写入另一组摘要来绕过默认来源。标题过长仍报错，可明确调整导航字号/条宽或由作者决定改标题，不自动省略或缩写。
- 字体绑定具体本地文件；`policies/progress-font-presets.v1.json` 是**进度导航独立预设**，不调用正文、字幕或 motion_label 最小字号门。`font_preset` 可选 small / compact（默认）/ comfortable，按画布短边相对1080缩放；1080画布分别32/40/48px，540画布为16/20/24px。`font_size_px` 可显式覆盖，只检查正整数、实际布局和可读性，不再套用正文/字幕最小值。
- 旧 spec 的 `typography_profile / typography_role` 保留输入兼容但不参与导航字号检查；新模板已删除这些字段。字号、预设文件哈希、脚本来源和实际布局全部进入计划，修改后旧计划失效。缺字、手机可读性仍须查看实际输出，尺寸预设不代替作者验看。
- 颜色使用已有视觉计划，附 `color_reason`。示例青绿色表示播放位置，不代表风险/安全级别，也不是强制 IP 色。
- 计划绑定原片、packaging、布局、spec、脚本、独立导航预设、字体和实现版本；布局检查增加现有 `information_motion` 角色，不改原布局文件。
- 输出目录必须新建或为空。生成 `candidate.mp4`、计划依赖的 filtergraph/text、合成回执、完整解码证据、新候选的 final-layout 与检查回执及 `result.json`。
- 资源使用既有 guard 和环境参数，不另建并发器、不写死资源阈值。遇资源暂停保留现场，不自动重试。

## 必查时刻

| 位置 | 检查 |
|---|---|
| 第 0 帧 | 进度为零，没有高亮从左侧留白漏出来 |
| 25% / 50% / 75% 附近 | 匀速增长、条没有越界或在转场闪烁 |
| 每章前一帧与首帧 | 章节标题按最终 scene 边界切换，不提前或延迟 |
| 最后一帧 | 条到满，片尾动作、笑声及音频尾巴保留 |
| 最长中文标题 | 手机尺寸可读、缺字/截断/换行检查，不靠缩字塞入 |
| 本片人物、字幕和平台按钮出现处 | 同时看实际像素和布局报告，不能只看空白背景样片 |

播放器进度控件与烧录条是不同东西；本功能产出视频画面的一部分，不支持观众点击跳章。平台章节元数据可另按平台能力输出。

## 本次实际验证及开源参考

2026-10-11：26 项相关单测/布局/字号回归通过；本机 FFmpeg 8.1 成功输出两段 6 秒、1080×1920、24 fps 的纯合成样片，顶部章节条与底部普通条各完整解码 144 帧。原版与两个包装版的音频数据哈希一致。查看了 270 px 宽的首帧、章前/章首和末帧拼图，章节名称、边界与终点显示正常。本次没有处理真实素材；`human_review` 保持 pending。

证据：[本地样片与回执](../../../outputs/progress-overlay-20261011/sample/)。测试最初遭遇 Python 3.13 Windows 临时目录 ACL 问题，改用已有 `windows_test_runtime.py adapter` 后通过，没有改系统权限。

- [FFmpeg overlay / drawtext 官方文档](https://ffmpeg.org/ffmpeg-filters.html#overlay)：实际使用的现有开源执行能力。
- [vmarker](https://github.com/ApeCodeAI/vmarker)：此前推荐的 SRT/章节条候选。当前本地方案已有所需的普通/章节功能，因此暂未安装或运行此项目；其 AI 分章也没有被引入。
- [chapter-progress-bar](https://github.com/serenawangCU/chapter-progress-bar) 与 [chapter-progress-bar-plus](https://github.com/DrBanshan/chapter-progress-bar-plus)：风格与交互参考，未把其代码或默认导出流程接入本地。后续需要特定样式时只添加到现有组件/渲染路线，不重新创建主时间轴。

回退：不调用此可选步骤，继续使用原包装候选；原片和音轨不被覆盖。新增候选仍需既有人工复核与发布授权。

## 通用交付入口接入 · 2026-10-28

2026-10-28 08:48:47 +08:00｜Codex（GPT-6，精确运行变体未独立核验）。历史样片与回退说明保留；现在经自媒体 `video-delivery` 交付时，必须明确 `use` 或有理由的 `not_used`，不能用缺字段表达不使用。详见 [统一输入合同](../../../self-media-creation-system/docs/video-delivery.md)。

选用时，`progress-select` 调用本组件 build，预留整个条带区域并绑定当前媒体/时间轴；交付检查重建计划，核对真实滤镜、章节文字、执行/解码回执及最终候选布局。`render` 返回的 `result.json` 新增 `render_receipt / decode_receipt / decoded_media / final_layout / layout_receipt` 哈希引用，供现有交付合同消费。成功输出仍为候选，不能代替作者观看与发布授权。

`bar_width_px` 可选：不填仍使用原左右留白宽度；填写后为指定偶数宽，必须在画布和边距内，不靠缩字适配。540×960 竖屏使用已有字号系统中的 `semantic_motion_540x960`（1080预设等比缩放），不建立另一套字号标准。`filtergraph(..., write_texts=False)` 仅供交付端只读重建比对。Python `render` 可注入原资源策略和监控 runner，默认策略不变；调用方仍须有该次执行授权。

| 变动 | 意图 | 决策 | 理由 | 证据 | 生效范围 |
|---|---|---|---|---|---|
| `progress_overlay.py` 输出回执、只读图重建及条宽参数 | 让原组件可被实际交付验证并适配局部净空 | 复用原 FFmpeg 和布局/字号门；不新增渲染器 | 旧 passed/result 标签不足以证明最终视频真正带条 | `tests/test_progress_overlay.py` 与布局/字号共30项测试、9个子测试通过；RWE 真实视频完整解码 | 原组件调用者；旧宽度/资源默认值保持 |
| `typography-gates.v1.json` | 在540竖屏保持相同相对可读性 | 增加原1080预设的半尺寸配置 | 不能为低清预览跳过字号门 | 字号回归及真实章节名画面 | 540×960的信息动效；其他画幅不变 |
| 自媒体薄适配器 | 使跨项目选用、预留、交付同源 | 在 `video-delivery` 中重新调用本组件和既有回执校验 | 防止待办/旧计划被误报为已渲染 | 字幕→进度条→完整合同合成正例；缺决定/预留/结果/错成片反例阻断 | 统一交付入口；不使用进度条须说明理由 |

公开版验收结果须依据本次独立测试与回执；原本机历史路径和项目结果不随源码分发。

### 脚本标题与独立导航字号

章节条默认复用当前稿件的二级标题，并绑定稿件哈希；进度导航采用独立的缩放预设和显式像素覆盖，不套用正文/字幕最小字号。标题来源、覆盖理由、稿件/预设漂移及后续执行边界见 [通用代码说明](progress-script-title-navigation-defaults-20261028.md)。正文和字幕策略保持原有规则。

公开文档适配：2026-11-08，Codex（精确运行模型未独立核验）。替换此处未随包提供的历史项目等价性和私有日志指引，保留通用方法及实际代码入口；不声明本轮发生媒体编码或作者审核。
