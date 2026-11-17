"""持久化后台任务表 `jobs` 的读写。调度语义(重试、并发、租约续期)在 `app/jobs.py`。"""
from __future__ import annotations

import json
import time
from typing import Any

from .core import connect, transaction
from .schema import init_db

#: 还没结束的任务状态
ACTIVE_JOB_STATES = ("queued", "running")


def _row(r: Any) -> dict[str, Any]:
    d = dict(r)
    for k in ("payload", "result"):
        if d.get(k):
            try:
                d[k] = json.loads(d[k])
            except (TypeError, ValueError):
                pass
    return d


def enqueue_job(kind: str, key: str = "", payload: dict[str, Any] | None = None, *,
                max_attempts: int = 3, run_at: float = 0.0) -> int | None:
    """排一个任务。同 kind 同 key 已有排队 / 在跑的就不排,返回 None(靠唯一索引,跨进程也成立)。"""
    init_db()
    now = time.time()
    with connect() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO jobs (kind, key, payload, max_attempts, next_run_at,"
            " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (kind, key, json.dumps(payload or {}, ensure_ascii=False), max(1, max_attempts),
             run_at, now, now))
        return int(cur.lastrowid or 0) if cur.rowcount else None


def claim_job(owner: str, kinds: list[str], lease: float) -> dict[str, Any] | None:
    """领一个到点的任务(或租约已过期的在跑任务),记上租约。没有就返回 None。"""
    if not kinds:
        return None
    init_db()
    now = time.time()
    marks = ",".join("?" * len(kinds))
    with transaction() as conn:
        row = conn.execute(
            f"SELECT id FROM jobs WHERE kind IN ({marks}) AND ("
            " (state = 'queued' AND next_run_at <= ?)"
            " OR (state = 'running' AND lease_expires_at < ?))"
            " ORDER BY next_run_at, id LIMIT 1",
            (*kinds, now, now)).fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE jobs SET state = 'running', attempts = attempts + 1, lease_owner = ?,"
            " lease_expires_at = ?, started_at = ?, updated_at = ? WHERE id = ?",
            (owner, now + lease, now, now, row["id"]))
        got = conn.execute("SELECT * FROM jobs WHERE id = ?", (row["id"],)).fetchone()
        return _row(got)


def heartbeat_job(job_id: int, owner: str, lease: float) -> bool:
    """续租。返回 False = 租约已不归我(被接手了),调用方应尽快收手。"""
    init_db()
    now = time.time()
    with connect() as conn:
        cur = conn.execute(
            "UPDATE jobs SET lease_expires_at = ?, updated_at = ?"
            " WHERE id = ? AND lease_owner = ? AND state = 'running'",
            (now + lease, now, job_id, owner))
        return cur.rowcount > 0


def finish_job(job_id: int, owner: str, result: Any = None) -> bool:
    init_db()
    now = time.time()
    with connect() as conn:
        cur = conn.execute(
            "UPDATE jobs SET state = 'done', result = ?, error = '', finished_at = ?,"
            " updated_at = ?, lease_owner = '', lease_expires_at = 0"
            " WHERE id = ? AND lease_owner = ? AND state = 'running'",
            (json.dumps(result, ensure_ascii=False, default=str), now, now, job_id, owner))
        return cur.rowcount > 0


def fail_job(job_id: int, owner: str, error: str, retry_at: float | None = None) -> bool:
    """记失败。给 `retry_at` 就回到排队、到点再跑;不给就判死。"""
    init_db()
    now = time.time()
    with connect() as conn:
        if retry_at is not None:
            cur = conn.execute(
                "UPDATE jobs SET state = 'queued', error = ?, next_run_at = ?, updated_at = ?,"
                " lease_owner = '', lease_expires_at = 0"
                " WHERE id = ? AND lease_owner = ? AND state = 'running'",
                (error[:2000], retry_at, now, job_id, owner))
        else:
            cur = conn.execute(
                "UPDATE jobs SET state = 'failed', error = ?, finished_at = ?, updated_at = ?,"
                " lease_owner = '', lease_expires_at = 0"
                " WHERE id = ? AND lease_owner = ? AND state = 'running'",
                (error[:2000], now, now, job_id, owner))
        return cur.rowcount > 0


def requeue_orphan_jobs(owner: str) -> int:
    """服务启动时:别的(已死的)进程留下的 running 任务立刻放回排队。

    只能由拿到实例锁的进程调用 —— 有锁就说明没有别的执行器活着,不必等租约过期。
    这次中断不是任务自己的错,不算一次尝试。
    """
    init_db()
    now = time.time()
    with connect() as conn:
        cur = conn.execute(
            "UPDATE jobs SET state = 'queued', attempts = MAX(attempts - 1, 0),"
            " lease_owner = '', lease_expires_at = 0, next_run_at = ?, updated_at = ?"
            " WHERE state = 'running' AND lease_owner != ?",
            (now, now, owner))
        return cur.rowcount


def active_job(kind: str, key: str = "") -> dict[str, Any] | None:
    """这个 kind + key 正在排队 / 在跑的任务。"""
    init_db()
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM jobs WHERE kind = ? AND key = ? AND state IN ('queued', 'running')"
            " LIMIT 1", (kind, key)).fetchone()
        return _row(row) if row else None


def latest_job(kind: str, key: str = "") -> dict[str, Any] | None:
    init_db()
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM jobs WHERE kind = ? AND key = ? ORDER BY id DESC LIMIT 1",
            (kind, key)).fetchone()
        return _row(row) if row else None


def list_jobs(states: tuple[str, ...] = ACTIVE_JOB_STATES, limit: int = 100) -> list[dict]:
    init_db()
    marks = ",".join("?" * len(states))
    with connect() as conn:
        cur = conn.execute(
            f"SELECT * FROM jobs WHERE state IN ({marks}) ORDER BY id LIMIT ?",
            (*states, limit))
        return [_row(r) for r in cur.fetchall()]


def prune_jobs(older_than: float) -> int:
    """删掉早就结束的任务(done / failed 且结束早于 `older_than`)。"""
    init_db()
    with connect() as conn:
        cur = conn.execute(
            "DELETE FROM jobs WHERE state IN ('done', 'failed') AND finished_at < ?",
            (older_than,))
        return cur.rowcount
