# 脚本与动效同步提示音示例

本示例只包含虚构文本与时间线，用于验证 `video-motion-sfx` 的候选计划和合成链。它不会调用在线模型、下载素材或发布视频。

先从固定的 Video Shotcraft 源码快照物化本地暖纸张／木质音效包：

```powershell
video-motion-sfx bootstrap-pack `
  --video-shotcraft-root <fixed-video-shotcraft-source> `
  --output-root <managed-pack-output>
```

根据脚本、场景和 Remotion 帧级动效生成试听计划：

```powershell
video-motion-sfx plan `
  --packaging-plan examples/motion-sfx/packaging-plan.json `
  --remotion-project-data examples/motion-sfx/remotion-project-data.json `
  --script examples/motion-sfx/script.txt `
  --density balanced `
  --pack-receipt <managed-pack-output>/motion-sfx-pack-receipt.json `
  --output-root <empty-managed-run-output>
```

把提示音混入已有候选 MP4：

```powershell
video-motion-sfx run `
  --packaging-plan examples/motion-sfx/packaging-plan.json `
  --remotion-project-data examples/motion-sfx/remotion-project-data.json `
  --script examples/motion-sfx/script.txt `
  --pack-receipt <managed-pack-output>/motion-sfx-pack-receipt.json `
  --program <candidate-program.mp4> `
  --output-root <empty-managed-run-output>
```

输出始终是 `candidate_only`。`needs_audition` 表示必须试听，技术 QA 或生成成功不构成发布许可。
