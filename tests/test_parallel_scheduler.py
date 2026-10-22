"""多项目并行渲染。

同一时刻**不同项目**可以并行渲染,而**同一项目**永远只有一个写入者
(项目锁保证)。

之前调度器是"扫到一个推进一个、串行等它跑完",所以一个项目渲染的
50 秒里,其它项目只能干等 —— 多项目并行渲染这件事根本没发生过。
"""
from __future__ import annotations

import asyncio
import threading
import time

import pytest

from app import config, db, main


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "data" / "projects")
    db._INITIALISED = False
    db.init_db()
    main.scheduler._failures.clear()
    main.scheduler._retry_after.clear()
    main.scheduler._queued.clear()
    main.scheduler._running.clear()
    yield db
    db._INITIALISED = False


def _project(pid: str, state: str = "DRAFT_RENDERING") -> dict:
    return {
        "id": pid, "title": f"并行测试 {pid}", "subject": "测试车", "category": "auto_long",
        "theme": "buyguide", "angle": "", "subjects": [], "duration": 240,
        "shot_count": 1, "narration_mode": "tts", "state": state,
        "state_reason": None, "quota_budget": 40.0, "spent_cost": 0.0,
        "est_cost": 1.0, "created_at": 1.0, "updated_at": 1.0,
    }


def test_three_projects_render_at_the_same_time(store, monkeypatch):
    """三个项目必须真的同时在跑 —— 用栅栏逼出并发。"""
    monkeypatch.setattr(config, "PIPELINE_CONCURRENCY", 3)
    for i in range(3):
        store.create_project(_project(f"p_{i}"))

    guard = threading.Lock()
    active = {"now": 0, "max": 0}
    per_pid: dict[str, int] = {}
    barrier = threading.Barrier(3, timeout=15)

    def fake_advance(pid, **kwargs):
        with guard:
            active["now"] += 1
            active["max"] = max(active["max"], active["now"])
            per_pid[pid] = per_pid.get(pid, 0) + 1
            assert per_pid[pid] == 1, "同一个项目不该被两个 worker 同时推进"
        try:
            # 三个都进来才放行 —— 并发度不足的话这里会超时
            barrier.wait()
        except threading.BrokenBarrierError:
            pass
        time.sleep(0.05)
        with guard:
            active["now"] -= 1
            per_pid[pid] -= 1
        return {"state": "DRAFT_RENDERING", "waiting": False}

    monkeypatch.setattr(main.orchestrator, "advance", fake_advance)

    async def scenario() -> int:
        main.scheduler.start()
        try:
            for _ in range(200):
                if active["max"] >= 3:
                    break
                await asyncio.sleep(0.02)
            return active["max"]
        finally:
            await main.scheduler.stop()

    peak = asyncio.run(scenario())
    assert peak == 3, f"三个项目应当同时在跑,实际并发峰值 {peak}"


def test_concurrency_never_exceeds_the_limit(store, monkeypatch):
    """并发不得超过 PIPELINE_CONCURRENCY —— 内存是硬约束。"""
    monkeypatch.setattr(config, "PIPELINE_CONCURRENCY", 2)
    for i in range(4):
        store.create_project(_project(f"p_{i}"))

    guard = threading.Lock()
    active = {"now": 0, "max": 0}

    def fake_advance(pid, **kwargs):
        with guard:
            active["now"] += 1
            active["max"] = max(active["max"], active["now"])
        time.sleep(0.15)
        with guard:
            active["now"] -= 1
        return {"state": "DRAFT_RENDERING", "waiting": False}

    monkeypatch.setattr(main.orchestrator, "advance", fake_advance)

    async def scenario() -> int:
        main.scheduler.start()
        try:
            await asyncio.sleep(1.2)
            return active["max"]
        finally:
            await main.scheduler.stop()

    peak = asyncio.run(scenario())
    assert peak <= 2, f"并发峰值 {peak} 超过了上限 2"


def test_same_project_is_never_advanced_twice_at_once(store, monkeypatch):
    """同一个项目在队列里只出现一次,也不会被两个 worker 同时拿到。"""
    monkeypatch.setattr(config, "PIPELINE_CONCURRENCY", 4)
    store.create_project(_project("p_one"))

    guard = threading.Lock()
    per_pid: dict[str, int] = {}
    overlaps: list[str] = []

    def fake_advance(pid, **kwargs):
        with guard:
            per_pid[pid] = per_pid.get(pid, 0) + 1
            if per_pid[pid] > 1:
                overlaps.append(pid)
        time.sleep(0.2)
        with guard:
            per_pid[pid] -= 1
        return {"state": "DRAFT_RENDERING", "waiting": False}

    monkeypatch.setattr(main.orchestrator, "advance", fake_advance)

    async def scenario() -> None:
        main.scheduler.start()
        try:
            await asyncio.sleep(0.8)
        finally:
            await main.scheduler.stop()

    asyncio.run(scenario())
    assert not overlaps, f"同一项目被同时推进了:{overlaps}"


def test_scan_enqueues_deduped_projects(store, monkeypatch):
    """扫描按 id 去重 —— 同一个项目不会在队列里排两次。"""
    store.create_project(_project("p_a"))
    store.create_project(_project("p_b"))
    store.create_project(_project("p_c"))

    async def scenario() -> list[str]:
        main.scheduler.start()
        try:
            await asyncio.sleep(0.1)
            return sorted(main.scheduler._queued)
        finally:
            await main.scheduler.stop()

    queued = asyncio.run(scenario())
    assert set(queued) <= {"p_a", "p_b", "p_c"}
    assert len(queued) == len(set(queued)), "队列里不该有重复"


def test_still_auto_projects_are_requeued(store, monkeypatch):
    """渲完 4 拍还在渲染态 → 重新入队,排到队尾给别的项目让位。"""
    store.create_project(_project("p_again"))
    seen: list[str] = []

    def fake_advance(pid, **kwargs):
        seen.append(pid)
        return {"state": "DRAFT_RENDERING", "waiting": False}

    monkeypatch.setattr(main.orchestrator, "advance", fake_advance)

    async def scenario() -> None:
        main.scheduler.start()
        try:
            await asyncio.sleep(0.5)
        finally:
            await main.scheduler.stop()

    asyncio.run(scenario())
    assert len(seen) > 1, "还在自动态的项目应当被反复推进"
