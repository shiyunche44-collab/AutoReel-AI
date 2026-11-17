"""项目状态机(计划 §3.4)。

两个关键设计:
- GATE_A / GATE_B 是人工确认态,只有用户显式确认才能推进 —— 防止在错误分镜上消耗配额。
- 所有阶段幂等:重跑时若已有产物则跳过,不重复提交生成任务。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class State(str, Enum):
    DRAFT_INTENT = "DRAFT_INTENT"
    FACTS_RESOLVED = "FACTS_RESOLVED"
    SCRIPT_READY = "SCRIPT_READY"
    STORYBOARD_READY = "STORYBOARD_READY"
    GATE_A = "GATE_A"
    ASSETS_READY = "ASSETS_READY"
    DRAFT_RENDERING = "DRAFT_RENDERING"
    GATE_B = "GATE_B"
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


#: 状态的种类
AUTO, GATE, PAUSED, TERMINAL = "auto", "gate", "paused", "terminal"

#: 自动态归哪条调度通道(见 `app/scheduler.py`)
#: - 渲染:ffmpeg + 概念动画的 Chrome,内存是硬约束(`PIPELINE_CONCURRENCY` 路)
#: - 备料:事实 / 写稿 / 分镜,等 LLM 和网络为主(`PREP_CONCURRENCY` 路)
RENDER_LANE, PREP_LANE = "render", "prep"


@dataclass(frozen=True)
class StateSpec:
    """一个状态的全部"调度属性",集中在一处声明。

    以前这些信息散在四个地方:`TRANSITIONS`、调度器的 `AUTO_ADVANCE_STATES` /
    `RENDER_STATES` / `PREP_STATES`、`next_auto_state` 里的顺序表、编排器的 if 链。
    新增状态漏改其中一处就是"静静卡住"。现在调度器、编排器和测试都从 `SPECS` 推导,
    模块加载时 `_validate()` 自检,声明不自洽直接起不来。
    """

    kind: str
    #: 自动态归哪条通道;非自动态为 None
    lane: str | None = None
    #: 这一步成功后去哪(自动态和门禁态必填)
    next: State | None = None


SPECS: dict[State, StateSpec] = {
    State.DRAFT_INTENT: StateSpec(AUTO, PREP_LANE, State.FACTS_RESOLVED),
    State.FACTS_RESOLVED: StateSpec(AUTO, PREP_LANE, State.SCRIPT_READY),
    State.SCRIPT_READY: StateSpec(AUTO, PREP_LANE, State.STORYBOARD_READY),
    State.STORYBOARD_READY: StateSpec(AUTO, PREP_LANE, State.GATE_A),
    State.GATE_A: StateSpec(GATE, None, State.ASSETS_READY),
    State.ASSETS_READY: StateSpec(AUTO, PREP_LANE, State.DRAFT_RENDERING),
    State.DRAFT_RENDERING: StateSpec(AUTO, RENDER_LANE, State.GATE_B),
    State.GATE_B: StateSpec(GATE, None, State.COMPOSING),
    State.COMPOSING: StateSpec(AUTO, RENDER_LANE, State.DONE),
    State.PAUSED_BUDGET: StateSpec(PAUSED),
    State.DONE: StateSpec(TERMINAL),
    State.FAILED: StateSpec(TERMINAL),
    State.CANCELLED: StateSpec(TERMINAL),
}


def _of_kind(kind: str) -> frozenset[State]:
    return frozenset(s for s, spec in SPECS.items() if spec.kind == kind)


#: 终态:到达后调度器不再自动推进。
TERMINAL_STATES = _of_kind(TERMINAL)

#: 需要用户显式确认才能离开的状态。
GATE_STATES = _of_kind(GATE)

#: 调度器负责推进的状态(人不该手动推)。
AUTO_STATES = _of_kind(AUTO)


def lane_of(state: State | str) -> str | None:
    """这个状态归哪条调度通道;不自动推进的状态(门禁、终态、配额暂停)返回 None。"""
    try:
        return SPECS[State(state)].lane
    except (ValueError, KeyError):
        return None


def is_auto(state: State | str) -> bool:
    """是否由后台调度器推进。"""
    return lane_of(state) is not None

#: 允许的状态转移。任何未列出的转移都会被 state.can_transition() 拒绝。
TRANSITIONS: dict[State, set[State]] = {
    # 下列状态会花钱(事实采集/脚本/版式的 LLM 调用、门禁 A 的 TTS 合成),
    # 额度不够时都可以就地转入 PAUSED_BUDGET。
    State.DRAFT_INTENT: {State.FACTS_RESOLVED, State.PAUSED_BUDGET, State.FAILED, State.CANCELLED},
    State.FACTS_RESOLVED: {State.SCRIPT_READY, State.PAUSED_BUDGET, State.FAILED, State.CANCELLED},
    State.SCRIPT_READY: {State.STORYBOARD_READY, State.PAUSED_BUDGET, State.FAILED, State.CANCELLED},
    State.STORYBOARD_READY: {State.GATE_A, State.FAILED, State.CANCELLED},
    State.GATE_A: {State.ASSETS_READY, State.SCRIPT_READY, State.STORYBOARD_READY,
                   State.PAUSED_BUDGET, State.FAILED, State.CANCELLED},
    State.ASSETS_READY: {State.DRAFT_RENDERING, State.FAILED, State.CANCELLED},
    State.DRAFT_RENDERING: {State.GATE_B, State.FAILED, State.PAUSED_BUDGET, State.CANCELLED},
    State.GATE_B: {State.COMPOSING, State.DRAFT_RENDERING, State.FAILED, State.PAUSED_BUDGET, State.CANCELLED},
    State.COMPOSING: {State.DONE, State.FAILED, State.CANCELLED},
    # 配额暂停后要能回到**暂停前的那个状态**:暂停前在事实采集,提额后
    # 接着采事实,而不是跳到渲染。(暂停前的状态记在 projects.resume_state)
    State.PAUSED_BUDGET: {
        State.DRAFT_INTENT, State.FACTS_RESOLVED, State.SCRIPT_READY, State.GATE_A,
        State.DRAFT_RENDERING, State.GATE_B,
        State.CANCELLED,
    },
    State.FAILED: {
        State.FACTS_RESOLVED, State.SCRIPT_READY, State.STORYBOARD_READY, State.GATE_A,
        State.ASSETS_READY, State.DRAFT_RENDERING, State.GATE_B,
        State.COMPOSING, State.CANCELLED,
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
    """自动态成功推进一步后到哪(门禁、终态、配额暂停返回 None,等人)。"""
    try:
        spec = SPECS[State(state)]
    except (ValueError, KeyError):
        return None
    return spec.next if spec.kind == AUTO else None


def _validate() -> None:
    """声明表自检。不自洽就让模块加载失败 —— 比上线后"静静卡住"好查得多。"""
    problems: list[str] = []
    for state in State:
        spec = SPECS.get(state)
        if spec is None:
            problems.append(f"{state} 没有声明")
            continue
        if spec.kind == AUTO and spec.lane not in (RENDER_LANE, PREP_LANE):
            problems.append(f"{state} 是自动态但没归通道")
        if spec.kind != AUTO and spec.lane is not None:
            problems.append(f"{state} 不是自动态却归了通道 {spec.lane}")
        if spec.kind in (AUTO, GATE):
            if spec.next is None:
                problems.append(f"{state} 没声明下一步")
            elif spec.next not in TRANSITIONS.get(state, set()):
                problems.append(f"{state} -> {spec.next} 不在转移表里")
    if problems:
        raise RuntimeError("状态机声明不自洽:" + ";".join(problems))


_validate()
