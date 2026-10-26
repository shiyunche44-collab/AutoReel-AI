"""迭代记录:看得见「到底有没有在变好」。

/team 的「迭代记录」卡读这里:
- **按周趋势**:每周发的作品 24 小时中位播放、2 秒留存、平均观看、涨粉,和你在选题闸的
  驳回率。数是分析师 24 小时那张快照(`features.day_snapshot`),和统计口径一致。
- **实验进度**:在跑的配对实验各发了几条、离能下结论还差几条。
- **变更日志**:每一次自动改动 —— 打法权重变动超过 0.1、实验开始 / 结束、自动调参 ——
  都写一条进 `iteration_log`。系统自己在改生产,改了什么必须看得见、追得回。
"""
from __future__ import annotations

import datetime as dt
import statistics
import time
from typing import Any

from .. import db

#: 趋势看几周
WEEKS = 8
#: 权重变动超过这么多才记进日志(权重夹在 0.5–2,0.1 约等于「多挑 / 少挑一成」)
WEIGHT_STEP = 0.1
#: 选题闸里「你已经表过态」的状态(批过以后的都算通过)
DECIDED = ("approved", "producing", "review", "scheduled", "published", "failed", "rejected")


def week_key(ts: float) -> str:
    y, w, _ = dt.datetime.fromtimestamp(ts).isocalendar()
    return f"{y}-W{w:02d}"


def _week_start(ts: float) -> dt.date:
    d = dt.datetime.fromtimestamp(ts).date()
    return d - dt.timedelta(days=d.weekday())


def _median(xs: list[float]) -> float | None:
    return round(statistics.median(xs), 2) if xs else None


def _mean(xs: list[float]) -> float | None:
    return round(statistics.fmean(xs), 4) if xs else None


# --------------------------------------------------------------------------
# 按周趋势
# --------------------------------------------------------------------------
def weekly(now: float | None = None, weeks: int = WEEKS) -> list[dict[str, Any]]:
    """最近 `weeks` 周(旧 → 新),没作品的周也占一格(数是 None),折线才不会把空周连过去。"""
    from . import features

    now = now or time.time()
    first = _week_start(now) - dt.timedelta(weeks=weeks - 1)
    since = dt.datetime.combine(first, dt.time()).timestamp()
    keys = [week_key(dt.datetime.combine(first + dt.timedelta(weeks=i), dt.time(12)).timestamp())
            for i in range(weeks)]
    buckets: dict[str, dict[str, list[float]]] = {
        k: {"views": [], "retention_2s": [], "avg_watch_sec": [], "followers": []} for k in keys}
    works = {k: 0 for k in keys}
    for pub in db.list_publications(limit=1000):
        if pub["platform"] not in features.PLATFORMS or pub["state"] != "published" \
                or pub.get("piece"):
            continue
        t0 = pub.get("published_at") or 0
        if t0 < since or week_key(t0) not in buckets:
            continue
        k = week_key(t0)
        works[k] += 1
        snap = features.day_snapshot(pub)
        if not snap:
            continue
        o = features.outcome(snap)
        b = buckets[k]
        for metric in ("views", "retention_2s", "avg_watch_sec"):
            if o.get(metric) is not None:
                b[metric].append(float(o[metric]))
        if o.get("followers_gained") is not None:
            b["followers"].append(float(o["followers_gained"]))

    decided = {k: 0 for k in keys}
    rejected = {k: 0 for k in keys}
    for item in db.list_plan_items(since=since, limit=2000):
        k = week_key(item["slot_at"])
        if k not in decided or item["status"] not in DECIDED:
            continue
        decided[k] += 1
        rejected[k] += item["status"] == "rejected"

    out = []
    for i, k in enumerate(keys):
        b = buckets[k]
        out.append({
            "week": k, "start": (first + dt.timedelta(weeks=i)).isoformat(),
            "works": works[k], "measured": len(b["views"]),
            "median_views": _median(b["views"]),
            "retention_2s": _mean(b["retention_2s"]),
            "avg_watch_sec": _median(b["avg_watch_sec"]),
            "followers": int(sum(b["followers"])) if b["followers"] else None,
            "decided": decided[k],
            "reject_rate": round(rejected[k] / decided[k], 3) if decided[k] else None,
        })
    return out


# --------------------------------------------------------------------------
# 变更日志
# --------------------------------------------------------------------------
def weight_snapshot(learnings: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    """所有维度的手册权重:{维度: {取值: 权重}}。"""
    from . import playbook
    from .features import DIMENSIONS

    return {d: w for d, _ in DIMENSIONS if (w := playbook.weights(d, learnings))}


def log_weight_changes(before: dict[str, dict[str, float]], after: dict[str, dict[str, float]],
                       ts: float | None = None) -> int:
    """前后两份权重,变动 ≥ `WEIGHT_STEP` 的各记一条。返回记了几条。"""
    from .features import DIMENSION_LABEL, value_label

    n = 0
    for dim in sorted(set(before) | set(after)):
        old, new = before.get(dim, {}), after.get(dim, {})
        for value in sorted(set(old) | set(new)):
            a, b = float(old.get(value, 1.0)), float(new.get(value, 1.0))
            if abs(b - a) < WEIGHT_STEP:
                continue
            verb = "多用" if b > a else "少用"
            db.add_iteration_log(
                "weight",
                f"{DIMENSION_LABEL.get(dim, dim)}「{value_label(dim, value)}」权重 "
                f"{a:.2f} → {b:.2f}({verb})",
                {"dimension": dim, "value": value, "before": a, "after": b}, ts=ts)
            n += 1
    return n


def _plan_label(p: dict[str, Any]) -> str:
    from .features import DIMENSION_LABEL, value_label

    dim = str(p["dimension"])
    return f"{DIMENSION_LABEL.get(dim, dim)}「{value_label(dim, str(p['value']))}」"


def log_plan_changes(old: list[dict[str, Any]], new: list[dict[str, Any]],
                     ts: float | None = None) -> int:
    """复盘官换实验时:做完的记「结束」、没做完被换掉的记「换下」、新下发的记「开始」。

    `old` 是 `playbook.progress()`(带 used / done),`new` 是新存进去的计划。"""
    key = lambda p: (str(p["dimension"]), str(p["value"]))  # noqa: E731
    new_keys = {key(p) for p in new}
    old_keys = {key(p) for p in old}
    n = 0
    for p in old:
        if key(p) in new_keys:
            continue
        if p.get("done"):
            title = f"实验结束:{_plan_label(p)} 攒够 {p['used']} 条,交给统计"
        else:
            title = f"实验换下:{_plan_label(p)} 只发了 {p.get('used', 0)}/{p.get('size')} 条"
        db.add_iteration_log("experiment", title,
                             {**{k: p.get(k) for k in ("dimension", "value", "used", "size")},
                              "event": "end" if p.get("done") else "drop"}, ts=ts)
        n += 1
    for p in new:
        if key(p) in old_keys:
            continue
        db.add_iteration_log("experiment",
                             f"实验开始:{_plan_label(p)},连发 {p.get('size')} 条",
                             {"dimension": p["dimension"], "value": p["value"],
                              "size": p.get("size"), "event": "start"}, ts=ts)
        n += 1
    return n


# --------------------------------------------------------------------------
# 页面
# --------------------------------------------------------------------------
KIND_LABEL = {"weight": "打法权重", "experiment": "配对实验", "param": "自动调参"}


def overview(now: float | None = None) -> dict[str, Any]:
    from . import playbook
    from .features import DIMENSION_LABEL, value_label

    exps = []
    for p in playbook.progress():
        dim = p["dimension"]
        exps.append({**p, "label": f"{DIMENSION_LABEL.get(dim, dim)}:{value_label(dim, p['value'])}",
                     "left": max(0, p["size"] - p["used"])})
    log = [{**r, "kind_label": KIND_LABEL.get(r["kind"], r["kind"])}
           for r in db.list_iteration_log(limit=30)]
    return {"weeks": weekly(now), "experiments": exps, "log": log,
            # 阶段 2:写脚本时带进提示词的账号经验(开场正反例 + 最近诊断)
            "script_hint": playbook.script_hint(now)}
