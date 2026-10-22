"""状态机测试(计划 §6.1)。

门禁是防止在错误分镜上消耗配额的唯一机制,因此"未确认不得推进"必须被锁死。
"""
from __future__ import annotations

import pytest

from app.state import (
    GATE_STATES,
    IllegalTransition,
    State,
    TERMINAL_STATES,
    assert_transition,
    can_transition,
    is_gate,
    is_terminal,
    next_auto_state,
)

PIPELINE = [
    State.DRAFT_INTENT, State.FACTS_RESOLVED, State.SCRIPT_READY,
    State.STORYBOARD_READY, State.GATE_A, State.ASSETS_READY,
    State.PROMPTS_READY, State.DRAFT_RENDERING, State.GATE_B,
    State.FINAL_RENDERING, State.QA_PASSED, State.COMPOSING, State.DONE,
]


# --------------------------------------------------------------------------
# 字符串序列化(曾出过 State.DRAFT_INTENT 污染数据库的 bug)
# --------------------------------------------------------------------------
def test_state_str_returns_bare_value():
    assert str(State.DRAFT_INTENT) == "DRAFT_INTENT"
    assert str(State.GATE_A) == "GATE_A"
    assert f"{State.FINAL_RENDERING}" == "FINAL_RENDERING"


def test_state_is_json_serialisable_as_string():
    import json

    payload = json.dumps({"state": str(State.DRAFT_RENDERING)})
    assert json.loads(payload)["state"] == "DRAFT_RENDERING"


# --------------------------------------------------------------------------
# 正向流程
# --------------------------------------------------------------------------
def test_full_pipeline_path_is_legal():
    for src, dst in zip(PIPELINE, PIPELINE[1:]):
        assert can_transition(src, dst), f"{src} -> {dst} 应被允许"


def test_next_auto_state_walks_the_pipeline():
    expected = {
        State.DRAFT_INTENT: State.FACTS_RESOLVED,
        State.FACTS_RESOLVED: State.SCRIPT_READY,
        State.SCRIPT_READY: State.STORYBOARD_READY,
        State.ASSETS_READY: State.PROMPTS_READY,
        State.PROMPTS_READY: State.DRAFT_RENDERING,
        State.FINAL_RENDERING: State.QA_PASSED,
        State.QA_PASSED: State.COMPOSING,
        State.COMPOSING: State.DONE,
    }
    for src, dst in expected.items():
        assert next_auto_state(src) == dst


# --------------------------------------------------------------------------
# 门禁:核心不变量
# --------------------------------------------------------------------------
def test_gate_states_block_auto_advance():
    assert GATE_STATES == {State.GATE_A, State.GATE_B}
    for gate in GATE_STATES:
        assert next_auto_state(gate) is None
        assert is_gate(gate)


def test_cannot_skip_gate_a_into_generation():
    """未过门禁 A 就不能进入任何生成阶段。"""
    forbidden = (State.ASSETS_READY, State.PROMPTS_READY,
                 State.DRAFT_RENDERING, State.FINAL_RENDERING)
    for target in forbidden:
        assert not can_transition(State.STORYBOARD_READY, target)
    assert can_transition(State.STORYBOARD_READY, State.GATE_A)


def test_cannot_skip_gate_b_into_final():
    assert not can_transition(State.DRAFT_RENDERING, State.FINAL_RENDERING)
    assert can_transition(State.DRAFT_RENDERING, State.GATE_B)
    assert can_transition(State.GATE_B, State.FINAL_RENDERING)


def test_gate_a_can_send_user_back_for_edits():
    assert can_transition(State.GATE_A, State.SCRIPT_READY)
    assert can_transition(State.GATE_A, State.STORYBOARD_READY)
    assert can_transition(State.GATE_B, State.DRAFT_RENDERING)


# --------------------------------------------------------------------------
# 非法转移必须被拒绝
# --------------------------------------------------------------------------
def test_illegal_transitions_rejected():
    illegal = [
        (State.DRAFT_INTENT, State.SCRIPT_READY),   # 跳过事实采集
        (State.SCRIPT_READY, State.DRAFT_RENDERING),
        (State.GATE_A, State.DRAFT_RENDERING),      # 跳过后面的素材与编译
        (State.COMPOSING, State.FINAL_RENDERING),   # 往回走但不合法
        (State.DONE, State.DRAFT_INTENT),           # 终态不可复活
        (State.CANCELLED, State.DRAFT_INTENT),
    ]
    for src, dst in illegal:
        assert not can_transition(src, dst), f"{src} -> {dst} 应被拒绝"
        with pytest.raises(IllegalTransition):
            assert_transition(src, dst)


def test_unknown_state_names_are_rejected():
    assert not can_transition("NOT_A_STATE", State.DONE)
    assert not can_transition(State.DONE, "ALSO_NOT_A_STATE")


# --------------------------------------------------------------------------
# 终态
# --------------------------------------------------------------------------
def test_terminal_states():
    assert TERMINAL_STATES == {State.DONE, State.FAILED, State.CANCELLED}
    for state in TERMINAL_STATES:
        assert is_terminal(state)
        assert next_auto_state(state) is None


def test_failed_can_resume_from_checkpoint():
    assert can_transition(State.FAILED, State.DRAFT_RENDERING)
    assert can_transition(State.FAILED, State.FINAL_RENDERING)
    assert can_transition(State.FAILED, State.SCRIPT_READY)


def test_paused_budget_resumes_the_state_it_came_from():
    """配额暂停后要能回到**暂停前的那个状态**。

    之前 PAUSED_BUDGET 只能回 DRAFT_RENDERING / FINAL_RENDERING,
    "不能从配额暂停直接跳到别的阶段"。现在暂停前在哪个阶段,提额后
    就从哪个阶段继续 —— 暂停前在事实采集,接着采事实,而不是跳到渲染。
    暂停前在哪个状态记在 `projects.resume_state`。
    """
    assert can_transition(State.PAUSED_BUDGET, State.DRAFT_RENDERING)
    assert can_transition(State.PAUSED_BUDGET, State.FINAL_RENDERING)
    assert can_transition(State.PAUSED_BUDGET, State.GATE_A)
    assert can_transition(State.PAUSED_BUDGET, State.FACTS_RESOLVED)
    assert can_transition(State.PAUSED_BUDGET, State.SCRIPT_READY)
    assert can_transition(State.PAUSED_BUDGET, State.DRAFT_INTENT)
    # 合成与完成是终点,不该从配额暂停跳过去
    assert not can_transition(State.PAUSED_BUDGET, State.COMPOSING)
    assert not can_transition(State.PAUSED_BUDGET, State.DONE)


def test_budget_can_pause_every_spending_stage():
    """会花钱的阶段都必须能就地转入 PAUSED_BUDGET。

    花钱的阶段:事实采集(DRAFT_INTENT)、脚本生成(FACTS_RESOLVED)、
    逐拍版式(SCRIPT_READY)、门禁 A 的 TTS 合成(GATE_A)。
    漏掉任何一个,那个阶段的超支就拦不住。
    """
    for st in (State.DRAFT_INTENT, State.FACTS_RESOLVED, State.SCRIPT_READY, State.GATE_A):
        assert can_transition(st, State.PAUSED_BUDGET), f"{st} 该能转入配额暂停"


def test_cancel_available_from_working_states():
    working = [s for s in State if s not in (State.DONE, State.CANCELLED)]
    missing = [s for s in working if not can_transition(s, State.CANCELLED)]
    assert missing == [State.GATE_A] or missing == []


# --------------------------------------------------------------------------
# 门禁放行:orchestrator.advance 的 confirmed 参数
# --------------------------------------------------------------------------
def _fake_project(state):
    return {
        "id": "p_test", "state": str(state), "subject": "测试",
        "category": "auto_long", "duration": 60, "narration_mode": "tts",
        "shot_count": 10, "quota_budget": 40.0, "spent_cost": 0.0,
    }


def test_advance_blocks_at_gate_without_confirmation(monkeypatch):
    """未确认时 advance 必须原地等待,这是门禁的全部意义。"""
    from app.pipeline import orchestrator
    from app.state import State

    monkeypatch.setattr(orchestrator.db, "get_project",
                        lambda *a, **k: _fake_project(State.GATE_A))
    out = orchestrator.advance("p_test")
    assert out["waiting"] is True
    assert out["state"] == "GATE_A"

    monkeypatch.setattr(orchestrator.db, "get_project",
                        lambda *a, **k: _fake_project(State.GATE_B))
    out = orchestrator.advance("p_test")
    assert out["waiting"] is True


def test_confirm_gate_passes_the_gate(monkeypatch):
    """回归:confirm_gate 曾调用 advance() 而 advance 又拒绝门禁状态,
    导致人工确认永远无法放行,流水线在 GATE_A 死锁。

    现在 confirm_gate 必须传 confirmed=True,门禁才真的过得去。
    """
    from app.pipeline import orchestrator
    from app.state import State

    seen: dict = {}

    def fake_advance(project_id, *, confirmed=False):
        seen["confirmed"] = confirmed
        seen["project_id"] = project_id
        return {"state": "ASSETS_READY", "waiting": False}

    monkeypatch.setattr(orchestrator.db, "get_project",
                        lambda *a, **k: _fake_project(State.GATE_A))
    monkeypatch.setattr(orchestrator, "advance", fake_advance)

    orchestrator.confirm_gate("p_test", orchestrator.GateRequest(gate="A"))
    assert seen["confirmed"] is True, "门禁 A 确认后必须以 confirmed=True 推进"


def test_confirm_gate_b_passes_the_gate(monkeypatch):
    from app.pipeline import orchestrator
    from app.state import State

    seen: dict = {}

    def fake_advance(project_id, *, confirmed=False):
        seen["confirmed"] = confirmed
        return {"state": "FINAL_RENDERING", "waiting": False}

    monkeypatch.setattr(orchestrator.db, "get_project",
                        lambda *a, **k: _fake_project(State.GATE_B))
    monkeypatch.setattr(orchestrator.db, "write_json", lambda *a, **k: None)
    monkeypatch.setattr(orchestrator, "advance", fake_advance)

    orchestrator.confirm_gate("p_test", orchestrator.GateRequest(gate="B"))
    assert seen["confirmed"] is True


# --------------------------------------------------------------------------
# 后台调度器必须覆盖所有"自己会往前走"的状态
# --------------------------------------------------------------------------
def test_scheduler_covers_every_autonomous_state():
    """回归:调度器曾只推进 FINAL_RENDERING 与 COMPOSING,漏掉 DRAFT_RENDERING。

    后果不是报错,而是草稿阶段的镜头永远停在 pending —— 页面看着一切正常,
    就是永远不动。这类"静默不工作"最难查,所以把状态集合锁死在这里。
    """
    from app.main import AUTO_ADVANCE_STATES
    from app.state import State

    for state in (State.DRAFT_RENDERING, State.FINAL_RENDERING, State.COMPOSING):
        assert str(state) in AUTO_ADVANCE_STATES, f"调度器未覆盖 {state}"


def test_scheduler_does_not_include_gates():
    """门禁绝不能出现在自动推进集合里 —— 否则人工审核形同虚设。"""
    from app.main import AUTO_ADVANCE_STATES
    from app.state import State

    for gate in (State.GATE_A, State.GATE_B):
        assert str(gate) not in AUTO_ADVANCE_STATES


def test_scheduler_does_not_auto_pass_gates():
    """advance 必须要求显式确认,调度器不传该参数。"""
    import inspect

    from app.pipeline import orchestrator

    sig = inspect.signature(orchestrator.advance)
    assert sig.parameters["confirmed"].default is False
