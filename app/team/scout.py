"""情报员:定时抓热点并落库,算车型名次涨跌。

**为什么要落库**:/api/topics 只在打开页面时现抓、内存里缓存 30 分钟,
抓完就没了 —— 看不出「这台车这两天在往上冲」。落成批次后,主编能拿
最近一批直接排选题(不用再等几十秒抓取),也能和一天前比名次。
"""
from __future__ import annotations

import time
from typing import Any, Iterable

from .. import config, db
from ..categories import load_profile
from ..models import new_id
from . import TeamError

#: 主编用的热点超过这么久就当过期,现抓一批
FRESH_HOURS = 12
#: 和多久之前的那批比名次
COMPARE_HOURS = 24
#: 有名次的来源(榜单)
RANKED = ("dongchedi.sales", "dongchedi.hot")


def snapshot_rows(items: Iterable[Any]) -> list[dict[str, Any]]:
    """热点条目 → 落库行。同来源内按出现顺序编名次(榜单本来就按名次给)。"""
    seen: dict[str, int] = {}
    rows = []
    for item in items:
        d = item.to_dict() if hasattr(item, "to_dict") else dict(item)
        source = str(d.get("source") or "")
        seen[source] = seen.get(source, 0) + 1
        rows.append({**{k: d.get(k, "") for k in ("source", "kind", "title", "car", "metric",
                                                   "url", "date")},
                     "rank": seen[source]})
    return rows


def movers(before: list[dict[str, Any]], after: list[dict[str, Any]],
           sources: tuple[str, ...] = RANKED) -> list[dict[str, Any]]:
    """两批榜单之间的名次变化。新上榜的 delta 为 None。按上升幅度排,新上榜排最前。"""
    def ranks(rows: list[dict[str, Any]]) -> dict[tuple[str, str], int]:
        out: dict[tuple[str, str], int] = {}
        for r in rows:
            if r.get("source") in sources and r.get("car") and r.get("rank"):
                out.setdefault((r["source"], r["car"]), int(r["rank"]))
        return out

    old, new = ranks(before), ranks(after)
    out = []
    for (source, car), rank in new.items():
        prev = old.get((source, car))
        if prev is None and old:
            out.append({"source": source, "car": car, "rank": rank, "prev": None, "delta": None})
        elif prev is not None and prev != rank:
            out.append({"source": source, "car": car, "rank": rank, "prev": prev, "delta": prev - rank})
    out.sort(key=lambda m: (m["delta"] is not None, -(m["delta"] or 0), m["rank"]))
    return out


def _batch_before(category: str, at: float, hours: float) -> dict[str, Any] | None:
    """离 at 往前 hours 小时最近的那批(没有就取上一批)。"""
    batches = [b for b in db.trend_batches(category, limit=30) if b["captured_at"] < at - 60]
    if not batches:
        return None
    target = at - hours * 3600
    older = [b for b in batches if b["captured_at"] <= target]
    return older[0] if older else batches[-1]


def latest(category: str) -> dict[str, Any] | None:
    batches = db.trend_batches(category, limit=1)
    return batches[0] if batches else None


def rising(category: str, limit: int = 8) -> list[dict[str, Any]]:
    """最新一批相对一天前的名次涨跌。"""
    cur = latest(category)
    if not cur:
        return []
    prev = _batch_before(category, cur["captured_at"], COMPARE_HOURS)
    if not prev:
        return []
    moved = movers(db.trend_rows(prev["batch"]), db.trend_rows(cur["batch"]))
    return [m for m in moved if m["delta"] is None or m["delta"] > 0][:limit]


def fresh_items(category: str, max_age_hours: float = FRESH_HOURS) -> list[dict[str, Any]]:
    """主编用的热点:最近一批没过期就直接用,过期了现抓一批。"""
    cur = latest(category)
    if cur and time.time() - cur["captured_at"] < max_age_hours * 3600:
        return db.trend_rows(cur["batch"])
    collect(category)
    cur = latest(category)
    return db.trend_rows(cur["batch"]) if cur else []


def collect(category: str) -> tuple[int, dict[str, str]]:
    from ..providers import trends

    profile = load_profile(category)
    items, errors = trends.collect(spec=profile.trends, brands=profile.trend_brands)
    if not items:
        why = ";".join(f"{k}:{v}" for k, v in errors.items()) or "没有条目"
        raise TeamError(f"热点一条都没抓到({why})")
    n = db.add_trend_batch(category, new_id("tb_"), time.time(), snapshot_rows(items))
    return n, errors


def _move_label(m: dict[str, Any]) -> str:
    return "新上榜" if m["delta"] is None else f"↑{m['delta']}"


def run(category: str | None = None) -> str:
    category = category or config.TEAM_CATEGORY
    n, errors = collect(category)
    up = rising(category, limit=3)
    parts = [f"抓到 {n} 条热点"]
    if errors:
        parts.append(f"{len(errors)} 个来源失败({'、'.join(errors)})")
    if up:
        parts.append("上升:" + "、".join(m["car"] + _move_label(m) for m in up))
    return ",".join(parts)
