"""概念动画(concept)节的保底与提拔。"""
from __future__ import annotations

import re
from typing import Sequence
from ...categories import CategoryProfile, Vocab
from ...models import ShotSpec
from .. import knobs

from .consts import MAX_CONCEPT


def concept_min(beats: int) -> int:
    """全片至少几拍概念动画。

    概念动画是全片视觉质量最高的部分(电池包格子图、补能曲线、尺寸线稿这类"看不见的东西"
    只有它讲得清),但实测一部片只用了 2–3 段。下限按拍数给:片子越长下限越高,
    但不超过上限 6 拍。
    下限比例跟着项目的「概念动画量」旋钮走(`knobs.concept_share`,默认 0.13)。
    """
    return min(MAX_CONCEPT, max(2 if knobs.value("concept_level") == "low" else 3,
                                round(beats * knobs.concept_share())))


def concept_guide(vocab: Vocab, beats: int = 0) -> str:
    """分镜提示词里 concept 版式的说明。品类没配动画模板时为空(提示词与之前一字不差)。
    返回值作为 format 参数填进 SYSTEM,里面的花括号不会再被解析。"""
    if not vocab.concept_scenes:
        return ""
    scenes = "\n".join(f"    {name}:{when}" for name, when in vocab.concept_scenes.items())
    lo = concept_min(beats) if beats else 0
    quota = f"全片 concept {lo}–{MAX_CONCEPT} 拍" if lo else f"全片最多 {MAX_CONCEPT} 拍"
    return (
        f"\n- concept:整屏概念动画,讲{vocab.subject_noun}身上看不见、拍不出来的东西时用。给 `concept`:\n"
        '  {"scene": "模板名", "title": "标题(12 字内)", "columns": [两个对比对象名,仅 compare 需要],\n'
        '    "rows": [{"label": "参数名", "values": ["取值含单位"], "fact_id": "f_xxx"}]}\n'
        "  rows 1–4 行,同样只能取自「可用事实」。scene 只能从下面选:\n"
        f"{scenes}\n"
        "  讲看不见的东西(电池、补能、性能、感知)的拍优先用 concept;"
        f"不要连续两拍 concept,{quota}。"
    )


def concept_scene_for(rows: Sequence[dict], narration: str, vocab: Vocab) -> str:
    """给一拍拍一张讲得通的概念模板:参数名 / 旁白里命中哪个模板的关键词就选它。

    配错图等于把画面讲错了 —— 讲补能速度的拍配一张电池包格子图,观众会以为在讲电池容量。
    所以这里必须**命中关键词**才给模板,一个都没命中就不配。
    """
    keys = getattr(vocab, "concept_scene_keys", {}) or {}
    if not keys:
        return ""
    text = " ".join([str(r.get("label", "")) for r in rows] + [narration or ""])
    best, best_hits = "", 0
    for scene, words in keys.items():
        hits = sum(1 for w in words if w and w in text)
        if hits > best_hits:
            best, best_hits = scene, hits
    return best if best_hits else ""


def promote_concepts(specs: list[ShotSpec], profile: CategoryProfile) -> list[ShotSpec]:
    """全片概念动画太少就补:把「本来就在讲参数、有核对过的行」的拍换成概念动画。

    复盘:概念动画是全片视觉质量最高的部分,一部片却只用了 2–3 段 —— 它是这行
    最难被抄的差异点,不该只当装饰用。补的时候只动**静态的数据面板**
    (panel / card),不动大数字(stat):大数字本身就是强画面,模型是特意选的;
    数字还是那批核对过的事实,不会多出没出处的东西;
    模板按参数名和旁白里的词选(见 `concept_scene_for`),不连着两拍 concept。
    """
    if not profile.vocab.concept_scenes:
        return specs
    target = concept_min(len(specs))
    count = sum(1 for s in specs if s.layout == "concept")
    if count >= target:
        return specs
    for n, s in enumerate(specs):
        if count >= target:
            break
        if s.layout not in ("panel", "card"):
            continue
        sec = profile.section(s.section)
        if "concept" not in (sec.layouts or ()):
            continue
        if (n and specs[n - 1].layout == "concept") or \
                (n + 1 < len(specs) and specs[n + 1].layout == "concept"):
            continue  # 别连着两拍 concept(前后两边都要看:后一拍可能是模型自己选的)
        ov = s.overlay if isinstance(s.overlay, dict) else {}
        card = s.card if isinstance(s.card, dict) else {}
        if card.get("columns"):
            continue  # 对比卡是两栏的,概念模板装不下
        rows = ov.get("rows") or card.get("rows") or []
        if not rows and ov.get("value"):
            rows = [{"label": ov.get("label", ""), "values": [ov["value"]],
                     "fact_id": ov.get("fact_id", ""), "tag": ov.get("tag", "")}]
        if not rows:
            continue
        scene = concept_scene_for(rows, s.narration_zh, profile.vocab)
        if not scene or scene not in profile.vocab.concept_scenes:
            continue
        s.layout = "concept"
        s.overlay = {"kind": "concept", "scene": scene, "title": "",
                     "rows": list(rows)[:4],
                     "source": ov.get("source") or card.get("source", "")}
        s.card = None
        s.visual_zh = re.sub(r"^\[[^\]]*\]", "[concept]", s.visual_zh)
        count += 1
    return specs
