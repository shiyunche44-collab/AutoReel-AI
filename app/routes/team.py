"""AI 团队:待办、排期、角色动态、评论、偏好、对标。"""
from __future__ import annotations

import threading
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from .. import config, db
from ..categories import load_profile
from ..team import plan as team_plan
from ..team import views as team_views
from ..templating import templates

router = APIRouter()


# --------------------------------------------------------------------------
# AI 团队
# --------------------------------------------------------------------------
@router.get("/team", response_class=HTMLResponse)
def team_page(request: Request):
    """AI 团队:待我处理、选题排期、各角色动态。"""
    return templates.TemplateResponse(request, "team.html", {"request": request})


def _team_themes(category: str) -> list[dict[str, Any]]:
    profile = load_profile(category)
    out = []
    for name, spec in profile.themes.items():
        out.append({"name": name, "label": spec.label, "multi": spec.multi,
                    "requires_angle": spec.requires_angle,
                    "durations": {a: list(profile.theme_durations(spec, a)) for a in config.ASPECTS}})
    return out


@router.get("/api/team")
def team_overview():
    data = team_views.overview()
    data["themes"] = _team_themes(data["category"])
    return data


@router.post("/api/team/roles/{name}/run")
def team_run_role(name: str):
    """手动让某个角色立刻干一次活。后台跑,页面轮询看结果。"""
    from .. import team

    try:
        team.role(name)
    except KeyError as exc:
        raise HTTPException(404, "没有这个角色") from exc
    threading.Thread(target=team.run_role, args=(name, "manual"), name=f"team-manual-{name}",
                     daemon=True).start()
    return {"ok": True}


class TeamPause(BaseModel):
    paused: bool


@router.post("/api/team/pause")
def team_pause(payload: TeamPause):
    from .. import team

    team.set_paused(payload.paused)
    return {"paused": team.paused()}


class PlanPatch(BaseModel):
    subject: str | None = None
    angle: str | None = None
    theme: str | None = None
    aspect: str | None = None
    duration: int | None = None
    hook: str | None = None
    slot_at: float | None = None
    note: str | None = None


class PlanReject(BaseModel):
    note: str = ""


def _plan_call(fn, *args):
    try:
        return team_plan.view(fn(*args))
    except team_plan.PlanError as exc:
        raise HTTPException(400, str(exc)) from exc


def _wake_asset() -> None:
    """选题刚批:素材官马上看一眼要不要提前建池,不等下一个半小时。"""
    from .. import team

    threading.Thread(target=team.run_role, args=("asset", "approve"), name="team-asset-wake",
                     daemon=True).start()


@router.post("/api/team/plan/{item_id}/approve")
def team_plan_approve(item_id: str):
    out = _plan_call(team_plan.approve, item_id)
    _wake_asset()
    return out


@router.post("/api/team/plan/{item_id}/reject")
def team_plan_reject(item_id: str, payload: PlanReject):
    return _plan_call(team_plan.reject, item_id, payload.note)


@router.patch("/api/team/plan/{item_id}")
def team_plan_patch(item_id: str, payload: PlanPatch):
    return _plan_call(team_plan.patch, item_id, payload.model_dump(exclude_none=True))


@router.post("/api/team/plan/approve-all")
def team_plan_approve_all():
    n = team_plan.approve_all(config.TEAM_CATEGORY)
    if n:
        _wake_asset()
    return {"approved": n}


class CutApprove(BaseModel):
    account_ids: list[str]
    title: str = ""
    publish_at: float | None = None
    #: 分发官的衍生品发到哪些账号:{"note": [...], "clip1": [...]}
    pieces: dict[str, list[str]] = {}


@router.post("/api/team/plan/{item_id}/publish")
def team_cut_approve(item_id: str, payload: CutApprove):
    """成片闸通过:按账号建发布任务,定时发。"""
    from ..team import publisher

    try:
        out = publisher.approve_cut(item_id, payload.account_ids, title=payload.title,
                                    publish_at=payload.publish_at, pieces=payload.pieces)
    except team_plan.PlanError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {**out, "item": team_plan.view(out["item"])}


@router.get("/api/projects/{project_id}/repurpose/{name}")
def repurpose_file(project_id: str, name: str):
    """分发官的衍生品预览:笔记图片 note_NN.jpg、切片 clipN.mp4。"""
    from ..pipeline import repurpose

    if name not in repurpose.preview_names():
        raise HTTPException(400, "不支持的文件")
    path = repurpose.folder(project_id) / name
    if not path.is_file():
        raise HTTPException(404, "衍生品还没出")
    return FileResponse(path, media_type="video/mp4" if name.endswith(".mp4") else "image/jpeg")


@router.post("/api/team/plan/{item_id}/reject-cut")
def team_cut_reject(item_id: str, payload: PlanReject):
    from ..team import publisher

    return _plan_call(publisher.reject_cut, item_id, payload.note)


@router.post("/api/team/plan/{item_id}/retry-gate")
def team_retry_gate(item_id: str):
    """门禁被机器拦下、你已经在项目页改好:清掉拦截,制片下一轮重新过闸。"""
    from ..team import producer

    if not db.get_plan_item(item_id):
        raise HTTPException(404, "选题不存在")
    return team_plan.view(producer.retry_gate(item_id))


@router.post("/api/team/accounts/{account_id}/resume")
def team_account_resume(account_id: str):
    """合规审核官暂停的账号:你看过作品状态后手动恢复自动发布。"""
    if not db.resume_account(account_id):
        raise HTTPException(400, "这个账号没有被暂停")
    return {"ok": True}


class LearningStatus(BaseModel):
    status: str


@router.post("/api/team/learnings/{learning_id:path}/status")
def team_learning_status(learning_id: str, payload: LearningStatus):
    """打法手册:你觉得某条结论不对(比如那周是热点带的),停用它;停用的不回灌。"""
    if payload.status not in ("active", "off"):
        raise HTTPException(400, "status 只能是 active 或 off")
    if not db.set_learning_status(learning_id, payload.status):
        raise HTTPException(404, "没有这条结论")
    return {"ok": True}


@router.post("/api/team/tuning/{key}/reset")
def team_tuning_reset(key: str):
    """流程体检自动调过的参数:恢复默认(白名单里的才认)。"""
    from ..team import health

    if key not in health.TUNABLES:
        raise HTTPException(404, "没有这个参数")
    if not health.reset(key):
        raise HTTPException(404, "这个参数没调过")
    return {"ok": True}


# 评论 id 里带「/」「+」,一律放在请求体里传
class CommentEdit(BaseModel):
    id: str
    reply: str | None = None
    kind: str | None = None


class CommentDecide(BaseModel):
    ids: list[str]
    action: str


class CommentTopic(BaseModel):
    id: str
    pooled: bool


class AutoThanks(BaseModel):
    enabled: bool


@router.post("/api/team/comments/edit")
def team_comment_edit(payload: CommentEdit):
    """改拟好的回复 / 改分类。改完仍要批准才发。"""
    from ..team import engagement

    try:
        return engagement.edit(payload.id, reply=payload.reply, kind=payload.kind)
    except engagement.EngageError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/api/team/comments/decide")
def team_comment_decide(payload: CommentDecide):
    """批量审核:approve 批准(运营官下一轮发,或你点「现在发」)/ skip 不回 / reset 退回草稿。"""
    from ..team import engagement

    try:
        return {"changed": engagement.decide(payload.ids, payload.action)}
    except engagement.EngageError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/api/team/comments/topic")
def team_comment_topic(payload: CommentTopic):
    from ..team import engagement

    try:
        engagement.set_topic(payload.id, payload.pooled)
    except engagement.EngageError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"ok": True}


@router.post("/api/team/comments/auto-thanks")
def team_comment_auto_thanks(payload: AutoThanks):
    """「夸」类模板感谢的自动档:开了以后这类回复不等你批就发。默认关。"""
    from ..team import engagement

    db.set_team_setting(engagement.AUTO_THANKS_KEY, payload.enabled)
    return {"enabled": payload.enabled}


# --------------------------------------------------------------------------
# 活跃官:主动去别人作品下互动
# --------------------------------------------------------------------------
class ActiveToggle(BaseModel):
    enabled: bool


@router.get("/api/team/active")
def team_active_overview():
    """「活跃官」面板:今天的额度、已用多少、最近的留痕。"""
    from ..team import activator

    return activator.stats()


@router.post("/api/team/active/toggle")
def team_active_toggle(payload: ActiveToggle):
    """一键停 / 开。停了下一轮立刻不动手。"""
    from ..team import activator

    activator.set_enabled(payload.enabled)
    return {"enabled": activator.enabled()}


@router.post("/api/team/active/run")
def team_active_run():
    """手动让它现在干一次(和角色卡上的「立即运行」一个意思)。"""
    from .. import team

    try:
        team.role("activator")
    except KeyError as exc:
        raise HTTPException(404, "没有这个角色") from exc
    threading.Thread(target=team.run_role, args=("activator", "manual"),
                     name="team-manual-activator", daemon=True).start()
    return {"ok": True}


# --------------------------------------------------------------------------
# 你的审核偏好:复盘归纳的几条话(可改 / 停用 / 自己加)+ 通过率乘数开关
# --------------------------------------------------------------------------
class PreferenceIn(BaseModel):
    text: str


class PreferencePatch(BaseModel):
    text: str | None = None
    status: str | None = None


class PreferenceRate(BaseModel):
    enabled: bool


def _pref_call(fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    from ..team import preference

    return preference.overview()


@router.get("/api/team/preferences")
def team_preferences():
    from ..team import preference

    return preference.overview()


@router.post("/api/team/preferences")
def team_preference_add(payload: PreferenceIn):
    from ..team import preference

    return _pref_call(preference.add, payload.text)


@router.patch("/api/team/preferences/{pref_id}")
def team_preference_patch(pref_id: str, payload: PreferencePatch):
    from ..team import preference

    return _pref_call(preference.update, pref_id, text=payload.text, status=payload.status)


@router.delete("/api/team/preferences/{pref_id}")
def team_preference_delete(pref_id: str):
    from ..team import preference

    return _pref_call(preference.remove, pref_id)


@router.post("/api/team/preferences/rate")
def team_preference_rate(payload: PreferenceRate):
    from ..team import preference

    return _pref_call(db.set_team_setting, preference.RATE_KEY, payload.enabled)


class BenchAdd(BaseModel):
    target: str
    note: str = ""


class BenchToggle(BaseModel):
    enabled: bool


class EventAdd(BaseModel):
    day: str
    title: str
    kind: str = "other"
    car: str = ""


@router.post("/api/team/benchmark/accounts")
def team_bench_add(payload: BenchAdd):
    """加对标账号:主页链接直接记;分享短链会开一次无头浏览器解析(顺便抓一屏作品)。"""
    from ..team import benchmark

    try:
        return benchmark.add_account(payload.target, payload.note)
    except benchmark.BenchError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/api/team/benchmark/accounts/{sec_uid}/toggle")
def team_bench_toggle(sec_uid: str, payload: BenchToggle):
    from ..team import benchmark

    try:
        benchmark.set_enabled(sec_uid, payload.enabled)
    except benchmark.BenchError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"enabled": payload.enabled}


@router.delete("/api/team/benchmark/accounts/{sec_uid}")
def team_bench_remove(sec_uid: str):
    from ..team import benchmark

    benchmark.remove_account(sec_uid)
    return {"ok": True}


@router.post("/api/team/events")
def team_event_add(payload: EventAdd):
    """手动记一个行业事件(上市 / 发布会 / 车展 / OTA / 降价)。"""
    from ..team import events

    try:
        return events.add_manual(config.TEAM_CATEGORY, payload.day, payload.title,
                                 payload.kind, payload.car)
    except events.EventError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/api/team/events/{event_id}/dismiss")
def team_event_dismiss(event_id: str):
    from ..team import events

    try:
        events.dismiss(event_id)
    except events.EventError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"ok": True}


@router.post("/api/team/plan/{item_id}/start")
def team_plan_start(item_id: str):
    """不等开工时间,马上做。没批的先批。"""
    from ..team import producer

    item = db.get_plan_item(item_id)
    if not item:
        raise HTTPException(404, "选题不存在")
    if item["status"] == "proposed":
        team_plan.approve(item_id)
    project_id = producer.start(item_id)
    if not project_id:
        item = db.get_plan_item(item_id) or {}
        raise HTTPException(400, item.get("note") or "这条选题已经开工或不能开工")
    return {"project_id": project_id}
