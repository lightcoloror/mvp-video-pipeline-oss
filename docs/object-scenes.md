# 对象场景：二维关系与轻量三维阶段

更新：2026-10-01，Codex（GPT-6；精确子型号未暴露）。

此入口扩展既有 `video-semantic-editorial` 和 Remotion 工程生成器。内容、录音、词级证据、字幕、音效、FFmpeg、QA 与确认记录继续使用原流程。首轮内置 `relation_flow_2d` 与 `stage_timeline_3d`，两者均属于 `remotion_knowledge_animation`。原件证据使用 `hyperframes_evidence_focus`。

## 创作及构建

中央路由卡仍为唯一入口，`visual_role_map_ref` 引用对象场景 spec／resolved plan。先写镜头的理解目标、对象、关系、初末状态和动作，再绑定原速录音中的词。不要先选特效再找句子配它。

```powershell
video-semantic-editorial object-scene-route --visual-job stages --needs-depth
video-semantic-editorial object-scene-components
video-semantic-editorial object-scenes --spec <object-spec.json> `
  --script-approval <existing-script-approval.json> `
  --voice-approval <existing-voice-approval.json> `
  --storyboard-approval <existing-storyboard-approval.json> --output <object-scene-plan.json>
video-semantic-editorial object-scenes-validate <object-scene-plan.json>
```

源码 checkout 可用 `python scripts/semantic_editorial.py` 执行同一命令。`video-remotion-skill build --spec <remotion-build-spec.json> --output <handoff.json>` 的现有 spec 增加可选字段 `object_scene_plan_path`。不提供该字段时沿用旧工程文件及依赖。

再用原 `video-remotion-skill materialize / doctor / run / validate-run`、FFmpeg outlet 和 QA。依赖只在物化工程安装。Remotion 保持 `4.0.477`、React/React DOM `19.2.3`；3D 增加 `@remotion/three 4.0.477`、Fiber `9.2.0`、Three.js `0.178.0`、`@types/three 0.170.0`。3D manifest 绑定原固定 Remotion checkout 的 Three 包与源文件；运行回执绑定安装包及 lockfile。

## 输入及时间合同

`video_creation_pipeline.object_scene_spec.v1` 复用 author binding 的源字段：`source_visual_production_plan`、`source_recording`、`source_word_alignment`、`source_packaging_plan`、`scene_id`、`source_media_role: creator_narration`、`playback_rate: 1`、录音起止与输出尺寸。每个源引用由现有 `artifact_reference` 生成，不能手写伪哈希。WAV 必须完整；实测模式只接受 `measured_word`，不能从句长插值。

`component_sources` 来自 `component_sources([...component_id])` 或 components 入口。`visual_tokens` 明确提供纸白、前景、主色、强调色、辅文及本地字体栈；不套入 Onda 的品牌皮肤。输出首轮限制为 1080×1920，默认 30fps。

每个场景具有 `id`、`component_id`、`understanding_target`、`visual_role: illustration|metaphor`、`relationship`、`initial_state`、`final_state`、`camera_purpose`、`headline`、`objects`、`region`、`events` 与 `temporal_expression`。标题、对象标签与金额须能追溯到冻结旁白的准确摘句；证据引用属于同一内容单元。金额额外声明 `amount_role: actual_payment|invoice_item|claim_payment`。三种口径不能互换。

对象通过稳定 `id` 跨镜头保留身份；`initial_split` 与 `initial_active_ids` 用来接续前一镜头已展开的阶段，避免下一镜头重做一次同样的动作。原二维关系组件支持 2–4 个对象；3/4 个对象须直接使用展开布局。`stage_timeline_3d` 支持两个阶段。标签长度按手机版式限制为 8/6/4 字；不是通过无限缩小字体容纳更多内容。

## 连续过程组件（2026-10-04，Codex／GPT-6）

新增 `queue_flow_2d` 与 `slider_crank_3d`。它们使用同一 authoring spec、词锚点解析器、路由、Remotion 生成器、依赖锁定及 FFmpeg／QA；原有两种组件的源码与生成行为保留。适用于包裹排队分流和活塞曲柄联动这两个明确机制，不是任意题材的自动万能动画。

`video-semantic-editorial object-scene-route --visual-job queue` 与 `--visual-job mechanism --needs-depth` 分别选择这两种组件；中央路由卡引用对应 spec／plan，渲染路线仍为 `remotion_knowledge_animation`。

每个新场景需要 `parameters`。参数、解析后的动作与 Python 预计算模型源码均纳入来源哈希；生成项目中的 `object_process_models.py` 是溯源副本，不是新服务或 JavaScript 运行时。必须修改创作源后生成新版本，不能直接改生成 TSX 或重写 manifest 来绕过检查。

### 对象材质与状态用色（2026-10-04）

执行者：Codex（GPT-6）。按本次用户修订，解释场景优先服从对象的材质、实际使用情境和叙事含义，不要求沿用个人 IP 配色。标题、字幕的 `visual_tokens` 与对象的 `parameters.colors` 分开；字体、版式可以保持一致，纸箱、传送带、金属零件及状态提示不自动染成品牌主色。

`queue_flow_2d` 默认采用牛皮纸箱、灰色传送带和支架，琥珀色表示等待、绿色表示出口开启、红色表示出口未开。状态同时由数字、文字和闸门位置表达，颜色不单独承担信息。`slider_crank_3d` 默认采用银灰活塞、钢色连杆、深灰外壳；蓝色用于辨认相连的旋转件，黄褐色用于剖切边界。机械配色是机制图解的材质及辨识约定，不宣称复制某台实物。

在新场景的 `parameters.colors` 中覆盖需要修改的角色即可，例如 `{"parcel":"#BB8844","belt":"#626D78"}`。省略该字段仍兼容原创作 spec，生成器注入对应题材的默认色；提供时接受同组件已有角色的六位十六进制色值，拒绝未知角色或含糊色值。全部默认角色在 `object_process_models.PROCESS_COLORS` 中；resolved plan 的 `process.colors` 保存完整用色，同原来源校验和重新生成链一起生效。

其他主题也先确定每种颜色指代的对象、状态或类别，再选择背景与强调色。跨镜头保留同一对象的颜色和图例；不要为了统一 IP 改变常见物体的识别色，也不要随镜头重新分配“颜色代表什么”。

| 组件 | 固定对象角色 | 动作 | 主要参数 |
| --- | --- | --- | --- |
| `queue_flow_2d` | 包裹、第一出口、第二出口，共三个角色 | `accelerate`、`open_exit`、`focus` | `parcel_count`、`slow_arrival_frames`、`fast_arrival_frames`、`service_frames`、`approach_frames`、`exit_travel_frames`、`fork_headway_frames`；速度及开闸事件 ID；三个对象 ID |
| `slider_crank_3d` | 活塞、连杆、曲柄、外壳，共四个角色 | `cutaway`、`orient`、`focus` | `crank_radius`、`rod_length`、`piston_radius`、`housing_radius`、`cycle_frames`、`initial_angle`；剖切及视角事件 ID；四个对象 ID |

动作窗口仍用 `at_for / until_for / span`。`until_for` 的末帧绑定动作完成点；速度变化、开闸、剖切和镜头方向由解析后的事件控制。连续运动由固定帧参数计算，未引入实时循环、随机积累、新的 ASR 或 TTS。

排队模型为有限、合成的离散事件。每个包裹有稳定编号、入场、准备、放行与离场帧，两个出口分别有服务能力；共用分流口的最小间隔同时约束调度与画面推进，防止开第二出口后包裹跳位。三维机构固定连杆长度，活塞限制在同一轴线上；前侧外壳剖切后，原部件继续运动，读连接关系时视角稳定。参数验证拒绝未知字段、角色不匹配、越界设计与不可读的队列规模。

本轮样片目录：`<workspace>/artifacts/motion-case-previews-20261004-v1/`。口播为原创草稿；当前只有明确标注的无声视觉节拍预览，供比较过程与空间表达。其 supplied timing／fixture attestations 不等于实际录音、声学同步或人工审批。实际使用时替换为原流程的录音、实测词级证据及已有确认记录，再生成新版本。

动作包含 `id`、`action`、`target_id` 和同一套时间表达式。`connect` 描画关系，`reveal`/`advance` 激活对象，`split` 改变关系布局，`focus` 改变当前强调对象；3D 的视角展开只由 `advance` 的时间窗控制，保持段不继续运动。

```json
{
  "id": "stage-split",
  "action": "split",
  "target_id": "stage-a",
  "temporal_expression": {
    "kind": "until_for",
    "anchor": {"word_id": "word-two-payments", "boundary": "end", "offset": {"value": 0, "unit": "seconds"}},
    "duration": {"value": 0.8, "unit": "seconds"}
  }
}
```

该例只展示表达式语法，不是本案估时。实际词 ID 和时间来自实测证据。`until_for` 在关键词边界完成，`at_for` 从边界开始，`span` 跨两个词锚点；都调用既有解析器。动作结束后至少保留一个可见帧，实际读图应留更长保持段。resolved plan 存储源表达式、解析窗口和 payoff frame，时间只投影进原 packaging，不重写其边界。

## 修改与复现

改参数或模板后先更新创作源及源引用，再用原分镜确认机制重新绑定、生成新版本目录。录音、脚本与词证据没有变化时复用对应确认，不增加审批台账。手改生成 TSX、然后重算 manifest 哈希，仍会被重新生成内容比对拒绝。

对象区域当前限制在 x≥0.09、x+width≤0.78、y≥0.18、y+height≤0.64，预留字幕、说明及右侧界面区域。v1 对象镜头为整幅解释画面；真人画中画叠加与具体人像避让尚须真实材料验证，不能把无人物测试写成已经通过。所有状态是当前 frame 的函数，没有累计式实时循环或未固定随机数。

## 主题与理解任务默认选择

| 主题/任务 | 镜头理解任务 | 默认路线及条件 |
|---|---|---|
| 医学、科学机制 | 因果、流动、阻塞、条件改变 | `causality` / `flow` → 二维对象；保持因果连续 |
| 人体、器械、工业结构 | 遮挡、剖面、空间 | `space --needs-depth` → 原 Remotion 路线中的 Three 场景设计；v1 两阶段组件不能代替人体建模 |
| 付款、办理步骤 | 阶段及顺序 | `stages` → 二维；确实需要空间表达时 `--needs-depth` → 三维阶段 |
| 条款、政策、原文 | 证据、条件与对比 | 原文 `evidence` → HyperFrames；条件关系另用二维对象 |
| 数据、成本、概率 | 数量、统一标尺、对比 | `quantity` / `comparison` → 已有 Remotion 图表场景；不把所有数值塞进节点图 |
| 病例、客户与理赔经历 | 人物、材料、关键时间线 | `person_or_emotion` → 原素材；只在需要解释的阶段插入动效 |
| 软件、AI 工具教学 | 真界面、焦点与流程 | `interface` → 原录屏；流程插入 `flow`，不伪造软件界面 |
| 历史、社会议题 | 材料、关系与时间 | 原素材与证据聚焦；关系镜头使用二维 |
| 生活常识、轻松解释 | 可辨识对象与操作 | 二维对象；剪纸/角色属于场景设计，不是默认科技风 |
| 个人观点与经验 | 人物表达、关键关系 | 原口播为主，选择性插入 `abstract_relationship` |
| 抽象概念 | 一致的边界、容器、路径隐喻 | `abstract_relationship` → 连续二维；隐喻保留说明 |
| 宣传、品牌 | 排版、品牌物体、音乐节奏 | 原 Remotion/HyperFrames 路由；不能以音乐节拍替代解释片的口播锚点 |

未覆盖的主题按“这一镜头要让人理解什么”选择，不新增主题关键词皮肤。仅明确缺少能力时提供 `--capability-gap complex_model_or_material`（Blender）、`continuous_2d_deformation`（Motion Canvas）或 `precise_geometry_or_curve`（Manim）交接建议；该命令不会安装、启动或重复搭建导出流程。

## 本轮验证与限制

首轮两个组件已在固定环境完成静音合成测试的真实 1080×1920/30fps 渲染，并进入原 FFmpeg、QA。测试不是质子案例验收、原型重建基线或真人对比片。实际代码/工程/回执及最新状态见工作区 `artifacts/motion-workflow-implementation-20261001/README.md`。

质子案例已定位 D 盘 04B 创作者精剪，851 帧、28.367 秒，哈希与历史回执相符；原速 PCM 已提取并按实测视频长度保留 100 毫秒静音尾段，原有效采样完全一致。冻结稿实际缴费措辞是“八万多”和“二十一万八千多”。E 盘归档读取出现硬件错误。实测词级证据及该精确片段的声音/分镜绑定尚缺，不能用合成测试的锚点或静音 WAV 替代真实验收。完整播放与真人避让也尚待验证。保留源文件、旧工程与旧片，不上传发布，不写知识库。

意图：让关系和阶段实际变化，减轻静态卡片重复。决策：扩展原作者时间基准和 Remotion 生成器，复用 Onda easing 与官方 Three 适配器。理由：保留一条时间线及一条渲染/合成链。证据：源绑定、回归、真实合成测试与随机 seek 回执。生效范围：本地候选制作；不继承为成片认可或发布授权。回滚：不传 `object_scene_plan_path` 即回到原生成器行为，旧素材不受影响。
