"""复盘官的统计:按特征分组,看哪种做法比账号平常好 / 差。全是确定性算术,不用 LLM。

**比什么**:
- 播放:每条作品 24 小时播放 ÷ 同账号所有作品 24 小时播放的中位数(提升倍数)。
  用中位数做基线,不会被一条爆款拉偏;按账号分开比,大号小号不混。
- 完播率:每条完播率 ÷ 全部作品完播率的均值。

**小样本保护**:一组的平均倍数向 1 收缩 —— `(倍数之和 + K) / (条数 + K)`,K=2,
相当于先假设每组都「和平常一样」,数据多了才慢慢偏离。少于 3 条不下结论,
3–5 条置信度「中」,6 条以上「高」;收缩后偏离不到 15% 也不算结论。
"""
from __future__ import annotations

import statistics
from collections import defaultdict
from typing import Any

from .features import DIMENSIONS

#: 收缩强度(先验相当于 K 条「和平常一样」的作品)
PRIOR = 2.0
MIN_N = 3
HIGH_N = 6
#: 收缩后至少偏离这么多才写成结论
MIN_EFFECT = 0.15
METRICS = ("views", "completion_rate")
METRIC_LABEL = {"views": "播放", "completion_rate": "完播率"}


def lifts(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """每条作品补上 `lift`:{views: 倍数, completion_rate: 倍数}(算不了的不放)。"""
    by_acc: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        v = r["outcome"].get("views")
        if v is not None:
            by_acc[r["account_id"]].append(float(v))
    base = {acc: statistics.median(vs) for acc, vs in by_acc.items()}
    rates = [float(r["outcome"]["completion_rate"]) for r in rows
             if r["outcome"].get("completion_rate") is not None]
    rate_mean = statistics.fmean(rates) if rates else 0.0
    out = []
    for r in rows:
        lift: dict[str, float] = {}
        v, b = r["outcome"].get("views"), base.get(r["account_id"])
        if v is not None and b:
            lift["views"] = float(v) / b
        c = r["outcome"].get("completion_rate")
        if c is not None and rate_mean > 0:
            lift["completion_rate"] = float(c) / rate_mean
        out.append({**r, "lift": lift})
    return out


def shrink(values: list[float], prior: float = PRIOR) -> float:
    return (sum(values) + prior) / (len(values) + prior)


def confidence(n: int) -> str:
    return "high" if n >= HIGH_N else "medium"


def learn(rows: list[dict[str, Any]], week: str) -> list[dict[str, Any]]:
    """作品行(`features.rows`) → 结论行(可直接给 `db.replace_learnings`)。"""
    rows = lifts(rows)
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        for dim, _ in DIMENSIONS:
            value = r["features"].get(dim)
            if not value:
                continue
            for metric in METRICS:
                if metric in r["lift"]:
                    groups[(dim, value, metric)].append(r)
    out = []
    for (dim, value, metric), members in groups.items():
        n = len(members)
        if n < MIN_N:
            continue
        effect = shrink([m["lift"][metric] for m in members])
        if abs(effect - 1) < MIN_EFFECT:
            continue
        top = sorted(members, key=lambda m: -m["lift"][metric])[:5]
        out.append({
            "id": f"{dim}:{value}:{metric}", "week": week, "dimension": dim, "value": value,
            "metric": metric, "effect": round(effect, 3), "n": n, "confidence": confidence(n),
            "evidence": [{"pub_id": m["pub_id"], "title": m["title"],
                          "lift": round(m["lift"][metric], 2)} for m in top],
        })
    out.sort(key=lambda r: -abs(r["effect"] - 1))
    return out


def sample_counts(rows: list[dict[str, Any]], dim: str) -> dict[str, int]:
    """某维度各取值有几条作品(复盘官挑「下周多试试」的用)。"""
    counts: dict[str, int] = defaultdict(int)
    for r in rows:
        value = r["features"].get(dim)
        if value:
            counts[value] += 1
    return dict(counts)
