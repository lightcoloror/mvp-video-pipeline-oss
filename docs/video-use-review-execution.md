# video-use 审片执行与可逆修改增量

2026-10-08，Codex（实际运行型号与推理档位见本轮 runtime-model-evidence.json）。

状态：本地候选实现；13 项有界合同回归通过。首次真实合成媒体命令被 resource_pressure_pause_required 拒绝，FFmpeg 未启动。实际 CLI 出片、波形/条带、单声道/无音轨、局部重渲染和复用尚未完成验收。不能标记 synthetic_verified 或 real_ready。继续前需要新的明确执行及新资源检查；不自动重试、不降低门槛。

## 意图、决策与范围

1. 审片执行：原 handoff 只描述切点，新增 execute 消费其窗口，调用单一 FFmpeg 出口生成短试听片段，再对短片一次解码取得条带和 PCM。复用固定 video-use helpers/timeline_view.py 的图形合成函数，替换它的直接解码、默认字体和临时目录入口；不改上游。理由：避免逐词整片解码以及多套 FFmpeg。窗口限 32 个/累计 120 秒/单窗 20 秒，资源门仍有效。真实输入、局部字幕、字体、上游和适配器哈希参与缓存；损坏缓存不复用。
2. 建议层：新增 suggest，仅消费已提供的 words/subtitles/utterances 与显式映射。较长词间间隙、完整重复句、带显式完整性/质量标注的重录生成候选，保留气口、不删中文/粤语所谓 filler、不把较晚版本当最佳。间隙不等于静音，质量标注不等于实际听感。
3. 修改层：prepare-edit 将显式选择转为现有 NLE 保留段结构，复用 spoken_rough_cut 的保留/删除互补校验；apply-edit 使用现有 approval_record.v1（target=storyboard）绑定该计划。计划绑定建议、媒体、上下文和选择文件，任何漂移拒绝执行。既有出口按场景缓存保留段，拼接后检查受影响场景两侧与内部删除产生的新接缝。文件存在、完整解码和技术生成均不能代替人工审片。

与现有规范关系：执行 `continuous-semantic-editing-workflow.md`，不重写语义规范；v1 handoff 仍是非执行计划，必须显式调用新增 execute/apply-edit 才可能执行。本增量不修改发布门、Registry、服务或上游代码。原文件保留；输出目录有不同任务或部分失败文件时拒绝覆盖，另选新目录保留失败证据。

## CLI

使用现有 `python scripts/video_use_review.py`：

```text
execute --handoff handoff.json --context context.json --cache cache --output review.json --font <font.ttc>
suggest --handoff handoff.json --context context.json --output suggestions.json
prepare-edit --suggestions suggestions.json --selection selection.json --output edit.json
apply-edit --edit-plan edit.json --approval approval.json --cache cache --output-dir revised --font <font.ttc>
```

`--only review-002,review-003` 可限制审片窗口。审批沿用现有 creative approval 入口，工具不会生成或默认授予真实剪辑审批。合成测试中的 fixture operator 仅验证哈希绑定，不是人类批准。

输入 context 是显式标准化侧文件，不是新的转写真源：

- schema=`video_creation_pipeline.video_use_context.v1`；media 为 candidate 的标准 artifact_reference。
- words、subtitles、utterances 均为有序、不重叠数组，每项含唯一 id、start/end（成片秒）、text。缺词级时间或字幕会阻断，不按字数估时。
- 每个 word 必须被 subtitle 和 utterance 覆盖。上游有缺漏先回到原逐字稿工作流，不由此补造。
- mapping 连续覆盖 candidate，每项含 output_start/end、source_start/end、标准 source artifact_reference；显式记录源→成片关系，源范围须在源时长内。
- timing_basis 说明时间依据；synthetic_fixture_only 区分合成夹具。utterance 可带 take_group、complete、quality_score，只表示输入标注。
- selection 为 suggestion_ids 数组；alternative_take 另须 meaning_review[id] 说明独有信息/限定/转折的取舍，再由审批确认。

波形局部归一化，仅辅助查看节奏，不能读出绝对响度。无音轨显示 NO AUDIO，不冒充静音正常。中文字体逐字检查覆盖；独立 HTML 保留较大的原文/字幕及短片，条带小字不是最终发布字幕。

## 证据与已知限制

证据根：`<workspace>/outputs/video-use-review-20261008/child/`，包括 baseline、upstream、资源拒绝日志、合同测试、待执行 acceptance.py 和最终独立回执。

纯合同测试使用不可解码的 metadata-only 文件与 mock 元数据，不能当成真实合成视频验收。首次媒体资源拒绝没有保留详细 reason，后续诊断采样正常，但不得把后续样本当成第一次失败原因。新版本 outlet 已补写 blocked.json 及 CLI 结构化拒绝详情。

实际语义理解、声学质量排序、相似但非完全重复语句的自动判别、真实听审均未实现/未验证。未修改上游 Mac 默认代码。未确认这一候选在 Windows 的实际像素、编码或音画边界质量。

回滚：停止使用四个新增子命令；撤回本轮新增 execution/edit_loop 模块、专用测试及 CLI 局部 diff 即可。保留输出证据和原 handoff、原片、所有无关脏改动，不执行 reset/stash。
