"""状态机声明表(`state.SPECS`)与它的推导物。

调度器的通道、编排器的处理器、`/advance` 的防护都从同一张表推导;这里守住
"表自洽 + 每个消费者都覆盖了表里该覆盖的状态"。以前同一份信息散在四处,
漏改一处就是"静静卡住"。
"""
from __future__ import annotations

import pytest

from app import config, db, state
from app.state import AUTO, GATE, SPECS, State


def test_every_state_is_declared():
    assert set(SPECS) == set(State)


def test_declarations_are_self_consistent():
    state._validate()  # 模块加载时已跑过一次,这里显式再跑,失败信息更直接


def test_validate_rejects_auto_state_without_lane(monkeypatch):
    monkeypatch.setitem(SPECS, State.COMPOSING, state.StateSpec(AUTO, None, State.DONE))
    with pytest.raises(RuntimeError, match="COMPOSING"):
        state._validate()


def test_validate_rejects_next_outside_transitions(monkeypatch):
    monkeypatch.setitem(SPECS, State.SCRIPT_READY,
                        state.StateSpec(AUTO, state.PREP_LANE, State.DONE))
    with pytest.raises(RuntimeError, match="SCRIPT_READY"):
        state._validate()


def test_scheduler_lanes_come_from_specs():
    from app import scheduler

    for s, spec in SPECS.items():
        if spec.kind == AUTO:
            assert str(s) in scheduler.AUTO_ADVANCE_STATES
            want = scheduler.RENDER_STATES if spec.lane == state.RENDER_LANE \
                else scheduler.PREP_STATES
            assert str(s) in want
        else:
            assert str(s) not in scheduler.AUTO_ADVANCE_STATES
    assert not (scheduler.RENDER_STATES & scheduler.PREP_STATES)


def test_every_auto_and_gate_state_has_a_stage_handler():
    """新增自动态 / 门禁态却没写处理器,调度器会把它入队然后什么也不做。"""
    from app.pipeline import orchestrator

    need = {s for s, spec in SPECS.items() if spec.kind in (AUTO, GATE)}
    assert set(orchestrator.STAGES) == need


# --------------------------------------------------------------------------
# 用临时库的部分
# --------------------------------------------------------------------------
@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "data" / "projects")
    monkeypatch.setattr(db, "_INITIALISED", False)
    db.init_db()
    yield db
    db._INITIALISED = False


def _project(pid: str, st: str, updated_at: float) -> dict:
    return {
        "id": pid, "title": pid, "subject": "测试车", "category": "auto_long",
        "theme": "buyguide", "angle": "", "subjects": [], "duration": 240,
        "shot_count": 1, "narration_mode": "tts", "state": st,
        "state_reason": None, "quota_budget": 40.0, "spent_cost": 0.0,
        "est_cost": 1.0, "created_at": updated_at, "updated_at": updated_at,
    }


def test_scan_reaches_old_auto_projects_beyond_the_latest_100(fresh_db, monkeypatch):
    """回归:扫描原来只看"最近更新的 100 个",久未更新的自动态项目会永远扫不到。"""
    from app import scheduler as sched_mod

    for i in range(120):
        fresh_db.create_project(_project(f"p_done_{i:03d}", "DONE", 1000.0 + i))
    fresh_db.create_project(_project("p_old_render", "DRAFT_RENDERING", 1.0))
    with fresh_db.connect() as conn:  # create_project 会把 updated_at 盖成现在
        conn.execute("UPDATE projects SET updated_at = 1.0 WHERE id = 'p_old_render'")

    s = sched_mod.Scheduler()
    seen: list[str] = []
    monkeypatch.setattr(s, "_enqueue", lambda pid, st=None: seen.append(pid))
    s._queue = object()  # 让 _scan 认为调度器在跑
    s._scan()
    assert seen == ["p_old_render"]


def test_advance_endpoint_defers_auto_states_to_scheduler(fresh_db, monkeypatch):
    """自动态下 `/advance` 不再同步推进(会和调度器抢写同一批文件),改为催调度器。"""
    from fastapi.testclient import TestClient

    from app.main import app
    from app.pipeline import orchestrator
    from app.routes import projects as routes

    fresh_db.create_project(_project("p_r", "DRAFT_RENDERING", 1.0))
    kicked: list[str] = []
    monkeypatch.setattr(routes.scheduler, "kick", kicked.append)

    def boom(*a, **k):
        raise AssertionError("自动态不该同步调 advance")

    monkeypatch.setattr(orchestrator, "advance", boom)
    r = TestClient(app).post("/api/projects/p_r/advance")
    assert r.status_code == 200
    assert r.json()["queued"] is True
    assert kicked == ["p_r"]


def test_advance_endpoint_still_runs_for_gate_states(fresh_db, monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app
    from app.pipeline import orchestrator

    fresh_db.create_project(_project("p_g", "GATE_A", 1.0))
    monkeypatch.setattr(orchestrator, "advance",
                        lambda pid, **k: {"state": "GATE_A", "waiting": True})
    r = TestClient(app).post("/api/projects/p_g/advance")
    assert r.status_code == 200 and r.json()["waiting"] is True
    assert TestClient(app).post("/api/projects/nope/advance").status_code == 404
