"""重拍与在途渲染的竞态。

用户点「换背景重拍」时,那一拍可能正在被渲染。之前的做法是:
重拍把该拍置回 pending,而**在途的渲染随后把状态覆盖回 succeeded** ——
用户的操作被静默吞掉,界面上看着像"重拍了",成片却还是原来那版。

两道防线:

1. **项目锁**。渲染与重拍互斥,重拍撞上渲染时明确报 409,让用户重试,
   而不是默默做掉。
2. **逐拍重读状态**(`explainer.render_pending`)。每渲一拍之前重读一次
   该拍的当前状态,已经是 succeeded 的直接跳过 —— 防止两路渲染抢同一个
   输出文件、产出半截 mp4。
"""
from __future__ import annotations

import threading

import pytest

from app import config, db
from app.categories import load_profile
from app.models import ShotSpec
from app.pipeline import explainer, locks, orchestrator


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "data" / "projects")
    db._INITIALISED = False
    db.init_db()
    yield db
    db._INITIALISED = False


def _project(pid: str = "p_rr") -> dict:
    return {
        "id": pid, "title": "重拍测试", "subject": "测试车", "category": "auto_long",
        "theme": "buyguide", "angle": "", "subjects": [], "duration": 240,
        "shot_count": 1, "narration_mode": "tts", "state": "DRAFT_RENDERING",
        "state_reason": None, "quota_budget": 40.0, "spent_cost": 0.0,
        "est_cost": 1.0, "created_at": 1.0, "updated_at": 1.0,
    }


def _spec(idx: int = 0) -> ShotSpec:
    return ShotSpec(idx=idx, duration=6.0, section="开场", visual_zh="测试",
                    narration_zh="测试旁白", layout="broll")


def _seed_shot(pid: str = "p_rr", idx: int = 0, status: str = "succeeded") -> None:
    db.upsert_shots(pid, [{"idx": idx, "stage": "draft", "status": status,
                           "model": "explainer", "provider_task_id": None,
                           "submitted_at": None, "last_polled_at": None,
                           "draft_path": None, "final_path": None, "error": None}])


def test_retry_during_render_is_rejected(store):
    """渲染途中点重拍:必须被明确拒绝,而不是被在途渲染覆盖掉。"""
    store.create_project(_project())
    _seed_shot(status="succeeded")
    with locks.project_lock("p_rr"):
        with pytest.raises(locks.ProjectBusy):
            orchestrator.retry("p_rr", 0, "final")
    # 拍的状态没被动过
    rows = db.get_shots("p_rr", "draft")
    assert rows[0]["status"] == "succeeded"


def test_retry_after_render_finishes_works(store):
    """没有并发时,重拍正常生效:该拍退回 pending,产物清掉。"""
    store.create_project(_project())
    _seed_shot(status="succeeded")
    db.update_shot("p_rr", 0, "draft", draft_path="drafts/000.mp4")
    orchestrator.retry("p_rr", 0, "final")
    row = db.get_shots("p_rr", "draft")[0]
    assert row["status"] == "pending"
    assert not row["draft_path"]


def test_render_pending_skips_an_already_rendered_beat(store, monkeypatch):
    """已经渲好的拍不会再渲一遍(防止两路抢同一个输出文件)。"""
    store.create_project(_project())
    _seed_shot(status="succeeded")

    def boom(*a, **k):
        raise AssertionError("不该重渲已经渲好的拍")

    monkeypatch.setattr(explainer, "render_beat", boom)
    out = explainer.render_pending("p_rr", [_spec()], subject="测试车",
                                   profile=load_profile("auto_long"))
    assert out["rendered"] == []
    assert out["errors"] == []


def test_render_pending_renders_a_pending_beat(store, monkeypatch):
    """反向:pending 的拍照常渲(别把防线做成什么都不干)。"""
    store.create_project(_project())
    _seed_shot(status="pending")
    calls: list[int] = []

    def fake_render(spec, **kwargs):
        calls.append(spec.idx)
        return config.project_dir("p_rr") / "drafts" / "000.mp4", []

    monkeypatch.setattr(explainer, "render_beat", fake_render)
    out = explainer.render_pending("p_rr", [_spec()], subject="测试车",
                                   profile=load_profile("auto_long"))
    assert calls == [0]
    assert out["rendered"] == [0]
    assert out["errors"] == []


def test_two_threads_rerender_during_render_only_one_wins(store):
    """并发重拍:一个成功,另一个拿到锁失败,不会互相覆盖。"""
    store.create_project(_project())
    _seed_shot(status="succeeded")
    results: list[str] = []
    guard = threading.Lock()
    barrier = threading.Barrier(2, timeout=10)

    def attempt() -> None:
        barrier.wait()
        try:
            orchestrator.retry("p_rr", 0, "final")
            out = "ok"
        except locks.ProjectBusy:
            out = "busy"
        with guard:
            results.append(out)

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert sorted(results) == ["busy", "ok"]
