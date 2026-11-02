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
from .pipeline import orchestrator
from .state import State

log = logging.getLogger(__name__)

#: 后台循环间隔(秒):多久扫一次项目表,推进处于渲染 / 合成态的项目。
SCHEDULER_INTERVAL = 20

#: 后台循环会自动推进的状态。缺任何一个都表现为"流水线静静卡住"——
#: 不会报错、不会告警,镜头只是永远停在 pending。DRAFT_RENDERING 曾被漏掉,
#: 导致草稿档从来不会自己跑起来。
AUTO_ADVANCE_STATES = frozenset({
    str(State.DRAFT_RENDERING),
    str(State.FINAL_RENDERING),
    # 解说轨成片渲完落在 QA_PASSED(跳过视觉质检),之后就是合成。漏了它项目会停在
    # QA_PASSED 不动 —— 之前只能手改库进 COMPOSING;手动 /advance 又会和调度器的合成撞车
    str(State.QA_PASSED),
    str(State.COMPOSING),
})


# --------------------------------------------------------------------------
# 后台调度
# --------------------------------------------------------------------------
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
    - **worker**(`PIPELINE_CONCURRENCY` 个):从队列取项目 → try-lock →
      在专用线程池里跑 `advance()`。

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
        #: project_id -> 连续失败次数
        self._failures: dict[str, int] = {}
        #: project_id -> 下次可重试的时刻(monotonic)
        self._retry_after: dict[str, float] = {}
        #: 待推进的项目队列
        self._queue: asyncio.Queue[str] | None = None
        #: 已入队(含正在处理的)—— 去重靠它
        self._queued: set[str] = set()
        #: 正在推进的项目
        self._running: set[str] = set()
        #: worker 任务
        self._workers: list[asyncio.Task] = []
        #: 跨线程的停机信号:渲染中的拍做完就收手
        self._drain = threading.Event()

    def _executor(self) -> ThreadPoolExecutor:
        """取(必要时重建)推进用的线程池。"""
        if self._pool is None:
            self._pool = ThreadPoolExecutor(
                max_workers=max(1, config.PIPELINE_CONCURRENCY),
                thread_name_prefix="pipeline")
        return self._pool

    # ---- 扫描 ----
    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._scan()
            except Exception:  # noqa: BLE001 - 扫描不能因单个项目出错而停
                log.exception("扫描项目状态失败")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=SCHEDULER_INTERVAL)
            except asyncio.TimeoutError:
                continue

    def _scan(self) -> None:
        """把处于自动推进态、且不在退避窗口里的项目入队。"""
        if self._queue is None:
            return
        for row in db.list_projects(limit=100):
            pid = row["id"]
            if row["state"] not in AUTO_ADVANCE_STATES:
                continue
            if pid in self._queued or pid in self._running:
                continue
            if time.monotonic() < self._retry_after.get(pid, 0.0):
                continue  # 还在退避窗口里,这一轮跳过
            self._enqueue(pid)

    def _enqueue(self, pid: str) -> None:
        """入队(去重)。"""
        if self._queue is None or pid in self._queued:
            return
        self._queued.add(pid)
        self._queue.put_nowait(pid)

    # ---- worker ----
    async def _worker(self) -> None:
        """worker:取项目 → 推进 → 还在自动态就重新入队。"""
        assert self._queue is not None
        while not self._stop.is_set():
            try:
                pid = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            self._running.add(pid)
            requeue = False
            try:
                requeue = await self._advance_one(pid)
            finally:
                self._running.discard(pid)
                self._queued.discard(pid)
                try:
                    self._queue.task_done()
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
            out = await loop.run_in_executor(self._executor(), self._advance_call, pid)
        except orchestrator.StateRaced as exc:
            # 别的执行流已经把状态推走了(比如另一路刚把它合成完):不是项目坏了,
            # 不判死。之前这里和下面的分支一样一律写 FAILED,把已发布的成片标成了失败。
            log.info("项目 %s 状态被别处推进,本轮让出:%s", pid, exc)
            now = db.get_project(pid) or {}
            return str(now.get("state") or "") in AUTO_ADVANCE_STATES
        except orchestrator.PipelineError as exc:
            # 配置错、lint 没过 —— 再跑一百遍也是这个结果,判死
            self._failures[pid] = MAX_CONSECUTIVE_FAILURES
            log.error("推进 %s 失败(不可恢复):%s", pid, exc)
            # 按**现在**的状态判死(这一轮可能已推进了几步);已完成 / 已结束的不覆盖
            cur = str((db.get_project(pid) or {}).get("state") or "")
            if cur not in (str(State.DONE), str(State.FAILED), str(State.CANCELLED)) and \
                    db.cas_state(pid, cur, str(State.FAILED), reason=f"后台推进失败:{exc}"):
                db.add_event(pid, str(State.FAILED), src=cur,
                             reason=f"后台推进失败:{exc}")
            return False
        except Exception as exc:  # noqa: BLE001 - 瞬时失败,留原状态退避重试
            n = self._failures.get(pid, 0) + 1
            self._failures[pid] = n
            wait = min(BACKOFF_MAX, BACKOFF_BASE * (2 ** (n - 1)))
            self._retry_after[pid] = time.monotonic() + wait
            log.exception("推进 %s 失败(第 %d 次,%.0fs 后重试)", pid, n, wait)
            db.add_event(pid, state, src=state,
                         reason=f"推进失败(第 {n} 次,{wait:.0f}s 后重试):"
                                f"{type(exc).__name__}: {exc}")
            if n >= MAX_CONSECUTIVE_FAILURES:
                db.update_project(pid, state=str(State.FAILED),
                                  state_reason=f"连续 {n} 次推进失败:{exc}")
                db.add_event(pid, str(State.FAILED), src=state,
                             reason=f"连续 {n} 次推进失败")
            return False
        # 成功 → 清掉失败计数
        self._failures.pop(pid, None)
        self._retry_after.pop(pid, None)
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
            self._executor()  # 池在启动时就建好
            self._queue = asyncio.Queue()
            self._queued.clear()
            self._running.clear()
            self._task = asyncio.create_task(self._run())
            self._workers = [
                asyncio.create_task(self._worker())
                for _ in range(max(1, config.PIPELINE_CONCURRENCY))
            ]

    async def stop(self) -> None:
        self._stop.set()   # 停止扫描与 worker
        self._drain.set()  # 让在途的渲染在**拍之间**收手
        for w in self._workers:
            try:
                await asyncio.wait_for(asyncio.shield(w), timeout=60)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):  # noqa: BLE001
                log.debug("停机时 worker 未能干净退出", exc_info=True)
        self._workers.clear()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                log.debug("停机时扫描任务被取消", exc_info=True)
        # 不 wait:在途的那一拍让它跑完(它写的是自己那一拍的文件)
        pool, self._pool = self._pool, None
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)


scheduler = Scheduler()
