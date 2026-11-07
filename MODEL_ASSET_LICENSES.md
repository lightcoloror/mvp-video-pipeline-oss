# 模型、权重与媒体资产许可证边界

更新时间：2026-08-05 14:20:00 +08:00
维护者：Codex（GPT-5）

## 核心声明

仓库、wheel 和 sdist **不包含**模型权重、checkpoint、Tokenizer 缓存、LoRA、向量索引、外部源码镜像、客户/个人媒体或生成视频/音频。根目录 `LICENSE` 的 Apache-2.0 只覆盖本项目有权发布的自有代码与文档，不会把这些外部资产重新授权为 Apache-2.0。

模型名称、代码许可证和模型权重许可证是三个不同概念。即使推理代码是 MIT/Apache，权重、训练数据、输入素材和生成输出仍可能受独立条款、版权、肖像权、商标权、平台政策或地域法律约束。

## 已接入或审阅的模型路线

| 路线 | 代码状态 | 模型/权重状态 | 本发行包 | 商业使用门 |
| --- | --- | --- | --- | --- |
| ACE-Step 1.5 | 固定提交 `6d467e4b...`，上游 MIT | 审计矩阵记录固定模型快照为 MIT；实际使用仍必须保存所下载快照的许可文件与 SHA-256 | 不包含 | 只有许可快照明确允许且配置 `commercial_use_allowed=true` 才能执行 |
| Microsoft CLAP / `msclap` | 固定提交 `e8a6467b...`，上游 MIT | 权重和 Tokenizer 缓存不由本项目授权；必须由操作者提供模型文件、哈希与许可快照 | 不包含 | 未明确允许时拒绝商业运行；禁止自动下载 |
| CrispASR | 固定提交 `9deefe8f...`，上游代码 MIT | ASR 模型和二进制须单独核验 | 不包含 | 只消费外部回执；本仓不替模型声明商业许可 |
| llama.cpp | 固定提交 `39b27f0d...`，上游代码 MIT | GGUF/视觉投影模型按各自模型卡和许可证处理 | 不包含 | 必须显式配置模型 SHA 与许可，不自动切在线 Provider |
| RobustVideoMatting | 固定提交 `53d74c68...`，上游代码 GPL-3.0 | 本地实测 `rvm_mobilenetv3.pth` SHA-256 为 `3c7c1d92033f7c38d6577c481d13a195d7d80a159b960f4f3119ac7b534cf4f8`；未发现独立于仓库 GPL-3.0 的权重重授权声明 | 不包含 | 仅由操作者显式提供外部路径；不自动下载、不进入 Apache-2.0 发行包，商业使用前须独立确认权重与输入/输出权利 |
| ChatTTS 类路线 | 非默认 Provider | 已审路线包含 CC BY-NC 等非商业模型条款 | 不包含 | `commercial_use_allowed=false`；不得用于商业输出 |
| ChatTTS-LongAudio | 不复制上游代码 | 已审路线为 CC BY-NC-ND，包含非商业和禁止演绎限制 | 不包含 | 永久保持非商业候选；不得由 Apache-2.0 覆盖 |
| 其他 TTS/BGM/视觉模型 | 仅合同或 Provider 接口 | 未核验前视为未授权 | 不包含 | 需要逐模型许可快照、哈希和显式 consent |

上述缩写哈希只用于阅读；运行时合同继续使用完整提交和完整 SHA-256。

## 生成媒体

- 仓库不分发演示 MP4、音频、封面或客户素材。
- 本地 synthetic 流程运行时生成的色块、正弦测试音和候选 MP4 只是技术验收产物，固定标记为 `candidate_only`，不随源码发行。
- Apache-2.0 不自动决定生成媒体的版权归属，也不保证输入、提示词、参考音频、人物、品牌、风格或输出可商用。
- 使用者必须核验所有输入素材、字体、音乐、音效、模型、权重和输出的权利，并保存相应证据。

## 发布与打包硬边界

`.gitignore`、`MANIFEST.in` 和 setuptools package-data 排除规则共同阻止常见媒体与权重扩展进入版本控制和发行包，包括：

`*.safetensors`、`*.ckpt`、`*.pt`、`*.pth`、`*.onnx`、`*.gguf`、模型 `*.bin`、`*.mp4`、`*.mov`、`*.mkv`、`*.webm`、`*.wav`、`*.mp3`、`*.flac`、`*.m4a` 及常见图片输出。

发布者仍必须在每次发行前解包 wheel/sdist 并扫描文件名、体积、魔数、敏感路径和秘密；ignore 规则不能替代发布审计。

## 2026-08-13 数字人 renderer 许可状态

- `synthetic-avatar-renderer-stub` 是本项目自有 Apache-2.0 合同/测试实现，只生成 FFmpeg 色卡，不使用模型、权重、真人素材或生物特征。
- canonical `SOURCE_INVENTORY.json` 当前没有 MuseTalk、LatentSync、Duix-Avatar、EchoMimic、LivePortrait、InfiniteTalk、HunyuanVideo-Avatar、Hallo2、GeneFace++ 或 Ultralight-Digital-Human 的正式记录；这些名称不得写入运行回执为“已审阅”“已安装”或“可商用”。
- 真实 renderer 接入前必须分别固定：上游代码提交与许可证、模型/权重快照与许可证、间接依赖、输入/输出权利、商用条款、本人肖像和声音授权、数据保留/删除/训练 opt-out、地域和费用上限。
- 任一证据缺失时 adapter 状态必须为 unavailable，且禁止自动切换其他本地模型或云 Provider。

## 2026-08-23 智能重取景模型资产边界

- MediaPipe 源码许可证不等于 Face Detector、Object Detector、Pose、Image Segmenter 等 `.task`／TFLite 模型资产的许可证。每个模型必须由操作者显式提供，记录文件 SHA-256、来源页面、模型卡、许可证快照和允许用途。
- `video-smart-reframe detect-faces` 不下载模型；缺文件时关闭失败。运行回执和 evidence provider 必须绑定实际模型 SHA-256，不能只写模型名称。
- PySceneDetect 和 libvips smartcrop 路线不需要模型权重；Norfair 只消费检测结果，不提供检测模型。它们也不能替代 MediaPipe 缺失后静默生成“人脸证据”。
- 当前仓库和发行包不包含重取景模型、真人脸部样本、客户媒体、跟踪数据库或生物特征模板。track ID 只表示同一运行内的几何轨迹候选，不声明人物身份。
- 更新记录：Codex (GPT-5.6 Sol)，2026-08-23 11:34:00 +08:00。生效范围仅为智能重取景 optional Provider；删除 adapter 即可回滚，不影响既有安全布局与单一 FFmpeg 出口。

## 2026-08-24 自动打码模型资产边界

- SentryBlur 人脸路线使用 InsightFace SCRFD，车牌路线使用 `open-image-models` YOLOv9-T；代码仓库许可证不自动覆盖模型权重、训练数据、输入客户媒体或输出权利。
- `privacy_redaction_job.v1` 的自动路线必须显式绑定独立模型缓存根、每个目标位于该根内的本地模型文件与 SHA-256，并生成稳定的模型及缓存根清单哈希。适配器把 HOME 与常用模型缓存变量定向到该根，设置离线环境和黑洞代理，且要求运行前后根目录清单哈希不变；缺模型、越界路径、缓存改写或哈希不一致时关闭失败。
- CloakFrame GUI 接回必须在 adoption spec 中记录实际工具版本和模型哈希。当前本机未构建或运行 CloakFrame，因此不得把源码审阅写成真实模型执行证明。
- 仓库、wheel 和 sdist 不包含 SentryBlur/CloakFrame 模型、外部源码、客户媒体、预览图或打码输出。更新记录：Codex (GPT-5.6 Sol)，2026-08-24 14:16:41 +08:00。
