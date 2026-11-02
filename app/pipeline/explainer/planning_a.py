"""分镜规划(上):逐拍核对、参数面板兜底、broll 连续上限。"""
from __future__ import annotations

import re
from typing import Any, Sequence
from ...categories import CategoryProfile
from ...models import Fact, ScriptSegment, ShotSpec
from .. import cards, knobs, rhythm
from ..grounding import numbers_in

from .consts import LAYOUTS, MAX_BROLL_RUN, SAFE_LAYOUT
from .text import _fact_label, _fact_value, _hint, _panel_from_rows, _stat_overlay, _unit_after, echoes_narration, ground_keywords, ground_stat


def _beat_line(i: int, seg: ScriptSegment | None, profile: CategoryProfile,
               facts: Sequence[Fact] = ()) -> str:
    sec = profile.section(seg.section if seg else "")
    options = list(sec.layouts) or list(LAYOUTS)
    # 开场画面旋钮:第一拍只给点名的那一种版式(段落本来就允许它时)
    wanted = knobs.value("hook_visual") if i == 0 else ""
    if wanted and wanted in options:
        options = [wanted]
    card = f" | 卡片:{cards.KIND_GUIDE[sec.card]}" if sec.card in cards.KIND_GUIDE else ""
    car = f"〔{seg.subject}〕" if seg is not None and seg.subject else ""
    # 本拍可用事实:脚本给这一拍引用过的那几条。
    #
    # 为什么必须逐拍给:事实清单是全片一起给的,模型得自己猜"这条参数属于哪一拍",
    # 猜不准就退一步选 broll —— 实测九成 broll 是这么来的(核对失败退下来的只有两三拍)。
    # 把该拍的事实摆到它面前,它才敢往 panel / stat 上填。
    own = [f for f in facts if f.id in set(seg.fact_refs or [])] if seg is not None else []
    own_txt = ""
    if own:
        own_txt = " | 本拍事实:" + ";".join(f"{f.claim}({f.id})" for f in own[:3])
    return (f"[第 {i + 1} 拍] 段落:{sec.name or '未分类'}{car}({sec.beat or '无'}) | "
            f"可选版式:{'/'.join(options)}{card} | 旁白:{seg.text if seg else ''}{own_txt}")


def facts_for(facts: Sequence[Fact], subject: str, section: str = "") -> list[Fact]:
    """一拍能用来核对叠加数字的事实:讲某台车的拍只认这台车 + 共用事实;共用拍全认。

    竞品对比段之外的拍**不认对比事实** —— 讲别家车的那条数字只该出现在对比卡上,
    混进本车的参数面板就是把别家的参数安到本车头上。
    """
    from ..script_gen import _is_comparison_section

    out = list(facts)
    if subject:
        out = [f for f in out if not f.subject or f.subject == subject]
    if not _is_comparison_section(section):
        out = [f for f in out if not f.compare_with]
    return out


def sync_narration(specs: Sequence[ShotSpec], segments: Sequence[ScriptSegment],
                   facts: Sequence[Fact], profile: Any = None) -> list[ShotSpec]:
    """把改过的脚本同步进分镜。

    门禁 A 的脚本编辑器改的是 script.json,而 TTS 读的是分镜里的 `narration_zh` ——
    不同步的话,在门禁 A 改的稿根本进不了成片(改完看着像生效,成片还是旧词)。
    这里只同步旁白,并**按新旁白重新核对**依赖旁白的那部分叠加数据
    (highlight / hook 的大字):大字是照抄旁白的话,旁白一改就可能变成复读,
    或者引用了已经被删掉的数字。

    版式、叠加卡片这些是按事实核过的,不受旁白改写影响,原样保留。
    """
    if len(specs) != len(segments):
        raise ValueError(f"分镜 {len(specs)} 拍与脚本 {len(segments)} 段对不上,没法按拍同步")
    out: list[ShotSpec] = []
    for spec, seg in zip(specs, segments):
        text = (seg.text or "").strip()
        spec = spec.model_copy(deep=True)
        if spec.narration_zh != text:
            spec.narration_zh = text
        ov = spec.overlay
        if isinstance(ov, dict) and ov.get("kind") in ("highlight", "hook"):
            kept = ground_keywords(ov.get("lines") or [], text, facts,
                                   drop_echo=(ov.get("kind") == "highlight"), profile=profile)
            if kept:
                spec.overlay = {**ov, "lines": kept}
            elif ov.get("kind") == "highlight" or not ov.get("stat"):
                # 大字全废:这一拍退回 broll(没字可弹,硬留个空框更难看)
                spec.layout = SAFE_LAYOUT
                spec.overlay = None
                spec.card = None
                spec.visual_zh = re.sub(r"^\[[^\]]*\]", f"[{SAFE_LAYOUT}]", spec.visual_zh)
            else:
                spec.overlay = {**ov, "lines": []}
        out.append(spec)
    return out


def _ground_beat(i: int, seg: ScriptSegment, item: dict, subject: str, profile: CategoryProfile,
                 facts: Sequence[Fact], card_log: list[dict] | None) -> ShotSpec:
    sec = profile.section(seg.section)
    options = list(sec.layouts) or list(LAYOUTS)
    layout = str(item.get("layout") or options[0]).strip()
    if layout not in options:
        layout = options[0]

    overlay: dict | None = None
    card: dict | None = None
    dropped: list[str] = []
    if layout == "panel":
        grounded, dropped = cards.ground(cards.normalise(item.get("panel"), "spec"), facts)
        if grounded and len(grounded["rows"]) >= 2:
            overlay = {**grounded, "kind": "panel"}
        elif grounded:
            # 只剩一行也上屏:能滚成大数字就滚,不能就一行面板。
            # 退成 broll 等于把这条核对过的参数白白扔了 —— 实测 broll 占比四成就是这么来的。
            stat = _stat_overlay(grounded["rows"][0], grounded.get("source", ""))
            if stat:
                overlay, layout = stat, "stat"
            else:
                overlay = {**grounded, "kind": "panel"}
        else:
            layout = SAFE_LAYOUT
    elif layout == "highlight":
        # drop_echo:大字照抄旁白的直接丢。旁白有字幕,同一句话在画面里出现两次
        # 既没加信息又挡住画面 —— 这两行白扔,行都扔光就退回 broll。
        asked = item.get("keywords")
        asked = [asked] if isinstance(asked, str) else list(asked or [])
        lines = ground_keywords(asked, seg.text, facts, drop_echo=True, profile=profile)
        echoed = [str(x).strip() for x in asked
                  if str(x).strip() and str(x).strip() not in lines
                  and echoes_narration(str(x), seg.text)]
        if echoed:
            dropped = [f"大字与旁白逐字重复,已丢:{'、'.join(echoed)}"]
        if lines:
            overlay = {"kind": "highlight", "lines": lines}
        else:
            layout = SAFE_LAYOUT
    elif layout == "stat":
        overlay, dropped = ground_stat(item.get("stat"), facts, seg.text, profile)
        if overlay is None:
            # 大数字做不出来(取值里没有能滚的数字):同一行数据改成参数面板照样上屏
            raw = item.get("stat")
            row = ({"label": raw.get("label"), "values": [raw.get("value")],
                    "fact_id": raw.get("fact_id")} if isinstance(raw, dict) else None)
            rescued = _panel_from_rows([row] if row else [], facts)
            if rescued:
                overlay, layout = rescued, "panel"
            else:
                layout = SAFE_LAYOUT
    elif layout == "hook":
        # 开场大字也不许照抄旁白:旁白有字幕,第一句话是观众看得最认真的一句,
        # 大字+字幕+口播三遍同一句就是在浪费这三秒。数字另有大数字展示,大字不再重复它
        lines = ground_keywords(item.get("keywords"), seg.text, facts, drop_echo=True, profile=profile)
        stat = None
        if item.get("stat"):
            stat, dropped = ground_stat(item.get("stat"), facts, profile=profile)
        if stat:
            shown = numbers_in(stat["value"])
            lines = [x for x in lines if not (numbers_in(x) & shown)]
        if lines or stat:
            overlay = {"kind": "hook", "lines": lines}
            if stat:
                overlay["stat"] = {"label": stat["label"], "value": stat["value"]}
        else:
            # 关键词和数字都核对不过:没有可弹的字,退回 broll
            layout = SAFE_LAYOUT
    elif layout == "concept":
        raw = item.get("concept")
        scene = str(raw.get("scene") or "").strip() if isinstance(raw, dict) else ""
        raw_rows = raw.get("rows") if isinstance(raw, dict) else None
        if scene not in profile.vocab.concept_scenes:
            dropped = [f"concept 模板 {scene or '(空)'} 不在可选模板里"]
            rescued = _panel_from_rows(raw_rows, facts)
            if rescued:
                overlay, layout = rescued, "panel"
            else:
                layout = SAFE_LAYOUT
        else:
            kind = "compare" if scene == "compare" else "spec"
            grounded, dropped = cards.ground(cards.normalise(raw, kind), facts)
            if grounded:
                overlay = {**grounded, "kind": "concept", "scene": scene,
                           "rows": grounded["rows"][:4]}
            else:
                rescued = _panel_from_rows(raw_rows, facts)
                if rescued:
                    overlay, layout = rescued, "panel"
                else:
                    layout = SAFE_LAYOUT
    elif layout == "card":
        raw_card = item.get("card")
        kind = sec.card or (raw_card.get("kind") if isinstance(raw_card, dict) else "") or "spec"
        card, dropped = cards.ground(cards.normalise(raw_card, kind), facts)
        if card is None:
            # 整屏卡作废(行不够 / 对比卡两栏不齐):同一批行改成左侧面板,信息不丢。
            # 对比卡除外 —— 它的信息天生是两栏的,压成一栏就是误导。
            rows = raw_card.get("rows") if isinstance(raw_card, dict) else None
            rescued = _panel_from_rows(rows, facts) if kind != "compare" else None
            if rescued:
                overlay, layout = rescued, "panel"
            else:
                layout = SAFE_LAYOUT
    if card_log is not None and (dropped or layout != str(item.get("layout") or layout)):
        card_log.append({"idx": i, "section": sec.name, "asked": item.get("layout"),
                         "layout": layout, "dropped": dropped})

    view = _hint(sec.angle, str(item.get("view") or ""), profile.vocab)
    visual = str(item.get("visual") or "").strip()[:30] or (sec.beat or f"{seg.subject or subject} 宣传画面")
    return ShotSpec(
        idx=i, duration=0.0, section=sec.name,
        visual_zh=f"[{layout}] {visual}",
        camera_zh="", ambience_zh="", narration_zh=seg.text,
        angle=sec.angle, on_screen_text=[],
        card=card, layout=layout, overlay=overlay,
        chapter=seg.heading or seg.subject or sec.chapter or sec.name, view=view,
        subject=seg.subject,
    )


def auto_panel(seg: ScriptSegment, facts: Sequence[Fact]) -> dict | None:
    """给还剩 broll 的拍自动配一个参数面板。

    判据只有一个:**事实里的数字在这一拍的旁白里说过**。
    脚本里的数字按合规 lint 必须来自它引用的事实,所以"数字对得上"就等于
    "这一拍真的在讲这条事实" —— 不会把别的参数安到这一拍上(那正是复盘里
    「画面和旁白各走各的」的病根)。一条都对不上就不配面板,老老实实留 broll。
    """
    said = numbers_in(seg.text or "")
    if not said:
        return None
    by_id = {f.id: f for f in facts}
    rows: list[dict] = []
    for ref in seg.fact_refs or []:
        f = by_id.get(ref)
        if f is None or not f.enabled or f.confidence == "low":
            continue
        value = _fact_value(f.claim, said)
        if not value:
            continue
        rows.append({"label": _fact_label(f), "values": [value], "fact_id": f.id})
        if len(rows) >= 4:
            break
    if not rows:
        return None
    return _panel_from_rows(rows, facts)


def rescue_broll(specs: list[ShotSpec], segments: Sequence[ScriptSegment],
                 facts: Sequence[Fact], profile: CategoryProfile) -> list[ShotSpec]:
    """把还剩 broll 的拍救成参数面板。

    为什么要有这一步:实测九成 broll 是分镜自己选的 —— 提示词原来写着
    「全片大约四成是 broll」,而事实清单是全片一起给的,模型猜不准哪条参数
    属于哪一拍,猜不准就退一步选 broll(核对失败退下来的只有两三拍)。
    复盘要求 broll 压到三成以内,光改提示词不保险,这里用脚本引用过的事实兜底:
    这一拍讲到了哪个数字,就把那个数字放上屏。
    """
    for n, spec in enumerate(specs):
        if spec.layout != SAFE_LAYOUT or n >= len(segments):
            continue
        sec = profile.section(spec.section)
        if "panel" not in (sec.layouts or ()):
            continue  # 这一段的视觉语法里没有面板,不硬塞
        overlay = auto_panel(segments[n], facts)
        if overlay:
            spec.layout = "panel"
            spec.overlay = overlay
            spec.visual_zh = re.sub(r"^\[[^\]]*\]", "[panel]", spec.visual_zh)
    return specs


def _headline_value(claim: str) -> str:
    """事实原文里第一个带单位的数字。没有带单位的数字就不上屏(返回空串),
    不再退回"第一个数字":那样会把"Ioniq 5"的 5 当成车身尺寸摆出来。"""
    for m in re.finditer(r"\d+(?:\.\d+)?", claim or ""):
        unit = _unit_after(claim, m.end())
        if unit:
            return m.group(0) + unit
    return ""


def _cap_rows(seg: ScriptSegment, facts: Sequence[Fact], comparison: bool,
              offset: int) -> list[dict]:
    """这一拍引用过的事实里,挑出能上屏的行。

    和 `auto_panel` 的区别:不要求数字在旁白里说过 —— 推导数字(「差了 228 公里」)
    和观点拍(「这钱花得值」)说的话对不上事实原文的取值,但它引用的事实就是它的依据。
    `offset` 让同一批引用连着几拍时各拍轮着展示不同的事实,不重复同一屏。
    """
    by_id = {f.id: f for f in facts}
    pool: list[tuple[Fact, str]] = []
    for ref in seg.fact_refs or []:
        f = by_id.get(ref)
        if f is None or not f.enabled or f.confidence == "low":
            continue
        if f.compare_with and not comparison:
            continue
        value = _headline_value(f.claim)
        if value:
            pool.append((f, value))
    if not pool:
        return []
    k = offset % len(pool)
    pool = pool[k:] + pool[:k]
    return [{"label": _fact_label(f), "values": [v], "fact_id": f.id} for f, v in pool[:2]]


def enforce_broll_cap(specs: list[ShotSpec], segments: Sequence[ScriptSegment],
                      facts: Sequence[Fact], profile: CategoryProfile) -> list[ShotSpec]:
    """broll 拍数超三成上限时,把超出的那部分换成它引用过的事实的面板。

    顺序:先旁白带(推导)数字的拍,再没数字但有引用的拍;每档里先隔拍转换
    (不连着出现一样的画面,也缩短静默区间),不够再放开相邻限制。视觉语法里没有
    面板的段(竞品对比 / 结论 / 开场)只在前面都做完仍超标时才破例,
    且只展示这一拍自己引用的事实。核对不过(`cards.ground`)的行不上屏。
    """
    cap = max(1, int(len(specs) * rhythm.BROLL_RATIO_MAX))
    excess = sum(1 for s in specs if s.layout == SAFE_LAYOUT) - cap
    forced: set[int] = set()

    def candidates(tier_numeric: bool, grammar_ok: bool) -> list[int]:
        out = []
        for n, s in enumerate(specs):
            if s.layout != SAFE_LAYOUT or n >= len(segments) or not segments[n].fact_refs:
                continue
            has_panel = "panel" in (profile.section(s.section).layouts or ())
            if has_panel != grammar_ok:
                continue
            if bool(numbers_in(segments[n].text or "")) != tier_numeric:
                continue
            out.append(n)
        return out

    def convert(n: int) -> bool:
        seg, spec = segments[n], specs[n]
        from ..script_gen import _is_comparison_section
        comparison = _is_comparison_section(spec.section)
        offset = 0
        m = n - 1
        while m >= 0 and m in forced and specs[m].section == spec.section:
            offset += 1
            m -= 1
        # 前一拍已经是同一批引用的面板:轮换行;和前后任一拍的画面指纹一样就放弃
        rows = _cap_rows(seg, facts, comparison, offset)
        overlay = _panel_from_rows(rows, facts) if rows else None
        if not overlay:
            return False
        trial = spec.model_copy(update={"layout": "panel", "overlay": overlay, "card": None})
        sig = rhythm._overlay_signature(trial)
        if any(0 <= m < len(specs) and rhythm._overlay_signature(specs[m]) == sig
               for m in (n - 1, n + 1)):
            return False
        spec.layout, spec.overlay, spec.card = "panel", overlay, None
        spec.visual_zh = re.sub(r"^\[[^\]]*\]", "[panel]", spec.visual_zh)
        forced.add(n)
        return True

    def run_len(n: int) -> int:
        """第 n 拍所在的连续 broll 串有多长。"""
        lo = hi = n
        while lo > 0 and specs[lo - 1].layout == SAFE_LAYOUT:
            lo -= 1
        while hi + 1 < len(specs) and specs[hi + 1].layout == SAFE_LAYOUT:
            hi += 1
        return hi - lo + 1

    def ordered(tier_numeric: bool, grammar_ok: bool) -> list[int]:
        # 长 broll 串里的拍优先:整段静默比零星几拍更伤节奏
        return sorted(candidates(tier_numeric, grammar_ok), key=lambda n: (-run_len(n), n))

    for grammar_ok in (True, False):
        for tier_numeric in (True, False):
            for spaced in (True, False):
                if excess <= 0:
                    break
                for n in ordered(tier_numeric, grammar_ok):
                    if excess <= 0:
                        break
                    if specs[n].layout != SAFE_LAYOUT:
                        continue
                    if spaced and ((n - 1) in forced or (n + 1) in forced):
                        continue
                    if convert(n):
                        excess -= 1
    # 上限够了也不放过过长的 broll 串:连着 4 拍以上没有任何叠加层,
    # 就是整段静默(复盘:每 5–6 秒必须有一次画面变化)—— 串中间的拍破例转换
    for _ in range(len(specs)):
        longest = [n for n in range(len(specs))
                   if specs[n].layout == SAFE_LAYOUT and run_len(n) >= MAX_BROLL_RUN]
        if not longest:
            break
        longest.sort(key=lambda n: (-run_len(n), n))
        for n in longest:
            lo = n
            while lo > 0 and specs[lo - 1].layout == SAFE_LAYOUT:
                lo -= 1
            if n - lo >= 1 and n < len(segments) and segments[n].fact_refs and convert(n):
                break
        else:
            break
    return specs
