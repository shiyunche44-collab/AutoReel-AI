"""分镜规划(下):关键词拍、节奏、broll 样式、兜底分镜与校验。"""
from __future__ import annotations

import re
from typing import Sequence
from ...categories import CategoryProfile
from ...models import Fact, ScriptSegment, ShotSpec
from .. import looks

from .consts import BROLL_STYLES, KEYWORD_CHARS, LAYOUTS, MAX_CARD_RUN, MAX_CONCEPT, SAFE_LAYOUT
from .text import _as_panel_overlay, _hint, ground_keywords
from .planning_a import facts_for


#: 估算一拍多长(合成旁白之前还不知道):字数 ÷ 这个语速。偏慢一点,宁可多配
QUIET_CPS = 5.0
#: 连着这么多秒都没有叠加层,下一拍就该上一个关键词大字(复盘:5–6 秒必须有一次变化)
QUIET_SECONDS = 5.5
#: 短于这个的拍放不下大字(一闪而过读不完)
KEYWORD_BEAT_MIN = 2.0

KEYWORD_SYSTEM = """你在给信息型解说视频里「没有数据可上屏」的观点句配大字关键词,让画面有变化。
每句配 1–2 行,每行 2–{kw} 字,是这句话的核心判断或要点。

要求:
1. **不要把原句抄一遍**:屏幕下方已经有字幕了。用更短更狠的说法概括,
   例:旁白「代价是能量密度低」→「代价:能量密度」;「买它等于买一个会持续更新的预期」→「买的是预期」。
2. 不得出现旁白里没有的数字、型号、英文;不写「最强」「第一」「闭眼入」这类夸张词。
3. 只概括旁白已经说的意思,不加新的判断,不替车做新结论。

只输出一个 JSON 对象:{{"items": [{{"idx": 拍号, "lines": ["…", "…"]}}]}},idx 用给定的拍号。

必需键: items"""


def add_keyword_beats(specs: list[ShotSpec], segments: Sequence[ScriptSegment],
                      facts: Sequence[Fact], profile: CategoryProfile, llm,
                      *, portrait: bool = False) -> list[ShotSpec]:
    """没数据可上屏的观点拍连成一串时,隔几拍配一个关键词大字,压低"整段没有画面变化"。

    为什么单独一步:数据拍(panel / stat)已经由 `enforce_broll_cap` 转完了,剩下的 broll
    是观点、取舍、过渡 —— 没有数字可摆,只能靠大字当信息层的变化。大字由 LLM 概括旁白,
    再走 `ground_keywords(drop_echo=True)`:照抄旁白的、编了数字型号的都不上屏。
    估算时长走字数,合成旁白之后才有真实时长,这里只管把静默串截短。
    """
    if llm is None or not specs:
        return specs
    cps = 4.7 if portrait else QUIET_CPS
    quiet = 0.0
    picks: list[int] = []
    for n, spec in enumerate(specs):
        text = segments[n].text if n < len(segments) else ""
        dur = max(1.0, len(text or "") / cps)
        if spec.layout != SAFE_LAYOUT:  # 面板 / 大字 / 大数字 / 卡片 / 动画 都是信息层的变化
            quiet = 0.0
            continue
        quiet += dur
        if quiet < QUIET_SECONDS or n == 0 or dur < KEYWORD_BEAT_MIN:
            continue
        allowed = "highlight" in (profile.section(spec.section).layouts or ()) or quiet > 9.0
        if allowed:
            picks.append(n)
            quiet = 0.0
    if not picks:
        return specs
    beats = "\n".join(f"[拍号 {specs[n].idx}] 段落:{specs[n].section} | 旁白:{segments[n].text}"
                      for n in picks)
    try:
        system = KEYWORD_SYSTEM.format(kw=KEYWORD_CHARS // 2)
        data = llm.structured(system, beats, required_keys=["items"], temperature=0.4)
        items = data.get("items") or []
    except Exception:  # noqa: BLE001 - 配不出来就保持原样,门禁会提示静默过长
        return specs
    lines_by_idx: dict[int, list] = {}
    for it in items:
        if isinstance(it, dict):
            try:
                lines_by_idx[int(it.get("idx"))] = it.get("lines") or []
            except (TypeError, ValueError):
                continue
    for n in picks:
        spec, seg = specs[n], segments[n]
        lines = ground_keywords(lines_by_idx.get(spec.idx), seg.text,
                                facts_for(facts, seg.subject, profile.section(seg.section).name),
                                drop_echo=True, profile=profile)
        if not lines:
            continue
        spec.layout, spec.card = "highlight", None
        spec.overlay = {"kind": "highlight", "lines": lines}
        spec.visual_zh = re.sub(r"^\[[^\]]*\]", "[highlight]", spec.visual_zh)
    return specs


def _demote(spec: ShotSpec, profile: CategoryProfile | None = None) -> None:
    """把这一拍降级:能用已核过的数据改成面板就改,否则退回 broll。

    降级不是直接扔回 broll —— 这一拍的数据都已经核对过了,改成左侧面板照样上屏
    (前提是这一段的视觉语法里允许面板)。扔掉就是白扔,broll 占比就是这么滚上去的。
    """
    if profile is not None:
        sec = profile.section(spec.section)
        if "panel" in (sec.layouts or ()):
            ov = _as_panel_overlay(spec)
            if ov:
                spec.layout = "panel"
                spec.overlay = ov
                spec.card = None
                spec.visual_zh = re.sub(r"^\[[^\]]*\]", "[panel]", spec.visual_zh)
                return
    spec.layout, spec.overlay, spec.card = SAFE_LAYOUT, None, None
    spec.visual_zh = re.sub(r"^\[[^\]]*\]", f"[{SAFE_LAYOUT}]", spec.visual_zh)


def enforce_rhythm(specs: list[ShotSpec], profile: CategoryProfile | None = None
                   ) -> list[ShotSpec]:
    """连续超过 MAX_CARD_RUN 拍整屏卡片时,把多出来的那一拍降级;
    连着两拍大数字也把后一拍降级(一个接一个滚数字像在念参数表);
    概念动画同理不连着放,全片超过 MAX_CONCEPT 拍的也降级。

    降级优先改成参数面板(见 `_demote`),实在没有数据才退回 broll。
    """
    run = 0
    prev = ""
    concepts = 0
    for pos, s in enumerate(specs):
        if s.layout == "hook" and pos > 0:
            _demote(s, profile)
        if s.layout in ("stat", "concept") and prev == s.layout:
            _demote(s, profile)
        if s.layout == "concept":
            concepts += 1
            if concepts > MAX_CONCEPT:
                _demote(s, profile)
        prev = s.layout
        if s.layout == "card":
            run += 1
            if run > MAX_CARD_RUN:
                _demote(s, profile)
                run = 0
        else:
            run = 0
    return specs


def assign_broll_styles(specs: list[ShotSpec]) -> list[ShotSpec]:
    """给 broll 拍轮换样式(确定性,不经 LLM):全屏 → 左上角角标 → 分屏 → …

    - 分屏不连着两拍(相邻 broll 都分屏就成了监控墙);
    - 每章第一拍开头已经有章节标牌占着左上角,不再放角标;
    - 别的版式的 overlay 不动;broll 的样式记在 overlay = {"kind": "broll", "style": ...}。
    - 轮换顺序跟着当前视觉包(暗场电影多全屏、杂志编排多分屏),没声明就用 BROLL_STYLES。
    """
    cycle = looks.current().broll_cycle or BROLL_STYLES
    k = 0
    prev_split = False
    for n, s in enumerate(specs):
        if s.layout != "broll":
            prev_split = False
            continue
        style = cycle[k % len(cycle)]
        k += 1
        chapter = s.chapter or s.section
        first = n == 0 or (specs[n - 1].chapter or specs[n - 1].section) != chapter
        if (style == "split" and prev_split) or (style == "lower_third" and first):
            style = "plain"
        s.overlay = None if style == "plain" else {"kind": "broll", "style": style}
        prev_split = style == "split"
    return specs


def broll_style(spec: ShotSpec) -> str:
    if spec.layout != "broll" or not isinstance(spec.overlay, dict):
        return "plain"
    style = spec.overlay.get("style")
    return style if style in BROLL_STYLES else "plain"


def fallback_beats(*, subject: str, profile: CategoryProfile,
                   segments: Sequence[ScriptSegment],
                   subjects: Sequence[str] | None = None) -> list[ShotSpec]:
    """没有 LLM 时:全部 broll。结构、时间轴、合成链路照样能验。"""
    out = []
    for i, seg in enumerate(segments):
        sec = profile.section(seg.section)
        out.append(ShotSpec(idx=i, duration=0.0, section=sec.name,
                            visual_zh=f"[{SAFE_LAYOUT}] {sec.beat or seg.subject or subject}",
                            narration_zh=seg.text, angle=sec.angle, layout=SAFE_LAYOUT,
                            chapter=seg.heading or seg.subject or sec.chapter or sec.name,
                            view=_hint(sec.angle, "", profile.vocab), subject=seg.subject))
    return assign_broll_styles(out)


def beat_subject(spec: ShotSpec, subjects: Sequence[str], fallback: str) -> str:
    """这一拍画面上该是哪台车(背景素材、车名角标、卡片底图都按它取)。

    - 拍上标了车:就是它;
    - 横评的开场 / 结论这类共用拍:按拍号在车单里轮换 —— 背景永远是车单上的车,
      不会出现选题名去查素材池查个空;
    - 单车项目:项目主体。"""
    if spec.subject:
        return spec.subject
    if subjects:
        # 共用拍(对决的每一章、横评的开场结论)旁白只点了一台车的名字,就配那台车:
        # 「Model Y 的后排更宽」配着 YU7 的画面,观众会以为在说 YU7
        named = [s for s in subjects if _names_car(spec.narration_zh, s)]
        if len(named) == 1:
            return named[0]
        return subjects[spec.idx % len(subjects)]
    return fallback


def _names_car(text: str, name: str) -> bool:
    """旁白里提没提到这台车:全名,或车名里最长的字母数字串(「YU7」「Model Y」→「Model」不算)。"""
    text = text or ""
    if name and name in text:
        return True
    tokens = [t for t in re.findall(r"[A-Za-z0-9]+(?: [A-Za-z0-9]+)*", name or "") if len(t) >= 2]
    return any(t in text for t in tokens if any(c.isdigit() for c in t) or " " in t or len(t) >= 4)


def validate_beats(specs: Sequence[ShotSpec]) -> list[str]:
    errors: list[str] = []
    if not specs:
        errors.append("没有任何节拍")
    for s in specs:
        if s.layout not in LAYOUTS:
            errors.append(f"第 {s.idx} 拍版式非法:{s.layout!r}")
        if not s.narration_zh.strip():
            errors.append(f"第 {s.idx} 拍没有旁白(时长由旁白决定,空拍无法定长)")
        if s.layout == "card" and not s.card:
            errors.append(f"第 {s.idx} 拍是 card 但没有卡片数据")
        if s.layout in ("panel", "highlight", "stat", "concept", "hook") and not s.overlay:
            errors.append(f"第 {s.idx} 拍是 {s.layout} 但没有叠加数据")
    return errors
