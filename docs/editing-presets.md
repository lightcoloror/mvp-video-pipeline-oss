# 创作者剪辑预设与执行边界

## 2026-10-28 01:04:11 | Codex（GPT-6，精确运行变体未独立核验）：连续缩放

人像、镜头、照片、图形等图层发生大小或位置变化时，默认连续过渡。普通素材之间的语义切镜仍由原 `transition` 合同控制；缩放由同一配置中的 `transitions.scale_motion` 控制。没有缩放目标的镜头保持静止。

共用实现：`scale_motion.build_scale_motion()` 和 `ffmpeg_scale_overlay()`。沿用 `scene_components/object-motion.ts` 的 cubic smoothstep；比例在对数空间插值，位置用同一进度移动，起止速度归零。复用本地 FFmpeg `vf_scale.c` 的 `eval=frame` 与 `vf_overlay.c` 的逐帧尺寸更新，单一真人图层，无新增动画库、编码器或云调用。

时长参考目标附近 ±4 秒的原生语音/动作 onset 间隔，三倍中位间隔作为候选；大倍率变化适当延长。0.4–1.4 秒是可调整的本地起始范围；缺少可靠时间证据时明确记为 0.7 秒策略回退。口播 onset 是节奏代理，不宣称检测了音乐节拍；最终速度仍需观看确认。相同姿态间隔 ≤0.8 秒保持姿态，不反复恢复全屏；邻接的不同姿态直接过渡。实际素材切点不自动变成人像缩放的硬切理由。

`audio_timeline` 的每条 `binding.segments[]` 可传下例。时间是**场景内相对秒**，几何是输出画幅中的像素；图层预先按输出画幅准备，再保持纵横比变换。缩小、放大、移至角落使用同一代码；字幕最后叠加，原声样本及场景时间不动。

```json
"scale_motion": {
  "targets": [{"start_s": 2, "end_s": 5, "rect": [90, 0, 360, 640], "anticipate": true}],
  "onsets_s": [0.1, 0.3, 0.5, 0.7, 0.9, 1.1]
}
```

`anticipate` 使局部演示出现前先让人像就位；前一布局持续到此时，面板应等本次 `settled_s` 后出现。瞬时缩放只接受显式 `mode: "cut"` 与非空 `reason`。目标过短、重叠、改变纵横比或没有平滑恢复空间时拒绝，交编辑复核，不暗中改成硬切。计划、政策和实现哈希进入原分段缓存键。

视觉节拍入口同样解析这一字段。当前 `semantic_render_plan` 的直切转换器尚不能执行该图层变换，会保留 `scale_motion_requires_audio_timeline_visual_binding` 阻断项；实际变换交给已有 `audio_timeline.compose` 或复用同一 helper 的项目适配器。不能只写字段便宣称所有渲染器已经接入。

| 意图 | 决策 | 理由 | 证据 | 生效范围 |
| --- | --- | --- | --- | --- |
| 让后续项目复用连续缩放 | 在原 editing-presets 增加 scale_motion，新增薄规划/表达式适配模块，接入 visual_defaults 与原音轨合成器 | 统一规则、缓存与执行，沿用成熟原生滤镜 | 本地 FFmpeg 源码 44d082edc87381d978e8588b148116b99fefdb43；FFmpeg 8.1 实跑 | 新建图层缩放目标；普通语义切镜、原声与字幕时钟不变 |
| 速度响应视频节奏 | 使用原生 onset 的局部中位间隔，并记录倍率、样本量与回退依据 | 全片统一固定时长无法适应不同口播与倍率 | 每事件 rhythm 字段；快慢节奏、端点速度、横竖画幅回归 | 有效时间证据的图层；音乐精确踩点须另提供已核对动作/节拍时钟 |
| 避免把未支持的参数当成已执行 | 直切转换器保留明确渲染绑定阻断项 | 防止另一入口忽略缩放计划 | semantic_render_plan 中的新阻断；既有相关回归 | 语义渲染计划薄转换器；不改旧工件验收状态 |
| 核实实际像素而非仅测试公式 | 使用同一 FFmpeg 出口渲染并解码 240 帧合成素材，另查看 RWE 11 处中间帧 | 变尺寸滤镜、帧同步和图层顺序均可能影响实片 | RWE exports/visual-preview-20261028_005654/qa/smooth-motion/verification.json；最大几何误差 2.36px | 几何连续性和本次实际预览；自然节奏仍待作者观看 |

## 2026-10-27 22:27:31：字幕合成预设共用

Codex（GPT-6，运行时具体变体未独立核验）：`subtitle_readability.subtitle_render_preset()` 现提供已有 `audio_timeline` 实际默认字号和边距，供薄适配器复用。540×960默认字号基线30、左右边距38、下边距86、底部居中；换行上限同时受可用宽度约束。实际字体/ASS名义字号与最终中文字形的差异仍需实渲染核对，不能只检查声明值。

| 意图 | 决策 | 理由 | 证据 | 生效范围 |
| --- | --- | --- | --- | --- |
| 防止项目适配器脱离既有字幕预设 | 从原合成器提取共享预设访问函数，原合成器与适配器共用 | RWE实际反馈暴露适配器写死27字号/36下边距，最低字号门通过仍不能证明默认位置正确或手机可读 | 14项字幕/音轨时钟回归；RWE名义30实测21px、按本机字体校准后实际30px的PNG与回执 | 预设访问方式；不改变原合成器数值或所有字体的全局字号 |

---

更新：2026-10-26（Asia/Shanghai）；工具：Codex。当前精确运行型号未独立核验。

配置真源是 `src/video_creation_pipeline/policies/editing-presets.v1.json`；气口使用 `spoken-pacing.v3.json`。本项目起始值可以随素材调整，不宣称适用于所有视频的行业标准。以下规则已经进入原有编译、音频、字幕或分镜入口；没有另建编辑器或 FFmpeg 执行器。

| 预设 | 已接入的行为 | 需要实际判断的部分 |
| --- | --- | --- |
| 重录与重复句 | 普通重录优先选择最后一遍作为候选。最后一遍明确不完整、有口误或质量问题时，优先比较其他有效版本；重复文本与 take_group 共用选择函数。删除须记录被比较区间、保留区间及完整性、意思、自然度、质量、独有信息检查；不允许把保留版本也删掉 | 最后一遍通常代表录制者满意，不等于每次都能无条件删除前面。质量评分与复核记录由实际审听/查看提供；代码不杜撰评分 |
| 首尾与反应 | 部分词截断、覆盖 `protected_events` 的删除被拒绝；编译要求首尾和删减边界检查。片头、片尾与内部切口生成待试听窗口 | 笑声、回应、呼吸、动作收尾必须标记；空数组不证明没有重要声音。检测未识别的事件仍须回听 |
| 音频切口 | 两条 FFmpeg 净剪入口应用安全余量内的 10ms 短淡入/淡出；最大 20ms、单侧不超过保留段四分之一。词/保护事件占据余量时不自动淡化；不重叠两遍人声、不移动时间轴 | 输出始终为待试听候选；短淡化不能保证解决所有环境底噪跳变。复杂环境声衔接需要专门处理 |
| 音量一致性 | 粗剪复核包含音量和削波；PCM 分段诊断报告 RMS dBFS、样本峰值、满刻度样本、音量差及增益候选。经过复核的逐保留段增益才进入滤镜；最终 mastering 仍测最终输出响度和真峰值 | RMS 不是 LUFS，样本峰值不是真峰值，满刻度计数不等于确认全部失真。推荐增益不自动应用，也不能修复已经削波的原声 |
| 降噪 | 默认 gentle；strong 需要绑定原声哈希的试听说明和“轻处理不足”记录。反馈含金属音、吞辅音、呼吸抽动时，strong 必须退到 gentle，gentle 必须退到 original；original 使用 anull。独立输出，保留原声 | 原声与处理版必须比较；原声回退仍可能重采样/封装，不宣称字节相同 |
| 配乐 | 复用现有关键内容窗口、语音侧链压低及密集短停顿慢释放；新增显式整段无配乐和局部 `no_music_windows`，无配乐不会暗中选另一首 | 静音窗口需有实际编辑理由；全无配乐命令不读取音乐输入。常规混音命令仅支持从时间轴 0 开始的单选择段；非零起点不能假装整片混音已实现 |
| 字幕 | 按标点、空格或显式语义断点排成 1–2 行；保留全部原文，保护指定术语、数字、否定词；超限要求拆成有真实时间依据的短 cue。复用现有字号基线，禁止缩小兜底。布局进入缓存和实际 ASS 合成 | 18 中文宽度单位/行、9 单位/秒、0.7 秒短显示提醒为本地预设；长句不能按字符数伪造词时间。还需手机实际大小验收 |
| 镜头与补充画面 | 视觉节拍必须有语义触发和信息推进；拒绝 `fixed_interval`、`keyword_only` 切镜依据。未解释的画面进入复核，传入渲染计划后保留阻断项 | 不自动判断补镜是否真实解释了旁白；主讲人持续出镜也是有效选择 |
| 转场与动效 | 默认 cut；其他转场必须有叙事理由；每场景内主要强调区间不得重叠，拒绝整句持续运动 | 当前渲染计划转换器只实现直接切换；非 cut/强调动作必须补渲染器绑定，不能把参数包称为已渲染效果 |
| 变速与同步 | 默认 1 倍；速度映射变化须绑定完整映射哈希的试听与含义复核。重新剪辑后字幕、音效、章节、进度条、审阅五类状态全部失效为 pending；旧哈希不能继续验证。再次变速复核在渲染前检查 | 自动投影只是候选，不能宣布字幕与声音语义同步已通过。各类可明确标记 not_used，但发生时间轴变化仍需重新决定 |

## 常用入口

```powershell
python scripts/spoken_rough_cut.py editing-policy
python scripts/spoken_rough_cut.py pacing-policy
python scripts/spoken_rough_cut.py inspect-audio-levels --audio speech.wav --windows speech-windows.json --output levels.json
```

`speech-windows.json` 格式为 `{"speech_windows":[{"start":0.2,"end":2.5}]}`。区间由实际说话片段确定，诊断以小块读取，不将整段采样展开到内存。`levels.json` 绑定源音频哈希；尚不提取视频音轨，使用现有 FFmpeg 出口准备 PCM。

主净剪入口沿用 `prepare → decision-template → 实际复核 → compile → select-delivery`。模板新增 `protected_events`、`boundary_review`、`rough_audio_review`、`clip_gains`。模板的复核值默认 false，不能直接复制文档或合成测试的 true 当成真实审听证据。编译时先完成粗剪音量判断，渲染后仍须试听切口，包装混音后还要最终响度检查。

独立 worktree 或其他目录布局可通过 `MVP_VIDEO_WORKSPACE` 指定包含 `self-media-creation-system` 和 `video-knowledge-pipeline` 的工作区。未设置时沿用仓库上两级目录；源素材审阅仍复用已有消费者，找不到依赖时报错，不自动转为导出视频。

`clip_gains` 为空表示没有已批准的增益修正；非空时必须按顺序覆盖每一保留区间，含 `start/end/gain_db/audio_checked/expression_preserved/evidence`，目前范围 ±12dB。不要把源区间填成成片区间。FFmpeg 路由会应用增益和安全淡化；剪映/Premiere 工程路由仍需在编辑器中核对相应音频处理，交付准备结果会明示此缺口。

Mastering 创建入口不再默套一个响度值。显式选 `--delivery-profile web_spoken_candidate`（本地 -16 LUFS 起点）、`--delivery-profile ebu_r128`（-23 LUFS）或 `--target-lufs <自定义数值>`。命名配置与不相符的显式目标会报错。现有两遍 loudnorm 后继续复测最终输出；限制器关闭自动增益提升，技术合格不代表听感验收。

`speech_cleanup` 创建入口支持 `--processing-review <json>` 与 `--audition-feedback <json>`；复核必须指向这份原声的 SHA-256。强处理未复核、退化反馈与请求强度冲突时会拒绝创建计划。

配乐 intent 可设 `music_mode: "none"` 与 `music_mode_reason`；常规音乐段可补 `no_music_windows: [{start_s,end_s,reason}]`（主时间轴绝对秒）。切换到无配乐之后仍要重建绑定该 intent 的候选集与计划；不会复用旧音乐选择。局部静音采用 0.1 秒外侧过渡、音频帧级包络，关键声音边界应留余量并试听；不要声称样本级精确静音。

语义渲染计划入口新增 `--visual-beats <beats.json>`。绑定的节拍必须来自同一 packaging 的同一时间轴；缺语义、未实现的转场/强调继续留在 blockers，不伪造执行验收。只做原声硬切、不使用补镜时可以不传节拍文件。

## 版本与证据

2026-10-26 11:47:01 | Codex（精确运行型号未独立核验）：新增 [原片优先的生产预设](production-presets.md)，包括源文件精确审阅、低价值大体积素材副本例外、色彩解释、构图、修复回退、J/L 与底声、声道、目标导出和实际文件检查。检查阶段默认只生成审阅数据与页面。

- v1/v2 气口包保留原含义并可读取；当前新建/编译采用 v3，不把旧包重标为新版。v3 沿用 v2 气口数值，只更新重录策略并要求新编辑检查。
- 强降噪旧计划若没有试听记录，不能继续执行；原文件不覆盖，补证据后创建新计划。其他旧工件的既有哈希也不伪装成通过新增检查。
- 语义复核字段是审阅记录，不是机器证明“已经听过”。检测、自动技术测试、实际观看/试听分开记录。
- 定向测试：`test_editing_presets`、`test_editing_preset_integration`、原有 spoken/audio/music/semantic 测试。合成波形用于验证时长、切口、静音区、削波检测和原文件不变，不能验证真实人声自然度。
- 本次实现证据目录：`<workspace>/outputs/editing-presets-20261026/`。未处理真实素材；推送状态独立记录在该目录的 `push-result.json`，实现测试不代表已上传。

参考原则：[Adobe 音频淡化](https://helpx.adobe.com/premiere/desktop/add-audio-effects/apply-audio-transitions/audio-crossfade-transitions.html)、[W3C 字幕](https://www.w3.org/WAI/media/av/captions/)、[EBU R128](https://tech.ebu.ch/publications/r128)。数字预设与这些通用原则应区分；气口依据见 [创作者口播气口预设](spoken-pacing-policy.md)。
