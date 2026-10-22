"""调度器不能占住事件循环。

**回归**:调度器原来在 `async def sweep()` 里直接调用**同步**的
`orchestrator.advance()`。advance 会跑 ffmpeg 子进程、TTS 与 LLM 的
HTTP 调用 —— 整段跑在事件循环线程上,渲染期间(一次 tick 约 50 秒,
合成可能几分钟)**整个 Web 服务冻结**:健康检查无响应、状态轮询卡住、
页面打不开。

现在 advance 丢进专用线程池,事件循环只负责调度。
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
    # 调度器是全局单例,失败计数与退避窗口也得清 —— 否则上一个用例
    # 留下的退避窗口会让下一个用例的项目被直接跳过。
    main.scheduler._failures.clear()
    main.scheduler._retry_after.clear()
    yield db
    db._INITIALISED = False


def _project(pid: str = "p_sched", state: str = "DRAFT_RENDERING") -> dict:
    return {
        "id": pid, "title": "调度测试", "subject": "测试车", "category": "auto_long",
        "theme": "buyguide", "angle": "", "subjects": [], "duration": 240,
        "shot_count": 1, "narration_mode": "tts", "state": state,
        "state_reason": None, "quota_budget": 40.0, "spent_cost": 0.0,
        "est_cost": 1.0, "created_at": 1.0, "updated_at": 1.0,
    }


def test_event_loop_stays_responsive_during_render(store, monkeypatch):
    """渲染在跑的时候,事件循环必须还能调度别的协程。

    这是 P0-1 的直接回归测试:旧实现里 sweep 会把整个事件循环占住,
    下面这个 `asyncio.sleep(0.05)` 会被拖到 advance 结束(2 秒后)。
    """
    store.create_project(_project())
    entered = threading.Event()

    def slow_advance(pid, **kwargs):
        entered.set()
        time.sleep(2.0)
        return {"state": "DRAFT_RENDERING", "waiting": False}

    monkeypatch.setattr(main.orchestrator, "advance", slow_advance)

    async def scenario() -> float:
        task = asyncio.create_task(main.scheduler.sweep())
        # 等 advance 真的进去了
        for _ in range(100):
            if entered.is_set():
                break
            await asyncio.sleep(0.02)
        assert entered.is_set(), "调度器应当已经进入 advance"
        t0 = time.monotonic()
        await asyncio.sleep(0.05)
        dt = time.monotonic() - t0
        await task
        return dt

    dt = asyncio.run(scenario())
    assert dt < 1.0, f"事件循环被占住了:一个 0.05 秒的 sleep 花了 {dt:.2f} 秒"


def test_health_endpoint_answers_while_rendering(store, monkeypatch):
    """渲染期间 /api/health 必须秒回。

    刻意**不**用 `with TestClient(...)`:那会跑 lifespan,里面
    `scheduler.stop()` 会把推进用的线程池关掉,后面的用例就废了。
    """
    from fastapi.testclient import TestClient

    store.create_project(_project())
    entered = threading.Event()

    def slow_advance(pid, **kwargs):
        entered.set()
        time.sleep(2.0)
        return {"state": "DRAFT_RENDERING", "waiting": False}

    monkeypatch.setattr(main.orchestrator, "advance", slow_advance)

    client = TestClient(main.app)
    async def scenario() -> float:
        task = asyncio.create_task(main.scheduler.sweep())
        for _ in range(100):
            if entered.is_set():
                break
            await asyncio.sleep(0.02)
        t0 = time.monotonic()
        resp = await asyncio.to_thread(client.get, "/api/health")
        # 只量响应本身:后面 `await task` 还要等那 2 秒的渲染结束,
        # 算进来就成了"整个测试花了 2 秒"。
        dt = time.monotonic() - t0
        assert resp.status_code == 200
        await task
        return dt

    dt = asyncio.run(scenario())
    assert dt < 1.5, f"渲染期间健康检查响应过慢:{dt:.2f} 秒"


def test_health_exposes_provider_call_stats(store):
    from fastapi.testclient import TestClient

    client = TestClient(main.app)
    resp = client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert "provider_calls" in body, "健康检查应暴露外部调用统计"
    assert "scheduler" in body
    assert body["scheduler"]["concurrency"] == config.PIPELINE_CONCURRENCY
    assert "disk" in body, "健康检查应暴露磁盘水位"
    assert body["disk"]["data_bytes"] >= 0
