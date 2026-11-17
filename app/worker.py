"""后台进程入口(`python run.py --role worker`):只跑后台循环,不开 HTTP。

和 `--role web` 配对使用,见 `app/background.py`。Web 随便重启;这个进程只在要加载
新的流水线代码时才需要重启(`restart_server.ps1 -Target worker`,它会先看忙不忙)。

- 抢不到实例锁(已有别的进程在跑后台循环)就直接退出,退出码 3。
- 事件循环里每 10 秒写一次心跳文件,看门狗据此判断卡死。
- 收到 Ctrl+C / SIGTERM(Windows 上还有 Ctrl+Break),或看到停机请求文件
  `data/<库名>.worker.stop`(`restart_server.ps1` 写的)就优雅停机:渲染在拍与拍之间收手,
  放掉项目租约和实例锁。被强杀也没关系:租约会过期,任务会在下次启动时被接手。
"""
from __future__ import annotations

import asyncio
import logging
import signal

from . import background, config, db, hang_watch, logging_setup

log = logging.getLogger(__name__)

#: 抢不到实例锁时的退出码(看门狗据此知道"已经有人在跑",不必重试)
EXIT_LOCKED = 3


async def _serve(stop: asyncio.Event) -> int:
    hang_watch.start(logging_setup.log_dir())
    # 上次没被取走的停机请求(比如那次是硬杀的)不能让这次一启动就退出
    background.stop_request_path().unlink(missing_ok=True)
    if not background.start():
        log.warning("这个库已有别的进程在跑后台循环,worker 不启动")
        return EXIT_LOCKED
    log.info("worker 已启动:调度器、后台任务、发布、指标、AI 团队")
    beat = asyncio.create_task(background.heartbeat_loop(stop))
    watcher = asyncio.create_task(background.watch_stop_request(stop))
    try:
        await stop.wait()
    finally:
        log.info("worker 停机中……")
        await background.stop()
        await beat
        await watcher
        background.clear_heartbeat()
        hang_watch.stop()
        log.info("worker 已停")
    return 0


def _install_signals(loop: asyncio.AbstractEventLoop, stop: asyncio.Event) -> None:
    def _request_stop(*_: object) -> None:
        loop.call_soon_threadsafe(stop.set)

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError, ValueError):
            # Windows 的事件循环不支持 add_signal_handler:退回 signal.signal
            signal.signal(sig, _request_stop)


def main() -> int:
    config.ensure_dirs()
    db.init_db()
    logging_setup.setup()

    async def _main() -> int:
        stop = asyncio.Event()
        _install_signals(asyncio.get_running_loop(), stop)
        return await _serve(stop)

    return asyncio.run(_main())
