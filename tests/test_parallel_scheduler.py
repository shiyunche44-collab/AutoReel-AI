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


# --------------------------------------------------------------------------
# 备料通道:门禁 A 之前的状态也归调度器
# --------------------------------------------------------------------------
def test_prep_states_are_picked_up_after_a_restart(store, monkeypatch):
    """服务重启后,停在事实采集 / 写稿 / 分镜的项目要被扫描接手。

    以前这几步靠建项目时起的一次性线程,重启后项目永远停在原地,没有任何报错。
    """
    store.create_project(_project("p_prep", state="DRAFT_INTENT"))
    order = ["FACTS_RESOLVED", "SCRIPT_READY", "STORYBOARD_READY", "GATE_A"]
    seen: list[str] = []

    def fake_advance(pid, **kwargs):
        seen.append(threading.current_thread().name)
        nxt = order[len(seen) - 1]
        store.update_project(pid, state=nxt)
        return {"state": nxt, "waiting": nxt == "GATE_A"}

    monkeypatch.setattr(main.orchestrator, "advance", fake_advance)

    async def scenario() -> None:
        main.scheduler.start()
        try:
            for _ in range(200):
                if len(seen) >= len(order):
                    break
                await asyncio.sleep(0.02)
            await asyncio.sleep(0.1)  # 到了门禁就该停手
        finally:
            await main.scheduler.stop()

    asyncio.run(scenario())
    assert store.get_project("p_prep")["state"] == "GATE_A"
    assert len(seen) == len(order), "到门禁 A 之后不该再被推进"
    assert all(name.startswith("pipeline-prep") for name in seen), seen


def test_prep_does_not_wait_behind_a_render(store, monkeypatch):
    """渲染名额只有 1 个、正被占着时,另一个项目的写稿照样能跑。"""
    monkeypatch.setattr(config, "PIPELINE_CONCURRENCY", 1)
    store.create_project(_project("p_render"))
    store.create_project(_project("p_prep", state="FACTS_RESOLVED"))
    release = threading.Event()
    prep_ran = threading.Event()

    def fake_advance(pid, **kwargs):
        if pid == "p_render":
            release.wait(timeout=10)
            return {"state": "DRAFT_RENDERING", "waiting": False}
        store.update_project(pid, state="GATE_A")
        prep_ran.set()
        return {"state": "GATE_A", "waiting": True}

    monkeypatch.setattr(main.orchestrator, "advance", fake_advance)

    async def scenario() -> bool:
        main.scheduler.start()
        try:
            for _ in range(200):
                if prep_ran.is_set():
                    break
                await asyncio.sleep(0.02)
            return prep_ran.is_set()
        finally:
            release.set()
            await main.scheduler.stop()

    assert asyncio.run(scenario()), "写稿被渲染堵住了"


def test_render_queued_in_prep_lane_is_rerouted(store, monkeypatch):
    """排在备料队列里的项目若已进了渲染态,不能在备料池里渲(会绕过渲染并发上限)。"""
    store.create_project(_project("p_moved", state="ASSETS_READY"))
    threads: list[str] = []

    def fake_advance(pid, **kwargs):
        threads.append(threading.current_thread().name)
        store.update_project(pid, state="GATE_B")
        return {"state": "GATE_B", "waiting": True}

    monkeypatch.setattr(main.orchestrator, "advance", fake_advance)

    async def scenario() -> None:
        sched = main.scheduler
        sched.start()
        try:
            # 入队时还是备料态,轮到它之前被人手动推进到了渲染态
            sched._queued.discard("p_moved")
            while not sched._prep_queue.empty():
                sched._prep_queue.get_nowait()
            sched._queued.add("p_moved")
            store.update_project("p_moved", state="DRAFT_RENDERING")
            sched._prep_queue.put_nowait("p_moved")
            for _ in range(200):
                if threads:
                    break
                await asyncio.sleep(0.02)
        finally:
            await sched.stop()

    asyncio.run(scenario())
    assert threads and not threads[0].startswith("pipeline-prep"), threads


def test_kick_enqueues_from_another_thread(store, monkeypatch):
    """建项目后 kick 一下就开始,不用等 20 秒一轮的扫描。"""
    import importlib

    monkeypatch.setattr(importlib.import_module("app.scheduler"), "SCHEDULER_INTERVAL", 3600)
    done = threading.Event()

    def fake_advance(pid, **kwargs):
        store.update_project(pid, state="GATE_A")
        done.set()
        return {"state": "GATE_A", "waiting": True}

    monkeypatch.setattr(main.orchestrator, "advance", fake_advance)

    async def scenario() -> bool:
        main.scheduler.start()
        try:
            await asyncio.sleep(0.1)  # 第一轮扫描已过,此时还没有项目
            store.create_project(_project("p_new", state="DRAFT_INTENT"))
            t = threading.Thread(target=main.scheduler.kick, args=("p_new",))
            t.start()
            for _ in range(200):
                if done.is_set():
                    break
                await asyncio.sleep(0.02)
            t.join()
            return done.is_set()
        finally:
            await main.scheduler.stop()

    assert asyncio.run(scenario()), "kick 之后项目没有被推进"


def test_kick_is_a_noop_when_scheduler_is_not_running(store):
    """调度器没在跑(测试、离线脚本):不报错,项目留在原状态等服务接手。"""
    store.create_project(_project("p_idle", state="DRAFT_INTENT"))
    main.scheduler.kick("p_idle")
    assert store.get_project("p_idle")["state"] == "DRAFT_INTENT"
    assert "p_idle" not in main.scheduler._queued
