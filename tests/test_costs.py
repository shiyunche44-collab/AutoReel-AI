"""用量估算与配额节流测试。

注意:金额是**等效用量**,用于节流与套餐到期后的参考,不是真实账单。
"""
from __future__ import annotations

import pytest

from app import config, costs
from app.costs import BudgetExceeded, CostLedger


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
        assert budget >= costs.estimate_explainer(duration)["with_retry"]


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
