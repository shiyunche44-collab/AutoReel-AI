"""快反通道:热度急升或事件当天,不等周单,单出一条高优先级竖版短稿选题等你批。

**触发。** ① 懂车帝榜单里冲进前 `TOP` 名、且一天内涨了 ≥ `CLIMB` 名(或新上榜);
② 行业日历里今天发生、带车型的事件。同一台车 `COOLDOWN_DAYS` 天内做过 / 排着就不再快反,
一天最多 `MAX_PER_DAY` 条(快反挤占产能,宁缺毋滥)。

**产出。** `plan_items(kind=rapid, heat=5)`,竖版 60–90 秒,发布时刻 = 现在 + `PREP_HOURS`
取整点。批了制片立刻开工(开工提前量远大于这几个小时),成片照样过成片闸。
没人批的,过了发布时刻和普通选题一样作废 —— 热点过了就过了。

**素材不够不硬做。** 用已有素材池:确认本车视频不到 `MIN_VIDEO_SECONDS` 秒就在选题上写明,
你看着办(批了素材官会先补池;补不上就别批)。
"""
from __future__ import annotations

import datetime as dt
import logging
import math
import re
from typing import Any

from .. import config, db
from ..categories import load_profile
from ..models import new_id
from . import Skip, TeamError

log = logging.getLogger(__name__)

#: 榜单前几名算「冲上来」
TOP = 5
#: 一天内至少涨几名
CLIMB = 3
#: 一天最多几条快反
MAX_PER_DAY = 2
#: 同车多少天内做过就不快反
COOLDOWN_DAYS = 3
#: 发布时刻 = 现在 + 这么多小时
PREP_HOURS = 6
#: 不发的时段:(几点起, 到几点)
QUIET = (23, 8)
#: 竖版目标时长
DURATION = 90
#: 确认本车视频少于这么多秒算素材不够
MIN_VIDEO_SECONDS = 60

_SOURCE_LABEL = {"dongchedi.sales": "懂车帝销量榜", "dongchedi.hot": "懂车帝热度榜"}
_BUSY = ("proposed", "approved", "producing", "review", "scheduled", "published")


def _norm(text: str) -> str:
    return re.sub(r"[\s\-_·()（）]+", "", str(text or "")).lower()


def triggers(category: str, now: dt.datetime) -> list[dict[str, Any]]:
    """该快反的车,事件当天的排前面(日子是死的,榜单明天还能再看)。"""
    from . import events, scout

    out: list[dict[str, Any]] = []
    for e in events.today_events(category, now.date()):
        if e["car"] and not e["plan_item_id"]:
            out.append({"car": e["car"], "why": f"今天{e['kind_label']}:{e['title']}",
                        "event_id": e["id"], "title": e["title"]})
    for m in scout.rising(category, limit=50):
        if m["rank"] is None or m["rank"] > TOP:
            continue
        if m["delta"] is not None and m["delta"] < CLIMB:
            continue
        label = _SOURCE_LABEL.get(m["source"], m["source"])
        why = (f"{label}新上榜就到第 {m['rank']} 名" if m["delta"] is None
               else f"{label}一天从第 {m['prev']} 名升到第 {m['rank']} 名")
        out.append({"car": m["car"], "why": why, "event_id": "", "title": ""})
    seen: set[str] = set()
    uniq = []
    for t in out:
        key = _norm(t["car"])
        if key and key not in seen:
            seen.add(key)
            uniq.append(t)
    return uniq


def recent_cars(category: str, now: float) -> set[str]:
    """冷却中的车:近 COOLDOWN_DAYS 天建过项目的 + 前后 COOLDOWN_DAYS 天排着的选题。"""
    since = now - COOLDOWN_DAYS * 86400
    out: set[str] = set()
    for p in db.list_projects(limit=200):
        if p.get("category") == category and p.get("created_at", 0) >= since \
                and p.get("state") != "CANCELLED":
            out.add(_norm(p.get("subject")))
    for item in db.list_plan_items(category, statuses=_BUSY, since=since,
                                   until=now + COOLDOWN_DAYS * 86400, limit=300):
        out.add(_norm(item["subject"]))
        out.update(_norm(s) for s in item.get("subjects") or [])
    out.discard("")
    return out


def made_today(category: str, now: dt.datetime) -> int:
    start = dt.datetime.combine(now.date(), dt.time()).timestamp()
    return sum(1 for i in db.list_plan_items(category, since=start - 86400, limit=300)
               if i["kind"] == "rapid" and i["created_at"] >= start)


def slot_for(now: dt.datetime) -> float:
    """现在 + PREP_HOURS 取整点;落在深夜(23 点到 8 点)就挪到早上 8 点,没人刷的时候发等于白发。"""
    at = dt.datetime.fromtimestamp(math.ceil((now + dt.timedelta(hours=PREP_HOURS)).timestamp() / 3600) * 3600)
    if at.hour >= QUIET[0]:
        at = dt.datetime.combine(at.date() + dt.timedelta(days=1), dt.time(QUIET[1]))
    elif at.hour < QUIET[1]:
        at = dt.datetime.combine(at.date(), dt.time(QUIET[1]))
    return at.timestamp()


def video_seconds(car: str) -> float:
    from ..pipeline import library

    return library.confirmed_seconds(library.Library.load(car), video_only=True)


def pick_idea(ideas: list[Any], car: str, profile: Any) -> Any | None:
    """只要单车、主题能出竖版的那条。"""
    key = _norm(car)
    for idea in ideas:
        if idea.subjects or profile.theme_spec(idea.theme).multi:
            continue
        if _norm(idea.subject) == key or key in _norm(idea.subject) or _norm(idea.subject) in key:
            return idea
    return None


def make(category: str, trig: dict[str, Any], *, llm: Any, now: dt.datetime) -> str:
    """给一个触发出一条快反选题,返回选题 id;LLM 没给出能用的就返回空串。"""
    from ..pipeline import topics
    from . import benchmark, editor, scout

    profile = load_profile(category)
    key = _norm(trig["car"])
    rows = [r for r in scout.fresh_items(category)
            if key in _norm(r.get("car")) or key in _norm(r.get("title"))]
    if trig["title"]:
        rows.insert(0, {"source": "news", "kind": "event", "title": trig["title"], "car": trig["car"],
                        "metric": trig["why"], "url": "", "date": now.date().isoformat()})
    if not rows:
        rows = [{"source": "rank", "kind": "model", "title": trig["why"], "car": trig["car"],
                 "metric": "", "url": "", "date": now.date().isoformat()}]
    hint = (f"这是快反选题:「{trig['car']}」{trig['why']}。只围绕这台车出选题,"
            f"做竖版 {DURATION} 秒以内的短稿:角度紧扣刚发生的事,开头三秒就说新闻点。")
    ideas = topics.suggest(rows, profile=profile, llm=llm, n=3, playbook=hint)
    idea = pick_idea(ideas, trig["car"], profile)
    if idea is None:
        return ""
    aspect, duration = editor.choose_format(profile, idea.theme, config.PORTRAIT, DURATION)
    secs = video_seconds(idea.subject)
    rationale = f"快反:{trig['why']}"
    note = ""
    if secs < MIN_VIDEO_SECONDS:
        note = (f"素材不够:确认本车视频只有 {secs:.0f} 秒(要 {MIN_VIDEO_SECONDS} 秒)。"
                "批了素材官会先补池;补不上就别批")
        rationale += f";素材池偏薄({secs:.0f}s)"
    item_id = new_id("pl_")
    evidence = [trig["why"], *idea.evidence, *benchmark.evidence_for(idea.subject, now.timestamp())]
    db.create_plan_item({
        "id": item_id, "category": category, "slot_at": slot_for(now), "kind": "rapid",
        "subject": idea.subject, "subjects": [], "angle": idea.angle, "theme": idea.theme,
        "aspect": aspect, "duration": duration, "hook": idea.hook, "search_terms": idea.search_terms,
        "evidence": evidence[:12], "rationale": rationale[:200], "heat": 5, "note": note,
    })
    if trig["event_id"]:
        db.update_industry_event(trig["event_id"], plan_item_id=item_id)
    return item_id


def run(category: str | None = None, *, llm: Any = None, now: dt.datetime | None = None) -> str:
    from ..pipeline import orchestrator

    category = category or config.TEAM_CATEGORY
    now = now or dt.datetime.now()
    room = MAX_PER_DAY - made_today(category, now)
    if room <= 0:
        raise Skip(f"今天已经出了 {MAX_PER_DAY} 条快反")
    cooled = recent_cars(category, now.timestamp())
    todo = [t for t in triggers(category, now) if _norm(t["car"]) not in cooled]
    if not todo:
        raise Skip("没有急升车型或今天的行业事件")
    llm = llm if llm is not None else orchestrator._llm()
    if llm is None:
        raise TeamError("没有可用的 LLM,出不了快反选题")
    made = []
    for trig in todo:
        if len(made) >= room:
            break
        try:
            item_id = make(category, trig, llm=llm, now=now)
        except Exception as exc:  # noqa: BLE001 - 一个触发出错不连坐下一个
            log.warning("快反选题失败 %s:%s", trig["car"], exc)
            continue
        if item_id:
            made.append(f"{trig['car']}({trig['why']})")
    if not made:
        raise TeamError(f"{len(todo)} 个快反触发都没出成选题")
    return f"出了 {len(made)} 条快反选题等你批:" + ";".join(made)
