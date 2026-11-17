"""后台循环(调度器、持久化任务、发布、指标、AI 团队)的起停,以及进程角色。

**进程角色**(`AUTOREEL_ROLE`,`run.py --role` 设置):

- `all`(默认,原来的样子):一个进程既开 Web 又跑后台循环。
- `web`:只开 Web。随便重启,不会打断渲染、上传、团队角色。
- `worker`:只跑后台循环,没有 HTTP(`app/worker.py`)。

拆开的原因:以前重启 Web(比如让新代码生效)= 打断渲染,所以要有
`restart_server.ps1` 的忙闲判断、看门狗、一堆"别手动杀进程"的规则。拆开后
Web 和后台各起各停;两边只通过数据库交流(项目状态、`jobs` 表、`scheduler_kicks`
表、项目租约),本来就是这么设计的。

worker 每 `HEARTBEAT_SECONDS` 在事件循环里写一次心跳文件,Web 的健康检查和
`ensure_server.ps1` 靠它判断 worker 活着、没卡死。
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Any

from . import config, instance_lock, jobs, maintenance

log = logging.getLogger(__name__)

ALL, WEB, WORKER = "all", "web", "worker"
ROLES = (ALL, WEB, WORKER)

#: worker 多久写一次心跳(秒)
HEARTBEAT_SECONDS = 10.0
#: 心跳多久没更新就算 worker 不在了(秒)
HEARTBEAT_STALE_SECONDS = 90.0


def role() -> str:
    r = (os.environ.get("AUTOREEL_ROLE") or ALL).strip().lower()
    return r if r in ROLES else ALL


def runs_background() -> bool:
    """本进程该不该(尝试)跑后台循环。"""
    return role() in (ALL, WORKER)


# --------------------------------------------------------------------------
# 心跳
# --------------------------------------------------------------------------
def heartbeat_path() -> Path:
    return config.DATA_DIR / f"{config.DB_FILENAME}.worker.heartbeat"


def write_heartbeat() -> None:
    try:
        heartbeat_path().write_text(f"{os.getpid()} {time.time():.0f}", encoding="ascii")
    except OSError:
        log.debug("写心跳失败", exc_info=True)


def read_heartbeat() -> dict[str, Any]:
    """{pid, at, age};没有心跳文件返回 {}。"""
    try:
        pid, at = heartbeat_path().read_text(encoding="ascii").split()
        return {"pid": int(pid), "at": float(at), "age": round(time.time() - float(at), 1)}
    except (OSError, ValueError):
        return {}


async def heartbeat_loop(stop: asyncio.Event) -> None:
    """在事件循环里跳:循环卡住心跳就停,看门狗据此判断 worker 卡死。"""
    while not stop.is_set():
        write_heartbeat()
        try:
            await asyncio.wait_for(stop.wait(), timeout=HEARTBEAT_SECONDS)
        except TimeoutError:
            continue


def stop_request_path() -> Path:
    """停机请求文件:`restart_server.ps1` 写它,worker 看到就优雅退出。

    Windows 上没法从别的进程给它发 SIGTERM(`Stop-Process` 是硬杀,渲染中的拍会被
    打断),所以用文件传话。"""
    return config.DATA_DIR / f"{config.DB_FILENAME}.worker.stop"


async def watch_stop_request(stop: asyncio.Event, poll: float = 1.0) -> None:
    """每秒看一眼停机请求文件;看到就删掉它并触发停机。"""
    path = stop_request_path()
    while not stop.is_set():
        if path.exists():
            log.info("收到停机请求(%s)", path.name)
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            stop.set()
            return
        try:
            await asyncio.wait_for(stop.wait(), timeout=poll)
        except TimeoutError:
            continue


def clear_heartbeat() -> None:
    try:
        heartbeat_path().unlink(missing_ok=True)
    except OSError:
        pass


# --------------------------------------------------------------------------
# 起停
# --------------------------------------------------------------------------
_primary = False


def start() -> bool:
    """抢实例锁,抢到就把后台循环全部起来。返回本进程是否在跑后台循环。

    必须在事件循环里调用(调度器要拿当前 loop)。
    """
    global _primary
    from .main import scheduler  # 延迟导入:调度器单例挂在 app.main 上(测试从那里打桩)
    from .metrics import worker as metrics_worker
    from .publish import accounts as publish_accounts
    from .publish import service as publish_service
    from .team import scheduler as team_scheduler

    # 一个库只让一个进程跑后台循环:抢不到锁说明已有别的进程在推进这个库(见 instance_lock)
    if not instance_lock.acquire():
        _primary = False
        return False
    _primary = True
    # 临时物和缓存只在跑后台循环的进程里清:Web 单独重启时清 data/_tmp,
    # 会删掉 worker 正在渲染的中间文件
    try:
        maintenance.prune_temp()
    except Exception:  # noqa: BLE001 - 清理失败不该挡住启动
        log.warning("清理临时目录失败", exc_info=True)
    try:
        pruned = maintenance.prune_caches()  # 按龄 + 体积上限
        if pruned["removed"]:
            log.info("缓存清理:删 %d 项,腾出 %s", pruned["removed"], pruned["freed_human"])
    except Exception:  # noqa: BLE001
        log.warning("清理缓存失败", exc_info=True)
    try:
        from . import db

        db.prune_llm_calls(90)  # 调用账本留 90 天
        from . import degrade

        degrade.prune(90)
    except Exception:  # noqa: BLE001
        log.warning("清理模型调用账本失败", exc_info=True)
    scheduler.start()
    jobs.runner.start()  # 持久化后台任务:上次没跑完的接着跑
    publish_accounts.import_from_file()
    publish_service.start()
    metrics_worker.start()
    team_scheduler.start()
    return True


async def stop() -> None:
    global _primary
    if not _primary:
        return
    from .main import scheduler
    from .metrics import worker as metrics_worker
    from .publish import service as publish_service
    from .team import scheduler as team_scheduler

    await scheduler.stop()
    jobs.runner.stop()
    publish_service.stop()
    metrics_worker.stop()
    team_scheduler.stop()
    instance_lock.release()
    _primary = False


def status() -> dict[str, Any]:
    """给健康检查 / 首页横幅:后台循环在不在跑、在哪个进程。

    `ok` = 这个库的后台循环有人在跑(本进程,或活着的 worker)。只读横幅看它。
    """
    r = role()
    if _primary:
        return {"ok": True, "mode": r, "here": True, "owner": instance_lock.owner()}
    beat = read_heartbeat()
    alive = bool(beat) and beat["age"] < HEARTBEAT_STALE_SECONDS
    if r == WEB:
        return {"ok": alive, "mode": r, "here": False, "worker": beat,
                "owner": instance_lock.owner()}
    # all 模式但没抢到锁:另一个进程(老式单进程服务,或 worker)在跑
    return {"ok": False, "mode": r, "here": False, "worker": beat,
            "owner": instance_lock.owner()}
