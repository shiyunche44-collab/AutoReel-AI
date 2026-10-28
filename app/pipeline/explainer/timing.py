"""按 TTS 时长给每拍写时间。"""
from __future__ import annotations

from typing import Any, Sequence
from ... import config, db
from ...categories import CategoryProfile
from ...models import ShotSpec
from .. import aspect, motion, narration


# --------------------------------------------------------------------------
# 定时长:先 TTS,再按旁白回填每拍时长
# --------------------------------------------------------------------------
def beat_durations(cues: Sequence[Any], count: int, gap: float | None = None) -> list[float]:
    """按字幕 cue 把句子时长(含句间停顿)归到各拍。

    与 `narration._concat_audio` 同口径:旁白轨 = Σ(句长 + 停顿),所以
    Σ 各拍时长 == 旁白轨总长,画面和声音不会越走越偏。
    """
    gap_s = config.SENTENCE_GAP_MS / 1000.0 if gap is None else gap
    out = [0.0] * count
    for c in cues:
        if 0 <= c.shot_idx < count:
            out[c.shot_idx] += (c.end - c.start) + gap_s
    return [round(x, 3) for x in out]


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
    for s, d in zip(specs, durations):
        s.duration = d
    mark_reveal_times(specs, nar.cues)
    return specs, nar


def mark_reveal_times(specs: Sequence[ShotSpec], cues: Sequence[Any]) -> None:
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
        new_rows = []
        for r, at in zip(rows, times):
            r = {k: v for k, v in r.items() if k != "at"}
            if at is not None:
                r["at"] = at
            new_rows.append(r)
        s.overlay = {**s.overlay, "rows": new_rows}
