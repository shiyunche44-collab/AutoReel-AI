"""团队的后台节奏线程。

写法同 `metrics/worker.py`:守护线程、每分钟醒一次、单次异常不退出。
不放进 FastAPI 的事件循环 —— 抓热点、调 LLM 动辄几十秒,会卡住整个 Web。
"""
from __future__ import annotations

import datetime as dt
import logging
import threading

from .. import config, db
from . import is_due, paused, roles, run_role

log = logging.getLogger(__name__)

TICK_SECONDS = 60

_thread: threading.Thread | None = None
_lock = threading.Lock()
_stop = threading.Event()


def tick(now: dt.datetime | None = None) -> list[str]:
    """把到点的角色挨个跑一遍。返回跑了哪些(测试用)。"""
    if not config.TEAM_ENABLED or paused():
        return []
    now = now or dt.datetime.now()
    ran = []
    for r in roles():
        last = db.last_team_run(r.name)
        if not is_due(r.cadence, now, last["started_at"] if last else None):
            continue
        run_role(r.name)
        ran.append(r.name)
    return ran


def _loop() -> None:
    # 刚开机先等一轮:服务启动时 Web、调度器、发布线程都在抢资源
    while not _stop.wait(TICK_SECONDS):
        try:
            tick()
        except Exception:  # noqa: BLE001 - 后台线程不能因单次异常退出
            log.exception("team tick failed")


def start() -> None:
    global _thread
    with _lock:
        if _thread and _thread.is_alive():
            return
        _stop.clear()
        _thread = threading.Thread(target=_loop, name="team-worker", daemon=True)
        _thread.start()


def stop() -> None:
    _stop.set()
