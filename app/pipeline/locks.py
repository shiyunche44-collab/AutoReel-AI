"""项目级互斥:同一时刻,一个项目只有一个写入者。

**为什么需要**

流水线的写操作会从两个方向同时到达:

- 后台调度器(`main.Scheduler`)推进处于自动态的项目;
- HTTP 请求 —— 用户点「确认门禁」「换背景重拍」「保存脚本」「手动推进」。

这两边跑在**不同的线程**里,彼此不知道对方存在。之前没有任何保护,
于是有三类竞态:

1. **重拍被吞**:用户在渲染途中点「换背景重拍」,重拍把该拍重置为
   pending,而在途的渲染随后把状态覆盖回 succeeded —— 用户的操作
   静默消失,而且界面上看起来"重拍了"。
2. **同拍双写**:两路推进都判定「state == DRAFT_RENDERING」,于是同时
   开渲同一批拍,输出到同一个固定路径,文件互相覆盖,可能产出半截 mp4。
3. **JSON 互相覆盖**:用户改脚本的同时,流水线在同步分镜,同一个
   storyboard.json 后写覆盖先写。

**为什么用 try-lock 而不是阻塞锁**

拿不到锁就**立刻放弃本轮**,不占着线程池的线程干等。正在做的那一路
本来就会把这活干完,下一轮再来。阻塞锁会让「点一下重拍」这种轻请求
挂住几十秒,体验上像是界面死了。

**不重入**

`advance()` 自己在里面加锁,`confirm_gate()` 因此**不能**再加一次 ——
这是非重入锁,再拿一次会自己把自己锁死。调用链上凡是已经持锁的地方,
内层一律不要再拿。
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Iterator


class ProjectBusy(RuntimeError):
    """项目正在被另一个执行流推进。

    路由层应映射成 HTTP 409,并给出"稍后重试"的提示 ——
    这是**预期内**的并发,不是错误。
    """


#: project_id -> 锁。条目在没有持有者时会被清掉(见 `_release`)。
_locks: dict[str, threading.Lock] = {}
#: project_id -> 引用计数。用来判断一个锁是否还有人在用。
_refs: dict[str, int] = {}
_guard = threading.Lock()


def _acquire(project_id: str) -> threading.Lock:
    """取这个项目的锁(不获取),并登记一次引用。"""
    with _guard:
        lock = _locks.get(project_id)
        if lock is None:
            lock = threading.Lock()
            _locks[project_id] = lock
        _refs[project_id] = _refs.get(project_id, 0) + 1
        return lock


def _release_ref(project_id: str) -> None:
    """销一次引用;没有持有者了就把锁条目清掉。"""
    with _guard:
        n = _refs.get(project_id, 0) - 1
        if n <= 0:
            _refs.pop(project_id, None)
            _locks.pop(project_id, None)
        else:
            _refs[project_id] = n


@contextmanager
def project_lock(project_id: str) -> Iterator[bool]:
    """项目锁。**拿不到就 yield False**,调用方应当放弃本轮。

    用法::

        with project_lock(pid) as got:
            if not got:
                return {"busy": True}
            ...  # 临界区

    `try-lock` 语义:拿不到锁立刻返回 False,绝不阻塞。
    """
    lock = _acquire(project_id)
    got = lock.acquire(blocking=False)
    try:
        yield got
    finally:
        if got:
            lock.release()
        _release_ref(project_id)


@contextmanager
def require_project_lock(project_id: str) -> Iterator[None]:
    """同 `project_lock`,但拿不到就抛 `ProjectBusy`。

    给 HTTP 层的写接口用:冲突要变成一个明确的 409,而不是悄悄不做事。
    """
    with project_lock(project_id) as got:
        if not got:
            raise ProjectBusy("该项目正在推进中,请稍后重试")
        yield


def busy_project_ids() -> list[str]:
    """当前被持有的项目 id(给健康检查/调试用)。"""
    with _guard:
        return sorted(pid for pid, n in _refs.items() if n > 0)
