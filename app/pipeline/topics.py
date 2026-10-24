"""热点选题:原始热点(providers.trends) -> LLM 归纳出可选的单品选题与多车横评选题。

只做「出主意」这一步:不建项目、不碰素材池。用户在创建页挑中一个选题后,
`subject` 填进选题、`angle` 随项目一路传到事实检索与脚本;横评选题的 `subjects` 填进车单。

约束:
- 单车选题只对应一款车(subject 是素材池的 key)。
- 横评选题(品类有 roundup 方向时才出):subject 是横评标题,subjects 是 3–5 款车,
  每款都必须在热点里出现过,不够数就整条丢掉。
- 角度必须落在某条热点上;LLM 写出热点里根本没出现的车,直接丢掉。
- 没有公开榜单的品类(母婴):新闻里很少写全型号,subject 只要含热点里出现过的品牌就放行;
  带 profile `trends.exclude_words`(如婴儿奶粉)的选题一律丢掉。
- 品类差异(来源说明、subject 写法、优先级)全在 profile 的 `trends:`。
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Mapping

from pydantic import BaseModel, Field

from ..categories import CategoryProfile
from .script_gen import AD_ABSOLUTE_TERMS, HOOK_MAX_CHARS, forbidden_in

#: 给 LLM 的热点条数上限,防止 prompt 过长
MAX_TREND_ITEMS = 80


class TopicIdea(BaseModel):
    """一个可选的选题。"""

    #: 单一产品的规范名(汽车优先用懂车帝 series_name;母婴是「品牌+型号 / 品类」)
    subject: str
    #: 一句话角度,如「月销破 3 万背后:它到底赢在哪」
    angle: str
    #: 内容方向名,必是 profile.themes 之一
    theme: str
    #: 开场钩子
    hook: str = ""
    #: 为什么现在热:引用哪条热点
    why_hot: str = ""
    #: 支撑热度的热点链接
    sources: list[str] = Field(default_factory=list)
    #: 热度 1-5
    heat: int = 3
    #: 角度定向检索词(3-6 个实词),如「特斯拉 Model Y 召回 2026」
    search_terms: str = ""
    #: 支撑这个选题的热点原文(榜单名次、新闻标题与摘要),建项目时作为事实素材
    evidence: list[str] = Field(default_factory=list)
    #: 多车横评选题的车单(theme 是横评方向时非空);此时 subject 是横评标题
    subjects: list[str] = Field(default_factory=list)


SYSTEM = """你是中文{domain}短视频账号的选题策划。下面是刚抓到的实时热点
({sources_desc})。请据此给出 {n} 个横屏解说视频选题。

硬性要求:
1. 只输出一个 JSON 对象:{{"topics": [...]}},不要 Markdown 围栏或解释文字。
2. 每个选题字段:
   - subject:**只能是一款具体{subject_label}**的全称,{subject_rule}
     不许写多款对比、不许写品牌泛称、不许写事件名。{roundup_rule}
   - angle:一句话选题角度(12-28 字),要有观点或悬念,必须落在下方某条热点上。
     排名写具体名次(「销量榜第 1 名」),不许用这些词:{forbidden}。
   - theme:从这些内容方向里选一个名字:{themes}
   - hook:视频第一句话(18 字以内),抛出反常识、争议或最硬的结论。同样不许用上面的禁用词。
   - why_hot:这个选题为什么现在热,点名引用的热点(榜单名次/新闻/热搜词)。
   - sources:引用热点的 url 列表(热点里没有 url 就留空数组)。
   - heat:热度 1-5 的整数。
   - search_terms:去新闻里查这个角度要用的 3-6 个检索词,空格分隔,只要实词
     (如「特斯拉 Model Y 召回 2026」),不要口语和问句。
3. 严禁编造热点里没有的事件、数字或{subject_label};热点里没提的{subject_label}不许出现。
4. 不同选题尽量换不同{subject_label};同一款最多出现 2 次,且角度必须明显不同。
5. {priority_rule}

可选内容方向:
{theme_desc}"""


#: 品类有横评方向时插进 SYSTEM 第 2 条 subject 说明之后;没有时为空串,prompt 与之前逐字一致
ROUNDUP_RULE = """
   - 例外:多车横评选题(theme={name})的 subject 写横评标题(8-20 字,如「20 万级纯电 SUV 横评」),
     另给 subjects:{lo}-{hi} 款车的数组,每款按上面的写法照抄热点里出现过的车,
     且同一价位或同一细分市场,才有得比。{n} 个选题里出 1-2 个横评,单车选题不许用 {name} 方向。"""


#: 品类有对决方向时追加在横评规则之后
VERSUS_RULE = """
   - 例外:1v1 对决选题(theme={name})的 subject 写对决标题(如「小米YU7 vs 特斯拉Model Y」),
     另给 subjects:正好 {lo} 款车的数组,写法同上,且是同价位、真会被放在一起纠结的两台。{n} 个选题里最多 1 个对决。"""

#: 价位段选购(有车单、且必须写角度的横评类片型):角度写价位段,车单是这个价位段的车
BAND_RULE = """
   - 例外:{label}选题(theme={name})的 subject 写选题标题(8-20 字,如「10-15万纯电车型怎么选」),
     angle 必须写明价位段和能源(如「10-15万纯电:续航和补能怎么取舍」),
     另给 subjects:{lo}-{hi} 款热点里出现过、指导价落在这个价位段的车。{n} 个选题里最多 1 个{label}。"""

#: 必须有角度的片型(单点深挖):角度写成"这期只讲的那一个点"
FOCUS_RULE = """
   - theme={name} 的 angle 写这期只讲的**那一个点**(一项技术、一个配置或一个争议参数),
     如「800V 高压平台到底快在哪」,不要写成泛泛的选购角度。"""


def versus_theme(profile: CategoryProfile):
    """品类的对决方向(取第一个);没有返回 None。"""
    return next((t for t in profile.themes.values() if t.versus), None)


def roundup_theme(profile: CategoryProfile):
    """品类的普通横评方向(取第一个,不含价位段选购这类必须写角度的);没有返回 None。"""
    return next((t for t in profile.themes.values() if t.roundup and not t.requires_angle), None)


def band_themes(profile: CategoryProfile) -> list:
    """价位段选购类方向:有车单、且必须写角度(角度 = 价位段)。"""
    return [t for t in profile.themes.values() if t.roundup and t.requires_angle]


def _norm(text: str) -> str:
    return re.sub(r"[\s\-_·()（）]+", "", str(text or "")).lower()


def clean_subject(subject: str) -> str:
    """品牌+车系拼接时的叠词:「吉利银河银河战舰700」-> 「吉利银河战舰700」。"""
    return re.sub(r"([一-鿿]{2,4})\1", r"\1", str(subject or "").strip())


def _trend_dict(item: Any) -> dict[str, Any]:
    if isinstance(item, Mapping):
        return dict(item)
    if hasattr(item, "to_dict"):
        return item.to_dict()
    return dict(vars(item))


def format_trends(items: Iterable[Any]) -> str:
    """把热点条目压成给 LLM 读的行。"""
    lines = []
    for i, raw in enumerate(items):
        if i >= MAX_TREND_ITEMS:
            break
        d = _trend_dict(raw)
        parts = [f"[{d.get('source', '')}]", str(d.get("title", "")).strip()]
        if d.get("car"):
            parts.append(f"车系:{d['car']}")
        if d.get("metric"):
            parts.append(f"指标:{d['metric']}")
        if d.get("date"):
            parts.append(f"日期:{d['date']}")
        if d.get("url"):
            parts.append(f"url:{d['url']}")
        lines.append(" | ".join(p for p in parts if p))
    return "\n".join(lines)


def evidence_for(subject: str, sources: list[str], trends: list[dict[str, Any]],
                 limit: int = 6, brands: Iterable[str] = ()) -> list[str]:
    """挑出支撑选题的热点原文:被引用的链接 + 提到这台车的条目(榜单优先)。

    这些行建项目时会作为一份事实素材进入抽取 —— 角度里的「月销 5 万」「召回」
    只出现在热点里,不传进去的话事实表里没有,脚本开场就只能凭空写。
    """
    key = _norm(subject)
    picked: list[dict[str, Any]] = []
    for t in trends:
        car = _norm(t.get("car", ""))
        hit = ((t.get("url") and t.get("url") in sources)
               or (car and (car in key or key in car))
               or (key and key in _norm(t.get("title", ""))))
        if hit and t not in picked:
            picked.append(t)
    if not picked:
        # 母婴新闻很少写全型号:按 subject 里的品牌兜底,别让依据空着
        bs = _brands_in(key, brands)
        picked = [t for t in trends if any(b in _norm(t.get("title", "")) for b in bs)]
    picked.sort(key=lambda t: t.get("kind") not in ("model", "rank"))
    lines = []
    for t in picked[:limit]:
        parts = [f"[{t.get('source', '')}] {t.get('title', '')}"]
        # 榜单条目的标题只有名次("销量榜第 3"),车名在 car 里:不写上,横评时
        # 各车抽取分不清这一行是谁的名次
        for label, k in (("车系", "car"), ("指标", "metric"), ("日期", "date"), ("链接", "url")):
            if t.get(k):
                parts.append(f"{label}:{t[k]}")
        lines.append(" | ".join(parts))
    return lines


def roster_evidence(roster: list[str], sources: list[str], trends: list[dict[str, Any]],
                    limit: int = 12) -> list[str]:
    """横评选题的热点依据:每台车各挑几条,合起来不超过 `limit`(建项目接口的上限)。"""
    per = max(1, limit // max(1, len(roster)))
    out: list[str] = []
    for car in roster:
        for line in evidence_for(car, sources, trends, limit=per):
            if line not in out:
                out.append(line)
    return out[:limit]


def roundup_roster(raw: Any, corpus: str, cars: list[str],
                   count: tuple[int, int]) -> list[str]:
    """横评车单:清洗、去重、只留热点里出现过的车;台数不在范围内返回 []。"""
    if isinstance(raw, str):
        raw = re.split(r"[、,，/]+", raw)
    out: list[str] = []
    seen: set[str] = set()
    for item in raw or []:
        name = clean_subject(str(item or ""))
        key = _norm(name)
        if name and key not in seen and _mentioned(name, corpus, cars):
            seen.add(key)
            out.append(name)
    lo, hi = count
    return out[:hi] if len(out) >= lo else []


def forbidden_terms(profile: CategoryProfile) -> list[str]:
    return list(dict.fromkeys((*profile.forbidden_terms, *AD_ABSOLUTE_TERMS)))


def _brands_in(key: str, brands: Iterable[str]) -> list[str]:
    """subject(已 _norm)里出现的品牌词(已 _norm,至少 2 字)。"""
    out = []
    for b in brands:
        nb = _norm(b)
        if len(nb) >= 2 and nb in key:
            out.append(nb)
    return out


def _mentioned(subject: str, corpus: str, cars: list[str], brands: Iterable[str] = ()) -> bool:
    """subject 是否真的出现在热点里(或与某个榜单车系互相包含)。

    `brands` 非空时(没有榜单的品类)再放宽一档:subject 里的某个品牌在热点里出现过。
    """
    s = _norm(subject)
    if not s:
        return False
    if s in corpus:
        return True
    if any(c and (c in s or s in c) for c in cars):
        return True
    return any(b in corpus for b in _brands_in(s, brands))


def suggest(
    items: Iterable[Any],
    *,
    profile: CategoryProfile,
    llm,
    n: int = 8,
    playbook: str = "",
) -> list[TopicIdea]:
    """由热点生成选题。无热点或无 LLM 时返回空列表。

    `playbook` 是复盘官的「已验证打法 / 应避免」(`team.playbook.prompt_text`),附在热点后面。"""
    items = list(items)
    if not items or llm is None:
        return []
    trends = [_trend_dict(i) for i in items[:MAX_TREND_ITEMS]]
    vocab = profile.vocab
    themes = profile.themes
    theme_desc = "\n".join(
        f"- {name}:{spec.label}。{(spec.intent or '').strip()[:120]}"
        for name, spec in themes.items()
    )
    rtheme = roundup_theme(profile)
    roundup_rule = ROUNDUP_RULE.format(name=rtheme.name, lo=rtheme.subject_count[0],
                                       hi=rtheme.subject_count[1], n=n) if rtheme else ""
    vtheme = versus_theme(profile)
    if vtheme:
        roundup_rule += VERSUS_RULE.format(name=vtheme.name, lo=vtheme.subject_count[0], n=n)
    for t in band_themes(profile):
        roundup_rule += BAND_RULE.format(name=t.name, label=t.label, lo=t.subject_count[0],
                                         hi=t.subject_count[1], n=n)
    for t in themes.values():
        if t.requires_angle and not t.multi:
            roundup_rule += FOCUS_RULE.format(name=t.name)
    spec = profile.trends
    # 有榜单(懂车帝)的品类车系名就是权威,不按品牌放宽 —— 否则「小米YU9」这种编的也能过
    brands = () if spec.rank else profile.trend_brands
    excluded = [_norm(w) for w in spec.exclude_words if _norm(w)]
    data = llm.structured(
        SYSTEM.format(
            sources_desc=spec.sources_desc,
            subject_rule=spec.subject_rule,
            priority_rule=spec.priority_rule,
            domain=vocab.domain,
            subject_label=vocab.subject_label,
            n=n,
            forbidden="、".join(f"「{t}」" for t in forbidden_terms(profile)),
            themes=", ".join(themes),
            theme_desc=theme_desc,
            roundup_rule=roundup_rule,
        ),
        "实时热点:\n" + format_trends(trends) + (f"\n\n{playbook}" if playbook else ""),
        required_keys=["topics"],
        temperature=0.8,
    )

    corpus = _norm(" ".join(f"{t.get('title', '')} {t.get('car', '')}" for t in trends))
    cars = [_norm(t["car"]) for t in trends if t.get("car")]
    urls = {str(t.get("url")) for t in trends if t.get("url")}
    per_subject: dict[str, int] = {}
    seen_angles: set[str] = set()
    out: list[TopicIdea] = []
    for raw in data.get("topics") or []:
        if not isinstance(raw, dict):
            continue
        theme = str(raw.get("theme") or "").strip()
        if theme not in themes:
            theme = profile.default_theme
        angle = str(raw.get("angle") or "").strip()
        roster: list[str] = []
        if themes[theme].multi:
            # 横评:subject 是标题(不用在热点里出现),车单每款都要在热点里出现过
            subject = str(raw.get("subject") or "").strip()[:40]
            roster = roundup_roster(raw.get("subjects"), corpus, cars, themes[theme].subject_count)
            if not subject or not angle or not roster:
                continue
        else:
            subject = clean_subject(raw.get("subject") or "")
            if not subject or not angle or not _mentioned(subject, corpus, cars, brands):
                continue
        if any(w in _norm(subject + angle) for w in excluded):
            continue
        # 角度带禁用词,脚本必然被 lint 拦下 —— 这种选题直接不给
        if forbidden_in(angle, profile) or forbidden_in(subject, profile):
            continue
        key = _norm(subject)
        if per_subject.get(key, 0) >= 2 or _norm(angle) in seen_angles:
            continue
        try:
            heat = max(1, min(5, int(raw.get("heat") or 3)))
        except (TypeError, ValueError):
            heat = 3
        srcs = raw.get("sources") or []
        if isinstance(srcs, str):
            srcs = [srcs]
        srcs = [str(u) for u in srcs if str(u) in urls]
        hook = str(raw.get("hook") or "").strip()
        if len(hook) > HOOK_MAX_CHARS or forbidden_in(hook, profile):
            hook = ""
        out.append(TopicIdea(
            subject=subject,
            angle=angle[:120],
            theme=theme,
            hook=hook,
            why_hot=str(raw.get("why_hot") or "").strip(),
            # 只保留真在热点里出现过的链接,LLM 自编的 url 一律丢掉
            sources=srcs,
            heat=heat,
            search_terms=" ".join(str(raw.get("search_terms") or "").split())[:40],
            evidence=(roster_evidence(roster, srcs, trends) if roster
                      else evidence_for(subject, srcs, trends, brands=brands)),
            subjects=roster,
        ))
        per_subject[key] = per_subject.get(key, 0) + 1
        seen_angles.add(_norm(angle))
        if len(out) >= n:
            break
    out.sort(key=lambda t: -t.heat)
    return out
