"""品类 Profile 的规格数据类(段落、方向、词表、原则、趋势)与它们的构建函数。"""
from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

#: 竖屏每个段落至少占几拍(见 `CategoryProfile.section_capacity`)
PORTRAIT_SHOTS_PER_SECTION = 2

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
    #: 这一段的章节名由脚本给(问答式的"问题"、槽点式的"争议"):脚本多输出一个 `heading`,
    #: 章节条与章节转场显示它,而不是段落名
    heading: bool = False


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
    #: 1v1 对决:有车单(subject_count 台),各车各采事实、各建素材池,但段落按**维度**排、
    #: 全部是共用段(每段同时讲两台车),不像横评那样每车一章
    versus: bool = False
    #: 必须填选题角度(单点深挖:角度就是"这期只讲的那一个点")
    requires_angle: bool = False
    #: 这个片型最长做多少秒(0 = 不限)。只讲一个点的片子拉到 8 分钟就是注水
    max_duration: int = 0
    #: 允许的开头 / 结尾写法(id 见 pipeline.variety),第一个是默认。空 = 只有默认那一种
    openings: tuple[str, ...] = ()
    endings: tuple[str, ...] = ()
    #: 只在某几种开头 / 结尾下才进提示词的规则:(规则原文, "opening"|"ending", (id, ...))
    rule_when: tuple[tuple[str, str, tuple[str, ...]], ...] = ()
    #: 有车单的片型在建项目页的选题框标签 / 占位;空 = 前端按横评 / 对决给默认文案
    roster_label: str = ""
    roster_placeholder: str = ""

    @property
    def multi(self) -> bool:
        """有车单的片型(横评 / 对决):建项目要填车单,事实与素材按车分开采。"""
        return self.roundup or self.versus

    def rules_for(self, opening: str, ending: str) -> tuple[str, ...]:
        """这个变体下进提示词的方向规则(去掉条件不符的那几条,顺序不变)。"""
        want = {"opening": opening, "ending": ending}
        skip = {text for text, kind, ids in self.rule_when if want.get(kind) not in ids}
        return tuple(r for r in self.script_rules if r not in skip)

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
    #: 门禁 A 的素材覆盖检查:必备视角名 → 算进它的素材视角。每组要有一定秒数的确认视频,
    #: 缺得多就提示先补素材(见 `library.footage_gaps`)。空 = 不查视角
    coverage_views: dict[str, tuple[str, ...]] = field(default_factory=dict)
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
    #: 错误的部件名 → 为什么错。模型压缩句子时会把两个部件并成一个不存在的东西
    #: ("碳陶卡钳":碳陶的是盘),懂行的观众一眼看穿。出现即 error
    term_guard: dict[str, str] = field(default_factory=dict)
    #: 行家最想听的硬料(圈速、制动距离、刚度、保值率…):事实里有却全片不用,提醒
    hero_terms: tuple[str, ...] = ()
    #: TTS 读法:写法 → 读法(只改送 TTS 的文本,字幕照原文)。长词优先替换
    pronounce: dict[str, str] = field(default_factory=dict)

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
    #: 找官方发布会回放当素材(新品有发布会的品类才开,汽车 / 数码)
    launch_replays: bool = False

    # ---- 概念动画(解说轨 concept 版式,模板在 motion/scenes/<名>.html)----
    #: 模板名 → 什么时候用(拼进分镜提示词)。空 = 这个品类不开 concept 版式
    concept_scenes: dict[str, str] = field(default_factory=dict)
    #: 模板名 → 命中它的关键词(参数名 / 旁白里出现就选这个模板)。
    #: 用途:概念动画用得少,需要**兜底**把它补上去时,靠这些词判断这一拍该配哪张图 ——
    #: 讲电池的拍配电池包模板,讲补能的配补能模板,配错图等于把画面讲错了。
    concept_scene_keys: dict[str, tuple[str, ...]] = field(default_factory=dict)
    #: 概念动画的量程:参数在同类里大概落在什么范围,动画里的表盘、长短条才按真实数值画。
    #: 键 = 单位,或「参数名关键词|单位」(更具体,优先);值 = "下限-上限",下限比上限大表示越小越好
    #: (零百秒数)。没配上量程的参数只做排版强调,不画表示数量的图形
    concept_ranges: dict[str, str] = field(default_factory=dict)

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
        elif isinstance(default, dict) and key in ("hint_groups", "coverage_views"):
            kwargs[key] = {str(k): tuple(str(x) for x in (v or ())) for k, v in value.items()}
        elif isinstance(default, dict) and key == "detail_types":
            # 细节类型 → 各种说法(说法本身是词表,所以是元组)
            kwargs[key] = {str(k): tuple(str(x) for x in (v or ())) for k, v in value.items()}
        elif isinstance(default, dict) and key == "concept_scene_keys":
            # 模板名 → 命中它的关键词(词表)
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
