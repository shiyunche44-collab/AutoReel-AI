"""发布记录。"""
from __future__ import annotations

import logging
import time
from typing import Any

from .core import connect
from .schema import init_db

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# 发布记录
# --------------------------------------------------------------------------
def create_publication(row: dict[str, Any]) -> None:
    init_db()
    now = time.time()
    row = {
        "aspect": "16:9", "artifact": "", "title": "", "description": "",
        "tags": "[]", "variant": "{}", "piece": "", "state": "pending", "remote_id": "", "remote_url": "",
        "upload_id": "", "error": "", "attempts": 0, "published_at": None, "scheduled_at": None,
        **row,
    }
    row.setdefault("created_at", now)
    row["updated_at"] = now
    with connect() as conn:
        conn.execute(
            """INSERT INTO publications
               (id, project_id, account_id, platform, aspect, artifact, title, description,
                tags, variant, piece, state, remote_id, remote_url, upload_id, error, attempts,
                published_at, scheduled_at, created_at, updated_at)
               VALUES (:id, :project_id, :account_id, :platform, :aspect, :artifact, :title,
                       :description, :tags, :variant, :piece, :state, :remote_id, :remote_url, :upload_id, :error,
                       :attempts, :published_at, :scheduled_at, :created_at, :updated_at)""",
            row,
        )


def get_publication(pub_id: str) -> dict[str, Any] | None:
    init_db()
    with connect() as conn:
        cur = conn.execute("SELECT * FROM publications WHERE id = ?", (pub_id,))
        row = cur.fetchone()
    return dict(row) if row else None


def list_publications(*, state: str | None = None, project_id: str | None = None,
                      limit: int = 200) -> list[dict[str, Any]]:
    init_db()
    clauses, params = [], []
    if state:
        clauses.append("state = ?")
        params.append(state)
    if project_id:
        clauses.append("project_id = ?")
        params.append(project_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)
    with connect() as conn:
        cur = conn.execute(
            f"SELECT * FROM publications {where} ORDER BY created_at DESC LIMIT ?", params
        )
        return [dict(r) for r in cur.fetchall()]


def update_publication(pub_id: str, **fields: Any) -> None:
    if not fields:
        return
    init_db()
    fields["updated_at"] = time.time()
    assignments = ", ".join(f"{k} = :{k}" for k in fields)
    with connect() as conn:
        conn.execute(
            f"UPDATE publications SET {assignments} WHERE id = :id",
            {"id": pub_id, **fields},
        )


def find_publication(project_id: str, account_id: str, aspect: str,
                     piece: str = "") -> dict[str, Any] | None:
    """幂等查询:同一项目 + 同一账号 + 同一画幅 + 同一件只应有一条记录。"""
    init_db()
    with connect() as conn:
        cur = conn.execute(
            "SELECT * FROM publications WHERE project_id = ? AND account_id = ? AND aspect = ?"
            " AND piece = ?",
            (project_id, account_id, aspect, piece),
        )
        row = cur.fetchone()
    return dict(row) if row else None


def cas_publication_state(pub_id: str, from_states: tuple[str, ...], to_state: str,
                         **fields: Any) -> bool:
    """把发布任务的状态**原子地**从 from_states 改到 to_state。抢到返回 True。

    发布是"一条片子只能发一次"的事:调度线程和手动重试可能同时盯上同一条任务,
    两边都传一遍就会在账号里留下两条一样的作品。所以抢占必须是数据库里的一条
    带条件的 UPDATE,不能是「先读后写」。

    抢不到(别人已经在传)返回 False,调用方直接跳过。
    """
    init_db()
    if not from_states:
        return False
    fields["state"] = to_state
    fields["updated_at"] = time.time()
    assignments = ", ".join(f"{k} = :{k}" for k in fields)
    where = "state IN (" + ", ".join(f":from_{i}" for i in range(len(from_states))) + ")"
    params: dict[str, Any] = {**fields, "id": pub_id}
    for i, st in enumerate(from_states):
        params[f"from_{i}"] = st
    with connect() as conn:
        cur = conn.execute(
            f"UPDATE publications SET {assignments} WHERE id = :id AND {where}", params)
        return bool(cur.rowcount)


def claim_publications(limit: int = 3, now: float | None = None) -> list[dict[str, Any]]:
    """取出待推进的发布任务。定时发布的没到点不领,被合规审核官暂停的账号不领。

    **只领 `pending`。** `uploading` 是「已经有人在上传了」,再领一次就会
    同一个账号并发传两遍 —— 实跑踩过:一条片子在抖音上留下 4 条一样的作品。
    卡住很久的 `uploading` 任务由 `advance` 自己的重试逻辑处理,不由这里领。

    **同一账号一次只给一条** —— 单账号高频上传会触发平台风控,这是硬要求。
    """
    init_db()
    with connect() as conn:
        rows = conn.execute(
            """SELECT * FROM publications
               WHERE state = 'pending'
                 AND (scheduled_at IS NULL OR scheduled_at <= ?)
                 AND account_id NOT IN (SELECT account_id FROM account_health WHERE paused = 1)
               ORDER BY created_at
               LIMIT ?""",
            (time.time() if now is None else now, limit * 4),
        ).fetchall()
    out: list[dict[str, Any]] = []
    busy: set[str] = set()
    for row in rows:
        account_id = row["account_id"]
        if account_id in busy:
            continue
        busy.add(account_id)
        out.append(dict(row))
        if len(out) >= limit:
            break
    return out


def publications_for_metrics(limit: int = 200) -> list[dict[str, Any]]:
    """需要抓数据的作品:已提交(待审)与已发布。"""
    init_db()
    with connect() as conn:
        cur = conn.execute(
            """SELECT * FROM publications
               WHERE state IN ('submitted', 'published') AND remote_id != ''
               ORDER BY published_at DESC LIMIT ?""",
            (limit,),
        )
        return [dict(r) for r in cur.fetchall()]
