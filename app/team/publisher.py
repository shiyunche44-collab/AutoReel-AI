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
import time
from typing import Any

from .. import config, db
from . import Skip, chief, compliance, preference, repurpose
from .plan import PlanError

#: 发布时刻离现在至少留这么久(审完马上发也给发布线程一点余量)
MIN_AHEAD = 120.0
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
    except Exception:  # noqa: BLE001 - 包装坏了就只用项目标题
        pass
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


def reject_cut(item_id: str, note: str = "") -> dict[str, Any]:
    """成片打回:这条不发了。要重做就在项目页改,或者让主编另排。"""
    if not db.cas_plan_status(item_id, ("review",), "rejected",
                              note=("成片打回:" + note)[:200]):
        raise PlanError("这条选题不在成片待审")
    item = db.get_plan_item(item_id) or {}
    preference.record(item, "cut", "reject", note=note)
    return item


def sync() -> int:
    """已排发布的选题跟着发布任务走:都发出去 → 已发布;有失败且没有在途的 → 失败。"""
    changed = 0
    for item in db.list_plan_items(statuses=("scheduled",), limit=200):
        # 只看主片:衍生品晚几天发、失败了也不该让选题算「发布失败」
        pubs = [p for p in db.list_publications(project_id=item["project_id"]) if repurpose.is_main(p)]
        if not pubs:
            continue
        states = {p["state"] for p in pubs}
        if states <= _OUT:
            db.update_plan_item(item["id"], status="published")
            changed += 1
        elif "failed" in states and not states & {"pending", "uploading"}:
            err = next((p["error"] for p in pubs if p["state"] == "failed" and p["error"]), "")
            db.update_plan_item(item["id"], status="failed", note=f"发布失败:{err}"[:200])
            changed += 1
    return changed


def run() -> str:
    changed = sync()
    if not changed:
        raise Skip("没有发布状态变化")
    return f"{changed} 条选题发布状态更新"
