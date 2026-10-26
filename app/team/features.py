"""每条发出去的作品抽特征 + 结果,给复盘官统计用。

**特征**:做这条片时我们能选的东西 —— 车型、片型、开头 / 结尾写法、视觉包、标题写法、
封面版本、画幅、时长档、发布星期 / 时段。都从项目和发布记录上读,不用 LLM。

**结果**:发布后约 24 小时那张快照(分析师 24h 那次)。各条作品在同一个时间点上比,
才不会把「发得早、累计久」当成「效果好」。还没到 24 小时的作品不算。
"""
from __future__ import annotations

import datetime as dt
from typing import Any

from .. import db

#: 统计的维度:(键, 中文名)
DIMENSIONS = (
    ("subject", "车型"), ("theme", "片型"), ("opening", "开头"), ("ending", "结尾"),
    ("look", "视觉包"), ("title_kind", "标题写法"), ("cover", "封面"), ("aspect", "画幅"),
    ("duration", "时长"), ("weekday", "发布星期"), ("hour", "发布时段"),
)
DIMENSION_LABEL = dict(DIMENSIONS)
#: 取哪个时间窗里的第一张快照当「24 小时结果」(小时)
DAY_WINDOW = (20.0, 72.0)
#: 早期诊断取最早那张快照,发布后多久之内算「早期」(小时)
EARLY_WINDOW = 24.0
#: 只统计这些平台(目前只有抖音有完整数据)
PLATFORMS = ("douyin",)


def value_label(dimension: str, value: str) -> str:
    """取值的中文名(开头 conflict → 冲突开场);查不到原样返回。"""
    from ..pipeline import looks, packaging, variety

    table: dict[str, Any] = {
        "opening": variety.OPENINGS, "ending": variety.ENDINGS, "look": looks.LOOKS,
    }.get(dimension, {})
    if value in table:
        return table[value].label
    if dimension == "title_kind":
        return packaging.KIND_LABEL.get(value, value)
    if dimension == "cover":
        return f"封面 {value.upper()}"
    return value


def _hours(snap: dict[str, Any], published_at: float) -> float:
    h = snap.get("hours_since_publish")
    return float(h) if h is not None else (snap["captured_at"] - published_at) / 3600


def day_snapshot(pub: dict[str, Any]) -> dict[str, Any] | None:
    t0 = pub.get("published_at")
    if not t0:
        return None
    lo, hi = DAY_WINDOW
    for snap in db.list_snapshots(pub["id"], limit=1000):
        if lo <= _hours(snap, t0) <= hi and snap.get("views") is not None:
            return snap
    return None


def duration_bucket(seconds: Any) -> str:
    try:
        s = int(seconds)
    except (TypeError, ValueError):
        return ""
    if s <= 90:
        return "≤90s"
    if s <= 180:
        return "90-180s"
    if s <= 300:
        return "3-5min"
    return ">5min"


def hour_bucket(ts: float) -> str:
    h = dt.datetime.fromtimestamp(ts).hour
    if h < 11:
        return "上午"
    if h < 14:
        return "中午"
    if h < 18:
        return "下午"
    if h < 21:
        return "晚上"
    return "深夜"


def extract(pub: dict[str, Any], project: dict[str, Any] | None) -> dict[str, str]:
    """一条发布记录的特征。取不到的维度留空串(统计时跳过)。"""
    from ..pipeline import variety

    project = project or {}
    pv = variety.parse(project.get("variant"))
    tv = variety.parse(pub.get("variant"))
    t0 = pub.get("published_at") or 0
    return {
        "subject": str(project.get("subject") or ""),
        "theme": str(project.get("theme") or ""),
        "opening": str(pv.get("opening") or ""),
        "ending": str(pv.get("ending") or ""),
        "look": str(pv.get("look") or ""),
        "title_kind": str(tv.get("title_kind") or ""),
        "cover": str(tv.get("cover") or ""),
        "aspect": str(pub.get("aspect") or project.get("aspect") or ""),
        "duration": duration_bucket(project.get("duration")),
        "weekday": "周" + "一二三四五六日"[dt.datetime.fromtimestamp(t0).weekday()] if t0 else "",
        "hour": hour_bucket(t0) if t0 else "",
    }


def outcome(snap: dict[str, Any]) -> dict[str, Any]:
    views = snap.get("views")
    eng = sum(int(snap.get(k) or 0) for k in ("likes", "comments", "favorites", "shares"))
    bounce = snap.get("bounce_2s")
    watch = snap.get("avg_watch_sec")
    rate = snap.get("completion_rate")
    # 抖音在没有播放数据时把深度指标一律填 0:跳出率 0.000000、平均观看 0 秒、完播 0。
    # 那不是「留存 100%」,是「还没人看」—— 播放为 0 时这些数当没有,否则一条零播放的
    # 作品会在统计里拖出一条假的高留存基线,后面每条真实作品都跟着被比低了。
    if not views:
        bounce = watch = rate = None
    elif watch is not None and float(watch) <= 0:
        watch = None
    return {
        "views": views,
        "completion_rate": rate,
        "bounce_2s": bounce,
        # 2 秒留存 = 1 − 2 秒跳出率。跟时长无关,能横着比不同长度的作品。
        "retention_2s": round(1.0 - float(bounce), 4) if bounce is not None else None,
        "avg_watch_sec": watch,
        "followers_gained": snap.get("followers_gained"),
        "engagement": round(eng / views, 4) if views else None,
    }


def rows(since: float | None = None) -> list[dict[str, Any]]:
    """有 24 小时结果的作品:[{pub_id, account_id, title, published_at, features, outcome}]。"""
    out = []
    for pub in db.list_publications(limit=1000):
        if pub["platform"] not in PLATFORMS or pub["state"] != "published" or pub.get("piece"):
            continue
        if since is not None and (pub.get("published_at") or 0) < since:
            continue
        snap = day_snapshot(pub)
        if not snap:
            continue
        out.append({"pub_id": pub["id"], "account_id": pub["account_id"],
                    "project_id": pub["project_id"], "title": pub.get("title") or "",
                    "published_at": pub["published_at"],
                    "features": extract(pub, db.get_project(pub["project_id"])),
                    "outcome": outcome(snap)})
    return out


# --------------------------------------------------------------------------
# 早期行:不等 24 小时,发布后 1 小时那张快照就能诊断
# --------------------------------------------------------------------------
def early_snapshot(pub: dict[str, Any]) -> dict[str, Any] | None:
    """最早的那张**有播放数据**的快照(发布后 1 小时左右那张)。

    刚发出去时那张快照播放是 0,深度指标全被平台填成 0 —— 拿它诊断会得出
    「留存 100%」的假结论。跳过它,找下一张真有数的。"""
    t0 = pub.get("published_at")
    if not t0:
        return None
    for snap in db.list_snapshots(pub["id"], limit=1000):
        if not snap.get("views"):
            continue
        if _hours(snap, t0) <= EARLY_WINDOW:
            return snap
    return None


def early_rows(since: float | None = None) -> list[dict[str, Any]]:
    """已经拿到早期快照的作品行。跟 `rows` 一样能抽特征,只是结果取的是最早那张。"""
    out = []
    for pub in db.list_publications(limit=1000):
        if pub["platform"] not in PLATFORMS or pub["state"] != "published" or pub.get("piece"):
            continue
        if since is not None and (pub.get("published_at") or 0) < since:
            continue
        snap = early_snapshot(pub)
        if not snap:
            continue
        out.append({"pub_id": pub["id"], "account_id": pub["account_id"],
                    "project_id": pub["project_id"], "title": pub.get("title") or "",
                    "published_at": pub["published_at"],
                    "hours": round(_hours(snap, pub["published_at"]), 2),
                    "features": extract(pub, db.get_project(pub["project_id"])),
                    "outcome": outcome(snap)})
    return out
