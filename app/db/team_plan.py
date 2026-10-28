"""AI 团队:热点快照与选题单。"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from typing import Any

from .core import connect, transaction
from .schema import init_db

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# AI 团队:热点快照 / 选题单 / 角色运行记录 / 团队设置
# --------------------------------------------------------------------------
_TREND_COLS = ("source", "kind", "title", "car", "rank", "metric", "url", "date")


def add_trend_batch(category: str, batch: str, captured_at: float,
                    rows: list[dict[str, Any]]) -> int:
    """一次抓取的热点整批落库。返回条数。"""
    init_db()
    with transaction() as conn:
        for row in rows:
            conn.execute(
                "INSERT INTO trend_snapshots (batch, category, captured_at, source, kind, title,"
                " car, rank, metric, url, date) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (batch, category, captured_at,
                 *[row.get(c) if c == "rank" else str(row.get(c) or "") for c in _TREND_COLS]),
            )
    return len(rows)


def trend_batches(category: str, limit: int = 20) -> list[dict[str, Any]]:
    """最近几批的 (batch, captured_at, 条数),新的在前。"""
    init_db()
    with connect() as conn:
        cur = conn.execute(
            "SELECT batch, MAX(captured_at) AS captured_at, COUNT(*) AS n FROM trend_snapshots"
            " WHERE category = ? GROUP BY batch ORDER BY captured_at DESC LIMIT ?",
            (category, limit),
        )
        return [dict(r) for r in cur.fetchall()]


def trend_rows(batch: str) -> list[dict[str, Any]]:
    init_db()
    with connect() as conn:
        cur = conn.execute("SELECT * FROM trend_snapshots WHERE batch = ? ORDER BY id", (batch,))
        return [dict(r) for r in cur.fetchall()]


_PLAN_JSON = ("subjects", "evidence")


def _plan_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    out = dict(row)
    for key in _PLAN_JSON:
        try:
            out[key] = json.loads(out.get(key) or "[]")
        except (TypeError, ValueError):
            out[key] = []
    return out


def create_plan_item(row: dict[str, Any]) -> None:
    init_db()
    now = time.time()
    row = {"kind": "single", "subjects": [], "angle": "", "theme": "", "aspect": "16:9",
           "duration": 240, "hook": "", "search_terms": "", "evidence": [], "rationale": "",
           "heat": 3, "status": "proposed", "project_id": "", "note": "", **row}
    row.setdefault("created_at", now)
    row["updated_at"] = now
    for key in _PLAN_JSON:
        if not isinstance(row[key], str):
            row[key] = json.dumps(row[key], ensure_ascii=False)
    with connect() as conn:
        conn.execute(
            """INSERT INTO plan_items
               (id, category, slot_at, kind, subject, subjects, angle, theme, aspect, duration,
                hook, search_terms, evidence, rationale, heat, status, project_id, note,
                created_at, updated_at)
               VALUES (:id, :category, :slot_at, :kind, :subject, :subjects, :angle, :theme,
                       :aspect, :duration, :hook, :search_terms, :evidence, :rationale, :heat,
                       :status, :project_id, :note, :created_at, :updated_at)""",
            row,
        )


def get_plan_item(item_id: str) -> dict[str, Any] | None:
    init_db()
    with connect() as conn:
        return _plan_row(conn.execute("SELECT * FROM plan_items WHERE id = ?", (item_id,)).fetchone())


def list_plan_items(category: str | None = None, *, statuses: tuple[str, ...] = (),
                    since: float | None = None, until: float | None = None,
                    limit: int = 200) -> list[dict[str, Any]]:
    """按目标发布时刻排序的选题。"""
    init_db()
    where, args = [], []
    if category:
        where.append("category = ?")
        args.append(category)
    if statuses:
        where.append(f"status IN ({','.join('?' * len(statuses))})")
        args.extend(statuses)
    if since is not None:
        where.append("slot_at >= ?")
        args.append(since)
    if until is not None:
        where.append("slot_at < ?")
        args.append(until)
    sql = "SELECT * FROM plan_items"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY slot_at, created_at LIMIT ?"
    args.append(limit)
    with connect() as conn:
        return [_plan_row(r) for r in conn.execute(sql, args).fetchall()]


def update_plan_item(item_id: str, **fields: Any) -> None:
    if not fields:
        return
    init_db()
    fields["updated_at"] = time.time()
    for key in _PLAN_JSON:
        if key in fields and not isinstance(fields[key], str):
            fields[key] = json.dumps(fields[key], ensure_ascii=False)
    cols = ", ".join(f"{k} = ?" for k in fields)
    with connect() as conn:
        conn.execute(f"UPDATE plan_items SET {cols} WHERE id = ?", (*fields.values(), item_id))


def cas_plan_status(item_id: str, expected: tuple[str, ...], new: str, **fields: Any) -> bool:
    """状态是 expected 之一才改成 new。两个执行流抢同一条选题时只有一个成功。"""
    init_db()
    fields = {"status": new, "updated_at": time.time(), **fields}
    cols = ", ".join(f"{k} = ?" for k in fields)
    marks = ",".join("?" * len(expected))
    with connect() as conn:
        cur = conn.execute(f"UPDATE plan_items SET {cols} WHERE id = ? AND status IN ({marks})",
                           (*fields.values(), item_id, *expected))
        return cur.rowcount == 1
