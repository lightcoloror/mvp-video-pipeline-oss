# 像素叙事短镜头：可选风格路由

更新：2026-10-13，Codex（运行模型未独立核验）。

## 入口与实际能力

用户已选择把 Cyber Farmhands 作为对应风格的可选路由。入口是现有
`video-visual-routing build-style --preset pixel_narrative`，不是另装工具。
原有五套风格、默认选择、时间线和合成出口不变。

本次接通的是**可查询、可校验的风格配方和场景交接**，不是已经完成的像素动画渲染器。
已有 Remotion/HyperFrames 绑定能消费配色与字体，但不会自动实现角色表演。
因此匹配的场景明确返回 `handoff_required` / `authoring_required`，必须制作实际镜头后验收。
不得把普通模板换色、JSON 校验通过或生成接手文件称为像素短片完成。

## 什么时候选

- 用户明确要像素叙事、复古游戏式小场景，或已经认可对应参考帧时才选；不因主题是 AI 就默认套用。
- 第一阶段用于 `concept_explanation`、`process_sequence`、`transition_or_emphasis`。
- 已有真人素材、证据引用、数据比较、真实经历与情绪故事等场景不自动转为像素表演。
- `style_recipe_handoff.status=excluded` 表示不应用这份镜头配方，不表示缺失素材已补齐。
- 不像素化原始保单、截图、表格或医学证据；不替换真人旁白，不克隆声音，不沿用上游角色身份。
- 本路由没有 Provider 生视频出口。现有 Seedance 交接会拒绝这个 preset，不能用云端生成绕过本地镜头制作。

颜色按场景材料和信息含义选择。配方色板只是说明性场景的起点，不是个人 IP 固定配色，
不得覆盖实物、风险等级或证据中的颜色意义。文字单独保持清晰，不强制使用像素中文字体。

## Agent 怎样使用

1. 从用户认可的参考片段里确定一个表达目标；先制作一个短镜头，不整片改造。
2. 读取 profile 的 `tokens.shot_recipe`，在现有场景计划中安排静帧、少量角色/物件图层与动作。
3. 先确认静帧。复用现有 `materials` 审核，不建立新审核账本。
4. 把 `enter / point / react / hold / exit` 等命名事件绑定到已有 packaging 场景和旁白锚点。
   不按字数推算真人说话时长，不改 SRT/旁白时钟，也不重写 packaging 场景边界。
5. 由现有 Remotion 或 HyperFrames 制作局部镜头，再交单一 FFmpeg 出口。
   如现有组件不能表达所需动作，保持“待制作”；不要把更换 token 冒充角色动作实现。
6. 检查事件边界与转场中间态，每页最多九帧；另做全速观看和旁白同步试听。
   联系表不能证明运动连贯、中文可读或音画同步已通过。
7. 通过现有 `storyboard -> materials -> visuals` 审核后，保存认可镜头、素材、参数、时间锚点和修改限制。

长中文必须换行、测量或调整镜头，不得照抄上游 `lines[:3]` 静默截断。
像素角色可用整数网格和 nearest-neighbor 缩放；源证据与文字不套这个缩放方法。

## 命令

在本仓库使用既有虚拟环境，不安装依赖；若未安装 console entrypoint，可执行：

```powershell
$env:PYTHONPATH = (Resolve-Path .\src).Path
.\.venv\Scripts\python.exe -B -m video_creation_pipeline.visual_routing_cli build-style --preset pixel_narrative --output .test-runtime/pixel-style.json
.\.venv\Scripts\python.exe -B -m video_creation_pipeline.visual_routing_cli validate --artifact .test-runtime/pixel-style.json
```

之后沿用 `build-routes --packaging-plan ... --routing-spec ... --style-profile ... --output ...`。
命令只生成候选合同，不启动上游、Provider、渲染器或服务。

## 来源与取舍

上游：<https://github.com/Initvalue145/cyber-farmhands>

固定 commit：`42790f11f24559b2971ccbc3c43a7737f45d5488`。
源码审阅记录：`<workspace>/source-reviews/cyber-farmhands-20261013/REVIEW.md`。

- `main_主程序.py:18 timeline`、`:58 frame_at`：局部镜头时间投影到共同时间轴。
- `engine_像素引擎.py:837 Scene` 中的 `mark`：动作通过命名事件定位。
- `main_主程序.py:84` 起的 sheet 分支：按选定时间抽帧检查。
- `common_通用组件.py:228 draw_dialogue`：统一角色对话构图；不采纳其长文本截断。

复用方式是 `architecture_pattern`，没有复制或执行上游渲染/音频代码。
审阅包含本地保留文本和此前读取的固定 commit 代码片段，不是上游全量 checkout/运行验收。
不采用上游默认 full render、固定输出覆盖、字数配时、合成哔声和硬编码 Windows 字体路径。

## 变更理由与退出

- 意图：让现有制作 Agent 找得到、知道如何使用像素叙事方法。
- 决策：在既有 profile 增加一个显式 preset，附带机器可读配方和局部镜头交接，增加 Provider 误路由拦截。
- 理由：复用现有风格合同、场景时钟、审核与合成出口；色彩绑定本身无法表达角色表演。
- 证据：固定上游源码；本地路由、Remotion/HyperFrames 绑定、Seedance 交接及定向离线测试。
- 生效范围：仅显式选择 `pixel_narrative` 的候选计划；旧 profile 哈希应全部保持不变。
- 回滚：停止选择该 preset；删除本次新增 profile/派生候选计划即可停用。
  如回退代码，只撤回本次新增 preset、条件分支和 Seedance 拦截及相应测试/入口，不整体恢复脏工作树。
- 未验：真实中文像素镜头、人物辨识度、横竖屏构图、音画同步和用户观感。

本次不改 Registry、SOURCE_INVENTORY、全局 Skill 或其他项目；不安装、联网、发布或处理真人素材。
