"""FastAPI 应用:Web 控制台 + 后台调度器。

设计要点:
- **后台调度循环**独立于请求。逐拍渲染要几分钟,由常驻循环每轮渲几拍推进,
  而不是让 HTTP 请求等待。
- **健康横幅**在启动时检测 ffmpeg 与生成通道,缺失能力以红色横幅明示,
  绝不静默降级。
- 所有密钥只从本地 .env 读取,**永远不会**出现在任何 API 响应里。
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import mimetypes
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from . import config, costs, db, logging_setup, maintenance
from .categories import available_categories, default_category, load_profile
from .llm.deepseek import DeepSeekProvider
from .llm.minimax import MiniMaxProvider
from .models import (
    GateRequest,
    ProjectCreate,
    RetryRequest,
    ScriptPatch,
    ScriptSegment,
    ShotPatch,
)
from .pipeline import aspect, concept_styles, library, orchestrator
from .pipeline import render as render_mod
from .pipeline.locks import ProjectBusy
from .pipeline.locks import busy_project_ids as locks_busy_ids
from .providers import cli
from .publish import accounts as publish_accounts
from .publish import browser as publish_browser
from .publish import metadata as publish_metadata
from .publish import service as publish_service
from .publish.registry import PLATFORMS, platforms_info
from .metrics import store as metrics_store
from .metrics import worker as metrics_worker
from .state import State
from .views import ProjectNotFound, build_detail, build_summary, needs_attention

log = logging.getLogger(__name__)

# Windows 注册表可能把 .js 登记成 text/plain,浏览器会拒绝按 ES Module 加载前端脚本
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/css", ".css")

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "web" / "templates"))


def _fmt_hhmmss(value: float) -> str:
    """时间戳格式化为 HH:MM:SS。

    必须在 Python 侧做:Jinja 的 `%` 是 Python 字符串格式化,
    在模板里写 '%H:%M:%S' 会被当成格式串直接抛 ValueError。
    """
    ts = time.localtime(float(value))
    return time.strftime("%H:%M:%S", ts)


def _fmt_datetime(value: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(value)))


def _basename(value: str) -> str:
    """取文件名。素材 URL 走 /media/{project}/{kind}/{file},需要剥掉目录。"""
    return str(value).replace("\\", "/").rsplit("/", 1)[-1]


def _media_kind(value: str) -> str:
    """素材所在的子目录。成片阶段的单拍可能直接复用草稿文件(final_path 指向 drafts/),
    URL 必须跟着真实目录走,不能一律当成 shots/。"""
    parts = str(value).replace("\\", "/").rsplit("/", 2)
    kind = parts[-2] if len(parts) >= 2 else ""
    return kind if kind in ("drafts", "shots") else "shots"


templates.env.filters["hhmmss"] = _fmt_hhmmss
templates.env.filters["datetime"] = _fmt_datetime
templates.env.filters["basename"] = _basename
templates.env.filters["media_kind"] = _media_kind

#: 后台循环间隔(秒):多久扫一次项目表,推进处于渲染 / 合成态的项目。
SCHEDULER_INTERVAL = 20

#: 后台循环会自动推进的状态。缺任何一个都表现为"流水线静静卡住"——
#: 不会报错、不会告警,镜头只是永远停在 pending。DRAFT_RENDERING 曾被漏掉,
#: 导致草稿档从来不会自己跑起来。
AUTO_ADVANCE_STATES = frozenset({
    str(State.DRAFT_RENDERING),
    str(State.FINAL_RENDERING),
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
        except orchestrator.PipelineError as exc:
            # 配置错、lint 没过 —— 再跑一百遍也是这个结果,判死
            self._failures[pid] = MAX_CONSECUTIVE_FAILURES
            log.error("推进 %s 失败(不可恢复):%s", pid, exc)
            db.update_project(pid, state=str(State.FAILED),
                              state_reason=f"后台推进失败:{exc}")
            db.add_event(pid, str(State.FAILED), src=state,
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
                pass
        self._workers.clear()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        # 不 wait:在途的那一拍让它跑完(它写的是自己那一拍的文件)
        pool, self._pool = self._pool, None
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)


scheduler = Scheduler()

HEALTH: dict[str, Any] = {}
# 侧边栏每页都要显示健康状态和套餐到期,做成模板全局,免得每个路由各传一遍
templates.env.globals.update(health=HEALTH, plan_expires_on=config.PLAN_EXPIRES_ON,
                             static_v=int(time.time()))  # 静态资源缓存戳:每次重启换新,改完样式不用强刷


@asynccontextmanager
async def lifespan(app: FastAPI):
    config.ensure_dirs()
    db.init_db()
    # 日志:控制台 + data/logs/aimovice.log(按天轮转)。此前整个项目
    # 一次也没用过 logging,排障只能看 serve.ps1 重定向的 stdout 文本。
    logging_setup.setup()
    # 清掉上次的临时物(data/_probe、data/_tmp 与散落的 _tmp_*.mp4)。
    # 它们是纯中间产物,却一直没有清理入口,已经积了 40 多 MB。
    try:
        maintenance.prune_temp()
    except Exception:  # noqa: BLE001 - 清理失败不该挡住服务启动
        log.warning("清理临时目录失败", exc_info=True)

    # ---- 启动自检 ----
    ffmpeg_ok, ffmpeg_msg = config.ffmpeg_status()
    cli_ok, cli_msg = cli.health_check()
    motion_ok, motion_msg = config.motion_status()
    deepseek = DeepSeekProvider()
    minimax = MiniMaxProvider()
    HEALTH.update(
        {
            "ffmpeg": {"ok": ffmpeg_ok, "message": ffmpeg_msg},
            "connector": {"ok": cli_ok, "message": cli_msg},
            "motion": {"ok": motion_ok, "message": motion_msg},
            "llm": {
                "deepseek": deepseek.available(),
                "minimax": minimax.available(),
                "active": "minimax" if minimax.available()
                          else ("deepseek" if deepseek.available() else None),
            },
            "plan_expires_on": config.PLAN_EXPIRES_ON,
            "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
    )

    scheduler.start()
    # 分发侧的后台循环:发布任务、指标抓取。
    # 都是守护线程,进程退出即结束,不挡 Web。
    publish_accounts.import_from_file()
    publish_service.start()
    metrics_worker.start()
    yield
    await scheduler.stop()
    publish_service.stop()
    metrics_worker.stop()


app = FastAPI(title="aimovice", version="0.1.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "web" / "static")), name="static")


@app.exception_handler(ProjectBusy)
async def _project_busy(request: Request, exc: ProjectBusy) -> JSONResponse:
    """项目正被别的执行流推进 → 409。

    这是**预期内**的并发(后台调度器与用户手动操作撞上了),不是错误。
    统一在这里转,免得每个写接口各写一遍 try/except。
    """
    return JSONResponse(status_code=409, content={"detail": str(exc)})


# --------------------------------------------------------------------------
# 视图
# --------------------------------------------------------------------------
# 视图模型在 app/views.py(纯函数,不碰 HTTP)。这里只留路由与调度。


# --------------------------------------------------------------------------
# 页面
# --------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    rows = db.list_projects()
    projects = [build_summary(r) for r in rows]
    # 需要人处理的项目(配额暂停 / 失败)顶到最上面 —— 卡住的项目不该要人
    # 自己去列表里翻。参见 views.NEEDS_ATTENTION。
    attention = needs_attention(rows)
    cats = available_categories()
    # 方向下拉按当前品类联动:默认展示第一个品类的方向
    active = cats[0] if cats else {"themes": [], "default_theme": ""}
    # ⚠️ 新版 Starlette 的签名为 TemplateResponse(request, name, context),
    #    旧的 (name, context) 形式会把 context 误当成 name,报 "unhashable type: dict"。
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "request": request,
            "projects": projects,
            "attention": attention,
            "categories": cats,
            "themes": active.get("themes", []),
            "default_theme": active.get("default_theme", ""),
            "health": HEALTH,
            "durations": config.SUPPORTED_DURATIONS,
            "plan_expires_on": config.PLAN_EXPIRES_ON,
        },
    )


#: 解说轨每拍的墙钟(秒),含 TTS 分摊与最终合成。YU7 实测 27 拍 5.4 分钟 ≈ 12 秒/拍
EXPLAINER_SECONDS_PER_BEAT = 12
#: 按画幅的墙钟系数。竖屏与横屏像素数相同,模糊铺底多一层滤镜,先按 1.0 记,实测后再调
RENDER_FACTOR = {config.LANDSCAPE: 1.0, config.PORTRAIT: 1.0}


def _explainer_minutes(beats: int, aspect_value: str = config.LANDSCAPE) -> int:
    factor = RENDER_FACTOR.get(aspect_value, 1.0)
    return max(1, math.ceil(beats * EXPLAINER_SECONDS_PER_BEAT * factor / 60))


@app.get("/project/{project_id}", response_class=HTMLResponse)
def project_page(request: Request, project_id: str):
    try:
        detail = build_detail(project_id, stage="final")
        if not detail.shots:
            detail = build_detail(project_id, stage="draft")
    except ProjectNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    profile = load_profile(detail.category)
    w, h = aspect.size_for(detail.aspect)
    beats = len(detail.shots) or detail.shot_count
    estimate = costs.estimate_explainer(detail.duration)
    facts_report = db.read_json(project_id, "facts_report.json", {}) or {}
    roster = []
    if detail.subjects:
        # 横评:每台车一个池子,费用按车累加;门禁 A 按车看事实数和素材秒数
        per = facts_report.get("per_subject") or {}
        thin = set(facts_report.get("subjects_thin") or [])
        vlm = 0
        for s in detail.subjects:
            st = library.Library.load(s.name).stats()
            vlm += st["vlm_calls"]
            got = per.get(s.name) or {}
            roster.append({"name": s.name, "group": s.group, "facts": got.get("facts"),
                           "product": got.get("product"), "thin": s.name in thin,
                           "usable_seconds": st["usable_seconds"],
                           "building": library.is_building(s.name)})
        lib = {"vlm_calls": vlm}
    else:
        lib = library.Library.load(detail.subject).stats()
    estimate["library_vlm_calls"] = lib["vlm_calls"]
    estimate["library"] = costs.estimate_library(lib["vlm_calls"])
    wall_clock = {"render_minutes": _explainer_minutes(beats, detail.aspect)}
    return templates.TemplateResponse(
        request,
        "project.html",
        {
            "request": request,
            "p": detail,
            "health": HEALTH,
            "lint": db.read_json(project_id, "lint.json", []) or [],
            "facts_report": facts_report,
            "roster": roster,
            "render": db.read_json(project_id, "render.json", {}) or {},
            "events": db.get_events(project_id, limit=40),
            "wall_clock": wall_clock,
            "estimate": estimate,
            "frame": f"{w} × {h}",
            "forbidden_terms": list(profile.forbidden_terms),
        },
    )


@app.get("/project/{project_id}/logs", response_class=HTMLResponse)
def project_logs(request: Request, project_id: str):
    return templates.TemplateResponse(
        request,
        "logs.html",
        {"request": request, "project_id": project_id,
         "events": db.get_events(project_id, limit=200)},
    )


# --------------------------------------------------------------------------
# 素材库:每个选题一个素材池,只读浏览
# --------------------------------------------------------------------------
def _pool_dirs() -> list[Path]:
    root = config.DATA_DIR / library.LIB_ROOT_NAME
    if not root.is_dir():
        return []
    return [d for d in root.iterdir() if (d / "library.json").is_file()]


def _load_pool(slug: str) -> library.Library:
    """按目录名取池子。目录名是 slug,真正的选题名记在 library.json 里。"""
    path = config.DATA_DIR / library.LIB_ROOT_NAME / slug / "library.json"
    if "/" in slug or "\\" in slug or slug.startswith(".") or not path.is_file():
        raise HTTPException(status_code=404, detail="素材池不存在")
    try:
        subject = json.loads(path.read_text(encoding="utf-8")).get("subject") or slug
    except (OSError, json.JSONDecodeError):
        raise HTTPException(status_code=404, detail="素材池已损坏")
    lib = library.Library.load(subject)
    if lib.dir.name != slug:
        raise HTTPException(status_code=404, detail="素材池不存在")
    return lib


def _pool_cover(lib: library.Library) -> str:
    item = next((i for i in lib.items if i.usable and i.frame), None) \
        or next((i for i in lib.items if i.frame), None)
    return item.id if item else ""


@app.get("/library", response_class=HTMLResponse)
def library_index(request: Request):
    pools = []
    for d in _pool_dirs():
        try:
            lib = _load_pool(d.name)
        except HTTPException:
            continue
        pools.append({"slug": d.name, "subject": lib.subject, "category": lib.category,
                      "updated_at": lib.updated_at, "cover": _pool_cover(lib),
                      "building": library.is_building(lib.subject), "stats": lib.stats()})
    pools.sort(key=lambda p: -p["updated_at"])
    return templates.TemplateResponse(request, "library.html", {"request": request, "pools": pools})


@app.get("/library/{slug}", response_class=HTMLResponse)
def library_pool(request: Request, slug: str):
    lib = _load_pool(slug)
    items = [i for i in lib.items if i.frame]
    # 可用的排前面,同类里视频按时长
    items.sort(key=lambda i: (not i.usable, i.kind != "video", -i.length))
    return templates.TemplateResponse(
        request,
        "library_pool.html",
        {"request": request, "slug": slug, "lib": lib, "items": items,
         "building": library.is_building(lib.subject), "stats": lib.stats()},
    )


@app.get("/library/{slug}/frame/{item_id}")
def library_frame(slug: str, item_id: str):
    lib = _load_pool(slug)
    item = next((i for i in lib.items if i.id == item_id), None)
    if not item or not item.frame:
        raise HTTPException(status_code=404, detail="没有这一帧")
    path = Path(item.frame).resolve()
    # 帧路径是绝对路径存的,只放行池子目录里面的文件
    if not path.is_file() or lib.dir.resolve() not in path.parents:
        raise HTTPException(status_code=404, detail="没有这一帧")
    return FileResponse(path, headers={"Cache-Control": "public, max-age=86400"})


@app.delete("/api/library/{slug}")
def delete_library_pool(slug: str, force: bool = False):
    """驱逐一个素材池(删掉它的目录)。

    素材池是跨项目共享的:还有项目在用这个池时会拒绝,除非 `force=true`。
    重建一个池要重新下载 + 视觉打标签,很贵 —— 所以别把别的项目正用着的
    池子删了。
    """
    lib = _load_pool(slug)
    out = maintenance.evict_pool(lib.subject, force=force)
    if not out.get("ok"):
        raise HTTPException(409, str(out.get("reason") or "驱逐失败"))
    return out


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------
@app.get("/api/health")
def health():
    out = dict(HEALTH)
    # 外部能力调用统计:之前完全不可见,只能靠猜额度烧在哪
    out["provider_calls"] = cli.call_stats()
    # 调度器当前盯着的项目与失败次数
    out["scheduler"] = {
        "concurrency": config.PIPELINE_CONCURRENCY,
        "interval": SCHEDULER_INTERVAL,
        "failures": dict(scheduler._failures),
        "busy_projects": locks_busy_ids(),
        "queued": sorted(scheduler._queued - scheduler._running),
        "running": sorted(scheduler._running),
    }
    # 磁盘水位:data/ 只会涨,并行渲染会成倍放大
    size = maintenance.data_size()
    out["disk"] = {
        "data_bytes": size,
        "data_human": maintenance.human(size),
        "over_budget": size > config.DATA_SIZE_WARN_BYTES,
        "warn_bytes": config.DATA_SIZE_WARN_BYTES,
    }
    return out


@app.get("/api/categories")
def categories():
    return available_categories()


@app.get("/api/estimate")
def estimate(duration: int = 240, category: str = "", aspect: str = config.LANDSCAPE):
    """时长跟着旁白走,这里只给节拍数的估计。"""
    profile = load_profile(category or default_category())
    beats = profile.shots_for(duration)
    if aspect not in config.ASPECTS:
        aspect = config.LANDSCAPE
    return {
        "duration": duration,
        "aspect": aspect,
        "cost": costs.estimate_explainer(duration),
        "wall_clock": {"render_minutes": _explainer_minutes(beats, aspect),
                       "note": f"逐拍渲染约 {EXPLAINER_SECONDS_PER_BEAT} 秒/拍,无生成排队"},
        "shot_count": beats,
    }


#: 热点选题内存缓存:category -> 结果。抓取 + LLM 要几十秒,30 分钟内复用
_TOPICS_CACHE: dict[str, dict[str, Any]] = {}
TOPICS_TTL = 30 * 60


@app.get("/api/topics")
def hot_topics(category: str = "", refresh: int = 0):
    """抓实时热点 -> LLM 归纳单车 / 横评选题。同步函数,FastAPI 放线程池里跑。"""
    from .pipeline import topics
    from .providers import trends

    category = category or default_category()
    cached = _TOPICS_CACHE.get(category)
    if cached and not refresh and time.time() - cached["fetched_at"] < TOPICS_TTL:
        return cached
    profile = load_profile(category)
    # 热点口径按品类走 profile.trends:母婴不调懂车帝,新闻 / 抖音 / B站换母婴词
    items, errors = trends.collect(spec=profile.trends, brands=profile.trend_brands)
    llm = orchestrator._llm()
    if llm is None:
        errors = {**errors, "llm": "没有可用的 LLM,只能列出原始热点"}
    try:
        ideas = topics.suggest(items, profile=profile, llm=llm)
    except Exception as exc:  # noqa: BLE001 - 选题失败仍返回原始热点,让用户自己挑
        ideas, errors = [], {**errors, "llm": f"选题生成失败:{exc}"}
    result = {
        "topics": [t.model_dump() for t in ideas],
        "trends": [i.to_dict() if hasattr(i, "to_dict") else dict(i) for i in items],
        "errors": errors,
        "sources_label": profile.trends.sources_label,
        "fetched_at": time.time(),
    }
    if ideas:
        _TOPICS_CACHE[category] = result
    return result


@app.post("/api/projects")
def create_project(payload: ProjectCreate, background: BackgroundTasks):
    try:
        project_id = orchestrator.create_project(payload)
    except orchestrator.PipelineError as exc:
        raise HTTPException(400, str(exc)) from exc
    # 后台先跑"零生成消耗"的前置阶段(事实→脚本→分镜),直到门禁 A
    background.add_task(_bootstrap, project_id)
    return {"id": project_id, "state": str(State.DRAFT_INTENT)}


def _bootstrap(project_id: str) -> None:
    """在后台把项目推进到门禁 A。不涉及任何生成调用,因此可以自动跑。"""
    try:
        orchestrator.run_until_waiting(project_id, max_steps=6)
    except Exception as exc:  # noqa: BLE001
        db.update_project(project_id, state=str(State.FAILED), state_reason=str(exc))


@app.get("/api/projects")
def list_projects():
    return [build_summary(r) for r in db.list_projects()]


@app.get("/api/projects/{project_id}")
def get_project(project_id: str, stage: str = "final"):
    try:
        return build_detail(project_id, stage)
    except ProjectNotFound as exc:
        raise HTTPException(404, str(exc)) from exc


@app.delete("/api/projects/{project_id}")
def delete_project(project_id: str, pool: bool = False):
    """删项目。

    `pool=true` 连这辆车（或这几台车）的**素材池**一起删 —— 池子是跨项目
    共享的,删之前会先看还有谁在用,有别人在用就拒绝。
    """
    row = db.get_project(project_id)
    if not row:
        raise HTTPException(404, "项目不存在")
    import shutil

    db.delete_shots(project_id, "draft")
    db.delete_shots(project_id, "final")
    db.update_project(project_id, state=str(State.CANCELLED))
    shutil.rmtree(config.project_dir(project_id), ignore_errors=True)
    out: dict[str, Any] = {"ok": True}
    if pool:
        names = [row.get("subject") or ""]
        try:
            items = json.loads(row.get("subjects") or "[]")
        except (json.JSONDecodeError, TypeError):
            items = []
        for item in items or []:
            if isinstance(item, dict) and item.get("name"):
                names.append(str(item["name"]))
        evicted = []
        blocked = []
        for name in names:
            if not name:
                continue
            res = maintenance.evict_pool(name, force=True)
            (evicted if res.get("ok") else blocked).append(name)
        out["pools_evicted"] = evicted
        out["pools_blocked"] = blocked
    return out


class AdvanceRequest(BaseModel):
    stage: str | None = None


@app.post("/api/projects/{project_id}/advance")
def advance_project(project_id: str):
    try:
        out = orchestrator.advance(project_id)
    except orchestrator.PipelineError as exc:
        raise HTTPException(400, str(exc)) from exc
    if out.get("busy"):
        # 同一个项目正被后台调度器推进,这一轮就让给它
        raise HTTPException(409, "该项目正在推进中,请稍后重试")
    return out


@app.post("/api/projects/{project_id}/gate")
def confirm_gate(project_id: str, payload: GateRequest):
    try:
        return orchestrator.confirm_gate(project_id, payload)
    except orchestrator.PipelineError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/projects/{project_id}/review")
def review(project_id: str):
    """门禁 B 的审片进度(整片一批)。"""
    row = db.get_project(project_id)
    if not row:
        raise HTTPException(404, "项目不存在")
    return orchestrator.review_progress(project_id)


@app.patch("/api/projects/{project_id}/script")
def patch_script(project_id: str, payload: ScriptPatch):
    project = db.get_project(project_id)
    if not project:
        raise HTTPException(404, "项目不存在")
    segments = payload.segments or [
        ScriptSegment(**d) for d in db.read_json(project_id, "script.json", []) or []
    ]
    try:
        issues = orchestrator.update_script(project_id, segments, payload.title, payload.thesis)
    except orchestrator.PipelineError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "segments": len(segments), "issues": issues}


@app.patch("/api/projects/{project_id}/shots")
def patch_shots(project_id: str, payload: ShotPatch):
    try:
        orchestrator.update_shots(project_id, payload.shots)
    except orchestrator.PipelineError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "shots": len(payload.shots)}


@app.post("/api/projects/{project_id}/shots/{idx}/retry")
def retry_shot(project_id: str, idx: int, payload: RetryRequest | None = None):
    stage = (payload.stage if payload else "final")
    orchestrator.retry(project_id, idx, stage)
    return {"ok": True}


class ConceptStyleRequest(BaseModel):
    style: str


@app.get("/api/concept-styles")
def concept_style_gallery():
    """全部概念动画风格 + 固定示例图(仓库里的静态文件,不生成)。"""
    return concept_styles.gallery()


@app.get("/api/concept-styles/image/{name}")
def concept_style_image(name: str):
    path = concept_styles.sample_image(Path(name).name)
    if not path:
        raise HTTPException(404, "图片不存在")
    return FileResponse(path, media_type="image/jpeg")


@app.get("/api/projects/{project_id}/concept-style")
def concept_style_status(project_id: str):
    if not db.get_project(project_id):
        raise HTTPException(404, "项目不存在")
    return concept_styles.project_status(project_id)


@app.post("/api/projects/{project_id}/concept-style")
def concept_style_set(project_id: str, payload: ConceptStyleRequest):
    try:
        return orchestrator.set_concept_style(project_id, payload.style)
    except orchestrator.PipelineError as exc:
        raise HTTPException(400, str(exc)) from exc


class BudgetRequest(BaseModel):
    budget: float


@app.patch("/api/projects/{project_id}/budget")
def set_project_budget(project_id: str, payload: BudgetRequest):
    """改项目的等效用量上限;项目若正因配额暂停,顺带恢复到暂停前的状态。"""
    try:
        return orchestrator.set_budget(project_id, payload.budget)
    except orchestrator.PipelineError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/project/{project_id}/budget")
async def project_budget_form(project_id: str, request: Request):
    """项目页上的"提额并继续"表单。

    用 urlencoded 表单提交(普通 HTML form),所以**零 JS**。表单自己
    解析,不用 Starlette 的 `request.form()` —— 那个硬依赖
    python-multipart,而本项目不需要为了一个数字输入框多装一个包。
    """
    body = (await request.body()).decode("utf-8", "replace")
    values = dict(parse_qsl(body))
    try:
        budget = float(values.get("budget") or 0)
    except (TypeError, ValueError):
        budget = 0.0
    try:
        orchestrator.set_budget(project_id, budget)
    except orchestrator.PipelineError as exc:
        raise HTTPException(400, str(exc)) from exc
    return RedirectResponse(f"/project/{project_id}", status_code=303)


@app.get("/api/projects/{project_id}/events")
def events(project_id: str):
    return db.get_events(project_id)


@app.get("/api/projects/{project_id}/status")
def status(project_id: str):
    """轻量轮询端点:只返回 UI 需要的字段。"""
    row = db.get_project(project_id)
    if not row:
        raise HTTPException(404, "项目不存在")
    stage = "draft" if row["state"] == str(State.DRAFT_RENDERING) else "final"
    shots = db.get_shots(project_id, stage)
    counts: dict[str, int] = {}
    for s in shots:
        counts[s["status"]] = counts.get(s["status"], 0) + 1
    return {
        "id": row["id"],
        "state": row["state"],
        "state_reason": row.get("state_reason"),
        "spent_cost": row["spent_cost"],
        "quota_budget": row["quota_budget"],
        "stage": stage,
        "counts": counts,
        "total": len(shots),
        # 并行渲染:这个项目现在是在排队还是在跑
        "queued": project_id in scheduler._queued and project_id not in scheduler._running,
        "running": project_id in scheduler._running,
    }


@app.get("/api/projects/{project_id}/download")
def download(project_id: str):
    path = aspect.output_path(project_id)
    if not path.is_file():
        raise HTTPException(404, "成片尚未生成")
    return FileResponse(path, media_type="video/mp4", filename=path.name)


@app.get("/api/projects/{project_id}/cover")
def cover(project_id: str):
    path = aspect.cover_path(project_id)
    if not path.is_file():
        raise HTTPException(404, "封面尚未生成")
    return FileResponse(path, media_type="image/jpeg")


@app.get("/media/{project_id}/{kind}/{name}")
def media(project_id: str, kind: str, name: str):
    """素材预览。kind ∈ {drafts, shots, audio}。"""
    allowed = {"drafts", "shots", "audio"}
    if kind not in allowed:
        raise HTTPException(400, f"不支持的目录:{kind}")
    safe = Path(name).name  # 阻断路径穿越
    path = config.project_dir(project_id) / kind / safe
    if not path.is_file():
        raise HTTPException(404, "文件不存在")
    return FileResponse(path)


@app.get("/api/render/selfcheck")
def render_selfcheck():
    return render_mod.self_check()


# --------------------------------------------------------------------------
# 分发:一键发布多平台
# --------------------------------------------------------------------------
class AccountPayload(BaseModel):
    """账号(凭据)。`id` 为空 = 新建。"""

    id: str | None = None
    platform: str
    name: str = ""
    auth_kind: str = "cookie"
    #: 凭据。**只写不读** —— 读回来永远是打码的
    credential: dict[str, Any] = {}
    #: 平台特定设置:B站分区 tid、YouTube 频道 ID、可见范围 …
    meta: dict[str, Any] = {}
    enabled: bool = True


class PublishPayload(BaseModel):
    """一次「一键发布」。"""

    #: 要发到哪些账号
    account_ids: list[str] = []
    #: 逐平台的文案覆盖:{"bilibili": {"title": ..., "description": ..., "tags": [...]}}
    overrides: dict[str, dict[str, Any]] | None = None
    #: 已发过的是否重发
    force: bool = False


@app.get("/publish", response_class=HTMLResponse)
def publish_page(request: Request):
    """分发台:账号管理 + 发布队列。"""
    accounts = [publish_accounts.account_view(a)
                for a in publish_accounts.list_accounts()]
    queue = [publish_service.publication_view(r) for r in db.list_publications(limit=200)]
    projects = []
    for row in db.list_projects(limit=100):
        if row["state"] != "DONE":
            continue
        projects.append({
            "id": row["id"], "title": row["title"], "subject": row["subject"],
            "has_output": aspect.output_path(row["id"]).is_file(),
            "aspect": aspect.of(row),
        })
    return templates.TemplateResponse(
        request, "publish.html",
        {"request": request, "accounts": accounts, "queue": queue,
         "projects": projects, "platforms": platforms_info()},
    )


@app.get("/analytics", response_class=HTMLResponse)
def analytics_page(request: Request):
    """数据看板:作品指标 + 趋势。"""
    data = metrics_store.overview()
    return templates.TemplateResponse(
        request, "analytics.html",
        {"request": request, "items": data["items"], "by_platform": data["by_platform"],
         "by_aspect": data["by_aspect"], "platforms": platforms_info()},
    )


@app.get("/api/publish/platforms")
def publish_platforms():
    return platforms_info()


@app.get("/api/publish/accounts")
def publish_accounts_list():
    return [publish_accounts.account_view(a) for a in publish_accounts.list_accounts()]


@app.get("/api/publish/queue")
def publish_queue():
    """发布队列(轮询用)。"""
    return [publish_service.publication_view(r) for r in db.list_publications(limit=200)]


@app.post("/api/publish/accounts")
def publish_account_save(payload: AccountPayload):
    try:
        account = publish_accounts.save_account(
            platform=payload.platform, name=payload.name or payload.platform,
            credential=payload.credential, meta=payload.meta,
            auth_kind=payload.auth_kind, account_id=payload.id, enabled=payload.enabled)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return publish_accounts.account_view(account)


@app.delete("/api/publish/accounts/{account_id}")
def publish_account_delete(account_id: str):
    if not db.get_account(account_id):
        raise HTTPException(404, "账号不存在")
    publish_accounts.delete_account(account_id)
    return {"ok": True}


@app.post("/api/publish/accounts/{account_id}/check")
def publish_account_check(account_id: str):
    """连通性自检:登录态还在吗、缺哪个权限。"""
    account = publish_accounts.get_account(account_id)
    if not account:
        raise HTTPException(404, "账号不存在")
    from .publish.registry import adapter_for

    try:
        status = adapter_for(account.platform).check_account(account)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": f"{type(exc).__name__}: {exc}", "hint": ""}
    return {"ok": status.ok, "message": status.message, "hint": status.hint,
            "detail": status.detail}


@app.post("/api/publish/login/{platform}")
def publish_login(platform: str):
    """打开浏览器扫码登录(小红书 / 视频号)。后台线程跑,不占住请求。"""
    urls = {"xiaohongshu": "https://creator.xiaohongshu.com/login",
            "channels": "https://channels.weixin.qq.com/login",
            "douyin": "https://creator.douyin.com/"}
    if platform not in urls:
        raise HTTPException(400, f"这个平台不用扫码登录:{platform}")

    def _run() -> None:
        try:
            publish_browser.login(platform, urls[platform])
        except Exception:  # noqa: BLE001 - 登录失败由用户看浏览器
            pass

    import threading

    threading.Thread(target=_run, name=f"login-{platform}", daemon=True).start()
    return {"ok": True, "message": "浏览器已打开,扫码登录后关掉窗口即可"}


@app.get("/api/projects/{project_id}/publish")
def project_publish_records(project_id: str):
    if not db.get_project(project_id):
        raise HTTPException(404, "项目不存在")
    return publish_service.list_for_project(project_id)


@app.get("/api/projects/{project_id}/publish/preview")
def project_publish_preview(project_id: str):
    """发布前的预览:各平台的文案、画幅、产物是否就绪。"""
    project = db.get_project(project_id)
    if not project:
        raise HTTPException(404, "项目不存在")
    ctx = publish_service.build_context(project_id)
    accounts = publish_accounts.list_accounts(only_enabled=True)
    by_platform: dict[str, dict[str, Any]] = {}
    for account in accounts:
        if account.platform in by_platform:
            continue
        meta = publish_metadata.metadata_for(account.platform, ctx)
        rule = PLATFORMS[account.platform]
        by_platform[account.platform] = {
            **meta,
            "accounts": [{"id": a.id, "name": a.name}
                         for a in accounts if a.platform == account.platform],
            "aspect": rule.aspect,
            # 平台推荐画幅与项目不一致时的提示(照样能发)
            "aspect_warning": publish_service.aspect_warning(account.platform, aspect.of(project)),
        }
    return {
        "project_id": project_id,
        "title": project["title"],
        "has_output": aspect.output_path(project_id).is_file(),
        "aspect": aspect.of(project),
        "aspect_label": aspect.label(aspect.of(project)),
        "platforms": by_platform,
        "published": publish_service.list_for_project(project_id),
    }


@app.post("/api/projects/{project_id}/publish")
def project_publish(project_id: str, payload: PublishPayload):
    """一键发布:给每个 (账号 × 画幅) 建一条发布任务。"""
    if not db.get_project(project_id):
        raise HTTPException(404, "项目不存在")
    if not payload.account_ids:
        raise HTTPException(400, "没有选平台")
    try:
        result = publish_service.enqueue(
            project_id, account_ids=payload.account_ids,
            overrides=payload.overrides, force=payload.force)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {
        "created": [publish_service.publication_view(r) for r in result["created"]],
        "skipped": [publish_service.publication_view(r) for r in result["skipped"]],
        "warnings": result["warnings"],
    }


@app.post("/api/publish/{pub_id}/retry")
def publish_retry(pub_id: str):
    """重试一条失败的任务。"""
    if not db.get_publication(pub_id):
        raise HTTPException(404, "任务不存在")
    try:
        return publish_service.retry(pub_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


# --------------------------------------------------------------------------
# 分发:数据看板
# --------------------------------------------------------------------------
@app.get("/api/metrics/overview")
def metrics_overview():
    return metrics_store.overview()


@app.get("/api/metrics/publications/{pub_id}")
def metrics_publication(pub_id: str):
    if not db.get_publication(pub_id):
        raise HTTPException(404, "作品不存在")
    return {"trend": metrics_store.trend(pub_id)}


@app.get("/api/metrics/platform/{platform}")
def metrics_platform(platform: str, days: int = 30):
    return {"series": metrics_store.platform_series(platform, days=days)}


@app.get("/favicon.ico")
def favicon():
    return RedirectResponse("/static/favicon.svg")
