# 第三方软件通知

更新时间：2026-08-21 08:46:43 +08:00
维护者：Codex（GPT-5.6 Sol）

本项目自有代码按 Apache License 2.0 发布。该许可证不改变任何第三方项目、模型、权重、外部服务或生成媒体的许可证与使用条款。

发行包不内嵌下列上游源码、二进制、模型或权重。除特别说明外，本项目只通过 Python 依赖、显式外部路径、localhost API 或文件契约调用它们；安装和使用者须同时遵守相应上游许可证。

## 直接运行时依赖

### python-jsonschema/jsonschema

- 仓库：https://github.com/python-jsonschema/jsonschema
- 审阅提交：`81f7a761cbaca107d3d261c1288155655b98ba08`
- 项目依赖：`jsonschema>=4.23,<5`
- 许可证：MIT
- Copyright (c) 2013 Julian Berman
- 使用方式：导入 `Draft202012Validator` 与 `FormatChecker`；不复制源码。

依赖安装器可能解析 jsonschema 的传递依赖；这些包保留各自许可证，未被本项目的 Apache-2.0 重新授权。

## 可选或外部运行时

### lightcoloror/model-provider-gateway

- 仓库：https://github.com/lightcoloror/model-provider-gateway
- 历史执行证据提交：`c5f3ec49644453e0cddb56350e3b243b49e0f7da`，仅作 provenance，不是运行兼容门。
- 已验证语义版本：`0.3.0`；实际 checkout commit、包版本和 Schema/组件哈希由每次 doctor/receipt 记录。
- 许可证：AGPL-3.0-only
- 使用方式：操作者显式提供外部源码根目录后，本项目按 HTTP v1、Python/JSON execution contract 和版本化 Schema 消费 `text` 与 `vision`；不复制 Provider Catalog、secret、consent、LiteLLM transport、fallback 或上游源码。
- 发行边界：Gateway 不是本项目安装依赖，其源码、二进制和许可证文件不进入 wheel/sdist。本项目的 Apache-2.0 不重新授权 Gateway；部署和网络使用者须单独遵守 AGPL-3.0-only。
- 能力边界：ASR、OCR、TTS、embedding 可被控制面描述，但共享远程 Provider 执行仍为 blocked；通用文件上传未实现。
### ace-step/ACE-Step-1.5

- 仓库：https://github.com/ace-step/ACE-Step-1.5
- 固定提交：`6d467e4b5081ccb0abf1ec1bf4fdf9051a2d34b0`
- 许可证：MIT
- Copyright (c) 2026 ACEStep
- 使用方式：仅在操作者显式提供固定源码根目录、checkpoint 清单和许可快照后动态导入官方 API；不复制源码，不自动下载模型。
- 模型边界：见 `MODEL_ASSET_LICENSES.md`；任何模型权重均不随本项目分发。

### microsoft/CLAP

- 仓库：https://github.com/microsoft/CLAP
- 固定提交：`e8a6467b87cd85716e20c6a008126150d9740be0`
- 可选包版本：`msclap==1.3.3`
- 许可证：MIT
- Copyright (c) Microsoft Corporation
- 使用方式：从操作者显式提供的固定源码目录动态导入；不复制源码，不走上游自动下载路径。
- 模型边界：CLAP 权重和 Tokenizer 缓存不包含在发行包中，必须单独核验许可快照。

### CrispStrobe/CrispASR

- 固定提交：`9deefe8f47273722415e4b4be5d87361b96177c9`
- 许可证：MIT
- Copyright (c) 2023-2026 The ggml authors
- 使用方式：只登记外部 ASR 回执和 benchmark 契约；不随包分发源码、二进制或模型。

### ggml-org/llama.cpp

- 固定提交：`39b27f0da0271c06986cb31b68bc0fe68e780616`（审阅版本 `b8644`）
- 许可证：MIT
- Copyright (c) 2023-2026 The ggml authors
- 使用方式：只生成或验证显式 localhost 多模态 Provider 计划；不随包分发源码、服务端或模型。

### asg017/sqlite-vec

- 仓库：https://github.com/asg017/sqlite-vec
- 固定提交：`633eecf5067ab12ef331b3c4500c765f8e6d6da0`（审阅版本 `v0.1.7`）
- 许可证：MIT OR Apache-2.0
- Copyright (c) 2024 Alex Garcia
- 使用方式：消费外部索引 benchmark/查询结果；不随包分发扩展二进制或源码。本项目不替上游选择或改变其双许可证。

### FFmpeg / ffprobe

- 项目：https://ffmpeg.org/
- 许可证：取决于用户安装的具体构建；FFmpeg 默认以 LGPL 许可发布，启用特定可选组件的构建可能受 GPL 或其他条款约束。
- 使用方式：用户提供的外部命令行运行时；本项目不分发 FFmpeg 二进制或编解码器。

### PeterL1n/RobustVideoMatting

- 仓库：https://github.com/PeterL1n/RobustVideoMatting
- 固定审阅提交：`53d74c6826735f01f4406b5ca9075eee27bec094`
- 许可证：GPL-3.0
- 使用方式：作为操作者显式提供的外部本地 Provider，只运行上游 `inference.py` 的 `png_sequence` alpha 输出接口；本项目不复制、不修改或分发上游源码。
- 编码边界：RVM 不负责写最终视频；抽帧、alpha QA、背景虚化/替换、透明通道编码与音轨保留全部进入本项目既有 `video_creation_pipeline.single_ffmpeg_outlet`。
- 发行边界：上游源码、Python 依赖、checkpoint、alpha 帧和生成媒体不进入 wheel/sdist。本项目 Apache-2.0 不重新授权 RVM、权重或输出媒体。
- 能力边界：合成插画人物 E2E 只证明接口和技术链可用；发丝、手部、快速运动、半透明物体与时间闪烁仍需已授权真人金标复核。

### data-privacy-stack/Presidio

- 仓库：https://github.com/data-privacy-stack/presidio
- 固定审阅提交：`e069216c17ad3712a54c0ce3c5578e2ccee8f45f`
- 许可证：MIT
- 使用方式：隐私质量门只适配其 `PatternRecognizer` 的正则／拒绝词表和结构化候选结果边界；没有 vendor 上游文件，也没有加载 spaCy、完整 Analyzer、Anonymizer 或 Image Redactor 运行时。
- 能力边界：该适配只产生候选，不能保证找出全部个人信息；中文保险／医疗语料的精确率和召回率尚未形成正式基准。

### OpenCV

- 仓库：https://github.com/opencv/opencv
- 固定审阅提交：`77dfa297d08fdecdc509fc01ad92a2e9ec776a57`
- 许可证：Apache-2.0
- 使用方式：复用本地已实测的 `QRCodeDetector.detect/detectMulti` 候选检测方式，并使用运行时自带 Haar cascade 产生人脸存在候选；不解码或保存二维码载荷，不分发 OpenCV 二进制或模型文件。
- 能力边界：帧采样、姿态、遮挡和画质都会导致漏检，不能代替人工从头到尾审片。

### Tesseract OCR

- 仓库：https://github.com/tesseract-ocr/tesseract
- 本地运行时：5.5.0，`chi_sim` 已通过最终视频抽帧冒烟测试。
- 许可证：Apache-2.0
- 使用方式：作为用户提供的外部 CLI，对最终候选的抽样帧进行像素复扫；canonical 文档 OCR 仍由 VKP -> `ebook_markdown_pipeline` 负责。本项目不分发 Tesseract 二进制或语言数据。
- 能力边界：本次只证明本地运行时可调用；源码账本中的克隆尚未晋级为全面源码审阅结论。

### heygen-com/hyperframes

- 仓库：https://github.com/heygen-com/hyperframes
- 固定提交：`67edb01bf4aa2f5931e838e46e14e0f51a5809ee`（CLI `0.7.109`）
- 许可证：Apache-2.0
- 使用方式：作为用户显式提供的外部固定源码 CLI，执行 HTML/WAAPI 的 check、inspect、snapshot 和中间视频渲染。本项目只分发独立编写的 schema、adapter 和模板，不分发上游源码、浏览器、FFmpeg 或示例媒体。
- 发行边界：HyperFrames 中间视频必须进入本项目现有单一 FFmpeg outlet 和 technical QA；它不授予素材权利、创作审批、上传或发布许可。

### xiaotianfotos/HyperFrames-RenderKit

- 仓库：https://github.com/xiaotianfotos/HyperFrames-RenderKit
- 固定审阅提交：`3bec38942ad1b4040c96968771afe00bf9fd8802`
- 许可证状态：固定提交 README 明确说明尚未授予项目级许可证。
- 使用方式：只在操作者显式提供外部固定源码时，按提交和脚本 SHA-256 调用 `delivery.mjs` 静态兼容扫描；本项目独立实现 assurance plan、preflight receipt 和官方 HyperFrames 适配。
- 发行边界：不复制、不 vendor、不再发布上游补丁、脚本、构建系统、自定义 Electron/Chromium 或媒体运行时。Linux Intel VAAPI H.264 加速仅为显式 handoff；Windows、NVIDIA/AMD、Alpha 和 ProRes 4444 均不得宣称已验证。

### flickr/justified-layout

- 仓库：https://github.com/flickr/justified-layout
- 固定审阅提交：`9d4db49cdbf45604c63a8b25293650af4fe952ab`（包版本 `4.1.0`）
- 许可证观察：根 `LICENSE` 为 MIT；`package.json` 元数据为 ISC。两者不一致，项目不替上游选择或改写许可证。
- 使用方式：操作者显式提供固定提交的外部 `lib/index.js` 时，以 Node 调用其几何 box 计算；项目只分发独立编写的 hash-bound preflight/layout contract，不复制、不 vendor 上游源码或 npm 依赖。
- 发行边界：上游源码、`node_modules`、媒体和任何派生产物不进入 wheel/sdist。布局结果仅是 candidate-only renderer handoff，不能修改原始素材、自动裁切、自动转换 HEIC、上传或发布。

### remotion-dev/skills

- 固定审阅提交：`21320596cf9008cf6ccaa6bf1a2b9f71c8f191c3`（`@remotion/skills` `4.0.513`）
- 许可证状态：审阅时未见仓库根 `LICENSE`；不复制 Skill Markdown、示例或 npm 依赖。
- 使用方式：仅以操作者显式提供的固定外部源码生成 hash-bound `remotion_skill_handoff.v1`，消费现有候选布局、动效和字幕工件。
- 发行边界：不安装或执行上游 Skill，不启动 Studio/渲染，不把它变成第二 FFmpeg、ASR、审核或发布系统。

### Vincentwei1021/video-shotcraft

- 仓库：https://github.com/Vincentwei1021/video-shotcraft
- 固定审阅提交：`0d6f0b57f0d4d6700761644c07f7ef03c3e50234`
- 许可证：Apache-2.0；仓内 `assets/audio/ATTRIBUTION.md` 标记的部分音频来源仍需在商业使用前单独核验。
- 使用方式：操作者显式提供固定外部源码后，本项目只读取配方 library、指定 card 和准确参考 TSX，生成 hash-bound `shot_recipe_selection`，继续使用已有 Remotion Runtime。
- 发行边界：不复制上游模板、源码、`node_modules`、示例媒体或音频；任何实际素材仍须经过本项目的许可清单、安全布局、单一 FFmpeg outlet 和 QA。上游音频默认禁止直接进入项目。

### browser-use/video-use

- 固定审阅提交：`92c2b34e44c205cbc2acae7f6ca7c1c219d5dd66`
- 许可证：MIT
- 使用方式：仅以操作者显式提供的固定源码路径读取 `SKILL.md` 和 `helpers/timeline_view.py` 的版本证据，生成 hash-bound `video_use_review_handoff.v1`。它列出成片开头、结尾和每个场景接缝的按需检查窗口。
- 发行边界：不复制上游源码，不调用其 ElevenLabs/Scribe 转写、yt-dlp、自己的 EDL/渲染器或 `grade.py`。任何修复仍由本项目单一 FFmpeg outlet 执行并进入既有 QA。

### songguoxs/seedance-prompt-skill

- 固定审阅提交：`57d1e2f273747c238dd892698a05137ab2f10d4a`
- 许可证：MIT
- 使用方式：仅以操作者显式提供的固定源码路径生成 hash-bound `seedance_prompt_handoff.v1`，复用时间戳分段、跨段连续性锚点、参考素材用途与负向约束的提示词结构。
- 发行边界：不复制 Skill 内容，不把 README/Skill 写出的服务规格当作本项目已验证能力，也不调用 Provider、上传参考媒体、克隆音色或自动发布。任何未来 Provider adapter 必须单独审阅服务合同、能力、consent、上传范围和输出 receipt。

### naive-kun/naive-video-skill

- 固定提交：`7065a5c1e84729ca8c41d6815d1490a039bc3fb0`
- 许可证：MIT
- 使用方式：只参考其素材锚点、布局安全和 motion-plan 设计思路；数字人口播底座的合同与实现为本项目自有代码，没有复制其状态机、审批、FFmpeg 编排或源码文件。
- 发行边界：上游源码和媒体不进入 wheel/sdist；本项目继续使用自己的 artifact/hash、TTS、审批门和单一 FFmpeg 出口。

### Jiang59991/VectCutAPI

- 仓库：https://github.com/Jiang59991/VectCutAPI
- 固定审阅提交：`f0b9b1ef89a73cd50af157e04a8088d68e5dd5f6`
- 许可证观察：仓库根 `LICENSE` 为 Apache-2.0，`pyproject.toml` classifier 标注 MIT；两处元数据不一致，本项目不替上游重新许可。
- 使用方式：操作者显式提供固定提交的外部源码树和隔离 Python 运行时后，调用 `pyJianYingDraft` 的资料、片段、时间范围与 JSON serializer/profile；完整项目目录只在全新 staging 中复制上游 `jianying_pro_10` 模板并调用 `write_profile_content` 写入。
- 发行边界：不分发上游源码、模板或依赖，不调用其 downloader、uploader、全局 cache、server、递归删除或覆盖逻辑。项目目录生成与剪映桌面端打开验证使用两份独立回执；前者不能冒充后者。

### lobehub/lobe-icons

- 仓库：https://github.com/lobehub/lobe-icons
- 固定审阅提交：`4aaf4ee1fb2678a7f989ea570f0f6ce14a9abf75`
- 许可证：MIT；品牌名称、标志和商标权仍由各权利人保留。
- 使用方式：仅登记为可选品牌图标素材来源。具体资产进入项目时必须保存包名、版本、图标标识、MIT notice 和独立商标使用声明。
- 发行边界：本项目不捆绑图标包或品牌资产；开源代码许可不等于获得品牌背书或商标使用许可。

### Thiings

- 网站：https://www.thiings.co/
- 许可状态：免费方案仅限个人、非商业用途且要求署名；商业用途必须保存适用付费层级的许可快照。
- 使用方式：只作为带用途门禁的可选素材来源登记。`thiings_free` 会拒绝商业项目，`thiings_paid` 在缺少许可快照时关闭失败。
- 发行边界：不抓取或打包素材，不下载完整集合，不将站点使用条款改写成开源许可证。

### Remotion Runtime

- 上游：`remotion-dev/remotion`，固定提交 `e57eedcb3a6065d7db492696c030a76f6962e169`，运行时版本 `4.0.477`。
- 许可证：该提交使用 Remotion License，不属于本项目 Apache-2.0。个人、最多 3 名员工的营利组织、非营利组织和非商业评估可按 Free License 使用；其他营利组织需要 Company License。
- 允许边界：符合资格者可以免费、包括商业用途地使用 Runtime 生成视频和图片，并可为自有使用场景修改；禁止为了出售、出租、许可、再许可 Remotion 派生品而复制或修改 Remotion 代码。
- 集成方式：本仓只保存 adapter、版本/提交和哈希合同，不 vendor Runtime、Skills 或 `node_modules`。物化工程复制固定提交的 `LICENSE.md`，并要求操作者显式声明 `license_basis`。
- 发行边界：Remotion 输出保持 `candidate_only` 并进入本仓既有 FFmpeg/QA；本项目许可证不覆盖 Remotion、外部浏览器、npm 依赖、用户素材或生成媒体权利。

## 智能重取景开源依赖

### 可选依赖

- `Breakthrough/PySceneDetect`：固定审阅提交 `131c5f60c9d40dd8fd281941d9b10a6a44fed4cc`，BSD-3-Clause。本项目通过可选 Python adapter 调用其 `SceneManager`、`AdaptiveDetector`／`ContentDetector`，只生成镜头边界候选；不复制源码、不使用其视频写出路径。
- `google-ai-edge/mediapipe`：固定审阅提交 `85c0cdc7d70653888903c88df45962b764296342`，Apache-2.0。本项目参考 AutoFlip 的 required/preferred 约束和镜头内运动策略，并为当前 Tasks `FaceDetector` 提供薄 adapter；不嵌入 legacy AutoFlip graph。模型资产不随代码许可自动获得授权，见 `MODEL_ASSET_LICENSES.md`。
- `tryolabs/norfair`：固定审阅提交 `e517b4236f6b67a6ecf342f5df1fccb7788dbc54`，BSD-3-Clause。本项目仅在显式安装 optional extra 时调用 `Detection`／`Tracker` 生成非身份 track ID，不使用其视频读写帮助层。
- `libvips/libvips`：固定审阅提交 `8bc2e4fc64f108a5aa830f24c96ee49d91b18649`，LGPL-2.1-or-later。本项目不 vendor、静态链接或重新许可 libvips；可选 `pyvips` adapter 只调用外部原生 runtime 的 `smartcrop(interesting=attention)` 生成静态候选框。
- 发行边界：上述上游源码、原生库、Python optional dependencies、模型、媒体和运行缓存均不进入本项目 wheel/sdist。缺依赖、缺模型或版本不明时 doctor 显式 blocked，不自动下载或切换实现。

## 自动打码可选外部工具

- `ssrajadh/sentryblur`：固定审阅提交 `ded52d269b6e8e19dbbaf79f353426da3a228a98`。本项目只从显式外部可执行文件调用 `faces`/`plates`，并绑定可执行文件、独立模型缓存根、根内模型和输出 SHA-256；运行前后缓存清单必须一致。禁止 `--last`、`prompt`、自动模型下载和静默 fallback。上游源码、模型、缓存和媒体不进入发行包。
- `nyattic/CloakFrame`：固定审阅提交 `abdf376799e88d4762d4534cfdf620a91f621c8f`。本项目只生成 GUI 交接和 adoption 合同，不复制或分发其 Qt/C++ GUI、跟踪器、模型、FFmpeg 编排或源码。当前本机未构建运行时，能力保持 `source_reviewed_runtime_unverified`。
- 能力边界：SentryBlur 支持人脸/车牌 pixelate 或 Gaussian；CloakFrame 支持 Mosaic/Gaussian、双向轨迹、误轨排除和手工轨迹。两者都不提供纯色遮挡，`solid` 路线关闭失败。所有输出仅为 `candidate_only`，必须重新进入隐私扫描。

## 无仓库级许可证的审阅来源

### Jiang59991/videocut-kit

- 固定提交：`b07990f9e57e6eeb801887fa3e36af5c8450ae68`
- 许可证状态：审阅时未发现根 LICENSE/COPYING/NOTICE。
- 允许边界：发行包不复制、不修改、不分发其文件。口播净剪只在用户显式提供外部源码路径后调用固定哈希的 `prepare_judge.js`、`snap_boundaries.js` 和 `auto_cut.js --dry-run`；前者只整理候选与全文，中间语义判断由 Agent 通读完成，后两者只生成 NLE 删除/保留区间，不执行上游 FFmpeg 剪辑。
- 本项目 Apache-2.0 不授予该上游代码的任何权利。

### waker240/FullVideoProductionSkill

- 固定提交：`f3b13fb590c775f66b7d7a43834033545502d302`
- 许可证状态：审阅时未发现根 LICENSE/COPYING/NOTICE。
- 允许边界：本项目只保留来源标注和独立实现的概念性合同，不复制 Remotion 组件、skill 文件或其他上游源码；发行包不包含上游文件。
- 本项目 Apache-2.0 不授予该上游代码的任何权利。

## 外部 Provider 与创作工具

ChatCut、HyperFrames、HeyGen、Remotion、MediaKit、Premiere、ComfyUI 及其他 Provider/工具不作为本项目源码或发行资产打包。它们仅能通过显式授权、外部安装或合同边界使用，并继续适用各自许可证、服务条款、素材权利和费用规则。

数字人 renderer 的合成 fixture 不包含或调用 MuseTalk、LatentSync、Duix-Avatar、EchoMimic、LivePortrait、InfiniteTalk、HunyuanVideo-Avatar、Hallo2、GeneFace++、Ultralight-Digital-Human、HeyGen 或其他数字人实现。上述候选在进入真实 adapter 前必须先加入 canonical source ledger，并独立完成代码、模型权重、间接依赖和商用条款审阅；本项目的 Apache-2.0 不覆盖它们。

## 完整许可证文本

本项目未 vendor 上述第三方源码，因此不在 wheel/sdist 中复制每一份上游许可证全文。安装的 Python 包、用户提供的源码树和外部二进制必须保留其原始许可证。若未来 vendor 任一第三方文件，发布者必须同时加入对应完整许可证、版权声明、修改说明和必要 NOTICE，之后才能发行。

## 2026-09-04 10:00:10 | Supervision 可选标注器

执行者：Codex（精确模型版本未公开）。Supervision 0.30.1，MIT，官方源码提交 `5f25aa0ee6dc22891415b6e3d2e1689ce7a32952`。意图：复用成熟可视化标注；决策：仅通过可选 Python 依赖调用 BoxAnnotator、RichLabelAnnotator、TraceAnnotator；理由：不复制检测器、跟踪器或视频写入器；证据：固定源码审阅及合成 FFmpeg 双跑；生效范围：诊断模式；回滚：移除可选依赖并使用默认 crop 预览。发行包不包含 Supervision 源码、模型、测试视频或系统字体。
