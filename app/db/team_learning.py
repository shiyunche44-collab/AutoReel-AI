"""AI 团队:打法手册、迭代日志、成片反馈、周报。"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

from .core import connect, transaction
from .schema import init_db

log = logging.getLogger(__name__)


def replace_learnings(week: str, rows: list[dict[str, Any]], source: str = "own") -> None:
    """写本周的结论。上周有、本周没算出来的标 stale(不再生效);你停用过的保持停用。

    `source` 分开过期:写对标先验不会把自己作品的结论标 stale,反过来也一样。"""
    init_db()
    now = time.time()
    with transaction() as conn:
        off = {r[0] for r in conn.execute("SELECT id FROM learnings WHERE status = 'off'")}
        conn.execute("UPDATE learnings SET status = 'stale', updated_at = ?"
                     " WHERE status = 'active' AND source = ?", (now, source))
        for r in rows:
            conn.execute(
                "INSERT INTO learnings (id, week, dimension, value, metric, effect, n, confidence,"
                " evidence, status, updated_at, source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(id) DO UPDATE SET week = excluded.week, effect = excluded.effect,"
                " n = excluded.n, confidence = excluded.confidence, evidence = excluded.evidence,"
                " status = excluded.status, updated_at = excluded.updated_at",
                (r["id"], week, r["dimension"], r["value"], r["metric"], r["effect"], r["n"],
                 r["confidence"], json.dumps(r.get("evidence") or [], ensure_ascii=False),
                 "off" if r["id"] in off else "active", now, source))


def list_learnings(statuses: tuple[str, ...] = ("active",)) -> list[dict[str, Any]]:
    init_db()
    marks = ",".join("?" * len(statuses))
    with connect() as conn:
        rows = conn.execute(f"SELECT * FROM learnings WHERE status IN ({marks})"
                            " ORDER BY dimension, effect DESC", statuses).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["evidence"] = json.loads(d.get("evidence") or "[]")
        out.append(d)
    return out


def set_learning_status(learning_id: str, status: str) -> bool:
    """你在 /team 停用 / 恢复一条结论。"""
    init_db()
    with connect() as conn:
        cur = conn.execute("UPDATE learnings SET status = ?, updated_at = ? WHERE id = ?",
                           (status, time.time(), learning_id))
        return cur.rowcount > 0


def add_iteration_log(kind: str, title: str, detail: dict[str, Any] | None = None,
                      ts: float | None = None) -> None:
    init_db()
    with connect() as conn:
        conn.execute("INSERT INTO iteration_log (ts, kind, title, detail) VALUES (?, ?, ?, ?)",
                     (ts or time.time(), kind, title,
                      json.dumps(detail or {}, ensure_ascii=False)))


def list_iteration_log(limit: int = 50, since: float | None = None) -> list[dict[str, Any]]:
    """新的在前。"""
    init_db()
    with connect() as conn:
        rows = conn.execute("SELECT * FROM iteration_log WHERE ts >= ? ORDER BY ts DESC, id DESC"
                            " LIMIT ?", (since or 0, limit)).fetchall()
    return [{**dict(r), "detail": json.loads(r["detail"] or "{}")} for r in rows]


def add_review_feedback(row: dict[str, Any]) -> None:
    init_db()
    row = {"ts": time.time(), "note": "", "title_from": "", "title_to": "", **row}
    row["features"] = json.dumps(row.get("features") or {}, ensure_ascii=False)
    with connect() as conn:
        conn.execute("INSERT INTO review_feedback (ts, item_id, stage, decision, note, title_from,"
                     " title_to, features) VALUES (:ts, :item_id, :stage, :decision, :note,"
                     " :title_from, :title_to, :features)", row)


def list_review_feedback(since: float | None = None, stage: str | None = None,
                         limit: int = 1000) -> list[dict[str, Any]]:
    """新的在前。"""
    init_db()
    sql = "SELECT * FROM review_feedback WHERE ts >= ?"
    args: list[Any] = [since or 0]
    if stage:
        sql += " AND stage = ?"
        args.append(stage)
    with connect() as conn:
        rows = conn.execute(sql + " ORDER BY ts DESC, id DESC LIMIT ?", (*args, limit)).fetchall()
    return [{**dict(r), "features": json.loads(r["features"] or "{}")} for r in rows]


def save_report(week: str, data: dict[str, Any]) -> None:
    init_db()
    with connect() as conn:
        conn.execute("INSERT INTO team_reports (week, data, created_at) VALUES (?, ?, ?)"
                     " ON CONFLICT(week) DO UPDATE SET data = excluded.data,"
                     " created_at = excluded.created_at",
                     (week, json.dumps(data, ensure_ascii=False), time.time()))


def latest_report() -> dict[str, Any] | None:
    init_db()
    with connect() as conn:
        row = conn.execute("SELECT * FROM team_reports ORDER BY week DESC LIMIT 1").fetchone()
    if not row:
        return None
    return {"week": row["week"], "created_at": row["created_at"], **json.loads(row["data"])}


#: 评论里允许改的字段(`update_comment`)
