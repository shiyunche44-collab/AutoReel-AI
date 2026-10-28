"""行业日历。"""
from __future__ import annotations

import logging
import time
from typing import Any

from .core import connect
from .schema import init_db

log = logging.getLogger(__name__)


def add_industry_event(row: dict[str, Any]) -> bool:
    """同品类同一天同标题的只记一次。返回是否新增。"""
    init_db()
    row = {"kind": "other", "car": "", "source": "manual", "url": "", "status": "active",
           "plan_item_id": "", **row}
    row.setdefault("created_at", time.time())
    with connect() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO industry_events (id, category, day, kind, car, title, source,"
            " url, status, plan_item_id, created_at) VALUES (:id, :category, :day, :kind, :car,"
            " :title, :source, :url, :status, :plan_item_id, :created_at)", row)
    return bool(cur.rowcount)


def get_industry_event(event_id: str) -> dict[str, Any] | None:
    init_db()
    with connect() as conn:
        row = conn.execute("SELECT * FROM industry_events WHERE id = ?", (event_id,)).fetchone()
    return dict(row) if row else None


def list_industry_events(category: str, *, day_from: str = "", day_to: str = "",
                         statuses: tuple[str, ...] = ("active",),
                         limit: int = 200) -> list[dict[str, Any]]:
    """按日期排。day_from / day_to 是闭区间的 YYYY-MM-DD。"""
    init_db()
    where, args = ["category = ?"], [category]
    if day_from:
        where.append("day >= ?")
        args.append(day_from)
    if day_to:
        where.append("day <= ?")
        args.append(day_to)
    if statuses:
        where.append(f"status IN ({','.join('?' * len(statuses))})")
        args.extend(statuses)
    sql = "SELECT * FROM industry_events WHERE " + " AND ".join(where) + \
        " ORDER BY day, created_at LIMIT ?"
    args.append(limit)
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]


def update_industry_event(event_id: str, **fields: Any) -> None:
    bad = set(fields) - {"status", "plan_item_id", "day", "kind", "car", "title"}
    if bad:
        raise ValueError(f"不能改的日历字段:{sorted(bad)}")
    if not fields:
        return
    init_db()
    sets = ", ".join(f"{k} = ?" for k in fields)
    with connect() as conn:
        conn.execute(f"UPDATE industry_events SET {sets} WHERE id = ?", (*fields.values(), event_id))
