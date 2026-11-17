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


def _skip_backoff(pid: str = "p_fail") -> None:
    """跳过退避窗口 = 模拟"过了足够久,调度器又轮到它了"(失败次数保留)。"""
    n = db.get_project(pid)["advance_failures"]
    db.set_advance_failures(pid, n, 0.0)


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
        _skip_backoff()
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
    assert store.get_project("p_fail")["advance_retry_at"] > time.time()

    # 扫描:还在退避窗口里,不该排进队列
    main.scheduler._queue = asyncio.Queue()
    main.scheduler._queued.clear()
    main.scheduler._scan()
    assert "p_fail" not in main.scheduler._queued

    # 退避过去之后就该排进队列
    _skip_backoff()
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
    assert store.get_project("p_fail")["advance_failures"] == 1
    _skip_backoff()
    _sweep()
    assert store.get_project("p_fail")["advance_failures"] == 0
    assert store.get_project("p_fail")["advance_retry_at"] == 0


def test_state_raced_does_not_fail_a_finished_project(store, monkeypatch):
    """另一路已把项目推到 DONE:迟到的一路 CAS 没抢到,不能把成片标成 FAILED。

    线上出过:p_8649d98ac728 合成完成(DONE)并已发布后,又被这条分支写成 FAILED。"""
    store.create_project(_project(state="COMPOSING"))

    def raced(pid, **kwargs):
        store.update_project(pid, state="DONE")  # 另一路赶在前面合成完了
        raise main.orchestrator.StateRaced("状态已被并发推进")

    monkeypatch.setattr(main.orchestrator, "advance", raced)
    _sweep()
    assert store.get_project("p_fail")["state"] == "DONE"


def test_pipeline_error_never_overwrites_done(store, monkeypatch):
    store.create_project(_project(state="COMPOSING"))

    def boom(pid, **kwargs):
        store.update_project(pid, state="DONE")
        raise main.orchestrator.PipelineError("别的原因")

    monkeypatch.setattr(main.orchestrator, "advance", boom)
    _sweep()
    assert store.get_project("p_fail")["state"] == "DONE"


def test_pipeline_error_fails_from_current_state(store, monkeypatch):
    """这一轮推进了几步才出错:按现在的状态判死,不是按开头读到的。"""
    store.create_project(_project(state="DRAFT_RENDERING"))

    def boom(pid, **kwargs):
        store.update_project(pid, state="COMPOSING")
        raise main.orchestrator.PipelineError("拼接失败")

    monkeypatch.setattr(main.orchestrator, "advance", boom)
    _sweep()
    assert store.get_project("p_fail")["state"] == "FAILED"


def test_failure_counters_survive_a_restart(store, monkeypatch):
    """失败次数和退避窗口落库:换一个调度器实例(= 服务重启)也还在。"""
    store.create_project(_project())

    def boom(pid, **kwargs):
        raise RuntimeError("抖")

    monkeypatch.setattr(main.orchestrator, "advance", boom)
    _sweep()
    _skip_backoff()
    _sweep()
    fresh = main.Scheduler()
    fresh._queue = asyncio.Queue()
    fresh._scan()
    assert "p_fail" not in fresh._queued, "重启后仍在退避窗口里"
    assert store.get_project("p_fail")["advance_failures"] == 2


def test_state_change_resets_failure_counter(store, monkeypatch):
    """人把 FAILED 项目点重试后,计数从零算:不能一次抖动就直接再判死。"""
    store.create_project(_project())
    store.set_advance_failures("p_fail", main.MAX_CONSECUTIVE_FAILURES - 1, 0.0)
    assert store.cas_state("p_fail", "DRAFT_RENDERING", "FAILED")
    assert store.cas_state("p_fail", "FAILED", "DRAFT_RENDERING")
    row = store.get_project("p_fail")
    assert row["advance_failures"] == 0 and row["advance_retry_at"] == 0
