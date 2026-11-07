# 口播净剪按交付需求选择路径

更新时间：2026-10-26 08:33:27；执行：Codex（运行模型未独立核验）。

本页是现有 `video-spoken-rough-cut` 的交付适配，不是另一套工作流。compile 只决定删留，生成 `spoken_edit_plan.v2`，下一步固定为 `select_delivery_from_request`。`v1` 仍可读取，但其旧剪映 next_action 只作历史证据，不能直接执行。

## AI 的默认选择

| 用户本次所需交付 | 请求字段 | 实际消费者 |
|---|---|---|
| 审核净剪配方 | `deliverable: review` | 自媒体 `video_review_delivery.prepare` → VKP 精确审核页；跳转同一原片，不导出媒体 |
| 生成净剪视频 | `deliverable: video` | 原声片段按配方合成，使用既有 single_ffmpeg_outlet、生命周期预检、资源守卫和解码 QA |
| 可编辑剪映工程 | `deliverable: editable_project, editor: jianying` | 原 VectCut 任务与 materialize / materialize-project；不自动假定工程已可用 |
| 可编辑 Premiere 时间线 | `deliverable: editable_project, editor: premiere` | 原 Premiere import job、文件队列合同与实际导入回执校验 |

不能因机器装了剪映、旧计划写着剪映或其他消费者暂不可用就选择剪映。需要可编辑工程但编辑器未知时，先恢复用户现有选择；仍未知则标记缺失信息，不默认一个编辑器。审核和视频请求不能携带 editor。缺依赖明确失败，不暗中降级或回退。

净剪视频不是字幕、BGM、动画和最终发布均已完成的成片。复杂包装继续用既有 HyperFrames、表达型字幕等后续组件；`rough_cut_rendered_pending_review`、`packaging_complete=false` 不能改称最终交付通过。

## 稳定入口

以下命令均在本仓库执行，CLI 也可使用已有 `video-spoken-rough-cut` 名称。

```json
{
  "schema": "video_creation_pipeline.spoken_delivery_request.v1",
  "deliverable": "review",
  "selection_basis": "用户本次要求审核已确认删留配方，不要求视频导出"
}
```

```text
python scripts/spoken_rough_cut.py select-delivery --plan <spoken-edit-plan.json> --request <delivery-request.json> --output <new-selection.json>
python scripts/spoken_rough_cut.py prepare-delivery --selection <selection.json> --output-dir <new-delivery-dir>
```

选择与准备步骤不会渲染视频。审核准备直接生成现有 HTML、JSON 和待审决定；FFmpeg 准备只生成时间映射和显式导出命令。剪映准备需同时给 `--vectcut-source --width --height --fps`；直接 `video-jianying-draft build-spoken` 也必须带 `--delivery-request`，验证 editable_project / jianying 后才能进入 serializer。旧无交付请求的剪映任务需重建，不改写历史文件。

Premiere 准备可给 `--project-id --sequence-id --playhead`。缺目标身份返回 `awaiting_target_identity`，有身份仅表示任务可用于原导入器。不会自动投递文件队列、打开软件、写入用户时间线，也不声称桌面插件在线。工程生成、实际导入与可编辑验收仍分别留证。

仅本次明确要求实际视频时，调用：

```text
python scripts/spoken_rough_cut.py render-delivery --selection <video-selection.json> --output-dir <new-render-dir> --project-root <existing-project-root> --lifecycle-manifest <manifest.json> --estimated-output-bytes <positive-bytes>
```

必须是 video 选择；审核选择即使传入渲染参数也被拒绝。新输出目录不可覆盖既有结果，必须在项目根内。导出前重验计划、请求、原片和生命周期文件，资源门不通过就停止。保留原声、源片到目标时间映射、原始配方和出口回执；解码、音轨存在及总时长通过后仍待真实听看。

## 入口与校验

自媒体 `resolve` 支持当前 `spoken_edit_plan`、`spoken_delivery_request` 引用。`rough_cut_review / rough_cut_video / editable_project` 直接选择本入口，已有口播计划的 `finished_video` 先完成净剪交付适配，再做包装及最终检查。无需重新选择创作流程。

若已有对应审核请求、最终交付清单，或当前明确要求技术返工，保留原来的阶段入口；不能因为历史口播配方仍在当前引用中，就重建审核任务或重新净剪。裸“继续”仍优先恢复已保存进度。

请求 schema 拒绝无意义 editor、未知字段和空理由；选择绑定请求与配方哈希，并重新推导路由。重新计算一个被篡改选择的哈希不能改变路由。v2 计划恢复旧剪映 next_action 会失败。各执行入口重新验证选路，剪映和 Premiere 不能消费别的交付请求。JSON 声明不能独立证明原生用户授权，Agent 仍必须忠实映射用户要求。

改动范围只包含交付选择、现有消费者薄适配与相关回归；不改变口播语义判断、停顿策略、原素材或既有审核决定。
