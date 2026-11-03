"""`CategoryProfile`:一个品类的完整声明与它的查询方法。"""
from __future__ import annotations

import random
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from .spec import (  # noqa: F401
    _PRINCIPLE_DIGIT,
    CARD_KINDS,
    COMMON_VIEWS,
    PORTRAIT_SHOTS_PER_SECTION,
    ConsistencySpec,
    Principle,
    ProfileError,
    SectionSpec,
    Slot,
    ThemeSpec,
    TrendSpec,
    Vocab,
    _build_principles,
    _build_trends,
    _build_vocab,
    _takes_remainder,
)


@dataclass(frozen=True)
class CategoryProfile:
    name: str
    label: str
    #: 新建项目时的**默认**画幅。真正的画幅是项目属性(见 pipeline/aspect.py)
    aspect_ratio: str = "16:9"
    duration_grid: int = 6
    #: 画面链路。目前只有 explainer = 横屏信息解说(实拍只当背景,信息靠叠加层,
    #: 时长跟着旁白走);竖屏逐镜头生成链路已删除
    track: str = "explainer"
    #: 可选成片时长(秒)。空 = 全局 SUPPORTED_DURATIONS
    durations: tuple[int, ...] = ()
    #: 竖屏项目的可选成片时长(秒)。空 = 全局 SUPPORTED_DURATIONS_PORTRAIT
    durations_portrait: tuple[int, ...] = ()
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
    #: 发布标题里额外要躲开的套话 / 搜索词式说法(与 packaging.TITLE_AVOID 合并);标题里出现就重写
    title_avoid: tuple[str, ...] = ()
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
    #: 配音音色候选(两个以上才成为复盘可学的旋钮,见 pipeline/knobs);空 = 只用 voice_id
    voices: tuple[str, ...] = ()
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

    def supported_durations(self, aspect: str | None = None) -> tuple[int, ...]:
        """这个画幅可选的成片时长。不给画幅 = 横屏(老调用方的语义)。"""
        from .. import config

        if aspect == config.PORTRAIT:
            return self.durations_portrait or config.SUPPORTED_DURATIONS_PORTRAIT
        return self.durations or config.SUPPORTED_DURATIONS

    def theme_durations(self, theme: ThemeSpec, aspect: str | None = None) -> tuple[int, ...]:
        """这个片型在这个画幅下可选的时长:画幅档位里不超过片型 `max_duration` 的那些。"""
        durations = self.supported_durations(aspect)
        if not theme.max_duration:
            return durations
        return tuple(d for d in durations if d <= theme.max_duration) or durations[:1]

    def all_durations(self) -> tuple[int, ...]:
        """横竖两种画幅的时长并集 —— 配置自检要对每一档都铺展得开。"""
        from .. import config

        return tuple(sorted({*self.supported_durations(config.LANDSCAPE),
                             *self.supported_durations(config.PORTRAIT)}))

    def shots_for(self, duration: int) -> int:
        """该时长铺几个镜头(解说轨叫节拍)。按本 profile 的栅格四舍五入。

        成片时长本来就跟着旁白走,目标时长只是估算;竖屏的 60/90/180 秒
        不一定是栅格的整数倍(栅格 8 秒时 60s → 8 拍),所以取最接近的拍数,
        不再要求整除。横屏档位都是整数倍,结果与原来一致。"""
        if duration <= 0:
            raise ProfileError(f"品类 {self.name}: 时长必须为正数,收到 {duration}s")
        return max(1, int(duration / self.duration_grid + 0.5))

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
    def plan_for(self, duration: int, name: str | None = None,
                 portrait: bool = False,
                 variant: Mapping[str, Any] | None = None) -> tuple[str, ...]:
        """返回该方向在该时长下的段落序列。

        两条路径:
        - 方向声明了 `blocks`:按权重**确定性铺展**到 duration/6 个镜头。
        - 未声明(老 profile):走手写 `section_plan`,缺失时按最近档裁剪补齐。
        """
        theme = self.theme_spec(name)
        if theme.roundup:
            return tuple(s.section for s in self.slots_for(duration, name, portrait=portrait,
                                                            variant=variant))
        if theme.uses_blocks:
            return self._expand_blocks(theme, duration, portrait, variant)
        plan = theme.plan or self.section_plan
        if duration in plan:
            return plan[duration]
        if not plan:
            raise ProfileError(f"品类 {self.name} 未声明任何 section_plan 或 blocks")
        # 退化路径:用最接近的方案裁剪/补齐
        closest = min(plan, key=lambda d: abs(d - duration))
        target_shots = self.shots_for(duration)
        base = list(plan[closest])
        while len(base) < target_shots:
            base.insert(max(1, len(base) - 1), base[len(base) // 2])
        return tuple(base[:target_shots])

    def slots_for(self, duration: int, name: str | None = None,
                  subjects: Sequence[str] | None = None,
                  portrait: bool = False,
                  variant: Mapping[str, Any] | None = None) -> tuple[Slot, ...]:
        """段落序列 + 每段讲哪台车。

        - 单车方向:等价于 `plan_for`,subject 全为空。
        - 横评方向:per_subject 块按车单展开成 N 份(每份带车名),和其它块一起
          走同一套最大余数铺展 —— 每台车的段落连在一起,车与车按车单顺序排。
          不给车单时用占位车名(按最少台数),只用于估节拍数和配置自检。
        - `portrait`:竖屏按 `section_capacity` 精简段落,见那里的说明。
        - `variant`:项目变体(开头写法、中段顺序),见 `variant_blocks`。
        """
        theme = self.theme_spec(name)
        if not theme.roundup:
            return tuple(Slot(s) for s in self.plan_for(duration, name, portrait, variant))
        names = [str(x).strip() for x in (subjects or theme.placeholder_subjects()) if str(x).strip()]
        theme = replace(theme, blocks=tuple(self.variant_blocks(theme, variant)))
        flat = self.roundup_blocks(theme, names, shots=self.section_capacity(duration, portrait))
        counts = self._allocate(flat, duration)
        out: list[Slot] = []
        for block, count in zip(flat, counts):
            out.extend([Slot(str(block.get("section", "")), str(block.get("subject", "")))] * count)
        return tuple(out[:self.shots_for(duration)])

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

    def max_subjects(self, theme: ThemeSpec, duration: int) -> int:
        """横评在这个时长下最多放得下几台车(精简到底后)。放不下最少台数时返回 0。"""
        lo, hi = theme.subject_count
        shots = self.shots_for(duration)
        for n in range(hi, lo - 1, -1):
            if self.roundup_min_blocks(theme, n) <= shots:
                return n
        return 0

    @staticmethod
    def roundup_min_blocks(theme: ThemeSpec, count: int) -> int:
        """`count` 台车精简到底(只留 drop=0 的段落)时的段落块数。"""
        core = [b for b in theme.per_subject if not b.get("drop")]
        return len(theme.blocks) + len(core) * count

    @staticmethod
    def shed_blocks(blocks: Sequence[dict[str, Any]], shots: int) -> list[dict[str, Any]]:
        """段落块多于拍数时,按 `drop` 从小到大整档砍掉(0 = 必留)。

        竖屏 60 秒只有 8 拍左右,单车方向的 12 个块铺不开 —— 不精简的话
        铺展会把**末尾**的块(结论)截掉。和横评每车段落的精简同一套规则。
        块数本来就放得下时原样返回,横屏档位的铺展结果不变。"""
        out = list(blocks)
        for level in sorted({b["drop"] for b in out if b.get("drop")}):
            if len(out) <= shots:
                break
            out = [b for b in out if b.get("drop") != level]
        return out

    @staticmethod
    def core_blocks(blocks: Sequence[dict[str, Any]]) -> int:
        """精简到底(只留 drop=0)时的块数。"""
        return sum(1 for b in blocks if not b.get("drop"))

    def section_capacity(self, duration: int, portrait: bool = False) -> int:
        """这个时长最多放几个段落。

        横屏 = 拍数(一个段落至少一拍)。竖屏要求每段至少约两拍:实测 90 秒竖屏
        铺 10 段、每段一拍,LLM 每段照样写 60–100 字,全片写到 886 字(目标 522),
        成片会比选的时长长七成。段落少了,每段才有篇幅讲透、总字数才压得住。"""
        shots = self.shots_for(duration)
        return max(1, shots // PORTRAIT_SHOTS_PER_SECTION) if portrait else shots

    @staticmethod
    def variant_blocks(theme: ThemeSpec,
                       variant: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
        """按项目变体调整段落块:

        - 去掉 `only_opening` 不含本项目开头写法的块(问题开场不需要「亮出出处」);
        - 相邻的 `movable` 块按 `order_seed` 重排(外观 / 内饰 / 三电 / 智能谁先讲都行),
          不相邻的、没标 movable 的块位置不动。

        不给 variant(老项目)= 片型默认开头 + 声明顺序,结果与以前相同。"""
        variant = variant or {}
        opening = str(variant.get("opening") or (theme.openings[0] if theme.openings else "conflict"))
        out = [b for b in theme.blocks
               if not b.get("only_opening") or opening in b["only_opening"]]
        seed = variant.get("order_seed")
        if not seed:
            return out
        rng = random.Random(int(seed))
        i = 0
        while i < len(out):
            j = i
            while j < len(out) and out[j].get("movable"):
                j += 1
            if j - i > 1:
                run = out[i:j]
                rng.shuffle(run)
                out[i:j] = run
            i = max(j, i + 1)
        return out

    def _expand_blocks(self, theme: ThemeSpec, duration: int,
                       portrait: bool = False,
                       variant: Mapping[str, Any] | None = None) -> tuple[str, ...]:
        capacity = self.section_capacity(duration, portrait)
        if theme.shots_per_section:
            capacity = min(capacity, max(1, self.shots_for(duration) // theme.shots_per_section))
        blocks = self.shed_blocks(self.variant_blocks(theme, variant), capacity)
        counts = self._allocate(blocks, duration)
        out: list[str] = []
        for block, count in zip(blocks, counts):
            out.extend([str(block.get("section", ""))] * count)
        return tuple(out[:self.shots_for(duration)])

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
        shots = self.shots_for(duration)
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
