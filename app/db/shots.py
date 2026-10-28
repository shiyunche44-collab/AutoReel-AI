"""镜头。"""
from __future__ import annotations

import logging
import time
from typing import Any

from .core import connect
from .schema import init_db

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# 镜头
# --------------------------------------------------------------------------
def upsert_shots(project_id: str, shots: list[dict[str, Any]]) -> None:
    init_db()
    now = time.time()
    payload = []
    for s in shots:
        item = dict(s)
        item.setdefault("project_id", project_id)
        item.setdefault("stage", "final")
        item.setdefault("status", "pending")
        item.setdefault("attempts", 0)
        item.setdefault("consistency", "ok")
        item.setdefault("chain_id", 0)
        item.setdefault("chain_pos", 0)
        item.setdefault("est_cost", 0.0)
        item["updated_at"] = now
        payload.append(item)
    with connect() as conn:
        conn.executemany(
            """INSERT INTO shots
               (project_id, idx, stage, status, model, provider_task_id, submitted_at,
                last_polled_at, draft_path, final_path, attempts, error, consistency,
                chain_id, chain_pos, est_cost)
               VALUES (:project_id, :idx, :stage, :status, :model, :provider_task_id,
                       :submitted_at, :last_polled_at, :draft_path, :final_path,
                       :attempts, :error, :consistency, :chain_id, :chain_pos, :est_cost)
               ON CONFLICT(project_id, idx, stage) DO UPDATE SET
                   status = excluded.status,
                   model = excluded.model,
                   provider_task_id = COALESCE(excluded.provider_task_id, shots.provider_task_id),
                   submitted_at = COALESCE(excluded.submitted_at, shots.submitted_at),
                   last_polled_at = excluded.last_polled_at,
                   draft_path = COALESCE(excluded.draft_path, shots.draft_path),
                   final_path = COALESCE(excluded.final_path, shots.final_path),
                   attempts = excluded.attempts,
                   error = excluded.error,
                   consistency = excluded.consistency,
                   chain_id = excluded.chain_id,
                   chain_pos = excluded.chain_pos,
                   est_cost = excluded.est_cost""",
            payload,
        )


def get_shots(project_id: str, stage: str | None = None) -> list[dict[str, Any]]:
    init_db()
    with connect() as conn:
        if stage:
            cur = conn.execute(
                "SELECT * FROM shots WHERE project_id = ? AND stage = ? ORDER BY idx",
                (project_id, stage),
            )
        else:
            cur = conn.execute(
                "SELECT * FROM shots WHERE project_id = ? ORDER BY stage, idx",
                (project_id,),
            )
        return [dict(r) for r in cur.fetchall()]


def update_shot(project_id: str, idx: int, stage: str, **fields: Any) -> None:
    if not fields:
        return
    init_db()
    assignments = ", ".join(f"{k} = :{k}" for k in fields)
    with connect() as conn:
        conn.execute(
            f"UPDATE shots SET {assignments} WHERE project_id = :pid AND idx = :idx AND stage = :stage",
            {"pid": project_id, "idx": idx, "stage": stage, **fields},
        )


def delete_shots(project_id: str, stage: str) -> None:
    init_db()
    with connect() as conn:
        conn.execute(
            "DELETE FROM shots WHERE project_id = ? AND stage = ?", (project_id, stage)
        )


def find_shot_by_task_id(task_id: str) -> dict[str, Any] | None:
    """按 provider task_id 反查镜头。

    ⚠️ **历史遗留**:解说轨的画面是本地渲染的,没有"提交给生成服务再轮询"
    这一步,`provider_task_id` 永远是空的。全项目无调用。
    `shots` 表的列保留(删列要重建表,对本地数据有风险),但这个函数已经
    没有意义了 —— 留着只会让人以为还有轮询器。
    """
    init_db()
    with connect() as conn:
        cur = conn.execute(
            "SELECT * FROM shots WHERE provider_task_id = ? LIMIT 1", (task_id,)
        )
        row = cur.fetchone()
    return dict(row) if row else None


def claim_pending_shots(project_id: str, stage: str, limit: int) -> list[dict[str, Any]]:
    """取出处于 pending 且未达重试上限的镜头,供调度器提交。

    ⚠️ **历史遗留**:同 `find_shot_by_task_id` —— 解说轨没有"提交"这一步。
    渲染的幂等现在由**项目锁**保证(同一项目同一时刻只有一个写入者),
    不再靠"provider_task_id 非空"这种标记。
    """
    init_db()
    with connect() as conn:
        cur = conn.execute(
            """SELECT * FROM shots
               WHERE project_id = ? AND stage = ? AND status = 'pending'
                 AND provider_task_id IS NULL
               ORDER BY chain_id, chain_pos, idx
               LIMIT ?""",
            (project_id, stage, limit),
        )
        return [dict(r) for r in cur.fetchall()]
