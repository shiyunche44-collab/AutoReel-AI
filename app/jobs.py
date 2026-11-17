"""持久化后台任务:重启后接着跑的"临时线程"替代品。

**为什么需要**

素材池构建、成片精修这类活原来是 `threading.Thread(...).start()` 起的一次性线程。
服务一重启线程就没了:素材池永远停在"在建 / 待打标签",渲染器干等 20 分钟后整片
退成卡片(2026-11-14);建池前预留的额度也没人结算,永远占着项目额度;精修的进度
只在内存里,页面上直接变回"未开始"。

**怎么用**

    @jobs.register("library.build", resource="library", max_attempts=1)
    def _build(payload: dict) -> dict: ...

    jobs.submit("library.build", key=slug(subject), payload={...})

- 任务落库(`jobs` 表)。同 kind 同 key 同时只有一个排队 / 在跑,靠库里的唯一索引去重,
  跨进程也成立 —— 取代各模块里自己维护的 `_running` 集合。
- 执行器 `runner` 只在拿到实例锁的进程里跑(`app/instance_lock.py`)。启动时把上一个
  进程留下的 running 任务放回排队(不算一次尝试)。只读服务也能 `submit`,占锁的服务接手。
- 每个任务归一类资源(`RESOURCES`),各有并发上限,互不占名额。
- 跑的时候按租约心跳;handler 抛异常按指数退避重试,到 `max_attempts` 判死。
  抛 `Permanent` 直接判死(参数错、业务校验不过 —— 重跑也是这个结果)。

**handler 必须幂等**:进程在任务中途被杀,重启后会从头再跑一遍。
"""
from __future__ import annotations

import importlib
import logging
import os
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from . import db

log = logging.getLogger(__name__)

#: 资源类 → 并发上限。新资源类先在这里登记。
RESOURCES: dict[str, int] = {
    # 素材池:下载 + 视觉打标签(有限流)。原来单车和横评车单各起各的线程,大致两路
    "library": 2,
    # 精修:一次完整合成,吃 ffmpeg 和内存
    "refine": 1,
    # 手动 / 事件触发的团队角色(定时的那些仍由 team.scheduler 串行跑)
    "team": 2,
    "default": 2,
}

#: 注册 handler 的模块。执行器启动时导入,保证 handler 都登记上了。
HANDLER_MODULES = (
    "app.pipeline.library.builder",
    "app.team",
    "app.team.publisher",
)

#: 没活时多久再看一眼库(秒)。`submit` 会立刻叫醒,这只是兜底(比如别的进程排的任务)
POLL_SECONDS = 2.0
#: 重试退避:第 n 次失败后等 BASE * 2^(n-1) 秒,封顶 MAX
RETRY_BASE = 30.0
RETRY_MAX = 1800.0
#: 结束超过这么久的任务清掉
KEEP_FINISHED_SECONDS = 30 * 86400


class Permanent(Exception):
    """不值得重试的失败。"""


@dataclass(frozen=True)
class JobKind:
    name: str
    fn: Callable[[dict[str, Any]], Any]
    resource: str
    max_attempts: int
    #: 租约时长(秒)。心跳每 lease/3 续一次;进程死了,过了这么久别的执行器才能接手
    lease: float


_KINDS: dict[str, JobKind] = {}


def register(name: str, *, resource: str = "default", max_attempts: int = 3,
             lease: float = 120.0) -> Callable[[Callable], Callable]:
    if resource not in RESOURCES:
        raise ValueError(f"未登记的资源类:{resource}")

    def deco(fn: Callable[[dict[str, Any]], Any]) -> Callable[[dict[str, Any]], Any]:
        _KINDS[name] = JobKind(name, fn, resource, max(1, max_attempts), lease)
        return fn

    return deco


def kind(name: str) -> JobKind:
    try:
        return _KINDS[name]
    except KeyError:
        raise ValueError(f"未登记的任务类型:{name}") from None


def submit(name: str, key: str = "", payload: dict[str, Any] | None = None, *,
           run_at: float = 0.0) -> int | None:
    """排一个任务。同 kind 同 key 已经在排 / 在跑就返回 None。"""
    k = kind(name)
    job_id = db.enqueue_job(name, key, payload, max_attempts=k.max_attempts, run_at=run_at)
    if job_id is not None:
        runner.wake()
    return job_id


def active(name: str, key: str = "") -> bool:
    """这个任务是不是还没结束(排队或在跑)。"""
    return db.active_job(name, key) is not None


def latest(name: str, key: str = "") -> dict[str, Any] | None:
    return db.latest_job(name, key)


def backoff(attempts: int) -> float:
    return min(RETRY_MAX, RETRY_BASE * (2 ** max(0, attempts - 1)))


class Runner:
    """执行器:每类资源若干个 worker 线程,各自从库里领本资源类的任务。"""

    def __init__(self) -> None:
        self.owner = f"{socket.gethostname()}:{os.getpid()}:{int(time.time())}"
        self._stop = threading.Event()
        self._wake: dict[str, threading.Event] = {}
        self._threads: list[threading.Thread] = []
        self._lock = threading.Lock()
        #: 正在跑的任务 id → kind(给健康检查看)
        self.running: dict[int, str] = {}

    @property
    def started(self) -> bool:
        return any(t.is_alive() for t in self._threads)

    def start(self) -> None:
        with self._lock:
            if self.started:
                return
            for mod in HANDLER_MODULES:
                importlib.import_module(mod)
            self._stop.clear()
            n = db.requeue_orphan_jobs(self.owner)
            if n:
                log.info("接手上次没跑完的后台任务 %d 个", n)
            try:
                db.prune_jobs(time.time() - KEEP_FINISHED_SECONDS)
            except Exception:  # noqa: BLE001 - 清理失败不挡启动
                log.warning("清理旧任务失败", exc_info=True)
            self._threads = []
            for res, limit in RESOURCES.items():
                self._wake[res] = threading.Event()
                for i in range(max(1, limit)):
                    t = threading.Thread(target=self._loop, args=(res,),
                                         name=f"job-{res}-{i}", daemon=True)
                    t.start()
                    self._threads.append(t)

    def stop(self, timeout: float = 5.0) -> None:
        """不等在跑的任务做完:进程退出后它留在 running,下次启动会被放回排队重跑。"""
        self._stop.set()
        for ev in self._wake.values():
            ev.set()
        deadline = time.monotonic() + timeout
        for t in self._threads:
            t.join(max(0.0, deadline - time.monotonic()))

    def wake(self) -> None:
        for ev in self._wake.values():
            ev.set()

    def _kinds_of(self, res: str) -> list[str]:
        return [k.name for k in _KINDS.values() if k.resource == res]

    def _loop(self, res: str) -> None:
        wake = self._wake[res]
        while not self._stop.is_set():
            try:
                job = db.claim_job(self.owner, self._kinds_of(res),
                                   lease=max((k.lease for k in _KINDS.values()
                                              if k.resource == res), default=120.0))
            except Exception:  # noqa: BLE001 - 库忙之类,下一轮再领
                log.warning("领取后台任务失败", exc_info=True)
                job = None
            if job is None:
                wake.wait(POLL_SECONDS)
                wake.clear()
                continue
            self.run_one(job)

    def run_one(self, job: dict[str, Any]) -> None:
        k = _KINDS.get(job["kind"])
        jid = int(job["id"])
        if k is None:  # 领的时候按已登记的 kind 筛过,不会到这里;防御一下
            db.fail_job(jid, self.owner, f"未登记的任务类型:{job['kind']}")
            return
        self.running[jid] = k.name
        beat_stop = threading.Event()

        def _beat() -> None:
            while not beat_stop.wait(k.lease / 3):
                try:
                    if not db.heartbeat_job(jid, self.owner, k.lease):
                        log.warning("任务 %s 的租约被接手了", jid)
                        return
                except Exception:  # noqa: BLE001 - 一次续租失败不要紧,租约还有余量
                    log.debug("续租失败", exc_info=True)

        beat = threading.Thread(target=_beat, name=f"job-beat-{jid}", daemon=True)
        beat.start()
        try:
            result = k.fn(dict(job.get("payload") or {}))
        except Permanent as exc:
            db.fail_job(jid, self.owner, str(exc))
            log.warning("后台任务 %s(%s)失败,不重试:%s", jid, k.name, exc)
        except Exception as exc:  # noqa: BLE001 - handler 出什么错都只记在任务上
            attempts = int(job.get("attempts") or 1)
            msg = str(exc) or type(exc).__name__
            if attempts >= k.max_attempts:
                db.fail_job(jid, self.owner, msg)
                log.warning("后台任务 %s(%s)第 %d 次失败,放弃:%s", jid, k.name, attempts, msg)
            else:
                wait = backoff(attempts)
                db.fail_job(jid, self.owner, msg, retry_at=time.time() + wait)
                log.warning("后台任务 %s(%s)第 %d 次失败,%.0fs 后重试:%s",
                            jid, k.name, attempts, wait, msg)
        else:
            db.finish_job(jid, self.owner, result)
        finally:
            beat_stop.set()
            self.running.pop(jid, None)


runner = Runner()
