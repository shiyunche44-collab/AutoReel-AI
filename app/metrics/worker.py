"""指标抓取的后台循环。

**为什么不塞进 FastAPI 的调度器**:抓一次数据要发好几个网络请求,有的平台
(小红书 / 视频号)还得开浏览器。放事件循环里会卡住整个 Web。

**为什么失败不重试**:抓数据失败通常意味着"这个平台现在抓不到"(登录态过期、
作品还没公开、平台在限流)。下一轮再来就行,当场重试只是白等。
"""
from __future__ import annotations

import threading
import time

from .. import config, db
from . import store

#: 循环的最小间隔(秒)。实际间隔由配置决定,这里只是兜底。
TICK_SECONDS = 30

_thread: threading.Thread | None = None
_lock = threading.Lock()
_stop = threading.Event()
_last_run = 0.0


def _loop() -> None:
    while not _stop.is_set():
        try:
            _tick()
        except Exception:  # noqa: BLE001 - 后台线程不能因单次异常退出
            pass
        _stop.wait(TICK_SECONDS)


def _tick() -> None:
    global _last_run
    if not config.METRICS_ENABLED:
        return
    interval = max(60, config.METRICS_INTERVAL_MIN * 60)
    now = time.time()
    if now - _last_run < interval:
        return
    _last_run = now
    result = store.record_all()
    if result.get("ok") or result.get("failed"):
        db.add_event("__metrics__", "metrics", reason=(
            f"抓取 {result.get('ok', 0)} 条成功,{result.get('failed', 0)} 条失败"))


def start() -> None:
    global _thread
    with _lock:
        if _thread and _thread.is_alive():
            return
        _stop.clear()
        _thread = threading.Thread(target=_loop, name="metrics-worker", daemon=True)
        _thread.start()


def stop() -> None:
    _stop.set()
