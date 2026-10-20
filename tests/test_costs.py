"""用量估算与配额节流测试(计划 §6.1)。

注意:金额是**等效用量**,用于相对比较与节流,不是真实账单
(用户的 M Plan 套餐已包含 H3,见计划 §2.4)。
"""
from __future__ import annotations

import pytest

from app import config, costs
from app.costs import (
    BudgetExceeded,
    CostLedger,
    estimate_project,
    estimate_reference_images,
    estimate_shot,
    estimate_video,
    wall_clock_estimate,
)


# --------------------------------------------------------------------------
# 单项估算对齐参考价目表
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "model,resolution,seconds,expected",
    [
        ("MiniMax-H3", "768P", 6, 0.48),
        ("MiniMax-H3", "2K", 6, 0.78),
        ("MiniMax-H3", "768P", 15, 1.20),
        ("MiniMax-H3-Max", "480P", 5, 0.25),
        ("MiniMax-Hailuo-2.3", "768P", 6, 0.28),
        ("MiniMax-Hailuo-2.3", "1080P", 6, 0.49),
        ("MiniMax-Hailuo-2.3", "768P", 10, 0.56),
    ],
)
def test_estimate_video_matches_reference_prices(model, resolution, seconds, expected):
    assert estimate_video(model, resolution, seconds) == pytest.approx(expected, abs=0.001)


def test_reference_images_free_up_to_five():
    assert estimate_reference_images(0) == 0.0
    assert estimate_reference_images(5) == 0.0
    assert estimate_reference_images(6) == pytest.approx(0.04)
    assert estimate_reference_images(9) == pytest.approx(0.16)


def test_estimate_shot_adds_reference_cost_to_final_only():
    draft = estimate_shot(stage="draft", duration=6, ref_image_count=5)
    final = estimate_shot(stage="final", duration=6, ref_image_count=5)
    assert draft == pytest.approx(0.28)
    assert final == pytest.approx(0.48)


# --------------------------------------------------------------------------
# 整条视频估算
# --------------------------------------------------------------------------
@pytest.mark.parametrize("duration,shot_count", [(60, 10), (90, 15), (120, 20), (180, 30)])
def test_estimate_project_shot_counts(duration, shot_count):
    est = estimate_project(duration)
    assert est["shot_count"] == shot_count
    assert est["duration"] == duration


def test_estimate_project_scales_linearly():
    """用量随镜头数线性增长 —— 这是配额控制的基础假设。"""
    a = estimate_project(60)["raw"]
    b = estimate_project(120)["raw"]
    assert b == pytest.approx(a * 2, rel=0.02)


def test_estimate_project_retry_factor_applied():
    est = estimate_project(60)
    assert est["with_retry"] == pytest.approx(est["raw"] * 1.5, rel=0.001)


def test_tts_cost_is_negligible():
    est = estimate_project(180)
    assert est["tts"] < 0.1
    assert est["tts"] < est["raw"] * 0.01


# --------------------------------------------------------------------------
# 配额节流
# --------------------------------------------------------------------------
def test_ledger_blocks_when_limit_would_be_exceeded():
    ledger = CostLedger(limit=10.0)
    ledger.commit(9.0)
    with pytest.raises(BudgetExceeded):
        ledger.check(2.0)
    ledger.check(0.5)  # 未超限应通过
    assert ledger.remaining() == pytest.approx(1.0)


def test_ledger_reserve_then_commit():
    ledger = CostLedger(limit=100.0)
    ledger.reserve(5.0)
    assert ledger.committed == 5.0
    ledger.commit(5.0, note="shot 1")
    assert ledger.spent == 5.0
    assert ledger.reserved == 0.0


def test_ledger_never_goes_negative():
    ledger = CostLedger(limit=10.0)
    ledger.commit(2.0)
    ledger.reserve(1.0)
    ledger.commit(1.0)
    assert ledger.reserved >= 0
    assert ledger.remaining() >= 0


def test_default_budget_exceeds_expected_usage():
    for duration in config.SUPPORTED_DURATIONS:
        budget = costs.default_budget_for(duration)
        usage = estimate_project(duration)["with_retry"]
        assert budget >= usage


# --------------------------------------------------------------------------
# 墙钟预期
# --------------------------------------------------------------------------
def test_wall_clock_respects_configured_concurrency():
    wc = wall_clock_estimate(120)
    assert wc["shot_count"] == 20
    assert wc["final_minutes_high"] > wc["final_minutes_low"] > 0
    # 并发上限为链数与配置值的较小者
    assert wc["concurrency"] >= 1


def test_longer_videos_take_longer():
    assert (wall_clock_estimate(180)["final_minutes_high"]
            > wall_clock_estimate(60)["final_minutes_high"])


# --------------------------------------------------------------------------
# 链内串行:旧公式假设每个镜头都能满并发,长视频下明显低估
# --------------------------------------------------------------------------
def test_wall_clock_accounts_for_chain_serialization():
    """墙钟必须按**波次**算,而不是 镜头数/并发。

    链内串行意味着一条链的 3 个镜头要依次跑完才释放下一条链,
    所以并行度上限是链数,不是镜头数。
    """
    wc = wall_clock_estimate(240)
    chains = -(-40 // config.CHAIN_GROUP_SIZE)
    assert wc["chains"] == chains == 14
    conc = min(config.GEN_MAX_CONCURRENCY, chains)
    waves = -(-chains // conc)
    assert wc["waves"] == waves == 2
    assert wc["is_upper_bound"] is True
    assert wc["final_minutes_low"] == waves * config.CHAIN_GROUP_SIZE * 15.0


def test_240s_and_180s_have_same_wall_clock():
    """选 240 秒作上限的核心理由:与 180 秒同为 2 个波次,不额外增加等待。"""
    a = wall_clock_estimate(180)
    b = wall_clock_estimate(240)
    assert a["waves"] == b["waves"]
    assert a["final_minutes_high"] == b["final_minutes_high"]
    assert b["shot_count"] > a["shot_count"]


def test_wall_clock_waves_match_manual_count():
    for duration in config.SUPPORTED_DURATIONS:
        n = duration // config.SHOT_GRID_SECONDS
        chains = -(-n // config.CHAIN_GROUP_SIZE)
        conc = max(1, min(config.GEN_MAX_CONCURRENCY, chains))
        wc = wall_clock_estimate(duration)
        assert wc["chains"] == chains
        assert wc["waves"] == -(-chains // conc)


def test_supported_durations_include_240():
    assert 240 in config.SUPPORTED_DURATIONS
    assert max(config.SUPPORTED_DURATIONS) == 240


# --------------------------------------------------------------------------
# 横屏解说轨
# --------------------------------------------------------------------------
def test_explainer_estimate_is_not_zero_and_cache_reduces_it():
    full = costs.estimate_explainer(240)
    assert full["with_retry"] > 0
    assert full["tts"] == costs.estimate_tts(int(240 * 5.8))
    assert full["bgm"] == costs.MUSIC_PRICE
    cached = costs.estimate_explainer(240, cached_ratio=1.0, bgm_cached=True)
    assert cached["tts"] == 0 and cached["bgm"] == 0
    assert cached["with_retry"] < full["with_retry"]


def test_explainer_spent_counts_only_new_work():
    assert costs.explainer_spent({"synth_chars": 0, "bgm_path": "x", "bgm_reused": True}) == 0
    fresh = costs.explainer_spent({"synth_chars": 1000, "bgm_path": "x", "bgm_reused": False})
    assert fresh == round(costs.estimate_tts(1000) + costs.MUSIC_PRICE, 4)
    assert costs.explainer_spent({"synth_chars": 1000, "bgm_path": None}) == \
        costs.estimate_tts(1000)


def test_library_estimate_scales_with_vlm_calls():
    assert costs.estimate_library(0) == 0
    assert costs.estimate_library(24) == round(24 * costs.VLM_CALL_PRICE
                                               + 2 * costs.LLM_CALL_PRICE, 4)
