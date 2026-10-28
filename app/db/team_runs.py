"""AI 团队:角色运行记录、账号健康、团队设置。"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

from .core import connect, transaction
from .schema import init_db

log = logging.getLogger(__name__)


def start_team_run(role: str, trigger: str = "schedule", stale_after: float = 3600) -> int | None:
    """登记一次角色运行。同角色已有一次在跑(且没超时)就返回 None。

    用数据库而不是进程内锁:两个服务连同一个库时(以前出过事),
    同一角色也只会跑一份。
    """
    init_db()
    now = time.time()
    with transaction() as conn:
        busy = conn.execute(
            "SELECT 1 FROM team_runs WHERE role = ? AND status = 'running' AND started_at > ?",
            (role, now - stale_after)).fetchone()
        if busy:
            return None
        cur = conn.execute("INSERT INTO team_runs (role, started_at, status, trigger)"
                           " VALUES (?, ?, 'running', ?)", (role, now, trigger))
        return int(cur.lastrowid)


def finish_team_run(run_id: int, status: str, summary: str = "") -> None:
    init_db()
    with connect() as conn:
        conn.execute("UPDATE team_runs SET finished_at = ?, status = ?, summary = ? WHERE id = ?",
                     (time.time(), status, summary[:500], run_id))


def list_team_runs(role: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    init_db()
    with connect() as conn:
        if role:
            cur = conn.execute("SELECT * FROM team_runs WHERE role = ? ORDER BY id DESC LIMIT ?",
                               (role, limit))
        else:
            cur = conn.execute("SELECT * FROM team_runs ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in cur.fetchall()]


def last_team_run(role: str, *, scheduled_only: bool = False) -> dict[str, Any] | None:
    init_db()
    sql = "SELECT * FROM team_runs WHERE role = ?"
    if scheduled_only:
        sql += " AND trigger = 'schedule'"
    with connect() as conn:
        row = conn.execute(sql + " ORDER BY id DESC LIMIT 1", (role,)).fetchone()
    return dict(row) if row else None


def get_account_health(account_id: str) -> dict[str, Any] | None:
    init_db()
    with connect() as conn:
        row = conn.execute("SELECT * FROM account_health WHERE account_id = ?",
                           (account_id,)).fetchone()
    return dict(row) if row else None


def list_account_health() -> list[dict[str, Any]]:
    init_db()
    with connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM account_health ORDER BY updated_at DESC").fetchall()]


def pause_account(account_id: str, reason: str) -> None:
    """暂停一个账号的自动发布:发布线程不再领它的任务(已排的留着,恢复后照发)。"""
    init_db()
    now = time.time()
    with connect() as conn:
        conn.execute(
            "INSERT INTO account_health (account_id, paused, reason, paused_at, updated_at)"
            " VALUES (?, 1, ?, ?, ?) ON CONFLICT(account_id) DO UPDATE SET paused = 1,"
            " reason = excluded.reason, paused_at = excluded.paused_at, updated_at = excluded.updated_at",
            (account_id, reason[:300], now, now))


def resume_account(account_id: str) -> bool:
    init_db()
    with connect() as conn:
        cur = conn.execute("UPDATE account_health SET paused = 0, updated_at = ? WHERE account_id = ?"
                           " AND paused = 1", (time.time(), account_id))
        return cur.rowcount > 0



def get_team_setting(key: str, default: Any = None) -> Any:
    init_db()
    with connect() as conn:
        row = conn.execute("SELECT value FROM team_settings WHERE key = ?", (key,)).fetchone()
    if not row:
        return default
    try:
        return json.loads(row["value"])
    except ValueError:
        return default


def set_team_setting(key: str, value: Any) -> None:
    init_db()
    with connect() as conn:
        conn.execute("INSERT INTO team_settings (key, value) VALUES (?, ?)"
                     " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                     (key, json.dumps(value, ensure_ascii=False)))


def prune_idle_runs(role: str) -> None:
    """轮询型角色(制片每 5 分钟看一眼)大多数时候没事可做:只留最近一条空转记录。"""
    init_db()
    with connect() as conn:
        conn.execute(
            "DELETE FROM team_runs WHERE role = ? AND status = 'idle' AND id < "
            "(SELECT MAX(id) FROM team_runs WHERE role = ? AND status = 'idle')",
            (role, role))
