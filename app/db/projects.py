"""项目与配额账本。"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from typing import Any

from .core import connect
from .schema import init_db

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# 项目
# --------------------------------------------------------------------------
def create_project(row: dict[str, Any], conn: sqlite3.Connection | None = None) -> None:
    """插入项目行。

    `conn` 不为空时用调用方给的连接(通常来自 `transaction()`),
    这样"插项目 + 记事件"可以是一个原子单元。
    """
    init_db()
    row = {"angle": "", "aspect": "16:9", "variant": {}, **row}
    subjects = row.get("subjects") or []
    row["subjects"] = subjects if isinstance(subjects, str) else json.dumps(subjects, ensure_ascii=False)
    if not isinstance(row["variant"], str):
        row["variant"] = json.dumps(row["variant"], ensure_ascii=False)
    sql = (
        """INSERT INTO projects
           (id, title, subject, category, theme, angle, variant, subjects, duration, shot_count, narration_mode,
            aspect, state, state_reason, quota_budget, spent_cost, est_cost, created_at, updated_at)
           VALUES (:id, :title, :subject, :category, :theme, :angle, :variant, :subjects, :duration, :shot_count, :narration_mode,
                   :aspect, :state, :state_reason, :quota_budget, :spent_cost, :est_cost,
                   :created_at, :updated_at)"""
    )
    if conn is not None:
        conn.execute(sql, row)
        return
    with connect() as own:
        own.execute(sql, row)


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


def recent_variants(category: str, limit: int = 4) -> list[dict[str, Any]]:
    """同品类最近建的几个项目的变体(新的在前),建项目挑变体时用来避开重复。"""
    init_db()
    with connect() as conn:
        cur = conn.execute(
            "SELECT variant FROM projects WHERE category = ? ORDER BY created_at DESC LIMIT ?",
            (category, limit))
        rows = [r[0] for r in cur.fetchall()]
    out = []
    for raw in rows:
        try:
            value = json.loads(raw or "{}")
        except (TypeError, ValueError):
            value = {}
        out.append(value if isinstance(value, dict) else {})
    return out


def recent_themes(category: str, limit: int = 2) -> list[str]:
    """同品类最近建的几个项目的片型(新的在前)。"""
    init_db()
    with connect() as conn:
        cur = conn.execute(
            "SELECT theme FROM projects WHERE category = ? ORDER BY created_at DESC LIMIT ?",
            (category, limit))
        return [str(r[0] or "") for r in cur.fetchall()]


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


#: `projects` 表里允许 `update_project` 改的列。
#:
#: 之前 SET 子句是用 f-string 直接拼 kwargs 的键。当前所有调用点的键都
#: 是内部字面量,**没有注入面**;但这是一张等着被人顺手塞进用户输入
#: 的表 —— 白名单把这条路堵死,代价只是一份常量。
_PROJECT_COLUMNS = frozenset({
    "title", "subject", "category", "theme", "angle", "variant", "thesis", "subjects",
    "duration", "shot_count", "narration_mode", "state", "state_reason",
    "quota_budget", "spent_cost", "reserved_cost", "resume_state", "est_cost",
    "created_at", "updated_at",
})


def update_project(project_id: str, *, conn: sqlite3.Connection | None = None,
                   **fields: Any) -> None:
    """改项目行。`conn` 不为空时用调用方给的连接(见 `transaction()`)。"""
    if not fields:
        return
    init_db()
    unknown = set(fields) - _PROJECT_COLUMNS
    if unknown:
        raise ValueError(f"未知列:{'、'.join(sorted(unknown))}")
    fields["updated_at"] = time.time()
    assignments = ", ".join(f"{k} = :{k}" for k in fields)
    sql = f"UPDATE projects SET {assignments} WHERE id = :id"
    args = {"id": project_id, **fields}
    if conn is not None:
        conn.execute(sql, args)
        return
    with connect() as own:
        own.execute(sql, args)


def cas_state(project_id: str, expected: str, new: str, *,
              conn: sqlite3.Connection | None = None,
              reason: str | None = None) -> bool:
    """状态机的 CAS 写:只有当前状态还是 `expected` 时才改成 `new`。

    返回 True = 改成了;False = 状态已经被别人推进了,调用方应当放弃
    本轮并重读状态。

    为什么需要:原来的 `_set_state` 是"读出来 → 判断转移合法 → 无条件
    UPDATE",中间有个 TOCTOU 窗口。两个执行流(后台调度器 + 用户手动
    推进)都读到 DRAFT_RENDERING 时,会各自开渲同一批拍,写同一个文件。
    把"判断"和"写"合成一条 SQL 就没有这个窗口了。
    """
    init_db()
    now = time.time()
    if reason is None:
        sql = ("UPDATE projects SET state = :new, updated_at = :now "
               "WHERE id = :id AND state = :expected")
    else:
        sql = ("UPDATE projects SET state = :new, state_reason = :reason, updated_at = :now "
               "WHERE id = :id AND state = :expected")
    args = {"id": project_id, "expected": expected, "new": new,
            "reason": reason, "now": now}
    if conn is not None:
        cur = conn.execute(sql, args)
        return cur.rowcount > 0
    with connect() as own:
        cur = own.execute(sql, args)
        return cur.rowcount > 0


# --------------------------------------------------------------------------
# 配额账本:预留 → 结算
# --------------------------------------------------------------------------
def reserve_spend(project_id: str, amount: float) -> bool:
    """预留额度。只有"已花费 + 在途预留 + 本次预留"不超过上限时才成功。

    真实花费只有调用完才知道,所以调用**之前**先把预估额度挂上:

    - 不挂就直接看已花费 —— 一次调用就能把额度冲破;
    - 挂了才知道"这个项目已经花到哪、还能不能再花",多个在途调用也
      不会各自都觉得"我还够"。

    返回 False 表示超限(调用方据此中止,不该真的去调)。
    """
    init_db()
    now = time.time()
    sql = (
        "UPDATE projects SET reserved_cost = reserved_cost + :amount, updated_at = :now "
        "WHERE id = :id AND (spent_cost + reserved_cost + :amount) <= quota_budget"
    )
    with connect() as conn:
        cur = conn.execute(sql, {"id": project_id, "amount": max(0.0, float(amount)),
                                  "now": now})
        return cur.rowcount > 0


def commit_spend(project_id: str, reserved: float, actual: float) -> None:
    """结算:释放预留,加上实际花费。"""
    init_db()
    now = time.time()
    sql = (
        "UPDATE projects SET spent_cost = spent_cost + :actual, "
        "reserved_cost = MAX(0, reserved_cost - :reserved), updated_at = :now "
        "WHERE id = :id"
    )
    with connect() as conn:
        conn.execute(sql, {"id": project_id, "reserved": max(0.0, float(reserved)),
                           "actual": max(0.0, float(actual)), "now": now})


def release_spend(project_id: str, reserved: float) -> None:
    """调用失败:释放预留,不记账。"""
    init_db()
    now = time.time()
    sql = ("UPDATE projects SET reserved_cost = MAX(0, reserved_cost - :reserved), "
           "updated_at = :now WHERE id = :id")
    with connect() as conn:
        conn.execute(sql, {"id": project_id, "reserved": max(0.0, float(reserved)),
                           "now": now})
