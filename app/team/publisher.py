"""发布员:成片闸 + 定时发布。

成片出来后选题停在「成片待审」(review)。**在人点「通过」之前,发布队列里
什么都没有** —— 不靠一个审核状态字段去拦已经建好的发布任务,而是根本不建,
误发的路径就不存在。

通过前合规审核官出一份审核单(`compliance.checklist`),有红项不能通过。
通过时按账号建发布任务(`publish.service.enqueue`,同项目同账号幂等),
写上 `scheduled_at`;发布线程到点才领(`db.claim_publications`)。发布时刻
默认是选题排期的时刻,已经过了就顺延到下一个发布时段。
"""
from __future__ import annotations

import datetime as dt
import logging
import threading
import time
import uuid
from typing import Any

from .. import config, db
from . import Skip, chief, compliance, preference, repurpose
from .plan import PlanError

log = logging.getLogger(__name__)

#: 发布时刻离现在至少留这么久(审完马上发也给发布线程一点余量)
MIN_AHEAD = 120.0
#: 返工重做一版大约要多久(实测海豹07 返工 18:05 打回、19:00 进待审),排发布时留出来
REDO_PREP_HOURS = 2.0
#: 这些发布状态算发出去了
_OUT = {"published", "submitted"}


def next_publish_time(slot_at: float, now: float | None = None,
                      times: tuple[str, ...] | None = None) -> float:
    """排期时刻还没过就用它,过了就顺延到下一个发布时段(TEAM_SLOT_TIMES)。"""
    now = time.time() if now is None else now
    if slot_at >= now + MIN_AHEAD:
        return slot_at
    times = times or config.TEAM_SLOT_TIMES
    base = dt.datetime.fromtimestamp(now + MIN_AHEAD)
    for ahead in range(0, 3):
        day = base.date() + dt.timedelta(days=ahead)
        for hhmm in sorted(times):
            h, m = (int(x) for x in hhmm.split(":"))
            at = dt.datetime.combine(day, dt.time(h, m))
            if at >= base:
                return at.timestamp()
    return now + MIN_AHEAD


def accounts() -> list[dict[str, Any]]:
    """能发的账号,带「默认勾选」标记:上次通过时选的;没选过就默认抖音。"""
    from ..publish import accounts as accounts_mod

    saved = db.get_team_setting("publish_accounts", None)
    out = []
    for a in accounts_mod.list_accounts(only_enabled=True):
        view = accounts_mod.account_view(a)
        health = db.get_account_health(a.id) or {}
        paused = bool(health.get("paused"))
        default = a.id in saved if isinstance(saved, list) else a.platform == "douyin"
        out.append({"id": a.id, "platform": a.platform, "platform_label": view["platform_label"],
                    "name": a.name, "default": default and not paused, "paused": paused,
                    "paused_reason": health.get("reason", "") if paused else ""})
    return out


def _titles(item: dict[str, Any]) -> tuple[str, list[str]]:
    """(默认发布标题, 候选标题):包装里选中的那条,没有就用片名。"""
    from ..pipeline import packaging

    pid = item["project_id"]
    project = db.get_project(pid) or {}
    pkg = None
    try:
        pkg = packaging.load(pid)
    except Exception as exc:  # noqa: BLE001 - 包装坏了就只用项目标题
        log.warning("包装坏了就只用项目标题: %s", exc)
    titles = [t.text for t in (pkg.titles if pkg else []) if getattr(t, "text", "")]
    try:
        picked = packaging.publish_choice(pid).get("title") or ""
    except Exception:  # noqa: BLE001
        picked = ""
    return picked or (titles[0] if titles else project.get("title") or item["subject"]), titles


def review_view(item: dict[str, Any]) -> dict[str, Any]:
    """成片闸卡片要的:成片、封面、候选标题、审核单、发布时刻。"""
    from ..pipeline import aspect

    pid = item["project_id"]
    project = db.get_project(pid) or {}
    title, titles = _titles(item)
    video = aspect.output_path(pid)
    return {
        "project_title": project.get("title") or item["subject"],
        "title": title,
        "titles": titles,
        "checklist": compliance.checklist(pid, title),
        "has_video": video.is_file(),
        "video_url": f"/api/projects/{pid}/download",
        "cover_url": f"/api/projects/{pid}/cover",
        "publish_at": next_publish_time(item["slot_at"]),
        "pieces": repurpose.view(item),
    }


def approve_cut(item_id: str, account_ids: list[str], *, title: str = "",
                publish_at: float | None = None,
                pieces: dict[str, list[str]] | None = None) -> dict[str, Any]:
    """成片闸通过:建发布任务,定时发。`pieces` = 分发官的衍生品发到哪些账号 {note/clip1: [账号]}。"""
    from ..publish import service as publish_service

    item = db.get_plan_item(item_id)
    if not item:
        raise PlanError("选题不存在")
    if item["status"] != "review":
        raise PlanError("这条选题不在成片待审")
    if not account_ids:
        raise PlanError("至少选一个发布账号")
    stopped = [aid for aid in account_ids if (db.get_account_health(aid) or {}).get("paused")]
    if stopped:
        raise PlanError("选的账号里有被合规审核官暂停的,先去恢复或换一个账号")
    # 审核单按最终要发的标题再查一遍:页面上改过标题,红项可能是新的
    default_title = _titles(item)[0]
    final = title.strip() or default_title
    sheet = compliance.checklist(item["project_id"], final)
    if sheet["red"]:
        raise PlanError("审核单有红项,不能发:" + compliance.red_summary(sheet))
    at = next_publish_time(float(publish_at or item["slot_at"]))
    # 总管:同账号发布别撞车(快反不挪)。人手改的时间也一视同仁,免得单账号短时间连发
    at = chief.free_publish_time(at, account_ids, rapid=item.get("kind") == "rapid",
                                 subject=item.get("subject") or "")
    # 衍生品有问题就整单不过,别主片排上了它没排
    repurpose.approve(item, pieces or {}, at, dry_run=True)
    if not db.cas_plan_status(item_id, ("review",), "scheduled", publish_at=at):
        raise PlanError("这条选题刚被别处处理过")
    from ..publish import accounts as accounts_mod

    overrides: dict[str, dict[str, Any]] = {}
    if title.strip():
        for aid in account_ids:
            acc = accounts_mod.get_account(aid)
            if acc:
                overrides[acc.platform] = {"title": title.strip()}
    try:
        out = publish_service.enqueue(item["project_id"], account_ids=account_ids,
                                      overrides=overrides)
    except Exception as exc:  # noqa: BLE001 - 建不了任务就退回待审,别卡在「已排」
        db.update_plan_item(item_id, status="review", publish_at=None)
        raise PlanError(f"建发布任务失败:{exc}") from exc
    if not out["created"] and not out["skipped"]:
        db.update_plan_item(item_id, status="review", publish_at=None)
        raise PlanError("选的账号都不可用")
    for row in out["created"]:
        db.update_publication(row["id"], scheduled_at=at)
    extras = repurpose.approve(item, pieces or {}, at)
    db.set_team_setting("publish_accounts", list(account_ids))
    changed = final != default_title
    preference.record(item, "cut", "approve", title_from=default_title if changed else "",
                      title_to=final if changed else "")
    when = dt.datetime.fromtimestamp(at).strftime("%m-%d %H:%M")
    db.add_event(item["project_id"], "team", reason=f"成片审核通过,定时 {when} 发布")
    return {"item": db.get_plan_item(item_id), "created": len(out["created"]),
            "skipped": len(out["skipped"]), "warnings": out.get("warnings") or [],
            "publish_at": at, "pieces": extras}


def reject_cut(item_id: str, note: str = "", *, redo: bool = False,
               bad_ranges: list[list[float]] | None = None) -> dict[str, Any]:
    """成片打回。`bad_ranges` = 成片里有问题的时间段 [[起, 止], ...](秒),重做时这些时间段
    用到的素材整条排除、新一版不再取。`redo=True`:按打回意见重做 —— 原选题记为已驳回,另开一条同题的新选题,
    直接是「已批」(选题闸早就过了),带上意见和返工次数,制作员按排期重新开工。

    返工有上限(`TEAM_MAX_REVISIONS`):到顶了只打回、不再重做,等人看。"""
    if not db.cas_plan_status(item_id, ("review",), "rejected",
                              note=("成片打回:" + note)[:200]):
        raise PlanError("这条选题不在成片待审")
    item = db.get_plan_item(item_id) or {}
    preference.record(item, "cut", "reject", note=note)
    if redo:
        child = _redo_item(item, note, bad_ranges)
        if child:
            item = {**item, "redo_item": child}
        else:
            abandon(item_id, note)
            item = {**(db.get_plan_item(item_id) or item), "redo_item": "",
                    "redo_stopped": f"已返工 {item.get('revision', 0)} 次,到上限了"}
    return item


def bad_materials(item: dict[str, Any], ranges: list[list[float]] | None) -> set[str]:
    """成片里问题时间段用到的素材:返回源地址和素材 id,新一版据此排除。
    时间轴按分镜时长顺序累加(和成片一致,开场片头另算的偏差忽略不计)。"""
    pid = item.get("project_id")
    if not pid or not ranges:
        return set()
    from ..pipeline import library

    starts, t = {}, 0.0
    for spec in db.read_json(pid, "storyboard.json", []) or []:
        dur = float(spec.get("duration") or 0)
        starts[int(spec.get("idx", len(starts)))] = (t, t + dur)
        t += dur
    bad_beats = {idx for idx, (a, b) in starts.items()
                 for lo, hi in ((float(r[0]), float(r[1])) for r in ranges if len(r) == 2)
                 if a < hi and b > lo}
    out: set[str] = set()
    pieces = (db.read_json(pid, "materials_used.json", {}) or {}).get("pieces") or []
    for p in pieces:
        if int(p.get("beat", -1)) in bad_beats:
            if p.get("source_url"):
                out.add(str(p["source_url"]))
            if p.get("item_id") and p["item_id"] != "bg":
                out.add(library.base_item_id(str(p["item_id"])))
    return out


def _redo_item(item: dict[str, Any], note: str,
               bad_ranges: list[list[float]] | None = None) -> str:
    """开返工选题,返回新选题 id;到返工上限返回空串。"""
    revision = int(item.get("revision") or 0) + 1
    if revision > config.TEAM_MAX_REVISIONS:
        return ""
    new_id = "pl_" + uuid.uuid4().hex[:12]
    keep = ("category", "slot_at", "kind", "subject", "subjects", "angle", "theme", "aspect",
            "duration", "hook", "search_terms", "evidence", "rationale", "heat")
    db.create_plan_item({
        **{k: item[k] for k in keep}, "id": new_id, "status": "approved",
        "slot_at": _redo_slot(item),
        "revision": revision, "redo_of": item["id"], "feedback": note.strip()[:300],
        "exclude": sorted(set(item.get("exclude") or []) | bad_materials(item, bad_ranges)),
        "note": f"第 {revision} 次返工(上一版 {item['id']} 被打回)",
    })
    return new_id


def _redo_slot(item: dict[str, Any]) -> float:
    """返工排到最近一个来得及的发布时段,不等下一个「空档」。

    原来沿用原时刻、来不及就取主编的下一个空档:空档被待批选题占着时会排到 5–6 天后
    (深蓝S05 返工排到 11-15、元UP 排到 11-13),当天就断更。返工的题早过了选题闸,
    制作员也不等开工时间(`producer.due`),所以只要留出重做的时间就行。
    和别的片撞时刻由 `chief.free_publish_time` 在排发布时错开。"""
    now = time.time()
    ready = now + REDO_PREP_HOURS * 3600
    soonest = next_publish_time(0.0, now=ready - MIN_AHEAD)
    at = float(item["slot_at"])
    return at if ready <= at <= soonest else soonest


def abandon(item_id: str, note: str = "") -> None:
    """返工到上限:这题放弃。不占发布空档,主编下一轮会补新题。"""
    item = db.get_plan_item(item_id) or {}
    db.update_plan_item(item_id, status="abandoned",
                        note=(f"已返工 {item.get('revision', 0)} 次仍不过,放弃。" + note)[:200])


def redo(item_id: str, note: str = "") -> dict[str, Any]:
    """失败 / 驳回 / 过期的选题按原因重开一条(同样受返工上限);到上限就放弃。
    给「定时 Claude 任务」和页面的「重做」按钮用:这三个状态原来是死胡同。"""
    item = db.get_plan_item(item_id)
    if not item:
        raise PlanError("选题不存在")
    if item["status"] not in ("failed", "rejected", "expired"):
        raise PlanError("只有失败 / 驳回 / 过期的选题能重做")
    if any(i.get("redo_of") == item_id for i in db.list_plan_items(limit=500)):
        raise PlanError("这条已经重做过了")
    reason = (note or item.get("note") or "").strip()
    child = _redo_item(item, reason or "上一版没走完,重做")
    if not child:
        abandon(item_id, reason)
        return {**(db.get_plan_item(item_id) or {}), "redo_item": "",
                "redo_stopped": f"已返工 {item.get('revision', 0)} 次,放弃这题"}
    return {**item, "redo_item": child}


def _refine_item(item_id: str) -> dict[str, Any]:
    if not config.OPENFILM_ENABLED:
        raise PlanError("精修没开启(OPENFILM_ENABLED=1 才有)")
    item = db.get_plan_item(item_id)
    if not item:
        raise PlanError("选题不存在")
    if item["status"] != "review":
        raise PlanError("这条选题不在成片待审,不能精修")
    return item


def refine_open(item_id: str) -> dict[str, Any]:
    """精修:导出成片到 Studio 项目并起 Studio。上次改的字幕还没应用就不重导,接着改。"""
    from ..pipeline import refine

    item = _refine_item(item_id)
    pid = item["project_id"]
    try:
        try:
            pending = refine.read_edits(pid)
        except refine.RefineError:
            pending = None
        if pending is None or not pending.any:
            refine.export(pid)
        return {"url": refine.studio_url(pid), "pending": len(pending.changed) if pending else 0}
    except refine.RefineError as exc:
        raise PlanError(str(exc)) from exc


def refine_apply(item_id: str) -> dict[str, Any]:
    """应用精修:改了几句字幕、重出成片。记进偏好(你常改哪类字幕)。耗时 = 一次完整合成。"""
    from ..pipeline import refine

    item = _refine_item(item_id)
    try:
        out = refine.apply(item["project_id"])
    except refine.RefineError as exc:
        raise PlanError(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 —— 含 ProjectBusy:别人正占着项目
        raise PlanError(str(exc)) from exc
    if out["changed"]:
        note = ";".join(f"「{e['old']}」→「{e['new']}」" for e in out["edits"])
        preference.record(item, "cut", "refine", note=note)
    return out


#: 后台应用精修的任务状态,按选题 id 存(只在内存里:服务重启就丢,成片本身不受影响)
_refine_jobs: dict[str, dict[str, Any]] = {}
_refine_lock = threading.Lock()


def refine_apply_start(item_id: str) -> dict[str, Any]:
    """后台应用精修:立刻返回,页面轮询 `refine_status`。同一条选题同一时刻只跑一个。"""
    _refine_item(item_id)  # 开关 / 选题状态的错在这里就抛,不用等到后台
    with _refine_lock:
        if (_refine_jobs.get(item_id) or {}).get("state") == "running":
            return dict(_refine_jobs[item_id])
        _refine_jobs[item_id] = {"state": "running", "started_at": time.time()}

    def _run() -> None:
        try:
            result = refine_apply(item_id)
            job = {"state": "done", "result": result}
        except Exception as exc:  # noqa: BLE001 - 失败原因交给页面显示
            job = {"state": "failed", "error": str(exc)}
        with _refine_lock:
            _refine_jobs[item_id] = {**_refine_jobs[item_id], **job, "finished_at": time.time()}

    threading.Thread(target=_run, name=f"refine-{item_id}", daemon=True).start()
    return dict(_refine_jobs[item_id])


def refine_status(item_id: str) -> dict[str, Any]:
    with _refine_lock:
        return dict(_refine_jobs.get(item_id) or {"state": "idle"})


#: 错误信息里有这些词才算网络问题(登录失效、审核不过、验证码都不该自动重发)
_NETWORK_HINTS = ("超时", "timeout", "timed out", "网络", "net::", "connection", "连接")
_NO_RETRY_HINTS = ("登录", "验证", "审核")
_RETRIED_KEY = "publish_auto_retried"


def _requeue_network_failures(pubs: list[dict[str, Any]]) -> bool:
    """发布因网络失败的任务自动重排一次(每条任务只一次)。重排了返回 True。

    只把任务放回 pending 让发布线程自己领,不在这里直接上传 ——
    两个执行流同时传同一条片子出过事(同一片子发了 4 条)。"""
    if not config.TEAM_AUTO_RELAY:
        return False
    done = db.get_team_setting(_RETRIED_KEY, None)
    done = list(done) if isinstance(done, list) else []
    failed = [p for p in pubs if p["state"] == "failed"]
    if not failed:
        return False
    for p in failed:
        err = str(p.get("error") or "").lower()
        if p["id"] in done or any(w in err for w in _NO_RETRY_HINTS) \
                or not any(w in err for w in _NETWORK_HINTS):
            return False
    for p in failed:
        db.update_publication(p["id"], state="pending", error="", attempts=0)
        db.add_event(p["project_id"], "team", reason="发布网络失败,自动重排一次")
        done.append(p["id"])
    db.set_team_setting(_RETRIED_KEY, done[-200:])
    return True


def sync() -> int:
    """已排发布的选题跟着发布任务走:都发出去 → 已发布;有失败且没有在途的 → 失败。"""
    changed = 0
    # 「失败」的也看:人在发布页手动重试发出去了,选题要跟着回到已发布
    for item in db.list_plan_items(statuses=("scheduled", "failed"), limit=200):
        if not item.get("project_id"):
            continue
        # 只看主片:衍生品晚几天发、失败了也不该让选题算「发布失败」
        pubs = [p for p in db.list_publications(project_id=item["project_id"]) if repurpose.is_main(p)]
        if not pubs:
            continue
        states = {p["state"] for p in pubs}
        if states <= _OUT:
            db.update_plan_item(item["id"], status="published")
            changed += 1
        elif item["status"] == "scheduled" and "failed" in states and not states & {"pending", "uploading"}:
            if _requeue_network_failures(pubs):
                changed += 1
                continue
            err = next((p["error"] for p in pubs if p["state"] == "failed" and p["error"]), "")
            db.update_plan_item(item["id"], status="failed", note=f"发布失败:{err}"[:200])
            changed += 1
    return changed


def run() -> str:
    changed = sync()
    try:
        from . import series_mix

        added = series_mix.run_once()
    except Exception as exc:  # noqa: BLE001 - 加合集是顺手的事,不能拖垮发布员
        log.warning("加合集出错: %s", exc)
        added = ""
    try:
        from . import first_comment

        said = first_comment.run_once()
    except Exception as exc:  # noqa: BLE001 - 发评论也是顺手的事
        log.warning("发第一条评论出错: %s", exc)
        said = ""
    if not changed and not added and not said:
        raise Skip("没有发布状态变化")
    parts = [f"{changed} 条选题发布状态更新"] if changed else []
    return ";".join(parts + ([f"合集:{added}"] if added else [])
                    + ([f"评论:{said}"] if said else []))
