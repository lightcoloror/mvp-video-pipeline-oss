# 外部动效组件与提示音示例

本目录只展示显式 override 的合同格式，不包含第三方组件源码、音频或本地绝对路径。

先由 `video-external-motion-assets catalog` 生成目录，再用已有 `semantic_motion_plan.v1` 生成选择计划。只有确需覆盖语义默认时才传 `--overrides`。

组件 ID 必须存在于当前固定目录；提示音 ID 必须来自已审核 pack。无合适提示音时省略 `sfx_asset_id`，系统会保留 `no_sfx`，不会随机兜底。
