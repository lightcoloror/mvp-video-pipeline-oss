# 表达型笑声字幕接入

2026-10-11 16:52:15 +08:00 | Codex（GPT-6）

可选 `scripts/expressive_captions.py build / validate / render` 为已经锁定、已烧录稳定台词/歌词的候选视频增加独立笑声层。沿用 packaging 主时钟、字号策略、final-layout、资源策略和唯一 FFmpeg 出口；不改台词、音量或剪辑时长。

2026-10-11 17:21:00 +08:00 | Codex（GPT-6）：用户认为下述早期 ASS 版过小、单调。现增加 [综艺笑声花字与透明贴图](variety-laughter-stickers.md)，支持 `variety / variety_reduced`；新增路径和验证范围见该文，原有三模式作为简单降级保留。当前大字版已实渲染，用户观感仍待复核。

## 参考项目如何吸收

2026-10-11 17:42:20 +08:00 | Codex（GPT-6）：综艺配方已加入逐字力度、联动回弹与错开退出。真实原声预览可显式使用 `timing_candidate` 状态，要求待回听声明，不能冒充人工已验证；字段及实际验证范围见综艺贴图文档的最新记录。旧版规则中的人类已验证与合成状态继续保留。

| 参考 | 本次接入 | 边界 |
|---|---|---|
| ASS / Aegisub / PyonFX 路线 | 直接生成简单 ASS：角色色、固定位置、比例渐大、线性上飘和淡出 | 简单效果无需 PyonFX；复杂曲线再评估 Captioneer |
| Captioneer | 真实声音事件与视觉参数分开，收尾时长显式记录 | 未安装或运行 Captioneer；不接 autopilot、重识别、自动删词 |
| Chorus | dialogue 与 sfx 分层、先布局再 transform | 不复制白色音效默认值，不按音高/音量猜情绪，不新增 MCP |
| PHONT、创意字幕及声音事件研究 | 动效集中到有声音表达价值的位置；静音与有声各检查一次 | 不推定动态字幕总能提高理解或自动识别准确 |

来源与源码核对：[研究记录](../../../source-reviews/expressive-captions-20261011/review.md)。当前选择轻量 ASS 适配，不引入另一套完整字幕应用。

## 流程位置

1. 粗剪完成基础音量、稳定字幕与歌词缺漏检查。保留完整唱句、表情和真实笑声尾巴。
2. 锁定剪辑及 packaging，从已校对的源音/剪辑映射把声音事件投影到当前帧号；回听核实身份、声音、起止和节奏。
3. 声音事件 sidecar 绑定当前 program、packaging 和复核证据的 SHA-256。它是当前剪辑的事件索引，不是另一份剪辑时间轴。
4. 指定角色及角色色，为事件选择 stable、grow 或 float_fade。普通对白继续保持稳定。
5. build 检查时钟、字体字形、最小字号、最大缩放与整个移动范围；所有表达层与台词/人物/证据/进度条共同接受 final-layout 检查。
6. render 经现有资源门和 FFmpeg 出口生成新候选，复制全部音轨，完整解码核对帧数。线程数使用现有资源策略，不写死新的并发数；资源暂停不自动重试。
7. 静音辨认角色和回应；有声核对原声起止/节奏/力度；手机尺寸复核。按新候选哈希更新最终隐私与发布前检查。

多个包装串联时，后一个组件绑定前一个候选及它输出的 final-layout.json，沿用同一 packaging。不能各自从原底片渲染后认为已经合并。

## 数据与执行

[事件模板](../examples/expressive-captions/events.template.json) 与 [效果模板](../examples/expressive-captions/spec.template.json) 的占位路径、哈希和角色说明必须替换；使用既有 `artifact_reference()` 生成文件引用。

- events：绑定 program/packaging/evidence，`review_status` 为 synthetic_fixture 或 human_audio_verified；后者需真实回听记录，字段本身不证明验收。
- 每个事件：event_id、scene_id、speaker_id、kind=laughter、start_frame、end_frame、text、fallback_text。
- 帧号以锁定成片为零点，前闭后开，事件须在本镜头内。没有逐声锚点时只做整段“哈哈哈”；有每声“哈”的真实锚点才拆事件，不能平均分配后称精准同步。重叠事件需不同安全位置，或合并为一个已核实声段。
- spec：字体文件、字号、已有 typography profile、角色表和按 event_id 索引的效果。角色必须有名字、颜色、身份建立方式和颜色理由。示例青色不是父亲或账号的全局默认色。

| mode | 参数约束 |
|---|---|
| stable | end_scale=1，rise_px=0，tail_frames=0 |
| grow | 固定位置，终值为起始字号的 1–1.35 倍，rise_px=0，tail_frames=0 |
| float_fade | 小幅向上移动，末段淡出；视觉收尾最多 0.5 秒且不跨镜头，不延长声音 end_frame |

```powershell
python scripts/expressive_captions.py build --program <base.mp4> --packaging <packaging.json> --layout <final-layout.json> --events <events.json> --spec <spec.json> --workspace-root <project-root> --output <caption-plan.json>
python scripts/expressive_captions.py validate --plan <caption-plan.json>
python scripts/expressive_captions.py render --plan <caption-plan.json> --output-root <new-output>
```

需 FFmpeg/libass；字体检查用可选依赖 expressive-fonts（fontTools），本机已有，本次未安装软件。内嵌软字幕轨会阻断，须先合成稳定字幕，防止丢字幕。外置 SRT 也须先走原有字幕流程。

输出 captions.ass、plain-events.json、candidate.mp4、final-layout.json 和回执。plain-events.json 保留真实声音区间、角色名及“父亲笑”等普通说明，供原有普通字幕/文本导出使用；它不是已经合并好的 SRT。动效不合适时可退为 stable；身份或原声待核时不能默默补入。

## 验证边界

三段 6 秒合成样例各解码 144 帧，音轨哈希与底片一致；检查了 360 像素宽首现前后、极值和退出画面。合成样例没有真实笑声音轨，只验证实现、角色对应和布局；真实笑声节奏、表达力度、观看舒适度和用户接受仍待项目内验证。

边界框是保守规划范围，不能替代像素检查与完整播放。ASS 线性移动、厘秒时间量化；当前不实现任意曲线、逐字舞动、情绪推断，也不承诺跨环境像素一致。字体、源片、剪辑、角色色、事件或字号策略改变后，旧计划失效，须重新 build。
