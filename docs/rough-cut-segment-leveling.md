# 粗剪分段人声音量对齐

2026-10-29（Asia/Shanghai），Codex（当前运行型号未另行核验）。

粗剪需要分别完成「各段人声基准音量对齐」和「应用后的复测与接缝试听」。整片 LUFS、整体峰值、听过全片的勾选，均不能代替这两项。最终 BGM 遮蔽、混音响度与真峰值仍在包装后检查。

## 执行顺序与入口

1. 完成语义切段、重录取舍、气口及边界检查。`spoken_rough_cut compile` 调用既有 videocut-kit dry-run 得到实际保留段。缺少分段音频证据时保留 `cut.segments.json`、`segment-level-spec.json` 等候选文件，返回未完成，不写可交付的 `spoken-edit-plan.json`。
2. 用已有受资源保护的音频出口取得未混 BGM 的人声 PCM；沿用原时钟，不用含长停顿的整段平均值冒充说话音量。准备后的 spec 已绑定源文件哈希、实际切段和逐字稿语音窗口。
3. `python scripts/spoken_rough_cut.py inspect-segment-levels --audio dialogue.wav --spec segment-level-spec.json --output before-levels.json`。测量复用 `audio_edit_quality.inspect_pcm_levels`，按实际语音时长加权汇总每段 RMS dBFS，并检查相邻段差异、削波、声道异常。**这不是 LUFS**。
4. `python scripts/spoken_rough_cut.py segment-leveling-template --report before-levels.json --output leveling-review.json`。自动生成以中位数为基准、保留 ±1 dB 自然差异、最大 ±6 dB 的逐段候选增益。Agent 试听后填写各段 `audio_checked/expression_preserved/evidence` 与整体 `expression_preserved/evidence`；已经一致的段显式记录 0 dB。不得自动把试听标记改为通过。
5. 把 review 放入 decisions 的 `rough_audio_review.segment_leveling`，其 `clip_gains` 原样放入 decisions 的同名字段，再编译。最终切段与测量不一致、逐字稿覆盖变化、音频/源素材哈希变化、增益丢失，均拒绝。语义 review complete 不等于粗剪音频验收完成。
6. 既有 FFmpeg / video-use 出口应用这些增益；Jianying、Premiere、OpenChatCut 等编辑器继续使用既有适配器或时间线音量控制。编辑器交接需携带逐段增益表，不能因导入成功而宣布音频已完成。`prepare_delivery` 显示该项仍待应用和复测；实际渲染结果明确保留 `rough_cut_audio_accepted=false`。
7. 导出当前剪辑的人声独立 stem（可只出音频，不要求整片视频导出）。复制 before spec，改 `phase=after`，`measured_media` 绑定当前已保存工程或剪辑媒体，使用同一组原始语音窗口再运行 `inspect-segment-levels`。默认 after 时钟是保留段顺序拼接；有静音/纯音乐插入时，显式给出每段 `output_segments`，保留插入后的真实位置。变速或多源工程用已编辑时钟的人声 stem 和保存的编辑版本建立基线，设置 `analysis_clock=edited_timeline`，review 的 `timeline_ref` 绑定当前完整 timeline JSON；主入口校验它与实际时间线一致。该模式在既有变速之后测量与对齐，不套用旧源时钟。
8. 将 after 报告引用填入 review 的 `after_ref`，逐接缝试听后填写 `joins_checked`。运行 `check-segment-leveling --review leveling-review.json --measured-media <当前媒体或工程快照> --output accepted.json`。该检查重读绑定 WAV 测量，校验前后同一组段与窗口，检查实际增益是否遗漏或重复应用（允许共同母带增益），检查剩余段差和相邻差。
9. 自媒体主入口 `video-delivery preflight/check/deliver` 必须提供 `rough_audio_review_ref` 指向该完整 review，而不是只提供简短的 accepted 状态。入口重验当前媒体、真实时间线、完整语音窗口和前后测量，覆盖本机编辑器路线。显式无 speech 的时间线才标为不适用。

## 参数与人工干预

统一参数在 `src/video_creation_pipeline/policies/editing-presets.v1.json` 的 `loudness.segment_leveling`，不是平台强制标准。默认验收段间范围及相邻差 ≤3 dB；实际应用相对预期的偏差容限 1 dB。短于一段的自然重音不被自动逐字拉平；数字通过仍要回听。

强调、角色或情绪确需较大差异时，`intentional_level_difference` 必须记录当前 `segments`、`audio_checked=true`、`expression_preserved=true` 和具体 `evidence`。这一例外只允许保留有意的音量差，不能豁免缺测、过期证据、削波、增益未执行或接缝试听。Agent 和人工均可以修订增益；修改后重测，不锁定某个编辑器。

报告中 `source_media`、`measured_media`、`source_audio`、before/after 引用使用现有 `contract_io.artifact_reference` 格式。`segments` 是 `{start,end}`（秒）的已保留说话段；`speech_windows` 是实际 ASR/逐字稿/VAD 语音区间，不能直接把所有段的全长填作语音。该工具核验区间与测量，不能独立证明传入 stem 已去除 BGM，需由既有 stem 提取路径和试听负责。

旧项目文件保留。只有整片响度或勾选证据的历史记录，不自动升级为本规则通过；下一次粗剪编译/交付需补测。当前成片不因本次流程修改被重新剪辑或自动冻结。

## 本次变动说明

| 变动 | 意图 | 决策 | 理由 | 证据 | 生效范围 |
|---|---|---|---|---|---|
| 分段测量与增益建议 | 发现段与段的人声不齐 | 复用现有 PCM 检查，按语音时长汇总；中位数、自然余量、有限增益 | 整片响度会掩盖局部差异，停顿会拉低全段 RMS | `segment_leveling.py`；真实项目前后声段差 9.08→2.24 dB，旧整片仍为 −16.1 LUFS | 人声粗剪；不改变 BGM 选择、语义剪辑或最终响度规则 |
| 粗剪硬检查 | 防止可选增益表导致漏做 | 编译及 video-use 编辑计划绑定所有保留段、语音窗口和逐段增益 | generic checkbox 无法证明分段测量 | `spoken_rough_cut.py`、`video_use_edit_loop.py`；遗漏、旧段、空增益回归 | 新计划编译；历史文件保留，缺证据不能成为新通过 |
| 复测与跨编辑器交付检查 | 验证增益实际应用 | 重测 dialogue stem，绑定当前媒体/时间线；主入口复核 | 导入成功、渲染成功和混音 LUFS 均不等于音量对齐 | `video_delivery.rough_audio_check`；丢增益、过期窗口、缺 after 回归 | 自媒体交付主入口及本机编辑器路线；不新增渲染器 |
| CLI 与发现文档 | 后续 Agent 能执行且允许人工改动 | 提供测量、未批准模板、验收命令；异常需具体记录 | 避免只写流程而没有可调用代码 | `spoken_rough_cut_cli.py`、本文、合成 PCM 与 CLI 回归 | 同一流程入口；不授予发布/上传/资源豁免权限 |
| 测试输入升级 | 既有测试验证新要求 | 用合成 PCM 完成前后测量，补 Windows 路径规范化 | 旧测试只勾选音频通过，无法覆盖本次漏项 | `tests/segment_level_fixture.py` 与两个项目回归结果 | 测试专用；不冒充人工验收真实成片 |

本地源码复用：`audio_edit_quality.py`、`editing_presets.py`、`contract_io.py`、`spoken_delivery._render_command`、`production_delivery.treatment_filters` 与现有 `video_use_edit_loop`。已有模块足够，因此没有新装库、下载模型或另建 FFmpeg 执行器。
