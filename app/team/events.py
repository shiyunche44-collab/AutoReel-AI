"""行业日历:上市、发布会、车展、OTA、降价这类有日子的事,提前 3–7 天进选题单,当天触发快反。

**两个来源。** 情报员每批新闻里,LLM 只挑「明确写了日期、还没发生或就在今天」的事件,
日期 / 车型都要能在原标题里对上(车型不在标题里就留空,不许猜);再加你在 /team 手动填的。
同品类同一天同标题只记一条。

**怎么用。** 主编排选题时,3–7 天后的事件写进提示词、命中的车加热度(预热、参数前瞻
来得及做);事件当天交给快反通道(`rapid`)。
"""
from __future__ import annotations

import datetime as dt
import logging
import re
from typing import Any

from .. import config, db
from ..models import new_id
from . import Skip, TeamError

log = logging.getLogger(__name__)

KIND_LABEL = {"launch": "上市", "event": "发布会", "show": "车展", "ota": "OTA",
              "price": "降价", "other": "其他"}
#: 主编提前多少天开始排
LEAD_MIN_DAYS = 3
LEAD_MAX_DAYS = 7
#: 抽取时只要这个范围内的日期(更远的新闻常是「预计明年」,不可靠)
HORIZON_DAYS = 45
#: 上次抽到哪一批热点(同一批不重复问 LLM)
_SEEN_KEY = "events_seen_batch"

SYSTEM = """你从汽车新闻标题里抽「有明确日期的行业事件」:新车上市、发布会、车展、OTA 推送、官方降价。

只输出 JSON:{"events": [{"i": 序号, "day": "YYYY-MM-DD", "kind": "launch|event|show|ota|price|other", "car": "车型", "title": "一句话"}]}
- 只要标题或摘要里**写明了具体日期**(如「10 月 28 日上市」「11.15 广州车展」)的;「即将」「近期」「预计明年」不要。
- 年份没写就按今天推算最近的那一天;已经过去的不要。
- car 必须是原文里出现的车型名,原样照抄;没有具体车型(如车展)留空字符串。不许猜。
- title 不超过 30 字,说清楚哪天发生什么。没有符合的就返回空列表。"""


class EventError(ValueError):
    pass


def _norm(text: str) -> str:
    return re.sub(r"[\s\-_·()（）]+", "", str(text or "")).lower()


def _day(text: str) -> dt.date | None:
    try:
        return dt.date.fromisoformat(str(text or "").strip()[:10])
    except ValueError:
        return None


# --------------------------------------------------------------------------
# 抽取
# --------------------------------------------------------------------------
def news_items(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """热点里可能带事件日期的条目:新闻 + 抖音热搜。"""
    return [r for r in rows if r.get("source") in ("news", "douyin") and r.get("title")]


def check(raw: dict[str, Any], items: list[dict[str, Any]], today: dt.date) -> dict[str, Any] | None:
    """校验 LLM 抽的一条事件,不合格返回 None。"""
    try:
        src = items[int(raw.get("i"))]
    except (TypeError, ValueError, IndexError):
        return None
    day = _day(raw.get("day"))
    if day is None or not (today <= day <= today + dt.timedelta(days=HORIZON_DAYS)):
        return None
    title = str(raw.get("title") or "").strip()[:40]
    if not title:
        return None
    kind = raw.get("kind") if raw.get("kind") in KIND_LABEL else "other"
    car = str(raw.get("car") or "").strip()[:40]
    source_text = _norm(src.get("title", "") + src.get("metric", ""))
    if car and _norm(car) not in source_text:
        car = ""  # 原文里没有的车型不认
    return {"day": day.isoformat(), "kind": kind, "car": car, "title": title,
            "url": str(src.get("url") or "")[:300]}


def extract(items: list[dict[str, Any]], llm: Any, today: dt.date) -> list[dict[str, Any]]:
    if not items or llm is None:
        return []
    lines = [f"{i} | {r['title'][:80]}" + (f" | {r['metric'][:60]}" if r.get("metric") else "")
             for i, r in enumerate(items[:40])]
    user = f"今天是 {today.isoformat()}。\n" + "\n".join(lines)
    data = llm.structured(SYSTEM, user, required_keys=["events"], temperature=0.1)
    out = []
    for raw in data.get("events") or []:
        ev = check(raw, items, today) if isinstance(raw, dict) else None
        if ev:
            out.append(ev)
    return out


def harvest(category: str, *, llm: Any = None, now: dt.datetime | None = None) -> int:
    """从最新一批热点里抽事件入库。返回新增条数;这一批抽过就 Skip。"""
    from ..pipeline import orchestrator
    from . import scout

    now = now or dt.datetime.now()
    cur = scout.latest(category)
    if not cur:
        raise Skip("还没有热点快照")
    if db.get_team_setting(_SEEN_KEY, "") == cur["batch"]:
        raise Skip("这批热点已经抽过事件")
    items = news_items(db.trend_rows(cur["batch"]))
    if not items:
        db.set_team_setting(_SEEN_KEY, cur["batch"])
        raise Skip("这批热点里没有新闻")
    llm = llm if llm is not None else orchestrator._llm()
    if llm is None:
        raise TeamError("没有可用的 LLM,抽不了行业事件")
    try:
        found = extract(items, llm, now.date())
    except Exception as exc:  # noqa: BLE001 - LLM 出错这批先不抽,下批再说
        raise TeamError(f"抽行业事件失败:{exc}") from exc
    n = sum(1 for ev in found
            if db.add_industry_event({"id": new_id("ev_"), "category": category, "source": "news", **ev}))
    db.set_team_setting(_SEEN_KEY, cur["batch"])
    return n


# --------------------------------------------------------------------------
# 手动维护
# --------------------------------------------------------------------------
def add_manual(category: str, day: str, title: str, kind: str = "other", car: str = "",
               today: dt.date | None = None) -> dict[str, Any]:
    today = today or dt.date.today()
    d = _day(day)
    if d is None:
        raise EventError("日期格式是 YYYY-MM-DD")
    if d < today:
        raise EventError("已经过去的事件不用记")
    title = (title or "").strip()
    if not title:
        raise EventError("写一句发生什么")
    if kind not in KIND_LABEL:
        raise EventError(f"类型只能是:{'、'.join(KIND_LABEL.values())}")
    eid = new_id("ev_")
    if not db.add_industry_event({"id": eid, "category": category, "day": d.isoformat(),
                                  "kind": kind, "car": (car or "").strip()[:40],
                                  "title": title[:40], "source": "manual"}):
        raise EventError("这一天已经有同样的事件了")
    return db.get_industry_event(eid)


def dismiss(event_id: str) -> None:
    if not db.get_industry_event(event_id):
        raise EventError("没有这个事件")
    db.update_industry_event(event_id, status="dismissed")


# --------------------------------------------------------------------------
# 给主编 / 快反 / 页面
# --------------------------------------------------------------------------
def upcoming(category: str, today: dt.date, lo: int = 0, hi: int = 30) -> list[dict[str, Any]]:
    rows = db.list_industry_events(category, day_from=(today + dt.timedelta(days=lo)).isoformat(),
                                   day_to=(today + dt.timedelta(days=hi)).isoformat())
    return [{**r, "kind_label": KIND_LABEL.get(r["kind"], r["kind"]),
             "in_days": (_day(r["day"]) - today).days} for r in rows]


def lead_events(category: str, today: dt.date) -> list[dict[str, Any]]:
    """主编该提前排的:3–7 天后的。"""
    return upcoming(category, today, LEAD_MIN_DAYS, LEAD_MAX_DAYS)


def lead_cars(category: str, today: dt.date) -> dict[str, dict[str, Any]]:
    """{归一化车名: 事件},主编给命中的选题加热度、写理由。"""
    return {_norm(e["car"]): e for e in lead_events(category, today) if e["car"]}


def prompt_text(category: str, today: dt.date) -> str:
    rows = lead_events(category, today)
    if not rows:
        return ""
    lines = [f"- {e['day'][5:]}({e['in_days']} 天后){e['kind_label']}:{e['title']}" for e in rows]
    return ("行业日历(这些事几天后发生,现在排预热 / 前瞻 / 参数解读正好赶上):\n"
            + "\n".join(lines))


def today_events(category: str, today: dt.date) -> list[dict[str, Any]]:
    return upcoming(category, today, 0, 0)


def run() -> str:
    category = config.TEAM_CATEGORY
    n = harvest(category)
    soon = upcoming(category, dt.date.today(), 0, LEAD_MAX_DAYS)
    return f"新记 {n} 个行业事件,未来 {LEAD_MAX_DAYS} 天共 {len(soon)} 个"
