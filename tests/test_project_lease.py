"""项目租约:项目写锁跨进程也成立(`pipeline/locks.py` 的第二层)。

"另一个进程持有"用库里一条别人的租约行模拟:进程内锁看不见它,只有租约能挡住。
"""
from __future__ import annotations

import time

import pytest

from app import db
from app.pipeline import locks


@pytest.fixture()
def store():
    db.init_db()
    yield db
    locks.release_all()


def _other_process_holds(pid: str, ttl: float = 60.0) -> None:
    assert db.try_acquire_lease(pid, "other-host:999:1:1:1", ttl)


def test_lease_blocks_a_project_held_by_another_process(store):
    _other_process_holds("p1")
    with locks.project_lock("p1") as got:
        assert got is False
    with pytest.raises(locks.ProjectBusy):
        with locks.require_project_lock("p1"):
            pass
    with locks.project_lock("p2") as got:  # 别的项目不受影响
        assert got is True


def test_lease_is_taken_and_released(store):
    with locks.project_lock("p1") as got:
        assert got
        assert locks.busy_project_ids() == ["p1"]
        rows = db.active_leases()
        assert [r["project_id"] for r in rows] == ["p1"]
        assert rows[0]["owner"].startswith(locks._PROCESS)
        assert not db.try_acquire_lease("p1", "someone-else", 60)  # 别的进程拿不到
    assert db.active_leases() == []
    assert locks.busy_project_ids() == []


def test_expired_lease_of_a_dead_process_is_taken_over(store):
    _other_process_holds("p1", ttl=-1)  # 已过期 = 持有者早死了
    with locks.project_lock("p1") as got:
        assert got


def test_keeper_renews_held_leases(store, monkeypatch):
    monkeypatch.setattr(locks, "LEASE_TTL", 0.3)
    with locks.project_lock("p1") as got:
        assert got
        time.sleep(0.8)  # 超过 TTL 两倍多:没续租的话早过期了
        assert not db.try_acquire_lease("p1", "someone-else", 60)


def test_release_all_frees_leases_on_shutdown(store):
    with locks.project_lock("p1") as got:
        assert got
        locks.release_all()
        assert db.active_leases() == []
    # 退出临界区时再放一次(owner 对不上就是空操作)不报错
    assert db.active_leases() == []


def test_db_error_counts_as_busy(store, monkeypatch):
    """拿不准就当忙:宁可这一轮不做,也别两路一起写。"""
    def boom(*a, **k):
        raise RuntimeError("database is locked")

    with monkeypatch.context() as m:  # 只撤这一个桩;整体 undo 会连临时数据目录一起撤掉
        m.setattr(db, "try_acquire_lease", boom)
        with locks.project_lock("p1") as got:
            assert got is False
        with locks.project_lock("p1") as got:  # 进程内锁也被放掉了,没有残留
            assert got is False
    with locks.project_lock("p1") as got:
        assert got
