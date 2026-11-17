"""机构画面(mech 版式):机构动画 / 剖视静帧 + 标注。

和概念动画(concept)的区别:concept 画的是**参数**(每一行都要引用事实),mech 画的是
**机理** —— 活塞怎么动、能量往哪流,讲的是不带数字的道理。所以它的规则反过来:

- 屏上的标注(marks)必须是这一拍旁白里**原样出现过**的词,而且不许带数字;
- 想放数字就走 rows,和别的版式一样逐行核对事实,且只留这一拍旁白说过的;
- 画哪个部件只能从 `vocab.mech_parts` 里选,模版只画示意,不画具体型号的结构。

只有段落 `layouts` 里写了 mech 的片型(原理拆解)会进这条路,别的片型的提示词一字不变。
"""
from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from ...categories import CategoryProfile, Vocab
from ...models import Fact, ScriptSegment, ShotSpec
from .. import cards
from ..grounding import numbers_in

#: 一拍最多几条标注、每条最多几个字
MAX_MARKS = 3
MARK_CHARS = 10
#: 机构画面在「允许 mech 的拍」里至少占多少(原理拆解靠它撑画面,不够就把 broll 补成 mech)
MIN_SHARE = 0.6

#: 同一张剖视图连用几拍就换一个视角(`mechanism_art.VIEWS`):镜头推拉盖不住"还是那张图"
VIEW_EVERY = 2

_DIGIT = re.compile(r"\d")
#: 这一拍在讲钱(价格、版本差价):不拿「上一拍的部件」接着配机构画面 —— 样片里讲指导价的拍
#: 配着涡轮动画,画面和话对不上
_MONEY = re.compile(r"\d(?:\.\d+)?\s*万|指导价|心动价|售价|落地价|优惠|差价|预算")
_SPACE = re.compile(r"\s+")


def enabled(profile: CategoryProfile, segments: Sequence[ScriptSegment]) -> bool:
    """这一片有没有允许 mech 的拍(决定提示词里出不出现部件清单)。"""
    if not profile.vocab.mech_parts:
        return False
    return any("mech" in (profile.section(s.section).layouts or ()) for s in segments)


def keys_of(spec: dict[str, str]) -> tuple[str, ...]:
    return tuple(w.strip() for w in re.split(r"[,,]", spec.get("keys", "")) if w.strip())


def mech_guide(vocab: Vocab, on: bool) -> str:
    """分镜提示词里 mech 版式的说明。没开时为空(提示词与之前一字不差)。
    返回值作为 format 参数填进 SYSTEM,里面的花括号不会再被解析。"""
    if not on or not vocab.mech_parts:
        return ""
    parts = "\n".join(f"    {name}:{spec.get('when', '')}" for name, spec in vocab.mech_parts.items())
    return (
        f"\n- mech:整屏机构画面(机构动画或剖视图),讲{vocab.subject_noun}里某个部件怎么工作时用。给 `mech`:\n"
        '  {"part": "部件名", "title": "这一拍在讲什么(12 字内,不带数字)",\n'
        f'    "marks": ["标注"], "rows": [{{"label": "参数名", "values": ["取值含单位"], "fact_id": "f_xxx"}}]}}\n'
        f"  marks 0–{MAX_MARKS} 条,每条 {MARK_CHARS} 字以内,必须是这一拍旁白里**原样出现**的部件名或动作"
        "(如「进气门晚关」「离合器结合」),不带数字;\n"
        "  rows 可以不给,给就 1–2 行,只能取自「可用事实」且这一拍旁白说过。part 只能从下面选:\n"
        f"{parts}\n"
        "  本片是原理拆解:段落「可选版式」里有 mech 的拍**优先用 mech**,连着几拍讲同一个部件就连着用同一个 part;"
        "只有这一拍只围绕一个数字时才换 stat。上面「全片 broll 不超过三成」之外,mech 不受连用限制。"
    )


def part_for(text: str, vocab: Vocab) -> str:
    """按旁白里的词挑部件:命中词最多的那个;一个都没命中返回空(不硬配)。"""
    best, hits = "", 0
    for name, spec in vocab.mech_parts.items():
        n = sum(1 for w in keys_of(spec) if w in (text or ""))
        if n > hits:
            best, hits = name, n
    return best


def _clean(text: Any) -> str:
    return _SPACE.sub("", str(text or "")).strip("。,,.;;:: ")  # noqa: B005 - 一组标点,不是一个词


def ground_marks(raw: Any, narration: str) -> list[dict[str, Any]]:
    """标注只留旁白里原样说过、且不带数字的。"""
    spoken = _SPACE.sub("", narration or "")
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw if isinstance(raw, list) else ():
        text = _clean(item.get("text") if isinstance(item, dict) else item)
        if not (2 <= len(text) <= MARK_CHARS) or _DIGIT.search(text):
            continue
        if text not in spoken or text in seen:
            continue
        seen.add(text)
        out.append({"text": text})
        if len(out) >= MAX_MARKS:
            break
    return out


def ground_mech(raw: Any, narration: str, facts: Sequence[Fact], vocab: Vocab
                ) -> tuple[dict[str, Any] | None, list[str]]:
    """核对一拍的机构画面数据 → (叠加数据或 None, 丢弃原因)。

    部件不认识时按旁白里的词再挑一次;还是挑不出就作废(调用方退回别的版式)。"""
    raw = raw if isinstance(raw, dict) else {}
    dropped: list[str] = []
    part = str(raw.get("part") or "").strip()
    if part not in vocab.mech_parts:
        guess = part_for(narration, vocab)
        if not guess:
            return None, [f"mech 部件 {part or '(空)'} 不在可选部件里,旁白里也没有命中任何部件的词"]
        dropped.append(f"mech 部件 {part or '(空)'} 不在可选部件里,按旁白改用 {guess}")
        part = guess
    return overlay_for(part, vocab, title=raw.get("title"), marks=raw.get("marks"),
                       rows=raw.get("rows"), narration=narration, facts=facts,
                       dropped=dropped), dropped


def overlay_for(part: str, vocab: Vocab, *, title: Any = "", marks: Any = None, rows: Any = None,
                narration: str = "", facts: Sequence[Fact] = (),
                dropped: list[str] | None = None) -> dict[str, Any]:
    spec = vocab.mech_parts[part]
    title = _clean(title)[:14]
    if _DIGIT.search(title):
        title = ""
    kept_rows: list[dict[str, Any]] = []
    source = ""
    if isinstance(rows, list) and rows:
        grounded, why = cards.ground(cards.normalise({"rows": rows}, "spec"), facts)
        if dropped is not None:
            dropped.extend(why)
        if grounded:
            said = numbers_in(narration or "")
            kept_rows = [r for r in grounded["rows"]
                         if all(numbers_in(str(v)) <= said for v in r.get("values") or [])][:2]
            source = grounded.get("source", "") if kept_rows else ""
    return {"kind": "mech", "part": part, "scene": spec.get("scene", ""),
            "mode": spec.get("mode", ""), "art": spec.get("art", ""), "view": 0, "title": title,
            "marks": ground_marks(marks, narration), "rows": kept_rows, "source": source}


def _set(spec: ShotSpec, overlay: dict[str, Any]) -> None:
    spec.layout = "mech"
    spec.overlay = overlay
    spec.card = None
    spec.visual_zh = re.sub(r"^\[[^\]]*\]", "[mech]", spec.visual_zh)


def promote_mech(specs: list[ShotSpec], profile: CategoryProfile) -> list[ShotSpec]:
    """原理拆解靠机构画面撑场:**每一章**允许 mech 的拍里它不到六成,就把还是 broll 的拍补成 mech。

    按章算而不是全片算:全片算的话前面几章补够了数,后面的「代价」一整章都是实拍,
    片子后三分之一看着又变回了宣传片(样片踩过)。

    部件按旁白里的词挑;这一拍没命中任何词,就接着用同一章上一拍的部件(旁白还在讲它,
    画面不该切回宣传片);这一章前面还没出现过部件,就用本章后面最先讲到的那个(代价章常常
    先报几句油耗才点到部件)。整章都没提部件就不补:第二轮样片里「用全片最多的部件兜底」
    让变速箱剖视图配上了油耗数字。讲钱、报数字的拍只认自己命中的词,不接续 ——
    配错图等于把画面讲错。最后给连用的剖视图排视角(`assign_views`)。"""
    vocab = profile.vocab
    if not vocab.mech_parts:
        return specs
    allowed = [s for s in specs if "mech" in (profile.section(s.section).layouts or ())]
    if not allowed:
        return specs
    chapters: dict[str, list[ShotSpec]] = {}
    for s in allowed:
        chapters.setdefault(s.chapter, []).append(s)
    for beats in chapters.values():
        need = int(len(beats) * MIN_SHARE + 0.999)
        have = sum(1 for s in beats if s.layout == "mech")
        last = ""
        for k, s in enumerate(beats):
            if s.layout == "mech" and isinstance(s.overlay, dict):
                last = str(s.overlay.get("part") or "")
                continue
            if have >= need or s.layout != "broll":
                continue
            part = part_for(s.narration_zh, vocab)
            if not part and not _MONEY.search(s.narration_zh or "") \
                    and not _DIGIT.search(s.narration_zh or ""):
                part = last or _next_part(beats[k + 1:], vocab)
            if not part:
                continue
            # 补上来的拍没有模型写的标注:旁白里命中的部件词就是现成的(照抄旁白、不带数字)
            said = [w for w in keys_of(vocab.mech_parts[part]) if w in (s.narration_zh or "")]
            _set(s, overlay_for(part, vocab, marks=said[:2], narration=s.narration_zh))
            last = part
            have += 1
    return assign_views(specs)


def _next_part(beats: list[ShotSpec], vocab: Any) -> str:
    """本章后面最先讲到的部件(已经是 mech 的拍,或旁白命中部件词的拍);没有返回空串。"""
    for s in beats:
        if s.layout == "mech" and isinstance(s.overlay, dict) and s.overlay.get("part"):
            return str(s.overlay["part"])
        part = part_for(s.narration_zh, vocab)
        if part:
            return part
    return ""


def assign_views(specs: list[ShotSpec]) -> list[ShotSpec]:
    """连着几拍用同一张剖视图时,每 `VIEW_EVERY` 拍换一个视角(整体 → 特写 → 爆炸图)。
    视角写进叠加数据的 `view`,渲染时按它取图;画出来的机构动画不受影响。"""
    from .. import mechanism_art

    run_part, n = "", 0
    for s in specs:
        ov = s.overlay if s.layout == "mech" and isinstance(s.overlay, dict) else None
        if ov is None or not ov.get("art"):
            run_part, n = "", 0
            continue
        part = str(ov.get("part") or "")
        n = n + 1 if part == run_part else 0
        run_part = part
        ov["view"] = (n // VIEW_EVERY) % len(mechanism_art.VIEWS)
    return specs


def scene_data(overlay: dict[str, Any], subject: str, idx: int = 0) -> dict[str, Any]:
    """mech 拍的叠加数据 → 模版数据。模版文件只许 ASCII,所以要上屏的中文全从这里给。"""
    from .. import motion

    base = motion.scene_data({**overlay, "rows": overlay.get("rows") or []}, subject)
    marks = [{"text": str(m.get("text", "")), **({"at": float(m["at"])} if isinstance(m.get("at"), (int, float)) else {})}
             for m in overlay.get("marks") or [] if m.get("text")]
    return {**base, "marks": marks, "mode": overlay.get("mode", ""), "part": overlay.get("part", ""),
            "variant": int(idx) % 4, "words": WORDS}


#: 模版里固定出现的中文(模版文件本身只许 ASCII)
WORDS: dict[str, Any] = {
    "strokes": ["进气", "压缩", "做功", "排气"],
    "intake": "进气", "exhaust": "排气", "turbo": "涡轮", "compressor": "压气机",
    "engine": "发动机", "generator": "发电机", "battery": "电池", "motor": "驱动电机",
    "wheels": "车轮", "clutch": "离合器", "clutch_open": "离合器分离", "clutch_closed": "离合器结合",
    "fuel": "油", "power": "电", "sketch": "示意图",
    "sun": "太阳轮", "carrier": "行星架", "ring": "齿圈", "held": "固定不转", "speed": "转速",
    "stator": "定子", "rotor": "转子", "phase_current": "三相电流", "field": "旋转磁场",
    "charge_port": "充电口", "cells": "电芯", "cool_plate": "冷却板",
    "coolant_in": "冷却液进", "coolant_out": "冷却液出",
    "body": "车身", "spring": "弹簧", "air_spring": "空气弹簧", "damper": "减振器",
    "wheel_move": "车轮跳动", "body_move": "车身晃动",
}
