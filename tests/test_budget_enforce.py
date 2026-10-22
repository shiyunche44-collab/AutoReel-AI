"""配额要真的拦得住。

之前 `costs.CostLedger` / `BudgetExceeded` 只有单元测试在用,流水线
从不构造它:`quota_budget` 写进库、在界面上显示、`spent_cost` 会被累加,
但**没有任何代码比较过两者**。`State.PAUSED_BUDGET` 定义在转移表里,
却**没有任何代码路径能进入它**。

也就是说 README 宣称的"项目级节流阈值"从来没生效过:一次失控的
TTS/LLM 循环可以把整个套餐烧穿,毫无拦截。

现在:调用**之前**先预留额度,超限就中止调用并转入 PAUSED_BUDGET。
"""
from __future__ import annotations

import pytest

from app import config, costs, db
from app.pipeline import orchestrator, script_gen


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "data" / "projects")
    db._INITIALISED = False
    db.init_db()
    yield db
    db._INITIALISED = False


def _project(pid: str = "p_budget", state: str = "FACTS_RESOLVED",
             budget: float = 40.0, spent: float = 0.0) -> dict:
    return {
        "id": pid, "title": "配额测试", "subject": "测试车", "category": "auto_long",
        "theme": "buyguide", "angle": "", "subjects": [], "duration": 240,
        "shot_count": 10, "narration_mode": "tts", "state": state,
        "state_reason": None, "quota_budget": budget, "spent_cost": spent,
        "est_cost": 1.0, "created_at": 1.0, "updated_at": 1.0,
    }


# --------------------------------------------------------------------------
# 预留 → 结算
# --------------------------------------------------------------------------
def test_reserve_blocks_when_over_quota(store):
    store.create_project(_project(budget=0.01))
    assert db.reserve_spend("p_budget", 0.02) is False
    assert db.reserve_spend("p_budget", 0.001) is True


def test_reserve_accounts_for_in_flight_calls(store):
    """在途的预留也要算进额度 —— 两个并发的调用不能各自都觉得"我还够"。"""
    store.create_project(_project(budget=0.03))
    assert db.reserve_spend("p_budget", 0.02) is True
    assert db.reserve_spend("p_budget", 0.02) is False, "第二个预留应被拒"
    db.release_spend("p_budget", 0.02)
    assert db.reserve_spend("p_budget", 0.02) is True, "释放后应能再预留"


def test_commit_turns_reservation_into_spend(store):
    store.create_project(_project(budget=1.0))
    db.reserve_spend("p_budget", 0.5)
    db.commit_spend("p_budget", 0.5, 0.3)
    row = store.get_project("p_budget")
    assert row["spent_cost"] == pytest.approx(0.3)
    assert row["reserved_cost"] == pytest.approx(0.0)


def test_release_does_not_spend(store):
    store.create_project(_project(budget=1.0))
    db.reserve_spend("p_budget", 0.5)
    db.release_spend("p_budget", 0.5)
    row = store.get_project("p_budget")
    assert row["spent_cost"] == pytest.approx(0.0)
    assert row["reserved_cost"] == pytest.approx(0.0)


def test_project_spend_releases_on_error(store):
    """调用失败 → 释放预留,不记账。"""
    store.create_project(_project(budget=1.0))
    with pytest.raises(RuntimeError):
        with costs.project_spend("p_budget", 0.5):
            raise RuntimeError("调用炸了")
    row = store.get_project("p_budget")
    assert row["spent_cost"] == pytest.approx(0.0)
    assert row["reserved_cost"] == pytest.approx(0.0)


def test_project_spend_raises_before_the_call(store):
    """超限 → 抛 BudgetExceeded,调用**不会发生**。"""
    store.create_project(_project(budget=0.001))
    with pytest.raises(costs.BudgetExceeded):
        with costs.project_spend("p_budget", 10.0):
            raise AssertionError("不该走到这里")


# --------------------------------------------------------------------------
# 流水线层面:超限要停在 PAUSED_BUDGET
# --------------------------------------------------------------------------
def test_advance_pauses_on_budget_and_does_not_spend(store, monkeypatch):
    """额度不够时:不进 LLM 调用,项目停在 PAUSED_BUDGET。"""
    store.create_project(_project(state="FACTS_RESOLVED", budget=0.0001))
    called: list[str] = []

    monkeypatch.setattr(orchestrator, "_llm", lambda: object())
    monkeypatch.setattr(script_gen, "generate",
                        lambda *a, **k: called.append("generate"))

    out = orchestrator.advance("p_budget")
    assert out["state"] == "PAUSED_BUDGET"
    assert not called, "额度不够时不该真的去调 LLM"
    assert store.get_project("p_budget")["state"] == "PAUSED_BUDGET"


def test_paused_budget_is_actually_reachable(store, monkeypatch):
    """PAUSED_BUDGET 之前是**不可达**的 —— 转移表里有,代码里没路径能进。"""
    from app.state import State
    store.create_project(_project(state="FACTS_RESOLVED", budget=0.0001))
    # 替身 LLM:不然没配 DEEPSEEK_API_KEY 的机器(CI)会先停在"缺 Key",走不到预算检查
    monkeypatch.setattr(orchestrator, "_llm", lambda: object())
    monkeypatch.setattr(script_gen, "generate", lambda *a, **k: [])
    orchestrator.advance("p_budget")
    assert store.get_project("p_budget")["state"] == str(State.PAUSED_BUDGET)


def test_pause_records_where_it_came_from(store, monkeypatch):
    store.create_project(_project(state="FACTS_RESOLVED", budget=0.0001))
    monkeypatch.setattr(orchestrator, "_llm", lambda: object())
    monkeypatch.setattr(script_gen, "generate", lambda *a, **k: [])
    orchestrator.advance("p_budget")
    assert store.get_project("p_budget")["resume_state"] == "FACTS_RESOLVED"
