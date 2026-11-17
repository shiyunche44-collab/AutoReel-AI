"""持久化后台任务(`app/jobs.py` + `app/db/jobs.py`)。

要守住的几件事:去重跨进程成立(靠库)、失败退避重试、判死、进程被杀后重启接着跑
且不算一次尝试、建池预留的额度无论成败都结算。
"""
from __future__ import annotations

import threading
import time

import pytest

from app import config, db, jobs
from tests.conftest import wait_until


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "data" / "projects")
    monkeypatch.setattr(db, "_INITIALISED", False)
    db.init_db()
    yield db
    db._INITIALISED = False


@pytest.fixture()
def kinds(monkeypatch):
    """测试自己的任务类型,用完撤掉,不污染全局登记表。"""
    import importlib

    for mod in jobs.HANDLER_MODULES:  # 先把正式 handler 登记全,别让它们登记进副本后随副本丢掉
        importlib.import_module(mod)
    monkeypatch.setattr(jobs, "_KINDS", dict(jobs._KINDS))
    return jobs


# --------------------------------------------------------------------------
# 库层
# --------------------------------------------------------------------------
def test_enqueue_dedupes_active_jobs_by_kind_and_key(store):
    a = store.enqueue_job("k", "x", {"n": 1})
    assert a is not None
    assert store.enqueue_job("k", "x", {"n": 2}) is None   # 排着的同 key:不再排
    assert store.enqueue_job("k", "y") is not None          # 不同 key 不受影响
    assert store.enqueue_job("other", "x") is not None      # 不同 kind 不受影响
    job = store.claim_job("me", ["k"], lease=60)
    assert job["id"] == a and job["payload"] == {"n": 1} and job["attempts"] == 1
    assert store.enqueue_job("k", "x") is None              # 在跑的同 key:也不排
    assert store.finish_job(a, "me", {"ok": True})
    assert store.enqueue_job("k", "x") is not None          # 结束了就能再排


def test_claim_respects_next_run_at_and_lease(store):
    store.enqueue_job("k", "later", run_at=time.time() + 3600)
    assert store.claim_job("me", ["k"], lease=60) is None
    jid = store.enqueue_job("k", "now")
    job = store.claim_job("a", ["k"], lease=60)
    assert job["id"] == jid
    assert store.claim_job("b", ["k"], lease=60) is None    # 租约还在:别人领不走
    with store.connect() as conn:
        conn.execute("UPDATE jobs SET lease_expires_at = 0 WHERE id = ?", (jid,))
    stolen = store.claim_job("b", ["k"], lease=60)          # 租约过期:可以接手
    assert stolen["id"] == jid and stolen["lease_owner"] == "b"
    assert not store.heartbeat_job(jid, "a", 60)            # 原主人续租失败
    assert not store.finish_job(jid, "a")                   # 也不能替新主人收尾


def test_orphans_are_requeued_without_spending_an_attempt(store):
    jid = store.enqueue_job("k", "x", max_attempts=1)
    store.claim_job("dead-process", ["k"], lease=600)
    assert store.requeue_orphan_jobs("new-process") == 1
    job = store.claim_job("new-process", ["k"], lease=60)
    assert job["id"] == jid and job["attempts"] == 1        # 被杀的那次不算


# --------------------------------------------------------------------------
# 执行器
# --------------------------------------------------------------------------
def _runner(monkeypatch) -> jobs.Runner:
    monkeypatch.setattr(jobs, "POLL_SECONDS", 0.05)
    r = jobs.Runner()
    monkeypatch.setattr(jobs, "runner", r)
    return r


def test_runner_runs_and_records_result(store, kinds, monkeypatch):
    kinds.register("t.ok")(lambda p: {"echo": p["v"]})
    r = _runner(monkeypatch)
    r.start()
    try:
        jobs.submit("t.ok", "a", {"v": 7})
        assert wait_until(lambda: (jobs.latest("t.ok", "a") or {}).get("state") == "done")
        assert jobs.latest("t.ok", "a")["result"] == {"echo": 7}
    finally:
        r.stop()


def test_transient_failure_backs_off_then_gives_up(store, kinds, monkeypatch):
    calls: list[int] = []

    def flaky(p):
        calls.append(1)
        raise RuntimeError("网络抖了")

    kinds.register("t.flaky", max_attempts=2)(flaky)
    monkeypatch.setattr(jobs, "RETRY_BASE", 0.0)
    r = _runner(monkeypatch)
    r.start()
    try:
        jobs.submit("t.flaky", "a")
        assert wait_until(lambda: (jobs.latest("t.flaky", "a") or {}).get("state") == "failed")
    finally:
        r.stop()
    job = jobs.latest("t.flaky", "a")
    assert len(calls) == 2 and job["attempts"] == 2 and "网络抖了" in job["error"]


def test_permanent_failure_is_not_retried(store, kinds, monkeypatch):
    calls: list[int] = []

    def bad(p):
        calls.append(1)
        raise jobs.Permanent("参数不对")

    kinds.register("t.bad", max_attempts=5)(bad)
    r = _runner(monkeypatch)
    r.start()
    try:
        jobs.submit("t.bad", "a")
        assert wait_until(lambda: (jobs.latest("t.bad", "a") or {}).get("state") == "failed")
    finally:
        r.stop()
    assert calls == [1]


def test_resource_concurrency_limit(store, kinds, monkeypatch):
    monkeypatch.setitem(jobs.RESOURCES, "t.res", 1)
    gate = threading.Event()
    live: list[int] = []
    peak: list[int] = []

    def slow(p):
        live.append(1)
        peak.append(len(live))
        gate.wait(5)
        live.pop()

    kinds.register("t.slow", resource="t.res")(slow)
    r = _runner(monkeypatch)
    r.start()
    try:
        for k in "abc":
            jobs.submit("t.slow", k)
        assert wait_until(lambda: len(peak) >= 1)
        time.sleep(0.2)
        assert max(peak) == 1
        gate.set()
        assert wait_until(lambda: all((jobs.latest("t.slow", k) or {}).get("state") == "done"
                                      for k in "abc"))
    finally:
        gate.set()
        r.stop()
    assert max(peak) == 1


def test_register_rejects_unknown_resource(kinds):
    with pytest.raises(ValueError):
        kinds.register("t.x", resource="没有这类")


# --------------------------------------------------------------------------
# 素材池:重启后接着建,额度预留建完就结算
# --------------------------------------------------------------------------
def test_library_build_settles_reservation_after_restart(job_runner, monkeypatch):
    from app import costs
    from app.models import ProjectCreate
    from app.pipeline import library, orchestrator
    from app.pipeline.library import builder

    db.init_db()
    pid = orchestrator.create_project(ProjectCreate(subject="小米YU7", category="auto_long",
                                                    duration=240))
    reserved = costs.reserve_project(pid, 1.0)
    before = db.get_project(pid)

    # 模拟"上一个进程领了任务、建到一半被杀":任务停在 running,租约是死进程的
    jid = db.enqueue_job(library.BUILD_JOB, library.slug("小米YU7"), {
        "subject": "小米YU7", "category": "auto_long", "project_id": pid,
        "reserved": reserved, "vlm_before": 0, "opts": {}}, max_attempts=1)
    db.claim_job("dead-process", [library.BUILD_JOB], lease=600)
    assert library.is_building("小米YU7")

    monkeypatch.setattr(builder, "build", lambda subject, **k: {"vlm_calls": 12})
    monkeypatch.setattr(orchestrator, "_llm", lambda: None)
    job_runner.stop()
    job_runner.start()  # "重启":接手孤儿任务
    assert wait_until(lambda: (db.latest_job(library.BUILD_JOB, library.slug("小米YU7"))
                               or {}).get("state") == "done")
    assert not library.is_building("小米YU7")
    after = db.get_project(pid)
    assert db.latest_job(library.BUILD_JOB, library.slug("小米YU7"))["id"] == jid
    # 预留释放、按实际 12 次视觉调用记账
    assert after["reserved_cost"] == pytest.approx(before["reserved_cost"] - reserved)
    assert after["spent_cost"] == pytest.approx(before["spent_cost"]
                                                + costs.estimate_library(12))


def test_build_async_rejects_object_only_options(job_runner):
    from app.pipeline import library

    with pytest.raises(TypeError):
        library.build_async("小米YU7", on_done=lambda s: None)
