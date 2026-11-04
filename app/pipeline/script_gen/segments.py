"""段落对齐与按句拆分。"""
from __future__ import annotations

from typing import Iterable, Sequence
from ...categories import CategoryProfile, Slot
from ...models import ScriptSegment
from ..grounding import arabicize

from .hook import HOOK_MAX_CHARS


def ensure_disclaimers(
    segments: list[ScriptSegment],
    profile: CategoryProfile,
) -> list[ScriptSegment]:
    """在最后一个段落追加免责声明(仅在缺失时)。"""
    if not profile.required_disclaimers or not segments:
        return segments
    last = segments[-1]
    missing = [d for d in profile.required_disclaimers if d not in last.text]
    if missing:
        last.text = last.text.rstrip() + "".join(f"({d})" for d in missing)
    return segments


def total_chars(segments: Iterable[ScriptSegment]) -> int:
    return sum(len(s.text or "") for s in segments)


def unify_headings(segments: Sequence[ScriptSegment]) -> None:
    """同一段落(连续几项同名)用同一个 heading:取第一个非空的。

    章节条按章节名合并相邻拍,同一章里 heading 写得不一样就会被切成两章。"""
    i = 0
    while i < len(segments):
        j = i
        while j < len(segments) and segments[j].section == segments[i].section:
            j += 1
        head = next((s.heading for s in segments[i:j] if s.heading), "")
        for s in segments[i:j]:
            s.heading = head
        i = j


def normalize_segments(
    segments: Sequence[ScriptSegment],
    plan: Sequence[str] | Sequence[Slot],
) -> list[ScriptSegment]:
    """把段落数量对齐到分镜计划 —— 数量不一致会导致画面与旁白错位。

    `plan` 也可以是段落位(`Slot`):对齐后按位置写回每段讲哪台车 ——
    以段落位为准,不信 LLM 返回的车名。"""
    slots = [p if isinstance(p, Slot) else Slot(str(p)) for p in plan]
    plan = [s.section for s in slots]
    out = list(segments)
    # 段落过多:合并尾部
    while len(out) > len(plan):
        merged = out[-2].text.rstrip() + out[-1].text
        out[-2].text = merged
        out[-2].fact_refs = list(dict.fromkeys(out[-2].fact_refs + out[-1].fact_refs))
        out.pop()
    # 段落过少:复制最后一段占位,后续由用户/分镜阶段修正
    while len(out) < len(plan):
        template = out[-1] if out else ScriptSegment(idx=0, text="")
        out.append(
            ScriptSegment(
                idx=len(out),
                section=plan[len(out)] if len(out) < len(plan) else "",
                text=template.text,
                fact_refs=list(template.fact_refs),
            )
        )
    unify_headings(out)
    for i, seg in enumerate(out):
        seg.idx = i
        # LLM 偶尔整篇写中文数字:字幕全是汉字,lint 的引用检查也会被绕过
        seg.text = arabicize(seg.text)
        seg.subject = slots[i].subject if i < len(slots) else ""
    return out


#: 一拍最多多少字。按实测 TTS 语速(横屏 5.5、竖屏 4.7 字/秒,见 fit.py),28 字约 5–6 秒。
#:
#: 为什么在脚本层就拆:复盘要求「每 5–6 秒必须有一次画面变化」,而**一段脚本 = 一拍**
#: (`explainer.build_beats` 一段一版式)。拍太长,这一拍里就只剩一张画面撑着 ——
#: 实测成片 24 拍平均 8 秒一拍,后半程好几拍是同一张参数面板杵着不走。
SHOOT_CHARS_MAX = 28
#: 竖屏一拍的字数上限:约 4–5 秒换一次画面(竖屏刷得更快)
PORTRAIT_SHOOT_CHARS_MAX = 22
#: 拆出来的碎块太短就别单独成拍(2 个字的一拍纯属浪费画面)
MIN_PART_CHARS = 8
#: 句子边界(标点跟着前一句)
_SENT_END = "。!?！？;；\n"
#: 次级边界:逗号、顿号、冒号
_CLAUSE_END = "，,、：:"
#: 切在这些标点上句子最断气(「音响或电池规格分级。」),除非放不下,否则不切
_SOFT_END = "、：:"
#: 切在 `_SOFT_END` 上的代价,和「这一拍空出多少字」的平方比:约等于空出 20 字
_SOFT_CUT_PENALTY = 400


def _split_keep(text: str, chars: str) -> list[str]:
    """按 `chars` 里的标点断句,标点留在前半句。"""
    out: list[str] = []
    buf = ""
    for ch in text:
        buf += ch
        if ch in chars:
            if buf.strip():
                out.append(buf.strip())
            buf = ""
    if buf.strip():
        out.append(buf.strip())
    return out


#: 拆完落在句尾的半个标点 —— 字幕末尾挂着逗号不像话,换成句号
_TRAILING_MARKS = "，,、；;：:"


def _hard_cut(text: str, max_chars: int) -> list[str]:
    """没有标点可断的长串按 `max_chars` 切;块的后半段有空格就切在空格上。"""
    out: list[str] = []
    while len(text) > max_chars:
        cut = text.rfind(" ", max_chars // 2, max_chars + 1)
        cut = cut if cut > 0 else max_chars
        out.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if text:
        out.append(text)
    return out


def _pack_clauses(sent: str, max_chars: int, hard_max: int) -> list[str]:
    """把一个长句的分句装进尽量少、尽量匀的几拍。

    每个逗号都切一刀的话,一句话会变成三四拍短句,每拍都带句号,推理读起来全是断的。
    """
    clauses: list[str] = []
    for clause in _split_keep(sent, _CLAUSE_END):
        # 连逗号都没有:稍长一点(1.5 倍内)整句留着 —— 硬切会把词劈开
        # (实测「激光雷 / 达」);再长才硬切,优先切在空格上,宁可切坏也不能留一个 30 秒的拍
        clauses.extend([clause] if len(clause) <= hard_max * 1.5
                       else _hard_cut(clause, hard_max))
    # best[i] = 前 i 个分句的最小代价;cut[i] = 对应的上一刀位置
    best = [0.0] + [float("inf")] * len(clauses)
    cut = [0] * (len(clauses) + 1)
    for i in range(1, len(clauses) + 1):
        for j in range(i - 1, -1, -1):
            size = sum(len(c) for c in clauses[j:i])
            if size > max_chars and i - j > 1:
                break
            cost = best[j] + max(0, max_chars - size) ** 2
            if i < len(clauses) and clauses[i - 1][-1] in _SOFT_END:
                cost += _SOFT_CUT_PENALTY
            if cost < best[i]:
                best[i], cut[i] = cost, j
    parts: list[str] = []
    i = len(clauses)
    while i > 0:
        parts.append("".join(clauses[cut[i]:i]))
        i = cut[i]
    return parts[::-1]


def _split_text(text: str, max_chars: int, first_max: int | None = None,
                keep_chars: int = 0) -> list[str]:
    """把一段话切成每块不超过 `max_chars` 字的几块,尽量断在句末标点上。

    `first_max`:第一句的上限(钩子段的第一句要守黄金三秒,不能被装成一个长句)。
    `keep_chars`:一句不超过这么多字就整句留着,不从中间切(0 = 同 `max_chars`)。
    """
    parts: list[str] = []
    for n, sent in enumerate(_split_keep(text, _SENT_END)):
        limit = first_max if n == 0 and first_max else max_chars
        keep = limit if n == 0 and first_max else max(max_chars, keep_chars)
        parts.extend([sent] if len(sent) <= keep else _pack_clauses(sent, limit, max_chars))
    # 太短的碎块并到相邻块:一个 3 字的拍既撑不住画面也不像人话。
    merged: list[str] = []
    for p in parts:
        # 钩子第一句并上下一块就破了黄金三秒的字数
        cap = first_max if first_max and len(merged) == 1 else max_chars
        if merged and (len(p) < MIN_PART_CHARS or len(merged[-1]) < MIN_PART_CHARS) \
                and len(merged[-1]) + len(p) <= cap:
            merged[-1] += p
            continue
        merged.append(p)
    cap = first_max if first_max and len(merged) == 2 else max_chars
    if len(merged) > 1 and len(merged[-1]) < MIN_PART_CHARS and \
            len(merged[-2]) + len(merged[-1]) <= cap:
        merged[-2] += merged[-1]
        merged.pop()
    # 切在逗号上的块保留逗号:换成句号,一句话会被念成两句,每拍都收一次尾音。
    # 字幕末尾的逗号由 `timeline.wrap_subtitle` 去掉。
    return merged


def split_segments(segments: Sequence[ScriptSegment],
                   max_chars: int = SHOOT_CHARS_MAX,
                   keep_chars: int = 0) -> list[ScriptSegment]:
    """把过长的段落拆成句子级的小段 —— 一段 = 一拍,拍太长就没有画面变化。

    拆在句末标点;一句太长再拆到逗号。拆出来的每一段保留原来的段落名、
    车名和事实引用(引用跟着数字走,拆开也不丢出处)。

    开场段也拆:复盘要的「0–3 秒先给冲突、3–10 秒立刻兑现承诺」正好对应
    开场的第一句和第二句,拆开这两句才分得开。拆完第一段仍然是钩子段
    (`hook_lint` 只看第一段,黄金三秒的规矩不变)。
    """
    out: list[ScriptSegment] = []
    for seg in segments:
        text = (seg.text or "").strip()
        if not text:
            continue
        first_max = HOOK_MAX_CHARS if not out else None
        parts = (_split_text(text, max_chars, first_max, keep_chars)
                 if len(text) > max_chars else [text])
        for p in parts:
            out.append(ScriptSegment(idx=len(out), section=seg.section, text=p,
                                     fact_refs=list(seg.fact_refs), subject=seg.subject,
                                     heading=seg.heading))
    return out
