# 通用动画来源选择：复用现有镜头路由

## 2026-11-10：完整原生风格库与成功案例复用

执行：Codex，精确运行模型未独立核验。用户要求：有完整风格、提示词、样片代码的项目，以固定版本保留原生内容与对应关系，不再用摘要替代；今后同类项目按此原则处理。

### 保存与适配分开

- 原仓库的提示词、风格、样片、素材、共享代码、版本锁与许可按原目录保存，原生 ID、原 JSON/Markdown/HTML 索引不改名或重写。完整资料常驻来源包，不是每轮全部塞进模型上下文。
- 路由层只提供原生索引和选中条目的原路径；先读取完整选中风格及其样例、提示词、素材、共用依赖，再在现有项目外层适配本次内容/尺寸/时间和出口。保留基线与局部改动，不回写上游。
- “完整”指保存上游实际提供的指定 scope，不假装仓库外的模型、付费服务、链接素材或未公开工程也已具备。完整源码、依赖可用、成功复现、中文新片效果分别记录。
- 安装器、hooks、账号、上传、费用和发布指令不因原文被完整保存就获得执行权；许可证和来源原文一并保留。
- 后续同类来源优先按这一模式接入原路由，不再把成熟项目拆成另一份同义方法摘要或另建风格库平台。

### 实际注册入口

使用现有注册的 invoke_main.py，不切回旧开发副本：

~~~powershell
python -B <workspace>/local-tools-runtime/self-media-main-20261029/invoke_main.py mvp scripts/semantic_editorial.py animation-source-bundle --project lemo-opuscar --entry archive-lighttable
python -B <workspace>/local-tools-runtime/self-media-main-20261029/invoke_main.py mvp scripts/semantic_editorial.py animation-source-bundle --project video-shotcraft --entry ai-stream-response
python -B <workspace>/local-tools-runtime/self-media-main-20261029/invoke_main.py mvp scripts/semantic_editorial.py animation-source-bundle --project ai-design-skills --entry skills/landing-page-design/SKILL.md
python -B <workspace>/local-tools-runtime/self-media-main-20261029/invoke_main.py mvp scripts/semantic_editorial.py animation-source-bundle --project openfilm-one-prompt --verify
python -B <workspace>/local-tools-runtime/self-media-main-20261029/invoke_main.py mvp scripts/semantic_editorial.py object-scene-route --visual-job flow --technique continuous_transform --source-project pdoom-explainer --source-project openfilm-one-prompt --delivery video --narration-timing unverified
~~~

原生条目返回 native_entry 原对象、原文件位置和共享依赖根；不改内容。Shotcraft 多实现卡保留多个 TSX，不能把 “A.tsx / B.tsx” 当成一个不存在的文件。--verify 离线核验保存范围全部文件；普通查询只核绑定的 manifest 和原生索引，返回状态明确区别全量核验。

已加入原路由的来源 ID：lemo-opuscar、video-shotcraft、ai-design-skills、pdoom-explainer、openfilm-one-prompt、ai-music-creator、indieshade-video-director、rea-video。product_intro、music_compose、production_breakdown、narration_directing 是原来源选择器的维度，不是新顶层工作流；style_exploration/continuous_transform/song_pv 也已补相应来源。

indieshade 项目的实际 Skill 名为 narration-video-director，适用配音解说与代码动效，不是此前误匹配的中文采访粗剪 Skill。不把它改名混成同一个项目。Shotcraft 如指其他独立工具，应按准确仓库区分，当前这里只把已明确的 Vincentwei1021/video-shotcraft 挂入原配方链。

### 从案例到新作品

1. 按本次表达选择一个具体条目，先看其实际参考和完整源文件。
2. 默认从原样例工程改内容，保持原构图、动作和素材关系；只在用户要求或兼容需要时改设计。
3. 先让选中基线可重开/取帧，再适配当前旁白和素材；原样例通过与新作品通过分开。
4. 延续包装时钟和既有编辑/渲染/QA。局部改动复验相关边界，已确认部分不重做。
5. 将用户认可的新版本连同源 commit、原条目 ID、局部 patch、素材/时间来源保存在既有作品归档中，下次直接续用，不另起一套案例状态库。

当前交付仅完成来源完整性、路由和离线合成接入验证，没有把全部上游样片在 Windows 重渲染，也没有用本人的实际稿件做观感验收。


更新：2026-10-27，Codex。

此入口帮助制作 Agent 根据镜头需要选择已研究的方法、画面模块、底层库、渲染候选和剪辑工具。它扩展现有 `video-semantic-editorial object-scene-route`，不新增顶层工作流、渲染器、时间线或执行出口。来源推荐本身不安装依赖、不执行上游、不生成媒体，也不授予网络、上传和发布权限。

适用范围是通用解释动画、动效和局部镜头。医学、工业、软件、教育、人物叙事等都先写清观看者应理解的关系，再选择表达方式；没有把某条 RWE 视频的机构名称、业务节点或口播写成通用默认值。

## 现有入口各负责什么

| 层级 | 现有入口 | 本页如何衔接 |
|---|---|---|
| 交付任务选择 | `self_media_agent.py resolve` | 选择当前交付物的既有消费者，技术维护不重开创作 |
| 创作表达形式 | `self_media_agent.py creation-route` | story、travel-experience、expository、technical-rework；动画风格不是新主路线 |
| 分镜的理解任务 | `semantic_editorial.py object-scene-route` | 依据因果、流程、比较、数量、空间、证据等选择原有路线 |
| 动画参考与模块选择 | 同一命令的可选参数；`semantic_editorial.py animation-catalog` | 增补候选来源、适用理由、证据状态和下一步 |
| 已有可执行对象组件 | `semantic_editorial.py object-scene-components` / `object-scenes` | 复用真实组件源码、原录音和词锚点；不复制第二份组件实现 |
| 视频路线与风格 | `video-visual-routing build-style` / `build-routes` | 保持原内容路线、风格合同、scene边界及局部失效策略 |
| 已审查外部组件 | `external_motion_assets.py catalog` / `plan` | 继续负责 Onda、Remotion Bits、ClipKit 的固定源码目录与 cue 投影 |
| 外部软件试用 | `creative_tool_handoff.py` | FilmCraft 等维持原交接、验证与返回合同 |
| 制作和交付 | 原 Remotion/HyperFrames、manual-media、FFmpeg outlet、QA | 只有通过对应制作与检查后才能报告实际候选媒体 |

`animation-catalog` 是选择地图，不取代 `SOURCE_INVENTORY.json` 的来源台账，也不取代上述消费者。完整工作台、提示词方法、代码组件和渲染引擎分别标明类别；一个项目能提供方法，不代表其整套平台必须接入。

## 使用命令

从本仓根目录使用已有环境，不为查询目录安装包：

```powershell
python -B scripts/semantic_editorial.py animation-catalog

python -B scripts/semantic_editorial.py object-scene-route --visual-job flow --technique diagram --technique continuous_transform --delivery video --narration-timing unverified

python -B scripts/semantic_editorial.py object-scene-route --visual-job flow
```

第二条表示希望用图解和连续变化说明流程、最终交付视频，而口播时间证据尚未核验。推荐只能指出应选的模块与应补的输入，不会推算说话时长。第三条不带新参数，保持旧调用的返回行为。

`--technique` 可以重复。先从目录读取来源的准确 ID，再使用一个或多个 `--source-project` 限定候选：

```text
python -B scripts/semantic_editorial.py object-scene-route --visual-job flow --technique diagram --delivery video --narration-timing unverified --source-project <目录中的来源ID> --source-project <另一来源ID>
```

这些占位符必须替换，不能把展示名称、仓库模糊简称或未知 ID 当作有效值。指定 `--source-project` 后，候选只包含显式 ID 列表，不再并入所选手法的默认项目；重复 ID 去重。所选手法仍提供检查项和匹配说明，未审查的手法/来源组合显示缺口，不能使不适用的模块获得执行资格。没有显式来源时，才使用手法的候选项目。完整参数与允许值以当前 `--help` 和目录为准。

### 当前24种手法

下表对应 `policies/animation-source-catalog.v1.json` 的实际键。它们是选择和检查维度，不是24个均已运行的插件。

| `--technique` | 使用目的 | 来源举例 |
|---|---|---|
| `diagram` | 关系、权限与流程图 | diagram-design、anything2explainer、Anatomy、既有语义动效 |
| `anatomy_mechanism` | 器件结构与机构动作 | Anatomy、Three.js、既有Remotion |
| `algorithm_trace` | 算法步骤与路径搜索 | Pathcraft、anything2explainer、Canvas 2D |
| `collage` | 纸质拼贴与剪纸 | Paper-Cut、mg-styles-15 |
| `sticker` | 贴纸与综艺贴图 | mg-styles-15、Paper-Cut、既有外部动效目录 |
| `kinetic_type` | 字体与排版动效 | mg-styles-15、OpenDesign、Onda、Remotion Bits |
| `continuous_transform` | 一镜到底与连续变形 | OneTake、mg-styles-15、Canvas 2D |
| `character_performance` | 角色动作与情绪 | ClaudeAnimationBase、PDoomVideo、p5.js、p5.brush |
| `song_pv` | 完整歌曲PV的制作方法 | PDoomVideo、ClaudeAnimationBase、Hypit |
| `watercolor` | 水彩与水墨 | p5.js、p5.brush、ClaudeAnimationBase |
| `illustration` | Canvas 2D插画 | Canvas 2D、mg-styles-15 |
| `sketch` | 粗线与手绘草图 | Rough.js、Canvas 2D |
| `three_d` | 三维空间与机构 | Three.js、Blender、既有Remotion |
| `clay` | 黏土材质动画 | Blender、Three.js |
| `mathematics` | 数学几何与公式推演 | Manim |
| `whiteboard` | 板书与笔迹揭示 | hand-drawn-follow-animation、Manim、Rough.js |
| `data_chart` | 数据与图表动画 | anything2explainer、既有Remotion/语义动效 |
| `interactive_web` | 网页交互与事件动画 | motion-web、Anatomy、Pathcraft、emil-animation-skills |
| `narration` | 口播与语义B-roll | Hypit、leaderai-media-toolkit、anything2explainer |
| `image_motion` | 照片局部运动 | image-motion-animation、Paper-Cut、claude-code-video-toolkit |
| `editing` | 可编辑工程与交付 | openfilm、Hypit、FilmCraft、OpenCut、既有工具交接 |
| `rendering` | 渲染与采样 | HyperFrames、Remotion、leaderai-media-toolkit、Multiplane、Inkling |
| `style_exploration` | 风格样片与提示词参考 | mg-styles-15、Paper-Cut、Skillry |
| `native_generated_collage` | 原生生成式拼贴方法参考 | gbro-collage-broll、MiniMax纸质拼贴Skill |

名称只用于阅读；`--source-project` 的准确 ID 以目录为准。身份未完全解析或仅有参考材料的来源仍保持对应状态，不能因出现在表中就视为已接入。

### 常用组合

下表中的参数接在 `python -B scripts/semantic_editorial.py object-scene-route` 后。参数只生成推荐，不执行制作。

| 场景 | 示例参数 |
|---|---|
| 连续变化的流程解释 | `--visual-job flow --technique diagram --technique continuous_transform --delivery video --narration-timing unverified` |
| 器件结构拆解 | `--visual-job space --needs-depth --technique anatomy_mechanism --delivery video --source-project anatomy --narration-timing unverified` |
| 原创角色歌曲PV | `--visual-job authored_performance --technique character_performance --technique song_pv --delivery video --source-project pdoomvideo --source-project claudeanimationbase --narration-timing none` |
| 数量变化的图表镜头 | `--visual-job quantity --technique data_chart --delivery video --narration-timing word` |
| 原生可交互算法演示 | `--visual-job flow --technique algorithm_trace --technique interactive_web --delivery interactive --source-project pathcraft` |
| 静态剪纸关系插画 | `--visual-job abstract_relationship --technique collage --delivery still --source-project paper-cut` |

`word` 只是声明手头已有词级时间，之后仍需绑定并验证实际产物。歌曲PV例子的 `none` 表示当前无需口播同步；仍使用已有packaging的场景窗口，若需要歌词/节拍同步，须补对应实测时间和现有音乐合同，不能据此自动推断。

原创虚构角色、编排动作和歌曲PV使用 `authored_performance`。它要求先完成场景设计和角色媒体制作，返回 `authored_media_handoff_required=true`；产物再接现有 `existing_media_broll` 与原媒体交接/QA。它不要求预先存在真人拍摄素材，也不宣称已制作成片。`person_or_emotion` 继续用于真实人物和经历的素材表达，保护原素材和事实，不用于绕过虚构角色的制作步骤。

`--delivery interactive` 和 `--delivery still` 会退出视频执行路线：顶层 `visual_route` 与 `component_id` 为 `null`，`delivery_route` 分别为 `native_web_interaction`、`static_composition`。交互使用原生事件，静帧使用静态构图；两者不要求口播锚点，也不返回待执行的视频渲染消费者。只有后续明确改做视频时才重新选择视频交付并补帧时钟适配，不能把交互页面直接称为可导出视频。

## 怎样使用推荐结果

视频推荐先保留原来的 `content_kind`、`visual_route`、组件/专家交接和时间权威，再提供可选的分层 `module_selection`。交互/静帧交付则按上文切换到自身交付路线，不沿用视频组件。按真正需要使用其中一层或数层，不要求凑齐整套技术栈。

- **创作方法**：分镜、关键姿态、先静帧后运动、章节/接触表检查等。学习方法不等于复制其原模型、上传和渲染流程。
- **画面模块**：可以追到具体源文件、函数、导出组件和成功镜头的代码。优先改事实、标签、尺寸、对象和可解释参数，保留有效构图及动作关系。
- **底层库**：Canvas 2D、Rough.js、p5.js/p5.brush、Three.js 等提供表达能力；不单独证明完整动画已具备。
- **渲染候选**：必须满足当前帧时钟、确定性、素材加载、输出尺寸和既有QA要求，不能仅依据速度宣传替换现有出口。
- **剪辑工具**：用于人/Agent编辑和媒体交换。能导入某种格式不代表字幕、字体、转场和音频无损往返。

现有对象组件的可用项及源码身份由 `object_scene_component_catalog()` 提供。新来源的研究记录、固定版本或实验结果只说明相应范围；没有实际调用与成片证据时保持候选、待制作或未验证。目录读取成功、JSON校验、预览截图和真实媒体验收是不同证据。

选择前先说明对象、关系、初始状态、最终状态、动作和镜头目的。流程镜头要让同一对象持续变化；角色表演要有可辨识姿态与动作目的；结构解释要有可信结构资料。不要用“加动效”代替构图设计，也不要为了风格统一改变证据、材质或状态颜色的含义。

## 源码与证据边界

推送时的可移植性补充（2026-10-27，Codex）：本机的对象创作、外部组件、编辑器等可选集成未必已存在于干净仓库。来源查询不依赖这些功能已部署；`existing_component_catalog_status=existing_source_inventory_unavailable` 时返回空组件候选，仍可查询研究来源。本文列出的下游入口是本地集成地图，使用前应检查相应安装/源码，而不能据文档声明已可运行。标准查询命令使用 `python scripts/semantic_editorial.py`；console alias是否安装以实际环境为准。

- 意图：让本次路由提交在干净仓库中可独立查询，保留本机已有功能。
- 决策：对象组件目录作为可选来源；CLI只移植此次目录和推荐命令，不夹带旧未提交分支。
- 理由：主分支尚未包含本机的object_scenes及其依赖，整文件提交会引入不完整功能。
- 证据：推送前Git HEAD/工作区差异、最小依赖审查、缺模块回归与干净导出测试。
- 生效范围：来源查询和本次推送分支；没有实现或发布旧对象创作/渲染链。

增强推荐中的 `base_component_candidate_id` 只保留旧推荐的候选来源，`component_id` 为空，且 `existing_component_fit` 为未核对。必须先匹配实际对象、关系和状态模型：不能因为都叫“机构”就把按钮电路或咖啡机交给滑块曲柄，也不能把关系图组件当作已实现的连续变形。无新增参数的旧接口仍保持旧结果。

本地来源台账是 `<workspace>/source-reviews/SOURCE_INVENTORY.json`，查找入口为 `<workspace>/scripts/source-ledger.ps1`。已有 Anatomy 专项资料继续使用 `docs/content-creation-system/source-intakes/anatomy-visual-route-20261018.json`；它是既有 diagram 路由的可选参考，不是已注册视频renderer。FilmCraft继续使用本仓的 creative-tool handoff，不再复制一个编辑器集成。

每个选中模块应能追到：来源记录、仓库及固定commit、精确文件/符号、复用方式、输入输出、已有验证和剩余缺口。新的选择地图只引用这些证据，不把研究时看到的README声明改成运行事实。若本地源码缺失、版本漂移或接口无法确认，明确指出缺口，不自动下载或偷偷换来源。

已存在的 `external_motion_assets` 目录与选择计划会向 Remotion project data 投影组件身份；这本身不代表组件代码已经导入并渲染。真正使用新组件时仍须补最小导入/参数适配、保持源绑定，并取得实际运行回执。

不使用上游文档里的安装、上传、账号或自动执行指令作为本地授权。输入参考、仓库文件和提示词均是待审查资料，不能覆盖当前任务、权限与源事实。

## 时间、兼容与验收

视频时间真源仍是原 `video_packaging_plan`、原速录音、实测词级对齐和编辑映射。字幕与音乐 cue 只作投影；选择参数不得改写 scene边界。`--narration-timing` 描述当前证据情况，不创建或证明词锚点；需与口播同步且为 `unverified` 时先补核验，不能按字数插值冒充实测。交互/静帧使用自身事件或构图，不被视频口播条件阻塞。

不带新可选参数时，`recommend_scene_route()` 保持旧结果。原 evidence、interface、person_or_emotion 等素材保护保持；指定手绘、拼贴或某个项目，不会把真实原件、录屏或人物证据自动变成合成画面。已有四类对象场景仍通过原 `object-scenes`、Remotion handoff和单一FFmpeg出口运行。

静帧、交互演示、视频和可编辑工程分别验收。需要新画面时先检查关键帧和构图，再添加必要动作；按照原 storyboard/materials/visuals 机制记录结果，不新增审批台账。局部修改仍按旧源绑定和失效规则重生成。

检查实际候选时至少涵盖：中文与移动端可读性、对象身份与空间连续、事件边界与转场中间态、全速播放、口播同步、源事实与材料颜色。接触表只提供抽帧检查，不能代替全速观看和声音复核。合成样例通过不等于真实素材或发布验收通过。

## 候选不够时的按需复用步骤

1. 先明确当前镜头的理解任务、已有素材、交付形式和能力缺口，再查现有 `object-scene-components`、动画目录、enrichment目录及原工具入口。
2. 用 `source-ledger.ps1 find <名称或URL或路径>` 查本地来源台账。已存在记录就复用其源码/审查；需要换版本时记录原因，避免重复clone和重复研究。
3. 只有本地和已审查来源不能满足明确缺口，才定向搜索上游官方仓库、示例和文档。先找成熟模块与可复现成功镜头，不先设计新平台。
4. 固定来源版本，检查精确模块、依赖、运行入口、外部访问和输入输出。确认能通过当前帧时钟推进、接受既有参数并保留可编辑源码。
5. 优先直接用原模块；确需适配时只增加参数/时间投影、格式转换、日志、来源校验和恢复逻辑。保持原素材、编辑、渲染、音轨和QA出口。
6. 用有代表性的短合成镜头验证相关上游行为，再用当前任务素材检查构图和同步。记录成功、失败和未验项；未通过时维持候选状态。
7. 将复用结论写回本次变更说明并引用台账证据；依实际授权更新相应发现入口。不要靠新增catalog条目宣称运行能力已经提升。

## 变更理由与生效范围

### 组件适用性核对

- 意图：避免同一理解任务下不同机构误用同一个现成组件。
- 决策：增强推荐保留旧组件作候选，同时要求对象、关系与状态模型核对；不自动绑定。
- 理由：`mechanism`、`flow` 等标签不足以证明具体代码适用。
- 证据：独立按钮电路／咖啡机用例暴露了旧 `slider_crank_3d` 默认被泛化的问题。
- 生效范围：仅新增参数分支；原组件源与旧调用保持不变。

### A. 只读动画来源目录

- 意图：让制作Agent找得到已研究的项目和可复用模块，避免每次重新搜索。
- 决策：在现有semantic-editorial入口提供只读目录，沿用已有enrichment目录的展示方式。
- 理由：方法、组件、库、渲染与剪辑属于不同层级；目录应帮助选择已有消费者。
- 证据：现有 `semantic_editorial_cli.py` 的 enrichment-catalog、object-scene-components 与来源台账；新条目的源码证据由目录引用。
- 生效范围：候选发现和下一步说明，不安装、不渲染、不更改现有执行权威。

### B. 原场景推荐的可选增强

- 意图：把用户的表现需要和交付需要带入已有理解任务选择。
- 决策：扩展原object-scene-route可选参数并提供分层module_selection；默认调用保持旧行为。显式source-project列表限定唯一候选集，不并入手法默认来源；手法仍提供匹配及检查项。
- 理由：避免新建同义router，保留已有组件、专家能力缺口和真实素材保护。
- 证据：现有 `object_scene_routes.recommend_scene_route()` 的JOBS/SPECIALISTS，及 `object_scenes.object_scene_component_catalog()` 的真实源码引用；新增目录实际24个technique键与选择代码的 `explicit_sources_only` 分支。
- 生效范围：显式提供新参数的推荐结果；不改原build-routes、renderers、时间线和输出媒体。

### C. 时间与能力状态

- 意图：防止把研究结论、参数选择或合同通过误报为动画和同步已完成。
- 决策：推荐保留源码、运行证据与时间缺口；未验证来源继续候选。
- 理由：源代码存在、调用成功、画面可用和当前口播同步分别需要证据。
- 证据：原visual-routing输入重算、packaging时钟、author binding、外部组件metadata-only和creative-tool交接边界。
- 生效范围：本新增选择结果及其使用说明；不重写历史回执或伪造人工验收。

### D. 原创角色与歌曲PV的入口

- 意图：区分原创编排表演与真实人物证据，避免创作角色PV被误要求提供真人原片。
- 决策：增加理解任务 `authored_performance`，先设计并制作媒体，再通过 `authored_media_handoff_required` 接回既有existing-media路线；person_or_emotion保护保持。
- 理由：二者都可能表达情绪，但原创媒体制作和真实素材使用有不同输入，不应借flow之类任务掩盖创作目的。
- 证据：当前推荐代码的authored_performance分支、来源目录的character_performance/song_pv及原媒体交接机制；本页不把代码路径检查视作实际角色样片验收。
- 生效范围：新理解任务及其候选推荐，不改变旧人物素材路由，不生成角色、配音或成片。

### E. 非视频交付退出视频路线

- 意图：让网页交互与静态图使用合适的交付方式，避免被不相关的口播和视频渲染要求阻塞。
- 决策：interactive/still的新调用清空顶层visual_route/component_id，分别返回native_web_interaction/static_composition；不提供视频执行消费者。
- 理由：原生事件、静态构图和逐帧视频有不同时间模型，HTML可运行不代表可视频导出。
- 证据：当前推荐代码中的delivery分支、video_execution_applicable和非视频time_binding.next_action；目录保留各来源自身交付能力与适配缺口。
- 生效范围：显式非视频交付的新增调用，不改变旧默认视频行为；以后转视频须另补确定帧时钟和原制作链输入。

### F. 文档、回归与退出

- 意图：使接手者能使用、核对和停用本增量。
- 决策：将入口层级、命令、复用步骤和能力边界写入本页，验证记录由本次实际测试回执提供。
- 理由：文档与测试声明必须对应实际行为，不能把计划写成通过。
- 证据：本页引用当前本地既有实现；此文档编写未执行测试，最终运行结果另见实施回执。
- 生效范围：开发与使用说明；不构成任何未运行测试的通过声明。

退出时停止传新增参数即可恢复原推荐路径。回退代码只撤销本次目录、可选分支、CLI增量与对应文档/测试，不覆盖现有脏工作树，不删除既有媒体、源码和回执。

## 2026-11-19：Wise Motion 几何海报案例

来源 `wise-motion` 已进入 `kinetic_type` 路由；固定提交 `29285fb36e09d1c9bbbbb6458d804e1bdfe30259`。原效果与标题延后 0.25 秒的本机案例、来源、时序、许可及实际验证见 `<workspace>/outputs/vid-case-routes-20261115-01/repro-project/evidence/wise-case-20261119/README.md`。这是单案例候选，完整播放和观感待审；源码目录不等于独立安装或整体运行验收。执行：Codex（具体运行模型未独立核验）。

同日追加关键词强调（`kinetic_type`）、三项内容（`diagram`）、百分比柱图（`data_chart`）三个本机可编辑候选；各自原版、修改版、数据边界与验证见 `<workspace>/outputs/vid-case-routes-20261115-01/repro-project/evidence/wise-practical-20261119/README.md`。柱图数值仅为模拟示范，真实项目需核验来源与时间绑定。

同日另加入花字（`sticker`）、中心关系图（`diagram`）与主体接力转场（`continuous_transform`）候选，来源、适配及对照验证见 `<workspace>/outputs/vid-case-routes-20261115-01/repro-project/evidence/wise-expressive-20261119/README.md`。透明花字经 libvpx 合成到模拟背景，但未验证其他剪辑软件兼容性；路由仅实时校验 MP4 预览，透明层使用前需另核哈希。

## 2026-11-20：Juzu 关键词屏幕案例

`narration` 来源增加 `juzu`，固定提交 `2a57736afa4276878b833c0cadbca5919fc279c0`；上游 b01 二维屏幕、原版与改字版、原稿／导演表／cues 对应及测试见 `<workspace>/outputs/vid-case-routes-20261115-01/repro-project/evidence/juzu-case-20261120/README.md`。示范时间线只按字数估算，原音频 cues 有 8 处引用缺失句号；不得当作真实口播同步。完整三维片场、Apple MLX 配音和音乐环境未运行，Kazi／Kaikou 未新增集成。执行：Codex（具体运行模型未独立核验）。
