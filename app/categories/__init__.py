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

    @property
    def uses_blocks(self) -> bool:
        return bool(self.blocks)


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

    # ---- 素材检索 ----
    discovery_zh: tuple[str, ...] = ("{subject} 宣传片", "{subject} 官方", "{subject} 产品展示",
                                     "{subject} 实拍")
    discovery_en: tuple[str, ...] = ("{en} official", "{en} commercial", "{en} product video")
    #: 品类专属的垃圾标题词 / 展示类标题词 / 官方号名字特征,与通用词表合并
    junk_words: tuple[str, ...] = ()
    showcase_words: tuple[str, ...] = ()
    official_markers: tuple[str, ...] = ()

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
        elif isinstance(default, dict):
            kwargs[key] = {str(k): str(v) for k, v in (value or {}).items()}
        else:
            kwargs[key] = str(value).rstrip("\n")
    return Vocab(**kwargs)


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
    #: 官方素材渠道:品牌键 -> {aliases: [...], sites: [域名...], media: [媒体中心域名...],
    #: tiktok: [官方号...]}。
    #: 只声明**域名**,不写死车型页地址 —— 车型页靠 `site:域名 车型` 站内检索定位,
    #: 官网改版不会让配置失效。新增品牌只改 YAML,不改代码。
    official_sites: dict[str, dict[str, tuple[str, ...]]] = field(default_factory=dict)
    script_style: str = ""
    #: 品类词表(见 Vocab)
    vocab: Vocab = field(default_factory=Vocab)
    #: 事实来源 → 检索词(主体名之后的部分)。没列的来源用 providers.facts 里的通用词
    fact_queries: dict[str, str] = field(default_factory=dict)
    #: 静图来源(providers.official.GALLERIES 里的名字),按顺序合并
    galleries: tuple[str, ...] = ()
    #: 旁白音色,空 = providers.audio.DEFAULT_VOICE_ID
    voice_id: str = ""
    #: 骨架品类为 True 时,平台会明确提示"该品类尚未完整实现"
    skeleton: bool = False
    todo: tuple[str, ...] = ()

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

    def _expand_blocks(self, theme: ThemeSpec, duration: int) -> tuple[str, ...]:
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
        blocks = theme.blocks
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

        out: list[str] = []
        for block, count in zip(blocks, base):
            out.extend([str(block.get("section", ""))] * count)
        return tuple(out[:shots])

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

    blocks: list[dict[str, Any]] = []
    for item in (raw.get("blocks") or []):
        if not isinstance(item, dict):
            raise ProfileError(f"方向 {name}: blocks 的每一项必须是 section/weight 映射")
        blocks.append({
            "section": str(item.get("section", "")),
            "weight": int(item.get("weight", 1)),
            # 必须显式搬运:不搬的话 remainder: false 会在解析阶段被丢掉,
            # 配置看起来写了、实际不生效 —— 铺展结果会静默回到旧行为。
            "remainder": bool(item.get("remainder", True)),
        })

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
        script_style=str(raw.get("script_style", "")),
        skeleton=bool(raw.get("skeleton", False)),
        todo=tuple(str(t) for t in (raw.get("todo") or ())),
    )


def _min_supported_shots(profile: CategoryProfile) -> int:
    """最短受支持时长对应的镜头数 —— 块数不能超过它,否则铺展会挤爆。"""
    durations = profile.supported_durations() or (60,)
    return min(durations) // profile.duration_grid


#: 解说轨的版式名。与 pipeline/explainer.LAYOUTS 保持一致(这里不反向导入流水线)
EXPLAINER_LAYOUTS: tuple[str, ...] = ("broll", "panel", "highlight", "card", "stat")


def _validate(profile: CategoryProfile) -> None:
    if profile.track != "explainer":
        raise ProfileError(f"品类 {profile.name}: track={profile.track!r} 只能是 explainer"
                           "(竖屏链路已删除)")
    for sname, sec in profile.sections.items():
        bad = [x for x in sec.layouts if x not in EXPLAINER_LAYOUTS]
        if bad:
            raise ProfileError(f"品类 {profile.name} 段落 {sname}: 未知版式 {bad}")

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
        if theme.uses_blocks:
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
            },
            "themes": [
                {"name": t.name, "label": t.label, "intent": t.intent}
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
