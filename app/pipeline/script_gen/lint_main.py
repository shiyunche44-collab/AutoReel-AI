"""compliance_lint:把各项校验串起来。"""
from __future__ import annotations

from typing import Any, Mapping, Sequence
from ...categories import CategoryProfile
from ...models import Fact, ScriptSegment
from ..grounding import price_band, strip_band
from .. import variety

from .lint_base import AD_ABSOLUTE_TERMS, LintIssue, has_numeric_claim
from .scrub import _BENIGN_FIRST_RE, _COMPETITOR_MARKERS, _DISPARAGING_TERMS, _is_comparison_section, vendor_attribution
from .hook import hook_lint, stance_lint
from .lint_checks import conflict_lint, depth_lint, derived_lint, duplicate_lint, ending_lint, hero_lint, meaning_lint, range_lint, rival_lint, term_lint, thesis_lint
from .spoken import spoken_lint


def compliance_lint(
    segments: Sequence[ScriptSegment],
    profile: CategoryProfile,
    facts: Sequence[Fact],
    theme=None,
    thesis: str = "",
    variant: Mapping[str, Any] | None = None,
    angle: str = "",
) -> list[LintIssue]:
    """机械校验脚本。error 级问题必须修复后才能进入分镜阶段。

    `variant`:项目的开头 / 结尾写法,开场立场与结尾行动这两条按它查。
    `angle`:选题角度。里面写了价位段(价位段选购),句子里复述这个价位段不算要查出处的数字。"""
    issues: list[LintIssue] = []
    known_ids = {f.id for f in facts}
    by_id = {f.id: f for f in facts}
    principle_ids = {p.id for p in profile.principles}
    band = price_band(angle)
    forbidden = list(profile.forbidden_terms)
    if theme is not None:
        forbidden.extend(theme.extra_forbidden_terms)
    allow_critique = bool(getattr(theme, "allow_subject_critique", False))
    # 有车单的片型(横评 / 对决):事实本来就带车名,规则 7 管张冠李戴
    theme_roundup = bool(getattr(theme, "multi", getattr(theme, "roundup", False)))
    # 规则 5 只在**产品是叙事主体**的方向生效。
    #
    # 它的触发词表里含"电池""充电""配置"这类词,而这些词在品牌叙事里同样高频
    # —— "这家车企最早是做电池起家的"会被判成"用公司事实讲产品参数",
    # 于是整个品牌历史方向被自己的 lint 掐死。品牌/市场方向的叙事主体本就是
    # company/industry 层,这条规则对它们没有意义,因此直接跳过。
    theme_requires_product = True if theme is None else (
        "product" in (theme.required_scopes or ("product",))
    )

    for seg in segments:
        text = seg.text or ""

        # 1. 数字必须有事实引用(年款年份除外)。原理条目不含数字,只引原理不算有出处
        fact_refs = [r for r in seg.fact_refs if r not in principle_ids]
        if has_numeric_claim(strip_band(text, band)) and not fact_refs:
            issues.append(
                LintIssue(
                    seg.idx,
                    "error",
                    f"段落含数字但没有任何事实引用:{text[:40]}..."
                    "请补充 fact_refs,或改写为不含数字的定性表述",
                )
            )

        # 2. 引用必须指向真实存在的事实
        for ref in seg.fact_refs:
            if ref not in known_ids and ref not in principle_ids:
                issues.append(
                    LintIssue(seg.idx, "error", f"引用了不存在的事实 ID:{ref}")
                )

        # 3. 品类禁用词 + 方向禁用词(广告法红线)
        text_ad = _BENIGN_FIRST_RE.sub("", text)
        for term in forbidden:
            if term in text_ad:
                issues.append(
                    LintIssue(seg.idx, "error", f"出现品类禁用词「{term}」:{text[:40]}...")
                )

        # 4. 通用绝对化用语(所有品类共享的红线)
        for term in AD_ABSOLUTE_TERMS:
            if term in text_ad:
                issues.append(
                    LintIssue(seg.idx, "error", f"出现广告法绝对化用语「{term}」")
                )

        # 5. 层级混用:用公司工商信息讲产品 —— 解说视频最典型的讲错对象。
        #    只在产品向方向生效,理由见 theme_requires_product 的注释。
        cited = [f for f in facts if f.id in seg.fact_refs]
        if theme_requires_product and cited and all(f.scope != "product" for f in cited):
            if any(keyword in text for keyword in profile.vocab.subject_words):
                issues.append(
                    LintIssue(
                        seg.idx,
                        "error",
                        "把公司层面事实当作产品参数来讲了"
                        f"(引用了 {len(cited)} 条 company/industry 事实)。"
                        "请改写为定性体验描述,或先补充产品级事实。",
                    )
                )

        # 6. 禁止贬低竞品 —— 对**所有方向**生效,不只 cost 方向。
        #    判别靠竞品标记词:允许讲本车缺点(allow_subject_critique)与
        #    不许贬低别人靠的是同一个句子里有没有点名第三方。
        if any(m in text for m in _COMPETITOR_MARKERS):
            for term in _DISPARAGING_TERMS:
                if term in text:
                    issues.append(
                        LintIssue(
                            seg.idx,
                            "error",
                            f"在评价其他品牌时使用了贬义词「{term}」:{text[:40]}..."
                            + ("本方向允许讲自身缺点,但" if allow_critique else "")
                            + "不得贬低竞品,请改写为客观的差异陈述",
                        )
                    )
                    break

        # 7. 多车横评:讲 A 车的段落不许引用 B 车的事实(参数张冠李戴)。
        #    开场 / 结论这类共用段(subject 为空)可以引用任何一台车。
        if seg.subject:
            others = sorted({f.subject for f in cited if f.subject and f.subject != seg.subject})
            if others:
                issues.append(
                    LintIssue(seg.idx, "error",
                              f"第 {seg.idx + 1} 段讲{seg.subject},却引用了"
                              f"{'、'.join(others)}的事实。请改成引用{seg.subject}自己的事实,"
                              "或把比较挪到开场 / 结论")
                )

        # 8. 对比章节不许拿厂商口径当论据。
        #    厂商自己做的消费者选择调查,拿来证明本车比对手好,是替厂商站台;
        #    车评引用它掉的是自己的可信度。改用可比参数(售价/续航/加速/配置)。
        if _is_comparison_section(seg.section):
            vendor = vendor_attribution(text)
            if vendor:
                issues.append(
                    LintIssue(
                        seg.idx,
                        "error",
                        f"对比章节拿厂商口径当论据「{vendor}」:{text[:40]}..."
                        "请换成可比参数(售价/续航/加速/配置),或删掉这句",
                    )
                )

        # 9. 对比事实(讲的是别家车的那条数字)只能出现在竞品对比段。
        #    拿到「版本选择」段里讲,就是把别家的参数安到本车头上。
        #    横评方向不管这事:那里每台车的事实本来就带车名,规则 7 已经管住了张冠李戴。
        if not theme_roundup and not _is_comparison_section(seg.section):
            for ref in seg.fact_refs:
                f = by_id.get(ref)
                if f is not None and f.compare_with:
                    issues.append(
                        LintIssue(
                            seg.idx,
                            "error",
                            f"这段引用了讲{f.compare_with}的对比事实({f.id}),"
                            "只能用在竞品对比段 —— 别家的数字不能出现在本车的段落里",
                        )
                    )
                    break

    issues.extend(hook_lint(segments))
    issues.extend(stance_lint(segments, variety.opening(variant, theme)))
    issues.extend(meaning_lint(segments))
    issues.extend(depth_lint(segments, profile, facts))
    issues.extend(term_lint(segments, profile))
    issues.extend(derived_lint(segments, facts))
    issues.extend(conflict_lint(segments, facts))
    issues.extend(range_lint(segments, facts))
    issues.extend(hero_lint(segments, profile, facts))
    issues.extend(rival_lint(segments, facts))
    issues.extend(duplicate_lint(segments, facts))
    issues.extend(thesis_lint(segments, thesis))
    issues.extend(ending_lint(segments, variety.ending(variant, theme)))
    issues.extend(spoken_lint(segments))
    return issues
