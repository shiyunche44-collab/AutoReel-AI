"""项目级互斥:同一时刻,一个项目只有一个写入者。

流水线的写操作会从两个方向到达 —— 后台调度器推进自动态项目,以及
用户点「确认门禁」「换背景重拍」「保存脚本」。两边在不同线程里跑,
之前没有任何保护,于是"重拍被在途渲染静默吞掉"这类问题反复出现。
"""
from __future__ import annotations

import threading
import time

import pytest

from app.pipeline import locks


def test_only_one_holder_at_a_time():
    """两个线程抢同一个项目,任一时刻临界区里只应有一个。"""
    inside = 0
    max_inside = 0
    guard = threading.Lock()

    def worker() -> None:
        nonlocal inside, max_inside
        with locks.project_lock("p1") as got:
            if not got:
                return
            with guard:
                inside += 1
                max_inside = max(max_inside, inside)
            time.sleep(0.05)
            with guard:
                inside -= 1

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert max_inside == 1


def test_different_projects_do_not_block_each_other():
    """锁是按项目的:A 项目忙,不影响 B 项目。"""
    with locks.project_lock("p1") as got1:
        assert got1
        with locks.project_lock("p2") as got2:
            assert got2, "别的项目不该被挡住"


def test_lock_is_not_reentrant():
    """同一线程再拿一次拿不到。

    这正是 `confirm_gate` 不能自己加锁的原因:它调用的 `advance()` 内部
    已经加了锁,再拿一次会自己把自己锁死(项目永远推不动)。
    """
    with locks.project_lock("p1") as got:
        assert got
        with locks.project_lock("p1") as again:
            assert not again


def test_require_project_lock_raises_when_busy():
    with locks.project_lock("p1"):
        with pytest.raises(locks.ProjectBusy):
            with locks.require_project_lock("p1"):
                pass


def test_lock_entry_is_cleaned_up():
    """没有持有者时要把锁条目清掉,否则项目数一多字典会无限增长。"""
    with locks.project_lock("p1"):
        assert "p1" in locks._locks
    assert "p1" not in locks._locks
    assert "p1" not in locks._refs


def test_advance_takes_the_project_lock(monkeypatch):
    """**回归**:两个并发 advance 只能有一个进临界区。

    之前 `advance` 完全裸奔,后台调度器与用户手动推进会同时判定
    "state == DRAFT_RENDERING",于是同时开渲同一批拍、写到同一个
    固定输出路径,可能产出半截 mp4。
    """
    from app.pipeline import orchestrator

    entered = threading.Event()
    release = threading.Event()
    calls: list[str] = []

    def slow_advance(project_id: str, **kwargs):
        calls.append(project_id)
        entered.set()
        release.wait(5)
        return {"state": "DRAFT_RENDERING", "waiting": False}

    monkeypatch.setattr(orchestrator, "_advance", slow_advance)

    t = threading.Thread(target=lambda: orchestrator.advance("p_lock"))
    t.start()
    try:
        assert entered.wait(5), "第一个 advance 应当进入临界区"
        out = orchestrator.advance("p_lock")
        assert out.get("busy") is True, "第二个 advance 必须让出,不能同时进"
    finally:
        release.set()
        t.join(5)
    assert len(calls) == 1, "临界区只应被执行过一次"


def test_busy_result_shape():
    """让出时返回的字典要带 busy 标记,路由层据此转 409。"""
    from app.pipeline import orchestrator

    with locks.project_lock("p_busy"):
        out = orchestrator.advance("p_busy")
    assert out.get("busy") is True
    assert out.get("waiting") is False
