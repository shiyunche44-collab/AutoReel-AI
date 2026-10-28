"""评论与回复。"""
from __future__ import annotations

import logging
import time
from typing import Any

from .core import connect, transaction
from .schema import init_db

log = logging.getLogger(__name__)


_COMMENT_FIELDS = {"kind", "kind_source", "reply_draft", "reply_source", "reply_state",
                   "reply_error", "replied_at", "topic_state", "reply_style"}


def add_comments(rows: list[dict[str, Any]]) -> int:
    """新评论入库,已有的只更新点赞数(分类、草稿、审核状态都不动)。返回新增条数。"""
    init_db()
    now = time.time()
    n = 0
    with transaction() as conn:
        for r in rows:
            cur = conn.execute(
                "INSERT OR IGNORE INTO comments (id, publication_id, account_id, remote_id, author,"
                " text, created_at, likes, kind, kind_source, reply_draft, reply_source,"
                " reply_state, reply_style, fetched_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (r["id"], r["publication_id"], r.get("account_id"), r.get("remote_id"),
                 r.get("author") or "", r.get("text") or "", r["created_at"], r.get("likes") or 0,
                 r.get("kind") or "other", r.get("kind_source") or "rule",
                 r.get("reply_draft") or "", r.get("reply_source") or "template",
                 r.get("reply_state") or "draft", r.get("reply_style") or "", now, now))
            if cur.rowcount:
                n += 1
            else:
                conn.execute("UPDATE comments SET likes = ? WHERE id = ?",
                             (r.get("likes") or 0, r["id"]))
    return n


def get_comment(comment_id: str) -> dict[str, Any] | None:
    init_db()
    with connect() as conn:
        row = conn.execute("SELECT * FROM comments WHERE id = ?", (comment_id,)).fetchone()
    return dict(row) if row else None


def list_comments(*, states: tuple[str, ...] = (), kinds: tuple[str, ...] = (),
                  publication_id: str | None = None, since: float | None = None,
                  topic_states: tuple[str, ...] | None = None,
                  limit: int = 200) -> list[dict[str, Any]]:
    """新评论在前。"""
    init_db()
    where, args = [], []
    for col, vals in (("reply_state", states), ("kind", kinds), ("topic_state", topic_states)):
        if vals:
            where.append(f"{col} IN ({','.join('?' * len(vals))})")
            args.extend(vals)
    if publication_id:
        where.append("publication_id = ?")
        args.append(publication_id)
    if since is not None:
        where.append("created_at >= ?")
        args.append(since)
    sql = "SELECT * FROM comments" + (" WHERE " + " AND ".join(where) if where else "")
    with connect() as conn:
        rows = conn.execute(sql + " ORDER BY created_at DESC LIMIT ?", (*args, limit)).fetchall()
    return [dict(r) for r in rows]


def update_comment(comment_id: str, **fields: Any) -> None:
    bad = set(fields) - _COMMENT_FIELDS
    if bad:
        raise ValueError(f"不能改的评论字段:{sorted(bad)}")
    if not fields:
        return
    init_db()
    sets = ", ".join(f"{k} = ?" for k in fields)
    with connect() as conn:
        conn.execute(f"UPDATE comments SET {sets}, updated_at = ? WHERE id = ?",
                     (*fields.values(), time.time(), comment_id))


def cas_comment_state(comment_id: str, expected: tuple[str, ...], new: str, **fields: Any) -> bool:
    """回复状态从 expected 之一改成 new;已经被别人改走就返回 False(批量审核和发送并发时不重发)。"""
    bad = set(fields) - _COMMENT_FIELDS
    if bad:
        raise ValueError(f"不能改的评论字段:{sorted(bad)}")
    init_db()
    sets = "".join(f", {k} = ?" for k in fields)
    marks = ",".join("?" * len(expected))
    with connect() as conn:
        cur = conn.execute(
            f"UPDATE comments SET reply_state = ?{sets}, updated_at = ?"
            f" WHERE id = ? AND reply_state IN ({marks})",
            (new, *fields.values(), time.time(), comment_id, *expected))
        return cur.rowcount > 0
