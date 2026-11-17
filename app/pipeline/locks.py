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

**两层**

1. 进程内的 `threading.Lock`(便宜,管同一进程里的线程);
2. 数据库里的项目租约(`project_leases`),管**不同进程**:主服务的调度器和只读服务
   上的 HTTP 写接口原来彼此看不见对方的锁。持有期间后台线程每 `LEASE_TTL/3` 续租,
   进程被杀后租约最多 `LEASE_TTL` 秒过期,别人就能接手;正常停机时 `release_all()` 立刻放。
"""
from __future__ import annotations

import itertools
import logging
import os
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

from .. import db

log = logging.getLogger(__name__)

#: 租约有效期(秒)。持有者每 TTL/3 续一次
LEASE_TTL = 120.0
#: 本进程的身份;每次拿锁再拼上线程号和序号,保证两次持有不会被当成同一个主人
_PROCESS = f"{socket.gethostname()}:{os.getpid()}:{int(time.time())}"
_seq = itertools.count(1)
#: 本进程持有的租约:project_id -> owner
_held: dict[str, str] = {}
_keeper: threading.Thread | None = None
_keeper_stop = threading.Event()


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
    owner = ""
    if got:
        owner = _take_lease(project_id)
        if not owner:  # 别的进程正持有这个项目
            lock.release()
            got = False
    try:
        yield got
    finally:
        if got:
            _drop_lease(project_id, owner)
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
    """当前被持有的项目 id(健康检查 / 重启前看忙不忙用)。跨进程:读库里没过期的租约。"""
    try:
        return sorted({r["project_id"] for r in db.active_leases()})
    except Exception:  # noqa: BLE001 - 库读不了就退回本进程的视图
        log.warning("读项目租约失败,只报本进程持有的", exc_info=True)
        with _guard:
            return sorted(pid for pid, n in _refs.items() if n > 0)


# --------------------------------------------------------------------------
# 数据库租约
# --------------------------------------------------------------------------
def _take_lease(project_id: str) -> str:
    """拿租约,成功返回 owner,失败(别的进程持有 / 库出错)返回空串。"""
    owner = f"{_PROCESS}:{threading.get_ident()}:{next(_seq)}"
    try:
        if not db.try_acquire_lease(project_id, owner, LEASE_TTL):
            return ""
    except Exception:  # noqa: BLE001 - 拿不准就当忙:宁可这一轮不做,也别两路一起写
        log.warning("拿项目 %s 的租约失败,本轮当作忙", project_id, exc_info=True)
        return ""
    with _guard:
        _held[project_id] = owner
    _ensure_keeper()
    return owner


def _drop_lease(project_id: str, owner: str) -> None:
    with _guard:
        if _held.get(project_id) == owner:
            _held.pop(project_id, None)
    try:
        db.release_lease(project_id, owner)
    except Exception:  # noqa: BLE001 - 放不掉也不要紧,TTL 到了自然过期
        log.warning("释放项目 %s 的租约失败(%.0f 秒后自然过期)", project_id, LEASE_TTL,
                    exc_info=True)


def _ensure_keeper() -> None:
    """起(或复用)续租线程:一个进程一个,给本进程持有的所有租约续期。"""
    global _keeper
    with _guard:
        _keeper_stop.clear()
        if _keeper is not None and _keeper.is_alive():
            return
        _keeper = threading.Thread(target=_keep, name="project-lease-keeper", daemon=True)
        _keeper.start()


def _keep() -> None:
    while not _keeper_stop.wait(LEASE_TTL / 3):
        with _guard:
            held = dict(_held)
        if not held:
            continue
        try:
            lost = db.renew_leases(held, LEASE_TTL)
        except Exception:  # noqa: BLE001 - 一次续不上不要紧,租约还有余量
            log.warning("续项目租约失败", exc_info=True)
            continue
        for pid in lost:
            # 只有本进程卡住超过 TTL 才会到这里(看门狗那时多半也在动手了)
            log.error("项目 %s 的租约已被别的执行流接手,本进程手头那一步可能和它撞车", pid)


def release_all() -> None:
    """正常停机时放掉本进程持有的全部租约,重启后不用等 TTL 过期。"""
    with _guard:
        held = dict(_held)
        _held.clear()
    _keeper_stop.set()
    for pid, owner in held.items():
        try:
            db.release_lease(pid, owner)
        except Exception:  # noqa: BLE001
            log.warning("停机时释放项目 %s 的租约失败", pid, exc_info=True)
