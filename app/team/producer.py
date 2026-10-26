"""制片:已批选题到开工时间就建项目;项目停在门禁时替人过闸;跟踪出片进度。

**开工时间 = 发布时刻 - TEAM_LEAD_HOURS。** 事实别抓太早(热点角度会变味),
也要给出片和成片审核留出时间。

**只碰门禁,不碰渲染。** 渲染态由服务里原有的 Scheduler 推进,这里不调
advance —— 两个执行流同时推进同一个项目以前出过事(成片损坏)。

**自动过闸 = AI 审稿员。** 门禁 A 走 `confirm_gate` 原路(它自己会跑 lint 和
节奏拦截线、旁白质检,有问题就抛错不放行),**从不 force**;门禁 B 先查每拍都
渲出来了。被拦下就在选题上记一笔 `hold`,停下等人 —— 人在项目页处理完、
项目状态变了,hold 自动清掉,流程接着走。成片出来后停在「成片待审」,
那是给人的最后一道闸。
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
_GATES = {str(State.GATE_A), str(State.GATE_B)}

#: 正在过闸的项目(门禁 A 要合成旁白,一跑几分钟,别重复起线程)
_gating: set[str] = set()
_gating_lock = threading.Lock()


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


def cut_problems(project_id: str) -> list[str]:
    """门禁 B 的机器检查:分镜里每一拍(时长 > 0)都有渲染成功的草稿。"""
    specs = db.read_json(project_id, "storyboard.json", []) or []
    shots = {r["idx"]: r for r in db.get_shots(project_id, "draft")}
    bad = [int(s.get("idx", i)) for i, s in enumerate(specs)
           if float(s.get("duration") or 0) > 0
           and (shots.get(int(s.get("idx", i))) or {}).get("status") != "succeeded"]
    if not specs:
        return ["没有分镜"]
    return [f"第 {', '.join(str(i + 1) for i in bad[:8])} 拍没渲染成功"] if bad else []


def _hold(item_id: str, state: str, reason: str) -> None:
    db.update_plan_item(item_id, hold=reason[:300], hold_state=state)
    # hold 处理完就清掉了;记一条事件,复盘官的流程体检才数得出门禁最常拦什么
    pid = (db.get_plan_item(item_id) or {}).get("project_id")
    if pid:
        db.add_event(pid, "team", src=state, reason=f"拦下:{reason[:300]}")
    log.info("plan %s held at %s: %s", item_id, state, reason)


def pass_gate(item: dict[str, Any], state: str) -> None:
    """替人确认门禁。拦下就记 hold。"""
    from ..models import GateRequest
    from ..pipeline import locks, orchestrator

    pid = item["project_id"]
    gate = "A" if state == str(State.GATE_A) else "B"
    try:
        if gate == "B":
            problems = cut_problems(pid)
            if problems:
                _hold(item["id"], state, "门禁 B 没放行:" + ";".join(problems))
                return
        out = orchestrator.confirm_gate(pid, GateRequest(gate=gate))
    except locks.ProjectBusy:
        return  # 别的执行流正在推进,下一轮再看
    except Exception as exc:  # noqa: BLE001 - lint 拦截 / 旁白失败 / 状态变了:都交给人
        msg = str(exc)
        _hold(item["id"], state, msg if msg.startswith("门禁") else f"门禁 {gate} 没放行:{msg}")
        return
    db.add_event(pid, "team", src=state, reason=f"AI 审稿员自动过门禁 {gate}")
    if out.get("state") == str(State.PAUSED_BUDGET):
        _hold(item["id"], str(State.PAUSED_BUDGET), "额度用完了,提额后会自动继续")


def _gate_async(item: dict[str, Any], state: str, background: bool) -> bool:
    pid = item["project_id"]
    with _gating_lock:
        if pid in _gating:
            return False
        _gating.add(pid)

    def _run() -> None:
        try:
            pass_gate(item, state)
        finally:
            with _gating_lock:
                _gating.discard(pid)

    if background:
        threading.Thread(target=_run, name=f"team-gate-{pid}", daemon=True).start()
    else:
        _run()
    return True


def sync(*, background: bool = True) -> int:
    """制作中的选题跟着项目状态走,停在门禁的替人过闸。返回动了几条。"""
    changed = 0
    for item in db.list_plan_items(statuses=("producing",), limit=200):
        project = db.get_project(item["project_id"]) if item["project_id"] else None
        state = project.get("state") if project else str(State.CANCELLED)
        if state in _DONE:
            db.update_plan_item(item["id"], status="review", hold="", hold_state="")
            changed += 1
            continue
        if state in _FAILED:
            reason = (project or {}).get("state_reason") or "项目已删除"
            db.update_plan_item(item["id"], status="failed", note=str(reason)[:200])
            changed += 1
            continue
        if item.get("hold"):
            if item.get("hold_state") == state:
                continue  # 还卡在原地,等人
            db.update_plan_item(item["id"], hold="", hold_state="")  # 人处理过了
        if state in _GATES and config.TEAM_AUTO_GATES:
            if _gate_async(item, state, background):
                changed += 1
    return changed


def retry_gate(item_id: str) -> dict[str, Any]:
    """人看过拦截原因、改好了:清掉 hold,下一轮重新试着过闸。"""
    db.update_plan_item(item_id, hold="", hold_state="")
    return db.get_plan_item(item_id) or {}


def run(now: float | None = None) -> str:
    started = [pid for pid in (start(i["id"]) for i in due(now)) if pid]
    changed = sync()
    if not started and not changed:
        raise Skip("没有到开工时间的选题")
    parts = []
    if started:
        parts.append(f"开工 {len(started)} 条:{'、'.join(started)}")
    if changed:
        parts.append(f"{changed} 条选题推进 / 状态更新")
    return ",".join(parts)
