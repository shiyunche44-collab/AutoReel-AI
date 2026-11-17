"""降级记录:质量打了折、但流程照样往下走的路径,每走一次记一行(`degradations` 表)。

README 的设计原则之一是"失败就降级,不卡住流程":概念动画渲不出退回数据卡、统一风格
封面做不出退回老封面、某个事实源挂了就跳过……这些路径不报错、片子照样出,所以
**只写日志就等于看不见**:日志按天轮转,没人能回答"这周有几拍概念动画变成了数据卡"。

    degrade.note("concept.to_card", f"第 {idx} 拍 {scene}:{exc}")

- `kind` 用 `对象.去向` 的短名,稳定不变(按它汇总);`detail` 放这一次的原因。
- 项目从模型调用账本的上下文标签里取(`orchestrator.advance` 已挂上),不用层层传。
- 记录失败绝不影响流程本身。汇总看 `/api/degradations`,`/api/health` 带最近 24 小时。
"""
from __future__ import annotations

import logging
import time
from typing import Any

from . import db
from .llm import ledger

log = logging.getLogger(__name__)


def note(kind: str, detail: str = "", *, project_id: str = "") -> None:
    try:
        pid = project_id or ledger.current_tags().get("project_id", "")
        db.init_db()
        with db.connect() as conn:
            conn.execute("INSERT INTO degradations (at, kind, detail, project_id)"
                         " VALUES (?, ?, ?, ?)", (time.time(), kind, str(detail)[:500], pid))
    except Exception:  # noqa: BLE001 - 记录失败不能影响降级本身
        log.debug("记降级失败", exc_info=True)


def summary(since: float) -> list[dict[str, Any]]:
    """按 kind 汇总:次数、涉及几个项目、最近一次的时间和原因。"""
    db.init_db()
    with db.connect() as conn:
        cur = conn.execute(
            "SELECT kind, COUNT(*) AS n, COUNT(DISTINCT NULLIF(project_id, '')) AS projects,"
            " MAX(at) AS last_at,"
            " (SELECT detail FROM degradations d2 WHERE d2.kind = d.kind"
            "  ORDER BY d2.id DESC LIMIT 1) AS last_detail"
            " FROM degradations d WHERE at >= ? GROUP BY kind ORDER BY n DESC", (since,))
        return [dict(r) for r in cur.fetchall()]


def recent(limit: int = 50, *, kind: str = "", project_id: str = "") -> list[dict[str, Any]]:
    db.init_db()
    where, args = [], []
    if kind:
        where.append("kind = ?")
        args.append(kind)
    if project_id:
        where.append("project_id = ?")
        args.append(project_id)
    sql = "SELECT * FROM degradations" + (" WHERE " + " AND ".join(where) if where else "")
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(sql + " ORDER BY id DESC LIMIT ?", (*args, limit))]


def prune(older_than_days: float = 90) -> int:
    db.init_db()
    with db.connect() as conn:
        return conn.execute("DELETE FROM degradations WHERE at < ?",
                            (time.time() - older_than_days * 86400,)).rowcount
