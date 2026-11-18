"""后台调度器:扫描处于生成态的项目并推进。

从 main.py 拆出来。`app.main` 仍然重新导出这里的公开名,旧引用不受影响。
**新增会自动推进的状态必须加进 `AUTO_ADVANCE_STATES`**,漏了就是"静静卡住"。
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import config, db
from .pipeline import locks, orchestrator
from .state import AUTO_STATES, PREP_LANE, RENDER_LANE, State, lane_of

log = logging.getLogger(__name__)

#: 后台循环间隔(秒):多久扫一次项目表,推进处于渲染 / 合成态的项目。
SCHEDULER_INTERVAL = 20

#: 后台循环会自动推进的状态,和各自的通道。**都从 `state.SPECS` 推导**:新增状态只改
#: 那张声明表。以前这里手写一份集合,漏了就"静静卡住"(DRAFT_RENDERING 曾被漏掉,
#: 草稿档从来不会自己跑起来)。
#:
#: - **渲染通道**(`PIPELINE_CONCURRENCY` 路):ffmpeg + 概念动画的 Chrome,内存是硬约束。
#: - **备料通道**(`PREP_CONCURRENCY` 路):事实采集、写稿、分镜 —— 等 LLM 和网络为主,
#:   不吃本机资源。新项目写稿不用排在别人的渲染后面,反过来也一样。
#:
#: 备料这几个状态原来靠建项目时起的一次性后台线程推进(`_bootstrap`):服务在中途重启,
#: 项目就永远停在 DRAFT_INTENT / FACTS_RESOLVED,没人接手。现在归调度器。
RENDER, PREP = RENDER_LANE, PREP_LANE
RENDER_STATES = frozenset(str(s) for s in AUTO_STATES if lane_of(s) == RENDER)
PREP_STATES = frozenset(str(s) for s in AUTO_STATES if lane_of(s) == PREP)
AUTO_ADVANCE_STATES = RENDER_STATES | PREP_STATES


# --------------------------------------------------------------------------
# 后台调度
# --------------------------------------------------------------------------
#: 多久取一次别的进程留下的"催一下"(秒)
KICK_POLL_SECONDS = 1.0

#: 同一个项目连续失败多少次才判定为"彻底失败"。
#:
#: 之前是"一失败就 FAILED",但网络抖动、ffmpeg 偶发退出都是**瞬时**的,
#: 一次失败就把项目判死等于白白重跑一次昂贵的前置阶段(事实采集、
#: 脚本生成都是要花钱的)。现在瞬时失败留在原状态退避重试,连着失败
#: 若干次才判死。
MAX_CONSECUTIVE_FAILURES = 5

#: 退避:第 n 次失败后等多久再试(秒),按 2 的幂增长,封顶 5 分钟。
BACKOFF_BASE = 5.0
BACKOFF_MAX = 300.0


class Scheduler:
    """扫描处于生成态的项目并推进。

    ⚠️ **推进是同步的重活**:ffmpeg 子进程、TTS 与 LLM 的 HTTP 调用。
    之前直接写在 `async def sweep()` 里,整段跑在事件循环线程上 ——
    渲染期间(一次 tick 约 50 秒,合成可能几分钟)**整个 Web 服务冻结**:
    健康检查、状态轮询、页面加载全部无响应。前端每 5 秒轮询一次状态,
    用户看到的就是转圈卡死。

    **并发模型:扫描与推进分开。**

    - **扫描**(每 `SCHEDULER_INTERVAL` 一轮):把处于自动态的项目**入队**,
      按 id 去重 —— 同一个项目不会在队列里出现两次。
    - **worker**:从队列取项目 → try-lock → 在专用线程池里跑 `advance()`。
      渲染通道 `PIPELINE_CONCURRENCY` 个,备料通道 `PREP_CONCURRENCY` 个(见 `lane_of`),
      两条通道互不占名额。

    于是:同一时刻**不同项目**可以并行渲染,而**同一项目**永远只有一个
    写入者(项目锁保证)。队列化天然是轮转的 —— 一个项目渲 4 拍要 50 秒,
    这段时间别的项目照跑,不会出现"长项目把其它项目全饿死"。

    **优雅停机**:`stop()` 先给一个 `threading.Event`(跨线程可见),
    `advance()` 在**每拍之间**检查它 —— 手头这一拍做完就收手,剩下的拍
    留到下次。于是重启后从断点接着跑,而不是留下半截 mp4。
    """

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        #: 专用线程池。**惰性创建**:`stop()` 会把它关掉(关掉之后不能
        #: 再提交任务),所以每次用之前都要确保它还在 —— 否则服务重启
        #: 或测试里再跑一轮时会撞上 "cannot schedule new futures
        #: after shutdown"。
        self._pool: ThreadPoolExecutor | None = None
        #: 备料通道的线程池(同样惰性创建)
        self._prep_pool: ThreadPoolExecutor | None = None
        # 连续失败次数与退避到期时刻落在 projects.advance_failures / advance_retry_at:
        # 原来存在这里的两个 dict 里,服务一重启就清零。
        #: 待推进的项目队列(渲染通道)
        self._queue: asyncio.Queue[str] | None = None
        #: 待推进的项目队列(备料通道)
        self._prep_queue: asyncio.Queue[str] | None = None
        #: 调度器所在的事件循环;`kick()` 从别的线程入队要经过它
        self._loop: asyncio.AbstractEventLoop | None = None
        #: 已入队(含正在处理的)—— 去重靠它,两条通道共用
        self._queued: set[str] = set()
        #: 正在推进的项目
        self._running: set[str] = set()
        #: worker 任务
        self._workers: list[asyncio.Task] = []
        #: 跨线程的停机信号:渲染中的拍做完就收手
        self._drain = threading.Event()

    def _executor(self, lane: str = RENDER) -> ThreadPoolExecutor:
        """取(必要时重建)这条通道推进用的线程池。"""
        if lane == PREP:
            if self._prep_pool is None:
                self._prep_pool = ThreadPoolExecutor(
                    max_workers=max(1, config.PREP_CONCURRENCY),
                    thread_name_prefix="pipeline-prep")
            return self._prep_pool
        if self._pool is None:
            self._pool = ThreadPoolExecutor(
                max_workers=max(1, config.PIPELINE_CONCURRENCY),
                thread_name_prefix="pipeline")
        return self._pool

    def _queue_for(self, lane: str | None) -> asyncio.Queue[str] | None:
        if lane == PREP:
            return self._prep_queue
        return self._queue if lane == RENDER else None

    # ---- 扫描 ----
    async def _run(self) -> None:
        next_scan = 0.0
        while not self._stop.is_set():
            if time.monotonic() >= next_scan:
                next_scan = time.monotonic() + SCHEDULER_INTERVAL
                try:
                    self._scan()
                except Exception:  # noqa: BLE001 - 扫描不能因单个项目出错而停
                    log.exception("扫描项目状态失败")
            try:
                # 别的进程(--role web)催过的项目:每秒取一次,不用等满一轮扫描
                for pid in db.pop_kicks():
                    self._kick(pid)
            except Exception:  # noqa: BLE001 - 取不到就等下一秒 / 下一轮扫描
                log.debug("读取跨进程催办失败", exc_info=True)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=KICK_POLL_SECONDS)
            except TimeoutError:
                continue

    def _scan(self) -> None:
        """把处于自动推进态、且不在退避窗口里的项目入队。"""
        if self._queue is None and self._prep_queue is None:
            return
        # 按状态查,不按"最近 100 个":项目多了以后,久未更新的自动态项目(比如退避中的)
        # 会被挤出最近 100 个,从此再没人扫到它。
        for row in db.list_projects_in_states(sorted(AUTO_ADVANCE_STATES)):
            pid = row["id"]
            if pid in self._queued or pid in self._running:
                continue
            if time.time() < float(row.get("advance_retry_at") or 0.0):
                continue  # 还在退避窗口里,这一轮跳过
            self._enqueue(pid, row["state"])

    def _enqueue(self, pid: str, state: str | None = None) -> None:
        """入队(去重),按**当前状态**进对应通道。不给 `state` 就现读一次。"""
        if pid in self._queued:
            return
        if state is None:
            state = str((db.get_project(pid) or {}).get("state") or "")
        queue = self._queue_for(lane_of(state))
        if queue is None:
            return
        self._queued.add(pid)
        queue.put_nowait(pid)

    def kick(self, pid: str) -> None:
        """马上把这个项目入队,不等下一轮扫描(建项目后调用,省掉最多 20 秒的空等)。

        **线程安全**:路由和团队角色都跑在别的线程里,而 `asyncio.Queue` 只能在事件循环
        线程上碰,所以经 `call_soon_threadsafe` 转一道。调度器不在本进程时记进库里
        (`scheduler_kicks`),跑调度器的进程每秒取走。
        """
        loop = self._loop
        if loop is None or self._stop.is_set():
            # 调度器不在本进程(Web 进程 / 只读服务 / 离线脚本):记进库里,
            # 跑调度器的那个进程每秒取一次。没人跑的话,下次启动的扫描也会接手。
            try:
                db.request_kick(pid)
            except Exception:  # noqa: BLE001 - 催不到不要紧,扫描会接手
                log.debug("记录跨进程催办失败", exc_info=True)
            return
        try:
            loop.call_soon_threadsafe(self._kick, pid)
        except RuntimeError:  # 事件循环已经关了:等下次启动的扫描
            log.debug("调度器已停,项目 %s 留给下次扫描", pid)

    def _kick(self, pid: str) -> None:
        if pid in self._running:
            return
        try:
            self._enqueue(pid)
        except Exception:  # noqa: BLE001 - 入队失败不要紧,下一轮扫描会补上
            log.exception("即时入队 %s 失败", pid)

    # ---- worker ----
    async def _worker(self, lane: str = RENDER) -> None:
        """worker:取项目 → 推进 → 还在自动态就重新入队(按新状态进对应通道)。"""
        queue = self._queue_for(lane)
        assert queue is not None
        while not self._stop.is_set():
            try:
                pid = await asyncio.wait_for(queue.get(), timeout=1.0)
            except TimeoutError:
                continue
            self._running.add(pid)
            requeue = False
            try:
                # 排队期间状态可能已经换了通道(比如人在备料态手动推进到了渲染态):
                # 在备料池里渲染会绕过渲染并发上限,所以不在这里做,改投到对的通道。
                state = str((db.get_project(pid) or {}).get("state") or "")
                if lane_of(state) == lane:
                    requeue = await self._advance_one(pid)
                else:
                    requeue = state in AUTO_ADVANCE_STATES
            finally:
                self._running.discard(pid)
                self._queued.discard(pid)
                try:
                    queue.task_done()
                except ValueError:
                    pass
            # 重新入队要等 `_queued` 里的自己被摘掉,否则去重会把它挡回去
            if requeue:
                self._enqueue(pid)

    def _advance_call(self, pid: str) -> dict:
        """在线程池里跑的那一下。"""
        return orchestrator.advance(pid, stop_event=self._drain)

    async def _advance_one(self, pid: str) -> bool:
        """推进一个项目,并按失败类型分别处理。

        返回 True 表示"这个项目还在自动态,应该重新入队"。
        """
        loop = asyncio.get_running_loop()
        row = db.get_project(pid) or {}
        state = str(row.get("state") or "")
        try:
            # 关键:推进放到线程池,事件循环不被占住
            out = await loop.run_in_executor(
                self._executor(lane_of(state) or RENDER), self._advance_call, pid)
        except orchestrator.StateRaced as exc:
            # 别的执行流已经把状态推走了(比如另一路刚把它合成完):不是项目坏了,
            # 不判死。之前这里和下面的分支一样一律写 FAILED,把已发布的成片标成了失败。
            log.info("项目 %s 状态被别处推进,本轮让出:%s", pid, exc)
            now = db.get_project(pid) or {}
            return str(now.get("state") or "") in AUTO_ADVANCE_STATES
        except orchestrator.PipelineError as exc:
            # 配置错、lint 没过 —— 再跑一百遍也是这个结果,判死
            log.error("推进 %s 失败(不可恢复):%s", pid, exc)
            # 按**现在**的状态判死(这一轮可能已推进了几步);已完成 / 已结束的不覆盖
            cur = str((db.get_project(pid) or {}).get("state") or "")
            if cur not in (str(State.DONE), str(State.FAILED), str(State.CANCELLED)) and \
                    db.cas_state(pid, cur, str(State.FAILED), reason=f"后台推进失败:{exc}"):
                db.add_event(pid, str(State.FAILED), src=cur,
                             reason=f"后台推进失败:{exc}")
            return False
        except Exception as exc:  # noqa: BLE001 - 瞬时失败,留原状态退避重试
            n = int(row.get("advance_failures") or 0) + 1
            wait = min(BACKOFF_MAX, BACKOFF_BASE * (2 ** (n - 1)))
            db.set_advance_failures(pid, n, time.time() + wait)
            log.exception("推进 %s 失败(第 %d 次,%.0fs 后重试)", pid, n, wait)
            db.add_event(pid, state, src=state,
                         reason=f"推进失败(第 {n} 次,{wait:.0f}s 后重试):"
                                f"{type(exc).__name__}: {exc}")
            if n >= MAX_CONSECUTIVE_FAILURES:
                # CAS:失败这一轮里状态可能已被推进(甚至已完成),不能无条件写 FAILED
                cur = str((db.get_project(pid) or {}).get("state") or "")
                if cur in AUTO_ADVANCE_STATES and db.cas_state(
                        pid, cur, str(State.FAILED), reason=f"连续 {n} 次推进失败:{exc}"):
                    db.add_event(pid, str(State.FAILED), src=cur,
                                 reason=f"连续 {n} 次推进失败")
            return False
        # 成功 → 清掉失败计数
        if row.get("advance_failures") or row.get("advance_retry_at"):
            db.clear_advance_failures(pid)
        if isinstance(out, dict) and out.get("busy"):
            log.debug("项目 %s 正被别的执行流推进,本轮让出", pid)
            return False
        # 还在自动态 → 该重新入队(排到队尾,给别的项目让出位置)
        now = db.get_project(pid) or {}
        return str(now.get("state") or "") in AUTO_ADVANCE_STATES

    # ---- 兼容旧调用 ----
    async def sweep(self) -> None:
        """扫描一轮(历史上是"直接推进一轮",现在是"入队,交给 worker")。"""
        self._scan()

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._drain.clear()
            # 启动前单独推进过(`_advance_one` 会惰性建池)留下的旧池按当时的并发配置建的,
            # 不能复用:换掉,按现在的 PIPELINE_CONCURRENCY / PREP_CONCURRENCY 重建
            for old in (self._pool, self._prep_pool):
                if old is not None:
                    old.shutdown(wait=False)
            self._pool = self._prep_pool = None
            self._executor()  # 池在启动时就建好
            self._executor(PREP)
            self._loop = asyncio.get_running_loop()
            self._queue = asyncio.Queue()
            self._prep_queue = asyncio.Queue()
            self._queued.clear()
            self._running.clear()
            self._task = asyncio.create_task(self._run())
            self._workers = [
                asyncio.create_task(self._worker(RENDER))
                for _ in range(max(1, config.PIPELINE_CONCURRENCY))
            ] + [
                asyncio.create_task(self._worker(PREP))
                for _ in range(max(1, config.PREP_CONCURRENCY))
            ]

    async def stop(self) -> None:
        self._stop.set()   # 停止扫描与 worker
        self._drain.set()  # 让在途的渲染在**拍之间**收手
        self._loop = None  # 之后的 kick() 不再入队
        if self._workers:
            # 一起等,总共最多 60 秒(逐个等的话 worker 多了会叠成几分钟)
            _, pending = await asyncio.wait(self._workers, timeout=60)
            if pending:
                log.debug("停机时还有 %d 个 worker 没收尾", len(pending))
        self._workers.clear()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                log.debug("停机时扫描任务被取消", exc_info=True)
        # 不 wait:在途的那一拍让它跑完(它写的是自己那一拍的文件)
        # 在途的那一拍做完会自己放租约;这里把本进程还挂着的都放掉,重启后不用等 TTL
        locks.release_all()
        pools = (self._pool, self._prep_pool)
        self._pool = self._prep_pool = None
        for pool in pools:
            if pool is not None:
                pool.shutdown(wait=False, cancel_futures=True)


scheduler = Scheduler()
