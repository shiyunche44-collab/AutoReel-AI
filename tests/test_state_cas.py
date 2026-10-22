"""状态机的 CAS 写:判断和写必须是同一条 SQL。

原来的 `_set_state` 是"读出来 → 判断转移合法 → 无条件 UPDATE",
中间有 TOCTOU 窗口。两个执行流都读到 DRAFT_RENDERING 时,会各自
开渲同一批拍。`cas_state` 把判断和写合成一条 SQL,窗口就没了。
"""
from __future__ import annotations

import threading

import pytest

from app import config, db


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "data" / "projects")
    db._INITIALISED = False
    db.init_db()
    yield db
    db._INITIALISED = False


def _project(pid: str = "p_cas") -> dict:
    return {
        "id": pid, "title": "CAS 测试", "subject": "测试车", "category": "auto_long",
        "theme": "buyguide", "angle": "", "subjects": [], "duration": 240,
        "shot_count": 10, "narration_mode": "tts", "state": "DRAFT_INTENT",
        "state_reason": None, "quota_budget": 40.0, "spent_cost": 0.0,
        "est_cost": 1.0, "created_at": 1.0, "updated_at": 1.0,
    }


def test_cas_applies_once(store):
    db.create_project(_project())
    assert db.cas_state("p_cas", "DRAFT_INTENT", "FACTS_RESOLVED") is True
    assert db.get_project("p_cas")["state"] == "FACTS_RESOLVED"
    assert db.cas_state("p_cas", "DRAFT_INTENT", "SCRIPT_READY") is False
    assert db.get_project("p_cas")["state"] == "FACTS_RESOLVED"


def test_concurrent_cas_only_one_wins(store):
    """并发 CAS:只有一个能改成功,其余全部失败。"""
    db.create_project(_project())
    n = 8
    results: list[bool] = []
    guard = threading.Lock()
    barrier = threading.Barrier(n, timeout=10)

    def attempt(i: int) -> None:
        barrier.wait()
        ok = db.cas_state("p_cas", "DRAFT_INTENT", f"STATE_{i}")
        with guard:
            results.append(ok)

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert sum(1 for r in results if r) == 1, "只应有一个 CAS 成功"
    assert len(results) == n


def test_cas_writes_state_reason(store):
    db.create_project(_project())
    db.cas_state("p_cas", "DRAFT_INTENT", "FACTS_RESOLVED", reason="采集到 3 条事实")
    assert db.get_project("p_cas")["state_reason"] == "采集到 3 条事实"


def test_cas_inside_transaction_rolls_back(store):
    """CAS 写在事务里,事务回滚时状态也要回滚。"""
    db.create_project(_project())
    with pytest.raises(RuntimeError):
        with db.transaction() as conn:
            db.cas_state("p_cas", "DRAFT_INTENT", "FACTS_RESOLVED", conn=conn)
            raise RuntimeError("中途炸了")
    assert db.get_project("p_cas")["state"] == "DRAFT_INTENT"


def test_set_state_rejects_concurrent_movement(monkeypatch):
    """`_set_state` 在状态已被别人推进时必须拒绝,而不是覆盖回去。"""
    from app.pipeline import orchestrator
    from app.state import State

    class _FakeDb:
        """把 cas_state 变成"失败",模拟状态已被别人推进。"""

        def __init__(self) -> None:
            self.transaction_cm = None

        def transaction(self):
            from app.db import transaction
            return transaction()

        def cas_state(self, *a, **k):
            return False

        def add_event(self, *a, **k):
            raise AssertionError("CAS 失败时不该记事件")

    monkeypatch.setattr(orchestrator, "db", _FakeDb())
    with pytest.raises(orchestrator.PipelineError):
        orchestrator._set_state("p_x", State.DRAFT_INTENT, State.FACTS_RESOLVED)
