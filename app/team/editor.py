"""主编:给未来几天的空档排选题,等用户在 /team 批。

**只填空档,不动已排的。** 每天跑一次:被驳回的、过期没批的空出来,下一轮补上;
已批的不碰。所以「每周批一次」和「每天补位」是同一个动作。

**选题来自 `topics.suggest`**(和首页热点选题同一套规则:车型必须在热点里出现、
禁用词剔除、角度去重),这里只多做三件事:冷却(同车 N 天内不重复)、
上升车型加热度、按热度把选题放进最近的空档。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import random
import re
import time
from typing import Any

from .. import config, db
from ..categories import CategoryProfile, load_profile
from ..models import new_id
from . import Skip, TeamError, scout

#: 离发布不到这么多小时的空档不排了(来不及出片 + 审核)
MIN_PREP_HOURS = 8
#: 这些状态的选题占着空档
HOLDING = ("proposed", "approved", "producing", "review", "scheduled", "published")


def _norm(text: str) -> str:
    return re.sub(r"[\s\-_·()（）]+", "", str(text or "")).lower()


def slot_times(now: dt.datetime, days: int, per_day: int, times: tuple[str, ...]) -> list[float]:
    """未来 days 天的发布时刻。一天多条而时刻不够时,同一时刻往后错开 1 小时。"""
    times = times or ("18:30",)
    out = []
    earliest = now + dt.timedelta(hours=MIN_PREP_HOURS)
    for d in range(0, days + 1):
        day = now.date() + dt.timedelta(days=d)
        for i in range(per_day):
            h, m = (int(x) for x in times[i % len(times)].split(":"))
            at = dt.datetime.combine(day, dt.time(h, m)) + dt.timedelta(hours=i // len(times))
            if earliest <= at <= now + dt.timedelta(days=days):
                out.append(at.timestamp())
    return sorted(out)


def open_slots(slots: list[float], items: list[dict[str, Any]]) -> list[float]:
    taken = {round(i["slot_at"]) for i in items if i["status"] in HOLDING}
    return [s for s in slots if round(s) not in taken]


def cooled_subjects(category: str, now: float, days: int) -> set[str]:
    """冷却期内不能再排的车型:近期做过的项目 + 已排着的选题。"""
    since = now - days * 86400
    out: set[str] = set()
    for p in db.list_projects(limit=300):
        if p.get("category") == category and p.get("created_at", 0) >= since \
                and p.get("state") != "CANCELLED":
            out.add(_norm(p.get("subject")))
            out.update(_norm(s.get("name")) for s in db.project_subjects(p))
    for item in db.list_plan_items(category, statuses=HOLDING, limit=500):
        out.add(_norm(item["subject"]))
        out.update(_norm(s) for s in item.get("subjects") or [])
    out.discard("")
    return out


def choose_format(profile: CategoryProfile, theme_name: str,
                  aspect: str | None = None, duration: int | None = None,
                  weights: dict[str, float] | None = None, seed: str = "") -> tuple[str, int]:
    """团队默认画幅 / 时长落到这个主题允许的档位上。横评取最长档(车多才放得下)。

    `weights`:打法手册里「时长」各档的权重(`features.duration_bucket`)。有就 70% 按权重
    在允许的时长里挑、30% 照旧取最接近默认的那档(探索)。画幅不学 —— 换画幅等于换账号形态。"""
    aspect = aspect if aspect in config.ASPECTS else config.TEAM_ASPECT
    if aspect not in config.ASPECTS:
        aspect = config.LANDSCAPE
    theme = profile.theme_spec(theme_name)
    durations = profile.theme_durations(theme, aspect)
    if theme.multi:
        return aspect, max(durations)
    want = duration or config.TEAM_DURATION
    default = min(durations, key=lambda d: (abs(d - want), d))
    if duration or not weights or len(durations) < 2:
        return aspect, default
    from ..pipeline import variety
    from .features import duration_bucket

    rng = random.Random(int(hashlib.sha256(f"duration:{seed}".encode()).hexdigest()[:16], 16))
    if rng.random() < variety.EXPLORE:
        return aspect, default
    ws = [max(0.01, float(weights.get(duration_bucket(d), 1.0))) for d in durations]
    if len(set(ws)) == 1:
        return aspect, default
    return aspect, rng.choices(list(durations), ws)[0]


def learned_weight(idea: Any, weights: dict[str, dict[str, float]] | None) -> float:
    """打法手册给这个选题的预测倍数:片型、车型权重的几何平均(`playbook.predict`,
    和选题闸卡片上显示的是同一个数;没结论的 = 1)。"""
    if not weights:
        return 1.0
    from . import playbook

    return playbook.predict({"theme": idea.theme, "subject": idea.subject}, by_dim=weights)


def taste_weight(idea: Any, rates: dict[str, Any] | None) -> float:
    """你的通过率乘数(`preference.multiplier`):你常批的车型 / 片型往前排。没开 / 没数 = 1。"""
    if not rates:
        return 1.0
    from . import preference

    return preference.multiplier({"theme": idea.theme, "subject": idea.subject}, rates)


def rank_ideas(ideas: list[Any], cooled: set[str], rising_cars: set[str],
               weights: dict[str, dict[str, float]] | None = None,
               rates: dict[str, Any] | None = None) -> list[Any]:
    """去掉冷却中的,上升车型热度 +1,乘上打法手册的权重和你的通过率乘数,按分排。"""
    out = []
    for idea in ideas:
        names = [idea.subject] if not idea.subjects else list(idea.subjects)
        if any(_norm(n) in cooled for n in names):
            continue
        boost = 1 if any(_norm(n) in rising_cars for n in names) else 0
        out.append((min(5, idea.heat + boost) * learned_weight(idea, weights)
                    * taste_weight(idea, rates), boost, idea))
    out.sort(key=lambda x: (-x[0], -x[1]))
    return [x[2] for x in out]


def _rationale(idea: Any, boosted: bool, weight: float = 1.0, extra: tuple[str, ...] = (),
               taste: float = 1.0) -> str:
    why = (idea.why_hot or "").strip()
    if boosted:
        why = (why + ";" if why else "") + "榜单名次在上升"
    for note in extra:
        why = (why + ";" if why else "") + note
    if abs(weight - 1) >= 0.05:
        note = f"打法手册加权 ×{weight:.2f}" if weight > 1 else f"打法手册降权 ×{weight:.2f}"
        why = (why + ";" if why else "") + note
    if abs(taste - 1) >= 0.05:
        note = f"你常批这类 ×{taste:.2f}" if taste > 1 else f"你常驳这类 ×{taste:.2f}"
        why = (why + ";" if why else "") + note
    return why[:200]


def run(category: str | None = None, *, llm: Any = None,
        now: dt.datetime | None = None) -> str:
    from ..pipeline import orchestrator, topics

    category = category or config.TEAM_CATEGORY
    now = now or dt.datetime.now()
    profile = load_profile(category)
    expire_stale(category, now.timestamp())
    window = slot_times(now, config.TEAM_PLAN_DAYS, config.TEAM_DAILY_SLOTS, config.TEAM_SLOT_TIMES)
    planned = db.list_plan_items(category, since=now.timestamp(), limit=500)
    empty = open_slots(window, planned)
    if not empty:
        raise Skip(f"未来 {config.TEAM_PLAN_DAYS} 天已排满")

    llm = llm if llm is not None else orchestrator._llm()
    if llm is None:
        raise TeamError("没有可用的 LLM,排不了选题")
    items = scout.fresh_items(category)
    if not items:
        raise TeamError("没有热点可用")
    from . import benchmark, engagement, events, playbook, preference

    learnings = db.list_learnings()
    weights = {d: playbook.weights(d, learnings) for d in ("theme", "subject", "duration")}
    # 打法手册 + 观众在评论里问的(运营官回灌)+ 行业日历 + 对标爆款
    hints = "\n\n".join(t for t in (preference.prompt_text(), playbook.prompt_text(learnings),
                                     engagement.audience_text(now.timestamp(), mark=True),
                                     events.prompt_text(category, now.date()),
                                     benchmark.prompt_text(now.timestamp())) if t)
    ideas = topics.suggest(items, profile=profile, llm=llm, n=min(12, len(empty) * 2 + 2),
                           playbook=hints)
    rising_cars = {_norm(m["car"]) for m in scout.rising(category, limit=20)}
    event_cars = events.lead_cars(category, now.date())
    bench_cars = benchmark.hot_cars(now.timestamp())
    cooled = cooled_subjects(category, now.timestamp(), config.TEAM_COOLDOWN_DAYS)
    rates = preference.rates() if preference.rate_on() else None
    ranked = rank_ideas(ideas, cooled, rising_cars | set(event_cars) | bench_cars, weights, rates)
    if not ranked:
        raise Skip(f"LLM 给了 {len(ideas)} 个选题,都在冷却期或不合规")

    made = 0
    used: set[str] = set()
    for slot, idea in zip(empty, ranked):
        key = _norm(idea.subject)
        if key in used:
            continue
        used.add(key)
        aspect, duration = choose_format(profile, idea.theme, weights=weights.get("duration"),
                                         seed=f"{slot}:{idea.subject}")
        names = [idea.subject] if not idea.subjects else list(idea.subjects)
        keys = [_norm(n) for n in names]
        extra = tuple(f"{event_cars[k]['in_days']} 天后{event_cars[k]['kind_label']}:"
                      f"{event_cars[k]['title']}" for k in dict.fromkeys(keys) if k in event_cars)
        if any(k in bench_cars for k in keys):
            extra += ("对标号同车型出了爆款",)
        evidence = list(idea.evidence) + [x for n in names
                                          for x in benchmark.evidence_for(n, now.timestamp())]
        db.create_plan_item({
            "id": new_id("pl_"), "category": category, "slot_at": slot,
            "kind": "multi" if idea.subjects else "single",
            "subject": idea.subject, "subjects": list(idea.subjects),
            "angle": idea.angle, "theme": idea.theme, "aspect": aspect, "duration": duration,
            "hook": idea.hook, "search_terms": idea.search_terms, "evidence": evidence[:12],
            "rationale": _rationale(idea, any(k in rising_cars for k in keys),
                                    learned_weight(idea, weights), extra,
                                    taste_weight(idea, rates)),
            "heat": idea.heat,
        })
        made += 1
    left = len(empty) - made
    return f"新排 {made} 条待批选题" + (f",还有 {left} 个空档没合适的" if left > 0 else "")


def expire_stale(category: str, now: float | None = None) -> int:
    """过了发布时刻还没批的选题作废,空档让给下一轮。"""
    now = now or time.time()
    n = 0
    for item in db.list_plan_items(category, statuses=("proposed",), until=now, limit=500):
        if db.cas_plan_status(item["id"], ("proposed",), "expired"):
            n += 1
    return n
