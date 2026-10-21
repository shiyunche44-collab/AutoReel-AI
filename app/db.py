"""SQLite 持久化。

分工原则:
- **可编辑的创作内容**(facts / script / storyboard)存 JSON 文件,方便人工查看与修改。
- **运行时状态**(项目元信息、镜头状态、provider task_id、事件流)存 SQLite,
  因为它们是并发写入、需要事务、且断点续跑依赖它们。

WAL 模式 + 每次调用独立连接,适配 FastAPI 的线程池。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from . import config

_SCHEMA_LOCK = threading.Lock()
_INITIALISED = False

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id              TEXT PRIMARY KEY,
    title           TEXT NOT NULL,
    subject         TEXT NOT NULL,
    category        TEXT NOT NULL DEFAULT 'auto_long',
    theme           TEXT NOT NULL DEFAULT '',
    angle           TEXT NOT NULL DEFAULT '',
    subjects        TEXT NOT NULL DEFAULT '[]',
    duration        INTEGER NOT NULL,
    shot_count      INTEGER NOT NULL DEFAULT 0,
    narration_mode  TEXT NOT NULL DEFAULT 'tts',
    state           TEXT NOT NULL,
    state_reason    TEXT,
    quota_budget    REAL NOT NULL DEFAULT 40,
    spent_cost      REAL NOT NULL DEFAULT 0,
    est_cost        REAL NOT NULL DEFAULT 0,
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS shots (
    project_id       TEXT NOT NULL,
    idx              INTEGER NOT NULL,
    stage            TEXT NOT NULL DEFAULT 'final',
    status           TEXT NOT NULL DEFAULT 'pending',
    model            TEXT DEFAULT '',
    provider_task_id TEXT,
    submitted_at     REAL,
    last_polled_at   REAL,
    draft_path       TEXT,
    final_path       TEXT,
    attempts         INTEGER NOT NULL DEFAULT 0,
    error            TEXT,
    consistency      TEXT NOT NULL DEFAULT 'ok',
    chain_id         INTEGER NOT NULL DEFAULT 0,
    chain_pos        INTEGER NOT NULL DEFAULT 0,
    est_cost         REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (project_id, idx, stage),
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_shots_task ON shots(provider_task_id);
CREATE INDEX IF NOT EXISTS idx_shots_status ON shots(project_id, stage, status);

CREATE TABLE IF NOT EXISTS job_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  TEXT NOT NULL,
    created_at  REAL NOT NULL,
    src         TEXT,
    dst         TEXT NOT NULL,
    reason      TEXT DEFAULT '',
    cost        REAL NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_events_project ON job_events(project_id, id);
"""


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    """打开一个带 WAL 的连接,退出时自动提交/回滚。"""
    config.ensure_dirs()
    conn = sqlite3.connect(config.DATA_DIR / "aimovice.db", timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        yield conn
    finally:
        conn.close()


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    """返回某张表已存在的列名集合。"""
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _migrate(conn: sqlite3.Connection) -> None:
    """给旧库补上后加的列。

    `SCHEMA` 全是 `CREATE TABLE IF NOT EXISTS`,它**不会**给已存在的表补列 ——
    本项目此前也没有任何迁移机制。新装机器上建表是完整的,老机器上的
    aimovice.db 则停留在建表那一刻。因此每加一列都必须在这里显式补。
    """
    if "theme" not in _column_names(conn, "projects"):
        conn.execute("ALTER TABLE projects ADD COLUMN theme TEXT NOT NULL DEFAULT ''")
    if "angle" not in _column_names(conn, "projects"):
        conn.execute("ALTER TABLE projects ADD COLUMN angle TEXT NOT NULL DEFAULT ''")
    if "subjects" not in _column_names(conn, "projects"):
        conn.execute("ALTER TABLE projects ADD COLUMN subjects TEXT NOT NULL DEFAULT '[]'")


def init_db() -> None:
    """建表 + 迁移。幂等,可重复调用。"""
    global _INITIALISED
    with _SCHEMA_LOCK:
        if _INITIALISED:
            return
        config.ensure_dirs()
        conn = sqlite3.connect(config.DATA_DIR / "aimovice.db", timeout=30)
        try:
            conn.executescript(SCHEMA)
            _migrate(conn)
            conn.commit()
        finally:
            conn.close()
        _INITIALISED = True


# --------------------------------------------------------------------------
# JSON 创作内容读写
# --------------------------------------------------------------------------
def _json_path(project_id: str, name: str) -> Path:
    return config.project_dir(project_id) / name


def write_json(project_id: str, name: str, payload: Any) -> Path:
    path = _json_path(project_id, name)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return path


def read_json(project_id: str, name: str, default: Any = None) -> Any:
    path = _json_path(project_id, name)
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


# --------------------------------------------------------------------------
# 项目
# --------------------------------------------------------------------------
def create_project(row: dict[str, Any]) -> None:
    init_db()
    row = {"angle": "", **row}
    subjects = row.get("subjects") or []
    row["subjects"] = subjects if isinstance(subjects, str) else json.dumps(subjects, ensure_ascii=False)
    with connect() as conn:
        conn.execute(
            """INSERT INTO projects
               (id, title, subject, category, theme, angle, subjects, duration, shot_count, narration_mode,
                state, state_reason, quota_budget, spent_cost, est_cost, created_at, updated_at)
               VALUES (:id, :title, :subject, :category, :theme, :angle, :subjects, :duration, :shot_count, :narration_mode,
                       :state, :state_reason, :quota_budget, :spent_cost, :est_cost,
                       :created_at, :updated_at)""",
            row,
        )


def get_project(project_id: str) -> dict[str, Any] | None:
    init_db()
    with connect() as conn:
        cur = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,))
        row = cur.fetchone()
    return dict(row) if row else None


def list_projects(limit: int = 100) -> list[dict[str, Any]]:
    init_db()
    with connect() as conn:
        cur = conn.execute(
            "SELECT * FROM projects ORDER BY updated_at DESC LIMIT ?", (limit,)
        )
        return [dict(r) for r in cur.fetchall()]


def project_subjects(row: dict[str, Any] | None) -> list[dict[str, str]]:
    """项目行里的横评车单 -> [{name, group}]。单车项目 / 旧库 / 坏数据都返回 []。"""
    raw = (row or {}).get("subjects") or "[]"
    try:
        items = json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, TypeError):
        return []
    out = []
    for item in items or []:
        if isinstance(item, dict) and str(item.get("name") or "").strip():
            out.append({"name": str(item["name"]).strip(), "group": str(item.get("group") or "").strip()})
    return out


def update_project(project_id: str, **fields: Any) -> None:
    if not fields:
        return
    init_db()
    fields["updated_at"] = time.time()
    assignments = ", ".join(f"{k} = :{k}" for k in fields)
    with connect() as conn:
        conn.execute(
            f"UPDATE projects SET {assignments} WHERE id = :id",
            {"id": project_id, **fields},
        )


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
    """按 provider task_id 反查镜头 —— 轮询器定位任务用。"""
    init_db()
    with connect() as conn:
        cur = conn.execute(
            "SELECT * FROM shots WHERE provider_task_id = ? LIMIT 1", (task_id,)
        )
        row = cur.fetchone()
    return dict(row) if row else None


def claim_pending_shots(project_id: str, stage: str, limit: int) -> list[dict[str, Any]]:
    """取出处于 pending 且未达重试上限的镜头,供调度器提交。

    这是"幂等"的关键:已提交(provider_task_id 非空)或已成功的镜头不会被重复领取。
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


# --------------------------------------------------------------------------
# 事件
# --------------------------------------------------------------------------
def add_event(project_id: str, dst: str, src: str | None = None,
              reason: str = "", cost: float = 0.0) -> None:
    init_db()
    with connect() as conn:
        conn.execute(
            "INSERT INTO job_events (project_id, created_at, src, dst, reason, cost)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (project_id, time.time(), src, dst, reason, cost),
        )


def get_events(project_id: str, limit: int = 200) -> list[dict[str, Any]]:
    init_db()
    with connect() as conn:
        cur = conn.execute(
            "SELECT * FROM job_events WHERE project_id = ? ORDER BY id DESC LIMIT ?",
            (project_id, limit),
        )
        return [dict(r) for r in cur.fetchall()]
