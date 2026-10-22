"""优雅停机:在途的渲染要在**拍之间**收手。

服务被收走时(README:托管后台任务有 1 小时上限),如果是在一拍的中间
被砍掉,产出的是**半截 mp4** —— 播放器打不开,重新渲又得再花一次钱。

所以停机信号必须在拍与拍之间检查:手头这一拍做完就停,剩下的拍留到
下次,重启后从断点接着跑。
"""
from __future__ import annotations

import asyncio
import threading
import time

import pytest

from app import config, db, main
from app.categories import load_profile
from app.models import ShotSpec
from app.pipeline import explainer


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "data" / "projects")
    db._INITIALISED = False
    db.init_db()
    yield db
    db._INITIALISED = False


def _project(pid: str = "p_drain") -> dict:
    return {
        "id": pid, "title": "停机测试", "subject": "测试车", "category": "auto_long",
        "theme": "buyguide", "angle": "", "subjects": [], "duration": 240,
        "shot_count": 2, "narration_mode": "tts", "state": "DRAFT_RENDERING",
        "state_reason": None, "quota_budget": 40.0, "spent_cost": 0.0,
        "est_cost": 1.0, "created_at": 1.0, "updated_at": 1.0,
    }


def _spec(idx: int) -> ShotSpec:
    return ShotSpec(idx=idx, duration=6.0, section="开场", visual_zh="测试",
                    narration_zh="测试旁白", layout="broll")


def _seed_shots(pid: str = "p_drain", n: int = 2) -> None:
    db.upsert_shots(pid, [{"idx": i, "stage": "draft", "status": "pending",
                           "model": "explainer", "provider_task_id": None,
                           "submitted_at": None, "last_polled_at": None,
                           "draft_path": None, "final_path": None, "error": None}
                          for i in range(n)])


# --------------------------------------------------------------------------
# 拍与拍之间收手
# --------------------------------------------------------------------------
def test_stop_signal_prevents_any_render(store, monkeypatch):
    """一开始就收到停机信号 → 一拍都不渲。"""
    store.create_project(_project())
    _seed_shots()
    calls: list[int] = []
    monkeypatch.setattr(explainer, "render_beat",
                        lambda spec, **kw: (calls.append(spec.idx), (Path_none(), []))[1])
    out = explainer.render_pending("p_drain", [_spec(0), _spec(1)], subject="测试车",
                                   profile=load_profile("auto_long"),
                                   should_stop=lambda: True)
    assert calls == [], "收到停机信号就不该再开渲"
    assert out["rendered"] == []


def test_stop_signal_triggers_between_beats(store, monkeypatch):
    """第二拍之前收到信号 → 只渲第一拍,第二拍留着下次。"""
    store.create_project(_project())
    _seed_shots()
    calls: list[int] = []

    def fake_render(spec, **kwargs):
        calls.append(spec.idx)
        return config.project_dir("p_drain") / "drafts" / f"{spec.idx:03d}.mp4", []

    monkeypatch.setattr(explainer, "render_beat", fake_render)
    checks = {"n": 0}

    def should_stop() -> bool:
        checks["n"] += 1
        return checks["n"] > 1  # 第一次检查放行,第二次拦下

    out = explainer.render_pending("p_drain", [_spec(0), _spec(1)], subject="测试车",
                                   profile=load_profile("auto_long"),
                                   should_stop=should_stop)
    assert calls == [0], "只该渲第一拍"
    assert out["rendered"] == [0]
    assert out["stage_complete"] is False, "还有拍没渲完,不该算完成"


def test_no_stop_signal_renders_everything(store, monkeypatch):
    """没有停机信号时行为不变。"""
    store.create_project(_project())
    _seed_shots()
    calls: list[int] = []

    def fake_render(spec, **kwargs):
        calls.append(spec.idx)
        return config.project_dir("p_drain") / "drafts" / f"{spec.idx:03d}.mp4", []

    monkeypatch.setattr(explainer, "render_beat", fake_render)
    out = explainer.render_pending("p_drain", [_spec(0), _spec(1)], subject="测试车",
                                   profile=load_profile("auto_long"))
    assert calls == [0, 1]
    assert out["stage_complete"] is True


# --------------------------------------------------------------------------
# 调度器停机
# --------------------------------------------------------------------------
def test_scheduler_stop_raises_the_drain_signal():
    """stop() 要把停机信号立起来 —— 在途的渲染靠它收手。"""
    async def scenario() -> bool:
        main.scheduler.start()
        await asyncio.sleep(0.05)
        await main.scheduler.stop()
        return main.scheduler._drain.is_set()

    assert asyncio.run(scenario()) is True


def test_scheduler_stop_waits_for_in_flight_work(store, monkeypatch):
    """停机要**等**在途的活收尾,而不是把线程砍掉。"""
    store.create_project(_project())
    started = threading.Event()
    finished = threading.Event()

    def fake_advance(pid, **kwargs):
        started.set()
        # 模拟一拍的耗时
        time.sleep(0.3)
        finished.set()
        return {"state": "DRAFT_RENDERING", "waiting": False}

    monkeypatch.setattr(main.orchestrator, "advance", fake_advance)

    async def scenario() -> bool:
        main.scheduler.start()
        # 等 worker 真的接了活
        for _ in range(100):
            if started.is_set():
                break
            await asyncio.sleep(0.02)
        assert started.is_set(), "worker 应当已经接了这个项目"
        await main.scheduler.stop()
        return finished.is_set()

    assert asyncio.run(scenario()) is True, "停机应等在途的那一拍做完"


def Path_none():  # noqa: N802 - 测试里的占位路径
    import tempfile
    from pathlib import Path
    return Path(tempfile.gettempdir()) / "unused.mp4"
