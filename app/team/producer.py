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

import json
import logging
import threading
import time
from typing import Any

from .. import config, db
from ..state import State
from . import Skip, chief
from .plan import to_payload

log = logging.getLogger(__name__)

#: 项目到了这些状态,选题就算出片 / 失败
_DONE = {str(State.DONE)}
_FAILED = {str(State.FAILED), str(State.CANCELLED)}
_GATES = {str(State.GATE_A), str(State.GATE_B)}

#: 正在过闸的项目(门禁 A 要合成旁白,一跑几分钟,别重复起线程)
_gating: set[str] = set()
#: 正在跑成片机审的项目
_reviewing: set[str] = set()
_gating_lock = threading.Lock()


def due(now: float | None = None) -> list[dict[str, Any]]:
    now = now or time.time()
    # 返工的题不等开工时间:上一版已经做过一遍,事实不会变味,拖着只会断更
    return [i for i in db.list_plan_items(statuses=("approved",), limit=200)
            if i["slot_at"] - config.TEAM_LEAD_HOURS * 3600 <= now
            or int(i.get("revision") or 0) > 0]


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
    if item.get("exclude"):
        from ..pipeline.explainer import EXCLUDE_FILE

        db.write_json(project_id, EXCLUDE_FILE, list(item["exclude"]))
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
    item = db.get_plan_item(item_id) or {}
    db.update_plan_item(item_id, hold=reason[:300], hold_state=state,
                        hold_count=int(item.get("hold_count") or 0) + 1, hold_at=time.time())
    # hold 处理完就清掉了;记一条事件,复盘官的流程体检才数得出门禁最常拦什么
    pid = item.get("project_id")
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


def machine_review(item: dict[str, Any]) -> dict[str, Any]:
    """成片机审:视觉模型把成片看一遍(`pipeline.cut_review`),结论记在选题上。

    通过(或机审没看完)→ 进成片待审,卡片带上结论,终审只需要复核;
    不通过 → 按机审指出的时间段打回重做(受返工上限约束),不占终审的时间。"""
    from ..pipeline import cut_review, orchestrator
    from . import publisher

    report = cut_review.review_project(item["project_id"], llm=orchestrator._llm())
    brief = {k: report.get(k) for k in ("passed", "inconclusive", "summary", "issues",
                                         "bad_ranges", "notes", "checked_at", "vlm_calls")}
    brief["errors"] = (report.get("errors") or [])[:3]
    if not db.cas_plan_status(item["id"], ("producing",), "review", hold="", hold_state="",
                              machine_review=json.dumps(brief, ensure_ascii=False)):
        return report  # 这期间有人动过这条选题
    if not report.get("passed") and not report.get("inconclusive"):
        try:
            publisher.reject_cut(item["id"], str(report.get("summary") or "机审不通过"),
                                 redo=True, bad_ranges=report.get("bad_ranges") or None)
        except Exception as exc:  # noqa: BLE001 - 打回不成就留在待审,终审能看到机审结论
            log.warning("机审打回失败(%s):%s", item["id"], exc)
    return report


def _review_async(item: dict[str, Any], background: bool) -> int:
    pid = item["project_id"]
    with _gating_lock:
        if pid in _reviewing:
            return 0
        _reviewing.add(pid)

    def _run() -> None:
        try:
            machine_review(item)
        except Exception as exc:  # noqa: BLE001 - 机审自己坏了不能把片子卡在制作中
            log.warning("成片机审出错(%s),直接进待审:%s", pid, exc)
            db.cas_plan_status(item["id"], ("producing",), "review", hold="", hold_state="")
        finally:
            with _gating_lock:
                _reviewing.discard(pid)

    if background:
        threading.Thread(target=_run, name=f"team-review-{pid}", daemon=True).start()
    else:
        _run()
    return 1


def sync(*, background: bool = True) -> int:
    """制作中的选题跟着项目状态走,停在门禁的替人过闸。返回动了几条。"""
    changed = 0
    for item in db.list_plan_items(statuses=("producing",), limit=200):
        project = db.get_project(item["project_id"]) if item["project_id"] else None
        state = project.get("state") if project else str(State.CANCELLED)
        if state in _DONE:
            if config.TEAM_MACHINE_REVIEW:
                # 成片先过机审(几分钟,后台跑):通过才进待审,不通过直接打回重做
                changed += _review_async(item, background)
            else:
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
                verdict = _stuck(item, state)
                if verdict == "wait":
                    continue  # 还卡在原地,等人(或等下一次自动重试)
                if verdict == "fail":
                    # 同一道门禁连续两次没过:这一版救不回来,记失败,接力按原因重开
                    db.update_plan_item(item["id"], status="failed", hold="", hold_state="",
                                        note=("门禁连续被拦:" + str(item["hold"]))[:200])
                    changed += 1
                    continue
                db.update_plan_item(item["id"], hold="", hold_state="")  # 自动再试一次
            else:
                # 人处理过了
                db.update_plan_item(item["id"], hold="", hold_state="", hold_count=0)
        if state in _GATES and config.TEAM_AUTO_GATES:
            if chief.render_busy():
                continue  # 总管:渲染名额占满,放闸会排队抢资源,下一轮再看
            if _gate_async(item, state, background):
                changed += 1
    return changed


def _stuck(item: dict[str, Any], state: str) -> str:
    """卡在门禁的选题怎么办:"wait" 等着 / "retry" 再试一次 / "fail" 放弃这一版。

    原来一律等人,人不在就断更(2026-11 上旬 9 天断了 3 天,门禁 A 卡住占其中一部分)。
    第一次被拦隔 `TEAM_HOLD_RETRY_MINUTES` 再试(旁白合成、素材池常常只是当时没好);
    第二次还被拦就不再试。额度暂停不算在内,提额后它自己会继续。"""
    if not config.TEAM_AUTO_RELAY or state not in _GATES:
        return "wait"
    if time.time() - float(item.get("hold_at") or 0) < config.TEAM_HOLD_RETRY_MINUTES * 60:
        return "wait"
    return "retry" if int(item.get("hold_count") or 0) < 2 else "fail"


#: 只接力这么久以内失败的选题:更早的是历史,不该被翻出来重做
RELAY_WINDOW = 36 * 3600


def relay(now: float | None = None) -> int:
    """失败接力:近期失败(项目失败 / 门禁连续被拦)的选题自动重开一条已批选题。
    返工上限由 `publisher.redo` 管(到顶就放弃);发布失败不在这里,归发布员。"""
    if not config.TEAM_AUTO_RELAY:
        return 0
    from . import publisher
    from .plan import PlanError

    now = now or time.time()
    n = 0
    for item in db.list_plan_items(statuses=("failed",), limit=500):
        if now - float(item.get("updated_at") or 0) > RELAY_WINDOW:
            continue
        if str(item.get("note") or "").startswith("发布失败"):
            continue
        try:
            out = publisher.redo(item["id"])
        except PlanError:
            continue  # 已经重做过
        n += 1
        log.info("plan %s relayed -> %s", item["id"], out.get("redo_item") or "abandoned")
    return n


def retry_gate(item_id: str) -> dict[str, Any]:
    """人看过拦截原因、改好了:清掉 hold,下一轮重新试着过闸。"""
    db.update_plan_item(item_id, hold="", hold_state="")
    return db.get_plan_item(item_id) or {}


def run(now: float | None = None) -> str:
    ready, later = chief.admit(due(now), now)  # 日产量上限:超了的顺延到明天
    started = [pid for pid in (start(i["id"]) for i in ready) if pid]
    changed = sync() + relay(now)
    if not started and not changed:
        raise Skip(f"总管:今天产能已满,{len(later)} 条顺延" if later else "没有到开工时间的选题")
    parts = []
    if started:
        parts.append(f"开工 {len(started)} 条:{'、'.join(started)}")
    if changed:
        parts.append(f"{changed} 条选题推进 / 状态更新")
    if later:
        parts.append(f"总管顺延 {len(later)} 条到明天")
    return ",".join(parts)
