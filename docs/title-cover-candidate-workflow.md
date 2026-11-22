# 标题封面候选工作流指南

- 许可：Apache-2.0
- 状态：仅生成候选与方法参考（`candidate_only`、`publish_authorized: false`）
- 原指南归属与时间：`Codex (GPT-5.6 Sol)`，`2026-08-21 16:40:00 +08:00`（保留原始归属与时间）
- 公开源整理：`Antigravity`，`2026-11-22`（基于公开基线 `2b45f93cdf1d439485cdfb3e7447ce57de2d9b4f` 核对既有一方 CLI 与测试源码；未修改代码、未运行包命令或渲染器）

## 1. 流程定位

标题和封面是 canonical 视频主链中的前置候选包装阶段，位于 `content-video-brief` 与 `content project plan` 之后、拍摄和高成本素材生成之前；选定方案会继续投影到既有的 `creative_alignment_plan.v1`，不建立第二个内容真源、独立渲染器、FFmpeg 出口或发布门。

```mermaid
flowchart LR
    A[content-video-brief] --> B[content project plan]
    B --> C[三组标题封面候选]
    C --> D[开头与证据绑定]
    D --> E[适用内容保险扫描]
    E --> F[工作候选选择]
    F --> G[Remotion Still 多比例候选]
    G --> H[尺寸与 360 px 小图视觉 QA]
    H --> I[creative_alignment_plan]
    I --> J[拍摄/素材生成/剪辑]
    J --> K[人工审核与 release gate]
    K --> L[条件允许时的平台实验与只读复盘]
```

## 2. 三组表达策略与结构绑定

`title_cover_candidate_plan.v1` 要求在同一计划内提供三种表达策略：

- `searchable`：清楚说明问题、对象与收益，适合搜索或知识检索入口。
- `curiosity`：保留合理信息缺口，但必须在开头兑现，不得制造无关悬念。
- `evidence_led`：突出数字、对比、人物或证据结构，所有数字与结论必须回溯到已有来源。

每组候选须绑定：标题、互补而非照抄标题的封面文字（`overlay_text`）、视觉焦点（`visual_focus`）、受众承诺（`audience_promise`）、点击理由（`click_reason`）、开头兑现摘要（`opening_payoff_binding.payoff_summary`）、既有 `scene_id` 及该 scene 下的 `evidence_refs`。标题不得脱离脚本与证据擅自扩写事实。`dbs-xhs-title` 或 `dbs-hook` 等辅助工具仅可作为候选灵感或开头检查参考，不是项目真源或自动批准器。

## 3. 封面来源与当前渲染资格边界

封面来源仅允许四种显式 `source_kind`：`dedicated_photo`（专门拍摄封面照）、`video_frame`（视频候选帧）、`evidence_visual`（已有证据视觉）与 `typography_only`（纯排版封面）。`availability` 区分 `planned` 与 `available`，`planned` 来源不得冒充可用素材。

关于从视频抽帧的参考思想：方法上可参考 MIT 许可项目 `thumbnail-video` 的“场景变化 + 均匀抽样”入围思路，再人工检查清晰度、闭眼、人脸、负空间与重复帧；因上游缺少质量评分且不支持可靠的 Windows 浏览器探测，本仓未整套复制其实现。

**当前已公开源码的渲染资格明确界定（`2b45f93cdf1d439485cdfb3e7447ce57de2d9b4f`）：**

- 在 [../src/video_creation_pipeline/title_cover.py](../src/video_creation_pipeline/title_cover.py) 中，计划构建与校验逻辑固定以 `source["source_kind"] == "typography_only"` 判定 `render_eligible`：
  - `typography_only`：`render_readiness.status = "ready_for_remotion_still_candidate"`，`render_eligible = true`；
  - 其余来源（`dedicated_photo`、`video_frame`、`evidence_visual`）：即使已绑定路径与哈希，在当前计划级检查中仍为 `"blocked_requires_safe_layout_source_binding"`（`render_eligible = false`），且 `visual_quality_review_required = true`。
- **不得误认为仅提供 safe-media layout 或附加 `--include-safe-layout-media` 就能在本阶段直接解锁非纯排版封面渲染**：非纯排版路线仍须经过独立的 safe-media layout 裁切/保护区/素材复制集成确认，并履行人物授权、隐私与适用内容审核门。

## 4. 多比例独立构图与视觉 QA

同一语义方案按以下项目级渲染 profile 分别生成构图，而不是机械裁切横图：

| Profile | 项目尺寸 | 用途 |
| --- | ---: | --- |
| `youtube_16_9` | 3840×2160 | 横版自定义缩略图候选 |
| `bilibili_16_9` | 1920×1080 | 横版 16:9 展示候选 |
| `bilibili_4_3` | 1440×1080 | 横版 4:3 展示候选 |
| `vertical_profile_3_4` | 1080×1440 | 竖屏主页/资料页候选 |
| `vertical_first_frame_9_16` | 1080×1920 | 竖屏首帧候选 |

上述 profile 为本仓库定义的确定性项目渲染规格，不替代任何外部平台的实时上传校验。接入 Remotion 时会注册 `Cover-<candidateId>-<profileId>` `<Still>` 组合，各规格记录 `small_size_preview_width_px: 360`。技术尺寸校验通过不等于创意通过或具备发布许可；人物清晰度、闭眼、遮脸、文字溢出与 360 px 小图可读性仍须人工视觉 QA。

本工作流仅吸收了 `guizang-social-card-skill` 的移动端小图检查与多比例独立构图思想；鉴于其根许可证为 AGPL-3.0 且子包 metadata 存在不一致，本 Apache-2.0 仓库未复制其源码。正式渲染沿用 Remotion 官方 `<Still>` 与 `still` CLI，不另行引入 Satori 等并行渲染引擎。

## 5. 操作者前置条件与样例边界

运行以下 CLI 流程前，操作者需自行准备并确认以下未随仓捆绑的条件：

1. **配套项目输入**：操作者自备且相互匹配的 `<operator-project-plan.json>`、`<operator-title-cover-spec.json>`、`<operator-alignment-projection-spec.json>`、`<operator-packaging-plan.json>` 与 `<operator-remotion-handoff-spec.json>`。
   - **随仓样例局限**：[../examples/title-cover/title-cover-spec.json](../examples/title-cover/title-cover-spec.json) 仅是结构/字段形态示例，其 `evidence_refs`（如 `claim_fixture_contract` 等）绑定于其自身上下文，**不是**可直接脱离配套项目计划独立运行的完整工程。可参考 [../tests/test_title_cover.py](../tests/test_title_cover.py) 中 `setUp()` 如何成对构造匹配的 `project-plan.json` 与 `spec.json`（仅作源码参考，本文档整理未运行测试）。
2. **外部 Remotion 环境与兼容浏览器**：本仓不包含外部 Remotion Skills 仓库（`<operator-remotion-skills-root>`）、Remotion Runtime（`<operator-remotion-runtime-source>`）或兼容的无头浏览器可执行文件（`<operator-compatible-headless-browser>`）。
3. **真实的 Remotion 许可依据（`<operator-license-basis>`）**：`video-remotion-skill materialize` 必须由操作者根据自身主体与用途显式传入 `--license-basis`（不可依赖文档默认值），CLI 允许的枚举值为：`individual`、`for_profit_up_to_3_employees`、`non_profit`、`evaluation`、`company_license`。

## 6. 文档化 CLI 顺序与 `title_cover_plan_path` 衔接

以下参数名均逐项核对自 [../src/video_creation_pipeline/title_cover_cli.py](../src/video_creation_pipeline/title_cover_cli.py) 与 [../src/video_creation_pipeline/remotion_skill_cli.py](../src/video_creation_pipeline/remotion_skill_cli.py)（在仓库根目录执行，命令行中的 `<...>` 均需替换为操作者本地实际路径或取值）：

```powershell
# 1. 构建并校验标题封面候选计划（均为 candidate_only）
video-title-cover build `
  --project-plan <operator-project-plan.json> `
  --spec <operator-title-cover-spec.json> `
  --output title-cover-plan.json

video-title-cover validate --plan title-cover-plan.json

# 2. 仅适用于涉保险领域内容：生成并执行保险内容扫描（扫描通过不等于法律或监管批准）
video-title-cover build-insurance-request `
  --plan title-cover-plan.json `
  --policy src/video_creation_pipeline/policies/insurance-content-cn-2026-08.v1.json `
  --output-dir title-cover-insurance-inputs `
  --output title-cover-insurance-request.json

video-title-cover scan-insurance `
  --request title-cover-insurance-request.json `
  --output title-cover-insurance-report.json

# 3. 投影至创意对齐规格
video-title-cover project-alignment-spec `
  --plan title-cover-plan.json `
  --spec <operator-alignment-projection-spec.json> `
  --output creative-alignment-spec.json

# 4. 通过 title_cover_plan_path 衔接 Remotion Skills 交接、实例化与封面候选渲染
video-remotion-skill build `
  --skills-root <operator-remotion-skills-root> `
  --packaging-plan <operator-packaging-plan.json> `
  --spec <operator-remotion-handoff-spec.json> `
  --output remotion-handoff.json

video-remotion-skill materialize `
  --handoff remotion-handoff.json `
  --runtime-source <operator-remotion-runtime-source> `
  --project-dir remotion-project `
  --license-basis <operator-license-basis>

video-remotion-skill run-covers `
  --manifest remotion-project/remotion-project-manifest.json `
  --runtime-source <operator-remotion-runtime-source> `
  --browser <operator-compatible-headless-browser>
```

**参数与衔接说明：**

- **`title_cover_plan_path` 桥梁**：在 `<operator-remotion-handoff-spec.json>` 中传入 `"title_cover_plan_path": "title-cover-plan.json"` 后，`remotion_skill_cli.py build` 会将其传给 `build_remotion_skill_handoff(...)`，随后由 [../src/video_creation_pipeline/remotion_project.py](../src/video_creation_pipeline/remotion_project.py) 中的 `_title_cover_variants(...)` 校验该计划并为各候选与 profile 生成 `<Still>` 组合。
- **标题投影兼容边界**：选中候选投影至 `creative_alignment_spec.v1` 后，标题允许不同于 brief 的基准标题，但须精确匹配候选计划中选定的 `working_candidate_id`；未绑定标题封面候选计划的既有旧流程仍维持原项目标题一致校验。
- **保险内容扫描边界**：`build-insurance-request`（支持可选 `--created-at`、`--distribution-channel`）与 `scan-insurance` 仅作为涉保险内容的前置规则自查（输出 `publish_authorized: false`），**绝不构成法律意见、监管备案或发布批准**；非保险内容无需套用。
- **其余可用子命令**：`video-remotion-skill` 另提供 `doctor`（检查 Skills/Runtime/Browser 就绪度）、`validate`（校验 `handoff`）、`validate-project`（校验 `manifest`）、`validate-cover-run --receipt <receipt.json>`（校验封面运行回执）；`materialize` 另提供可选开关 `--include-safe-layout-media`，`run-covers` 支持可选 `--timeout`。

## 7. 仓内关联源码与样例索引

| 仓内路径（相对 `docs/`） | 角色 |
| --- | --- |
| [../src/video_creation_pipeline/title_cover_cli.py](../src/video_creation_pipeline/title_cover_cli.py) | `video-title-cover` CLI 入口源码 |
| [../src/video_creation_pipeline/remotion_skill_cli.py](../src/video_creation_pipeline/remotion_skill_cli.py) | `video-remotion-skill` CLI 入口源码 |
| [../src/video_creation_pipeline/title_cover.py](../src/video_creation_pipeline/title_cover.py) | 标题封面计划构建与 `typography_only` 渲染资格校验 |
| [../src/video_creation_pipeline/remotion_project.py](../src/video_creation_pipeline/remotion_project.py) | `_title_cover_variants` 读取 `title_cover_candidate_plan` 生成 `<Still>` 变体 |
| [../src/video_creation_pipeline/policies/insurance-content-cn-2026-08.v1.json](../src/video_creation_pipeline/policies/insurance-content-cn-2026-08.v1.json) | 涉保险内容扫描策略文件 |
| [../examples/title-cover/title-cover-spec.json](../examples/title-cover/title-cover-spec.json) | 标题封面 spec 形态示例（需配合匹配项目计划理解，不可独立直跑） |
| [../tests/test_title_cover.py](../tests/test_title_cover.py) | 标题封面单元测试源码（仅作构造配套输入与调用方式的参考） |

## 8. 实验复盘边界与外部参考链接

- **有条件的平台测试**：仅当目标平台原生功能支持、且内容已通过人工与合规审核批准上线后，才宜对多组标题封面方案做条件受控的实验，并联合观察展现点击率、每次展现观看时长、前段留存、完播、负反馈、有效咨询、误解与合规修改，而不只看单一点击率。例如，在过往外部资料中，YouTube 原生标题/缩略图 A/B 测试依据观看时长份额评估胜者且 Shorts 不在支持之列；所有数据回流须保持**只读**，严禁生产管线自动修改线上标题、自动上传或自动发布。
- **外部权威链接（仅供参考，未在本次文档整理中核验实时可用性或当前平台规则）**：
  - YouTube 标题与缩略图建议：<https://support.google.com/youtube/answer/12340300?hl=en-GB>
  - YouTube 标题/缩略图 A/B 测试：<https://support.google.com/youtube/answer/16391400?hl=en-GB>
  - YouTube 自定义缩略图规格：<https://support.google.com/youtube/answer/72431?hl=en-GB>
  - B站 4:3 与 16:9 封面说明：<https://www.bilibili.com/opus/1106096748969852979>
  - TikTok 安全区课程：<https://ads.tiktok.com/business/creativecenter/quicktok/online/tiktok_creative_accelerator/pc/en>
- **未执行声明**：本次文档适配未执行任何 Python 包命令、CLI、单元测试、Remotion 渲染器或浏览器环境，亦未包含任何历史运行回执或真实业务素材。
