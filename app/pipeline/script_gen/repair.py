"""门禁 A 之前的自修:把机器查得出、也修得了的两类错在写稿当场修掉。

原来 lint 只报不修:「段落含数字但没有任何事实引用」「引用了不存在的事实 ID」一出现,
项目就停在门禁 A 等人(2026-11 上旬钛7、MONA 返工版都卡在这),人不在就断更。
换说法的重复(结论段把主论点再说两三遍)lint 查不出,成片出来才被打回。

修的顺序由便宜到贵:
1. 不存在的事实 ID 直接摘掉;
2. 带数字却没引用的段落,先在事实表里按数字找出处(找得到就补引用,稿子一个字不动);
3. 找不到出处的交给 LLM 改写(最多 `REPAIR_PASSES` 次):有出处的数字补引用,没出处的改成定性说法;
4. 还不行就删掉带数字的句子 —— 宁可少说一个参数,也不带着没出处的数字出片。

**不新增数字**:每一步的结果都要过「数字都在引用的事实里」这道检查,不过就不用。
"""
from __future__ import annotations

import re
from collections.abc import Sequence

from ...categories import CategoryProfile
from ...models import Fact, ScriptSegment
from ..grounding import arabicize, numbers_in, price_band, strip_band
from .lint_base import _MODEL_CODE_RE, _YEAR_RE, has_numeric_claim, log
from .scrub import _is_comparison_section

#: LLM 改写一段最多试几次
REPAIR_PASSES = 2
#: 给 LLM 看的事实最多这么多条
REPAIR_FACTS_MAX = 60
#: 两句话的二元组重合到这个比例算「换说法重复」,后出现的那句删掉
NEAR_REPEAT = 0.6
#: 重合到这个比例 lint 直接报 error(自修之后不该再有;人改稿改回来了就拦)
NEAR_REPEAT_ERROR = 0.75
#: 短于这么多字的句子不比(口头语、追问本来就像)
NEAR_MIN_CHARS = 10

REPAIR_SYSTEM = """你是解说视频的事实核对编辑。下面这段口播里有数字,但没有标出处。对照事实清单把它改对。

规则:
- 数字能在事实清单里找到:那句原样保留,fact_refs 填上对应事实的 id;
- 找不到出处的数字:把那句改成不带任何数字的定性说法(也不许写成中文数字),意思和语气不变;
- 不新增事实清单以外的数字,不加长,不改这段的立场,不加开场白;
- 输出 JSON:{"text": "改好的整段口播", "fact_refs": ["事实 id"]}"""

_SENT_RE = re.compile(r"[^。!?!?;；]+[。!?!?;；]*")
_CLAUSE_RE = re.compile(r"[^,,、]+[,,、]*")
_STRIP_RE = re.compile(r"[\s，,。.!！?？;；:：、\"'“”‘’()()《》…—-]+")


def sentences(text: str) -> list[str]:
    return [m.group(0) for m in _SENT_RE.finditer(text or "") if m.group(0).strip()]


def _claim_numbers(text: str) -> set[str]:
    """这段话里要出处的数字(年款年份、型号代号里的不算)。"""
    return numbers_in(_MODEL_CODE_RE.sub("", _YEAR_RE.sub("", arabicize(text or ""))))


def _fact_numbers(fact: Fact) -> set[str]:
    return numbers_in(f"{fact.claim} {fact.basis}")


def _candidates(seg: ScriptSegment, facts: Sequence[Fact]) -> list[Fact]:
    """这段可以引用的事实:没被剔除、不是别的车的、对比事实只给对比段。产品级的排前面。"""
    comparison = _is_comparison_section(seg.section)
    out = [f for f in facts if f.enabled
           and (not seg.subject or not f.subject or f.subject == seg.subject)
           and (not f.compare_with or comparison)]
    return sorted(out, key=lambda f: f.scope != "product")


def match_refs(seg: ScriptSegment, facts: Sequence[Fact], band=None) -> list[str]:
    """按数字在事实表里找出处。两位以上的数字(含小数)全部找到才算;找不全返回空。

    个位整数不要求对上,也不能单独当依据:「一周一充」「3 块屏」里的数到处都是,
    3 块屏和 3 秒不是一回事(`_grounded` 验改写稿也是这个口径)。"""
    left = {n for n in _claim_numbers(strip_band(seg.text or "", band)) if len(n) >= 2}
    if not left:
        return []
    picked: list[str] = []
    for fact in _candidates(seg, facts):
        hit = left & _fact_numbers(fact)
        if not hit:
            continue
        picked.append(fact.id)
        left -= hit
        if not left or len(picked) >= 3:
            break
    return picked if not left else []


def _grounded(text: str, refs: Sequence[str], by_id: dict[str, Fact], band=None) -> bool:
    """改写稿过不过:没数字,或者有引用且两位以上的数字都在引用的事实里。"""
    plain = strip_band(text or "", band)
    if not has_numeric_claim(plain):
        return True
    cited = [by_id[r] for r in refs if r in by_id]
    if not cited:
        return False
    known = set().union(*(_fact_numbers(f) for f in cited))
    return all(n in known for n in _claim_numbers(plain) if len(n) >= 2)


def _rewrite(seg: ScriptSegment, facts: Sequence[Fact], by_id: dict[str, Fact], llm,
             band=None) -> tuple[str, list[str]] | None:
    table = "\n".join(f"{f.id} | {f.claim}" + (f" | 口径:{f.basis}" if f.basis else "")
                      for f in _candidates(seg, facts)[:REPAIR_FACTS_MAX])
    user = f"【这段口播】\n{seg.text}\n\n【事实清单】\n{table or '(没有可用事实)'}"
    limit = len(seg.text or "") * 1.3 + 10
    for _ in range(REPAIR_PASSES):
        try:
            data = llm.structured(REPAIR_SYSTEM, user, required_keys=["text"], temperature=0.2)
        except Exception as exc:  # noqa: BLE001 - 改写失败就走删句兜底
            log.warning("事实引用自修失败:%s", exc)
            continue
        text = arabicize(str(data.get("text") or "").strip())
        refs = data.get("fact_refs") or []
        refs = [refs] if isinstance(refs, str) else refs
        refs = [str(r) for r in refs if str(r) in by_id]
        if text and len(text) <= limit and _grounded(text, refs, by_id, band):
            return text, refs
    return None


def strip_numeric(text: str, band=None) -> str:
    """删掉带数字的句子;整段都带数字时退到删带数字的分句。可能返回空串。"""
    keep = [s for s in sentences(text) if not has_numeric_claim(strip_band(s, band))]
    if keep:
        return "".join(keep).strip()
    clauses = [m.group(0) for m in _CLAUSE_RE.finditer(text or "")]
    kept = "".join(c for c in clauses if not has_numeric_claim(strip_band(c, band))).strip()
    kept = re.sub(r"[,,、]+$", "。", kept)
    return kept if len(_STRIP_RE.sub("", kept)) >= 6 else ""


def repair_grounding(segments: Sequence[ScriptSegment], facts: Sequence[Fact],
                     profile: CategoryProfile, llm=None,
                     angle: str = "") -> tuple[list[ScriptSegment], list[str]]:
    """修「数字没引用 / 引用不存在」。返回(新稿, 每处改动的说明)。"""
    by_id = {f.id: f for f in facts}
    principle_ids = {p.id for p in profile.principles}
    band = price_band(angle)
    out: list[ScriptSegment] = []
    notes: list[str] = []
    last = len(segments) - 1
    for pos, seg in enumerate(segments):
        refs = [r for r in seg.fact_refs if r in by_id or r in principle_ids]
        if len(refs) != len(seg.fact_refs):
            gone = [r for r in seg.fact_refs if r not in refs]
            notes.append(f"第 {seg.idx + 1} 段:摘掉不存在的事实引用 {'、'.join(gone)}")
            seg = seg.model_copy(update={"fact_refs": refs})
        real = [r for r in seg.fact_refs if r not in principle_ids]
        if not has_numeric_claim(strip_band(seg.text or "", band)) or real:
            out.append(seg)
            continue
        found = match_refs(seg, facts, band)
        if found:
            notes.append(f"第 {seg.idx + 1} 段:按数字补上事实引用 {'、'.join(found)}")
            out.append(seg.model_copy(update={"fact_refs": [*seg.fact_refs, *found]}))
            continue
        fixed = _rewrite(seg, facts, by_id, llm, band) if llm is not None else None
        if fixed:
            text, new_refs = fixed
            notes.append(f"第 {seg.idx + 1} 段:数字没出处,已改写"
                         + (f"并引用 {'、'.join(new_refs)}" if new_refs else "成定性说法"))
            keep = [r for r in seg.fact_refs if r in principle_ids]
            out.append(seg.model_copy(update={"text": text, "fact_refs": [*keep, *new_refs]}))
            continue
        text = strip_numeric(seg.text or "", band)
        if text:
            notes.append(f"第 {seg.idx + 1} 段:数字没出处,删掉带数字的句子")
            out.append(seg.model_copy(update={"text": text}))
        elif pos in (0, last):
            out.append(seg)  # 开场和结论不能整段删,留给门禁 A 拦
        else:
            notes.append(f"第 {seg.idx + 1} 段:整段都是没出处的数字,删掉")
    return _reindex(out), notes


def _reindex(segments: Sequence[ScriptSegment]) -> list[ScriptSegment]:
    return [s if s.idx == i else s.model_copy(update={"idx": i}) for i, s in enumerate(segments)]


def _bigrams(text: str) -> set[str]:
    key = _STRIP_RE.sub("", text or "")
    return {key[i:i + 2] for i in range(len(key) - 1)}


def similarity(a: str, b: str) -> float:
    """两句话的二元组重合度(Jaccard)。"""
    x, y = _bigrams(a), _bigrams(b)
    return len(x & y) / len(x | y) if x and y else 0.0


def near_repeats(segments: Sequence[ScriptSegment],
                 threshold: float = NEAR_REPEAT) -> list[tuple[int, int, str, float]]:
    """换说法的重复:[(先出现的段, 后出现的段, 后出现的那句, 重合度)]。整句相同的归 repeat_lint。"""
    seen: list[tuple[int, str]] = []
    out: list[tuple[int, int, str, float]] = []
    for seg in segments:
        for sent in sentences(seg.text or ""):
            if len(_STRIP_RE.sub("", sent)) < NEAR_MIN_CHARS:
                continue
            # 数字不一样的不比:「550 Plus 12.38 万」「650 Plus 13.38 万」是并列的配置表,不是重复
            nums = numbers_in(sent)
            best = max(((similarity(sent, old), idx) for idx, old in seen
                        if numbers_in(old) == nums), default=(0.0, -1))
            if best[0] >= threshold:
                out.append((best[1], seg.idx, sent, best[0]))
            else:
                seen.append((seg.idx, sent))
    return out


def drop_near_repeats(segments: Sequence[ScriptSegment]) -> tuple[list[ScriptSegment], list[str]]:
    """后出现的近似重复句删掉。段落删空了就整段去掉(最后一段保留原样,结尾不能没有)。"""
    dupes: dict[int, list[str]] = {}
    for _first, idx, sent, _score in near_repeats(segments):
        dupes.setdefault(idx, []).append(sent)
    if not dupes:
        return list(segments), []
    out: list[ScriptSegment] = []
    notes: list[str] = []
    last = len(segments) - 1
    for pos, seg in enumerate(segments):
        drop = dupes.get(seg.idx)
        if not drop:
            out.append(seg)
            continue
        text = "".join(s for s in sentences(seg.text or "") if s not in drop).strip()
        if text:
            notes.append(f"第 {seg.idx + 1} 段:删掉和前文重复的 {len(drop)} 句(「{drop[0][:14]}」)")
            out.append(seg.model_copy(update={"text": text}))
        elif pos == last:
            out.append(seg)
        else:
            notes.append(f"第 {seg.idx + 1} 段:整段和前文重复,删掉(「{drop[0][:14]}」)")
    return _reindex(out), notes


def repair(segments: Sequence[ScriptSegment], facts: Sequence[Fact], profile: CategoryProfile,
           llm=None, angle: str = "") -> tuple[list[ScriptSegment], list[str]]:
    """写稿后的自修:先去重(少修几段),再修事实引用。"""
    segments, a = drop_near_repeats(segments)
    segments, b = repair_grounding(segments, facts, profile, llm=llm, angle=angle)
    return segments, a + b
