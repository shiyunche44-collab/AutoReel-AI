"""各项机械校验(warning / error)。"""
from __future__ import annotations

import re
from typing import Sequence
from ...categories import CategoryProfile, Vocab
from ...models import Fact, ScriptSegment
from ..grounding import YEAR_RE, arabicize, differing_measures, numbers_in
from .. import variety

from .lint_base import LintIssue, _MODEL_CODE_RE, _YEAR_RE, has_numeric_claim
from .prompt import REVIEW_TOPIC, matched_principles, usable_facts
from .scrub import _is_comparison_section
from .hook import _BASIS_WINDOW


#: "所以呢"的痕迹:换算成场景、跟别的比、点出对谁。含数字的段一个都没有,就是在念参数
_MEANING_WORDS = ("意味着", "相当于", "等于", "也就是", "换句话说", "够", "比", "同级", "同价位",
                  "适合", "不适合", "如果你", "对你", "你", "家里", "通勤", "高速", "长途",
                  "停车", "车位", "地库", "一家", "孩子", "娃", "老人", "所以", "差不多",
                  "多出", "少了", "省下", "多花", "只要", "就能", "代价")

#: 含数字的段里,多少比例没有"所以呢"才提醒(偶尔一两段纯参数是正常的)
MEANING_MISS_RATIO = 0.4


def meaning_lint(segments: Sequence[ScriptSegment]) -> list[LintIssue]:
    """参数流还是判断流(warning)。

    专业车评的每个数字后面都跟一句"对你意味着什么";只报参数,观众听完记不住任何一句。
    按全片算比例,只报一条,免得每段一条把真正的问题淹掉。
    """
    numeric = [s for s in segments if has_numeric_claim(s.text)]
    bare = [s for s in numeric if not any(w in (s.text or "") for w in _MEANING_WORDS)]
    if not numeric or len(bare) / len(numeric) < MEANING_MISS_RATIO:
        return []
    shown = "、".join(str(s.idx + 1) for s in bare[:8])
    return [LintIssue(bare[0].idx, "warning",
                      f"{len(bare)}/{len(numeric)} 段只报参数、没有\"所以呢\"(第 {shown} 段):"
                      "每个数字后面接一句对观众意味着什么 —— 换算成场景、跟同级比、或点出适合谁")]


def depth_lint(
    segments: Sequence[ScriptSegment],
    profile: CategoryProfile,
    facts: Sequence[Fact],
) -> list[LintIssue]:
    """技术深度检查:空话(挡)、该讲原理的段落没讲(提醒)、数字没带口径(提醒)。

    合规 lint 管"不许错",这里管"不够专业":只念参数、用"很猛"代替论据,
    观众听完不知道这台车为什么好、好在哪、代价是什么。

    空洞评价是 **error**:profile 的 script_rules 把它列为硬规则
    ("不许用'很猛'这类空洞评价代替论据"),而且它是成片里最容易被观众听出来的
    一类偷懒 —— 模型写"这个数据很猛"是因为拿不出论据,与其放过去让人在门禁 A
    里逐条找,不如在这里直接拦住。其余两条是 warning。
    """
    vocab = profile.vocab
    principle_ids = {p.id for p in profile.principles}
    by_id = {f.id: f for f in facts}
    issues: list[LintIssue] = []

    for seg in segments:
        text = seg.text or ""
        hit = next((t for t in vocab.filler_terms if t in text), None)
        if hit:
            issues.append(LintIssue(seg.idx, "error",
                                    f"空洞评价「{hit}」:说清楚比什么、好在哪,或删掉:{text[:40]}"))
        if has_numeric_claim(text):
            said = numbers_in(text)
            missing = []
            for r in seg.fact_refs:
                claim = by_id[r].claim if r in by_id else ""
                for t in vocab.basis_terms:
                    pos = claim.find(t)
                    if pos < 0 or t in text:
                        continue
                    # 只有句子里的数字恰好是这个口径下的数,才需要说口径(讲价格不用说 CLTC)
                    if numbers_in(claim[pos:pos + len(t) + _BASIS_WINDOW]) & said:
                        missing.append(t)
            if missing:
                issues.append(LintIssue(seg.idx, "warning",
                                        f"数字没带口径:引用的事实是 {missing[0]} 口径,"
                                        f"句子里应说明(如「{missing[0]} 标称」):{text[:40]}"))

    # 无出处的技术名词:原理库认得这个词,但事实里根本没提 —— 多半是模型
    # 凭印象补的配置("空悬""四驱"),这台车未必有
    fact_text = "\n".join(f.claim for f in facts if f.enabled).lower()
    for seg in segments:
        low = (seg.text or "").lower()
        ghost = next((t for p in profile.principles for t in p.terms
                      if t.lower() in low and t.lower() not in fact_text), None)
        if ghost:
            issues.append(LintIssue(seg.idx, "warning",
                                    f"「{ghost}」不见于任何事实,这台车未必有这项配置:{seg.text[:40]}"))

    available = bool(matched_principles(profile, facts))
    for section in vocab.depth_sections:
        segs = [s for s in segments if s.section == section]
        if not segs or not available:
            continue
        if not any(r in principle_ids for s in segs for r in s.fact_refs):
            issues.append(LintIssue(segs[0].idx, "warning",
                                    f"「{section}」只列了参数,没有引用任何原理(k_)讲清为什么"
                                    "和代价 —— 技术深度不够"))
    return issues


def is_hero(fact: Fact, vocab: Vocab) -> bool:
    """行家硬料:圈速、制动距离、刚度、保值率这类最能说服懂车观众的事实。"""
    return any(t.lower() in fact.claim.lower() for t in vocab.hero_terms)


def term_lint(segments: Sequence[ScriptSegment], profile: CategoryProfile) -> list[LintIssue]:
    """错误的部件名(error)。

    「碳陶卡钳」:事实原文是「卡钳 + 碳陶瓷制动盘」,模型压短句子时把两个部件
    并成了一个不存在的东西。数字全对也没用 —— 弹幕第一条就是"卡钳哪来的碳陶"。
    """
    issues: list[LintIssue] = []
    for seg in segments:
        hit = next((t for t in profile.vocab.term_guard if t in (seg.text or "")), None)
        if hit:
            issues.append(LintIssue(seg.idx, "error",
                                    f"部件名错误「{hit}」:{profile.vocab.term_guard[hit]}"))
    return issues


#: 推算数字的限定词:没有它,"按电池÷续航粗算"的电耗听起来就是官方电耗
_HEDGE_WORDS = ("约", "粗算", "推算", "折算", "算下来", "大概", "左右", "差不多", "合")


#: 推算事实末尾的出处注记
_SOURCE_NOTE_RE = re.compile(r"[(（][^()（）]*源自[^()（）]*[)）]")


def derived_lint(segments: Sequence[ScriptSegment], facts: Sequence[Fact]) -> list[LintIssue]:
    """推算数字没说是推算(warning)。"""
    by_id = {f.id: f for f in facts}
    issues: list[LintIssue] = []
    for seg in segments:
        text = seg.text or ""
        for r in seg.fact_refs:
            f = by_id.get(r)
            if f is None or f.source_kind != "derived":
                continue
            # 只看推算出来的那个数:推算句里也写着源头的 73kWh、643 公里,那些是官方数
            src = by_id.get(f.source_query)
            # 去掉「(推算,源自 f_63d4…)」:事实 ID 里的数字不是推算出来的数
            claim = _SOURCE_NOTE_RE.sub("", f.claim)
            own = numbers_in(claim) - (numbers_in(src.claim) if src else set())
            if own & numbers_in(text) and not any(w in text for w in _HEDGE_WORDS):
                issues.append(LintIssue(seg.idx, "warning",
                                        f"这个数是推算的({f.id}),句子里要说「约 / 粗算」,"
                                        f"不然听起来像官方数据:{text[:40]}"))
                break
    return issues


def conflict_lint(segments: Sequence[ScriptSegment], facts: Sequence[Fact]) -> list[LintIssue]:
    """引用的数字和另一条事实对同一参数给的数不一样(warning)。

    「零百 2.92 秒」(官方)和「零百 2.98 秒,不含起步」(媒体)同时在库:念哪个都行,
    但不说口径,懂行的观众会拿另一个数来反驳。
    """
    pool = [f for f in usable_facts(facts) if f.source_kind != "derived" and not f.compare_with]
    issues: list[LintIssue] = []
    seen: set[frozenset[str]] = set()  # 同一对事实全片只提一次
    for seg in segments:
        said = numbers_in(seg.text or "")
        hit = None
        for r in seg.fact_refs:
            a = next((f for f in pool if f.id == r), None)
            if a is None:
                continue
            for b in pool:
                if b.id == a.id or b.id in seg.fact_refs or frozenset((a.id, b.id)) in seen:
                    continue
                for label, unit, va, vb in differing_measures(a.claim, a.basis, b.claim, b.basis):
                    if numbers_in(" ".join(va)) & said:
                        hit = (a, b, label, unit, va, vb)
                        break
                if hit:
                    break
            if hit:
                break
        if hit and _names_year(seg.text or "", hit[0]):
            hit = None  # 「2025款标准版 835 公里」已经说清是哪一款
        if hit:
            a, b, label, unit, va, vb = hit
            seen.add(frozenset((a.id, b.id)))
            issues.append(LintIssue(seg.idx, "warning",
                                    f"「{label or unit}」有两个数:{'/'.join(sorted(va))}{unit}({a.id},"
                                    f"口径 {a.basis or '未注明'})和 {'/'.join(sorted(vb))}{unit}"
                                    f"({b.id},口径 {b.basis or '未注明'}),句子里说清是哪个口径"))
    return issues


def _names_year(text: str, fact: Fact) -> bool:
    years = YEAR_RE.findall(f"{fact.basis} {fact.claim}")
    return any(f"{y}款" in text or f"{y} 款" in text or f"{y}年" in text for y in years)


#: 事实里写明的百分比区间("10%-80%")
_PCT_RANGE_RE = re.compile(r"(\d+)\s*%\s*[-–—~～至到]\s*(\d+)\s*%")


def range_lint(segments: Sequence[ScriptSegment], facts: Sequence[Fact]) -> list[LintIssue]:
    """事实写了区间条件,句子用了这个数却没说区间(warning)。

    「12 分钟充电」和「10%-80% 充 12 分钟」是两回事:不说区间,观众会以为是充满。
    """
    by_id = {f.id: f for f in facts}
    issues: list[LintIssue] = []
    for seg in segments:
        text = seg.text or ""
        said = numbers_in(text)
        for r in seg.fact_refs:
            f = by_id.get(r)
            if f is None:
                continue
            for clause in re.split(r"[,，;；。]", f.claim):
                m = _PCT_RANGE_RE.search(clause)
                if not m:
                    continue
                bound = {m.group(1), m.group(2)}
                if (numbers_in(clause) - bound) & said and not bound <= said:
                    issues.append(LintIssue(seg.idx, "warning",
                                            f"事实写的是 {m.group(1)}%-{m.group(2)}% 区间,"
                                            f"句子里要带上:{text[:40]}"))
                    break
            else:
                continue
            break
    return issues


def hero_lint(segments: Sequence[ScriptSegment], profile: CategoryProfile,
              facts: Sequence[Fact]) -> list[LintIssue]:
    """事实里有行家硬料,全片却一条没用(warning,全片一条)。"""
    hero = [f for f in usable_facts(facts) if not f.compare_with and is_hero(f, profile.vocab)]
    if not hero or not segments:
        return []
    used = {r for s in segments for r in s.fact_refs}
    if any(f.id in used for f in hero):
        return []
    shown = ";".join(f.claim[:24] for f in hero[:3])
    return [LintIssue(segments[0].idx, "warning",
                      f"有 {len(hero)} 条行家硬料一条没用({shown}…):"
                      "懂车的观众最想听的是这些,挑一条放进开场或卖点段")]


def _rival_aliases(name: str) -> set[str]:
    """竞品的几种叫法:「特斯拉Model Y」也可以只说「Model Y」,「岚图追光S」可以说「追光S」。"""
    out = {name}
    latin = re.search(r"[A-Za-z][A-Za-z0-9 .\-]*[A-Za-z0-9]", name)
    if latin and len(latin.group(0)) >= 3:
        out.add(latin.group(0).strip())
    if len(name) > 4:
        out.add(name[2:])  # 去掉两个字的品牌名
    return out


def rival_lint(segments: Sequence[ScriptSegment], facts: Sequence[Fact]) -> list[LintIssue]:
    """竞品对比段没点名竞品(warning)。

    「同价位比,它的牌面是…」—— 跟谁比?看选购视频的人要的是车名。
    事实里现成有对比料(compare_with)时,对比段至少要点到其中一台。
    """
    rivals = {f.compare_with for f in usable_facts(facts) if f.compare_with}
    segs = [s for s in segments if _is_comparison_section(s.section)]
    if not rivals or not segs:
        return []
    if any(alias in (s.text or "") for s in segs for r in rivals for alias in _rival_aliases(r)):
        return []
    return [LintIssue(segs[0].idx, "warning",
                      f"竞品对比段没点名任何一台竞品:事实里现成有和{'、'.join(sorted(rivals))}"
                      "的对比,至少点一台的名字和同一维度的数字")]


#: 结论段的识别词。主论点要在这里落地。
_CONCLUSION_SECTION_WORDS = ("结论",)


def _is_conclusion_section(section: str) -> bool:
    return any(w in (section or "") for w in _CONCLUSION_SECTION_WORDS)


def thesis_lint(segments: Sequence[ScriptSegment], thesis: str) -> list[LintIssue]:
    """结论段有没有回扣主论点。

    主论点(全片那句判断)是视频的脊椎:每一段为它服务,最后一段把它落地。
    提示词要求结论段**原样包含**这句话,所以这里是精确匹配。
    """
    if not thesis.strip():
        return []
    ends = [s for s in segments if _is_conclusion_section(s.section)]
    if not ends:
        return []
    if any(thesis in (s.text or "") for s in ends):
        return []
    return [LintIssue(ends[-1].idx, "warning",
                     f"结论段没有回扣主论点「{thesis}」——"
                     f"全片每一段都为它服务,最后一段要把它落地"
                     f"(原样写进结论即可):{ends[-1].text[:40]}")]


#: 第一人称声称亲身体验的通用说法(品类词在 vocab.firsthand_terms)
_FIRSTHAND_TERMS = ("亲测", "亲身体验", "我亲自", "我实测", "我用下来", "我体验过")
_FIRSTHAND_DENIED_RE = re.compile(r"(?:不是|并非|没有|没)我?(?:亲测|亲身体验|亲自|实测|开过|试驾)")


def firsthand_lint(segments: Sequence[ScriptSegment], profile: CategoryProfile) -> list[LintIssue]:
    """第一人称假体验(error):账号没开过这台车,这么写就是撒谎。转述要说明是谁的体验。"""
    terms = (*_FIRSTHAND_TERMS, *profile.vocab.firsthand_terms)
    issues: list[LintIssue] = []
    for seg in segments:
        # 「这不是我亲测」是在交代来源,不算假体验
        text = _FIRSTHAND_DENIED_RE.sub("", seg.text or "")
        hit = next((t for t in terms if t in text), None)
        if hit:
            issues.append(LintIssue(seg.idx, "error",
                                    f"第一人称声称亲身体验「{hit}」:没有开过不许这么写,"
                                    f"改成转述并说明来源,或删掉:{seg.text[:40]}"))
    return issues


def attribution_lint(segments: Sequence[ScriptSegment], profile: CategoryProfile,
                     facts: Sequence[Fact]) -> list[LintIssue]:
    """引用了体验转述事实,却没说是谁说的(warning):听起来就成了作者自己的体验。"""
    reviews = {f.id for f in facts if f.topic == REVIEW_TOPIC}
    words = profile.vocab.attribution_words
    # 按段落查:拆拍时每一拍都带着整段的引用,来源通常只在其中一拍说一次
    by_section: dict[str, list[ScriptSegment]] = {}
    for seg in segments:
        by_section.setdefault(seg.section, []).append(seg)
    issues: list[LintIssue] = []
    for group in by_section.values():
        text = "".join(s.text or "" for s in group)
        if any(reviews.intersection(s.fact_refs) for s in group) and not any(w in text for w in words):
            issues.append(LintIssue(group[0].idx, "warning",
                                    f"引用了体验转述事实却没说来源,补上是谁说的:{text[:40]}"))
    return issues


#: 个位数多半是「3 个版本」「第 2 点」这种计数,报出来全是噪声
_NUMBER_SOURCE_MIN = 10


def _plain_numbers(text: str) -> list[float]:
    """文本里的数值(年款和型号代号不算)。"""
    stripped = _MODEL_CODE_RE.sub("", _YEAR_RE.sub("", arabicize(text or "")))
    return [float(x) for x in re.findall(r"\d+(?:\.\d+)?", stripped)]


def _spellings(value: float) -> set[str]:
    """一个数算出来后可能的写法:取整、留一到两位,再去掉小数点(和 numbers_in 同一口径)。"""
    forms = {f"{value:.0f}", f"{value:.1f}", f"{value:.2f}"}
    return {(f.rstrip("0").rstrip(".") if "." in f else f).replace(".", "") for f in forms}


def _backed_numbers(values: Sequence[float]) -> set[str]:
    """引用事实里的数,以及它们的换算、差值、比值、百分比。"""
    known: set[str] = set()
    for a in values:
        known |= _spellings(a) | _spellings(a / 1000) | _spellings(a / 10000)
        for b in values:
            if a > b > 0:
                known |= _spellings(a - b) | _spellings(a / b) | _spellings(b / a * 100)
                known |= _spellings((a - b) / a * 100) | _spellings((a - b) / b * 100)
    return known


def number_source_lint(segments: Sequence[ScriptSegment], facts: Sequence[Fact]) -> list[LintIssue]:
    """数字有引用,但引用的事实里没有这个数(warning)。

    合规 lint 只查"带数字的句子有没有引用",查不出「每天通勤 50 公里」挂着一条无关事实。
    差值、比值、换算算有来源:算账是分析的一部分。先观察,误报少再升 error。"""
    by_id = {f.id: f for f in facts}
    issues: list[LintIssue] = []
    for seg in segments:
        said = [v for v in _plain_numbers(seg.text) if v >= _NUMBER_SOURCE_MIN]
        if not said:
            continue
        cited = [by_id[r] for r in seg.fact_refs if r in by_id]
        if not cited:
            continue  # 完全没引用的情况规则 1 已经拦了
        known = _backed_numbers([v for f in cited for v in _plain_numbers(f"{f.claim} {f.basis}")])
        loose = sorted({f"{v:g}" for v in said if not _spellings(v) & known})
        if loose:
            issues.append(LintIssue(seg.idx, "warning",
                                    f"数字 {'、'.join(loose)} 在引用的事实里找不到,"
                                    f"也不是它们的差值或比值,核对出处:{(seg.text or '')[:40]}"))
    return issues


def stock_phrase_lint(segments: Sequence[ScriptSegment], profile: CategoryProfile) -> list[LintIssue]:
    """换了车还在说的套话和念出来的提纲词(warning):听多了就是模板。"""
    whole = "\n".join(s.text or "" for s in segments)
    issues: list[LintIssue] = []
    for word in profile.vocab.scaffold_words:
        if word in whole:
            issues.append(LintIssue(0, "warning", f"提纲用语「{word}」念出来了,换成自然的说法"))
    for phrase in profile.vocab.stock_phrases:
        count = whole.count(phrase)
        if count > 1:
            issues.append(LintIssue(0, "warning", f"套话「{phrase}」全片用了 {count} 次,最多一次"))
    return issues


def ending_lint(segments: Sequence[ScriptSegment],
                ending: variety.Ending | None = None) -> list[LintIssue]:
    """结尾要给行动,不只给总结。

    复盘原话:「现在结尾停在『标准版闭眼入』,没有下一步。补一句提问
    (你在纠结哪台?评论区说车型,我下期做横评)或下期预告 —— 评论区互动和连看
    都靠这一句。」

    只报 warning:结尾怎么写是风格问题,不该拦住出片。
    按项目变体的结尾写法查(二选一要有「还是」和问号,下期预告要有「下期」……),
    判决式结尾不查这条(由 thesis_lint 查有没有回扣主论点)。
    """
    ending = ending or variety.ENDINGS[variety.DEFAULT_ENDING]
    if not segments or not ending.markers:
        return []
    last = segments[-1]
    text = last.text or ""
    if all(any(w in text for w in group) for group in ending.markers):
        return []
    return [LintIssue(last.idx, "warning", ending.hint)]


def duplicate_lint(
    segments: Sequence[ScriptSegment],
    facts: Sequence[Fact],
) -> list[LintIssue]:
    """重复检测:同一条事实被多个章节反复引用、同一个数字在多段重复出现。

    车评最常见的观感问题是"这段刚才不是讲过了吗"。实测典型:同一张价格表在
    「定位与价格」和「版本选择」各讲一遍,后者只是复读,没有新增信息。

    这里只报 warning:价格在「定位」和「版本选择」里各出现一次是合理的,
    真正的重复靠**跨章节引用同一条事实**来判定更准 —— 同一个章节里连续几段
    引用同一条事实是正常的(那一段的参数本来就出自它)。
    """
    issues: list[LintIssue] = []
    by_id = {f.id: f for f in facts}

    # 1. 同一条事实被几个不同章节引用
    seen: dict[str, list[int]] = {}
    for seg in segments:
        for ref in seg.fact_refs:
            seen.setdefault(ref, []).append(seg.idx)
    for ref, idxs in seen.items():
        secs = sorted({segments[i].section for i in idxs if i < len(segments)})
        if len(secs) >= 2:
            claim = by_id[ref].claim[:26] if ref in by_id else ref
            issues.append(LintIssue(idxs[0], "warning",
                                    f"同一条事实被「{'、'.join(secs)}」多个章节引用"
                                    f"({claim}):这几段是不是在重说同一件事?"))

    # 2. 同一个数字在多段重复出现。跳过个位数(3 块屏、3 米轴距不算重复)
    #    和年款年份(2026 款会出现在好几段)。
    hits: dict[str, list[int]] = {}
    for seg in segments:
        for n in numbers_in(seg.text or ""):
            if len(n) < 2 or _YEAR_RE.fullmatch(n):
                continue
            hits.setdefault(n, []).append(seg.idx)
    for n, idxs in hits.items():
        if len(idxs) >= 3:
            where = "、".join(str(i + 1) for i in idxs)
            issues.append(LintIssue(idxs[0], "warning",
                                    f"数字 {n} 在第 {where} 段反复出现,考虑删掉重复的一次"))
    return issues


#: 事实密度(每分钟引用多少条不同事实)。太稀是在讲空话,太密观众记不住
FACTS_PER_MIN_LOW = 2.5
FACTS_PER_MIN_HIGH = 14.0
#: 一段(一拍)里同时出现的不同数字超过这个数,观众跟不上
NUMBERS_PER_BEAT_MAX = 4


def density_lint(segments: Sequence[ScriptSegment], profile: CategoryProfile,
                 facts: Sequence[Fact]) -> list[LintIssue]:
    """信息密度(warning):每分钟事实数、每拍数字数。

    分钟数按字数除以横屏语速估算(脚本阶段还没有真实时长);只提醒,不拦流程。
    """
    from .fit import LANDSCAPE_CHARS_PER_SECOND

    if not segments:
        return []
    known = {f.id for f in facts}
    principle_ids = {p.id for p in profile.principles}
    cited = {r for s in segments for r in s.fact_refs if r in known and r not in principle_ids}
    chars = sum(len(s.text or "") for s in segments)
    minutes = chars / LANDSCAPE_CHARS_PER_SECOND / 60 if chars else 0.0
    issues: list[LintIssue] = []
    if minutes >= 1.0:
        rate = len(cited) / minutes
        if rate < FACTS_PER_MIN_LOW:
            issues.append(LintIssue(segments[0].idx, "warning",
                                    f"信息偏稀:约 {minutes:.1f} 分钟只引用了 {len(cited)} 条事实"
                                    f"(每分钟 {rate:.1f} 条,建议不少于 {FACTS_PER_MIN_LOW})"))
        elif rate > FACTS_PER_MIN_HIGH:
            issues.append(LintIssue(segments[0].idx, "warning",
                                    f"信息过密:约 {minutes:.1f} 分钟引用了 {len(cited)} 条事实"
                                    f"(每分钟 {rate:.1f} 条,建议不超过 {FACTS_PER_MIN_HIGH:.0f})"))
    for s in segments:
        n = len(numbers_in(s.text or ""))
        if n > NUMBERS_PER_BEAT_MAX:
            issues.append(LintIssue(s.idx, "warning",
                                    f"这一段塞了 {n} 个不同数字(建议不超过 {NUMBERS_PER_BEAT_MAX}),"
                                    "观众来不及消化,拆成两拍或挑最关键的"))
    return issues
