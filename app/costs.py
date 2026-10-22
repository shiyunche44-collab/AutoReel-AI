"""等效用量估算与配额节流。

横屏解说轨不调视频生成,花钱的只有 TTS、BGM、几次 LLM,以及素材池打标签时的视觉调用。
价格来自 MiniMax 国际站 Pay-as-you-go 公开标价(没有标价的几项是粗估),用途**仅限**:
  1. 项目级"节流阈值"的量化依据;
  2. 套餐到期后的降级预案参考。
它**不是**账单依据。UI 中所有金额一律标注为"等效用量"。
"""
from __future__ import annotations

import math
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator

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


# --------------------------------------------------------------------------
# 项目级配额账本
# --------------------------------------------------------------------------
#: 各阶段在**调用之前**要预留的额度(等效美元)。
#:
#: 预留值宁可取高:估高了顶多提前触发暂停,估低了就拦不住。
STAGE_RESERVE: dict[str, float] = {
    # 事实抽取:结构化 LLM 一次,外加可能的二次整理
    "facts": round(LLM_CALL_PRICE * 2, 4),
    # 脚本生成:一次长输入长输出的结构化调用
    "script": LLM_CALL_PRICE,
    # 逐拍版式:一次
    "beats": LLM_CALL_PRICE,
}

#: 素材池打标签的预留视觉调用数(每台车一次性)
LIBRARY_RESERVE_VLM_CALLS = 60


def stage_reserve(stage: str, *, chars: int = 0) -> float:
    """某个阶段开始前要预留的额度。"""
    if stage == "tts":
        return round(estimate_tts(int(chars)) + MUSIC_PRICE, 4)
    if stage == "library":
        return round(estimate_library(LIBRARY_RESERVE_VLM_CALLS), 4)
    return round(STAGE_RESERVE.get(stage, 0.0), 4)


def reserve_project(project_id: str, amount: float) -> float:
    """为项目预留额度。超限抛 `BudgetExceeded`。返回预留的额度。"""
    from . import db  # 延迟导入:db 不依赖 costs,避免循环

    amount = max(0.0, float(amount))
    if amount <= 0:
        return 0.0
    row = db.get_project(project_id) or {}
    if not db.reserve_spend(project_id, amount):
        raise BudgetExceeded(
            float(row.get("spent_cost") or 0.0) + amount,
            float(row.get("quota_budget") or 0.0),
        )
    return amount


def commit_project(project_id: str, reserved: float, actual: float) -> None:
    """结算:释放预留,加上实际花费。"""
    from . import db

    db.commit_spend(project_id, reserved, actual)


def release_project(project_id: str, reserved: float) -> None:
    """调用失败:释放预留,不记账。"""
    from . import db

    db.release_spend(project_id, reserved)


class _Reservation:
    """在途调用的预留额度。"""

    def __init__(self, project_id: str, reserved: float) -> None:
        self.project_id = project_id
        self.reserved = reserved
        self._settled = False

    def actual(self, amount: float) -> None:
        """修正实际花费(比预留多或少都可能)。"""
        self._settled = True
        commit_project(self.project_id, self.reserved, amount)


@contextmanager
def project_spend(project_id: str, estimated: float) -> Iterator[_Reservation]:
    """预留 → 调用 → 结算。

    用法::

        with costs.project_spend(pid, costs.stage_reserve("tts", chars=n)) as spend:
            result = synthesize()
            spend.actual(costs.explainer_spent(result))

    调用抛异常 → 释放预留、不记账;正常返回 → 按预留值结算
    (没调 `actual()` 的话)。超限 → 抛 `BudgetExceeded`,**调用不会发生**。
    """
    reserved = reserve_project(project_id, estimated)
    spend = _Reservation(project_id, reserved)
    try:
        yield spend
    except BaseException:
        release_project(project_id, reserved)
        raise
    else:
        if not spend._settled:
            commit_project(project_id, reserved, reserved)
