"""项目事件流。"""
from __future__ import annotations

import logging
import sqlite3
import time
from typing import Any

from .core import connect
from .schema import init_db

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# 事件
# --------------------------------------------------------------------------
def add_event(project_id: str, dst: str, src: str | None = None,
              reason: str = "", cost: float = 0.0,
              conn: sqlite3.Connection | None = None) -> None:
    """记一条事件。

    `conn` 不为空时用调用方给的连接(通常来自 `transaction()`),
    让"改状态 + 记事件"成为一个原子单元 —— 事件流是本项目唯一的
    排障依据,状态与事件不一致的裂缝比没有事件更误导人。
    """
    init_db()
    sql = (
        "INSERT INTO job_events (project_id, created_at, src, dst, reason, cost)"
        " VALUES (?, ?, ?, ?, ?, ?)"
    )
    args = (project_id, time.time(), src, dst, reason, cost)
    if conn is not None:
        conn.execute(sql, args)
        return
    with connect() as own:
        own.execute(sql, args)


def get_events(project_id: str, limit: int = 200) -> list[dict[str, Any]]:
    init_db()
    with connect() as conn:
        cur = conn.execute(
            "SELECT * FROM job_events WHERE project_id = ? ORDER BY id DESC LIMIT ?",
            (project_id, limit),
        )
        return [dict(r) for r in cur.fetchall()]
