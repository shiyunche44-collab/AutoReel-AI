"""制片:已批选题到开工时间就建项目,推进到脚本门禁;跟踪出片进度。

**开工时间 = 发布时刻 - TEAM_LEAD_HOURS。** 事实别抓太早(热点角度会变味),
也要给出片和成片审核留出时间。

**只推进到门禁 A。** 之后的渲染由服务里原有的 Scheduler 负责,这里不碰 ——
两个执行流同时推进同一个项目以前出过事(成片损坏)。
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any

from .. import config, db
from ..state import State
from . import Skip
from .plan import to_payload

log = logging.getLogger(__name__)

#: 项目到了这些状态,选题就算出片 / 失败
_DONE = {str(State.DONE)}
_FAILED = {str(State.FAILED), str(State.CANCELLED)}


def due(now: float | None = None) -> list[dict[str, Any]]:
    now = now or time.time()
    return [i for i in db.list_plan_items(statuses=("approved",), limit=200)
            if i["slot_at"] - config.TEAM_LEAD_HOURS * 3600 <= now]


def _bootstrap(project_id: str) -> None:
    """同 main._bootstrap:后台推进到门禁 A,零生成消耗。"""
    from ..pipeline import orchestrator

    try:
        orchestrator.run_until_waiting(project_id, max_steps=6)
    except Exception as exc:  # noqa: BLE001
        db.update_project(project_id, state=str(State.FAILED), state_reason=str(exc))


def start(item_id: str, *, background: bool = True) -> str:
    """建项目。抢不到(别处已开工)返回空串。"""
    from ..pipeline import orchestrator

    if not db.cas_plan_status(item_id, ("approved",), "producing"):
        return ""
    item = db.get_plan_item(item_id)
    try:
        project_id = orchestrator.create_project(to_payload(item))
    except Exception as exc:  # noqa: BLE001 - 参数不合法 / 配置问题:记在选题上给人看
        db.update_plan_item(item_id, status="failed", note=f"建项目失败:{exc}"[:200])
        return ""
    db.update_plan_item(item_id, project_id=project_id)
    db.add_event(project_id, "team", reason=f"AI 团队按选题单开工({item_id})")
    if background:
        threading.Thread(target=_bootstrap, args=(project_id,), name=f"team-{project_id}",
                         daemon=True).start()
    else:
        _bootstrap(project_id)
    return project_id


def sync() -> int:
    """制作中的选题跟着项目状态走。返回变了几条。"""
    changed = 0
    for item in db.list_plan_items(statuses=("producing",), limit=200):
        project = db.get_project(item["project_id"]) if item["project_id"] else None
        state = project.get("state") if project else str(State.CANCELLED)
        if state in _DONE:
            db.update_plan_item(item["id"], status="ready")
            changed += 1
        elif state in _FAILED:
            reason = (project or {}).get("state_reason") or "项目已删除"
            db.update_plan_item(item["id"], status="failed", note=str(reason)[:200])
            changed += 1
    return changed


def run(now: float | None = None) -> str:
    started = [pid for pid in (start(i["id"]) for i in due(now)) if pid]
    changed = sync()
    if not started and not changed:
        raise Skip("没有到开工时间的选题")
    parts = []
    if started:
        parts.append(f"开工 {len(started)} 条:{'、'.join(started)}")
    if changed:
        parts.append(f"{changed} 条选题状态更新")
    return ",".join(parts)
