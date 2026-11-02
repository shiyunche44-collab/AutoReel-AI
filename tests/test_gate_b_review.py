"""门禁 B 逐拍确认:标记不满意的拍,只重渲这几拍;还有标记没处理时不放行。"""
from __future__ import annotations

import pytest

from app import config, db
from app.models import ShotSpec
from app.pipeline import explainer, locks, orchestrator
from app.state import State


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "data" / "projects")
    db._INITIALISED = False
    db.init_db()
    yield db
    db._INITIALISED = False


def _project(state: str = "GATE_B") -> dict:
    return {
        "id": "p_gb", "title": "审片", "subject": "测试车", "category": "auto_long",
        "theme": "buyguide", "angle": "", "subjects": [], "duration": 240,
        "shot_count": 4, "narration_mode": "tts", "state": state,
        "state_reason": None, "quota_budget": 40.0, "spent_cost": 0.0,
        "est_cost": 1.0, "created_at": 1.0, "updated_at": 1.0,
    }


@pytest.fixture()
def gate_b(store):
    store.create_project(_project())
    specs = [ShotSpec(idx=i, duration=6.0, section="开场", visual_zh="测试",
                      narration_zh="测试旁白", layout="broll") for i in range(4)]
    orchestrator._save_specs("p_gb", specs)
    store.upsert_shots("p_gb", [
        {"idx": i, "stage": "draft", "status": "succeeded", "model": "explainer",
         "provider_task_id": None, "submitted_at": None, "last_polled_at": None,
         "draft_path": f"d{i}.mp4", "final_path": None, "error": None} for i in range(4)])
    explainer.beats_dir("p_gb")
    for i in range(4):
        db.write_json("p_gb", f"beats/{i:03d}.json", [{"item_id": f"clip{i}"}])
    return "p_gb"


def _status(pid: str) -> dict[int, str]:
    return {r["idx"]: r["status"] for r in db.get_shots(pid, "draft")}


def test_flag_and_unflag(gate_b):
    assert orchestrator.flag_beats(gate_b, [1, 3]) == [1, 3]
    assert orchestrator.review_progress(gate_b)["flagged"] == [1, 3]
    assert orchestrator.flag_beats(gate_b, [1], flagged=False) == [3]


def test_flag_rejects_unknown_beat_and_wrong_state(gate_b, store):
    with pytest.raises(orchestrator.PipelineError):
        orchestrator.flag_beats(gate_b, [99])
    store.update_project(gate_b, state="DONE")
    with pytest.raises(orchestrator.PipelineError):
        orchestrator.flag_beats(gate_b, [1])


def test_rerender_only_touches_selected_beats(gate_b):
    orchestrator.flag_beats(gate_b, [1, 2])
    out = orchestrator.rerender_beats(gate_b, [1])
    assert out == {"rerender": [1]}
    assert _status(gate_b) == {0: "succeeded", 1: "pending", 2: "succeeded", 3: "succeeded"}
    assert db.get_project(gate_b)["state"] == State.DRAFT_RENDERING.value
    # 只清重拍的那拍的标记,别的拍的标记还在
    assert orchestrator.review_progress(gate_b)["flagged"] == [2]
    # 刚用过的片段排后面:和单拍重拍同一套 +3
    assert db.read_json(gate_b, "beats_used.json", {}) == {"clip1": 3}


def test_state_is_switched_after_beats_are_pending(gate_b, monkeypatch):
    """先切状态,调度器会抢在拍置 pending 之前开渲 —— 顺序不能反。"""
    seen: dict[int, str] = {}
    real = orchestrator._set_state

    def spy(pid, src, dst, **kw):
        seen.update(_status(pid))
        return real(pid, src, dst, **kw)

    monkeypatch.setattr(orchestrator, "_set_state", spy)
    orchestrator.rerender_beats(gate_b, [0, 2])
    assert seen[0] == seen[2] == "pending"


def test_rerender_needs_selection_and_lock(gate_b):
    with pytest.raises(orchestrator.PipelineError):
        orchestrator.rerender_beats(gate_b, [])
    with locks.project_lock(gate_b):
        with pytest.raises(locks.ProjectBusy):
            orchestrator.rerender_beats(gate_b, [1])
    assert set(_status(gate_b).values()) == {"succeeded"}


def test_confirm_gate_refuses_while_flagged(gate_b, monkeypatch):
    orchestrator.flag_beats(gate_b, [2])
    monkeypatch.setattr(orchestrator, "advance", lambda *a, **k: pytest.fail("不该放行"))
    with pytest.raises(orchestrator.PipelineError, match="不满意"):
        orchestrator.confirm_gate(gate_b, orchestrator.GateRequest(gate="B"))


def test_confirm_gate_passes_when_nothing_flagged(gate_b, monkeypatch):
    orchestrator.flag_beats(gate_b, [2])
    orchestrator.flag_beats(gate_b, [2], flagged=False)
    monkeypatch.setattr(orchestrator, "advance", lambda *a, **k: {"ok": True})
    assert orchestrator.confirm_gate(gate_b, orchestrator.GateRequest(gate="B")) == {"ok": True}
    saved = db.read_json(gate_b, "review_state.json", {})
    assert saved["approved"] == [0, 1, 2, 3] and saved["flagged"] == []


def test_single_retry_clears_its_flag(gate_b):
    orchestrator.flag_beats(gate_b, [1, 2])
    orchestrator.retry(gate_b, 1, "final")
    assert orchestrator.review_progress(gate_b)["flagged"] == [2]


def test_review_routes(gate_b):
    from fastapi.testclient import TestClient

    from app import main

    client = TestClient(main.app)
    resp = client.patch(f"/api/projects/{gate_b}/review", json={"idxs": [1, 2]})
    assert resp.status_code == 200 and resp.json() == {"flagged": [1, 2]}
    assert client.get(f"/api/projects/{gate_b}/review").json()["flagged"] == [1, 2]
    assert client.patch(f"/api/projects/{gate_b}/review", json={"idxs": [99]}).status_code == 400
    assert client.post(f"/api/projects/{gate_b}/review/rerender", json={"idxs": []}).status_code == 400
    with locks.project_lock(gate_b):
        busy = client.post(f"/api/projects/{gate_b}/review/rerender", json={"idxs": [1]})
    assert busy.status_code == 409
    ok = client.post(f"/api/projects/{gate_b}/review/rerender", json={"idxs": [1, 2]})
    assert ok.status_code == 200 and ok.json() == {"rerender": [1, 2]}
