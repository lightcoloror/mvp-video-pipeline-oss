# 脚本到候选成片离线闭环

更新：2026-08-31
执行工具/模型：Codex（GPT-5.6 Terra）

## 稳定入口

```powershell
python scripts\autonomous_video_candidate.py run --output-root .test-runtime\autonomous-candidate
python scripts\autonomous_video_candidate.py validate --receipt .test-runtime\autonomous-candidate\receipts\run-receipt.json
```

入口只消费仓库内公开、虚构的 `semantic-motion`、`vkp_bundle` fixture，以及显式的 `fixture-attestation.json`，再使用已经固定审阅的本地 HyperFrames、video-use 和 artifact-lifecycle-core 源码。它会生成：脚本/时间线输入清单、VKP 只读编辑桥接、语义动效计划、实际 HyperFrames 候选、字幕 sidecar、合成音轨候选、技术 QA、video-use ReviewPacket 和 `media` 生命周期清单。

## 运行边界

- 所有成片都是 `candidate_only`，不等同于真人、真实素材或可发布成片。
- 不调用在线模型、Provider、下载、上传、发布或 TTS；不会修改源 fixture、VKP 或用户媒体。
- HyperFrames 负责已固定源码下的动效渲染；后续静音轨和合成音轨均通过项目唯一 FFmpeg outlet。
- ReviewPacket 是只读审阅交接，不授予编辑或发布权限。生命周期运行 `plan_only`，不执行 quarantine/purge。
- fixture attestation 只允许项目自有公开合成素材；客户、个人素材、凭据或把 demo 写成生产成功的标记都会 fail-closed。

## 已验证的闭环

`2026-08-31 01:30:11 +08:00`，本地固定 HyperFrames/Chrome 对 6 秒公开合成画面完成一次实际渲染；随后以相同输出根双跑，第二次只做哈希校验并返回 `resumed_verified`。候选 MP4 的 SHA-256 为 `38290e45b9eac591663a516139fad2a807463ad0e768d80cc59b513f9e800163`，尺寸 `1080x1920`、`30 fps`、时长 `6.016 s`；技术 QA 通过，black/silence 比例均为 `0`。完整机器回执见同目录 `AUTONOMOUS-VIDEO-CANDIDATE-20260831-05-completion-receipt.json`。

## 变更理由

| 项目 | 内容 |
| --- | --- |
| 意图 | 给接手 Agent 一个可一条命令复跑的、实际产出候选 MP4 的离线闭环。 |
| 决策 | 复用现有 semantic motion、VKP bridge、FFmpeg receipts、technical QA、video-use 和 media lifecycle adapter，只补薄胶水。 |
| 理由 | 避免新增渲染器、语音路线、审核状态机或第二套 FFmpeg 编排，同时保留每个输入、输出和工具版本的哈希证据。 |
| 证据 | `receipts/run-receipt.json`、`qa/qa-report.json`、`review/review-packet.json`、`lifecycle/media-manifest.json`。 |
| 生效范围 | 仅 `autonomous_video_candidate` 入口、合成 fixture 与本说明；现有生产主链不变。 |
| 回滚 | 删除新增入口、测试 fixture 与本说明；运行输出位于调用方指定目录，不会影响源 fixture。 |
