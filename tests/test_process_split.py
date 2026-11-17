"""Web / worker 拆成两个进程(`app/background.py`、`app/worker.py`)。

两个进程只通过库和几个小文件交流:心跳文件、停机请求文件、`scheduler_kicks` 表、
`jobs` 表。这里守住这些"接口"。真起两个进程的冒烟测试太慢,不放进单测。
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from app import background, db


def test_role_comes_from_env(monkeypatch):
    monkeypatch.delenv("AUTOREEL_ROLE", raising=False)
    assert background.role() == "all" and background.runs_background()
    monkeypatch.setenv("AUTOREEL_ROLE", "web")
    assert background.role() == "web" and not background.runs_background()
    monkeypatch.setenv("AUTOREEL_ROLE", "worker")
    assert background.runs_background()
    monkeypatch.setenv("AUTOREEL_ROLE", "乱写")
    assert background.role() == "all"


def test_web_role_reports_worker_liveness_from_heartbeat(monkeypatch):
    from app import config

    config.ensure_dirs()
    monkeypatch.setenv("AUTOREEL_ROLE", "web")
    assert background.status()["ok"] is False          # 没有心跳文件:worker 不在
    background.write_heartbeat()
    st = background.status()
    assert st["ok"] is True and st["mode"] == "web" and st["worker"]["age"] < 5
    background.heartbeat_path().write_text(f"1 {time.time() - 600:.0f}", encoding="ascii")
    assert background.status()["ok"] is False          # 心跳停了 10 分钟:卡死或死了


def test_web_banner_says_worker_is_missing(monkeypatch):
    from fastapi.testclient import TestClient

    from app import main

    monkeypatch.setenv("AUTOREEL_ROLE", "web")
    monkeypatch.setattr(main.hang_watch, "start", lambda *a, **k: None)
    monkeypatch.setattr(main.logging_setup, "setup", lambda: None)
    started: list[str] = []
    monkeypatch.setattr(main.scheduler, "start", lambda: started.append("scheduler"))
    saved = dict(main.HEALTH)
    try:
        with TestClient(main.app) as client:     # 跑 lifespan:web 角色不起后台循环
            assert started == []
            assert client.get("/api/health").json()["role"] == "web"
            assert "后台进程没在跑" in client.get("/").text
            background.write_heartbeat()
            html = client.get("/").text            # 中间件每个请求前刷新
            assert "后台进程没在跑" not in html and "只读服务" not in html
            assert client.get("/api/health").json()["background"]["ok"] is True
    finally:
        main.HEALTH.clear()
        main.HEALTH.update(saved)


def test_kick_without_local_scheduler_goes_through_db():
    from app.scheduler import Scheduler

    db.init_db()
    s = Scheduler()                       # 没 start:相当于 Web 进程
    s.kick("p_1")
    s.kick("p_2")
    s.kick("p_1")                         # 重复催只记一行
    assert sorted(db.pop_kicks()) == ["p_1", "p_2"]
    assert db.pop_kicks() == []


def test_running_scheduler_picks_up_db_kicks(monkeypatch):
    """worker 里的调度器每秒取走别的进程留下的 kick,不用等 20 秒一轮的扫描。"""
    from app import scheduler as sched_mod

    db.init_db()
    monkeypatch.setattr(sched_mod, "KICK_POLL_SECONDS", 0.05)
    monkeypatch.setattr(sched_mod, "SCHEDULER_INTERVAL", 3600)
    s = sched_mod.Scheduler()
    seen: list[str] = []
    monkeypatch.setattr(s, "_kick", seen.append)

    async def scenario() -> None:
        s._loop = asyncio.get_running_loop()
        s._queue = asyncio.Queue()
        task = asyncio.create_task(s._run())
        await asyncio.sleep(0.1)
        db.request_kick("p_web")          # "Web 进程"建了个项目
        for _ in range(50):
            if seen:
                break
            await asyncio.sleep(0.02)
        s._stop.set()
        await task

    asyncio.run(scenario())
    assert seen == ["p_web"]


def test_stop_request_file_stops_worker_loop():
    from app import config

    config.ensure_dirs()

    async def scenario() -> float:
        stop = asyncio.Event()
        task = asyncio.create_task(background.watch_stop_request(stop, poll=0.02))
        await asyncio.sleep(0.05)
        assert not stop.is_set()
        background.stop_request_path().write_text("restart", encoding="ascii")
        t0 = time.monotonic()
        await asyncio.wait_for(stop.wait(), timeout=2)
        await task
        return time.monotonic() - t0

    assert asyncio.run(scenario()) < 1.0
    assert not background.stop_request_path().exists()   # 取走即删,不会挡下次启动


def test_worker_exits_when_lock_is_taken(monkeypatch):
    from app import worker

    monkeypatch.setattr(worker.hang_watch, "start", lambda *a, **k: None)
    monkeypatch.setattr(background, "start", lambda: False)
    assert asyncio.run(worker._serve(asyncio.Event())) == worker.EXIT_LOCKED


def test_manual_team_run_is_a_job_not_a_thread():
    """手动跑角色交给 worker(持久化任务),Web 进程里不再起线程。"""
    from fastapi.testclient import TestClient

    from app import jobs, team
    from app.main import app

    client = TestClient(app)
    assert client.post("/api/team/roles/scout/run").json() == {"ok": True}
    assert client.post("/api/team/roles/scout/run").json() == {"ok": True}   # 去重,不报错
    assert jobs.active(team.ROLE_JOB, "scout")
    assert client.post("/api/team/roles/nope/run").status_code == 404


@pytest.mark.parametrize("path", sorted(Path("scripts").glob("*.ps1")))
def test_powershell_scripts_are_ascii(path):
    """PowerShell 5.1 按 ANSI 读 .ps1,非 ASCII 字符会把解析器搞坏(CLAUDE.md)。"""
    data = path.read_bytes()
    bad = [i for i, c in enumerate(data) if c > 127]
    assert not bad, f"{path} 第 {data[:bad[0]].count(b'\n') + 1} 行有非 ASCII 字符"
