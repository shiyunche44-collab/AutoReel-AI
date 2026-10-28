"""② 脚本生成 + 合规 lint。

lint 是这个模块存在的核心理由。解说视频的两类翻车:
- **编造数据**:模型写出一个材料里没有的参数。
- **违反广告法**:绝对化用语("最强""第一""顶级")会让投放被拒。

两者都在这里被机械拦截,而不是指望模型自觉。
"""

from __future__ import annotations

import json  # noqa: F401
import logging  # noqa: F401
import re  # noqa: F401
from typing import Any, Callable, Iterable, Mapping, Sequence  # noqa: F401
from ...categories import CategoryProfile, Slot, Vocab  # noqa: F401
from ...models import Fact, ScriptSegment  # noqa: F401
from ..grounding import YEAR_RE, arabicize, differing_measures, numbers_in, price_band, strip_band  # noqa: F401
from .. import knobs, variety  # noqa: F401
from ..timeline import narration_budget_chars  # noqa: F401

from .lint_base import (  # noqa: E402,F401
    AD_ABSOLUTE_TERMS,
    LintIssue,
    _MODEL_CODE_RE,
    _NUMERIC_RE,
    _YEAR_RE,
    forbidden_in,
    has_numeric_claim,
    log,
)
from .prompt import (  # noqa: E402,F401
    COMPARE_TOPIC,
    CONTROVERSY_TOPIC,
    HEADING_MAX_CHARS,
    QUALITY_TOPIC,
    ROUNDUP_SCOPE_LINE,
    SYSTEM,
    _QUALITY_RULE,
    _SCOPE_POLICY_COMPANY,
    _SCOPE_POLICY_PRODUCT,
    _TITLE_POLICY_COMPANY,
    _TITLE_POLICY_PRODUCT,
    _angle_policy,
    _heading_rule,
    _hook_rule,
    _portrait_policy,
    _scope_policy,
    _theme_rules,
    _title_policy,
    matched_principles,
    roster_line,
    roundup_facts_payload,
    usable_facts,
)
from .fit import (  # noqa: E402,F401
    FIT_PASSES,
    LANDSCAPE_CHARS_PER_SECOND,
    LANDSCAPE_LENGTH_TOLERANCE,
    PORTRAIT_CHARS_PER_SECOND,
    PORTRAIT_LENGTH_TOLERANCE,
    SHORTEN_SYSTEM,
    _shorten_once,
    fit_length,
    spoken_chars,
)
from .scrub import (  # noqa: E402,F401
    _BENIGN_FIRST_RE,
    _COMPARISON_SECTION_WORDS,
    _COMPETITOR_MARKERS,
    _DISPARAGING_TERMS,
    _NOT_A_NAME,
    _PERSON_QUOTE_RE,
    _VENDOR_CLAIM_MARKERS,
    _is_comparison_section,
    strip_foreign_comparison_refs,
    vendor_attribution,
)
from .hook import (  # noqa: E402,F401
    HOOK_FILLER,
    HOOK_MAX_CHARS,
    HOOK_SYSTEM,
    JUDGE_SYSTEM,
    _BASIS_WINDOW,
    _HOOK_SECTION_WORDS,
    _STANCE_WORDS,
    _hook_ok,
    _is_hook_section,
    first_sentence,
    hook_lint,
    stance_lint,
)
from .lint_checks import (  # noqa: E402,F401
    MEANING_MISS_RATIO,
    _CONCLUSION_SECTION_WORDS,
    _HEDGE_WORDS,
    _MEANING_WORDS,
    _PCT_RANGE_RE,
    _SOURCE_NOTE_RE,
    _is_conclusion_section,
    _names_year,
    _rival_aliases,
    conflict_lint,
    depth_lint,
    derived_lint,
    duplicate_lint,
    ending_lint,
    hero_lint,
    is_hero,
    meaning_lint,
    range_lint,
    rival_lint,
    term_lint,
    thesis_lint,
)
from .lint_main import (  # noqa: E402,F401
    compliance_lint,
)
from .segments import (  # noqa: E402,F401
    MIN_PART_CHARS,
    PORTRAIT_SHOOT_CHARS_MAX,
    SHOOT_CHARS_MAX,
    _CLAUSE_END,
    _SENT_END,
    _TRAILING_MARKS,
    _hard_cut,
    _split_keep,
    _split_text,
    ensure_disclaimers,
    normalize_segments,
    split_segments,
    total_chars,
    unify_headings,
)
from .spoken import (  # noqa: E402,F401
    FRAGMENT_CHARS,
    MERGE_SLACK,
    PATTERN_MAX,
    _DANGLING_TAILS,
    _PATTERNS,
    _PUNCT_RE,
    _SELF_EXPOSE,
    _TAIL_PUNCT_RE,
    _TERMINAL_STOPS,
    _join_spoken,
    _plain_len,
    clean_spoken,
    ends_dangling,
    spoken_lint,
    strip_self_exposure,
)


def generate(
    *,
    subject: str,
    profile: CategoryProfile,
    facts: Sequence[Fact],
    plan: Sequence[str],
    duration: int,
    llm,
    theme=None,
    angle: str = "",
    slots: Sequence[Slot] | None = None,
    subjects: Sequence[dict[str, str]] | None = None,
    portrait: bool = False,
    hook: str = "",
    variant: Mapping[str, Any] | None = None,
    hook_locked: bool = False,
) -> tuple[list[ScriptSegment], str, str]:
    """生成解说脚本。返回 (段落列表, 标题, 主论点)。

    `angle`:选题角度(热点选题)。为空时 prompt 与无角度版本逐字一致。
    `slots` / `subjects`:多车横评的段落位(段落 + 车名)与车单。不传时 prompt 与单车版逐字一致。
    `portrait`:竖屏项目多一组短视频规则;横屏时 prompt 与旧版逐字一致。
    `variant`:开头 / 结尾写法(见 variety)。不传 = 片型默认变体,提示词与以前逐字一致。
    `hook_locked`:`hook` 是 `choose_hook` 挑出来的,第一句直接用。
    没挂 `learned_hint`(测试、脚本)时提示词与以前逐字一致。
    """
    roundup = bool(subjects)
    angle = (angle or "").strip()
    vocab = profile.vocab
    # 语速旋钮调快 / 调慢时,同样时长能念的字数按同一比例变(默认 1.0,字数预算不变)
    budget = narration_budget_chars(
        duration, (PORTRAIT_CHARS_PER_SECOND if portrait else LANDSCAPE_CHARS_PER_SECOND)
        * knobs.speed_factor())
    shot_count = max(1, len(plan))
    scope_label = {"product": "product", "company": "company", "industry": "industry"}
    facts = usable_facts(facts)
    if not roundup and not any(_is_comparison_section(s) for s in plan):
        # 段落计划里没有对比段(竖屏短时长会精简掉):对比事实只能用在对比段,
        # 给了 LLM 也只会写进本车段落、被 lint 拦下
        facts = [f for f in facts if not f.compare_with]
    def _line(f: Fact) -> str:
        tags = scope_label.get(f.scope, "company")
        if f.topic == QUALITY_TOPIC:
            tags += " · topic=quality"
        elif f.topic == CONTROVERSY_TOPIC:
            tags += " · 争议"
        if f.compare_with:
            tags += f" · 对比[{f.compare_with}]"
        if is_hero(f, vocab):
            tags += " · 硬料"
        return (f"- {f.id} [{tags}] | {f.claim} | "
                f"口径:{f.basis or '未注明'} | 来源:{f.source_kind or f.source_tool} | "
                f"可信度:{f.confidence}")

    if roundup:
        facts_payload = roundup_facts_payload(facts, subjects or (), _line)
    else:
        facts_payload = "\n".join(_line(f) for f in facts)
    facts_payload = facts_payload or "(无可用事实:请完全避免写出任何具体数字)"
    if roundup and slots:
        plan_lines = [f"{s.section}〔{s.subject}〕" if s.subject else s.section for s in slots]
    else:
        plan_lines = list(plan)
    principles_payload = "\n".join(
        f"- {p.id} [{' / '.join(p.terms)}] 原理:{p.explain}"
        + (f" 代价/边界:{p.tradeoff}" if p.tradeoff else "")
        for p in matched_principles(profile, facts)
    ) or "(无)"

    user = (
        f"Product subject: {subject}\n"
        f"Category: {profile.label}\n"
        f"Script style: {profile.script_style or '口语化,克制,有画面感'}\n"
        f"Total duration: {duration} seconds\n"
        f"Target total characters: {budget}\n"
        + (f"Topic angle: {angle}\n" if angle else "")
        + (f"车单:{roster_line(subjects or ())}\n" if roundup else "")
        + "\n"
        f"可用事实清单:\n{facts_payload}\n\n"
        f"段落序列:\n" + "\n".join(f"{i + 1}. {s}" for i, s in enumerate(plan_lines))
    )

    system = SYSTEM.format(
        domain=vocab.domain,
        number_examples=vocab.number_examples,
        subject_noun=vocab.subject_noun,
        category=profile.label,
        chars=budget,
        per_shot=max(20, budget // shot_count),
        facts=facts_payload,
        principles=principles_payload,
        plan="\n".join(f"{i + 1}. {s}" for i, s in enumerate(plan_lines)),
        scope_line=(ROUNDUP_SCOPE_LINE if roundup else
                    f"只讲{vocab.subject_noun}/这个品牌本身,不评价其他无关产品。"),
        keywords=", ".join(profile.visual_keywords[:4]),
        theme_intent=f"   {theme.intent}" if theme is not None and theme.intent
        else "   按品类惯例组织叙事。",
        theme_rules=_theme_rules(theme, facts, [_heading_rule(profile, plan),
                                                _hook_rule(hook, hook_locked)],
                                 variant=variant),
        rule10=variety.rule10(variant, theme),
        scope_policy=_scope_policy(theme, vocab),
        title_policy=_title_policy(theme) if theme is not None else _TITLE_POLICY_PRODUCT,
        angle_policy=_angle_policy(angle) + _portrait_policy(portrait),
        subject=subject,
    )
    hint = learned()
    if hint:
        system += LEARNED_BLOCK.format(hint=hint)
    data = llm.structured(
        system,
        user,
        required_keys=["segments", "title", "thesis"],
        temperature=0.7,
    )

    segments: list[ScriptSegment] = []
    raw_segments = data.get("segments") or []
    for i, item in enumerate(raw_segments):
        if not isinstance(item, dict):
            continue
        text = str(item.get("text", "")).strip()
        if not text:
            continue
        section = str(item.get("section", "")).strip()
        if not section:
            section = plan[i] if i < len(plan) else ""
        refs = item.get("fact_refs") or []
        if isinstance(refs, str):
            refs = [refs]
        heading = ""
        if profile.section(section).heading:
            heading = str(item.get("heading") or "").strip()[:HEADING_MAX_CHARS + 2]
        segments.append(
            ScriptSegment(
                idx=len(segments),
                section=section,
                text=text,
                fact_refs=[str(r) for r in refs if str(r)],
                heading=heading,
            )
        )

    title = str(data.get("title") or "").strip() or f"{subject} 详解"
    thesis = str(data.get("thesis") or "").strip()
    if not roundup:
        segments = strip_foreign_comparison_refs(segments, facts)
    if portrait:
        segments = fit_length(segments, budget, llm)
    else:
        segments = fit_length(segments, budget, llm, tolerance=LANDSCAPE_LENGTH_TOLERANCE,
                              keep_count=True)
    return segments, title, thesis


# --------------------------------------------------------------------------
# 账号经验回灌 + 开场多版挑选(只在服务里开:`team.playbook.install`)
# --------------------------------------------------------------------------
#: 打法手册给脚本的一段经验(开场正反例、最近诊断出的问题)。None / 返回空 = 提示词不变
learned_hint: Callable[[], str] | None = None
#: 开场写几版再挑。0 / 1 = 不挑(测试、脚本里的默认),服务里设成 `TEAM_HOOK_CANDIDATES`
hook_candidates: int = 0

LEARNED_BLOCK = """

本账号的经验(来自已发作品的真实数据,照着改进;和上面的硬性要求冲突时以硬性要求为准):
{hint}"""


def learned() -> str:
    """挂上的账号经验;没挂 / 出错 → 空串(写脚本不能因为这段提示失败)。"""
    if learned_hint is None:
        return ""
    try:
        return (learned_hint() or "").strip()
    except Exception:  # noqa: BLE001 - 经验只是锦上添花
        log.warning("读账号经验失败,这次不带", exc_info=True)
        return ""


def choose_hook(*, subject: str, profile: CategoryProfile, facts: Sequence[Fact], llm,
                n: int = 3, angle: str = "", hook: str = "") -> dict[str, Any] | None:
    """先写 `n` 版第一句,再让评审按账号经验打分,挑最高的那句。

    返回 {chosen, candidates: [{text, fact_refs, score, why}]};能用的不到两版、
    或者 LLM 出错 → None(照常写脚本,不挑)。多两次短 LLM 调用。"""
    facts = [f for f in usable_facts(facts) if not f.compare_with]
    fact_ids = {f.id for f in facts}
    hint = learned()
    extra = "".join((
        f"- 选题角度:{angle}\n" if angle else "",
        f"- 选题策划给的参考钩子:「{hook}」(可以当其中一版,也可以都写得更好)\n" if hook else "",
        f"- 本账号的经验:\n{hint}\n" if hint else "",
    ))
    try:
        data = llm.structured(
            HOOK_SYSTEM.format(subject=subject, n=n, max_chars=HOOK_MAX_CHARS, extra=extra,
                               facts="\n".join(f"- {f.id} | {f.claim}" for f in facts) or "(无)"),
            f"写 {n} 版第一句。", required_keys=["candidates"], temperature=0.9)
        cands = []
        for c in data.get("candidates") or []:
            if not isinstance(c, dict):
                continue
            text = first_sentence(str(c.get("text") or ""))
            refs = [str(r) for r in (c.get("fact_refs") or []) if str(r)]
            if _hook_ok(text, refs, profile, fact_ids) and text not in {x["text"] for x in cands}:
                cands.append({"text": text, "fact_refs": refs, "score": None, "why": ""})
        if len(cands) < 2:
            return None
        judged = llm.structured(
            JUDGE_SYSTEM.format(extra=f"- 本账号的经验(符合的加分):\n{hint}\n" if hint else ""),
            "\n".join(f"{i}. {c['text']}" for i, c in enumerate(cands)),
            required_keys=["scores"], temperature=0.2)
    except Exception:  # noqa: BLE001 - 挑不出来就照常写
        log.warning("开场多版挑选失败,照常写脚本", exc_info=True)
        return None
    for s in judged.get("scores") or []:
        try:
            i, score = int(s.get("i")), float(s.get("score"))
        except (AttributeError, TypeError, ValueError):
            continue
        if 0 <= i < len(cands):
            cands[i]["score"], cands[i]["why"] = score, str(s.get("why") or "")
    if all(c["score"] is None for c in cands):
        return None
    best = max(cands, key=lambda c: c["score"] if c["score"] is not None else -1)
    return {"chosen": best["text"], "candidates": cands}
