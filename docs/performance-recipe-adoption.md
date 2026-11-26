# 可编辑的注意、理解与反应配方

记录：2026-10-10 10:36:58 +0800，Codex（gpt-6-astra / ultra）；已核对原生 turn_context。

本增量在 `explainer_recipes.build_performance_recipe` 和既有
`scripts/audio_timeline.py` 中增加一个显式选择的合成物件配方。场景依次为
attention、understanding、reaction，示例是机器人先看向纸箱、核对易碎标记、
再把同一个纸箱轻放到托盘。它是可编辑的测试素材，不是默认人物 IP 或真实软件证据。

## 采用决定与来源

研究包为 `<workspace>/source-reviews/motion-workflow-intake-20261009/`。
五项输入和所列 35 个文件的 SHA256、字节数及 Git blob 均已在本任务重新核验。
它是固定提交的源码子集，不能当作安装完整的上游工程。

| 来源与符号 | 决定 | 本地落点及理由 |
|---|---|---|
| ClaudeAnimationBase `0ac8bf2b31942376cb6b8c4074715595d512acd2`：`clawd.js` 的 `feel/emotions`，`core.js` 的 `arcPt/spring`，`timeline.js` 的 `fn(t,lt,dur)` | adapt | 新增 `performance_recipe.pose_at`：视线变化、确认点头、搬运弧线与有限落点缓冲。参考表演结构和纯时间函数，独立绘制灰色合成机器人，没有复制 Clawd 形象、水彩、音乐节拍或持续抖动。四个相关源码/许可证文件固定绑定到每段依赖。MIT 来源保留。 |
| OneTake `f5b1858b4b3e69d49c4cd7374f7c77761e8a1545`：`motion.js:morphRect/camera/view`、`ui_kit.js:stage/timeWarp` | reference / no_delta | 既有 `remotion_project.build_remotion_timeline_preview` 已提供场景投影；`author_bindings` 提供时间锚点。新配方保留 `object_id`、端点姿态并使用包装计划全局秒，不导入另一套变形、相机或 timeWarp。当前样例没有相机重构的实际缺口。上游非商业条款保留；未复制库。 |
| PDoomVideo `fa546a38092e75f2b079e6a86d6abc54dd525d17`：`timeline.js:chapter/drawWorld`、`src/ch/c01_lab.js` | no_delta | 既有 `object_scenes.object_scene_projection` 和 `remotion_project` 已计算全局、局部及持续帧数；组件依赖已有固定哈希。新配方继续复用场景投影，不创建章节 DSL 或 Agent 调度器。该项目许可证未确认，未复制代码或歌曲材料。 |
| DogSC `e207d11b111c5e58152238fb64115b2009850547`：`AutoZoomPlanner.makeAnimations/repairingLegacyAutomaticAnimations` | reference_only | `smart_reframe`、`semantic_motion` 已处理显式焦点区域；本合成样例没有点击录制或自动相机轨道，不需加入点击聚类。保留自动/手工轨道分离的参考，未安装或移植 macOS App。 |

## 入口与边界

```powershell
python scripts/audio_timeline.py performance-plan --packaging packaging.json --spec spec.json --output plan.json
python scripts/audio_timeline.py performance-assets --packaging packaging.json --plan plan.json --cache visual-cache --output new-assets
python scripts/audio_timeline.py render --packaging packaging.json --binding audio-binding.json --cache assembly-cache --output new-candidate
```

从 `performance-assets` 的 manifest 取每段 `visual`、`provenance`、
`visual_start_s`、`semantic_description`，按已有 `audio_timeline_binding.v1`
绑定显式 PCM 音频和字幕。未增加编码器、第二套时间表、Provider 或发布入口。
资产绘制使用已安装的 Pillow，编码、解码与拼接统一经过既有 FFmpeg 出口。
动作配方本身消费包装场景和 cue；没有执行下载的 JavaScript。

`performance_spec.v1` 要求 `synthetic_only=true`、显式本地字体、固定研究根、
稳定物件 ID/标签/材质色及颜色理由。三个 cue 各有 scene_id、phase、label、
全局 start_s/end_s。它们必须落在相应包装场景内并对齐帧，动作与结果停留均
至少 0.4 秒。输出为 360–720 px 的方形、12–30 fps、最多 15 秒。本次验收是
720×720 / 24 fps / 9 秒；其他尺寸只有边界合同，没有逐尺寸视觉验收。

标签按字形测量后换行，最多两行，不因变长缩小字体。缺字、标签不合尺寸、
非法数值、表达式字符串、越界时间、漂移来源都关闭失败。样例使用纸箱棕色、
灰色机器人和蓝色托盘；颜色表示材料/物件，不把品牌色或风险等级强加给场景。

每段重生成键包括本段 cue、包装投影、对象、字体、上游固定来源和本地实现
依赖。全局 spec 的路径/哈希保留在计划，不加入每段键，因此改一个标签不会
错误失效所有段。视觉缓存复用既有 `verified_cache` 的逐产物哈希验证，合成
继续复用 `audio_timeline` 的 PCM/视觉/字幕分段缓存。安装的 FFmpeg/Pillow
版本不在该键内；更换工具版本须换缓存根，延续现有音频合成器的限制。

## 验收结果与限制

本次实测保持旧 blocked 派发不变，使用全新样例：

| 修改 | 实际重新生成的段 | 实际复用的段 |
|---|---|---|
| 初版 | 三段 | 无 |
| 中间段改为长中文标签 | understanding | attention、reaction |
| 搬运动作及提示音延后 0.5 秒 | reaction | attention、understanding |
| 无变化再次执行 | 无 | 三段；最终 MP4 SHA256 与上一版一致 |

22 项相关回归通过；三个有变化的成片逐一完整解码并检查 216 帧，共 648 帧。
实际像素追踪的包裹中心与配方偏差最大约 0.54 px，两处接缝位移均为 0。
这只证明此合成样例的位置连续，不证明语义、真实人脸或任意素材的观感。
首次颜色阈值追踪误把注意提示线算进包裹；定位后按已知物件尺寸筛选捕获像素
的主体聚类，保留原失败日志，没有放宽验收阈值。

真实 FFmpeg 非零退出与截断 MP4 均拒绝通过。空记录/采集失败返回 invalid，
没有适用接缝返回 not_evaluated，不能因 considered=0 获得“满分”。
缓存标记只在编码、流信息和完整逐帧解码通过后写入，残留目录关闭失败。

已查看渲染前关键姿态、编码后关键帧和接缝三帧。用户随后在本线程原生回复
“动作连贯，提示音合适”，完成这份 9 秒候选的正常速度播放与试听确认；回执
按最终 MP4 SHA256 绑定。Agent 没有冒称自己完成播放或试听。真实素材、原声
和通用风格认可仍为 real_acceptance_pending；本确认只适用于当前合成样片。

详细证据位于 `<workspace>/outputs/motion-workflow-adoption-20261010/child/`：
`source-verification.json`、`media-inspection/verification.json`、各版本 manifest、
`native-processes.jsonl` 和每条命令的资源记录。临时目录 ACL 失败已用现有
Windows 受管测试兼容层解决；无全局权限修改。

## 变更理由和撤回

意图是把已有“静帧先行、同一时钟、对象连续、局部改稿”的规范补成可执行配方。
决定仅新增这一种合成动作资产，复用现有入口、字体检查、缓存及 FFmpeg 出口。
证据是固定源码对照、实际样片、两次局部变化及无变化重用。本增量不声称承接
旧物件拆解样例，不执行旧 blocked 验收，也不改变真实素材路由。

撤回时停止使用显式 performance-plan/performance-assets 入口，按本任务
差异移除新增文件与少量接入代码；不要 reset、stash 或删除其他未提交工作。
没有提交、推送、知识库写入、服务或系统配置变更。
