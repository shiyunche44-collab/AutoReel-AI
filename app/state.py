"""项目状态机(计划 §3.4)。

两个关键设计:
- GATE_A / GATE_B 是人工确认态,只有用户显式确认才能推进 —— 防止在错误分镜上消耗配额。
- 所有阶段幂等:重跑时若已有产物则跳过,不重复提交生成任务。
"""
from __future__ import annotations

from enum import Enum


class State(str, Enum):
    DRAFT_INTENT = "DRAFT_INTENT"
    FACTS_RESOLVED = "FACTS_RESOLVED"
    SCRIPT_READY = "SCRIPT_READY"
    STORYBOARD_READY = "STORYBOARD_READY"
    GATE_A = "GATE_A"
    ASSETS_READY = "ASSETS_READY"
    PROMPTS_READY = "PROMPTS_READY"
    DRAFT_RENDERING = "DRAFT_RENDERING"
    GATE_B = "GATE_B"
    FINAL_RENDERING = "FINAL_RENDERING"
    QA_PASSED = "QA_PASSED"
    COMPOSING = "COMPOSING"
    DONE = "DONE"
    PAUSED_BUDGET = "PAUSED_BUDGET"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

    def __str__(self) -> str:
        """返回裸值而不是 "State.X"。

        `str, Enum` 混用在 Python 3.11+ 中 str() 会返回 "State.DRAFT_INTENT",
        直接落库或返回给前端都会污染状态字段,必须显式覆写。
        """
        return str(self.value)


#: 终态:到达后调度器不再自动推进。
TERMINAL_STATES = {State.DONE, State.FAILED, State.CANCELLED}

#: 需要用户显式确认才能离开的状态。
GATE_STATES = {State.GATE_A, State.GATE_B}

#: 允许的状态转移。任何未列出的转移都会被 state.can_transition() 拒绝。
TRANSITIONS: dict[State, set[State]] = {
    State.DRAFT_INTENT: {State.FACTS_RESOLVED, State.FAILED, State.CANCELLED},
    State.FACTS_RESOLVED: {State.SCRIPT_READY, State.FAILED, State.CANCELLED},
    State.SCRIPT_READY: {State.STORYBOARD_READY, State.FAILED, State.CANCELLED},
    State.STORYBOARD_READY: {State.GATE_A, State.FAILED, State.CANCELLED},
    State.GATE_A: {State.ASSETS_READY, State.SCRIPT_READY, State.STORYBOARD_READY, State.FAILED, State.CANCELLED},
    State.ASSETS_READY: {State.PROMPTS_READY, State.FAILED, State.CANCELLED},
    State.PROMPTS_READY: {State.DRAFT_RENDERING, State.FAILED, State.CANCELLED},
    State.DRAFT_RENDERING: {State.GATE_B, State.FAILED, State.PAUSED_BUDGET, State.CANCELLED},
    State.GATE_B: {State.FINAL_RENDERING, State.DRAFT_RENDERING, State.FAILED, State.PAUSED_BUDGET, State.CANCELLED},
    State.FINAL_RENDERING: {State.QA_PASSED, State.GATE_B, State.FAILED, State.PAUSED_BUDGET, State.CANCELLED},
    State.QA_PASSED: {State.COMPOSING, State.FAILED, State.CANCELLED},
    State.COMPOSING: {State.DONE, State.FAILED, State.CANCELLED},
    # 以下状态可被用户从任意阶段重置或恢复
    State.PAUSED_BUDGET: {
        State.DRAFT_RENDERING, State.GATE_B, State.FINAL_RENDERING, State.CANCELLED,
    },
    State.FAILED: {
        State.FACTS_RESOLVED, State.SCRIPT_READY, State.STORYBOARD_READY, State.GATE_A,
        State.ASSETS_READY, State.PROMPTS_READY, State.DRAFT_RENDERING, State.GATE_B,
        State.FINAL_RENDERING, State.QA_PASSED, State.COMPOSING, State.CANCELLED,
    },
    State.CANCELLED: set(),
    State.DONE: set(),
}


class IllegalTransition(RuntimeError):
    """请求了状态机未允许的转移。"""


def can_transition(src: State | str, dst: State | str) -> bool:
    try:
        src_state = State(src)
        dst_state = State(dst)
    except ValueError:
        return False
    return dst_state in TRANSITIONS.get(src_state, set())


def assert_transition(src: State | str, dst: State | str) -> None:
    if not can_transition(src, dst):
        raise IllegalTransition(f"非法状态转移:{src} -> {dst}")


def is_gate(state: State | str) -> bool:
    """是否为需要人工确认的门禁态。"""
    try:
        return State(state) in GATE_STATES
    except ValueError:
        return False


def is_terminal(state: State | str) -> bool:
    try:
        return State(state) in TERMINAL_STATES
    except ValueError:
        return True


def next_auto_state(state: State | str) -> State | None:
    """返回该状态下唯一合理的自动推进目标(门禁态返回 None,等待人工)。

    用于调度器:门禁态必须停下等人,其余状态按流水线下推。
    """
    try:
        cur = State(state)
    except ValueError:
        return None
    if cur in GATE_STATES or cur in TERMINAL_STATES:
        return None
    auto_order = [
        State.FACTS_RESOLVED, State.SCRIPT_READY, State.STORYBOARD_READY,
        State.ASSETS_READY, State.PROMPTS_READY, State.DRAFT_RENDERING,
        State.FINAL_RENDERING, State.QA_PASSED, State.COMPOSING, State.DONE,
    ]
    for candidate in auto_order:
        if candidate in TRANSITIONS.get(cur, set()):
            return candidate
    return None
