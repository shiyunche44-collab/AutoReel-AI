"""剪辑 v2 的"语义配画面":每一刀按它念到的半句挑素材,并记下这一刀对没对上题。

两层(都不需要新的外部服务):

1. **视角意图**(确定性):半句里的说法 → 想要的视角(`vocab.view_words`),
   交给 `library.fill(views=...)`,命中视角的素材算对题、排在前面。
2. **文字模型复选**(可选,`EDIT_V2_RERANK`,默认开):`fill` 排序前几名的素材描述
   (入库时视觉模型写的 `desc`)和这半句一起交给文字模型,挑最能让观众"看到这半句在讲的东西"
   的一条。思路同 NarratoAI:先让视觉模型看懂素材,再让语言模型按解说挑片段。

对题判定(`judge`)写进时间线,汇总成"对题率",作为这一阶段的回归指标。
"""
from __future__ import annotations

import logging
import os
import re
from collections.abc import Callable, Sequence
from typing import Any

from ...categories import Vocab

log = logging.getLogger(__name__)

#: 半句太短(几个字的语气词)不值得让模型挑
MIN_TEXT_CHARS = 4
_WORD = re.compile(r"[0-9A-Za-z一-鿿]")

CHOOSE_SYSTEM = """你在给汽车解说短视频配画面。给你一句正在念的旁白,和几段候选素材(每段有视角和画面描述)。
挑一段最能让观众"看到这句话在讲的东西"的素材:
- 旁白讲到具体部件 / 位置(尾灯、中控屏、后排),优先画面里正是它的;
- 旁白讲动态表现(加速、过弯),优先行驶画面;
- 旁白是观点、价格、结论这类看不见的东西,选画面最干净、车最完整的;
- 绝不能选和旁白讲的部件相矛盾的特写(讲刹车却是门把手)。
只输出 JSON:{"pick": 候选编号(从 1 开始), "why": "不超过 15 字"}"""


def rerank_enabled() -> bool:
    return os.getenv("EDIT_V2_RERANK", "1").strip().lower() not in ("0", "false", "off")


def views_for(text: str, vocab: Vocab) -> set[str]:
    return vocab.views_in(text) if text else set()


def _speakable(text: str) -> int:
    return len(_WORD.findall(text or ""))


def candidates_text(items: Sequence[Any]) -> str:
    lines = []
    for n, i in enumerate(items, 1):
        desc = (getattr(i, "desc", "") or "").strip().replace("\n", " ")[:60] or "(无描述)"
        lines.append(f"{n}. [{getattr(i, 'view', '') or '?'}] {desc}")
    return "\n".join(lines)


def llm_chooser(llm: Any, subject: str) -> Callable[[str, list[Any]], int | None]:
    """`library.fill(choose=...)` 用的挑选器。模型不可用 / 出错 / 编号不合法都返回 None(按排序取)。"""
    cache: dict[tuple[str, tuple[str, ...]], int | None] = {}

    def choose(text: str, items: list[Any]) -> int | None:
        if _speakable(text) < MIN_TEXT_CHARS or len(items) < 2:
            return None
        key = (text, tuple(getattr(i, "id", "") for i in items))
        if key in cache:
            return cache[key]
        user = f"车型:{subject}\n旁白:{text}\n候选素材:\n{candidates_text(items)}"
        try:
            data = llm.structured(CHOOSE_SYSTEM, user, required_keys=["pick"], temperature=0.2,
                                  max_retries=1)
            k = int(data.get("pick")) - 1
        except Exception:  # noqa: BLE001 —— 挑不出来就按排序取
            log.info("取材复选失败,按排序取", exc_info=True)
            k = -1
        result = k if 0 <= k < len(items) else None
        cache[key] = result
        return result

    return choose


def judge(text: str, item: Any, vocab: Vocab) -> tuple[bool, str]:
    """这一刀对没对上题。返回 (这半句有没有具体可看的东西, 对上的依据)。

    - 半句点到了视角 / 部件 → "需要对题";素材视角命中 → "view",素材描述里认出同一部件 → "detail";
    - 半句没点到任何可看的东西(观点、价格) → 不计入对题率。
    """
    if item is None:
        return bool(views_for(text, vocab) or vocab.detail_types_in(text)), ""
    want_views = views_for(text, vocab)
    want_detail = vocab.detail_types_in(text) if text else set()
    needs = bool(want_views or want_detail)
    if want_views and getattr(item, "view", "") in want_views:
        return needs, "view"
    if want_detail and vocab.detail_types_in(getattr(item, "desc", "") or "") & want_detail:
        return needs, "detail"
    return needs, ""


def match_rate(clips: Sequence[Any]) -> tuple[int, int]:
    """(对上题的刀数, 需要对题的刀数)。clip 带 `need` / `match` 字段。"""
    need = [c for c in clips if getattr(c, "need", False)]
    return sum(1 for c in need if getattr(c, "match", "")), len(need)
