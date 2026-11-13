"""一个库只让一个进程跑后台循环(调度器、发布、指标、AI 团队)。

**为什么需要**

项目锁(`pipeline/locks.py`)是进程内的 `threading.Lock`,拦不住第二个进程。
8848 / 8860 / 8849 连的是同一个库,两个进程各起一个调度器就会推进同一个项目:
重复渲染、重复合成、写坏 mp4,报"后台推进失败"。以前只靠"启动前先确认端口"
这条约定,出过不止一次事。

**做法**

启动时对 `data/<库文件名>.scheduler.lock` 加操作系统级的排他锁,拿着不放直到进程
退出。抢不到 = 已经有别的进程在跑后台循环,本进程就只开 Web(能看、能点门禁,
不推进、不发布、不跑团队)。

用操作系统的文件锁而不是"库里一行 + 心跳":进程崩了或被强杀,锁由系统立刻收回,
没有"上一个持有者死了,要等心跳超时"这段空窗,也不会留下要人手工清的残锁。

锁文件跟着库走(同一个 `DATA_DIR` + `DB_FILENAME`),测试用的临时库各有各的锁。
"""
from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import IO, Any

from . import config

log = logging.getLogger(__name__)

_guard = threading.Lock()
#: 持锁期间一直开着的文件句柄;关掉 = 放锁
_held: IO[bytes] | None = None


def lock_path() -> Path:
    return config.DATA_DIR / f"{config.DB_FILENAME}.scheduler.lock"


def _owner_path() -> Path:
    # 持有者信息单独放一个文件:Windows 的文件锁是强制锁,别的进程读不了被锁住的字节
    return config.DATA_DIR / f"{config.DB_FILENAME}.scheduler.owner"


def _try_lock(fh: IO[bytes]) -> bool:
    try:
        if os.name == "nt":
            import msvcrt

            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def acquire() -> bool:
    """抢后台循环的独占权。True = 本进程是主;False = 别的进程已经在跑。

    同一进程里重复调用是幂等的(已持有就直接返回 True)。
    """
    global _held
    with _guard:
        if _held is not None:
            return True
        config.ensure_dirs()
        fh = open(lock_path(), "a+b")  # noqa: SIM115 - 句柄要一直开着,关掉就放锁了
        if not _try_lock(fh):
            fh.close()
            return False
        _held = fh
    try:
        _owner_path().write_text(f"{os.getpid()} {int(time.time())}", encoding="ascii")
    except OSError:  # 这个文件只是给人看的,写不了不影响锁
        log.debug("写持锁进程信息失败", exc_info=True)
    return True


def release() -> None:
    """放锁(正常停机时调;进程被杀时系统会自己收回)。"""
    global _held
    with _guard:
        fh, _held = _held, None
    if fh is None:
        return
    try:
        fh.close()
    except OSError:
        log.debug("关闭锁文件失败", exc_info=True)


def held() -> bool:
    return _held is not None


def owner() -> dict[str, Any]:
    """当前(或最近一次)持锁的进程:{pid, since}。读不到返回 {}。"""
    try:
        pid, since = _owner_path().read_text(encoding="ascii").split()
        return {"pid": int(pid), "since": int(since)}
    except (OSError, ValueError):
        return {}
