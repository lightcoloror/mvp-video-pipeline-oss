# 原片优先的生产预设

更新：2026-10-26 11:59:29（Asia/Shanghai） | Codex（精确运行型号未独立核验）。

意图：在既有剪辑预设上接入素材检查、色彩、构图、修复、声音衔接及导出验收，减少中间媒体。规则真源为 `policies/production-presets.v1.json`；原有十类剪辑规则继续使用 `editing-presets.v1.json`。实现复用源文件哈希、VKP `targeted-review.html`、智能裁切、调色和单一受控 FFmpeg 出口。

## 默认审阅原片

```powershell
python scripts/spoken_rough_cut.py production-policy
python scripts/spoken_rough_cut.py inspect-source --source-video original.mp4 --project-type life --checks checks.json --output-dir review-new
```

`checks.json` 示例：

```json
{"ranges":[{"start":12.4,"end":18.8,"checks":["technical","color","composition","audio"]}]}
```

检查名还包括 `restoration`、`split_edit`、`delivery`。每项进入原片精确时间区间和待判断内容，生成 JSON 与既有 HTML 审阅页，不转码、不拼接、不生成代理、抽帧图或音频。没有 `--checks` 时只给片头、中部、片尾的代表性短区间，明确标记为抽查，不能当成全片已检查。`delivery` 同时生成正常观看和静音观看任务，并要求检查手机实际显示大小。

默认读取 ffprobe 元数据：平均/标称帧率、旋转、音轨、采样率、声道、色彩。两种帧率不相等只是待查提示，不能直接证明 VFR。需要时加 `--sample-cadence`，最多在三个位置各读取 120 个视频 packet 的 PTS，不解码、不生成媒体。按显示时间排序以排除 B 帧存储顺序影响；抽样没有变动不证明全片 CFR，变动也仍要排查时间戳异常。

## 粗剪素材副本是例外

`assess-working-copy --source-video original.mp4 --assessment assessment.json --output decision.json` 只做评估。评估必须绑定原片 artifact reference，包含 `full_source_checked`、`useful_content_accounted`、`evidence`、有序不重叠的 `useful_ranges`（start/end）和 `estimated_output_bytes`。

本地起始阈值为：有用内容不超过原长 25%，预计体积减少至少 70% 且至少 512 MiB。三者同时满足才给出可生成候选副本的结论；这是可调整的本地策略，不是行业标准。没有完整内容判断、信息价值不稀疏、节省不明显时继续原片审阅。

`source_delivery_request.production.output_purpose = working_copy` 时，既有 `render-delivery` 在创建媒体前重新核对该评估、源哈希及实际保留区间。未达到条件拒绝渲染。评估通过不会自动执行。原片仍在时，本地占用增加的是副本体积；`immediate_reclaimed_bytes` 始终为 0。未来归档替换可能减少的体积另列，删除原片永远不由此评估授权。

生成后还按候选文件真实大小复核相同阈值；估算过于乐观、实际节省不足时报告未满足例外条件，保留原片和失败候选证据，不自动删除或把结果标为成功。

明确要求制作交付候选的视频使用 `final_candidate`；它与为了方便检查而额外生成素材副本不同。审阅入口没有任何导出降级路径。

## 八类规则的实际入口

| 类别 | 已执行的规则 | 验收边界 |
|---|---|---|
| 素材技术 | `inspect-source`、可选 packet 抽样、原片时间区间审阅 | 不自动统一帧率，不插帧，不因为浏览器不支持就转码 |
| 色彩与匹配 | 调色 build/validate 与口播 render 共用源色彩检查；识别到 HDR/Log/广色域时拒绝进入现有 SDR 8-bit 路径；未知解释留 pending；源片区间要求核对曝光、白平衡、肤色、白色、高光和相邻镜头 | 没有新增 HDR/Log 转换器；应使用经验证的转换路径后重新建计划。未知色彩不能冒充 BT.709；编码器缺失标记时仅对已确认 BT.709 的源保留相同 VUI 标签 |
| 构图裁切 | 既有 `smart_reframe create-request` 新增 `--project-type` 与 `--composition-review`，核对所需眼睛/手/关键物件/操作区检查及 unresolved_issues，绑定原片和审阅文件；既有必需区域保留、主体选择、稳定路径、安全区继续生效 | 配置项目类型必须提供相应审阅；布尔字段是人工记录，不是自动识别人脸或手部。裁切成片仍需查看 |
| 防抖/增强 | 默认关闭；`review-treatment` 接收 source/candidate、operation、start/end 和逐项比较；眼睛/牙齿/文字变化、闪烁、扭曲或过度裁切选择原片回退；防抖放大超过 1.15 也回退 | 这里只消费已存在候选，不安装/启动模型。`production.restoration_review` 接入实际口播导出，拒绝使用已判退化的候选；不自动渲染一个回退副本 |
| J/L 声画切换 | `production.audio_edit` 进入真实滤镜：声音沿原剪辑时钟，移动相邻视频入出点；总时长不变，检查源余量、上下文和真实回应 | 仅支持当前单源净剪时间线的相邻片段；不跨多个源自动构造对话。正偏移是声音先切的 J-cut，负偏移是画面先切的 L-cut |
| 底声与声道 | PCM 检查新增每声道 RMS、单声道相消和声道不平衡提示；不自动混单声道。多个独立音轨必须明确选择并确认所有说话者均已涵盖。同原片房间底声可按审核后的成片区间叠入，有限淡化、明确增益、无循环/时间拉伸 | 底声源必须同场景、无对白或反应，目标须已核对；检测本身不证明“这是同一房间”或“音轨只含一个人” |
| 目标导出 | `source_sdr_h264` 保留原画幅和时间戳；`web_sdr_1080` 要求已确认 BT.709，按横/竖方向限尺寸且不放大；H.264/AAC/48kHz，保留声道，不自动 30fps | 这是本地通用配置，不声称等于每个平台最新规范；HDR 交付另走已验证路径 |
| 最终验收 | 已有 render 对实际文件核对时长/音轨/编码/像素格式/规格，向 null 解码并记录黑场/冻结检测；正常速度、手机、静音、同步状态单列 pending | 黑场/静帧只是待核项，不自动删除；缺字段记 unverified，渲染候选不等于发布或人工验收通过 |

## 配置与声音编辑

既有 `spoken_delivery_request.v1` 可增加：

```json
{"production":{"project_type":"spoken","output_purpose":"final_candidate","export_profile":"source_sdr_h264"}}
```

`production.audio_edit`、`working_copy_assessment`、`restoration_review` 都是通过 `contract_io.artifact_reference` 生成的文件引用，不是裸路径。修改引用文件会使既有选择失效，须重新建立交付选择。

声音编辑文件包含 `source`、`source_plan_sha256`、可选 `audio_stream_ordinal`（音频流内从 0 编号）、`audio_stream_review`、`split_edits`、`room_tone`。不得复制合成测试里的审阅 true 作为实际证据。

- `split_edits`：`boundary_after_segment` 从 0 编号，`video_cut_offset_s` 为声画切点差；每项要求 `context_checked`、`chronology_preserved`、`reaction_not_fabricated`、`visual_handles_checked` 与 `evidence`。实际画面余量不足或片段非正时长会拒绝。
- `room_tone`：`start/end` 为成片时间，`source_range.start/end` 为原片底声时间，源段不得短于使用时长；`gain_db` 在 -60 到 0 之间，且要求 `same_scene`、`contains_no_speech_or_reaction`、`target_non_dialogue_checked`、`auditioned` 与 `evidence`。不得将原对白重复垫入，不能借此制造回应。
- 既有 NLE 路线不会自动得到这些 FFmpeg 处理；带声音处理配置的编辑器/纯审阅请求会被拒绝，不能把配置包当作编辑器已经实现。

项目类型只决定检查重点：口播偏表达/声音/可读性；生活偏反应/环境声/动作收尾；教程偏操作/步骤/结果。配置不改变原话、速度或素材选择。

修复比较记录中的 `start/end` 只覆盖被比较区间；`review-treatment` 不把一次小样比较标成整条候选通过。实际采用修复素材的口播导出要求该区间覆盖时间线真正使用的全部源区间，否则先回到对应原片区间审阅。

## 验证与回滚

公开版验收结果须依据本次独立测试与回执；原本机历史路径和项目结果不随源码分发。

改动前快照位于证据目录 `before/`；回滚仅恢复本次文件差异，保留已有未提交工作。当前任务不自动提交、推送、清理或上传媒体。
