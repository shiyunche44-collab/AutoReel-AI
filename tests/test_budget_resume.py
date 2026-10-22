"""提额后能继续。

配额暂停不是死局:提额之后要能回到暂停前的那个状态接着干,而不是
让人手动从头再跑一遍(事实采集、脚本生成都是要花钱的)。
"""
from __future__ import annotations

import pytest

from app import config, db
from app.pipeline import orchestrator, script_gen
from app.state import State


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "data" / "projects")
    db._INITIALISED = False
    db.init_db()
    yield db
    db._INITIALISED = False


def _project(pid: str = "p_resume", state: str = "FACTS_RESOLVED",
             budget: float = 40.0) -> dict:
    return {
        "id": pid, "title": "提额测试", "subject": "测试车", "category": "auto_long",
        "theme": "buyguide", "angle": "", "subjects": [], "duration": 240,
        "shot_count": 10, "narration_mode": "tts", "state": state,
        "state_reason": None, "quota_budget": budget, "spent_cost": 0.0,
        "est_cost": 1.0, "created_at": 1.0, "updated_at": 1.0,
    }


def _pause(store, monkeypatch, budget: float = 0.0001) -> None:
    store.create_project(_project(budget=budget))
    monkeypatch.setattr(orchestrator, "_llm", lambda: object())
    monkeypatch.setattr(script_gen, "generate", lambda *a, **k: [])
    orchestrator.advance("p_resume")


def test_raise_budget_resumes_to_the_paused_state(store, monkeypatch):
    """提额后回到暂停前的状态 —— 接着干,不是从头再来。"""
    _pause(store, monkeypatch)
    assert store.get_project("p_resume")["state"] == str(State.PAUSED_BUDGET)

    out = orchestrator.set_budget("p_resume", 50.0)
    assert out["budget"] == 50.0
    assert out.get("resumed") is True
    row = store.get_project("p_resume")
    assert row["state"] == str(State.FACTS_RESOLVED)
    assert row["quota_budget"] == pytest.approx(50.0)


def test_raise_budget_works_without_pausing(store):
    """没暂停时也能改上限(下次新建项目按新额度来)。"""
    store.create_project(_project())
    out = orchestrator.set_budget("p_resume", 12.5)
    assert out["budget"] == 12.5
    assert out.get("resumed") is None
    assert store.get_project("p_resume")["quota_budget"] == pytest.approx(12.5)


def test_budget_endpoint_resumes(store, monkeypatch):
    """接口层:PATCH /api/projects/{id}/budget"""
    from fastapi.testclient import TestClient

    from app import main

    _pause(store, monkeypatch)
    client = TestClient(main.app)
    resp = client.patch("/api/projects/p_resume/budget", json={"budget": 30.0})
    assert resp.status_code == 200, resp.text
    assert store.get_project("p_resume")["state"] == str(State.FACTS_RESOLVED)


def test_budget_form_resumes(store, monkeypatch):
    """项目页的表单(零 JS):POST /project/{id}/budget"""
    from fastapi.testclient import TestClient

    from app import main

    _pause(store, monkeypatch)
    client = TestClient(main.app)
    resp = client.post("/project/p_resume/budget", data={"budget": "30"},
                        follow_redirects=False)
    assert resp.status_code == 303, resp.text
    assert store.get_project("p_resume")["state"] == str(State.FACTS_RESOLVED)
    assert store.get_project("p_resume")["quota_budget"] == pytest.approx(30.0)


def test_resume_restores_ability_to_spend(store, monkeypatch):
    """提额之后要真的能继续花 —— 预留不再被拒。"""
    _pause(store, monkeypatch)
    orchestrator.set_budget("p_resume", 50.0)
    from app import costs
    assert costs.reserve_project("p_resume", 1.0) == pytest.approx(1.0)
