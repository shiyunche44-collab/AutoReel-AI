"""活跃官:对外互动留痕。"""
from __future__ import annotations

import logging
import time
from typing import Any

from .core import connect, transaction
from .schema import init_db

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# 活跃官:对外互动留痕(评论 / 点赞 / 分享 / 关注)
# --------------------------------------------------------------------------
#: 允许改的字段(`update_outbound_action`)
_OUTBOUND_FIELDS = {"text", "state", "reason", "started_at", "done_at", "title",
                     "author", "sec_uid"}


def add_outbound_actions(rows: list[dict[str, Any]]) -> int:
    """新动作入库。同一目标同一动作已经有了就忽略(唯一索引兜着)。返回新增条数。"""
    init_db()
    now = time.time()
    n = 0
    with transaction() as conn:
        for r in rows:
            cur = conn.execute(
                "INSERT OR IGNORE INTO outbound_actions (id, day, slot, aweme_id, sec_uid, target,"
                " author, title, action, text, state, reason, created_at, started_at, done_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (r["id"], r["day"], r.get("slot") or "", r.get("aweme_id") or "",
                 r.get("sec_uid") or "",
                 r.get("target") or r.get("aweme_id") or r.get("sec_uid") or "",
                 r.get("author") or "", r.get("title") or "", r["action"],
                 r.get("text") or "", r.get("state") or "planned", r.get("reason") or "",
                 r.get("created_at") or now, r.get("started_at"), r.get("done_at")))
            if cur.rowcount:
                n += 1
    return n


def get_outbound_action(action_id: str) -> dict[str, Any] | None:
    init_db()
    with connect() as conn:
        row = conn.execute("SELECT * FROM outbound_actions WHERE id = ?",
                           (action_id,)).fetchone()
    return dict(row) if row else None


def list_outbound_actions(*, day: str | None = None, actions: tuple[str, ...] = (),
                          states: tuple[str, ...] = (), target: str | None = None,
                          since: float | None = None, limit: int = 200) -> list[dict[str, Any]]:
    """新的在前。"""
    init_db()
    where, args = [], []
    if day:
        where.append("day = ?")
        args.append(day)
    if actions:
        where.append(f"action IN ({','.join('?' * len(actions))})")
        args.extend(actions)
    if states:
        where.append(f"state IN ({','.join('?' * len(states))})")
        args.extend(states)
    if target:
        where.append("target = ?")
        args.append(target)
    if since is not None:
        where.append("created_at >= ?")
        args.append(since)
    sql = "SELECT * FROM outbound_actions"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC LIMIT ?"
    args.append(limit)
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]


def update_outbound_action(action_id: str, **fields: Any) -> None:
    bad = set(fields) - _OUTBOUND_FIELDS
    if bad:
        raise ValueError(f"不能改的互动字段:{sorted(bad)}")
    if not fields:
        return
    init_db()
    sets = ", ".join(f"{k} = ?" for k in fields)
    with connect() as conn:
        conn.execute(f"UPDATE outbound_actions SET {sets} WHERE id = ?",
                     (*fields.values(), action_id))


def cas_outbound_state(action_id: str, expected: tuple[str, ...], new: str,
                       **fields: Any) -> bool:
    """状态从 expected 之一改成 new;已经被别人改走就返回 False(抢不到就说明有人在做)。"""
    bad = set(fields) - _OUTBOUND_FIELDS
    if bad:
        raise ValueError(f"不能改的互动字段:{sorted(bad)}")
    init_db()
    sets = "".join(f", {k} = ?" for k in fields)
    marks = ",".join("?" * len(expected))
    with connect() as conn:
        cur = conn.execute(
            f"UPDATE outbound_actions SET state = ?{sets} WHERE id = ? AND state IN ({marks})",
            (new, *fields.values(), action_id, *expected))
        return cur.rowcount > 0


def count_outbound(day: str, action: str | None = None,
                   states: tuple[str, ...] = ("done",)) -> int:
    """某天某动作做了多少条(默认只数成功的)。"""
    init_db()
    sql = "SELECT COUNT(*) FROM outbound_actions WHERE day = ?"
    args: list[Any] = [day]
    if action:
        sql += " AND action = ?"
        args.append(action)
    if states:
        sql += f" AND state IN ({','.join('?' * len(states))})"
        args.extend(states)
    with connect() as conn:
        return int(conn.execute(sql, args).fetchone()[0])


def reset_stuck_outbound(older_than: float, now: float | None = None) -> int:
    """卡在 doing 超过 `older_than` 秒的(进程崩了)退回 planned,好重排一次。返回改了几条。"""
    init_db()
    now = now or time.time()
    with transaction() as conn:
        cur = conn.execute(
            "UPDATE outbound_actions SET state = 'planned', started_at = NULL"
            " WHERE state = 'doing' AND COALESCE(started_at, created_at) <= ?",
            (now - older_than,))
        return cur.rowcount
