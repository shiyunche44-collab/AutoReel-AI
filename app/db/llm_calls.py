"""模型调用账本 `llm_calls` 的读写。记录逻辑在 `app/llm/ledger.py`。"""
from __future__ import annotations

import time
from typing import Any

from .core import connect
from .schema import init_db

_COLUMNS = ("at", "kind", "provider", "model", "purpose", "project_id", "role", "system_hash",
            "prompt_chars", "prompt_tokens", "completion_tokens", "reasoning_tokens",
            "latency_ms", "ok", "finish_reason", "error")


def add_llm_call(row: dict[str, Any]) -> None:
    init_db()
    vals = [row.get(c) for c in _COLUMNS]
    with connect() as conn:
        conn.execute(f"INSERT INTO llm_calls ({', '.join(_COLUMNS)}) "
                     f"VALUES ({', '.join('?' * len(_COLUMNS))})", vals)


def llm_usage(since: float, group_by: str = "purpose") -> list[dict[str, Any]]:
    """按 `group_by`(purpose / model / kind / project_id / role / day)汇总调用。"""
    keys = {"purpose": "purpose", "model": "model", "kind": "kind", "project_id": "project_id",
            "role": "role", "day": "date(at, 'unixepoch', 'localtime')"}
    if group_by not in keys:
        raise ValueError(f"不支持按 {group_by} 汇总")
    expr = keys[group_by]
    init_db()
    with connect() as conn:
        cur = conn.execute(
            f"SELECT {expr} AS key, COUNT(*) AS calls, SUM(1 - ok) AS errors,"
            " SUM(COALESCE(prompt_tokens, 0)) AS prompt_tokens,"
            " SUM(COALESCE(completion_tokens, 0)) AS completion_tokens,"
            " SUM(COALESCE(reasoning_tokens, 0)) AS reasoning_tokens,"
            " ROUND(AVG(latency_ms)) AS avg_ms, MAX(latency_ms) AS max_ms"
            f" FROM llm_calls WHERE at >= ? GROUP BY {expr} ORDER BY calls DESC",
            (since,))
        return [dict(r) for r in cur.fetchall()]


def recent_llm_calls(limit: int = 50, *, project_id: str = "",
                     errors_only: bool = False) -> list[dict[str, Any]]:
    init_db()
    where, args = [], []
    if project_id:
        where.append("project_id = ?")
        args.append(project_id)
    if errors_only:
        where.append("ok = 0")
    sql = "SELECT * FROM llm_calls" + (" WHERE " + " AND ".join(where) if where else "")
    with connect() as conn:
        cur = conn.execute(sql + " ORDER BY id DESC LIMIT ?", (*args, limit))
        return [dict(r) for r in cur.fetchall()]


def prune_llm_calls(older_than_days: float = 90) -> int:
    init_db()
    with connect() as conn:
        cur = conn.execute("DELETE FROM llm_calls WHERE at < ?",
                           (time.time() - older_than_days * 86400,))
        return cur.rowcount
