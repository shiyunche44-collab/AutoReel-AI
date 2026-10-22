"""视觉包(look):一个项目从头到尾的画面包装。

以前 HUD 面板、数据卡、章节条、字幕、封面、概念动画模版、BGM 全是模块常量 —— 每条片子
长得一模一样,而且数据卡是蓝色、HUD 是橙色,两套视觉语言在同一条片子里打架。

现在一个项目选一套 look(建项目时自动挑,避开最近几条用过的;门禁 B 之前可改),
画面上所有包装都从它取:

==========  ===================================================================
hud         面板纸面 / 墨色 / 强调色、网格与角标、数字字体
cards       数据卡底色与文字色
subs        字幕样式:半透明底条 / 描边无底 / 色块
concept     概念动画模版(没单独选过模版时用它)
bgm         自动收曲的 Mixkit 风格
broll       broll 拍样式的轮换顺序
==========  ===================================================================

`hud` 是基线:它的取值就是 look 出现之前的常量,老项目、没选 look 的项目画出来逐像素不变。

渲染是按项目串行的(render_pending 一拍一拍画),所以用 contextvar 记"当前 look",
绘制函数照旧读 `hud.ACCENT` 这类名字,不必把 look 一层层传下去。
"""
from __future__ import annotations

import contextlib
import contextvars
import hashlib
import random
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator, Mapping

RGB = tuple[int, int, int]
RGBA = tuple[int, int, int, int]


def mix(base: RGB, ink: RGB, t: float) -> RGB:
    """两个颜色按比例混成不透明实色(PIL 直接写像素,不做 alpha 混合)。"""
    return tuple(int(round(base[i] + (ink[i] - base[i]) * t)) for i in range(3))  # type: ignore[return-value]


@dataclass(frozen=True)
class Look:
    id: str
    label: str
    blurb: str
    # ---- HUD 面板 ----
    paper: RGB
    ink: RGB
    key: RGB
    muted: RGB
    accent: RGB
    paper_alpha: int = 244
    #: 纸面上的发丝网格 / 取景框角标
    grid: bool = True
    brackets: bool = True
    #: 数字字体候选(常规, 粗体);空 = HUD 默认的等宽字体
    number_fonts: tuple[tuple[str, ...], tuple[str, ...]] = ((), ())
    # ---- 数据卡 ----
    card_bg_top: RGB = (15, 17, 21)
    card_bg_bottom: RGB = (28, 34, 48)
    card_panel: RGBA = (255, 255, 255, 22)
    card_text: RGB = (255, 255, 255)
    card_muted: RGB = (176, 184, 196)
    card_accent: RGB = (77, 163, 255)
    card_accent_b: RGB = (255, 181, 71)
    card_rule: RGBA = (255, 255, 255, 40)
    # ---- 叠层(章节条 / 标牌)----
    bar_bg: RGBA = (0, 0, 0, 175)
    bar_on_text: RGBA = (20, 20, 20, 255)
    #: 章节标牌、角标名牌的深色底
    label_bg: RGB = (10, 16, 30)
    # ---- 字幕 ----
    #: box = 半透明黑底条;outline = 白字描边无底;band = 色块底(用 sub_box 的颜色)
    subs: str = "box"
    #: ASS 颜色(&HAABBGGRR)
    sub_text: str = "&H00FFFFFF"
    sub_box: str = "&H80000000"
    # ---- 其它 ----
    concept: str = "studio"
    bgm_genres: tuple[str, ...] = ()
    #: broll 拍的样式轮换顺序;空 = explainer 默认
    broll_cycle: tuple[str, ...] = ()
    #: 封面强调色(深底 / 浅底各一)
    cover_accent: RGB | None = None
    extra: Mapping[str, Any] = field(default_factory=dict)

    # 纸上的淡色,都预先混成实色
    @property
    def grid_color(self) -> RGB:
        return mix(self.paper, self.ink, 0.07)

    @property
    def rule_color(self) -> RGB:
        return mix(self.paper, self.ink, 0.26)

    @property
    def edge_color(self) -> RGB:
        return mix(self.paper, self.ink, 0.55)

    @property
    def accent_dark(self) -> RGB:
        """纸上小字用的强调色:浅纸上压深一档,深纸上提亮一档,保证对比度。"""
        light_paper = sum(self.paper) > 380
        return mix(self.accent, self.ink, 0.24) if light_paper else mix(self.accent, (255, 255, 255), 0.18)

    def hud_color(self, name: str) -> Any:
        """hud 模块里那几个颜色名 → 这套 look 的取值。"""
        return {
            "PAPER": self.paper, "INK": self.ink, "KEY": self.key, "MUTED": self.muted,
            "ACCENT": self.accent, "PAPER_ALPHA": self.paper_alpha,
            "GRID_COLOR": self.grid_color, "RULE_COLOR": self.rule_color,
            "EDGE_COLOR": self.edge_color, "ACCENT_DARK": self.accent_dark,
        }[name]


DEFAULT = "hud"

LOOKS: dict[str, Look] = {lk.id: lk for lk in (
    # 基线:就是 look 出现之前的样子(取值与旧常量逐个相同)
    Look(
        "hud", "工程 HUD", "浅色规格纸 + 发丝网格 + 橙色角标,等宽数字",
        paper=(236, 238, 233), ink=(18, 22, 26), key=(61, 68, 76), muted=(93, 101, 110),
        accent=(255, 90, 31),
        concept="studio",
    ),
    Look(
        "cinema", "暗场电影", "暗底金字、衬线数字、描边字幕,不画网格和角标,慢节奏",
        paper=(17, 18, 21), ink=(238, 233, 224), key=(200, 192, 180), muted=(150, 145, 138),
        accent=(222, 176, 94), paper_alpha=228, grid=False, brackets=False,
        number_fonts=(("georgia.ttf",), ("georgiab.ttf",)),
        card_bg_top=(8, 8, 10), card_bg_bottom=(32, 27, 22), card_panel=(255, 240, 220, 16),
        card_text=(240, 234, 224), card_muted=(170, 160, 146), card_accent=(222, 176, 94),
        card_accent_b=(150, 176, 200), card_rule=(255, 240, 220, 36),
        bar_bg=(0, 0, 0, 150), bar_on_text=(18, 16, 12, 255), label_bg=(0, 0, 0),
        subs="outline", sub_text="&H00E0EAF0",
        concept="cinema", bgm_genres=("cinematic", "ambient"),
        broll_cycle=("plain", "plain", "lower_third"),
        cover_accent=(222, 176, 94),
    ),
    Look(
        "blueprint", "工程蓝图", "深蓝图纸、青色线框、等宽数字,硬切",
        paper=(14, 38, 64), ink=(226, 240, 255), key=(168, 198, 228), muted=(126, 158, 190),
        accent=(64, 208, 255), paper_alpha=236,
        card_bg_top=(8, 24, 44), card_bg_bottom=(14, 44, 74), card_panel=(120, 200, 255, 20),
        card_text=(230, 242, 255), card_muted=(150, 182, 214), card_accent=(64, 208, 255),
        card_accent_b=(255, 200, 80), card_rule=(120, 200, 255, 50),
        bar_bg=(6, 22, 40, 200), bar_on_text=(6, 22, 40, 255), label_bg=(6, 22, 40),
        subs="band", sub_box="&H50402008",
        concept="blueprint", bgm_genres=("tech",),
        broll_cycle=("split", "plain", "lower_third"),
        cover_accent=(64, 208, 255),
    ),
    Look(
        "editorial", "杂志编排", "米白纸面、红色强调、窄体数字,色块字幕,多用分栏",
        paper=(250, 246, 238), ink=(24, 22, 20), key=(70, 64, 58), muted=(118, 110, 102),
        accent=(214, 40, 58), grid=False, brackets=False,
        number_fonts=(("bahnschrift.ttf",), ("bahnschrift.ttf",)),
        card_bg_top=(246, 242, 234), card_bg_bottom=(232, 226, 214), card_panel=(0, 0, 0, 12),
        card_text=(24, 22, 20), card_muted=(108, 100, 92), card_accent=(214, 40, 58),
        card_accent_b=(30, 90, 150), card_rule=(0, 0, 0, 40),
        bar_bg=(24, 22, 20, 200), bar_on_text=(255, 255, 255, 255), label_bg=(24, 22, 20),
        subs="band", sub_text="&H00141618", sub_box="&H18EEF6FA",
        concept="editorial", bgm_genres=("corporate-music", "documentary"),
        broll_cycle=("split", "lower_third", "split", "plain"),
        cover_accent=(214, 40, 58),
    ),
)}

_ACTIVE: contextvars.ContextVar[str] = contextvars.ContextVar("look", default=DEFAULT)


def get(look_id: str | None) -> Look:
    return LOOKS.get(str(look_id or ""), LOOKS[DEFAULT])


def current() -> Look:
    return get(_ACTIVE.get())


@contextlib.contextmanager
def use(look: str | Look | None) -> Iterator[Look]:
    """在这段代码里把 look 设成当前 look(画面板、数据卡、章节条都跟着它走)。"""
    lk = look if isinstance(look, Look) else get(look)
    token = _ACTIVE.set(lk.id)
    try:
        yield lk
    finally:
        _ACTIVE.reset(token)


def of_variant(variant: Mapping[str, Any] | None) -> Look:
    return get((variant or {}).get("look"))


def of_project(project_id: str) -> Look:
    """项目的视觉包(存在 projects.variant 里);老项目 = 基线 hud。"""
    import json

    from .. import db

    raw = (db.get_project(project_id) or {}).get("variant") or "{}"
    try:
        variant = json.loads(raw) if isinstance(raw, str) else dict(raw)
    except (TypeError, ValueError):
        variant = {}
    return of_variant(variant if isinstance(variant, dict) else {})


def pick(project_id: str, recent: Iterable[Mapping[str, Any]] = (), chosen: str | None = None) -> str:
    """给新项目挑 look:用户指定的优先,否则避开最近几条用过的(种子来自项目 ID,可复现)。"""
    if chosen:
        if chosen not in LOOKS:
            raise ValueError(f"视觉包可选:{'、'.join(lk.label for lk in LOOKS.values())}")
        return chosen
    used = [str((v or {}).get("look") or "") for v in recent]
    fresh = [k for k in LOOKS if k not in used]
    digest = hashlib.sha256(f"look:{project_id}".encode()).hexdigest()
    rng = random.Random(int(digest[:16], 16))
    if fresh:
        return rng.choice(fresh)
    return max(LOOKS, key=lambda k: used.index(k))


def options() -> list[dict[str, str]]:
    return [{"id": lk.id, "label": lk.label, "blurb": lk.blurb, "concept": lk.concept}
            for lk in LOOKS.values()]
