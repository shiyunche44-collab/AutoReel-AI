"""事件循环卡死探测:卡住时把所有线程的栈落盘。

2026-11-13 8848 卡死(端口在听、404 都不回、CPU 为 0),现场只能靠猜。
这里在事件循环里放一个心跳协程,另起守护线程盯着它;心跳超过
`STALL_SECONDS` 没跳,就用 `faulthandler` 把全部线程的栈写到
`data/logs/hang-<时间>.txt`(直接写文件,不经 logging,因为卡住的
可能正是日志)。同一次卡死只写一份,恢复后再卡会再写。
"""
from __future__ import annotations

import asyncio
import datetime as dt
import faulthandler
import logging
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

BEAT_SECONDS = 5.0
STALL_SECONDS = 60.0

_last_beat = time.monotonic()
_stop = threading.Event()


async def _heartbeat() -> None:
    global _last_beat
    while not _stop.is_set():
        _last_beat = time.monotonic()
        await asyncio.sleep(BEAT_SECONDS)


def _dump(log_dir: Path, stalled: float) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"hang-{dt.datetime.now():%Y%m%d-%H%M%S}.txt"
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(f"event loop heartbeat stalled for {stalled:.0f}s\n\n")
        fh.flush()
        faulthandler.dump_traceback(file=fh, all_threads=True)
    return path


def _watch(log_dir: Path) -> None:
    dumped = False
    while not _stop.wait(BEAT_SECONDS):
        stalled = time.monotonic() - _last_beat
        if stalled < STALL_SECONDS:
            dumped = False
            continue
        if dumped:
            continue
        dumped = True
        try:
            _dump(log_dir, stalled)
        except OSError:
            pass


def start(log_dir: Path) -> None:
    """在事件循环里调用:起心跳协程 + 守护监视线程。"""
    global _last_beat
    _stop.clear()
    _last_beat = time.monotonic()
    asyncio.get_running_loop().create_task(_heartbeat())
    threading.Thread(target=_watch, args=(log_dir,), name="hang-watch", daemon=True).start()


def stop() -> None:
    _stop.set()
