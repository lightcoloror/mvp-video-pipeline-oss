# PhotoCraft 分层素材修订合同

更新时间：2026-10-11 18:36:58 +0800；执行者：Codex（GPT-6 Astra，ultra；本次原生派发指定）。

用于现有 image shot unit 的封面或辅助素材人工修订。沿用 `creative_tool_handoff.py`、
`contract_io`、`generation_contracts.validate_generation_receipt` 和既有技术 QA。
这是人/Agent 可读合同，不是 PhotoCraft 原生工程，不授予模型、软件、上传或发布权限。

## 输入与出口

```powershell
python -B scripts/creative_tool_handoff.py revision-plan --handoff <photocraft-handoff.json> --brief <revision-brief.json> --output <new-plan.json>
python -B scripts/creative_tool_handoff.py revision-validate --input <revision-plan-or-return.json>
```

先由原来的 `build --tool photocraft` 生成交接；不另建入口。Plan 绑定交接、原镜头、
修改 brief 及原可编辑素材的字节哈希。源素材变化即拒绝使用旧计划，输出不能覆盖输入。

Brief 的 schema 为 `video_creation_pipeline.photocraft_revision_brief.v1`：

| 字段 | 必须明确的内容 |
| --- | --- |
| source_kind、editable_source | 合成/公开/用户授权来源及原分层文件引用；非合成材料还需引用材料授权记录。引用存在不代表工具替用户判定授权有效。 |
| canvas | 与 shot unit 相同的宽、高及色彩空间。 |
| layers | 逐层稳定 ID、从下到上的 order、parent_id、类型、可见性、0—1 透明度、混合模式、蒙版及效果；不支持的效果仍列出。 |
| text layer.font | 原中文文本、字体 family/style、available/missing/unverified；substitution_allowed 必须为 false。缺字/替换先报告，不能静默改字体。 |
| color_role | 每层是物件色、语义编码、品牌注释或版式色及原因。保留物件、照片、证据原色；品牌色只用于独立注释和结构。 |
| crop | preserve_canvas 或 explicit_crop、整数像素矩形及理由。不得以比例转换默许裁掉证据。 |
| export | PNG、preserve/opaque 的 alpha 策略、keep_editable_source=true。原工程、可编辑返回件、扁平预览是三个不同路径。 |
| requested_change | 具体允许的人工作业，如只移动注释层；不写“随意优化”。 |

Return 的 schema 为 `video_creation_pipeline.photocraft_revision_return.v1`，包含
source_plan、scene_id、editable_result、preview、change_notes、software_trial、manual_media、
status=candidate_only、publish_approved=false 和 return_sha256。
五类 loss_report 必须全部填写，每类记录 preserved/changed/unsupported/unverified、
before、after、detail 及 evidence 引用；“没有报告”不能等于“无损”。

| 损失类别 | 软件试验时逐项比较 |
| --- | --- |
| layers | ID/层序/分组、文本可编辑性、蒙版、混合模式、效果；合并、栅格化、缺层或不支持功能逐项列出。 |
| fonts | 实际字体与字重、中文缺字、行折、字距；不得用 PSD 可解析推定字体与排版相同。 |
| transparency | 图层 opacity、蒙版边缘、半透明像素、合成背景、导出 alpha；报告白边或透明变实底。 |
| crop | 原画布及导出尺寸、裁剪像素、对象/证据是否被截断；各比例独立确认。 |
| export | 可编辑格式/版本、PNG 位深、色彩空间与配置文件、分层/效果损失及平台限制；扁平 PNG 不代替可编辑源。 |

未运行软件时软件状态必须是 blocked/unverified，五类损失全部保持 unverified。
reported_executed 需提供 open/edit/save/reopen/export 五步证据引用；校验器仅确认绑定和
报告结构，始终返回 runtime_verified=false，不能证明报告中的编辑行为或视觉保真。

manual_media 可暂为空；一旦填入，必须绑定当前 shot unit 的 manual image generation task
和确切预览的 generation receipt。调用原有验证器检查技术/视觉回执，不能用本合同替换
原素材与分镜审批。软件未验证或任何损失非 preserved 时，can_enter_existing_qa=false。
即使具备报告证据并可进入 QA，也仍是候选；当前技术 QA、人工视觉复核与发布批准各自保留。
发生有意损失时先退回人工讨论并更新源计划，不靠改一个布尔字段放行。

## 本轮验证与缺口

本轮产物位于 `<workspace>/outputs/selective-followup-20261011/design/`。
`synthetic/revision-brief.json` 是可复用示例；来源为合成 SVG 与程序化 PNG，后者仅为颜色/
alpha 合同样本，不是 PhotoCraft 导出或中文排版截图。`revision-return-unverified.json`
演示完整但不放行的损失报告。

固定源只覆盖少量 Rust 文本，`crates/psd/src/lib.rs` 的不修改文件字节往返声明，不能
证明编辑后保留 Photoshop 效果。本机限定查询未发现 PhotoCraft 可执行程序或登记安装，
也没有完整构建树；未下载、构建或启动。字体、蒙版、图层顺序、保存重开和导出仍未实测。

恢复条件：提供已安装且来源可核对的可执行入口，或另行授权安装/构建；随后在单独批准
的资源预算内，只用合成短文档执行上述五步。没有这些条件就保留 blocked，不重试联网。

## 意图、范围与回滚

把既有 PhotoCraft 请求细化为可审计的分层素材修订及返回约束，不接管视频流水线。
没有新编辑器、渲染器、主题系统或自动发布。回滚仅移除本次新增模块、测试、文档和
现有 CLI 的两个子命令，并按本次 before/after 哈希确认没有后续修改；禁止整仓 reset。
