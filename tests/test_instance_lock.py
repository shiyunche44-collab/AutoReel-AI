"""一个库只让一个进程跑后台循环。

项目锁是进程内的,拦不住第二个进程;两个服务连同一个库各起一个调度器,
会推进同一个项目、写坏成片。这里用真的第二个进程来验证文件锁。
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from app import config, instance_lock

ROOT = Path(__file__).resolve().parents[1]

#: 在另一个进程里对同一个数据目录抢锁,打印结果
_CHILD = textwrap.dedent("""
    import sys
    from pathlib import Path
    from app import config, instance_lock
    config.DATA_DIR = Path(sys.argv[1])
    config.PROJECTS_DIR = config.DATA_DIR / "projects"
    got = instance_lock.acquire()
    print("GOT" if got else "BUSY", flush=True)
    if got and len(sys.argv) > 2:
        sys.stdin.read()  # 拿着锁,等父进程关掉 stdin
""")


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "data" / "projects")
    instance_lock.release()
    yield tmp_path / "data"
    instance_lock.release()


def _child(data_dir: Path, *extra: str, **kwargs) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-X", "utf8", "-c", _CHILD, str(data_dir), *extra],
        cwd=ROOT, stdout=subprocess.PIPE, stdin=subprocess.PIPE, text=True,
        env={**os.environ, "TEAM_ENABLED": "0"}, **kwargs)


def test_second_process_cannot_take_the_lock(data_dir):
    assert instance_lock.acquire() is True
    assert instance_lock.owner()["pid"] == os.getpid()
    child = _child(data_dir)
    out, _ = child.communicate(timeout=60)
    assert out.strip() == "BUSY"


def test_acquire_is_idempotent_in_one_process(data_dir):
    assert instance_lock.acquire() is True
    assert instance_lock.acquire() is True
    assert instance_lock.held()


def test_lock_is_free_again_after_release(data_dir):
    assert instance_lock.acquire() is True
    instance_lock.release()
    assert not instance_lock.held()
    out, _ = _child(data_dir).communicate(timeout=60)
    assert out.strip() == "GOT"


def test_lock_comes_back_when_the_holder_dies(data_dir):
    """持锁进程被强杀(没机会放锁):系统收回,下一个进程马上能拿到,不留残锁。"""
    holder = _child(data_dir, "hold")
    try:
        assert holder.stdout.readline().strip() == "GOT"
        assert instance_lock.acquire() is False, "别的进程拿着锁时不该抢到"
    finally:
        holder.kill()
        holder.wait(timeout=30)
    assert instance_lock.acquire() is True


def test_standby_server_starts_no_background_loops(data_dir, monkeypatch):
    """抢不到锁的服务只开 Web:调度器、发布、指标、团队一个都不起。"""
    from fastapi.testclient import TestClient

    from app import db, main
    from app.metrics import worker as metrics_worker
    from app.publish import service as publish_service
    from app.team import scheduler as team_scheduler

    monkeypatch.setattr(db, "_INITIALISED", False)
    started: list[str] = []
    monkeypatch.setattr(main.scheduler, "start", lambda: started.append("scheduler"))
    monkeypatch.setattr(publish_service, "start", lambda: started.append("publish"))
    monkeypatch.setattr(metrics_worker, "start", lambda: started.append("metrics"))
    monkeypatch.setattr(team_scheduler, "start", lambda: started.append("team"))
    monkeypatch.setattr(main.hang_watch, "start", lambda *a, **k: None)
    monkeypatch.setattr(main.logging_setup, "setup", lambda: None)
    monkeypatch.setattr(main.maintenance, "prune_temp", lambda: None)
    monkeypatch.setattr(main.maintenance, "prune_caches", lambda: {"removed": 0})
    saved = dict(main.HEALTH)

    holder = _child(data_dir, "hold")
    try:
        assert holder.stdout.readline().strip() == "GOT"
        with TestClient(main.app) as client:
            assert started == []
            health = client.get("/api/health").json()
            assert health["background"]["ok"] is False
            assert health["background"]["owner"]["pid"] == holder.pid
            assert "只读服务" in client.get("/").text
    finally:
        holder.kill()
        holder.wait(timeout=30)
        main.HEALTH.clear()
        main.HEALTH.update(saved)
        db._INITIALISED = False
