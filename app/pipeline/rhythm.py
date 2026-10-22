"""节奏硬约束:画面变化间隔 + broll 占比。

复盘原话(analysis/teardown.html):

    「建议加一条硬约束:每 5–6 秒必须有一次画面变化(换镜头 / 换版式 / 进大字 / 进动画),
      且全片 broll 占比从四成压到三成以内,腾出来的位置给 concept 和 stat。」

两条约束的口径,先说清楚 —— 口径不对,指标就是自欺欺人:

**画面变化 = 信息层的变化**
    换版式 / 进大字 / 进整屏卡片 / 进概念动画。
    「换镜头」单独统计,不算进这条约束:实测背景本来就每 2.4–3.7 秒一刀
    (`library.fill` 按 4.5 秒一刀拼),把它算进来会让指标永远合格,
    等于没有指标。观众感知到的「画面变了」是叠加层换了,不是底图换了。

**broll 占比 = 两个口径都给**
    拍数口径给审片的人看(一眼能数),秒数口径给观众看(他占的是时间)。
    两个口径都超三成才算超标 —— 只看拍数会被"短 broll + 长面板"糊弄过去。
"""
from __future__ import annotations

from typing import Any, Sequence

from ..models import ShotSpec

#: 相邻两次画面变化之间最长允许多少秒(复盘要求 5–6 秒一次)
MAX_CHANGE_GAP = 6.0
#: 全片 broll 占比上限(拍数与秒数两个口径)
BROLL_RATIO_MAX = 0.30
#: 连续同版式(且叠加内容也一样)最多几拍 —— 再多就是同一屏画面反复
MAX_SAME_RUN = 2
#: 冷开场前几秒每 1 秒一刀硬切 —— 这几秒算"一直在变"
HOOK_CHANGE_SECONDS = 3.0


def _overlay_signature(spec: ShotSpec) -> tuple:
    """叠加内容指纹:同样的版式 + 同样的行/词 = 同一屏画面。"""
    ov = spec.overlay if isinstance(spec.overlay, dict) else {}
    rows = ov.get("rows") or []
    lines = ov.get("lines") or []
    return (spec.layout, spec.chapter or spec.section,
            tuple(r.get("fact_id", "") for r in rows if isinstance(r, dict)),
            tuple(str(x) for x in lines))


def broll_ratio(specs: Sequence[ShotSpec]) -> float:
    """broll 拍数占全片的比例。"""
    if not specs:
        return 0.0
    n = sum(1 for s in specs if s.layout == "broll")
    return n / len(specs)


def broll_seconds_ratio(specs: Sequence[ShotSpec]) -> tuple[float, float]:
    """(broll 秒数占比, 全片秒数)。时长还没回填(0)时退化成拍数口径。"""
    total = sum(float(s.duration or 0) for s in specs)
    if total <= 0:
        return broll_ratio(specs), 0.0
    broll = sum(float(s.duration or 0) for s in specs if s.layout == "broll")
    return broll / total, total


def change_points(specs: Sequence[ShotSpec]) -> list[tuple[float, int]]:
    """画面变化的时间点:[(秒, 拍号)]。

    一次变化 = 换了版式 / 换了章节 / 换了 broll 样式 / 换了叠加内容。
    冷开场前几秒每 1 秒一刀硬切,也算一次变化。
    """
    out: list[tuple[float, int]] = []
    t = 0.0
    prev_sig: tuple | None = None
    for s in specs:
        dur = max(0.0, float(s.duration or 0))
        sig = _overlay_signature(s)
        if prev_sig is None or sig != prev_sig or s.layout == "hook":
            out.append((round(t, 3), s.idx))
        prev_sig = sig
        t += dur
    return out


def quiet_gaps(specs: Sequence[ShotSpec]) -> list[float]:
    """每一段「画面没变」的时长。

    两种版式不产生静默区间:
    - 概念动画:整拍都在动;
    - 冷开场:前 3 秒每 1 秒一刀硬切,只有硬切之后的那截算静默。
    """
    gaps: list[float] = []
    start: float | None = None
    t = 0.0
    prev_sig: tuple | None = None
    for s in specs:
        dur = max(0.0, float(s.duration or 0))
        sig = _overlay_signature(s)
        changed = prev_sig is None or sig != prev_sig
        prev_sig = sig
        fast = s.layout == "hook"
        if changed or fast:
            if start is not None:
                gaps.append(t - start)
            start = None
        if s.layout != "concept":
            if start is None:
                start = t + (min(HOOK_CHANGE_SECONDS, dur) if fast else 0.0)
        t += dur
    if start is not None:
        gaps.append(t - start)
    return [round(g, 3) for g in gaps]


def max_change_gap(specs: Sequence[ShotSpec]) -> float:
    """最长的一段「没有画面变化」的时间。"""
    gaps = quiet_gaps(specs)
    return max(gaps) if gaps else 0.0


def same_run(specs: Sequence[ShotSpec]) -> tuple[int, int]:
    """最长同版式同内容连拍: (几拍, 从第几拍开始)。"""
    best = best_start = run = 0
    start = 0
    prev: tuple | None = None
    for n, s in enumerate(specs):
        sig = _overlay_signature(s)
        if prev is not None and sig == prev:
            run += 1
        else:
            run = 1
            start = n
        prev = sig
        if run > best:
            best, best_start = run, start
    return best, best_start


def longest_beat(specs: Sequence[ShotSpec]) -> tuple[float, int]:
    """最长的一拍: (秒, 拍号)。"""
    best = (0.0, 0)
    for s in specs:
        d = float(s.duration or 0)
        if d > best[0]:
            best = (d, s.idx)
    return best


def report(specs: Sequence[ShotSpec]) -> dict[str, Any]:
    """一份节奏体检报告。写进 rhythm.json,门禁和前端都能看。"""
    ratio, total = broll_seconds_ratio(specs)
    beat_ratio = broll_ratio(specs)
    run, run_start = same_run(specs)
    longest, longest_idx = longest_beat(specs)
    return {
        "beats": len(specs),
        "broll_beats": sum(1 for s in specs if s.layout == "broll"),
        "broll_ratio": round(beat_ratio, 4),
        "broll_seconds": round(sum(float(s.duration or 0) for s in specs
                                    if s.layout == "broll"), 3),
        "total_seconds": round(total, 3),
        "broll_second_ratio": round(ratio, 4),
        "max_change_gap": max_change_gap(specs),
        "longest_beat": round(longest, 3),
        "longest_beat_idx": longest_idx,
        "same_run": run,
        "same_run_start": run_start,
        "problems": problems(specs),
    }


def problems(specs: Sequence[ShotSpec]) -> list[str]:
    """超标项。空列表 = 节奏达标。"""
    out: list[str] = []
    if not specs:
        return out
    beat_ratio = broll_ratio(specs)
    if beat_ratio > BROLL_RATIO_MAX + 1e-6:
        n = sum(1 for s in specs if s.layout == "broll")
        out.append(f"broll {n}/{len(specs)} 拍({beat_ratio:.0%}),超过三成上限")
    ratio, total = broll_seconds_ratio(specs)
    if total > 0 and ratio > BROLL_RATIO_MAX + 1e-6:
        out.append(f"broll 占全片 {ratio:.0%} 的时长(约 {total * ratio:.0f}/{total:.0f} 秒),"
                   "超过三成上限")
    gap = max_change_gap(specs)
    if gap > MAX_CHANGE_GAP + 1e-6:
        out.append(f"有 {gap:.1f} 秒没有画面变化(上限 {MAX_CHANGE_GAP:.0f} 秒)")
    run, start = same_run(specs)
    if run > MAX_SAME_RUN:
        out.append(f"从第 {start + 1} 拍起连续 {run} 拍画面完全一样")
    return out
