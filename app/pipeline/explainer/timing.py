"""按 TTS 时长给每拍写时间。"""
from __future__ import annotations

from typing import Any, Sequence
from ... import config, db
from ...categories import CategoryProfile
from ...models import ShotSpec
from .. import aspect, motion, narration, timeline


# --------------------------------------------------------------------------
# 定时长:先 TTS,再按旁白回填每拍时长
# --------------------------------------------------------------------------
def beat_durations(cues: Sequence[Any], count: int, gap: float | None = None) -> list[float]:
    """按字幕 cue 把句子时长(含句间停顿)归到各拍。

    与 `narration._concat_audio` 同口径:旁白轨 = Σ(句长 + 停顿),所以
    Σ 各拍时长 == 旁白轨总长,画面和声音不会越走越偏。
    """
    out = [0.0] * count
    for i, c in enumerate(cues):
        if not 0 <= c.shot_idx < count:
            continue
        if gap is not None:
            span = (c.end - c.start) + gap
        elif i + 1 < len(cues):
            span = cues[i + 1].start - c.start  # 句长 + 这一句后面实际留的停顿
        else:
            span = (c.end - c.start) + timeline.gap_after(c.text)
        out[c.shot_idx] += span
    return [round(x, 3) for x in out]


#: 画面比声音早切多少秒(上限)。切在上一句的换气停顿里,下一拍的第一个字出口时画面已经到位
CUT_LEAD_MAX = 0.12
#: 提前量不超过那段停顿的这个比例,免得画面切得比上一句的尾音还早
CUT_LEAD_OF_GAP = 0.6


def cut_leads(cues: Sequence[Any], count: int) -> dict[int, float]:
    """每一拍的画面比旁白早切多少秒(拍序号 → 秒;第一拍没有,不在字典里)。

    旁白是一条连续的轨,切点完全由画面决定:把切点从"下一句开口的那一刻"
    提前到上一句尾音之后的停顿中间,就是剪辑里的 J-cut 的最小形式,
    去掉"声音和画面同一帧切"的机械感。总时长不变。"""
    first: dict[int, Any] = {}
    last: dict[int, Any] = {}
    for c in cues:
        if 0 <= c.shot_idx < count:
            first.setdefault(c.shot_idx, c)
            last[c.shot_idx] = c
    order = sorted(first)
    leads: dict[int, float] = {}
    for prev, cur in zip(order, order[1:]):
        gap = first[cur].start - last[prev].end
        lead = min(CUT_LEAD_MAX, CUT_LEAD_OF_GAP * gap)
        if lead >= 0.02:
            leads[cur] = round(lead, 3)
    return leads


def apply_cut_leads(durations: list[float], leads: dict[int, float]) -> list[float]:
    """把各拍提前量折进时长:拍 k 的开头提前 L_k,上一拍就少 L_k,总长不变。
    折完某一拍会不到原来的 80%(极短的拍)就放弃这次调整。"""
    order = [k for k, d in enumerate(durations) if d > 0]
    out = list(durations)
    for prev, cur in zip(order, order[1:]):
        lead = leads.get(cur, 0.0)
        if lead and out[prev] - lead >= durations[prev] * 0.8:
            out[prev] = round(out[prev] - lead, 3)
            out[cur] = round(out[cur] + lead, 3)
        else:
            leads.pop(cur, None)
    return out


def time_beats(project_id: str, specs: list[ShotSpec], profile: CategoryProfile
               ) -> tuple[list[ShotSpec], narration.NarrationResult]:
    """跑 TTS(含字幕与 BGM),把每拍时长写回分镜。"""
    project = db.get_project(project_id)
    nar = narration.build(project_id, specs, profile=profile,
                          size=aspect.frame_size(project), line_chars=aspect.line_chars(project))
    if not nar.ok or not nar.cues:
        raise RuntimeError("旁白生成失败:" + "; ".join(nar.warnings or ["未知原因"]))
    if nar.missing_sentences:
        # 解说轨的时长由旁白决定:缺句子就是缺画面(实测丢的正好是结尾的结论段)。
        # 宁可停在这里让人再点一次确认,也不出一部没有结论的片子
        raise RuntimeError("旁白有句子没合成出来,请重新确认门禁 A 重试:"
                           + "; ".join(nar.warnings))
    if nar.qa_failed:
        # 语音质检:自动重合成后仍读错 / 错位 / 哑句的句子,不带着坏音频去渲染
        why = {f["index"]: f["reason"] for f in nar.qa_report.get("findings", [])
               if f.get("level") == "error"}
        detail = ";".join(f"第 {i + 1} 句 {why.get(i, '不合格')}" for i in nar.qa_failed[:8])
        raise RuntimeError(f"旁白质检未通过:{detail}。已自动重合成仍不合格,请改写这几句后重新确认门禁 A")
    durations = beat_durations(nar.cues, len(specs))
    leads: dict[int, float] = {}
    if config.CUT_LEAD_ENABLED:
        leads = cut_leads(nar.cues, len(specs))
        durations = apply_cut_leads(durations, leads)
    for s, d in zip(specs, durations):
        s.duration = d
    mark_reveal_times(specs, nar.cues, leads)
    return specs, nar


def mark_reveal_times(specs: Sequence[ShotSpec], cues: Sequence[Any],
                      leads: dict[int, float] | None = None) -> None:
    """concept 拍的每行参数记下旁白说到它的时刻(`rows[k].at`,相对拍头的秒数),
    动画里这一行就在那一刻出现 —— 参数跟着嘴说的走,而不是按固定间隔一条条排出来。"""
    by_shot: dict[int, list[Any]] = {}
    for c in cues:
        by_shot.setdefault(c.shot_idx, []).append(c)
    for s in specs:
        if s.layout != "concept" or not s.overlay or not by_shot.get(s.idx):
            continue
        mine = sorted(by_shot[s.idx], key=lambda c: c.start)
        rows = list(s.overlay.get("rows") or [])
        times = motion.row_reveal_times(rows, mine, mine[0].start, s.duration)
        lead = (leads or {}).get(s.idx, 0.0)  # 画面提前切了,拍头比开口早,出现时刻跟着后移
        times = [None if t is None else round(t + lead, 3) for t in times]
        new_rows = []
        for r, at in zip(rows, times):
            r = {k: v for k, v in r.items() if k != "at"}
            if at is not None:
                r["at"] = at
            new_rows.append(r)
        s.overlay = {**s.overlay, "rows": new_rows}
