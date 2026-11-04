"""车评分析:写稿前先想清楚这台车的核心矛盾和要论证什么。

主论点和全文在同一次调用里产出时,模型没有"想"这一步,写出来的章节就是参数表。
这里先单独做一次分析,结果作为论证大纲传给脚本,也存进 `review_brief.json`。
分析失败不挡流程:没有大纲时脚本照常写。
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence
from ...categories import CategoryProfile, Slot
from ...models import Fact
from ..derive import LEDGER_TOPIC

from .lint_base import log
from .prompt import (
    COMPARE_TOPIC,
    CONTROVERSY_TOPIC,
    HEADING_MAX_CHARS,
    OWNER_TOPIC,
    REVIEW_TOPIC,
    reviewer_identity,
    banned_terms,
    usable_facts,
)


#: 论点少于这个数,大纲撑不起全片:按没有分析处理
MIN_ARGUMENTS = 2

BRIEF_SYSTEM = """你是{voice}

现在还不写稿。先分析「{subject}」,得出这期片子要论证什么。只能依据下面的事实清单:
- 判断、推断可以大胆下,包括{company}的意图、它的短板、谁不该买;
- 不许出现事实清单里没有的数字、配置、技术名词;
- 别人的使用感受只能来自标了「体验转述」的事实;你没有亲身用过,不许假装用过;
- 标了「对比」的事实只放进 rival,不放进 arguments;
- 标了「算账」的事实是算好的版本差价账,有的话至少一条论点要把它写进 fact_refs,reasoning 里说清这笔钱换来什么、值不值;
- 清单里没写到不等于没有:不许从「没看到差异」推出「没差别、不值、不必买」,资料不全也不算短板,判断和短板都要有写明的事实撑着。
- 判断、章名、主论点里不许出现这些词:{banned};

本期分析视角:{lens}
{extra}
好的判断是有人会反对的。{example}。不要照抄这个例子,判断要从这份事实清单里来。
每条论点要换一种论证手法:
{moves}

只输出 JSON,字段:
- tension:它的核心矛盾,一句话,哪两件事在打架;
- maker_bet:{company}在赌什么,一句话;
- thesis:主论点,30 字以内的判断,有取舍,不是广告语;
- arguments:恰好 {n_args} 条,按论证的先后排,每条 {{"claim": 判断, "heading": "章名({heading_max} 字以内,讲观点,不要写成部位或功能的名字)",
  "fact_refs": ["事实ID"], "reasoning": "由这些事实怎么推出判断", "counter": "反方怎么说,或判断在什么情况下不成立", "move": "用的论证手法"}};
- weakness:真实的短板,{{"claim": 判断, "heading": 章名, "fact_refs": [...], "reasoning": 依据和影响谁}};
- rival:最该比的对手和比什么,{{"claim": 判断, "heading": 章名, "fact_refs": [...], "reasoning": 差距对谁重要}},没有「对比」事实就给空对象。

事实清单:
{facts}"""


def _fact_line(f: Fact) -> str:
    # 对决片的事实两台车混在一起,不标车名分析会张冠李戴
    tags = [f.subject] if f.subject else []
    if f.compare_with:
        tags.append(f"对比[{f.compare_with}]")
    if f.topic == REVIEW_TOPIC:
        tags.append("体验转述")
    if f.topic == OWNER_TOPIC:
        tags.append("本人亲历")
    if f.topic == LEDGER_TOPIC:
        tags.append("算账")
    if f.topic == CONTROVERSY_TOPIC:
        tags.append("争议")
    tag = f" [{' · '.join(tags)}]" if tags else ""
    return f"- {f.id}{tag} | {f.claim} | 口径:{f.basis or '未注明'}"


def _refs(raw: Any, known: set[str]) -> list[str]:
    raw = [raw] if isinstance(raw, str) else (raw or [])
    return [str(r) for r in raw if str(r) in known]


def _point(raw: Any, known: set[str]) -> dict[str, Any] | None:
    """一条论点:判断是必需的,引用里不存在的 ID 去掉。"""
    if not isinstance(raw, Mapping) or not str(raw.get("claim") or "").strip():
        return None
    out = {k: str(raw.get(k) or "").strip() for k in ("claim", "heading", "reasoning", "counter", "move")}
    out["heading"] = out["heading"][:HEADING_MAX_CHARS]
    out["fact_refs"] = _refs(raw.get("fact_refs"), known)
    return out


def write_brief(*, subject: str, profile: CategoryProfile, facts: Sequence[Fact], llm,
                n_args: int = 4, lens: str = "", angle: str = "", why: str = "",
                history: Sequence[Mapping[str, Any]] = (), rule: str = "",
                sections: Sequence[str] = ()) -> dict[str, Any] | None:
    """做一次车评分析。LLM 失败或论点不足时返回 None(照常写稿)。

    `lens`:分析视角 id(见 vocab.lenses),同一台车换视角才写得出不一样的片子。
    `why`:选题依据。`history`:同车已做过的片子({thesis, headings}),要求换论点。
    `rule` / `sections`:片型对论点的要求和论点对应的段落(问答的问题、对决的维度)。"""
    facts = usable_facts(facts)
    if not facts:  # 没事实可分析:模型只会写一篇「资料缺失」当论点
        return None
    own = {f.id for f in facts if not f.compare_with}
    compare = {f.id for f in facts if f.compare_with or f.topic == COMPARE_TOPIC}
    vocab = profile.vocab
    said = [h for h in history if h.get("thesis")]
    extra = "".join((
        f"本期选题角度:{angle}\n" if angle else "",
        f"这期为什么现在做:{why}\n" if why else "",
        "这个产品之前做过这些片子,这次要换论点,不许重复它们的主论点和章名:\n"
        + "".join(f"- {h['thesis']}(章名:{'、'.join(h.get('headings') or []) or '无'})\n" for h in said)
        if said else "",
        f"本片型的要求:{rule}\n" if rule else "",
        "arguments 依次对应这些段落:\n"
        + "".join(f"- {s}:{profile.section(s).beat}\n" for s in sections)
        if rule and sections else "",
    ))
    system = BRIEF_SYSTEM.format(
        voice=reviewer_identity(vocab),
        company=vocab.company_noun, banned=banned_terms(profile), example=vocab.judgment_example or "判断要有取舍,不是复述参数",
        subject=subject, lens=vocab.lenses.get(lens) or "由你判断这台车最值得从哪个角度讲",
        extra=extra, moves="\n".join(f"- {m}" for m in vocab.review_moves) or "- 替反方说话",
        n_args=n_args, heading_max=HEADING_MAX_CHARS,
        facts="\n".join(_fact_line(f) for f in facts) or "(无)")
    try:
        data = llm.structured(system, f"分析「{subject}」,输出 JSON。",
                              required_keys=["tension", "thesis", "arguments"], temperature=0.7)
    except Exception as exc:  # noqa: BLE001 —— 分析是加分项,失败不拦脚本
        log.warning("车评分析失败,不带大纲写稿:%s", exc)
        return None
    points = [p for p in (_point(a, own) for a in data.get("arguments") or []) if p][:n_args]
    if len(points) < min(MIN_ARGUMENTS, n_args) or not str(data.get("thesis") or "").strip():
        log.warning("车评分析不可用(%d 条论点),不带大纲写稿", len(points))
        return None
    return {
        "lens": lens,
        "tension": str(data.get("tension") or "").strip(),
        "maker_bet": str(data.get("maker_bet") or "").strip(),
        "thesis": str(data.get("thesis")).strip(),
        "arguments": points,
        "weakness": _point(data.get("weakness"), own),
        "rival": _point(data.get("rival"), compare),
    }


def align_slots(slots: Sequence[Slot], brief: Mapping[str, Any] | None,
                profile: CategoryProfile) -> list[Slot]:
    """分析给的论点比论点段少时,多出来的段并进前一个论点,不让脚本硬凑一章。段数不变。"""
    if not brief:
        return list(slots)
    room = len(brief.get("arguments") or [])
    seen: list[str] = []
    out: list[Slot] = []
    for s in slots:
        if profile.section(s.section).argument:
            if s.section not in seen and len(seen) < room:
                seen.append(s.section)
            if s.section not in seen and seen:
                out.append(Slot(seen[-1], s.subject))
                continue
        out.append(s)
    return out


def argument_slots(plan: Sequence[str], profile: CategoryProfile) -> list[str]:
    """段落序列里的论点段(去重,保持先后)。"""
    return list(dict.fromkeys(s for s in plan if profile.section(s).argument))


def _point_line(label: str, p: Mapping[str, Any]) -> str:
    parts = [f"章名「{p['heading']}」" if p.get("heading") else "",
             f"判断:{p['claim']}",
             f"依据:{'、'.join(p['fact_refs'])}" if p.get("fact_refs") else "",
             f"推理:{p['reasoning']}" if p.get("reasoning") else "",
             f"反方或边界:{p['counter']}" if p.get("counter") else "",
             f"手法:{p['move']}" if p.get("move") else ""]
    return f"- {label}:" + ";".join(x for x in parts if x)


def _section_point(section: str, point: Mapping[str, Any], profile: CategoryProfile) -> str:
    """没有 heading 的段落(「为什么」「对决价格」)不给章名:脚本用不上,还会让它多写一行。"""
    return _point_line(section, point if profile.section(section).heading else {**point, "heading": ""})


def outline_block(brief: Mapping[str, Any] | None, plan: Sequence[str],
                  profile: CategoryProfile) -> str:
    """脚本提示词里的论证大纲 + 每段要做的事。论点按段落序列里的先后对号入座。"""
    beats = [f"- {s}:{profile.section(s).beat}" for s in dict.fromkeys(plan) if profile.section(s).beat]
    lines = ["\n\n每段要做的事:", *beats] if beats else []
    if not brief:
        return "\n".join(lines)
    outline = ["\n\n本片论证大纲(写稿前做的分析,照它的论点和先后写,用自己的话说,别照念;"
               "「判断」「反方」这些标签不要念出来,反方只在最有争议的一两章说;"
               "大纲不能替代事实引用,数字仍要写 fact_refs):"]
    if brief.get("tension"):
        outline.append(f"- 核心矛盾:{brief['tension']}")
    if brief.get("maker_bet"):
        outline.append(f"- {profile.vocab.company_noun}在赌什么:{brief['maker_bet']}")
    outline.append(f"- 主论点(thesis 字段原样用这句):{brief['thesis']}")
    for section, point in zip(argument_slots(plan, profile), brief.get("arguments") or [], strict=False):
        outline.append(_section_point(section, point, profile))
    for key, words in (("weakness", ("短板", "代价")), ("rival", ("竞品", "对比"))):
        point = brief.get(key)
        section = next((s for s in dict.fromkeys(plan) if any(w in s for w in words)), "")
        if point and section:
            outline.append(_section_point(section, point, profile))
    return "\n".join(outline) + "\n".join(lines)
