"""发布员:成片闸 + 定时发布。

成片出来后选题停在「成片待审」(review)。**在人点「通过」之前,发布队列里
什么都没有** —— 不靠一个审核状态字段去拦已经建好的发布任务,而是根本不建,
误发的路径就不存在。

通过时按账号建发布任务(`publish.service.enqueue`,同项目同账号幂等),
写上 `scheduled_at`;发布线程到点才领(`db.claim_publications`)。发布时刻
默认是选题排期的时刻,已经过了就顺延到下一个发布时段。
"""
from __future__ import annotations

import datetime as dt
import time
from typing import Any

from .. import config, db
from . import Skip
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
        default = a.id in saved if isinstance(saved, list) else a.platform == "douyin"
        out.append({"id": a.id, "platform": a.platform, "platform_label": view["platform_label"],
                    "name": a.name, "default": default})
    return out


def review_view(item: dict[str, Any]) -> dict[str, Any]:
    """成片闸卡片要的:成片、封面、候选标题、发布时刻、账号。"""
    from ..pipeline import aspect, packaging

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
    video = aspect.output_path(pid)
    return {
        "project_title": project.get("title") or item["subject"],
        "title": picked or (titles[0] if titles else project.get("title") or item["subject"]),
        "titles": titles,
        "has_video": video.is_file(),
        "video_url": f"/api/projects/{pid}/download",
        "cover_url": f"/api/projects/{pid}/cover",
        "publish_at": next_publish_time(item["slot_at"]),
    }


def approve_cut(item_id: str, account_ids: list[str], *, title: str = "",
                publish_at: float | None = None) -> dict[str, Any]:
    """成片闸通过:建发布任务,定时发。"""
    from ..publish import service as publish_service

    item = db.get_plan_item(item_id)
    if not item:
        raise PlanError("选题不存在")
    if item["status"] != "review":
        raise PlanError("这条选题不在成片待审")
    if not account_ids:
        raise PlanError("至少选一个发布账号")
    at = next_publish_time(float(publish_at or item["slot_at"]))
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
    db.set_team_setting("publish_accounts", list(account_ids))
    when = dt.datetime.fromtimestamp(at).strftime("%m-%d %H:%M")
    db.add_event(item["project_id"], "team", reason=f"成片审核通过,定时 {when} 发布")
    return {"item": db.get_plan_item(item_id), "created": len(out["created"]),
            "skipped": len(out["skipped"]), "warnings": out.get("warnings") or [],
            "publish_at": at}


def reject_cut(item_id: str, note: str = "") -> dict[str, Any]:
    """成片打回:这条不发了。要重做就在项目页改,或者让主编另排。"""
    if not db.cas_plan_status(item_id, ("review",), "rejected",
                              note=("成片打回:" + note)[:200]):
        raise PlanError("这条选题不在成片待审")
    return db.get_plan_item(item_id) or {}


def sync() -> int:
    """已排发布的选题跟着发布任务走:都发出去 → 已发布;有失败且没有在途的 → 失败。"""
    changed = 0
    for item in db.list_plan_items(statuses=("scheduled",), limit=200):
        pubs = db.list_publications(project_id=item["project_id"])
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
