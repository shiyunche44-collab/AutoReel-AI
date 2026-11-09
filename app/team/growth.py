"""涨粉看板(只读):粉丝曲线 + 每条主片的漏斗 + 账号状态。

给定时任务「增长官」读:它据此判断卡在漏斗哪一层(开头 / 中段 / 转化 / 选题),
再通过偏好接口调方向。这里只汇总已有数据(分析师写的 account_daily、metric_snapshots),
不抓网络、不写库。
"""
from __future__ import annotations

import datetime as dt
import json
import time
from typing import Any

from .. import db
from . import compliance


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
        "followers_gained": snap.get("followers_gained"),
    }


def summary(days: int = 14, now: float | None = None) -> dict[str, Any]:
    """近 `days` 天的抖音涨粉情况。"""
    now = time.time() if now is None else now
    since = now - days * 86400
    pubs = [p for p in db.list_publications(limit=500)
            if p.get("platform") == "douyin" and not p.get("piece")
            and p["state"] in ("published", "submitted")
            and (p.get("published_at") or p.get("updated_at") or 0) >= since]
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
        "works": works,
        "accounts": [{"account_id": a["account_id"], "paused": bool(a.get("paused")),
                      "reason": a.get("reason"), "paused_at": a.get("paused_at")}
                     for a in db.list_account_health()],
    }
