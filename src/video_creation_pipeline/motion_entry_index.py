"""Unified offline motion-style entry index, import-closure unpacker, and fail-closed reference gate."""

from __future__ import annotations

import json
import re
from copy import deepcopy
from pathlib import Path, PurePosixPath
from typing import Any

from .contract_io import atomic_write_json, file_sha256, payload_sha256, read_json_object
from .errors import ContractError
from .object_scene_routes import JOBS, animation_source_catalog, source_bundle_for_project
from .remotion_skill_handoff import _reference_implementation
from .upstream_source_bundle import _multiple_native_implementations

INDEX_SCHEMA = "video_creation_pipeline.motion_entry_index.v1"
UNPACK_SCHEMA = "video_creation_pipeline.motion_entry_unpack.v1"
BINDING_SCHEMA = "video_creation_pipeline.motion_reference_binding.v1"
DEFAULT_INDEX_PATH = Path(__file__).with_name("policies") / "motion-entry-index.v1.json"

TIER_T1 = "T1_verified_local_case"
TIER_T2 = "T2_full_upstream_bundle"
VALID_TIERS = {TIER_T1, TIER_T2}
TIER_ALIASES = {
    "t1": TIER_T1,
    "t1_verified_local_case": TIER_T1,
    "local": TIER_T1,
    "t2": TIER_T2,
    "t2_full_upstream_bundle": TIER_T2,
    "upstream": TIER_T2,
}

TONE_ALIASES = {
    "documentary": "documentary",
    "纪实": "documentary",
    "新闻": "documentary",
    "实证": "documentary",
    "档案": "documentary",
    "editorial": "editorial",
    "社论": "editorial",
    "编辑部": "editorial",
    "干货": "editorial",
    "理性": "editorial",
    "serious": "serious",
    "严肃": "serious",
    "克制": "serious",
    "权威": "serious",
    "严谨": "serious",
    "explainer": "explainer",
    "科普": "explainer",
    "解释": "explainer",
    "拆解": "explainer",
    "知识": "explainer",
    "product": "product",
    "产品": "product",
    "发布": "product",
    "演示": "product",
    "playful": "playful",
    "轻松": "playful",
    "活泼": "playful",
    "趣味": "playful",
    "可爱": "playful",
    "expressive": "expressive",
    "强表达": "expressive",
    "表现力": "expressive",
    "动感": "expressive",
    "cinematic": "cinematic",
    "电影": "cinematic",
    "电影感": "cinematic",
    "大片": "cinematic",
    "dramatic": "dramatic",
    "戏剧": "dramatic",
    "冲击": "dramatic",
    "warm": "warm",
    "温暖": "warm",
    "温情": "warm",
    "温和": "warm",
    "minimal": "minimal",
    "极简": "minimal",
    "克制极简": "minimal",
    "干净": "minimal",
    "retro": "retro",
    "复古": "retro",
    "怀旧": "retro",
    "tech": "tech",
    "科技": "tech",
    "技术": "tech",
    "ai": "tech",
    "data_driven": "data_driven",
    "数据": "data_driven",
    "数据驱动": "data_driven",
    "literary": "literary",
    "文艺": "literary",
    "文学": "literary",
    "narrative": "narrative",
    "叙事": "narrative",
    "故事": "narrative",
}

MEDIUM_ALIASES = {
    "typography": "typography",
    "文字": "typography",
    "排版": "typography",
    "字卡": "typography",
    "字体": "typography",
    "diagram": "diagram",
    "图表结构": "diagram",
    "流程图": "diagram",
    "关系图": "diagram",
    "示意图": "diagram",
    "ui_screen": "ui_screen",
    "界面": "ui_screen",
    "屏幕": "ui_screen",
    "软件": "ui_screen",
    "清单": "ui_screen",
    "chart": "chart",
    "图表": "chart",
    "柱状图": "chart",
    "数据图": "chart",
    "icon_morph": "icon_morph",
    "变形": "icon_morph",
    "图标": "icon_morph",
    "图形变形": "icon_morph",
    "illustration_2d": "illustration_2d",
    "插画": "illustration_2d",
    "角色": "illustration_2d",
    "卡通": "illustration_2d",
    "paper_collage": "paper_collage",
    "拼贴": "paper_collage",
    "剪纸": "paper_collage",
    "hand_drawn": "hand_drawn",
    "手绘": "hand_drawn",
    "素描": "hand_drawn",
    "水彩": "hand_drawn",
    "archival_evidence": "archival_evidence",
    "档案": "archival_evidence",
    "实证": "archival_evidence",
    "证据": "archival_evidence",
    "报纸": "archival_evidence",
    "motion_poster": "motion_poster",
    "海报": "motion_poster",
    "开场": "motion_poster",
    "camera_rhythm": "camera_rhythm",
    "镜头": "camera_rhythm",
    "运镜": "camera_rhythm",
    "节奏": "camera_rhythm",
    "3d_material": "3d_material",
    "3d": "3d_material",
    "三维": "3d_material",
    "材质": "3d_material",
    "黏土": "3d_material",
    "retro_game": "retro_game",
    "游戏": "retro_game",
    "像素": "retro_game",
    "cinematic_film": "cinematic_film",
    "电影": "cinematic_film",
    "胶片": "cinematic_film",
    "ink_oriental": "ink_oriental",
    "水墨": "ink_oriental",
    "东方": "ink_oriental",
    "国风": "ink_oriental",
    "print_etching": "print_etching",
    "版画": "print_etching",
    "印刷": "print_etching",
}

BILINGUAL_SYNONYMS: tuple[tuple[str, ...], ...] = (
    ("证据", "实证", "档案", "条款", "理赔", "证据页", "红线", "批注", "evidence", "archival", "archival_evidence", "dossier", "court", "documentary", "redacted", "newspaper"),
    ("严肃", "克制", "理性", "权威", "严谨", "serious", "editorial"),
    ("变形", "图标", "转场", "过渡", "收束", "morph", "icon", "icon_morph", "transition", "continuous_transform", "brand"),
    ("插画", "角色", "人物", "骨架", "微动", "伴读", "卡通", "illustration", "illustration_2d", "character", "character_performance", "rig", "pelican", "playful", "cartoon"),
    ("数据", "柱状图", "图表", "数值", "增长", "占比", "统计", "chart", "bar", "data", "data_chart", "data_driven", "quantity", "kpi", "counter"),
    ("关系", "因果", "流程", "步骤", "结构", "机制", "节点", "diagram", "flow", "causality", "stages", "abstract_relationship", "relation", "steps"),
    ("文字", "字体", "排版", "金句", "强调", "花字", "贴纸", "重拍", "typography", "kinetic_type", "word", "title", "sticker", "emphasis"),
    ("界面", "软件", "屏幕", "终端", "代码", "清单", "旁白", "ui", "ui_screen", "screen", "interface", "terminal", "narration"),
    ("水墨", "国风", "东方", "毛笔", "宣纸", "ink", "ink_oriental", "oriental", "woodblock"),
    ("手绘", "素描", "白板", "水彩", "粉笔", "hand_drawn", "sketch", "watercolor", "whiteboard"),
    ("拼贴", "剪纸", "报纸", "杂志", "版画", "印刷", "paper_collage", "collage", "paper", "print_etching", "etching", "risograph"),
    ("电影", "胶片", "镜头", "运镜", "复古", "cinematic", "cinematic_film", "film", "camera", "camera_rhythm", "retro"),
    ("3d", "三维", "材质", "黏土", "立体", "机械", "three_d", "3d_material", "clay", "material", "mechanism"),
    ("游戏", "像素", "复古游戏", "game", "pixel", "retro_game"),
    ("海报", "开场", "片头", "封面", "收尾", "motion_poster", "poster", "opening", "outro"),
)

PLACEHOLDER_STRINGS = frozenset({"", "tbd", "todo", "n/a", "na", "none", "placeholder", "待补充", "待定", "无"})

_REPRO_ROOT = Path("outputs/vid-case-routes-20261115-01/repro-project")
_CANDIDATE_ROOT = Path("outputs/motion-inventory-agy-20261125-01/candidate-project")

_T1_CASE_METADATA: dict[str, dict[str, Any]] = {
    "case-20261125-shotcraft-icon-morph": {
        "title": "图标变形与品牌收束（Shotcraft UI-to-Brand Morph）",
        "visual_jobs": ["emphasis", "flow", "interface"],
        "content_tones": ["product", "explainer", "tech", "minimal"],
        "visual_medium": "icon_morph",
        "summary_zh": "三态界面符号经路径/形态连续变形收束为品牌主体，带平滑推进与明确文字说明带；适合概念收敛、工具功能演示与品牌过渡。",
        "use_when": "需要把多个零散功能或符号连续变形汇总成单一主题、品牌或产品标识时",
        "avoid_when": "严肃医疗/理赔实证原文展示或需要真实人像出镜时",
        "entry_file": str(_CANDIDATE_ROOT / "src/cases/IconMorphCase.tsx"),
        "boundary_root": str(_CANDIDATE_ROOT),
        "preview_stills": [
            str(_CANDIDATE_ROOT / "evidence/shotcraft-icon-morph-v2-base-at-1p60s.png"),
            str(_CANDIDATE_ROOT / "evidence/shotcraft-icon-morph-v2-base-at-3p60s.png"),
        ],
        "editable_params": ["title", "subtitle", "accentColor", "stages", "durationInFrames"],
        "known_pitfalls": [
            "1920x1080 舞台缩放到 1280x720 须使用 position:absolute + flexShrink:0，禁止 flex 压缩高度",
            "底部说明带需显式固定 top 位置留白，避免与中心变形主体重叠",
        ],
    },
    "case-20261125-pelican-rig-motion": {
        "title": "SVG 角色骨架微动（Pelican Rig Motion）",
        "visual_jobs": ["authored_performance", "person_or_emotion", "emphasis"],
        "content_tones": ["playful", "warm", "explainer", "expressive"],
        "visual_medium": "illustration_2d",
        "summary_zh": "基于分层 SVG 骨架关节（头、喙、翼、脚）与确定性帧时钟驱动的角色循环表演与微动；适合轻松科普、IP 伴读与插画角色过渡。",
        "use_when": "需要确定性帧驱动的矢量插画/IP 角色微动、轻幽默停顿或故事开场时",
        "avoid_when": "严肃合规证据页、医疗理赔核查或高密度数据对比",
        "entry_file": str(_CANDIDATE_ROOT / "src/cases/PelicanRigCase.tsx"),
        "boundary_root": str(_CANDIDATE_ROOT),
        "preview_stills": [
            str(_CANDIDATE_ROOT / "evidence/pelican-rig-motion-base-at-2p20s.png"),
            str(_CANDIDATE_ROOT / "evidence/pelican-rig-motion-base-at-3p80s.png"),
        ],
        "editable_params": ["title", "subtitle", "accentColor", "cycleSpeed", "wingAmplitude"],
        "known_pitfalls": [
            "关节旋转 pivot 必须显式绑定 SVG transform-origin，避免部位错位",
            "严禁依赖 requestAnimationFrame 累加状态，必须由帧号纯函数计算",
        ],
    },
    "case-20261120-juzu-b01-screen": {
        "title": "旁白同步清单屏（Juzu B01 Screen）",
        "visual_jobs": ["stages", "flow", "evidence"],
        "content_tones": ["editorial", "serious", "explainer", "documentary"],
        "visual_medium": "ui_screen",
        "summary_zh": "Canvas 2D 旁白驱动的结构化清单屏幕，随口播时间轴逐条点亮标题与要点；适合严肃知识拆解、条款与流程核对清单。",
        "use_when": "口播逐条列举规则、步骤或核查要点，需要沉稳高对比清单屏同步呈现时",
        "avoid_when": "缺少实测词时间戳却要求逐字精准卡点（示例使用字符估算草稿时间轴）",
        "entry_file": str(_REPRO_ROOT / "src/juzu-case-20261120/index.tsx"),
        "boundary_root": str(_REPRO_ROOT),
        "preview_stills": [
            str(_REPRO_ROOT / "evidence/juzu-case-20261120/juzu-screen-base-20261120-r3-5.5s.png"),
            str(_REPRO_ROOT / "evidence/juzu-screen-base-20261120-r3-2s.png"),
        ],
        "editable_params": ["timeline.lines", "SCREEN.head", "SCREEN.items", "big"],
        "known_pitfalls": [
            "示例为字符估算时间轴，正式成片必须替换为实测词级时间戳",
            "Canvas 字体须先 await FontFace.load() 完成后再首帧绘制",
        ],
    },
    "case-20261119-wise-expression-fun": {
        "title": "重拍字卡/弹跳贴纸（Wise Word-Slam Overlay）",
        "visual_jobs": ["emphasis", "person_or_emotion"],
        "content_tones": ["expressive", "playful", "editorial"],
        "visual_medium": "typography",
        "summary_zh": "高冲击重拍强调字卡（支持透明通道 WebM 叠层输出），通过节奏弹跳突出金句或情绪转折词；适合口播强调与轻综艺点缀。",
        "use_when": "需要在口播关键词或情绪反转处叠加高辨识度重拍花字/贴纸时",
        "avoid_when": "长句子大段阅读或克制型金融/医疗严肃免责声明",
        "entry_file": str(_REPRO_ROOT / "src/wise-expressive-20261119/index.tsx"),
        "boundary_root": str(_REPRO_ROOT),
        "preview_stills": [
            str(_REPRO_ROOT / "evidence/wise-expressive-20261119/fun-base-at-0.7.png"),
            str(_REPRO_ROOT / "evidence/wise-expression-fun-base-20261119-2s.png"),
        ],
        "editable_params": ["content", "effect='word-slam'", "default_ease"],
        "known_pitfalls": [
            "640x360 舞台 scale(2) 适配 1280x720，改字数须检查换行与溢出",
            "透明 WebM 叠层进入剪辑软件前需实测 alpha 通道兼容性",
        ],
    },
    "case-20261119-wise-expression-relation": {
        "title": "中心辐射关系网（Wise Cat-Context-Links）",
        "visual_jobs": ["abstract_relationship", "causality", "space"],
        "content_tones": ["explainer", "editorial", "expressive"],
        "visual_medium": "diagram",
        "summary_zh": "以 1 个核心概念为中心辐射连接 6 个关联节点的动态关系网；适合拆解多维因素、因果关联或制度组成结构。",
        "use_when": "解释一个中心主体与多个周边维度/要素的辐射关联时",
        "avoid_when": "节点标签超过 3 字或节点数不固定且需要大段说明时",
        "entry_file": str(_REPRO_ROOT / "src/wise-expressive-20261119/index.tsx"),
        "boundary_root": str(_REPRO_ROOT),
        "preview_stills": [
            str(_REPRO_ROOT / "evidence/wise-expression-relation-base-20261119-2s.png"),
        ],
        "editable_params": ["relation.center (1-2 chars)", "relation.labels (6 items, 1-3 chars)", "effect='cat-context-links'"],
        "known_pitfalls": [
            "源码强校验 center 1-2 字且 labels 恰好 6 项每项 1-3 字，扩展须同步调整布局",
        ],
    },
    "case-20261119-wise-expression-carry": {
        "title": "共享对象连贯转场（Wise Shared-Object Carry）",
        "visual_jobs": ["flow", "comparison", "emphasis"],
        "content_tones": ["editorial", "explainer", "minimal"],
        "visual_medium": "icon_morph",
        "summary_zh": "保留视觉锚点对象作为桥梁，把旧场景平滑承接到新场景的连贯转场；适合前后概念对照与章节承上启下。",
        "use_when": "相邻两个论点有同一个核心主语/对象，希望不硬切完成视角切换时",
        "avoid_when": "两个镜头主题毫无关联却强行共享视觉锚点",
        "entry_file": str(_REPRO_ROOT / "src/wise-expressive-20261119/index.tsx"),
        "boundary_root": str(_REPRO_ROOT),
        "preview_stills": [
            str(_REPRO_ROOT / "evidence/wise-expressive-20261119/carry-at-0.8.png"),
            str(_REPRO_ROOT / "evidence/wise-expression-carry-base-20261119-2s.png"),
        ],
        "editable_params": ["sceneLabels[0..1]", "effect='shared-object'", "default_ease"],
        "known_pitfalls": [
            "sceneLabels 必须恰好 2 项（旧/新标签），缺失会直接抛错",
        ],
    },
    "case-20261119-wise-practical-words": {
        "title": "聚焦动能文字（Wise Word-Focus）",
        "visual_jobs": ["emphasis", "comparison"],
        "content_tones": ["editorial", "serious", "minimal", "explainer"],
        "visual_medium": "typography",
        "summary_zh": "克制高可读的关键词聚焦排版动效，通过明暗对比与位移动势把视线锁定在核心观点词；适合干货口播与理性陈述。",
        "use_when": "口播抛出核心判断、金句或定义，需要干净利落的纯文字节奏视觉强化时",
        "avoid_when": "需要同时展示复杂数据图表或真实操作录屏",
        "entry_file": str(_REPRO_ROOT / "src/wise-practical-20261119/index.tsx"),
        "boundary_root": str(_REPRO_ROOT),
        "preview_stills": [
            str(_REPRO_ROOT / "evidence/wise-practical-v2-words-base-20261119-2s.png"),
        ],
        "editable_params": ["content", "effect='word-focus'", "default_ease"],
        "known_pitfalls": [
            "需保证 Source Han Sans SC Bold/Light 与 Oswald 字体加载完成后再首帧绘制",
        ],
    },
    "case-20261119-wise-practical-steps": {
        "title": "标题与分步结构卡（Wise Title-Content Steps）",
        "visual_jobs": ["stages", "flow", "comparison"],
        "content_tones": ["editorial", "serious", "explainer", "minimal"],
        "visual_medium": "diagram",
        "summary_zh": "先确立大标题锚点再逐层展开步骤/论点结构的知识分步卡片；适合三步法、方法论与操作流程拆解。",
        "use_when": "需要按 1-2-3 逻辑顺序清晰交代流程、步骤或结论依据时",
        "avoid_when": "单页塞入过多长段落导致移动端字号低于可读门限",
        "entry_file": str(_REPRO_ROOT / "src/wise-practical-20261119/index.tsx"),
        "boundary_root": str(_REPRO_ROOT),
        "preview_stills": [
            str(_REPRO_ROOT / "evidence/wise-practical-v2-steps-base-20261119-2s.png"),
        ],
        "editable_params": ["content", "effect='title-content'", "default_ease"],
        "known_pitfalls": [
            "改稿须遵守 readability.css 字号层级约束，避免正文挤压标题",
        ],
    },
    "case-20261119-wise-practical-chart": {
        "title": "动态柱状增长图（Wise Bar-Growth Chart）",
        "visual_jobs": ["quantity", "comparison", "evidence"],
        "content_tones": ["data_driven", "serious", "editorial", "explainer"],
        "visual_medium": "chart",
        "summary_zh": "确定性时间轴驱动的柱状数据生长与数值对比镜头，内建明确的数据来源/模拟标注槽位；适合保费/赔付/统计占比可视化。",
        "use_when": "展示多组数值高低对比、逐年变化或比例差异时",
        "avoid_when": "省略数据口径/模拟标注或篡改柱高真实比例",
        "entry_file": str(_REPRO_ROOT / "src/wise-practical-20261119/index.tsx"),
        "boundary_root": str(_REPRO_ROOT),
        "preview_stills": [
            str(_REPRO_ROOT / "evidence/wise-practical-v2-chart-base-20261119-2s.png"),
        ],
        "editable_params": ["content.bars", "effect='bar-growth'", "disclaimer_label"],
        "known_pitfalls": [
            "保留真实性/口径声明槽位；柱高比例必须严格忠于真实数值",
        ],
    },
    "case-20261119-wise-geometric-poster": {
        "title": "几何动态海报开场（Wise Geometric Poster）",
        "visual_jobs": ["emphasis", "space"],
        "content_tones": ["editorial", "expressive", "minimal", "cinematic"],
        "visual_medium": "motion_poster",
        "summary_zh": "强设计感的瑞士/几何色块动态海报排版，利用色块切割与大字号建立章节开场或强观点定调。",
        "use_when": "大章节切分、高格调片头引子或强视觉主题声明时",
        "avoid_when": "需要展示细小证据原文或大段密集解释正文",
        "entry_file": str(_REPRO_ROOT / "src/wise-case-20261119/base.tsx"),
        "boundary_root": str(_REPRO_ROOT),
        "preview_stills": [
            str(_REPRO_ROOT / "evidence/wise-case-20261119/wise-poster-base-20261119-r2-at-2.83s.png"),
            str(_REPRO_ROOT / "evidence/wise-poster-base-20261119-r2-2s.png"),
        ],
        "editable_params": ["effect-base.js parameters", "headline", "palette"],
        "known_pitfalls": [
            "高对比色块切割下须核查中文标题可读性与边缘安全区",
        ],
    },
    "case-20261115-shape-transition": {
        "title": "SVG 几何形态过渡（Shotcraft SvgShapeMorph）",
        "visual_jobs": ["emphasis", "flow", "comparison"],
        "content_tones": ["tech", "product", "minimal", "explainer"],
        "visual_medium": "icon_morph",
        "summary_zh": "深色舞台上的矢量形状连续变形转场（SvgShapeMorph），配顶部标题与底部 A→B 状态演进标注。",
        "use_when": "用干净的矢量形状过渡表达状态演变（A → B）或功能切换时",
        "avoid_when": "亮色纸面文档风格中未调整底色直接硬拼",
        "entry_file": str(_REPRO_ROOT / "src/scenes.tsx"),
        "boundary_root": str(_REPRO_ROOT),
        "preview_stills": [
            str(_REPRO_ROOT / "evidence/transition-base-v3-2s.png"),
        ],
        "editable_params": ["title", "labelA", "labelB", "startFrame"],
        "known_pitfalls": [
            "SvgShapeMorph 默认深底色 #0a0b10，嵌入亮色体系需统一过渡遮罩或重设背景 token",
        ],
    },
    "case-20261115-ip-illustration-movement": {
        "title": "IP 插画轻浮动伴读（IP Illustration Movement）",
        "visual_jobs": ["person_or_emotion", "authored_performance"],
        "content_tones": ["warm", "editorial", "explainer", "minimal"],
        "visual_medium": "illustration_2d",
        "summary_zh": "温润纸面底色上左文右图布局，个人 IP 插画以克制的上下呼吸微浮动配合文案讲解，不抢信息焦点。",
        "use_when": "个人 IP 品牌讲解、温情/温和科普场景需要轻量插画伴读而非激烈表演时",
        "avoid_when": "需要多关节复杂动作表演（应改用 pelican-rig-motion）",
        "entry_file": str(_REPRO_ROOT / "src/scenes.tsx"),
        "boundary_root": str(_REPRO_ROOT),
        "preview_stills": [
            str(_REPRO_ROOT / "evidence/character-base-v4-2s.png"),
        ],
        "editable_params": ["title", "labelA", "startFrame", "illustration asset"],
        "known_pitfalls": [
            "插画位移幅度保持克制（<=10px）避免分散阅读焦点；透明 PNG 底边贴齐安全底线",
        ],
    },
    "case-20261115-narrated-relation": {
        "title": "口播驱动因果关系流（ObjectFlow2D Narrated Relation）",
        "visual_jobs": ["causality", "flow", "stages", "abstract_relationship"],
        "content_tones": ["explainer", "editorial", "serious", "minimal"],
        "visual_medium": "diagram",
        "summary_zh": "内置 ObjectFlow2D 关系流程图，按口播 cueFrames 依次连接并点亮节点；适合方法论、流程与因果机制拆解。",
        "use_when": "随着旁白节奏逐步展开多节点流程、因果链或机制拆解时",
        "avoid_when": "需要 3D 物理机械机构（改用 slider_crank_3d）或纯情绪转场",
        "entry_file": str(_REPRO_ROOT / "src/scenes.tsx"),
        "boundary_root": str(_REPRO_ROOT),
        "preview_stills": [
            str(_REPRO_ROOT / "evidence/relation-base-2s-v2.png"),
        ],
        "editable_params": ["title", "cueFrames", "scene.originLabel", "scene.objects", "palette"],
        "known_pitfalls": [
            "cueFrames 必须对齐实际口播时间戳，不可凭字数估算",
        ],
    },
}

_SHOTCRAFT_CATEGORY_MAP: dict[str, dict[str, Any]] = {
    "ui-entrance": {
        "visual_jobs": ["interface", "emphasis", "stages"],
        "techniques": ["product_intro", "editing"],
        "content_tones": ["product", "tech", "explainer", "minimal"],
        "visual_medium": "ui_screen",
    },
    "typography": {
        "visual_jobs": ["emphasis", "comparison"],
        "techniques": ["kinetic_type"],
        "content_tones": ["editorial", "explainer", "expressive", "minimal"],
        "visual_medium": "typography",
    },
    "transition": {
        "visual_jobs": ["flow", "emphasis", "comparison"],
        "techniques": ["continuous_transform", "editing"],
        "content_tones": ["product", "editorial", "cinematic", "explainer"],
        "visual_medium": "icon_morph",
    },
    "effects": {
        "visual_jobs": ["emphasis", "space"],
        "techniques": ["image_motion", "editing"],
        "content_tones": ["expressive", "cinematic", "tech", "dramatic"],
        "visual_medium": "camera_rhythm",
    },
    "interaction": {
        "visual_jobs": ["interface", "flow", "causality"],
        "techniques": ["product_intro", "interactive_web"],
        "content_tones": ["product", "tech", "explainer"],
        "visual_medium": "ui_screen",
    },
    "data": {
        "visual_jobs": ["quantity", "comparison", "evidence"],
        "techniques": ["data_chart", "diagram"],
        "content_tones": ["data_driven", "serious", "editorial", "explainer"],
        "visual_medium": "chart",
    },
    "rhythm": {
        "visual_jobs": ["emphasis", "stages", "flow"],
        "techniques": ["editing", "kinetic_type"],
        "content_tones": ["expressive", "dramatic", "product", "cinematic"],
        "visual_medium": "camera_rhythm",
    },
    "camera": {
        "visual_jobs": ["space", "evidence", "emphasis"],
        "techniques": ["image_motion", "editing"],
        "content_tones": ["cinematic", "documentary", "dramatic", "product"],
        "visual_medium": "camera_rhythm",
    },
    "opening": {
        "visual_jobs": ["emphasis", "space", "stages"],
        "techniques": ["kinetic_type", "product_intro"],
        "content_tones": ["cinematic", "editorial", "product", "dramatic"],
        "visual_medium": "motion_poster",
    },
    "outro": {
        "visual_jobs": ["emphasis", "comparison"],
        "techniques": ["kinetic_type", "product_intro"],
        "content_tones": ["editorial", "product", "minimal"],
        "visual_medium": "motion_poster",
    },
}

_LEMO_CATEGORY_MAP: dict[str, dict[str, Any]] = {
    "实证影像": {
        "visual_jobs": ["evidence", "comparison", "causality"],
        "techniques": ["collage", "editing", "faceless_article_explainer"],
        "content_tones": ["documentary", "serious", "editorial"],
        "visual_medium": "archival_evidence",
    },
    "剪辑与纪实": {
        "visual_jobs": ["evidence", "flow", "emphasis"],
        "techniques": ["editing", "narration", "faceless_article_explainer"],
        "content_tones": ["documentary", "editorial", "serious"],
        "visual_medium": "archival_evidence",
    },
    "手绘与绘画": {
        "visual_jobs": ["authored_performance", "person_or_emotion", "causality"],
        "techniques": ["illustration", "sketch", "watercolor"],
        "content_tones": ["warm", "literary", "narrative", "expressive"],
        "visual_medium": "hand_drawn",
    },
    "东方传统": {
        "visual_jobs": ["authored_performance", "space", "emphasis"],
        "techniques": ["illustration", "watercolor", "style_exploration"],
        "content_tones": ["literary", "cinematic", "warm", "serious"],
        "visual_medium": "ink_oriental",
    },
    "印刷与版画": {
        "visual_jobs": ["evidence", "comparison", "emphasis"],
        "techniques": ["collage", "sketch", "style_exploration"],
        "content_tones": ["editorial", "retro", "serious", "literary"],
        "visual_medium": "print_etching",
    },
    "图形与排版": {
        "visual_jobs": ["emphasis", "comparison", "stages"],
        "techniques": ["kinetic_type", "diagram", "style_exploration"],
        "content_tones": ["editorial", "minimal", "expressive"],
        "visual_medium": "typography",
    },
    "信息与发布": {
        "visual_jobs": ["quantity", "flow", "interface", "stages"],
        "techniques": ["data_chart", "diagram", "product_intro"],
        "content_tones": ["data_driven", "product", "explainer", "tech"],
        "visual_medium": "chart",
    },
    "卡通与动画": {
        "visual_jobs": ["authored_performance", "person_or_emotion", "emphasis"],
        "techniques": ["character_performance", "illustration"],
        "content_tones": ["playful", "expressive", "warm"],
        "visual_medium": "illustration_2d",
    },
    "游戏": {
        "visual_jobs": ["authored_performance", "interface", "stages"],
        "techniques": ["character_performance", "style_exploration"],
        "content_tones": ["playful", "retro", "tech", "expressive"],
        "visual_medium": "retro_game",
    },
    "电影与时代": {
        "visual_jobs": ["person_or_emotion", "emphasis", "space"],
        "techniques": ["editing", "image_motion", "style_exploration"],
        "content_tones": ["cinematic", "dramatic", "retro", "narrative"],
        "visual_medium": "cinematic_film",
    },
    "材质与 3D": {
        "visual_jobs": ["mechanism", "space", "emphasis"],
        "techniques": ["three_d", "clay", "style_exploration"],
        "content_tones": ["cinematic", "product", "tech", "expressive"],
        "visual_medium": "3d_material",
    },
}

_JS_IMPORT_RE = re.compile(
    r"""(?:import|export)\s+(?:type\s+)?(?:[\s\S]*?\s+from\s+)?['"]([^'"]+)['"]"""
    r"""|require\(\s*['"]([^'"]+)['"]\s*\)"""
    r"""|import\(\s*['"]([^'"]+)['"]\s*\)"""
)
_HTML_ASSET_RE = re.compile(
    r"""<(?:script[^>]+src|link[^>]+href)=['"]([^'"]+)['"]""",
    re.IGNORECASE,
)
_CSS_IMPORT_RE = re.compile(
    r"""@import\s+(?:url\(\s*)?['"]([^'"]+)['"]""",
    re.IGNORECASE,
)
_RESOLVE_EXTS = ("", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".json", ".css")
_INDEX_EXTS = ("index.ts", "index.tsx", "index.js", "index.jsx")


def _norm_path(path: str | Path) -> str:
    return Path(path).resolve().as_posix()


def _dedupe(items: list[str]) -> list[str]:
    return list(dict.fromkeys(item for item in items if item))


def _check_safe_path_in_root(target: Path, boundary_root: Path, label: str = "file") -> Path:
    root = boundary_root.resolve()
    if not root.is_dir() or root.is_symlink() or getattr(root, "is_junction", lambda: False)():
        raise ContractError(f"invalid boundary root for {label}: {boundary_root}")
    resolved = target.resolve()
    if not resolved.is_relative_to(root):
        raise ContractError(f"{label} escapes boundary root: {target}")
    cursor = target
    while True:
        if cursor.is_symlink() or getattr(cursor, "is_junction", lambda: False)():
            raise ContractError(f"linked path rejected for {label}: {cursor}")
        if cursor.resolve() == root:
            break
        if cursor == cursor.parent:
            raise ContractError(f"{label} escapes boundary root: {target}")
        cursor = cursor.parent
    if not resolved.is_file():
        raise ContractError(f"{label} missing: {target}")
    return resolved


def _resolve_relative_target(current_file: Path, specifier: str, boundary_root: Path) -> Path | None:
    cleaned = specifier.split("?", 1)[0].split("#", 1)[0].strip()
    if not cleaned:
        return None
    if re.match(r"^(?:[a-zA-Z][a-zA-Z0-9+.-]*:|//)", cleaned):
        return None
    suffix = current_file.suffix.lower()
    is_relative = cleaned.startswith(".")
    if not is_relative:
        # In HTML/CSS, bare local paths like "film.js" or "style.css" are relative to current file.
        if suffix in {".html", ".htm", ".css"} and not cleaned.startswith("/"):
            is_relative = True
        else:
            return None
    base_dir = current_file.parent
    raw_target = base_dir / cleaned
    root = boundary_root.resolve()
    if not raw_target.resolve().is_relative_to(root):
        raise ContractError(f"import path escapes boundary root: {specifier} from {current_file.name}")
    candidates: list[Path] = []
    for ext in _RESOLVE_EXTS:
        cand = Path(str(raw_target) + ext) if ext else raw_target
        candidates.append(cand)
    if raw_target.is_dir():
        for idx in _INDEX_EXTS:
            candidates.append(raw_target / idx)
    for cand in candidates:
        if cand.is_file():
            return _check_safe_path_in_root(cand, root, label="dependency")
    return None


def resolve_import_closure(
    entry_files: str | Path | list[str | Path],
    boundary_root: str | Path,
    *,
    max_files: int = 32,
) -> dict[str, Any]:
    """Statically resolve relative JS/TS/HTML/CSS dependency closure inside boundary_root."""
    root = Path(boundary_root).resolve()
    if not root.is_dir() or root.is_symlink() or getattr(root, "is_junction", lambda: False)():
        raise ContractError(f"invalid boundary root: {boundary_root}")
    seeds = [entry_files] if isinstance(entry_files, (str, Path)) else list(entry_files)
    if not seeds:
        raise ContractError("resolve_import_closure requires at least one entry file")
    queue: list[tuple[Path, str]] = []
    for index, seed in enumerate(seeds):
        checked = _check_safe_path_in_root(Path(seed), root, label="entry file")
        queue.append((checked, "entry" if index == 0 else "extra_entry"))
    visited: dict[Path, dict[str, Any]] = {}
    truncated = False
    cursor = 0
    while cursor < len(queue):
        current, role = queue[cursor]
        cursor += 1
        if current in visited:
            continue
        if len(visited) >= max_files:
            truncated = True
            break
        visited[current] = {
            "path": current.as_posix(),
            "relative": current.relative_to(root).as_posix(),
            "role": role,
            "bytes": current.stat().st_size,
            "sha256": file_sha256(current),
        }
        suffix = current.suffix.lower()
        if suffix not in {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".html", ".htm", ".css"}:
            continue
        try:
            text = current.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        specifiers: list[str] = []
        if suffix in {".html", ".htm"}:
            specifiers.extend(_HTML_ASSET_RE.findall(text))
            for block in re.findall(r"<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)</script>", text, re.IGNORECASE):
                for match in _JS_IMPORT_RE.findall(block):
                    specifiers.append(next(group for group in match if group))
        elif suffix == ".css":
            specifiers.extend(_CSS_IMPORT_RE.findall(text))
        else:
            for match in _JS_IMPORT_RE.findall(text):
                specifiers.append(next(group for group in match if group))
        for spec in specifiers:
            dep = _resolve_relative_target(current, spec, root)
            if dep is not None and dep not in visited:
                queue.append((dep, "import_dependency"))
    return {
        "boundary_root": root.as_posix(),
        "files": list(visited.values()),
        "truncated": truncated,
    }


def _enrich_shotcraft_tags(card: dict[str, Any], base: dict[str, Any]) -> tuple[list[str], list[str], list[str], str]:
    jobs = list(base["visual_jobs"])
    techniques = list(base["techniques"])
    tones = list(base["content_tones"])
    medium = str(base["visual_medium"])
    blob = " ".join(
        str(card.get(k, "")) for k in ("name", "summary", "use", "intention", "category")
    ).lower() + " " + " ".join(str(t) for t in card.get("tags", [])).lower()
    if any(k in blob for k in ("morph", "变形", "icon", "logo", "shape-morph")):
        techniques.append("continuous_transform")
        if medium not in {"chart", "ui_screen"} or "morph" in blob:
            medium = "icon_morph"
    if any(k in blob for k in ("chart", "bar", "pie", "counter", "kpi", "metric", "数据", "图表", "柱状", "折线")):
        jobs.append("quantity")
        techniques.append("data_chart")
        tones.append("data_driven")
        medium = "chart"
    if any(k in blob for k in ("evidence", "quote", "citation", "doc", "paper", "新闻", "证据", "条款", "文档", "引用")):
        jobs.append("evidence")
        tones.extend(["serious", "documentary"])
    if any(k in blob for k in ("step", "timeline", "flow", "diagram", "node", "因果", "流程", "步骤", "节点", "结构")):
        jobs.extend(["flow", "stages"])
        techniques.append("diagram")
    if any(k in blob for k in ("svg", "path", "draw", "stroke", "手绘", "描边", "线条")):
        techniques.append("sketch")
    if any(k in blob for k in ("3d", "perspective", "cube", "flip", "parallax", "景深")):
        techniques.append("three_d")
        jobs.append("space")
    if any(k in blob for k in ("code", "terminal", "ai", "agent", "chat", "stream", "copilot")):
        jobs.append("interface")
        tones.extend(["tech", "product"])
    if any(k in blob for k in ("sticker", "badge", "confetti", "弹跳", "贴纸", "活泼")):
        techniques.append("sticker")
        tones.append("playful")
    return _dedupe(jobs), _dedupe(techniques), _dedupe(tones), medium


def _enrich_lemo_tags(row: dict[str, Any], base: dict[str, Any]) -> tuple[list[str], list[str], list[str], str]:
    jobs = list(base["visual_jobs"])
    techniques = list(base["techniques"])
    tones = list(base["content_tones"])
    medium = str(base["visual_medium"])
    blob = " ".join(
        [
            str(row.get("slug", "")),
            str(row.get("en", "")),
            str(row.get("cn", "")),
            str(row.get("line", "")),
            str(row.get("line_cn", "")),
            " ".join(str(u) for u in row.get("uses", [])),
        ]
    ).lower()
    if any(k in blob for k in ("paper", "cutout", "collage", "剪纸", "拼贴", "剪报", "报纸")):
        techniques.append("collage")
        medium = "paper_collage" if "剪纸" in blob or "cutout" in blob or "collage" in blob else medium
    if any(k in blob for k in ("archive", "档案", "证据", "redacted", "court", "dossier", "news", "新闻", "报道", "纪实")):
        jobs.append("evidence")
        tones.extend(["documentary", "serious", "editorial"])
    if any(k in blob for k in ("clay", "黏土", "陶土")):
        techniques.append("clay")
        medium = "3d_material"
    if any(k in blob for k in ("whiteboard", "白板", "黑板", "chalk", "粉笔")):
        techniques.append("whiteboard")
        medium = "hand_drawn"
    if any(k in blob for k in ("data", "chart", "dashboard", "数据", "图表", "信息图", "财经")):
        jobs.append("quantity")
        techniques.append("data_chart")
        tones.append("data_driven")
    if any(k in blob for k in ("cartoon", "anime", "角色", "卡通", "插画", "动画")):
        techniques.extend(["character_performance", "illustration"])
    return _dedupe(jobs), _dedupe(techniques), _dedupe(tones), medium


def _build_t1_entries(catalog: dict[str, Any]) -> list[dict[str, Any]]:
    projects_by_id = {p["id"]: p for p in catalog["projects"]}
    entries: list[dict[str, Any]] = []
    for case in catalog.get("local_case_examples", []):
        cid = case["id"]
        meta = _T1_CASE_METADATA.get(cid)
        if meta is None:
            continue
        source_project = case.get("source_project") or "local-builtin"
        project_info = projects_by_id.get(source_project, {})
        commit = (
            project_info.get("source_bundle", {}).get("commit")
            or project_info.get("commit")
        )
        entry_file = _norm_path(meta["entry_file"])
        boundary_root = _norm_path(meta["boundary_root"])
        stills = [_norm_path(p) for p in meta["preview_stills"] if Path(p).is_file()]
        evidence_doc = _norm_path(case["evidence"]) if case.get("evidence") and Path(case["evidence"]).is_file() else None
        entries.append(
            {
                "entry_id": f"local:{cid}",
                "tier": TIER_T1,
                "source_project": source_project,
                "upstream_commit": commit,
                "upstream_entry": case.get("upstream_entry") or case.get("upstream_effect"),
                "title": meta["title"],
                "category": "verified_local_case",
                "visual_jobs": list(meta["visual_jobs"]),
                "techniques": list(case.get("techniques", [])),
                "content_tones": list(meta["content_tones"]),
                "visual_medium": meta["visual_medium"],
                "summary_zh": meta["summary_zh"],
                "summary_en": "",
                "use_when": meta["use_when"],
                "avoid_when": meta["avoid_when"],
                "boundary_root": boundary_root,
                "entry_file": entry_file,
                "extra_entry_files": [],
                "card_doc": evidence_doc,
                "preview_stills": stills,
                "preview_mp4": _norm_path(case["preview"]) if case.get("preview") else None,
                "preview_sha256": case.get("preview_sha256"),
                "editable_params": list(meta["editable_params"]),
                "known_pitfalls": list(meta["known_pitfalls"]),
                "tags": _dedupe([cid, source_project, *case.get("techniques", []), *meta["content_tones"], meta["visual_medium"]]),
            }
        )
    return entries


def _build_shotcraft_entries(valid_techniques: set[str]) -> list[dict[str, Any]]:
    bundle = source_bundle_for_project("video-shotcraft", verify_files=False)
    manifest = read_json_object(bundle["manifest_path"], "video-shotcraft manifest")
    sc_root = Path(manifest["root"]).resolve()
    by_path = {row["path"]: row for row in manifest["files"]}
    lib = read_json_object(sc_root / "gallery/api/library.json", "video-shotcraft library")
    entries: list[dict[str, Any]] = []
    for card in lib.get("cards", []):
        name = str(card["name"])
        category = str(card.get("category", "ui-entrance"))
        base = _SHOTCRAFT_CATEGORY_MAP.get(category, _SHOTCRAFT_CATEGORY_MAP["ui-entrance"])
        jobs, techniques, tones, medium = _enrich_shotcraft_tags(card, base)
        techniques = [t for t in techniques if t in valid_techniques]
        src_rel = str(card.get("source", ""))
        if src_rel not in by_path:
            raise ContractError(f"video-shotcraft card source missing in manifest: {name}")
        card_path = _check_safe_path_in_root(sc_root / src_rel, sc_root, label="shotcraft card")
        try:
            imp = _reference_implementation(card_path)
        except ContractError:
            imp = ""
        impls = [imp] if imp in by_path else _multiple_native_implementations(card_path, by_path)
        if not impls:
            raise ContractError(f"video-shotcraft card implementation missing: {name}")
        resolved_impls = [_check_safe_path_in_root(sc_root / rel, sc_root, label="shotcraft impl").as_posix() for rel in impls]
        media_rel = f"gallery/media/{name}.mp4"
        preview_mp4 = (sc_root / media_rel).as_posix() if media_rel in by_path and (sc_root / media_rel).is_file() else None
        entries.append(
            {
                "entry_id": f"shotcraft:{name}",
                "tier": TIER_T2,
                "source_project": "video-shotcraft",
                "upstream_commit": manifest["commit"],
                "upstream_entry": name,
                "title": f"{name} ({category})",
                "category": category,
                "visual_jobs": jobs,
                "techniques": techniques,
                "content_tones": tones,
                "visual_medium": medium,
                "summary_zh": str(card.get("summary", "")),
                "summary_en": "",
                "use_when": str(card.get("use", "")),
                "avoid_when": "",
                "intention": str(card.get("intention", "")),
                "duration": str(card.get("duration", "")),
                "energy": str(card.get("energy", "")),
                "boundary_root": sc_root.as_posix(),
                "entry_file": resolved_impls[0],
                "extra_entry_files": resolved_impls[1:],
                "card_doc": card_path.as_posix(),
                "preview_stills": [],
                "preview_mp4": preview_mp4,
                "preview_sha256": by_path[media_rel]["sha256"] if media_rel in by_path else None,
                "editable_params": [],
                "known_pitfalls": [
                    "上游参考 TSX 禁止直接整树拷贝；须在当前项目独立适配并绑定时间轴/字号/安全区",
                ],
                "tags": _dedupe([name, category, *[str(t) for t in card.get("tags", [])], *techniques, *tones, medium]),
            }
        )
    return entries


def _build_lemo_entries(valid_techniques: set[str]) -> list[dict[str, Any]]:
    bundle = source_bundle_for_project("lemo-opuscar", verify_files=False)
    manifest = read_json_object(bundle["manifest_path"], "lemo-opuscar manifest")
    lm_root = Path(manifest["root"]).resolve()
    by_path = {row["path"]: row for row in manifest["files"]}
    catalog_path = _check_safe_path_in_root(lm_root / "styleboard/catalog.json", lm_root, label="lemo catalog")
    rows = json.loads(catalog_path.read_text(encoding="utf-8"))
    if not isinstance(rows, list):
        raise ContractError("lemo-opuscar styleboard/catalog.json must be a JSON list")
    entries: list[dict[str, Any]] = []
    for row in rows:
        slug = str(row["slug"])
        cat_zh = str(row.get("cat", "图形与排版"))
        base = _LEMO_CATEGORY_MAP.get(cat_zh, _LEMO_CATEGORY_MAP["图形与排版"])
        jobs, techniques, tones, medium = _enrich_lemo_tags(row, base)
        techniques = [t for t in techniques if t in valid_techniques]
        film_rel = f"styles/{slug}/demo/film.js"
        html_rel = f"styles/{slug}/demo/index.html"
        md_rel = f"styles/{slug}/STYLE.md"
        if film_rel in by_path:
            primary = _check_safe_path_in_root(lm_root / film_rel, lm_root, label="lemo film.js").as_posix()
            extras = (
                [_check_safe_path_in_root(lm_root / html_rel, lm_root, label="lemo demo html").as_posix()]
                if html_rel in by_path
                else []
            )
        elif html_rel in by_path:
            primary = _check_safe_path_in_root(lm_root / html_rel, lm_root, label="lemo demo html").as_posix()
            extras = []
        else:
            raise ContractError(f"lemo-opuscar demo entry missing for slug: {slug}")
        card_doc = _check_safe_path_in_root(lm_root / md_rel, lm_root, label="lemo STYLE.md").as_posix() if md_rel in by_path else None
        stills: list[str] = []
        for still_rel in (f"styleboard/img/{slug}_0.jpg", f"styles/{slug}/poster.jpg"):
            if still_rel in by_path and (lm_root / still_rel).is_file():
                stills.append((lm_root / still_rel).resolve().as_posix())
        uses_str = ", ".join(str(u) for u in row.get("uses", []))
        entries.append(
            {
                "entry_id": f"lemo:{slug}",
                "tier": TIER_T2,
                "source_project": "lemo-opuscar",
                "upstream_commit": manifest["commit"],
                "upstream_entry": slug,
                "title": f"{row.get('cn', slug)} ({row.get('en', slug)})",
                "category": f"{cat_zh} / {row.get('cat_en', '')}".strip(" /"),
                "visual_jobs": jobs,
                "techniques": techniques,
                "content_tones": tones,
                "visual_medium": medium,
                "summary_zh": str(row.get("line_cn", "")),
                "summary_en": str(row.get("line", "")),
                "use_when": uses_str,
                "avoid_when": "",
                "boundary_root": lm_root.as_posix(),
                "entry_file": primary,
                "extra_entry_files": extras,
                "card_doc": card_doc,
                "preview_stills": stills,
                "preview_mp4": None,
                "preview_sha256": None,
                "editable_params": [],
                "known_pitfalls": [
                    "lemo-opuscar 为原生 HTML/Canvas 风格样本，移植进视频管线时须先读 STYLE.md 与 demo 源码并在工程外重构确定性帧时钟",
                ],
                "tags": _dedupe([slug, str(row.get("cn", "")), str(row.get("en", "")), cat_zh, str(row.get("cat_en", "")), *techniques, *tones, medium]),
            }
        )
    return entries


def build_motion_entry_index(*, output_path: str | Path | None = None) -> dict[str, Any]:
    """Compile the unified offline motion-style entry index from local T1 cases and preserved upstream bundles."""
    catalog = animation_source_catalog()
    valid_techniques = set(catalog["techniques"])
    t1 = _build_t1_entries(catalog)
    shotcraft = _build_shotcraft_entries(valid_techniques)
    lemo = _build_lemo_entries(valid_techniques)
    entries = [*t1, *shotcraft, *lemo]
    ids = [e["entry_id"] for e in entries]
    if len(ids) != len(set(ids)):
        raise ContractError("duplicate entry_id detected while building motion entry index")
    index: dict[str, Any] = {
        "schema": INDEX_SCHEMA,
        "catalog_sha256": catalog["catalog_sha256"],
        "entry_count": len(entries),
        "counts_by_tier": {
            TIER_T1: len(t1),
            TIER_T2: len(shotcraft) + len(lemo),
        },
        "counts_by_source": {
            "local_case_examples": len(t1),
            "video-shotcraft": len(shotcraft),
            "lemo-opuscar": len(lemo),
        },
        "controlled_vocabularies": {
            "visual_jobs": sorted(JOBS),
            "techniques": sorted(valid_techniques),
            "content_tones": sorted(set(TONE_ALIASES.values())),
            "visual_mediums": sorted(set(MEDIUM_ALIASES.values())),
            "tiers": [TIER_T1, TIER_T2],
        },
        "entries": entries,
    }
    index["index_sha256"] = payload_sha256(index, "index_sha256")
    if output_path is not None:
        atomic_write_json(index, output_path)
    return index


def load_motion_entry_index(index_path: str | Path | None = None) -> dict[str, Any]:
    path = Path(index_path) if index_path is not None else DEFAULT_INDEX_PATH
    value = read_json_object(path, "motion entry index")
    if value.get("schema") != INDEX_SCHEMA:
        raise ContractError("unsupported motion entry index schema")
    if value.get("index_sha256") != payload_sha256(value, "index_sha256"):
        raise ContractError("motion entry index integrity drift")
    entries = value.get("entries")
    if not isinstance(entries, list) or not entries or value.get("entry_count") != len(entries):
        raise ContractError("invalid motion entry index coverage")
    return value


def _normalize_filter_alias(value: str | None, mapping: dict[str, str], label: str) -> str | None:
    if value is None or not str(value).strip():
        return None
    raw = str(value).strip().lower()
    if raw in mapping:
        return mapping[raw]
    raise ContractError(f"unsupported {label}: {value}")


def _tokenize_query(query: str) -> tuple[list[str], list[str], list[tuple[str, ...]]]:
    lowered = query.lower().strip()
    if not lowered:
        return [], [], []
    ascii_tokens = [tok for tok in re.findall(r"[a-z0-9][a-z0-9_-]*", lowered) if len(tok) >= 2]
    zh_chunks = re.findall(r"[\u3400-\u9fff]+", lowered)
    zh_bigrams: list[str] = []
    for chunk in zh_chunks:
        if len(chunk) == 1:
            zh_bigrams.append(chunk)
        else:
            for i in range(len(chunk) - 1):
                zh_bigrams.append(chunk[i : i + 2])
    matched_groups: list[tuple[str, ...]] = []
    for group in BILINGUAL_SYNONYMS:
        if any(term in lowered for term in group):
            matched_groups.append(group)
    return _dedupe(ascii_tokens), _dedupe(zh_bigrams), matched_groups


def find_motion_entries(
    *,
    visual_job: str | None = None,
    technique: str | None = None,
    tone: str | None = None,
    medium: str | None = None,
    tier: str | None = None,
    source_project: str | None = None,
    query: str = "",
    limit: int = 5,
    index_path: str | Path | None = None,
) -> dict[str, Any]:
    """Deterministically rank motion-style entries with T1 local-case priority and preview/code links."""
    index = load_motion_entry_index(index_path)
    if visual_job is not None and visual_job not in JOBS:
        raise ContractError(f"unsupported visual_job: {visual_job}")
    valid_techniques = set(index["controlled_vocabularies"]["techniques"])
    if technique is not None and technique not in valid_techniques:
        raise ContractError(f"unsupported technique: {technique}")
    norm_tone = _normalize_filter_alias(tone, TONE_ALIASES, "tone")
    norm_medium = _normalize_filter_alias(medium, MEDIUM_ALIASES, "medium")
    norm_tier = _normalize_filter_alias(tier, TIER_ALIASES, "tier")
    ascii_tokens, zh_bigrams, syn_groups = _tokenize_query(query)
    has_query = bool(query.strip())
    has_filters = any(v is not None for v in (visual_job, technique, norm_tone, norm_medium, norm_tier, source_project))
    scored: list[tuple[int, int, str, dict[str, Any], list[str]]] = []

    for entry in index["entries"]:
        if norm_tier and entry["tier"] != norm_tier:
            continue
        if source_project and entry["source_project"] != source_project:
            continue
        if visual_job and visual_job not in entry["visual_jobs"]:
            continue
        if technique and technique not in entry["techniques"]:
            continue
        if norm_tone and norm_tone not in entry["content_tones"]:
            continue
        if norm_medium and entry["visual_medium"] != norm_medium:
            continue

        score = 0
        reasons: list[str] = []
        if visual_job:
            score += 24
            reasons.append(f"visual_job:{visual_job}")
        if technique:
            score += 24
            reasons.append(f"technique:{technique}")
        if norm_tone:
            score += 20
            reasons.append(f"tone:{norm_tone}")
        if norm_medium:
            score += 26
            reasons.append(f"medium:{norm_medium}")

        searchable = " ".join(
            [
                entry["entry_id"],
                entry["title"],
                entry["category"],
                entry["source_project"],
                entry.get("upstream_entry") or "",
                entry["summary_zh"],
                entry.get("summary_en", ""),
                entry["use_when"],
                entry.get("intention", ""),
                entry["visual_medium"],
                " ".join(entry["visual_jobs"]),
                " ".join(entry["techniques"]),
                " ".join(entry["content_tones"]),
                " ".join(entry["tags"]),
            ]
        ).lower()

        if has_query:
            q_lower = query.strip().lower()
            query_signal = 0
            if q_lower == entry["entry_id"].lower() or q_lower == (entry.get("upstream_entry") or "").lower():
                query_signal += 200
                reasons.append("exact_entry_match")
            elif q_lower in searchable:
                query_signal += 45
                reasons.append("phrase_match")
            ascii_hits = sum(1 for tok in ascii_tokens if tok in searchable)
            if ascii_hits:
                query_signal += min(ascii_hits, 8) * 14
                reasons.append(f"token_hits:{ascii_hits}")
            zh_hits = sum(1 for bg in zh_bigrams if bg in searchable)
            if zh_hits:
                query_signal += min(zh_hits, 10) * 6
                reasons.append(f"zh_bigram_hits:{zh_hits}")
            syn_hits = sum(1 for grp in syn_groups if any(term in searchable for term in grp))
            if syn_hits:
                query_signal += min(syn_hits, 6) * 22
                reasons.append(f"semantic_groups:{syn_hits}")
            if query_signal == 0:
                continue
            score += query_signal
        elif not has_filters:
            score += 10

        # Priority bonus for locally rendered & audited T1 cases when semantically matched.
        if entry["tier"] == TIER_T1:
            score += 26
            reasons.append("tier:T1_verified_local_case")
        if entry.get("preview_stills"):
            score += 4
            reasons.append("has_preview_stills")

        tier_rank = 0 if entry["tier"] == TIER_T1 else 1
        scored.append((score, tier_rank, entry["entry_id"], entry, reasons))

    scored.sort(key=lambda item: (-item[0], item[1], item[2]))
    top = scored[: max(1, min(limit, 25))]
    candidates = [
        {
            "entry_id": entry["entry_id"],
            "tier": entry["tier"],
            "score": score,
            "match_reasons": reasons,
            "source_project": entry["source_project"],
            "title": entry["title"],
            "category": entry["category"],
            "visual_jobs": entry["visual_jobs"],
            "techniques": entry["techniques"],
            "content_tones": entry["content_tones"],
            "visual_medium": entry["visual_medium"],
            "summary_zh": entry["summary_zh"],
            "use_when": entry["use_when"],
            "avoid_when": entry.get("avoid_when", ""),
            "entry_file": entry["entry_file"],
            "card_doc": entry.get("card_doc"),
            "preview_stills": entry.get("preview_stills", []),
            "preview_mp4": entry.get("preview_mp4"),
            "editable_params": entry.get("editable_params", []),
            "known_pitfalls": entry.get("known_pitfalls", []),
            "next_command": f"python -B scripts/semantic_editorial.py motion-unpack --entry-id {entry['entry_id']}",
        }
        for score, _rank, _eid, entry, reasons in top
    ]
    return {
        "schema": "video_creation_pipeline.motion_entry_search.v1",
        "index_sha256": index["index_sha256"],
        "total_index_entries": index["entry_count"],
        "matched_count": len(scored),
        "filters": {
            "visual_job": visual_job,
            "technique": technique,
            "tone": norm_tone,
            "medium": norm_medium,
            "tier": norm_tier,
            "source_project": source_project,
            "query": query,
        },
        "candidates": candidates,
        "guidance": [
            "优先用 read_file 检查候选的 preview_stills 确认视觉气质与内容匹配，再运行 motion-unpack 读取完整依赖代码闭包。",
            "严禁只读摘要后直接手写动效；正式进入编码或 render-plan 前须填写并校验 motion_reference_binding.v1。",
        ],
    }


def unpack_motion_entry(entry_id: str, *, index_path: str | Path | None = None) -> dict[str, Any]:
    """Resolve full code closure, live SHA-256 digests, preview stills, and binding template for one entry."""
    index = load_motion_entry_index(index_path)
    entry = next((item for item in index["entries"] if item["entry_id"] == entry_id), None)
    if entry is None:
        raise ContractError(f"unknown motion entry_id: {entry_id}")
    boundary = Path(entry["boundary_root"]).resolve()
    seeds = [entry["entry_file"], *entry.get("extra_entry_files", [])]
    closure = resolve_import_closure(seeds, boundary)
    files = list(closure["files"])
    seen_paths = {item["path"] for item in files}

    card_doc = entry.get("card_doc")
    if card_doc and Path(card_doc).is_file():
        checked_doc = _check_safe_path_in_root(Path(card_doc), boundary, label="card doc")
        if checked_doc.as_posix() not in seen_paths:
            files.append(
                {
                    "path": checked_doc.as_posix(),
                    "relative": checked_doc.relative_to(boundary).as_posix(),
                    "role": "style_or_card_doc",
                    "bytes": checked_doc.stat().st_size,
                    "sha256": file_sha256(checked_doc),
                }
            )
            seen_paths.add(checked_doc.as_posix())

    stills_out = []
    for still in entry.get("preview_stills", []):
        still_path = Path(still)
        if still_path.is_file():
            checked_still = _check_safe_path_in_root(still_path, boundary, label="preview still")
            stills_out.append(
                {
                    "path": checked_still.as_posix(),
                    "relative": checked_still.relative_to(boundary).as_posix(),
                    "bytes": checked_still.stat().st_size,
                    "sha256": file_sha256(checked_still),
                }
            )

    preview_mp4_check = None
    if entry.get("preview_mp4"):
        mp4_path = Path(entry["preview_mp4"])
        expected_sha = entry.get("preview_sha256")
        if not mp4_path.is_file():
            mp4_status = "missing"
        elif expected_sha and file_sha256(mp4_path) == expected_sha:
            mp4_status = "verified"
        elif expected_sha:
            mp4_status = "changed"
        else:
            mp4_status = "present_unhashed"
        preview_mp4_check = {
            "path": mp4_path.as_posix(),
            "expected_sha256": expected_sha,
            "status": mp4_status,
        }

    binding_template = {
        "schema": BINDING_SCHEMA,
        "mode": "adapted_from_index",
        "entry_id": entry["entry_id"],
        "index_sha256": index["index_sha256"],
        "source_project": entry["source_project"],
        "upstream_commit": entry.get("upstream_commit"),
        "inspected_source_files": [
            {"path": item["path"], "relative": item["relative"], "sha256": item["sha256"]}
            for item in files
        ],
        "borrow": [],
        "change": [],
        "forbid": [],
        "notes": "",
    }
    return {
        "schema": UNPACK_SCHEMA,
        "entry_id": entry["entry_id"],
        "tier": entry["tier"],
        "index_sha256": index["index_sha256"],
        "entry": deepcopy(entry),
        "preview_stills": stills_out,
        "preview_mp4_check": preview_mp4_check,
        "inspected_source_files": files,
        "closure_truncated": closure["truncated"],
        "binding_template": binding_template,
        "next_steps": [
            "用 read_file 查看 preview_stills（如有）与 inspected_source_files 中的实际代码与样式文件。",
            "基于 binding_template 填写非空的 borrow / change / forbid 适配清单并保存为 JSON。",
            "运行 python -B scripts/semantic_editorial.py motion-binding-check --binding <binding.json> 校验门禁。",
        ],
    }


def _validate_nonempty_string_list(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ContractError(f"motion reference binding requires non-empty list: {field}")
    cleaned: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ContractError(f"motion reference binding {field} entries must be strings")
        stripped = item.strip()
        if len(stripped) < 3 or stripped.lower() in PLACEHOLDER_STRINGS:
            raise ContractError(f"motion reference binding {field} contains empty or placeholder item")
        cleaned.append(stripped)
    return cleaned


def validate_motion_reference_binding(
    binding: dict[str, Any],
    *,
    index_path: str | Path | None = None,
) -> dict[str, Any]:
    """Fail-closed validation ensuring an Agent inspected real index code before authoring or render-plan."""
    if not isinstance(binding, dict) or binding.get("schema") != BINDING_SCHEMA:
        raise ContractError("unsupported motion reference binding schema")
    mode = binding.get("mode")
    if mode == "from_scratch_approved":
        if binding.get("human_approved") is not True:
            raise ContractError("from_scratch_approved requires human_approved=true")
        approved_by = str(binding.get("approved_by") or "").strip()
        reason = str(binding.get("human_reason") or "").strip()
        if len(approved_by) < 2 or approved_by.lower() in PLACEHOLDER_STRINGS:
            raise ContractError("from_scratch_approved requires non-empty approved_by")
        if len(reason) < 6 or reason.lower() in PLACEHOLDER_STRINGS:
            raise ContractError("from_scratch_approved requires concrete human_reason")
        return {
            "status": "valid_from_scratch_approved",
            "mode": mode,
            "approved_by": approved_by,
        }
    if mode != "adapted_from_index":
        raise ContractError("motion reference binding mode must be adapted_from_index or from_scratch_approved")

    index = load_motion_entry_index(index_path)
    if binding.get("index_sha256") != index["index_sha256"]:
        raise ContractError("motion reference binding index_sha256 drift")
    entry_id = str(binding.get("entry_id") or "").strip()
    entry = next((item for item in index["entries"] if item["entry_id"] == entry_id), None)
    if entry is None:
        raise ContractError(f"motion reference binding references unknown entry_id: {entry_id}")

    inspected = binding.get("inspected_source_files")
    if not isinstance(inspected, list) or not inspected:
        raise ContractError("motion reference binding requires non-empty inspected_source_files")
    boundary = Path(entry["boundary_root"]).resolve()
    expected_primary = Path(entry["entry_file"]).resolve()
    seen_resolved: set[Path] = set()
    for row in inspected:
        if not isinstance(row, dict):
            raise ContractError("inspected_source_files entries must be objects")
        raw_path = str(row.get("path") or "").strip()
        expected_sha = str(row.get("sha256") or "").strip().lower()
        if not raw_path or not re.fullmatch(r"[0-9a-f]{64}", expected_sha):
            raise ContractError("inspected_source_files entry requires path and 64-char sha256")
        checked = _check_safe_path_in_root(Path(raw_path), boundary, label="inspected source file")
        actual_sha = file_sha256(checked)
        if actual_sha != expected_sha:
            raise ContractError(f"inspected source file sha256 mismatch: {checked.name}")
        seen_resolved.add(checked)
    if expected_primary not in seen_resolved:
        raise ContractError("inspected_source_files must include the entry primary code file")

    borrow = _validate_nonempty_string_list(binding.get("borrow"), "borrow")
    change = _validate_nonempty_string_list(binding.get("change"), "change")
    forbid = _validate_nonempty_string_list(binding.get("forbid"), "forbid")
    return {
        "status": "valid_motion_reference_binding",
        "mode": mode,
        "entry_id": entry_id,
        "tier": entry["tier"],
        "source_project": entry["source_project"],
        "verified_file_count": len(seen_resolved),
        "borrow_count": len(borrow),
        "change_count": len(change),
        "forbid_count": len(forbid),
    }
