"""/team 页面要的数据。"""
from __future__ import annotations

import datetime as dt
import time
from typing import Any

from .. import config, db
from . import compliance, next_slot, paused, publisher, roles, scout
from .plan import view

#: 往回看多久的选题(已发布 / 失败的也显示一阵)
LOOKBACK_DAYS = 3
#: 还在流程里的状态
OPEN = ("proposed", "approved", "producing", "review", "scheduled")


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
        "settings": {
            "daily_slots": config.TEAM_DAILY_SLOTS, "plan_days": config.TEAM_PLAN_DAYS,
            "slot_times": list(config.TEAM_SLOT_TIMES), "lead_hours": config.TEAM_LEAD_HOURS,
            "cooldown_days": config.TEAM_COOLDOWN_DAYS, "auto_gates": config.TEAM_AUTO_GATES,
        },
    }
