"""口语二改:稿子写完、压完字数之后,让 LLM 只换说法再过一遍。

第一遍写稿要同时管事实、论证和字数,出来的话偏书面。这一步只管"像人说话",所以可以放开改,
但它碰不得内容:不能出现原稿没有的数字,口径、转述来源都逐段核对,对不上的段退回原句。
次要的数字可以删,连报参数才听着不像念配置表;删掉的数字对应的事实引用一起摘掉,事实 lint 才看得见。
整步失败就用原稿,不拦出片。
"""
from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping, Sequence
from typing import Any

from ...categories import CategoryProfile
from ...models import Fact, ScriptSegment
from ..derive import LEDGER_TOPIC, ledger_numbers
from ..grounding import numbers_in
from .hook import first_sentence
from .lint_base import log
from .lint_checks import _HEDGE_WORDS
from .lint_main import compliance_lint
from .prompt import banned_terms, reviewer_identity
from .spoken import _SELF_EXPOSE

#: 一段改完的字数相对原文的上下限:加了太多是在添内容,少了太多是丢内容
LENGTH_LIMITS = (0.7, 1.15)
#: 短段落多几个字不算添内容
LENGTH_SLACK = 4
#: 一段至少留下这个比例的数字:删是为了不念配置表,不是把算过的账改成空话
MIN_NUMBERS_KEPT = 0.5
#: 一段的数字到这么多就算连报,给模型一个删减目标。只说"可以删"的话它一个都不删
CROWDED_NUMBERS = 8
CROWDED_TARGET = 0.7
_LATIN_RE = re.compile(r"[A-Za-z][A-Za-z0-9-]*")
#: 含转述词单字的普通词:「数据」里的「据」不是在说「据报道」,「有人会说」是自问自答
_NOT_ATTRIBUTION_RE = re.compile(r"数据|依据|证据|根据|占据|用户群|有人会说|有人说")
#: 约数词后面同一分句里最近的那个数,以及「100km左右」这种说在后面的
_HEDGED_RE = re.compile("(?:" + "|".join(_HEDGE_WORDS)
                        + r")[^,，。!?！？;；\d]{0,10}(\d+(?:\.\d+)?)|(\d+(?:\.\d+)?)[^\d,，。]{0,5}左右")
_COMPLETE_END = "。!?！？…"

SPOKEN_SYSTEM = """你是{voice}

下面是你写好的一条关于「{subject}」的解说稿,事实和论证都定了。现在把它念一遍,改成你平时跟朋友讲这件事的说法。
观点和论证不动,只换说法。别只删几个字、换几个标点:每一句都重新想一遍,嘴上会怎么说就怎么写,整段念完要听得出是另一个人在讲。

要改的:
- 书面腔换成说话的词,念着拗口的就重新组织;
- 把观众当成对面坐着的人,该说「你」就说「你」,用「如果你」把参数接到他自己的情况上;
- 句子长短错开:长句能断就断,一句话说完一个意思;
- 短句要带着判断(「未必。」「这钱不值。」),不要写预告下文的短句(「这也要看条件。」「但你要留心。」),直接说内容;
- 说话没有分号,用句号或逗号;
- 段与段之间别用「先……」「再……」排队,也不要每段都一个节奏;
- 资料名不要念(「专业数据集」「老款资料」):老款的数就说老款,其余的数直接讲;
- 「我没能核实」这种交代去掉,想提醒就对观众说,比如让他下单前核对配置表;
- 带 max_numbers 的段落数字太多,听着像念配置表,必须删到这个数以内(同一个数说两遍算一个)。只留撑得住这段判断的数,其余的直接不提,或者用不带数字的话带过,比如「功率和扭矩也跟着涨了」。价格、结论靠它的数要留;一笔账里的数要么都留,要么都不提,别只剩半截;
- 口头的连接词可以加,但同一个全片最多两次,别让它变成新的口头禅。

不许动的:
- 留下的数字和单位原样照写,不换算、不改写法,不许出现原稿没有的数字;
- 不新增任何事实、配置、技术名词、人物、情节或亲身体验;
- 「CLTC 标称」这类口径、「约」「粗算」这类说明,都留着;
- 「有车主反映」「有媒体评测」这类转述来源留着,也不要新加来源;
- 第 1 段的第一句一个字不改;
- 不许用这些词:{banned}。

字数:每段和原来差不多,不超过原来的 1.1 倍。

只输出 JSON:{{"segments": [{{"i": 段号, "text": "改好的这一段"}}]}},每段都要有。"""


def _attributions(text: str, profile: CategoryProfile) -> set[str]:
    plain = _NOT_ATTRIBUTION_RE.sub("", text)
    return {w for w in profile.vocab.attribution_words if w in plain}


def _hedged_numbers(text: str) -> set[str]:
    return {(a or b).replace(".", "") for a, b in _HEDGED_RE.findall(text)}


def _lost_hedge(old: str, new: str) -> bool:
    """原来带「约」「粗算」的数还留着,却不带了。这个数整个删掉不算。"""
    return bool((_hedged_numbers(old) & numbers_in(new)) - _hedged_numbers(new))


def _changed_content(old: str, new: str, profile: CategoryProfile, first: bool,
                     keep: frozenset[str] = frozenset()) -> str:
    """改稿动了内容就返回原因,没动返回空串。"""
    size = len(old)
    if not LENGTH_LIMITS[0] * size <= len(new) <= LENGTH_LIMITS[1] * size + LENGTH_SLACK:
        return f"字数 {size} → {len(new)}"
    if new[-1] not in _COMPLETE_END:
        return "没有说完"
    before, after = numbers_in(old), numbers_in(new)
    if after - before:
        return f"多了数字 {sorted(after - before)}"
    if len(after) < MIN_NUMBERS_KEPT * len(before):
        return f"数字删太多 {len(before)} → {len(after)}"
    if (before & keep) - after:
        return f"删了论点或差价账里的数字 {sorted((before & keep) - after)}"
    added = set(_LATIN_RE.findall(new)) - set(_LATIN_RE.findall(old))
    if added:
        return f"多了词 {sorted(added)}"
    vocab = profile.vocab
    if any(term in old and term not in new for term in vocab.basis_terms):
        return "丢了口径"
    if _lost_hedge(old, new):
        return "丢了约数说明"
    words_old, words_new = _attributions(old, profile), _attributions(new, profile)
    if words_new - words_old or (words_old and not words_new):
        return f"转述来源变了 {sorted(words_old ^ words_new)}"
    if _SELF_EXPOSE.search(new):
        return "在交代缺数据"
    if first and first_sentence(old) != first_sentence(new):
        return "改了开场第一句"
    return ""


def _live_refs(seg: ScriptSegment, new: str, facts_by_id: Mapping[str, Fact]) -> list[str]:
    """摘掉这段不再说到的事实:原来念了它的数、改完一个都不剩的。不带数字的事实不动。"""
    before, after = numbers_in(seg.text), numbers_in(new)
    live = []
    for ref in seg.fact_refs:
        fact = facts_by_id.get(ref)
        said = numbers_in(fact.claim) & before if fact else set()
        if not said or said & after:
            live.append(ref)
    return live


def _draft_item(n: int, seg: ScriptSegment) -> dict[str, Any]:
    item: dict[str, Any] = {"i": n, "section": seg.section, "text": seg.text}
    count = len(numbers_in(seg.text))
    if count >= CROWDED_NUMBERS:
        item["max_numbers"] = math.ceil(count * CROWDED_TARGET)
    return item


def rewrite_spoken(segments: Sequence[ScriptSegment], *, subject: str, profile: CategoryProfile,
                   facts: Sequence[Fact], llm, theme=None, thesis: str = "",
                   variant: Mapping[str, Any] | None = None,
                   angle: str = "") -> list[ScriptSegment]:
    """返回改好的稿子。段数、顺序、事实引用都不变;没通过核对的段保留原句。"""
    original = list(segments)
    if not original:
        return original
    system = SPOKEN_SYSTEM.format(voice=reviewer_identity(profile.vocab),
                                  subject=subject, banned=banned_terms(profile))
    draft = json.dumps([_draft_item(n, s) for n, s in enumerate(original)], ensure_ascii=False)
    try:
        data = llm.structured(system, draft, required_keys=["segments"], temperature=0.6)
    except Exception as exc:  # noqa: BLE001 - 二改失败不拦出片
        log.warning("口语二改失败,用原稿:%s", exc)
        return original

    # 个位数多半是「一档」「三句话」展开来的,不算论点里的数字。算过的账也不能改成空话
    keep = frozenset(n for n in numbers_in(thesis) if len(n) > 1).union(
        *(ledger_numbers(f.claim) for f in facts if f.topic == LEDGER_TOPIC))
    facts_by_id = {f.id: f for f in facts}
    out = list(original)
    for item in data.get("segments") or []:
        if not isinstance(item, dict):
            continue
        try:
            n = int(item.get("i"))
        except (TypeError, ValueError):
            continue
        text = str(item.get("text") or "").strip()
        if not text or not 0 <= n < len(out):
            continue
        reason = _changed_content(original[n].text, text, profile, n == 0, keep)
        if reason:
            log.info("口语二改退回第 %d 段(%s):%s", n + 1, reason, text[:30])
            continue
        dropped = numbers_in(original[n].text) - numbers_in(text)
        if dropped:
            log.info("口语二改第 %d 段删了数字:%s", n + 1, sorted(dropped))
        out[n] = original[n].model_copy(update={
            "text": text, "fact_refs": _live_refs(original[n], text, facts_by_id)})

    def errors(segs: Sequence[ScriptSegment]):
        return [i for i in compliance_lint(segs, profile, facts, theme=theme, thesis=thesis,
                                           variant=variant, angle=angle) if i.is_error]

    known = {i.message for i in errors(original)}
    fresh = [i for i in errors(out) if i.message not in known]
    if fresh:
        for issue in fresh:
            if 0 <= issue.segment_idx < len(out):
                log.info("口语二改退回第 %d 段(lint):%s", issue.segment_idx + 1, issue.message[:60])
                out[issue.segment_idx] = original[issue.segment_idx]
        if any(i.message not in known for i in errors(out)):
            log.warning("口语二改过不了 lint,用原稿")
            return original
    return out
