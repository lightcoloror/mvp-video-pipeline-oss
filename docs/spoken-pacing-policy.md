# 创作者口播气口预设

更新：2026-10-26（Asia/Shanghai），Codex（当前运行型号未另行核验）。

## 判断与依据

保留自然气口、保护尾音、保留有意义的停顿、先生成候选再复核，这些原则合理。但没有找到支持“所有口播统一保留 0.25–0.35 秒、所有句间只留 0.12–0.18 秒”的通用标准。以下数字是本项目的剪辑起始预设，不是声学检测结果或普适最佳值。

- [Descript 的官方流程](https://help.descript.com/script-editing/shorten-word-gaps)将“检测多长的间隔”和“缩短后保留多长”分开，并支持回听上下文。本文采用这种分离；不把产品示例数值当行业标准。
- 普通话研究表明，停顿和边界前延长能够帮助理解句法结构：[Tsukuma 等，1990](https://www.isca-archive.org/icslp_1990/tsukuma90_icslp.html)。停顿时长也与语篇边界层级相关：[Yang 等，2005](https://www.isca-archive.org/interspeech_2005/yang05_interspeech.html)。因此不应把所有气口压成同一长度。这些研究没有给出本表的剪辑阈值。

## 默认值

单一配置来源：`src/video_creation_pipeline/policies/spoken-pacing.v3.json`，名称 `natural_conversational_v3`。

| 情形 | 进入压缩候选的阈值 | 回听确认后的起始保留目标 |
| --- | --- | --- |
| 普通句内空隙 | ≥0.50 秒 | 0.30 秒；通常从 0.25–0.35 秒调整 |
| 普通句间停顿 | ≥0.90 秒 | 0.55 秒；通常从 0.45–0.70 秒调整 |
| 开头空白 | ≥0.40 秒 | 0.20 秒 |
| 强调、情绪、话题转换、有意义的呼吸、轮次转换 | 不按长度自动压缩 | 保留原长，不设 1 秒等上限 |
| 位置不确定或短于对应阈值 | 不产生压缩建议 | 保留原长，不为凑目标补长 |

句尾 **0.15 秒**是词尾时间标注后的保护余量，参考区间为 0.12–0.18 秒，**不是整个句间间隔**。下一词前另留至少 0.10 秒。普通 0.30 秒气口默认两侧各 0.15 秒；0.25 秒气口分配为尾部 0.15 秒、起音前 0.10 秒。当前预设最低保护不能通过单行决策覆盖；如确需改变，须显式修改策略、复测并生成新版本。

目标可以按声音与上下文调整，不强制落在推荐区间。句间保留低于 0.45 秒时，须填写 `override_reason`，且仍满足两侧最低保护。ASR 词边界可能偏移，固定余量不能保证尾音完整；试听后有风险应保留或增加余量。

## 接入与复核

查询实际生效配置，不读取另一个手抄版本：

```powershell
python scripts/spoken_rough_cut.py pacing-policy
```

两条现有入口共用预设与验证函数：

1. `spoken_rough_cut`：`prepare → decision-template → 回听/全文复核 → compile`。长停顿默认 `review_required`、置信度 0，建议仅放在 `recommendation`；不预填已批准的 `compress`。
2. `video_use_edit_loop`：词间隔候选保持 `keep_pending_review`。同 utterance/跨 utterance 仅用于初步选择句内/句间目标，不能自动认定句法边界或情绪。选中停顿时，必须通过同一音频与上下文复核。

主入口中的一条已复核决策可填写如下字段；须保留原有候选 ID、区间与位置，不把其他类型冒充停顿，也不能把停顿改名绕过保护：

```json
{
  "action": "compress",
  "preserve_s": 0.30,
  "confidence": 0.90,
  "reason": "回听后确认是句内普通空隙，切口两侧词音完整",
  "pause_review": {
    "semantic_role": "ordinary",
    "audio_checked": true,
    "context_checked": true,
    "evidence": "填写实际音频版本、源区间和回听结论；不能照抄为已试听"
  }
}
```

次入口在 selection 中使用 `pause_review: {"suggestion-001": {同上复核字段}}`。`emphasis`、`emotion`、`topic_change`、`breath`、`turn_taking` 只能保留/待复核。未知作用也不能压缩。代码验证复核记录，不能证明填写者确实听过声音；不得用勾选字段代替回听。

词时间之间的空隙不等于静音，可能仍有换气、笑声、句尾延长或背景回应。重要原声事件沿用现有字幕覆盖与声音事件检查。保留区间与整句删除发生冲突时阻断编译并回看上下文，不能让“去重复”暗中覆盖已保留的停顿。

## 编译保护与旧版本

- 禁止把停顿直接设为 `delete`；`compress` 只删除中部，保留两侧保护。
- 压缩范围不得覆盖现有非空隙词 token。实际 token 本身不准时，须先校正或保留候选。
- 现有 `snap_boundaries.js` 运行后，把停顿删除区间限制回安全窗口，并向内取百分之一秒，适配固定版本 `auto_cut.js` 的输出精度。缺失或更改停顿身份会失败。
- 检查最终实际 `delete_segments`，防止吸附、合并或其他删除吞掉气口/尾音。次入口继续采用向内取视频帧边界，不扩大候选删除区间。
- 原有 `natural_conversational_v1` / `natural_conversational_v2` 判定包及计划可继续校验、读取，保留历史含义；不得以旧包生成新模板或重新编译。新工作在新输出目录重新 prepare，逐条复核，不给旧版结果贴 v3 标签。参数变化使判定包哈希变化，旧审核不能自动沿用。
- 本变更不重新处理既有项目或导出视频。真实项目仍须正常速度试听连续句子，检查断字、连读过急、呼吸/情绪丢失及音频切口；纯合成测试不能代替听感验收。

测试入口：`tests/test_spoken_pacing.py`、`tests/test_spoken_rough_cut.py`、`tests/test_spoken_delivery.py`、`tests/test_video_use_edit_loop.py`。其中固定源码集成用 `VIDEOCUT_KIT_SOURCE_ROOT` 指向已有 `videocut-kit`，运行真实 Node 脚本的 dry-run，只使用合成 token 与占位文件，不启动 FFmpeg。

2026-10-26 补充（Codex，精确运行型号未独立核验）：v3 保留 v2 的气口数值，按用户补充将普通重录设为优先最后一遍候选；同时接入首尾、切口和粗剪音量检查。完整字段与另外九类预设见 [创作者剪辑预设](editing-presets.md)。
