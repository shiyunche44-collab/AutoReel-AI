"""涨粉看板(只读):粉丝曲线 + 每条主片的漏斗 + 账号状态。

给定时任务「增长官」读:它据此判断卡在漏斗哪一层(开头 / 中段 / 转化 / 选题),
再通过偏好接口调方向。这里只汇总已有数据(分析师写的 account_daily、metric_snapshots),
不抓网络、不写库。

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


def goals(works: list[dict[str, Any]], since: str | None = None) -> dict[str, Any]:
    """两周目标的达标情况。只算发布满 `GOAL_MATURE_HOURS` 小时、有快照的主片;
    `since`(YYYY-MM-DD)给了就只算那天之后发的。"""
    t0 = _since_ts(since)
    mature = [w for w in works
              if w.get("views") is not None
              and (w.get("hours_since_publish") or 0) >= GOAL_MATURE_HOURS
              and (t0 is None or (w.get("published_at") or 0) >= t0)]
    ready = len(mature) >= GOAL_MIN_WORKS
    metrics = []
    for key, label, op, target, layer in GOALS:
        vals = [w[key] for w in mature if w.get(key) is not None]
        value = round(statistics.median(vals), 4) if vals else None
        met = None if value is None else (value >= target if op == ">=" else
                                          value < target if op == "<" else value > target)
        metrics.append({"key": key, "label": label, "op": op, "target": target, "value": value,
                        "n": len(vals), "met": met, "layer": layer})
    missed = [m for m in metrics if m["met"] is False]
    if not mature:
        verdict = "还没有发布满 24 小时的作品,没有可判断的数据"
    elif not ready:
        verdict = (f"才发满 {len(mature)}/{GOAL_MIN_WORKS} 条:样本不够,先别改打法"
                   + (";目前差在:" + "、".join(m["label"] for m in missed) if missed else ""))
    elif not missed and all(m["met"] for m in metrics):
        verdict = "三项都达标:留人这一关过了,可以开始盯涨粉(关注引导、栏目、置顶评论)"
    else:
        verdict = "没达标:" + ";".join(m["layer"] for m in missed) if missed else "有指标没数据,判断不了"
    return {"since": since if t0 is not None else None, "works": len(mature),
            "min_works": GOAL_MIN_WORKS, "ready": ready, "hold": not ready,
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
    }


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
    return {
        "days": days,
        "generated_at": now,
        "followers": followers,
        "followers_now": followers[-1]["followers"] if followers else None,
        "followers_change": (followers[-1]["followers"] - followers[0]["followers"]
                             if len(followers) >= 2 else None),
        "totals": {"works": len(works), "views": sum(views),
                   "followers_gained": sum(gained)},
        "goals": goals(works, since if since is not None else config.TEAM_GOAL_SINCE),
        "works": works,
        "accounts": [{"account_id": a["account_id"], "paused": bool(a.get("paused")),
                      "reason": a.get("reason"), "paused_at": a.get("paused_at")}
                     for a in db.list_account_health()],
    }
