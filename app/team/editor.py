"""主编:给未来几天的空档排选题,等用户在 /team 批。

**只填空档,不动已排的。** 每天跑一次:被驳回的、过期没批的空出来,下一轮补上;
已批的不碰。所以「每周批一次」和「每天补位」是同一个动作。

**选题来自 `topics.suggest`**(和首页热点选题同一套规则:车型必须在热点里出现、
禁用词剔除、角度去重),这里只多做四件事:冷却(同车 N 天内不重复)、
上升车型加热度、只留榜单前几名的车(`TEAM_TOP_RANK`)、按热度把选题放进最近的空档。
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
#: 这些状态的选题算近期做过/排着(冷却去重用)
HOLDING = ("proposed", "approved", "producing", "review", "scheduled", "published")
#: 这些状态的选题占着未来的发布空档(已提前发布的不再占未来空档,避免断更)
SLOT_HOLDING = ("proposed", "approved", "producing", "review", "scheduled")


def _norm(text: str) -> str:
    return re.sub(r"[\s\-_·()（）]+", "", str(text or "")).lower()


def _same_car(a: str, b: str) -> bool:
    """规范化车名是否指同一台车(兼容「哈弗长城H10」与「长城H10」这类品牌前缀差异)。"""
    ka, kb = _norm(a), _norm(b)
    if not ka or not kb:
        return False
    return ka == kb or (len(ka) >= 2 and len(kb) >= 2 and (ka.endswith(kb) or kb.endswith(ka)))


def _in_car_set(name: str, pool: set[str]) -> bool:
    k = _norm(name)
    return bool(k) and (k in pool or any(_same_car(k, c) for c in pool))


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
    taken = {round(i["slot_at"]) for i in items if i["status"] in SLOT_HOLDING}
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


#: 榜单第 1 名比圈内最后一名多乘这么多(线性):同在前 10,也先做更靠前的
RANK_BONUS = 0.3
#: 榜单前几名之外的车只在补位时用,分数再打这个折
OUTSIDE_WEIGHT = 0.5
#: 单点深挖(只讲一项技术 / 配置)这类片型打的折:讲给已经在看这台车的人听,新号没有这批人
NICHE_WEIGHT = 0.7
#: 冲粉期高转化片型加权:双车对决(pk)、同预算选购(priceband/roundup)、买车避坑问答(buyguide/qa)优先
GROWTH_THEMES: dict[str, float] = {
    "pk": 1.25,
    "priceband": 1.2,
    "roundup": 1.15,
    "buyguide": 1.1,
    "qa": 1.1,
    "debate": 1.05,
}


def _names(idea: Any) -> list[str]:
    return [idea.subject] if not idea.subjects else list(idea.subjects)


def top_cars(items: list[dict[str, Any]], n: int | None = None) -> dict[str, dict[str, Any]]:
    """榜单前 n 名的车:{规范化车名: {car, label, rank}}。一台车上了两个榜取名次靠前的那个。

    没有榜单数据(来源挂了)或 n = 0 → 空,调用方就不限范围 —— 宁可照旧排,不要静静排不出。"""
    n = config.TEAM_TOP_RANK if n is None else n
    out: dict[str, dict[str, Any]] = {}
    for r in items:
        key, rank = _norm(r.get("car")), int(r.get("rank") or 0)
        if r.get("source") not in scout.RANKED or not key or not 0 < rank <= n:
            continue
        if key not in out or rank < out[key]["rank"]:
            out[key] = {"car": str(r["car"]), "rank": rank,
                        "label": scout.RANK_LABEL.get(r["source"], "榜单")}
    return out


def gate_hit(idea: Any, top: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    """这个选题沾到的榜单前几名里名次最靠前的那台;一台都不沾 = None。

    选题里写的是「品牌+车系」(吉利银河星愿),榜单里是车系(星愿):互相包含就算同一台。
    横评 / 对决 / 价位段:车单里有一台在圈内就算。"""
    hits = [v for name in _names(idea) if len(k := _norm(name)) >= 2
            for c, v in top.items() if len(c) >= 2 and (c in k or k in c)]
    return min(hits, key=lambda v: v["rank"]) if hits else None


def gate_weight(idea: Any, top: dict[str, dict[str, Any]] | None, exempt: set[str] = frozenset(),
                n: int | None = None) -> float:
    """榜单范围给这个选题的乘数:圈内按名次 1–1.3,圈外打折;没开 = 1。

    `exempt` 里的车(观众点名问的)按第 1 名算:有人在问,比榜单名次更直接。"""
    if not top:
        return 1.0
    if any(_norm(x) in exempt for x in _names(idea)):
        return 1 + RANK_BONUS
    hit = gate_hit(idea, top)
    if hit is None:
        return OUTSIDE_WEIGHT
    n = max(1, n or config.TEAM_TOP_RANK or max(v["rank"] for v in top.values()))
    return round(1 + RANK_BONUS * max(0, n - hit["rank"]) / n, 3)


def in_gate(idea: Any, top: dict[str, dict[str, Any]] | None,
            exempt: set[str] = frozenset()) -> bool:
    return gate_weight(idea, top, exempt) >= 1.0


def niche_weight(idea: Any, profile: CategoryProfile | None) -> float:
    """单点深挖类片型降权(`NICHE_WEIGHT`),双车PK / 预算选购 / 买车问答加权(`GROWTH_THEMES`)。"""
    if profile is None or idea.theme not in profile.themes:
        return 1.0
    theme = profile.theme_spec(idea.theme)
    if theme.requires_angle and not theme.multi:
        return NICHE_WEIGHT
    return GROWTH_THEMES.get(idea.theme, 1.0)


def gate_hint(top: dict[str, dict[str, Any]]) -> str:
    """告诉 LLM 这批选题只从哪些车里出。"""
    if not top:
        return ""
    cars = "、".join(f"{v['car']}({v['label']}第 {v['rank']})"
                    for v in sorted(top.values(), key=lambda v: v["rank"]))
    return ("【选车范围】单车选题只从这些榜单靠前的车里选,名次越靠前越优先;"
            "横评 / 对决 / 价位段的车单里至少有一台在这里面:\n" + cars
            + "\n榜单更靠后的车、只在新闻里出现的车,这批不做(观众评论里点名问的除外)。"
            "\n角度优先写观众会搜的问法:这台车到底该不该买、和谁怎么选、哪个版本值,"
            "不要只讲一项配置或一个参数。")


def rank_ideas(ideas: list[Any], cooled: set[str], rising_cars: set[str],
               weights: dict[str, dict[str, float]] | None = None,
               rates: dict[str, Any] | None = None, *,
               top: dict[str, dict[str, Any]] | None = None, exempt: set[str] = frozenset(),
               profile: CategoryProfile | None = None) -> list[Any]:
    """去掉冷却中的,上升车型热度 +1,乘上打法手册的权重和你的通过率乘数,按分排。

    `top`(`top_cars`)给了就再乘榜单范围的乘数,并且圈外的一律排在圈内的后面;
    `exempt`:不受范围限制的车(观众点名问的、对标号刚出爆款的)。`profile` 给了就给单点深挖降权。"""
    out = []
    for idea in ideas:
        names = _names(idea)
        check_names = [idea.subject] if idea.subjects else names
        if any(_in_car_set(n, cooled) for n in check_names):
            continue
        boost = 1 if any(_in_car_set(n, rising_cars) for n in names) else 0
        gate = gate_weight(idea, top, exempt)
        out.append((gate < 1.0, min(5, idea.heat + boost) * learned_weight(idea, weights)
                    * taste_weight(idea, rates) * gate * niche_weight(idea, profile), boost, idea))
    out.sort(key=lambda x: (x[0], -x[1], -x[2]))
    return [x[3] for x in out]


KINDS = ("model", "band", "topic")
KIND_LABEL = {"model": "热门车型", "band": "价位段选购", "topic": "热点话题"}
#: 这些方向的单车选题算热点话题(观众常问 / 争议类)
TOPIC_THEMES = ("qa", "debate")


def slot_kind(slot: float, plan: tuple[str, ...] | None = None) -> str:
    """这个发布时刻按星期该做哪类选题;没配轮换 = 空串(不限)。"""
    plan = config.TEAM_WEEK_PLAN if plan is None else plan
    if len(plan) != 7:
        return ""
    kind = plan[dt.datetime.fromtimestamp(slot).weekday()]
    return kind if kind in KINDS else ""


def idea_kind(idea: Any, profile: CategoryProfile, hot_keys: set[str]) -> str:
    """选题属于哪一类:价位段选购(含双车PK/多车横评)/ 热点话题 / 热门车型。"""
    from ..pipeline import topics

    if idea.theme in {t.name for t in topics.band_themes(profile)} or (
        idea.theme in profile.themes and profile.theme_spec(idea.theme).multi
    ):
        return "band"
    names = [idea.subject] if not idea.subjects else list(idea.subjects)
    if idea.theme in TOPIC_THEMES or any(_norm(n) in hot_keys for n in names):
        return "topic"
    return "model"


def rotation_hint(slots: list[float]) -> str:
    """告诉 LLM 这批空档按星期各要哪几类,价位段选购的价位段写什么。"""
    need = [slot_kind(s) for s in slots]
    if not any(need):
        return ""
    lines = ["【本批选题的类型配比(按发布日星期轮换,尽量凑齐)】"]
    for kind in KINDS:
        n = need.count(kind)
        if n:
            lines.append(f"- {KIND_LABEL[kind]} {n} 条")
    if "band" in need:
        lines.append(f"- 价位段选购的价位段与能源写「{config.TEAM_BAND_HINT}」(angle 写明取舍点),"
                     "本批必须包含至少 1 条 1v1 对决(theme=pk,subjects 填 2 款榜单内同级车)"
                     "或价位段选购(theme=priceband)")
    if "topic" in need:
        lines.append("- 热点话题 = 当前热榜 / 新闻 / 行业事件里最多人在讨论的那个点,或观众评论里问得多的")
    return "\n".join(lines)


def slot_aspect(slot: float) -> str | None:
    """横竖屏对照:配置的星期排横屏,其余(和对照结束后)返回 None = 用团队默认画幅。"""
    days = config.TEAM_LANDSCAPE_WEEKDAYS
    if not days:
        return None
    day = dt.datetime.fromtimestamp(slot).date()
    until = config.TEAM_LANDSCAPE_UNTIL
    if until:
        try:
            if day > dt.date.fromisoformat(until):
                return None
        except ValueError:
            return None
    return config.LANDSCAPE if day.weekday() in days else None


def segment_theme(profile: CategoryProfile) -> Any | None:
    """品类里带细分名单的片型(「一次讲透」);没有 = None。"""
    return next((t for t in profile.themes.values()
                 if t.segment and profile.vocab.segments.get(t.segment)), None)


def next_segment_car(names: tuple[str, ...], cooled: set[str], done: set[str]) -> str:
    """细分名单里下一台该做的车:不在冷却期的里面,先挑没做过的,都做过就从头轮。没有 = 空串。"""
    fresh = [n for n in names if not _in_car_set(n, cooled)]
    return next((n for n in fresh if not _in_car_set(n, done)), fresh[0] if fresh else "")


def plan_segment(category: str, profile: CategoryProfile, empty: list[float],
                 now: float) -> tuple[int, list[float]]:
    """细分连更:配置的星期直接从片型的细分名单里排一台车。返回 (排了几条, 剩下的空档)。

    不问 LLM、不看全市场榜单 —— 硬派越野和皮卡进不了销量榜前 10,按榜单永远排不到;
    这条线靠同一个细分连着更来攒车粉。名单全在冷却期时空档还给常规选题,不留空。"""
    days = config.TEAM_WALKTHROUGH_WEEKDAYS
    theme = segment_theme(profile)
    if not days or theme is None:
        return 0, empty
    from ..pipeline import series

    names = profile.vocab.segments[theme.segment]
    cooled = cooled_subjects(category, now, config.TEAM_COOLDOWN_DAYS)
    done = {_norm(p.get("subject")) for p in db.list_projects(limit=300)
            if p.get("category") == category and p.get("theme") == theme.name
            and p.get("state") != "CANCELLED"}
    done.discard("")
    column = series.name_for(profile.vocab, theme.name) or theme.label
    made, rest = 0, []
    used_days: set[dt.date] = set()  # 一天最多一条,当天其余空档还给常规选题
    for slot in empty:
        day = dt.datetime.fromtimestamp(slot)
        car = (next_segment_car(names, cooled, done)
               if day.weekday() in days and day.date() not in used_days else "")
        if not car:
            rest.append(slot)
            continue
        used_days.add(day.date())
        aspect, duration = choose_format(profile, theme.name, aspect=config.LANDSCAPE,
                                         duration=theme.max_duration or None)
        db.create_plan_item({
            "id": new_id("pl_"), "category": category, "slot_at": slot,
            "status": "approved" if config.TEAM_AUTO_APPROVE_TOPICS else "proposed",
            "kind": "single", "subject": car, "subjects": [],
            "angle": "", "theme": theme.name, "aspect": aspect, "duration": duration,
            "hook": "", "search_terms": "", "evidence": [],
            "rationale": f"细分连更「{column}」:按名单轮到这台",
            "heat": 3,
        })
        cooled.add(_norm(car))
        made += 1
    return made, rest


def assign_slots(slots: list[float], ranked: list[Any], kind_of: dict[int, str]) -> list[tuple[float, Any]]:
    """每个空档先挑排名最高的同类选题;该类没有就退而求其次拿排名最高的剩余选题。"""
    left = list(ranked)
    out: list[tuple[float, Any]] = []
    for slot in slots:
        want = slot_kind(slot)
        pick = next((i for i in left if want and kind_of.get(id(i)) == want), None)
        if pick is None and left:
            pick = left[0]
        if pick is None:
            break
        left.remove(pick)
        out.append((slot, pick))
    return out


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


def auto_approve_open(category: str, now: float | None = None) -> int:
    """自动过选题闸开启时,把未过期且无素材告警的待批选题自动放行。"""
    if not config.TEAM_AUTO_APPROVE_TOPICS:
        return 0
    now = now or time.time()
    n = 0
    for item in db.list_plan_items(category, statuses=("proposed",), since=now, limit=500):
        if "素材不够" in str(item.get("note") or ""):
            continue
        if db.cas_plan_status(item["id"], ("proposed",), "approved"):
            n += 1
    return n


def run(category: str | None = None, *, llm: Any = None,
        now: dt.datetime | None = None) -> str:
    from ..pipeline import orchestrator, topics

    category = category or config.TEAM_CATEGORY
    now = now or dt.datetime.now()
    profile = load_profile(category)
    expire_stale(category, now.timestamp())
    auto_ok = auto_approve_open(category, now.timestamp())
    window = slot_times(now, config.TEAM_PLAN_DAYS, config.TEAM_DAILY_SLOTS, config.TEAM_SLOT_TIMES)
    planned = db.list_plan_items(category, since=now.timestamp(), limit=500)
    empty = open_slots(window, planned)
    # 细分连更的日子先排掉(不问 LLM),剩下的空档再走常规选题
    seg_made, empty = plan_segment(category, profile, empty, now.timestamp())
    seg_note = f"细分连更排 {seg_made} 条" if seg_made else ""
    if not empty:
        if seg_made:
            return seg_note + (f",自动批准 {auto_ok} 条选题" if auto_ok else "")
        if auto_ok:
            return f"自动批准 {auto_ok} 条选题,未来 {config.TEAM_PLAN_DAYS} 天已排满"
        raise Skip(f"未来 {config.TEAM_PLAN_DAYS} 天已排满")

    llm = llm if llm is not None else orchestrator._llm()
    if llm is None:
        raise TeamError("没有可用的 LLM,排不了选题")
    items = scout.fresh_items(category)
    if not items:
        raise TeamError("没有热点可用")
    from . import benchmark, engagement, events, playbook, preference, search_terms

    top = top_cars(items)
    learnings = db.list_learnings()
    weights = {d: playbook.weights(d, learnings) for d in ("theme", "subject", "duration")}
    # 打法手册 + 观众在评论里问的(运营官回灌)+ 行业日历 + 对标爆款
    asked = engagement.audience_topics(now.timestamp())
    asked_cars = {_norm(t["car"]): t for t in asked}
    hints = "\n\n".join(t for t in (preference.prompt_text(), playbook.prompt_text(learnings),
                                     engagement.audience_text(now.timestamp(), mark=True),
                                     engagement.topics_text(asked),
                                     search_terms.prompt_text(now.timestamp()),
                                     events.prompt_text(category, now.date()),
                                     benchmark.prompt_text(now.timestamp()),
                                     rotation_hint(empty), gate_hint(top)) if t)
    ideas = topics.suggest(items, profile=profile, llm=llm,
                           n=max(8, min(12, len(empty) * 2 + 4)),
                           playbook=hints)
    rising_cars = {_norm(m["car"]) for m in scout.rising(category, limit=20)}
    event_cars = events.lead_cars(category, now.date())
    bench_cars = benchmark.hot_cars(now.timestamp())
    cooled = cooled_subjects(category, now.timestamp(), config.TEAM_COOLDOWN_DAYS)
    rates = preference.rates() if preference.rate_on() else None
    # 不受榜单范围限制的:观众在评论里点名问的、对标号同车型刚出爆款的
    exempt = set(asked_cars) | bench_cars
    ranked = rank_ideas(ideas, cooled, rising_cars | set(event_cars) | bench_cars | set(asked_cars),
                        weights, rates, top=top, exempt=exempt, profile=profile)
    if not ranked:
        raise Skip(f"LLM 给了 {len(ideas)} 个选题,都在冷却期或不合规")

    made = 0
    deduped: list[Any] = []
    seen: set[str] = set()
    for idea in ranked:
        if not _in_car_set(idea.subject, seen):
            seen.add(_norm(idea.subject))
            deduped.append(idea)
    # 圈内的够填满空档就不用圈外的;不够才拿圈外分数最高的补位(宁可降权,不留空档断更)
    inside = [i for i in deduped if in_gate(i, top, exempt)]
    outside = [i for i in deduped if not in_gate(i, top, exempt)]
    deduped = inside + outside[:max(0, len(empty) - len(inside))]
    hot_keys = set(event_cars) | set(asked_cars)
    kind_of = {id(i): idea_kind(i, profile, hot_keys) for i in deduped}
    for slot, idea in assign_slots(empty, deduped, kind_of):
        aspect, duration = choose_format(profile, idea.theme, aspect=slot_aspect(slot),
                                         weights=weights.get("duration"),
                                         seed=f"{slot}:{idea.subject}")
        names = _names(idea)
        keys = [_norm(n) for n in names]
        hit = gate_hit(idea, top) if top else None
        extra = ((f"{hit['car']}在{hit['label']}第 {hit['rank']}",) if hit
                 else (f"不在榜单前 {config.TEAM_TOP_RANK},补位",) if top and not in_gate(
                     idea, top, exempt) else ())
        if niche_weight(idea, profile) < 1:
            extra += (f"单点深挖降权 ×{NICHE_WEIGHT:g}",)
        elif niche_weight(idea, profile) > 1:
            extra += (f"冲粉片型加权 ×{niche_weight(idea, profile):g}",)
        extra += tuple(f"{event_cars[k]['in_days']} 天后{event_cars[k]['kind_label']}:"
                      f"{event_cars[k]['title']}" for k in dict.fromkeys(keys) if k in event_cars)
        if any(k in bench_cars for k in keys):
            extra += ("对标号同车型出了爆款",)
        # 观众在评论里问的(放最前面:卡片上的「观众问的」标就认这几个字)
        extra = tuple(f"观众问的:{asked_cars[k]['n']} 条评论问到{asked_cars[k]['car']}"
                      f"(「{asked_cars[k]['samples'][0]}」)"
                      for k in dict.fromkeys(keys) if k in asked_cars) + extra
        evidence = list(idea.evidence) + [x for n in names
                                          for x in benchmark.evidence_for(n, now.timestamp())]
        status = ("approved" if config.TEAM_AUTO_APPROVE_TOPICS and in_gate(idea, top, exempt)
                  else "proposed")
        db.create_plan_item({
            "id": new_id("pl_"), "category": category, "slot_at": slot,
            "status": status,
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
        auto_ok += status == "approved"
    left = len(empty) - made
    label = (f"新排 {made} 条选题(自动批准 {auto_ok} 条)" if config.TEAM_AUTO_APPROVE_TOPICS
             else f"新排 {made} 条待批选题")
    return ((seg_note + ";" if seg_note else "") + label
            + (f",还有 {left} 个空档没合适的" if left > 0 else ""))


def expire_stale(category: str, now: float | None = None) -> int:
    """过了发布时刻还没批的选题作废,空档让给下一轮。"""
    now = now or time.time()
    n = 0
    for item in db.list_plan_items(category, statuses=("proposed",), until=now, limit=500):
        if db.cas_plan_status(item["id"], ("proposed",), "expired"):
            n += 1
    return n
