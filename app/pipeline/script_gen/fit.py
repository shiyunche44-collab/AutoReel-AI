"""按目标字数让 LLM 压缩脚本。"""
from __future__ import annotations

import json
import re
from typing import Sequence
from ...models import ScriptSegment

from .lint_base import log


#: 竖屏按实测 TTS 语速算字数预算。默认的 5.8 字/秒比实际快:四个成片实测
#: 4.58–4.82 字/秒(YU7 90 秒竖屏 479 字念了 104.5 秒)。竖屏 60/90 秒是平台档位,
#: 必须按真实语速算
PORTRAIT_CHARS_PER_SECOND = 4.7

#: 横屏同样按成片实测算(字数 ÷ 成片秒数,含拍间停顿):p_c113015a437b 592 字
#: 念了 108.5 秒 = 5.46 字/秒,逐句中位 5.64。横屏一拍最多 28 字、拍数少,停顿占比
#: 比竖屏小,所以比竖屏快。原来的 5.8 偏快约 6%,再叠 1.25 倍放行就能超时长三成
LANDSCAPE_CHARS_PER_SECOND = 5.5

#: 竖屏脚本念出来超过目标字数这么多倍,就让 LLM 压一遍。实测 90 秒竖屏目标 522 字,
#: 提示词里写明「硬上限」、段落砍到 6 段后仍写到 870 字 —— 光靠提示词压不住。
#: 竖屏的时长是平台规则(60/90 秒档)而不是估算,超了就不是选的那个时长
PORTRAIT_LENGTH_TOLERANCE = 1.1

#: 横屏同理但更宽松。实测 240 秒目标(预算 1392 字)写出 2272 字、成片 442 秒,
#: 超了 85%;「时长只是目标」不等于可以翻倍。横屏段落数由计划定死(对齐计划时
#: 缺段会被复制占位),所以压缩时必须保持段数,只缩每段,不许删段。
#: 1.25 放行时 120 秒目标最多能写到约 160 秒;1.15 把上限压到约 140 秒
LANDSCAPE_LENGTH_TOLERANCE = 1.15

SHORTEN_SYSTEM = """你是短视频解说的剪辑编导。下面这份脚本念出来太长,把它压到 {chars} 字以内(空白不算字)。

规则:
- 只删不加:不新增段落、不新增事实、不新增数字,不改数字;每段的 section 和 fact_refs 原样保留
  (删掉的句子里如果有数字,对应的 fact_refs 也删掉);
- 先删次要参数、重复的解释和过渡句,每段留最硬的一两个数字和一句观点;
- 可以整段删除,但第一段(开场钩子)和最后一段(结论)必须保留原意;
- 输出 JSON:{{"segments": [{{"section": "...", "text": "...", "fact_refs": ["..."]}}]}}"""


def spoken_chars(segments: Sequence[ScriptSegment]) -> int:
    """念出来的字数(空白不算)。"""
    return sum(len(re.sub(r"\s+", "", s.text or "")) for s in segments)


#: 压缩最多跑几遍。实测 LLM 一遍只删一成多(p_41e696af3941:527 → 466 字,目标 423),
#: 一遍压完还超容差就接着压,不然竖屏 90 秒稿照样念到 110 秒以上
FIT_PASSES = 4


def fit_length(segments: list[ScriptSegment], budget: int, llm,
               tolerance: float = PORTRAIT_LENGTH_TOLERANCE,
               keep_count: bool = False) -> list[ScriptSegment]:
    """超出目标字数 `tolerance` 倍时让 LLM 删,一遍不够就再删(最多 `FIT_PASSES` 遍)。

    某一遍压坏了(空、没变短、丢了开场或结论)就丢掉那一遍、接着下一遍,全都坏了就用原稿。"""
    if not segments or budget <= 0:
        return segments
    misses = 0
    for _ in range(FIT_PASSES):
        if spoken_chars(segments) <= budget * tolerance:
            break
        shorter = _shorten_once(segments, budget, llm, keep_count, misses)
        if shorter is None:
            misses += 1
            continue    # 这一遍没压好,下一遍重来(温度 0.3,换次采样常能过)
        segments = shorter
    return segments


def _shorten_once(segments: list[ScriptSegment], budget: int, llm,
                  keep_count: bool, misses: int = 0) -> list[ScriptSegment] | None:
    """让 LLM 压一遍;结果不可用返回 None。"""
    have = spoken_chars(segments)
    draft = json.dumps({"segments": [{"section": s.section, "text": s.text, "fact_refs": s.fact_refs}
                                     for s in segments]}, ensure_ascii=False)
    # 瞄着预算再低一点压:LLM 离目标只差一点时容易原样返回,压到刚好又总差几个字
    aim = int(budget * (0.9 if misses else 0.95))
    system = SHORTEN_SYSTEM.format(chars=aim)
    if keep_count:
        system += (f"\n- 本稿必须保持恰好 {len(segments)} 段、顺序不变:不许删段或合并,"
                   "每段都只缩短(压到只剩最硬的一个数字加一句观点)。")
    if misses:
        system += (f"\n- 上 {misses} 遍你几乎没删(或删坏了):这遍每段都砍掉约三分之一,宁可少说一个参数;"
                   + ("" if keep_count else "同一 section 里相邻的短句可以合并成一句。"))
    try:
        # 明说要删多少字:只给目标字数时 LLM 习惯性只删一点
        data = llm.structured(system,
                              f"目标 {aim} 字,现在 {have} 字,至少删掉 {have - aim} 字。\n{draft}",
                              required_keys=["segments"], temperature=0.3)
    except Exception as exc:  # noqa: BLE001 —— 压缩是锦上添花,失败不拦脚本
        log.warning("脚本压缩失败,用原稿:%s", exc)
        return None
    known = {s.section for s in segments}
    # 压缩稿不带 heading(只删不加的提示词里没有它),按段落名从原稿搬回来
    headings = {s.section: s.heading for s in segments if s.heading}
    out: list[ScriptSegment] = []
    for item in data.get("segments") or []:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text", "")).strip()
        section = str(item.get("section", "")).strip()
        if not text or section not in known:
            continue
        refs = item.get("fact_refs") or []
        refs = [refs] if isinstance(refs, str) else refs
        out.append(ScriptSegment(idx=len(out), section=section, text=text,
                                 fact_refs=[str(r) for r in refs if str(r)],
                                 heading=headings.get(section, "")))
    if keep_count and len(out) != len(segments):
        log.warning("脚本压缩改变了段数(%d → %d),用原稿", len(segments), len(out))
        return None
    if not out or spoken_chars(out) >= have or out[0].section != segments[0].section \
            or out[-1].section != segments[-1].section:
        log.warning("脚本压缩结果不可用(%d 段,%d → %d 字),用原稿", len(out), have, spoken_chars(out))
        return None
    log.info("脚本压缩:%d → %d 字(目标 %d)", have, spoken_chars(out), budget)
    return out
