# Animation source routing

2026-10-27 · Codex

This change extends the existing semantic-editorial command with an upstream source catalog and explicit scene recommendations. It does not install or run upstream tools. This share snapshot contains 34 public/reference source records and 24 techniques; workstation-specific integration records remain local.

```text
python scripts/semantic_editorial.py animation-catalog
python scripts/semantic_editorial.py object-scene-route --visual-job flow --technique diagram --technique continuous_transform --delivery video --narration-timing unverified
python scripts/semantic_editorial.py object-scene-route --visual-job authored_performance --technique song_pv --technique character_performance
python scripts/semantic_editorial.py object-scene-route --visual-job flow --technique interactive_web --delivery interactive
```

`--technique` and `--source-project` may repeat. Explicit source IDs restrict the source list; an incompatible source retains its unresolved fit. `--output` writes the result JSON. Unknown IDs fail instead of substituting an unreviewed implementation.

Video, native interaction and still images have separate delivery needs. Interactive/still requests do not require narration or a video renderer. `authored_performance` designs original character media before returning it to the existing media route; real people and source evidence retain their preservation rules.

Source records distinguish workflows, shots, foundations, renderers and editors. Public commits and module paths are research evidence, not installation or runtime claims. The share catalog intentionally has no local file paths or local evidence hashes. Recommendations explicitly report that local evidence must be supplied and checked before adaptation.

The optional object-authoring integration is read directly when present. When absent or unreadable, `existing_component_catalog_status` is `existing_source_inventory_unavailable`; source browsing still works. Old default components remain candidates pending object, relation and state-model fit. A generic mechanism request does not prove that a slider-crank is suitable.

Narration timing flags describe evidence, not proof. Word/sentence declarations still require validation of the actual timing artifact; unverified timing is never estimated from character counts. A stateful interactive demo needs a deterministic time adapter before video sampling.

Start with a complete static composition, then adapt only the necessary upstream objects, labels, assets, camera and time interface. Check event boundaries, transition midpoints, text measurements, object identity and source semantics before full-speed playback and audio review. Source selection does not certify these downstream checks.

## Change rationale

Intent: reuse reviewed source modules across varied animation needs. Decision: extend the existing selector and CLI with a data catalog, without a new renderer. Reason: preserve owned scene/time/media contracts while keeping source evidence distinct from execution. Evidence: fixed upstream source paths, baseline compatibility checks and selector tests. Scope: explicit recommendation queries only.

Intent: make the upload repository-neutral. Decision: remove local paths, usernames, local inventory hashes, internal integration entries and internal discovery topology from this share snapshot. Reason: the upload review rejected those environment details. Evidence: a frozen-payload metadata scan and clean-checkout tests. Scope: uploaded copy only; complete local research and configuration are preserved.

Intent: avoid bundling unrelated unpublished local integrations. Decision: move only the new CLI commands and make the object catalog optional. Reason: source browsing should work on a clean checkout. Evidence: missing-module regression. Scope: this feature branch; other local changes are not included.

## Techniques

- `diagram`: 关系、权限与流程图
- `anatomy_mechanism`: 器件结构与机构动作
- `algorithm_trace`: 算法与路径搜索
- `collage`: 纸质拼贴与剪纸
- `sticker`: 贴纸与综艺贴图
- `kinetic_type`: 字体与排版动效
- `continuous_transform`: 一镜到底与连续变形
- `character_performance`: 角色动作与情绪
- `song_pv`: 完整歌曲 PV
- `watercolor`: 水彩与水墨
- `illustration`: Canvas 2D 插画
- `sketch`: 粗线与手绘草图
- `three_d`: 三维空间与机构
- `clay`: 黏土材质动画
- `mathematics`: 数学几何与公式推演
- `whiteboard`: 板书与笔迹揭示
- `data_chart`: 数据与图表动画
- `interactive_web`: 网页交互与事件动画
- `narration`: 口播与语义 B-roll
- `image_motion`: 照片局部运动
- `editing`: 可编辑工程与交付
- `rendering`: 渲染与采样
- `style_exploration`: 风格样片与提示词参考
- `native_generated_collage`: 原生生成式拼贴参考
