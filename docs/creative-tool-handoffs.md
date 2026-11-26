# Shaders / FilmCraft / PhotoCraft 可选交接

2026-10-11 | Codex

接入层级：**已有镜头合同到人工/Agent 试用任务的薄适配**。不是三个软件的安装器、启动器或原生工程导出器。`runtime_verified=false`，不新增 Provider、不注册第二个渲染器。

## 何时使用

- Shaders：已经明确需要某种局部纹理、变形或转场，并且静态构图已经认可。
- FilmCraft：希望在 UI 里人工修改一个短镜头或短时间线，再交回现有生产流程。
- PhotoCraft：希望人工修订分层图像、封面或单张辅助素材；输入须为 image 镜头。
- 已有工具能直接解决时继续用原工具，不要求每条内容经过新工具。

## 调用

先使用现有 `generation_contracts.build_shot_generation_unit` 创建
`generator="manual"` 的 `shot_generation_unit.v1`，不更改 VKP Timeline。
从本仓根目录运行：

```powershell
python -B scripts/creative_tool_handoff.py build --tool shaders --source-review-root <workspace>/source-reviews/radar-intake-20261011 --shot-unit <existing-shot-unit.json> --output <new-handoff.json>
python -B scripts/creative_tool_handoff.py validate --handoff <new-handoff.json>
```

`--tool` 可改为 `filmcraft` 或 `photocraft`。输出保留源镜头、原始 edit job 的哈希、文案、尺寸、时长、素材引用和连续性要求。更改原始输入后必须重新生成。该 JSON 供人或 Agent 阅读，不能冒充上述软件能够直接导入的工程文件。

## 接回现有出口

试用产物保留可编辑工程/源码、预览或候选媒体、变更说明及未映射项目。由现有 manual-media 的 generation receipt/validation 接回，再走已有 FFmpeg/技术 QA/人工播放复核，不绕过原审批门。

PhotoCraft 分层素材的具体合同与损失报告见 [photocraft-material-revision.md](photocraft-material-revision.md)。
同一 `scripts/creative_tool_handoff.py` 提供 `revision-plan` / `revision-validate`；
它们只准备和检查合同，不启动软件，也不生成第二套 manual-media 或 QA 出口。

FilmCraft 首轮检查中文、音画同步、保存重开、局部修改和交换损失报告；不能把 EDL/FCPXML 的格式名称当无损证据。PhotoCraft 检查中文字体、蒙版、层序和保存重开。Shaders 必须验证实际框架入口的网络行为、WebGPU 失败替代和现有时钟的逐帧推进；不得改证据颜色。

## 仍未完成

- 三者的真实 Windows 运行/导入导出/视觉验收。
- Shaders 完整依赖与实际调用路径的网络复核。
- FilmCraft / PhotoCraft 构建入口、发布二进制与固定源码的身份闭环；先前选定文件取件仍有缺口。

本地来源见 `<workspace>/source-reviews/radar-intake-20261011/REVIEW.md`。
本适配器只核对选定模块，不把源码子集说成完整 checkout 或安全审计。
无需安装模型、修改 hooks/MCP 或调用账号即可准备交接。

## 离线验证增量（2026-10-12）

更新工具：Codex；具体运行模型未独立核验。直接任务关联旧队列
`SELECTIVE-VIDEO-HANDOFF-20261011-01`，本次没有投递、领取或修改旧队列。

继续使用同一入口，增加 `verification-plan`、`verification-validate`、`consume-photocraft`。
帧率必须由现有输出时间线明确传入分子／分母；不会从 29.97 小数猜成 30000/1001，
也不会改写源 edit job。短验证计划最多 60 秒，时长仍绑定原 shot unit。

```powershell
python -B scripts/creative_tool_handoff.py verification-plan --handoff <existing-filmcraft-or-shaders-handoff.json> --fps-numerator 30000 --fps-denominator 1001 --output <new-plan.json>
python -B scripts/creative_tool_handoff.py verification-validate --input <plan-or-filmcraft-return.json>
python -B scripts/creative_tool_handoff.py consume-photocraft --input <existing-revision-return.json> --handoff <expected-photocraft-handoff.json>
```

这些仍是验证请求／证据合同，`application_import_file=false`、`runtime_verified=false`。
原 build/validate 与 PhotoCraft revision-plan/revision-validate 不变。

### Shaders 控制层

`scene_components/shader_frame_driver.mjs` 是宿主时钟的薄适配，不导入上游引擎、不建 GPU、
不设 RAF、不启动浏览器，也不读媒体。通过 factory 接收另外核验的 renderer，使用上游的
`initialize`、`renderFrame({deltaSeconds, waitForGpu:true})`、`setOnUnavailable`、`dispose`。

- 第 0 帧传 `deltaSeconds:0`，到第 N 帧则顺序传 N 次 `fps_denominator/fps_numerator`。
  模拟效果即使由宿主直接请求第 N 帧，也必须累计执行中间帧，禁止一次传目标时间。
- 同一帧和暂停都复用已有结果，不调用零增量重画来假定模拟完全静止。
  尚未开始时暂停，使用传入的本地静态图。
- 倒退先释放原实例，用新实例从零重播；重播受 `maxFrame` 有限上界控制。
  并发 draw 拒绝，初始化／绘制错误可见，不静默吞掉配置错误。
- 使用明确的节点 ID，按 job/scene namespace 与 replay generation 隔离 ID，重写本地 mask 引用；
  复制 preset，禁止共享 renderer 实例。结束必须 `dispose()`。
- 不支持 WebGPU、运行中设备失效、减少动态效果时，交回调用方提供的静态图；不会自动启用 WebGL。

定向测试通过固定哈希加载**真实上游 presetRenderer 控制代码**，将缺失依赖替换成内存合成后端。
还执行真实 telemetry collector 的 start/stop，POST 由内存 sink 拦住，未发网络请求。
这证明控制调用顺序与失败分支，不证明 GPU 像素确定性、纹理／设备缓存隔离或完整软件可用。
源码显示节点注册表位于实例内，动画时间使用对象键；默认 device 则可在页面内共享，
依赖的 root/time/texture 等实现不在本次完整审查范围，因此真实 GPU 状态隔离仍是 unverified。

遥测的已知路径为 `start → warmup/samples → aggregateData/collectEnvironment → sendTelemetry → fetch POST`，
目标 `https://shaders.com/api/telemetry`。配置采样率为 5%，但 `collector.start()` 本身不检查该值，
本次没有拿到完整实例化／启用调用链。`enablePerformanceTracking:false` 不能当成断网开关。
报告列出未覆盖依赖；未来启用真实 renderer 前仍需核入口和素材加载路径，并在宿主执行外部请求拒绝策略。
当前适配器不提供启动命令，不能把这个控制模块当网络沙箱。

### FilmCraft 返回合同

固定源码仅证明交换层声明并分派 EDL、FCP7 XML、FCPXML、OTIO、AAF、OMF；
各格式实现／完整构建依赖缺失，六种格式的实际导入导出均未验收。
导入和导出均可能把不支持内容放进 `Report` 而继续成功，返回合同因此要求两份原始 JSON 报告。
`Info` 和 `Warning` 都进入待复核项；不能通过省略 warning 或仅检查进程退出码过关。
普通 AAF/OMF import 可能报告内嵌音频未提取；EDL 要显式给帧率，XML 要核实际文档类型。

`filmcraft_trial_return.v1` 绑定验证计划、scene 与有理帧率，逐项报告 timing、subtitles_fonts、
audio、effects_transitions、media_paths、canvas_color。未跑软件时全部只能 unverified，
不得返回所谓已生成原生工程／媒体。声明执行过时必须提供 Windows 二进制与来源文件引用，
以及 import、中文字幕、音画时钟、save、reopen、local_edit、export 七项证据。
请求包换扩展名也不能当作原生工程。所有文件哈希只能验证绑定，不能独立证明格式或观感。

回收候选视频仍要求现有 manual-video generation receipt；所有损失和交换报告通过复核后，
仅允许进入既有 QA，`runtime_verified`、`native_project_verified`、`publish_approved` 不因合同通过而变成 true。
当前本机限范围查找未找到可用 FilmCraft，缺 Windows 来源闭环，软件验证 blocked。
此前 REVIEW.md 关于 `.prproj`、VST/AU/OpenFX 和平台成熟度的文字属于历史审查结论，
本轮离线没有重新读取完整上游资料，不能改称当前软件实测结论。

### PhotoCraft 与 Yovoice

视频侧只调用既有 `validate_revision_return`，并核对返回物属于指定的 PhotoCraft handoff。
已消费设计侧的合成返回合同，其软件未执行、五项损失未验证，因此不能进入既有 QA；
没有复制或再次集成编辑器。Yovoice 保持延后，无下载、模型调用或新接入。

本轮证据入口：`<workspace>/outputs/selective-followup-20261011/video/README.md`。
退出方式：停用新增可选命令／调用方 dispose 控制器即可。增量补丁及变更前文件另存于该目录，
撤回只针对本次增量，不 reset/stash 或覆盖原有并发改动。

## DrawAI 局部修改离线映射（2026-11-08）

Codex（GPT-6.1 Sol）按 `DRAWAI-VIDEO-HANDOFF-20261108-01` 接入可选检查入口。
复用现有 shot unit、原 edit job、文件引用和手工素材返回路径，输入为人工修改后的原生
`.drawai` 基线、AI 局部修改候选及两份小型工程快照。

```text
python scripts/creative_tool_handoff.py drawai-revision-check --shot-unit <unit.json> --brief <brief.json> --source-review-root <review-root> --output <new-check.json>
```

brief 的 `before_canvas`、`candidate_canvas`、`before_project`、`candidate_project`
显式给出本地路径，`expected_revision` 绑定人工基线文件 SHA-256 的前 12 位，和固定上游
`contentHash()` 一致。`page_id`、`video_node_id`、`mutable_node_fields`、`mutable_shot_ids`
声明可修改范围。检查其余页面、节点、几何、pinned 状态及工程文件的完整性。工程的
`timeline.json` 使用上游的 `fps`、`durationInFrames`、`shots[].startFrame/durationFrames/file`，
总长与原 shot unit 对齐，空洞或重叠进入阻断项。`asset-sources.json` 的 `entries`
记录资产 path、sha256、source 和 license。`upstream_warnings` 保留供现有 QA 复核。

`preview_frames` 可绑定已有文件和帧号，观感状态进入 pending。返回值记录
`editable_project_candidate`，媒体回收继续走现有 generation receipt、技术 QA 与人工验收。
本入口只读取和验证快照。DSH 实际操作、场景执行、声音同步和 5–10 秒样片观感均待专项验收。

最小合成示例、固定源码验证、测试及恢复材料在
`<workspace>/outputs/drawai-video-adoption-20261108/`。源审查固定为
`fourzkw/dsh-drawai@f26770ca91934e89d39e025433467ebc325e82b8` 的 28 文件快照。
DrawAI 使用的 Shotcraft/mg-styles 继续按各自来源区分，本地 Shotcraft 固定版本沿用原选择。
退出时停止调用此可选入口。回撤代码时按上述目录的变更前文件与精确增量处理。

## 变更理由

意图：让已审工具有明确输入/产物交接；决策：复用 shot unit、contract_io 和手工素材入口；理由：避免再造时钟、编辑器或渲染器；证据：固定源码清单与本模块定向测试；生效范围：可选 build/validate；退出：停止使用此入口即可，现有默认路线不变。
