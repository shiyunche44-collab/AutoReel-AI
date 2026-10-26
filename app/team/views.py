"""/team 页面要的数据。"""
from __future__ import annotations

import datetime as dt
import time
from typing import Any

from .. import config, db
from . import (activator, benchmark, compliance, engagement, events, iteration, next_slot, paused,
               playbook, preference, publisher, rapid, roles, scout)
from . import health
from .plan import view
from .features import duration_bucket

#: 往回看多久的选题(已发布 / 失败的也显示一阵)
LOOKBACK_DAYS = 3
#: 还在流程里的状态
OPEN = ("proposed", "approved", "producing", "review", "scheduled")


def _playbook_views() -> list[dict[str, Any]]:
    """手册结论。对标先验被自己同一取值的结论盖过的标 `shadowed`(不生效,只给你看)。"""
    rows = db.list_learnings(("active", "off"))
    live = {r["id"] for r in playbook.merged(rows)}
    return [{**r, "text": playbook.describe(r), "shadowed": r["id"] not in live} for r in rows]


def _experiment_views() -> list[dict[str, Any]]:
    """在跑的配对实验(可能几个并行):固定哪个维度、试哪种写法、已经用了几条。"""
    from . import features

    out = []
    for p in playbook.progress():
        dim, value = p["dimension"], p["value"]
        out.append({**p, "label": f"{features.DIMENSION_LABEL.get(dim, dim)}:"
                                  f"{features.value_label(dim, value)}",
                    "left": max(0, p["size"] - p["used"])})
    return out


def overview(category: str | None = None) -> dict[str, Any]:
    category = category or config.TEAM_CATEGORY
    now = dt.datetime.now()
    team = []
    for r in roles():
        last = db.last_team_run(r.name)
        busy = [x for x in db.list_team_runs(r.name, limit=5) if x["status"] != "idle"]
        team.append({
            "name": r.name, "label": r.label, "duty": r.duty, "cadence": r.cadence.label(),
            "last": last,
            "last_work": busy[0] if busy else None,
            "next_at": next_slot(r.cadence, now, last["started_at"] if last else None).timestamp(),
        })
    since = time.time() - LOOKBACK_DAYS * 86400
    rows = {i["id"]: i for i in db.list_plan_items(category, since=since, limit=300)}
    # 还在流程里的不管排期多早都要显示(成片待审拖了几天也得能看到)
    for i in db.list_plan_items(category, statuses=OPEN, limit=200):
        rows.setdefault(i["id"], i)
    items = [view(i) for i in sorted(rows.values(), key=lambda r: r["slot_at"])
             if i["status"] not in ("expired",)]
    # 待批选题的预测倍数(打法手册各维度权重的几何平均),和主编排序用的是同一个函数
    by_dim = {d: playbook.weights(d) for d in playbook.PROMPT_DIMENSIONS}
    for i in items:
        if i["status"] == "proposed":
            i["predict"] = playbook.predict(
                {"subject": i.get("subject") or "", "theme": i.get("theme") or "",
                 "duration": duration_bucket(i.get("duration")), "aspect": i.get("aspect") or ""},
                by_dim=by_dim)
    latest = scout.latest(category)
    return {
        "category": category,
        "enabled": config.TEAM_ENABLED,
        "paused": paused(),
        "roles": team,
        "plan": items,
        "pending": sum(1 for i in items if i["status"] == "proposed"),
        "reviews": sum(1 for i in items if i["status"] == "review"),
        "held": sum(1 for i in items if i["status"] == "producing" and i.get("hold")),
        "accounts": publisher.accounts(),
        "health": compliance.health(),
        "rising": scout.rising(category),
        "trends_at": latest["captured_at"] if latest else None,
        "trends_n": latest["n"] if latest else 0,
        "runs": [x for x in db.list_team_runs(limit=40) if x["status"] != "idle"][:20],
        "playbook": _playbook_views(),
        # 当前在跑的配对实验(攒样本用):固定哪个维度、试哪种写法、还要几条
        "experiments": _experiment_views(),
        # 迭代记录:按周趋势、实验进度、自动改动的变更日志
        "iteration": iteration.overview(),
        # 你的审核偏好:归纳出的几条话 + 通过率乘数
        "preference": preference.overview(),
        "report": db.latest_report(),
        "comments": comment_queue(),
        "engage": {**engagement.stats(),
                   "auto_thanks": bool(db.get_team_setting(engagement.AUTO_THANKS_KEY, False))},
        "active": activator.stats(),
        "events": events.upcoming(category, now.date(), 0, 30),
        "event_kinds": events.KIND_LABEL,
        "bench": {"accounts": benchmark.accounts_view(), "hits": benchmark.hits(limit=10),
                  "burst_ratio": benchmark.BURST_RATIO, "max": benchmark.MAX_ACCOUNTS},
        "rapid": {"top": rapid.TOP, "climb": rapid.CLIMB, "per_day": rapid.MAX_PER_DAY,
                  "prep_hours": rapid.PREP_HOURS},
        # 复盘官按流程体检自动调过的参数(白名单,有上下限),页面上能一键恢复默认
        "tuning": health.tuned_view(),
        "settings": {
            "daily_slots": config.TEAM_DAILY_SLOTS, "plan_days": config.TEAM_PLAN_DAYS,
            "slot_times": list(config.TEAM_SLOT_TIMES), "lead_hours": config.TEAM_LEAD_HOURS,
            "cooldown_days": config.TEAM_COOLDOWN_DAYS, "auto_gates": config.TEAM_AUTO_GATES,
        },
    }


def comment_queue(limit: int = 60) -> list[dict[str, Any]]:
    """评论审核区:待审 / 已批待发 / 发送失败的,新的在前;每条带作品标题。"""
    rows = db.list_comments(states=("draft", "approved", "sending", "failed"), limit=limit)
    titles: dict[str, str] = {}
    out = []
    for r in rows:
        if r["publication_id"] not in titles:
            pub = db.get_publication(r["publication_id"]) or {}
            titles[r["publication_id"]] = pub.get("title") or ""
        out.append({**r, "work_title": titles[r["publication_id"]],
                    "kind_label": engagement.KIND_LABEL.get(r["kind"], r["kind"])})
    return out
