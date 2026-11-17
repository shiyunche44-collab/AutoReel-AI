"""涨粉看板(只读):粉丝曲线 + 每条主片的漏斗 + 账号状态。

给定时任务「增长官」读:它据此判断卡在漏斗哪一层(开头 / 中段 / 转化 / 选题),
再通过偏好接口调方向。这里只汇总已有数据(分析师写的 account_daily、metric_snapshots),
不抓网络、不写库。

`conversion`:主页访问 → 关注(转化层)。只展示,不进 `goals` 的达标判断 —— 一周只有几十次主页访问,样本太小。

`goals`:固定打法后的两周目标(中位播放、2 秒跳出、5 秒完播)和达标情况。**先看留人,再看涨粉** ——
播放只有几十的时候,粉丝数不动是正常的。样本不到 `GOAL_MIN_WORKS` 条不下结论,也不该改打法。
"""
from __future__ import annotations

import datetime as dt
import json
import statistics
import time
from typing import Any

from .. import config, db
from . import compliance

#: 发满这么多条(满 24 小时的主片)才判断达没达标;之前不改打法
GOAL_MIN_WORKS = 10
#: 发布不满这么多小时的作品数据还在涨,不计入目标
GOAL_MATURE_HOURS = 24.0
#: (指标, 名字, 方向, 目标值, 没达标时卡在哪一层)。都取成熟作品的中位数
GOALS = (
    ("views", "中位播放", ">=", 300, "选题 / 分发:播放上不去,先看车型是不是榜单前列、标题带没带车名"),
    ("bounce_2s", "2 秒跳出", "<", 0.30, "开头:前 2 秒就划走,第一句和第一个画面要改"),
    ("completion_5s", "5 秒完播", ">", 0.45, "开场拍:第一句说完人就走了,判断要更硬、更短"),
)

#: 片型自己的一套目标:这些片型的作品不进上面那套(搜索向)目标,单独判断。
#: 原理拆解走推荐流量,通过线比搜索向高,数据也涨得慢(72 小时才算数)。
#: 「推荐页流量占比 ≥30%」没法在这里算 —— 服务没抓流量来源,那一项看周报里创作者中心的数
THEME_GOALS: dict[str, dict[str, Any]] = {
    "mechanism": {
        "min_works": 8,
        "mature_hours": 72.0,
        "metrics": (
            ("views", "中位播放", ">=", 500, "分发:没进推荐。看开场 3 秒有没有把问题和车亮出来、封面是不是问题句"),
            ("completion_5s", "5 秒完播", ">", 0.50, "开场:问题不够反常识,或者第一个画面不是机构"),
            ("followers_gained", "条均涨粉", ">=", 3, "转化:看完不关注。改结尾的下期预告、合集和主页定位,不动片型",
             "mean"),
        ),
        "passed": "三项都达标:这条线跑通了,可以加到每周 5 条并继续扩部件动画",
    },
}


def _followers(days: int, now: float) -> list[dict[str, Any]]:
    since = (dt.date.fromtimestamp(now) - dt.timedelta(days=days)).isoformat()
    daily = db.list_account_daily("douyin", since)
    out, prev = [], None
    for day, n in daily.items():
        out.append({"day": day, "followers": n, "gain": None if prev is None else n - prev})
        prev = n
    return out


def _variant(project: dict[str, Any] | None) -> dict[str, Any]:
    raw = (project or {}).get("variant") or "{}"
    try:
        v = json.loads(raw) if isinstance(raw, str) else dict(raw)
    except (TypeError, ValueError):
        return {}
    return v if isinstance(v, dict) else {}


def _raw_metric(snap: dict[str, Any], key: str) -> float | None:
    """快照原始数据里的指标(没单独落列的,如 5 秒完播)。读不到 = None。"""
    raw = snap.get("raw")
    try:
        raw = json.loads(raw) if isinstance(raw, str) else raw
        value = ((raw or {}).get("metrics") or {}).get(key)
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError, AttributeError):
        return None


def _since_ts(since: str | None) -> float | None:
    try:
        return dt.datetime.fromisoformat(since).timestamp() if since else None
    except ValueError:
        return None


def goals(works: list[dict[str, Any]], since: str | None = None,
          spec: dict[str, Any] | None = None) -> dict[str, Any]:
    """两周目标的达标情况。只算发布满 `GOAL_MATURE_HOURS` 小时、有快照的主片;
    `since`(YYYY-MM-DD)给了就只算那天之后发的。`spec`:某个片型自己的一套目标(`THEME_GOALS`)。"""
    spec = spec or {}
    min_works = spec.get("min_works", GOAL_MIN_WORKS)
    hours = spec.get("mature_hours", GOAL_MATURE_HOURS)
    t0 = _since_ts(since)
    mature = [w for w in works
              if w.get("views") is not None
              and (w.get("hours_since_publish") or 0) >= hours
              and (t0 is None or (w.get("published_at") or 0) >= t0)]
    ready = len(mature) >= min_works
    metrics = []
    for key, label, op, target, layer, *how in spec.get("metrics", GOALS):
        vals = [w[key] for w in mature if w.get(key) is not None]
        agg = statistics.mean if how and how[0] == "mean" else statistics.median
        value = round(agg(vals), 4) if vals else None
        met = None if value is None else (value >= target if op == ">=" else
                                          value < target if op == "<" else value > target)
        metrics.append({"key": key, "label": label, "op": op, "target": target, "value": value,
                        "n": len(vals), "met": met, "layer": layer})
    missed = [m for m in metrics if m["met"] is False]
    if not mature:
        verdict = f"还没有发布满 {hours:g} 小时的作品,没有可判断的数据"
    elif not ready:
        verdict = (f"才发满 {len(mature)}/{min_works} 条:样本不够,先别改打法"
                   + (";目前差在:" + "、".join(m["label"] for m in missed) if missed else ""))
    elif not missed and all(m["met"] for m in metrics):
        verdict = spec.get("passed") or "三项都达标:留人这一关过了,可以开始盯涨粉(关注引导、栏目、置顶评论)"
    else:
        verdict = "没达标:" + ";".join(m["layer"] for m in missed) if missed else "有指标没数据,判断不了"
    return {"since": since if t0 is not None else None, "works": len(mature),
            "min_works": min_works, "mature_hours": hours, "ready": ready, "hold": not ready,
            "metrics": metrics, "verdict": verdict}


def _work(pub: dict[str, Any], now: float) -> dict[str, Any]:
    project = db.get_project(pub["project_id"]) if pub.get("project_id") else None
    snap = db.latest_snapshot(pub["id"]) or {}
    variant = _variant(project)
    t0 = pub.get("published_at")
    return {
        "publication_id": pub["id"],
        "account_id": pub.get("account_id"),
        "title": pub.get("title"),
        "published_at": t0,
        "hours_since_publish": round((now - t0) / 3600, 1) if t0 else None,
        "subject": (project or {}).get("subject"),
        "theme": (project or {}).get("theme"),
        "aspect": (project or {}).get("aspect"),
        "duration": (project or {}).get("duration"),
        "opening": variant.get("opening"),
        "ending": variant.get("ending"),
        "series": variant.get("series"),
        "views_24h": compliance.views_at_day(pub),
        "snapshot_at": snap.get("captured_at"),
        "views": snap.get("views"),
        "likes": snap.get("likes"),
        "comments": snap.get("comments"),
        "shares": snap.get("shares"),
        "completion_rate": snap.get("completion_rate"),
        "avg_watch_sec": snap.get("avg_watch_sec"),
        "bounce_2s": snap.get("bounce_2s"),
        "completion_5s": _raw_metric(snap, "completion_rate_5s"),
        "followers_gained": snap.get("followers_gained"),
        "homepage_visits": _raw_metric(snap, "homepage_visit_count"),
    }


def _conversion(works: list[dict[str, Any]]) -> dict[str, Any]:
    """转化层:进了主页的人里有多少点了关注。只算两个数都读到的作品;没人进主页时比例为 None。"""
    rows = [w for w in works
            if w.get("homepage_visits") is not None and w.get("followers_gained") is not None]
    visits = int(sum(w["homepage_visits"] for w in rows))
    gained = int(sum(w["followers_gained"] for w in rows))
    return {"homepage_visits": visits, "followers_gained": gained, "works": len(rows),
            "visit_to_follow": round(gained / visits, 4) if visits else None}


def summary(days: int = 14, now: float | None = None, since: str | None = None) -> dict[str, Any]:
    """近 `days` 天的抖音涨粉情况。`since`:两周目标从哪天开始算(默认 `TEAM_GOAL_SINCE`)。"""
    now = time.time() if now is None else now
    cutoff = now - days * 86400
    pubs = [p for p in db.list_publications(limit=500)
            if p.get("platform") == "douyin" and not p.get("piece")
            and p["state"] in ("published", "submitted")
            and (p.get("published_at") or p.get("updated_at") or 0) >= cutoff]
    pubs.sort(key=lambda p: p.get("published_at") or p.get("updated_at") or 0, reverse=True)
    works = [_work(p, now) for p in pubs]
    followers = _followers(days, now)
    gained = [w["followers_gained"] for w in works if w["followers_gained"] is not None]
    views = [w["views"] for w in works if w["views"] is not None]
    goal_since = since if since is not None else config.TEAM_GOAL_SINCE
    return {
        "days": days,
        "generated_at": now,
        "followers": followers,
        "followers_now": followers[-1]["followers"] if followers else None,
        "followers_change": (followers[-1]["followers"] - followers[0]["followers"]
                             if len(followers) >= 2 else None),
        "totals": {"works": len(works), "views": sum(views),
                   "followers_gained": sum(gained)},
        "conversion": _conversion(works),
        "goals": goals([w for w in works if w.get("theme") not in THEME_GOALS], goal_since),
        "goals_by_theme": {name: goals([w for w in works if w.get("theme") == name], goal_since, spec)
                           for name, spec in THEME_GOALS.items()},
        "works": works,
        "accounts": [{"account_id": a["account_id"], "paused": bool(a.get("paused")),
                      "reason": a.get("reason"), "paused_at": a.get("paused_at")}
                     for a in db.list_account_health()],
    }
