"""口播清洗与口播 lint。"""
from __future__ import annotations

import re
from typing import Sequence
from ...models import ScriptSegment

from .lint_base import LintIssue
from .hook import first_sentence
from .segments import SHOOT_CHARS_MAX, _SENT_END, _split_keep


# --------------------------------------------------------------------------
# 口播清洗:碎句、半句、自曝
#
# 成片里"是不折腾。""能确定的是。""我没有可靠数字,不乱说。"这类句子 TTS 单独念出来
# 像报幕,也是最容易听出 AI 味的地方。提示词里已经写了规则,这里是确定性的兜底:
# 模型没听话时不靠人在门禁 A 逐句找。
# --------------------------------------------------------------------------
#: 比这短的一拍是碎句(开场钩子除外,它本来就要短)
FRAGMENT_CHARS = 9
#: 合并后最多放宽到一拍上限的几倍(字幕自己会换行,略长不碍事)
MERGE_SLACK = 1.25
#: 句尾挂着这些词,这句话没说完
_DANGLING_TAILS = ("的是", "就是", "还有", "比如", "例如", "以及", "来说", "而言", "的话", "如果",
                   "因为", "所以", "但是", "不过", "然后", "并且", "是", "比", "和", "跟", "与",
                   "及", "或", "在", "把", "被", "对", "给", "而", "但")
#: 口播自曝:在片子里交代"我缺数据 / 我不乱说"。观众听到的是 AI 在打补丁,不是车评人
_SELF_EXPOSE = re.compile(
    r"不乱说|不乱讲|不敢乱说|不瞎说|不乱下结论"
    r"|我(?:这边|这里|手上)?(?:没有|没法|没办法|无法|没查到|查不到|不敢|不太确定|不确定|不知道)"
    r"[^。!?;;\n]{0,12}(?:数字|数据|确切|可靠|准确|依据|说|给你|保证)"
    r"|没有(?:可靠|确切|公开|准确)的?(?:数字|数据|信息)")
_TERMINAL_STOPS = "。;；"
_PUNCT_RE = re.compile(r"[\uff0c,\u3002\u3001;\uff1b:\uff1a!?\uff01\uff1f\s]")
_TAIL_PUNCT_RE = re.compile(r"[\uff0c,\u3002\u3001;\uff1b:\uff1a!?\uff01\uff1f\s]+$")


def _plain_len(text: str) -> int:
    return len(_PUNCT_RE.sub("", text or ""))


def ends_dangling(text: str) -> bool:
    """这句话是不是没说完("现代 Ioniq 5 的四驱版比。""能确定的是。")。"""
    t = _TAIL_PUNCT_RE.sub("", text or "")
    return bool(t) and t.endswith(_DANGLING_TAILS)


def strip_self_exposure(text: str) -> str:
    """去掉自曝句,以及它留下的半截话("……不乱说。能确定的是。" 里的「能确定的是。」)。
    没有自曝句的文本原样返回(不改标点)。"""
    sents = _split_keep(text, _SENT_END)
    if not any(_SELF_EXPOSE.search(s) for s in sents):
        return text
    kept: list[str] = []
    cut_prev = False
    for s in sents:
        if _SELF_EXPOSE.search(s):
            cut_prev = True
            continue
        if cut_prev and ends_dangling(s) and _plain_len(s) <= 12:
            continue  # 自曝句后面那句"能确定的是"没了下文
        cut_prev = False
        kept.append(s)
    return "".join(kept).strip()


def _join_spoken(a: str, b: str) -> str:
    """把两句话接成一句:前一句的句号换成逗号;问号、感叹号保留。"""
    a = (a or "").rstrip()
    if a and a[-1] in _TERMINAL_STOPS:
        a = a[:-1] + "，"
    return a + (b or "").lstrip()


def clean_spoken(segments: Sequence[ScriptSegment],
                 max_chars: int = SHOOT_CHARS_MAX) -> list[ScriptSegment]:
    """口播清洗:去自曝句,把碎句和半句并进相邻的拍。

    - 第一拍(钩子)和最后一拍(结论)不动,碎句也不并进钩子:黄金三秒要短;
    - 半句("……的四驱版比。")优先和**后一拍**合,话还没说完;碎句("价格是阶梯式的。")
      优先并进**前一拍**,当补充说明;
    - 只在同一段落、同一台车内合并,事实引用取并集;合并后太长就放弃,留给 lint 提示。
    """
    out: list[ScriptSegment] = []
    for seg in segments:
        text = strip_self_exposure((seg.text or "").strip())
        if text:
            out.append(seg.model_copy(update={"text": text, "fact_refs": list(seg.fact_refs)}))

    limit = int(max_chars * MERGE_SLACK)
    k = 1
    while 0 < k < len(out) - 1:
        s = out[k]
        dangling = ends_dangling(s.text)
        if not dangling and _plain_len(s.text) >= FRAGMENT_CHARS:
            k += 1
            continue
        for side in (("next", "prev") if dangling else ("prev", "next")):
            j = k + 1 if side == "next" else k - 1
            if j == 0:
                continue
            other = out[j]
            if other.section != s.section or other.subject != s.subject:
                continue
            first, second = (s, other) if side == "next" else (other, s)
            if len(first.text) + len(second.text) > limit:
                continue
            merged = first.model_copy(update={
                "text": _join_spoken(first.text, second.text),
                "fact_refs": list(dict.fromkeys([*first.fact_refs, *second.fact_refs])),
                "heading": first.heading or second.heading})
            lo = min(k, j)
            out[lo:lo + 2] = [merged]
            k = max(1, lo)
            break
        else:
            k += 1
    for n, seg in enumerate(out):
        seg.idx = n
    return out


#: 一种句式全片用几次以内算正常(超了就是在套模板)
PATTERN_MAX = 3
_PATTERNS = {
    "「不是……,是……」": re.compile(r"不是[^。!?;！？；]{1,14}[,，]是"),
    "「代价是……」": re.compile(r"代价(?:是|就是)"),
    "「原理是 / 原理上……」": re.compile(r"原理(?:是|上)"),
    "「这是取舍」": re.compile(r"这是取舍|是取舍"),
}


def spoken_lint(segments: Sequence[ScriptSegment]) -> list[LintIssue]:
    """口播毛病(warning):碎句、半句、自曝句、同一句式用太多次。

    确定性清洗(`clean_spoken`)做完还剩下的才会报到这里 —— 通常是合并后太长、
    或者跨段落没法并的,需要人在门禁 A 改稿。"""
    issues: list[LintIssue] = []
    last = len(segments) - 1
    for n, seg in enumerate(segments):
        text = seg.text or ""
        if _SELF_EXPOSE.search(text):
            issues.append(LintIssue(seg.idx, "warning",
                                    f"口播自曝(AI 在交代缺数据),整句删掉:{first_sentence(text)}"))
        elif ends_dangling(text) and n != last:
            issues.append(LintIssue(seg.idx, "warning", f"半句收尾,话没说完:{text[-16:]}"))
        elif 0 < n < last and _plain_len(text) < FRAGMENT_CHARS:
            issues.append(LintIssue(seg.idx, "warning", f"碎句(念出来像报幕),并进前后一句:{text}"))
    whole = "\n".join(s.text or "" for s in segments)
    for name, pat in _PATTERNS.items():
        count = len(pat.findall(whole))
        if count > PATTERN_MAX:
            issues.append(LintIssue(0, "warning", f"句式 {name} 全片用了 {count} 次,换着说"))
    return issues
