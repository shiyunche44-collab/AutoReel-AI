"""等效用量估算与配额节流。

⚠️ 重要:用户持有 M Plan Explore 套餐,档位规格已包含"视频模型,如 H3",
因此 H3 生成走**套餐额度,不产生按量付费账单**。

本模块的价格表来自 MiniMax 国际站 Pay-as-you-go 公开标价,其用途**仅限**:
  1. 不同模型/分辨率/时长之间的**相对消耗速度比较**;
  2. 项目级"节流阈值"的量化依据(防止单条视频无限重试);
  3. 套餐到期后的降级预案参考。
它**不是**账单依据。UI 中所有金额一律标注为"等效用量"。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from . import config


# --------------------------------------------------------------------------
# 价格表(美元 / 等效)
# --------------------------------------------------------------------------
#: 视频输出单价:美元 / 输出秒
PER_SECOND_PRICES: dict[str, float] = {
    "MiniMax-H3:768P": 0.08,
    "MiniMax-H3:2K": 0.13,
    "MiniMax-H3-Max:480P": 0.05,
    "MiniMax-H3-Max:768P": 0.08,
}

#: 按条计费的旧模型:美元 / 条
PER_CLIP_PRICES: dict[tuple[str, str, int], float] = {
    ("MiniMax-Hailuo-2.3-Fast", "768P", 6): 0.19,
    ("MiniMax-Hailuo-2.3-Fast", "768P", 10): 0.32,
    ("MiniMax-Hailuo-2.3-Fast", "1080P", 6): 0.33,
    ("MiniMax-Hailuo-2.3", "768P", 6): 0.28,
    ("MiniMax-Hailuo-2.3", "768P", 10): 0.56,
    ("MiniMax-Hailuo-2.3", "1080P", 6): 0.49,
}

#: H3 参考图:前 5 张免费,其后每张 0.04
FREE_REFERENCE_IMAGES = 5
REFERENCE_IMAGE_PRICE = 0.04

#: TTS:美元 / 百万字符
TTS_PER_MILLION_CHARS = 60.0
#: 图像:image-01 美元 / 张
IMAGE_PRICE = 0.0035
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
def estimate_video(model: str, resolution: str, duration: float) -> float:
    """估算单条视频生成的等效用量。"""
    key = f"{model}:{resolution}"
    if key in PER_SECOND_PRICES:
        return round(PER_SECOND_PRICES[key] * duration, 4)
    clip = PER_CLIP_PRICES.get((model, resolution, int(duration)))
    if clip is not None:
        return round(clip, 4)
    # 未知组合:按同模型最便宜档位保守估算,并标记调用方需人工确认。
    cheapest = min(
        (v for k, v in PER_SECOND_PRICES.items() if k.startswith(model)), default=None
    )
    return round((cheapest or 0.05) * duration, 4)


def estimate_reference_images(count: int) -> float:
    """超出免费额度的参考图才计费。"""
    if count <= FREE_REFERENCE_IMAGES:
        return 0.0
    return round((count - FREE_REFERENCE_IMAGES) * REFERENCE_IMAGE_PRICE, 4)


def estimate_tts(total_chars: int) -> float:
    return round(total_chars * TTS_PER_MILLION_CHARS / 1_000_000, 4)


def estimate_images(count: int) -> float:
    return round(count * IMAGE_PRICE, 4)


def estimate_shot(
    *,
    stage: str,
    duration: float,
    ref_image_count: int = 0,
    origin: str = "generate",
) -> float:
    """估算单个镜头在其所处档位下的等效用量。

    `origin="clip"` 表示该镜头用网上抓来的真实视频片段,没有生成成本 ——
    这正是混合方案省钱的地方,必须让它在账本上真的体现成 0,
    否则"省了多少"永远是个说不清的数。
    """
    if origin == "clip":
        return 0.0
    if stage == "draft":
        cost = estimate_video(config.DRAFT_MODEL, "768P", duration)
    else:
        cost = estimate_video(config.FINAL_MODEL, config.FINAL_RESOLUTION, duration)
        cost += estimate_reference_images(ref_image_count)
    return cost


def savings_from_clips(clip_count: int, *, duration: int | None = None) -> float:
    """N 个镜头改用真实素材能省下的等效用量。

    草稿档那一次生成省不掉:草稿档要的是"快",真实片段没经过任何模型,
    但审片时人看的正是草稿,所以省的是成片档那一次。
    """
    if clip_count <= 0:
        return 0.0
    secs = duration or config.SHOT_GRID_SECONDS
    return round(clip_count * estimate_video(config.FINAL_MODEL,
                                             config.FINAL_RESOLUTION, secs), 4)


def estimate_project(duration: int, *, retry_factor: float = 1.5,
                     real_clip_ratio: float = 0.0) -> dict:
    """按计划 §2.4 的表格估算整条视频的等效用量。

    `real_clip_ratio` 是"有多少比例的成片镜头改用真实素材",取值 0~1。
    放进估算而不是留到事后统计,是因为**省下的钱要看得见** —— 混合方案
    对用户唯一的直接价值就是这个数,藏在运行日志里等于不存在。
    """
    n = config.shots_for_duration(duration)
    ratio = min(1.0, max(0.0, float(real_clip_ratio)))
    final_shots = n * (1 - ratio)

    draft = sum(estimate_video(config.DRAFT_MODEL, "768P", config.SHOT_GRID_SECONDS) for _ in range(n))
    final = sum(
        estimate_video(config.FINAL_MODEL, config.FINAL_RESOLUTION, config.SHOT_GRID_SECONDS)
        for _ in range(n)
    )
    # 真实片段省掉的是成片档那一次生成(参考图与 TTS 不省)
    final_after_clips = final * (1 - ratio)
    ref_total = round(estimate_reference_images(config.CATEGORY_REF_LIMIT) * n, 4)
    tts = estimate_tts(int(duration * 5.8))  # 中文口播约 5.8 字/秒
    raw = round(draft + final_after_clips + ref_total + tts, 4)
    savings = round(final - final_after_clips, 4)
    return {
        "duration": duration,
        "shot_count": n,
        "draft": round(draft, 4),
        "final": round(final_after_clips, 4),
        "final_without_clips": round(final, 4),
        "real_clip_ratio": round(ratio, 4),
        "real_clip_shots": int(round(n * ratio)),
        "savings": savings,
        "references": ref_total,
        "tts": tts,
        "raw": raw,
        "with_retry": round(raw * retry_factor, 4),
    }


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
    est = estimate_project(duration)
    return round(max(config.DEFAULT_QUOTA_BUDGET, est["with_retry"] * 1.5), 2)


def wall_clock_estimate(duration: int) -> dict:
    """按一致性链编排给出墙钟预期(分钟)。

    ⚠️ 这里必须算**链内串行**,不能简单用 `镜头数 / 并发`。
    `chains.ready_shots()` 每条链同时只放行一个镜头,因此并行度上限是
    **链数**而不是镜头数:一条链的 3 个镜头必须依次跑完,才能释放下一条链。
    于是总时长 = 波次 × 链长 × 单镜头耗时。

    旧公式(镜头数 / 并发 × 单镜头耗时)假设每个镜头都能满并发,只在
    "链数 ≤ 并发"时才碰巧正确;一旦镜头数超过并发就明显低估。180 秒
    (30 镜头 / 10 链 / 并发 8)已经要跑 2 波次,240 秒(14 链)仍是 2 波次 ——
    这正是把时长上限定在 240 秒的原因。

    末波往往是不满链,故结果是**上界**。宁可高估,也不要让人低估等待时间
    后误判为卡死。
    """
    n = config.shots_for_duration(duration)
    chain_len = max(1, config.CHAIN_GROUP_SIZE)
    chains = max(1, math.ceil(n / chain_len))
    concurrency = max(1, min(config.GEN_MAX_CONCURRENCY, chains))
    waves = max(1, math.ceil(chains / concurrency))
    # 每个波次内,每条链要串行跑满 chain_len 个镜头
    per_wave = chain_len
    # 草稿档:约 1 分钟/镜头
    draft = round(waves * per_wave * 1.0, 1)
    # 成片档:15-30 分钟/镜头
    final_low = round(waves * per_wave * 15.0, 1)
    final_high = round(waves * per_wave * 30.0, 1)
    return {
        "shot_count": n,
        "concurrency": concurrency,
        "chains": chains,
        "waves": waves,
        "chain_length": chain_len,
        "is_upper_bound": True,
        "draft_minutes": draft,
        "final_minutes_low": final_low,
        "final_minutes_high": final_high,
    }
