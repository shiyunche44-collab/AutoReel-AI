"""品类 Profile 注册表。

新增品类 = 在 `profiles/` 下放一个 YAML,**核心引擎零改动**。

Profile 声明了什么:
- 事实来源路由(用哪些 iFinD / 企查查 接口)
- 内容方向库(每个方向一套段落结构、视觉指导、脚本规则)
- 每个段落的视觉指导、运镜倾向、参考图视角
- 合规词表与免责声明

为什么不把段落结构交给 LLM 自由发挥:汽车解说的"钩子→卖点→参数→对比→建议"
是行业惯例,写死在 Profile 里能保证每条视频的节奏稳定,而 LLM 只负责
在给定结构内填内容 —— 这是把"结构正确性"从概率问题变成确定性问题。

**方向库(theme)**:一条视频只讲一个内容方向(车型迭代史 / 横向对比 / ...),
而不是把六个方向塞进同一条 60 秒视频。每个方向用一份 `blocks` 权重表声明
"有哪些段落、各占多重的篇幅",再由 `plan_for()` 按目标镜头数**确定性铺展**
成最终的段落序列。这样任意时长都能得到结构正确的序列,不必手写
`duration -> 段落列表` —— 7 个方向 × 5 个时长手写就是 35 条,极易写错。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Sequence

import yaml

PROFILES_DIR = Path(__file__).resolve().parent / "profiles"

#: 数据卡片版式,渲染实现在 pipeline/cards.py
CARD_KINDS: tuple[str, ...] = ("spec", "compare", "timeline", "bars")


class ProfileError(RuntimeError):
    """品类配置缺失或非法。"""


def _takes_remainder(block: dict[str, Any]) -> bool:
    """该段落块是否允许接收铺展的补位名额。

    默认允许。开场钩子这类铺垫段落应设 `remainder: false`,只取 floor ——
    否则最大余数法会把所有余量派给它(权重小 → 小数部分最大 → 优先补位),
    60 秒视频会变成 12 秒悬念开场 + 8 秒正片。
    """
    return bool(block.get("remainder", True))


@dataclass(frozen=True)
class SectionSpec:
    name: str
    #: 视觉指导(中文),会进入提示词编译
    beat: str = ""
    #: 运镜倾向,编译器会归一到官方运镜词表
    camera: str = ""
    #: 参考图视角标签
    angle: str = "exterior_45"
    #: 旁白风格提示
    narration_style: str = ""
    #: 该段落是否必须引用事实(参数硬数据为 true)
    requires_fact: bool = False
    #: 该段落画面需要出现**第二台车**(对比 / 迭代史场景)。
    #: 编译器据此允许可选的第二主体锚点;规则 9 仍只校验主主体。
    multi_subject: bool = False
    #: 数据卡片版式(spec / compare / timeline / bars)。非空时该段落的镜头
    #: 由代码渲染成卡片,数字逐条核对事实 —— 视频模型画不出准确的数字。
    card: str = ""
    #: 横屏解说轨:该段落允许的画面版式(见 pipeline/explainer.LAYOUTS),第一个是默认
    layouts: tuple[str, ...] = ()
    #: 横屏解说轨:该段落归属的章节(底部章节条上的名字),空则用段落名
    chapter: str = ""
    #: 该段落只讲某一主题的事实(如 quality = 召回 / 抽检记录)。事实里没有这个主题时
    #: 整段跳过(名额让给前一段),不让脚本硬写"没有记录"之类查无实据的结论
    fact_topic: str = ""


@dataclass(frozen=True)
class ThemeSpec:
    """一个内容方向。"""

    name: str
    label: str
    #: 方向意图,进入脚本提示词,决定叙事口吻与取舍
    intent: str = ""
    #: 方向专属硬规则,逐条进入脚本提示词
    script_rules: tuple[str, ...] = ()
    #: 方向专属事实来源名(沿用 providers.facts.SOURCES 命名空间)
    fact_sources: tuple[str, ...] = ()
    #: 该方向需要哪些事实层级,用于事实缺口告警
    required_scopes: tuple[str, ...] = ("product",)
    #: 允许直陈本车缺点(与"禁止贬低竞品"并存,只对 cost 方向打开)
    allow_subject_critique: bool = False
    #: 方向专属禁用词,与品类级 forbidden_terms 合并
    extra_forbidden_terms: tuple[str, ...] = ()
    #: 有序段落块:[{section: 段落名, weight: 相对权重}, ...]
    blocks: tuple[dict[str, Any], ...] = ()
    #: 传统写法:duration -> 段落序列。未声明 blocks 的 profile 走这条路径。
    plan: dict[int, tuple[str, ...]] = field(default_factory=dict)
    #: 多车横评:每台车各来一份的段落块(YAML 里 blocks 中的 `{per_subject: [...]}` 一项)。
    #: 非空 = 这是横评方向
    per_subject: tuple[dict[str, Any], ...] = ()
    #: per_subject 块在 blocks 里的位置(展开时插在这里)
    per_subject_at: int = -1
    #: 车单台数范围 (最少, 最多);只对横评方向有意义
    subject_count: tuple[int, int] = (0, 0)

    @property
    def uses_blocks(self) -> bool:
        return bool(self.blocks) or bool(self.per_subject)

    @property
    def roundup(self) -> bool:
        return bool(self.per_subject)

    def placeholder_subjects(self) -> tuple[str, ...]:
        """没有真车单时(配置自检、估节拍数)用的占位车名:按最少台数。"""
        return tuple(f"车{i + 1}" for i in range(max(1, self.subject_count[0])))


@dataclass(frozen=True)
class Slot:
    """铺展后的一个段落位:段落名 + 讲哪台车(空串 = 单车项目或共用段)。"""

    section: str
    subject: str = ""


@dataclass(frozen=True)
class ConsistencySpec:
    shared_reference_images: int = 5
    chain_last_frame: bool = True
    chain_group_size: int = 3


#: 素材视角里品类无关的三个:人、文字卡、其它。永远在视角表里,也永远不可用作背景
COMMON_VIEWS: tuple[str, ...] = ("people", "text", "other")


@dataclass(frozen=True)
class Vocab:
    """品类词表:引擎各层提示词与规则里**随品类变**的那部分文字。

    默认值是"通用消费品"口径,品类 YAML 的 `vocab:` 块按需覆盖。引擎代码里
    不再出现任何品类专属的词 —— 汽车的词全在 auto_long.yaml 里。
    """

    # ---- 主体称呼 ----
    #: 提示词里的品类名("你是中文{domain}品类的…")
    domain: str = "消费品"
    #: 指代主体的说法("只讲这台车")
    subject_noun: str = "这款产品"
    #: 主体的类别名("车型:小米YU7"、"目标车型")
    subject_label: str = "产品"
    #: 主体的单字简称("车是否完整在画面内")
    subject_short: str = "产品"
    #: 英文品类名词,进图片检索的相关性描述
    subject_en: str = "product"
    #: 公司的说法("这家车企成立于…")
    company_noun: str = "企业"

    # ---- 脚本 / 分镜提示词里的举例 ----
    number_examples: str = "参数、价格、销量等"
    product_examples: str = "规格、材质、尺寸、功能、价格"
    product_examples_short: str = "规格、功能、价格"
    spec_claim_example: str = "这款产品重量只有 5 公斤"
    keyword_examples: str = '"核心卖点"、"关键材质"'
    stat_examples: str = "售价、重量、容量"
    #: 分镜提示词里 view 怎么选
    view_guide: str = "讲产品本身、外观、细节给 product,讲使用、场景给 in_use。"
    #: 合规 lint 规则 5 的触发词:出现这些词说明这句在讲产品本身
    subject_words: tuple[str, ...] = ("这款", "该产品", "配置", "售价", "参数", "规格")

    # ---- 素材视角 ----
    #: 打标签用的视角表(不含 COMMON_VIEWS)
    views: tuple[str, ...] = ("product_hero", "detail", "in_use", "scene", "packaging")
    #: 背景偏好(分镜的 view 字段)→ 优先的素材视角。第一个是全片的主背景
    hint_groups: dict[str, tuple[str, ...]] = field(default_factory=lambda: {
        "product": ("product_hero", "detail", "packaging"),
        "in_use": ("in_use", "scene"),
    })
    #: 段落 angle → 背景偏好,没列的用 default_hint
    hint_of_angle: dict[str, str] = field(default_factory=lambda: {
        "lifestyle": "in_use", "result": "in_use",
    })
    default_hint: str = "product"
    #: 素材视角 → 镜头 angle。图片只有视角在这里的才当背景
    angle_of_view: dict[str, str] = field(default_factory=lambda: {
        "product_hero": "product_hero", "detail": "detail", "packaging": "product_hero",
        "in_use": "lifestyle", "scene": "lifestyle",
    })

    # ---- 打标签提示词 ----
    tag_variant_note: str = "注意同品牌的不同型号、不同代是不同产品。"
    tag_identify: str = ("画面主体是什么;有产品的话是什么品牌和型号,依据是什么"
                         "(logo、包装字样、外形),看不出来就直说看不出来;")
    tag_angles: str = ("产品整体展示、局部或材质特写、有人在使用、家居或户外场景、包装盒,"
                       "还是人物、文字卡、其它;")
    tag_target_rule: str = (
        '描述明确认出是目标产品(同品牌同系列;颜色、规格不同仍算)→ "yes";\n'
        '  明确认出是**别的品牌或别的系列** → "no";没点名产品 → "unknown"。')
    tag_target_cues: str = "logo、字样、外形"
    tag_view_guide: str = (
        "  product_hero(产品整体)、detail(局部 / 材质 / 做工特写)、\n"
        "  in_use(有人在使用产品)、scene(产品在家居或户外场景里)、packaging(包装盒)、")
    tag_no_subject: str = '描述里没有任何产品或产品部件 → "no_subject"(局部特写算有产品);'
    tag_desc_example: str = "灰色推车折叠后立在客厅"

    # ---- 事实层 ----
    #: 事实分层的品类专属提示词,与 intake 的通用词表合并
    product_hints: tuple[str, ...] = ()
    industry_hints: tuple[str, ...] = ()
    #: 行业指标检索用的行业名,空 = 品类 label
    industry_topic: str = ""
    extract_spec_examples: str = "规格 / 材质 / 售价 / 适用人群 / 配置"
    extract_year_example: str = "2020 年上市的初代款售价 1999 元"
    extract_topic_examples: str = "参数 / 价格 / 销量 / 财务 / 品牌"
    #: 事实口径(basis)的示例
    extract_basis_example: str = "标准版 · 2026款"
    #: 技术机理类事实的示例(抽取时单独成条)
    extract_tech_examples: str = "材质工艺、核心结构、关键部件、安全设计"

    # ---- 脚本深度 ----
    #: 空话套话:只表态不给论据的评价词,depth_lint 出现即提醒
    filler_terms: tuple[str, ...] = ("很猛", "很良心", "有诚意", "闭眼入", "yyds", "真香",
                                     "这句话很重要", "没得说", "吊打", "天花板")
    #: 必须讲原理的段落(depth_lint 检查这些段落有没有引用原理条目)
    depth_sections: tuple[str, ...] = ()
    #: 口径词:引用的事实原文里有它、句子里却没说,depth_lint 提醒补口径(如 CLTC)
    basis_terms: tuple[str, ...] = ()

    # ---- 背景细节冲突 ----
    #: 细节类型 → 该类型在旁白 / 画面描述里的各种说法。
    #: 用途:这一拍在讲"刹车"时,背景就别配一个"车门把手特写"——
    #: 特写是很抢戏的画面,观众会以为它就是正在讲的东西。
    #: 空 = 该品类不做这个过滤(背景只按视角挑)。
    detail_types: dict[str, tuple[str, ...]] = field(default_factory=dict)
    #: 算"特写"的视角。特写是局部近景,讲某个部件时最容易配错。
    closeup_views: tuple[str, ...] = ("detail",)

    # ---- 素材检索 ----
    discovery_zh: tuple[str, ...] = ("{subject} 宣传片", "{subject} 官方", "{subject} 产品展示",
                                     "{subject} 实拍")
    discovery_en: tuple[str, ...] = ("{en} official", "{en} commercial", "{en} product video")
    #: 品类专属的垃圾标题词 / 展示类标题词 / 官方号名字特征,与通用词表合并
    junk_words: tuple[str, ...] = ()
    showcase_words: tuple[str, ...] = ()
    official_markers: tuple[str, ...] = ()

    # ---- 概念动画(解说轨 concept 版式,模板在 motion/scenes/<名>.html)----
    #: 模板名 → 什么时候用(拼进分镜提示词)。空 = 这个品类不开 concept 版式
    concept_scenes: dict[str, str] = field(default_factory=dict)

    # ---- 前端 ----
    ui_subject_label: str = "产品"
    ui_placeholder: str = "例如:某品牌某型号"
    ui_help: str = "填具体型号,越具体素材和事实越准。"

    @property
    def all_views(self) -> tuple[str, ...]:
        return tuple(self.views) + COMMON_VIEWS

    @property
    def view_enum(self) -> str:
        return "|".join(self.hint_groups)

    def hint_views(self, hint: str) -> set[str]:
        return set(self.hint_groups.get(hint, ()))

    @property
    def primary_views(self) -> set[str]:
        """主背景的视角(第一组),数据卡底图从这里挑。"""
        return set(next(iter(self.hint_groups.values()), ()))

    # ---- 背景细节冲突 ----
    def detail_types_in(self, text: str) -> set[str]:
        """文本里提到了哪些细节类型(按 detail_types 的说法匹配)。"""
        low = (text or "").lower()
        out: set[str] = set()
        for name, words in self.detail_types.items():
            if any(w.lower() in low for w in words):
                out.add(name)
        return out

    def detail_words(self, names: Iterable[str]) -> set[str]:
        """这些细节类型在描述里会出现的各种说法(小写)。"""
        out: set[str] = set()
        for name in names:
            for w in self.detail_types.get(name, ()):
                out.add(w.lower())
        return out


@dataclass(frozen=True)
class Principle:
    """原理库的一条:脚本讲"为什么"时可以引用的通用技术常识。

    只讲机理和取舍,**不含任何数字** —— 数字仍然只能来自事实层。脚本按 `id`
    (k_ 开头)引用它,事实清单里出现了 `terms` 之一才会进提示词。
    """

    id: str
    terms: tuple[str, ...]
    explain: str
    tradeoff: str = ""

    def matches(self, text: str) -> bool:
        low = text.lower()
        return any(t.lower() in low for t in self.terms)


#: 原理条目里不许出现阿拉伯数字(数字要可追溯,原理库没有来源)
_PRINCIPLE_DIGIT = re.compile(r"\d")


def _build_principles(raw: Any) -> tuple[Principle, ...]:
    out: list[Principle] = []
    seen: set[str] = set()
    for item in raw or ():
        if not isinstance(item, dict):
            raise ProfileError("principles 的每一项必须是 {id, terms, explain, tradeoff} 映射")
        pid = str(item.get("id", "")).strip()
        if not pid.startswith("k_") or pid in seen:
            raise ProfileError(f"原理条目 id 必须以 k_ 开头且不重复:{pid!r}")
        seen.add(pid)
        p = Principle(id=pid, terms=tuple(str(t) for t in (item.get("terms") or ())),
                      explain=str(item.get("explain", "")).strip(),
                      tradeoff=str(item.get("tradeoff", "") or "").strip())
        if not p.terms or not p.explain:
            raise ProfileError(f"原理条目 {pid} 缺 terms 或 explain")
        if _PRINCIPLE_DIGIT.search(p.explain + p.tradeoff):
            raise ProfileError(f"原理条目 {pid} 不能含数字(数字只能来自事实层)")
        out.append(p)
    return tuple(out)


def _build_vocab(raw: dict[str, Any]) -> Vocab:
    known = {f.name: f for f in Vocab.__dataclass_fields__.values()}
    kwargs: dict[str, Any] = {}
    for key, value in (raw or {}).items():
        if key not in known:
            raise ProfileError(f"vocab 里的未知字段 {key!r}")
        default = getattr(Vocab(), key)
        if isinstance(default, tuple):
            kwargs[key] = tuple(str(x) for x in (value or ()))
        elif isinstance(default, dict) and key == "hint_groups":
            kwargs[key] = {str(k): tuple(str(x) for x in (v or ())) for k, v in value.items()}
        elif isinstance(default, dict) and key == "detail_types":
            # 细节类型 → 各种说法(说法本身是词表,所以是元组)
            kwargs[key] = {str(k): tuple(str(x) for x in (v or ())) for k, v in value.items()}
        elif isinstance(default, dict):
            kwargs[key] = {str(k): str(v) for k, v in (value or {}).items()}
        else:
            kwargs[key] = str(value).rstrip("\n")
    return Vocab(**kwargs)


@dataclass(frozen=True)
class TrendSpec:
    """热点选题的抓取口径(providers.trends + pipeline.topics)。

    没写 `trends:` 的品类只抓新闻 / 抖音 / B站,且查询词为空 —— 等于没有热点。
    """

    #: 榜单接口:dongchedi = 懂车帝销量 / 热度榜;空 = 没有公开榜单,靠新闻里的榜单报道
    rank: str = ""
    news_queries: tuple[str, ...] = ()
    bili_queries: tuple[str, ...] = ()
    #: 抖音热搜过滤的通用词(命中任一才留)
    hot_words: tuple[str, ...] = ()
    #: 品牌兜底词;用时并上 official_sites 的 aliases
    brands: tuple[str, ...] = ()
    #: 选题禁区:subject / angle 带这些词直接丢(如母婴的婴儿奶粉,广告法第二十条)
    exclude_words: tuple[str, ...] = ()
    #: 创建页热点面板的来源说明
    sources_label: str = ""
    #: 选题提示词:热点由哪些来源组成
    sources_desc: str = "近期新闻、抖音热搜、B站高播放视频"
    #: 选题提示词:subject 该怎么写(接在「全称,」之后)
    subject_rule: str = "写成「品牌+型号」。"
    #: 选题提示词第 5 条:优先什么样的热点
    priority_rule: str = "优先「有新闻或热搜事件 + B站高播放」的交叉热点。"


def _build_trends(raw: Any) -> TrendSpec:
    known = TrendSpec.__dataclass_fields__
    kwargs: dict[str, Any] = {}
    for key, value in (raw or {}).items():
        if key not in known:
            raise ProfileError(f"trends 里的未知字段 {key!r}")
        if isinstance(getattr(TrendSpec(), key), tuple):
            kwargs[key] = tuple(str(x) for x in (value or ()))
        else:
            kwargs[key] = str(value or "").rstrip("\n")
    return TrendSpec(**kwargs)


@dataclass(frozen=True)
class CategoryProfile:
    name: str
    label: str
    aspect_ratio: str = "9:16"
    duration_grid: int = 6
    #: 画面链路。目前只有 explainer = 横屏信息解说(实拍只当背景,信息靠叠加层,
    #: 时长跟着旁白走);竖屏逐镜头生成链路已删除
    track: str = "explainer"
    #: 可选成片时长(秒)。空 = 全局 SUPPORTED_DURATIONS
    durations: tuple[int, ...] = ()
    fact_sources: tuple[str, ...] = ()
    #: duration -> 段落名列表,长度必须等于 duration/6
    #: (未声明 themes 的老 profile 走这条路径)
    section_plan: dict[int, tuple[str, ...]] = field(default_factory=dict)
    sections: dict[str, SectionSpec] = field(default_factory=dict)
    #: 内容方向库。name -> ThemeSpec
    themes: dict[str, ThemeSpec] = field(default_factory=dict)
    #: 未显式指定方向时使用哪个方向
    default_theme: str = ""
    visual_keywords: tuple[str, ...] = ()
    reference_query_template: str = "{subject} 实拍 {angle}"
    #: 视角标签 -> 检索用的中文描述
    angle_queries: dict[str, str] = field(default_factory=dict)
    forbidden_terms: tuple[str, ...] = ()
    required_disclaimers: tuple[str, ...] = ()
    music_prompt: str = "instrumental background music, steady tempo, no vocals"
    #: 自动收集 BGM 的 Mixkit 风格页 slug(https://mixkit.co/free-stock-music/<slug>/),空 = 不自动收
    bgm_genres: tuple[str, ...] = ()
    #: 官方素材渠道:品牌键 -> {aliases: [...], sites: [域名...], media: [媒体中心域名...],
    #: tiktok: [官方号...]}。
    #: 只声明**域名**,不写死车型页地址 —— 车型页靠 `site:域名 车型` 站内检索定位,
    #: 官网改版不会让配置失效。新增品牌只改 YAML,不改代码。
    official_sites: dict[str, dict[str, tuple[str, ...]]] = field(default_factory=dict)
    script_style: str = ""
    #: 原理库(见 Principle)
    principles: tuple[Principle, ...] = ()
    #: 推算规则名(pipeline/derive.py 的 RULES),按顺序跑
    derive: tuple[str, ...] = ()
    #: 品类词表(见 Vocab)
    vocab: Vocab = field(default_factory=Vocab)
    #: 事实来源 → 检索词(主体名之后的部分)。没列的来源用 providers.facts 里的通用词
    fact_queries: dict[str, str] = field(default_factory=dict)
    #: 静图来源(providers.official.GALLERIES 里的名字),按顺序合并
    galleries: tuple[str, ...] = ()
    #: 旁白音色,空 = providers.audio.DEFAULT_VOICE_ID
    voice_id: str = ""
    #: 热点选题口径(见 TrendSpec)
    trends: TrendSpec = field(default_factory=TrendSpec)
    #: 骨架品类为 True 时,平台会明确提示"该品类尚未完整实现"
    skeleton: bool = False
    todo: tuple[str, ...] = ()

    @property
    def trend_brands(self) -> tuple[str, ...]:
        """热点里认品牌用的词:trends.brands + 官方渠道的全部别名。"""
        words = [*self.trends.brands]
        for spec in self.official_sites.values():
            words.extend(spec.get("aliases", ()))
        return tuple(dict.fromkeys(w for w in words if w))

    # ------------------------------------------------------------------
    # 画幅与时长
    # ------------------------------------------------------------------
    @property
    def is_explainer(self) -> bool:
        return self.track == "explainer"

    def supported_durations(self) -> tuple[int, ...]:
        from .. import config

        return self.durations or config.SUPPORTED_DURATIONS

    def shots_for(self, duration: int) -> int:
        """该时长铺几个镜头(解说轨叫节拍)。按本 profile 的栅格算。"""
        if duration % self.duration_grid != 0:
            raise ProfileError(
                f"品类 {self.name}: 时长 {duration}s 必须是 {self.duration_grid} 的整数倍"
            )
        return duration // self.duration_grid

    @property
    def frame_size(self) -> tuple[int, int]:
        """成片像素尺寸。横屏 1920×1080,竖屏 1080×1920,其它比例按短边 1080 推。"""
        try:
            a, b = (int(x) for x in self.aspect_ratio.split(":"))
        except ValueError:
            a, b = 9, 16
        if a >= b:
            return (int(round(1080 * a / b / 2)) * 2, 1080)
        return (1080, int(round(1080 * b / a / 2)) * 2)

    # ------------------------------------------------------------------
    # 方向
    # ------------------------------------------------------------------
    def theme_spec(self, name: str | None = None) -> ThemeSpec:
        """取方向。未知或空名一律回退默认方向,**不抛异常**。

        回退而非抛错是有意的:方向已经写进 projects 表,手改库或换 profile
        都可能让库里出现当前配置里不存在的名字。此时让已跑到一半的项目
        继续跑(退化成默认叙事)远好过把它卡死。
        """
        key = (name or "").strip()
        if key and key in self.themes:
            return self.themes[key]
        if self.default_theme and self.default_theme in self.themes:
            return self.themes[self.default_theme]
        # 极端兜底:配置里一个方向都没有(不应通过 _validate)
        if self.themes:
            return next(iter(self.themes.values()))
        return ThemeSpec(name="", label=self.label)

    def sources_for(self, name: str | None = None) -> tuple[str, ...]:
        """该方向应采集的事实来源。未声明时回退品类级来源。"""
        theme = self.theme_spec(name)
        return theme.fact_sources or self.fact_sources

    # ------------------------------------------------------------------
    # 段落计划
    # ------------------------------------------------------------------
    def plan_for(self, duration: int, name: str | None = None) -> tuple[str, ...]:
        """返回该方向在该时长下的段落序列。

        两条路径:
        - 方向声明了 `blocks`:按权重**确定性铺展**到 duration/6 个镜头。
        - 未声明(老 profile):走手写 `section_plan`,缺失时按最近档裁剪补齐。
        """
        theme = self.theme_spec(name)
        if theme.roundup:
            return tuple(s.section for s in self.slots_for(duration, name))
        if theme.uses_blocks:
            return self._expand_blocks(theme, duration)
        plan = theme.plan or self.section_plan
        if duration in plan:
            return plan[duration]
        if not plan:
            raise ProfileError(f"品类 {self.name} 未声明任何 section_plan 或 blocks")
        # 退化路径:用最接近的方案裁剪/补齐
        closest = min(plan, key=lambda d: abs(d - duration))
        target_shots = duration // self.duration_grid
        base = list(plan[closest])
        while len(base) < target_shots:
            base.insert(max(1, len(base) - 1), base[len(base) // 2])
        return tuple(base[:target_shots])

    def slots_for(self, duration: int, name: str | None = None,
                  subjects: Sequence[str] | None = None) -> tuple[Slot, ...]:
        """段落序列 + 每段讲哪台车。

        - 单车方向:等价于 `plan_for`,subject 全为空。
        - 横评方向:per_subject 块按车单展开成 N 份(每份带车名),和其它块一起
          走同一套最大余数铺展 —— 每台车的段落连在一起,车与车按车单顺序排。
          不给车单时用占位车名(按最少台数),只用于估节拍数和配置自检。
        """
        theme = self.theme_spec(name)
        if not theme.roundup:
            return tuple(Slot(s) for s in self.plan_for(duration, name))
        names = [str(x).strip() for x in (subjects or theme.placeholder_subjects()) if str(x).strip()]
        flat = self.roundup_blocks(theme, names, shots=duration // self.duration_grid)
        counts = self._allocate(flat, duration)
        out: list[Slot] = []
        for block, count in zip(flat, counts):
            out.extend([Slot(str(block.get("section", "")), str(block.get("subject", "")))] * count)
        return tuple(out[:duration // self.duration_grid])

    @staticmethod
    def roundup_blocks(theme: ThemeSpec, subjects: Sequence[str],
                       shots: int | None = None) -> list[dict[str, Any]]:
        """横评方向的块表:per_subject 块在它的位置按车单展开。

        给了 `shots` 且每车全套段落放不下时**精简**:按 `drop` 从小到大整档砍掉
        每台车的同一种段落(先砍「适合谁」,再砍「参数价格」),直到放得下或只剩
        必留段落 —— 5 台车塞进 2 分钟时每台车只留亮相 + 卖点,而不是把结论截掉。
        每台车砍的段落相同,车与车的结构保持一致。"""
        per_subject = list(theme.per_subject)
        if shots is not None:
            levels = sorted({b["drop"] for b in per_subject if b.get("drop")})
            for level in levels:
                if len(theme.blocks) + len(per_subject) * len(subjects) <= shots:
                    break
                per_subject = [b for b in per_subject if b.get("drop") != level]
        at = theme.per_subject_at if theme.per_subject_at >= 0 else len(theme.blocks)
        per_car = [{**b, "subject": name} for name in subjects for b in per_subject]
        return [*theme.blocks[:at], *per_car, *theme.blocks[at:]]

    @staticmethod
    def roundup_min_blocks(theme: ThemeSpec, count: int) -> int:
        """`count` 台车精简到底(只留 drop=0 的段落)时的段落块数。"""
        core = [b for b in theme.per_subject if not b.get("drop")]
        return len(theme.blocks) + len(core) * count

    def _expand_blocks(self, theme: ThemeSpec, duration: int) -> tuple[str, ...]:
        counts = self._allocate(theme.blocks, duration)
        out: list[str] = []
        for block, count in zip(theme.blocks, counts):
            out.extend([str(block.get("section", ""))] * count)
        return tuple(out[:duration // self.duration_grid])

    def _allocate(self, blocks: Sequence[dict[str, Any]], duration: int) -> list[int]:
        """把段落块权重表铺展成恰好 duration/6 个段落的序列。

        纯函数、无随机:同一份配置 + 同一时长,结果永远相同。
        算法:
          1. base_i = max(1, floor(n * w_i / Σw))
          2. 多了 → 优先从"允许补位且仍 >1"的块里减,按权重从大到小
          3. 少了 → 按小数部分从大到小补 1,同分按声明顺序;
             **跳过声明了 `remainder: false` 的块**

        第 3 条为什么要跳过:最大余数法是标准做法,但它天然把余量派给
        "权重小、小数部分大"的块 —— 也就是开场钩子。实测 60 秒下 7 个方向
        里有 6 个拿到 2 个钩子,12 秒纯悬念开场把正片挤掉了。
        钩子这类铺垫段落取 floor 就够,补位名额应该留给内容块。
        """
        if duration % self.duration_grid != 0:
            raise ProfileError(
                f"品类 {self.name}: 时长 {duration}s 必须是 {self.duration_grid} 的整数倍"
            )
        shots = duration // self.duration_grid
        weights = [max(1, int(b.get("weight", 1))) for b in blocks]
        total = sum(weights)

        exact = [shots * w / total for w in weights]
        base = [max(1, int(x // 1)) for x in exact]

        diff = shots - sum(base)
        if diff > 0:
            ranked = sorted(
                range(len(blocks)),
                key=lambda i: (-(exact[i] - int(exact[i])), i),
            )
            order = [i for i in ranked if _takes_remainder(blocks[i])] or ranked
            for k in range(diff):
                base[order[k % len(order)]] += 1
        elif diff < 0:
            # 先减允许补位的块(它们本来就是有余量的),再退到其它块
            order = sorted(
                range(len(blocks)),
                key=lambda i: (not _takes_remainder(blocks[i]), -weights[i], i),
            )
            # 每轮扫一遍,直到补齐或所有块都压到 1(后者由 _validate 提前拦下)
            while diff < 0:
                shrunk = False
                for i in order:
                    if base[i] > 1:
                        base[i] -= 1
                        diff += 1
                        shrunk = True
                        if diff == 0:
                            break
                if not shrunk:
                    break
        return base

    def drop_unbacked_slots(self, slots: Sequence[Slot], topics: Iterable[str]) -> list[Slot]:
        """`drop_unbacked` 的段落位版本:只换段落名,讲哪台车不动。"""
        sections = self.drop_unbacked([s.section for s in slots], topics)
        return [Slot(sec, s.subject) for sec, s in zip(sections, slots)]

    def drop_unbacked(self, plan: Sequence[str], topics: Iterable[str]) -> list[str]:
        """声明了 fact_topic、但事实里没有该主题的段落,名额让给前一段(开头的让给后一段)。
        段落数不变 —— 铺展好的节拍数和时长估算都还成立。"""
        have = set(topics)
        keep = [not self.section(s).fact_topic or self.section(s).fact_topic in have
                for s in plan]
        if all(keep) or not any(keep):
            return list(plan)
        out = list(plan)
        for i, ok in enumerate(keep):
            if ok:
                continue
            prev = next((out[j] for j in range(i - 1, -1, -1) if keep[j]), None)
            nxt = next((plan[j] for j in range(i + 1, len(plan)) if keep[j]), None)
            out[i] = prev or nxt or out[i]
        return out

    def section(self, name: str) -> SectionSpec:
        return self.sections.get(name, SectionSpec(name=name))

    def reference_query(self, subject: str, angle: str) -> str:
        angle_desc = self.angle_queries.get(angle, angle)
        return self.reference_query_template.format(subject=subject, angle=angle_desc)

    def reference_prompt(self, subject: str, angle: str) -> str:
        """检索工具的**相关性过滤说明**(与 `reference_query` 分工不同)。

        `reference_query` 决定"搜什么"(中文检索串),`reference_prompt` 决定
        "要什么样的图"(英文相关性描述)。检索工具的 `queries[].prompt` 是必填的
        相关性过滤器,写得越具体,返回的越接近编辑部级产品照,越不像电商白底图
        或销量柱状图 —— 这正是素材层最需要挡掉的两类噪音。
        """
        view = _ANGLE_PROMPT_EN.get(angle, f"{angle} view")
        noun = self.vocab.subject_en
        return (
            f"real photograph of {subject} ({noun}), {view}, "
            f"professional editorial photo, {_ANGLE_NEGATIVE_EN}"
        )


#: 视角标签 → 英文相关性描述。只覆盖各 profile 实际用到的标签;
#: 未收录的标签退回 `"{angle} view"`,不会让整条检索失效。
_ANGLE_PROMPT_EN: dict[str, str] = {
    "exterior_45": "exterior three-quarter front view, entire vehicle visible in frame",
    "product_hero": "three-quarter hero product shot, entire product visible in frame",
    "rear": "rear view, tail lights and rear bumper clearly visible",
    "detail": "macro close-up detail shot of a specific component",
    "interior": "interior cabin view, dashboard and front seats visible",
    "screen_spec": "close-up of a screen or digital data readout interface",
    "industry": "auto show floor or press conference stage",
    "texture": "macro texture close-up of the product surface",
    "lifestyle": "real in-use lifestyle scene",
    "result": "close-up result shot after application",
    "comparison": "two products side by side for comparison",
}

#: 一律排除的噪音类型。电商白底图与销量图表是检索最高频的两类误命中:
#: 前者不是这台车,后者根本不是照片。
_ANGLE_NEGATIVE_EN = (
    "no diagrams, no charts, no graphs, no illustrations, no 3D renders, "
    "no text overlays, no watermarks, no e-commerce product listings, "
    "no showroom price boards"
)


# --------------------------------------------------------------------------
# 加载
# --------------------------------------------------------------------------
def _build_theme(name: str, raw: dict[str, Any]) -> ThemeSpec:
    plan: dict[int, tuple[str, ...]] = {}
    for key, value in (raw.get("plan") or {}).items():
        plan[int(key)] = tuple(str(v) for v in value)

    def _block(item: Any) -> dict[str, Any]:
        if not isinstance(item, dict):
            raise ProfileError(f"方向 {name}: blocks 的每一项必须是 section/weight 映射")
        return {
            "section": str(item.get("section", "")),
            "weight": int(item.get("weight", 1)),
            # 必须显式搬运:不搬的话 remainder: false 会在解析阶段被丢掉,
            # 配置看起来写了、实际不生效 —— 铺展结果会静默回到旧行为。
            "remainder": bool(item.get("remainder", True)),
            # 横评每车段落的精简顺序:拍数不够时先砍 drop 小的(1 先砍),0 = 必留
            "drop": int(item.get("drop", 0) or 0),
        }

    blocks: list[dict[str, Any]] = []
    per_subject: list[dict[str, Any]] = []
    per_subject_at = -1
    for item in (raw.get("blocks") or []):
        if isinstance(item, dict) and "per_subject" in item:
            # 多车横评:这一项是"每台车各来一份"的段落块组
            if per_subject_at >= 0:
                raise ProfileError(f"方向 {name}: per_subject 块最多一个")
            per_subject_at = len(blocks)
            per_subject = [_block(b) for b in (item.get("per_subject") or [])]
            if not per_subject:
                raise ProfileError(f"方向 {name}: per_subject 块不能为空")
            continue
        blocks.append(_block(item))
    count = raw.get("subject_count") or ([2, 5] if per_subject else [0, 0])
    try:
        lo, hi = (int(x) for x in count)
    except (TypeError, ValueError) as exc:
        raise ProfileError(f"方向 {name}: subject_count 必须是 [最少, 最多]") from exc

    return ThemeSpec(
        name=str(raw.get("name", name)),
        label=str(raw.get("label", name)),
        intent=str(raw.get("intent", "")),
        script_rules=tuple(str(r) for r in (raw.get("script_rules") or ())),
        fact_sources=tuple(str(s) for s in (raw.get("fact_sources") or ())),
        required_scopes=tuple(str(s) for s in (raw.get("required_scopes") or ("product",))),
        allow_subject_critique=bool(raw.get("allow_subject_critique", False)),
        extra_forbidden_terms=tuple(str(t) for t in (raw.get("extra_forbidden_terms") or ())),
        blocks=tuple(blocks),
        plan=plan,
        per_subject=tuple(per_subject),
        per_subject_at=per_subject_at,
        subject_count=(lo, hi),
    )


def _build(name: str, raw: dict[str, Any]) -> CategoryProfile:
    sections = {
        key: SectionSpec(
            name=key,
            beat=str(value.get("beat", "")),
            camera=str(value.get("camera", "")),
            angle=str(value.get("angle", "exterior_45")),
            narration_style=str(value.get("narration_style", "")),
            requires_fact=bool(value.get("requires_fact", False)),
            multi_subject=bool(value.get("multi_subject", False)),
            card=str(value.get("card", "") or ""),
            layouts=tuple(str(x) for x in (value.get("layouts") or ())),
            chapter=str(value.get("chapter", "") or ""),
            fact_topic=str(value.get("fact_topic", "") or ""),
        )
        for key, value in (raw.get("sections") or {}).items()
    }
    plan: dict[int, tuple[str, ...]] = {}
    for key, value in (raw.get("section_plan") or {}).items():
        plan[int(key)] = tuple(str(v) for v in value)

    themes = {
        str(key): _build_theme(str(key), value or {})
        for key, value in (raw.get("themes") or {}).items()
    }

    if not themes:
        # 未声明方向的老 profile(beauty / digital / home)合成一个默认方向,
        # 行为与改造前完全一致。
        themes["overview"] = ThemeSpec(
            name="overview",
            label="通用概览",
            fact_sources=tuple(raw.get("fact_sources") or ()),
            plan=dict(plan),
        )

    default_theme = str(raw.get("default_theme") or "")
    if default_theme not in themes:
        default_theme = "overview" if "overview" in themes else next(iter(themes))

    return CategoryProfile(
        name=str(raw.get("name", name)),
        label=str(raw.get("label", name)),
        aspect_ratio=str(raw.get("aspect_ratio", "9:16")),
        duration_grid=int(raw.get("duration_grid", 6)),
        track=str(raw.get("track", "explainer") or "explainer"),
        durations=tuple(int(d) for d in (raw.get("durations") or ())),
        fact_sources=tuple(raw.get("fact_sources") or ()),
        section_plan=plan,
        sections=sections,
        themes=themes,
        default_theme=default_theme,
        visual_keywords=tuple(raw.get("visual_keywords") or ()),
        reference_query_template=str(
            raw.get("reference_query_template", "{subject} 实拍 {angle}")
        ),
        angle_queries={str(k): str(v) for k, v in (raw.get("angle_queries") or {}).items()},
        official_sites={
            str(brand): {
                key: tuple(str(x) for x in (spec or {}).get(key) or ())
                for key in ("aliases", "sites", "media", "tiktok", "company")
            }
            for brand, spec in (raw.get("official_sites") or {}).items()
        },
        vocab=_build_vocab(raw.get("vocab") or {}),
        fact_queries={str(k): str(v) for k, v in (raw.get("fact_queries") or {}).items()},
        galleries=tuple(str(g) for g in (raw.get("galleries") or ())),
        voice_id=str(raw.get("voice_id") or ""),
        forbidden_terms=tuple(raw.get("forbidden_terms") or ()),
        required_disclaimers=tuple(raw.get("required_disclaimers") or ()),
        music_prompt=str(
            raw.get("music_prompt", "instrumental background music, no vocals")
        ),
        bgm_genres=tuple(str(g) for g in (raw.get("bgm_genres") or ())),
        script_style=str(raw.get("script_style", "")),
        principles=_build_principles(raw.get("principles")),
        derive=tuple(str(d) for d in (raw.get("derive") or ())),
        trends=_build_trends(raw.get("trends")),
        skeleton=bool(raw.get("skeleton", False)),
        todo=tuple(str(t) for t in (raw.get("todo") or ())),
    )


def _min_supported_shots(profile: CategoryProfile) -> int:
    """最短受支持时长对应的镜头数 —— 块数不能超过它,否则铺展会挤爆。"""
    durations = profile.supported_durations() or (60,)
    return min(durations) // profile.duration_grid


#: 解说轨的版式名。与 pipeline/explainer.LAYOUTS 保持一致(这里不反向导入流水线)
EXPLAINER_LAYOUTS: tuple[str, ...] = ("broll", "panel", "highlight", "card", "stat", "concept", "hook")


def _validate(profile: CategoryProfile) -> None:
    if profile.track != "explainer":
        raise ProfileError(f"品类 {profile.name}: track={profile.track!r} 只能是 explainer"
                           "(竖屏链路已删除)")
    for sname, sec in profile.sections.items():
        bad = [x for x in sec.layouts if x not in EXPLAINER_LAYOUTS]
        if bad:
            raise ProfileError(f"品类 {profile.name} 段落 {sname}: 未知版式 {bad}")
        if "concept" in sec.layouts and not profile.vocab.concept_scenes:
            raise ProfileError(f"品类 {profile.name} 段落 {sname}: 用了 concept 版式,"
                               "但 vocab.concept_scenes 没配任何动画模板")

    # 1. 老写法:手写 section_plan
    for duration, plan in profile.section_plan.items():
        expected = duration // profile.duration_grid
        if len(plan) != expected:
            raise ProfileError(
                f"品类 {profile.name}: 时长 {duration}s 需 {expected} 个段落,"
                f"实际声明 {len(plan)} 个"
            )
        unknown = [s for s in plan if s not in profile.sections]
        if unknown:
            raise ProfileError(
                f"品类 {profile.name}: section_plan 引用了未定义的段落 {unknown}"
            )

    # 2. 方向库
    min_shots = _min_supported_shots(profile)
    for tname, theme in profile.themes.items():
        if theme.roundup:
            lo, hi = theme.subject_count
            if not 1 <= lo <= hi:
                raise ProfileError(
                    f"品类 {profile.name} 方向 {tname}: subject_count 需满足 1 ≤ 最少 ≤ 最多")
            if not any(not b.get("drop") for b in theme.per_subject):
                raise ProfileError(
                    f"品类 {profile.name} 方向 {tname}: per_subject 至少要有一个必留(drop=0)段落")
            # 最多台数精简到底,也要塞得进最短时长 —— 任何车数 × 任何时长都能建项目
            need = CategoryProfile.roundup_min_blocks(theme, hi)
            if need > min_shots:
                raise ProfileError(
                    f"品类 {profile.name} 方向 {tname}: {hi} 台车精简后仍要 {need} 个段落块,"
                    f"超过最短时长的镜头数 {min_shots}")
            for block in (*theme.blocks, *theme.per_subject):
                section = str(block.get("section", ""))
                if section not in profile.sections:
                    raise ProfileError(
                        f"品类 {profile.name} 方向 {tname}: 引用了未定义的段落 {section!r}")
                if int(block.get("weight", 1)) < 1:
                    raise ProfileError(
                        f"品类 {profile.name} 方向 {tname}: 段落块 {section} 的 weight 必须 ≥1")
        elif theme.uses_blocks:
            if len(theme.blocks) > min_shots:
                raise ProfileError(
                    f"品类 {profile.name} 方向 {tname}: 声明了 {len(theme.blocks)} 个段落块,"
                    f"超过最短时长的镜头数 {min_shots},铺展时必然压到 0"
                )
            for block in theme.blocks:
                section = str(block.get("section", ""))
                if not section:
                    raise ProfileError(f"品类 {profile.name} 方向 {tname}: 段落块缺少 section")
                if section not in profile.sections:
                    raise ProfileError(
                        f"品类 {profile.name} 方向 {tname}: 引用了未定义的段落 {section!r}"
                    )
                weight = int(block.get("weight", 1))
                if weight < 1:
                    raise ProfileError(
                        f"品类 {profile.name} 方向 {tname}: 段落块 {section} 的 weight 必须 ≥1"
                    )
        else:
            for duration, plan in theme.plan.items():
                expected = duration // profile.duration_grid
                if len(plan) != expected:
                    raise ProfileError(
                        f"品类 {profile.name} 方向 {tname}: 时长 {duration}s 需 {expected} 个段落,"
                        f"实际声明 {len(plan)} 个"
                    )
                unknown = [s for s in plan if s not in profile.sections]
                if unknown:
                    raise ProfileError(
                        f"品类 {profile.name} 方向 {tname}: 引用了未定义的段落 {unknown}"
                    )

    # 3. 卡片版式写错会让该段落静默退回生成,加载期就拦掉
    for sname, sec in profile.sections.items():
        if sec.card and sec.card not in CARD_KINDS:
            raise ProfileError(
                f"品类 {profile.name} 段落 {sname}: card={sec.card!r} 不在 {CARD_KINDS} 之中"
            )

    # 4. 铺展必须落在栅格上 —— 这是最容易写错的环节,在加载期就拦掉
    for duration in profile.supported_durations():
        if duration % profile.duration_grid != 0:
            continue
        for tname in profile.themes:
            got = len(profile.plan_for(duration, tname))
            if got != duration // profile.duration_grid:
                raise ProfileError(
                    f"品类 {profile.name} 方向 {tname}: {duration}s 铺展出 {got} 个段落,"
                    f"应为 {duration // profile.duration_grid} 个"
                )

    # 5. 视角表前后自洽:写错一个视角名,背景会静默挑不到图
    v = profile.vocab
    all_views = set(v.all_views)
    for group, views in v.hint_groups.items():
        bad = [x for x in views if x not in all_views]
        if bad:
            raise ProfileError(f"品类 {profile.name}: hint_groups.{group} 引用了未知视角 {bad}")
    bad = [x for x in v.angle_of_view if x not in all_views]
    if bad:
        raise ProfileError(f"品类 {profile.name}: angle_of_view 引用了未知视角 {bad}")
    bad = [x for x in (*v.hint_of_angle.values(), v.default_hint) if x not in v.hint_groups]
    if bad:
        raise ProfileError(f"品类 {profile.name}: hint_of_angle / default_hint 引用了未知偏好 {bad}")

    if profile.default_theme not in profile.themes:
        raise ProfileError(
            f"品类 {profile.name}: default_theme={profile.default_theme!r} 不在 themes 中"
        )


#: `extends` 继承时按键合并(子 profile 只写增改的条目)的字典;其余键整体覆盖。
#: themes 故意整体覆盖:父 profile 的方向是按它的栅格和版式写的,不该悄悄带过来。
_MERGED_KEYS = ("sections", "official_sites", "angle_queries", "vocab", "fact_queries")


def _load_raw(name: str, chain: tuple[str, ...] = ()) -> dict[str, Any]:
    path = PROFILES_DIR / f"{name}.yaml"
    if not path.is_file():
        available = sorted(p.stem for p in PROFILES_DIR.glob("*.yaml"))
        raise ProfileError(f"未知品类 {name!r}。可用品类:{available}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    parent = str(raw.pop("extends", "") or "")
    if not parent:
        return raw
    if parent in chain or parent == name:
        raise ProfileError(f"品类 {name}: extends 循环引用 {chain + (name, parent)}")
    base = _load_raw(parent, chain + (name,))
    merged = {**base, **raw}
    for key in _MERGED_KEYS:
        if isinstance(base.get(key), dict) and isinstance(raw.get(key), dict):
            merged[key] = {**base[key], **raw[key]}
    return merged


@lru_cache(maxsize=None)
def load_profile(name: str) -> CategoryProfile:
    raw = _load_raw(name)
    profile = _build(name, raw)
    _validate(profile)
    return profile


def available_categories() -> list[dict[str, Any]]:
    out = []
    for path in sorted(PROFILES_DIR.glob("*.yaml")):
        try:
            profile = load_profile(path.stem)
        except Exception as exc:  # pragma: no cover - 配置错误不应拖垮列表页
            out.append({"name": path.stem, "label": path.stem, "error": str(exc),
                        "skeleton": True, "durations": [], "themes": [],
                        "default_theme": ""})
            continue
        out.append({
            "name": profile.name,
            "label": profile.label,
            "skeleton": profile.skeleton,
            "todo": list(profile.todo),
            # 时长由 profile 声明(缺省用全局档位),不再取 section_plan 的键 ——
            # 方向库模式下 plan 由 blocks 铺展而来,没有"键"可言。
            "durations": list(profile.supported_durations()),
            "track": profile.track,
            "aspect_ratio": profile.aspect_ratio,
            "default_theme": profile.default_theme,
            "ui": {
                "subject_label": profile.vocab.ui_subject_label,
                "placeholder": profile.vocab.ui_placeholder,
                "help": profile.vocab.ui_help,
                "trends_label": profile.trends.sources_label,
            },
            "themes": [
                {"name": t.name, "label": t.label, "intent": t.intent,
                 "roundup": t.roundup, "subject_count": list(t.subject_count)}
                for t in profile.themes.values()
            ],
        })
    return out


def default_category() -> str:
    from .. import config

    try:
        load_profile(config.DEFAULT_CATEGORY)
    except ProfileError:
        return "auto_long"
    return config.DEFAULT_CATEGORY
