# 视频收尾归档入口

2026-10-12｜Codex（GPT-6）。本仓库复用 self-media-creation-system 的归档分级，不维护第二套归档引擎。

项目收尾默认做**可返工归档**：把已采用终稿、精剪/关键母版、字幕/音轨/动效材料、时间映射、人工修正、来源引用与配方放在同一项目归档入口，标明已压平图层和缺失材料及可修改范围。

按实际恢复目标评估是否升级为**可复现归档**：跨机/迁移目录重建、长期重新生成、复杂或版本敏感依赖、事实处理链重放、母版不足以覆盖且恢复成本高。升级后需实跑迁移目录中的声明范围并留比较回执，不能仅凭哈希正确或已打包声称可复现。明确只需回看时才选简单留存，并说明理由与原片备份引用。

使用已部署的 self-media-creation-system：

```powershell
# 在 self-media-creation-system 目录运行
python scripts/artifact_lifecycle.py archive-policy
python scripts/artifact_lifecycle.py archive-plan --assessment <项目评估.json> --output <新的归档计划.json>
```

完整规则与验收边界见[已公开主流程归档规则](https://github.com/lightcoloror/self-media-creation-system-oss/blob/34f6c1322391bc92253d763b9e03677402a2b869/self-media-creation-system/docs/video-archive-levels.md)。使用者显式配置该公开 Owner；本仓不复制其归档执行器。

`archive-plan` 只生成分级和待验收清单；`08_publish.py` 只收集发布文件。材料已收集、文件完整性、限定范围重建、上传和网盘下载回验分开记录。归档选级和缓存排除不授权删除原片，也不自动调用渲染、模型或上传。
