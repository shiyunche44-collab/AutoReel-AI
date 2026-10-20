"""等效用量估算与配额节流。

横屏解说轨不调视频生成,花钱的只有 TTS、BGM、几次 LLM,以及素材池打标签时的视觉调用。
价格来自 MiniMax 国际站 Pay-as-you-go 公开标价(没有标价的几项是粗估),用途**仅限**:
  1. 项目级"节流阈值"的量化依据;
  2. 套餐到期后的降级预案参考。
它**不是**账单依据。UI 中所有金额一律标注为"等效用量"。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from . import config


# --------------------------------------------------------------------------
# 价格表(美元 / 等效)
# --------------------------------------------------------------------------
#: TTS:美元 / 百万字符
TTS_PER_MILLION_CHARS = 60.0
#: 以下三项没有可查的公开标价,是**粗估**(偏保守),只用来让解说轨的用量不再显示成 0
#: BGM:music 模型 美元 / 首
MUSIC_PRICE = 0.15
#: 一次 LLM 结构化调用(事实抽取 / 脚本 / 版式,长输入长输出)
LLM_CALL_PRICE = 0.01
#: 一次视觉模型描述(素材池打标签,每张图一次)
VLM_CALL_PRICE = 0.003
#: 解说轨每条片子固定的 LLM 调用:事实抽取、脚本、逐拍版式
EXPLAINER_LLM_CALLS = 3


class BudgetExceeded(RuntimeError):
    """预估用量超过项目级节流阈值。"""

    def __init__(self, projected: float, limit: float) -> None:
        super().__init__(
            f"预计等效用量 {projected:.2f} 超过项目节流上限 {limit:.2f},已暂停。"
        )
        self.projected = projected
        self.limit = limit


@dataclass
class CostLedger:
    """项目的累计用量账本。

    `spent` 只累加**已提交**的任务(提交即产生消耗);
    `reserved` 是在途任务的预估值,用于并发前的节流预检。
    """

    limit: float
    spent: float = 0.0
    reserved: float = 0.0
    items: list[dict] = field(default_factory=list)

    @property
    def committed(self) -> float:
        return self.spent + self.reserved

    def remaining(self) -> float:
        return max(0.0, self.limit - self.committed)

    def check(self, projected: float) -> None:
        if self.committed + projected > self.limit:
            raise BudgetExceeded(self.committed + projected, self.limit)

    def commit(self, amount: float, note: str = "") -> None:
        self.reserved = max(0.0, self.reserved - amount)
        self.spent += amount
        self.items.append({"amount": amount, "note": note})

    def reserve(self, amount: float) -> None:
        self.reserved += amount


# --------------------------------------------------------------------------
# 单项估算
# --------------------------------------------------------------------------
def estimate_tts(total_chars: int) -> float:
    return round(total_chars * TTS_PER_MILLION_CHARS / 1_000_000, 4)


def estimate_explainer(duration: int, *, cached_ratio: float = 0.0,
                       bgm_cached: bool = False) -> dict:
    """横屏解说轨一条片子的等效用量:TTS + BGM + 固定几次 LLM。不调视频生成。

    `cached_ratio`:旁白里命中句子缓存的比例;`bgm_cached`:BGM 来自共享曲库。
    素材池(打标签)是每台车一次性的,不摊进单条片子,另由 `estimate_library` 给。
    """
    ratio = min(1.0, max(0.0, float(cached_ratio)))
    tts = estimate_tts(int(duration * 5.8 * (1 - ratio)))
    bgm = 0.0 if bgm_cached else MUSIC_PRICE
    llm = round(EXPLAINER_LLM_CALLS * LLM_CALL_PRICE, 4)
    raw = round(tts + bgm + llm, 4)
    # 解说轨没有生成重试;留 1.2 倍给门禁 A 改稿重确认(改动句重合成)
    return {"duration": duration, "tts": tts, "bgm": bgm, "llm": llm, "raw": raw,
            "with_retry": round(raw * 1.2, 4)}


def explainer_spent(narration: dict) -> float:
    """按一次 `narration.build` 的结果算这次实际花了多少(只算真正送去合成的)。"""
    tts = estimate_tts(int(narration.get("synth_chars") or 0))
    bgm = 0.0 if narration.get("bgm_reused") or not narration.get("bgm_path") else MUSIC_PRICE
    return round(tts + bgm, 4)


def estimate_library(vlm_calls: int) -> float:
    """素材池打标签的等效用量:每次视觉调用 + 每 12 张一次结构化 LLM。"""
    calls = max(0, int(vlm_calls))
    return round(calls * VLM_CALL_PRICE + math.ceil(calls / 12) * LLM_CALL_PRICE, 4)


def default_budget_for(duration: int) -> float:
    """按时长给出一个宽松的默认节流阈值。"""
    est = estimate_explainer(duration)
    return round(max(config.DEFAULT_QUOTA_BUDGET, est["with_retry"] * 1.5), 2)
