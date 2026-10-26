"""复盘官的统计:按特征分组,看哪种做法比账号平常好 / 差。全是确定性算术,不用 LLM。

**比什么**:
- 播放:每条作品 24 小时播放 ÷ 同账号所有作品 24 小时播放的中位数(提升倍数)。
  用中位数做基线,不会被一条爆款拉偏;按账号分开比,大号小号不混。
- 完播率:每条完播率 ÷ 全部作品完播率的均值。
- 平均观看秒数:每条 ÷ 全部作品的均值。
- 2 秒留存:(1 − 2 秒跳出率)÷ 全部作品的均值。

**为什么盯留存而不是只盯完播**:官方规则写明「前几秒留不住人、完播差,后续就没有量」。
完播率会被视频长度稀释 —— 2 分钟和 8 分钟的完播率本来就不可比,拿它给不同长度
的作品排名是错的。2 秒留存和平均观看秒数跟时长无关,能横着比,才指得到「开头」
这个真正的瓶颈上。这两个指标创作者中心早就给了,之前一直躺在库里没人学。

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
METRICS = ("views", "completion_rate", "avg_watch_sec", "retention_2s")
METRIC_LABEL = {"views": "播放", "completion_rate": "完播率",
                "avg_watch_sec": "平均观看秒数", "retention_2s": "2秒留存"}

# --------------------------------------------------------------------------
# 早期诊断:不等 24 小时,发布后 1 小时那张快照就能看出前 2 秒留没留住人
# --------------------------------------------------------------------------
#: 2 秒留存低于这个数:前 2 秒掉了一半以上的人
RETENTION_BAD = 0.55
#: 2 秒留存高于这个数算留住了
RETENTION_GOOD = 0.75
#: 平均观看低于这个秒数:人进来马上就划走
WATCH_BAD = 8.0
#: 平均观看高于这个秒数算站得住
WATCH_GOOD = 20.0

#: 早期诊断给的动作建议(按问题类型)
ADVICE = {
    "hook": "前 2 秒就要给判断或画面,别铺垫、别先介绍自己;"
            "第一句改成一句带判断的反常识话,再给证据。",
    "pace": "前 15 秒没讲清「为什么值得看下去」。把结论和最有信息量的那一条前置,"
            "铺垫和背景往后挪。",
    "distribution": "内容留住了人,但没推给更多人。查封面第一眼和标题前 10 个字,"
                    "再看是不是只有粉丝看得到(fan_view_proportion 接近 1)。",
}


def lifts(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """每条作品补上 `lift`:{指标: 倍数}(算不了的不放)。

    播放按账号自己的中位数比(大号小号不混);其余指标全账号一条基线线,
    平均观看秒数用中位数(免得一条长片把基线拉飞)。"""
    by_acc: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        v = r["outcome"].get("views")
        if v is not None:
            by_acc[r["account_id"]].append(float(v))
    base = {acc: statistics.median(vs) for acc, vs in by_acc.items()}

    flat: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        for metric in METRICS:
            if metric == "views":
                continue
            val = r["outcome"].get(metric)
            if val is not None:
                flat[metric].append(float(val))
    baselines = {
        m: (statistics.median(vs) if m == "avg_watch_sec" else statistics.fmean(vs))
        for m, vs in flat.items() if vs
    }

    out = []
    for r in rows:
        lift: dict[str, float] = {}
        v, b = r["outcome"].get("views"), base.get(r["account_id"])
        if v is not None and b:
            lift["views"] = float(v) / b
        for metric in METRICS:
            if metric == "views":
                continue
            val = r["outcome"].get(metric)
            bb = baselines.get(metric)
            if val is not None and bb:
                lift[metric] = float(val) / bb
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


# --------------------------------------------------------------------------
# 早期诊断
# --------------------------------------------------------------------------
def diagnose(row: dict[str, Any]) -> dict[str, Any]:
    """一条(早期)作品 → {level, problems, advice}。

    `level` 是最严重的问题等级:bad / warn / ok。
    `problems` 是问题代号(hook / pace / distribution),`advice` 是对应的动作建议。

    诊断只用三个数:2 秒留存、平均观看秒数、播放。都是创作者中心直接给的,
    不用等 24 小时、不用猜。"""
    o = row.get("outcome") or {}
    ret = o.get("retention_2s")
    watch = o.get("avg_watch_sec")
    views = o.get("views")

    if not views:
        # 还没有播放数据:没什么可诊断的,别拿平台填的 0 当成「留存 100%」
        return {"level": "ok", "problems": [], "advice": []}

    problems: list[str] = []
    if ret is not None and ret < RETENTION_BAD:
        problems.append("hook")
    elif ret is not None and ret < RETENTION_GOOD:
        problems.append("pace")
    if watch is not None and watch < WATCH_BAD:
        problems.append("pace")
    if not problems and views is not None and views < 50:
        # 留存还行但没量:留住了人,没推给更多人
        problems.append("distribution")

    level = "ok"
    if "hook" in problems:
        level = "bad"
    elif problems:
        level = "warn"
    return {
        "level": level,
        "problems": problems,
        "advice": [ADVICE[p] for p in problems if p in ADVICE],
    }


def early_report(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """早期行(`features.early_rows`) → 可直接进周报的一行一条。"""
    out = []
    for r in rows:
        o = r["outcome"]
        d = diagnose(r)
        out.append({
            "pub_id": r["pub_id"],
            "title": r["title"],
            "hours": r.get("hours"),
            "views": o.get("views"),
            "retention_2s": o.get("retention_2s"),
            "bounce_2s": o.get("bounce_2s"),
            "avg_watch_sec": o.get("avg_watch_sec"),
            "completion_rate": o.get("completion_rate"),
            "followers_gained": o.get("followers_gained"),
            "level": d["level"],
            "advice": d["advice"],
        })
    out.sort(key=lambda r: ({"bad": 0, "warn": 1, "ok": 2}.get(r["level"], 3),
                             -(r["views"] or 0)))
    return out
