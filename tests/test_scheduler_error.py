"""失败要分级,不能一律判死。

之前调度器的兜底是 `except Exception: pass` —— 什么都吞掉,项目就
不再推进,界面上永远显示 pending,**没人知道发生过什么**;而另一处
兜底是一失败就 FAILED,网络抖一下就把一个已经花了钱的前置阶段判死。

现在:

- `PipelineError`(配置错、lint 没过)—— 再跑一百遍也是这个结果,判死;
- 其它异常 —— 瞬时失败,留在原状态,退避后重试,连着失败到上限才判死。
"""
from __future__ import annotations

import asyncio
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


def _project(pid: str = "p_fail", state: str = "DRAFT_RENDERING") -> dict:
    return {
        "id": pid, "title": "失败分级测试", "subject": "测试车", "category": "auto_long",
        "theme": "buyguide", "angle": "", "subjects": [], "duration": 240,
        "shot_count": 1, "narration_mode": "tts", "state": state,
        "state_reason": None, "quota_budget": 40.0, "spent_cost": 0.0,
        "est_cost": 1.0, "created_at": 1.0, "updated_at": 1.0,
    }


def _sweep() -> None:
    """推进一次(扫描与 worker 分离之后,真正跑 advance 的是 worker)。"""
    asyncio.run(main.scheduler._advance_one("p_fail"))


def test_transient_failure_keeps_state_and_records_event(store, monkeypatch):
    """瞬时失败:留在原状态 + 记事件 + 退避。"""
    store.create_project(_project())

    def boom(pid, **kwargs):
        raise RuntimeError("网络抖了一下")

    monkeypatch.setattr(main.orchestrator, "advance", boom)
    _sweep()

    row = store.get_project("p_fail")
    assert row["state"] == "DRAFT_RENDERING", "瞬时失败不该把项目判死"
    reasons = [e["reason"] for e in store.get_events("p_fail")]
    assert any("推进失败" in r for r in reasons), f"应留下失败事件,实际:{reasons}"


def test_permanent_failure_marks_failed(store, monkeypatch):
    """PipelineError:不可恢复,直接判死。"""
    store.create_project(_project())

    def boom(pid, **kwargs):
        raise main.orchestrator.PipelineError("lint 还有 3 处必须改")

    monkeypatch.setattr(main.orchestrator, "advance", boom)
    _sweep()

    row = store.get_project("p_fail")
    assert row["state"] == "FAILED"
    assert "lint" in (row["state_reason"] or "")


def test_repeated_transient_failures_eventually_mark_failed(store, monkeypatch):
    """连着瞬时失败到上限,才升级为 FAILED。"""
    store.create_project(_project())

    def boom(pid, **kwargs):
        raise RuntimeError("一直抖")

    monkeypatch.setattr(main.orchestrator, "advance", boom)
    for _ in range(main.MAX_CONSECUTIVE_FAILURES):
        # 清掉退避窗口 = 模拟"过了足够久,调度器又轮到它了"。
        # 退避窗口本身是防止短时间内疯狂重试的,不该挡住失败计数的累积。
        main.scheduler._retry_after.pop("p_fail", None)
        _sweep()

    row = store.get_project("p_fail")
    assert row["state"] == "FAILED", "连续失败到上限应判死"
    assert "连续" in (row["state_reason"] or "")


def test_backoff_window_blocks_requeue(store, monkeypatch):
    """退避窗口内不该把这个项目排进队列。"""
    store.create_project(_project())
    calls = {"n": 0}

    def boom(pid, **kwargs):
        calls["n"] += 1
        raise RuntimeError("抖")

    monkeypatch.setattr(main.orchestrator, "advance", boom)
    _sweep()  # 推进一次 → 失败,进退避窗口
    assert calls["n"] == 1
    assert main.scheduler._retry_after.get("p_fail", 0) > time.monotonic()

    # 扫描:还在退避窗口里,不该排进队列
    main.scheduler._queue = asyncio.Queue()
    main.scheduler._queued.clear()
    main.scheduler._scan()
    assert "p_fail" not in main.scheduler._queued

    # 退避过去之后就该排进队列
    main.scheduler._retry_after.pop("p_fail", None)
    main.scheduler._scan()
    assert "p_fail" in main.scheduler._queued


def test_success_clears_failure_counters(store, monkeypatch):
    """成功一次就把失败计数清掉。"""
    store.create_project(_project())
    state = {"fail": True}

    def maybe(pid, **kwargs):
        if state["fail"]:
            state["fail"] = False
            raise RuntimeError("抖")
        return {"state": "DRAFT_RENDERING", "waiting": False}

    monkeypatch.setattr(main.orchestrator, "advance", maybe)
    _sweep()
    assert main.scheduler._failures.get("p_fail") == 1
    main.scheduler._retry_after.pop("p_fail", None)  # 跳过退避窗口
    _sweep()
    assert "p_fail" not in main.scheduler._failures
